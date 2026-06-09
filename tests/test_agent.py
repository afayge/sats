from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.agent import AgentExecutionPolicy, AgentObservation, AgentPlan, TradeIntent, run_agent_once
from sats.agent.command_runner import AgentCommandRunner
from sats.agent.date_policy import normalize_agent_date, resolve_agent_time_context, sanitize_agent_tool_arguments
from sats.agent.planner import build_agent_plan
from sats.agent.python_runtime import RestrictedPythonRuntime
from sats.agent.synthesis import save_agent_report, synthesize_agent_result, _evidence_digest
from sats.agent.tools import AgentToolContext, build_default_tool_registry
from sats.agent.trading import AgentTradingExecutor
from sats.cli import main
from sats.data.resolver import MarketDataResolver
from sats.llm import LLMResponse
from sats.repl import ReplState, handle_repl_line, help_text
from sats.skills import Skill
from sats.storage.duckdb import DuckDBStorage
from sats.trading.models import BrokerAsset, BrokerPosition, OrderResult


class FakeDailyProvider:
    def __init__(self) -> None:
        self.daily_calls = 0
        self.quote_calls = 0

    def load_historical_daily_klines(self, symbols, *, start_date=None, end_date=None, storage=None):
        self.daily_calls += 1
        rows = []
        for symbol in symbols:
            for trade_date in ("20260518", "20260519", "20260520"):
                rows.append(
                    {
                        "ts_code": symbol,
                        "trade_date": trade_date,
                        "open": 10.0,
                        "high": 11.0,
                        "low": 9.5,
                        "close": 10.5,
                        "vol": 1000,
                        "amount": 10000,
                    }
                )
        frame = pd.DataFrame(rows)
        frame.attrs["data_source"] = "fake_provider_daily"
        return frame

    def load_realtime_quotes(self, *, symbols=None, universe_id=None):
        self.quote_calls += 1
        frame = pd.DataFrame([{"ts_code": symbols[0], "price": 12.3, "volume": 1000, "amount": 12300}])
        frame.attrs["data_source"] = "fake_provider_quote"
        return frame

    def load_index_daily(self, index_codes, *, start_date, end_date):
        return pd.DataFrame()

    def load_historical_minute_klines(self, symbols, *, period="1m", start_time=None, end_time=None, count=None):
        return pd.DataFrame()

    def load_realtime_minute_klines(self, symbols, *, period="1m", count=None):
        return pd.DataFrame()


class FakePlanLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"objective":"run command","success_criteria":["command succeeds"],'
                '"steps":[{"step_id":"cmd","kind":"command","title":"list skills","command":["skills"]},'
                '{"step_id":"final","kind":"final","title":"summary"}]}'
            )
        )


class FakeForecastDateLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"objective":"forecast","steps":['
                '{"step_id":"market","kind":"tool","title":"market","tool_name":"research.market_context",'
                '"arguments":{"trade_date":"2024-10-10","horizon":"today"}},'
                '{"step_id":"stock","kind":"tool","title":"stock","tool_name":"research.stock_context",'
                '"arguments":{"symbols":["002436"],"trade_date":"20241010"}},'
                '{"step_id":"final","kind":"final","title":"summary"}]}'
            )
        )


class FakeStockOnlyLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"objective":"stock forecast","steps":['
                '{"step_id":"stock","kind":"tool","title":"stock","tool_name":"research.stock_context",'
                '"arguments":{"symbols":["002436"],"trade_date":"20260606"}},'
                '{"step_id":"final","kind":"final","title":"summary"}]}'
            )
        )


class FakeMarketOnlyLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"objective":"market forecast","steps":['
                '{"step_id":"market","kind":"tool","title":"market","tool_name":"research.market_context",'
                '"arguments":{"horizons":["next_week"]}},'
                '{"step_id":"final","kind":"final","title":"summary"}]}'
            )
        )


class FakeSynthesisLLM:
    last_messages = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        FakeSynthesisLLM.last_messages = list(messages)
        return LLMResponse(content="## 核心结论\n本周指数震荡偏强，下周关注量能和上证指数关键支撑。仅供研究，不构成投资建议。")


class FakeClient:
    provider = "qmt"
    account_id = "acct"

    def __init__(self) -> None:
        self.requests = []

    def asset(self):
        return BrokerAsset(available_cash=50000, total_asset=100000, account_id="acct")

    def positions(self):
        return [BrokerPosition(ts_code="000001.SZ", quantity=200, available_quantity=100)]

    def place_order(self, request):
        self.requests.append(request)
        return OrderResult(sats_order_id="sats-1", broker_order_id="qmt-1", status="submitted", message="ok", request=request.to_dict())


