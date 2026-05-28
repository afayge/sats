from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.analysis.opportunity_discovery import (
    OpportunityCandidate,
    OpportunityDiscoveryResult,
    format_opportunity_discovery,
    is_opportunity_discovery_question,
    run_opportunity_discovery,
)
from sats.cli import main
from sats.indicators import IndicatorInput
from sats.screening.base import ScreeningInput
from sats.signals import SignalAnalysisResult, SignalEvent
from sats.storage.duckdb import DuckDBStorage


def _trade_dates(count: int, *, end: str = "20260520") -> list[str]:
    cursor = datetime.strptime(end, "%Y%m%d")
    dates = []
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    return sorted(dates)


def _bullish_daily(ts_code: str, *, end: str = "20260520") -> pd.DataFrame:
    dates = _trade_dates(80, end=end)
    closes = [10 + index * 0.03 for index in range(75)] + [12.0, 12.2, 12.4, 12.7, 13.2]
    rows = []
    for index, (trade_date, close) in enumerate(zip(dates, closes)):
        prev_close = closes[index - 1] if index else close
        open_price = close - 0.08
        high = close + 0.12
        low = close - 0.12
        volume = 1000.0
        if index == len(dates) - 1:
            open_price = 12.5
            high = 13.4
            low = 12.4
            volume = 2200.0
        rows.append(
            {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "open": round(open_price, 4),
                "high": round(high, 4),
                "low": round(low, 4),
                "close": round(close, 4),
                "vol": volume,
                "amount": volume * close,
                "pct_chg": (close / prev_close - 1.0) * 100 if index else 0.0,
            }
        )
    return pd.DataFrame(rows)


def _flat_daily(ts_code: str, *, end: str = "20260520") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "vol": 1000.0,
                "amount": 10000.0,
                "pct_chg": 0.0,
            }
            for trade_date in _trade_dates(80, end=end)
        ]
    )


class _FakeDiscoveryProvider:
    def __init__(self, *, bullish: bool = True) -> None:
        self.bullish = bullish
        self.all_calls: list[dict] = []
        self.indicator_calls: list[dict] = []

    def load_all_screening_inputs(self, trade_date, *, storage=None, trade_days=80, rule_name=None):
        self.all_calls.append({"trade_date": trade_date, "trade_days": trade_days, "rule_name": rule_name})
        symbols = ["000938.SZ", "600519.SH"] if self.bullish else ["000001.SZ"]
        rows = []
        for symbol in symbols:
            daily = _bullish_daily(symbol, end=trade_date) if self.bullish else _flat_daily(symbol, end=trade_date)
            rows.append(
                ScreeningInput(
                    ts_code=symbol,
                    trade_date=trade_date,
                    daily=daily,
                    daily_basic=pd.DataFrame(),
                    stock_basic={"ts_code": symbol, "name": f"名称{symbol[:6]}"},
                    metadata={"data_source": "fake_daily"},
                )
            )
        return rows

    def load_indicator_inputs(self, symbols, trade_date, *, lookback_days=180, storage=None):
        self.indicator_calls.append({"symbols": list(symbols), "trade_date": trade_date, "lookback_days": lookback_days})
        inputs = []
        for symbol in symbols:
            inputs.append(
                IndicatorInput(
                    ts_code=symbol,
                    trade_date=trade_date,
                    daily=_bullish_daily(symbol, end=trade_date),
                    daily_basic=pd.DataFrame(
                        [
                            {
                                "ts_code": symbol,
                                "trade_date": trade_date,
                                "pe": 18.0,
                                "pb": 2.1,
                                "total_mv": 123456.0,
                                "turnover_rate": 4.2,
                            }
                        ]
                    ),
                    moneyflow=pd.DataFrame(
                        [
                            {"ts_code": symbol, "trade_date": date, "main_net_amount": 100.0}
                            for date in _trade_dates(10, end=trade_date)
                        ]
                    ),
                    stock_basic={"name": f"名称{symbol[:6]}"},
                    data_sources={"daily": "fake", "daily_basic": "fake", "moneyflow": "fake"},
                )
            )
        return inputs


