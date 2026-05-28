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

from sats.cli import main
from sats.screening.base import ScreeningResult
from sats.signals import (
    SignalInput,
    analyze_signal_input,
    format_signal_analysis,
    format_signal_definitions,
    list_signal_definitions,
    screening_result_from_signal_input,
)
from sats.storage.duckdb import DuckDBStorage


def _trade_dates(count: int, *, end: str = "20260520") -> list[str]:
    cursor = datetime.strptime(end, "%Y%m%d")
    dates = []
    while len(dates) < count:
        if cursor.weekday() < 5:
            dates.append(cursor.strftime("%Y%m%d"))
        cursor -= timedelta(days=1)
    return sorted(dates)


def _signal_daily(ts_code: str = "000938.SZ", *, end: str = "20260520") -> pd.DataFrame:
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


class _FakeSignalProvider:
    calls: list[dict] = []

    def __init__(self, settings) -> None:
        self.settings = settings

    def load_screening_inputs(self, symbols, trade_date, *, storage=None, trade_days=80, rule_name=None):
        self.__class__.calls.append(
            {
                "symbols": list(symbols),
                "trade_date": trade_date,
                "trade_days": trade_days,
                "rule_name": rule_name,
            }
        )
        from sats.screening.base import ScreeningInput

        return [
            ScreeningInput(
                ts_code=symbol,
                trade_date=trade_date,
                daily=_signal_daily(symbol, end=trade_date),
                daily_basic=pd.DataFrame(),
                stock_basic={"ts_code": symbol, "name": f"名称{symbol[:6]}"},
                metadata={"data_source": "fake"},
            )
            for symbol in symbols
        ]


