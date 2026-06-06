from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pandas as pd
from prompt_toolkit import PromptSession
from prompt_toolkit.shortcuts import checkboxlist_dialog
from prompt_toolkit.styles import Style

from sats.storage.duckdb import DuckDBStorage
from sats.symbols import normalize_symbols

WATCHLIST_DIALOG_STYLE = Style.from_dict(
    {
        "dialog": "bg:#111827",
        "dialog frame.label": "#9ca3af",
        "dialog.body": "bg:#111827 #f9fafb",
        "checkbox-list": "bg:#111827 #f9fafb",
        "checkbox": "#f9fafb",
        "checkbox-selected": "bg:#2563eb #ffffff bold",
        "checkbox-checked": "#22d3ee bold",
        "button": "bg:#374151 #f9fafb",
        "button.text": "bg:#374151 #f9fafb",
        "button.arrow": "bg:#374151 #f9fafb bold",
        "button.focused": "bg:#2563eb #ffffff bold",
        "button.focused button.text": "bg:#2563eb #ffffff bold",
        "button.focused button.arrow": "bg:#2563eb #ffffff bold",
    }
)


@dataclass(frozen=True)
class WatchlistImportResult:
    added: int
    cancelled: bool = False


def format_watchlist(rows: list[dict]) -> str:
    if not rows:
        return "无关注股票"
    lines = []
    for index, row in enumerate(rows, start=1):
        ts_code = str(row.get("ts_code") or "").strip()
        name = str(row.get("name") or "").strip()
        note = str(row.get("note") or "").strip()
        parts = [ts_code]
        if name:
            parts.append(name)
        if note:
            parts.append(f"note={note}")
        lines.append(f"{index}. {' '.join(parts)}")
    return "\n".join(lines)


def upsert_watchlist_symbols(
    storage: DuckDBStorage,
    symbols: list[str],
    *,
    name: str = "",
    note: str = "",
) -> int:
    lookup = _stock_basic_name_lookup(storage)
    count = 0
    for symbol in _unique_symbols(symbols):
        storage.upsert_monitor_watchlist(
            ts_code=symbol,
            name=name or lookup.get(symbol, ""),
            note=note,
        )
        count += 1
    return count


def delete_watchlist_symbols(storage: DuckDBStorage, symbols: list[str]) -> int:
    count = 0
    for symbol in _unique_symbols(symbols):
        if storage.delete_monitor_watchlist(symbol):
            count += 1
    return count


def clear_watchlist(storage: DuckDBStorage) -> int:
    count = 0
    for row in storage.list_monitor_watchlist():
        symbol = str(row.get("ts_code") or "").strip()
        if symbol and storage.delete_monitor_watchlist(symbol):
            count += 1
    return count


def import_screened_to_watchlist(
    storage: DuckDBStorage,
    *,
    trade_date: str,
    rule_name: str | None = None,
    selector: Callable[[list[dict]], list[str] | None] | None = None,
) -> WatchlistImportResult:
    rows = storage.list_screening_stocks(trade_date=trade_date, rule_name=rule_name, passed=True)
    return select_and_import_watchlist(storage, rows, selector=selector)


def select_and_import_watchlist(
    storage: DuckDBStorage,
    rows: list[dict],
    *,
    selector: Callable[[list[dict]], list[str] | None] | None = None,
) -> WatchlistImportResult:
    if not rows:
        print("无可导入股票")
        return WatchlistImportResult(added=0)
    selected = selector(rows) if selector is not None else select_stock_rows(rows, title="加入关注列表", text="选择要加入关注列表的股票")
    if selected is None:
        print("已取消")
        return WatchlistImportResult(added=0, cancelled=True)
    if not selected:
        print("未选择股票")
        return WatchlistImportResult(added=0)
    names = {str(row.get("ts_code") or ""): str(row.get("name") or "") for row in rows}
    count = 0
    for symbol in _unique_symbols(selected):
        storage.upsert_monitor_watchlist(ts_code=symbol, name=names.get(symbol, ""))
        count += 1
    print(f"已加入关注列表 {count} 只股票")
    return WatchlistImportResult(added=count)


def run_watchlist_editor(storage: DuckDBStorage) -> int:
    session = PromptSession()
    while True:
        rows = storage.list_monitor_watchlist()
        print("\n关注列表")
        print("A 添加  D 删除  Q 退出")
        print(format_watchlist(rows))
        try:
            choice = session.prompt("watchlist> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return 0
        if choice in {"q", "quit", "exit"}:
            return 0
        if choice == "a":
            try:
                value = session.prompt("输入股票代码，逗号分隔多个：")
            except (EOFError, KeyboardInterrupt):
                continue
            symbols = parse_symbol_csv(value)
            if not symbols:
                print("未输入股票代码")
                continue
            count = upsert_watchlist_symbols(storage, symbols)
            print(f"已加入关注列表 {count} 只股票")
            continue
        if choice == "d":
            deleted = select_and_delete_watchlist(storage)
            if deleted:
                print(f"已删除 {deleted} 只股票")
            continue
        print("请输入 A、D 或 Q")


def select_and_delete_watchlist(
    storage: DuckDBStorage,
    *,
    selector: Callable[[list[dict]], list[str] | None] | None = None,
) -> int:
    rows = storage.list_monitor_watchlist()
    if not rows:
        print("无关注股票")
        return 0
    selected = selector(rows) if selector is not None else select_stock_rows(rows, title="删除关注股票", text="选择要删除的股票")
    if selected is None:
        print("已取消")
        return 0
    if not selected:
        print("未选择股票")
        return 0
    deleted = delete_watchlist_symbols(storage, selected)
    print(f"已删除 {deleted} 只股票" if deleted else "未找到关注股票")
    return deleted


def select_stock_rows(rows: list[dict], *, title: str, text: str) -> list[str] | None:
    values = [(str(row.get("ts_code") or ""), _row_label(row)) for row in rows]
    return checkboxlist_dialog(
        title=title,
        text=text,
        values=values,
        default_values=[],
        ok_text="确定",
        cancel_text="取消",
        style=WATCHLIST_DIALOG_STYLE,
    ).run()


def parse_symbol_csv(value: str) -> list[str]:
    return _unique_symbols(str(value or "").split(","))


def _row_label(row: dict) -> str:
    ts_code = str(row.get("ts_code") or "").strip()
    name = str(row.get("name") or "").strip()
    labels = _matched_labels(row)
    suffix = " ".join(item for item in (name, ",".join(labels)) if item)
    return f"{ts_code} {suffix}".strip()


def _matched_labels(row: dict) -> list[str]:
    raw_labels = row.get("matched_labels")
    if isinstance(raw_labels, list):
        return [str(label).strip() for label in raw_labels if str(label).strip()]
    metrics = row.get("metrics")
    if isinstance(metrics, dict):
        labels = metrics.get("matched_chan_rules", [])
        if isinstance(labels, list):
            return [str(label).strip() for label in labels if str(label).strip()]
    return []


def _unique_symbols(symbols: list[str] | tuple[str, ...] | object) -> list[str]:
    return normalize_symbols(symbols, required=False)


def _stock_basic_name_lookup(storage: DuckDBStorage) -> dict[str, str]:
    try:
        frame = storage.get_stock_basic()
    except Exception:
        return {}
    if not isinstance(frame, pd.DataFrame) or frame.empty or "ts_code" not in frame.columns:
        return {}
    lookup: dict[str, str] = {}
    for _, row in frame.iterrows():
        symbol = str(row.get("ts_code") or "").strip()
        if symbol:
            lookup[symbol] = str(row.get("name") or "").strip()
    return lookup
