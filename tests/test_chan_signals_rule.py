from __future__ import annotations

import unittest
from unittest.mock import patch

from sats.chan.engine import ChanSignal
from sats.screening.base import ScreeningInput
from sats.screening.registry import get_rule
from sats.screening.rules.chan_signals import ChanSignalsRule
from tests.fixtures import (
    make_chan_center_high_daily,
    make_chan_daily,
    make_chan_first_buy_daily,
    make_chan_first_sell_daily,
    make_chan_minute_30m,
    make_chan_second_sell_daily,
    make_chan_sell_minute_30m,
    make_chan_third_sell_daily,
    make_daily_basic,
)


class ChanSignalsRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = ChanSignalsRule()

    def _input(self, *, daily, minute) -> ScreeningInput:
        return ScreeningInput(
            ts_code="000001.SZ",
            trade_date="20260430",
            daily=daily,
            daily_basic=make_daily_basic(),
            stock_basic={"name": "平安银行", "market": "主板", "exchange": "SZSE"},
            metadata={"minute_30m": minute, "minute_30m_source": "tickflow_history"},
        )

    def test_marks_existing_buy_signals(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_daily(), minute=make_chan_minute_30m()))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("三买", result.metrics["matched_chan_rules"])
        self.assertIn("持股等待", result.metrics["matched_chan_rules"])
        self.assertTrue(result.metrics["evidence_refs"])

    def test_marks_first_buy_signal(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_first_buy_daily(), minute=make_chan_minute_30m()))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("一买", result.metrics["matched_chan_rules"])

    def test_marks_sell_signals(self) -> None:
        cases = [
            (make_chan_first_sell_daily(), make_chan_sell_minute_30m(), "一卖"),
            (make_chan_second_sell_daily(), make_chan_sell_minute_30m(), "二卖"),
            (make_chan_third_sell_daily(), make_chan_sell_minute_30m(base=9.62), "三卖"),
            (make_chan_center_high_daily(), make_chan_sell_minute_30m(), "中枢高抛"),
        ]
        for daily, minute, label in cases:
            with self.subTest(label=label):
                result = self.rule.evaluate(self._input(daily=daily, minute=minute))
                self.assertTrue(result.passed, result.failed_conditions)
                self.assertIn(label, result.metrics["matched_chan_rules"])
                self.assertIn("持币等待", result.metrics["matched_chan_rules"])

    def test_missing_30m_data_fails_without_traceback(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_chan_daily(), minute=None))

        self.assertFalse(result.passed)
        self.assertTrue(result.failed_conditions)
        self.assertIn("risk_flags", result.metrics)

    def test_buy_sell_conflict_is_marked(self) -> None:
        fake_signals = [
            _fake_signal("chan_first_buy", "一买", "buy"),
            _fake_signal("chan_first_sell", "一卖", "sell"),
        ]
        with patch("sats.screening.rules.chan_signals.evaluate_chan_signals", return_value=fake_signals):
            result = self.rule.evaluate(self._input(daily=make_chan_daily(), minute=make_chan_minute_30m()))

        self.assertTrue(result.passed)
        self.assertIn("buy_sell_signal_conflict", result.metrics["conflict_flags"])

    def test_registry_accepts_signal_aliases(self) -> None:
        self.assertIsInstance(get_rule("chan_signals"), ChanSignalsRule)
        self.assertIsInstance(get_rule("chan-signals"), ChanSignalsRule)
        self.assertIsInstance(get_rule("chan-ai-select"), ChanSignalsRule)


def _fake_signal(name: str, label: str, side: str) -> ChanSignal:
    return ChanSignal(
        signal_name=name,
        label=label,
        side=side,
        passed=True,
        score=70.0,
        level="test",
        matched_conditions=[name],
        failed_conditions=[],
        watch_levels={},
        risk_flags=[],
        evidence_refs=[],
        metrics={},
    )


if __name__ == "__main__":
    unittest.main()
