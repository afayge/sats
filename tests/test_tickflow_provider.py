from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.data.tickflow_provider import TickFlowDataProvider
from sats.storage.duckdb import DuckDBStorage


def _minute_frame(symbol: str, trade_time: str = "2026-05-14 09:31:00") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [symbol],
            "trade_time": [trade_time],
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "volume": [1200.0],
            "amount": [121200.0],
        }
    )


def _daily_frame(symbol: str) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": [symbol, symbol],
            "date": ["2026-05-13", "2026-05-14"],
            "open": [10.0, 10.2],
            "high": [10.3, 10.6],
            "low": [9.9, 10.1],
            "close": [10.1, 10.5],
            "volume": [100000.0, 140000.0],
            "amount": [1010000.0, 1470000.0],
        }
    )


class FakeKlines:
    def __init__(self, *, fail_batch: bool | Exception = False, fail_intraday=None) -> None:
        self.fail_batch = fail_batch
        self.fail_intraday = list(fail_intraday or [])
        self.intraday_batch_calls: list[dict] = []
        self.batch_calls: list[dict] = []
        self.intraday_calls: list[dict] = []
        self.get_calls: list[dict] = []

    def intraday_batch(self, symbols, **kwargs):
        self.intraday_batch_calls.append({"symbols": list(symbols), **kwargs})
        if isinstance(self.fail_batch, Exception):
            raise self.fail_batch
        if self.fail_batch:
            raise RuntimeError("batch failed")
        return {symbol: _minute_frame(symbol) for symbol in symbols}

    def batch(self, symbols, **kwargs):
        self.batch_calls.append({"symbols": list(symbols), **kwargs})
        if self.fail_batch:
            raise RuntimeError("batch failed")
        if kwargs.get("period") == "1d":
            return {symbol: _daily_frame(symbol) for symbol in symbols}
        return {symbol: _minute_frame(symbol, "2026-05-13 10:00:00") for symbol in symbols}

    def intraday(self, symbol, **kwargs):
        self.intraday_calls.append({"symbol": symbol, **kwargs})
        if self.fail_intraday:
            exc = self.fail_intraday.pop(0)
            if exc is not None:
                raise exc
        return _minute_frame(symbol)

    def get(self, symbol, **kwargs):
        self.get_calls.append({"symbol": symbol, **kwargs})
        if kwargs.get("period") == "1d":
            return _daily_frame(symbol)
        return _minute_frame(symbol, "2026-05-13 10:00:00")

    def ex_factors(self, symbols, **kwargs):
        return pd.DataFrame(
            [
                {"symbol": symbol, "timestamp": 1778716800000, "trade_date": "2026-05-14", "ex_factor": 1.02}
                for symbol in symbols
            ]
        )


class FakeUniverses:
    def list(self):
        return [{"id": "CN_Equity_A", "name": "A股", "region": "CN", "category": "equity", "symbol_count": 2}]

    def get(self, universe_id):
        return {"id": universe_id, "symbols": ["000001.SZ", "600519.SH"]}


class FakeInstruments:
    def __init__(self) -> None:
        self.batch_calls: list[list[str]] = []

    def batch(self, symbols):
        self.batch_calls.append(list(symbols))
        return [
            {
                "symbol": symbol,
                "code": symbol.split(".")[0],
                "name": f"名称{index}",
                "exchange": symbol.split(".")[1],
                "region": "CN",
                "ext": {"listing_date": "1991-04-03"},
            }
            for index, symbol in enumerate(symbols, start=1)
        ]


class FakeQuotes:
    def __init__(self) -> None:
        self.get_by_symbols_calls: list[list[str]] = []

    def get_by_symbols(self, symbols, *, as_dataframe=False):
        self.get_by_symbols_calls.append(list(symbols))
        frame = pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "open": 10.0,
                    "high": 10.8,
                    "low": 9.9,
                    "last_price": 10.5,
                    "prev_close": 10.0,
                    "volume": 150000.0,
                    "amount": 1575000.0,
                }
                for symbol in symbols
            ]
        )
        return frame if as_dataframe else frame.to_dict("records")

    def get_by_universes(self, universes, *, as_dataframe=False):
        return self.get_by_symbols(["000001.SZ", "600519.SH"], as_dataframe=as_dataframe)


