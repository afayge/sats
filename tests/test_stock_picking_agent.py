from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.analysis.stock_picking_agent import (
    build_stock_picking_plan,
    format_stock_picking_agent_result,
    resolve_theme_universe,
    run_stock_picking_agent,
)
from sats.cli import main
from sats.skills import Skill
from sats.storage.duckdb import DuckDBStorage
from tests.test_opportunity_discovery import _FakeHotSectorDiscoveryProvider, _bullish_daily, _flat_daily


def _skill(skill_id: str, description: str = "") -> Skill:
    return Skill(
        id=skill_id,
        name=skill_id,
        description=description or skill_id,
        triggers=(skill_id,),
        content=f"{skill_id} content",
        path=Path("SKILL.md"),
    )


def _theme_stock_basic_frame() -> pd.DataFrame:
    rows = [
        ("300408.SZ", "300408", "三环集团", "电子元件"),
        ("000636.SZ", "000636", "风华高科", "电子元件"),
        ("002138.SZ", "002138", "顺络电子", "电子元件"),
        ("300319.SZ", "300319", "麦捷科技", "电子元件"),
        ("002859.SZ", "002859", "洁美科技", "电子材料"),
        ("300285.SZ", "300285", "国瓷材料", "电子陶瓷"),
        ("603688.SH", "603688", "石英股份", "材料"),
        ("002436.SZ", "002436", "兴森科技", "封装基板"),
        ("300476.SZ", "300476", "胜宏科技", "电子元件"),
        ("688183.SH", "688183", "生益电子", "电子元件"),
        ("600183.SH", "600183", "生益科技", "电子材料"),
        ("000001.SZ", "000001", "平安银行", "银行"),
        ("000002.SZ", "000002", "万科A", "房地产"),
        ("000063.SZ", "000063", "中兴通讯", "通信设备"),
        ("000100.SZ", "000100", "TCL科技", "电子元件"),
        ("000333.SZ", "000333", "美的集团", "家电"),
        ("000725.SZ", "000725", "京东方A", "面板"),
        ("002008.SZ", "002008", "大族激光", "设备"),
        ("002049.SZ", "002049", "紫光国微", "半导体"),
        ("002241.SZ", "002241", "歌尔股份", "消费电子"),
        ("002415.SZ", "002415", "海康威视", "计算机设备"),
        ("002475.SZ", "002475", "立讯精密", "消费电子"),
        ("300059.SZ", "300059", "东方财富", "证券"),
        ("600519.SH", "600519", "贵州茅台", "白酒"),
    ]
    return pd.DataFrame(
        [{"ts_code": ts_code, "symbol": symbol, "name": name, "industry": industry} for ts_code, symbol, name, industry in rows]
    )


def _mlcc_theme_stocks(count: int = 11) -> list[dict]:
    rows = [
        ("300408.SZ", "三环集团", "MLCC 电子陶瓷材料和元件平台", "materials", 0.92),
        ("000636.SZ", "风华高科", "MLCC 被动元件业务", "manufacturer", 0.9),
        ("002138.SZ", "顺络电子", "片式被动元件平台", "component_platform", 0.82),
        ("300319.SZ", "麦捷科技", "片式电感和被动元件平台", "component_platform", 0.78),
        ("002859.SZ", "洁美科技", "MLCC 离型膜/纸等材料", "materials", 0.74),
        ("300285.SZ", "国瓷材料", "MLCC 陶瓷粉体材料", "materials", 0.7),
        ("603688.SH", "石英股份", "电子材料配套", "materials", 0.4),
        ("002436.SZ", "兴森科技", "电子元件封装配套", "component_platform", 0.63),
        ("300476.SZ", "胜宏科技", "电子元件平台配套", "component_platform", 0.61),
        ("688183.SH", "生益电子", "电子元件平台配套", "component_platform", 0.58),
        ("600183.SH", "生益科技", "电子材料平台", "materials", 0.55),
    ]
    return [
        {"ts_code": ts_code, "name": name, "reason": reason, "relation_type": relation_type, "confidence": confidence}
        for ts_code, name, reason, relation_type, confidence in rows[:count]
    ]


