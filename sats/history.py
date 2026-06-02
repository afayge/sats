from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sats.storage.duckdb import DuckDBStorage

HISTORY_KINDS = {"chat", "command"}
HISTORY_STATUSES = {"done", "error", "interrupted", "deleted"}
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


@dataclass(frozen=True, slots=True)
class InteractionHistoryRecord:
    history_id: str
    session_id: str
    kind: str
    request: str
    source: str
    output: str
    status: str
    duration_seconds: float | None
    report_path: str
    meta_json: str
    deleted_at: str
    created_at: str


class InteractionHistoryStore:
    def __init__(self, db_path: Path | str | None = None, *, storage: DuckDBStorage | None = None) -> None:
        self.storage = storage or DuckDBStorage(db_path or "data/sats.duckdb")

    def add_record(
        self,
        *,
        kind: str,
        request: str,
        source: str,
        output: str = "",
        status: str = "done",
        duration_seconds: float | None = None,
        report_path: str | None = None,
        session_id: str = "",
        meta: dict[str, Any] | None = None,
    ) -> str:
        clean_kind = _clean_choice(kind, HISTORY_KINDS, "kind")
        clean_status = _clean_choice(status, HISTORY_STATUSES, "status")
        history_id = _new_id("hist")
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO interaction_history
                    (history_id, session_id, kind, request, source, output, status,
                     duration_seconds, report_path, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    history_id,
                    str(session_id or ""),
                    clean_kind,
                    str(request or ""),
                    str(source or ""),
                    str(output or ""),
                    clean_status,
                    _clean_duration(duration_seconds),
                    str(report_path or ""),
                    json.dumps(meta or {}, ensure_ascii=False),
                ],
            )
        return history_id

    def list_records(self, *, kind: str | None = None, limit: int = DEFAULT_LIMIT) -> list[InteractionHistoryRecord]:
        clean_kind = _optional_kind(kind)
        clauses = ["status <> 'deleted'", "deleted_at IS NULL"]
        params: list[object] = []
        if clean_kind:
            clauses.append("kind = ?")
            params.append(clean_kind)
        params.append(_clamp_limit(limit))
        return self._fetch_records(
            f"""
            SELECT history_id, session_id, kind, request, source, output, status,
                   duration_seconds, report_path, meta_json, deleted_at, created_at
            FROM interaction_history
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, history_id DESC
            LIMIT ?
            """,
            params,
        )

    def search_records(
        self,
        query: str,
        *,
        kind: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> list[InteractionHistoryRecord]:
        clean_kind = _optional_kind(kind)
        terms = _query_terms(query)
        if not terms:
            return self.list_records(kind=clean_kind, limit=limit)
        clauses = ["status <> 'deleted'", "deleted_at IS NULL"]
        params: list[object] = []
        search_clauses = []
        for term in terms:
            pattern = f"%{term}%"
            search_clauses.append("(lower(request) LIKE lower(?) OR lower(output) LIKE lower(?))")
            params.extend([pattern, pattern])
        clauses.append(f"({' OR '.join(search_clauses)})")
        if clean_kind:
            clauses.append("kind = ?")
            params.append(clean_kind)
        params.append(_clamp_limit(limit))
        return self._fetch_records(
            f"""
            SELECT history_id, session_id, kind, request, source, output, status,
                   duration_seconds, report_path, meta_json, deleted_at, created_at
            FROM interaction_history
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC, history_id DESC
            LIMIT ?
            """,
            params,
        )

    def get_record(self, history_id: str) -> InteractionHistoryRecord | None:
        clean_id = _clean_required(history_id, "history_id")
        records = self._fetch_records(
            """
            SELECT history_id, session_id, kind, request, source, output, status,
                   duration_seconds, report_path, meta_json, deleted_at, created_at
            FROM interaction_history
            WHERE history_id = ? AND status <> 'deleted' AND deleted_at IS NULL
            LIMIT 1
            """,
            [clean_id],
        )
        return records[0] if records else None

    def delete_record(self, history_id: str) -> bool:
        clean_id = _clean_required(history_id, "history_id")
        self.storage.initialize()
        with self.storage.connect() as con:
            exists = con.execute(
                """
                SELECT 1
                FROM interaction_history
                WHERE history_id = ? AND status <> 'deleted' AND deleted_at IS NULL
                """,
                [clean_id],
            ).fetchone()
            if not exists:
                return False
            con.execute(
                """
                UPDATE interaction_history
                SET status = 'deleted', deleted_at = CURRENT_TIMESTAMP
                WHERE history_id = ?
                """,
                [clean_id],
            )
        return True

    def _fetch_records(self, query: str, params: list[object]) -> list[InteractionHistoryRecord]:
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(query, params).fetchall()
        return [_history_from_row(row) for row in rows]


def format_history_list(records: list[InteractionHistoryRecord]) -> str:
    if not records:
        return "无历史记录"
    rows = [
        ("序号", "历史ID", "时间", "类型", "状态", "输入摘要", "输出摘要"),
    ]
    for index, record in enumerate(records, start=1):
        rows.append(
            (
                str(index),
                record.history_id,
                _short_time(record.created_at),
                record.kind,
                record.status,
                _snippet(record.request, 28),
                _snippet(record.output, 36),
            )
        )
    widths = [max(_display_width(row[column]) for row in rows) for column in range(len(rows[0]))]
    lines = []
    for row in rows:
        lines.append("  ".join(_pad_display(value, widths[index]) for index, value in enumerate(row)).rstrip())
    return "\n".join(lines)


def format_history_detail(record: InteractionHistoryRecord) -> str:
    lines = [
        f"历史 ID: {record.history_id}",
        f"时间: {_short_time(record.created_at)}",
        f"类型: {record.kind}",
        f"状态: {record.status}",
        f"来源: {record.source}",
    ]
    if record.duration_seconds is not None:
        lines.append(f"耗时: {record.duration_seconds:.2f}s")
    if record.report_path:
        lines.append(f"报告: {record.report_path}")
    lines.extend(["", "请求:", record.request or "(空)", "", "结果:", record.output or "(无输出)"])
    return "\n".join(lines)


def _history_from_row(row: Any) -> InteractionHistoryRecord:
    duration = None if row[7] is None else float(row[7])
    return InteractionHistoryRecord(
        history_id=str(row[0] or ""),
        session_id=str(row[1] or ""),
        kind=str(row[2] or ""),
        request=str(row[3] or ""),
        source=str(row[4] or ""),
        output=str(row[5] or ""),
        status=str(row[6] or ""),
        duration_seconds=duration,
        report_path=str(row[8] or ""),
        meta_json=str(row[9] or "{}"),
        deleted_at=str(row[10] or ""),
        created_at=str(row[11] or ""),
    )


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _clean_required(value: str, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _clean_choice(value: str, choices: set[str], name: str) -> str:
    text = _clean_required(value, name)
    if text not in choices:
        raise ValueError(f"{name} must be one of: {', '.join(sorted(choices))}")
    return text


def _optional_kind(kind: str | None) -> str | None:
    if kind is None:
        return None
    return _clean_choice(kind, HISTORY_KINDS, "kind")


def _clean_duration(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _clamp_limit(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = DEFAULT_LIMIT
    return max(1, min(MAX_LIMIT, value))


def _query_terms(query: str) -> list[str]:
    text = str(query or "").strip()
    if not text:
        return []
    terms = [text]
    terms.extend(part for part in re.split(r"[\s,，。；;:：]+", text) if part)
    clean = []
    seen = set()
    for term in terms:
        key = term.lower()
        if key not in seen:
            seen.add(key)
            clean.append(term)
    return clean


def _snippet(text: str, width: int) -> str:
    value = " ".join(str(text or "").split())
    if not value:
        return "--"
    if _display_width(value) <= width:
        return value
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


def _short_time(value: str) -> str:
    text = str(value or "").replace("T", " ").strip()
    return text[:19] if len(text) > 19 else text


def _display_width(text: str) -> int:
    return sum(2 if ord(char) > 127 else 1 for char in str(text or ""))


def _pad_display(text: str, width: int) -> str:
    value = str(text or "")
    return value + (" " * max(0, width - _display_width(value)))
