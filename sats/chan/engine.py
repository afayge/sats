from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from sats.rag.chan_knowledge import search_chan_knowledge
from sats.screening.base import ScreeningInput
from sats.screening.rules.chan_composite import (
    CHAN_CENTER_OSCILLATION_LOW,
    CHAN_FIRST_BUY,
    CHAN_SECOND_BUY,
    CHAN_SECOND_THIRD_OVERLAP,
    CHAN_THIRD_BUY,
    ChanCompositeThresholds,
    _common_daily_context,
    _evaluate_center_low,
    _evaluate_center_low_daily,
    _evaluate_first_buy,
    _evaluate_first_buy_daily,
    _evaluate_second_buy,
    _evaluate_second_buy_daily,
    _evaluate_third_buy,
    _failed_daily_flags,
    _merge_daily_minute_result,
    _negative_area,
    _sub_rule,
    _unique_risk_flags,
)
from sats.screening.rules.chan_third_buy import (
    ChanThirdBuyRule,
    _latest_trade_date,
    _macd_histogram,
    _num,
    _prepare_minute,
    is_chan_daily_candidate,
)

CHAN_FIRST_SELL = "chan_first_sell"
CHAN_SECOND_SELL = "chan_second_sell"
CHAN_THIRD_SELL = "chan_third_sell"
CHAN_CENTER_OSCILLATION_HIGH = "chan_center_oscillation_high"
CHAN_BOTTOM_FRACTAL_CONFIRM = "chan_bottom_fractal_confirm"
CHAN_TOP_FRACTAL_CONFIRM = "chan_top_fractal_confirm"
CHAN_HOLD_BY_LEVEL = "chan_hold_by_level"
CHAN_CASH_BY_LEVEL = "chan_cash_by_level"

CHAN_SIGNAL_LABELS = {
    CHAN_FIRST_BUY: "一买",
    CHAN_SECOND_BUY: "二买",
    CHAN_THIRD_BUY: "三买",
    CHAN_SECOND_THIRD_OVERLAP: "二三买重合",
    CHAN_CENTER_OSCILLATION_LOW: "中枢低吸",
    CHAN_FIRST_SELL: "一卖",
    CHAN_SECOND_SELL: "二卖",
    CHAN_THIRD_SELL: "三卖",
    CHAN_CENTER_OSCILLATION_HIGH: "中枢高抛",
    CHAN_BOTTOM_FRACTAL_CONFIRM: "底分型确认",
    CHAN_TOP_FRACTAL_CONFIRM: "顶分型确认",
    CHAN_HOLD_BY_LEVEL: "持股等待",
    CHAN_CASH_BY_LEVEL: "持币等待",
}

CHAN_SIGNAL_SIDES = {
    CHAN_FIRST_BUY: "buy",
    CHAN_SECOND_BUY: "buy",
    CHAN_THIRD_BUY: "buy",
    CHAN_SECOND_THIRD_OVERLAP: "buy",
    CHAN_CENTER_OSCILLATION_LOW: "buy",
    CHAN_FIRST_SELL: "sell",
    CHAN_SECOND_SELL: "sell",
    CHAN_THIRD_SELL: "sell",
    CHAN_CENTER_OSCILLATION_HIGH: "sell",
    CHAN_BOTTOM_FRACTAL_CONFIRM: "buy",
    CHAN_TOP_FRACTAL_CONFIRM: "sell",
    CHAN_HOLD_BY_LEVEL: "hold",
    CHAN_CASH_BY_LEVEL: "cash",
}


@dataclass(frozen=True)
class ChanSignalThresholds(ChanCompositeThresholds):
    sell_latest_reversal_min: float = 0.015
    sell_minute_resistance_tolerance: float = 0.02
    third_sell_box_tolerance: float = 0.01
    third_sell_volume_ratio_min: float = 1.2


@dataclass(frozen=True, slots=True)
class ChanSignal:
    signal_name: str
    label: str
    side: str
    passed: bool
    score: float
    level: str
    matched_conditions: list[str]
    failed_conditions: list[str]
    watch_levels: dict[str, Any]
    risk_flags: list[str]
    evidence_refs: list[dict[str, Any]]
    metrics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_name": self.signal_name,
            "label": self.label,
            "side": self.side,
            "passed": self.passed,
            "score": self.score,
            "level": self.level,
            "matched_conditions": self.matched_conditions,
            "failed_conditions": self.failed_conditions,
            "watch_levels": self.watch_levels,
            "risk_flags": self.risk_flags,
            "evidence_refs": self.evidence_refs,
            "metrics": self.metrics,
        }


