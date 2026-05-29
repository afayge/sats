from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule


@dataclass(frozen=True)
class MonthlyBaseBreakoutThresholds:
    min_months: int = 60
    base_lookback_min: int = 24
    base_lookback_max: int = 96
    neckline_tolerance: float = 0.12
    below_neckline_ratio_min: float = 0.70
    pullback_depth_min: float = 0.15
    min_alternating_pivots: int = 5
    min_pullback_lows: int = 2
    breakout_buffer: float = 0.03
    early_max_breakout_age: int = 3
    early_premium_max: float = 0.35
    confirmed_max_breakout_age: int = 18
    confirmed_premium_min: float = 0.35


class MonthlyBaseBreakoutRule(ScreeningRule):
    name = "monthly_base_breakout"

    def __init__(self, thresholds: MonthlyBaseBreakoutThresholds | None = None) -> None:
        self.thresholds = thresholds or MonthlyBaseBreakoutThresholds()

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        checks: dict[str, bool] = {}
        metrics: dict[str, Any] = {"data_source": data.metadata.get("data_source", "unknown")}
        monthly, source = _resolve_monthly(data)
        monthly = _prepare_monthly(monthly, trade_date=data.trade_date)
        metrics.update(
            {
                "monthly_1M_source": source,
                "monthly_rows": len(monthly),
                "latest_monthly_trade_date": _latest_trade_date(monthly),
            }
        )

        checks["monthly_window_gte_60"] = len(monthly) >= self.thresholds.min_months
        if not checks["monthly_window_gte_60"]:
            metrics["reason"] = "需要至少60根月K识别长期箱体突破"
            return self._build_result(data, checks=checks, metrics=metrics)

        monthly = _with_moving_averages(monthly)
        early = _find_stage_setup(monthly, stage="early_breakout", thresholds=self.thresholds)
        confirmed = _find_stage_setup(monthly, stage="confirmed_run", thresholds=self.thresholds)
        stages = [stage for stage, setup in (("early_breakout", early), ("confirmed_run", confirmed)) if setup]

        setup = early or confirmed or _best_failed_setup(monthly, thresholds=self.thresholds)
        if setup:
            metrics.update(setup)
        metrics["stage"] = stages[0] if len(stages) == 1 else stages
        metrics["matched_stages"] = stages

        checks["neckline_touches_gte_2"] = _num(metrics.get("neckline_touch_count")) >= 2
        checks["alternating_pivots_gte_5"] = _num(metrics.get("alternating_pivot_count")) >= self.thresholds.min_alternating_pivots
        checks["pullback_lows_gte_2"] = _num(metrics.get("pullback_low_count")) >= self.thresholds.min_pullback_lows
        checks["base_mostly_below_neckline"] = _num(metrics.get("below_neckline_ratio")) >= self.thresholds.below_neckline_ratio_min
        checks["early_or_confirmed_stage"] = bool(stages)
        if not stages and "reason" not in metrics:
            metrics["reason"] = "未形成有效月线箱体突破或主升确认"
        return self._build_result(data, checks=checks, metrics=metrics)

    def _build_result(
        self,
        data: ScreeningInput,
        *,
        checks: dict[str, bool],
        metrics: dict[str, Any],
    ) -> ScreeningResult:
        matched = [name for name, passed in checks.items() if passed]
        failed = [name for name, passed in checks.items() if not passed]
        passed = bool(checks) and not failed
        return ScreeningResult(
            trade_date=data.trade_date,
            ts_code=data.ts_code,
            rule_name=self.name,
            passed=passed,
            score=_score(metrics, matched, failed),
            matched_conditions=matched,
            failed_conditions=failed,
            metrics=metrics,
        )


def _resolve_monthly(data: ScreeningInput) -> tuple[pd.DataFrame, str]:
    monthly = data.metadata.get("monthly_1M")
    source = str(data.metadata.get("monthly_1M_source") or "")
    if isinstance(monthly, pd.DataFrame) and not monthly.empty:
        return monthly, source or "metadata_monthly_1M"
    fallback = _monthly_from_daily(data.daily, trade_date=data.trade_date)
    return fallback, "daily_aggregation" if not fallback.empty else source or "unavailable"


