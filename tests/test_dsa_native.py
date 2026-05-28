from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.analysis.dsa_decision import build_local_dsa_decision
from sats.analysis.dsa_native import run_dsa_analysis
from sats.data.akshare_provider import _is_mini_racer_unraisable
from sats.indicators import IndicatorInput
from sats.storage.duckdb import DuckDBStorage


def _daily_frame(ts_code: str) -> pd.DataFrame:
    rows = []
    for index in range(1, 81):
        close = 10 + index * 0.1
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": f"202603{(index % 28) + 1:02d}" if index < 29 else f"202604{((index - 29) % 30) + 1:02d}" if index < 59 else f"202605{((index - 59) % 18) + 1:02d}",
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "vol": 1000 + index,
                "amount": 2000 + index,
                "pct_chg": 1.0,
            }
        )
    return pd.DataFrame(rows)


class _FakeTickFlowProvider:
    def __init__(self) -> None:
        self.indicator_symbols: list[str] = []
        self.quote_symbols: list[str] = []

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        self.indicator_symbols = list(symbols)
        return [
            IndicatorInput(
                ts_code=symbol,
                trade_date=trade_date,
                daily=_daily_frame(symbol),
                daily_basic=pd.DataFrame(
                    [{"ts_code": symbol, "trade_date": trade_date, "turnover_rate": 3.2, "total_mv": 100000.0}]
                ),
                stock_basic={"ts_code": symbol, "name": f"Tick{symbol[:6]}"},
                data_sources={"daily": "tickflow_daily", "daily_basic": "tickflow_realtime_basic_like"},
            )
            for symbol in symbols
        ]

    def load_realtime_quotes(self, *, symbols):
        self.quote_symbols = list(symbols)
        return pd.DataFrame(
            [
                {
                    "ts_code": symbol,
                    "trade_date": "20260518",
                    "trade_time": "2026-05-18 10:30:00",
                    "open": 12.0,
                    "high": 13.0,
                    "low": 11.5,
                    "close": 12.8,
                    "pre_close": 12.0,
                    "vol": 1200,
                    "amount": 3500,
                    "pct_chg": 6.67,
                    "turnover_rate": 4.5,
                    "data_source": "tickflow_quote",
                }
                for symbol in symbols
            ]
        )


class _FakeTushareProvider:
    def __init__(self) -> None:
        self.symbols: list[str] = []

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        self.symbols = list(symbols)
        return [
            IndicatorInput(
                ts_code=symbol,
                trade_date=trade_date,
                daily=pd.DataFrame(),
                daily_basic=pd.DataFrame(
                    [
                        {
                            "ts_code": symbol,
                            "trade_date": trade_date,
                            "pe": 11.2,
                            "pb": 1.3,
                            "total_mv": 120000.0,
                            "turnover_rate": 3.5,
                        }
                    ]
                ),
                moneyflow=pd.DataFrame(
                    [{"ts_code": symbol, "trade_date": trade_date, "main_net_amount": 1234.0, "data_source": "tushare_moneyflow_dc"}]
                ),
                fundamentals=pd.DataFrame(
                    [
                        {
                            "ts_code": symbol,
                            "end_date": "20260331",
                            "ann_date": "20260430",
                            "revenue": 10000.0,
                            "profit": 1200.0,
                            "roe": 9.8,
                            "debt_to_assets": 42.0,
                            "data_source": "tushare_fundamentals",
                        }
                    ]
                ),
                stock_basic={"ts_code": symbol, "name": f"Tu{symbol[:6]}"},
                data_sources={
                    "daily_basic": "tushare_daily_basic",
                    "moneyflow": "tushare_moneyflow_dc",
                    "fundamentals": "tushare_fundamentals",
                },
            )
            for symbol in symbols
        ]

    def load_hot_sector_context(
        self,
        trade_date,
        *,
        storage=None,
        lookback_days=5,
        top_industries=10,
        top_concepts=20,
    ):
        return {
            "trade_date": trade_date,
            "lookback_days": lookback_days,
            "hot_industries": [],
            "hot_concepts": [
                {
                    "sector_code": "885001.TI",
                    "name": "AI算力",
                    "sector_type": "concept",
                    "heat_score": 15.0,
                    "return_5d": 8.0,
                    "return_3d": 5.0,
                    "latest_pct_chg": 2.0,
                    "up_days_5": 4,
                    "up_days_3": 3,
                }
            ],
            "stock_hot_sectors": {
                "000001.SZ": [
                    {
                        "sector_code": "885001.TI",
                        "name": "AI算力",
                        "sector_type": "concept",
                        "heat_score": 15.0,
                        "return_5d": 8.0,
                        "return_3d": 5.0,
                        "latest_pct_chg": 2.0,
                        "up_days_5": 4,
                        "up_days_3": 3,
                    }
                ]
            },
            "missing_fields": [],
            "data_sources": {"sector_basic": "fake", "sector_daily": "fake", "sector_members": "fake"},
        }