def evaluate_chan_signals(
    data: ScreeningInput,
    *,
    thresholds: ChanSignalThresholds | None = None,
) -> list[ChanSignal]:
    config = thresholds or ChanSignalThresholds()
    common_checks, _, daily = _common_daily_context(data, thresholds=config)
    common_failed = [name for name, passed in common_checks.items() if not passed]
    common_passed = not common_failed
    third_rule = ChanThirdBuyRule()
    raw = {
        CHAN_FIRST_BUY: _evaluate_first_buy(data, daily=daily, thresholds=config),
        CHAN_SECOND_BUY: _evaluate_second_buy(data, daily=daily, thresholds=config),
        CHAN_THIRD_BUY: _evaluate_third_buy(third_rule, data),
        CHAN_CENTER_OSCILLATION_LOW: _evaluate_center_low(data, daily=daily, thresholds=config),
        CHAN_FIRST_SELL: _evaluate_first_sell(data, daily=daily, thresholds=config),
        CHAN_SECOND_SELL: _evaluate_second_sell(data, daily=daily, thresholds=config),
        CHAN_THIRD_SELL: _evaluate_third_sell(data, daily=daily, thresholds=config),
        CHAN_CENTER_OSCILLATION_HIGH: _evaluate_center_high(data, daily=daily, thresholds=config),
        CHAN_BOTTOM_FRACTAL_CONFIRM: _evaluate_bottom_fractal(data, daily=daily, thresholds=config),
        CHAN_TOP_FRACTAL_CONFIRM: _evaluate_top_fractal(data, daily=daily, thresholds=config),
    }
    if raw[CHAN_SECOND_BUY]["passed"] and raw[CHAN_THIRD_BUY]["passed"]:
        raw[CHAN_SECOND_THIRD_OVERLAP] = _sub_rule(
            True,
            True,
            {"second_buy_passed": True, "third_buy_passed": True},
            {"overlap_reason": "二买回抽不破一买低点，同时三买回抽不回日线中枢"},
            {
                "second_support": raw[CHAN_SECOND_BUY]["watch_levels"].get("support"),
                "third_support": raw[CHAN_THIRD_BUY]["watch_levels"].get("support"),
                "invalid": raw[CHAN_SECOND_BUY]["watch_levels"].get("invalid"),
            },
            [],
            score=max(_num(raw[CHAN_SECOND_BUY].get("score")), _num(raw[CHAN_THIRD_BUY].get("score"))) + 8.0,
        )
    else:
        raw[CHAN_SECOND_THIRD_OVERLAP] = _sub_rule(
            False,
            False,
            {"second_and_third_buy_both_passed": False},
            {},
            {},
            ["二买与三买未同时成立"],
        )

    signals = [
        _signal_from_result(name, raw[name], common_passed=common_passed, common_failed=common_failed)
        for name in CHAN_SIGNAL_LABELS
        if name not in {CHAN_HOLD_BY_LEVEL, CHAN_CASH_BY_LEVEL}
    ]
    buy_passed = any(signal.passed and signal.side == "buy" for signal in signals)
    sell_passed = any(signal.passed and signal.side == "sell" for signal in signals)
    if buy_passed and not sell_passed:
        signals.append(_position_signal(CHAN_HOLD_BY_LEVEL, "买点成立，按同级别卖点出现前持股等待"))
    if sell_passed and not buy_passed:
        signals.append(_position_signal(CHAN_CASH_BY_LEVEL, "卖点成立，按同级别买点出现前持币等待"))
    return signals


def chan_signal_daily_candidates(data: ScreeningInput) -> list[str]:
    config = ChanSignalThresholds()
    _, _, daily = _common_daily_context(data, thresholds=config)
    candidates = []
    if _evaluate_first_buy_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_FIRST_BUY)
    if _evaluate_second_buy_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_SECOND_BUY)
    if is_chan_daily_candidate(data):
        candidates.append(CHAN_THIRD_BUY)
    if _evaluate_center_low_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_CENTER_OSCILLATION_LOW)
    if _evaluate_first_sell_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_FIRST_SELL)
    if _evaluate_second_sell_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_SECOND_SELL)
    if _evaluate_third_sell_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_THIRD_SELL)
    if _evaluate_center_high_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_CENTER_OSCILLATION_HIGH)
    if _evaluate_bottom_fractal_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_BOTTOM_FRACTAL_CONFIRM)
    if _evaluate_top_fractal_daily(data, daily=daily, thresholds=config)["passed"]:
        candidates.append(CHAN_TOP_FRACTAL_CONFIRM)
    if CHAN_SECOND_BUY in candidates and CHAN_THIRD_BUY in candidates:
        candidates.append(CHAN_SECOND_THIRD_OVERLAP)
    return candidates