def _prepare_monthly(frame: pd.DataFrame | None, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = _rename_columns(frame.copy())
    if "period" in data.columns:
        data = data[data["period"].astype(str) == "1M"]
    required = ["trade_date", "open", "high", "low", "close"]
    for column in required:
        if column not in data.columns:
            return pd.DataFrame()
    if "vol" not in data.columns:
        if "volume" in data.columns:
            data["vol"] = data["volume"]
        else:
            data["vol"] = 0.0
    data["trade_date"] = data["trade_date"].astype(str)
    data = data[data["trade_date"] <= str(trade_date)]
    for column in ["open", "high", "low", "close", "vol", "amount", "pct_chg"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["trade_date", "open", "high", "low", "close"])
    return data.drop_duplicates(subset=["trade_date"], keep="last").sort_values("trade_date").reset_index(drop=True)


def _monthly_from_daily(frame: pd.DataFrame | None, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    daily = _rename_columns(frame.copy())
    required = ["trade_date", "open", "high", "low", "close"]
    for column in required:
        if column not in daily.columns:
            return pd.DataFrame()
    if "vol" not in daily.columns:
        daily["vol"] = daily["volume"] if "volume" in daily.columns else 0.0
    if "amount" not in daily.columns:
        daily["amount"] = 0.0
    daily["trade_date"] = daily["trade_date"].astype(str)
    daily = daily[daily["trade_date"] <= str(trade_date)]
    for column in ["open", "high", "low", "close", "vol", "amount"]:
        daily[column] = pd.to_numeric(daily[column], errors="coerce")
    daily = daily.dropna(subset=["trade_date", "open", "high", "low", "close"])
    if daily.empty:
        return pd.DataFrame()
    daily = daily.sort_values("trade_date").reset_index(drop=True)
    month_key = pd.to_datetime(daily["trade_date"], format="%Y%m%d", errors="coerce").dt.to_period("M")
    daily = daily[month_key.notna()].copy()
    daily["_month"] = month_key[month_key.notna()].astype(str).to_numpy()
    rows = []
    for _, group in daily.groupby("_month", sort=True):
        ordered = group.sort_values("trade_date")
        rows.append(
            {
                "ts_code": str(ordered.iloc[-1].get("ts_code") or ""),
                "period": "1M",
                "trade_date": str(ordered.iloc[-1]["trade_date"]),
                "open": _num(ordered.iloc[0].get("open")),
                "high": _num(ordered["high"].max()),
                "low": _num(ordered["low"].min()),
                "close": _num(ordered.iloc[-1].get("close")),
                "vol": _num(ordered["vol"].sum()),
                "amount": _num(ordered["amount"].sum()),
            }
        )
    monthly = pd.DataFrame(rows)
    if not monthly.empty:
        close = pd.to_numeric(monthly["close"], errors="coerce")
        monthly["pct_chg"] = close.pct_change().fillna(0.0) * 100.0
    return monthly


def _find_stage_setup(
    monthly: pd.DataFrame,
    *,
    stage: str,
    thresholds: MonthlyBaseBreakoutThresholds,
) -> dict[str, Any] | None:
    latest_index = len(monthly) - 1
    max_age = thresholds.early_max_breakout_age if stage == "early_breakout" else thresholds.confirmed_max_breakout_age
    first_candidate = max(thresholds.base_lookback_min, len(monthly) - max_age)
    for breakout_index in range(first_candidate, latest_index + 1):
        pre = monthly.iloc[max(0, breakout_index - thresholds.base_lookback_max):breakout_index].reset_index(drop=True)
        setup = _base_setup(pre, thresholds=thresholds)
        if setup is None:
            continue
        neckline = _num(setup["neckline"])
        breakout_close = _num(monthly.iloc[breakout_index].get("close"))
        latest = monthly.iloc[-1]
        latest_close = _num(latest.get("close"))
        breakout_premium = breakout_close / neckline - 1.0 if neckline > 0 else 0.0
        latest_premium = latest_close / neckline - 1.0 if neckline > 0 else 0.0
        if breakout_premium < thresholds.breakout_buffer or latest_premium < thresholds.breakout_buffer:
            continue
        age = latest_index - breakout_index + 1
        stage_checks = _stage_checks(monthly, stage=stage, latest_premium=latest_premium, thresholds=thresholds)
        if not all(stage_checks.values()):
            continue
        return {
            **setup,
            "stage": stage,
            "breakout_trade_date": str(monthly.iloc[breakout_index].get("trade_date") or ""),
            "breakout_close": breakout_close,
            "breakout_age_months": age,
            "breakout_premium": breakout_premium,
            "latest_close": latest_close,
            "latest_premium": latest_premium,
            "ma5": _num(latest.get("ma5")),
            "ma10": _num(latest.get("ma10")),
            "ma20": _num(latest.get("ma20")),
            "ma60": _num(latest.get("ma60")),
            **stage_checks,
        }
    return None


def _best_failed_setup(monthly: pd.DataFrame, *, thresholds: MonthlyBaseBreakoutThresholds) -> dict[str, Any]:
    latest_index = len(monthly) - 1
    start = max(thresholds.base_lookback_min, len(monthly) - thresholds.confirmed_max_breakout_age)
    for index in range(start, latest_index + 1):
        pre = monthly.iloc[max(0, index - thresholds.base_lookback_max):index].reset_index(drop=True)
        setup = _base_setup(pre, thresholds=thresholds, allow_failed=True)
        if setup:
            return setup
    pre = monthly.tail(thresholds.base_lookback_max).reset_index(drop=True)
    return _base_setup(pre, thresholds=thresholds, allow_failed=True) or {}


def _base_setup(
    pre: pd.DataFrame,
    *,
    thresholds: MonthlyBaseBreakoutThresholds,
    allow_failed: bool = False,
) -> dict[str, Any] | None:
    if len(pre) < thresholds.base_lookback_min:
        return None
    pivots = _pivots(pre, order=2)
    highs = [pivot for pivot in pivots if pivot[2] == "high"]
    neckline_group = _neckline_group(highs, tolerance=thresholds.neckline_tolerance)
    if not neckline_group:
        if not allow_failed:
            return None
        neckline = _num(pre["high"].max())
        touch_count = 0
    else:
        neckline = sum(point[1] for point in neckline_group) / len(neckline_group)
        touch_count = len(neckline_group)
    lows = [pivot for pivot in pivots if pivot[2] == "low"]
    pullback_lows = [point for point in lows if point[1] <= neckline * (1.0 - thresholds.pullback_depth_min)]
    alternating_count = _alternating_count(
        [point for point in pivots if point[1] <= neckline * (1.0 + thresholds.neckline_tolerance)]
    )
    below_ratio = float((pd.to_numeric(pre["close"], errors="coerce") <= neckline * 1.05).mean())
    setup = {
        "neckline": neckline,
        "neckline_touch_count": touch_count,
        "alternating_pivot_count": alternating_count,
        "pullback_low_count": len(pullback_lows),
        "below_neckline_ratio": below_ratio,
        "base_months": len(pre),
        "base_low": _num(pre["low"].min()),
        "base_high": _num(pre["high"].max()),
    }
    if allow_failed:
        return setup
    if touch_count < 2:
        return None
    if alternating_count < thresholds.min_alternating_pivots:
        return None
    if len(pullback_lows) < thresholds.min_pullback_lows:
        return None
    if below_ratio < thresholds.below_neckline_ratio_min:
        return None
    return setup


def _stage_checks(
    monthly: pd.DataFrame,
    *,
    stage: str,
    latest_premium: float,
    thresholds: MonthlyBaseBreakoutThresholds,
) -> dict[str, bool]:
    latest = monthly.iloc[-1]
    previous = monthly.iloc[-2] if len(monthly) >= 2 else latest
    ma5 = _num(latest.get("ma5"))
    ma10 = _num(latest.get("ma10"))
    ma20 = _num(latest.get("ma20"))
    ma60 = _num(latest.get("ma60"))
    previous_ma5 = _num(previous.get("ma5"))
    previous_ma20 = _num(previous.get("ma20"))
    if stage == "early_breakout":
        return {
            "early_premium_3_to_35pct": thresholds.breakout_buffer <= latest_premium <= thresholds.early_premium_max,
            "monthly_ma5_above_ma10": ma5 > ma10 > 0,
            "monthly_ma5_turning_up": ma5 >= previous_ma5 > 0,
            "monthly_ma20_not_falling": ma20 >= previous_ma20 > 0,
        }
    return {
        "confirmed_premium_gte_35pct": latest_premium >= thresholds.confirmed_premium_min,
        "monthly_ma_bull_stack_5_10_20_60": ma5 > ma10 > ma20 > ma60 > 0,
    }


def _with_moving_averages(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    for window in (5, 10, 20, 60):
        result[f"ma{window}"] = close.rolling(window=window, min_periods=window).mean()
    return result.reset_index(drop=True)


def _pivots(data: pd.DataFrame, order: int = 2) -> list[tuple[int, float, str]]:
    if len(data) < order * 2 + 1:
        return []
    highs = pd.to_numeric(data["high"], errors="coerce").to_numpy(dtype=float)
    lows = pd.to_numeric(data["low"], errors="coerce").to_numpy(dtype=float)
    pivots: list[tuple[int, float, str]] = []
    for index in range(order, len(data) - order):
        high_window = highs[index - order:index + order + 1]
        low_window = lows[index - order:index + order + 1]
        if pd.isna(highs[index]) or pd.isna(lows[index]):
            continue
        if highs[index] == max(high_window) and (highs[index] > max(high_window[:order]) and highs[index] > max(high_window[order + 1:])):
            pivots.append((index, float(highs[index]), "high"))
        if lows[index] == min(low_window) and (lows[index] < min(low_window[:order]) and lows[index] < min(low_window[order + 1:])):
            pivots.append((index, float(lows[index]), "low"))
    return sorted(pivots, key=lambda item: item[0])


def _neckline_group(
    highs: list[tuple[int, float, str]],
    *,
    tolerance: float,
) -> list[tuple[int, float, str]]:
    for anchor in sorted(highs, key=lambda item: item[1], reverse=True):
        price = anchor[1]
        group = [point for point in highs if price > 0 and abs(point[1] / price - 1.0) <= tolerance]
        if len(group) < 2:
            continue
        return group
    return []


def _alternating_count(pivots: list[tuple[int, float, str]]) -> int:
    best = 0
    current = 0
    previous_type = ""
    for _, _, pivot_type in sorted(pivots, key=lambda item: item[0]):
        if pivot_type == previous_type:
            current = 1
        else:
            current += 1
        previous_type = pivot_type
        best = max(best, current)
    return best


def _rename_columns(data: pd.DataFrame) -> pd.DataFrame:
    aliases = {
        "日期": "trade_date",
        "成交量": "vol",
        "成交额": "amount",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "涨跌幅": "pct_chg",
    }
    return data.rename(columns={col: aliases.get(str(col), col) for col in data.columns})


def _latest_trade_date(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return ""
    return str(frame["trade_date"].astype(str).max())


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _score(metrics: dict[str, Any], matched: list[str], failed: list[str]) -> float:
    score = min(60.0, len(matched) * 10.0)
    if metrics.get("matched_stages"):
        score += 18.0
    if "confirmed_run" in metrics.get("matched_stages", []):
        score += 8.0
    score += min(8.0, _num(metrics.get("neckline_touch_count")) * 2.0)
    score += min(6.0, _num(metrics.get("alternating_pivot_count")))
    score -= min(len(failed) * 6.0, 30.0)
    return round(max(0.0, min(score, 100.0)), 2)
