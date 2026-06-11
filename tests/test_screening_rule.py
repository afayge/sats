from __future__ import annotations

import importlib
import sys
import unittest

import pandas as pd

from sats.screening.base import ScreeningInput
from sats.screening.registry import get_rule, list_rules
from sats.screening.rule_composer import compose_rule_generation_plan, default_generated_rules_dir, generate_rule_code
from sats.screening.service import evaluate_inputs
from sats.screening.rules.ma_volume_relative_strength import MaVolumeRelativeStrengthRule
from sats.screening.rules.monthly_base_breakout import MonthlyBaseBreakoutRule

from tests.fixtures import make_benchmark, make_daily_basic, make_monthly_base_breakout, make_passing_daily, make_trade_dates


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


class SequoiaXRulesTest(unittest.TestCase):
    def _input(
        self,
        daily: pd.DataFrame,
        *,
        ts_code: str = "000001.SZ",
        trade_date: str = "20260430",
        daily_basic: pd.DataFrame | None = None,
    ) -> ScreeningInput:
        return ScreeningInput(
            ts_code=ts_code,
            trade_date=trade_date,
            daily=daily,
            daily_basic=daily_basic if daily_basic is not None else make_daily_basic(end=trade_date),
            stock_basic={"name": "测试股份"},
        )

    def test_turtle_trade_ports_breakout_and_amount_logic(self) -> None:
        rule = get_rule("TurtleTrade")

        result = rule.evaluate(self._input(_turtle_daily(), daily_basic=make_daily_basic(circ_mv=2_500_000.0)))
        failed = rule.evaluate(self._input(_turtle_daily(amount=99_999.0)))
        short = rule.evaluate(self._input(_turtle_daily().tail(20)))
        stale = rule.evaluate(self._input(_turtle_daily(end="20260429"), trade_date="20260430"))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertEqual(result.rule_name, "turtle_trade")
        self.assertEqual(result.metrics["amount"], 100_000.0)
        self.assertIn("amount_gte_100m", failed.failed_conditions)
        self.assertIn("data_window_21", short.failed_conditions)
        self.assertIn("daily_trade_date_current", stale.failed_conditions)

    def test_ma_volume_ports_golden_cross_and_volume_logic(self) -> None:
        rule = get_rule("ma-volume")

        result = rule.evaluate(self._input(_ma_volume_daily()))
        failed = rule.evaluate(self._input(_ma_volume_daily(latest_vol=1000.0)))
        short = rule.evaluate(self._input(_ma_volume_daily().tail(19)))
        stale = rule.evaluate(self._input(_ma_volume_daily(end="20260429"), trade_date="20260430"))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("ma5_crosses_above_ma20", result.matched_conditions)
        self.assertIn("volume_gt_1p5x_ma20", failed.failed_conditions)
        self.assertIn("data_window_20", short.failed_conditions)
        self.assertIn("daily_trade_date_current", stale.failed_conditions)

    def test_high_tight_flag_ports_momentum_consolidation_and_shrink(self) -> None:
        rule = get_rule("HighTightFlag")

        result = rule.evaluate(self._input(_high_tight_flag_daily()))
        failed = rule.evaluate(self._input(_high_tight_flag_daily(latest_vol=800.0)))
        short = rule.evaluate(self._input(_high_tight_flag_daily().tail(39)))
        stale = rule.evaluate(self._input(_high_tight_flag_daily(end="20260429"), trade_date="20260430"))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertGreater(result.metrics["momentum_ratio_40d"], 1.6)
        self.assertIn("volume_shrink_lt_0p6x_prior20", failed.failed_conditions)
        self.assertIn("data_window_40", short.failed_conditions)
        self.assertIn("daily_trade_date_current", stale.failed_conditions)

    def test_limit_up_shakeout_ports_limit_up_washout_logic(self) -> None:
        rule = get_rule("LimitUpShakeout")

        result = rule.evaluate(self._input(_limit_up_shakeout_daily()))
        failed = rule.evaluate(self._input(_limit_up_shakeout_daily(today_low=10.80)))
        short = rule.evaluate(self._input(_limit_up_shakeout_daily().tail(2)))
        stale = rule.evaluate(self._input(_limit_up_shakeout_daily(end="20260429"), trade_date="20260430"))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("yesterday_limit_up", result.matched_conditions)
        self.assertIn("support_low_gte_yesterday_close", failed.failed_conditions)
        self.assertIn("data_window_3", short.failed_conditions)
        self.assertIn("daily_trade_date_current", stale.failed_conditions)

    def test_uptrend_limit_down_ports_trend_limit_down_logic(self) -> None:
        rule = get_rule("UptrendLimitDown")

        result = rule.evaluate(self._input(_uptrend_limit_down_daily()))
        failed = rule.evaluate(self._input(_uptrend_limit_down_daily(down_factor=0.94)))
        short = rule.evaluate(self._input(_uptrend_limit_down_daily().tail(59)))
        stale = rule.evaluate(self._input(_uptrend_limit_down_daily(end="20260429"), trade_date="20260430"))

        self.assertTrue(result.passed, result.failed_conditions)
        self.assertIn("previous_ma20_gt_ma60", result.matched_conditions)
        self.assertIn("close_lte_90p5pct_previous_close", failed.failed_conditions)
        self.assertIn("data_window_60", short.failed_conditions)
        self.assertIn("daily_trade_date_current", stale.failed_conditions)

    def test_rps_breakout_prepares_cross_section_metadata(self) -> None:
        inputs = [
            self._input(_rps_daily("000001.SZ", gain=0.10), ts_code="000001.SZ"),
            self._input(_rps_daily("000002.SZ", gain=0.50), ts_code="000002.SZ"),
            self._input(_rps_daily("000003.SZ", gain=1.00), ts_code="000003.SZ"),
        ]

        results = evaluate_inputs(inputs, rule_name="rps-breakout")
        by_code = {item.ts_code: item for item in results}

        self.assertFalse(by_code["000001.SZ"].passed)
        self.assertIn("rps_gte_90", by_code["000001.SZ"].failed_conditions)
        self.assertTrue(by_code["000003.SZ"].passed, by_code["000003.SZ"].failed_conditions)
        self.assertEqual(inputs[2].metadata["rps_breakout"]["rps"], 100.0)

    def test_rps_breakout_rejects_breakout_window_short_data_and_stale_daily(self) -> None:
        failed_breakout = evaluate_inputs(
            [self._input(_rps_daily("000001.SZ", gain=1.0, near_high=False))],
            rule_name="rps_breakout",
        )[0]
        short = evaluate_inputs(
            [self._input(_rps_daily("000001.SZ", gain=1.0, count=120))],
            rule_name="rps_breakout",
        )[0]
        stale = evaluate_inputs(
            [self._input(_rps_daily("000001.SZ", gain=1.0, end="20260429"), trade_date="20260430")],
            rule_name="rps_breakout",
        )[0]

        self.assertIn("close_gte_90pct_120d_high", failed_breakout.failed_conditions)
        self.assertIn("data_window_121", short.failed_conditions)
        self.assertIn("daily_trade_date_current", stale.failed_conditions)

    def test_registry_accepts_sequoia_x_aliases(self) -> None:
        self.assertEqual(get_rule("turtle-trade").name, "turtle_trade")
        self.assertEqual(get_rule("MaVolume").name, "ma_volume")
        self.assertEqual(get_rule("HighTightFlagStrategy").name, "high_tight_flag")
        self.assertEqual(get_rule("limit-up-shakeout").name, "limit_up_shakeout")
        self.assertEqual(get_rule("UptrendLimitDown").name, "uptrend_limit_down")
        self.assertEqual(get_rule("RpsBreakout").name, "rps_breakout")


