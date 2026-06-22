from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from sats.screening.base import IntradayKlineRequirement, ScreeningInput, ScreeningResult, ScreeningRule


@dataclass(frozen=True)
class ChanThirdBuyThresholds:
    box_lookback: int = 20
    breakout_lookback: int = 10
    min_pullback_days: int = 2
    box_amplitude_max: float = 0.20
    box_tolerance: float = 0.01
    breakout_volume_ratio_min: float = 1.2
    ten_day_gain_max: float = 0.25
    ma20_bias_max: float = 0.12
    minute_min_rows: int = 35
    minute_ma_window: int = 5


class ChanThirdBuyRule(ScreeningRule):
    name = "chan_third_buy"
    intraday_kline_requirements = (
        IntradayKlineRequirement(
            period="30m",
            metadata_key="minute_30m",
            source_metadata_key="minute_30m_source",
            history_calendar_days=30,
            count=80,
        ),
    )

    def __init__(self, thresholds: ChanThirdBuyThresholds | None = None) -> None:
        self.thresholds = thresholds or ChanThirdBuyThresholds()

    def intraday_candidate_labels(
        self,
        data: ScreeningInput,
        requirement: IntradayKlineRequirement,
    ) -> list[str]:
        return [self.name] if is_chan_daily_candidate(data) else []

    def evaluate(self, data: ScreeningInput) -> ScreeningResult:
        checks, metrics = evaluate_chan_daily_setup(data, thresholds=self.thresholds)
        if _has_failed_daily_checks(checks):
            return self._build_result(data, checks=checks, metrics=metrics)

        minute_checks, minute_metrics = evaluate_chan_minute_confirmation(
            data.metadata.get("minute_30m"),
            trade_date=data.trade_date,
            box_high=_num(metrics.get("box_high")),
            breakout_trade_date=str(metrics.get("breakout_trade_date") or ""),
            thresholds=self.thresholds,
        )
        checks.update(minute_checks)
        metrics.update(minute_metrics)
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


def is_chan_daily_candidate(data: ScreeningInput) -> bool:
    checks, _ = evaluate_chan_daily_setup(data)
    return bool(checks) and not _has_failed_daily_checks(checks)


def evaluate_chan_daily_setup(
    data: ScreeningInput,
    *,
    thresholds: ChanThirdBuyThresholds | None = None,
) -> tuple[dict[str, bool], dict[str, Any]]:
    config = thresholds or ChanThirdBuyThresholds()
    checks: dict[str, bool] = {
        "not_st": not _is_st_stock(data.stock_basic),
        "not_bse": not _is_bse_stock(data.ts_code, data.stock_basic),
    }
    metrics: dict[str, Any] = {"data_source": data.metadata.get("data_source", "unknown")}

    daily = _prepare_daily(data.daily, trade_date=data.trade_date)
    latest_trade_date = _latest_trade_date(daily)
    metrics.update({"daily_rows": len(daily), "latest_daily_trade_date": latest_trade_date})

    required_rows = config.box_lookback + config.breakout_lookback + config.min_pullback_days + 5
    checks["data_window"] = len(daily) >= required_rows
    if not checks["data_window"]:
        metrics["reason"] = f"需要至少{required_rows}个交易日以识别日线箱体和突破回抽"
        return checks, metrics

    checks["daily_trade_date_current"] = latest_trade_date == data.trade_date
    if not checks["daily_trade_date_current"]:
        metrics["reason"] = "最新日线数据不是请求交易日"
        return checks, metrics

    setup = _find_latest_daily_breakout_setup(daily, config)
    if setup is None:
        checks["daily_third_buy_setup"] = False
        metrics["reason"] = "未找到近10日放量突破后回抽不跌回箱体的日线三买代理结构"
        return checks, metrics

    metrics.update(setup)
    checks.update(
        {
            "box_amplitude_lte_20pct": setup["box_amplitude"] <= config.box_amplitude_max,
            "breakout_volume_ratio_gte_1p2": setup["breakout_volume_ratio"] >= config.breakout_volume_ratio_min,
            "pullback_days_gte_2": setup["pullback_days"] >= config.min_pullback_days,
            "pullback_holds_box": setup["pullback_low"] >= setup["box_high"] * (1.0 - config.box_tolerance),
            "latest_close_near_or_above_box": setup["latest_close"] >= setup["box_high"] * (1.0 - config.box_tolerance),
            "ten_day_gain_lte_25pct": setup["ten_day_gain"] <= config.ten_day_gain_max,
            "ma20_bias_lte_12pct": setup["ma20_bias"] <= config.ma20_bias_max,
        }
    )
    return checks, metrics


