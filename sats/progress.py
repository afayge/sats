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
START_MARK = "→"
UPDATE_MARK = "·"
OK_MARK = "✓"
ERROR_MARK = "✗"


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
    last_detail_line: str = ""

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
        self._lines: list[str] = []
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
        self._emit(record, state="running")
        return step

    def render(self) -> None:
        return None

    def close(self) -> None:
        self._closed = True

    def _emit(self, record: _StepRecord, *, state: str, detail: str = "") -> None:
        if self._closed:
            return
        line = self._line(record, state=state, detail=detail)
        self._lines.append(line)
        self.stream.write(line)
        self.stream.write("\n")
        self.stream.flush()

    def _line(self, record: _StepRecord, *, state: str, detail: str = "") -> str:
        mark = {
            "running": START_MARK,
            "update": UPDATE_MARK,
            "ok": OK_MARK,
            "error": ERROR_MARK,
        }.get(state, UPDATE_MARK)
        label = _compact_space(record.label)
        if state == "running":
            text = f"{_style(mark, 'running', self.color)} {label}"
        else:
            parts = [f"{_style(mark, state, self.color)} {label}", _format_elapsed(record.elapsed)]
            compact_detail = _compact_space(detail or record.detail)
            if compact_detail:
                parts.append(compact_detail)
            text = f"{parts[0]} {parts[1]}" + (f"  {parts[2]}" if len(parts) > 2 else "")
        return _truncate_ansi(text, self._line_width())

    def _line_width(self) -> int:
        columns = shutil.get_terminal_size((DEFAULT_PANEL_WIDTH, 24)).columns
        return min(160, max(20, columns - 1))

    def _panel_lines(self) -> list[str]:
        return list(self._lines)


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
        detail = _compact_space(self.record.detail)
        if detail and detail != self.record.last_detail_line:
            self.record.last_detail_line = detail
            self.reporter._emit(self.record, state="update", detail=detail)

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
        self.reporter._emit(self.record, state="ok", detail=self.record.detail)

    def fail(self, *, message: str = "") -> None:
        if message:
            self.record.detail = str(message)
        self.record.state = "error"
        self.record.ended_at = time.monotonic()
        self.reporter._emit(self.record, state="error", detail=self.record.detail)

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
        "update": "36",
        "ok": "32",
        "error": "31",
    }
    code = colors.get(kind, "37")
    return f"\033[{code}m{text}\033[0m"


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
