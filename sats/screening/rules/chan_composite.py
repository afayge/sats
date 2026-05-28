from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule
from sats.screening.rules.chan_third_buy import (
    ChanThirdBuyRule,
    _is_bse_stock,
    _is_st_stock,
    _latest_trade_date,
    _macd_histogram,
    _num,
    _prepare_daily,
    _prepare_minute,
    is_chan_daily_candidate,
)


CHAN_FIRST_BUY = "chan_first_buy"
CHAN_SECOND_BUY = "chan_second_buy"
CHAN_THIRD_BUY = "chan_third_buy"
CHAN_SECOND_THIRD_OVERLAP = "chan_second_third_overlap"
CHAN_CENTER_OSCILLATION_LOW = "chan_center_oscillation_low"

CHAN_RULE_LABELS = {
    CHAN_FIRST_BUY: "一买",
    CHAN_SECOND_BUY: "二买",
    CHAN_THIRD_BUY: "三买",
    CHAN_SECOND_THIRD_OVERLAP: "二三买重合",
    CHAN_CENTER_OSCILLATION_LOW: "中枢低吸",
}


@dataclass(frozen=True)
class ChanCompositeThresholds:
    min_daily_rows: int = 60
    first_lookback: int = 80
    first_down_return_max: float = -0.04
    first_box_amplitude_max: float = 0.25
    first_near_low_tolerance: float = 0.03
    first_macd_area_ratio_max: float = 0.95
    first_latest_repair_min: float = 0.015
    second_lookback: int = 80
    second_min_rally: float = 0.10
    second_min_pullback: float = 0.025
    second_low_tolerance: float = 0.0
    second_latest_repair_min: float = 0.01
    center_lookback: int = 40
    center_amplitude_max: float = 0.20
    center_edge_tolerance: float = 0.02
    center_reclaim_min: float = 0.005
    minute_min_rows: int = 35
    minute_ma_window: int = 5
    minute_support_tolerance: float = 0.02


class ChanCompositeRule(ScreeningRule):
    name = "chan_composite"

    def __init__(self, thresholds: ChanCompositeThresholds | None = None) -> None:
        self.thresholds = thresholds or ChanCompositeThresholds()
        self.third_rule = ChanThirdBuyRule()

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        common_checks, common_metrics, daily = _common_daily_context(data, thresholds=self.thresholds)
        sub_rules = {
            CHAN_FIRST_BUY: _evaluate_first_buy(data, daily=daily, thresholds=self.thresholds),
            CHAN_SECOND_BUY: _evaluate_second_buy(data, daily=daily, thresholds=self.thresholds),
            CHAN_THIRD_BUY: _evaluate_third_buy(self.third_rule, data),
            CHAN_CENTER_OSCILLATION_LOW: _evaluate_center_low(data, daily=daily, thresholds=self.thresholds),
        }

        if sub_rules[CHAN_SECOND_BUY]["passed"] and sub_rules[CHAN_THIRD_BUY]["passed"]:
            sub_rules[CHAN_SECOND_THIRD_OVERLAP] = _overlap_sub_rule(
                sub_rules[CHAN_SECOND_BUY],
                sub_rules[CHAN_THIRD_BUY],
            )
        else:
            sub_rules[CHAN_SECOND_THIRD_OVERLAP] = {
                "passed": False,
                "daily_candidate": False,
                "score": 0.0,
                "checks": {"second_and_third_buy_both_passed": False},
                "metrics": {},
                "watch_levels": {},
                "risk_flags": ["二买与三买未同时成立"],
            }

        common_passed = common_checks.get("common_daily_available", False)
        matched_rule_names = [name for name in CHAN_RULE_LABELS if common_passed and sub_rules[name]["passed"]]
        matched_labels = [CHAN_RULE_LABELS[name] for name in matched_rule_names]
        failed_rule_names = [name for name in CHAN_RULE_LABELS if not sub_rules[name]["passed"]]
        common_failed_conditions = [name for name, passed in common_checks.items() if not passed]
        metrics = {
            **common_metrics,
            "daily_basic_source": data.metadata.get("daily_basic_source", ""),
            "minute_30m_source": data.metadata.get("minute_30m_source", ""),
            "chan_daily_candidates": data.metadata.get("chan_daily_candidates", []),
            "matched_chan_rules": matched_labels,
            "matched_chan_rule_names": matched_rule_names,
            "chan_rule_scores": {name: sub_rules[name]["score"] for name in CHAN_RULE_LABELS},
            "watch_levels": {
                name: sub_rules[name]["watch_levels"]
                for name in CHAN_RULE_LABELS
                if sub_rules[name]["watch_levels"]
            },
            "risk_flags": [*_common_risk_flags(common_failed_conditions), *_unique_risk_flags(sub_rules)],
            "sub_rules": {
                name: {
                    "label": CHAN_RULE_LABELS[name],
                    "passed": sub_rules[name]["passed"],
                    "daily_candidate": sub_rules[name]["daily_candidate"],
                    "checks": sub_rules[name]["checks"],
                    "metrics": sub_rules[name]["metrics"],
                }
                for name in CHAN_RULE_LABELS
            },
        }
        matched_conditions = list(matched_rule_names)
        failed_conditions = failed_rule_names if not matched_rule_names else []
        if common_failed_conditions:
            failed_conditions = [*common_failed_conditions, *failed_conditions]
        return ScreeningResult(
            trade_date=data.trade_date,
            ts_code=data.ts_code,
            rule_name=self.name,
            passed=bool(matched_rule_names),
            score=_composite_score(sub_rules, matched_rule_names),
            matched_conditions=matched_conditions,
            failed_conditions=failed_conditions,
            metrics=metrics,
        )


