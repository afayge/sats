from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sats.chat import (
    STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS,
    STOCK_ANALYSIS_DEFAULT_RAG_LIMIT,
    SYSTEM_PROMPT,
    ChatSession,
    _ChatSkillToolRegistry,
    _collections_for_plan,
    build_chat_messages,
)
from sats.chat_planner import build_chat_plan
from sats.chat_reference import ChatReferenceContext
from sats.cli import main
from sats.llm import LLMResponse, ToolCallRequest
from sats.analysis.opportunity_discovery import estimate_llm_message_tokens, llm_context_input_budget_tokens
from sats.screening.rule_composer import GeneratedRuleResult
from sats.skill_routing import SkillRouteContext, select_skills
from sats.skills import Skill, format_skill_list, load_skills, match_skills, parse_skill_file
from sats.stock_question import StockQuestion


class FakeLLM:
    instances: list["FakeLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.messages = []
        self.calls = []
        FakeLLM.instances.append(self)

    def chat(self, messages, tools=None):
        self.messages = messages
        self.calls.append({"messages": messages, "tools": tools})
        return SimpleNamespace(content="收到")


class HotSectorClarificationLLM:
    instances: list["HotSectorClarificationLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.messages = []
        self.calls = []
        HotSectorClarificationLLM.instances.append(self)

    def chat(self, messages, tools=None, timeout=None):
        self.messages = messages
        self.calls.append({"messages": messages, "tools": tools})
        prompt = "\n".join(str(message.get("content", "")) for message in messages)
        if "按以下 JSON 字段输出" in prompt:
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "intent": "opportunity_discovery",
                        "stock_names": ["热点板块"],
                        "needs_market_context": True,
                        "needs_opportunity_discovery": True,
                        "market_horizons": ["tomorrow"],
                        "missing_questions": ["请明确您关注的热点板块名称或领域（如半导体、新能源、医药等）"],
                        "confidence": 0.86,
                    },
                    ensure_ascii=False,
                )
            )
        return SimpleNamespace(content="收到")


