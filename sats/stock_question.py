from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sats.symbols import normalize_symbols


_SYMBOL_RE = re.compile(r"(?<![A-Za-z0-9])([034689]\d{5}(?:\.(?:SH|SZ|BJ))?)(?![A-Za-z0-9])", re.IGNORECASE)
_DATE_RE = re.compile(r"(?<!\d)(20\d{2})([-/]?)(\d{2})\2(\d{2})(?!\d)")
_TIME_RE = re.compile(r"(?<!\d)((?:[01]?\d|2[0-3]):[0-5]\d)(?!\d)")


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
        trade_date=extract_trade_date(text),
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


def extract_intraday_time(message: str) -> str | None:
    match = _TIME_RE.search(str(message or ""))
    if not match:
        return None
    hour, minute = match.group(1).split(":", 1)
    return f"{int(hour):02d}:{minute}:00"