def chan_composite_daily_candidates(
    data: ScreeningInput,
    *,
    thresholds: ChanCompositeThresholds | None = None,
) -> list[str]:
    config = thresholds or ChanCompositeThresholds()
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
    if CHAN_SECOND_BUY in candidates and CHAN_THIRD_BUY in candidates:
        candidates.append(CHAN_SECOND_THIRD_OVERLAP)
    return candidates


def _common_daily_context(
    data: ScreeningInput,
    *,
    thresholds: ChanCompositeThresholds,
) -> tuple[dict[str, bool], dict[str, Any], pd.DataFrame]:
    checks = {
        "not_st": not _is_st_stock(data.stock_basic),
        "not_bse": not _is_bse_stock(data.ts_code, data.stock_basic),
    }
    metrics: dict[str, Any] = {"data_source": data.metadata.get("data_source", "unknown")}
    daily = _prepare_daily(data.daily, trade_date=data.trade_date)
    latest_trade_date = _latest_trade_date(daily)
    metrics.update({"daily_rows": len(daily), "latest_daily_trade_date": latest_trade_date})
    checks["data_window"] = not daily.empty
    checks["daily_trade_date_current"] = latest_trade_date == data.trade_date
    checks["common_daily_available"] = all(checks.values())
    return checks, metrics, daily


def _evaluate_first_buy(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanCompositeThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_first_buy_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_repair_minute(
        data,
        thresholds=thresholds,
        support_level=_num(daily_result["metrics"].get("first_buy_c_low")),
        metric_prefix="first_buy",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=70.0)


def _evaluate_first_buy_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanCompositeThresholds,
) -> dict[str, Any]:
    checks: dict[str, bool] = {"first_buy_daily_window": len(daily) >= thresholds.min_daily_rows}
    metrics: dict[str, Any] = {}
    if not checks["first_buy_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    window = daily.tail(min(len(daily), thresholds.first_lookback)).reset_index(drop=True)
    if len(window) < thresholds.min_daily_rows:
        checks["first_buy_daily_window"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])

    a = window.iloc[:25]
    b = window.iloc[25:55]
    c = window.iloc[55:]
    if min(len(a), len(b), len(c)) < 5:
        checks["first_buy_abc_segments"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["无法分解下跌+盘整+下跌结构"])

    close = pd.to_numeric(window["close"], errors="coerce")
    macd_hist = _macd_histogram(close)
    a_return = _segment_return(a)
    c_return = _segment_return(c)
    b_amplitude = _amplitude(b)
    a_low = _num(a["low"].min())
    c_low = _num(c["low"].min())
    c_low_date = str(c.loc[c["low"].astype(float).idxmin(), "trade_date"])
    a_area = _negative_area(macd_hist.iloc[: len(a)])
    c_area = _negative_area(macd_hist.iloc[len(a) + len(b) :])
    latest = window.iloc[-1]
    latest_close = _num(latest.get("close"))

    metrics.update(
        {
            "first_buy_a_return": a_return,
            "first_buy_b_amplitude": b_amplitude,
            "first_buy_c_return": c_return,
            "first_buy_a_low": a_low,
            "first_buy_c_low": c_low,
            "first_buy_c_low_trade_date": c_low_date,
            "first_buy_macd_green_area_a": a_area,
            "first_buy_macd_green_area_c": c_area,
            "first_buy_latest_close": latest_close,
        }
    )
    checks.update(
        {
            "first_buy_abc_structure": a_return <= thresholds.first_down_return_max
            and b_amplitude <= thresholds.first_box_amplitude_max
            and c_return <= thresholds.first_down_return_max,
            "first_buy_c_near_or_new_low": c_low <= a_low * (1.0 + thresholds.first_near_low_tolerance),
            "first_buy_macd_divergence": a_area > 0 and c_area <= a_area * thresholds.first_macd_area_ratio_max,
            "first_buy_latest_bottom_repair": latest_close >= c_low * (1.0 + thresholds.first_latest_repair_min),
        }
    )
    watch_levels = {"support": c_low, "invalid": c_low}
    return _sub_rule(all(checks.values()), True, checks, metrics, watch_levels, _failed_daily_flags(checks))


