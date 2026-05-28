from __future__ import annotations

import unittest

import pandas as pd

from sats.screening.base import ScreeningInput
from sats.screening.registry import get_rule
from sats.screening.rules.chan_composite import ChanCompositeRule
from tests.fixtures import (
    make_chan_center_low_daily,
    make_chan_daily,
    make_chan_first_buy_daily,
    make_chan_minute_30m,
    make_chan_second_buy_daily,
    make_daily_basic,
)


class ChanCompositeRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = ChanCompositeRule()

    def _input(self, **overrides) -> ScreeningInput:
        payload = {
            "ts_code": "000001.SZ",
            "trade_date": "20260430",
            "daily": make_chan_daily(),
            "daily_basic": make_daily_basic(),
            "stock_basic": {"name": "平安银行", "market": "主板", "exchange": "SZSE"},
            "metadata": {"minute_30m": make_chan_minute_30m(), "minute_30m_source": "tickflow_history"},
        }
        payload.update(overrides)
        return ScreeningInput(**payload)

    def test_marks_third_buy_label(self) -> None:
        result = self.rule.evaluate(self._input())

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertEqual(result.rule_name, "chan_composite")
        self.assertIn("chan_third_buy", result.matched_conditions)
        self.assertIn("三买", result.metrics["matched_chan_rules"])

    def test_marks_first_buy_label(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_first_buy_daily()))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("chan_first_buy", result.matched_conditions)
        self.assertIn("一买", result.metrics["matched_chan_rules"])

    def test_marks_second_buy_label(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_second_buy_daily()))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("chan_second_buy", result.matched_conditions)
        self.assertIn("二买", result.metrics["matched_chan_rules"])

    def test_marks_center_low_label(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_center_low_daily()))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("chan_center_oscillation_low", result.matched_conditions)
        self.assertIn("中枢低吸", result.metrics["matched_chan_rules"])

    def test_marks_second_third_overlap_when_both_pass(self) -> None:
        result = self.rule.evaluate(self._input(daily=_second_and_third_daily()))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("chan_second_buy", result.matched_conditions)
        self.assertIn("chan_third_buy", result.matched_conditions)
        self.assertIn("chan_second_third_overlap", result.matched_conditions)
        self.assertIn("二三买重合", result.metrics["matched_chan_rules"])

    def test_no_match_returns_failed_result_with_reasons(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_daily(no_pullback=True)))

        self.assertFalse(result.passed)
        self.assertIn("chan_first_buy", result.failed_conditions)
        self.assertIn("risk_flags", result.metrics)

    def test_common_filters_block_sub_rule_matches(self) -> None:
        result = self.rule.evaluate(
            self._input(
                daily=make_chan_first_buy_daily(),
                stock_basic={"name": "ST平安", "market": "主板", "exchange": "SZSE"},
            )
        )

        self.assertFalse(result.passed)
        self.assertEqual(result.metrics["matched_chan_rules"], [])
        self.assertIn("not_st", result.failed_conditions)
        self.assertIn("公共条件: 排除ST失败", result.metrics["risk_flags"])

    def test_missing_30m_data_fails_without_traceback(self) -> None:
        result = self.rule.evaluate(self._input(metadata={}))

        self.assertFalse(result.passed)
        self.assertIn("chan_third_buy", result.failed_conditions)
        self.assertTrue(result.metrics["risk_flags"])

    def test_registry_accepts_composite_aliases(self) -> None:
        self.assertIsInstance(get_rule("chan_composite"), ChanCompositeRule)
        self.assertIsInstance(get_rule("chan-composite"), ChanCompositeRule)
        self.assertIsInstance(get_rule("chan-stock-select"), ChanCompositeRule)


def _second_and_third_daily() -> pd.DataFrame:
    daily = make_chan_daily()
    extra = daily.copy()
    extra["close"] = extra["close"] * 0.86
    extra["open"] = extra["open"] * 0.86
    extra["high"] = extra["high"] * 0.86
    extra["low"] = extra["low"] * 0.86
    combined = pd.concat([extra.iloc[:35], daily], ignore_index=True)
    combined = combined.tail(80).reset_index(drop=True)
    combined["trade_date"] = make_chan_second_buy_daily()["trade_date"].tail(len(combined)).to_list()
    return combined


if __name__ == "__main__":
    unittest.main()