def evaluate_chan_minute_confirmation(
    minute_frame: Any,
    *,
    trade_date: str,
    box_high: float,
    breakout_trade_date: str,
    thresholds: ChanThirdBuyThresholds | None = None,
) -> tuple[dict[str, bool], dict[str, Any]]:
    config = thresholds or ChanThirdBuyThresholds()
    checks: dict[str, bool] = {}
    metrics: dict[str, Any] = {}
    minute = _prepare_minute(minute_frame, trade_date=trade_date)
    metrics["minute_30m_rows"] = len(minute)
    metrics["latest_minute_trade_date"] = _latest_trade_date(minute)

    checks["minute_30m_available"] = len(minute) >= config.minute_min_rows
    if not checks["minute_30m_available"]:
        metrics["reason"] = "缺少足够30分钟K线确认数据"
        return checks, metrics

    checks["minute_30m_trade_date_current"] = metrics["latest_minute_trade_date"] == trade_date
    if not checks["minute_30m_trade_date_current"]:
        metrics["reason"] = "最新30分钟K线不是请求交易日"
        return checks, metrics

    minute = minute.copy()
    close = pd.to_numeric(minute["close"], errors="coerce")
    minute["ma5"] = close.rolling(window=config.minute_ma_window, min_periods=config.minute_ma_window).mean()
    minute["macd_hist"] = _macd_histogram(close)

    pullback = minute[minute["trade_date"].astype(str) > str(breakout_trade_date)]
    if pullback.empty:
        pullback = minute[minute["trade_date"].astype(str) >= str(breakout_trade_date)]
    if pullback.empty:
        pullback = minute

    low_index = pullback["low"].astype(float).idxmin()
    latest = minute.iloc[-1]
    minute_pullback_low = _num(pullback.loc[low_index, "low"])
    latest_close = _num(latest.get("close"))
    latest_ma5 = _num(latest.get("ma5"))
    low_macd_hist = _num(minute.loc[low_index, "macd_hist"])
    latest_macd_hist = _num(latest.get("macd_hist"))

    metrics.update(
        {
            "minute_30m_pullback_low": minute_pullback_low,
            "minute_30m_latest_close": latest_close,
            "minute_30m_ma5": latest_ma5,
            "minute_30m_macd_hist_pullback_low": low_macd_hist,
            "minute_30m_macd_hist_latest": latest_macd_hist,
        }
    )
    checks["minute_pullback_holds_box"] = minute_pullback_low >= box_high * (1.0 - config.box_tolerance)
    checks["minute_close_above_ma5"] = latest_ma5 > 0 and latest_close > latest_ma5
    checks["minute_macd_hist_improving"] = latest_macd_hist > low_macd_hist
    return checks, metrics


def _find_latest_daily_breakout_setup(
    daily: pd.DataFrame,
    config: ChanThirdBuyThresholds,
) -> dict[str, Any] | None:
    latest_index = len(daily) - 1
    first_candidate = max(config.box_lookback, latest_index - config.breakout_lookback + 1)
    last_candidate = latest_index - config.min_pullback_days
    if last_candidate < first_candidate:
        return None

    close = pd.to_numeric(daily["close"], errors="coerce")
    ma20 = close.rolling(window=20, min_periods=20).mean()
    latest_close = _num(daily.iloc[-1]["close"])
    ten_day_gain = _window_return(daily, periods=10)
    latest_ma20 = _num(ma20.iloc[-1])
    ma20_bias = (latest_close - latest_ma20) / latest_ma20 if latest_ma20 > 0 else 1.0

    for breakout_index in range(last_candidate, first_candidate - 1, -1):
        box = daily.iloc[breakout_index - config.box_lookback : breakout_index]
        if len(box) < config.box_lookback:
            continue
        box_high = _num(box["high"].max())
        box_low = _num(box["low"].min())
        box_mid = _num(box["close"].mean())
        if box_mid <= 0:
            continue
        box_amplitude = (box_high - box_low) / box_mid
        if box_amplitude > config.box_amplitude_max:
            continue

        breakout = daily.iloc[breakout_index]
        breakout_close = _num(breakout.get("close"))
        if breakout_close <= box_high:
            continue
        breakout_volume_ratio = _volume_ratio_at(daily, breakout_index)
        if breakout_volume_ratio < config.breakout_volume_ratio_min:
            continue

        pullback = daily.iloc[breakout_index + 1 :]
        if len(pullback) < config.min_pullback_days:
            continue
        pullback_low = _num(pullback["low"].min())
        if pullback_low < box_high * (1.0 - config.box_tolerance):
            continue
        if latest_close < box_high * (1.0 - config.box_tolerance):
            continue

        return {
            "box_high": box_high,
            "box_low": box_low,
            "box_amplitude": box_amplitude,
            "breakout_trade_date": str(breakout.get("trade_date") or ""),
            "breakout_close": breakout_close,
            "breakout_volume_ratio": breakout_volume_ratio,
            "pullback_days": len(pullback),
            "pullback_low": pullback_low,
            "latest_close": latest_close,
            "ma20": latest_ma20,
            "ma20_bias": ma20_bias,
            "ten_day_gain": ten_day_gain,
        }
    return None