class FakeSdkQuotes:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def get(self, *, symbols=None, universes=None, as_dataframe=False):
        self.calls.append({"symbols": list(symbols or []), "universes": list(universes or [])})
        selected = symbols or ["000001.SZ", "600519.SH"]
        return FakeQuotes().get_by_symbols(list(selected), as_dataframe=as_dataframe)


class FakeDepth:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, symbol):
        self.calls.append(symbol)
        return {
            "symbol": symbol,
            "timestamp": 1778716800000,
            "bid_prices": [10.1, 10.0, 9.9, 9.8, 9.7],
            "bid_volumes": [1000, 2000, 3000, 4000, 5000],
            "ask_prices": [10.2, 10.3, 10.4, 10.5, 10.6],
            "ask_volumes": [1100, 2100, 3100, 4100, 5100],
        }


class FakeFinancials:
    def shares(self, symbols, **kwargs):
        return pd.DataFrame(
            [
                {
                    "symbol": symbol,
                    "period_end": "2026-03-31",
                    "float_shares": 150_000_000.0,
                    "total_shares": 200_000_000.0,
                }
                for symbol in symbols
            ]
        )


class FakeClient:
    def __init__(self, *, fail_batch: bool | Exception = False, fail_intraday=None) -> None:
        self.klines = FakeKlines(fail_batch=fail_batch, fail_intraday=fail_intraday)
        self.universes = FakeUniverses()
        self.instruments = FakeInstruments()
        self.quotes = FakeQuotes()
        self.depth = FakeDepth()
        self.financials = FakeFinancials()


class FakeSdkQuoteClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.quotes = FakeSdkQuotes()


class FakeTickFlowPermissionError(Exception):
    status_code = 403


class FakeTickFlowRateLimitError(Exception):
    status_code = 429


def _settings():
    return SimpleNamespace(tickflow_api_key="", tickflow_base_url="https://api.tickflow.org")