def _daily_frame(rows: list[dict[str, float]], *, end: str = "20260430", ts_code: str = "000001.SZ") -> pd.DataFrame:
    dates = make_trade_dates(len(rows), end=end)
    result = []
    for index, (trade_date, row) in enumerate(zip(dates, rows)):
        close = float(row["close"])
        previous_close = float(rows[index - 1]["close"]) if index > 0 else close
        result.append(
            {
                "ts_code": ts_code,
                "trade_date": trade_date,
                "open": row.get("open", close - 0.05),
                "high": row.get("high", close + 0.10),
                "low": row.get("low", close - 0.10),
                "close": close,
                "vol": row.get("vol", 1000.0),
                "amount": row.get("amount", 50_000.0),
                "pct_chg": (close / previous_close - 1.0) * 100 if previous_close > 0 else 0.0,
            }
        )
    return pd.DataFrame(result)


def _turtle_daily(*, amount: float = 100_000.0, end: str = "20260430") -> pd.DataFrame:
    rows = [{"close": 10.0 + index * 0.02} for index in range(20)]
    rows.append({"open": 10.90, "high": 11.10, "low": 10.80, "close": 11.00, "amount": amount})
    return _daily_frame(rows, end=end)


def _ma_volume_daily(*, latest_vol: float = 2000.0, end: str = "20260430") -> pd.DataFrame:
    rows = [{"close": 10.0, "vol": 1000.0} for _ in range(19)]
    rows.append({"close": 5.0, "vol": 1000.0})
    rows.append({"close": 20.0, "vol": latest_vol})
    return _daily_frame(rows, end=end)


