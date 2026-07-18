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

from sats.agent import AgentExecutionPolicy, AgentObservation, AgentPlan, AgentStep, TradeIntent, run_agent_once
from sats.agent.command_runner import AgentCommandRunner
from sats.agent.date_policy import normalize_agent_date, resolve_agent_time_context, sanitize_agent_tool_arguments
from sats.agent.planner import build_agent_plan
from sats.agent.planner_v2 import _is_sector_return_ranking_text, _sector_ranking_period_text
from sats.agent.runtime import _execute_step
from sats.agent.python_runtime import RestrictedPythonRuntime
from sats.agent.recovery import failure_from_message
from sats.agent.synthesis import _compact_evidence_digest, _evidence_digest, collect_agent_sources, save_agent_report, synthesize_agent_result
from sats.agent.tools import AgentToolContext, AgentToolRegistry, AgentToolResult, AgentToolSpec, build_default_tool_registry
from sats.agent.tools.command_tools import RECURSIVE_SATS_COMMANDS, SATS_COMMANDS
from sats.agent.tools.research_tools import _sector_period_from_text
from sats.agent.trading import AgentTradingExecutor
from sats.analysis.dsa_native import DsaAnalysisRanking, DsaAnalysisRunResult, DsaStockAnalysis
from sats.backtesting.service import BacktestResult
from sats.backtesting.strategy_spec import StrategySpec
from sats.catalog import build_capability_catalog
from sats.chat import ChatResult, format_chat_result
from sats.cli import build_parser, main
from sats.data.astock_provider import AStockDataProvider
from sats.data.resolver import MarketDataResolver
from sats.llm import LLMResponse
from sats.screening.base import ScreeningResult
from sats.repl import ReplState, handle_repl_line, help_text
from sats.skills import Skill
from sats.storage.duckdb import DuckDBStorage
from sats.stock_question import extract_natural_trade_date
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


class FakeThemeReturnsOldPlanLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"objective":"theme returns","steps":['
                '{"step_id":"discover","kind":"tool","title":"discover","tool_name":"research.discover_opportunities",'
                '"arguments":{"query":"CVD金刚石散热 相关股票","trade_date":"20260620","limit":20}},'
                '{"step_id":"daily","kind":"tool","title":"daily","tool_name":"data.stock_daily",'
                '"arguments":{"symbols":"从步骤1输出中提取的股票代码列表","start_date":"20251220","end_date":"20260618"}},'
                '{"step_id":"final","kind":"final","title":"summary"}]}'
            )
        )


class FakeThemeListOldPlanLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"objective":"theme list","steps":['
                '{"step_id":"discover","kind":"tool","title":"discover","tool_name":"research.discover_opportunities",'
                '"arguments":{"query":"内存和存储相关A股股票","limit":20}},'
                '{"step_id":"final","kind":"final","title":"summary"}]}'
            )
        )