class _FakeAkShareProvider:
    def load_realtime_quotes(self, symbols):
        return pd.DataFrame([{"ts_code": symbol, "name": f"Ak{symbol[:6]}", "pe": 12.5, "data_source": "akshare_spot_em"} for symbol in symbols])

    def load_chip_context(self, symbols):
        return {symbol: {"profit_ratio": 0.66, "avg_cost": 10.5, "data_source": "akshare_stock_cyq_em"} for symbol in symbols}

    def load_fundamental_context(self, symbols):
        return {symbol: {"industry": "银行", "data_source": "akshare_stock_individual_info_em"} for symbol in symbols}


class _FakeLLM:
    def __init__(self) -> None:
        self.messages = []
        self.timeouts = []

    def chat(self, messages, timeout=None):
        self.messages.append(messages)
        self.timeouts.append(timeout)
        return SimpleNamespace(
            content=json.dumps(
                {
                    "score": 82,
                    "advice": "买入",
                    "trend": "看多",
                    "summary": "趋势偏强，资金流配合。",
                    "risk": "注意追高风险，不构成投资建议。",
                },
                ensure_ascii=False,
            )
        )


class _BrokenLLM:
    def chat(self, messages, timeout=None):
        raise RuntimeError("llm unavailable")


class _CountingBrokenLLM:
    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, timeout=None):
        self.calls += 1
        raise TimeoutError("llm timeout")


class _InvalidAdviceLLM:
    def chat(self, messages, timeout=None):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "sentiment_score": 82,
                    "operation_advice": "关注",
                    "trend_prediction": "看多",
                    "analysis_summary": "LLM 给出了非法评级。",
                    "risk_warning": "不构成投资建议。",
                },
                ensure_ascii=False,
            )
        )


class _SellLLM:
    def chat(self, messages, timeout=None):
        return SimpleNamespace(
            content=json.dumps(
                {
                    "sentiment_score": 20,
                    "operation_advice": "减仓",
                    "trend_prediction": "看空",
                    "analysis_summary": "LLM 给出激进卖出建议。",
                    "risk_warning": "不构成投资建议。",
                },
                ensure_ascii=False,
            )
        )


def _decision_indicator(
    *,
    close: float,
    ma5: float,
    ma10: float,
    ma20: float,
    macd_signal: str,
    rsi12: float,
    volume_status: str,
    moneyflow: dict | None = None,
    fundamentals: dict | None = None,
    kdj: dict | None = None,
    boll: dict | None = None,
):
    return SimpleNamespace(
        close=close,
        technical={
            "ma": {"ma5": ma5, "ma10": ma10, "ma20": ma20},
            "bias": {"ma5": (close - ma5) / ma5 * 100},
            "macd": {"signal": macd_signal},
            "rsi": {"rsi12": rsi12},
            "kdj": kdj or {},
            "boll": boll or {},
        },
        volume={"status": volume_status},
        moneyflow=moneyflow or {},
        fundamentals=fundamentals or {},
        support_resistance={"support": [], "resistance": []},
    )