def _evaluate_first_sell(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_first_sell_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_sell_minute(
        data,
        thresholds=thresholds,
        resistance_level=_num(daily_result["metrics"].get("first_sell_c_high")),
        metric_prefix="first_sell",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=70.0)


def _evaluate_first_sell_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    checks: dict[str, bool] = {"first_sell_daily_window": len(daily) >= thresholds.min_daily_rows}
    metrics: dict[str, Any] = {}
    if not checks["first_sell_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    window = daily.tail(min(len(daily), thresholds.first_lookback)).reset_index(drop=True)
    if len(window) < thresholds.min_daily_rows:
        checks["first_sell_daily_window"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    a = window.iloc[:25]
    b = window.iloc[25:55]
    c = window.iloc[55:]
    if min(len(a), len(b), len(c)) < 5:
        checks["first_sell_abc_segments"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["无法分解上涨+盘整+上涨结构"])

    close = pd.to_numeric(window["close"], errors="coerce")
    macd_hist = _macd_histogram(close)
    a_return = _segment_return(a)
    c_return = _segment_return(c)
    b_amplitude = _amplitude(b)
    a_high = _num(a["high"].max())
    c_high = _num(c["high"].max())
    a_area = _positive_area(macd_hist.iloc[: len(a)])
    c_area = _positive_area(macd_hist.iloc[len(a) + len(b) :])
    latest_close = _num(window.iloc[-1].get("close"))
    metrics.update(
        {
            "first_sell_a_return": a_return,
            "first_sell_b_amplitude": b_amplitude,
            "first_sell_c_return": c_return,
            "first_sell_a_high": a_high,
            "first_sell_c_high": c_high,
            "first_sell_red_area_a": a_area,
            "first_sell_red_area_c": c_area,
            "first_sell_latest_close": latest_close,
        }
    )
    checks.update(
        {
            "first_sell_abc_structure": a_return >= abs(thresholds.first_down_return_max)
            and b_amplitude <= thresholds.first_box_amplitude_max
            and c_return >= abs(thresholds.first_down_return_max),
            "first_sell_c_near_or_new_high": c_high >= a_high * (1.0 - thresholds.first_near_low_tolerance),
            "first_sell_macd_divergence": a_area > 0 and c_area <= a_area * thresholds.first_macd_area_ratio_max,
            "first_sell_latest_top_reversal": latest_close <= c_high * (1.0 - thresholds.sell_latest_reversal_min),
        }
    )
    return _sub_rule(
        all(checks.values()),
        True,
        checks,
        metrics,
        {"resistance": c_high, "invalid": c_high},
        _failed_daily_flags(checks),
    )


def _evaluate_second_sell(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_second_sell_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_sell_minute(
        data,
        thresholds=thresholds,
        resistance_level=_num(daily_result["metrics"].get("second_sell_rebound_high")),
        metric_prefix="second_sell",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=72.0)


def _evaluate_second_sell_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    checks: dict[str, bool] = {"second_sell_daily_window": len(daily) >= thresholds.min_daily_rows}
    metrics: dict[str, Any] = {}
    if not checks["second_sell_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    window = daily.tail(min(len(daily), thresholds.second_lookback)).reset_index(drop=True)
    high_search = window.iloc[:-10]
    if high_search.empty:
        checks["second_sell_prior_high"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["未找到一卖高点"])
    first_high_pos = int(pd.to_numeric(high_search["high"], errors="coerce").idxmax())
    if first_high_pos >= len(window) - 15:
        checks["second_sell_prior_high"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["一卖高点过近，尚未形成二卖反抽"])
    first_high = _num(window.loc[first_high_pos, "high"])
    after_high = window.iloc[first_high_pos + 1 :]
    low_pos = int(pd.to_numeric(after_high["low"], errors="coerce").idxmin())
    rebound = window.iloc[low_pos + 1 :]
    if rebound.empty:
        checks["second_sell_rebound_exists"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["一卖后尚未出现首次反抽"])
    low_after_high = _num(window.loc[low_pos, "low"])
    rebound_high = _num(rebound["high"].max())
    latest_close = _num(window.iloc[-1].get("close"))
    macd_hist = _macd_histogram(pd.to_numeric(window["close"], errors="coerce"))
    rebound_hist = macd_hist.iloc[low_pos + 1 :]
    latest_hist = _num(macd_hist.iloc[-1])
    decline = 1.0 - low_after_high / first_high if first_high > 0 else 0.0
    rebound_gain = rebound_high / low_after_high - 1.0 if low_after_high > 0 else 0.0
    metrics.update(
        {
            "second_sell_first_high": first_high,
            "second_sell_low_after_high": low_after_high,
            "second_sell_rebound_high": rebound_high,
            "second_sell_decline": decline,
            "second_sell_rebound_gain": rebound_gain,
            "second_sell_latest_close": latest_close,
            "second_sell_macd_hist_latest": latest_hist,
            "second_sell_macd_hist_rebound_max": _num(rebound_hist.max()),
        }
    )
    checks.update(
        {
            "second_sell_decline_after_first_sell": decline >= thresholds.second_min_rally,
            "second_sell_first_rebound": rebound_gain >= thresholds.second_min_pullback,
            "second_sell_rebound_not_break_first_high": rebound_high <= first_high * (1.0 + thresholds.second_low_tolerance),
            "second_sell_latest_reversal": latest_close <= rebound_high * (1.0 - thresholds.second_latest_repair_min),
            "second_sell_macd_worsening": latest_hist < _num(rebound_hist.max()),
        }
    )
    return _sub_rule(
        all(checks.values()),
        True,
        checks,
        metrics,
        {"resistance": rebound_high, "invalid": first_high},
        _failed_daily_flags(checks),
    )


def _evaluate_third_sell(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_third_sell_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_sell_minute(
        data,
        thresholds=thresholds,
        resistance_level=_num(daily_result["metrics"].get("box_low")),
        metric_prefix="third_sell",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=76.0)


def _evaluate_third_sell_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    required_rows = 20 + 10 + 2 + 5
    checks: dict[str, bool] = {"third_sell_daily_window": len(daily) >= required_rows}
    metrics: dict[str, Any] = {}
    if not checks["third_sell_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    latest_index = len(daily) - 1
    first_candidate = max(20, latest_index - 10 + 1)
    last_candidate = latest_index - 2
    setup: dict[str, Any] | None = None
    for breakdown_index in range(last_candidate, first_candidate - 1, -1):
        box = daily.iloc[breakdown_index - 20 : breakdown_index]
        box_high = _num(box["high"].max())
        box_low = _num(box["low"].min())
        box_mid = _num(box["close"].mean())
        if box_mid <= 0 or (box_high - box_low) / box_mid > 0.20:
            continue
        breakdown = daily.iloc[breakdown_index]
        breakdown_close = _num(breakdown.get("close"))
        if breakdown_close >= box_low:
            continue
        volume_ratio = _volume_ratio_at(daily, breakdown_index)
        if volume_ratio < thresholds.third_sell_volume_ratio_min:
            continue
        pullback = daily.iloc[breakdown_index + 1 :]
        if len(pullback) < 2:
            continue
        pullback_high = _num(pullback["high"].max())
        latest_close = _num(daily.iloc[-1].get("close"))
        if pullback_high > box_low * (1.0 + thresholds.third_sell_box_tolerance):
            continue
        if latest_close > box_low * (1.0 + thresholds.third_sell_box_tolerance):
            continue
        setup = {
            "box_high": box_high,
            "box_low": box_low,
            "box_amplitude": (box_high - box_low) / box_mid,
            "breakdown_trade_date": str(breakdown.get("trade_date") or ""),
            "breakdown_close": breakdown_close,
            "breakdown_volume_ratio": volume_ratio,
            "pullback_days": len(pullback),
            "pullback_high": pullback_high,
            "latest_close": latest_close,
        }
        break
    if setup is None:
        checks["daily_third_sell_setup"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["未找到跌破中枢后反抽不回中枢的三卖代理结构"])
    metrics.update(setup)
    checks.update(
        {
            "box_amplitude_lte_20pct": setup["box_amplitude"] <= 0.20,
            "breakdown_volume_ratio_gte_1p2": setup["breakdown_volume_ratio"] >= thresholds.third_sell_volume_ratio_min,
            "pullback_days_gte_2": setup["pullback_days"] >= 2,
            "pullback_rejected_by_box": setup["pullback_high"] <= setup["box_low"] * (1.0 + thresholds.third_sell_box_tolerance),
            "latest_close_near_or_below_box": setup["latest_close"] <= setup["box_low"] * (1.0 + thresholds.third_sell_box_tolerance),
        }
    )
    return _sub_rule(
        all(checks.values()),
        True,
        checks,
        metrics,
        {"resistance": setup["box_low"], "invalid": setup["box_high"]},
        _failed_daily_flags(checks),
    )


def _evaluate_center_high(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_center_high_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_sell_minute(
        data,
        thresholds=thresholds,
        resistance_level=_num(daily_result["metrics"].get("center_box_high")),
        metric_prefix="center_high",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=64.0)


def _evaluate_center_high_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    checks: dict[str, bool] = {"center_high_daily_window": len(daily) >= thresholds.center_lookback}
    metrics: dict[str, Any] = {}
    if not checks["center_high_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    window = daily.tail(thresholds.center_lookback).reset_index(drop=True)
    prior = window.iloc[:-1]
    latest = window.iloc[-1]
    box_high = _num(prior["high"].max())
    box_low = _num(prior["low"].min())
    box_mid = _num(prior["close"].mean())
    latest_high = _num(latest.get("high"))
    latest_close = _num(latest.get("close"))
    amplitude = (box_high - box_low) / box_mid if box_mid > 0 else 1.0
    macd_hist = _macd_histogram(pd.to_numeric(window["close"], errors="coerce"))
    prior_high_pos = int(pd.to_numeric(prior["high"], errors="coerce").idxmax()) if not prior.empty else 0
    prior_probe_hist = _num(macd_hist.iloc[prior_high_pos])
    latest_hist = _num(macd_hist.iloc[-1])
    metrics.update(
        {
            "center_box_high": box_high,
            "center_box_low": box_low,
            "center_box_amplitude": amplitude,
            "center_latest_high": latest_high,
            "center_latest_close": latest_close,
            "center_macd_hist_prior_probe": prior_probe_hist,
            "center_macd_hist_latest": latest_hist,
        }
    )
    checks.update(
        {
            "center_box_amplitude_ok": amplitude <= thresholds.center_amplitude_max,
            "center_high_probe_near_edge": latest_high >= box_high * (1.0 - thresholds.center_edge_tolerance),
            "center_high_rejected": latest_close <= box_high * (1.0 - thresholds.center_reclaim_min),
            "center_high_not_third_buy": latest_close <= box_high,
            "center_high_probe_strength_weaker": latest_hist < prior_probe_hist,
        }
    )
    return _sub_rule(
        all(checks.values()),
        True,
        checks,
        metrics,
        {"resistance": box_high, "invalid": box_high * (1.0 + thresholds.center_edge_tolerance)},
        _failed_daily_flags(checks),
    )


def _evaluate_bottom_fractal(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_bottom_fractal_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_buy_minute(
        data,
        thresholds=thresholds,
        support_level=_num(daily_result["metrics"].get("bottom_fractal_low")),
        metric_prefix="bottom_fractal",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=58.0)


def _evaluate_bottom_fractal_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    checks: dict[str, bool] = {"bottom_fractal_daily_window": len(daily) >= 5}
    metrics: dict[str, Any] = {}
    if not checks["bottom_fractal_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    window = daily.tail(5).reset_index(drop=True)
    left, pivot, right = window.iloc[-4], window.iloc[-3], window.iloc[-2]
    latest = window.iloc[-1]
    fractal_low = _num(pivot.get("low"))
    fractal_high = max(_num(left.get("high")), _num(pivot.get("high")), _num(right.get("high")))
    latest_close = _num(latest.get("close"))
    metrics.update(
        {
            "bottom_fractal_low": fractal_low,
            "bottom_fractal_high": fractal_high,
            "bottom_fractal_latest_close": latest_close,
        }
    )
    checks.update(
        {
            "bottom_fractal_shape": fractal_low < _num(left.get("low")) and fractal_low < _num(right.get("low")),
            "bottom_fractal_not_broken": _num(window.iloc[-2:]["low"].min()) >= fractal_low,
            "bottom_fractal_reclaimed": latest_close >= fractal_high,
        }
    )
    return _sub_rule(
        all(checks.values()),
        True,
        checks,
        metrics,
        {"support": fractal_low, "trigger": fractal_high, "invalid": fractal_low},
        _failed_daily_flags(checks),
    )


def _evaluate_top_fractal(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_top_fractal_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_sell_minute(
        data,
        thresholds=thresholds,
        resistance_level=_num(daily_result["metrics"].get("top_fractal_high")),
        metric_prefix="top_fractal",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=58.0)


def _evaluate_top_fractal_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanSignalThresholds,
) -> dict[str, Any]:
    checks: dict[str, bool] = {"top_fractal_daily_window": len(daily) >= 5}
    metrics: dict[str, Any] = {}
    if not checks["top_fractal_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    window = daily.tail(5).reset_index(drop=True)
    left, pivot, right = window.iloc[-4], window.iloc[-3], window.iloc[-2]
    latest = window.iloc[-1]
    fractal_high = _num(pivot.get("high"))
    fractal_low = min(_num(left.get("low")), _num(pivot.get("low")), _num(right.get("low")))
    latest_close = _num(latest.get("close"))
    metrics.update(
        {
            "top_fractal_high": fractal_high,
            "top_fractal_low": fractal_low,
            "top_fractal_latest_close": latest_close,
        }
    )
    checks.update(
        {
            "top_fractal_shape": fractal_high > _num(left.get("high")) and fractal_high > _num(right.get("high")),
            "top_fractal_not_broken": _num(window.iloc[-2:]["high"].max()) <= fractal_high,
            "top_fractal_rejected": latest_close <= fractal_low,
        }
    )
    return _sub_rule(
        all(checks.values()),
        True,
        checks,
        metrics,
        {"resistance": fractal_high, "trigger": fractal_low, "invalid": fractal_high},
        _failed_daily_flags(checks),
    )


def _evaluate_buy_minute(
    data: ScreeningInput,
    *,
    thresholds: ChanSignalThresholds,
    support_level: float,
    metric_prefix: str,
) -> dict[str, Any]:
    from sats.screening.rules.chan_composite import _evaluate_repair_minute

    return _evaluate_repair_minute(
        data,
        thresholds=thresholds,
        support_level=support_level,
        metric_prefix=metric_prefix,
    )


def _evaluate_sell_minute(
    data: ScreeningInput,
    *,
    thresholds: ChanSignalThresholds,
    resistance_level: float,
    metric_prefix: str,
) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    metrics: dict[str, Any] = {}
    minute = _prepare_minute(data.metadata.get("minute_30m"), trade_date=data.trade_date)
    metrics[f"{metric_prefix}_minute_30m_rows"] = len(minute)
    metrics[f"{metric_prefix}_latest_minute_trade_date"] = _latest_trade_date(minute)
    checks[f"{metric_prefix}_minute_30m_available"] = len(minute) >= thresholds.minute_min_rows
    if not checks[f"{metric_prefix}_minute_30m_available"]:
        return {"checks": checks, "metrics": metrics, "passed": False, "risk_flags": ["缺少足够30分钟K线确认数据"]}
    checks[f"{metric_prefix}_minute_30m_trade_date_current"] = metrics[f"{metric_prefix}_latest_minute_trade_date"] == data.trade_date
    if not checks[f"{metric_prefix}_minute_30m_trade_date_current"]:
        return {"checks": checks, "metrics": metrics, "passed": False, "risk_flags": ["最新30分钟K线不是请求交易日"]}

    minute = minute.copy()
    close = pd.to_numeric(minute["close"], errors="coerce")
    minute["ma5"] = close.rolling(window=thresholds.minute_ma_window, min_periods=thresholds.minute_ma_window).mean()
    minute["macd_hist"] = _macd_histogram(close)
    recent = minute.tail(24)
    high_index = recent["high"].astype(float).idxmax()
    minute_high = _num(minute.loc[high_index, "high"])
    latest = minute.iloc[-1]
    latest_close = _num(latest.get("close"))
    latest_ma5 = _num(latest.get("ma5"))
    high_hist = _num(minute.loc[high_index, "macd_hist"])
    latest_hist = _num(latest.get("macd_hist"))
    metrics.update(
        {
            f"{metric_prefix}_minute_30m_high": minute_high,
            f"{metric_prefix}_minute_30m_latest_close": latest_close,
            f"{metric_prefix}_minute_30m_ma5": latest_ma5,
            f"{metric_prefix}_minute_30m_macd_hist_high": high_hist,
            f"{metric_prefix}_minute_30m_macd_hist_latest": latest_hist,
        }
    )
    checks[f"{metric_prefix}_minute_close_below_ma5"] = latest_ma5 > 0 and latest_close < latest_ma5
    checks[f"{metric_prefix}_minute_macd_hist_worsening"] = latest_hist < high_hist
    if resistance_level > 0:
        checks[f"{metric_prefix}_minute_rejected_by_resistance"] = minute_high <= resistance_level * (
            1.0 + thresholds.sell_minute_resistance_tolerance
        )
    return {"checks": checks, "metrics": metrics, "passed": all(checks.values()), "risk_flags": _failed_daily_flags(checks)}


def _signal_from_result(
    name: str,
    result: dict[str, Any],
    *,
    common_passed: bool,
    common_failed: list[str],
) -> ChanSignal:
    checks = result["checks"]
    passed = bool(common_passed and result["passed"])
    matched = [key for key, value in checks.items() if value]
    failed = [key for key, value in checks.items() if not value]
    if common_failed:
        failed = [*common_failed, *failed]
    risk_flags = [*_common_risk_flags(common_failed), *result["risk_flags"]]
    return ChanSignal(
        signal_name=name,
        label=CHAN_SIGNAL_LABELS[name],
        side=CHAN_SIGNAL_SIDES[name],
        passed=passed,
        score=_num(result.get("score")) if passed else 0.0,
        level="日线+30m",
        matched_conditions=matched if passed else [],
        failed_conditions=failed,
        watch_levels={key: value for key, value in result["watch_levels"].items() if value is not None},
        risk_flags=risk_flags,
        evidence_refs=_evidence_refs(name),
        metrics=result["metrics"],
    )


def _position_signal(name: str, reason: str) -> ChanSignal:
    return ChanSignal(
        signal_name=name,
        label=CHAN_SIGNAL_LABELS[name],
        side=CHAN_SIGNAL_SIDES[name],
        passed=True,
        score=50.0,
        level="操作原则",
        matched_conditions=[name],
        failed_conditions=[],
        watch_levels={},
        risk_flags=[reason],
        evidence_refs=_evidence_refs("chan_interval_nesting"),
        metrics={"reason": reason},
    )


def _evidence_refs(rule_id: str) -> list[dict[str, Any]]:
    refs = search_chan_knowledge(rule_id, rule_ids=[rule_id], limit=1)
    if not refs and rule_id in {CHAN_HOLD_BY_LEVEL, CHAN_CASH_BY_LEVEL}:
        refs = search_chan_knowledge("区间套 级别 持股 持币", rule_ids=["chan_interval_nesting"], limit=1)
    return [
        {
            "rule_id": item["rule_id"],
            "label": item["label"],
            "source_pages": item["source_pages"],
            "source": item["source"],
        }
        for item in refs
    ]


def _common_risk_flags(failed_conditions: list[str]) -> list[str]:
    labels = {
        "not_st": "公共条件: 排除ST失败",
        "not_bse": "公共条件: 排除北交所失败",
        "data_window": "公共条件: 日线数据不足",
        "daily_trade_date_current": "公共条件: 最新日线不是请求交易日",
        "common_daily_available": "公共条件: 日线硬条件未满足",
    }
    return [labels.get(name, f"公共条件: {name}") for name in failed_conditions]


def _segment_return(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 0.0
    start = _num(frame.iloc[0].get("close"))
    end = _num(frame.iloc[-1].get("close"))
    return end / start - 1.0 if start > 0 else 0.0


def _amplitude(frame: pd.DataFrame) -> float:
    if frame.empty:
        return 1.0
    high = _num(frame["high"].max())
    low = _num(frame["low"].min())
    mid = _num(frame["close"].mean())
    return (high - low) / mid if mid > 0 else 1.0


def _positive_area(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").fillna(0.0)
    return float(values[values > 0].sum())


def _volume_ratio_at(daily: pd.DataFrame, index: int, *, lookback: int = 5) -> float:
    if "vol" not in daily.columns:
        return 0.0
    start = max(0, index - lookback)
    previous = pd.to_numeric(daily.iloc[start:index]["vol"], errors="coerce")
    baseline = _num(previous.mean())
    current = _num(daily.iloc[index].get("vol"))
    return current / baseline if baseline > 0 else 0.0
