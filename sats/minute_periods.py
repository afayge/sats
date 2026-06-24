from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any

import pandas as pd


NATIVE_MINUTE_PERIODS = ("1m", "5m", "15m", "30m", "60m")
MAX_MINUTE_PERIOD_MINUTES = 240

_NATIVE_MINUTES = tuple(int(period[:-1]) for period in NATIVE_MINUTE_PERIODS)
_MINUTE_PERIOD_RE = re.compile(r"^\s*(?P<amount>\d+)\s*(?P<unit>m|min|mins|minute|minutes|分钟|分)\s*$", re.IGNORECASE)
_LOWER_M_RE = re.compile(r"^\s*\d+\s*m\s*$")
_MINUTE_ALIAS_RE = re.compile(r"^\s*\d+\s*(?:min|mins|minute|minutes|分钟|分)\s*$", re.IGNORECASE)


def looks_like_minute_period(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    return bool(_MINUTE_ALIAS_RE.match(text) or (_LOWER_M_RE.match(text) and text.endswith("m")))


def normalize_minute_period(value: Any) -> str:
    text = str(value or "").strip()
    match = _MINUTE_PERIOD_RE.match(text)
    if not match:
        raise ValueError(_unsupported_period_message(value))
    minutes = int(match.group("amount"))
    if minutes < 1 or minutes > MAX_MINUTE_PERIOD_MINUTES:
        raise ValueError(_unsupported_period_message(value))
    return f"{minutes}m"


def minute_period_minutes(value: Any) -> int:
    return int(normalize_minute_period(value)[:-1])


def native_minute_base_period(value: Any) -> str:
    target_minutes = minute_period_minutes(value)
    for minutes in sorted(_NATIVE_MINUTES, reverse=True):
        if minutes <= target_minutes and target_minutes % minutes == 0:
            return f"{minutes}m"
    return "1m"


def native_minute_count_for(count: int | None, *, target_period: str, base_period: str) -> int | None:
    if count is None:
        return None
    target_minutes = minute_period_minutes(target_period)
    base_minutes = minute_period_minutes(base_period)
    return max(1, int(count)) * max(1, target_minutes // base_minutes)


def tail_minute_klines(frame: pd.DataFrame, count: int | None) -> pd.DataFrame:
    if frame is None or frame.empty or count is None or "ts_code" not in frame.columns:
        return frame
    sort_column = "trade_time" if "trade_time" in frame.columns else "datetime" if "datetime" in frame.columns else None
    data = frame.sort_values(["ts_code", sort_column]) if sort_column else frame
    return data.groupby("ts_code", group_keys=False).tail(max(1, int(count))).reset_index(drop=True)


def aggregate_minute_klines(frame: pd.DataFrame, *, target_period: str, source_period: str | None = None) -> pd.DataFrame:
    target = normalize_minute_period(target_period)
    source = normalize_minute_period(source_period or native_minute_base_period(target))
    if frame is None or frame.empty:
        return frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame()
    if target == source:
        data = frame.copy()
        if "period" in data.columns:
            data["period"] = target
        return data

    data = frame.copy()
    time_column = "trade_time" if "trade_time" in data.columns else "datetime" if "datetime" in data.columns else ""
    if not time_column or "ts_code" not in data.columns:
        return pd.DataFrame(columns=list(data.columns))

    data["_parsed_trade_time"] = pd.to_datetime(data[time_column], errors="coerce")
    buckets = data["_parsed_trade_time"].map(lambda value: _session_bucket(value, minute_period_minutes(target)))
    valid = buckets.notna()
    if not valid.any():
        return pd.DataFrame(columns=list(data.columns))
    data = data.loc[valid].copy()
    data["_session"] = buckets.loc[valid].map(lambda item: item[0])
    data["_bucket_index"] = buckets.loc[valid].map(lambda item: item[1])
    data["_bucket_end"] = buckets.loc[valid].map(lambda item: item[2])
    data["trade_date"] = data["_bucket_end"].map(lambda value: value.strftime("%Y%m%d"))
    data = data.sort_values(["ts_code", "_bucket_end", "_parsed_trade_time"])
    for column in ("open", "high", "low", "close", "vol", "amount"):
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")
    group_keys = ["ts_code", "trade_date", "_session", "_bucket_index", "_bucket_end"]
    grouped = data.groupby(group_keys, sort=True, dropna=False)
    result = grouped.agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        vol=("vol", "sum"),
        amount=("amount", "sum"),
    ).reset_index()
    result["period"] = target
    result["trade_time"] = result["_bucket_end"].map(lambda value: value.strftime("%Y-%m-%d %H:%M:%S"))
    if "datetime" in frame.columns:
        result["datetime"] = result["trade_time"]
    source_label = _derived_source_label(frame, target_period=target, source_period=source)
    result["data_source"] = source_label
    columns = [
        "ts_code",
        "period",
        "trade_date",
        "trade_time",
        "open",
        "high",
        "low",
        "close",
        "vol",
        "amount",
        "data_source",
    ]
    if "datetime" in result.columns:
        columns.insert(3, "datetime")
    result = result[columns].dropna(subset=["ts_code", "trade_date", "trade_time", "open", "high", "low", "close"])
    result = result.drop_duplicates(subset=["ts_code", "period", "trade_time"], keep="last").reset_index(drop=True)
    result.attrs["data_source"] = source_label
    if "tickflow" in source_label:
        result.attrs["tickflow_source"] = source_label
    return result


def _session_bucket(value: pd.Timestamp, target_minutes: int) -> tuple[str, int, datetime] | None:
    if pd.isna(value):
        return None
    timestamp = value.to_pydatetime() if isinstance(value, pd.Timestamp) else value
    session = _session_bounds(timestamp)
    if session is None:
        return None
    label, start, end = session
    elapsed = int((timestamp - start).total_seconds() // 60)
    elapsed = max(1, elapsed)
    bucket_index = (elapsed - 1) // target_minutes
    bucket_end = start + timedelta(minutes=(bucket_index + 1) * target_minutes)
    if bucket_end > end:
        bucket_end = end
    return label, bucket_index, bucket_end


def _session_bounds(value: datetime) -> tuple[str, datetime, datetime] | None:
    current = value.time()
    trade_date = value.date()
    morning_start = datetime.combine(trade_date, time(9, 30))
    morning_end = datetime.combine(trade_date, time(11, 30))
    afternoon_start = datetime.combine(trade_date, time(13, 0))
    afternoon_end = datetime.combine(trade_date, time(15, 0))
    if time(9, 30) <= current <= time(11, 30):
        return "am", morning_start, morning_end
    if time(13, 0) <= current <= time(15, 0):
        return "pm", afternoon_start, afternoon_end
    return None


def _derived_source_label(frame: pd.DataFrame, *, target_period: str, source_period: str) -> str:
    source = str(frame.attrs.get("data_source") or frame.attrs.get("tickflow_source") or "").strip()
    if not source and "data_source" in frame.columns:
        values = [str(item).strip() for item in frame["data_source"].dropna().unique().tolist() if str(item).strip()]
        source = "+".join(values[:3])
    source = source or "minute_k"
    return f"{source}_derived_{target_period}_from_{source_period}"


def _unsupported_period_message(value: Any) -> str:
    native = ", ".join(NATIVE_MINUTE_PERIODS)
    return (
        f"Unsupported minute K period: {value}. "
        f"Use integer minute periods from 1m to {MAX_MINUTE_PERIOD_MINUTES}m; native periods: {native}"
    )