def _many_theme_stocks() -> list[dict]:
    extras = [
        ("000001.SZ", "平安银行", "LLM 明确列出的测试股票", "other", 0.51),
        ("000002.SZ", "万科A", "LLM 明确列出的测试股票", "other", 0.52),
        ("000063.SZ", "中兴通讯", "LLM 明确列出的测试股票", "other", 0.53),
        ("000100.SZ", "TCL科技", "LLM 明确列出的测试股票", "other", 0.54),
        ("000333.SZ", "美的集团", "LLM 明确列出的测试股票", "other", 0.55),
        ("000725.SZ", "京东方A", "LLM 明确列出的测试股票", "other", 0.56),
        ("002008.SZ", "大族激光", "LLM 明确列出的测试股票", "other", 0.57),
        ("002049.SZ", "紫光国微", "LLM 明确列出的测试股票", "other", 0.58),
        ("002241.SZ", "歌尔股份", "LLM 明确列出的测试股票", "other", 0.59),
        ("002415.SZ", "海康威视", "LLM 明确列出的测试股票", "other", 0.6),
        ("002475.SZ", "立讯精密", "LLM 明确列出的测试股票", "other", 0.61),
        ("300059.SZ", "东方财富", "LLM 明确列出的测试股票", "other", 0.62),
    ]
    return [
        *_mlcc_theme_stocks(),
        *[
            {"ts_code": ts_code, "name": name, "reason": reason, "relation_type": relation_type, "confidence": confidence}
            for ts_code, name, reason, relation_type, confidence in extras
        ],
    ]


