from __future__ import annotations

import unittest

import pandas as pd

from sats.stock_basic_lookup import resolve_stock_mentions, resolve_symbol_or_name_values, stock_basic_rows_to_documents


def _stock_basic() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"ts_code": "000938.SZ", "symbol": "000938", "name": "紫光股份", "industry": "计算机", "market": "主板", "exchange": "SZSE"},
            {"ts_code": "600519.SH", "symbol": "600519", "name": "贵州茅台", "industry": "白酒", "market": "主板", "exchange": "SSE"},
            {"ts_code": "600000.SH", "symbol": "600000", "name": "浦发银行", "industry": "银行", "market": "主板", "exchange": "SSE"},
            {"ts_code": "000001.SZ", "symbol": "000001", "name": "平安银行", "industry": "银行", "market": "主板", "exchange": "SZSE"},
        ]
    )


class StockBasicLookupTest(unittest.TestCase):
    def test_resolves_mixed_stock_names_and_codes(self) -> None:
        symbols = resolve_symbol_or_name_values(["紫光股份", "600519"], _stock_basic())

        self.assertEqual(symbols, ["000938.SZ", "600519.SH"])

    def test_ambiguous_stock_name_requires_code(self) -> None:
        with self.assertRaisesRegex(ValueError, "匹配到多个结果"):
            resolve_symbol_or_name_values(["银行"], _stock_basic())

    def test_resolves_stock_mentions_in_message_order(self) -> None:
        stock_basic = pd.concat(
            [
                _stock_basic(),
                pd.DataFrame(
                    [
                        {"ts_code": "688700.SH", "symbol": "688700", "name": "东威科技"},
                        {"ts_code": "688559.SH", "symbol": "688559", "name": "海目星"},
                    ]
                ),
            ],
            ignore_index=True,
        )

        symbols = resolve_stock_mentions("东威科技 和 海目星 公司介绍", stock_basic)

        self.assertEqual(symbols, ["688700.SH", "688559.SH"])

    def test_stock_basic_rows_become_searchable_documents(self) -> None:
        documents = stock_basic_rows_to_documents(_stock_basic())

        self.assertIn("股票名称: 紫光股份", documents[0]["content"])
        self.assertIn("股票代码: 000938.SZ", documents[0]["content"])
        self.assertEqual(documents[0]["title"], "紫光股份 000938.SZ")


if __name__ == "__main__":
    unittest.main()
