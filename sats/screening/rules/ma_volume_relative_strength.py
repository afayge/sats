from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from sats.screening.base import ScreeningInput, ScreeningResult, ScreeningRule


@dataclass(frozen=True)
class Thresholds:
    ma5_bias_max: float = 0.04
    three_day_gain_max: float = 0.09
    ten_day_gain_max: float = 0.18
    volume_ratio_min: float = 1.2
    volume_ratio_max: float = 2.0
    breakout_volume_ratio_max: float = 2.5
    close_position_min: float = 0.5


class MaVolumeRelativeStrengthRule(ScreeningRule):
    name = "ma_volume_relative_strength"

    def __init__(self, thresholds: Thresholds | None = None) -> None:
        self.thresholds = thresholds or Thresholds()

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        metrics: dict[str, Any] = {}
        checks: dict[str, bool] = {}

        daily = _prepare_daily(data.daily)
        latest_trade_date = _latest_trade_date(daily)
        if len(daily) < 60:
            return self._build_result(
                data,
                checks={"data_window": False},
                metrics={
                    "daily_rows": len(daily),
                    "latest_daily_trade_date": latest_trade_date,
                    "data_source": data.metadata.get("data_source", "unknown"),
                    "reason": "需要至少60个交易日以计算MA60",
                },
            )
        if latest_trade_date != data.trade_date:
            return self._build_result(
                data,
                checks={"daily_trade_date_current": False},
                metrics={
                    "daily_rows": len(daily),
                    "latest_daily_trade_date": latest_trade_date,
                    "data_source": data.metadata.get("data_source", "unknown"),
                    "reason": "最新日线数据不是请求交易日",
                },
            )

        daily = _with_moving_averages(daily)
        latest = daily.iloc[-1]

        close = _num(latest["close"])
        ma5 = _num(latest["ma5"])
        ma10 = _num(latest["ma10"])
        ma20 = _num(latest["ma20"])
        ma60 = _num(latest["ma60"])
        if min(ma5, ma10, ma20, ma60) <= 0:
            return self._build_result(
                data,
                checks={"ma_window": False},
                metrics={"daily_rows": len(daily), "reason": "均线窗口不足或均线无效"},
            )

        metrics.update(
            {
                "close": close,
                "ma5": ma5,
                "ma10": ma10,
                "ma20": ma20,
                "ma60": ma60,
                "latest_daily_trade_date": latest_trade_date,
                "data_source": data.metadata.get("data_source", "unknown"),
            }
        )

        recent3 = daily.tail(3)
        checks["close_above_ma5_3d"] = bool((recent3["close"] > recent3["ma5"]).all())

        recent4 = daily.tail(4)
        bullish_days_4 = int((recent4["close"] > recent4["open"]).sum())
        checks["bullish_days_3_of_4"] = bullish_days_4 >= 3
        metrics["bullish_days_4"] = bullish_days_4

        three_day_gain = _window_return(daily, periods=3)
        checks["three_day_gain_lte_9pct"] = three_day_gain <= self.thresholds.three_day_gain_max
        metrics["three_day_gain"] = three_day_gain

        ma5_bias = (close - ma5) / ma5 if ma5 > 0 else 1.0
        checks["ma5_bias_lte_4pct"] = 0 <= ma5_bias <= self.thresholds.ma5_bias_max
        metrics["ma5_bias"] = ma5_bias

        latest_close_position = _close_position(latest)
        checks["latest_close_upper_half"] = latest_close_position >= self.thresholds.close_position_min
        metrics["latest_close_position"] = latest_close_position

        volume_ratio = _volume_ratio(daily)
        platform_breakout = _is_platform_breakout(daily)
        volume_in_normal_range = self.thresholds.volume_ratio_min <= volume_ratio <= self.thresholds.volume_ratio_max
        volume_breakout_allowed = (
            platform_breakout
            and self.thresholds.volume_ratio_min <= volume_ratio <= self.thresholds.breakout_volume_ratio_max
        )
        checks["volume_ratio_1p2_to_2_or_breakout"] = bool(volume_in_normal_range or volume_breakout_allowed)
        checks["positive_day"] = _pct_change(daily) > 0
        metrics["volume_ratio_5d"] = volume_ratio
        metrics["platform_breakout"] = platform_breakout
        metrics["volume_breakout_allowed"] = volume_breakout_allowed

        ten_day_gain = _window_return(daily, periods=10)
        checks["ten_day_gain_lte_18pct"] = ten_day_gain <= self.thresholds.ten_day_gain_max
        metrics["ten_day_gain"] = ten_day_gain

        checks["ma_bull_stack_5_10_20_60"] = ma5 > ma10 > ma20 > ma60

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
        score = _score(metrics, matched, failed)
        return ScreeningResult(
            trade_date=data.trade_date,
            ts_code=data.ts_code,
            rule_name=self.name,
            passed=passed,
            score=score,
            matched_conditions=matched,
            failed_conditions=failed,
            metrics=metrics,
        )