class AgentTest(unittest.TestCase):
    def _rich_stock_observations(self) -> tuple[AgentObservation, ...]:
        return (
            AgentObservation(
                step_id="quote",
                kind="tool",
                status="done",
                content="realtime_quote: 1 rows",
                payload={
                    "tool_name": "data.realtime_quotes",
                    "data_names": ["realtime_quote"],
                    "result": {
                        "payload": {
                            "sample": [{"ts_code": "002436.SZ", "price": 40.87, "pct_chg": 2.64, "volume": 18436000, "fetched_at": "2026-06-04 15:00:00"}],
                            "provenance": [{"dataset": "realtime_quote", "source": "duckdb_cache"}],
                        },
                        "data_names": ["realtime_quote"],
                    },
                },
            ),
            AgentObservation(
                step_id="stock_context",
                kind="tool",
                status="done",
                content="stock context",
                payload={
                    "tool_name": "research.stock_context",
                    "data_names": ["stock_context"],
                    "result": {
                        "payload": {
                            "stock_context": {
                                "stocks": [
                                    {
                                        "ts_code": "002436.SZ",
                                        "name": "兴森科技",
                                        "trade_date": "20260604",
                                        "daily_tail": [{"trade_date": "20260604", "open": 39.51, "high": 42.5, "low": 39.5, "close": 40.87, "vol": 18436000}],
                                        "indicator_result": {"close": 40.87, "ma5": 40.04, "ma20": 35.78, "rsi6": 69.0},
                                    }
                                ]
                            }
                        },
                        "data_names": ["stock_context"],
                    },
                },
            ),
            AgentObservation(
                step_id="indicators",
                kind="tool",
                status="done",
                content="indicators",
                payload={
                    "tool_name": "research.internal_analysis",
                    "data_names": ["internal_analysis"],
                    "result": {"payload": {"analysis": {"kind": "indicators", "trade_date": "20260604", "indicators": [{"ts_code": "002436.SZ", "close": 40.87, "ma5": 40.04}]}}},
                },
            ),
            AgentObservation(
                step_id="analyze_signals",
                kind="tool",
                status="done",
                content="signals",
                payload={
                    "tool_name": "research.internal_analysis",
                    "data_names": ["internal_analysis"],
                    "result": {
                        "payload": {
                            "analysis": {
                                "kind": "analyze_signals",
                                "trade_date": "20260604",
                                "results": [
                                    {
                                        "ts_code": "002436.SZ",
                                        "name": "兴森科技",
                                        "close": 40.87,
                                        "score": 73.5,
                                        "decision": "买入观察",
                                        "trend": "短线偏强",
                                        "selected_signals": ["short_up"],
                                        "key_levels": {"support": 40.04, "resistance": 42.48},
                                        "events": [{"signal_id": "ma_dragon_sea_kline", "label": "蛟龙出海买入点", "category": "ma_kline", "side": "buy", "score": 80}],
                                    }
                                ],
                            }
                        }
                    },
                },
            ),
            AgentObservation(
                step_id="factor_summary",
                kind="tool",
                status="done",
                content="factor",
                payload={"tool_name": "research.internal_analysis", "data_names": ["internal_analysis"], "result": {"payload": {"analysis": {"kind": "factor_summary", "profile": "balanced", "summary": "成长和动量因子偏强"}}}},
            ),
        )

    def _rich_market_observations(self) -> tuple[AgentObservation, ...]:
        return (
            AgentObservation(
                step_id="market",
                kind="tool",
                status="done",
                content="market",
                payload={
                    "tool_name": "research.market_context",
                    "result": {
                        "payload": {
                            "market_context": {
                                "trade_date": "20260605",
                                "requested_dimensions": ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors"],
                                "indices": [
                                    {
                                        "ts_code": "000001.SH",
                                        "name": "上证指数",
                                        "trade_date": "20260605",
                                        "latest": {"close": 4027.736, "pct_chg": -0.74},
                                        "technical": {"ma": {"ma5": 4058.2}},
                                        "missing_fields": [],
                                    },
                                    {
                                        "ts_code": "399001.SZ",
                                        "name": "深证成指",
                                        "trade_date": "20260605",
                                        "latest": {"close": 12888.12, "pct_chg": -0.52},
                                        "technical": {"ma": {"ma5": 13012.5}},
                                        "missing_fields": [],
                                    },
                                    {
                                        "ts_code": "399006.SZ",
                                        "name": "创业板指",
                                        "trade_date": "20260605",
                                        "latest": {"close": 2620.3, "pct_chg": 0.21},
                                        "technical": {"ma": {"ma5": 2608.4}},
                                        "missing_fields": [],
                                    },
                                ],
                                "market_breadth": {"advancing_count": 3276, "declining_count": 2114},
                                "limit_sentiment": {"emotion_stage": "冰点", "limit_up_count": 116, "limit_down_count": 23},
                                "hot_sector_context": {
                                    "hot_industries": [{"name": "电力", "sector_type": "industry", "latest_pct_chg": 1.2, "heat_score": 8.0}],
                                    "hot_concepts": [{"name": "AI算力", "sector_type": "concept", "latest_pct_chg": 2.0, "heat_score": 15.0}],
                                    "missing_fields": [],
                                    "data_sources": {"sector_daily": "fake"},
                                },
                            }
                        }
                    },
                },
            ),
        )

    def test_synthesis_returns_detailed_analysis_without_step_log(self) -> None:
        skill = Skill(
            id="sats-market-assistant",
            name="sats-market-assistant",
            description="大盘、宽度、情绪和板块轮动分析方法。",
            triggers=("大盘",),
            content="从指数趋势、市场宽度和情绪确认。",
            path=Path("skills/sats-market-assistant/SKILL.md"),
            category="analysis",
        )
        plan = AgentPlan(objective="分析这周大盘走势，预测下周大盘走势")
        observations = (
            AgentObservation(
                step_id="market",
                kind="tool",
                status="done",
                content='{"status":"ok","market_context":{"trade_date":"20260605"}}',
                payload={
                    "tool_name": "research.market_context",
                    "data_names": ["market_context"],
                    "result": {
                        "payload": {
                            "market_context": {
                                "trade_date": "20260605",
                                "core_indices": [{"ts_code": "000001.SH", "close": 4027.736, "pct_chg": 0.42}],
                            },
                            "provenance": [{"dataset": "index_daily", "source": "duckdb_cache"}],
                        },
                        "data_names": ["market_context"],
                    },
                },
            ),
        )

        result = synthesize_agent_result(
            message="分析这周大盘走势，预测下周大盘走势",
            plan=plan,
            observations=observations,
            skills=(skill,),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=FakeSynthesisLLM,
        )
        context_text = "\n".join(str(item.get("content") or "") for item in FakeSynthesisLLM.last_messages)

        self.assertTrue(result.used_llm)
        self.assertIn("核心结论", result.content)
        self.assertNotIn("Agent objective", result.content)
        self.assertNotIn("[done]", result.content)
        self.assertIn("大盘、宽度、情绪", context_text)
        self.assertIn("duckdb_cache", context_text)

    def test_stock_synthesis_context_uses_rich_report_style(self) -> None:
        result = synthesize_agent_result(
            message="怎么评价兴森科技，预测未来几天走势",
            plan=AgentPlan(objective="评价兴森科技"),
            observations=self._rich_stock_observations(),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=FakeSynthesisLLM,
        )
        context_text = "\n".join(str(item.get("content") or "") for item in FakeSynthesisLLM.last_messages)

        self.assertTrue(result.used_llm)
        self.assertIn("stock_analysis", context_text)
        self.assertIn("今日/近期盘面表", context_text)
        self.assertIn("关键变化对比", context_text)
        self.assertIn("Analyze 信号表", context_text)
        self.assertIn("关键价位", context_text)
        self.assertIn("综合判断", context_text)
        self.assertIn("情景推演表", context_text)
        self.assertIn("002436.SZ", context_text)
        self.assertIn("ma_dragon_sea_kline", context_text)

    def test_agent_digest_keeps_all_explicit_indicator_symbols(self) -> None:
        symbols = [f"300{i:03d}.SZ" for i in range(12)]
        rows = [{"ts_code": symbol, "close": 10 + i, "ma5": 9 + i} for i, symbol in enumerate(symbols)]
        observations = (
            AgentObservation(
                step_id="indicators",
                kind="tool",
                status="done",
                content="indicators",
                payload={
                    "tool_name": "research.internal_analysis",
                    "arguments": {"kind": "indicators", "symbols": symbols, "trade_date": "20260609"},
                    "result": {"payload": {"analysis": {"kind": "indicators", "trade_date": "20260609", "results": rows}}},
                },
            ),
        )

        digest = _evidence_digest(observations)

        self.assertEqual([item["ts_code"] for item in digest["indicators"]], symbols)
        self.assertEqual(digest["indicator_coverage"]["requested_count"], 12)
        self.assertEqual(digest["indicator_coverage"]["included_count"], 12)
        self.assertEqual(digest["indicator_coverage"]["omitted_count"], 0)

    def test_agent_digest_keeps_generic_indicator_limit_without_explicit_symbols(self) -> None:
        rows = [{"ts_code": f"300{i:03d}.SZ", "close": 10 + i, "ma5": 9 + i} for i in range(12)]
        observations = (
            AgentObservation(
                step_id="indicators",
                kind="tool",
                status="done",
                content="indicators",
                payload={
                    "tool_name": "research.internal_analysis",
                    "result": {"payload": {"analysis": {"kind": "indicators", "trade_date": "20260609", "results": rows}}},
                },
            ),
        )

        digest = _evidence_digest(observations)

        self.assertEqual(len(digest["indicators"]), 8)
        self.assertEqual([item["ts_code"] for item in digest["indicators"]], [f"300{i:03d}.SZ" for i in range(8)])
        self.assertEqual(digest["indicator_coverage"], {})

    def test_agent_digest_keeps_all_explicit_stock_context_symbols(self) -> None:
        symbols = [f"300{i:03d}.SZ" for i in range(12)]
        stocks = [
            {
                "ts_code": symbol,
                "name": f"股票{i}",
                "trade_date": "20260609",
                "daily_tail": [{"trade_date": "20260609", "close": 10 + i, "pct_chg": 1.0}],
                "indicator_result": {"close": 10 + i, "ma5": 9 + i, "rsi6": 30 + i},
            }
            for i, symbol in enumerate(symbols)
        ]
        observations = (
            AgentObservation(
                step_id="stock_context",
                kind="tool",
                status="done",
                content="stock context",
                payload={
                    "tool_name": "research.stock_context",
                    "arguments": {"symbols": symbols, "trade_date": "20260609"},
                    "result": {"payload": {"stock_context": {"stocks": stocks}}},
                },
            ),
        )

        digest = _evidence_digest(observations)

        self.assertEqual([item["ts_code"] for item in digest["stock_context"]], symbols)

    def test_synthesis_context_includes_full_auto_loaded_skill_content(self) -> None:
        skill = Skill(
            id="custom-stock-skill",
            name="custom-stock-skill",
            description="自定义个股分析方法",
            triggers=(),
            content="FULL_SKILL_BODY: 只使用 observations/provenance 中的真实行情。",
            path=Path("skills/custom/SKILL.md"),
            category="analysis",
            applies_to=("stock_analysis",),
            evidence=("stock_context", "analyze_signals"),
            auto_load="full",
            priority=99,
        )

        synthesize_agent_result(
            message="怎么评价兴森科技，预测未来几天走势",
            plan=AgentPlan(objective="评价兴森科技"),
            observations=self._rich_stock_observations(),
            skills=(skill,),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=FakeSynthesisLLM,
        )
        context_text = "\n".join(str(item.get("content") or "") for item in FakeSynthesisLLM.last_messages)

        self.assertIn("mode=full", context_text)
        self.assertIn("FULL_SKILL_BODY", context_text)

    def test_market_and_discovery_synthesis_use_matching_style_guides(self) -> None:
        synthesize_agent_result(
            message="分析这周大盘走势，预测下周大盘走势",
            plan=AgentPlan(objective="大盘分析"),
            observations=self._rich_market_observations(),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=FakeSynthesisLLM,
        )
        market_context = "\n".join(str(item.get("content") or "") for item in FakeSynthesisLLM.last_messages)
        self.assertIn("market_analysis", market_context)
        self.assertIn("核心指数表", market_context)
        self.assertIn("市场宽度/情绪", market_context)
        self.assertIn("399001.SZ", market_context)
        self.assertIn("399006.SZ", market_context)
        self.assertIn("hot_sector_context", market_context)
        self.assertIn("电力", market_context)
        self.assertIn("AI算力", market_context)
        self.assertNotIn("今日/近期盘面表", market_context)

        discovery_obs = (
            AgentObservation(
                step_id="discover",
                kind="tool",
                status="done",
                content="discover",
                payload={"tool_name": "research.discover_opportunities", "result": {"payload": {"stock_picking_agent": {"candidates": [{"ts_code": "002436.SZ", "score": 73.5}]}}}},
            ),
        )
        synthesize_agent_result(
            message="推荐一些短期上涨的股票",
            plan=AgentPlan(objective="短线机会发现"),
            observations=discovery_obs,
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=FakeSynthesisLLM,
        )
        discovery_context = "\n".join(str(item.get("content") or "") for item in FakeSynthesisLLM.last_messages)
        self.assertIn("discovery", discovery_context)
        self.assertIn("候选排序表", discovery_context)
        self.assertIn("触发条件", discovery_context)
        self.assertIn("失效条件", discovery_context)

    def test_synthesis_fallback_uses_markdown_report_sections(self) -> None:
        result = synthesize_agent_result(
            message="怎么评价兴森科技，预测未来几天走势",
            plan=AgentPlan(objective="评价兴森科技"),
            observations=self._rich_stock_observations(),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=None,
        )

        self.assertFalse(result.used_llm)
        self.assertIn("# 个股走势研究报告", result.content)
        self.assertIn("## 今日/近期盘面", result.content)
        self.assertIn("## 技术指标与 Analyze 信号", result.content)
        self.assertIn("## 关键价位", result.content)
        self.assertIn("002436.SZ", result.content)
        self.assertNotIn("[done]", result.content)
        self.assertNotIn("Agent objective", result.content)

    def test_market_synthesis_fallback_uses_real_indices_and_hot_sectors(self) -> None:
        result = synthesize_agent_result(
            message="分析这周大盘走势，预测下周大盘走势",
            plan=AgentPlan(objective="大盘分析"),
            observations=self._rich_market_observations(),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=None,
        )

        self.assertFalse(result.used_llm)
        self.assertIn("# A股大盘走势研究报告", result.content)
        self.assertIn("399001.SZ", result.content)
        self.assertIn("399006.SZ", result.content)
        self.assertIn("电力", result.content)
        self.assertIn("AI算力", result.content)
        self.assertNotIn("核心指数数据缺失", result.content)

    def test_fallback_planner_stock_analysis_uses_deterministic_stock_components(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 下周走势", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]
        internal_steps = [step for step in plan.steps if step.tool_name == "research.internal_analysis"]
        internal_kinds = [step.arguments.get("kind") for step in internal_steps]

        self.assertEqual(tools, ["research.stock_context", "research.internal_analysis"])
        self.assertEqual(internal_kinds, ["indicators"])
        self.assertNotIn("chat.answer", tools)

    def test_llm_stock_plan_is_augmented_with_indicators_only(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 下周走势", settings=settings, policy=AgentExecutionPolicy(), llm_factory=FakeStockOnlyLLM, tool_registry=registry)
        internal_steps = [step for step in plan.steps if step.tool_name == "research.internal_analysis"]

        self.assertEqual([step.arguments.get("kind") for step in internal_steps], ["indicators"])

    def test_dsa_stock_request_adds_native_dsa_analysis(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用 DSA 分析 002436 买卖点", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        internal_steps = [step for step in plan.steps if step.tool_name == "research.internal_analysis"]

        self.assertIn("native_dsa", [step.arguments.get("kind") for step in internal_steps])
        self.assertNotIn("trade.submit_intent", [step.tool_name for step in plan.steps if step.kind == "tool"])

    def test_dsa_followup_uses_reference_context_symbols(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)
        reference_context = SimpleNamespace(
            symbols=["002436.SZ", "300276.SZ"],
            trade_date="20260605",
            source="agent",
            data_name="上条输出",
            system_message="上一条回答分析了 002436.SZ 和 300276.SZ。",
        )

        plan = build_agent_plan(
            "上面2个股票用DSA进行分析",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
            reference_context=reference_context,
        )
        native_dsa = next(
            step
            for step in plan.steps
            if step.tool_name == "research.internal_analysis" and step.arguments.get("kind") == "native_dsa"
        )

        self.assertEqual(native_dsa.arguments["symbols"], ["002436.SZ", "300276.SZ"])
        self.assertEqual(native_dsa.arguments["trade_date"], "20260605")

    def test_planner_preserves_explicit_signal_selection(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 ma_kline 信号", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        analyze_step = next(step for step in plan.steps if step.tool_name == "research.internal_analysis" and step.arguments.get("kind") == "analyze_signals")

        self.assertEqual(analyze_step.arguments["signals"], "ma_kline")

    def test_fallback_planner_market_analysis_uses_market_tools_without_factor_pick(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析这周大盘走势，预测下周大盘走势", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]

        self.assertEqual(tools, ["research.market_context"])
        self.assertNotIn("analyze_signals", [step.arguments.get("kind") for step in plan.steps if step.tool_name == "research.internal_analysis"])
        self.assertNotIn("factor.pick", tools)
        self.assertNotIn("chat.answer", tools)
        market_step = next(step for step in plan.steps if step.tool_name == "research.market_context")
        self.assertEqual(market_step.arguments["dimensions"], ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors"])

    def test_fallback_planner_adds_chan_context_for_chan_requests(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用缠论分析 002436", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]

        self.assertEqual(tools, ["research.chan_context", "research.stock_context", "research.internal_analysis"])
        self.assertEqual(
            [step.arguments.get("kind") for step in plan.steps if step.tool_name == "research.internal_analysis"],
            ["indicators"],
        )

    def test_fallback_planner_routes_rule_generation_to_shared_tool(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("新增一个低位放量突破筛选规则", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tool_step = next(step for step in plan.steps if step.kind == "tool")

        self.assertEqual(tool_step.tool_name, "research.rule_generation")
        self.assertEqual(tool_step.arguments["action"], "plan")

    def test_llm_market_plan_without_dimensions_gets_default_hot_sectors(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "分析这周大盘走势，预测下周大盘走势",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=FakeMarketOnlyLLM,
            tool_registry=registry,
        )
        market_step = next(step for step in plan.steps if step.tool_name == "research.market_context")

        self.assertEqual(market_step.arguments["dimensions"], ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors"])

    def test_fallback_planner_factor_pick_does_not_add_analyze_signals(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用因子选股 top20", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)

        self.assertIn("factor.pick", [step.tool_name for step in plan.steps if step.kind == "tool"])
        self.assertNotIn("analyze_signals", [step.arguments.get("kind") for step in plan.steps if step.tool_name == "research.internal_analysis"])

    def test_agent_report_writes_final_synthesis_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact = save_agent_report(
                content="## 核心结论\n这是详细分析正文。",
                message="保存报告",
                settings=SimpleNamespace(project_root=Path(tmp)),
                store=None,
                session_id="default",
                turn_id="turn-1",
            )
            report_path = Path(artifact["path"])

            self.assertTrue(report_path.exists())
            self.assertEqual(report_path.name, "agent_report.md")
            self.assertIn("这是详细分析正文", report_path.read_text(encoding="utf-8"))

    def test_agent_date_policy_normalizes_dates_and_forecast_horizons(self) -> None:
        self.assertEqual(normalize_agent_date("2024-10-10"), "20241010")
        context = resolve_agent_time_context("预测兴森科技下周走势", today="20260606")
        sanitized = sanitize_agent_tool_arguments(
            "research.market_context",
            {"trade_date": "2024-10-10", "horizon": "today"},
            "预测兴森科技下周走势",
            today="20260606",
        )
        bad = sanitize_agent_tool_arguments(
            "data.stock_daily",
            {"symbols": ["000001"], "start_date": "2024-13-40", "end_date": "2024-10-10"},
            "加载日线",
            today="20260606",
        )

        self.assertEqual(context.horizons, ("next_week",))
        self.assertEqual(context.explicit_dates, ())
        self.assertNotIn("trade_date", sanitized.arguments)
        self.assertEqual(sanitized.arguments["horizons"], ["next_week"])
        self.assertIn("日期格式无效", bad.error)

    def test_planner_sanitizes_llm_generated_forecast_dates(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        with patch("sats.agent.date_policy.agent_today", return_value="20260606"):
            plan = build_agent_plan(
                "预测兴森科技下周走势",
                settings=settings,
                policy=AgentExecutionPolicy(),
                llm_factory=FakeForecastDateLLM,
                tool_registry=registry,
            )

        self.assertEqual(plan.steps[0].tool_name, "research.market_context")
        self.assertNotIn("trade_date", plan.steps[0].arguments)
        self.assertEqual(plan.steps[0].arguments["horizons"], ["next_week"])
        self.assertEqual(plan.steps[1].arguments["trade_date"], "20260606")
        self.assertEqual(plan.steps[1].arguments["horizons"], ["next_week"])

    def test_research_stock_context_forecast_uses_daily_only(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("x.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="预测兴森科技下周走势",
        )
        stock_payload = {
            "002436.SZ": {
                "ts_code": "002436.SZ",
                "trade_date": "20260606",
                "daily_tail": [{"trade_date": "20260605", "close": 10.0}],
                "indicator_result": {"ts_code": "002436.SZ"},
                "missing_fields": [],
            }
        }

        with (
            patch("sats.agent.date_policy.agent_today", return_value="20260606"),
            patch("sats.agent.tools.research_tools.ensure_stock_analysis_data", return_value=stock_payload) as ensure,
        ):
            result = registry.execute(
                "research.stock_context",
                {"symbols": ["002436"], "trade_date": "2024-10-10"},
                context,
            )

        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.args[1], "20260606")
        self.assertEqual(ensure.call_args.kwargs["periods"], ())
        self.assertEqual(result.status, "done")
        self.assertEqual(result.payload["stock_context"]["requested_horizons"], ["next_week"])
        self.assertIn("optional_minute_15m", result.payload["stock_context"]["stocks"][0]["missing_fields"])

    def test_research_internal_analysis_analyze_signals_uses_short_up(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("x.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="预测兴森科技下周走势",
        )
        stock_payload = {
            "002436.SZ": {
                "ts_code": "002436.SZ",
                "trade_date": "20260606",
                "daily_tail": [{"trade_date": "20260605", "open": 9.8, "high": 10.2, "low": 9.7, "close": 10.0}],
                "name": "兴森科技",
                "missing_fields": [],
            }
        }
        run = SimpleNamespace(
            trade_date="20260606",
            results=[SimpleNamespace(to_dict=lambda: {"ts_code": "002436.SZ", "selected_signals": ["short_up"]})],
        )

        with (
            patch("sats.agent.tools.research_tools.ensure_stock_analysis_data", return_value=stock_payload),
            patch("sats.agent.tools.research_tools.analyze_signal_inputs", return_value=run) as analyze,
        ):
            result = registry.execute(
                "research.internal_analysis",
                {"kind": "analyze_signals", "symbols": ["002436"], "trade_date": "20260606", "signals": "short_up"},
                context,
            )

        analyze.assert_called_once()
        self.assertEqual(analyze.call_args.kwargs["selected_signals"], "short_up")
        self.assertFalse(analyze.call_args.kwargs["report"])
        self.assertEqual(result.status, "done")
        self.assertEqual(result.payload["analysis"]["kind"], "analyze_signals")

    def test_explicit_intraday_date_is_preserved_for_minute_path(self) -> None:
        sanitized = sanitize_agent_tool_arguments(
            "research.stock_context",
            {"symbols": ["002436"], "trade_date": "2024-10-10"},
            "看兴森科技 2024-10-10 15m",
            today="20260606",
        )

        self.assertEqual(sanitized.arguments["trade_date"], "20241010")
        self.assertEqual(sanitized.metadata["changes"], ["normalized trade_date to YYYYMMDD"])

    def test_resolver_uses_duckdb_first_then_provider_and_writeback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_stock_daily(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "trade_date": "20260518", "close": 10.0},
                        {"ts_code": "000001.SZ", "trade_date": "20260519", "close": 10.1},
                    ]
                )
            )
            provider = FakeDailyProvider()
            resolver = MarketDataResolver(settings, storage=storage, provider=provider)

            first = resolver.load_stock_daily(["000001"], start_date="20260518", end_date="20260520")
            second = resolver.load_stock_daily(["000001"], start_date="20260518", end_date="20260520")

            self.assertEqual(provider.daily_calls, 1)
            self.assertEqual(set(first["trade_date"].astype(str)), {"20260518", "20260519", "20260520"})
            self.assertEqual(second.attrs["market_data_provenance"][0]["source"], "duckdb_cache")

    def test_resolver_quote_ttl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_realtime_quote_cache(
                pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "price": 11.0,
                            "fetched_at": (datetime.now() - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    ]
                )
            )
            provider = FakeDailyProvider()
            resolver = MarketDataResolver(settings, storage=storage, provider=provider)

            quote = resolver.load_realtime_quotes(["000001"], for_trading=True)

            self.assertEqual(provider.quote_calls, 1)
            self.assertEqual(float(quote.iloc[0]["price"]), 12.3)

    def test_restricted_python_allows_resolver_and_rejects_market_literals(self) -> None:
        class Resolver:
            def load_stock_daily(self, symbols, *, start_date, end_date):
                frame = pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260518", "close": 10.0}])
                frame.attrs["market_data_provenance"] = [{"source": "duckdb_cache"}]
                return frame

        runtime = RestrictedPythonRuntime(resolver=Resolver(), timeout_seconds=2)
        ok = runtime.run("def run(context):\n    daily = resolver.load_stock_daily(['000001'], start_date='20260518', end_date='20260518')\n    return {'rows': len(daily)}")
        bad = runtime.run("RESULT = {'close': 10.5}")
        banned = runtime.run("import os\nRESULT = 1")

        self.assertEqual(ok.status, "done")
        self.assertEqual(ok.result["rows"], 1)
        self.assertEqual(bad.status, "error")
        self.assertIn("market data literals", bad.error)
        self.assertEqual(banned.status, "error")

    def test_command_runner_uses_argv_and_guards_trading(self) -> None:
        calls = []

        def fake_cli(argv):
            calls.append(argv)
            return 0

        runner = AgentCommandRunner(policy=AgentExecutionPolicy(auto_trade=("buy",), broker="noop", live_trading=False), cli_main=fake_cli)
        blocked = AgentCommandRunner(policy=AgentExecutionPolicy(), cli_main=fake_cli).run(["qmt", "buy", "--symbol", "000001", "--quantity", "100"])
        dry_run = runner.run(["qmt", "buy", "--symbol", "000001", "--quantity", "100"])

        self.assertEqual(blocked.status, "error")
        self.assertIn("--dry-run", calls[0])
        self.assertEqual(dry_run.status, "done")

    def test_trading_executor_requires_permission_and_uses_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", qmt_account_id="acct")
            storage = DuckDBStorage(settings.db_path)
            provider = FakeDailyProvider()
            resolver = MarketDataResolver(settings, storage=storage, provider=provider)
            client = FakeClient()
            denied = AgentTradingExecutor(
                settings=settings,
                storage=storage,
                resolver=resolver,
                policy=AgentExecutionPolicy(),
                client=client,
            ).execute(TradeIntent(ts_code="000001.SZ", side="buy", quantity=100))
            allowed = AgentTradingExecutor(
                settings=settings,
                storage=storage,
                resolver=resolver,
                policy=AgentExecutionPolicy(auto_trade=("buy",), broker="qmt", live_trading=True, max_order_value=20000),
                client=client,
            ).execute(TradeIntent(ts_code="000001.SZ", side="buy", quantity=100))

            self.assertEqual(denied.status, "rejected")
            self.assertEqual(allowed.status, "submitted")
            self.assertEqual(client.requests[0].quantity, 100)

    def test_agent_runtime_executes_planned_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", openai_model="m", llm_timeout_seconds=10)
            calls = []

            def fake_cli(argv):
                calls.append(argv)
                print("skills output")
                return 0

            result = run_agent_once("列出 skills", settings=settings, llm_factory=FakePlanLLM, cli_main=fake_cli)

            self.assertEqual(calls, [["skills"]])
            self.assertIn("skills output", result.content)
            self.assertNotIn("[done]", result.content)

    def test_agent_runtime_replans_once_after_failed_step(self) -> None:
        class ReplanLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                ReplanLLM.calls += 1
                command = "bad" if ReplanLLM.calls == 1 else "skills"
                return LLMResponse(
                    content=(
                        '{"objective":"run command","steps":['
                        f'{{"step_id":"cmd_{command}","kind":"command","title":"{command}","command":["{command}"]}},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            ReplanLLM.calls = 0
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp), openai_model="m", llm_timeout_seconds=10)
            calls = []

            def fake_cli(argv):
                calls.append(argv)
                return 1 if argv == ["bad"] else 0

            result = run_agent_once("列出 skills", settings=settings, llm_factory=ReplanLLM, cli_main=fake_cli)

            self.assertEqual(calls, [["bad"], ["skills"]])
            self.assertIn("sats bad", result.content)
            self.assertIn("returncode=1", result.content)
            self.assertIn("sats skills", result.content)
            self.assertNotIn("[done]", result.content)

    def test_tool_registry_lists_core_capabilities_and_guards_market_literals(self) -> None:
        registry = build_default_tool_registry()
        names = set(registry.names())
        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )

        self.assertTrue({"chat.answer", "data.stock_daily", "research.backtest", "factor.pick", "sats_command.run", "trade.submit_intent"}.issubset(names))
        rejected = registry.execute(
            "data.stock_daily",
            {"symbols": ["000001"], "start_date": "20260518", "end_date": "20260520", "close": 10.5},
            context,
        )

        self.assertEqual(rejected.status, "error")
        self.assertIn("market data guard", rejected.content)

    def test_tool_registry_exposes_provider_capability_catalog(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )

        result = registry.execute(
            "data.list_provider_capabilities",
            {"provider": "tickflow", "realtime": True, "compact": True},
            context,
        )

        self.assertEqual(result.status, "done")
        capability_ids = {item["capability_id"] for item in result.payload["capabilities"]}
        self.assertIn("tickflow.realtime_quotes", capability_ids)
        self.assertIn("tickflow.realtime_minute_klines", capability_ids)
        self.assertTrue(all(item["provider"] == "tickflow" for item in result.payload["capabilities"]))
        self.assertTrue(all(item["realtime"] for item in result.payload["capabilities"]))

    def test_planner_context_includes_provider_data_capabilities(self) -> None:
        registry = build_default_tool_registry()
        payload = json.loads(registry.planner_context())

        tool_names = {item["name"] for item in payload["tools"]}
        capability_ids = {item["capability_id"] for item in payload["data_capabilities"]}

        self.assertIn("data.list_provider_capabilities", tool_names)
        self.assertIn("tushare.index_member_all", capability_ids)
        self.assertIn("tushare.margin_detail", capability_ids)
        self.assertIn("tickflow.realtime_quotes", capability_ids)
        self.assertIn("tickflow.market_depth", capability_ids)

    def test_llm_planner_can_choose_tushare_dataset_from_capability_context(self) -> None:
        class CapabilityPlannerLLM:
            last_messages = []

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                CapabilityPlannerLLM.last_messages = list(messages)
                content = messages[-1]["content"]
                for expected in ("data_capabilities", "tushare.margin_detail", "tushare.index_member_all"):
                    if expected not in content:
                        raise AssertionError(f"missing planner capability context: {expected}")
                return LLMResponse(
                    content=(
                        '{"objective":"query margin","steps":['
                        '{"step_id":"margin","kind":"tool","title":"融资融券明细","tool_name":"data.get_tushare_stock_data",'
                        '"arguments":{"dataset":"margin_detail","params":{"trade_date":"20260605"},"limit":20}},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("查融资融券明细和申万成分", settings=settings, policy=AgentExecutionPolicy(), llm_factory=CapabilityPlannerLLM, tool_registry=registry)

        self.assertEqual(plan.steps[0].tool_name, "data.get_tushare_stock_data")
        self.assertEqual(plan.steps[0].arguments["dataset"], "margin_detail")

    def test_llm_planner_can_choose_tickflow_realtime_tool_from_capability_context(self) -> None:
        class TickflowPlannerLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                content = messages[-1]["content"]
                for expected in ("tickflow.realtime_quotes", "tickflow.realtime_minute_klines"):
                    if expected not in content:
                        raise AssertionError(f"missing planner capability context: {expected}")
                return LLMResponse(
                    content=(
                        '{"objective":"query realtime","steps":['
                        '{"step_id":"quote","kind":"tool","title":"实时行情","tool_name":"data.realtime_quotes",'
                        '"arguments":{"symbols":["002436"]}},'
                        '{"step_id":"minute","kind":"tool","title":"分钟K","tool_name":"data.stock_minute",'
                        '"arguments":{"symbols":["002436"],"period":"30m","count":80}},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用 TickFlow 看 002436 实时行情和30分钟K", settings=settings, policy=AgentExecutionPolicy(), llm_factory=TickflowPlannerLLM, tool_registry=registry)

        self.assertEqual([step.tool_name for step in plan.steps[:2]], ["data.realtime_quotes", "data.stock_minute"])

    def test_fallback_planner_routes_plain_chat_to_chat_tool(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("解释均线金叉", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)

        self.assertEqual(plan.steps[0].kind, "tool")
        self.assertEqual(plan.steps[0].tool_name, "chat.answer")

    def test_chat_answer_tool_calls_plain_chat(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path(".")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )
        with patch("sats.agent.tools.chat_tools.build_plain_chat_answer", return_value=SimpleNamespace(content="plain answer")) as chat:
            result = registry.execute("chat.answer", {"message": "解释均线金叉"}, context)

        chat.assert_called_once()
        self.assertEqual(result.status, "done")
        self.assertEqual(result.content, "plain answer")

    def test_cli_agent_entrypoint(self) -> None:
        stdout = StringIO()
        fake = SimpleNamespace(content="agent ok", tool_call_count=1, data_names=("Agent",), artifacts=(), turn_id="turn", session_id="agent")
        with (
            patch("sats.cli.load_settings", return_value=SimpleNamespace(db_path=Path("x.duckdb"))),
            patch("sats.cli.run_agent_once", return_value=fake) as run_agent,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["agent", "hello"]), 0)

        run_agent.assert_called_once()
        self.assertIn("数据: Agent", stdout.getvalue())
        self.assertIn("agent ok", stdout.getvalue())

    def test_repl_agent_and_goal_commands(self) -> None:
        calls = []
        output = []
        state = ReplState()

        self.assertNotIn("/agent", help_text())
        self.assertTrue(handle_repl_line("/goal status", runner=lambda argv: 0, printer=output.append, state=state))
        self.assertTrue(handle_repl_line("/goal hello goal", runner=lambda argv: calls.append(argv) or 0, printer=output.append, state=state))
        self.assertTrue(handle_repl_line("/goal cancel", runner=lambda argv: 0, printer=output.append, state=state))

        self.assertEqual(calls[0], ["agent", "hello", "goal"])
        self.assertTrue(any("当前没有 Agent 目标" in item for item in output))
        self.assertEqual(state.agent_goal, "")


if __name__ == "__main__":
    unittest.main()
