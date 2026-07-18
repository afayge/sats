from __future__ import annotations

from typing import Any

import pandas as pd

from sats.screening.base import ScreeningInput, ScreeningResult


def evaluate_generated_rule(data: ScreeningInput, *, rule_name: str, spec: dict[str, Any]) -> ScreeningResult:
    daily = _prepare_daily(data.daily, trade_date=data.trade_date)
    daily_basic = _prepare_daily_basic(data.daily_basic, trade_date=data.trade_date)
    metrics: dict[str, Any] = {
        "generated_rule": True,
        "decision_name": spec.get("decision_name", ""),
        "goal": spec.get("goal", ""),
        "daily_rows": len(daily),
        "daily_basic_rows": len(daily_basic),
    }
    matched: list[str] = []
    failed: list[str] = []
    details: list[dict[str, Any]] = []
    score_numerator = 0.0
    score_denominator = 0.0
    required_failed: list[str] = []

    for condition in spec.get("conditions", []):
        if not isinstance(condition, dict):
            continue
        condition_id = str(condition.get("id") or condition.get("kind") or "condition")
        label = str(condition.get("label") or condition_id)
        weight = float(condition.get("weight") or 1.0)
        required = bool(condition.get("required", True))
        passed, value, reason = _evaluate_condition(condition, data, daily, daily_basic)
        score_denominator += weight
        if passed:
            matched.append(condition_id)
            score_numerator += weight
        else:
            failed.append(condition_id)
            if required:
                required_failed.append(condition_id)
        details.append(
            {
                "id": condition_id,
                "label": label,
                "passed": passed,
                "value": value,
                "reason": reason,
                "weight": weight,
                "required": required,
                "source": str(condition.get("source") or "user"),
            }
        )

    score = round(score_numerator / score_denominator * 100.0, 2) if score_denominator > 0 else 0.0
    metrics["condition_details"] = details
    metrics["pass_condition"] = spec.get("pass_condition", "全部条件满足")
    metrics["required_failed_conditions"] = required_failed
    metrics["soft_failed_conditions"] = [
        str(row.get("id") or "") for row in details if not bool(row.get("required", True)) and not bool(row.get("passed"))
    ]
    return ScreeningResult(
        trade_date=data.trade_date,
        ts_code=data.ts_code,
        rule_name=rule_name,
        passed=bool(details) and not required_failed,
        score=score,
        matched_conditions=matched,
        failed_conditions=failed,
        metrics=metrics,
    )