def _high_tight_flag_daily(*, latest_vol: float = 500.0, end: str = "20260430") -> pd.DataFrame:
    rows: list[dict[str, float]] = []
    for index in range(30):
        close = 10.0 + index * 0.30
        rows.append(
            {
                "close": close,
                "high": 20.0 if index == 20 else close + 0.20,
                "low": 10.0 if index == 0 else close - 0.20,
                "vol": 1000.0,
            }
        )
    for index in range(10):
        close = 17.5 + (index % 3) * 0.10
        rows.append({"close": close, "high": 18.0, "low": 17.2, "vol": latest_vol if index == 9 else 1000.0})
    return _daily_frame(rows, end=end)


def _limit_up_shakeout_daily(*, today_low: float = 11.0, end: str = "20260430") -> pd.DataFrame:
    return _daily_frame(
        [
            {"open": 9.90, "high": 10.10, "low": 9.80, "close": 10.00, "vol": 1000.0},
            {"open": 10.20, "high": 10.95, "low": 10.10, "close": 10.95, "vol": 1000.0},
            {"open": 11.50, "high": 11.60, "low": today_low, "close": 11.20, "vol": 3000.0},
        ],
        end=end,
    )


def _uptrend_limit_down_daily(
    *,
    down_factor: float = 0.90,
    latest_vol: float = 5000.0,
    end: str = "20260430",
) -> pd.DataFrame:
    rows = [{"close": 10.0 + index * 0.05, "vol": 1000.0} for index in range(60)]
    previous_close = rows[-1]["close"]
    close = previous_close * down_factor
    rows.append({"open": previous_close * 0.98, "high": previous_close * 0.99, "low": close - 0.05, "close": close, "vol": latest_vol})
    return _daily_frame(rows, end=end)


def _rps_daily(
    ts_code: str,
    *,
    gain: float,
    near_high: bool = True,
    count: int = 121,
    end: str = "20260430",
) -> pd.DataFrame:
    start = 10.0
    finish = start * (1.0 + gain)
    step = (finish - start) / max(count - 1, 1)
    rows = []
    for index in range(count):
        close = start + step * index
        high = close * 1.02
        if not near_high and index == count // 2:
            high = finish * 2.0
        rows.append({"close": close, "high": high, "low": close * 0.98, "vol": 1000.0})
    return _daily_frame(rows, end=end, ts_code=ts_code)


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