class TickFlowProviderTest(unittest.TestCase):
    def _provider(self, client: FakeClient) -> TickFlowDataProvider:
        return TickFlowDataProvider(_settings(), client=client, sleep=lambda _: None, clock=lambda: 0.0)

    def test_init_passes_timeout_and_retries_to_sdk(self) -> None:
        settings = SimpleNamespace(
            tickflow_api_key="key",
            tickflow_base_url="https://api.tickflow.org",
            tickflow_timeout_seconds=12,
            tickflow_max_retries=4,
        )

        with patch("sats.data.tickflow_provider.TickFlow", return_value=FakeClient()) as tickflow:
            provider = TickFlowDataProvider(settings)

        self.assertIsInstance(provider.client, FakeClient)
        tickflow.assert_called_once_with(
            api_key="key",
            base_url="https://api.tickflow.org",
            timeout=12,
            max_retries=4,
        )

    def test_realtime_minute_klines_support_all_periods(self) -> None:
        for period in ["1m", "5m", "15m", "30m", "60m"]:
            client = FakeClient()
            provider = self._provider(client)

            frame = provider.load_realtime_minute_klines(["000001.SZ"], period=period, count=20)

            self.assertEqual(frame.iloc[0]["period"], period)
            self.assertEqual(frame.iloc[0]["trade_date"], "20260514")
            self.assertEqual(frame.iloc[0]["vol"], 12.0)
            self.assertEqual(frame.iloc[0]["amount"], 121.2)
            self.assertEqual(frame.iloc[0]["data_source"], "tickflow")
            self.assertEqual(client.klines.intraday_batch_calls[0]["period"], period)
            self.assertEqual(client.klines.intraday_batch_calls[0]["count"], 20)

    def test_rejects_invalid_period(self) -> None:
        provider = self._provider(FakeClient())

        with self.assertRaisesRegex(ValueError, "Unsupported minute K period"):
            provider.load_realtime_minute_klines(["000001.SZ"], period="10m")

    def test_batch_requests_are_chunked_by_100_symbols(self) -> None:
        client = FakeClient()
        provider = self._provider(client)
        symbols = [f"{index:06d}.SZ" for index in range(250)]

        frame = provider.load_realtime_minute_klines(symbols, period="1m")

        sizes = [len(call["symbols"]) for call in client.klines.intraday_batch_calls]
        self.assertEqual(sizes, [100, 100, 50])
        self.assertEqual(len(frame), 250)
        self.assertEqual(client.klines.intraday_calls, [])
        self.assertEqual(frame.attrs["tickflow_source"], "tickflow_intraday_batch")

    def test_realtime_minute_batch_limiter_uses_30_per_minute(self) -> None:
        client = FakeClient()
        sleeps: list[float] = []
        provider = TickFlowDataProvider(_settings(), client=client, sleep=sleeps.append, clock=lambda: 0.0)
        symbols = [f"{index:06d}.SZ" for index in range(250)]

        provider.load_realtime_minute_klines(symbols, period="30m")

        self.assertEqual([len(call["symbols"]) for call in client.klines.intraday_batch_calls], [100, 100, 50])
        self.assertEqual(sleeps, [2.0, 2.0])

    def test_historical_minute_klines_passes_time_window(self) -> None:
        client = FakeClient()
        provider = self._provider(client)

        frame = provider.load_historical_minute_klines(
            ["000001.SZ"],
            period="5m",
            start_time="20260501",
            end_time="20260514",
        )

        call = client.klines.batch_calls[0]
        self.assertEqual(call["period"], "5m")
        self.assertEqual(call["adjust"], "none")
        self.assertIsInstance(call["start_time"], int)
        self.assertIsInstance(call["end_time"], int)
        self.assertEqual(frame.iloc[0]["trade_date"], "20260513")

    def test_batch_failure_falls_back_to_single_symbol_requests(self) -> None:
        client = FakeClient(fail_batch=True)
        provider = self._provider(client)

        frame = provider.load_realtime_minute_klines(["000001.SZ", "600519.SH"], period="1m")

        self.assertEqual(len(client.klines.intraday_calls), 2)
        self.assertEqual(sorted(frame["ts_code"].tolist()), ["000001.SZ", "600519.SH"])

    def test_batch_permission_error_falls_back_to_single_intraday(self) -> None:
        client = FakeClient(fail_batch=FakeTickFlowPermissionError("当前套餐不支持日内批量查询"))
        provider = self._provider(client)

        frame = provider.load_realtime_minute_klines(["000001.SZ", "600519.SH"], period="30m")

        self.assertEqual(len(client.klines.intraday_calls), 2)
        self.assertEqual(frame.attrs["tickflow_source"], "tickflow_single_intraday")
        self.assertEqual(sorted(frame["ts_code"].tolist()), ["000001.SZ", "600519.SH"])

    def test_single_intraday_permission_error_is_clear(self) -> None:
        client = FakeClient(
            fail_batch=FakeTickFlowPermissionError("当前套餐不支持日内批量查询"),
            fail_intraday=[FakeTickFlowPermissionError("当前套餐不支持日内分时")],
        )
        provider = self._provider(client)

        with self.assertRaisesRegex(ValueError, "当前套餐不支持实时分钟K线"):
            provider.load_realtime_minute_klines(["000001.SZ"], period="30m")

    def test_single_intraday_rate_limit_waits_once_and_retries(self) -> None:
        client = FakeClient(
            fail_batch=FakeTickFlowPermissionError("当前套餐不支持日内批量查询"),
            fail_intraday=[FakeTickFlowRateLimitError("日内分时限流 (30/min)，请 17270ms 后重试")],
        )
        sleeps: list[float] = []
        provider = TickFlowDataProvider(_settings(), client=client, sleep=sleeps.append, clock=lambda: 0.0)

        frame = provider.load_realtime_minute_klines(["000001.SZ"], period="30m")

        self.assertEqual(len(client.klines.intraday_calls), 2)
        self.assertTrue(any(abs(delay - 17.27) < 0.01 for delay in sleeps))
        self.assertEqual(frame.iloc[0]["ts_code"], "000001.SZ")

    def test_stock_basic_can_load_from_tickflow_universe_and_instruments(self) -> None:
        provider = self._provider(FakeClient())

        frame = provider.load_stock_basic()

        self.assertEqual(frame["ts_code"].tolist(), ["000001.SZ", "600519.SH"])
        self.assertEqual(frame.iloc[0]["exchange"], "SZSE")
        self.assertEqual(frame.iloc[1]["exchange"], "SSE")

    def test_universe_and_instrument_helpers(self) -> None:
        provider = self._provider(FakeClient())

        universes = provider.list_universes()
        symbols = provider.load_universe_symbols("CN_Equity_A")
        instruments = provider.load_instruments(["000001.SZ"])

        self.assertEqual(universes.iloc[0]["id"], "CN_Equity_A")
        self.assertEqual(symbols, ["000001.SZ", "600519.SH"])
        self.assertEqual(instruments.iloc[0]["ts_code"], "000001.SZ")
        self.assertEqual(instruments.iloc[0]["list_date"], "19910403")

    def test_historical_daily_klines_can_be_cached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            storage = DuckDBStorage(Path(tmp) / "sats.duckdb")
            provider = self._provider(FakeClient())

            frame = provider.load_historical_daily_klines(
                ["000001.SZ"],
                start_date="20260513",
                end_date="20260514",
                storage=storage,
            )
            rows = storage.get_stock_daily(["20260514"])

            self.assertEqual(frame.iloc[-1]["trade_date"], "20260514")
            self.assertAlmostEqual(float(frame.iloc[-1]["pct_chg"]), (10.5 / 10.1 - 1.0) * 100)
            self.assertEqual(len(rows), 1)

    def test_load_klines_supports_non_daily_periods_and_rejects_invalid_period(self) -> None:
        provider = self._provider(FakeClient())

        for period in ["1d", "1w", "1M", "1Q", "1Y"]:
            frame = provider.load_klines(["000001.SZ"], period=period, count=2)
            self.assertEqual(frame.iloc[0]["period"], period)

        with self.assertRaisesRegex(ValueError, "Unsupported K-line period"):
            provider.load_klines(["000001.SZ"], period="10m")

    def test_realtime_daily_quotes_use_1m_minute_kline(self) -> None:
        provider = self._provider(FakeClient())

        frame = provider.load_realtime_daily_quotes(["000001.SZ"], trade_date="20260514")

        self.assertEqual(frame.iloc[0]["ts_code"], "000001.SZ")
        self.assertAlmostEqual(float(frame.iloc[0]["close"]), 10.1)
        self.assertAlmostEqual(float(frame.iloc[0]["pct_chg"]), 0.0)
        self.assertAlmostEqual(float(frame.iloc[0]["vol"]), 12.0)

    def test_realtime_quotes_can_load_by_symbols_or_universe(self) -> None:
        client = FakeClient()
        provider = self._provider(client)

        by_symbols = provider.load_realtime_quotes(symbols=["000001"])

        self.assertEqual(by_symbols.iloc[0]["data_source"], "tickflow_realtime_minute_quote")
        self.assertEqual(by_symbols.iloc[0]["close"], 10.1)
        self.assertIn("pre_close", by_symbols.columns)
        self.assertEqual(client.klines.intraday_batch_calls[0]["symbols"], ["000001.SZ"])
        self.assertEqual(client.klines.intraday_batch_calls[0]["period"], "1m")
        self.assertEqual(client.klines.intraday_batch_calls[0]["count"], 1)
        self.assertEqual(client.quotes.get_by_symbols_calls, [])

        by_universe = provider.load_realtime_quotes(universe_id="CN_Equity_A")

        self.assertEqual(by_universe["ts_code"].tolist(), ["000001.SZ", "600519.SH"])
        self.assertEqual(client.quotes.get_by_symbols_calls[0], ["000001.SZ", "600519.SH"])

    def test_realtime_quotes_support_sdk_get_shape(self) -> None:
        client = FakeSdkQuoteClient()
        provider = self._provider(client)

        by_symbols = provider.load_realtime_quotes(symbols=["000001.SZ"])
        by_universe = provider.load_realtime_quotes(universe_id="CN_Equity_A")

        self.assertEqual(by_symbols.iloc[0]["ts_code"], "000001.SZ")
        self.assertEqual(by_symbols.iloc[0]["data_source"], "tickflow_realtime_minute_quote")
        self.assertEqual(by_universe["ts_code"].tolist(), ["000001.SZ", "600519.SH"])
        self.assertEqual(client.quotes.calls[0]["universes"], ["CN_Equity_A"])

    def test_realtime_quotes_fall_back_to_quote_endpoint_when_1m_minute_fails(self) -> None:
        client = FakeClient(
            fail_batch=FakeTickFlowPermissionError("当前套餐不支持日内批量查询"),
            fail_intraday=[FakeTickFlowPermissionError("当前套餐不支持日内分时")],
        )
        provider = self._provider(client)

        frame = provider.load_realtime_quotes(symbols=["000001.SZ"])

        self.assertEqual(frame.iloc[0]["data_source"], "tickflow_quote")
        self.assertAlmostEqual(float(frame.iloc[0]["close"]), 10.5)
        self.assertEqual(client.quotes.get_by_symbols_calls[0], ["000001.SZ"])

    def test_intraday_timeshare_uses_200_symbol_batches_and_alias_source(self) -> None:
        client = FakeClient()
        sleeps: list[float] = []
        provider = TickFlowDataProvider(_settings(), client=client, sleep=sleeps.append, clock=lambda: 0.0)
        symbols = [f"{index:06d}.SZ" for index in range(250)]

        frame = provider.load_intraday_timeshare(symbols, period="1m")

        self.assertEqual([len(call["symbols"]) for call in client.klines.intraday_batch_calls], [200, 50])
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(frame["data_source"].unique().tolist(), ["tickflow_intraday_kline_alias"])

    def test_market_depth_and_ex_factors_are_standardized(self) -> None:
        provider = self._provider(FakeClient())

        depth = provider.load_market_depth(["000001.SZ"])
        factors = provider.load_ex_factors(["000001.SZ"], start_time="20260501", end_time="20260514")

        self.assertEqual(depth.iloc[0]["ts_code"], "000001.SZ")
        self.assertEqual(depth.iloc[0]["bid_vol_1"], 10.0)
        self.assertEqual(factors.iloc[0]["ts_code"], "000001.SZ")
        self.assertEqual(factors.iloc[0]["data_source"], "tickflow_ex_factor")

    def test_realtime_daily_basic_like_uses_1m_minute_quote_and_share_data(self) -> None:
        provider = self._provider(FakeClient())

        frame = provider.load_realtime_daily_basic_like(["000001.SZ"], trade_date="20260514")

        self.assertEqual(frame.iloc[0]["ts_code"], "000001.SZ")
        self.assertAlmostEqual(float(frame.iloc[0]["turnover_rate"]), 12.0 / 15000.0)
        self.assertAlmostEqual(float(frame.iloc[0]["circ_mv"]), 10.1 * 15000.0)
        self.assertEqual(frame.attrs["daily_basic_source"], "tickflow_realtime_basic_like")


if __name__ == "__main__":
    unittest.main()
