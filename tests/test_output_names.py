from __future__ import annotations

import json
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from sats.output_names import SecurityNameOutput, SecurityNameResolver, enrich_security_names
from sats.storage import DuckDBStorage


class SecurityOutputNamesTest(unittest.TestCase):
    def test_plain_output_adds_stock_and_index_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                    ]
                )
            )
            resolver = SecurityNameResolver(SimpleNamespace(), db_path=db_path)

            output = enrich_security_names(
                "股票 000001.SZ 涨幅 +1.00%\n指数 000300.SH 涨幅 -0.50%\n",
                resolver,
            )

        self.assertIn("000001.SZ 平安银行", output)
        self.assertIn("000300.SH 沪深300", output)

    def test_existing_names_are_not_duplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台"},
                    ]
                )
            )
            resolver = SecurityNameResolver(SimpleNamespace(), db_path=db_path)

            output = enrich_security_names("600519.SH 贵州茅台 现价 1500.00", resolver)

        self.assertEqual(output, "600519.SH 贵州茅台 现价 1500.00")

    def test_json_output_adds_name_fields_and_stays_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                    ]
                )
            )
            resolver = SecurityNameResolver(SimpleNamespace(), db_path=db_path)

            output = enrich_security_names(
                json.dumps(
                    {
                        "stocks": [{"ts_code": "000001.SZ", "close": 10.0}],
                        "indices": [{"index_code": "000300.SH", "close": 3800.0}],
                    },
                    ensure_ascii=False,
                ),
                resolver,
            )

        payload = json.loads(output)
        self.assertEqual(payload["stocks"][0]["name"], "平安银行")
        self.assertEqual(payload["indices"][0]["name"], "沪深300")

    def test_unknown_index_name_uses_astock_provider(self) -> None:
        class Provider:
            def __init__(self, settings) -> None:
                pass

            def fetch_tushare_dataset(self, dataset, params, *, fields, limit):
                return {"rows": [{"ts_code": "000852.SH", "name": "中证1000"}]}

        with tempfile.TemporaryDirectory() as tmp:
            resolver = SecurityNameResolver(
                SimpleNamespace(),
                db_path=Path(tmp) / "sats.duckdb",
                provider_factory=Provider,
            )

            output = enrich_security_names("000852.SH 收盘 6500", resolver)

        self.assertEqual(output, "000852.SH 中证1000 收盘 6500")

    def test_fragmented_terminal_output_does_not_duplicate_existing_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "sats.duckdb"
            DuckDBStorage(db_path).upsert_stock_basic(
                pd.DataFrame(
                    [
                        {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行"},
                    ]
                )
            )
            target = io.StringIO()
            output = SecurityNameOutput(
                target,
                SecurityNameResolver(SimpleNamespace(), db_path=db_path),
            )

            output.write("000001.SZ")
            output.write(" 平安银行")
            output.write("\n")
            output.flush()

        self.assertEqual(target.getvalue(), "000001.SZ 平安银行\n")


if __name__ == "__main__":
    unittest.main()