class TimeoutLLM:
    instances: list["TimeoutLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.calls = []
        TimeoutLLM.instances.append(self)

    def chat(self, messages, tools=None):
        self.calls.append({"messages": messages, "tools": tools})
        raise TimeoutError("Request timed out.")


class LightTimeoutDefaultLLM:
    instances: list["LightTimeoutDefaultLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.messages = []
        LightTimeoutDefaultLLM.instances.append(self)

    def chat(self, messages, tools=None):
        self.messages = messages
        if self.kwargs.get("profile") == "light":
            raise TimeoutError("light timeout")
        return SimpleNamespace(content="默认模型回答")


class TitleOnlyLLM:
    instances: list["TitleOnlyLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.messages = []
        TitleOnlyLLM.instances.append(self)

    def chat(self, messages, tools=None):
        self.messages = messages
        return SimpleNamespace(content="# 下周大概率上涨的股票候选")


class ContextLengthErrorLLM:
    instances: list["ContextLengthErrorLLM"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.messages = []
        ContextLengthErrorLLM.instances.append(self)

    def chat(self, messages, tools=None):
        self.messages = messages
        raise RuntimeError("maximum context length is 1048565 tokens")


class RecordingMemoryExtractor:
    def __init__(self) -> None:
        self.extract_llms = []
        self.summary_llms = []

    def extract(self, user_message: str, assistant_message: str, *, llm):
        self.extract_llms.append(llm)
        return []

    def summarize(self, existing_summary: str, messages, *, llm):
        self.summary_llms.append(llm)
        return "摘要"


class RecordingProgress:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def step(self, label: str, *, total=None):
        record = {"label": label, "state": "running", "message": ""}
        self.records.append(record)
        return RecordingProgressStep(record)


class RecordingProgressStep:
    def __init__(self, record: dict) -> None:
        self.record = record
        self.done = False

    def update(self, current=None, *, message: str = "") -> None:
        if message:
            self.record["message"] = message

    def complete(self, *, message: str = "") -> None:
        self.record["state"] = "ok"
        self.record["message"] = message
        self.done = True

    def fail(self, *, message: str = "") -> None:
        self.record["state"] = "error"
        self.record["message"] = message
        self.done = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.done:
            return None
        if exc_type is None:
            self.complete()
        else:
            self.fail(message=str(exc) if exc else "")
        return None


def _fake_stock_payload() -> dict:
    daily_tail = [
        {
            "trade_date": f"20260{1 + index // 30:02d}{1 + index % 28:02d}",
            "open": 10 + index * 0.1,
            "high": 10.2 + index * 0.1,
            "low": 9.8 + index * 0.1,
            "close": 10.1 + index * 0.1,
            "vol": 1000 + index,
            "amount": 2000 + index,
            "pct_chg": 0.5,
        }
        for index in range(180)
    ]
    minute_rows = [
        {
            "trade_time": f"2026-05-27 10:{index % 60:02d}:00",
            "open": 20.0,
            "high": 20.5,
            "low": 19.8,
            "close": 20.1 + index * 0.01,
            "vol": 100 + index,
        }
        for index in range(160)
    ]
    return {
        "user_question": "预测600578明天走势",
        "trade_date": "20260527",
        "symbols": ["600578.SH"],
        "data_policy": "real data",
        "stocks": [
            {
                "ts_code": "600578.SH",
                "name": "京能电力",
                "trade_date": "20260527",
                "price_context": {"close": 21.5, "pct_chg": 1.2, "data_source": "quote"},
                "indicator_result": {
                    "close": 21.5,
                    "technical": {
                        "ma": {"ma5": 21.0, "ma10": 20.7, "ma20": 20.2, "ma60": 19.5},
                        "ma_alignment": "多头排列",
                        "bias": {"ma5": 2.38, "ma10": 3.86, "ma20": 6.44},
                        "macd": {"signal": "多头"},
                        "rsi": {"rsi6": 58.0, "rsi12": 55.0, "rsi24": 52.0},
                    },
                    "patterns": {"latest": ["阳线"]},
                    "volume": {"status": "放量上涨", "volume_ratio_5d": 1.6},
                    "support_resistance": {"support": [20.8, 20.1], "resistance": [22.0, 22.6]},
                    "moneyflow": {"main_net_amount": 1000},
                    "fundamentals": {"pe": 18.5},
                    "data_sources": {"daily": "tickflow"},
                },
                "daily_tail": daily_tail,
                "minute_curves": {
                    "15m": {"source": "tickflow", "rows": minute_rows},
                    "30m": {"source": "tickflow", "rows": minute_rows[:120]},
                },
                "data_sources": {"daily": "tickflow", "quote": "tickflow"},
                "missing_fields": [],
            }
        ],
    }


def _fake_market_payload() -> dict:
    indices = []
    for index, code in enumerate(("000001.SH", "399001.SZ", "399006.SZ", "399330.SZ", "000300.SH", "000905.SH", "000688.SH", "899050.BJ")):
        indices.append(
            {
                "ts_code": code,
                "name": f"指数{index}",
                "trade_date": "20260527",
                "latest": {"close": 3000 + index, "pct_chg": 0.2 + index * 0.1, "amount": 10000},
                "technical": {"ma": {"ma5": 3000, "ma10": 2990, "ma20": 2980, "ma60": 2950}, "volume_status": "量能正常"},
                "daily_tail": [
                    {"trade_date": f"20260{1 + day // 30:02d}{1 + day % 28:02d}", "close": 2800 + day, "pct_chg": 0.1}
                    for day in range(120)
                ],
                "missing_fields": [],
            }
        )
    return {
        "user_intent": "a_share_market_analysis",
        "trade_date": "20260527",
        "requested_indices": [item["ts_code"] for item in indices],
        "requested_dimensions": ["core_indices", "market_breadth", "limit_sentiment", "hot_sectors"],
        "requested_horizons": ["tomorrow"],
        "indices": indices,
        "market_breadth": {"advancing_count": 3200, "declining_count": 1800, "median_pct_chg": 0.3},
        "limit_sentiment": {"emotion_stage": "正常", "limit_up_count": 55, "limit_down_count": 5, "broken_limit_count": 18},
        "hot_sector_context": {
            "hot_industries": [{"name": "电力", "sector_type": "industry", "heat_score": 8.0, "latest_pct_chg": 1.2}],
            "hot_concepts": [{"name": "AI算力", "sector_type": "concept", "heat_score": 15.0, "latest_pct_chg": 2.0}],
            "stock_hot_sectors": {},
            "missing_fields": [],
            "data_sources": {"sector_basic": "fake", "sector_daily": "fake", "sector_members": "fake"},
        },
        "data_sources": {"index_daily": "tickflow", "hot_sector_context": "fake"},
        "missing_fields": [],
    }


class SkillLoadingTest(unittest.TestCase):
    def test_loads_skill_metadata_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / "skills" / "market"
            skill_dir.mkdir(parents=True)
            path = skill_dir / "SKILL.md"
            path.write_text(
                "# market-skill\n"
                "description: 股票筛选助手\n"
                "triggers: 股票, price_volume_ma\n"
                "\n"
                "只解释筛选规则。\n",
                encoding="utf-8",
            )

            skill = parse_skill_file(path, skill_id="market")
            skills = load_skills(root / "skills")

        self.assertEqual(skill.id, "market")
        self.assertEqual(skill.name, "market-skill")
        self.assertEqual(skill.description, "股票筛选助手")
        self.assertEqual(skill.triggers, ("股票", "price_volume_ma"))
        self.assertEqual(skill.content, "只解释筛选规则。")
        self.assertEqual([item.id for item in skills], ["market"])

    def test_loads_yaml_front_matter_skill_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "---\n"
                "name: tickflow\n"
                "description: 使用 TickFlow 获取实时行情和 K 线数据\n"
                "category: data-source\n"
                "source: Vibe-Trading adapted for SATS\n"
                "triggers: TickFlow, 实时行情, 分钟K\n"
                "requires_tools: tickflow_provider, minute_k\n"
                "metadata: {\"ignored\": true}\n"
                "---\n"
                "\n"
                "# TickFlow Skill\n"
                "正文。\n",
                encoding="utf-8",
            )

            skill = parse_skill_file(path, skill_id="tickflow")

        self.assertEqual(skill.id, "tickflow")
        self.assertEqual(skill.name, "tickflow")
        self.assertEqual(skill.description, "使用 TickFlow 获取实时行情和 K 线数据")
        self.assertEqual(skill.category, "data-source")
        self.assertEqual(skill.source, "Vibe-Trading adapted for SATS")
        self.assertEqual(skill.triggers, ("TickFlow", "实时行情", "分钟K"))
        self.assertEqual(skill.requires_tools, ("tickflow_provider", "minute_k"))
        self.assertEqual(skill.content, "# TickFlow Skill\n正文。")

    def test_loads_yaml_front_matter_skill_without_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "---\n"
                "name: tushare-data\n"
                "description: 面向中文自然语言的 Tushare 数据研究技能\n"
                "---\n"
                "\n"
                "正文。\n",
                encoding="utf-8",
            )

            skill = parse_skill_file(path, skill_id="tushare-data")

        self.assertEqual(skill.name, "tushare-data")
        self.assertEqual(skill.description, "面向中文自然语言的 Tushare 数据研究技能")
        self.assertEqual(skill.triggers, ())
        self.assertEqual(skill.content, "正文。")

    def test_loads_skill_auto_routing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "SKILL.md"
            path.write_text(
                "---\n"
                "name: custom-stock-skill\n"
                "description: 自定义个股分析方法\n"
                "category: analysis\n"
                "triggers: 自定义\n"
                "requires_tools: indicators\n"
                "applies_to: stock_analysis, opportunity_discovery\n"
                "evidence: indicators, analyze_signals\n"
                "auto_load: full\n"
                "priority: 77\n"
                "aliases: 自定义股票方法, custom stock\n"
                "---\n"
                "\n"
                "正文。\n",
                encoding="utf-8",
            )

            skill = parse_skill_file(path, skill_id="custom-stock-skill")

        self.assertEqual(skill.applies_to, ("stock_analysis", "opportunity_discovery"))
        self.assertEqual(skill.evidence, ("indicators", "analyze_signals"))
        self.assertEqual(skill.auto_load, "full")
        self.assertEqual(skill.priority, 77)
        self.assertEqual(skill.aliases, ("自定义股票方法", "custom stock"))

    def test_skill_router_selects_metadata_only_stock_skill(self) -> None:
        skills = [
            Skill(
                "custom-stock-skill",
                "custom-stock-skill",
                "自定义个股分析方法",
                (),
                "正文。",
                Path("SKILL.md"),
                category="analysis",
                applies_to=("stock_analysis",),
                evidence=("indicators",),
                auto_load="full",
                priority=10,
            )
        ]

        route = select_skills(
            SkillRouteContext(
                message="分析 000938 下周走势",
                intent="stock_analysis",
                planned_tools=("research.stock_context", "research.internal_analysis"),
                internal_analysis_kinds=("indicators",),
            ),
            skills,
        )

        self.assertEqual(route.skill_ids, ("custom-stock-skill",))
        self.assertEqual(route.selections[0].load_mode, "full")
        self.assertIn("technical", route.collections)

    def test_skill_router_prioritizes_explicit_skill_name(self) -> None:
        skills = [
            Skill("market", "market", "", (), "body", Path("market")),
            Skill("risk-analysis", "risk-analysis", "", (), "body", Path("risk")),
        ]

        route = select_skills(
            SkillRouteContext(message="只用 risk-analysis 解释", intent="market_analysis", explicit_skill_names=("risk-analysis",)),
            skills,
        )

        self.assertEqual(route.skill_ids[0], "risk-analysis")

    def test_matches_skills_by_trigger_and_limits_results(self) -> None:
        skills = [
            Skill("a", "a", "", ("股票",), "a", Path("a")),
            Skill("b", "b", "", ("price_volume_ma",), "b", Path("b")),
            Skill("c", "c", "", ("筛选",), "c", Path("c")),
            Skill("d", "d", "", ("无关",), "d", Path("d")),
        ]

        matched = match_skills("帮我解释 price_volume_ma 股票筛选", skills, limit=3)

        self.assertEqual([item.id for item in matched], ["b", "a", "c"])

    def test_matches_tickflow_and_tushare_data_skills(self) -> None:
        skills = load_skills(Path("skills"))

        tushare_match = match_skills("帮我查 Tushare 财报趋势", skills)
        tickflow_match = match_skills("用 TickFlow 看实时行情和分钟K", skills)
        moneyflow_match = match_skills("最近资金流怎么样", skills)
        micro_match = match_skills("用 TickFlow 看分钟K微观结构", skills)
        financial_match = match_skills("分析财报、PE、ROE、ST风险", skills)
        report_match = match_skills("生成研究报告", skills)
        unrelated_match = match_skills("今天午饭吃什么", skills)

        self.assertIn("tushare-data", [item.id for item in tushare_match])
        self.assertIn("tickflow", [item.id for item in tickflow_match])
        self.assertIn("tushare-data", [item.id for item in moneyflow_match])
        self.assertEqual([item.id for item in micro_match], ["tickflow", "minute-analysis", "market-microstructure"])
        self.assertEqual([item.id for item in financial_match], ["tushare-data", "ashare-pre-st-filter", "financial-statement"])
        self.assertEqual([item.id for item in report_match], ["report-generate", "risk-analysis"])
        self.assertEqual(unrelated_match, [])

    def test_loads_china_market_skills_metadata(self) -> None:
        skills = {skill.id: skill for skill in load_skills(Path("skills"))}
        expected = {
            "quant-factor-screener": "strategy",
            "high-dividend-strategy": "strategy",
            "undervalued-stock-screener": "strategy",
            "small-cap-growth-identifier": "strategy",
            "esg-screener": "analysis",
            "portfolio-health-check": "risk-analysis",
            "risk-adjusted-return-optimizer": "risk-analysis",
            "suitability-report-generator": "tool",
            "tech-hype-vs-fundamentals": "analysis",
            "insider-trading-analyzer": "analysis",
            "event-driven-detector": "flow",
            "sentiment-reality-gap": "analysis",
        }

        for skill_id, category in expected.items():
            with self.subTest(skill_id=skill_id):
                skill = skills[skill_id]
                self.assertEqual(skill.category, category)
                self.assertIn("finskills China-market adapted for SATS", skill.source)
                self.assertIn("Apache-2.0", skill.source)
                self.assertTrue(skill.triggers)
                self.assertTrue(skill.requires_tools)

    def test_matches_china_market_skills(self) -> None:
        skills = load_skills(Path("skills"))
        cases = {
            "多因子选股模型": "quant-factor-screener",
            "高股息红利策略": "high-dividend-strategy",
            "低估值价值投资": "undervalued-stock-screener",
            "小盘成长专精特新": "small-cap-growth-identifier",
            "ESG公司治理": "esg-screener",
            "组合健康诊断压力测试": "portfolio-health-check",
            "组合优化再平衡": "risk-adjusted-return-optimizer",
            "适当性报告风险披露": "suitability-report-generator",
            "董监高增持信号": "insider-trading-analyzer",
            "事件驱动并购重组": "event-driven-detector",
            "科技泡沫基本面": "tech-hype-vs-fundamentals",
            "市场错杀情绪与基本面背离": "sentiment-reality-gap",
            "行业轮动经济周期": "sector-rotation",
        }

        for query, expected in cases.items():
            with self.subTest(query=query):
                matched = [skill.id for skill in match_skills(query, skills)]
                self.assertIn(expected, matched)

    def test_loads_dsa_strategy_skills_metadata(self) -> None:
        skills = {skill.id: skill for skill in load_skills(Path("skills"))}
        expected = {
            "bull-trend": "strategy",
            "shrink-pullback": "strategy",
            "ma-golden-cross": "strategy",
            "volume-breakout": "strategy",
            "box-oscillation": "strategy",
            "bottom-volume": "strategy",
            "one-yang-three-yin": "strategy",
            "dragon-head": "strategy",
            "hot-theme": "analysis",
            "emotion-cycle": "analysis",
            "expectation-repricing": "analysis",
            "growth-quality": "analysis",
        }

        for skill_id, category in expected.items():
            with self.subTest(skill_id=skill_id):
                skill = skills[skill_id]
                self.assertEqual(skill.category, category)
                self.assertIn("daily_stock_analysis strategies adapted for SATS", skill.source)
                self.assertTrue(skill.triggers)
                self.assertTrue(skill.requires_tools)

    def test_matches_dsa_strategy_skills(self) -> None:
        skills = load_skills(Path("skills"))
        cases = {
            "多头趋势回踩低吸": "bull-trend",
            "缩量回踩MA10": "shrink-pullback",
            "均线金叉确认": "ma-golden-cross",
            "放量突破阻力": "volume-breakout",
            "箱体震荡区间交易": "box-oscillation",
            "底部放量企稳": "bottom-volume",
            "一阳夹三阴形态": "one-yang-three-yin",
            "龙头战法": "dragon-head",
            "热点题材退潮": "hot-theme",
            "情绪周期恐慌底": "emotion-cycle",
            "预期重估和预期差": "expectation-repricing",
            "成长质量和ROE": "growth-quality",
            "波浪理论": "elliott-wave",
        }

        for query, expected in cases.items():
            with self.subTest(query=query):
                matched = [skill.id for skill in match_skills(query, skills)]
                self.assertIn(expected, matched)

    def test_format_skill_list(self) -> None:
        skills = [Skill("a", "alpha", "描述", ("股票", "筛选"), "body", Path("a"))]

        self.assertEqual(format_skill_list([]), "无可用 skill")
        self.assertIn("1. alpha - 描述 触发: 股票, 筛选", format_skill_list(skills))

    def test_cli_skills_lists_yaml_front_matter_descriptions(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            self.assertEqual(main(["skills"]), 0)

        output = stdout.getvalue()
        self.assertIn("tickflow - 使用 TickFlow Python SDK 获取", output)
        self.assertIn("tushare-data - 面向中文自然语言的 Tushare 数据研究技能", output)
        self.assertIn("[data-source]", output)
        self.assertIn("workflow-templates", output)


class ChatSessionTest(unittest.TestCase):
    def test_chat_planner_routes_stock_market_discovery_and_general_questions(self) -> None:
        skills = load_skills(Path("skills"))

        stock_plan = build_chat_plan(
            "000938 技术面分析，给出买入时机",
            skills=skills,
            stock_question=StockQuestion(symbols=["000938.SZ"], has_stock_question=True),
        )
        self.assertEqual(stock_plan.intent, "stock_analysis")
        self.assertIn("stock_context", stock_plan.data_requirements)
        self.assertIn("market_context", stock_plan.data_requirements)
        self.assertIn("technical-basic", stock_plan.skills)
        self.assertIn("risk-analysis", stock_plan.skills)
        self.assertIn("bull-trend", stock_plan.skills)

        market_plan = build_chat_plan("今天A股大盘分析，明天走势预测", skills=skills)
        self.assertEqual(market_plan.intent, "market_analysis")
        self.assertIn("market_context", market_plan.data_requirements)
        self.assertIn("sats-market-assistant", market_plan.skills)
        self.assertIn("sentiment-analysis", market_plan.skills)
        self.assertIn("sector-rotation", market_plan.skills)
        self.assertIn("market-microstructure", market_plan.skills)
        self.assertIn("emotion-cycle", market_plan.skills)

        discovery_plan = build_chat_plan("给出几个未来几天可能上涨的股票", skills=skills)
        self.assertEqual(discovery_plan.intent, "stock_picking_agent")
        self.assertIn("stock_picking_agent", discovery_plan.internal_actions)
        self.assertIn("opportunity_discovery", discovery_plan.internal_actions)
        self.assertIn("sats-market-assistant", discovery_plan.skills)
        self.assertIn("hot-theme", discovery_plan.skills)
        self.assertIn("sector-rotation", discovery_plan.skills)

        rule_plan = build_chat_plan("新增一个低位放量突破筛选规则", skills=skills)
        self.assertEqual(rule_plan.intent, "screening_rule_generation")
        self.assertIn("sats-market-assistant", rule_plan.skills)
        self.assertEqual(rule_plan.internal_actions, ())

        general_plan = build_chat_plan("今天午饭吃什么", skills=skills)
        self.assertEqual(general_plan.intent, "general_qa")
        self.assertEqual(general_plan.data_requirements, ())
        self.assertEqual(general_plan.internal_actions, ())

        financial_plan = build_chat_plan(
            "评价 000938 基本面和估值",
            skills=skills,
            stock_question=StockQuestion(symbols=["000938.SZ"], has_stock_question=True),
        )
        self.assertIn("financial-statement", financial_plan.skills)
        self.assertIn("valuation-model", financial_plan.skills)
        self.assertIn("fundamental-filter", financial_plan.skills)

    def test_stock_analysis_defaults_to_all_stock_domain_rag_collections(self) -> None:
        skills = load_skills(Path("skills"))
        plan = build_chat_plan(
            "分析000938",
            skills=skills,
            stock_question=StockQuestion(symbols=["000938.SZ"], has_stock_question=True),
        )

        self.assertEqual(plan.intent, "stock_analysis")
        self.assertEqual(_collections_for_plan(plan), STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS)

    def test_chat_planner_routes_china_market_skills_to_rag_collections(self) -> None:
        skills = load_skills(Path("skills"))
        cases = {
            "多因子选股模型": ("quant-factor-screener", ("signals", "fundamental")),
            "高股息红利策略": ("high-dividend-strategy", ("fundamental",)),
            "低估值价值投资": ("undervalued-stock-screener", ("fundamental",)),
            "ESG公司治理": ("esg-screener", ("fundamental",)),
            "组合健康诊断压力测试": ("portfolio-health-check", ("risk",)),
            "组合优化再平衡": ("risk-adjusted-return-optimizer", ("risk",)),
            "适当性报告风险披露": ("suitability-report-generator", ("risk",)),
            "董监高增持信号": ("insider-trading-analyzer", ("sentiment",)),
            "事件驱动并购重组": ("event-driven-detector", ("sentiment",)),
            "科技泡沫基本面": ("tech-hype-vs-fundamentals", ("fundamental",)),
            "市场错杀情绪与基本面背离": ("sentiment-reality-gap", ("sentiment",)),
            "行业轮动经济周期": ("sector-rotation", ("market", "sentiment")),
            "均线金叉策略": ("ma-golden-cross", ("technical", "signals")),
            "放量突破策略": ("volume-breakout", ("technical", "signals")),
            "龙头战法": ("dragon-head", ("market", "sentiment")),
            "热点题材退潮": ("hot-theme", ("market", "sentiment")),
            "情绪周期恐慌底": ("emotion-cycle", ("market", "sentiment")),
            "预期重估和预期差": ("expectation-repricing", ("fundamental", "sentiment")),
            "成长质量和ROE": ("growth-quality", ("fundamental",)),
        }

        for query, (expected_skill, expected_collections) in cases.items():
            with self.subTest(query=query):
                plan = build_chat_plan(query, skills=skills)
                self.assertIn(expected_skill, plan.skills)
                collections = _collections_for_plan(plan)
                for collection in expected_collections:
                    self.assertIn(collection, collections)

        factor_plan = build_chat_plan("多因子选股模型", skills=skills)
        self.assertEqual(factor_plan.intent, "stock_research_framework")
        self.assertIn("market_context", factor_plan.data_requirements)

    def test_chat_planner_keeps_real_data_requirements_for_china_market_stock_query(self) -> None:
        skills = load_skills(Path("skills"))
        plan = build_chat_plan(
            "分析紫光股份董监高增持",
            skills=skills,
            stock_question=StockQuestion(symbols=["000938.SZ"], has_stock_question=True),
        )

        self.assertEqual(plan.intent, "stock_analysis")
        self.assertIn("insider-trading-analyzer", plan.skills)
        self.assertIn("stock_context", plan.data_requirements)
        self.assertIn("market_context", plan.data_requirements)

    def test_chat_planner_uses_preprocess_hints(self) -> None:
        skills = load_skills(Path("skills"))
        preprocess = SimpleNamespace(
            intent="stock_analysis",
            symbols=("000938.SZ",),
            needs_stock_context=True,
            needs_market_context=True,
            needs_opportunity_discovery=False,
            needs_indicators=True,
            skill_hints=("technical-basic",),
        )

        plan = build_chat_plan("分析紫光股份技术面", skills=skills, preprocess=preprocess)

        self.assertEqual(plan.intent, "stock_analysis")
        self.assertIn("stock_context", plan.data_requirements)
        self.assertIn("market_context", plan.data_requirements)
        self.assertIn("technical-basic", plan.skills)

    def test_chat_session_injects_matched_skill_and_keeps_history(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro", light_model_name="mimo-light")
        skills = [
            Skill(
                "market",
                "market-skill",
                "股票筛选助手",
                ("股票",),
                "解释筛选规则，不构成投资建议。",
                Path("SKILL.md"),
            )
        ]
        session = ChatSession(settings=settings, skills=skills, llm_factory=FakeLLM, memory_enabled=False)

        result = session.ask("帮我解释股票筛选")

        self.assertEqual(result.content, "收到")
        self.assertEqual(result.skill_names, ("market-skill",))
        self.assertEqual(FakeLLM.instances[0].kwargs["model_name"], "mimo-light")
        self.assertEqual(FakeLLM.instances[0].kwargs["profile"], "light")
        messages = FakeLLM.instances[0].messages
        self.assertIn("SATS CLI 助手", messages[0]["content"])
        self.assertIn("chat_plan", messages[1]["content"])
        self.assertIn("market-skill", messages[2]["content"])
        self.assertNotIn("解释筛选规则，不构成投资建议。", messages[2]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "帮我解释股票筛选"})
        self.assertEqual(session.history[-2]["role"], "user")
        self.assertEqual(session.history[-1]["role"], "assistant")

    def test_chat_session_returns_clear_message_when_main_llm_times_out(self) -> None:
        TimeoutLLM.instances = []
        settings = SimpleNamespace(
            project_root=Path("."),
            openai_model="mimo-v2.5-pro",
            light_model_name="mimo-light",
            llm_timeout_seconds=180,
        )
        session = ChatSession(settings=settings, skills=[], llm_factory=TimeoutLLM, memory_enabled=False)
        with patch("sats.chat.build_market_llm_context", return_value=SimpleNamespace(system_message="market")):
            result = session.ask("帮我总结今天的大盘")

        self.assertIn("请求超时", result.content)
        self.assertIn("mimo-v2.5-pro", result.content)
        self.assertNotIn("mimo-light / mimo-v2.5-pro", result.content)
        self.assertIn("切换更快的模型", result.content)
        self.assertEqual(len(TimeoutLLM.instances), 1)
        self.assertEqual(TimeoutLLM.instances[0].kwargs["model_name"], "mimo-v2.5-pro")
        self.assertEqual(TimeoutLLM.instances[0].kwargs["profile"], "default")
        self.assertEqual(TimeoutLLM.instances[0].kwargs["timeout_seconds"], 180)

    def test_chat_session_uses_default_model_for_market_analysis(self) -> None:
        LightTimeoutDefaultLLM.instances = []
        settings = SimpleNamespace(
            project_root=Path("."),
            openai_model="deepseek-v4-pro",
            light_model_name="mimo-light",
            llm_timeout_seconds=180,
        )
        session = ChatSession(settings=settings, skills=[], llm_factory=LightTimeoutDefaultLLM, memory_enabled=False)

        with patch("sats.chat.build_market_llm_context", return_value=SimpleNamespace(system_message="market")):
            result = session.ask("帮我总结今天的大盘")

        self.assertEqual(result.content, "默认模型回答")
        self.assertEqual([item.kwargs["profile"] for item in LightTimeoutDefaultLLM.instances], ["default"])
        self.assertEqual([item.kwargs["timeout_seconds"] for item in LightTimeoutDefaultLLM.instances], [180])
        self.assertEqual(LightTimeoutDefaultLLM.instances[0].kwargs["model_name"], "deepseek-v4-pro")

    def test_chat_session_uses_light_model_for_memory_tasks(self) -> None:
        FakeLLM.instances = []
        extractor = RecordingMemoryExtractor()
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                project_root=Path("."),
                db_path=Path(tmp) / "sats.duckdb",
                openai_model="main-model",
                light_model_name="light-model",
            )
            session = ChatSession(
                settings=settings,
                skills=[],
                llm_factory=FakeLLM,
                memory_enabled=True,
                memory_extractor=extractor,
                summary_threshold_messages=2,
                summary_refresh_messages=1,
            )

            result = session.ask("帮我解释股票筛选")

        self.assertEqual(result.content, "收到")
        self.assertEqual(len(FakeLLM.instances), 1)
        self.assertEqual(FakeLLM.instances[0].kwargs["model_name"], "light-model")
        self.assertEqual(FakeLLM.instances[0].kwargs["profile"], "light")
        self.assertEqual(extractor.extract_llms[0].kwargs["model_name"], "light-model")
        self.assertEqual(extractor.extract_llms[0].kwargs["profile"], "light")
        self.assertIs(extractor.extract_llms[0], extractor.summary_llms[0])

    def test_chat_session_generates_screening_rule_plan_without_llm(self) -> None:
        FakeLLM.instances = []
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path("."), db_path=Path(tmp) / "sats.duckdb", openai_model="deepseek-v4-pro")
            session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
            with patch("sats.chat_components.list_rules", return_value=[]):
                result = session.ask("新增一个低位放量突破筛选规则 rule_name: nl_test_low_volume_breakout_chat")

        self.assertIn("新筛选规则生成计划", result.content)
        self.assertIn("nl_test_low_volume_breakout_chat", result.content)
        self.assertIn("确认生成规则 nl_test_low_volume_breakout_chat", result.content)
        self.assertEqual(result.data_names, ("规则计划",))
        self.assertEqual(FakeLLM.instances, [])

    def test_chat_session_confirms_pending_screening_rule_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path("."), db_path=Path(tmp) / "sats.duckdb", openai_model="deepseek-v4-pro")
            session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
            with patch("sats.chat_components.list_rules", return_value=[]):
                session.ask("新增一个低位放量突破筛选规则 rule_name: nl_test_confirm_rule_chat")
            generated = GeneratedRuleResult(
                rule_name="nl_test_confirm_rule_chat",
                class_name="NlTestConfirmRuleChatRule",
                path=Path("sats/screening/rules/generated/nl_test_confirm_rule_chat.py"),
            )

            with (
                patch("sats.chat_components.list_rules", return_value=[]),
                patch("sats.chat.generate_rule_code", return_value=generated) as generator,
            ):
                result = session.ask("确认生成规则 nl_test_confirm_rule_chat")

        generator.assert_called_once()
        self.assertIn("已生成筛选规则: nl_test_confirm_rule_chat", result.content)
        self.assertIn("sats screen --rule nl_test_confirm_rule_chat", result.content)
        self.assertEqual(result.data_names, ("生成规则",))

    def test_chat_session_rejects_confirmation_without_pending_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path("."), db_path=Path(tmp) / "sats.duckdb", openai_model="deepseek-v4-pro")
            session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)

            result = session.ask("确认生成规则 nl_missing")

        self.assertIn("当前没有待生成规则", result.content)

    def test_chat_session_revises_rule_plan_questions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(project_root=Path("."), db_path=Path(tmp) / "sats.duckdb", openai_model="deepseek-v4-pro")
            session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)

            with patch("sats.chat_components.list_rules", return_value=[]):
                first = session.ask("新增筛选规则 rule_name: nl_test_news_volume 使用新闻舆情和放量")
            self.assertIn("需要你确认", first.content)
            self.assertIn("新闻/舆情", first.content)

            with patch("sats.chat_components.list_rules", return_value=[]):
                second = session.ask("去掉新闻舆情条件")

        self.assertIn("确认生成规则 nl_test_news_volume", second.content)
        self.assertNotIn("暂不支持的数据需求", second.content)

    def test_chat_session_can_inject_yaml_front_matter_skill(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, llm_factory=FakeLLM)

        result = session.ask("用 TickFlow 看实时行情和分钟K")

        self.assertIn("tickflow", result.skill_names)
        self.assertIn("tickflow", FakeLLM.instances[0].messages[1]["content"])

    def test_chat_session_injects_real_stock_context_before_llm(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM)
        question = StockQuestion(symbols=["002436.SZ"], trade_date=None, has_stock_question=True)

        with (
            patch(
                "sats.chat.build_stock_llm_context",
                return_value=SimpleNamespace(
                    system_message='真实股票结构化数据 {"ts_code":"002436.SZ","minute_curves":{"15m":[],"30m":[]}}',
                    question=question,
                    trade_date="20260515",
                ),
            ) as builder,
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[{"ts_code":"000001.SH"}]}'),
            ) as market_builder,
        ):
            result = session.ask("分析002436")

        self.assertEqual(result.content, "收到")
        builder.assert_called_once()
        market_builder.assert_called_once()
        messages = FakeLLM.instances[0].messages
        self.assertIn("SATS CLI 助手", messages[0]["content"])
        self.assertIn("chat_plan", messages[1]["content"])
        self.assertIn("真实股票结构化数据", messages[2]["content"])
        self.assertIn("真实 A 股大盘结构化数据", messages[2]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "分析002436"})
        self.assertEqual(result.data_names, ("个股", "大盘"))

    def test_chat_session_uses_preprocessor_symbols_for_stock_name(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
        question = StockQuestion(symbols=["000938.SZ"], trade_date=None, has_stock_question=True)
        preprocess = SimpleNamespace(
            intent="stock_analysis",
            symbols=("000938.SZ",),
            stock_names=("紫光股份",),
            trade_date=None,
            as_of_time=None,
            reference_needed=False,
            needs_stock_context=True,
            needs_market_context=True,
            needs_opportunity_discovery=False,
            needs_indicators=True,
            skill_hints=("technical-basic",),
            confidence=0.9,
            missing_questions=(),
            system_message=lambda: "SATS chat_preprocess:\n- symbols: 000938.SZ",
        )
        stock_calls = []

        def fake_stock_context(message, *, question, **kwargs):
            stock_calls.append(question)
            return SimpleNamespace(
                system_message='真实股票结构化数据 {"ts_code":"000938.SZ"}',
                question=question,
                trade_date="20260520",
            )

        with (
            patch("sats.chat.preprocess_chat_message", return_value=preprocess),
            patch("sats.chat.build_stock_llm_context", side_effect=fake_stock_context),
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[]}'),
            ),
        ):
            result = session.ask("分析紫光股份技术面")

        self.assertEqual(stock_calls[0].symbols, ["000938.SZ"])
        self.assertIn("个股", result.data_names)
        payload = "\n".join(message["content"] for message in FakeLLM.instances[0].messages)
        self.assertIn("chat_preprocess", payload)
        self.assertIn("000938.SZ", payload)

    def test_chat_session_uses_quote_context_without_full_stock_context_for_quote_question(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
        preprocess = SimpleNamespace(
            intent="stock_quote",
            symbols=("000001.SZ",),
            stock_names=(),
            trade_date=None,
            as_of_time=None,
            reference_needed=True,
            needs_stock_context=False,
            needs_market_context=False,
            needs_opportunity_discovery=False,
            needs_indicators=False,
            needs_realtime_quote_context=True,
            market_indices=(),
            market_dimensions=(),
            market_horizons=(),
            market_plan_source="",
            skill_hints=("tickflow",),
            confidence=0.9,
            missing_questions=(),
            source="local",
            system_message=lambda: "SATS chat_preprocess:\n- quote=True",
        )

        with (
            patch("sats.chat.preprocess_chat_message", return_value=preprocess),
            patch(
                "sats.chat.build_stock_quote_llm_context",
                return_value=SimpleNamespace(system_message='真实实时报价 {"ts_code":"000001.SZ"}'),
            ) as quote_builder,
            patch("sats.chat.build_stock_llm_context") as stock_builder,
            patch("sats.chat.build_market_llm_context") as market_builder,
        ):
            result = session.ask("查看上面股票实时报价")

        quote_builder.assert_called_once()
        stock_builder.assert_not_called()
        market_builder.assert_not_called()
        self.assertIn("实时报价", result.data_names)
        payload = "\n".join(message["content"] for message in FakeLLM.instances[0].messages)
        self.assertIn("真实实时报价", payload)

    def test_chat_session_preprocessor_questions_stop_before_llm(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
        preprocess = SimpleNamespace(
            intent="stock_analysis",
            symbols=(),
            stock_names=("银行",),
            trade_date=None,
            as_of_time=None,
            reference_needed=False,
            needs_stock_context=True,
            needs_market_context=True,
            needs_opportunity_discovery=False,
            needs_indicators=True,
            skill_hints=(),
            confidence=0.5,
            missing_questions=("股票名称“银行”匹配到多个结果，请指定 6 位代码。",),
            system_message=lambda: "",
        )

        with patch("sats.chat.preprocess_chat_message", return_value=preprocess):
            result = session.ask("分析银行技术面")

        self.assertIn("需要先确认", result.content)
        self.assertEqual(FakeLLM.instances, [])

    def test_chat_session_injects_real_market_context_before_llm(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM)

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[{"ts_code":"000001.SH"}]}'),
            ) as builder,
        ):
            result = session.ask("今天A股大盘分析，明天和下周走势预测")

        self.assertEqual(result.content, "收到")
        builder.assert_called_once()
        self.assertEqual(builder.call_args.kwargs["dimensions"], ("core_indices", "market_breadth", "limit_sentiment", "hot_sectors"))
        self.assertEqual(builder.call_args.kwargs["horizons"], ("today", "tomorrow", "next_week"))
        messages = FakeLLM.instances[0].messages
        self.assertIn("chat_plan", messages[1]["content"])
        self.assertIn("真实 A 股大盘结构化数据", messages[2]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "今天A股大盘分析，明天和下周走势预测"})
        self.assertEqual(result.data_names, ("大盘",))

    def test_chat_session_handles_today_market_tomorrow_trend_question(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM)

        with patch(
            "sats.chat.build_market_llm_context",
            return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[{"ts_code":"000001.SH"}]}'),
        ) as builder:
            result = session.ask("分析今天大盘走势，预测明天走势")

        self.assertEqual(result.content, "收到")
        builder.assert_called_once()
        self.assertEqual(builder.call_args.kwargs["dimensions"], ("core_indices", "market_breadth", "limit_sentiment", "hot_sectors"))
        self.assertEqual(builder.call_args.kwargs["horizons"], ("today", "tomorrow"))
        messages = FakeLLM.instances[0].messages
        self.assertIn("真实 A 股大盘结构化数据", messages[2]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "分析今天大盘走势，预测明天走势"})
        self.assertEqual(result.data_names, ("大盘",))

    def test_chat_session_handles_today_possessive_market_tomorrow_trend_question(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM)

        with patch(
            "sats.chat.build_market_llm_context",
            return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[{"ts_code":"000001.SH"}]}'),
        ) as builder:
            result = session.ask("分析今天的大盘走势，预测明天走势")

        self.assertEqual(result.content, "收到")
        self.assertNotIn("补充 6 位股票代码", result.content)
        builder.assert_called_once()
        self.assertEqual(builder.call_args.kwargs["dimensions"], ("core_indices", "market_breadth", "limit_sentiment", "hot_sectors"))
        self.assertEqual(builder.call_args.kwargs["horizons"], ("today", "tomorrow"))
        messages = FakeLLM.instances[0].messages
        self.assertIn("真实 A 股大盘结构化数据", messages[2]["content"])
        self.assertEqual(messages[-1], {"role": "user", "content": "分析今天的大盘走势，预测明天走势"})
        self.assertEqual(result.data_names, ("大盘",))

    def test_chat_session_injects_opportunity_discovery_context_before_llm(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        skills = [
            Skill(
                "sats-market-assistant",
                "sats-market-assistant",
                "SATS 市场助手",
                ("选股",),
                "市场助手正文。",
                Path("SKILL.md"),
            )
        ]
        session = ChatSession(settings=settings, skills=skills, llm_factory=FakeLLM, memory_enabled=False)

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch(
                "sats.chat.run_stock_picking_agent",
                return_value=SimpleNamespace(system_message='真实短线机会发现 {"ts_code":"000938.SZ"}'),
            ) as discover,
        ):
            result = session.ask("给出几个股票，预计未来几天有上涨趋势的股票")

        self.assertIn("sats-market-assistant", result.skill_names)
        discover.assert_called_once()
        self.assertIsNone(discover.call_args.kwargs["limit"])
        payload = "\n".join(message["content"] for message in FakeLLM.instances[0].messages)
        self.assertIn("真实短线机会发现", payload)
        self.assertIn("000938.SZ", payload)

    def test_chat_session_uses_compact_opportunity_context_for_final_llm(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(
            project_root=Path("."),
            db_path=Path("sats.duckdb"),
            openai_model="deepseek-v4-pro",
            llm_context_limit_tokens=20000,
            llm_context_output_reserve_tokens=0,
        )
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
        fake_result = SimpleNamespace(
            system_message="FULL_DISCOVER_CONTEXT " + ("长字段" * 10000),
            system_message_for_llm=lambda: 'COMPACT_DISCOVER_CONTEXT {"ts_code":"000938.SZ"}',
            message="",
            candidates=[],
            report_path="",
        )

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=SimpleNamespace(system_message="FULL_MARKET_CONTEXT")),
            patch("sats.chat.run_stock_picking_agent", return_value=fake_result),
        ):
            session.ask("给出几个股票，预计未来几天有上涨趋势的股票")

        messages = FakeLLM.instances[0].messages
        payload = "\n".join(message["content"] for message in messages)
        self.assertIn("COMPACT_DISCOVER_CONTEXT", payload)
        self.assertIn("000938.SZ", payload)
        self.assertNotIn("FULL_DISCOVER_CONTEXT", payload)
        self.assertNotIn("FULL_MARKET_CONTEXT", payload)
        self.assertLess(
            sum(estimate_llm_message_tokens(message["content"]) for message in messages),
            llm_context_input_budget_tokens(settings),
        )

    def test_chat_session_passes_requested_limit_and_tomorrow_horizon_to_stock_picking_agent(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch(
                "sats.chat.run_stock_picking_agent",
                return_value=SimpleNamespace(system_message='真实短线机会发现 {"ts_code":"000938.SZ"}'),
            ) as discover,
        ):
            session.ask("列出10支明天大概率上涨的股票")

        discover.assert_called_once()
        self.assertEqual(discover.call_args.kwargs["limit"], 10)
        self.assertEqual(discover.call_args.kwargs["market_horizons"], ("tomorrow",))

    def test_chat_session_treats_hot_sector_as_auto_discovery_context(self) -> None:
        HotSectorClarificationLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=HotSectorClarificationLLM, memory_enabled=False)

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch(
                "sats.chat.run_stock_picking_agent",
                return_value=SimpleNamespace(system_message='真实短线机会发现 {"ts_code":"000938.SZ"}'),
            ) as discover,
        ):
            result = session.ask("根据今天的热点板块，筛选一些明天大概率上涨的股票")

        self.assertNotIn("需要先确认", result.content)
        discover.assert_called_once()
        self.assertTrue(discover.call_args.kwargs["hot_sector_enabled"])
        self.assertIn("tomorrow", discover.call_args.kwargs["market_horizons"])

    def test_chat_session_falls_back_when_opportunity_llm_returns_only_title(self) -> None:
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=TitleOnlyLLM, memory_enabled=False)
        candidate = SimpleNamespace(
            ts_code="000938.SZ",
            name="紫光股份",
            events=[{"label": "蛟龙出海"}],
            llm_reason="技术信号共振",
            entry_trigger="放量突破压力位",
            invalidation="跌破支撑位",
            risk="市场情绪转弱",
            hot_sectors=[],
            ranking_score=98.0,
            local_score=90.0,
            decision="买入观察",
            trend="震荡",
        )
        fake_result = SimpleNamespace(
            message="",
            candidates=[candidate],
            report_path="reports/opportunity_discovery_20260529.md",
            system_message="真实短线机会发现 000938.SZ",
        )

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch("sats.chat.run_stock_picking_agent", return_value=fake_result),
        ):
            result = session.ask("给出几个股票，预计未来几天有上涨趋势的股票")

        self.assertIn("000938.SZ 紫光股份", result.content)
        self.assertIn("触发: 放量突破压力位", result.content)
        self.assertIn("报告: reports/opportunity_discovery_20260529.md", result.content)

    def test_chat_session_falls_back_to_local_discover_output_on_final_context_error(self) -> None:
        ContextLengthErrorLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=ContextLengthErrorLLM, memory_enabled=False)
        candidate = SimpleNamespace(
            ts_code="000938.SZ",
            name="紫光股份",
            events=[{"label": "蛟龙出海"}],
            llm_reason="技术信号共振",
            entry_trigger="放量突破压力位",
            invalidation="跌破支撑位",
            risk="市场情绪转弱",
            hot_sectors=[],
            chan_signals=[],
            ranking_score=98.0,
            local_score=90.0,
            decision="买入观察",
            trend="看多",
        )
        fake_result = SimpleNamespace(
            message="",
            candidates=[candidate],
            candidate_count=1,
            scanned_count=5,
            trade_date="20260520",
            signals="short_up",
            llm_unavailable=False,
            llm_pool_count=1,
            report_path="reports/opportunity_discovery_20260520.md",
            system_message_for_llm=lambda: 'COMPACT_DISCOVER_CONTEXT {"ts_code":"000938.SZ"}',
        )

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch("sats.chat.run_stock_picking_agent", return_value=fake_result),
        ):
            result = session.ask("给出几个股票，预计未来几天有上涨趋势的股票")

        self.assertIn("000938.SZ 紫光股份", result.content)
        self.assertIn("触发: 放量突破压力位", result.content)
        self.assertIn("报告: reports/opportunity_discovery_20260520.md", result.content)
        self.assertNotIn("LLM错误", result.content)

    def test_build_chat_messages_without_skill(self) -> None:
        messages = build_chat_messages("hello", history=[{"role": "assistant", "content": "old"}], skills=[])

        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1], {"role": "assistant", "content": "old"})
        self.assertEqual(messages[2], {"role": "user", "content": "hello"})
        self.assertIn("不得编造实时行情", SYSTEM_PROMPT)
        self.assertIn("价格", SYSTEM_PROMPT)

    def test_build_chat_messages_can_include_stock_context(self) -> None:
        messages = build_chat_messages("分析002436", skills=[], stock_context="真实股票数据")

        self.assertEqual(messages[1], {"role": "system", "content": "真实股票数据"})
        self.assertEqual(messages[2], {"role": "user", "content": "分析002436"})

    def test_build_chat_messages_can_include_market_context(self) -> None:
        messages = build_chat_messages("分析大盘", skills=[], market_context="真实大盘数据")

        self.assertEqual(messages[1], {"role": "system", "content": "真实大盘数据"})
        self.assertEqual(messages[2], {"role": "user", "content": "分析大盘"})

    def test_build_chat_messages_can_include_opportunity_context(self) -> None:
        messages = build_chat_messages("选股", skills=[], opportunity_context="真实短线机会数据")

        self.assertEqual(messages[1], {"role": "system", "content": "真实短线机会数据"})
        self.assertEqual(messages[2], {"role": "user", "content": "选股"})

    def test_build_chat_messages_can_include_chan_context(self) -> None:
        messages = build_chat_messages("解释三买", skills=[], chan_context="缠论RAG证据")

        self.assertEqual(messages[1], {"role": "system", "content": "缠论RAG证据"})
        self.assertEqual(messages[2], {"role": "user", "content": "解释三买"})

    def test_build_chat_messages_can_include_research_context(self) -> None:
        messages = build_chat_messages("解释三买", skills=[], research_context="股票知识库RAG证据")

        self.assertEqual(messages[1], {"role": "system", "content": "股票知识库RAG证据"})
        self.assertEqual(messages[2], {"role": "user", "content": "解释三买"})

    def test_chat_session_injects_stock_research_rag(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=load_skills(Path("skills")), llm_factory=FakeLLM, memory_enabled=False)
        fake_research = SimpleNamespace(
            system_message="股票知识库RAG证据",
            sources=({"type": "knowledge", "collection": "chan"},),
        )

        with (
            patch("sats.chat.build_chan_chat_context", return_value=None),
            patch("sats.chat.build_stock_research_context", return_value=fake_research) as research,
        ):
            result = session.ask("解释三买和背驰")

        research.assert_called_once()
        self.assertIn("知识库RAG", result.data_names)
        self.assertEqual(result.sources, ({"type": "knowledge", "collection": "chan"},))
        payload = "\n".join(message["content"] for message in FakeLLM.instances[0].messages)
        self.assertIn("股票知识库RAG证据", payload)

    def test_chat_session_uses_default_stock_domain_rag_for_stock_analysis(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
        question = StockQuestion(symbols=["000938.SZ"], has_stock_question=True)
        fake_research = SimpleNamespace(
            system_message="股票知识库RAG证据",
            sources=({"type": "knowledge", "collection": "technical"},),
        )

        with (
            patch(
                "sats.chat.build_stock_llm_context",
                return_value=SimpleNamespace(
                    system_message='真实股票结构化数据 {"ts_code":"000938.SZ"}',
                    question=question,
                    trade_date="20260520",
                ),
            ),
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[]}'),
            ),
            patch("sats.chat.build_stock_research_context", return_value=fake_research) as research,
        ):
            result = session.ask("分析000938")

        research.assert_called_once()
        self.assertEqual(research.call_args.kwargs["collections"], STOCK_ANALYSIS_DEFAULT_RAG_COLLECTIONS)
        self.assertEqual(research.call_args.kwargs["limit"], STOCK_ANALYSIS_DEFAULT_RAG_LIMIT)
        self.assertIn("知识库RAG", result.data_names)

    def test_general_chat_does_not_inject_stock_research_rag(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)

        with patch("sats.chat.build_stock_research_context") as research:
            result = session.ask("今天午饭吃什么")

        research.assert_not_called()
        self.assertNotIn("知识库RAG", result.data_names)

    def test_explicit_knowledge_keeps_explicit_rag_scope_and_default_limit(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(
            settings=settings,
            skills=[],
            llm_factory=FakeLLM,
            memory_enabled=False,
            knowledge="chan",
        )
        question = StockQuestion(symbols=["000938.SZ"], has_stock_question=True)
        fake_research = SimpleNamespace(
            system_message="显式知识库RAG证据",
            sources=({"type": "knowledge", "collection": "chan"},),
        )

        with (
            patch(
                "sats.chat.build_stock_llm_context",
                return_value=SimpleNamespace(
                    system_message='真实股票结构化数据 {"ts_code":"000938.SZ"}',
                    question=question,
                    trade_date="20260520",
                ),
            ),
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[]}'),
            ),
            patch("sats.chat.build_stock_research_context", return_value=fake_research) as research,
        ):
            result = session.ask("分析000938")

        research.assert_called_once()
        self.assertEqual(research.call_args.kwargs["knowledge"], "chan")
        self.assertEqual(research.call_args.kwargs["limit"], 6)
        self.assertIn("知识库RAG", result.data_names)

    def test_chat_session_injects_chan_skill_and_rag_for_chan_question(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=load_skills(Path("skills")), llm_factory=FakeLLM)

        result = session.ask("解释三买和背驰")

        self.assertIn("chan-theory", result.skill_names)
        payload = "\n".join(message["content"] for message in FakeLLM.instances[0].messages)
        self.assertIn("chan-theory", payload)
        self.assertIn("rag_evidence", payload)
        self.assertIn("chan_third_buy", payload)

    def test_chat_session_inherits_last_stock_question_for_followup(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=load_skills(Path("skills")), llm_factory=FakeLLM)
        stock_calls = []

        def fake_stock_context(message, *, question, **kwargs):
            stock_calls.append((message, question))
            return SimpleNamespace(
                system_message=f'真实股票结构化数据 {{"symbols":{question.symbols!r},"minute_curves":{{"15m":[],"30m":[]}}}}',
                question=question,
                trade_date=question.trade_date or "20260515",
            )

        with (
            patch("sats.chat.build_stock_llm_context", side_effect=fake_stock_context),
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[{"ts_code":"000001.SH"}]}'),
            ) as market_builder,
        ):
            session.ask("分析002436 2026-05-15")
            result = session.ask("继续分析它的三买结构")

        self.assertIn("chan-theory", result.skill_names)
        self.assertEqual(stock_calls[1][0], "继续分析它的三买结构")
        self.assertEqual(stock_calls[1][1].symbols, ["002436.SZ"])
        self.assertEqual(stock_calls[1][1].trade_date, "20260515")
        self.assertEqual(market_builder.call_count, 2)

    def test_chat_session_followup_can_override_trade_date(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM)
        stock_calls = []

        def fake_stock_context(message, *, question, **kwargs):
            stock_calls.append(question)
            return SimpleNamespace(
                system_message='真实股票结构化数据 {"minute_curves":{"15m":[],"30m":[]}}',
                question=question,
                trade_date=question.trade_date or "20260515",
            )

        with (
            patch("sats.chat.build_stock_llm_context", side_effect=fake_stock_context),
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[{"ts_code":"000001.SH"}]}'),
            ),
        ):
            session.ask("分析002436,600519 2026-05-15")
            session.ask("继续分析它们 2026-05-18")

        self.assertEqual(stock_calls[1].symbols, ["002436.SZ", "600519.SH"])
        self.assertEqual(stock_calls[1].trade_date, "20260518")

    def test_chat_session_followup_without_last_stock_asks_for_symbol(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=load_skills(Path("skills")), llm_factory=FakeLLM)

        result = session.ask("继续分析它的三买结构")

        self.assertIn("请先提供明确股票代码", result.content)
        self.assertEqual(FakeLLM.instances, [])

    def test_chat_session_uses_reference_context_symbols_for_stock_analysis(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
        reference = ChatReferenceContext(
            system_message="SATS 上一条 /results 筛选结果上下文:\nstructured_screening_results_json: []",
            symbols=["000001.SZ", "600519.SH"],
            trade_date="20260522",
            source="/results",
            data_name="筛选结果",
        )
        stock_calls = []

        def fake_stock_context(message, *, question, **kwargs):
            stock_calls.append(question)
            return SimpleNamespace(
                system_message='真实股票结构化数据 {"stocks":[]}',
                question=question,
                trade_date=question.trade_date,
            )

        with (
            patch("sats.chat.build_stock_llm_context", side_effect=fake_stock_context),
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[]}'),
            ) as market_builder,
        ):
            result = session.ask("分析上面列表，选出最高评分的5支", reference_context=reference)

        self.assertIn("筛选结果", result.data_names)
        self.assertIn("个股", result.data_names)
        self.assertIn("大盘", result.data_names)
        self.assertEqual(stock_calls[0].symbols, ["000001.SZ", "600519.SH"])
        self.assertEqual(stock_calls[0].trade_date, "20260522")
        market_builder.assert_called_once()
        payload = "\n".join(message["content"] for message in FakeLLM.instances[0].messages)
        self.assertIn("上一条 /results 筛选结果上下文", payload)
        self.assertIn("真实股票结构化数据", payload)

    def test_chat_session_current_symbols_override_reference_context_symbols(self) -> None:
        FakeLLM.instances = []
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FakeLLM, memory_enabled=False)
        reference = ChatReferenceContext(
            system_message="SATS 上一条输出上下文",
            symbols=["600519.SH"],
            trade_date="20260522",
        )
        stock_calls = []

        def fake_stock_context(message, *, question, **kwargs):
            stock_calls.append(question)
            return SimpleNamespace(
                system_message='真实股票结构化数据 {"stocks":[]}',
                question=question,
                trade_date=question.trade_date or "20260522",
            )

        with (
            patch("sats.chat.build_stock_llm_context", side_effect=fake_stock_context),
            patch(
                "sats.chat.build_market_llm_context",
                return_value=SimpleNamespace(system_message='真实 A 股大盘结构化数据 {"indices":[]}'),
            ),
        ):
            session.ask("分析000001，上面列表仅参考", reference_context=reference)

        self.assertEqual(stock_calls[0].symbols, ["000001.SZ"])

    def test_chat_session_can_load_skill_with_readonly_tool(self) -> None:
        class ToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="load_skill",
                                arguments={"name": "valuation-model"},
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已加载估值 skill")

        skills = [
            Skill(
                "valuation-model",
                "valuation-model",
                "估值分析框架",
                ("估值",),
                "完整估值指引。",
                Path("SKILL.md"),
                category="analysis",
                source="test",
            )
        ]
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=skills, llm_factory=ToolLLM)

        result = session.ask("帮我做估值分析")

        self.assertEqual(result.content, "已加载估值 skill")
        self.assertEqual(result.tool_call_count, 1)
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertIn("完整估值指引", tool_messages[0]["content"])
        assistant_tool_messages = [
            item for item in session._llm.messages if item.get("role") == "assistant" and item.get("tool_calls")
        ]
        self.assertTrue(assistant_tool_messages)
        self.assertNotIn("reasoning_content", assistant_tool_messages[0])
        self.assertNotIn("additional_kwargs", assistant_tool_messages[0])

    def test_chat_session_tool_followup_falls_back_to_default_model(self) -> None:
        class ToolFallbackLLM:
            instances: list["ToolFallbackLLM"] = []

            def __init__(self, *args, **kwargs) -> None:
                self.kwargs = kwargs
                self.calls = 0
                self.messages = []
                self.tools = []
                ToolFallbackLLM.instances.append(self)

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.kwargs.get("profile") == "light" and self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="load_skill",
                                arguments={"name": "valuation-model"},
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                if self.kwargs.get("profile") == "light":
                    raise TimeoutError("light followup timeout")
                return LLMResponse(content="默认模型已总结")

        skills = [
            Skill(
                "valuation-model",
                "valuation-model",
                "估值分析框架",
                ("估值",),
                "完整估值指引。",
                Path("SKILL.md"),
                category="analysis",
                source="test",
            )
        ]
        settings = SimpleNamespace(project_root=Path("."), openai_model="main-model", light_model_name="light-model", llm_timeout_seconds=180)
        session = ChatSession(settings=settings, skills=skills, llm_factory=ToolFallbackLLM, memory_enabled=False)

        result = session.ask("帮我做估值分析")

        self.assertEqual(result.content, "默认模型已总结")
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual([item.kwargs["profile"] for item in ToolFallbackLLM.instances], ["light", "default"])
        self.assertEqual([item.kwargs["timeout_seconds"] for item in ToolFallbackLLM.instances], [180, 180])

    def test_chat_session_can_fetch_market_context_with_readonly_tool(self) -> None:
        class MarketToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.kwargs = kwargs
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="get_a_share_market_context",
                                arguments={
                                    "trade_date": "20260521",
                                    "horizons": ["tomorrow", "day_after_tomorrow"],
                                    "dimensions": ["core_indices", "limit_sentiment"],
                                },
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已获取大盘上下文")

        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro", light_model_name="light-model")
        session = ChatSession(settings=settings, skills=[], llm_factory=MarketToolLLM)

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch(
                "sats.chat.get_a_share_market_context",
                return_value={"trade_date": "20260521", "indices": [{"ts_code": "000001.SH"}]},
            ) as market_context,
        ):
            result = session.ask("需要时获取大盘数据")

        self.assertEqual(result.content, "已获取大盘上下文")
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(session._llm.kwargs["model_name"], "light-model")
        self.assertEqual(session._llm.kwargs["profile"], "light")
        self.assertEqual(session._llm.calls, 2)
        tool_names = [item["function"]["name"] for item in session._llm.tools]
        self.assertIn("get_a_share_market_context", tool_names)
        market_context.assert_called_once()
        self.assertEqual(market_context.call_args.kwargs["horizons"], ["tomorrow", "day_after_tomorrow"])
        self.assertEqual(market_context.call_args.kwargs["dimensions"], ["core_indices", "limit_sentiment"])
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertIn("000001.SH", tool_messages[0]["content"])

    def test_chat_session_can_discover_opportunities_with_readonly_tool(self) -> None:
        class DiscoveryToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="discover_a_share_opportunities",
                                arguments={
                                    "query": "MLCC相关股票",
                                    "trade_date": "20260521",
                                    "limit": 3,
                                    "hot_sector": False,
                                    "hot_sector_days": 3,
                                },
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已获取短线机会")

        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=DiscoveryToolLLM, memory_enabled=False)
        fake_result = SimpleNamespace(
            to_dict=lambda: {
                "trade_date": "20260521",
                "theme_universe": {"theme": "MLCC", "source": "llm_theme_universe"},
                "candidates": [{"ts_code": "000938.SZ", "name": "紫光股份"}],
            }
        )

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch("sats.chat.run_stock_picking_agent", return_value=fake_result) as discover,
        ):
            result = session.ask("需要时调用机会发现工具")

        self.assertEqual(result.content, "已获取短线机会")
        self.assertEqual(result.tool_call_count, 1)
        tool_names = [item["function"]["name"] for item in session._llm.tools]
        self.assertIn("discover_a_share_opportunities", tool_names)
        discover.assert_called_once()
        self.assertEqual(discover.call_args.kwargs["query"], "MLCC相关股票")
        self.assertEqual(discover.call_args.kwargs["limit"], 3)
        self.assertEqual(discover.call_args.kwargs["candidate_limit"], 50)
        self.assertFalse(discover.call_args.kwargs["hot_sector_enabled"])
        self.assertEqual(discover.call_args.kwargs["hot_sector_days"], 3)
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertIn("000938.SZ", tool_messages[0]["content"])
        self.assertIn("theme_universe", tool_messages[0]["content"])

    def test_chat_discovery_tool_extracts_limit_from_query_when_argument_missing(self) -> None:
        class DiscoveryToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="discover_a_share_opportunities",
                                arguments={"query": "列出10支明天大概率上涨的股票"},
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已获取短线机会")

        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=DiscoveryToolLLM, memory_enabled=False)
        fake_result = SimpleNamespace(to_dict=lambda: {"candidates": []})

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch("sats.chat.run_stock_picking_agent", return_value=fake_result) as discover,
        ):
            session.ask("需要时调用机会发现工具")

        discover.assert_called_once()
        self.assertEqual(discover.call_args.kwargs["limit"], 10)
        self.assertEqual(discover.call_args.kwargs["candidate_limit"], 50)

    def test_chat_session_can_fetch_stock_context_with_readonly_tool(self) -> None:
        class StockToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="get_stock_research_context",
                                arguments={"symbols": ["000938"], "trade_date": "20260521"},
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已获取个股上下文")

        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=StockToolLLM, memory_enabled=False)
        fake_context = SimpleNamespace(payload={"trade_date": "20260521", "stocks": [{"ts_code": "000938.SZ"}]})

        with (
            patch("sats.chat.build_stock_llm_context", return_value=fake_context) as stock_context,
            patch("sats.chat.build_market_llm_context", return_value=None),
        ):
            result = session.ask("需要时获取个股研究上下文")

        self.assertEqual(result.content, "已获取个股上下文")
        tool_names = [item["function"]["name"] for item in session._llm.tools]
        self.assertIn("get_stock_research_context", tool_names)
        stock_context.assert_called_once()
        question = stock_context.call_args.kwargs["question"]
        self.assertEqual(question.symbols, ["000938.SZ"])
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertIn("000938.SZ", tool_messages[0]["content"])

    def test_chat_tool_registry_lists_tushare_stock_datasets(self) -> None:
        class DatasetProvider:
            def list_tushare_stock_datasets(self, *, category=None, include_deprecated=True):
                return [{"dataset": "income", "category": category, "status": "active"}]

        registry = _ChatSkillToolRegistry([], SimpleNamespace())

        with patch("sats.chat.AStockDataProvider", return_value=DatasetProvider()):
            payload = json.loads(
                registry.execute(
                    "list_tushare_stock_datasets",
                    {"category": "财务数据", "include_deprecated": False},
                )
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["datasets"][0]["dataset"], "income")
        tool_names = [item["function"]["name"] for item in registry.definitions()]
        self.assertIn("list_tushare_stock_datasets", tool_names)
        self.assertIn("get_tushare_stock_data", tool_names)

    def test_chat_tool_registry_lists_tushare_general_datasets(self) -> None:
        class DatasetProvider:
            def list_tushare_datasets(self, *, domain=None, category=None, include_deprecated=True, tags=None):
                return [{"dataset": "index_daily", "domain": domain, "category": category, "tags": tags or []}]

        registry = _ChatSkillToolRegistry([], SimpleNamespace())

        with patch("sats.chat.AStockDataProvider", return_value=DatasetProvider()):
            payload = json.loads(
                registry.execute(
                    "list_tushare_datasets",
                    {"domain": "指数专题", "category": "指数专题", "tags": ["index"], "include_deprecated": False},
                )
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["datasets"][0]["dataset"], "index_daily")
        self.assertEqual(payload["datasets"][0]["domain"], "指数专题")
        tool_names = [item["function"]["name"] for item in registry.definitions()]
        self.assertIn("list_tushare_datasets", tool_names)
        self.assertIn("get_tushare_data", tool_names)

    def test_chat_tool_registry_lists_provider_capabilities(self) -> None:
        registry = _ChatSkillToolRegistry([], SimpleNamespace())

        payload = json.loads(
            registry.execute(
                "list_provider_capabilities",
                {"provider": "tickflow", "realtime": True, "compact": True},
            )
        )

        self.assertEqual(payload["status"], "ok")
        capability_ids = {item["capability_id"] for item in payload["capabilities"]}
        self.assertIn("tickflow.realtime_quotes", capability_ids)
        self.assertIn("tickflow.realtime_minute_klines", capability_ids)
        tool_names = [item["function"]["name"] for item in registry.definitions()]
        self.assertIn("list_provider_capabilities", tool_names)

    def test_chat_tool_registry_lists_akshare_datasets(self) -> None:
        class DatasetProvider:
            def list_akshare_datasets(self, **kwargs):
                return [{"dataset": "stock_zh_a_spot_em", "query": kwargs.get("query")}]

        registry = _ChatSkillToolRegistry([], SimpleNamespace())

        with patch("sats.chat.AStockDataProvider", return_value=DatasetProvider()):
            payload = json.loads(
                registry.execute(
                    "list_akshare_datasets",
                    {"query": "spot", "compact": True},
                )
            )

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["datasets"][0]["dataset"], "stock_zh_a_spot_em")
        tool_names = [item["function"]["name"] for item in registry.definitions()]
        self.assertIn("list_akshare_datasets", tool_names)
        self.assertIn("describe_akshare_dataset", tool_names)
        self.assertIn("get_akshare_data", tool_names)

    def test_chat_session_can_fetch_akshare_data_with_readonly_tool(self) -> None:
        class AkShareDataToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="get_akshare_data",
                                arguments={
                                    "dataset": "stock_zh_a_spot_em",
                                    "params": {},
                                    "fields": ["代码", "最新价"],
                                    "limit": 1,
                                },
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已获取 AkShare 数据")

        class DatasetProvider:
            def fetch_akshare_dataset(self, dataset, params=None, *, fields=None, limit=200):
                return {
                    "dataset": dataset,
                    "params": params or {},
                    "columns": fields or [],
                    "rows": [{"代码": "000001", "最新价": 10.5}],
                    "row_count": 1,
                    "returned_row_count": 1,
                    "data_source": f"akshare_{dataset}",
                    "missing_fields": [],
                    "market_data_provenance": [{"dataset": dataset, "source": "akshare"}],
                }

        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=AkShareDataToolLLM, memory_enabled=False)

        with (
            patch("sats.chat.AStockDataProvider", return_value=DatasetProvider()),
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
        ):
            result = session.ask("需要时获取 AkShare stock_zh_a_spot_em")

        self.assertEqual(result.content, "已获取 AkShare 数据")
        self.assertEqual(result.tool_call_count, 1)
        tool_names = [item["function"]["name"] for item in session._llm.tools]
        self.assertIn("get_akshare_data", tool_names)
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertIn("akshare_stock_zh_a_spot_em", tool_messages[0]["content"])

    def test_chat_session_can_fetch_tushare_stock_data_with_readonly_tool(self) -> None:
        class TushareDataToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="get_tushare_stock_data",
                                arguments={
                                    "dataset": "daily_basic",
                                    "params": {"ts_code": "000001", "trade_date": "20260521"},
                                    "fields": ["ts_code", "pe", "pb"],
                                    "limit": 1,
                                },
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已获取 Tushare 数据")

        class DatasetProvider:
            def fetch_tushare_stock_dataset(self, dataset, params=None, *, fields=None, limit=200):
                return {
                    "dataset": dataset,
                    "params": params or {},
                    "columns": fields or [],
                    "rows": [{"ts_code": "000001.SZ", "pe": 12.0, "pb": 1.1}],
                    "row_count": 1,
                    "returned_row_count": 1,
                    "data_source": "tushare_daily_basic",
                    "missing_fields": [],
                }

        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=TushareDataToolLLM, memory_enabled=False)

        with (
            patch("sats.chat.AStockDataProvider", return_value=DatasetProvider()),
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
        ):
            result = session.ask("需要时获取 Tushare daily_basic")

        self.assertEqual(result.content, "已获取 Tushare 数据")
        self.assertEqual(result.tool_call_count, 1)
        tool_names = [item["function"]["name"] for item in session._llm.tools]
        self.assertIn("get_tushare_stock_data", tool_names)
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertIn("tushare_daily_basic", tool_messages[0]["content"])
        self.assertIn("000001.SZ", tool_messages[0]["content"])

    def test_chat_session_can_fetch_tushare_general_data_with_readonly_tool(self) -> None:
        class TushareDataToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="get_tushare_data",
                                arguments={
                                    "dataset": "index_daily",
                                    "params": {"ts_code": "000001.SH", "trade_date": "20260521"},
                                    "fields": ["ts_code", "close"],
                                    "limit": 1,
                                },
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已获取指数数据")

        class DatasetProvider:
            def fetch_tushare_dataset(self, dataset, params=None, *, fields=None, limit=200):
                return {
                    "dataset": dataset,
                    "domain": "指数专题",
                    "params": params or {},
                    "columns": fields or [],
                    "rows": [{"ts_code": "000001.SH", "close": 3100.0}],
                    "row_count": 1,
                    "returned_row_count": 1,
                    "data_source": "tushare_index_daily",
                    "missing_fields": [],
                }

        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=TushareDataToolLLM, memory_enabled=False)

        with (
            patch("sats.chat.AStockDataProvider", return_value=DatasetProvider()),
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
        ):
            result = session.ask("需要时获取 Tushare index_daily")

        self.assertEqual(result.content, "已获取指数数据")
        self.assertEqual(result.tool_call_count, 1)
        tool_names = [item["function"]["name"] for item in session._llm.tools]
        self.assertIn("get_tushare_data", tool_names)
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertIn("tushare_index_daily", tool_messages[0]["content"])
        self.assertIn("000001.SH", tool_messages[0]["content"])

    def test_internal_analysis_tool_rejects_unknown_kind(self) -> None:
        class InternalToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="run_internal_analysis",
                                arguments={"kind": "shell", "symbols": ["000938"]},
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="内部分析返回")

        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=InternalToolLLM, memory_enabled=False)

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
        ):
            result = session.ask("需要时调用内部分析")

        self.assertEqual(result.content, "内部分析返回")
        tool_names = [item["function"]["name"] for item in session._llm.tools]
        self.assertIn("run_internal_analysis", tool_names)
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertIn("unsupported internal analysis kind", tool_messages[0]["content"])

    def test_internal_analysis_tool_can_return_factor_summary(self) -> None:
        class FactorToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []
                self.tools = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                self.tools = tools or []
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="run_internal_analysis",
                                arguments={"kind": "factor_summary", "symbols": ["000938"], "trade_date": "20260521"},
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="因子摘要返回")

        settings = SimpleNamespace(project_root=Path("."), db_path=Path("sats.duckdb"), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=[], llm_factory=FactorToolLLM, memory_enabled=False)
        fake_payload = {
            "kind": "factor_summary",
            "profile": "balanced",
            "exposures": [{"ts_code": "000938.SZ", "score": 1.2}],
        }

        with (
            patch("sats.chat.build_stock_llm_context", return_value=None),
            patch("sats.chat.build_market_llm_context", return_value=None),
            patch("sats.chat.AStockDataProvider"),
            patch("sats.chat.snapshot_from_screening_inputs") as snapshot,
            patch("sats.chat.summarize_factor_exposure", return_value=fake_payload),
            patch("sats.chat.ensure_optional_dependencies", create=True) as ensure,
        ):
            snapshot.return_value = (SimpleNamespace(), None)
            result = session.ask("需要时调用因子摘要")

        self.assertEqual(result.content, "因子摘要返回")
        ensure.assert_not_called()
        tool_messages = [item for item in session._llm.messages if item.get("role") == "tool"]
        self.assertIn("factor_summary", tool_messages[0]["content"])
        self.assertIn("000938.SZ", tool_messages[0]["content"])

    def test_chat_session_preserves_reasoning_content_for_tool_followup(self) -> None:
        class ReasoningToolLLM:
            def __init__(self, *args, **kwargs) -> None:
                self.calls = 0
                self.messages = []

            def chat(self, messages, tools=None):
                self.calls += 1
                self.messages = messages
                if self.calls == 1:
                    return LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCallRequest(
                                id="call-1",
                                name="load_skill",
                                arguments={"name": "valuation-model"},
                            )
                        ],
                        reasoning_content="thinking",
                        finish_reason="tool_calls",
                    )
                return LLMResponse(content="已加载估值 skill")

        skills = [
            Skill(
                "valuation-model",
                "valuation-model",
                "估值分析框架",
                ("估值",),
                "完整估值指引。",
                Path("SKILL.md"),
                category="analysis",
                source="test",
            )
        ]
        settings = SimpleNamespace(project_root=Path("."), openai_model="deepseek-v4-pro")
        session = ChatSession(settings=settings, skills=skills, llm_factory=ReasoningToolLLM)

        result = session.ask("帮我做估值分析")

        self.assertEqual(result.content, "已加载估值 skill")
        assistant_tool_messages = [
            item for item in session._llm.messages if item.get("role") == "assistant" and item.get("tool_calls")
        ]
        self.assertTrue(assistant_tool_messages)
        self.assertEqual(assistant_tool_messages[0]["reasoning_content"], "thinking")
        self.assertEqual(assistant_tool_messages[0]["additional_kwargs"], {"reasoning_content": "thinking"})


if __name__ == "__main__":
    unittest.main()