class _ThemeDiscoveryProvider(_FakeHotSectorDiscoveryProvider):
    def __init__(
        self,
        *,
        sector_basic: pd.DataFrame | None = None,
        sector_members: pd.DataFrame | None = None,
        bullish_symbols: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.stock_basic_frame = _theme_stock_basic_frame()
        self.sector_basic_frame = sector_basic if sector_basic is not None else pd.DataFrame()
        self.sector_members_frame = sector_members if sector_members is not None else pd.DataFrame()
        self.bullish_symbols = set(bullish_symbols or self.stock_basic_frame["ts_code"].astype(str).tolist())
        self.ths_basic_calls = 0
        self.ths_member_calls: list[list[str]] = []

    def load_stock_basic(self, *, storage=None):
        return self.stock_basic_frame

    def load_ths_sector_basic(self, *, storage=None):
        self.ths_basic_calls += 1
        return self.sector_basic_frame

    def load_ths_sector_members(self, sector_codes, *, storage=None):
        self.ths_member_calls.append(list(sector_codes))
        if self.sector_members_frame.empty:
            return self.sector_members_frame
        return self.sector_members_frame[self.sector_members_frame["sector_code"].isin(sector_codes)].copy()

    def load_screening_inputs(self, symbols, trade_date, *, storage=None, trade_days=80, rule_name=None):
        rows = super().load_screening_inputs(symbols, trade_date, storage=storage, trade_days=trade_days, rule_name=rule_name)
        for row in rows:
            if row.ts_code not in self.bullish_symbols:
                row.daily = _flat_daily(row.ts_code, end=trade_date)
            local = self.stock_basic_frame[self.stock_basic_frame["ts_code"].astype(str) == row.ts_code]
            if not local.empty:
                row.stock_basic = local.iloc[0].dropna().to_dict()
        return rows

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        inputs = super().load_indicator_inputs(symbols, trade_date, lookback_days=lookback_days, storage=storage)
        for item in inputs:
            if item.ts_code not in self.bullish_symbols:
                item.daily = _flat_daily(item.ts_code, end=trade_date)
            local = self.stock_basic_frame[self.stock_basic_frame["ts_code"].astype(str) == item.ts_code]
            if not local.empty:
                item.stock_basic = local.iloc[0].dropna().to_dict()
        return inputs


class _ThemeUniverseLLM:
    def __init__(self, stocks, *, uncertainties=None) -> None:
        self.stocks = stocks
        self.uncertainties = uncertainties or []
        self.calls: list[list[dict]] = []

    def chat(self, messages):
        self.calls.append(messages)
        prompt = messages[0]["content"]
        if "主题股票池解析器" in prompt:
            return SimpleNamespace(
                content=json.dumps(
                    {"theme": "MLCC", "stocks": self.stocks, "uncertainties": self.uncertainties},
                    ensure_ascii=False,
                )
            )
        return SimpleNamespace(
            content=json.dumps(
                {
                    "rankings": [
                        {"ts_code": "300408.SZ", "reason": "Analyze 信号更匹配"},
                        {"ts_code": "000636.SZ", "reason": "Analyze 信号次之"},
                        {"ts_code": "002138.SZ", "reason": "Analyze 信号第三"},
                        {"ts_code": "300319.SZ", "reason": "Analyze 信号第四"},
                    ]
                },
                ensure_ascii=False,
            )
        )


class StockPickingAgentTest(unittest.TestCase):
    def test_build_stock_picking_plan_maps_natural_language_to_profiles(self) -> None:
        skills = [
            _skill("sats-market-assistant"),
            _skill("technical-basic"),
            _skill("chan-theory"),
            _skill("fundamental-filter"),
            _skill("financial-statement"),
            _skill("risk-analysis"),
            _skill("hot-theme"),
            _skill("sector-rotation"),
        ]

        hot = build_stock_picking_plan("热点板块共振，未来几天可能上涨", skills=skills)
        self.assertEqual(hot.profile, "hot_sector_momentum")
        self.assertIn("hot-theme", hot.skills)
        self.assertIn("sentiment", hot.collections)
        self.assertIn("热点板块共振", hot.constraints)

        chan = build_stock_picking_plan("按缠论三买找短线候选", skills=skills)
        self.assertEqual(chan.profile, "chan_structure")
        self.assertIn("chan-theory", chan.skills)
        self.assertIn("chan", chan.collections)

        fundamental = build_stock_picking_plan("低估值基本面稳健的股票", skills=skills)
        self.assertEqual(fundamental.profile, "fundamental_quality")
        self.assertIn("fundamental", fundamental.collections)

        theme = build_stock_picking_plan("MLCC相关股票，未来几天可能上涨", skills=skills)
        self.assertEqual(theme.theme, "MLCC")

    def test_run_agent_uses_discovery_candidates_rag_and_agent_llm_ranking(self) -> None:
        calls = []
        factory_calls = []

        class RankingLLM:
            def chat(self, messages):
                calls.append(messages)
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "rankings": [
                                {
                                    "ts_code": "600519.SH",
                                    "reason": "技术与约束更匹配",
                                    "entry_trigger": "放量突破",
                                    "invalidation": "跌破 MA10",
                                    "risk": "追高风险",
                                    "evidence_refs": ["signals"],
                                    "data_limits": "无新闻数据",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                )

        def llm_factory(**kwargs):
            factory_calls.append(kwargs)
            return RankingLLM()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(
                project_root=root,
                db_path=root / "sats.duckdb",
                openai_model="deepseek-v4-pro",
                light_model_name="mimo-light",
            )
            fake_rag = SimpleNamespace(
                system_message="SATS RAG evidence: signals",
                sources=({"collection": "signals", "source": "skills/quant-factor-screener/SKILL.md"},),
            )
            with (
                patch(
                    "sats.analysis.opportunity_discovery.get_a_share_market_context",
                    return_value={"trade_date": "20260520", "missing_fields": []},
                ),
                patch("sats.analysis.stock_picking_agent.build_stock_research_context", return_value=fake_rag),
            ):
                result = run_stock_picking_agent(
                    query="低估值基本面稳健，优先热点板块",
                    settings=settings,
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=_FakeHotSectorDiscoveryProvider(),
                    skills=[_skill("sats-market-assistant"), _skill("risk-analysis"), _skill("fundamental-filter")],
                    trade_date="20260520",
                    limit=1,
                    candidate_limit=2,
                    reports_dir=root / "reports",
                    llm_factory=llm_factory,
                )

        self.assertEqual(result.plan.profile, "fundamental_quality")
        self.assertEqual(factory_calls[0]["model_name"], "mimo-light")
        self.assertEqual(factory_calls[0]["profile"], "light")
        self.assertEqual(result.candidates[0].ts_code, "600519.SH")
        self.assertIn("证据 signals", result.candidates[0].llm_reason)
        self.assertTrue(result.report_path)
        self.assertEqual(result.evidence_sources[0]["collection"], "signals")
        self.assertIn("SATS RAG evidence", calls[0][0]["content"])
        self.assertIn("600519.SH", format_stock_picking_agent_result(result))

    def test_agent_uses_query_limit_when_limit_is_not_explicit(self) -> None:
        class NoopLLM:
            def chat(self, messages):
                return SimpleNamespace(content=json.dumps({"rankings": []}, ensure_ascii=False))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.stock_picking_agent.build_stock_research_context", return_value=None),
            ):
                result = run_stock_picking_agent(
                    query="列出10支明天大概率上涨的股票",
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb", openai_model="deepseek-v4-pro"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=_FakeHotSectorDiscoveryProvider(),
                    skills=[_skill("sats-market-assistant"), _skill("technical-basic"), _skill("risk-analysis")],
                    trade_date="20260520",
                    limit=None,
                    candidate_limit=2,
                    report=False,
                    llm_factory=lambda: NoopLLM(),
                )

        self.assertEqual(len(result.candidates), 2)
        self.assertEqual(result.discovery.candidate_count, 2)
        formatted = format_stock_picking_agent_result(result)
        self.assertIn("短期信号候选: 2 只 | 展示: 2 只", formatted)

    def test_mlcc_theme_uses_llm_universe_when_ths_has_no_match(self) -> None:
        llm = _ThemeUniverseLLM(
            [
                {"ts_code": "300408.SZ", "name": "三环集团", "reason": "MLCC 电子陶瓷材料", "confidence": 0.92},
                {"ts_code": "", "name": "风华高科", "reason": "MLCC 元件业务", "confidence": 0.9},
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _ThemeDiscoveryProvider()
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.stock_picking_agent.build_stock_research_context", return_value=None),
            ):
                result = run_stock_picking_agent(
                    query="MLCC相关股票，未来几天可能上涨",
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb", openai_model="deepseek-v4-pro"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    skills=[_skill("sats-market-assistant"), _skill("technical-basic"), _skill("risk-analysis")],
                    trade_date="20260520",
                    limit=2,
                    candidate_limit=2,
                    report=False,
                    llm_factory=lambda: llm,
                    hot_sector_enabled=False,
                )

        self.assertEqual(result.theme_universe.source, "llm_theme_universe")
        self.assertEqual(result.theme_universe.symbols, ("300408.SZ", "000636.SZ"))
        self.assertEqual(result.theme_universe.count, 2)
        self.assertEqual(result.theme_universe.stocks[0].reason, "MLCC 电子陶瓷材料")
        self.assertEqual(provider.all_calls, [])
        self.assertEqual(provider.symbol_calls[0]["symbols"], ["300408.SZ", "000636.SZ"])
        self.assertEqual(result.discovery.scanned_count, 2)
        formatted = format_stock_picking_agent_result(result)
        self.assertIn("LLM 主题线索 MLCC，经本地 stock_basic 校验，共 2 只", formatted)
        self.assertIn("短期信号候选: 2 只", formatted)
        self.assertIn("选股Agent: technical_short_up / short_term", formatted)
        self.assertIn("排名", formatted)
        self.assertIn("代码", formatted)
        self.assertIn("主要信号", formatted)
        self.assertIn("详情:", formatted)

    def test_mlcc_theme_universe_keeps_full_pool_when_only_some_pass_short_term_signals(self) -> None:
        llm = _ThemeUniverseLLM(_mlcc_theme_stocks())
        bullish = {"300408.SZ", "000636.SZ", "002138.SZ", "300319.SZ"}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _ThemeDiscoveryProvider(bullish_symbols=bullish)
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.stock_picking_agent.build_stock_research_context", return_value=None),
            ):
                result = run_stock_picking_agent(
                    query="mlcc相关的A股股票，并筛选一些短期大概率上涨的股票",
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb", openai_model="deepseek-v4-pro"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    skills=[_skill("sats-market-assistant"), _skill("technical-basic"), _skill("risk-analysis")],
                    trade_date="20260520",
                    limit=4,
                    candidate_limit=30,
                    report=False,
                    llm_factory=lambda: llm,
                    hot_sector_enabled=False,
                )

        self.assertEqual(result.theme_universe.source, "llm_theme_universe")
        self.assertEqual(result.theme_universe.count, 11)
        self.assertEqual(len(result.theme_universe.stocks), 11)
        self.assertEqual(provider.symbol_calls[0]["symbols"], [item["ts_code"] for item in _mlcc_theme_stocks()])
        self.assertEqual(result.discovery.scanned_count, 11)
        self.assertEqual(result.discovery.candidate_count, 4)
        self.assertEqual(result.discovery.llm_pool_count, 4)
        self.assertEqual(len(result.candidates), 4)
        payload = result.to_dict()
        self.assertEqual(payload["theme_universe"]["count"], 11)
        self.assertEqual(len(payload["theme_universe"]["stocks"]), 11)
        self.assertEqual(len(payload["opportunity_discovery"]["candidates"]), 4)
        formatted = format_stock_picking_agent_result(result)
        self.assertIn("主题股票池: LLM 主题线索 MLCC，经本地 stock_basic 校验，共 11 只", formatted)
        self.assertIn("短期信号候选: 4 只", formatted)
        self.assertIn("LLM分析池: 4 只", formatted)
        self.assertIn("扫描: 11 只", formatted)
        self.assertIn("展示: 4 只", formatted)
        self.assertIn("详情:", formatted)

    def test_llm_theme_universe_keeps_all_returned_stocks_without_symbol_cap(self) -> None:
        llm = _ThemeUniverseLLM(_many_theme_stocks())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            universe = resolve_theme_universe(
                "MLCC相关股票",
                _ThemeDiscoveryProvider(),
                DuckDBStorage(root / "sats.duckdb"),
                lambda: llm,
                settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb", openai_model="deepseek-v4-pro"),
                trade_date="20260520",
                max_symbols=3,
            )

        self.assertEqual(universe.source, "llm_theme_universe")
        self.assertEqual(universe.count, 23)
        self.assertEqual(len(universe.symbols), 23)
        self.assertEqual(universe.symbols[-1], "300059.SZ")
        prompt = llm.calls[0][0]["content"]
        self.assertIn("MLCC 相关 A 股股票有哪些", prompt)
        self.assertNotIn("最多给出", prompt)

    def test_llm_theme_universe_uses_light_profile(self) -> None:
        llm = _ThemeUniverseLLM(_mlcc_theme_stocks(1))
        factory_calls = []

        def llm_factory(**kwargs):
            factory_calls.append(kwargs)
            return llm

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            universe = resolve_theme_universe(
                "MLCC相关股票",
                _ThemeDiscoveryProvider(),
                DuckDBStorage(root / "sats.duckdb"),
                llm_factory,
                settings=SimpleNamespace(
                    project_root=root,
                    db_path=root / "sats.duckdb",
                    openai_model="deepseek-v4-pro",
                    light_model_name="mimo-light",
                ),
                trade_date="20260520",
            )

        self.assertEqual(universe.source, "llm_theme_universe")
        self.assertEqual(factory_calls[0]["model_name"], "mimo-light")
        self.assertEqual(factory_calls[0]["profile"], "light")

    def test_candidate_limit_does_not_change_llm_theme_pool_size(self) -> None:
        llm = _ThemeUniverseLLM(_many_theme_stocks())

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _ThemeDiscoveryProvider()
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.stock_picking_agent.build_stock_research_context", return_value=None),
            ):
                result = run_stock_picking_agent(
                    query="MLCC相关股票，未来几天可能上涨",
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb", openai_model="deepseek-v4-pro"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    skills=[_skill("sats-market-assistant"), _skill("technical-basic"), _skill("risk-analysis")],
                    trade_date="20260520",
                    limit=5,
                    candidate_limit=30,
                    report=False,
                    llm_factory=lambda: llm,
                    hot_sector_enabled=False,
                )

        self.assertEqual(result.theme_universe.count, 23)
        self.assertEqual(provider.symbol_calls[0]["symbols"], [item["ts_code"] for item in _many_theme_stocks()])

    def test_llm_theme_universe_filters_invalid_non_a_share_and_name_conflicts(self) -> None:
        llm = _ThemeUniverseLLM(
            [
                {"ts_code": "300408.SZ", "name": "风华高科", "reason": "代码名称冲突", "confidence": 0.95},
                {"ts_code": "00700.HK", "name": "腾讯控股", "reason": "非 A 股", "confidence": 0.95},
                {"ts_code": "", "name": "不存在股份", "reason": "无法识别", "confidence": 0.95},
                {"ts_code": "000636.SZ", "name": "风华高科", "reason": "MLCC 元件业务", "confidence": 0.9},
                {"ts_code": "600519.SH", "name": "贵州茅台", "reason": "低置信度噪声", "confidence": 0.2},
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            universe = resolve_theme_universe(
                "MLCC相关股票",
                _ThemeDiscoveryProvider(),
                DuckDBStorage(root / "sats.duckdb"),
                lambda: llm,
                settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb", openai_model="deepseek-v4-pro"),
                trade_date="20260520",
            )

        self.assertEqual(universe.source, "llm_theme_universe")
        self.assertEqual(universe.symbols, ("300408.SZ", "000636.SZ", "600519.SH"))
        self.assertEqual(universe.stocks[0].name, "三环集团")
        self.assertEqual(universe.stocks[-1].confidence, 0.2)
        warnings = "\n".join(universe.warnings)
        self.assertIn("code_name_conflict", warnings)
        self.assertIn("unrecognized:00700.HK", warnings)
        self.assertIn("low_confidence", warnings)

    def test_ths_exact_theme_match_does_not_call_llm(self) -> None:
        def fail_llm():
            raise AssertionError("LLM should not be called when THS has an exact theme board")

        sector_basic = pd.DataFrame(
            [{"sector_code": "885999.TI", "name": "MLCC", "sector_type": "concept", "exchange": "THS"}]
        )
        sector_members = pd.DataFrame(
            [
                {"sector_code": "885999.TI", "ts_code": "300408.SZ", "name": "三环集团"},
                {"sector_code": "885999.TI", "ts_code": "000636.SZ", "name": "风华高科"},
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            universe = resolve_theme_universe(
                "MLCC相关股票",
                _ThemeDiscoveryProvider(sector_basic=sector_basic, sector_members=sector_members),
                DuckDBStorage(root / "sats.duckdb"),
                fail_llm,
                settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                trade_date="20260520",
            )

        self.assertEqual(universe.source, "ths_sector")
        self.assertEqual(universe.matched_sector, "MLCC")
        self.assertEqual(universe.symbols, ("300408.SZ", "000636.SZ"))

    def test_no_theme_universe_does_not_fall_back_to_unrelated_broad_sector(self) -> None:
        llm = _ThemeUniverseLLM(
            [
                {"ts_code": "", "name": "电子元件", "reason": "泛化板块，不是具体 A 股", "confidence": 0.8},
                {"ts_code": "ABC", "name": "Unknown", "reason": "非法代码", "confidence": 0.8},
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _ThemeDiscoveryProvider()
            with patch("sats.analysis.stock_picking_agent.build_stock_research_context", return_value=None):
                result = run_stock_picking_agent(
                    query="MLCC相关股票",
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb", openai_model="deepseek-v4-pro"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    skills=[_skill("sats-market-assistant")],
                    trade_date="20260520",
                    report=False,
                    llm_factory=lambda: llm,
                )

        self.assertEqual(result.theme_universe.source, "none")
        self.assertEqual(provider.all_calls, [])
        self.assertEqual(provider.symbol_calls, [])
        self.assertIn("未能确认 MLCC 相关 A 股股票，不关联无关板块", result.message)

    def test_llm_theme_universe_does_not_expand_sector_names_without_specific_stocks(self) -> None:
        llm = _ThemeUniverseLLM(
            [
                {"ts_code": "", "name": "电子元件", "reason": "板块名，不是具体 A 股", "confidence": 0.8},
                {"ts_code": "", "name": "消费电子", "reason": "板块名，不是具体 A 股", "confidence": 0.8},
            ]
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            universe = resolve_theme_universe(
                "MLCC相关股票",
                _ThemeDiscoveryProvider(),
                DuckDBStorage(root / "sats.duckdb"),
                lambda: llm,
                settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb", openai_model="deepseek-v4-pro"),
                trade_date="20260520",
            )

        self.assertEqual(universe.source, "none")
        self.assertEqual(universe.symbols, ())
        warnings = "\n".join(universe.warnings)
        self.assertIn("unrecognized:电子元件", warnings)
        self.assertIn("unrecognized:消费电子", warnings)

    def test_cli_discover_with_query_dispatches_stock_picking_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            fake_result = SimpleNamespace(
                message="",
                llm_unavailable=False,
                to_dict=lambda: {"query": "热点板块", "agent_plan": {"profile": "hot_sector_momentum"}},
            )
            stdout = io.StringIO()
            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.run_stock_picking_agent", return_value=fake_result) as agent,
                redirect_stdout(stdout),
            ):
                exit_code = main(["discover", "--trade-date", "20260520", "--json", "热点板块"])

        self.assertEqual(exit_code, 0)
        agent.assert_called_once()
        self.assertEqual(agent.call_args.kwargs["query"], "热点板块")
        self.assertEqual(json.loads(stdout.getvalue())["agent_plan"]["profile"], "hot_sector_momentum")

    def test_cli_discover_explicit_limit_overrides_query_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            fake_result = SimpleNamespace(message="显式 limit", llm_unavailable=False)
            stdout = io.StringIO()
            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.run_stock_picking_agent", return_value=fake_result) as agent,
                redirect_stdout(stdout),
            ):
                exit_code = main(["discover", "--trade-date", "20260520", "--limit", "3", "列出10支明天大概率上涨的股票"])

        self.assertEqual(exit_code, 0)
        agent.assert_called_once()
        self.assertEqual(agent.call_args.kwargs["limit"], 3)


if __name__ == "__main__":
    unittest.main()
