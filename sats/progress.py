from __future__ import annotations

import os
import shutil
import sys
import time
from dataclasses import dataclass
from typing import TextIO

try:
    from prompt_toolkit.utils import get_cwidth
except Exception:  # pragma: no cover - prompt_toolkit is a SATS runtime dependency.
    def get_cwidth(text: str) -> int:
        return len(text)


FILLED_BLOCK = "#"
EMPTY_BLOCK = "-"
DEFAULT_WIDTH = 20
DEFAULT_PANEL_WIDTH = 96
MAX_VISIBLE_ROWS = 8
DETAIL_VISIBLE_ROWS = 3


class NoOpProgressReporter:
    enabled = False

    def step(self, label: str, *, total: int | None = None) -> "NoOpProgressStep":
        return NoOpProgressStep()

    def close(self) -> None:
        return None


class NoOpProgressStep:
    def update(self, current: int | None = None, *, message: str = "") -> None:
        return None

    def advance(self, amount: int = 1, *, message: str = "") -> None:
        return None

    def complete(self, *, message: str = "") -> None:
        return None

    def fail(self, *, message: str = "") -> None:
        return None

    def __enter__(self) -> "NoOpProgressStep":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


@dataclass(slots=True)
class _StepRecord:
    label: str
    total: int | None
    current: int = 0
    state: str = "running"
    detail: str = ""
    started_at: float = 0.0
    ended_at: float | None = None

    @property
    def elapsed(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)