class _FakeHotSectorDiscoveryProvider(_FakeDiscoveryProvider):
    def __init__(self) -> None:
        super().__init__(bullish=True)
        self.hot_sector_calls: list[dict] = []

    def load_hot_sector_context(self, trade_date, *, storage=None, lookback_days=5):
        self.hot_sector_calls.append({"trade_date": trade_date, "lookback_days": lookback_days})
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
                "000938.SZ": [
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


class _FakeDiverseDiscoveryProvider(_FakeDiscoveryProvider):
    def __init__(self, symbols: list[str]) -> None:
        super().__init__(bullish=True)
        self.symbols = symbols

    def load_all_screening_inputs(self, trade_date, *, storage=None, trade_days=80, rule_name=None):
        self.all_calls.append({"trade_date": trade_date, "trade_days": trade_days, "rule_name": rule_name})
        rows = []
        for index, symbol in enumerate(self.symbols):
            rows.append(
                ScreeningInput(
                    ts_code=symbol,
                    trade_date=trade_date,
                    daily=_bullish_daily(symbol, end=trade_date),
                    daily_basic=pd.DataFrame(),
                    stock_basic={"ts_code": symbol, "name": f"名称{symbol[:6]}", "industry": f"行业{index % 4}"},
                    metadata={"data_source": "fake_daily"},
                )
            )
        return rows


def _fake_signal_result(ts_code: str, *, trade_date: str, score: float) -> SignalAnalysisResult:
    return SignalAnalysisResult(
        ts_code=ts_code,
        trade_date=trade_date,
        name=f"名称{ts_code[:6]}",
        close=13.2,
        score=score,
        decision="买入观察",
        trend="看多",
        selected_signals=["short_up"],
        key_levels={"support": 12.5, "resistance": 14.0},
        events=[
            SignalEvent(
                signal_id="fake_buy",
                label="短线买入信号",
                category="test",
                side="buy",
                confidence=0.8,
                score=score,
                reason="测试信号",
            )
        ],
    )


class OpportunityDiscoveryTest(unittest.TestCase):
    def test_question_detection_matches_short_term_stock_selection_intent(self) -> None:
        self.assertTrue(is_opportunity_discovery_question("给出几个股票，预计未来几天有上涨趋势的股票"))
        self.assertFalse(is_opportunity_discovery_question("今天午饭吃什么"))

    def test_run_opportunity_discovery_screens_in_memory_and_does_not_write_results(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = DuckDBStorage(root / "sats.duckdb")
            provider = _FakeDiscoveryProvider()
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")

            with patch(
                "sats.analysis.opportunity_discovery.get_a_share_market_context",
                return_value={"trade_date": "20260520", "indices": [{"ts_code": "000001.SH"}], "missing_fields": []},
            ):
                result = run_opportunity_discovery(
                    settings=settings,
                    storage=storage,
                    provider=provider,
                    trade_date="20260520",
                    limit=1,
                    candidate_limit=2,
                    reports_dir=root / "reports",
                    llm_enabled=False,
                )

            self.assertEqual(provider.all_calls[0]["rule_name"], "signal_discovery")
            self.assertEqual(provider.indicator_calls[0]["symbols"], ["000938.SZ", "600519.SH"])
            self.assertEqual(result.scanned_count, 2)
            self.assertEqual(len(result.candidates), 1)
            self.assertEqual(result.candidates[0].events[0]["side"], "buy")
            self.assertIn("technical", result.candidates[0].indicator)
            self.assertIn("fundamentals", result.candidates[0].indicator)
            self.assertEqual(storage.list_screening_results(), [])
            self.assertTrue(result.report_path)
            self.assertTrue(Path(result.report_path or "").exists())

    def test_run_opportunity_discovery_can_forward_market_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            storage = DuckDBStorage(root / "sats.duckdb")
            provider = _FakeDiscoveryProvider()
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")

            with patch(
                "sats.analysis.opportunity_discovery.get_a_share_market_context",
                return_value={"trade_date": "20260520", "indices": [{"ts_code": "000001.SH"}], "missing_fields": []},
            ) as market_context:
                run_opportunity_discovery(
                    settings=settings,
                    storage=storage,
                    provider=provider,
                    trade_date="20260520",
                    limit=1,
                    candidate_limit=2,
                    reports_dir=root / "reports",
                    report=False,
                    llm_enabled=False,
                    market_indices=["000001.SH", "399330.SZ"],
                    market_dimensions=["core_indices", "limit_sentiment"],
                    market_horizons=["tomorrow", "day_after_tomorrow"],
                    market_plan_source="llm+local_market_plan",
                )

        self.assertEqual(market_context.call_args.kwargs["indices"], ["000001.SH", "399330.SZ"])
        self.assertEqual(market_context.call_args.kwargs["dimensions"], ["core_indices", "limit_sentiment"])
        self.assertEqual(market_context.call_args.kwargs["horizons"], ["tomorrow", "day_after_tomorrow"])
        self.assertEqual(market_context.call_args.kwargs["market_plan_source"], "llm+local_market_plan")

    def test_hot_sector_priority_can_lift_slightly_lower_signal_candidate(self) -> None:
        def fake_analyze(item, selected_signals="short_up"):
            scores = {"000938.SZ": 60.0, "600519.SH": 66.0}
            return _fake_signal_result(item.ts_code, trade_date=item.trade_date, score=scores[item.ts_code])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _FakeHotSectorDiscoveryProvider()
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.opportunity_discovery.analyze_signal_input", side_effect=fake_analyze),
            ):
                result = run_opportunity_discovery(
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    trade_date="20260520",
                    limit=2,
                    candidate_limit=2,
                    report=True,
                    reports_dir=root / "reports",
                    llm_enabled=False,
                    hot_sector_days=3,
                )
            report_text = Path(result.report_path or "").read_text(encoding="utf-8")

        self.assertEqual(provider.hot_sector_calls, [{"trade_date": "20260520", "lookback_days": 3}])
        self.assertEqual(result.candidates[0].ts_code, "000938.SZ")
        self.assertEqual(result.candidates[0].local_score, 60.0)
        self.assertGreater(result.candidates[0].hot_sector_score, 0)
        self.assertGreater(result.candidates[0].ranking_score, result.candidates[1].ranking_score)
        self.assertEqual(result.candidates[0].hot_sectors[0]["name"], "AI算力")
        self.assertEqual(result.hot_sector_context["hot_concepts"][0]["name"], "AI算力")
        formatted = format_opportunity_discovery(result)
        self.assertIn("排名分", formatted)
        self.assertIn("热点 AI算力", formatted)
        self.assertIn("热点板块", report_text)
        self.assertIn("AI算力", report_text)
        self.assertIn("hot_sector_context", result.system_message)

    def test_no_hot_sector_disables_hot_context_and_uses_local_score_order(self) -> None:
        def fake_analyze(item, selected_signals="short_up"):
            scores = {"000938.SZ": 60.0, "600519.SH": 66.0}
            return _fake_signal_result(item.ts_code, trade_date=item.trade_date, score=scores[item.ts_code])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _FakeHotSectorDiscoveryProvider()
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.opportunity_discovery.analyze_signal_input", side_effect=fake_analyze),
            ):
                result = run_opportunity_discovery(
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    trade_date="20260520",
                    limit=2,
                    candidate_limit=2,
                    report=False,
                    llm_enabled=False,
                    hot_sector_enabled=False,
                )

        self.assertEqual(provider.hot_sector_calls, [])
        self.assertEqual([candidate.ts_code for candidate in result.candidates], ["600519.SH", "000938.SZ"])
        self.assertEqual(result.candidates[0].ranking_score, result.candidates[0].local_score)
        self.assertEqual(result.hot_sector_context, {})

    def test_equal_score_candidates_do_not_cluster_by_code_prefix(self) -> None:
        symbols = ["000001.SZ", "000002.SZ", "002001.SZ", "300001.SZ", "600001.SH", "688001.SH"]

        def fake_analyze(item, selected_signals="short_up"):
            return _fake_signal_result(item.ts_code, trade_date=item.trade_date, score=66.0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _FakeDiverseDiscoveryProvider(symbols)
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.opportunity_discovery.analyze_signal_input", side_effect=fake_analyze),
            ):
                result = run_opportunity_discovery(
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    trade_date="20260520",
                    limit=4,
                    candidate_limit=4,
                    report=False,
                    llm_enabled=False,
                    hot_sector_enabled=False,
                )

        prefixes = [candidate.ts_code[:3] for candidate in result.candidates]
        self.assertLess(prefixes.count("000") + prefixes.count("002"), 4)
        self.assertGreaterEqual(len({candidate.ts_code.split(".", 1)[1] for candidate in result.candidates}), 2)

    def test_llm_over_concentrated_ranking_is_rebalanced_when_scores_are_close(self) -> None:
        symbols = ["000001.SZ", "000002.SZ", "002001.SZ", "300001.SZ", "600001.SH"]
        calls = []

        def fake_analyze(item, selected_signals="short_up"):
            return _fake_signal_result(item.ts_code, trade_date=item.trade_date, score=66.0)

        class ClusteredLLM:
            def chat(self, messages):
                calls.append(messages)
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "rankings": [
                                {"ts_code": "000001.SZ", "reason": "同板块1"},
                                {"ts_code": "000002.SZ", "reason": "同板块2"},
                                {"ts_code": "002001.SZ", "reason": "同板块3"},
                            ]
                        },
                        ensure_ascii=False,
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _FakeDiverseDiscoveryProvider(symbols)
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.opportunity_discovery.analyze_signal_input", side_effect=fake_analyze),
            ):
                result = run_opportunity_discovery(
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    trade_date="20260520",
                    limit=3,
                    candidate_limit=5,
                    report=False,
                    llm_factory=lambda: ClusteredLLM(),
                    hot_sector_enabled=False,
                )

        self.assertEqual(len(calls), 1)
        self.assertLess(sum(candidate.ts_code.startswith(("000", "002")) for candidate in result.candidates), 3)
        self.assertTrue(any("分散调整" in candidate.llm_reason for candidate in result.candidates))

    def test_llm_clustered_ranking_is_kept_when_scores_are_much_stronger(self) -> None:
        symbols = ["000001.SZ", "000002.SZ", "002001.SZ", "300001.SZ"]
        scores = {"000001.SZ": 80.0, "000002.SZ": 79.0, "002001.SZ": 78.0, "300001.SZ": 60.0}

        def fake_analyze(item, selected_signals="short_up"):
            return _fake_signal_result(item.ts_code, trade_date=item.trade_date, score=scores[item.ts_code])

        class StrongClusterLLM:
            def chat(self, messages):
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "rankings": [
                                {"ts_code": "000001.SZ", "reason": "强势1"},
                                {"ts_code": "000002.SZ", "reason": "强势2"},
                                {"ts_code": "002001.SZ", "reason": "强势3"},
                            ]
                        },
                        ensure_ascii=False,
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = _FakeDiverseDiscoveryProvider(symbols)
            with (
                patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}),
                patch("sats.analysis.opportunity_discovery.analyze_signal_input", side_effect=fake_analyze),
            ):
                result = run_opportunity_discovery(
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=provider,
                    trade_date="20260520",
                    limit=3,
                    candidate_limit=4,
                    report=False,
                    llm_factory=lambda: StrongClusterLLM(),
                    hot_sector_enabled=False,
                )

        self.assertEqual([candidate.ts_code for candidate in result.candidates], ["000001.SZ", "000002.SZ", "002001.SZ"])

    def test_run_opportunity_discovery_uses_llm_ranking_once(self) -> None:
        calls = []

        class RankingLLM:
            def chat(self, messages):
                calls.append(messages)
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "rankings": [
                                {
                                    "ts_code": "600519.SH",
                                    "reason": "信号更集中",
                                    "entry_trigger": "放量突破",
                                    "invalidation": "跌破 MA10",
                                    "risk": "追高风险",
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market_context = {
                "trade_date": "20260520",
                "limit_sentiment": {
                    "limit_up_count": 20,
                    "limit_down_count": 3,
                    "broken_limit_count": 12,
                    "emotion_stage": "退潮",
                    "data_source": "tushare_limit_list_d",
                    "missing_fields": [],
                },
                "missing_fields": [],
            }
            with patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value=market_context):
                result = run_opportunity_discovery(
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=_FakeDiscoveryProvider(),
                    trade_date="20260520",
                    limit=1,
                    candidate_limit=2,
                    report=False,
                    llm_factory=lambda: RankingLLM(),
                )

        self.assertEqual(len(calls), 1)
        self.assertIn("limit_sentiment", calls[0][0]["content"])
        self.assertIn("退潮", calls[0][0]["content"])
        self.assertEqual(result.candidates[0].ts_code, "600519.SH")
        self.assertEqual(result.candidates[0].llm_reason, "信号更集中")

    def test_run_opportunity_discovery_builds_interactive_llm_with_short_timeout(self) -> None:
        factory_calls = []

        class RankingLLM:
            def chat(self, messages):
                return SimpleNamespace(
                    content=json.dumps(
                        {
                            "rankings": [
                                {
                                    "ts_code": "600519.SH",
                                    "reason": "信号更集中",
                                    "entry_trigger": "放量突破",
                                    "invalidation": "跌破 MA10",
                                    "risk": "追高风险",
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
            with patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}):
                run_opportunity_discovery(
                    settings=SimpleNamespace(
                        project_root=root,
                        db_path=root / "sats.duckdb",
                        openai_model="mimo-v2.5-pro",
                    ),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=_FakeDiscoveryProvider(),
                    trade_date="20260520",
                    limit=1,
                    candidate_limit=2,
                    report=False,
                    llm_factory=llm_factory,
                )

        self.assertEqual(factory_calls[0]["model_name"], "mimo-v2.5-pro")

    def test_no_candidates_returns_message_and_skips_llm(self) -> None:
        def fail_llm():
            raise AssertionError("LLM should not be called")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = run_opportunity_discovery(
                settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                storage=DuckDBStorage(root / "sats.duckdb"),
                provider=_FakeDiscoveryProvider(bullish=False),
                trade_date="20260520",
                report=False,
                llm_factory=fail_llm,
            )

        self.assertEqual(result.candidates, [])
        self.assertEqual(result.message, "无符合中短期上涨信号的候选股票")

    def test_llm_failure_falls_back_to_local_signal_sorting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch("sats.analysis.opportunity_discovery.get_a_share_market_context", return_value={}):
                result = run_opportunity_discovery(
                    settings=SimpleNamespace(project_root=root, db_path=root / "sats.duckdb"),
                    storage=DuckDBStorage(root / "sats.duckdb"),
                    provider=_FakeDiscoveryProvider(),
                    trade_date="20260520",
                    limit=2,
                    report=False,
                    llm_factory=lambda: SimpleNamespace(chat=lambda _: (_ for _ in ()).throw(RuntimeError("down"))),
                )

        self.assertTrue(result.llm_unavailable)
        self.assertEqual(len(result.candidates), 2)

    def test_cli_discover_dispatches_service_and_prints_fallback_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(
                project_root=root,
                db_path=root / "sats.duckdb",
                tushare_token="",
                tushare_timeout_seconds=30,
            )
            result = OpportunityDiscoveryResult(
                trade_date="20260520",
                signals="short_up",
                candidates=[
                    OpportunityCandidate(
                        ts_code="000938.SZ",
                        name="紫光股份",
                        trade_date="20260520",
                        local_score=88,
                        decision="买入观察",
                        trend="看多",
                        close=13.2,
                        events=[{"label": "蛟龙出海", "side": "buy"}],
                        llm_reason="本地信号靠前",
                    )
                ],
                candidate_count=1,
                scanned_count=2,
                report_path=str(root / "reports" / "demo.md"),
                llm_unavailable=True,
            )
            stdout = io.StringIO()

            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.run_opportunity_discovery", return_value=result) as runner,
                redirect_stdout(stdout),
            ):
                exit_code = main(["discover", "--trade-date", "20260520", "--limit", "1", "--hot-sector-days", "3"])

        self.assertEqual(exit_code, 0)
        runner.assert_called_once()
        kwargs = runner.call_args.kwargs
        self.assertTrue(kwargs["hot_sector_enabled"])
        self.assertEqual(kwargs["hot_sector_days"], 3)
        output = stdout.getvalue()
        self.assertIn("analyzing...", output)
        self.assertIn("大模型不可用，已使用本地信号排序", output)
        self.assertIn("000938.SZ 紫光股份", output)
        self.assertIn("报告:", output)

    def test_format_opportunity_discovery_message(self) -> None:
        result = OpportunityDiscoveryResult(
            trade_date="20260520",
            signals="short_up",
            candidates=[],
            candidate_count=0,
            scanned_count=1,
            message="无符合中短期上涨信号的候选股票",
        )

        self.assertEqual(format_opportunity_discovery(result), "无符合中短期上涨信号的候选股票")


if __name__ == "__main__":
    unittest.main()
