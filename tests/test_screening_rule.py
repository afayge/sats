from __future__ import annotations

import importlib
import sys
import unittest

import pandas as pd

from sats.screening.base import ScreeningInput
from sats.screening.registry import get_rule, list_rules
from sats.screening.rule_composer import compose_rule_generation_plan, default_generated_rules_dir, generate_rule_code
from sats.screening.rules.ma_volume_relative_strength import MaVolumeRelativeStrengthRule
from sats.screening.rules.monthly_base_breakout import MonthlyBaseBreakoutRule

from tests.fixtures import make_benchmark, make_daily_basic, make_monthly_base_breakout, make_passing_daily


class MaVolumeRelativeStrengthRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = MaVolumeRelativeStrengthRule()

    def _input(self, **overrides) -> ScreeningInput:
        payload = {
            "ts_code": "000001.SZ",
            "trade_date": "20260430",
            "daily": make_passing_daily(),
            "daily_basic": make_daily_basic(),
            "industry_daily": make_benchmark(),
            "fallback_index_daily": None,
        }
        payload.update(overrides)
        return ScreeningInput(**payload)

    def test_passes_all_core_conditions(self) -> None:
        result = self.rule.evaluate(self._input())

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertEqual(result.rule_name, "ma_volume_relative_strength")
        self.assertIn("close_above_ma5_3d", result.matched_conditions)
        self.assertIn("ma_bull_stack_5_10_20_60", result.matched_conditions)
        self.assertGreater(result.score, 70)

    def test_volume_ratio_boundaries_pass(self) -> None:
        low = self.rule.evaluate(self._input(daily=make_passing_daily(latest_volume=1200)))
        high = self.rule.evaluate(self._input(daily=make_passing_daily(latest_volume=2000)))

        self.assertTrue(low.passed, low.failed_conditions)
        self.assertTrue(high.passed, high.failed_conditions)
        self.assertAlmostEqual(low.metrics["volume_ratio_5d"], 1.2)
        self.assertAlmostEqual(high.metrics["volume_ratio_5d"], 2.0)

    def test_platform_breakout_allows_slightly_higher_volume(self) -> None:
        result = self.rule.evaluate(self._input(daily=make_passing_daily(latest_volume=2400)))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertAlmostEqual(result.metrics["volume_ratio_5d"], 2.4)
        self.assertTrue(result.metrics["platform_breakout"])
        self.assertTrue(result.metrics["volume_breakout_allowed"])

    def test_rejects_high_volume_without_platform_breakout(self) -> None:
        daily = make_passing_daily(latest_volume=2400)
        spike = daily.index[-11]
        daily.loc[spike, "close"] = daily.loc[daily.index[-1], "close"] + 0.3
        daily.loc[spike, "high"] = daily.loc[spike, "close"] + 0.03
        daily.loc[spike, "low"] = daily.loc[spike, "close"] - 0.17

        result = self.rule.evaluate(self._input(daily=daily))

        self.assertFalse(result.passed)
        self.assertIn("volume_ratio_1p2_to_2_or_breakout", result.failed_conditions)
        self.assertFalse(result.metrics["platform_breakout"])

    def test_removed_old_filters_no_longer_block_the_rule(self) -> None:
        daily = make_passing_daily()
        latest = daily.index[-1]
        close = daily.loc[latest, "close"]
        daily.loc[latest, "open"] = close + 0.05
        daily.loc[latest, "high"] = close + 0.6
        daily.loc[latest, "low"] = close - 0.8

        result = self.rule.evaluate(
            self._input(
                daily=daily,
                daily_basic=make_daily_basic(turnover_rate=1.0, circ_mv=10_000.0),
                industry_daily=make_benchmark(strong=True),
            )
        )

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertNotIn("no_bearish_long_upper_shadow", result.failed_conditions)
        self.assertNotIn("relative_strength_positive", result.failed_conditions)
        self.assertNotIn("float_mv_50_to_200_yi", result.failed_conditions)
        self.assertNotIn("turnover_rate_in_cap_range", result.failed_conditions)

    def test_rejects_when_latest_data_is_not_requested_trade_date(self) -> None:
        result = self.rule.evaluate(
            self._input(
                trade_date="20260514",
                daily=make_passing_daily(end="20260513"),
            )
        )

        self.assertFalse(result.passed)
        self.assertIn("daily_trade_date_current", result.failed_conditions)
        self.assertEqual(result.metrics["latest_daily_trade_date"], "20260513")


class MonthlyBaseBreakoutRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rule = MonthlyBaseBreakoutRule()

    def _input(self, monthly=None, **overrides) -> ScreeningInput:
        payload = {
            "ts_code": "000001.SZ",
            "trade_date": "20260430",
            "daily": make_passing_daily(),
            "daily_basic": pd.DataFrame(),
            "stock_basic": {"name": "测试股份"},
            "metadata": {
                "monthly_1M": monthly if monthly is not None else make_monthly_base_breakout(),
                "monthly_1M_source": "test_monthly",
            },
        }
        payload.update(overrides)
        return ScreeningInput(**payload)

    def test_passes_early_breakout(self) -> None:
        result = self.rule.evaluate(self._input())

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertEqual(result.rule_name, "monthly_base_breakout")
        self.assertIn("early_breakout", result.metrics["matched_stages"])
        self.assertIn("early_or_confirmed_stage", result.matched_conditions)

    def test_passes_confirmed_run(self) -> None:
        result = self.rule.evaluate(self._input(monthly=make_monthly_base_breakout(stage="confirmed_run")))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("confirmed_run", result.metrics["matched_stages"])
        self.assertGreaterEqual(result.metrics["latest_premium"], 0.35)

    def test_rejects_without_neckline_touches(self) -> None:
        result = self.rule.evaluate(self._input(monthly=make_monthly_base_breakout(no_neckline=True)))

        self.assertFalse(result.passed)
        self.assertIn("neckline_touches_gte_2", result.failed_conditions)

    def test_rejects_without_deep_pullbacks(self) -> None:
        result = self.rule.evaluate(self._input(monthly=make_monthly_base_breakout(shallow_pullbacks=True)))

        self.assertFalse(result.passed)
        self.assertIn("pullback_lows_gte_2", result.failed_conditions)

    def test_rejects_short_monthly_window(self) -> None:
        monthly = make_monthly_base_breakout().tail(40)
        result = self.rule.evaluate(self._input(monthly=monthly))

        self.assertFalse(result.passed)
        self.assertIn("monthly_window_gte_60", result.failed_conditions)

    def test_does_not_filter_st_or_bse_stocks(self) -> None:
        result = self.rule.evaluate(
            self._input(stock_basic={"name": "ST样本", "market": "北交所", "exchange": "BSE"})
        )

        self.assertTrue(result.passed, result.failed_conditions)

    def test_registry_accepts_hyphen_alias(self) -> None:
        rule = get_rule("monthly-base-breakout")

        self.assertEqual(rule.name, "monthly_base_breakout")


class GeneratedScreeningRuleTest(unittest.TestCase):
    def _input(self) -> ScreeningInput:
        return ScreeningInput(
            ts_code="000001.SZ",
            trade_date="20260430",
            daily=make_passing_daily(latest_volume=1800),
            daily_basic=make_daily_basic(),
            stock_basic={"name": "测试股份"},
            industry_daily=make_benchmark(),
        )

    def test_composer_generates_valid_rule_code(self) -> None:
        rule_name = "nl_unit_low_volume_breakout"
        plan = compose_rule_generation_plan(
            f"新增一个低位放量突破筛选规则 rule_name: {rule_name}",
            existing_rule_names=list_rules(),
        )

        self.assertTrue(plan.ready, plan.questions)
        self.assertEqual(plan.rule_name, rule_name)
        self.assertIn("低位", "\n".join(condition["label"] for condition in plan.conditions))

        generated_dir = default_generated_rules_dir()
        path = generated_dir / f"{rule_name}.py"
        module_name = f"sats.screening.rules.generated.{rule_name}"
        path.unlink(missing_ok=True)
        sys.modules.pop(module_name, None)
        try:
            result = generate_rule_code(plan)
            importlib.invalidate_caches()

            self.assertEqual(result.path, path)
            self.assertIn(rule_name, list_rules())
            rule = get_rule(rule_name.replace("_", "-"))
            screening_result = rule.evaluate(self._input())
            self.assertEqual(screening_result.rule_name, rule_name)
            self.assertIsInstance(screening_result.passed, bool)
            self.assertIn("generated_rule", screening_result.metrics)
        finally:
            path.unlink(missing_ok=True)
            sys.modules.pop(module_name, None)
            importlib.invalidate_caches()


if __name__ == "__main__":
    unittest.main()