class ConsoleProgressReporter:
    enabled = True

    def __init__(
        self,
        *,
        stream: TextIO | None = None,
        width: int = DEFAULT_WIDTH,
        color: bool | None = None,
        request: str | None = None,
        title: str = "SATS",
    ) -> None:
        self.stream = stream or sys.stderr
        self.width = max(5, int(width))
        self.color = _supports_color(self.stream) if color is None else bool(color)
        self.request = str(request or "").strip()
        self.title = str(title or "SATS").strip() or "SATS"
        self._records: list[_StepRecord] = []
        self._rendered_lines = 0
        self._closed = False
        self._started_at = time.monotonic()

    def step(self, label: str, *, total: int | None = None) -> "ConsoleProgressStep":
        record = _StepRecord(
            label=str(label or "进度").strip() or "进度",
            total=total,
            started_at=time.monotonic(),
        )
        self._records.append(record)
        step = ConsoleProgressStep(self, record=record)
        step.update(0 if total is not None else None)
        return step

    def render(self) -> None:
        if self._closed:
            return
        lines = self._panel_lines()
        if self._rendered_lines:
            self.stream.write(f"\033[{self._rendered_lines}A")
            self.stream.write("\033[J")
        self.stream.write("\n".join(lines))
        self.stream.write("\n")
        self.stream.flush()
        self._rendered_lines = len(lines)

    def close(self) -> None:
        if self._closed:
            return
        if self._records:
            self.render()
        self._closed = True
        self._rendered_lines = 0

    def _panel_lines(self) -> list[str]:
        panel_width = self._panel_width()
        inner_width = panel_width - 2
        title = f" {self.title} "
        title_width = _display_width(title)
        left = max(0, (inner_width - title_width) // 2)
        right = max(0, inner_width - title_width - left)
        top = f"┌{'─' * left}{title}{'─' * right}┐"
        bottom = f"└{'─' * inner_width}┘"
        header = self._fit_line(
            _style("Running agent", "title", self.color),
            self._progress_summary(),
            inner_width,
        )
        request = f"Request: {self.request}" if self.request else "Request:"
        current = self._current_record()
        current_text = "Current"
        if current is not None:
            detail = f" {current.detail}" if current.detail else ""
            current_text = f"Current  {current.label}{detail}"
        rows = [
            top,
            self._body(header, inner_width),
            self._body(_muted(request, self.color), inner_width),
            self._body("", inner_width),
            self._body(_muted(current_text, self.color), inner_width),
            self._body("", inner_width),
            self._body(self._table_header(inner_width), inner_width),
            self._body("  " + "─" * max(0, inner_width - 2), inner_width),
        ]
        for record in self._visible_records():
            if isinstance(record, str):
                rows.append(self._body(_muted(record, self.color), inner_width))
            else:
                rows.append(self._body(self._table_row(record, inner_width), inner_width))
        rows.extend(self._detail_section(inner_width))
        rows.append(bottom)
        return rows

    def _panel_width(self) -> int:
        columns = shutil.get_terminal_size((DEFAULT_PANEL_WIDTH, 24)).columns
        available = max(20, columns - 1)
        return min(160, available)

    def _body(self, text: str, inner_width: int) -> str:
        plain_len = _display_width(text)
        if plain_len > inner_width:
            text = _truncate_ansi(text, inner_width)
            plain_len = _display_width(text)
        return f"│{text}{' ' * max(0, inner_width - plain_len)}│"

    def _fit_line(self, left: str, right: str, width: int) -> str:
        right_width = _display_width(right)
        if right_width >= width:
            return _truncate_ansi(right, width)
        left_width = _display_width(left)
        if left_width + right_width + 2 > width:
            left = _truncate_ansi(left, max(0, width - right_width - 2))
            left_width = _display_width(left)
        return f"{left}{' ' * max(1, width - left_width - right_width)}{right}"

    def _progress_summary(self) -> str:
        total = max(1, len(self._records))
        done = sum(1 for record in self._records if record.state in {"ok", "error"})
        ratio = done / total
        elapsed = _format_elapsed(time.monotonic() - self._started_at)
        return f"{elapsed}  {self._bar(ratio)}  {done}/{total}"

    def _bar(self, ratio: float) -> str:
        ratio = min(1.0, max(0.0, float(ratio)))
        filled = int(round(self.width * ratio))
        return f"{FILLED_BLOCK * filled}{EMPTY_BLOCK * (self.width - filled)}"

    def _current_record(self) -> _StepRecord | None:
        for record in reversed(self._records):
            if record.state == "running":
                return record
        return self._records[-1] if self._records else None

    def _visible_records(self) -> list[_StepRecord | str]:
        records: list[_StepRecord | str]
        if len(self._records) > MAX_VISIBLE_ROWS:
            recent_count = MAX_VISIBLE_ROWS - 1
            hidden_count = len(self._records) - recent_count
            records = [f"... {hidden_count} older steps", *self._records[-recent_count:]]
        else:
            records = list(self._records)
        return records

    def _detail_section(self, inner_width: int) -> list[str]:
        rows = [
            self._body("  " + "─" * max(0, inner_width - 2), inner_width),
            self._body(_muted("  Recent details", self.color), inner_width),
        ]
        for line in self._recent_detail_lines():
            rows.append(self._body(line, inner_width))
        return rows

    def _recent_detail_lines(self) -> list[str]:
        records: list[_StepRecord] = []
        for record in reversed(self._records):
            if str(record.detail or "").strip():
                records.append(record)
            if len(records) >= DETAIL_VISIBLE_ROWS:
                break
        lines = [self._detail_row(record) for record in reversed(records)]
        while len(lines) < DETAIL_VISIBLE_ROWS:
            lines.append("")
        return lines

    def _table_widths(self, inner_width: int) -> tuple[int, int, int, int]:
        state_width = min(8, max(3, inner_width // 5))
        time_width = min(8, max(3, inner_width // 6))
        fixed_width = 2 + state_width + 2 + 2 + time_width + 2
        remaining = max(0, inner_width - fixed_width)
        tool_width = min(22, max(0, remaining // 2))
        detail_width = max(0, remaining - tool_width)
        return state_width, tool_width, time_width, detail_width

    def _table_header(self, inner_width: int) -> str:
        state_width, tool_width, time_width, detail_width = self._table_widths(inner_width)
        text = (
            f"  {_pad_display('State', state_width)}"
            f"  {_pad_display('Tool', tool_width)}"
            f"  {_pad_display('Time', time_width, align='right')}"
            f"  {_truncate_plain('Detail', detail_width)}"
        )
        return _muted(text, self.color)

    def _table_row(self, record: _StepRecord, inner_width: int) -> str:
        state_width, tool_width, time_width, detail_width = self._table_widths(inner_width)
        state = _style(_pad_display(record.state, state_width), record.state, self.color)
        tool = _pad_display(_truncate_plain(record.label, tool_width), tool_width)
        elapsed = _format_elapsed(record.elapsed)
        detail = _truncate_plain(record.detail, detail_width)
        return f"  {state}  {tool}  {_pad_display(elapsed, time_width, align='right')}  {detail}"

    def _detail_row(self, record: _StepRecord) -> str:
        state = _style(record.state, record.state, self.color)
        label = _truncate_plain(record.label, 24)
        detail = _compact_space(record.detail)
        return f"  {state}  {label}: {detail}"


@dataclass
class ConsoleProgressStep:
    reporter: ConsoleProgressReporter
    record: _StepRecord

    @property
    def done(self) -> bool:
        return self.record.state != "running"

    def update(self, current: int | None = None, *, message: str = "") -> None:
        if current is not None:
            self.record.current = int(current)
        if message:
            self.record.detail = str(message)
        elif self.record.total is not None:
            self.record.detail = f"{max(0, self.record.current)}/{max(0, int(self.record.total))}"
        self.reporter.render()

    def advance(self, amount: int = 1, *, message: str = "") -> None:
        self.update(self.record.current + int(amount), message=message)

    def complete(self, *, message: str = "") -> None:
        if self.record.total is not None:
            self.record.current = max(int(self.record.total), self.record.current)
        if message:
            self.record.detail = str(message)
        elif self.record.total is not None:
            self.record.detail = f"{max(0, self.record.current)}/{max(0, int(self.record.total))}"
        self.record.state = "ok"
        self.record.ended_at = time.monotonic()
        self.reporter.render()

    def fail(self, *, message: str = "") -> None:
        if message:
            self.record.detail = str(message)
        self.record.state = "error"
        self.record.ended_at = time.monotonic()
        self.reporter.render()

    def __enter__(self) -> "ConsoleProgressStep":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.done:
            return
        if exc_type is None:
            self.complete()
        else:
            self.fail(message=str(exc) if exc else "")


def create_progress(
    *,
    json_mode: bool = False,
    stream: TextIO | None = None,
    enabled: bool | None = None,
    force: bool = False,
    width: int = DEFAULT_WIDTH,
    request: str | None = None,
    title: str = "SATS",
) -> ConsoleProgressReporter | NoOpProgressReporter:
    if json_mode:
        return NoOpProgressReporter()
    output = stream or sys.stderr
    if force:
        return ConsoleProgressReporter(stream=output, width=width, request=request, title=title)
    if enabled is False:
        return NoOpProgressReporter()
    if enabled is True:
        return ConsoleProgressReporter(stream=output, width=width, request=request, title=title)
    if not _is_tty(output):
        return NoOpProgressReporter()
    return ConsoleProgressReporter(stream=output, width=width, request=request, title=title)


def _is_tty(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _supports_color(stream: TextIO) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    term = os.environ.get("TERM", "")
    return _is_tty(stream) and term.lower() not in {"", "dumb"}


def _style(text: str, kind: str, color: bool) -> str:
    if not color:
        return text
    colors = {
        "title": "36;1",
        "running": "36",
        "ok": "32",
        "error": "31",
    }
    code = colors.get(kind, "37")
    return f"\033[{code}m{text}\033[0m"


def _muted(text: str, color: bool) -> str:
    if not color:
        return text
    return f"\033[90m{text}\033[0m"


def _format_elapsed(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m {secs:02d}s"
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{total}s"


def _truncate_plain(text: str, width: int) -> str:
    value = str(text or "")
    if _display_width(value) <= width:
        return value
    if width <= 3:
        return _slice_display(value, width)
    return _slice_display(value, width - 3) + "..."


def _compact_space(text: str) -> str:
    return " ".join(str(text or "").split())


def _truncate_ansi(value: str, width: int) -> str:
    plain = _strip_ansi(value)
    if _display_width(plain) <= width:
        return value
    return _truncate_plain(plain, width)


def _display_width(value: str) -> int:
    return get_cwidth(_strip_ansi(str(value or "")))


def _slice_display(value: str, width: int) -> str:
    if width <= 0:
        return ""
    chars: list[str] = []
    used = 0
    for char in str(value or ""):
        char_width = get_cwidth(char)
        if used + char_width > width:
            break
        chars.append(char)
        used += char_width
    return "".join(chars)


def _pad_display(value: str, width: int, *, align: str = "left") -> str:
    text = _truncate_plain(value, width)
    padding = max(0, width - _display_width(text))
    if align == "right":
        return f"{' ' * padding}{text}"
    return f"{text}{' ' * padding}"


def _strip_ansi(value: str) -> str:
    result = []
    in_escape = False
    for char in value:
        if char == "\033":
            in_escape = True
            continue
        if in_escape:
            if char.isalpha():
                in_escape = False
            continue
        result.append(char)
    return "".join(result)