def _evaluate_condition(
    condition: dict[str, Any],
    data: ScreeningInput,
    daily: pd.DataFrame,
    daily_basic: pd.DataFrame,
) -> tuple[bool, Any, str]:
    kind = str(condition.get("kind") or "")
    if kind == "exclude_st":
        name = str(data.stock_basic.get("name") or data.stock_basic.get("股票简称") or "")
        passed = "ST" not in name.upper() and "退" not in name
        return passed, name, "排除 ST/退市风险"
    if kind == "exclude_bse":
        value = str(data.ts_code or "")
        passed = not (value.endswith(".BJ") or value.startswith(("8", "4")))
        return passed, value, "排除北交所股票"
    if kind == "min_daily_rows":
        minimum = int(condition.get("min") or 1)
        return len(daily) >= minimum, len(daily), f"至少需要 {minimum} 个交易日"
    if daily.empty:
        return False, None, "缺少日线数据"

    latest = daily.iloc[-1]
    if kind == "pct_chg_between":
        value = _pct_change(daily)
        lower = float(condition.get("min") or 0.0)
        upper = float(condition.get("max") or 0.0)
        return lower <= value <= upper, round(value, 4), f"涨跌幅需在 {lower}-{upper}%"
    if kind == "pct_chg_gte":
        value = _pct_change(daily)
        minimum = float(condition.get("min") or 0.0)
        return value >= minimum, round(value, 4), f"涨跌幅需大于等于 {minimum}%"
    if kind == "volume_ratio_gte":
        window = int(condition.get("window") or 5)
        value = _volume_ratio(daily, window=window)
        minimum = float(condition.get("min") or 1.0)
        return value >= minimum, round(value, 4), f"{window} 日量比需大于等于 {minimum}"
    if kind == "volume_ratio_lte":
        window = int(condition.get("window") or 5)
        value = _volume_ratio(daily, window=window)
        maximum = float(condition.get("max") or 1.0)
        return value <= maximum, round(value, 4), f"{window} 日量比需小于等于 {maximum}"
    if kind == "close_above_ma":
        window = int(condition.get("window") or 5)
        ma = _ma(daily, window)
        close = _num(latest.get("close"))
        return close > ma > 0, {"close": close, f"ma{window}": round(ma, 4)}, f"收盘价需站上 MA{window}"
    if kind == "ma_stack":
        windows = [int(item) for item in condition.get("windows", [5, 10, 20, 60])]
        mas = {f"ma{window}": _ma(daily, window) for window in windows}
        values = [mas[f"ma{window}"] for window in windows]
        passed = all(value > 0 for value in values) and all(values[index] > values[index + 1] for index in range(len(values) - 1))
        return passed, {key: round(value, 4) for key, value in mas.items()}, "均线需多头排列"
    if kind == "ma_slope_gte":
        window = int(condition.get("window") or 20)
        lookback = max(1, int(condition.get("lookback") or 5))
        minimum = float(condition.get("min") or 0.0)
        current = _ma(daily, window)
        previous = _ma(daily.iloc[:-lookback], window) if len(daily) > lookback else 0.0
        value = (current / previous - 1.0) * 100.0 if previous > 0 else 0.0
        return current > 0 and previous > 0 and value >= minimum, round(value, 4), f"MA{window} {lookback} 日斜率需大于等于 {minimum}%"
    if kind == "recent_close_not_below_ma":
        window = int(condition.get("window") or 20)
        lookback = max(1, int(condition.get("lookback") or 5))
        tolerance = max(0.0, float(condition.get("tolerance") or 0.0))
        prepared = _with_ma_columns(daily, [window])
        recent = prepared.tail(lookback)
        ma_column = f"ma{window}"
        valid = recent[ma_column].notna() & (recent[ma_column] > 0)
        passed = len(recent) == lookback and bool(valid.all()) and bool((recent["close"] >= recent[ma_column] * (1.0 - tolerance)).all())
        minimum_bias = None
        if bool(valid.any()):
            minimum_bias = float(((recent.loc[valid, "close"] / recent.loc[valid, ma_column]) - 1.0).min())
        return passed, {"minimum_close_bias": minimum_bias, "tolerance": tolerance}, f"最近 {lookback} 日收盘不得有效跌破 MA{window}"
    if kind == "recent_low_near_any_ma":
        windows = [int(item) for item in condition.get("windows", [5, 10])]
        lookback = max(1, int(condition.get("lookback") or 5))
        touch_tolerance = max(0.0, float(condition.get("touch_tolerance") or 0.02))
        break_tolerance = max(0.0, float(condition.get("break_tolerance") or 0.01))
        require_reclaim = bool(condition.get("require_reclaim", True))
        prepared = _with_ma_columns(daily, windows)
        recent = prepared.tail(lookback)
        latest_row = prepared.iloc[-1]
        candidates: list[dict[str, Any]] = []
        for window in windows:
            column = f"ma{window}"
            valid = recent[column].notna() & (recent[column] > 0)
            if len(recent) != lookback or not bool(valid.all()):
                continue
            distance = ((recent["low"] - recent[column]).abs() / recent[column]).min()
            held = bool((recent["close"] >= recent[column] * (1.0 - break_tolerance)).all())
            latest_ma = _num(latest_row.get(column))
            reclaimed = _num(latest_row.get("close")) >= latest_ma if require_reclaim else True
            candidates.append(
                {
                    "window": window,
                    "minimum_low_distance": round(float(distance), 6),
                    "held": held,
                    "reclaimed": reclaimed,
                    "passed": float(distance) <= touch_tolerance and held and reclaimed,
                }
            )
        passed = any(bool(item.get("passed")) for item in candidates)
        selected = next((item for item in candidates if item.get("passed")), candidates[0] if candidates else {})
        return passed, {"selected_support": selected, "candidates": candidates}, f"最近 {lookback} 日需回踩指定均线附近且未有效跌破"
    if kind == "range_position_lte":
        window = int(condition.get("window") or 60)
        maximum = float(condition.get("max") or 0.35)
        value = _range_position(daily, window=window)
        return value <= maximum, round(value, 4), f"{window} 日区间位置需小于等于 {maximum}"
    if kind == "breakout_high":
        window = int(condition.get("window") or 20)
        tolerance = float(condition.get("tolerance") or 0.0)
        value = _breakout_value(daily, window=window)
        close = _num(latest.get("close"))
        target = value * (1.0 + tolerance)
        return close > target > 0, {"close": close, "previous_high": round(value, 4)}, f"收盘价需突破前 {window} 日高点"
    if kind == "turnover_between":
        value = _latest_basic_value(daily_basic, "turnover_rate")
        lower = float(condition.get("min") or 0.0)
        upper = float(condition.get("max") or 0.0)
        return lower <= value <= upper, round(value, 4), f"换手率需在 {lower}-{upper}%"
    if kind == "circ_mv_between":
        value = _latest_basic_value(daily_basic, "circ_mv")
        lower = float(condition.get("min") or 0.0)
        upper = float(condition.get("max") or 0.0)
        return lower <= value <= upper, round(value, 4), "流通市值需在指定区间"
    if kind == "daily_basic_max":
        column = str(condition.get("column") or "")
        value = _latest_basic_value(daily_basic, column)
        maximum = float(condition.get("max") or 0.0)
        return value <= maximum, round(value, 4), f"{column} 需小于等于 {maximum}"
    if kind == "daily_basic_min":
        column = str(condition.get("column") or "")
        value = _latest_basic_value(daily_basic, column)
        minimum = float(condition.get("min") or 0.0)
        return value >= minimum, round(value, 4), f"{column} 需大于等于 {minimum}"
    if kind == "relative_strength_gte":
        window = int(condition.get("window") or 20)
        minimum = float(condition.get("min") or 0.0)
        value = _relative_strength(data, daily, window=window)
        return value >= minimum, round(value, 4), f"{window} 日相对强度需大于等于 {minimum}%"
    if kind == "window_return_gte":
        window = int(condition.get("window") or 20)
        minimum = float(condition.get("min") or 0.0)
        value = _window_return(daily, window=window) if len(daily) > window else 0.0
        return len(daily) > window and value >= minimum, round(value, 4), f"{window} 日收益率需大于等于 {minimum}%"
    return False, None, f"不支持的生成规则条件: {kind}"


