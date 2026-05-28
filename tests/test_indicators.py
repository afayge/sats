from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.cli import main
from sats.data.tushare_provider import TushareDataProvider
from sats.indicators import IndicatorCalculator, IndicatorInput
from sats.storage.duckdb import DuckDBStorage


def _daily_frame(rows: int = 130, *, end: str = "20260514") -> pd.DataFrame:
    dates = pd.bdate_range(end=pd.to_datetime(end), periods=rows).strftime("%Y%m%d").tolist()
    data = []
    for index, date in enumerate(dates):
        close = 10.0 + index * 0.05
        open_ = close - 0.03
        data.append(
            {
                "ts_code": "000001.SZ",
                "trade_date": date,
                "open": open_,
                "high": close + 0.10,
                "low": close - 0.12,
                "close": close,
                "vol": 1000 + index * 10,
                "amount": 10000 + index * 100,
                "pct_chg": 0.5,
            }
        )
    return pd.DataFrame(data)


def _daily_basic_frame(end: str = "20260514") -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "trade_date": end,
                "turnover_rate": 8.0,
                "turnover_rate_f": 8.5,
                "pe": 12.3,
                "pb": 1.5,
                "ps": 2.1,
                "circ_mv": 1_200_000.0,
                "total_mv": 1_800_000.0,
            }
        ]
    )


class IndicatorCalculatorTest(unittest.TestCase):
    def test_calculates_core_technical_indicators(self) -> None:
        item = IndicatorInput(
            ts_code="000001.SZ",
            trade_date="20260514",
            daily=_daily_frame(),
            daily_basic=_daily_basic_frame(),
            moneyflow=pd.DataFrame(
                {
                    "ts_code": ["000001.SZ"] * 10,
                    "trade_date": pd.bdate_range(end="20260514", periods=10).strftime("%Y%m%d"),
                    "main_net_amount": list(range(1, 11)),
                }
            ),
            fundamentals=pd.DataFrame(
                [
                    {
                        "ts_code": "000001.SZ",
                        "end_date": "20260331",
                        "ann_date": "20260420",
                        "revenue": 1000.0,
                        "profit": 120.0,
                        "roe": 10.5,
                        "debt_to_assets": 45.0,
                    }
                ]
            ),
            stock_basic={"name": "平安银行"},
        )

        result = IndicatorCalculator().calculate(item)

        self.assertEqual(result.name, "平安银行")
        self.assertEqual(result.technical["ma_alignment"], "多头排列")
        self.assertIn("macd", result.technical)
        self.assertIn("rsi6", result.technical["rsi"])
        self.assertIn("upper", result.technical["boll"])
        self.assertIn("atr14", result.technical["atr"])
        self.assertIn("k", result.technical["kdj"])
        self.assertEqual(result.moneyflow["main_net_amount_5d"], 40.0)
        self.assertEqual(result.moneyflow["main_net_amount_10d"], 55.0)
        self.assertEqual(result.fundamentals["pe"], 12.3)
        self.assertEqual(result.fundamentals["roe"], 10.5)

    def test_detects_candlestick_support_and_wave(self) -> None:
        daily = _daily_frame(rows=80)
        latest = daily.index[-1]
        daily.loc[latest, ["open", "close", "high", "low"]] = [16.0, 16.01, 16.2, 15.8]

        result = IndicatorCalculator().calculate(
            IndicatorInput(ts_code="000001.SZ", trade_date="20260514", daily=daily)
        )

        self.assertIn("十字星", result.patterns["latest"])
        self.assertIn("support", result.support_resistance)
        self.assertIn(result.elliott_wave["pattern"], {"potential_5_wave", "potential_abc", "no_clear_wave"})


