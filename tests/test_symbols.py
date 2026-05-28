from __future__ import annotations

import unittest

from sats.data.tickflow_provider import _normalize_ts_code as tickflow_normalize
from sats.data.tushare_provider import _normalize_ts_code as tushare_normalize
from sats.symbols import normalize_symbols, normalize_ts_code, parse_symbol_csv


class SymbolNormalizationTest(unittest.TestCase):
    def test_normalizes_bare_a_share_codes(self) -> None:
        cases = {
            "000001": "000001.SZ",
            "300001": "300001.SZ",
            "605300": "605300.SH",
            "688001": "688001.SH",
            "430047": "430047.BJ",
            "830000": "830000.BJ",
            "000001.sz": "000001.SZ",
        }

        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_ts_code(raw), expected)

    def test_batch_parsing_deduplicates_and_preserves_order(self) -> None:
        self.assertEqual(parse_symbol_csv("000001,605300,000001.SZ"), ["000001.SZ", "605300.SH"])
        self.assertEqual(normalize_symbols(["605300", "000001", "605300.SH"]), ["605300.SH", "000001.SZ"])

    def test_empty_input_can_be_required_or_optional(self) -> None:
        with self.assertRaisesRegex(ValueError, "At least one symbol"):
            parse_symbol_csv("")
        self.assertEqual(parse_symbol_csv("", required=False), [])

    def test_provider_wrappers_share_same_normalization(self) -> None:
        self.assertEqual(tickflow_normalize("000001"), "000001.SZ")
        self.assertEqual(tushare_normalize("605300"), "605300.SH")


if __name__ == "__main__":
    unittest.main()