def _prepare_daily(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = _rename_columns(frame.copy())
    if "trade_date" in data.columns:
        data["trade_date"] = data["trade_date"].astype(str)
        data = data[data["trade_date"] <= str(trade_date)]
    for column in ("open", "high", "low", "close", "vol", "volume", "pct_chg"):
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    if "vol" not in data.columns and "volume" in data.columns:
        data["vol"] = data["volume"]
    required = [column for column in ("open", "high", "low", "close", "vol") if column in data.columns]
    if required:
        data = data.dropna(subset=required)
    return data.sort_values("trade_date").reset_index(drop=True) if "trade_date" in data.columns else data.reset_index(drop=True)


def _prepare_daily_basic(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "trade_date" in data.columns:
        data["trade_date"] = data["trade_date"].astype(str)
        data = data[data["trade_date"] <= str(trade_date)]
    for column in data.columns:
        if column not in {"ts_code", "trade_date"}:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.sort_values("trade_date").reset_index(drop=True) if "trade_date" in data.columns else data.reset_index(drop=True)


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
    return data.rename(columns={column: aliases.get(str(column), column) for column in data.columns})


def _pct_change(data: pd.DataFrame) -> float:
    latest = data.iloc[-1]
    if "pct_chg" in data.columns and not pd.isna(latest.get("pct_chg")):
        return _num(latest.get("pct_chg"))
    if len(data) < 2:
        return 0.0
    prev_close = _num(data.iloc[-2].get("close"))
    close = _num(latest.get("close"))
    return (close / prev_close - 1.0) * 100.0 if prev_close > 0 else 0.0


def _volume_ratio(data: pd.DataFrame, *, window: int) -> float:
    if len(data) <= window:
        return 0.0
    latest = _num(data.iloc[-1].get("vol"))
    base = _num(data.iloc[-window - 1 : -1]["vol"].mean())
    return latest / base if base > 0 else 0.0


def _ma(data: pd.DataFrame, window: int) -> float:
    if len(data) < window:
        return 0.0
    return _num(pd.to_numeric(data["close"], errors="coerce").rolling(window=window, min_periods=window).mean().iloc[-1])


def _with_ma_columns(data: pd.DataFrame, windows: list[int]) -> pd.DataFrame:
    result = data.copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    for window in windows:
        result[f"ma{window}"] = close.rolling(window=window, min_periods=window).mean()
    return result


def _range_position(data: pd.DataFrame, *, window: int) -> float:
    if len(data) < window:
        return 1.0
    frame = data.iloc[-window:]
    low = _num(frame["low"].min())
    high = _num(frame["high"].max())
    close = _num(frame.iloc[-1].get("close"))
    return (close - low) / (high - low) if high > low else 1.0


def _breakout_value(data: pd.DataFrame, *, window: int) -> float:
    if len(data) <= window:
        return 0.0
    return _num(data.iloc[-window - 1 : -1]["high"].max())


def _latest_basic_value(data: pd.DataFrame, column: str) -> float:
    if data.empty or column not in data.columns:
        return 0.0
    value = data.iloc[-1].get(column)
    return _num(value)


def _relative_strength(data: ScreeningInput, daily: pd.DataFrame, *, window: int) -> float:
    benchmark = data.industry_daily if data.industry_daily is not None and not data.industry_daily.empty else data.fallback_index_daily
    if benchmark is None or benchmark.empty or len(daily) <= window:
        return 0.0
    benchmark_daily = _prepare_daily(benchmark, trade_date=data.trade_date)
    if len(benchmark_daily) <= window:
        return 0.0
    stock_return = _window_return(daily, window=window)
    benchmark_return = _window_return(benchmark_daily, window=window)
    return stock_return - benchmark_return


def _window_return(data: pd.DataFrame, *, window: int) -> float:
    close = _num(data.iloc[-1].get("close"))
    base = _num(data.iloc[-window - 1].get("close"))
    return (close / base - 1.0) * 100.0 if base > 0 else 0.0


def _num(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