def _evaluate_second_buy(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanCompositeThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_second_buy_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_repair_minute(
        data,
        thresholds=thresholds,
        support_level=_num(daily_result["metrics"].get("second_buy_first_low")),
        metric_prefix="second_buy",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=72.0)


def _evaluate_second_buy_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanCompositeThresholds,
) -> dict[str, Any]:
    checks: dict[str, bool] = {"second_buy_daily_window": len(daily) >= thresholds.min_daily_rows}
    metrics: dict[str, Any] = {}
    if not checks["second_buy_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    window = daily.tail(min(len(daily), thresholds.second_lookback)).reset_index(drop=True)
    if len(window) < thresholds.min_daily_rows:
        checks["second_buy_daily_window"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])

    low_search = window.iloc[:-10]
    if low_search.empty:
        checks["second_buy_prior_low"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["未找到一买低点"])
    first_low_pos = int(pd.to_numeric(low_search["low"], errors="coerce").idxmin())
    if first_low_pos >= len(window) - 15:
        checks["second_buy_prior_low"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["一买低点过近，尚未形成二买回抽"])

    first_low = _num(window.loc[first_low_pos, "low"])
    after_low = window.iloc[first_low_pos + 1 :]
    high_pos = int(pd.to_numeric(after_low["high"], errors="coerce").idxmax())
    pullback = window.iloc[high_pos + 1 :]
    if pullback.empty:
        checks["second_buy_pullback_exists"] = False
        return _sub_rule(False, True, checks, metrics, {}, ["一买后尚未出现首次回抽"])

    high_after_low = _num(window.loc[high_pos, "high"])
    pullback_low = _num(pullback["low"].min())
    latest_close = _num(window.iloc[-1].get("close"))
    close = pd.to_numeric(window["close"], errors="coerce")
    macd_hist = _macd_histogram(close)
    pullback_hist = macd_hist.iloc[high_pos + 1 :]
    min_pullback_hist = _num(pullback_hist.min())
    latest_hist = _num(macd_hist.iloc[-1])
    rally_gain = high_after_low / first_low - 1.0 if first_low > 0 else 0.0
    pullback_depth = (high_after_low - pullback_low) / high_after_low if high_after_low > 0 else 0.0

    metrics.update(
        {
            "second_buy_first_low": first_low,
            "second_buy_first_low_trade_date": str(window.loc[first_low_pos, "trade_date"]),
            "second_buy_high_after_low": high_after_low,
            "second_buy_pullback_low": pullback_low,
            "second_buy_rally_gain": rally_gain,
            "second_buy_pullback_depth": pullback_depth,
            "second_buy_macd_hist_pullback_min": min_pullback_hist,
            "second_buy_macd_hist_latest": latest_hist,
            "second_buy_latest_close": latest_close,
        }
    )
    checks.update(
        {
            "second_buy_rally_after_first_buy": rally_gain >= thresholds.second_min_rally,
            "second_buy_first_pullback": pullback_depth >= thresholds.second_min_pullback,
            "second_buy_pullback_not_break_first_low": pullback_low >= first_low * (1.0 - thresholds.second_low_tolerance),
            "second_buy_latest_repair": latest_close >= pullback_low * (1.0 + thresholds.second_latest_repair_min),
            "second_buy_macd_improving": latest_hist > min_pullback_hist,
        }
    )
    watch_levels = {"support": pullback_low, "invalid": first_low}
    return _sub_rule(all(checks.values()), True, checks, metrics, watch_levels, _failed_daily_flags(checks))


