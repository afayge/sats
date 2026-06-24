from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from sats.agent import AgentExecutionPolicy
from sats.agent.tools import AgentToolContext, build_default_tool_registry
from sats.catalog import build_capability_catalog
from sats.data.akshare_datasets import list_akshare_datasets
from sats.data.astock_operations import (
    ASTOCK_PUBLIC_METHOD_MAPPINGS,
    execute_astock_operation,
    list_astock_capabilities,
)
from sats.data.astock_provider import AStockDataProvider
from sats.data.tickflow_provider import TickFlowDataProvider
from sats.data.tushare_stock_datasets import list_tushare_datasets
from sats.repl import CLI_COMMANDS, repl_command_to_argv


class CapabilityCatalogTest(unittest.TestCase):
    def test_provider_catalog_contains_all_registered_datasets_and_tickflow_methods(self) -> None:
        all_rows = list_astock_capabilities(limit=1)
        tushare = list_astock_capabilities(provider="tushare", limit=1)
        akshare = list_astock_capabilities(provider="akshare", limit=1)
        tickflow = list_astock_capabilities(provider="tickflow", limit=100)

        self.assertEqual(tushare["total"], len(list_tushare_datasets(include_deprecated=True)))
        self.assertEqual(akshare["total"], len(list_akshare_datasets()))
        self.assertEqual(all_rows["total"], tushare["total"] + akshare["total"] + tickflow["total"] + 31)

        public_tickflow = {
            name
            for name, value in TickFlowDataProvider.__dict__.items()
            if callable(value) and not name.startswith("_")
        }
        operation_methods = {
            item["operation"].split(".", 1)[1]
            for item in tickflow["items"]
        }
        expected_aliases = {
            "load_universe_symbols": "universe_symbols",
            "load_instruments": "instruments",
            "load_stock_basic": "stock_basic",
            "load_indicator_inputs": "indicator_inputs",
            "load_realtime_quotes": "realtime_quotes",
            "load_klines": "klines",
            "load_historical_daily_klines": "historical_daily_klines",
            "load_realtime_daily_quotes": "realtime_daily_quotes",
            "load_current_klines": "current_klines",
            "load_realtime_daily_basic_like": "realtime_daily_basic_like",
            "load_intraday_timeshare": "intraday_timeshare",
            "load_market_depth": "market_depth",
            "load_ex_factors": "ex_factors",
            "load_realtime_minute_klines": "realtime_minute_klines",
            "load_historical_minute_klines": "historical_minute_klines",
            "list_universes": "universes",
        }
        self.assertEqual(public_tickflow, set(expected_aliases))
        self.assertEqual(set(expected_aliases.values()), operation_methods)

    def test_all_public_astock_methods_have_an_explicit_capability_mapping(self) -> None:
        public_methods = {
            name
            for name, value in AStockDataProvider.__dict__.items()
            if callable(value) and not name.startswith("_")
        }
        self.assertEqual(public_methods - set(ASTOCK_PUBLIC_METHOD_MAPPINGS), set())

    def test_tickflow_only_interfaces_are_reachable_through_astock_facade(self) -> None:
        class TickFlowBackend:
            def list_universes(self):
                return pd.DataFrame([{"universe_id": "CN_Equity_A"}])

            def load_universe_symbols(self, universe_id):
                return ["000001"]

            def load_instruments(self, symbols):
                return pd.DataFrame([{"ts_code": symbols[0], "name": "平安银行"}])

            def load_klines(self, symbols, **kwargs):
                return pd.DataFrame([{"ts_code": symbols[0], "close": 10.0}])

            def load_realtime_daily_basic_like(self, symbols, **kwargs):
                return pd.DataFrame([{"ts_code": symbols[0], "turnover_rate": 1.0}])

            def load_intraday_timeshare(self, symbols, **kwargs):
                return pd.DataFrame([{"ts_code": symbols[0], "close": 10.0}])

            def load_market_depth(self, symbols):
                return pd.DataFrame([{"ts_code": symbols[0], "bid_price": 9.9}])

            def load_ex_factors(self, symbols, **kwargs):
                return pd.DataFrame([{"ts_code": symbols[0], "factor": 1.0}])

        provider = AStockDataProvider(
            SimpleNamespace(),
            tickflow_provider=TickFlowBackend(),
            tushare_provider=SimpleNamespace(),
            akshare_provider=SimpleNamespace(),
        )

        self.assertFalse(provider.list_universes().empty)
        self.assertEqual(provider.load_universe_symbols(), ["000001.SZ"])
        self.assertFalse(provider.load_instruments(["000001"]).empty)
        self.assertFalse(provider.load_klines(["000001"], period="1d").empty)
        self.assertFalse(provider.load_realtime_daily_basic_like(["000001"], trade_date="20260622").empty)
        self.assertFalse(provider.load_intraday_timeshare(["000001"]).empty)
        self.assertFalse(provider.load_market_depth(["000001"]).empty)
        self.assertFalse(provider.load_ex_factors(["000001"]).empty)

    def test_astock_fetch_validates_operation_dataset_fields_and_required_params(self) -> None:
        class Provider:
            def load_market_depth(self, symbols):
                frame = pd.DataFrame([{"ts_code": symbols[0], "bid_price": 10.0}])
                frame.attrs["data_source"] = "tickflow_depth"
                return frame

            def fetch_tushare_dataset(self, dataset, params=None, *, fields=None, limit=200):
                return {
                    "dataset": dataset,
                    "columns": fields or ["ts_code", "trade_date"],
                    "rows": [{"ts_code": "000001.SZ", "trade_date": "20260620"}],
                    "row_count": 1,
                    "data_source": f"tushare_{dataset}",
                    "missing_fields": [],
                }

        depth = execute_astock_operation(
            "tickflow.market_depth",
            {"symbols": ["000001"]},
            provider=Provider(),
            limit=20,
        )
        self.assertEqual(depth["data"][0]["ts_code"], "000001.SZ")
        self.assertEqual(depth["provenance"][0]["source"], "tickflow_depth")

        daily = execute_astock_operation(
            "tushare.dataset.fetch",
            {"dataset": "daily", "params": {"ts_code": "000001.SZ"}},
            fields=["ts_code", "trade_date"],
            provider=Provider(),
        )
        self.assertEqual(daily["dataset"], "daily")
        self.assertEqual(daily["rows"][0]["ts_code"], "000001.SZ")

        with self.assertRaisesRegex(ValueError, "unknown AStock operation"):
            execute_astock_operation("provider.anything", {}, provider=Provider())
        with self.assertRaisesRegex(ValueError, "unknown output fields"):
            execute_astock_operation(
                "tushare.dataset.fetch",
                {"dataset": "daily", "params": {}},
                fields=["not_a_field"],
                provider=Provider(),
            )
        with self.assertRaisesRegex(ValueError, "missing required dataset parameters"):
            execute_astock_operation(
                "akshare.dataset.fetch",
                {"dataset": "futures_foreign_commodity_realtime", "params": {}},
                provider=Provider(),
            )

    def test_astock_fetch_normalizes_minute_period_alias(self) -> None:
        captured = {}

        class Provider:
            def load_realtime_minute_klines(self, symbols, *, period="1m", count=None):
                captured["period"] = period
                frame = pd.DataFrame(
                    [
                        {
                            "ts_code": symbols[0],
                            "period": period,
                            "trade_time": "2026-05-14 10:00:00",
                            "close": 10.0,
                        }
                    ]
                )
                frame.attrs["data_source"] = "test_minute"
                return frame

        payload = execute_astock_operation(
            "astock.minute.realtime",
            {"symbols": ["000001"], "period": "30分钟", "count": 10},
            provider=Provider(),
        )

        self.assertEqual(captured["period"], "30m")
        self.assertEqual(payload["data"][0]["period"], "30m")

    def test_catalog_json_is_bounded_and_does_not_include_setting_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                project_root=Path.cwd(),
                db_path=Path(tmp) / "missing.duckdb",
                secret_marker="DO_NOT_LEAK_THIS_VALUE",
            )
            catalog = build_capability_catalog(
                settings=settings,
                section="providers",
                provider="akshare",
                query="stock",
                limit=3,
            )

        encoded = json.dumps(catalog, ensure_ascii=False)
        self.assertEqual(catalog["schema_version"], "1.0")
        self.assertLessEqual(catalog["data"]["providers"]["returned"], 3)
        self.assertNotIn("DO_NOT_LEAK_THIS_VALUE", encoded)

    def test_cli_repl_agent_tools_expose_catalog(self) -> None:
        self.assertIn("catalog", CLI_COMMANDS)
        self.assertEqual(
            repl_command_to_argv("/catalog --section providers --provider tickflow"),
            ["catalog", "--section", "providers", "--provider", "tickflow"],
        )
        registry = build_default_tool_registry()
        self.assertIn("catalog.capabilities", registry.names())
        self.assertIn("data.astock_catalog", registry.names())
        self.assertIn("data.astock_fetch", registry.names())

        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path.cwd(), db_path=Path("data/sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )
        result = registry.execute(
            "data.astock_catalog",
            {"provider": "tushare", "query": "资金流", "limit": 2},
            context,
        )
        self.assertEqual(result.status, "done")
        self.assertEqual(result.payload["astock_catalog"]["returned"], 2)

        catalog_result = registry.execute(
            "catalog.capabilities",
            {"section": "providers", "provider": "tickflow", "limit": 2},
            context,
        )
        self.assertEqual(catalog_result.status, "done")
        self.assertEqual(catalog_result.payload["catalog"]["counts"]["providers"], 16)

    def test_stock_basic_lookup_recovers_legacy_astock_fetch_name_param(self) -> None:
        registry = build_default_tool_registry()
        stock_basic = pd.DataFrame(
            [
                {"ts_code": "300046.SZ", "symbol": "300046", "name": "台基股份", "industry": "功率器件"},
                {"ts_code": "300373.SZ", "symbol": "300373", "name": "扬杰科技", "industry": "功率器件"},
            ]
        )
        context = AgentToolContext(
            settings=SimpleNamespace(project_root=Path.cwd(), db_path=Path("data/sats.duckdb")),
            storage=SimpleNamespace(),
            resolver=SimpleNamespace(),
            policy=AgentExecutionPolicy(),
            command_runner=SimpleNamespace(),
            trader=SimpleNamespace(),
        )

        with patch("sats.agent.tools.data_tools.AStockDataProvider", return_value=SimpleNamespace(load_stock_basic=lambda storage=None: stock_basic)):
            result = registry.execute(
                "data.astock_fetch",
                {"operation": "astock.stock_basic", "params": {"name": "台基股份"}, "limit": 5},
                context,
            )

        self.assertEqual(result.status, "done")
        self.assertEqual(result.data_names, ("stock_basic",))
        self.assertEqual(result.payload["sample"][0]["ts_code"], "300046.SZ")
        self.assertEqual(result.payload["sample"][0]["name"], "台基股份")

    def test_planner_context_uses_provider_summaries_not_full_dataset_catalog(self) -> None:
        payload = json.loads(build_default_tool_registry().planner_context())

        self.assertEqual({item["provider"] for item in payload["data_capabilities"]}, {"astock", "tickflow", "tushare", "akshare"})
        self.assertIn("data.astock_catalog", {item["name"] for item in payload["tools"]})
        self.assertLess(len(payload["data_capabilities"]), 10)


if __name__ == "__main__":
    unittest.main()
