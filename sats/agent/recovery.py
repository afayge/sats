from __future__ import annotations

import hashlib
import re
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*([^\s,;}]+)"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{12,}\b"),
)
_VOLATILE = re.compile(r"\b(?:0x[0-9a-f]+|\d{4,}|[0-9a-f]{16,})\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FailureFrame:
    path: str
    line: int
    function: str


@dataclass(frozen=True, slots=True)
class AgentFailure:
    failure_id: str
    category: str
    stage: str
    tool: str
    exception_type: str
    message: str
    frames: tuple[FailureFrame, ...]
    fingerprint: str
    retryable: bool
    repair_level: str
    attempt: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["frames"] = [asdict(frame) for frame in self.frames]
        value["metadata"] = dict(self.metadata)
        return value


@dataclass(frozen=True, slots=True)
class RecoveryAttempt:
    attempt: int
    strategy: str
    status: str
    failure_id: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def capture_exception(
    exc: BaseException,
    *,
    project_root: Path | str,
    stage: str,
    tool: str = "",
    attempt: int = 1,
    category: str = "",
) -> AgentFailure:
    root = Path(project_root).resolve()
    trace = traceback.TracebackException.from_exception(
        exc,
        capture_locals=False,
        compact=True,
        max_group_width=10,
        max_group_depth=5,
    )
    frames = tuple(_project_frames(trace, root))
    exception_type = type(exc).__name__
    message = redact_text("".join(trace.format_exception_only()).strip(), project_root=root)
    selected = category or classify_failure(message, exception_type=exception_type, stage=stage, tool=tool)
    retryable, repair_level = recovery_policy(selected)
    fingerprint = failure_fingerprint(
        category=selected,
        tool=tool,
        exception_type=exception_type,
        message=message,
        frames=frames,
    )
    return AgentFailure(
        failure_id=f"fail_{uuid.uuid4().hex[:16]}",
        category=selected,
        stage=str(stage or "runtime"),
        tool=str(tool or ""),
        exception_type=exception_type,
        message=message,
        frames=frames,
        fingerprint=fingerprint,
        retryable=retryable,
        repair_level=repair_level,
        attempt=max(1, int(attempt)),
    )


def failure_from_message(
    message: str,
    *,
    project_root: Path | str,
    stage: str,
    tool: str = "",
    attempt: int = 1,
    exception_type: str = "AgentToolError",
    category: str = "",
    frames: Iterable[FailureFrame] = (),
) -> AgentFailure:
    clean = redact_text(message, project_root=Path(project_root).resolve())
    selected = category or classify_failure(clean, exception_type=exception_type, stage=stage, tool=tool)
    retryable, repair_level = recovery_policy(selected)
    frame_tuple = tuple(frames)
    return AgentFailure(
        failure_id=f"fail_{uuid.uuid4().hex[:16]}",
        category=selected,
        stage=str(stage or "runtime"),
        tool=str(tool or ""),
        exception_type=exception_type,
        message=clean,
        frames=frame_tuple,
        fingerprint=failure_fingerprint(
            category=selected,
            tool=tool,
            exception_type=exception_type,
            message=clean,
            frames=frame_tuple,
        ),
        retryable=retryable,
        repair_level=repair_level,
        attempt=max(1, int(attempt)),
    )


def classify_failure(message: str, *, exception_type: str = "", stage: str = "", tool: str = "") -> str:
    text = f"{exception_type} {stage} {tool} {message}".lower()
    if any(token in text for token in ("requires --live-trading", "requires explicit --auto-trade", "auto-trade does not allow")):
        return "trade_permission"
    if "requires explicit" in text or "confirmation required" in text or "approval" in text:
        return "permission_required"
    if "trade" in text and any(token in text for token in ("blocked", "permission", "disabled", "not allowed")):
        return "trade_blocked"
    if any(token in text for token in ("modulenotfounderror", "importerror", "no module named", "dependency")):
        return "dependency_error"
    if any(token in text for token in ("timeout", "timed out", "timeouterror")):
        return "timeout"
    if any(token in text for token in ("connectionerror", "connection refused", "connection reset", "dns", "network is unreachable")):
        return "connection_error"
    if any(token in text for token in ("rate limit", "ratelimit", "too many requests", "http 429")):
        return "rate_limit"
    if any(token in text for token in ("missing required argument", "must be string", "must be integer", "must be number", "invalid argument")):
        return "invalid_arguments"
    if any(token in text for token in ("unknown agent tool", "unknown operation", "dataset is not registered", "data interface")):
        return "data_interface"
    if tool == "analysis.python_program" or any(token in text for token in ("python program", "syntaxerror", "nameerror", "typeerror")):
        return "python_code_error"
    if any(token in text for token in ("no data", "empty result", "missing_fields", "data quality", "数据缺失")):
        return "data_quality"
    if any(token in text for token in ("invalid action", "protocol", "jsondecodeerror", "missing action")):
        return "protocol_error"
    return "local_code_defect" if stage in {"executor", "source", "runtime"} else "runtime_error"


def recovery_policy(category: str) -> tuple[bool, str]:
    if category in {"timeout", "connection_error", "rate_limit"}:
        return True, "runtime"
    if category in {"protocol_error", "invalid_arguments", "data_interface", "python_code_error"}:
        return False, "runtime"
    if category == "local_code_defect":
        return False, "source_proposal"
    if category in {"permission_required", "trade_permission", "trade_blocked", "dependency_error"}:
        return False, "confirmation"
    return False, "none"


def failure_fingerprint(
    *,
    category: str,
    tool: str,
    exception_type: str,
    message: str,
    frames: Iterable[FailureFrame] = (),
) -> str:
    frame_list = list(frames)
    terminal = frame_list[-1] if frame_list else None
    frame_key = f"{terminal.path}:{terminal.line}:{terminal.function}" if terminal else ""
    normalized = _VOLATILE.sub("#", str(message or "").lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    raw = "|".join((category, tool, exception_type, frame_key, normalized))
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:24]


def redact_text(value: Any, *, project_root: Path | None = None) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        if "bearer" in pattern.pattern.lower():
            text = pattern.sub("Bearer [REDACTED]", text)
        elif not pattern.groups:
            text = pattern.sub("[REDACTED]", text)
        else:
            text = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    if project_root is not None:
        root = str(project_root)
        text = text.replace(root, "<project>")
    text = re.sub(r"(?<![\w.])/(?:Users|home|private|var|tmp)/[^\s:'\"]+", "<external-path>", text)
    return text[:4000]


def is_readonly_retry_allowed(side_effect: str, failure: AgentFailure) -> bool:
    return str(side_effect or "readonly") == "readonly" and failure.retryable


def _project_frames(trace: traceback.TracebackException, root: Path) -> list[FailureFrame]:
    rows: list[FailureFrame] = []
    for frame in trace.stack or ():
        try:
            path = Path(frame.filename).resolve()
            relative = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        rows.append(FailureFrame(path=relative, line=int(frame.lineno or 0), function=str(frame.name or "")))
    for child in getattr(trace, "exceptions", None) or ():
        rows.extend(_project_frames(child, root))
    cause = trace.__cause__ or trace.__context__
    if cause is not None:
        rows.extend(_project_frames(cause, root))
    return rows[-12:]
