from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def current_shanghai_datetime() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def current_shanghai_trade_date() -> str:
    return current_shanghai_datetime().strftime("%Y%m%d")


def is_a_share_trading_time_now() -> bool:
    now = current_shanghai_datetime()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    morning_start = 9 * 60 + 30
    morning_end = 11 * 60 + 30
    afternoon_start = 13 * 60
    afternoon_end = 15 * 60
    return morning_start <= minutes <= morning_end or afternoon_start <= minutes <= afternoon_end


def is_current_trading_session_date(trade_date: str) -> bool:
    return str(trade_date) == current_shanghai_trade_date() and is_a_share_trading_time_now()