def _prepare_daily(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    if data.empty:
        return data
    data = _rename_columns(data)
    required = ["trade_date", "open", "high", "low", "close"]
    for column in required:
        if column not in data.columns:
            raise ValueError(f"daily data missing required column: {column}")
    if "vol" not in data.columns:
        if "volume" in data.columns:
            data["vol"] = data["volume"]
        else:
            raise ValueError("daily data missing required column: vol")
    data["trade_date"] = data["trade_date"].astype(str)
    for column in ["open", "high", "low", "close", "vol", "pct_chg"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close", "vol"])
    return data.sort_values("trade_date").reset_index(drop=True)


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


def _with_moving_averages(data: pd.DataFrame) -> pd.DataFrame:
    result = data.copy()
    close = pd.to_numeric(result["close"], errors="coerce")
    for window in (5, 10, 20, 60):
        result[f"ma{window}"] = close.rolling(window=window, min_periods=window).mean()
    return result.reset_index(drop=True)


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _window_return(data: pd.DataFrame, *, periods: int) -> float:
    if len(data) < periods + 1:
        return 1.0
    start = _num(data.iloc[-periods - 1]["close"])
    end = _num(data.iloc[-1]["close"])
    if start <= 0:
        return 1.0
    return end / start - 1.0


def _close_position(row: pd.Series) -> float:
    high = _num(row.get("high"))
    low = _num(row.get("low"))
    close = _num(row.get("close"))
    span = high - low
    if span <= 0:
        return 1.0
    return (close - low) / span


def _volume_ratio(data: pd.DataFrame) -> float:
    if len(data) < 6:
        return 0.0
    latest = _num(data.iloc[-1]["vol"])
    base = _num(data.iloc[-6:-1]["vol"].mean())
    if base <= 0:
        return 0.0
    return latest / base


def _pct_change(data: pd.DataFrame) -> float:
    latest = data.iloc[-1]
    if "pct_chg" in data.columns and not pd.isna(latest.get("pct_chg")):
        return _num(latest.get("pct_chg"))
    if len(data) < 2:
        return 0.0
    prev_close = _num(data.iloc[-2]["close"])
    close = _num(latest["close"])
    if prev_close <= 0:
        return 0.0
    return (close / prev_close - 1.0) * 100


def _latest_trade_date(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return ""
    return str(frame["trade_date"].astype(str).max())


def _is_platform_breakout(data: pd.DataFrame, lookback: int = 20) -> bool:
    if len(data) < lookback + 1:
        return False
    closes = pd.to_numeric(data["close"], errors="coerce")
    today_close = _num(closes.iloc[-1])
    prior = closes.iloc[-lookback - 1 : -1]
    prior_high = _num(prior.max())
    baseline = _num(prior.mean())
    if baseline <= 0 or today_close <= prior_high:
        return False
    amplitude = (_num(prior.max()) - _num(prior.min())) / baseline
    return amplitude <= 0.18


def _score(metrics: dict[str, Any], matched: list[str], failed: list[str]) -> float:
    score = min(70.0, len(matched) * 6.0)
    close_position = _num(metrics.get("latest_close_position"))
    volume_ratio = _num(metrics.get("volume_ratio_5d"))

    if close_position >= 0.75:
        score += 8
    if 1.2 <= volume_ratio <= 1.6:
        score += 8
    elif 1.6 < volume_ratio <= 2.0:
        score += 5
    if metrics.get("platform_breakout"):
        score += 5
    score -= min(len(failed) * 4.0, 30.0)
    return round(max(0.0, min(score, 100.0)), 2)