class FakeInvalidStockBasicLLM:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def chat(self, messages, timeout=None):
        return LLMResponse(
            content=(
                '{"objective":"lookup stock","steps":['
                '{"step_id":"basic","kind":"tool","title":"basic","tool_name":"data.astock_fetch",'
                '"arguments":{"operation":"astock.stock_basic","params":{"name":"台基股份"},"limit":5}},'
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


def _third_party_openai_settings() -> SimpleNamespace:
    return SimpleNamespace(
        openai_model="gpt-5.5",
        llm_provider="openai",
        openai_base_url="https://api.9527code.com/v1",
        llm_timeout_seconds=10,
    )


def _large_market_observation() -> AgentObservation:
    sectors = [
        {"name": f"热点板块{i}", "heat_score": 1000 - i, "reason": "强势上涨" + ("x" * 500)}
        for i in range(80)
    ]
    indices = [
        {
            "ts_code": code,
            "name": name,
            "trade_date": "20260630",
            "latest": {"close": 4000 + i, "pct_chg": 0.5 + i / 10, "amount": 1000000 + i, "vol": 20000 + i},
            "technical": {"summary": "多头" + ("y" * 2000), "ma5": 4000, "ma20": 3900},
            "weekly": {"pct_chg": i, "detail": "z" * 2000},
            "daily_tail": [{"trade_date": f"202606{i:02d}", "close": 4000 + i} for i in range(1, 25)],
        }
        for i, (code, name) in enumerate(
            (
                ("000001.SH", "上证指数"),
                ("399001.SZ", "深证成指"),
                ("399006.SZ", "创业板指"),
                ("000300.SH", "沪深300"),
                ("000905.SH", "中证500"),
                ("000688.SH", "科创50"),
                ("899050.BJ", "北证50"),
                ("399330.SZ", "深证100"),
            )
        )
    ]
    market_context = {
        "trade_date": "20260630",
        "requested_horizons": ["tomorrow"],
        "requested_dimensions": ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors", "fund_flow"],
        "indices": indices,
        "market_breadth": {"summary": "宽度偏强", "huge": "b" * 80_000},
        "limit_sentiment": {"summary": "情绪回暖", "huge": "l" * 60_000},
        "fund_flow": {"market": {"net_amount": -1000000000.0}, "sector_top": [{"name": "算力概念", "net_amount": 500000000.0}], "sector_bottom": [{"name": "银行", "net_amount": -300000000.0}]},
        "hot_sector_context": {"sectors": sectors, "huge": "h" * 120_000},
        "hot_sectors": sectors,
        "data_sources": {"source": "duckdb_cache", "huge": "d" * 50_000},
    }
    payload = {
        "market_context": market_context,
        "provenance": [{"dataset": "index_daily", "source": "duckdb_cache", "trade_date": "20260630"} for _ in range(200)],
    }
    return AgentObservation(
        step_id="market",
        kind="tool",
        status="done",
        content=json.dumps({"status": "ok", "market_context": market_context}, ensure_ascii=False),
        payload={"tool_name": "research.market_context", "data_names": ["market_context"], "result": {"payload": payload}},
    )


def _capability_observations() -> tuple[AgentObservation, ...]:
    summary_catalog = {
        "section": "summary",
        "counts": {
            "commands": 34,
            "agent-tools": 58,
            "skills": 209,
            "knowledge": 9,
            "providers": 1277,
            "screening-rules": 13,
            "signals": 193,
            "factors": 308,
            "api": 5,
        },
        "data": {
            "summary": {
                "commands": {"total": 34, "top_level_count": 34},
                "agent-tools": {"total": 58, "by_category": {"web": 4, "analysis": 1, "data_catalog": 6, "command": 2, "trade": 5}},
                "skills": {"total": 209, "by_category": {"strategy": 80, "analysis": 44, "data-source": 30}},
                "knowledge": {"total": 9},
                "providers": {"total": 1277, "by_provider": {"astock": 31, "tickflow": 16, "tushare": 154, "akshare": 1076}},
                "screening-rules": {"total": 13},
                "signals": {"total": 193},
                "factors": {"total": 308},
                "api": {"total": 5},
            }
        },
        "consistency": {"warnings": []},
    }
    skills_catalog = {
        "section": "skills",
        "counts": {"skills": 209},
        "data": {
            "skills": {
                "total": 209,
                "returned": 3,
                "offset": 0,
                "limit": 12,
                "truncated": True,
                "by_category": {"strategy": 80, "analysis": 44, "data-source": 30},
                "items": [
                    {"id": "chan-theory", "name": "chan-theory", "category": "strategy", "description": "缠论买卖点方法论"},
                    {"id": "tickflow", "name": "tickflow", "category": "data-source", "description": "实时行情数据源"},
                    {"id": "risk-analysis", "name": "risk-analysis", "category": "risk-analysis", "description": "风险分析框架"},
                ],
            }
        },
        "consistency": {"warnings": []},
    }
    return (
        AgentObservation(
            step_id="capability_catalog_summary",
            kind="tool",
            status="done",
            content="catalog section summary",
            payload={"tool_name": "catalog.capabilities", "result": {"payload": {"catalog": summary_catalog}, "data_names": ["SATS capabilities"]}},
        ),
        AgentObservation(
            step_id="capability_catalog_skills",
            kind="tool",
            status="done",
            content="catalog section skills",
            payload={"tool_name": "catalog.capabilities", "result": {"payload": {"catalog": skills_catalog}, "data_names": ["Skills"]}},
        ),
    )


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
                                "requested_dimensions": ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors", "fund_flow"],
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
                                "fund_flow": {"market": {"net_amount": -1000000000.0}, "sector_top": [{"name": "算力概念", "net_amount": 500000000.0}], "sector_bottom": [{"name": "银行", "net_amount": -300000000.0}]},
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

    def test_third_party_openai_synthesis_compacts_large_prompt(self) -> None:
        class CaptureLLM:
            last_messages = []

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                CaptureLLM.last_messages = list(messages)
                return SimpleNamespace(content="压缩合成成功")

        result = synthesize_agent_result(
            message="评价 通富微电 今天走势，预测明天走势",
            plan=AgentPlan(objective="评价 通富微电 今天走势，预测明天走势"),
            observations=(_large_market_observation(),),
            skills=(),
            settings=_third_party_openai_settings(),
            llm_factory=CaptureLLM,
        )
        context_text = "\n".join(str(item.get("content") or "") for item in CaptureLLM.last_messages)

        self.assertTrue(result.used_llm)
        self.assertEqual(result.compact_mode, "gateway_compact")
        self.assertLessEqual(result.prompt_chars, 28000)
        self.assertIn("observations_summary", context_text)
        self.assertNotIn('"observations":', context_text)

    def test_third_party_openai_synthesis_uses_strict_stream_and_bounded_transport(self) -> None:
        class StrictStreamLLM:
            init_kwargs = {}
            calls = []

            def __init__(self, *args, **kwargs) -> None:
                StrictStreamLLM.init_kwargs = dict(kwargs)

            def strict_stream_chat(self, messages, timeout=None):
                StrictStreamLLM.calls.append({"messages": list(messages), "timeout": timeout})
                return SimpleNamespace(content="流式综合成功")

            def chat(self, messages, timeout=None):
                raise AssertionError("third-party synthesis must not use non-stream chat")

        StrictStreamLLM.init_kwargs = {}
        StrictStreamLLM.calls = []
        settings = _third_party_openai_settings()
        settings.llm_timeout_seconds = 180
        result = synthesize_agent_result(
            message="评价 通富微电 今天走势，预测明天走势",
            plan=AgentPlan(objective="评价 通富微电 今天走势，预测明天走势"),
            observations=(_large_market_observation(),),
            skills=(),
            settings=settings,
            llm_factory=StrictStreamLLM,
        )

        self.assertTrue(result.used_llm)
        self.assertEqual(result.transport_mode, "strict_stream")
        self.assertEqual(result.effective_timeout_seconds, 110)
        self.assertEqual(result.transport_max_retries, 0)
        self.assertEqual(StrictStreamLLM.init_kwargs["timeout_seconds"], 110)
        self.assertEqual(StrictStreamLLM.init_kwargs["max_retries"], 0)
        self.assertEqual([call["timeout"] for call in StrictStreamLLM.calls], [110])

    def test_third_party_openai_synthesis_retries_with_ultra_compact_prompt(self) -> None:
        class RetryLLM:
            messages_by_call = []

            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0

            def chat(self, messages, timeout=None):
                self.calls += 1
                RetryLLM.messages_by_call.append(list(messages))
                if self.calls == 1:
                    raise RuntimeError("status_code=500, upstream error: do request failed")
                return SimpleNamespace(content="重试合成成功")

        RetryLLM.messages_by_call = []
        result = synthesize_agent_result(
            message="评价 通富微电 今天走势，预测明天走势",
            plan=AgentPlan(objective="评价 通富微电 今天走势，预测明天走势"),
            observations=(_large_market_observation(),),
            skills=(),
            settings=_third_party_openai_settings(),
            llm_factory=RetryLLM,
        )

        self.assertTrue(result.used_llm)
        self.assertEqual(result.content, "重试合成成功")
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(result.compact_mode, "ultra_compact")
        self.assertEqual(len(result.attempt_errors), 1)
        first_chars = sum(len(str(item.get("content") or "")) for item in RetryLLM.messages_by_call[0])
        retry_chars = sum(len(str(item.get("content") or "")) for item in RetryLLM.messages_by_call[1])
        self.assertLessEqual(first_chars, 28000)
        self.assertLessEqual(retry_chars, 12000)

    def test_third_party_openai_synthesis_retries_timeout(self) -> None:
        class TimeoutThenSuccessLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0

            def chat(self, messages, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("Request timed out.")
                return SimpleNamespace(content="超时重试合成成功")

        result = synthesize_agent_result(
            message="评价 通富微电 今天走势，预测明天走势",
            plan=AgentPlan(objective="评价 通富微电 今天走势，预测明天走势"),
            observations=(_large_market_observation(),),
            skills=(),
            settings=_third_party_openai_settings(),
            llm_factory=TimeoutThenSuccessLLM,
        )

        self.assertTrue(result.used_llm)
        self.assertEqual(result.content, "超时重试合成成功")
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(result.compact_mode, "ultra_compact")
        self.assertEqual(len(result.attempt_errors), 1)

    def test_third_party_openai_synthesis_double_failure_falls_back_with_error_meta(self) -> None:
        class FailingLLM:
            messages_by_call = []

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                FailingLLM.messages_by_call.append(list(messages))
                raise RuntimeError("status_code=500, upstream error: do request failed")

        FailingLLM.messages_by_call = []
        result = synthesize_agent_result(
            message="评价 通富微电 今天走势，预测明天走势",
            plan=AgentPlan(objective="评价 通富微电 今天走势，预测明天走势"),
            observations=(_large_market_observation(),),
            skills=(),
            settings=_third_party_openai_settings(),
            llm_factory=FailingLLM,
        )

        self.assertFalse(result.used_llm)
        self.assertEqual(result.retry_count, 1)
        self.assertEqual(result.compact_mode, "ultra_compact")
        self.assertEqual(result.error_type, "RuntimeError")
        self.assertIn("do request failed", result.error_message)
        self.assertEqual(len(result.attempt_errors), 2)
        self.assertLessEqual(result.prompt_chars, 12000)
        self.assertIn("已围绕", result.content)

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
                "period_returns": {"6m": {"start_trade_date": "20251210", "end_trade_date": "20260609", "pct_change": 10.0 + i}},
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
        self.assertEqual(digest["stock_context"][0]["period_returns"]["6m"]["pct_change"], 10.0)

    def test_agent_digest_keeps_minute_curves_and_optional_fields_separate(self) -> None:
        observations = (
            AgentObservation(
                step_id="stock_context",
                kind="tool",
                status="done",
                content="stock context",
                payload={
                    "tool_name": "research.stock_context",
                    "arguments": {"symbols": ["002436"], "trade_date": "20260609"},
                    "result": {
                        "payload": {
                            "stock_context": {
                                "stocks": [
                                    {
                                        "ts_code": "002436.SZ",
                                        "name": "兴森科技",
                                        "trade_date": "20260609",
                                        "daily_tail": [{"trade_date": "20260609", "close": 10.0}],
                                        "minute_curves": {
                                            "15m": {
                                                "period": "15m",
                                                "source": "tickflow_history+realtime",
                                                "row_count": 16,
                                                "start_trade_time": "2026-06-09 09:45:00",
                                                "end_trade_time": "2026-06-09 15:00:00",
                                                "rows": [
                                                    {"trade_time": "2026-06-09 14:45:00", "close": 10.2},
                                                    {"trade_time": "2026-06-09 15:00:00", "close": 10.4},
                                                ],
                                            }
                                        },
                                        "missing_fields": [],
                                        "optional_fields_not_requested": ["optional_minute_30m"],
                                    }
                                ]
                            }
                        }
                    },
                },
            ),
        )

        digest = _evidence_digest(observations)
        stock = digest["stock_context"][0]

        self.assertEqual(stock.get("missing_fields", []), [])
        self.assertEqual(stock["optional_fields_not_requested"], ["optional_minute_30m"])
        self.assertEqual(stock["minute_curves"]["15m"]["row_count"], 16)
        self.assertEqual(stock["minute_curves"]["15m"]["tail"][-1]["trade_time"], "2026-06-09 15:00:00")

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
        self.assertIn("fund_flow", market_context)
        self.assertIn("资金流", market_context)
        self.assertIn("电力", market_context)
        self.assertIn("AI算力", market_context)
        self.assertIn("不得把盘中累计成交额直接与前一交易日全天成交额比较", market_context)
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

    def test_capability_synthesis_uses_capability_overview_style(self) -> None:
        synthesize_agent_result(
            message="列出支持的 skills",
            plan=AgentPlan(objective="列出支持的 skills"),
            observations=_capability_observations(),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=FakeSynthesisLLM,
        )
        context_text = "\n".join(str(item.get("content") or "") for item in FakeSynthesisLLM.last_messages)

        self.assertIn("capability_overview", context_text)
        self.assertIn("能力总览", context_text)
        self.assertIn("Skills 与 Tools 的区别", context_text)
        self.assertIn("analysis.python_program", context_text)
        self.assertNotIn("今日/近期盘面表", context_text)

    def test_screening_digest_keeps_named_candidates_and_original_order(self) -> None:
        for candidate_count in (151, 139):
            with self.subTest(candidate_count=candidate_count):
                selected_rows = [
                    {
                        "ts_code": f"300{i:03d}.SZ",
                        "name": f"候选{i}",
                        "score": 101 - i,
                        "matched_conditions": ["MA5 > MA10 > MA20", "回踩 MA10 后重新站上"],
                        "failed_conditions": [],
                        "latest_trade_date": "20260716",
                        "data_source": "tushare_daily",
                        "condition_details": [{"large": "不进入 digest" * 100}],
                    }
                    for i in range(1, 21)
                ]
                observation = AgentObservation(
                    step_id="screen",
                    kind="tool",
                    status="done",
                    content="matched",
                    payload={
                        "tool_name": "workflow.screened_stock_analysis",
                        "result": {
                            "status": "done",
                            "payload": {
                                "business_status": "matched",
                                "selection_strategy": "ephemeral_spec",
                                "rule": "nl_trend_pullback",
                                "trade_date": "20260716",
                                "candidate_count": candidate_count,
                                "candidate_limit": 20,
                                "selected_rows": selected_rows,
                                "selected_symbols": [item["ts_code"] for item in selected_rows],
                                "analysis_output": "格式化分析文本，不是字典",
                                "semantic_spec": {
                                    "assumptions": ["MA20 为趋势失效线"],
                                    "conditions": [{"id": "ma_stack", "label": "MA5 > MA10 > MA20", "required": True}],
                                },
                            },
                        },
                    },
                )

                digest = _evidence_digest((observation,))["screened_stock_analysis"]

                self.assertEqual(digest["candidate_count"], candidate_count)
                self.assertEqual(digest["candidate_limit"], 20)
                self.assertEqual([item["ts_code"] for item in digest["selected_rows"]], [f"300{i:03d}.SZ" for i in range(1, 21)])
                self.assertEqual([item["name"] for item in digest["selected_rows"]], [f"候选{i}" for i in range(1, 21)])
                self.assertEqual(digest["assumptions"], ["MA20 为趋势失效线"])
                self.assertNotIn("condition_details", json.dumps(digest, ensure_ascii=False))
                compact = _compact_evidence_digest({"screened_stock_analysis": digest}, compact_mode="compact")
                ultra = _compact_evidence_digest({"screened_stock_analysis": digest}, compact_mode="ultra_compact")
                for payload in (compact, ultra):
                    candidates = payload["screened_stock_analysis"]["selected_rows"]
                    self.assertEqual(len(candidates), 20)
                    self.assertEqual([item["ts_code"] for item in candidates], [f"300{i:03d}.SZ" for i in range(1, 21)])
                    self.assertNotIn("truncated_json", json.dumps(payload, ensure_ascii=False))

    def test_screening_candidates_override_incidental_capability_style(self) -> None:
        workflow = AgentObservation(
            step_id="screen",
            kind="tool",
            status="done",
            content="matched",
            payload={
                "tool_name": "workflow.screened_stock_analysis",
                "result": {
                    "status": "done",
                    "payload": {
                        "business_status": "matched",
                        "rule": "nl_trend_pullback",
                        "trade_date": "20260716",
                        "candidate_count": 139,
                        "candidate_limit": 2,
                        "selected_rows": [
                            {"ts_code": "001206.SZ", "name": "依依股份", "score": 100, "matched_conditions": ["均线多头"]},
                            {"ts_code": "002828.SZ", "name": "贝肯能源", "score": 99, "matched_conditions": ["回踩不破"]},
                        ],
                        "analysis_output": "格式化分析文本",
                    },
                },
            },
        )

        synthesize_agent_result(
            message="列出今天趋势较强、回踩不破关键均线的个股",
            plan=AgentPlan(objective="列出今天趋势较强、回踩不破关键均线的个股"),
            observations=(*_capability_observations(), workflow),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=FakeSynthesisLLM,
        )
        context_text = "\n".join(str(item.get("content") or "") for item in FakeSynthesisLLM.last_messages)

        self.assertIn('"analysis_style": "discovery"', context_text)
        self.assertIn("候选排序表", context_text)
        self.assertIn("001206.SZ", context_text)
        self.assertIn("依依股份", context_text)

    def test_screening_fallback_lists_name_and_code_when_llm_unavailable(self) -> None:
        workflow = AgentObservation(
            step_id="screen",
            kind="tool",
            status="done",
            content="matched",
            payload={
                "tool_name": "workflow.screened_stock_analysis",
                "result": {
                    "status": "done",
                    "payload": {
                        "business_status": "matched",
                        "rule": "nl_trend_pullback",
                        "trade_date": "20260716",
                        "candidate_count": 151,
                        "candidate_limit": 2,
                        "selected_rows": [
                            {"ts_code": "000931.SZ", "name": "中关村", "score": 100, "matched_conditions": ["均线多头"]},
                            {"ts_code": "002415.SZ", "name": "海康威视", "score": 99, "matched_conditions": ["回踩不破"]},
                        ],
                        "analysis_output": "格式化分析文本",
                        "semantic_spec": {"assumptions": ["MA20 为趋势失效线"]},
                    },
                },
            },
        )

        result = synthesize_agent_result(
            message="今天趋势较强、回踩不破关键均线的股票",
            plan=AgentPlan(objective="今天趋势较强、回踩不破关键均线的股票"),
            observations=(
                workflow,
                AgentObservation(
                    step_id="minute_analysis",
                    kind="tool",
                    status="error",
                    content="缺少真实 15m 分钟K数据",
                    payload={"tool_name": "research.internal_analysis"},
                ),
            ),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=None,
        )

        self.assertIn("候选排序表", result.content)
        self.assertIn("中关村", result.content)
        self.assertIn("000931.SZ", result.content)
        self.assertIn("海康威视", result.content)
        self.assertIn("002415.SZ", result.content)
        self.assertIn("缺少真实 15m 分钟K数据", result.content)
        self.assertNotIn("SATS 支持的 Skills 与 Agent 能力总览", result.content)

    def test_capability_fallback_describes_codex_like_boundaries(self) -> None:
        result = synthesize_agent_result(
            message="列出支持的 skills",
            plan=AgentPlan(objective="列出支持的 skills"),
            observations=_capability_observations(),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=None,
        )

        self.assertFalse(result.used_llm)
        self.assertIn("联网搜索与页面读取", result.content)
        self.assertIn("analysis.python_program", result.content)
        self.assertIn("data.astock_catalog", result.content)
        self.assertIn("受限 Python 自编程", result.content)
        self.assertIn("SATS 注册工具和安全策略", result.content)
        self.assertIn("sats catalog --section skills", result.content)
        self.assertNotIn("真实数据工具调用", result.content)
        self.assertNotIn("不构成投资建议", result.content)

    def test_discovery_synthesis_keeps_ten_candidate_summaries_without_truncated_json(self) -> None:
        candidates = [
            {
                "ts_code": f"300{i:03d}.SZ",
                "name": f"候选{i}",
                "trade_date": "20260609",
                "ranking_score": 100 - i,
                "local_score": 80 - i,
                "close": 10 + i,
                "decision": "观察",
                "trend": "看多",
                "events": [
                    {
                        "signal_id": "short_up",
                        "label": "短线买入信号",
                        "side": "buy",
                        "confidence": 80,
                        "reason": "技术分析详情" * 120,
                        "risk_flags": ["跌破支撑"],
                    }
                ],
                "key_levels": {"support": 9 + i, "resistance": 12 + i},
                "indicator": {
                    "technical": {"ma5": 10 + i, "ma20": 9 + i, "macd": "金叉"},
                    "volume": {"volume_ratio": 1.5},
                    "factor": {"profile": "balanced", "score": 1.2, "factor_values": {"alpha": "大字段" * 200}},
                },
                "hot_sectors": [{"name": "AI算力", "heat_score": 12}],
                "entry_trigger": "放量突破",
                "invalidation": "跌破5日线",
                "risk": "仅供观察",
            }
            for i in range(1, 11)
        ]
        discovery_payload = {
            "trade_date": "20260609",
            "signals": "short_up",
            "candidates": candidates,
            "candidate_count": 10,
            "scanned_count": 5000,
            "llm_pool_count": 10,
        }
        observations = (
            AgentObservation(
                step_id="discover",
                kind="tool",
                status="done",
                content="discover",
                payload={
                    "tool_name": "research.discover_opportunities",
                    "result": {
                        "payload": {
                            "status": "ok",
                            "stock_picking_agent": {"query": "选取10支明天大概率上涨的股票", "opportunity_discovery": discovery_payload},
                            "opportunity_discovery": discovery_payload,
                        }
                    },
                },
            ),
        )

        digest = _evidence_digest(observations)
        digest_text = json.dumps(digest["discovery"], ensure_ascii=False)

        self.assertEqual(len(digest["discovery"]["candidates"]), 10)
        self.assertEqual(digest["discovery"]["candidate_summary"]["omitted_count"], 0)
        self.assertNotIn("truncated_json", digest_text)
        self.assertNotIn("factor_values", digest_text)

        synthesize_agent_result(
            message="选取10支明天大概率上涨的股票",
            plan=AgentPlan(objective="短线机会发现"),
            observations=observations,
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=FakeSynthesisLLM,
        )
        context_text = "\n".join(str(item.get("content") or "") for item in FakeSynthesisLLM.last_messages)

        self.assertNotIn("truncated_json", context_text)
        self.assertIn('"omitted_count": 0', context_text)
        for i in range(1, 11):
            self.assertIn(f"300{i:03d}.SZ", context_text)
            self.assertIn(f"候选{i}", context_text)

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

    def test_synthesis_fallback_displays_native_dsa_rankings_and_tactical_levels(self) -> None:
        observations = self._rich_stock_observations() + (
            AgentObservation(
                step_id="native_dsa",
                kind="tool",
                status="done",
                content="native dsa",
                payload={
                    "tool_name": "research.internal_analysis",
                    "data_names": ["internal_analysis"],
                    "result": {
                        "payload": {
                            "analysis": {
                                "kind": "native_dsa",
                                "trade_date": "20260604",
                                "rankings": [
                                    {
                                        "code": "002436.SZ",
                                        "name": "兴森科技",
                                        "score": 76,
                                        "advice": "买入",
                                        "trend": "看多",
                                        "decision_type": "buy",
                                        "confidence_level": "中",
                                    }
                                ],
                                "analyses": [
                                    {
                                        "ts_code": "002436.SZ",
                                        "name": "兴森科技",
                                        "risk_factors": ["乖离偏高"],
                                        "missing_fields": ["news_context: provider_unavailable"],
                                        "hot_sectors": [{"name": "AI算力"}],
                                        "dashboard": {
                                            "battle_plan": {
                                                "sniper_points": {
                                                    "ideal_buy": 40.0,
                                                    "secondary_buy": 39.2,
                                                    "stop_loss": 37.8,
                                                    "take_profit": 44.0,
                                                },
                                                "position_strategy": {"suggested_position": "试探仓 20%-30%"},
                                            }
                                        },
                                    }
                                ],
                            }
                        }
                    },
                },
            ),
        )
        result = synthesize_agent_result(
            message="用 DSA 分析兴森科技买卖点",
            plan=AgentPlan(objective="用 DSA 分析兴森科技买卖点"),
            observations=observations,
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=None,
        )

        self.assertIn("## DSA 策略研判", result.content)
        self.assertIn("买入", result.content)
        self.assertIn("AI算力", result.content)
        self.assertIn("DSA 战术价位", result.content)
        self.assertIn("37.8", result.content)

    def test_synthesis_fallback_flags_missing_public_event_evidence(self) -> None:
        result = synthesize_agent_result(
            message="事件驱动分析 002436",
            plan=AgentPlan(objective="事件驱动分析 002436"),
            observations=self._rich_stock_observations(),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=None,
        )

        self.assertIn("未获取到公开事件证据", result.content)

    def test_evidence_digest_keeps_web_evidence_separate_from_market_data(self) -> None:
        digest = _evidence_digest(
            (
                AgentObservation(
                    step_id="web",
                    kind="tool",
                    status="done",
                    content="web",
                    payload={
                        "tool_name": "web.search",
                        "data_names": ["Web Search"],
                        "result": {
                            "payload": {
                                "web_search": {
                                    "status": "ok",
                                    "query": "贵州茅台 最新公告",
                                    "results": [
                                        {
                                            "title": "贵州茅台公告",
                                            "url": "https://example.com/a",
                                            "snippet": "公开公告摘要",
                                            "source": "ddgs",
                                            "fetched_at": "2026-06-09T00:00:00Z",
                                        }
                                    ],
                                }
                            }
                        },
                    },
                ),
            )
        )

        self.assertEqual(digest["web_evidence"][0]["kind"], "web_search")
        self.assertEqual(digest["web_evidence"][0]["url"], "https://example.com/a")
        self.assertEqual(digest["web_evidence"][0]["source_id"], "S1")
        self.assertEqual(digest["web_sources"][0]["id"], "S1")
        self.assertEqual(digest["quotes"], [])
        self.assertEqual(digest["market_context"], {})

    def test_agent_sources_are_stable_and_chat_output_appends_clickable_table(self) -> None:
        observations = (
            AgentObservation(
                step_id="web",
                kind="tool",
                status="done",
                payload={
                    "tool_name": "web.search",
                    "result": {
                        "payload": {
                            "web_search": {
                                "backend": "responses",
                                "fetched_at": "2026-06-19T00:00:00Z",
                                "sources": [
                                    {"title": "公告", "url": "https://www.sse.com.cn/a"},
                                    {"title": "公告重复", "url": "https://www.sse.com.cn/a"},
                                ],
                            }
                        }
                    },
                },
            ),
        )

        sources = collect_agent_sources(observations)
        rendered = format_chat_result(ChatResult(content="公司发布公告。[S1]", skill_names=(), sources=tuple(sources)))

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["id"], "S1")
        self.assertIn("## 来源", rendered)
        self.assertIn("[公告](https://www.sse.com.cn/a)", rendered)

    def test_agent_fallback_keeps_responses_inline_source_ids(self) -> None:
        observations = (
            AgentObservation(
                step_id="web",
                kind="tool",
                status="done",
                payload={
                    "tool_name": "web.search",
                    "result": {
                        "payload": {
                            "web_search": {
                                "status": "ok",
                                "backend": "responses",
                                "query": "贵州茅台 最新公告",
                                "answer": "公司发布公告。[S1]",
                                "sources": [{"id": "S1", "title": "公告", "url": "https://www.sse.com.cn/a"}],
                                "results": [
                                    {
                                        "source_id": "S1",
                                        "title": "公告",
                                        "url": "https://www.sse.com.cn/a",
                                        "snippet": "公告摘要",
                                    }
                                ],
                            }
                        }
                    },
                },
            ),
        )

        result = synthesize_agent_result(
            message="贵州茅台 最新公告",
            plan=AgentPlan(objective="贵州茅台 最新公告"),
            observations=observations,
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=None,
        )

        self.assertIn("公司发布公告。[S1]", result.content)
        self.assertIn("| S1 |", result.content)

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
        self.assertIn("## 资金流", result.content)
        self.assertIn("算力概念", result.content)
        self.assertIn("电力", result.content)
        self.assertIn("AI算力", result.content)
        self.assertNotIn("核心指数数据缺失", result.content)

    def test_company_fundamentals_synthesis_fallback_shows_names_and_codes(self) -> None:
        observations = (
            AgentObservation(
                step_id="company",
                kind="tool",
                status="done",
                content="company fundamentals",
                payload={
                    "tool_name": "research.internal_analysis",
                    "arguments": {"kind": "company_fundamentals", "symbols": ["688700.SH", "688559.SH"]},
                    "result": {
                        "payload": {
                            "analysis": {
                                "kind": "company_fundamentals",
                                "companies": [
                                    {
                                        "ts_code": "688700.SH",
                                        "name": "东威科技",
                                        "company_profile": {"com_name": "昆山东威科技股份有限公司", "main_business": "专用设备"},
                                        "main_business": "专用设备",
                                        "business_composition": [{"end_date": "20251231", "bz_item": "设备", "bz_sales": 100.0}],
                                        "valuation": {"trade_date": "20260618", "pe": 20.0, "pb": 2.0},
                                        "financial_indicators": [{"end_date": "20251231", "roe": 12.0}],
                                        "missing_fields": [],
                                    },
                                    {
                                        "ts_code": "688559.SH",
                                        "name": "海目星",
                                        "company_profile": {"com_name": "海目星激光科技集团股份有限公司", "main_business": "激光设备"},
                                        "main_business": "激光设备",
                                        "business_composition": [],
                                        "valuation": {"trade_date": "20260618", "pe": 25.0, "pb": 2.5},
                                        "financial_indicators": [{"end_date": "20251231", "roe": 10.0}],
                                        "missing_fields": [],
                                    },
                                ],
                            }
                        }
                    },
                },
            ),
        )

        result = synthesize_agent_result(
            message="东威科技和海目星公司介绍、业务及基本面",
            plan=AgentPlan(objective="公司基本面"),
            observations=observations,
            skills=(),
            settings=SimpleNamespace(openai_model="m"),
            llm_factory=None,
        )

        self.assertIn("东威科技", result.content)
        self.assertIn("688700.SH", result.content)
        self.assertIn("海目星", result.content)
        self.assertIn("688559.SH", result.content)
        self.assertNotIn("真实日线行情", result.content)

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

    def test_company_fundamentals_request_resolves_names_and_avoids_daily_tools(self) -> None:
        registry = build_default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", openai_model="m", llm_timeout_seconds=10)
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "688700.SH", "symbol": "688700", "name": "东威科技"},
                        {"ts_code": "688559.SH", "symbol": "688559", "name": "海目星"},
                    ]
                )
            )

            plan = build_agent_plan(
                "东威科技 和 海目星 公司介绍，业务以及基本面介绍",
                settings=settings,
                policy=AgentExecutionPolicy(),
                llm_factory=None,
                tool_registry=registry,
            )

        self.assertEqual([step.tool_name for step in plan.steps if step.kind == "tool"], ["research.internal_analysis"])
        self.assertEqual(plan.steps[0].arguments["kind"], "company_fundamentals")
        self.assertEqual(plan.steps[0].arguments["symbols"], ["688700.SH", "688559.SH"])

    def test_llm_company_plan_names_are_replaced_with_resolved_symbols(self) -> None:
        class CompanyPlanLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return LLMResponse(
                    content=(
                        '{"objective":"company","steps":['
                        '{"step_id":"stock","kind":"tool","title":"stock","tool_name":"research.stock_context",'
                        '"arguments":{"symbols":["东威科技","海目星"],"trade_date":"20260621"}},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", openai_model="m", llm_timeout_seconds=10)
            DuckDBStorage(settings.db_path).upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "688700.SH", "symbol": "688700", "name": "东威科技"},
                        {"ts_code": "688559.SH", "symbol": "688559", "name": "海目星"},
                    ]
                )
            )

            plan = build_agent_plan(
                "东威科技 和 海目星 公司介绍，业务以及基本面介绍",
                settings=settings,
                policy=AgentExecutionPolicy(),
                llm_factory=CompanyPlanLLM,
                tool_registry=build_default_tool_registry(),
            )

        self.assertEqual(plan.steps[0].tool_name, "research.internal_analysis")
        self.assertEqual(plan.steps[0].arguments["kind"], "company_fundamentals")
        self.assertEqual(plan.steps[0].arguments["symbols"], ["688700.SH", "688559.SH"])

    def test_fallback_planner_adds_web_search_for_latest_public_info(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("002436 最新公告和新闻", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]

        self.assertIn("web.search", tools)
        self.assertIn("research.stock_context", tools)
        web_step = next(step for step in plan.steps if step.tool_name == "web.search")
        self.assertEqual(web_step.arguments["freshness"], "d")
        self.assertEqual(web_step.arguments["context_size"], "medium")

    def test_fallback_planner_uses_high_context_for_deep_public_research(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "全面对比 002436 最近公告和新闻并生成研究报告",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        web_step = next(step for step in plan.steps if step.tool_name == "web.search")

        self.assertEqual(web_step.arguments["context_size"], "high")

    def test_fallback_planner_opens_explicit_public_url(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "请读取 https://example.com/report 并总结关键事实",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        web_step = next(step for step in plan.steps if step.tool_name == "web.open")

        self.assertEqual(web_step.arguments["url"], "https://example.com/report")
        self.assertIn("总结关键事实", web_step.arguments["query"])

    def test_fallback_planner_does_not_web_search_for_latest_market_price(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "查询 002436 最新价和涨跌幅",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]

        self.assertNotIn("web.search", tools)

    def test_fallback_planner_routes_social_sentiment_to_hot_mentions(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("贵州茅台 社媒舆情是否发酵", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]

        self.assertEqual(tools, ["web.hot_mentions"])
        self.assertEqual(plan.steps[0].arguments["keyword"], "贵州茅台")

    def test_fallback_planner_routes_xueqiu_hotspot_to_xueqiu_mentions(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("雪球热点 贵州茅台 是否发酵", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]

        self.assertEqual(tools, ["web.hot_mentions"])
        self.assertEqual(plan.steps[0].arguments["keyword"], "贵州茅台")
        self.assertEqual(plan.steps[0].arguments["platforms"], ["xueqiu_spot"])

    def test_fallback_planner_routes_explicit_akshare_dataset(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用 AkShare 查询 stock_zh_a_spot_em 数据", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]

        self.assertEqual(tools, ["data.describe_akshare_dataset", "data.get_akshare_data"])
        self.assertEqual(plan.steps[0].arguments["dataset"], "stock_zh_a_spot_em")
        self.assertEqual(plan.steps[1].arguments["dataset"], "stock_zh_a_spot_em")

    def test_llm_stock_plan_is_augmented_with_indicators_only(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 下周走势", settings=settings, policy=AgentExecutionPolicy(), llm_factory=FakeStockOnlyLLM, tool_registry=registry)
        internal_steps = [step for step in plan.steps if step.tool_name == "research.internal_analysis"]

        self.assertEqual([step.arguments.get("kind") for step in internal_steps], ["indicators"])

    def test_price_action_request_adds_knowledge_context(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 开盘溢价率为负，今天该走还是该留", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        knowledge_step = next(step for step in plan.steps if step.tool_name == "research.knowledge_context")

        self.assertIn("price-action", knowledge_step.arguments["collections"])

    def test_price_action_volume_price_request_adds_knowledge_context(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 量价背离和放量下跌风险", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        knowledge_step = next(step for step in plan.steps if step.tool_name == "research.knowledge_context")

        self.assertIn("price-action", knowledge_step.arguments["collections"])

    def test_price_action_ma_signal_request_adds_knowledge_context(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 60日均线不穿和线上缩量阴", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        knowledge_step = next(step for step in plan.steps if step.tool_name == "research.knowledge_context")

        self.assertIn("price-action", knowledge_step.arguments["collections"])

    def test_price_action_rsi_request_adds_knowledge_context(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 RSI低于20后出现底背离", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        knowledge_step = next(step for step in plan.steps if step.tool_name == "research.knowledge_context")

        self.assertIn("price-action", knowledge_step.arguments["collections"])

    def test_price_action_trend_execution_request_adds_knowledge_context(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 主升浪首次分歧，量比低于1.5但RPS强度高", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        knowledge_step = next(step for step in plan.steps if step.tool_name == "research.knowledge_context")

        self.assertIn("price-action", knowledge_step.arguments["collections"])

    def test_price_action_double_volume_request_adds_knowledge_context(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 左倍量抄底和右倍量逃顶，是否跌破倍量低点", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        knowledge_step = next(step for step in plan.steps if step.tool_name == "research.knowledge_context")

        self.assertIn("price-action", knowledge_step.arguments["collections"])

    def test_price_action_520_ma_request_adds_knowledge_context(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 520均线战法，20日线向上后的金叉买点和回踩买点", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        knowledge_step = next(step for step in plan.steps if step.tool_name == "research.knowledge_context")

        self.assertIn("price-action", knowledge_step.arguments["collections"])

    def test_price_action_washout_request_adds_knowledge_context(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("分析 002436 回踩均线洗盘和假跌破支撑，是否已经放量突破", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        knowledge_step = next(step for step in plan.steps if step.tool_name == "research.knowledge_context")

        self.assertIn("price-action", knowledge_step.arguments["collections"])

    def test_dsa_stock_request_adds_native_dsa_analysis(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用 DSA 分析 002436 买卖点", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        internal_steps = [step for step in plan.steps if step.tool_name == "research.internal_analysis"]

        self.assertIn("native_dsa", [step.arguments.get("kind") for step in internal_steps])
        self.assertNotIn("trade.submit_intent", [step.tool_name for step in plan.steps if step.kind == "tool"])

    def test_dsa_ma_strategy_adds_ma_signals_and_native_dsa(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用 DSA 分析 002436 均线金叉", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        internal_steps = [step for step in plan.steps if step.tool_name == "research.internal_analysis"]
        analyze_step = next(step for step in internal_steps if step.arguments.get("kind") == "analyze_signals")

        self.assertEqual([step.arguments.get("kind") for step in internal_steps], ["indicators", "analyze_signals", "native_dsa"])
        self.assertEqual(analyze_step.arguments["signals"], "ma")

    def test_wave_strategy_selects_wave_signals(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("波浪理论分析 002436", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        analyze_step = next(step for step in plan.steps if step.tool_name == "research.internal_analysis" and step.arguments.get("kind") == "analyze_signals")

        self.assertEqual(analyze_step.arguments["signals"], "wave")

    def test_das_followup_hot_theme_and_repricing_adds_supporting_evidence(self) -> None:
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
            "用DAS看上面2个股票的热点题材和预期重估",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
            reference_context=reference_context,
        )
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]
        factor_step = next(step for step in plan.steps if step.tool_name == "research.internal_analysis" and step.arguments.get("kind") == "factor_summary")

        self.assertIn("research.market_context", tools)
        self.assertIn("web.search", tools)
        self.assertIn("native_dsa", [step.arguments.get("kind") for step in plan.steps if step.tool_name == "research.internal_analysis"])
        self.assertEqual(factor_step.arguments["profile"], "fundamental_quality")

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
        self.assertEqual(market_step.arguments["dimensions"], ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors", "fund_flow", "catalysts"])

    def test_fallback_planner_adds_chan_context_for_chan_requests(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用缠论分析 002436", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]
        internal_steps = [step for step in plan.steps if step.tool_name == "research.internal_analysis"]
        analyze_step = next(step for step in internal_steps if step.arguments.get("kind") == "analyze_signals")

        self.assertEqual(tools, ["research.chan_context", "research.stock_context", "research.internal_analysis", "research.internal_analysis", "research.internal_analysis"])
        self.assertEqual(
            [step.arguments.get("kind") for step in internal_steps],
            ["indicators", "analyze_signals", "native_dsa"],
        )
        self.assertEqual(analyze_step.arguments["signals"], "chan")

    def test_chan_planner_passes_explicit_minute_periods(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用缠论 15分钟线 分析 002119", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        stock_step = next(step for step in plan.steps if step.tool_name == "research.stock_context")
        internal_steps = [step for step in plan.steps if step.tool_name == "research.internal_analysis"]

        self.assertEqual(stock_step.arguments["minute_periods"], ["15m"])
        self.assertTrue(internal_steps)
        self.assertTrue(all(step.arguments.get("minute_periods") == ["15m"] for step in internal_steps))

    def test_fallback_planner_routes_rule_generation_to_shared_tool(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("新增一个低位放量突破筛选规则", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)
        tool_step = next(step for step in plan.steps if step.kind == "tool")

        self.assertEqual(tool_step.tool_name, "research.rule_generation")
        self.assertEqual(tool_step.arguments["action"], "plan")

    def test_fallback_planner_routes_screened_analysis_to_workflow(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "用 price_volume_ma 筛选，并对筛选股票制定明天交易计划",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        step = next(step for step in plan.steps if step.kind == "tool")

        self.assertEqual(step.tool_name, "workflow.screened_stock_analysis")
        self.assertEqual(plan.analysis_mode, "batch")
        self.assertEqual(plan.natural_task["candidate_limit"], 20)

    def test_screened_analysis_modes_follow_user_request(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        grouped = build_agent_plan(
            "筛选结果按风险和信号强弱分组分析",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        per_stock = build_agent_plan(
            "筛选前5只，逐股详细分析并给出明天计划",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        per_stock_default_limit = build_agent_plan(
            "筛选结果逐股详细分析并给出明天计划",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        self.assertEqual(grouped.analysis_mode, "group")
        self.assertEqual(per_stock.analysis_mode, "per_stock")
        self.assertEqual(per_stock.natural_task["candidate_limit"], 5)
        self.assertEqual(per_stock_default_limit.natural_task["candidate_limit"], 5)

    def test_screened_analysis_workflow_dry_run_selects_candidates_without_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = build_default_tool_registry()
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_screening_results(
                [
                    ScreeningResult(
                        trade_date="20260520",
                        ts_code=f"00000{index}.SZ",
                        rule_name="price_volume_ma",
                        passed=True,
                        score=100 - index,
                        matched_conditions=["signal"],
                        failed_conditions=[],
                        metrics={"matched_signal_labels": ["放量"]},
                    )
                    for index in range(1, 7)
                ]
            )
            calls: list[list[str]] = []
            runner = SimpleNamespace(
                run=lambda argv, timeout=None: calls.append(list(argv))
                or SimpleNamespace(output="analysis should not run", returncode=0, status="done", argv=tuple(argv))
            )
            context = AgentToolContext(
                settings=SimpleNamespace(project_root=Path(tmp), db_path=Path(tmp) / "sats.duckdb"),
                storage=storage,
                resolver=SimpleNamespace(),
                policy=AgentExecutionPolicy(dry_run=True),
                command_runner=runner,
                trader=SimpleNamespace(),
                message="筛选结果按风险和信号强弱分组分析",
            )

            result = registry.execute(
                "workflow.screened_stock_analysis",
                {"message": context.message, "rule": "price_volume_ma", "trade_date": "20260520"},
                context,
            )

            self.assertEqual(result.status, "done")
            self.assertEqual(result.payload["analysis_mode"], "group")
            self.assertEqual(result.payload["candidate_count"], 6)
            self.assertEqual(result.payload["candidate_limit"], 20)
            self.assertTrue(result.payload["dry_run"])
            self.assertEqual(calls, [])

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

        self.assertEqual(market_step.arguments["dimensions"], ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors", "fund_flow", "catalysts"])

    def test_llm_market_plan_normalizes_legacy_dimensions_and_keeps_unknown_visible(self) -> None:
        class LegacyMarketDimensionsLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return LLMResponse(
                    content=(
                        '{"objective":"market review","steps":['
                        '{"step_id":"market","kind":"tool","title":"市场数据","tool_name":"research.market_context",'
                        '"arguments":{"dimensions":["breadth","sentiment","hot_sectors","fund_flow"]}},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        plan = build_agent_plan(
            "评价和分析这周A股走势，最近走势，预测下周走势，以及热点板块",
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            policy=AgentExecutionPolicy(),
            llm_factory=LegacyMarketDimensionsLLM,
            tool_registry=build_default_tool_registry(),
        )
        market_step = next(step for step in plan.steps if step.tool_name == "research.market_context")

        self.assertEqual(
            market_step.arguments["dimensions"],
            ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors", "fund_flow", "catalysts"],
        )

    def test_fallback_planner_factor_pick_does_not_add_analyze_signals(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("用因子选股 top20", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)

        self.assertIn("factor.pick", [step.tool_name for step in plan.steps if step.kind == "tool"])
        self.assertNotIn("analyze_signals", [step.arguments.get("kind") for step in plan.steps if step.tool_name == "research.internal_analysis"])

    def test_fallback_planner_routes_opportunity_request_to_existing_tools(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "选取10支明天大概率上涨的股票",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.market_context", "analysis.python_program"])
        self.assertNotIn("research.discover_opportunities", [step.tool_name for step in tool_steps])
        self.assertEqual(tool_steps[0].arguments["dimensions"], ["hot_sectors"])
        self.assertEqual(tool_steps[0].arguments["horizons"], ["tomorrow"])
        self.assertIn("limit = 10", tool_steps[1].arguments["code"])
        self.assertEqual(plan.success_criteria, ("基于已注册工具返回热点板块候选观察名单；缺失真实数据时说明具体缺口。",))

    def test_fallback_planner_hot_sector_opportunity_uses_market_context_and_program(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "根据热点板块，选取6支 下周大概率上涨的股票",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.market_context", "analysis.python_program"])
        self.assertEqual(tool_steps[0].arguments["dimensions"], ["hot_sectors"])
        self.assertEqual(tool_steps[0].arguments["horizons"], ["next_week"])
        self.assertIn("limit = 6", tool_steps[1].arguments["code"])
        self.assertNotIn("research.discover_opportunities", [step.tool_name for step in tool_steps])

    def test_fallback_planner_routes_theme_stock_list_without_opportunity_prediction(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)
        message = "A股中和内存，存储相关的股票。列出相关股票，以及简单信息，不用进行短期机会预测"

        plan = build_agent_plan(
            message,
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.theme_stock_list"])
        self.assertNotIn("research.discover_opportunities", [step.tool_name for step in tool_steps])

    def test_planner_does_not_route_explicit_opportunity_prediction_to_removed_tool(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "内存和存储相关A股，未来几天有上涨潜力的股票",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.market_context", "analysis.python_program"])
        self.assertNotIn("research.discover_opportunities", [step.tool_name for step in tool_steps])

    def test_fallback_planner_preserves_enabled_hot_sector_adjacent_routes(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        ranking = build_agent_plan(
            "热点板块排行",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        current_hot = build_agent_plan(
            "今天的强势板块有哪些",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        theme_list = build_agent_plan(
            "半导体相关股票有哪些",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        serenity = build_agent_plan(
            "Serenity 半导体卡位筛选",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        self.assertEqual([step.tool_name for step in ranking.steps if step.kind == "tool"], ["research.market_context"])
        self.assertEqual(next(step for step in ranking.steps if step.kind == "tool").arguments["dimensions"], ["hot_sectors"])
        self.assertEqual([step.tool_name for step in current_hot.steps if step.kind == "tool"], ["research.market_context"])
        self.assertEqual(next(step for step in current_hot.steps if step.kind == "tool").arguments["dimensions"], ["hot_sectors"])
        self.assertEqual([step.tool_name for step in theme_list.steps if step.kind == "tool"], ["research.theme_stock_list"])
        self.assertEqual([step.tool_name for step in serenity.steps if step.kind == "tool"], ["research.serenity_screen"])

    def test_llm_planner_theme_list_misroute_is_rewritten_to_theme_stock_list(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)
        message = "A股中和内存，存储相关的股票。列出相关股票，以及简单信息，不用进行短期机会预测"

        plan = build_agent_plan(
            message,
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=FakeThemeListOldPlanLLM,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.theme_stock_list"])

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
        self.assertEqual(normalize_agent_date("yesterday", today="20260701"), "20260630")
        self.assertEqual(normalize_agent_date("today", today="20260701"), "20260701")
        context = resolve_agent_time_context("预测兴森科技下周走势", today="20260606")
        historical_context = resolve_agent_time_context("评价昨天的大盘走势", today="20260626")
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
        relative = sanitize_agent_tool_arguments(
            "research.market_context",
            {"trade_date": "yesterday", "horizons": ["today"]},
            "评价A股昨天走势，预测今天走势",
            today="20260701",
        )

        self.assertEqual(context.horizons, ("next_week",))
        self.assertEqual(context.explicit_dates, ())
        self.assertEqual(historical_context.explicit_dates, ("20260625",))
        self.assertNotIn("trade_date", sanitized.arguments)
        self.assertEqual(sanitized.arguments["horizons"], ["next_week"])
        self.assertFalse(relative.error)
        self.assertEqual(relative.arguments["trade_date"], "20260630")
        self.assertEqual(relative.arguments["horizons"], ["today"])
        self.assertIn("日期格式无效", bad.error)

    def test_natural_trade_date_parses_relative_terms_with_explicit_date_priority(self) -> None:
        self.assertEqual(extract_natural_trade_date("评价昨天的大盘走势", today="20260626"), "20260625")
        self.assertEqual(extract_natural_trade_date("评价昨日的大盘走势", today="20260626"), "20260625")
        self.assertEqual(extract_natural_trade_date("评价前天的大盘走势", today="20260626"), "20260624")
        self.assertEqual(extract_natural_trade_date("评价今天的大盘走势", today="20260626"), "20260626")
        self.assertEqual(extract_natural_trade_date("评价今日的大盘走势", today="20260626"), "20260626")
        self.assertEqual(extract_natural_trade_date("2026-06-25 不是昨天", today="20260626"), "20260625")
        self.assertEqual(extract_natural_trade_date("A-share market yesterday", today="20260701"), "20260630")

    def test_fallback_planner_uses_natural_trade_date_for_historical_market_review(self) -> None:
        class FixedDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 6, 26, 12, 0, tzinfo=tz)

        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        with patch("sats.stock_question.datetime", FixedDateTime):
            plan = build_agent_plan(
                "评价昨天的大盘走势",
                settings=settings,
                policy=AgentExecutionPolicy(),
                llm_factory=None,
                tool_registry=registry,
            )

        market_step = next(step for step in plan.steps if step.tool_name == "research.market_context")
        self.assertEqual(market_step.arguments["trade_date"], "20260625")
        self.assertNotEqual(market_step.arguments.get("horizons"), ["today"])

    def test_fallback_planner_keeps_forecast_horizons_without_historical_trade_date(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "预测下周大盘走势",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        market_step = next(step for step in plan.steps if step.tool_name == "research.market_context")
        self.assertEqual(market_step.arguments["horizons"], ["next_week"])
        self.assertNotIn("trade_date", market_step.arguments)

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

    def test_planner_routes_theme_stock_return_requests_to_single_tool(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)
        message = "cvd金刚石散热相关股票，列出这些股票的6个月内涨跌幅情况"

        plan = build_agent_plan(
            message,
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=FakeThemeReturnsOldPlanLLM,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.theme_stock_returns"])
        self.assertEqual(tool_steps[0].arguments["query"], message)
        self.assertEqual(tool_steps[0].arguments["period"], "6m")
        self.assertNotIn("data.stock_daily", [step.tool_name for step in plan.steps])

    def test_planner_routes_sector_return_ranking_before_theme_returns(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)
        messages = [
            ("概念板块中一年跌幅最大的10个板块", "bottom"),
            ("过去一年A股跌幅最大的10个概念板块", "bottom"),
            ("一年内上涨最多的板块，10个", "top"),
        ]

        for message, direction in messages:
            with self.subTest(message=message):
                plan = build_agent_plan(
                    message,
                    settings=settings,
                    policy=AgentExecutionPolicy(),
                    llm_factory=None,
                    tool_registry=registry,
                )

                tool_steps = [step for step in plan.steps if step.kind == "tool"]
                self.assertEqual([step.tool_name for step in tool_steps], ["research.sector_return_ranking"])
                self.assertEqual(tool_steps[0].arguments["sector_type"], "concept")
                self.assertEqual(tool_steps[0].arguments["period"], "1y")
                self.assertEqual(tool_steps[0].arguments["direction"], direction)
                self.assertEqual(tool_steps[0].arguments["limit"], 10)

    def test_planner_routes_today_sector_decline_ranking_to_single_day(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "今天 最大跌幅板块",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.sector_return_ranking"])
        self.assertEqual(tool_steps[0].arguments["period"], "1d")
        self.assertEqual(tool_steps[0].arguments["direction"], "bottom")
        self.assertEqual(tool_steps[0].arguments["limit"], 10)
        self.assertTrue(_is_sector_return_ranking_text("今天 最大跌幅板块"))
        self.assertEqual(_sector_ranking_period_text("今天 最大跌幅板块"), "1d")

    def test_planner_and_tool_parse_recent_trading_day_sector_period(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "近5个交易日的热点板块，给出20个列表",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.sector_return_ranking"])
        self.assertEqual(tool_steps[0].arguments["period"], "5d")
        self.assertEqual(tool_steps[0].arguments["direction"], "top")
        self.assertEqual(tool_steps[0].arguments["limit"], 20)
        self.assertEqual(_sector_period_from_text("近5个交易日热点板块"), "5d")
        self.assertEqual(_sector_ranking_period_text("最近5个交易日板块涨幅排行"), "5d")

    def test_planner_routes_industry_sector_gain_ranking(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "过去半年涨幅最大的行业板块前5名",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.sector_return_ranking"])
        self.assertEqual(tool_steps[0].arguments["sector_type"], "industry")
        self.assertEqual(tool_steps[0].arguments["period"], "6m")
        self.assertEqual(tool_steps[0].arguments["direction"], "top")
        self.assertEqual(tool_steps[0].arguments["limit"], 5)

    def test_planner_routes_custom_program_request_to_catalog_then_python(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "找不到现成工具，计算自定义指标",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["data.astock_catalog", "analysis.python_program"])

    def test_planner_routes_peer_bottom_one_year_question_to_theme_returns(self) -> None:
        registry = build_default_tool_registry()
        message = "台基股份 近一年的涨幅是多少？为什么在功率器件中 台基股份是涨幅垫底"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_basic(
                pd.DataFrame([{"ts_code": "300046.SZ", "symbol": "300046", "name": "台基股份"}])
            )
            plan = build_agent_plan(
                message,
                settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10, db_path=db_path),
                policy=AgentExecutionPolicy(),
                llm_factory=FakeInvalidStockBasicLLM,
                tool_registry=registry,
            )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual([step.tool_name for step in tool_steps], ["research.theme_stock_returns"])
        self.assertEqual(tool_steps[0].arguments["query"], message)
        self.assertEqual(tool_steps[0].arguments["theme"], "功率器件")
        self.assertEqual(tool_steps[0].arguments["period"], "1y")
        self.assertEqual(tool_steps[0].arguments["symbols"], ["300046.SZ"])

    def test_planner_rewrites_invalid_stock_basic_fetch_to_safe_lookup(self) -> None:
        registry = build_default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_basic(
                pd.DataFrame([{"ts_code": "300046.SZ", "symbol": "300046", "name": "台基股份"}])
            )
            plan = build_agent_plan(
                "查询台基股份股票基础信息",
                settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10, db_path=db_path),
                policy=AgentExecutionPolicy(),
                llm_factory=FakeInvalidStockBasicLLM,
                tool_registry=registry,
            )

        tool_steps = [step for step in plan.steps if step.kind == "tool"]
        self.assertEqual(tool_steps[0].tool_name, "data.stock_basic")
        self.assertEqual(tool_steps[0].arguments["name"], "台基股份")
        self.assertNotEqual(tool_steps[0].arguments.get("operation"), "astock.stock_basic")

    def test_theme_stock_returns_merges_web_and_theme_universe_and_keeps_period_returns(self) -> None:
        registry = build_default_tool_registry()
        stock_basic = pd.DataFrame(
            [
                {"ts_code": "600172.SH", "symbol": "600172", "name": "黄河旋风"},
                {"ts_code": "301071.SZ", "symbol": "301071", "name": "力量钻石"},
                {"ts_code": "002046.SZ", "symbol": "002046", "name": "国机精工"},
                {"ts_code": "000519.SZ", "symbol": "000519", "name": "中兵红箭"},
                {"ts_code": "688028.SH", "symbol": "688028", "name": "沃尔德"},
            ]
        )
        web_payload = {
            "status": "ok",
            "query": "cvd金刚石散热A股股票",
            "backend": "rag",
            "answer": "黄河旋风（600172）、力量钻石（301071）、国机精工（002046）、中兵红箭（000519）、沃尔德（688028）",
            "sources": [{"id": "S1", "title": "CVD金刚石散热片主要上市公司介绍", "url": "https://example.com/a"}],
            "results": [],
        }
        theme_universe = SimpleNamespace(
            theme="CVD金刚石散热",
            stocks=(
                SimpleNamespace(ts_code="000519.SZ", name="中兵红箭", reason="中南钻石", source="llm_theme_universe"),
                SimpleNamespace(ts_code="600172.SH", name="黄河旋风", reason="CVD金刚石", source="llm_theme_universe"),
            ),
            warnings=(),
        )

        def fake_ensure(symbols, trade_date, **kwargs):
            symbol = symbols[0]
            return {
                symbol: {
                    "ts_code": symbol,
                    "name": stock_basic.set_index("ts_code").loc[symbol, "name"],
                    "requested_trade_date": trade_date,
                    "trade_date": "20260618",
                    "price_context": {"close": 10.0},
                    "period_returns": {
                        "6m": {
                            "start_trade_date": "20251222",
                            "end_trade_date": "20260618",
                            "pct_change": 12.3,
                        }
                    },
                    "missing_fields": [],
                }
            }

        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            llm_factory=lambda: SimpleNamespace(),
            message="cvd金刚石散热相关股票，列出这些股票的6个月内涨跌幅情况",
        )

        with (
            patch("sats.agent.tools.research_tools.AStockDataProvider", return_value=SimpleNamespace(load_stock_basic=lambda storage=None: stock_basic)),
            patch("sats.agent.tools.research_tools.web_search", return_value=web_payload),
            patch("sats.agent.tools.research_tools.resolve_theme_universe", return_value=theme_universe),
            patch("sats.agent.tools.research_tools.ensure_stock_analysis_data", side_effect=fake_ensure),
        ):
            result = registry.execute(
                "research.theme_stock_returns",
                {"query": context.message, "period": "6m", "limit": 30},
                context,
            )

        self.assertEqual(result.status, "done")
        payload = result.payload["theme_stock_returns"]
        self.assertEqual(payload["candidate_count"], 5)
        self.assertEqual([item["ts_code"] for item in payload["stocks"]], ["600172.SH", "301071.SZ", "002046.SZ", "000519.SZ", "688028.SH"])
        self.assertTrue(all(item["period_returns"]["6m"]["pct_change"] == 12.3 for item in payload["stocks"]))
        self.assertEqual(payload["coverage"]["returned_count"], 5)

    def test_theme_stock_list_returns_basic_info_without_short_up_discovery(self) -> None:
        registry = build_default_tool_registry()
        stock_basic = pd.DataFrame(
            [
                {"ts_code": "603986.SH", "symbol": "603986", "name": "兆易创新", "industry": "半导体", "market": "主板", "exchange": "SSE"},
                {"ts_code": "688525.SH", "symbol": "688525", "name": "佰维存储", "industry": "半导体", "market": "科创板", "exchange": "SSE"},
            ]
        )
        theme_universe = SimpleNamespace(
            theme="存储芯片",
            source="ths_sector",
            matched_sector="存储芯片",
            count=2,
            stocks=(
                SimpleNamespace(ts_code="603986.SH", name="兆易创新", relation_type="ths_member", source="ths_sector", reason=""),
                SimpleNamespace(ts_code="688525.SH", name="佰维存储", relation_type="ths_member", source="ths_sector", reason=""),
            ),
            warnings=(),
        )
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            llm_factory=None,
            message="A股中和内存，存储相关的股票。列出相关股票，以及简单信息，不用进行短期机会预测",
        )

        with (
            patch("sats.agent.tools.research_tools.AStockDataProvider", return_value=SimpleNamespace(load_stock_basic=lambda storage=None: stock_basic)),
            patch("sats.agent.tools.research_tools.resolve_theme_universe", return_value=theme_universe) as resolver,
        ):
            result = registry.execute(
                "research.theme_stock_list",
                {"query": context.message, "limit": 50},
                context,
            )

        self.assertEqual(result.status, "done")
        resolver.assert_called_once()
        payload = result.payload["theme_stock_list"]
        self.assertEqual(payload["theme"], "存储芯片")
        self.assertEqual(payload["source"], "ths_sector")
        self.assertEqual(payload["matched_sector"], "存储芯片")
        self.assertEqual(payload["theme_universe_count"], 2)
        self.assertEqual([item["ts_code"] for item in payload["stocks"]], ["603986.SH", "688525.SH"])
        self.assertEqual(payload["stocks"][0]["industry"], "半导体")
        self.assertIn("未运行 short_up", payload["policy"])

    def test_theme_stock_returns_includes_focus_symbol_and_ranking(self) -> None:
        registry = build_default_tool_registry()
        stock_basic = pd.DataFrame(
            [
                {"ts_code": "300046.SZ", "symbol": "300046", "name": "台基股份"},
                {"ts_code": "300373.SZ", "symbol": "300373", "name": "扬杰科技"},
                {"ts_code": "300623.SZ", "symbol": "300623", "name": "捷捷微电"},
            ]
        )
        web_payload = {"status": "ok", "query": "功率器件 A股股票", "backend": "rag", "answer": "", "sources": [], "results": []}
        theme_universe = SimpleNamespace(
            theme="功率器件",
            stocks=(
                SimpleNamespace(ts_code="300373.SZ", name="扬杰科技", reason="功率半导体", source="llm_theme_universe"),
                SimpleNamespace(ts_code="300623.SZ", name="捷捷微电", reason="功率器件", source="llm_theme_universe"),
            ),
            warnings=(),
        )
        returns = {"300373.SZ": 80.0, "300623.SZ": 30.0, "300046.SZ": -10.0}

        def fake_ensure(symbols, trade_date, **kwargs):
            symbol = symbols[0]
            return {
                symbol: {
                    "ts_code": symbol,
                    "name": stock_basic.set_index("ts_code").loc[symbol, "name"],
                    "requested_trade_date": trade_date,
                    "trade_date": "20260618",
                    "period_returns": {
                        "1y": {
                            "start_trade_date": "20250618",
                            "end_trade_date": "20260618",
                            "pct_change": returns[symbol],
                        }
                    },
                    "missing_fields": [],
                }
            }

        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            llm_factory=lambda: SimpleNamespace(),
            message="台基股份 近一年的涨幅是多少？为什么在功率器件中 台基股份是涨幅垫底",
        )

        with (
            patch("sats.agent.tools.research_tools.AStockDataProvider", return_value=SimpleNamespace(load_stock_basic=lambda storage=None: stock_basic)),
            patch("sats.agent.tools.research_tools.web_search", return_value=web_payload),
            patch("sats.agent.tools.research_tools.resolve_theme_universe", return_value=theme_universe),
            patch("sats.agent.tools.research_tools.ensure_stock_analysis_data", side_effect=fake_ensure),
        ):
            result = registry.execute(
                "research.theme_stock_returns",
                {"query": context.message, "theme": "功率器件", "symbols": ["300046.SZ"], "period": "1y", "limit": 30},
                context,
            )

        payload = result.payload["theme_stock_returns"]
        self.assertEqual(result.status, "done")
        self.assertEqual(payload["focus_symbols"], ["300046.SZ"])
        self.assertEqual(payload["period"], "1y")
        self.assertEqual({item["ts_code"] for item in payload["stocks"]}, {"300046.SZ", "300373.SZ", "300623.SZ"})
        taiji = next(item for item in payload["ranking"] if item["ts_code"] == "300046.SZ")
        self.assertEqual(taiji["rank"], 3)
        self.assertEqual(taiji["peer_count"], 3)
        self.assertTrue(taiji["is_bottom"])
        self.assertEqual(taiji["pct_change"], -10.0)

    def test_sector_return_ranking_computes_bottom_concepts_from_tushare_rows(self) -> None:
        registry = build_default_tool_registry()

        class Provider:
            def fetch_tushare_dataset(self, dataset, params=None, *, fields=None, limit=200):
                params = params or {}
                if dataset == "ths_index":
                    return {
                        "dataset": dataset,
                        "rows": [
                            {"ts_code": "885001.TI", "name": "AI算力"},
                            {"ts_code": "885002.TI", "name": "单日脉冲"},
                            {"ts_code": "885003.TI", "name": "低空经济"},
                        ],
                        "data_source": "fake_ths_index",
                        "missing_fields": [],
                    }
                closes = {
                    "885001.TI": [10, 8],
                    "885002.TI": [10, 9],
                    "885003.TI": [10, 12],
                }[params["ts_code"]]
                return {
                    "dataset": dataset,
                    "rows": [
                        {"ts_code": params["ts_code"], "trade_date": "20250620", "close": closes[0]},
                        {"ts_code": params["ts_code"], "trade_date": "20260620", "close": closes[1]},
                    ],
                    "data_source": "fake_ths_daily",
                    "missing_fields": [],
                }

        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="概念板块中一年跌幅最大的10个板块",
        )

        with patch("sats.agent.tools.research_tools.AStockDataProvider", return_value=Provider()):
            result = registry.execute(
                "research.sector_return_ranking",
                {"query": context.message, "period": "1y", "direction": "bottom", "limit": 2},
                context,
            )

        payload = result.payload["sector_return_ranking"]
        self.assertEqual(result.status, "done")
        self.assertEqual([item["sector_code"] for item in payload["ranking"]], ["885001.TI", "885002.TI"])
        self.assertEqual(payload["ranking"][0]["name"], "AI算力")
        self.assertEqual(payload["ranking"][0]["pct_change"], -20.0)
        self.assertEqual(payload["coverage"]["computed_count"], 3)

    def test_sector_return_ranking_today_uses_single_day_pct_change(self) -> None:
        registry = build_default_tool_registry()

        class Provider:
            def __init__(self) -> None:
                self.daily_params = []

            def fetch_tushare_dataset(self, dataset, params=None, *, fields=None, limit=200):
                params = params or {}
                if dataset == "ths_index":
                    return {
                        "dataset": dataset,
                        "rows": [
                            {"ts_code": "885001.TI", "name": "AI算力"},
                            {"ts_code": "885002.TI", "name": "低空经济"},
                            {"ts_code": "885003.TI", "name": "机器人"},
                        ],
                        "data_source": "fake_ths_index",
                        "missing_fields": [],
                    }
                self.daily_params.append(dict(params))
                pct_changes = {"885001.TI": -1.2, "885002.TI": -3.4, "885003.TI": 0.5}
                closes = {"885001.TI": 100.0, "885002.TI": 200.0, "885003.TI": 300.0}
                return {
                    "dataset": dataset,
                    "rows": [
                        {
                            "ts_code": params["ts_code"],
                            "trade_date": "20260701",
                            "close": closes[params["ts_code"]],
                            "pct_change": pct_changes[params["ts_code"]],
                        }
                    ],
                    "data_source": "fake_ths_daily",
                    "missing_fields": [],
                }

        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="今天 最大跌幅板块",
        )
        provider = Provider()

        with patch("sats.agent.tools.research_tools.AStockDataProvider", return_value=provider):
            result = registry.execute(
                "research.sector_return_ranking",
                {"query": context.message, "period": "1y", "direction": "bottom", "trade_date": "20260702", "limit": 2},
                context,
            )

        payload = result.payload["sector_return_ranking"]
        self.assertEqual(result.status, "done")
        self.assertEqual(payload["period"], "1d")
        self.assertEqual(payload["requested_trade_date"], "20260702")
        self.assertEqual(payload["actual_trade_date"], "20260701")
        self.assertEqual(payload["start_date"], "20260701")
        self.assertEqual(payload["lookup_start_date"], "20260618")
        self.assertTrue(all(params["start_date"] == "20260618" for params in provider.daily_params))
        self.assertEqual([item["sector_code"] for item in payload["ranking"]], ["885002.TI", "885001.TI"])
        self.assertEqual(payload["ranking"][0]["pct_change"], -3.4)
        self.assertEqual(payload["ranking"][0]["sample_days"], 1)
        self.assertEqual(payload["ranking"][0]["start_trade_date"], "20260701")

    def test_analysis_python_program_executes_readonly_resolver_allows_imports_and_rejects_dangerous_calls(self) -> None:
        registry = build_default_tool_registry()

        class Resolver:
            def load_stock_daily(self, symbols, *, start_date, end_date):
                frame = pd.DataFrame([{"ts_code": symbols[0], "trade_date": start_date, "close": 10.0}])
                frame.attrs["market_data_provenance"] = [{"source": "fake_daily"}]
                return frame

        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=Resolver(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="运行程序",
            observations=(
                AgentObservation(
                    step_id="loop_1_research_market_context",
                    kind="tool",
                    status="done",
                    content="{}",
                    payload={
                        "tool_name": "research.market_context",
                        "result": {
                            "status": "done",
                            "content": "{}",
                            "payload": {
                                "market_context": {
                                    "hot_sectors": [
                                        {"name": "AI算力"},
                                        {"sector_name": "半导体"},
                                    ]
                                }
                            },
                        },
                    },
                ),
            ),
        )
        code = (
            "def run(context):\n"
            "    daily = resolver.load_stock_daily(['000001'], start_date='20260620', end_date='20260620')\n"
            "    return {'rows': daily.to_dict('records'), 'provenance': daily.attrs.get('market_data_provenance')}\n"
        )
        observation_code = (
            "def run(context):\n"
            "    obs = context['observations_by_step']['loop_1_research_market_context']\n"
            "    payload = obs['payload']['result']['payload']\n"
            "    sectors = payload.get('market_context', {}).get('hot_sectors') or []\n"
            "    rows = []\n"
            "    for sector in sectors:\n"
            "        rows.append({'name': sector.get('name') or sector.get('sector_name') or ''})\n"
            "    return {'rows': rows, 'observation_count': len(context.get('observations') or [])}\n"
        )

        ok = registry.execute("analysis.python_program", {"task": "取日线", "code": code}, context)
        observed = registry.execute("analysis.python_program", {"task": "提取热点板块", "code": observation_code}, context)
        import_code = (
            "import json\n"
            "import math\n"
            "def run(context):\n"
            "    return {'value': json.loads('{\"x\": 4}')['x'], 'root': math.sqrt(9)}\n"
        )
        os_import_only = registry.execute("analysis.python_program", {"task": "导入 os", "code": "import os\nRESULT = {'ok': True}"}, context)
        imported = registry.execute("analysis.python_program", {"task": "导入纯库", "code": import_code}, context)
        bad_open = registry.execute("analysis.python_program", {"task": "坏程序", "code": "RESULT = open('x')"}, context)
        bad_exec = registry.execute("analysis.python_program", {"task": "坏程序", "code": "exec('RESULT = 1')"}, context)
        bad_import_func = registry.execute("analysis.python_program", {"task": "坏程序", "code": "RESULT = __import__('json')"}, context)
        bad_os = registry.execute("analysis.python_program", {"task": "坏程序", "code": "import os\nRESULT = os.system('echo hi')"}, context)
        bad_from_os = registry.execute("analysis.python_program", {"task": "坏程序", "code": "from os import system\nRESULT = system('echo hi')"}, context)
        bad_star_os = registry.execute("analysis.python_program", {"task": "坏程序", "code": "from os import *\nRESULT = 1"}, context)
        bad_top_context = registry.execute("analysis.python_program", {"task": "坏程序", "code": "rows = context.get('observations')\nRESULT = {'rows': rows}"}, context)

        self.assertEqual(ok.status, "done")
        self.assertEqual(ok.payload["python_program"]["rows"][0]["ts_code"], "000001")
        self.assertEqual(observed.status, "done")
        self.assertEqual([row["name"] for row in observed.payload["python_program"]["rows"]], ["AI算力", "半导体"])
        self.assertEqual(observed.payload["python_program"]["observation_count"], 1)
        self.assertEqual(os_import_only.status, "done")
        self.assertEqual(imported.status, "done")
        self.assertEqual(imported.payload["python_program"]["value"], 4)
        self.assertEqual(imported.payload["python_program"]["root"], 3.0)
        self.assertEqual(bad_open.status, "error")
        self.assertIn("forbids open()", bad_open.payload["python_program"]["error"])
        self.assertEqual(bad_exec.status, "error")
        self.assertIn("forbids exec()", bad_exec.payload["python_program"]["error"])
        self.assertEqual(bad_import_func.status, "error")
        self.assertIn("forbids __import__()", bad_import_func.payload["python_program"]["error"])
        self.assertEqual(bad_os.status, "error")
        self.assertIn("forbids os access", bad_os.payload["python_program"]["error"])
        self.assertEqual(bad_from_os.status, "error")
        self.assertIn("forbids os access", bad_from_os.payload["python_program"]["error"])
        self.assertEqual(bad_star_os.status, "error")
        self.assertIn("forbids os access", bad_star_os.payload["python_program"]["error"])
        self.assertEqual(bad_top_context.status, "error")
        self.assertIn("def run(context)", bad_top_context.payload["python_program"]["error"])

    def test_hot_sector_fallback_program_ranks_candidates_from_existing_tools(self) -> None:
        registry = build_default_tool_registry()
        plan = build_agent_plan(
            "根据热点板块，选取2支下周大概率上涨的股票",
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            policy=AgentExecutionPolicy(),
            llm_factory=None,
            tool_registry=registry,
        )
        program_step = next(step for step in plan.steps if step.tool_name == "analysis.python_program")

        class Resolver:
            def load_stock_daily(self, symbols, *, start_date, end_date):
                rows = []
                prices = {
                    "000001.SZ": [10, 10.4, 10.8, 11.2, 11.8, 12.5],
                    "000002.SZ": [20, 20.1, 20.2, 20.3, 20.4, 20.5],
                    "000003.SZ": [30, 29.8, 29.9, 30.0, 30.1, 30.0],
                }
                vols = {
                    "000001.SZ": [100, 105, 110, 115, 120, 180],
                    "000002.SZ": [100, 100, 100, 100, 100, 100],
                    "000003.SZ": [100, 95, 90, 92, 94, 80],
                }
                for symbol in symbols:
                    for index, close in enumerate(prices.get(symbol, []), start=1):
                        rows.append(
                            {
                                "ts_code": symbol,
                                "trade_date": f"202606{index:02d}",
                                "close": close,
                                "vol": vols[symbol][index - 1],
                            }
                        )
                frame = pd.DataFrame(rows)
                frame.attrs["data_source"] = "fake_stock_daily"
                return frame

        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=Resolver(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="根据热点板块，选取2支下周大概率上涨的股票",
            observations=(
                AgentObservation(
                    step_id="market_context",
                    kind="tool",
                    status="done",
                    content="market",
                    payload={
                        "tool_name": "research.market_context",
                        "result": {
                            "status": "done",
                            "content": "{}",
                            "payload": {
                                "market_context": {
                                    "requested_dimensions": ["hot_sectors"],
                                    "hot_sector_context": {
                                        "stock_hot_sectors": {
                                            "000001.SZ": [{"name": "AI算力", "stock_name": "强势一号", "heat_score": 20}],
                                            "000002.SZ": [{"name": "半导体", "stock_name": "稳健二号", "heat_score": 18}],
                                            "000003.SZ": [{"name": "机器人", "stock_name": "弱势三号", "heat_score": 5}],
                                        },
                                        "missing_fields": [],
                                    },
                                    "data_sources": {"hot_sector_context": "fake_hot_sector"},
                                }
                            },
                        },
                    },
                ),
            ),
        )

        result = registry.execute("analysis.python_program", program_step.arguments, context)
        payload = result.payload["python_program"]

        self.assertEqual(result.status, "done")
        self.assertEqual(payload["kind"], "hot_sector_candidates")
        self.assertEqual(payload["requested_limit"], 2)
        self.assertEqual(payload["returned_count"], 2)
        self.assertEqual([row["ts_code"] for row in payload["rows"]], ["000001.SZ", "000002.SZ"])
        self.assertEqual(payload["rows"][0]["name"], "强势一号")
        self.assertEqual(payload["rows"][0]["rank"], 1)
        self.assertIn("AI算力", payload["rows"][0]["hot_sectors"])
        self.assertIn("不构成投资建议", payload["risk_notice"])

    def test_generated_tool_loader_only_registers_readonly_safe_specs(self) -> None:
        from sats.agent.tools.generated.loader import load_generated_tool_specs

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "good_tool.py").write_text(
                "from sats.agent.tools.base import AgentToolSpec, AgentToolResult, object_schema\n"
                "def _run(context, arguments):\n"
                "    return AgentToolResult(status='done', content='ok', payload={'ok': True}, data_names=('generated',))\n"
                "def tool_specs():\n"
                "    return [AgentToolSpec(name='generated.good', description='good', side_effect='readonly', input_schema=object_schema(), executor=_run, metadata={'writes_db': False})]\n",
                encoding="utf-8",
            )
            (root / "bad_tool.py").write_text(
                "import os\n"
                "from sats.agent.tools.base import AgentToolSpec\n"
                "def tool_specs():\n"
                "    return [AgentToolSpec(name='generated.bad', description='bad')]\n",
                encoding="utf-8",
            )

            specs = load_generated_tool_specs(root)

        self.assertEqual([spec.name for spec in specs], ["generated.good"])

    def test_synthesis_digest_keeps_all_theme_stock_return_rows(self) -> None:
        stocks = [
            {
                "ts_code": symbol,
                "name": name,
                "trade_date": "20260618",
                "period_returns": {"6m": {"start_trade_date": "20251222", "end_trade_date": "20260618", "pct_change": index * 3.0}},
                "missing_fields": [],
            }
            for index, (symbol, name) in enumerate(
                [
                    ("600172.SH", "黄河旋风"),
                    ("301071.SZ", "力量钻石"),
                    ("002046.SZ", "国机精工"),
                    ("000519.SZ", "中兵红箭"),
                    ("688028.SH", "沃尔德"),
                ],
                start=1,
            )
        ]
        payload = {
            "status": "ok",
            "theme_stock_returns": {
                "query": "cvd金刚石散热相关股票",
                "theme": "CVD金刚石散热",
                "period": "6m",
                "candidate_count": 5,
                "stocks": stocks,
                "ranking": [
                    {
                        "ts_code": item["ts_code"],
                        "name": item["name"],
                        "rank": index,
                        "peer_count": 5,
                        "pct_change": item["period_returns"]["6m"]["pct_change"],
                    }
                    for index, item in enumerate(stocks, start=1)
                ],
                "coverage": {"requested_count": 5, "returned_count": 5, "missing_symbols": []},
            },
        }
        observation = AgentObservation(
            step_id="theme_stock_returns",
            kind="tool",
            status="done",
            content=json.dumps(payload, ensure_ascii=False),
            payload={
                "tool_name": "research.theme_stock_returns",
                "arguments": {"query": "cvd金刚石散热相关股票，列出这些股票的6个月内涨跌幅情况"},
                "result": {"payload": payload, "data_names": ["theme_stock_returns"]},
                "data_names": ["theme_stock_returns"],
            },
        )
        plan = AgentPlan(objective="cvd金刚石散热相关股票，列出这些股票的6个月内涨跌幅情况")

        digest = _evidence_digest((observation,))
        compact = _compact_evidence_digest(digest, compact_mode="gateway_compact")
        ultra = _compact_evidence_digest(digest, compact_mode="ultra_compact")
        result = synthesize_agent_result(
            message=plan.objective,
            plan=plan,
            observations=(observation,),
            skills=(),
            settings=SimpleNamespace(openai_model="m"),
            llm_factory=None,
        )

        self.assertEqual(len(digest["theme_stock_returns"]["stocks"]), 5)
        self.assertEqual(len(compact["theme_stock_returns"]["stocks"]), 5)
        self.assertEqual(len(ultra["theme_stock_returns"]["stocks"]), 5)
        self.assertEqual([item["rank"] for item in ultra["theme_stock_returns"]["stocks"]], [1, 2, 3, 4, 5])
        self.assertNotIn("truncated_json", json.dumps(compact["theme_stock_returns"], ensure_ascii=False))
        self.assertNotIn("truncated_json", json.dumps(ultra["theme_stock_returns"], ensure_ascii=False))
        for item in stocks:
            self.assertIn(item["ts_code"], result.content)
            self.assertIn(item["name"], result.content)
            self.assertIn(item["ts_code"], json.dumps(ultra["theme_stock_returns"], ensure_ascii=False))
        self.assertIn("20251222", result.content)
        self.assertIn("20260618", result.content)
        self.assertNotIn("主题股票池或区间涨跌幅数据缺失", result.content)

    def test_synthesis_formats_sector_return_ranking_rows(self) -> None:
        payload = {
            "status": "ok",
            "sector_return_ranking": {
                "query": "概念板块中一年跌幅最大的10个板块",
                "source": "ths",
                "sector_type": "concept",
                "period": "1y",
                "direction": "bottom",
                "ranking": [
                    {
                        "rank": 1,
                        "sector_code": "885001.TI",
                        "name": "AI算力",
                        "start_trade_date": "20250620",
                        "end_trade_date": "20260620",
                        "start_close": 10,
                        "end_close": 8,
                        "pct_change": -20.0,
                        "sample_days": 2,
                        "data_source": "fake_ths_daily",
                    }
                ],
                "coverage": {"sector_count": 1, "computed_count": 1, "returned_count": 1, "missing_count": 0},
            },
        }
        observation = AgentObservation(
            step_id="sector_return_ranking",
            kind="tool",
            status="done",
            content=json.dumps(payload, ensure_ascii=False),
            payload={
                "tool_name": "research.sector_return_ranking",
                "arguments": {"query": "概念板块中一年跌幅最大的10个板块"},
                "result": {"payload": payload, "data_names": ["sector_return_ranking"]},
                "data_names": ["sector_return_ranking"],
            },
        )
        plan = AgentPlan(objective="概念板块中一年跌幅最大的10个板块")

        result = synthesize_agent_result(
            message=plan.objective,
            plan=plan,
            observations=(observation,),
            skills=(),
            settings=SimpleNamespace(openai_model="m"),
            llm_factory=None,
        )

        self.assertIn("板块指数涨跌幅排行表", result.content)
        self.assertIn("885001.TI", result.content)
        self.assertIn("AI算力", result.content)
        self.assertIn("-20.0", result.content)
        self.assertNotIn("主题股票池或区间涨跌幅数据缺失", result.content)

    def test_synthesis_formats_single_day_sector_ranking_rows(self) -> None:
        payload = {
            "status": "ok",
            "sector_return_ranking": {
                "query": "今天 最大跌幅板块",
                "source": "ths",
                "sector_type": "concept",
                "period": "1d",
                "direction": "bottom",
                "requested_trade_date": "20260702",
                "actual_trade_date": "20260701",
                "ranking": [
                    {
                        "rank": 1,
                        "sector_code": "885002.TI",
                        "name": "低空经济",
                        "trade_date": "20260701",
                        "close": 200.0,
                        "pct_change": -3.4,
                        "sample_days": 1,
                        "data_source": "fake_ths_daily",
                    }
                ],
                "coverage": {"sector_count": 1, "computed_count": 1, "returned_count": 1, "missing_count": 0},
            },
        }
        observation = AgentObservation(
            step_id="sector_return_ranking",
            kind="tool",
            status="done",
            content=json.dumps(payload, ensure_ascii=False),
            payload={
                "tool_name": "research.sector_return_ranking",
                "arguments": {"query": "今天 最大跌幅板块"},
                "result": {"payload": payload, "data_names": ["sector_return_ranking"]},
                "data_names": ["sector_return_ranking"],
            },
        )
        plan = AgentPlan(objective="今天 最大跌幅板块")

        result = synthesize_agent_result(
            message=plan.objective,
            plan=plan,
            observations=(observation,),
            skills=(),
            settings=SimpleNamespace(openai_model="m"),
            llm_factory=None,
        )

        self.assertIn("板块当日涨跌幅排行表", result.content)
        self.assertIn("当日涨跌幅", result.content)
        self.assertIn("低空经济", result.content)
        self.assertNotIn("区间涨跌幅", result.content)

    def test_synthesis_uses_latest_nonempty_sector_ranking_despite_errors(self) -> None:
        empty_payload = {
            "status": "ok",
            "sector_return_ranking": {
                "query": "近5个交易日热点板块",
                "source": "em",
                "sector_type": "concept",
                "period": "近5个交易日",
                "direction": "top",
                "ranking": [],
                "coverage": {"sector_count": 1000, "computed_count": 0, "returned_count": 0, "missing_count": 1000},
            },
        }
        latest_payload = {
            "status": "ok",
            "sector_return_ranking": {
                "query": "近5个交易日热点板块",
                "source": "em",
                "sector_type": "concept",
                "period": "5d",
                "direction": "top",
                "trade_date": "20260708",
                "start_date": "20260703",
                "ranking": [
                    {
                        "rank": 1,
                        "sector_code": "BK1348.DC",
                        "name": "辅料",
                        "start_trade_date": "20260703",
                        "end_trade_date": "20260708",
                        "start_close": 2760.68,
                        "end_close": 2946.71,
                        "pct_change": 6.7386,
                        "sample_days": 4,
                        "data_source": "tushare_dc_daily",
                    }
                ],
                "coverage": {"sector_count": 1000, "computed_count": 954, "returned_count": 20, "missing_count": 46},
                "missing": [{"sector_code": f"BK{i:04d}.DC", "name": f"缺失{i}", "reason": "sector_daily_insufficient"} for i in range(80)],
            },
        }
        observations = (
            AgentObservation(
                step_id="loop_1_research_sector_return_ranking",
                kind="tool",
                status="done",
                content=json.dumps(empty_payload, ensure_ascii=False),
                payload={
                    "tool_name": "research.sector_return_ranking",
                    "arguments": {"query": "近5个交易日热点板块", "period": "近5个交易日"},
                    "result": {"payload": empty_payload, "data_names": ["sector_return_ranking"]},
                    "data_names": ["sector_return_ranking"],
                },
            ),
            AgentObservation(
                step_id="loop_2_analysis_python_program",
                kind="tool",
                status="error",
                content="name 'context' is not defined",
                payload={"tool_name": "analysis.python_program", "arguments": {}, "result": {"payload": {"python_program": {"status": "error"}}}},
            ),
            AgentObservation(
                step_id="loop_3_research_sector_return_ranking",
                kind="tool",
                status="done",
                content=json.dumps(latest_payload, ensure_ascii=False),
                payload={
                    "tool_name": "research.sector_return_ranking",
                    "arguments": {"query": "近5个交易日热点板块", "period": "5d"},
                    "result": {"payload": latest_payload, "data_names": ["sector_return_ranking"]},
                    "data_names": ["sector_return_ranking"],
                },
            ),
            AgentObservation(step_id="max_iterations", kind="runtime", status="error", content="conversation loop reached max_iterations=6"),
        )
        plan = AgentPlan(objective="近5个交易日的热点板块，给出20个列表")

        result = synthesize_agent_result(
            message=plan.objective,
            plan=plan,
            observations=observations,
            skills=(),
            settings=SimpleNamespace(openai_model="m"),
            llm_factory=None,
        )

        self.assertIn("板块指数涨跌幅排行表", result.content)
        self.assertIn("BK1348.DC", result.content)
        self.assertIn("辅料", result.content)
        self.assertIn("6.7386", result.content)
        self.assertNotIn("板块指数区间涨跌幅排行数据缺失", result.content)

    def test_synthesis_forces_sector_ranking_fallback_even_when_llm_returns_wrong_candidate_answer(self) -> None:
        class WrongSectorLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return LLMResponse(content="有效候选数量为0，网络搜索仅提供零散的个股案例。")

        payload = {
            "status": "ok",
            "sector_return_ranking": {
                "query": "过去一年A股跌幅最大的10个概念板块",
                "source": "ths",
                "sector_type": "concept",
                "period": "1y",
                "direction": "bottom",
                "ranking": [
                    {
                        "rank": 1,
                        "sector_code": "885573.TI",
                        "name": "猪肉",
                        "start_trade_date": "20250625",
                        "end_trade_date": "20260625",
                        "pct_change": -23.447,
                        "data_source": "tushare_ths_daily",
                    }
                ],
                "coverage": {"sector_count": 412, "computed_count": 408, "returned_count": 10, "missing_count": 4},
            },
        }
        observation = AgentObservation(
            step_id="sector_return_ranking",
            kind="tool",
            status="done",
            content=json.dumps(payload, ensure_ascii=False),
            payload={
                "tool_name": "research.sector_return_ranking",
                "arguments": {"query": "过去一年A股跌幅最大的10个概念板块"},
                "result": {"payload": payload, "data_names": ["sector_return_ranking"]},
                "data_names": ["sector_return_ranking"],
            },
        )
        plan = AgentPlan(objective="过去一年A股跌幅最大的10个概念板块")

        result = synthesize_agent_result(
            message=plan.objective,
            plan=plan,
            observations=(observation,),
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=WrongSectorLLM,
        )

        self.assertFalse(result.used_llm)
        self.assertIn("板块指数涨跌幅排行表", result.content)
        self.assertIn("885573.TI", result.content)
        self.assertIn("猪肉", result.content)
        self.assertNotIn("有效候选数量", result.content)
        self.assertNotIn("网络搜索", result.content)
        self.assertNotIn("个股案例", result.content)

    def test_synthesis_empty_sector_ranking_reports_coverage_not_web_candidates(self) -> None:
        payload = {
            "status": "ok",
            "sector_return_ranking": {
                "query": "过去一年A股跌幅最大的10个概念板块",
                "source": "ths",
                "sector_type": "concept",
                "period": "1y",
                "direction": "bottom",
                "ranking": [],
                "coverage": {"sector_count": 10, "computed_count": 0, "returned_count": 0, "missing_count": 10},
                "missing": [{"sector_code": "885001.TI", "name": "AI算力", "reason": "sector_daily_insufficient"}],
            },
        }
        observation = AgentObservation(
            step_id="sector_return_ranking",
            kind="tool",
            status="done",
            content=json.dumps(payload, ensure_ascii=False),
            payload={
                "tool_name": "research.sector_return_ranking",
                "arguments": {"query": "过去一年A股跌幅最大的10个概念板块"},
                "result": {"payload": payload, "data_names": ["sector_return_ranking"]},
                "data_names": ["sector_return_ranking"],
            },
        )
        plan = AgentPlan(objective="过去一年A股跌幅最大的10个概念板块")

        result = synthesize_agent_result(
            message=plan.objective,
            plan=plan,
            observations=(observation,),
            skills=(),
            settings=SimpleNamespace(openai_model="m"),
            llm_factory=None,
        )

        self.assertIn("computed_count", result.content)
        self.assertIn("sector_daily_insufficient", result.content)
        self.assertNotIn("网络搜索", result.content)
        self.assertNotIn("个股候选", result.content)
        self.assertNotIn("candidate_count", result.content)

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
        stock = result.payload["stock_context"]["stocks"][0]
        self.assertNotIn("optional_minute_15m", stock["missing_fields"])
        self.assertEqual(stock["optional_fields_not_requested"], ["optional_minute_15m", "optional_minute_30m"])
        self.assertEqual(result.payload["stock_context"]["optional_fields_not_requested"], ["optional_minute_15m", "optional_minute_30m"])

    def test_research_stock_context_today_tomorrow_trend_fetches_default_minutes(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("x.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="评价兴森科技今天走势，预测明天走势",
        )
        stock_payload = {
            "002436.SZ": {
                "ts_code": "002436.SZ",
                "trade_date": "20260606",
                "daily_tail": [{"trade_date": "20260606", "close": 10.0}],
                "indicator_result": {"ts_code": "002436.SZ"},
                "minute_curves": {
                    "15m": {"period": "15m", "row_count": 16, "source": "tickflow_history+realtime"},
                    "30m": {"period": "30m", "row_count": 8, "source": "tickflow_history+realtime"},
                },
                "missing_fields": [],
            }
        }

        with (
            patch("sats.agent.date_policy.agent_today", return_value="20260606"),
            patch("sats.agent.tools.research_tools.ensure_stock_analysis_data", return_value=stock_payload) as ensure,
        ):
            result = registry.execute(
                "research.stock_context",
                {"symbols": ["002436"], "trade_date": "20260606"},
                context,
            )

        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.args[1], "20260606")
        self.assertEqual(ensure.call_args.kwargs["periods"], ("15m", "30m"))
        stock = result.payload["stock_context"]["stocks"][0]
        self.assertEqual(result.payload["stock_context"]["requested_minute_periods"], ["15m", "30m"])
        self.assertEqual(set(stock["minute_curves"]), {"15m", "30m"})
        self.assertEqual(stock["missing_fields"], [])
        self.assertNotIn("optional_fields_not_requested", stock)

    def test_research_stock_context_preserves_period_returns_from_original_message(self) -> None:
        registry = build_default_tool_registry()
        message = "看002436 6个月内涨跌幅"
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("x.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message=message,
        )
        stock_context = SimpleNamespace(
            payload={
                "stocks": [
                    {
                        "ts_code": "002436.SZ",
                        "trade_date": "20260619",
                        "period_returns": {"6m": {"start_trade_date": "20251222", "end_trade_date": "20260619", "pct_change": 18.5}},
                    }
                ]
            }
        )

        with patch("sats.agent.tools.research_tools.build_stock_context_component", return_value=stock_context) as builder:
            result = registry.execute(
                "research.stock_context",
                {"symbols": ["002436"], "trade_date": "20260620"},
                context,
            )

        builder.assert_called_once()
        self.assertEqual(builder.call_args.args[0], message)
        self.assertEqual(result.payload["stock_context"]["stocks"][0]["period_returns"]["6m"]["end_trade_date"], "20260619")

    def test_research_internal_analysis_preserves_period_returns_from_original_message(self) -> None:
        registry = build_default_tool_registry()
        message = "看002436 6个月内涨跌幅"
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), db_path=Path("x.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message=message,
        )
        stock_context = SimpleNamespace(
            trade_date="20260619",
            payload={
                "stocks": [
                    {
                        "ts_code": "002436.SZ",
                        "name": "兴森科技",
                        "trade_date": "20260619",
                        "indicator_result": {"close": 34.5},
                        "period_returns": {"6m": {"start_trade_date": "20251222", "end_trade_date": "20260619", "pct_change": 18.5}},
                    }
                ]
            },
        )

        with patch("sats.chat_components.build_stock_context_component", return_value=stock_context) as builder:
            result = registry.execute(
                "research.internal_analysis",
                {"kind": "indicators", "symbols": ["002436"], "trade_date": "20260620"},
                context,
            )

        builder.assert_called_once()
        self.assertEqual(builder.call_args.args[0], message)
        self.assertEqual(result.payload["analysis"]["trade_date"], "20260619")
        self.assertEqual(result.payload["analysis"]["results"][0]["period_returns"]["6m"]["pct_change"], 18.5)

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

    def test_signal_input_from_context_preserves_explicit_minute_curve_metadata(self) -> None:
        from sats.agent.tools.research_tools import _signal_input_from_context

        signal_input = _signal_input_from_context(
            {
                "ts_code": "002119.SZ",
                "name": "康强电子",
                "trade_date": "20260625",
                "daily_tail": [{"trade_date": "20260625", "open": 1.0, "high": 2.0, "low": 1.0, "close": 2.0, "vol": 100}],
                "chan_minute_period": "15m",
                "minute_curves": {
                    "15m": {
                        "period": "15m",
                        "source": "tickflow_history",
                        "rows": [
                            {
                                "trade_time": "2026-06-25 10:00:00",
                                "open": 1.0,
                                "high": 2.0,
                                "low": 1.0,
                                "close": 2.0,
                                "vol": 100,
                            }
                        ],
                    }
                },
            }
        )

        self.assertEqual(signal_input.metadata["chan_minute_period"], "15m")
        self.assertIn("minute_15m", signal_input.metadata)
        self.assertEqual(signal_input.metadata["minute_15m"]["period"].unique().tolist(), ["15m"])
        self.assertEqual(signal_input.metadata["minute_15m_source"], "tickflow_history")

    def test_research_internal_analysis_native_dsa_returns_structured_payload_without_report(self) -> None:
        registry = build_default_tool_registry()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context = AgentToolContext(
                settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                storage=SimpleNamespace(),
                resolver=SimpleNamespace(),
                policy=AgentExecutionPolicy(),
                command_runner=SimpleNamespace(),
                trader=SimpleNamespace(),
                message="用 DSA 分析兴森科技买卖点",
            )
            fake_result = DsaAnalysisRunResult(
                analyzed_codes=["002436.SZ"],
                skipped_codes=[],
                rankings=[DsaAnalysisRanking("002436.SZ", "兴森科技", 76, "买入", "看多", decision_type="buy", confidence_level="中")],
                source_report=None,
                archived_report=None,
                analyses=[
                    DsaStockAnalysis(
                        ts_code="002436.SZ",
                        name="兴森科技",
                        score=76,
                        advice="买入",
                        trend="看多",
                        summary="趋势偏强。",
                        risk="仅供研究。",
                        indicator={},
                        quote={},
                        chip={},
                        akshare_context={},
                        dashboard={"battle_plan": {"sniper_points": {"ideal_buy": 10.0, "stop_loss": 9.5}}},
                        context_pack={},
                        missing_fields=[],
                        market_phase={},
                        hot_sectors=[],
                        data_sources={},
                    )
                ],
            )

            with patch("sats.analysis.dsa_native.run_dsa_analysis", return_value=fake_result) as run_dsa:
                result = registry.execute(
                    "research.internal_analysis",
                    {"kind": "native_dsa", "symbols": ["002436"], "trade_date": "20260606"},
                    context,
                )

        run_dsa.assert_called_once()
        self.assertFalse(run_dsa.call_args.kwargs["report"])
        self.assertFalse(run_dsa.call_args.kwargs["llm_enabled"])
        self.assertEqual(result.status, "done")
        self.assertEqual(result.payload["analysis"]["trade_date"], "20260606")
        self.assertEqual(result.payload["analysis"]["rankings"][0]["code"], "002436.SZ")
        self.assertEqual(result.payload["analysis"]["analyses"][0]["ts_code"], "002436.SZ")

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

    def test_data_index_daily_returns_grouped_recent_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            dates = pd.bdate_range(end="2026-05-20", periods=12).strftime("%Y%m%d").tolist()
            for code in ("000001.SH", "399001.SZ"):
                storage.upsert_industry_daily(
                    code,
                    pd.DataFrame(
                        [
                            {
                                "index_code": code,
                                "trade_date": trade_date,
                                "close": 3000 + index,
                                "pct_chg": 0.1,
                                "vol": 1000 + index,
                                "amount": 10000 + index,
                            }
                            for index, trade_date in enumerate(dates)
                        ]
                    ),
                )
            resolver = MarketDataResolver(settings, storage=storage, provider=FakeDailyProvider())
            context = AgentToolContext(
                settings=settings,
                storage=storage,
                resolver=resolver,
                policy=AgentExecutionPolicy(),
                command_runner=SimpleNamespace(),
                trader=SimpleNamespace(),
                message="获取指数日线",
            )

            result = build_default_tool_registry().execute(
                "data.index_daily",
                {
                    "index_codes": ["000001.SH", "399001.SZ"],
                    "start_date": dates[0],
                    "end_date": dates[-1],
                },
                context,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(len(result.payload["sample"]), 20)
        self.assertEqual({row["index_code"] for row in result.payload["sample"]}, {"000001.SH", "399001.SZ"})

    def test_stock_minute_tool_normalizes_period_alias_before_resolver(self) -> None:
        captured = {}

        class Resolver:
            def load_stock_minute(self, symbols, *, period="1m", start_time=None, end_time=None, count=None):
                captured["period"] = period
                frame = pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "period": period,
                            "trade_date": "20260514",
                            "trade_time": "2026-05-14 10:00:00",
                            "open": 10.0,
                            "high": 10.5,
                            "low": 9.9,
                            "close": 10.2,
                            "vol": 100.0,
                            "amount": 1000.0,
                        }
                    ]
                )
                frame.attrs["market_data_provenance"] = [{"dataset": "stock_minute", "source": "test"}]
                return frame

        context = AgentToolContext(
            settings=SimpleNamespace(db_path=Path("data/sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=Resolver(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="获取 15min 分钟K",
        )

        result = build_default_tool_registry().execute(
            "data.stock_minute",
            {"symbols": ["000001"], "period": "15min", "count": 20},
            context,
        )

        self.assertEqual(result.status, "done")
        self.assertEqual(captured["period"], "15m")

    def test_resolver_derives_unsupported_minute_period_from_native_base(self) -> None:
        class TickFlowMinuteBackend:
            def __init__(self) -> None:
                self.calls = []

            def load_historical_minute_klines(self, symbols, *, period="1m", start_time=None, end_time=None, count=None):
                self.calls.append({"symbols": list(symbols), "period": period, "start_time": start_time, "end_time": end_time, "count": count})
                frame = pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "period": period,
                            "trade_date": "20260514",
                            "trade_time": "2026-05-14 09:35:00",
                            "open": 10.0,
                            "high": 10.5,
                            "low": 9.8,
                            "close": 10.2,
                            "vol": 100.0,
                            "amount": 1000.0,
                            "data_source": "fake_tickflow",
                        },
                        {
                            "ts_code": "000001.SZ",
                            "period": period,
                            "trade_date": "20260514",
                            "trade_time": "2026-05-14 09:40:00",
                            "open": 10.2,
                            "high": 10.8,
                            "low": 10.0,
                            "close": 10.6,
                            "vol": 200.0,
                            "amount": 2200.0,
                            "data_source": "fake_tickflow",
                        },
                    ]
                )
                frame.attrs["data_source"] = "fake_tickflow"
                return frame

            def load_realtime_minute_klines(self, symbols, *, period="1m", count=None):
                return pd.DataFrame()

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            backend = TickFlowMinuteBackend()
            provider = AStockDataProvider(settings, tickflow_provider=backend, tushare_provider=SimpleNamespace(), akshare_provider=SimpleNamespace())
            resolver = MarketDataResolver(settings, storage=storage, provider=provider)

            frame = resolver.load_stock_minute(
                ["000001"],
                period="10min",
                start_time="2026-05-14 09:30:00",
                end_time="2026-05-14 10:00:00",
            )

        self.assertEqual(backend.calls[0]["period"], "5m")
        self.assertEqual(frame["period"].unique().tolist(), ["10m"])
        self.assertEqual(frame.iloc[0]["datetime"], "2026-05-14 09:40:00")
        self.assertEqual(float(frame.iloc[0]["vol"]), 300.0)
        self.assertTrue(frame.attrs.get("market_data_provenance"))

    def test_astock_provider_labels_native_minute_period_when_backend_omits_column(self) -> None:
        class TickFlowMinuteBackend:
            def load_historical_minute_klines(self, symbols, *, period="1m", start_time=None, end_time=None, count=None):
                return pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "trade_date": "20260514",
                            "trade_time": "2026-05-14 10:00:00",
                            "open": 10.0,
                            "high": 10.5,
                            "low": 9.8,
                            "close": 10.2,
                            "vol": 100.0,
                            "amount": 1000.0,
                        }
                    ]
                )

        settings = SimpleNamespace(db_path=Path("data/sats.duckdb"))
        provider = AStockDataProvider(settings, tickflow_provider=TickFlowMinuteBackend(), tushare_provider=SimpleNamespace(), akshare_provider=SimpleNamespace())

        frame = provider.load_historical_minute_klines(["000001"], period="15m")

        self.assertEqual(frame["period"].unique().tolist(), ["15m"])
        self.assertEqual(frame.iloc[0]["datetime"], "2026-05-14 10:00:00")

    def test_synthesis_uses_data_index_daily_rows_when_market_context_omits_indices(self) -> None:
        observations = (
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
                                "trade_date": "20260618",
                                "requested_dimensions": ["hot_sectors"],
                                "hot_sector_context": {"hot_industries": [{"name": "半导体设备"}]},
                            }
                        }
                    },
                },
            ),
            AgentObservation(
                step_id="indices",
                kind="tool",
                status="done",
                content="index_daily: 6 rows",
                payload={
                    "tool_name": "data.index_daily",
                    "result": {
                        "payload": {
                            "sample": [
                                {
                                    "index_code": code,
                                    "trade_date": trade_date,
                                    "close": close,
                                    "pct_chg": pct_chg,
                                    "vol": 1000.0,
                                    "amount": 10000.0,
                                }
                                for code, trade_date, close, pct_chg in (
                                    ("000001.SH", "20260617", 4108.0, 0.4),
                                    ("000001.SH", "20260618", 4090.0, -0.4),
                                    ("399001.SZ", "20260617", 15880.0, 1.3),
                                    ("399001.SZ", "20260618", 16030.0, 0.9),
                                    ("399006.SZ", "20260617", 4167.0, 1.6),
                                    ("399006.SZ", "20260618", 4252.0, 2.0),
                                )
                            ],
                            "provenance": [{"dataset": "index_daily", "source": "duckdb_cache"}],
                        }
                    },
                },
            ),
        )

        result = synthesize_agent_result(
            message="评价和分析这周A股走势，预测下周走势",
            plan=AgentPlan(objective="A股大盘分析"),
            observations=observations,
            skills=(),
            settings=SimpleNamespace(openai_model="m", llm_timeout_seconds=10),
            llm_factory=None,
        )

        self.assertIn("000001.SH", result.content)
        self.assertIn("399001.SZ", result.content)
        self.assertIn("399006.SZ", result.content)
        self.assertNotIn("核心指数数据缺失", result.content)

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

    def test_resolver_realtime_quote_does_not_short_circuit_on_fresh_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_realtime_quote_cache(
                pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "price": 11.0,
                            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    ]
                )
            )
            provider = FakeDailyProvider()
            resolver = MarketDataResolver(settings, storage=storage, provider=provider)

            quote = resolver.load_realtime_quotes(["000001"], for_trading=True)

            self.assertEqual(provider.quote_calls, 1)
            self.assertEqual(float(quote.iloc[0]["price"]), 12.3)
            self.assertEqual(quote.attrs["data_source"], "fake_provider_quote")

    def test_resolver_realtime_quote_does_not_fall_back_to_cache_on_error(self) -> None:
        class FailingQuoteProvider(FakeDailyProvider):
            def load_realtime_quotes(self, *, symbols=None, universe_id=None):
                self.quote_calls += 1
                raise RuntimeError("tickflow offline")

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_realtime_quote_cache(
                pd.DataFrame(
                    [
                        {
                            "ts_code": "000001.SZ",
                            "price": 11.0,
                            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        }
                    ]
                )
            )
            provider = FailingQuoteProvider()
            resolver = MarketDataResolver(settings, storage=storage, provider=provider)

            with self.assertRaisesRegex(RuntimeError, "tickflow offline"):
                resolver.load_realtime_quotes(["000001"], for_trading=True)

            self.assertEqual(provider.quote_calls, 1)

    def test_restricted_python_allows_resolver_and_rejects_market_literals(self) -> None:
        class Resolver:
            def load_stock_daily(self, symbols, *, start_date, end_date):
                frame = pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260518", "close": 10.0}])
                frame.attrs["market_data_provenance"] = [{"source": "duckdb_cache"}]
                return frame

        runtime = RestrictedPythonRuntime(resolver=Resolver(), timeout_seconds=2)
        ok = runtime.run("def run(context):\n    daily = resolver.load_stock_daily(['000001'], start_date='20260518', end_date='20260518')\n    return {'rows': len(daily)}")
        bad = runtime.run("RESULT = {'close': 10.5}")
        imported = runtime.run("import json\nimport math\nRESULT = {'value': json.loads('{\"x\": 4}')['x'], 'root': math.sqrt(9)}")
        safe_builtins = runtime.run(
            "def run(context):\n"
            "    try:\n"
            "        return {'mapping': isinstance(context, dict), 'kind': str(type(context)), "
            "'has': hasattr(resolver, 'load_stock_daily'), 'method': getattr(resolver, 'load_stock_daily') is not None, "
            "'reversed': list(reversed([1, 2]))}\n"
            "    except (Exception, TypeError):\n"
            "        return {'mapping': False}\n"
        )
        protected = runtime.run("def run(context):\n    return getattr(resolver, '__class__')")
        banned = runtime.run("import os\nRESULT = os.system('echo hi')")

        self.assertEqual(ok.status, "done")
        self.assertEqual(ok.result["rows"], 1)
        self.assertEqual(imported.status, "done")
        self.assertEqual(imported.result, {"value": 4, "root": 3.0})
        self.assertEqual(safe_builtins.status, "done")
        self.assertEqual(safe_builtins.result["reversed"], [2, 1])
        self.assertTrue(safe_builtins.result["mapping"])
        self.assertEqual(protected.status, "error")
        self.assertIn("protected attribute", protected.error)
        self.assertEqual(bad.status, "error")
        self.assertIn("market data literals", bad.error)
        self.assertEqual(banned.status, "error")
        self.assertIn("forbids os access", banned.error)

    def test_tool_registry_preserves_nested_python_failure_and_emits_one_detection(self) -> None:
        registry = AgentToolRegistry()
        failure = failure_from_message(
            "NameError: name 'isinstance' is not defined; missing_fields=[]",
            project_root=Path("."),
            stage="python_runtime",
            tool="analysis.python_program",
        )
        registry.register(
            AgentToolSpec(
                name="analysis.python_program",
                description="test",
                side_effect="readonly",
                input_schema={"type": "object", "properties": {}, "required": []},
                executor=lambda context, arguments: AgentToolResult(
                    status="error",
                    content="wrapped error with missing_fields",
                    payload={"python_program": {"failure": failure.to_dict()}},
                ),
            )
        )
        events: list[tuple[str, dict]] = []
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path("."), self_repair_mode="off", self_repair_max_attempts=0),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="test",
            event_callback=lambda event_type, payload: events.append((event_type, dict(payload))),
        )

        result = registry.execute("analysis.python_program", {}, context)

        self.assertEqual(result.payload["failure"]["category"], "python_code_error")
        self.assertEqual(result.payload["failure"]["failure_id"], failure.failure_id)
        self.assertEqual([name for name, _payload in events].count("failure_detected"), 1)
        self.assertEqual([name for name, _payload in events].count("failure_exhausted"), 1)
        self.assertEqual({payload["failure_id"] for _name, payload in events}, {failure.failure_id})

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

    def test_command_runner_adds_noreport_for_agent_report_capable_commands(self) -> None:
        calls = []

        def fake_cli(argv):
            calls.append(list(argv))
            return 0

        runner = AgentCommandRunner(policy=AgentExecutionPolicy(), cli_main=fake_cli)

        runner.run(["analyze", "--stocks", "000001"])
        runner.run(["deep-analysis", "--stocks", "000001"])
        runner.run(["serenity-screen", "--theme", "AI"])
        runner.run(["trading-committee", "--stocks", "000001"])
        runner.run(["factor", "analyze", "--factor", "barra_style_value"])
        runner.run(["factor", "pick", "--profile", "value"])
        runner.run(["discover", "--limit", "3", "选三只短线股"])

        self.assertEqual(calls[0], ["analyze", "--stocks", "000001", "--noreport"])
        self.assertEqual(calls[1], ["deep-analysis", "--stocks", "000001", "--noreport"])
        self.assertEqual(calls[2], ["serenity-screen", "--theme", "AI", "--noreport"])
        self.assertEqual(calls[3], ["trading-committee", "--stocks", "000001", "--noreport"])
        self.assertEqual(calls[4], ["factor", "analyze", "--factor", "barra_style_value", "--noreport"])
        self.assertEqual(calls[5], ["factor", "pick", "--profile", "value", "--noreport"])
        self.assertEqual(calls[6], ["discover", "--limit", "3", "--noreport", "选三只短线股"])

    def test_command_runner_rejects_agent_report_writing_cli_paths(self) -> None:
        calls = []

        def fake_cli(argv):
            calls.append(argv)
            return 0

        runner = AgentCommandRunner(policy=AgentExecutionPolicy(), cli_main=fake_cli)

        for argv in (
            ["dsa", "--stocks", "000001"],
            ["analyze-dsa", "--stocks", "000001"],
            ["analyze-chan", "--stocks", "000001"],
            ["portfolio", "run", "--phase", "report"],
            ["portfolio", "run", "--phase=close"],
        ):
            result = runner.run(argv)
            self.assertEqual(result.status, "error")
            self.assertIn("research.write_report", result.stderr)

        self.assertEqual(calls, [])

    def test_agent_command_catalog_covers_all_non_recursive_cli_commands(self) -> None:
        parser = build_parser()
        subparsers = next(action for action in parser._actions if getattr(action, "choices", None))
        registered = set(subparsers.choices)

        self.assertEqual(set(SATS_COMMANDS), registered - set(RECURSIVE_SATS_COMMANDS))
        self.assertEqual(set(RECURSIVE_SATS_COMMANDS), registered & set(RECURSIVE_SATS_COMMANDS))
        self.assertTrue({"period-change", "trading-committee", "web"}.issubset(SATS_COMMANDS))
        command_description = build_default_tool_registry().get("sats_command.run").description
        self.assertTrue(all(command in command_description for command in SATS_COMMANDS))

    def test_agent_command_runner_rejects_obsolete_agent_command(self) -> None:
        calls = []

        def fake_cli(argv):
            calls.append(argv)
            return 0

        result = AgentCommandRunner(policy=AgentExecutionPolicy(), cli_main=fake_cli).run(["agent", "hello"])

        self.assertEqual(result.status, "error")
        self.assertIn("OBSOLETE: sats agent", result.stderr)
        self.assertEqual(calls, [])

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

    def test_agent_runtime_resolves_step_symbols_placeholder(self) -> None:
        symbols = ["600172.SH", "301071.SZ", "002046.SZ", "000519.SZ", "688028.SH", "300373.SZ"]
        captured: dict[str, dict[str, object]] = {}

        def executor(context, arguments):
            captured["arguments"] = dict(arguments)
            return AgentToolResult(status="done", content="ok", payload={"status": "ok"}, data_names=("stock_context",))

        registry = AgentToolRegistry(
            [
                AgentToolSpec(
                    name="research.stock_context",
                    description="test stock context",
                    executor=executor,
                )
            ]
        )
        previous = AgentObservation(
            step_id="step_2",
            kind="tool",
            status="done",
            content="candidates",
            payload={"result": {"payload": {"stocks": [{"ts_code": symbol} for symbol in symbols]}}},
        )
        step = AgentStep(
            step_id="stock_context",
            kind="tool",
            tool_name="research.stock_context",
            arguments={"symbols": ["${step_2.symbols[0:5]}"], "trade_date": "20260624"},
        )

        observation = _execute_step(
            step,
            message="获取前五个候选股票上下文",
            command_runner=AgentCommandRunner(policy=AgentExecutionPolicy()),
            python_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            tool_registry=registry,
            tool_context=AgentToolContext(
                settings=SimpleNamespace(),
                storage=SimpleNamespace(),
                resolver=SimpleNamespace(),
                policy=AgentExecutionPolicy(),
                command_runner=SimpleNamespace(),
                trader=SimpleNamespace(),
                message="获取前五个候选股票上下文",
            ),
            observations=[previous],
            trade_context={},
        )

        self.assertEqual(observation.status, "done")
        self.assertEqual(captured["arguments"]["symbols"], symbols[:5])
        self.assertEqual(observation.payload["arguments"]["symbols"], symbols[:5])
        self.assertEqual(observation.payload["raw_arguments"]["symbols"], ["${step_2.symbols[0:5]}"])

    def test_agent_runtime_rejects_unresolved_step_placeholder_before_tool_execution(self) -> None:
        called = False

        def executor(context, arguments):
            nonlocal called
            called = True
            return AgentToolResult(status="done", content="ok")

        registry = AgentToolRegistry([AgentToolSpec(name="research.stock_context", description="test stock context", executor=executor)])
        step = AgentStep(
            step_id="stock_context",
            kind="tool",
            tool_name="research.stock_context",
            arguments={"symbols": ["${missing.symbols[0:5]}"], "trade_date": "20260624"},
        )

        observation = _execute_step(
            step,
            message="获取前五个候选股票上下文",
            command_runner=AgentCommandRunner(policy=AgentExecutionPolicy()),
            python_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            tool_registry=registry,
            tool_context=AgentToolContext(
                settings=SimpleNamespace(),
                storage=SimpleNamespace(),
                resolver=SimpleNamespace(),
                policy=AgentExecutionPolicy(),
                command_runner=SimpleNamespace(),
                trader=SimpleNamespace(),
                message="获取前五个候选股票上下文",
            ),
            observations=[],
            trade_context={},
        )

        self.assertFalse(called)
        self.assertEqual(observation.status, "error")
        self.assertIn("unresolved agent placeholder", observation.content)
        self.assertIn("observation missing not found", observation.content)

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
            self.assertTrue(result.observations[0].payload["replan_decision"]["should_replan"])
            self.assertEqual(result.observations[0].payload["replan_decision"]["error_category"], "command_failed")
            self.assertIn("sats bad", result.content)
            self.assertIn("returncode=1", result.content)
            self.assertIn("sats skills", result.content)
            self.assertNotIn("[done]", result.content)

    def test_agent_runtime_replans_multiple_distinct_errors_until_success(self) -> None:
        class MultiReplanLLM:
            planner_calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                prompt = "\n".join(str(item.get("content") or "") for item in messages)
                if "steps 每项字段" not in prompt:
                    return LLMResponse(content="最终恢复总结")
                MultiReplanLLM.planner_calls += 1
                command = ["bad_one", "bad_two", "skills"][MultiReplanLLM.planner_calls - 1]
                return LLMResponse(
                    content=(
                        '{"objective":"recover command","steps":['
                        f'{{"step_id":"cmd_{command}","kind":"command","title":"{command}","command":["{command}"]}},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            MultiReplanLLM.planner_calls = 0
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp), openai_model="m", llm_timeout_seconds=10)
            calls = []

            def fake_cli(argv):
                calls.append(argv)
                return 0 if argv == ["skills"] else 1

            result = run_agent_once(
                "列出 skills",
                settings=settings,
                policy=AgentExecutionPolicy(max_iterations=5),
                llm_factory=MultiReplanLLM,
                cli_main=fake_cli,
            )

            self.assertEqual(calls, [["bad_one"], ["bad_two"], ["skills"]])
            self.assertEqual(MultiReplanLLM.planner_calls, 3)
            decisions = [
                item.payload["replan_decision"]
                for item in result.observations
                if item.status == "error" and "replan_decision" in item.payload
            ]
            self.assertEqual(len(decisions), 2)
            self.assertTrue(all(item["should_replan"] for item in decisions))
            self.assertEqual([item["error_category"] for item in decisions], ["command_failed", "command_failed"])

    def test_agent_runtime_stops_after_repeated_replanned_error(self) -> None:
        class RepeatErrorLLM:
            planner_calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                prompt = "\n".join(str(item.get("content") or "") for item in messages)
                if "steps 每项字段" not in prompt:
                    return LLMResponse(content="重复错误总结")
                RepeatErrorLLM.planner_calls += 1
                return LLMResponse(
                    content=(
                        '{"objective":"recover command","steps":['
                        '{"step_id":"cmd_bad","kind":"command","title":"bad","command":["bad"]},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            RepeatErrorLLM.planner_calls = 0
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp), openai_model="m", llm_timeout_seconds=10)
            calls = []

            def fake_cli(argv):
                calls.append(argv)
                return 1

            result = run_agent_once("列出 skills", settings=settings, llm_factory=RepeatErrorLLM, cli_main=fake_cli)

            self.assertEqual(calls, [["bad"], ["bad"]])
            self.assertIn("repeated_error", [item.step_id for item in result.observations])
            decisions = [
                item.payload["replan_decision"]
                for item in result.observations
                if item.kind == "command" and item.status == "error"
            ]
            self.assertTrue(decisions[0]["should_replan"])
            self.assertFalse(decisions[1]["should_replan"])
            self.assertTrue(decisions[1]["repeated"])

    def test_agent_runtime_does_not_replan_confirmation_or_trade_permission_errors(self) -> None:
        class BlockingLLM:
            planner_calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                prompt = "\n".join(str(item.get("content") or "") for item in messages)
                if "steps 每项字段" not in prompt:
                    return LLMResponse(content="阻断总结")
                BlockingLLM.planner_calls += 1
                if "用户目标：交易 买入 000001" in prompt:
                    return LLMResponse(
                        content=(
                            '{"objective":"trade","steps":['
                            '{"step_id":"trade","kind":"trade","title":"buy",'
                            '"trade":{"ts_code":"000001.SZ","side":"buy","quantity":100}},'
                            '{"step_id":"final","kind":"final","title":"summary"}]}'
                        )
                    )
                return LLMResponse(
                    content=(
                        '{"objective":"confirm","steps":['
                        '{"step_id":"confirm","kind":"command","title":"needs confirm","command":["skills"],"requires_confirmation":true},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            BlockingLLM.planner_calls = 0
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp), openai_model="m", llm_timeout_seconds=10)
            calls = []

            def fake_cli(argv):
                calls.append(argv)
                return 0

            confirmation = run_agent_once("需要确认后列出 skills", settings=settings, llm_factory=BlockingLLM, cli_main=fake_cli)
            trade = run_agent_once("交易 买入 000001", settings=settings, llm_factory=BlockingLLM, cli_main=fake_cli)

            self.assertEqual(calls, [])
            self.assertEqual(BlockingLLM.planner_calls, 2)
            confirm_decision = confirmation.observations[0].payload["replan_decision"]
            trade_decision = trade.observations[0].payload["replan_decision"]
            self.assertEqual(confirm_decision["error_category"], "requires_confirmation")
            self.assertEqual(trade_decision["error_category"], "trade_permission")
            self.assertFalse(confirm_decision["should_replan"])
            self.assertFalse(trade_decision["should_replan"])

    def test_agent_runtime_replans_after_akshare_catalog_lookup(self) -> None:
        class CatalogReplanLLM:
            calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                CatalogReplanLLM.calls += 1
                if CatalogReplanLLM.calls == 1:
                    return LLMResponse(
                        content=(
                            '{"objective":"akshare catalog","steps":['
                            '{"step_id":"catalog","kind":"tool","title":"catalog","tool_name":"data.list_akshare_datasets",'
                            '"arguments":{"query":"cpi","compact":true}},'
                            '{"step_id":"final","kind":"final","title":"summary"}]}'
                        )
                    )
                if CatalogReplanLLM.calls == 2:
                    return LLMResponse(
                        content=(
                            '{"objective":"akshare fetch","steps":['
                            '{"step_id":"fetch","kind":"tool","title":"fetch","tool_name":"data.get_akshare_data",'
                            '"arguments":{"dataset":"macro_china_cpi","params":{},"limit":1}},'
                            '{"step_id":"final","kind":"final","title":"summary"}]}'
                        )
                    )
                return LLMResponse(content="已获取 AkShare 数据")

        class DatasetProvider:
            def list_akshare_datasets(self, **kwargs):
                return [{"dataset": "macro_china_cpi", "domain": "宏观经济", "query": kwargs.get("query")}]

            def fetch_akshare_dataset(self, dataset, params=None, *, fields=None, limit=200):
                return {
                    "dataset": dataset,
                    "params": params or {},
                    "columns": ["date", "value"],
                    "rows": [{"date": "2026-05", "value": 1.2}],
                    "row_count": 1,
                    "returned_row_count": 1,
                    "data_source": f"akshare_{dataset}",
                    "missing_fields": [],
                    "market_data_provenance": [{"dataset": dataset, "source": "akshare"}],
                }

        with tempfile.TemporaryDirectory() as tmp:
            CatalogReplanLLM.calls = 0
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb", project_root=Path(tmp), openai_model="m", llm_timeout_seconds=10)
            with patch("sats.agent.tools.data_tools.AStockDataProvider", return_value=DatasetProvider()):
                result = run_agent_once("用 AkShare 获取 CPI 数据", settings=settings, llm_factory=CatalogReplanLLM)

        self.assertEqual(CatalogReplanLLM.calls, 3)
        self.assertEqual(result.tool_call_count, 2)
        self.assertIn("已获取 AkShare 数据", result.content)

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
        self.assertTrue({"web.search", "web.open", "web.social_hot", "web.hot_mentions"}.issubset(names))
        self.assertIsNone(registry.get("research.discover_opportunities"))
        self.assertNotIn("research.discover_opportunities", names)
        self.assertNotIn("research.discover_opportunities", {item["name"] for item in registry.summaries()})
        rejected = registry.execute(
            "data.stock_daily",
            {"symbols": ["000001"], "start_date": "20260518", "end_date": "20260520", "close": 10.5},
            context,
        )

        self.assertEqual(rejected.status, "error")
        self.assertIn("market data guard", rejected.content)

    def test_catalog_agent_tools_omits_disabled_discover_opportunities_tool(self) -> None:
        payload = build_capability_catalog(
            settings=SimpleNamespace(),
            section="agent-tools",
            query="",
            limit=200,
        )
        rows = payload["data"]["agent-tools"]["items"]

        self.assertNotIn("research.discover_opportunities", {item["name"] for item in rows})

    def test_tool_registry_resolves_company_names_at_execution_boundary(self) -> None:
        class CompanyProvider:
            def __init__(self) -> None:
                self.symbols = []

            def load_company_fundamentals(self, symbols, *, trade_date, storage, periods):
                self.symbols = list(symbols)
                return {
                    symbol: {
                        "ts_code": symbol,
                        "name": "东威科技",
                        "company_profile": {},
                        "main_business": "专用设备",
                        "business_composition": [],
                        "valuation": {},
                        "financial_indicators": [],
                        "income": [],
                        "balance_sheet": [],
                        "cashflow": [],
                        "data_sources": {},
                        "missing_fields": [],
                    }
                    for symbol in symbols
                }

        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_stock_basic(pd.DataFrame([{"ts_code": "688700.SH", "symbol": "688700", "name": "东威科技"}]))
            context = AgentToolContext(
                settings=settings,
                storage=storage,
                resolver=SimpleNamespace(),
                policy=AgentExecutionPolicy(),
                command_runner=SimpleNamespace(),
                trader=SimpleNamespace(),
                message="东威科技公司介绍",
            )
            company_provider = CompanyProvider()
            with patch("sats.agent.tools.research_tools.AStockDataProvider", return_value=company_provider):
                result = build_default_tool_registry().execute(
                    "research.internal_analysis",
                    {"kind": "company_fundamentals", "symbols": ["东威科技"], "trade_date": "20260621"},
                    context,
                )

        self.assertEqual(result.status, "done")
        self.assertEqual(company_provider.symbols, ["688700.SH"])
        self.assertEqual(result.payload["analysis"]["companies"][0]["ts_code"], "688700.SH")

    def test_tool_registry_rejects_unknown_company_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(db_path=Path(tmp) / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            storage.upsert_stock_basic(pd.DataFrame([{"ts_code": "688700.SH", "symbol": "688700", "name": "东威科技"}]))
            context = AgentToolContext(
                settings=settings,
                storage=storage,
                resolver=SimpleNamespace(),
                policy=AgentExecutionPolicy(),
                command_runner=SimpleNamespace(),
                trader=SimpleNamespace(),
            )

            result = build_default_tool_registry().execute(
                "research.internal_analysis",
                {"kind": "company_fundamentals", "symbols": ["不存在公司"]},
                context,
            )

        self.assertEqual(result.status, "error")
        self.assertIn("请补充 6 位股票代码", result.content)

    def test_disabled_discover_opportunities_tool_is_not_executable_from_default_registry(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="选三只短线股",
        )

        with patch("sats.agent.tools.research_tools.build_opportunity_component") as builder:
            result = registry.execute("research.discover_opportunities", {"query": "选三只短线股", "limit": 3}, context)

        self.assertEqual(result.status, "error")
        self.assertIn("unknown agent tool", result.content)
        builder.assert_not_called()

    def test_strategy_draft_tool_returns_data_without_artifacts(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="写一个5日和20日均线策略",
        )

        result = registry.execute("research.strategy_draft", {"request": context.message, "symbols": ["000001"]}, context)

        self.assertEqual(result.status, "done")
        self.assertFalse(result.artifacts)
        self.assertIn("spec", result.payload)
        self.assertIn("draft", result.payload)

    def test_backtest_tool_returns_metrics_without_artifacts(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
            message="回测 000001 的均线策略",
        )
        spec = StrategySpec(
            name="均线策略",
            symbols=("000001.SZ",),
            start_date="20260101",
            end_date="20260201",
        )
        fake_result = BacktestResult(
            spec=spec,
            metrics={"total_return": 0.1, "annual_return": 0.2, "max_drawdown": -0.05},
            message="done",
        )

        with patch("sats.agent.tools.research_tools.run_strategy_backtest", return_value=fake_result):
            result = registry.execute("research.backtest", {"spec": spec.to_dict()}, context)

        self.assertEqual(result.status, "done")
        self.assertFalse(result.artifacts)
        self.assertEqual(result.payload["backtest"]["metrics"]["total_return"], 0.1)
        self.assertIn("report_text", result.payload)

    def test_daily_portfolio_report_phase_returns_data_without_writing_report(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )
        agent = SimpleNamespace(
            status=lambda mode="paper": {
                "trading_mode": mode,
                "latest_run": {"trade_date": "20260623", "phase": "afternoon-buy", "status": "done"},
                "pending_intents": 0,
            },
            run=lambda **kwargs: (_ for _ in ()).throw(AssertionError("run should not be called")),
        )

        with patch("sats.agent.tools.workflow_tools.DailyPortfolioAgent", return_value=agent):
            result = registry.execute(
                "workflow.daily_portfolio",
                {"phase": "report", "trading_mode": "paper", "trade_date": "20260623"},
                context,
            )

        self.assertEqual(result.status, "done")
        self.assertFalse(result.artifacts)
        self.assertTrue(result.payload["daily_portfolio"]["report_policy"].startswith("Agent 工具只返回"))

    def test_factor_tool_uses_stocks_cli_flag(self) -> None:
        registry = build_default_tool_registry()
        calls: list[list[str]] = []

        def run(argv):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, output="ok", argv=list(argv), status="done")

        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(run=run),
            trader=SimpleNamespace(),
        )

        result = registry.execute(
            "factor.analyze",
            {"factor": "barra_style_value", "symbols": "000001,600519"},
            context,
        )

        self.assertEqual(result.status, "done")
        self.assertEqual(calls[0], ["factor", "analyze", "--factor", "barra_style_value", "--stocks", "000001,600519", "--noreport"])

    def test_web_tool_keeps_external_failure_as_structured_evidence_gap(self) -> None:
        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )
        payload = {"status": "error", "query": "贵州茅台 最新公告", "results": [], "error": "timeout", "from_cache": False}

        with patch("sats.agent.tools.web_tools.search", return_value=payload):
            result = registry.execute("web.search", {"query": "贵州茅台 最新公告"}, context)

        self.assertEqual(result.status, "done")
        self.assertEqual(result.payload["web_search"]["status"], "error")
        self.assertIn("timeout", result.content)

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
        self.assertIn("data.list_akshare_datasets", tool_names)
        self.assertIn("data.describe_akshare_dataset", tool_names)
        self.assertIn("data.get_akshare_data", tool_names)
        self.assertIn("tushare.index_member_all", capability_ids)
        self.assertIn("tushare.margin_detail", capability_ids)
        self.assertIn("tickflow.realtime_quotes", capability_ids)
        self.assertIn("tickflow.market_depth", capability_ids)
        self.assertIn("akshare.dataset_catalog", capability_ids)

    def test_tool_registry_can_call_akshare_catalog_tools(self) -> None:
        class DatasetProvider:
            def list_akshare_datasets(self, **kwargs):
                return [{"dataset": "stock_zh_a_spot_em", "query": kwargs.get("query")}]

            def describe_akshare_dataset(self, dataset):
                return {"dataset": dataset, "input_fields": []}

            def fetch_akshare_dataset(self, dataset, params=None, *, fields=None, limit=200):
                return {
                    "dataset": dataset,
                    "params": params or {},
                    "columns": fields or ["代码"],
                    "rows": [{"代码": "000001"}],
                    "row_count": 1,
                    "returned_row_count": 1,
                    "data_source": f"akshare_{dataset}",
                    "missing_fields": [],
                    "market_data_provenance": [{"dataset": dataset, "source": "akshare"}],
                }

        registry = build_default_tool_registry()
        context = AgentToolContext(
            settings=SimpleNamespace(),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )

        with patch("sats.agent.tools.data_tools.AStockDataProvider", return_value=DatasetProvider()):
            listed = registry.execute("data.list_akshare_datasets", {"query": "spot", "compact": True}, context)
            described = registry.execute("data.describe_akshare_dataset", {"dataset": "stock_zh_a_spot_em"}, context)
            fetched = registry.execute("data.get_akshare_data", {"dataset": "stock_zh_a_spot_em", "fields": ["代码"], "limit": 1}, context)

        self.assertEqual(listed.status, "done")
        self.assertEqual(listed.payload["datasets"][0]["dataset"], "stock_zh_a_spot_em")
        self.assertEqual(described.payload["dataset"]["dataset"], "stock_zh_a_spot_em")
        self.assertEqual(fetched.payload["akshare_data"]["data_source"], "akshare_stock_zh_a_spot_em")

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

    def test_llm_planner_removes_chat_answer_after_research_steps(self) -> None:
        class ResearchPlannerLLM:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                return LLMResponse(
                    content=(
                        '{"objective":"market review","steps":['
                        '{"step_id":"market","kind":"tool","title":"市场数据","tool_name":"research.market_context",'
                        '"arguments":{"dimensions":["market_breadth"]}},'
                        '{"step_id":"answer","kind":"tool","title":"汇总","tool_name":"chat.answer",'
                        '"arguments":{"message":"基于以上数据总结","knowledge":"使用前序步骤结果"}},'
                        '{"step_id":"final","kind":"final","title":"summary"}]}'
                    )
                )

        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan(
            "评价这周A股表现",
            settings=settings,
            policy=AgentExecutionPolicy(),
            llm_factory=ResearchPlannerLLM,
            tool_registry=registry,
        )

        tools = [step.tool_name for step in plan.steps if step.kind == "tool"]
        self.assertEqual(tools, ["research.market_context"])
        self.assertEqual(plan.steps[-1].kind, "final")

    def test_fallback_planner_routes_plain_chat_to_chat_tool(self) -> None:
        registry = build_default_tool_registry()
        settings = SimpleNamespace(openai_model="m", llm_timeout_seconds=10)

        plan = build_agent_plan("解释均线金叉", settings=settings, policy=AgentExecutionPolicy(), llm_factory=None, tool_registry=registry)

        self.assertEqual(plan.steps[0].kind, "tool")
        self.assertEqual(plan.steps[0].tool_name, "chat.answer")

    def test_chat_answer_tool_calls_plain_chat(self) -> None:
        registry = build_default_tool_registry()
        self.assertNotIn("knowledge", registry.get("chat.answer").input_schema["properties"])
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path(".")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )
        with patch("sats.agent.tools.chat_tools.build_plain_chat_answer", return_value=SimpleNamespace(content="plain answer")) as chat:
            result = registry.execute(
                "chat.answer",
                {"message": "解释均线金叉", "knowledge": "使用前序步骤结果"},
                context,
            )

        chat.assert_called_once()
        self.assertNotIn("knowledge", chat.call_args.kwargs)
        self.assertEqual(result.status, "done")
        self.assertEqual(result.content, "plain answer")

    def test_agent_runtime_uses_final_synthesis_without_chat_answer_replan(self) -> None:
        class ResearchRuntimeLLM:
            planner_calls = 0
            synthesis_calls = 0

            def __init__(self, *args, **kwargs) -> None:
                pass

            def chat(self, messages, timeout=None):
                prompt = "\n".join(str(item.get("content") or "") for item in messages)
                if "steps 每项字段" in prompt:
                    ResearchRuntimeLLM.planner_calls += 1
                    return LLMResponse(
                        content=(
                            '{"objective":"market review","steps":['
                            '{"step_id":"market","kind":"tool","title":"市场数据","tool_name":"research.market_context",'
                            '"arguments":{"dimensions":["market_breadth"]}},'
                            '{"step_id":"answer","kind":"tool","title":"汇总","tool_name":"chat.answer",'
                            '"arguments":{"message":"基于以上数据总结","knowledge":"使用前序步骤结果"}},'
                            '{"step_id":"final","kind":"final","title":"summary"}]}'
                        )
                    )
                ResearchRuntimeLLM.synthesis_calls += 1
                return LLMResponse(content="最终市场分析")

        with tempfile.TemporaryDirectory() as tmp:
            ResearchRuntimeLLM.planner_calls = 0
            ResearchRuntimeLLM.synthesis_calls = 0
            settings = SimpleNamespace(
                db_path=Path(tmp) / "sats.duckdb",
                project_root=Path(tmp),
                openai_model="m",
                llm_timeout_seconds=10,
            )
            context = SimpleNamespace(payload={"market_breadth": {"up_count": 10, "down_count": 20}})
            with patch("sats.agent.tools.research_tools.build_market_context_component", return_value=context):
                result = run_agent_once(
                    "评价这周A股表现",
                    settings=settings,
                    llm_factory=ResearchRuntimeLLM,
                )

        self.assertEqual(ResearchRuntimeLLM.planner_calls, 1)
        self.assertEqual(ResearchRuntimeLLM.synthesis_calls, 1)
        self.assertEqual(result.content, "最终市场分析")
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(
            [item.payload.get("tool_name") for item in result.observations if item.kind == "tool"],
            ["research.market_context"],
        )

    def test_cli_agent_entrypoint(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            main(["agent", "hello"])

        self.assertIn("OBSOLETE: sats agent", str(raised.exception))

    def test_repl_agent_and_goal_commands(self) -> None:
        calls = []
        output = []
        state = ReplState()

        self.assertNotIn("/agent", help_text())
        self.assertTrue(handle_repl_line("/goal status", runner=lambda argv: 0, printer=output.append, state=state))
        self.assertTrue(handle_repl_line("/goal hello goal", runner=lambda argv: calls.append(argv) or 0, printer=output.append, state=state))
        self.assertTrue(handle_repl_line("/goal cancel", runner=lambda argv: 0, printer=output.append, state=state))
        self.assertTrue(handle_repl_line("/engine agent", runner=lambda argv: calls.append(argv) or 0, printer=output.append, state=state))
        self.assertTrue(handle_repl_line("/chat --agent hello", runner=lambda argv: calls.append(argv) or 0, printer=output.append, state=state))
        self.assertTrue(handle_repl_line("/agent hello", runner=lambda argv: calls.append(argv) or 0, printer=output.append, state=state))

        self.assertEqual(calls, [])
        self.assertTrue(any("OBSOLETE: /goal" in item for item in output))
        self.assertTrue(any("OBSOLETE: engine agent" in item for item in output))
        self.assertTrue(any("OBSOLETE: /chat --agent" in item for item in output))
        self.assertTrue(any("OBSOLETE: /agent" in item for item in output))
        self.assertEqual(state.agent_goal, "")


if __name__ == "__main__":
    unittest.main()