class SignalAnalysisTest(unittest.TestCase):
    def setUp(self) -> None:
        _FakeSignalProvider.calls = []

    def test_signal_definitions_include_abu_interwoven_groups(self) -> None:
        text = format_signal_definitions(category="ma_kline")

        self.assertIn("ma_kline", text)
        self.assertIn("ma_dragon_sea_kline", text)
        self.assertIn("蛟龙出海买入点", text)
        self.assertIn("葛兰威尔第②买入点", text)
        self.assertIn("kc_double_needle_graph", format_signal_definitions(category="kline_graph"))
        graph_text = format_signal_definitions(category="graph_graph")
        self.assertIn("graph_cypher_bullish_chan", graph_text)
        self.assertIn("graph_bat_third_target_chan", graph_text)
        self.assertIn("graph_elliott_up_c_chan", graph_text)

    def test_short_up_signal_group_only_lists_buy_side_short_term_signals(self) -> None:
        definitions = list_signal_definitions(category="short_up")

        self.assertTrue(definitions)
        self.assertTrue(all(item.side == "buy" for item in definitions))
        short_up_categories = {"ma_kline", "kline_graph", "ma_graph", "graph_graph", "chan", "trendline"}
        self.assertTrue(all(item.category in short_up_categories for item in definitions))

    def test_analyze_signal_input_detects_ma_kline_composite(self) -> None:
        result = analyze_signal_input(
            SignalInput(
                ts_code="000938.SZ",
                trade_date="20260520",
                daily=_signal_daily(),
                stock_basic={"name": "紫光股份"},
            ),
            selected_signals="ma_kline",
        )

        ids = [event.signal_id for event in result.events]
        self.assertIn("ma_dragon_sea_kline", ids)
        self.assertTrue(result.score > 50)
        self.assertIn(result.decision, {"买入观察", "持有"})
        self.assertIn("蛟龙出海买入点", format_signal_analysis([result]))

    def test_analyze_signal_input_short_up_filters_out_sell_events(self) -> None:
        result = analyze_signal_input(
            SignalInput(
                ts_code="000938.SZ",
                trade_date="20260520",
                daily=_signal_daily(),
                stock_basic={"name": "紫光股份"},
            ),
            selected_signals="short_up",
        )

        self.assertTrue(result.events)
        self.assertTrue(all(event.side == "buy" for event in result.events))
        short_up_categories = {"ma_kline", "kline_graph", "ma_graph", "graph_graph", "chan", "trendline"}
        self.assertTrue(all(event.category in short_up_categories for event in result.events))

    def test_analyze_signal_llm_review_uses_structured_trade_date_context(self) -> None:
        captured = {}

        class FakeLLM:
            def chat(self, messages):
                captured["messages"] = messages
                return SimpleNamespace(content="结构化信号偏强，注意回撤风险。")

        with patch("sats.signals.engine.ChatLLM", return_value=FakeLLM()):
            result = analyze_signal_input(
                SignalInput(
                    ts_code="000938.SZ",
                    trade_date="20260520",
                    daily=_signal_daily(),
                    stock_basic={"name": "紫光股份"},
                    metadata={"data_sources": {"daily": "tickflow_daily"}},
                ),
                selected_signals="ma_kline",
                llm_review=True,
            )

        self.assertEqual(result.llm_summary, "结构化信号偏强，注意回撤风险。")
        prompt = captured["messages"][0]["content"]
        self.assertIn("不得编造价格", prompt)
        self.assertIn('"trade_date": "20260520"', prompt)
        self.assertIn('"data_sources"', prompt)

    def test_signal_composite_screening_result_contains_matched_labels(self) -> None:
        result = screening_result_from_signal_input(
            SignalInput(
                ts_code="000938.SZ",
                trade_date="20260520",
                daily=_signal_daily(),
                stock_basic={"name": "紫光股份"},
            ),
            selected_signals="ma_kline",
        )

        self.assertTrue(result.passed)
        self.assertEqual(result.rule_name, "signal_composite")
        self.assertIn("matched_signal_labels", result.metrics)
        self.assertIn("蛟龙出海买入点 ✚ K线信号", result.metrics["matched_signal_labels"])

    def test_cli_analyze_signals_lists_category(self) -> None:
        stdout = io.StringIO()

        with redirect_stdout(stdout):
            exit_code = main(["analyze", "signals", "--category", "kline_graph"])

        self.assertEqual(exit_code, 0)
        self.assertIn("kc_double_needle_graph", stdout.getvalue())

    def test_cli_analyze_stocks_generates_report_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            stdout = io.StringIO()

            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.AStockDataProvider", _FakeSignalProvider),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "analyze",
                        "--stocks",
                        "000938",
                        "--signals",
                        "ma_kline",
                        "--trade-date",
                        "20260520",
                    ]
                )

            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("analyzing...", output)
            self.assertIn("000938.SZ", output)
            self.assertIn("报告:", output)
            self.assertEqual(_FakeSignalProvider.calls[0]["symbols"], ["000938.SZ"])
            self.assertEqual(_FakeSignalProvider.calls[0]["rule_name"], "signal_composite")
            reports = list((root / "reports").glob("signal_analysis_20260520_stocks_*.md"))
            self.assertEqual(len(reports), 1)
            self.assertIn("SATS 信号分析报告", reports[0].read_text(encoding="utf-8"))

    def test_cli_analyze_noreport_does_not_write_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            stdout = io.StringIO()

            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.AStockDataProvider", _FakeSignalProvider),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "analyze",
                        "--stocks",
                        "000938",
                        "--signals",
                        "ma_kline",
                        "--trade-date",
                        "20260520",
                        "--noreport",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertNotIn("报告:", stdout.getvalue())
            self.assertFalse((root / "reports").exists())

    def test_cli_analyze_json_outputs_structured_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = SimpleNamespace(project_root=root, db_path=root / "sats.duckdb")
            stdout = io.StringIO()

            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.AStockDataProvider", _FakeSignalProvider),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "analyze",
                        "--stocks",
                        "000938",
                        "--signals",
                        "ma_kline",
                        "--trade-date",
                        "20260520",
                        "--json",
                        "--noreport",
                    ]
                )

            payload = json.loads(stdout.getvalue()[stdout.getvalue().index("{"):])
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["results"][0]["ts_code"], "000938.SZ")
            self.assertIn("events", payload["results"][0])

    def test_cli_analyze_from_screened_uses_saved_passed_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "sats.duckdb"
            storage = DuckDBStorage(db_path)
            storage.upsert_stock_basic(
                pd.DataFrame([{"ts_code": "000938.SZ", "symbol": "000938", "name": "紫光股份"}])
            )
            storage.upsert_screening_results(
                [
                    ScreeningResult(
                        trade_date="20260520",
                        ts_code="000938.SZ",
                        rule_name="price_volume_ma",
                        passed=True,
                        score=80,
                        matched_conditions=[],
                        failed_conditions=[],
                        metrics={},
                    )
                ]
            )
            settings = SimpleNamespace(project_root=root, db_path=db_path)
            stdout = io.StringIO()

            with (
                patch("sats.cli.load_settings", return_value=settings),
                patch("sats.cli.AStockDataProvider", _FakeSignalProvider),
                redirect_stdout(stdout),
            ):
                exit_code = main(
                    [
                        "analyze",
                        "--from-screened",
                        "--trade-date",
                        "20260520",
                        "--rule",
                        "price_volume_ma",
                        "--signals",
                        "ma_kline",
                        "--noreport",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertEqual(_FakeSignalProvider.calls[0]["symbols"], ["000938.SZ"])
            self.assertIn("000938.SZ", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
