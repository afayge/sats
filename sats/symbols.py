from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def normalize_ts_code(value: Any) -> str:
    raw = str(value or "").strip().upper()
    if len(raw) == 9 and raw[:6].isdigit() and raw[6] == ".":
        return raw
    if len(raw) != 6 or not raw.isdigit():
        return raw
    if raw.startswith("6"):
        return f"{raw}.SH"
    if raw.startswith(("0", "3")):
        return f"{raw}.SZ"
    if raw.startswith(("4", "8", "9")):
        return f"{raw}.BJ"
    return raw


def normalize_symbols(values: Iterable[Any] | Any, *, required: bool = True) -> list[str]:
    if isinstance(values, str):
        raw_values: Iterable[Any] = [values]
    elif values is None:
        raw_values = []
    elif isinstance(values, Iterable):
        raw_values = values
    else:
        raw_values = [values]
    result: list[str] = []
    seen: set[str] = set()
    for raw in raw_values:
        symbol = normalize_ts_code(raw)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        result.append(symbol)
    if required and not result:
        raise ValueError("At least one symbol is required")
    return result


def parse_symbol_csv(value: str, *, required: bool = True) -> list[str]:
    return normalize_symbols([item.strip() for item in str(value or "").split(",")], required=required)
