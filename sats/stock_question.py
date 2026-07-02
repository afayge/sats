from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sats.symbols import normalize_symbols


_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9])([034689]\d{5}(?:\.(?:SH|SZ|BJ))?)(?![A-Za-z0-9])", re.IGNORECASE)
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})([-/]?)(\d{2})\2(\d{2})(?!\d)")
_TIME_RE = re.compile(r"(?<!\d)((?:[01]?\d|2[0-3]):[0-5]\d)(?!\d)")
_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_RELATIVE_TRADE_DATE_TERMS = (
    (("前天", "day_before_yesterday", "day before yesterday"), 2),
    (("昨天", "昨日", "yesterday"), 1),
    (("今天", "今日", "today", "current"), 0),
)


@dataclass(frozen=True, slots=True)
class StockQuestion:
    symbols: list[str]
    trade_date: str | None = None
    as_of_time: str | None = None
    has_stock_question: bool = False


def parse_stock_question(message: str) -> StockQuestion:
    text = str(message or "").strip()
    symbols = extract_stock_symbols(text)
    if not symbols:
        return StockQuestion(symbols=[], has_stock_question=False)
    return StockQuestion(
        symbols=symbols,
        trade_date=extract_natural_trade_date(text),
        as_of_time=extract_intraday_time(text),
        has_stock_question=True,
    )


def extract_stock_symbols(message: str) -> list[str]:
    raw_symbols: list[str] = []
    seen: set[str] = set()
    for match in _SYMBOL_RE.finditer(str(message or "")):
        raw = match.group(1).upper()
        if raw not in seen:
            seen.add(raw)
            raw_symbols.append(raw)
    return normalize_symbols(raw_symbols, required=False)


def extract_trade_date(message: str) -> str | None:
    match = _DATE_RE.search(str(message or ""))
    if not match:
        return None
    value = "".join([match.group(1), match.group(3), match.group(4)])
    try:
        datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"交易日格式无效: {match.group(0)}") from exc
    return value


def extract_natural_trade_date(message: str, *, today: str | None = None) -> str | None:
    explicit = extract_trade_date(message)
    if explicit:
        return explicit
    text = str(message or "")
    if not text:
        return None
    base = _normalize_today(today) if today else datetime.now(_SHANGHAI_TZ)
    for terms, offset in _RELATIVE_TRADE_DATE_TERMS:
        if _contains_relative_trade_date_term(text, terms):
            return (base - timedelta(days=offset)).strftime("%Y%m%d")
    return None


def _normalize_today(today: str | None) -> datetime:
    value = extract_trade_date(today or "")
    if not value:
        raise ValueError(f"日期格式无效: {today}")
    return datetime.strptime(value, "%Y%m%d")


def _contains_relative_trade_date_term(text: str, terms: tuple[str, ...]) -> bool:
    value = str(text or "")
    lowered = value.lower()
    for term in terms:
        if _contains_cjk(term):
            if term in value:
                return True
            continue
        pattern = r"(?<![a-z0-9_])" + re.escape(term.lower()) + r"(?![a-z0-9_])"
        if re.search(pattern, lowered):
            return True
    return False


def _contains_cjk(value: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(value or ""))


def extract_intraday_time(message: str) -> str | None:
    match = _TIME_RE.search(str(message or ""))
    if not match:
        return None
    hour, minute = match.group(1).split(":", 1)
    return f"{int(hour):02d}:{minute}:00"