def _evaluate_third_buy(third_rule: ChanThirdBuyRule, data: ScreeningInput) -> dict[str, Any]:
    result = third_rule.evaluate(data)
    metrics = dict(result.metrics)
    watch_levels = {}
    if _num(metrics.get("box_high")) > 0:
        watch_levels = {"support": _num(metrics.get("box_high")), "invalid": _num(metrics.get("box_low"))}
    return _sub_rule(
        result.passed,
        is_chan_daily_candidate(data),
        {name: True for name in result.matched_conditions} | {name: False for name in result.failed_conditions},
        metrics,
        watch_levels,
        [f"三买失败: {name}" for name in result.failed_conditions[:3]],
        score=result.score,
    )


def _evaluate_center_low(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanCompositeThresholds,
) -> dict[str, Any]:
    daily_result = _evaluate_center_low_daily(data, daily=daily, thresholds=thresholds)
    minute_result = _evaluate_repair_minute(
        data,
        thresholds=thresholds,
        support_level=_num(daily_result["metrics"].get("center_box_low")),
        metric_prefix="center_low",
    )
    return _merge_daily_minute_result(daily_result, minute_result, base_score=64.0)


def _evaluate_center_low_daily(
    data: ScreeningInput,
    *,
    daily: pd.DataFrame,
    thresholds: ChanCompositeThresholds,
) -> dict[str, Any]:
    checks: dict[str, bool] = {"center_low_daily_window": len(daily) >= thresholds.center_lookback}
    metrics: dict[str, Any] = {}
    if not checks["center_low_daily_window"]:
        return _sub_rule(False, True, checks, metrics, {}, ["日线数据不足"])
    window = daily.tail(thresholds.center_lookback).reset_index(drop=True)
    prior = window.iloc[:-1]
    latest = window.iloc[-1]
    box_high = _num(prior["high"].max())
    box_low = _num(prior["low"].min())
    box_mid = _num(prior["close"].mean())
    latest_low = _num(latest.get("low"))
    latest_close = _num(latest.get("close"))
    amplitude = (box_high - box_low) / box_mid if box_mid > 0 else 1.0
    close = pd.to_numeric(window["close"], errors="coerce")
    macd_hist = _macd_histogram(close)
    latest_hist = _num(macd_hist.iloc[-1])
    prior_low_pos = int(pd.to_numeric(prior["low"], errors="coerce").idxmin()) if not prior.empty else 0
    prior_probe_hist = _num(macd_hist.iloc[prior_low_pos])

    metrics.update(
        {
            "center_box_high": box_high,
            "center_box_low": box_low,
            "center_box_amplitude": amplitude,
            "center_latest_low": latest_low,
            "center_latest_close": latest_close,
            "center_macd_hist_prior_probe": prior_probe_hist,
            "center_macd_hist_latest": latest_hist,
        }
    )
    checks.update(
        {
            "center_box_amplitude_ok": amplitude <= thresholds.center_amplitude_max,
            "center_low_probe_near_edge": latest_low <= box_low * (1.0 + thresholds.center_edge_tolerance),
            "center_low_reclaimed": latest_close >= box_low * (1.0 + thresholds.center_reclaim_min),
            "center_low_not_third_sell": latest_close >= box_low,
            "center_low_probe_strength_weaker": latest_hist > prior_probe_hist,
        }
    )
    watch_levels = {"support": box_low, "invalid": box_low * (1.0 - thresholds.center_edge_tolerance)}
    return _sub_rule(all(checks.values()), True, checks, metrics, watch_levels, _failed_daily_flags(checks))