class IndicatorProviderAndStorageTest(unittest.TestCase):
    def test_storage_roundtrips_moneyflow_and_fundamentals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            storage.upsert_stock_moneyflow(
                pd.DataFrame(
                    [{"ts_code": "000001.SZ", "trade_date": "20260514", "main_net_amount": 12.0}]
                )
            )
            storage.upsert_stock_fundamentals(
                pd.DataFrame(
                    [{"ts_code": "000001.SZ", "end_date": "20260331", "ann_date": "20260420", "roe": 11.0}]
                )
            )

            self.assertEqual(storage.get_stock_moneyflow(["000001.SZ"]).iloc[0]["main_net_amount"], 12.0)
            self.assertEqual(storage.get_stock_fundamentals(["000001.SZ"], as_of="20260514").iloc[0]["roe"], 11.0)

    def test_tushare_provider_loads_indicator_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                db_path=Path(tmp) / "sats.duckdb",
                tushare_token="",
                tushare_timeout_seconds=30,
                tickflow_api_key="",
            )
            provider = TushareDataProvider(settings)
            provider.pro = _FakeIndicatorPro()
            storage = DuckDBStorage(settings.db_path)

            inputs = provider.load_indicator_inputs(["000001.SZ"], "20260514", storage=storage)

            self.assertEqual(len(inputs), 1)
            self.assertFalse(inputs[0].daily.empty)
            self.assertFalse(inputs[0].daily_basic.empty)
            self.assertFalse(inputs[0].moneyflow.empty)
            self.assertFalse(inputs[0].fundamentals.empty)
            self.assertEqual(inputs[0].stock_basic["name"], "平安银行")


class IndicatorCliTest(unittest.TestCase):
    def test_cli_indicators_prints_text_and_json(self) -> None:
        settings = SimpleNamespace(db_path=Path(":memory:"))
        fake_inputs = [
            IndicatorInput(
                ts_code="000001.SZ",
                trade_date="20260514",
                daily=_daily_frame(rows=130),
                daily_basic=_daily_basic_frame(),
                stock_basic={"name": "平安银行"},
            )
        ]

        with (
            patch("sats.cli.load_settings", return_value=settings),
            patch("sats.cli.AStockDataProvider") as provider_cls,
        ):
            provider_cls.return_value.load_indicator_inputs.return_value = fake_inputs
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    main(["indicators", "--symbols", "000001", "--trade-date", "20260514"]),
                    0,
                )
            self.assertIn("000001.SZ 平安银行", stdout.getvalue())
            self.assertEqual(provider_cls.return_value.load_indicator_inputs.call_args_list[0].args[0], ["000001.SZ"])

            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(
                    main(["indicators", "--symbols", "000001.SZ", "--trade-date", "20260514", "--json"]),
                    0,
                )
            parsed = json.loads(stdout.getvalue())
            self.assertEqual(parsed[0]["ts_code"], "000001.SZ")
            self.assertIn("technical", parsed[0])


class _FakeIndicatorPro:
    def stock_basic(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "symbol": "000001",
                    "name": "平安银行",
                    "industry": "银行",
                    "market": "主板",
                    "exchange": "SZSE",
                    "list_date": "19910403",
                }
            ]
        )

    def daily(self, **kwargs):
        return _daily_frame(rows=80)

    def daily_basic(self, **kwargs):
        return _daily_basic_frame()

    def moneyflow_dc(self, **kwargs):
        dates = pd.bdate_range(end="20260514", periods=10).strftime("%Y%m%d")
        return pd.DataFrame(
            {
                "ts_code": ["000001.SZ"] * 10,
                "trade_date": dates,
                "main_net_amount": list(range(10)),
            }
        )

    def income(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20260331",
                    "ann_date": "20260420",
                    "total_revenue": 1000.0,
                    "n_income": 100.0,
                }
            ]
        )

    def fina_indicator(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20260331",
                    "ann_date": "20260420",
                    "roe": 11.0,
                    "debt_to_assets": 44.0,
                }
            ]
        )

    def balancesheet(self, **kwargs):
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "end_date": "20260331",
                    "ann_date": "20260420",
                    "total_assets": 1000.0,
                    "total_liab": 400.0,
                }
            ]
        )


if __name__ == "__main__":
    unittest.main()
