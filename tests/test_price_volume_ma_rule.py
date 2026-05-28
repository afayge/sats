from __future__ import annotations

import unittest

from sats.screening.base import ScreeningInput
from sats.screening.registry import get_rule
from sats.screening.rules.price_volume_ma import PriceVolumeMaRule

from tests.fixtures import make_daily_basic, make_price_volume_daily


class PriceVolumeMaRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = PriceVolumeMaRule()

    def _input(self, **overrides) -> ScreeningInput:
        payload = {
            "ts_code": "000001.SZ",
            "trade_date": "20260430",
            "daily": make_price_volume_daily(),
            "daily_basic": make_daily_basic(),
            "stock_basic": {"name": "平安银行", "market": "主板", "exchange": "SZSE"},
        }
        payload.update(overrides)
        return ScreeningInput(**payload)

    def test_passes_all_core_conditions(self) -> None:
        result = self.rule.evaluate(self._input())

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertEqual(result.rule_name, "price_volume_ma")
        self.assertIn("pct_chg_3_to_5", result.matched_conditions)
        self.assertIn("volume_ratio_gt_1", result.matched_conditions)
        self.assertIn("ma_bull_stack_5_10_20_60", result.matched_conditions)

    def test_pct_chg_boundaries(self) -> None:
        low = self.rule.evaluate(self._input(daily=make_price_volume_daily(pct_chg=3.0)))
        high = self.rule.evaluate(self._input(daily=make_price_volume_daily(pct_chg=5.0)))
        below = self.rule.evaluate(self._input(daily=make_price_volume_daily(pct_chg=2.99)))
        above = self.rule.evaluate(self._input(daily=make_price_volume_daily(pct_chg=5.01)))

        self.assertTrue(low.passed, low.failed_conditions)
        self.assertTrue(high.passed, high.failed_conditions)
        self.assertFalse(below.passed)
        self.assertFalse(above.passed)
        self.assertIn("pct_chg_3_to_5", below.failed_conditions)
        self.assertIn("pct_chg_3_to_5", above.failed_conditions)

    def test_volume_ratio_must_be_strictly_greater_than_one(self) -> None:
        exact = self.rule.evaluate(self._input(daily=make_price_volume_daily(latest_volume=1000.0)))
        above = self.rule.evaluate(self._input(daily=make_price_volume_daily(latest_volume=1001.0)))

        self.assertFalse(exact.passed)
        self.assertIn("volume_ratio_gt_1", exact.failed_conditions)
        self.assertTrue(above.passed, above.failed_conditions)

    def test_turnover_and_circ_mv_boundaries(self) -> None:
        low = self.rule.evaluate(self._input(daily_basic=make_daily_basic(turnover_rate=5.0, circ_mv=500_000.0)))
        high = self.rule.evaluate(self._input(daily_basic=make_daily_basic(turnover_rate=10.0, circ_mv=2_000_000.0)))
        bad_turnover = self.rule.evaluate(self._input(daily_basic=make_daily_basic(turnover_rate=4.99)))
        bad_circ_mv = self.rule.evaluate(self._input(daily_basic=make_daily_basic(circ_mv=2_000_001.0)))

        self.assertTrue(low.passed, low.failed_conditions)
        self.assertTrue(high.passed, high.failed_conditions)
        self.assertFalse(bad_turnover.passed)
        self.assertFalse(bad_circ_mv.passed)
        self.assertIn("turnover_rate_5_to_10", bad_turnover.failed_conditions)
        self.assertIn("circ_mv_50_to_200_yi", bad_circ_mv.failed_conditions)

    def test_rejects_non_bullish_ma_stack(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_price_volume_daily(ma_stack=False)))

        self.assertFalse(result.passed)
        self.assertIn("ma_bull_stack_5_10_20_60", result.failed_conditions)

    def test_rejects_when_latest_data_is_not_requested_trade_date(self) -> None:
        result = self.rule.evaluate(
            self._input(
                trade_date="20260514",
                daily=make_price_volume_daily(end="20260513"),
                daily_basic=make_daily_basic(end="20260513"),
            )
        )

        self.assertFalse(result.passed)
        self.assertIn("daily_trade_date_current", result.failed_conditions)
        self.assertEqual(result.metrics["latest_daily_trade_date"], "20260513")

    def test_rejects_st_and_bse_but_not_688(self) -> None:
        st = self.rule.evaluate(self._input(stock_basic={"name": "ST样本"}))
        bse = self.rule.evaluate(
            self._input(ts_code="430047.BJ", stock_basic={"name": "北交样本", "exchange": "BSE"})
        )
        star = self.rule.evaluate(
            self._input(ts_code="688001.SH", stock_basic={"name": "科创样本", "exchange": "SSE"})
        )

        self.assertFalse(st.passed)
        self.assertFalse(bse.passed)
        self.assertTrue(star.passed, star.failed_conditions)
        self.assertIn("not_st", st.failed_conditions)
        self.assertIn("not_bse", bse.failed_conditions)

    def test_registry_accepts_canonical_and_hyphen_alias(self) -> None:
        self.assertIsInstance(get_rule("price_volume_ma"), PriceVolumeMaRule)
        self.assertIsInstance(get_rule("price-volume-ma"), PriceVolumeMaRule)

    def test_uses_precomputed_union_metadata_when_present(self) -> None:
        result = self.rule.evaluate(
            self._input(
                metadata={
                    "data_source": "tushare_daily",
                    "price_volume_ma": {
                        "selected": True,
                        "selection_source": "current+legacy",
                        "current_logic_passed": True,
                        "legacy_logic_passed": True,
                        "raw_metrics": {
                            "current": {
                                "pct_chg": 4.0,
                                "volume_ratio": 1.2,
                                "turnover_rate": 8.0,
                                "circ_mv": 1_200_000.0,
                                "ma5": 10.0,
                                "ma10": 9.0,
                                "ma20": 8.0,
                                "ma60": 7.0,
                            }
                        },
                    },
                }
            )
        )

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertEqual(result.metrics["selection_source"], "current+legacy")
        self.assertTrue(result.metrics["current_logic_passed"])
        self.assertTrue(result.metrics["legacy_logic_passed"])

    def test_precomputed_union_metadata_can_reject(self) -> None:
        result = self.rule.evaluate(
            self._input(
                metadata={
                    "price_volume_ma": {
                        "selected": False,
                        "selection_source": "",
                        "current_logic_passed": False,
                        "legacy_logic_passed": False,
                        "raw_metrics": {},
                    }
                }
            )
        )

        self.assertFalse(result.passed)
        self.assertIn("price_volume_ma_union_selected", result.failed_conditions)


if __name__ == "__main__":
    unittest.main()