def _evaluate_repair_minute(
    data: ScreeningInput,
    *,
    thresholds: ChanCompositeThresholds,
    support_level: float,
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
    low_index = recent["low"].astype(float).idxmin()
    minute_low = _num(minute.loc[low_index, "low"])
    latest = minute.iloc[-1]
    latest_close = _num(latest.get("close"))
    latest_ma5 = _num(latest.get("ma5"))
    low_hist = _num(minute.loc[low_index, "macd_hist"])
    latest_hist = _num(latest.get("macd_hist"))
    metrics.update(
        {
            f"{metric_prefix}_minute_30m_low": minute_low,
            f"{metric_prefix}_minute_30m_latest_close": latest_close,
            f"{metric_prefix}_minute_30m_ma5": latest_ma5,
            f"{metric_prefix}_minute_30m_macd_hist_low": low_hist,
            f"{metric_prefix}_minute_30m_macd_hist_latest": latest_hist,
        }
    )
    checks[f"{metric_prefix}_minute_close_above_ma5"] = latest_ma5 > 0 and latest_close > latest_ma5
    checks[f"{metric_prefix}_minute_macd_hist_improving"] = latest_hist > low_hist
    if support_level > 0:
        checks[f"{metric_prefix}_minute_holds_support"] = minute_low >= support_level * (
            1.0 - thresholds.minute_support_tolerance
        )
    return {"checks": checks, "metrics": metrics, "passed": all(checks.values()), "risk_flags": _failed_daily_flags(checks)}


def _merge_daily_minute_result(
    daily_result: dict[str, Any],
    minute_result: dict[str, Any],
    *,
    base_score: float,
) -> dict[str, Any]:
    checks = {**daily_result["checks"], **minute_result["checks"]}
    metrics = {**daily_result["metrics"], **minute_result["metrics"]}
    passed = bool(daily_result["passed"] and minute_result["passed"])
    score = base_score if passed else max(0.0, base_score - 8.0 * sum(1 for ok in checks.values() if not ok))
    return _sub_rule(
        passed,
        daily_result["daily_candidate"],
        checks,
        metrics,
        daily_result["watch_levels"],
        [*daily_result["risk_flags"], *minute_result["risk_flags"]],
        score=score,
    )


def _overlap_sub_rule(second: dict[str, Any], third: dict[str, Any]) -> dict[str, Any]:
    watch_levels = {
        "second_support": second["watch_levels"].get("support"),
        "third_support": third["watch_levels"].get("support"),
        "invalid": second["watch_levels"].get("invalid"),
    }
    return _sub_rule(
        True,
        True,
        {"second_buy_passed": True, "third_buy_passed": True},
        {"overlap_reason": "二买回抽不破一买低点，同时三买回抽不回日线中枢"},
        {key: value for key, value in watch_levels.items() if value is not None},
        [],
        score=max(_num(second.get("score")), _num(third.get("score"))) + 8.0,
    )


def _sub_rule(
    passed: bool,
    daily_candidate: bool,
    checks: dict[str, bool],
    metrics: dict[str, Any],
    watch_levels: dict[str, Any],
    risk_flags: list[str],
    *,
    score: float = 0.0,
) -> dict[str, Any]:
    return {
        "passed": passed,
        "daily_candidate": daily_candidate and any(checks.values()),
        "checks": checks,
        "metrics": metrics,
        "watch_levels": watch_levels,
        "risk_flags": risk_flags,
        "score": round(max(0.0, min(float(score), 100.0)), 2),
    }


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


def _negative_area(series: pd.Series) -> float:
    values = pd.to_numeric(series, errors="coerce").fillna(0.0)
    return abs(float(values[values < 0].sum()))


def _failed_daily_flags(checks: dict[str, bool]) -> list[str]:
    return [name for name, passed in checks.items() if not passed]


def _unique_risk_flags(sub_rules: dict[str, dict[str, Any]]) -> list[str]:
    flags = []
    seen = set()
    for name, result in sub_rules.items():
        if result["passed"]:
            continue
        for flag in result["risk_flags"]:
            text = f"{CHAN_RULE_LABELS.get(name, name)}: {flag}"
            if text not in seen:
                seen.add(text)
                flags.append(text)
    return flags[:12]


def _common_risk_flags(failed_conditions: list[str]) -> list[str]:
    labels = {
        "not_st": "公共条件: 排除ST失败",
        "not_bse": "公共条件: 排除北交所失败",
        "data_window": "公共条件: 日线数据不足",
        "daily_trade_date_current": "公共条件: 最新日线不是请求交易日",
        "common_daily_available": "公共条件: 日线硬条件未满足",
    }
    return [labels.get(name, f"公共条件: {name}") for name in failed_conditions]


def _composite_score(sub_rules: dict[str, dict[str, Any]], matched_rule_names: list[str]) -> float:
    if not matched_rule_names:
        return 0.0
    score = max(_num(sub_rules[name].get("score")) for name in matched_rule_names)
    score += max(0, len(matched_rule_names) - 1) * 4.0
    if CHAN_SECOND_THIRD_OVERLAP in matched_rule_names:
        score += 6.0
    return round(max(0.0, min(score, 100.0)), 2)