def _prepare_daily(frame: pd.DataFrame, *, trade_date: str) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame()
    data = _rename_columns(frame.copy())
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
    data = data[data["trade_date"] <= str(trade_date)]
    for column in ["open", "high", "low", "close", "vol", "pct_chg"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["open", "high", "low", "close", "vol"])
    return data.sort_values("trade_date").reset_index(drop=True)


def _prepare_minute(frame: Any, *, trade_date: str) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return pd.DataFrame()
    data = frame.copy()
    if "period" in data.columns:
        data = data[data["period"].astype(str) == "30m"]
    if "trade_date" in data.columns:
        data["trade_date"] = data["trade_date"].astype(str)
        data = data[data["trade_date"] <= str(trade_date)]
    required = ["trade_date", "trade_time", "open", "high", "low", "close"]
    for column in required:
        if column not in data.columns:
            return pd.DataFrame()
    for column in ["open", "high", "low", "close", "vol", "amount"]:
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    data = data.dropna(subset=["trade_date", "trade_time", "open", "high", "low", "close"])
    return data.sort_values("trade_time").reset_index(drop=True)


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


def _macd_histogram(close: pd.Series) -> pd.Series:
    ema_fast = close.ewm(span=12, adjust=False).mean()
    ema_slow = close.ewm(span=26, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=9, adjust=False).mean()
    return (dif - dea) * 2.0


def _volume_ratio_at(data: pd.DataFrame, index: int) -> float:
    if index < 5:
        return 0.0
    latest = _num(data.iloc[index].get("vol"))
    base = _num(pd.to_numeric(data.iloc[index - 5 : index]["vol"], errors="coerce").mean())
    if base <= 0:
        return 0.0
    return latest / base


def _window_return(data: pd.DataFrame, *, periods: int) -> float:
    if len(data) < periods + 1:
        return 1.0
    start = _num(data.iloc[-periods - 1]["close"])
    end = _num(data.iloc[-1]["close"])
    if start <= 0:
        return 1.0
    return end / start - 1.0


def _latest_trade_date(frame: pd.DataFrame) -> str:
    if frame is None or frame.empty or "trade_date" not in frame.columns:
        return ""
    return str(frame["trade_date"].astype(str).max())


def _is_st_stock(stock_basic: dict[str, Any]) -> bool:
    return "ST" in str(stock_basic.get("name") or "").upper()


def _is_bse_stock(ts_code: str, stock_basic: dict[str, Any]) -> bool:
    raw = (ts_code or "").strip().upper()
    code = raw.split(".", 1)[0]
    market = str(stock_basic.get("market") or "")
    exchange = str(stock_basic.get("exchange") or "").upper()
    return raw.endswith(".BJ") or exchange == "BSE" or "北交" in market or code.startswith(("43", "81", "82", "83", "87", "88", "92"))


def _has_failed_daily_checks(checks: dict[str, bool]) -> bool:
    if not checks:
        return True
    return any(not passed for passed in checks.values())


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if pd.isna(value):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _score(metrics: dict[str, Any], matched: list[str], failed: list[str]) -> float:
    score = min(70.0, len(matched) * 5.0)
    if not failed:
        score += 10.0
    volume_ratio = _num(metrics.get("breakout_volume_ratio"))
    ma20_bias = _num(metrics.get("ma20_bias"))
    macd_delta = _num(metrics.get("minute_30m_macd_hist_latest")) - _num(metrics.get("minute_30m_macd_hist_pullback_low"))
    if volume_ratio >= 1.5:
        score += 6.0
    elif volume_ratio >= 1.2:
        score += 3.0
    if 0 <= ma20_bias <= 0.08:
        score += 6.0
    if macd_delta > 0:
        score += min(8.0, macd_delta * 10.0)
    score -= min(len(failed) * 5.0, 35.0)
    return round(max(0.0, min(score, 100.0)), 2)
