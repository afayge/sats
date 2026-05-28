from __future__ import annotations

import curses
import shutil
import time
from dataclasses import dataclass
from typing import Any

import pandas as pd
from prompt_toolkit.utils import get_cwidth

from sats.config import Settings
from sats.data.astock_provider import AStockDataProvider
from sats.storage.duckdb import DuckDBStorage


@dataclass(slots=True)
class MonitorDisplaySnapshot:
    runtime: dict
    positions: list[dict]
    watchlist: list[dict]
    buy_candidates: list[dict]
    trade_events: list[dict]
    monitor_events: list[dict]
    scheduled_runs: list[dict]
    quote_error: str = ""


class MonitorDisplay:
    def __init__(
        self,
        *,
        settings: Settings,
        storage: DuckDBStorage,
        provider: AStockDataProvider | None = None,
        refresh_seconds: int = 3,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.provider = provider or AStockDataProvider(settings)
        self.refresh_seconds = max(1, int(refresh_seconds))

    def snapshot(self) -> MonitorDisplaySnapshot:
        positions = self.storage.list_monitor_positions(enabled=True)
        watchlist = self.storage.list_monitor_watchlist(enabled=True)
        buy_candidates = self.storage.list_monitor_buy_candidates(enabled=True)
        quote_error = ""
        symbols = _unique_symbols([row.get("ts_code") for row in positions] + [row.get("ts_code") for row in watchlist])
        try:
            quotes = self.provider.load_realtime_quotes(symbols=symbols) if symbols else pd.DataFrame()
            quote_lookup = _quote_lookup(quotes)
            positions = [_position_with_quote(row, quote_lookup.get(str(row["ts_code"]), {})) for row in positions]
            watchlist = [_watchlist_with_quote(row, quote_lookup.get(str(row["ts_code"]), {})) for row in watchlist]
        except Exception as exc:
            quote_error = str(exc)
            positions = [_position_with_quote(row, {}) for row in positions]
            watchlist = [_watchlist_with_quote(row, {}) for row in watchlist]
        return MonitorDisplaySnapshot(
            runtime=self.storage.get_monitor_runtime("monitor"),
            positions=positions,
            watchlist=watchlist,
            buy_candidates=buy_candidates,
            trade_events=self.storage.list_monitor_trade_events(limit=50),
            monitor_events=self.storage.list_monitor_events(limit=100),
            scheduled_runs=self.storage.list_scheduled_task_runs(limit=20),
            quote_error=quote_error,
        )

    def run(self) -> None:
        curses.wrapper(self._run_curses)

    def _run_curses(self, stdscr) -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        if curses.has_colors():
            curses.start_color()
            curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
            curses.init_pair(2, curses.COLOR_GREEN, curses.COLOR_BLACK)
            curses.init_pair(3, curses.COLOR_CYAN, curses.COLOR_BLACK)
        while True:
            key = stdscr.getch()
            if key in {ord("q"), ord("Q")}:
                break
            snapshot = self.snapshot()
            _draw_dashboard(stdscr, snapshot)
            time.sleep(self.refresh_seconds)


def format_monitor_dashboard(snapshot: MonitorDisplaySnapshot, *, width: int | None = None, height: int | None = None) -> str:
    size = shutil.get_terminal_size((120, 30))
    return "\n".join(
        render_monitor_dashboard_lines(
            snapshot,
            width=width or size.columns,
            height=height or size.lines,
        )
    )


def render_monitor_dashboard_lines(snapshot: MonitorDisplaySnapshot, *, width: int, height: int) -> list[str]:
    panel_width = max(80, int(width))
    panel_height = max(20, int(height))
    inner_width = panel_width - 2
    top_height = max(9, min(panel_height - 8, int(panel_height * 0.6)))
    info_height = max(4, panel_height - top_height - 2)
    watch_width = max(42, min(52, int(inner_width * 0.24)))
    position_width = max(1, inner_width - watch_width - 1)

    lines = [_top_border(panel_width, "monitor")]
    lines.extend(_top_panel_lines(snapshot, watch_width=watch_width, position_width=position_width, rows=max(5, top_height - 1)))
    lines.append(_separator(panel_width))
    lines.extend(_info_panel_lines(snapshot, width=inner_width, rows=max(2, info_height - 2)))
    lines.append(_bottom_border(panel_width))
    return lines[:panel_height]


def _draw_dashboard(stdscr, snapshot: MonitorDisplaySnapshot) -> None:
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    for idx, line in enumerate(render_monitor_dashboard_lines(snapshot, width=max(1, width - 1), height=height)):
        _safe_addstr(stdscr, idx, 0, line[: max(1, width - 1)], _line_attr(line))
    stdscr.refresh()


def _top_panel_lines(snapshot: MonitorDisplaySnapshot, *, watch_width: int, position_width: int, rows: int) -> list[str]:
    left_lines = _watchlist_lines(snapshot.watchlist, watch_width, rows)
    right_lines = _position_lines(snapshot.positions, position_width, rows)
    result = []
    for index in range(rows):
        left = left_lines[index] if index < len(left_lines) else " " * watch_width
        right = right_lines[index] if index < len(right_lines) else " " * position_width
        result.append(f"│{left}│{right}│")
    return result


def _watchlist_lines(rows: list[dict], width: int, rows_limit: int) -> list[str]:
    content_width = max(1, width - 2)
    table_widths = _watchlist_widths(content_width)
    lines = [
        _segment_title("watchList", width),
        _segment_line(_table_row(["NO", "股票代码", "股票名称", "价格", "涨幅"], table_widths), width),
        _segment_line("─" * content_width, width),
    ]
    for index, row in enumerate(rows[: max(0, rows_limit - len(lines))], start=1):
        lines.append(
            _segment_line(
                _table_row(
                    [
                        str(index),
                        str(row.get("ts_code") or ""),
                        str(row.get("name") or ""),
                        _fmt(row.get("current_price")),
                        _fmt_pct(row.get("pct_chg")),
                    ],
                    table_widths,
                ),
                width,
            )
        )
    while len(lines) < rows_limit:
        lines.append(" " * width)
    return lines


def _position_lines(rows: list[dict], width: int, rows_limit: int) -> list[str]:
    content_width = max(1, width - 2)
    table_widths = _position_widths(content_width)
    lines = [
        _segment_title("positions", width),
        _segment_line(_table_row(["NO", "股票代码", "股票名称", "买入时间", "成本价", "数量", "实时价格", "盈亏", "盈亏比"], table_widths), width),
        _segment_line("─" * content_width, width),
    ]
    for index, row in enumerate(rows[: max(0, rows_limit - len(lines))], start=1):
        lines.append(
            _segment_line(
                _table_row(
                    [
                        str(index),
                        str(row.get("ts_code") or ""),
                        str(row.get("name") or ""),
                        str(row.get("buy_date") or ""),
                        _fmt(row.get("buy_price")),
                        _fmt_quantity(row.get("quantity")),
                        _fmt(row.get("current_price")),
                        _fmt_signed(row.get("pnl_amount")),
                        _fmt_pct(row.get("pnl_pct")),
                    ],
                    table_widths,
                ),
                width,
            )
        )
    while len(lines) < rows_limit:
        lines.append(" " * width)
    return lines


def _info_panel_lines(snapshot: MonitorDisplaySnapshot, *, width: int, rows: int) -> list[str]:
    lines = [f"│{_pad_to_width(' Info', width)}│"]
    info_rows = _info_rows(snapshot)
    for row in info_rows[:rows]:
        lines.append(f"│{_pad_to_width(' ' + _truncate_to_width(row, max(1, width - 2)), width)}│")
    while len(lines) < rows + 1:
        lines.append(f"│{' ' * width}│")
    return lines


def _info_rows(snapshot: MonitorDisplaySnapshot) -> list[str]:
    rows = []
    runtime = snapshot.runtime
    rows.append(f"运行 monitor {runtime.get('status', 'stopped')} PID {runtime.get('pid') or ''} 心跳 {runtime.get('heartbeat_at', '')}".strip())
    if snapshot.quote_error:
        rows.append(f"行情错误: {snapshot.quote_error}")
    for item in snapshot.buy_candidates[:10]:
        rows.append(f"待买 {item.get('ts_code')} {item.get('name', '')} {item.get('signal_label', '')}")
    for item in snapshot.trade_events[:20]:
        rows.append(f"交易 {item.get('created_at', '')} {item.get('ts_code')} {item.get('action')} {item.get('status')} {item.get('message', '')}")
    for item in snapshot.scheduled_runs[:10]:
        rows.append(f"定时任务 {_scheduled_run_line(item)}")
    for item in snapshot.monitor_events[:20]:
        rows.append(f"监控 {item.get('created_at', '')} {item.get('message', '')}")
    return rows or ["无信息"]


def _watchlist_widths(width: int) -> list[int]:
    fixed = [2, 9, 7, 8]
    spaces = 4
    name_width = max(4, width - sum(fixed) - spaces)
    return [2, 9, name_width, 7, 8]


def _position_widths(width: int) -> list[int]:
    fixed = [2, 9, 10, 6, 6, 8, 7, 8]
    spaces = 8
    name_width = max(8, width - sum(fixed) - spaces)
    return [2, 9, name_width, 10, 6, 6, 8, 7, 8]


def _table_row(values: list[str], widths: list[int]) -> str:
    cells = [_pad_to_width(_truncate_to_width(str(value), width), width) for value, width in zip(values, widths, strict=True)]
    return " ".join(cells)


def _segment_title(title: str, width: int) -> str:
    return _pad_to_width(_center_text(title, max(1, width - 2)), width)


def _segment_line(text: str, width: int) -> str:
    return f" {_pad_to_width(_truncate_to_width(text, max(1, width - 2)), max(1, width - 2))} "


def _top_border(width: int, title: str) -> str:
    label = f" {title} "
    inner_width = width - 2
    if _display_width(label) >= inner_width:
        return f"┌{'─' * inner_width}┐"
    left = (inner_width - _display_width(label)) // 2
    right = inner_width - left - _display_width(label)
    return f"┌{'─' * left}{label}{'─' * right}┐"


def _separator(width: int) -> str:
    return f"├{'─' * max(0, width - 2)}┤"


def _bottom_border(width: int) -> str:
    return f"└{'─' * max(0, width - 2)}┘"


def _center_text(text: str, width: int) -> str:
    value = _truncate_to_width(text, width)
    total = max(0, width - _display_width(value))
    left = total // 2
    right = total - left
    return f"{' ' * left}{value}{' ' * right}"


def _scheduled_run_line(row: dict) -> str:
    duration = row.get("duration_seconds")
    duration_text = "" if duration is None else f"{float(duration):.1f}s"
    message = str(row.get("error") or row.get("output_text") or "").strip().replace("\n", " ")
    if len(message) > 80:
        message = f"{message[:77]}..."
    return f"{row.get('finished_at', '')} {row.get('task_name')} {row.get('status')} {duration_text} {message}".strip()


def _position_with_quote(row: dict, quote: dict) -> dict:
    result = dict(row)
    price = _num(quote.get("close"))
    buy_price = _num(row.get("buy_price"))
    quantity = _num(row.get("quantity"))
    result["current_price"] = price if price > 0 else None
    result["pnl_amount"] = (price - buy_price) * quantity if price > 0 and buy_price > 0 and quantity > 0 else None
    result["pnl_pct"] = ((price / buy_price - 1.0) * 100.0) if price > 0 and buy_price > 0 else None
    return result


def _watchlist_with_quote(row: dict, quote: dict) -> dict:
    result = dict(row)
    price = _num(quote.get("close"))
    result["current_price"] = price if price > 0 else None
    result["pct_chg"] = _optional_num(quote.get("pct_chg"))
    return result


def _line_attr(line: str) -> int:
    if not curses.has_colors():
        return 0
    tokens = str(line).split()
    if any(token.startswith("+") and any(char.isdigit() for char in token) for token in tokens):
        return curses.color_pair(1)
    if any(token.startswith("-") and any(char.isdigit() for char in token) for token in tokens):
        return curses.color_pair(2)
    if " Info" in line:
        return curses.A_REVERSE
    return 0


def _quote_lookup(frame: pd.DataFrame) -> dict[str, dict]:
    if frame is None or frame.empty or "ts_code" not in frame.columns:
        return {}
    return {str(row["ts_code"]): row.dropna().to_dict() for _, row in frame.iterrows()}


def _safe_addstr(stdscr, y: int, x: int, text: str, attr: int = 0) -> None:
    try:
        stdscr.addstr(y, x, str(text), attr)
    except curses.error:
        pass


def _unique_symbols(values: list[Any]) -> list[str]:
    result = []
    for value in values:
        symbol = str(value or "").strip()
        if symbol and symbol not in result:
            result.append(symbol)
    return result


def _num(value: Any) -> float:
    try:
        if pd.isna(value):
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_num(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any) -> str:
    number = _num(value)
    return "" if number == 0 else f"{number:.2f}"


def _fmt_signed(value: Any) -> str:
    if value is None:
        return ""
    number = _num(value)
    return "" if number == 0 else f"{number:+.2f}"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return ""
    number = _num(value)
    return "" if number == 0 else f"{number:+.2f}%"


def _fmt_quantity(value: Any) -> str:
    number = _num(value)
    if number == 0:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}"


def _display_width(text: str) -> int:
    return get_cwidth(str(text))


def _pad_to_width(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _display_width(str(text)))


def _truncate_to_width(text: str, width: int) -> str:
    value = str(text)
    if _display_width(value) <= width:
        return value
    if width <= 0:
        return ""
    if width <= 3:
        return "." * width
    limit = width - 3
    output = ""
    current = 0
    for char in value:
        char_width = _display_width(char)
        if current + char_width > limit:
            break
        output += char
        current += char_width
    return f"{output}..."