class NativeDsaAnalysisTest(unittest.TestCase):
    def test_local_decision_outputs_dsa_style_buy_hold_and_sell_ratings(self) -> None:
        strong = build_local_dsa_decision(
            _decision_indicator(
                close=10.1,
                ma5=10.0,
                ma10=9.5,
                ma20=9.0,
                macd_signal="零轴上金叉",
                rsi12=65,
                volume_status="缩量回调",
            )
        )
        neutral = build_local_dsa_decision(
            _decision_indicator(
                close=10.05,
                ma5=10.0,
                ma10=10.0,
                ma20=10.0,
                macd_signal="中性",
                rsi12=50,
                volume_status="量能正常",
            )
        )
        weak = build_local_dsa_decision(
            _decision_indicator(
                close=8.0,
                ma5=10.0,
                ma10=11.0,
                ma20=12.0,
                macd_signal="死叉",
                rsi12=35,
                volume_status="放量下跌",
            )
        )

        self.assertEqual(strong.operation_advice, "强烈买入")
        self.assertEqual(strong.decision_type, "buy")
        self.assertIn(neutral.operation_advice, {"持有", "观望"})
        self.assertEqual(neutral.decision_type, "hold")
        self.assertIn(weak.operation_advice, {"卖出", "强烈卖出"})
        self.assertEqual(weak.decision_type, "sell")

    def test_run_dsa_analysis_normalizes_symbols_merges_sources_and_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            storage = DuckDBStorage(settings.db_path)
            tickflow = _FakeTickFlowProvider()
            tushare = _FakeTushareProvider()
            llm = _FakeLLM()

            result = run_dsa_analysis(
                ["000001", "600519"],
                trade_date="20260518",
                reports_dir=root / "reports",
                settings=settings,
                storage=storage,
                tickflow_provider=tickflow,
                tushare_provider=tushare,
                akshare_provider=_FakeAkShareProvider(),
                llm=llm,
            )

            self.assertEqual(tickflow.indicator_symbols, ["000001.SZ", "600519.SH"])
            self.assertEqual(tickflow.quote_symbols, ["000001.SZ", "600519.SH"])
            self.assertEqual(tushare.symbols, ["000001.SZ", "600519.SH"])
            self.assertEqual(result.analyzed_codes, ["000001.SZ", "600519.SH"])
            self.assertEqual([row.code for row in result.rankings], ["000001.SZ", "600519.SH"])
            self.assertEqual(result.rankings[0].score, 64)
            self.assertEqual(result.rankings[0].advice, "买入")
            self.assertFalse(result.llm_unavailable)
            self.assertIsNotNone(result.archived_report)
            self.assertEqual(result.analyses[0].summary, "趋势偏强，资金流配合。")
            dashboard = result.analyses[0].dashboard
            self.assertIn("core_conclusion", dashboard)
            self.assertIn("data_perspective", dashboard)
            self.assertIn("intelligence", dashboard)
            self.assertIn("battle_plan", dashboard)
            self.assertEqual(result.analyses[0].hot_sectors[0]["name"], "AI算力")
            self.assertIn("news_context: provider_unavailable", result.analyses[0].missing_fields)
            self.assertEqual(result.analyses[0].market_phase["phase"], "historical_replay")
            report_text = result.archived_report.read_text(encoding="utf-8")
            self.assertIn("SATS DSA 原生分析报告", report_text)
            self.assertIn("买入类:", report_text)
            self.assertIn("可比股票数:", report_text)
            self.assertIn("#### 核心结论", report_text)
            self.assertIn("#### 数据视角", report_text)
            self.assertIn("#### 战术计划", report_text)
            self.assertIn("本地原始评级", report_text)
            self.assertIn("稳定性调整后评级", report_text)
            self.assertIn("LLM: available", report_text)
            self.assertIn("新闻/舆情：未启用", report_text)
            self.assertIn("热点板块：AI算力", report_text)
            self.assertIn("daily=tickflow_daily", report_text)
            self.assertIn("moneyflow=tushare_moneyflow_dc", report_text)
            self.assertIn("hot_sector=tushare_ths", report_text)
            self.assertTrue(llm.messages)
            self.assertIn("不得编造价格", llm.messages[0][0]["content"])
            self.assertIn('"trade_date": "20260518"', llm.messages[0][-1]["content"])
            self.assertIn('"data_sources"', llm.messages[0][-1]["content"])
            self.assertIn('"missing_fields"', llm.messages[0][-1]["content"])
            self.assertEqual(llm.timeouts, [20, 20])

    def test_run_dsa_analysis_falls_back_when_optional_sources_fail(self) -> None:
        class BrokenAkShare:
            def load_realtime_quotes(self, symbols):
                raise RuntimeError("akshare unavailable")

            def load_chip_context(self, symbols):
                raise RuntimeError("akshare unavailable")

            def load_fundamental_context(self, symbols):
                raise RuntimeError("akshare unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            result = run_dsa_analysis(
                ["000001"],
                trade_date="20260518",
                reports_dir=root / "reports",
                settings=settings,
                storage=DuckDBStorage(settings.db_path),
                tickflow_provider=_FakeTickFlowProvider(),
                tushare_provider=_FakeTushareProvider(),
                akshare_provider=BrokenAkShare(),
                llm=_BrokenLLM(),
            )

            self.assertEqual(result.analyzed_codes, ["000001.SZ"])
            self.assertTrue(result.llm_unavailable)
            self.assertNotIn(result.rankings[0].advice, {"关注", "买入观察", "回避"})
            self.assertTrue(result.archived_report.exists())
            report_text = result.archived_report.read_text(encoding="utf-8")
            self.assertIn("不构成投资建议", report_text)
            self.assertIn("LLM: unavailable，本地规则评级", report_text)

    def test_run_dsa_analysis_falls_back_when_llm_returns_invalid_advice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            result = run_dsa_analysis(
                ["000001"],
                trade_date="20260518",
                reports_dir=root / "reports",
                settings=settings,
                storage=DuckDBStorage(settings.db_path),
                tickflow_provider=_FakeTickFlowProvider(),
                tushare_provider=_FakeTushareProvider(),
                akshare_provider=_FakeAkShareProvider(),
                llm=_InvalidAdviceLLM(),
            )

            self.assertTrue(result.llm_unavailable)
            self.assertNotEqual(result.rankings[0].advice, "关注")
            self.assertIn(result.rankings[0].advice, {"强烈买入", "买入", "持有", "观望", "减仓", "卖出", "强烈卖出"})

    def test_run_dsa_analysis_breaks_llm_calls_after_first_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            llm = _CountingBrokenLLM()
            result = run_dsa_analysis(
                ["000001", "600519"],
                trade_date="20260518",
                reports_dir=root / "reports",
                settings=settings,
                storage=DuckDBStorage(settings.db_path),
                tickflow_provider=_FakeTickFlowProvider(),
                tushare_provider=_FakeTushareProvider(),
                akshare_provider=_FakeAkShareProvider(),
                llm=llm,
                llm_timeout_seconds=5,
            )

            self.assertEqual(llm.calls, 1)
            self.assertTrue(result.llm_unavailable)
            self.assertTrue(all(item.llm_unavailable for item in result.analyses))
            self.assertTrue(result.archived_report.exists())

    def test_run_dsa_analysis_can_skip_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            llm = _CountingBrokenLLM()
            result = run_dsa_analysis(
                ["000001", "600519"],
                trade_date="20260518",
                reports_dir=root / "reports",
                settings=settings,
                storage=DuckDBStorage(settings.db_path),
                tickflow_provider=_FakeTickFlowProvider(),
                tushare_provider=_FakeTushareProvider(),
                akshare_provider=_FakeAkShareProvider(),
                llm=llm,
                llm_enabled=False,
            )

            self.assertEqual(llm.calls, 0)
            self.assertFalse(result.llm_unavailable)
            self.assertTrue(all(item.llm_disabled for item in result.analyses))
            self.assertIn("LLM: disabled，本地规则评级", result.archived_report.read_text(encoding="utf-8"))

    def test_mini_racer_unraisable_filter_only_matches_known_destructor_noise(self) -> None:
        def mini_racer_del():
            return None

        mini_racer_del.__qualname__ = "MiniRacer.__del__"
        mini_racer_del.__module__ = "py_mini_racer.py_mini_racer"
        noise = SimpleNamespace(
            exc_type=AttributeError,
            exc_value=AttributeError("'NoneType' object has no attribute 'mr_free_context'"),
            object=mini_racer_del,
        )
        other = SimpleNamespace(
            exc_type=AttributeError,
            exc_value=AttributeError("'NoneType' object has no attribute 'mr_free_context'"),
            object=lambda: None,
        )

        self.assertTrue(_is_mini_racer_unraisable(noise))
        self.assertFalse(_is_mini_racer_unraisable(other))

    def test_local_decision_downgrades_overheated_buy(self) -> None:
        decision = build_local_dsa_decision(
            _decision_indicator(
                close=13.0,
                ma5=12.0,
                ma10=11.0,
                ma20=10.0,
                macd_signal="零轴上金叉",
                rsi12=78,
                volume_status="缩量回调",
                moneyflow={"main_net_amount": 1000.0, "main_net_amount_5d": 500.0, "main_net_amount_10d": 300.0},
                kdj={"k": 90, "d": 82, "j": 106},
                boll={"position": "上轨上方"},
            )
        )

        self.assertEqual(decision.raw_operation_advice, "买入")
        self.assertIn(decision.operation_advice, {"持有", "观望"})
        self.assertTrue(decision.adjustment_reasons)

    def test_local_decision_downgrades_buy_when_main_flow_out(self) -> None:
        decision = build_local_dsa_decision(
            _decision_indicator(
                close=10.1,
                ma5=10.0,
                ma10=9.5,
                ma20=9.0,
                macd_signal="零轴上金叉",
                rsi12=65,
                volume_status="缩量回调",
                moneyflow={"main_net_amount": -100.0, "main_net_amount_5d": -50.0},
            )
        )

        self.assertEqual(decision.raw_operation_advice, "强烈买入")
        self.assertEqual(decision.operation_advice, "观望")
        self.assertIn("主力资金流出", "；".join(decision.adjustment_reasons))

    def test_run_dsa_analysis_marks_external_unsupported_and_groups_rankings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            result = run_dsa_analysis(
                ["688001", "600519"],
                trade_date="20260518",
                reports_dir=root / "reports",
                settings=settings,
                storage=DuckDBStorage(settings.db_path),
                tickflow_provider=_FakeTickFlowProvider(),
                tushare_provider=_FakeTushareProvider(),
                akshare_provider=_FakeAkShareProvider(),
                llm=_FakeLLM(),
            )

            self.assertEqual([row.code for row in result.rankings], ["600519.SH", "688001.SH"])
            self.assertTrue(result.rankings[0].external_supported)
            self.assertFalse(result.rankings[1].external_supported)
            self.assertEqual(result.rankings[1].external_skip_reason, "daily_stock_analysis 不支持")

    def test_run_dsa_analysis_does_not_let_llm_force_sell_rating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            result = run_dsa_analysis(
                ["000001"],
                trade_date="20260518",
                reports_dir=root / "reports",
                settings=settings,
                storage=DuckDBStorage(settings.db_path),
                tickflow_provider=_FakeTickFlowProvider(),
                tushare_provider=_FakeTushareProvider(),
                akshare_provider=_FakeAkShareProvider(),
                llm=_SellLLM(),
            )

            self.assertNotIn(result.rankings[0].advice, {"减仓", "卖出", "强烈卖出"})
            self.assertFalse(result.llm_unavailable)


if __name__ == "__main__":
    unittest.main()
