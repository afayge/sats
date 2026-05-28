from __future__ import annotations

import unittest

from sats.screening.base import ScreeningInput
from sats.screening.registry import get_rule
from sats.screening.rules.chan_third_buy import ChanThirdBuyRule

from tests.fixtures import make_chan_daily, make_chan_minute_30m, make_daily_basic


class ChanThirdBuyRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = ChanThirdBuyRule()

    def _input(self, **overrides) -> ScreeningInput:
        payload = {
            "ts_code": "000001.SZ",
            "trade_date": "20260430",
            "daily": make_chan_daily(),
            "daily_basic": make_daily_basic(),
            "stock_basic": {"name": "平安银行", "market": "主板", "exchange": "SZSE"},
            "metadata": {"minute_30m": make_chan_minute_30m()},
        }
        payload.update(overrides)
        return ScreeningInput(**payload)

    def test_passes_daily_third_buy_with_30m_confirmation(self) -> None:
        result = self.rule.evaluate(self._input())

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertEqual(result.rule_name, "chan_third_buy")
        self.assertIn("pullback_holds_box", result.matched_conditions)
        self.assertIn("minute_macd_hist_improving", result.matched_conditions)

    def test_rejects_when_daily_pullback_breaks_box(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_daily(pullback_breaks_box=True)))

        self.assertFalse(result.passed)
        self.assertIn("daily_third_buy_setup", result.failed_conditions)

    def test_rejects_breakout_without_pullback(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_daily(no_pullback=True)))

        self.assertFalse(result.passed)
        self.assertIn("daily_third_buy_setup", result.failed_conditions)

    def test_rejects_overheated_latest_price(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_daily(hot_latest=True)))

        self.assertFalse(result.passed)
        self.assertIn("ma20_bias_lte_12pct", result.failed_conditions)

    def test_rejects_missing_30m_data(self) -> None:
        result = self.rule.evaluate(self._input(metadata={}))

        self.assertFalse(result.passed)
        self.assertIn("minute_30m_available", result.failed_conditions)

    def test_rejects_when_30m_pullback_breaks_box(self) -> None:
        result = self.rule.evaluate(
            self._input(metadata={"minute_30m": make_chan_minute_30m(breaks_box=True)})
        )

        self.assertFalse(result.passed)
        self.assertIn("minute_pullback_holds_box", result.failed_conditions)

    def test_rejects_when_30m_macd_does_not_improve(self) -> None:
        result = self.rule.evaluate(
            self._input(metadata={"minute_30m": make_chan_minute_30m(macd_improving=False)})
        )

        self.assertFalse(result.passed)
        self.assertIn("minute_macd_hist_improving", result.failed_conditions)

    def test_registry_accepts_canonical_and_hyphen_alias(self) -> None:
        self.assertIsInstance(get_rule("chan_third_buy"), ChanThirdBuyRule)
        self.assertIsInstance(get_rule("chan-third-buy"), ChanThirdBuyRule)


if __name__ == "__main__":
    unittest.main()
