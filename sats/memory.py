from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sats.chat_events import ChatTurnEvent
from sats.llm import extract_json_object
from sats.storage.duckdb import DuckDBStorage


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    memory_id: str
    memory_type: str
    content: str
    tags: tuple[str, ...]
    importance: float
    source_session_id: str
    created_at: str
    updated_at: str
    last_used_at: str


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    memory_type: str
    content: str
    tags: tuple[str, ...] = ()
    importance: float = 0.5


class ChatMemoryStore:
    def __init__(self, db_path: Path | str | None = None, *, storage: DuckDBStorage | None = None) -> None:
        self.storage = storage or DuckDBStorage(db_path or "data/sats.duckdb")

    def create_session(self, session_id: str, *, title: str = "", model_name: str = "") -> None:
        clean_session_id = _clean_required(session_id, "session_id")
        clean_title = str(title or "").strip()
        self.storage.initialize()
        with self.storage.connect() as con:
            exists = con.execute(
                "SELECT 1 FROM chat_sessions WHERE session_id = ?",
                [clean_session_id],
            ).fetchone()
            if exists:
                con.execute(
                    """
                    UPDATE chat_sessions
                    SET title = COALESCE(NULLIF(?, ''), title),
                        model_name = COALESCE(NULLIF(?, ''), model_name),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE session_id = ?
                    """,
                    [clean_title, str(model_name or ""), clean_session_id],
                )
                return
            con.execute(
                """
                INSERT INTO chat_sessions (session_id, title, model_name, summary)
                VALUES (?, ?, ?, '')
                """,
                [clean_session_id, clean_title or None, str(model_name or "")],
            )

    def ensure_session(self, session_id: str, *, model_name: str = "") -> None:
        clean_session_id = _clean_required(session_id, "session_id")
        self.storage.initialize()
        with self.storage.connect() as con:
            exists = con.execute(
                "SELECT 1 FROM chat_sessions WHERE session_id = ?",
                [clean_session_id],
            ).fetchone()
            if exists:
                if model_name:
                    con.execute(
                        """
                        UPDATE chat_sessions
                        SET model_name = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE session_id = ?
                        """,
                        [str(model_name or ""), clean_session_id],
                    )
                else:
                    con.execute(
                        "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                        [clean_session_id],
                    )
                return
            con.execute(
                """
                INSERT INTO chat_sessions (session_id, model_name, summary)
                VALUES (?, ?, '')
                """,
                [clean_session_id, str(model_name or "")],
            )

    def update_session_title(self, session_id: str, title: str) -> None:
        clean_session_id = _clean_required(session_id, "session_id")
        clean_title = str(title or "").strip()
        self.create_session(clean_session_id)
        with self.storage.connect() as con:
            con.execute(
                """
                UPDATE chat_sessions
                SET title = ?, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                [clean_title or None, clean_session_id],
            )

    def get_session_summary(self, session_id: str) -> str:
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                "SELECT summary FROM chat_sessions WHERE session_id = ?",
                [session_id],
            ).fetchone()
        return str(row[0] or "") if row else ""

    def update_session_summary(self, session_id: str, summary: str) -> None:
        self.ensure_session(session_id)
        with self.storage.connect() as con:
            con.execute(
                """
                UPDATE chat_sessions
                SET summary = ?, updated_at = CURRENT_TIMESTAMP
                WHERE session_id = ?
                """,
                [str(summary or ""), session_id],
            )

    def add_message(self, session_id: str, role: str, content: str) -> str:
        clean_session_id = _clean_required(session_id, "session_id")
        clean_role = _clean_required(role, "role")
        clean_content = _clean_required(content, "content")
        message_id = _new_id("msg")
        self.ensure_session(clean_session_id)
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO chat_messages (message_id, session_id, role, content)
                VALUES (?, ?, ?, ?)
                """,
                [message_id, clean_session_id, clean_role, clean_content],
            )
            con.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                [clean_session_id],
            )
        return message_id

    def start_chat_turn(
        self,
        *,
        turn_id: str,
        session_id: str,
        request: str,
        meta: dict[str, Any] | None = None,
    ) -> str:
        clean_turn_id = _clean_required(turn_id, "turn_id")
        clean_session_id = _clean_required(session_id, "session_id")
        clean_request = _clean_required(request, "request")
        self.ensure_session(clean_session_id)
        with self.storage.connect() as con:
            exists = con.execute("SELECT 1 FROM chat_turns WHERE turn_id = ?", [clean_turn_id]).fetchone()
            if exists:
                con.execute(
                    """
                    UPDATE chat_turns
                    SET session_id = ?, request = ?, status = 'running',
                        meta_json = ?, started_at = CURRENT_TIMESTAMP,
                        completed_at = NULL, error_json = NULL
                    WHERE turn_id = ?
                    """,
                    [clean_session_id, clean_request, _json(meta or {}), clean_turn_id],
                )
            else:
                con.execute(
                    """
                    INSERT INTO chat_turns (turn_id, session_id, request, status, meta_json)
                    VALUES (?, ?, ?, 'running', ?)
                    """,
                    [clean_turn_id, clean_session_id, clean_request, _json(meta or {})],
                )
            con.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                [clean_session_id],
            )
        return clean_turn_id

    def add_chat_turn_event(self, event: ChatTurnEvent) -> str:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO chat_turn_events
                    (event_id, turn_id, seq, event_type, item_type, item_name, status,
                     content, payload_json, started_at, completed_at, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    event.event_id,
                    event.turn_id,
                    int(event.seq),
                    event.event_type,
                    event.item_type,
                    event.item_name,
                    event.status,
                    event.content,
                    _json(dict(event.payload)),
                    event.started_at,
                    event.completed_at,
                    event.duration_seconds,
                ],
            )
        return event.event_id

    def add_chat_turn_item(
        self,
        *,
        turn_id: str,
        item_type: str,
        item_name: str = "",
        status: str = "running",
        input_payload: dict[str, Any] | None = None,
        output_payload: dict[str, Any] | None = None,
        artifact_paths: list[str] | tuple[str, ...] = (),
        item_id: str | None = None,
        seq: int | None = None,
        duration_seconds: float | None = None,
    ) -> str:
        clean_item_id = str(item_id or "").strip() or _new_id("item")
        clean_turn_id = _clean_required(turn_id, "turn_id")
        clean_type = _clean_required(item_type, "item_type")
        self.storage.initialize()
        with self.storage.connect() as con:
            item_seq = int(seq) if seq is not None else int(
                con.execute(
                    "SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_turn_items WHERE turn_id = ?",
                    [clean_turn_id],
                ).fetchone()[0]
                or 1
            )
            con.execute(
                """
                INSERT INTO chat_turn_items
                    (item_id, turn_id, seq, item_type, item_name, status, input_json,
                     output_json, artifact_paths_json, completed_at, duration_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? = 'running' THEN NULL ELSE CURRENT_TIMESTAMP END, ?)
                """,
                [
                    clean_item_id,
                    clean_turn_id,
                    item_seq,
                    clean_type,
                    str(item_name or ""),
                    str(status or "running"),
                    _json(input_payload or {}),
                    _json(output_payload or {}),
                    _json(list(artifact_paths)),
                    str(status or "running"),
                    duration_seconds,
                ],
            )
        return clean_item_id

    def complete_chat_turn_item(
        self,
        item_id: str,
        *,
        status: str = "done",
        output_payload: dict[str, Any] | None = None,
        artifact_paths: list[str] | tuple[str, ...] = (),
        duration_seconds: float | None = None,
    ) -> None:
        clean_item_id = _clean_required(item_id, "item_id")
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                UPDATE chat_turn_items
                SET status = ?,
                    output_json = ?,
                    artifact_paths_json = ?,
                    completed_at = CURRENT_TIMESTAMP,
                    duration_seconds = ?
                WHERE item_id = ?
                """,
                [
                    str(status or "done"),
                    _json(output_payload or {}),
                    _json(list(artifact_paths)),
                    duration_seconds,
                    clean_item_id,
                ],
            )

    def add_chat_artifact(
        self,
        *,
        session_id: str,
        turn_id: str,
        kind: str,
        path: str,
        title: str = "",
        mime_type: str = "",
        summary: str = "",
        meta: dict[str, Any] | None = None,
        artifact_id: str | None = None,
    ) -> str:
        clean_artifact_id = str(artifact_id or "").strip() or _new_id("art")
        clean_session_id = _clean_required(session_id, "session_id")
        clean_turn_id = _clean_required(turn_id, "turn_id")
        clean_kind = _clean_required(kind, "kind")
        clean_path = _clean_required(path, "path")
        self.ensure_session(clean_session_id)
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO chat_artifacts
                    (artifact_id, session_id, turn_id, kind, title, path, mime_type, summary, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    clean_artifact_id,
                    clean_session_id,
                    clean_turn_id,
                    clean_kind,
                    str(title or ""),
                    clean_path,
                    str(mime_type or ""),
                    str(summary or ""),
                    _json(meta or {}),
                ],
            )
            con.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                [clean_session_id],
            )
        return clean_artifact_id

    def create_pending_action(
        self,
        *,
        session_id: str,
        action_type: str,
        payload: dict[str, Any],
        turn_id: str = "",
        title: str = "",
        expires_at: str | None = None,
        action_id: str | None = None,
    ) -> str:
        clean_action_id = str(action_id or "").strip() or _new_id("act")
        clean_session_id = _clean_required(session_id, "session_id")
        clean_type = _clean_required(action_type, "action_type")
        self.ensure_session(clean_session_id)
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO chat_pending_actions
                    (action_id, session_id, turn_id, action_type, title, status,
                     payload_json, result_json, expires_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, '{}', ?)
                """,
                [
                    clean_action_id,
                    clean_session_id,
                    str(turn_id or ""),
                    clean_type,
                    str(title or ""),
                    _json(payload or {}),
                    expires_at,
                ],
            )
            con.execute(
                "UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
                [clean_session_id],
            )
        return clean_action_id

    def get_pending_action(self, action_id: str) -> dict[str, Any] | None:
        clean_action_id = _clean_required(action_id, "action_id")
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                """
                SELECT action_id, session_id, turn_id, action_type, title, status,
                       payload_json, result_json, expires_at, created_at, updated_at
                FROM chat_pending_actions
                WHERE action_id = ?
                """,
                [clean_action_id],
            ).fetchone()
        if not row:
            return None
        return {
            "action_id": str(row[0]),
            "session_id": str(row[1] or ""),
            "turn_id": str(row[2] or ""),
            "action_type": str(row[3] or ""),
            "title": str(row[4] or ""),
            "status": str(row[5] or ""),
            "payload": _loads_json_object(row[6]),
            "result": _loads_json_object(row[7]),
            "expires_at": str(row[8] or ""),
            "created_at": str(row[9] or ""),
            "updated_at": str(row[10] or ""),
        }

    def list_pending_actions(
        self,
        *,
        session_id: str = "",
        action_type: str = "",
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.storage.initialize()
        clauses: list[str] = []
        params: list[Any] = []
        if str(session_id or "").strip():
            clauses.append("session_id = ?")
            params.append(str(session_id).strip())
        if str(action_type or "").strip():
            clauses.append("action_type = ?")
            params.append(str(action_type).strip())
        if status is not None:
            clauses.append("status = ?")
            params.append(str(status or "").strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, int(limit or 20)))
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT action_id, session_id, turn_id, action_type, title, status,
                       payload_json, result_json, expires_at, created_at, updated_at
                FROM chat_pending_actions
                {where}
                ORDER BY updated_at DESC, created_at DESC, action_id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "action_id": str(row[0] or ""),
                "session_id": str(row[1] or ""),
                "turn_id": str(row[2] or ""),
                "action_type": str(row[3] or ""),
                "title": str(row[4] or ""),
                "status": str(row[5] or ""),
                "payload": _loads_json_object(row[6]),
                "result": _loads_json_object(row[7]),
                "expires_at": str(row[8] or ""),
                "created_at": str(row[9] or ""),
                "updated_at": str(row[10] or ""),
            }
            for row in rows
        ]

    def update_pending_action(
        self,
        action_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        clean_action_id = _clean_required(action_id, "action_id")
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                UPDATE chat_pending_actions
                SET status = ?, result_json = ?, updated_at = CURRENT_TIMESTAMP
                WHERE action_id = ?
                """,
                [str(status or ""), _json(result or {}), clean_action_id],
            )

    def update_pending_action_payload(
        self,
        action_id: str,
        *,
        payload: dict[str, Any] | None = None,
        title: str | None = None,
        status: str | None = None,
    ) -> None:
        clean_action_id = _clean_required(action_id, "action_id")
        self.storage.initialize()
        assignments = ["payload_json = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = [_json(payload or {})]
        if title is not None:
            assignments.append("title = ?")
            params.append(str(title or ""))
        if status is not None:
            assignments.append("status = ?")
            params.append(str(status or ""))
        params.append(clean_action_id)
        with self.storage.connect() as con:
            con.execute(
                f"""
                UPDATE chat_pending_actions
                SET {", ".join(assignments)}
                WHERE action_id = ?
                """,
                params,
            )

    def latest_chat_turn_id(self, session_id: str = "") -> str:
        self.storage.initialize()
        clause = ""
        params: list[Any] = []
        if str(session_id or "").strip():
            clause = "WHERE session_id = ?"
            params.append(str(session_id).strip())
        with self.storage.connect() as con:
            row = con.execute(
                f"""
                SELECT turn_id
                FROM chat_turns
                {clause}
                ORDER BY started_at DESC, turn_id DESC
                LIMIT 1
                """,
                params,
            ).fetchone()
        return str(row[0] or "") if row else ""

    def get_chat_turn_trace(self, turn_id: str) -> dict[str, Any] | None:
        clean_turn_id = _clean_required(turn_id, "turn_id")
        self.storage.initialize()
        with self.storage.connect() as con:
            turn = con.execute(
                """
                SELECT turn_id, session_id, request, intent, status, started_at,
                       completed_at, duration_seconds, meta_json, error_json
                FROM chat_turns
                WHERE turn_id = ?
                """,
                [clean_turn_id],
            ).fetchone()
            if not turn:
                return None
            events = con.execute(
                """
                SELECT seq, event_type, item_type, item_name, status, content,
                       payload_json, duration_seconds
                FROM chat_turn_events
                WHERE turn_id = ?
                ORDER BY seq ASC, event_id ASC
                """,
                [clean_turn_id],
            ).fetchall()
            items = con.execute(
                """
                SELECT seq, item_type, item_name, status, input_json, output_json,
                       artifact_paths_json, duration_seconds
                FROM chat_turn_items
                WHERE turn_id = ?
                ORDER BY seq ASC, item_id ASC
                """,
                [clean_turn_id],
            ).fetchall()
            artifacts = con.execute(
                """
                SELECT artifact_id, kind, title, path, mime_type, summary, meta_json, created_at
                FROM chat_artifacts
                WHERE turn_id = ?
                ORDER BY created_at ASC, artifact_id ASC
                """,
                [clean_turn_id],
            ).fetchall()
        return {
            "turn": {
                "turn_id": str(turn[0]),
                "session_id": str(turn[1] or ""),
                "request": str(turn[2] or ""),
                "intent": str(turn[3] or ""),
                "status": str(turn[4] or ""),
                "started_at": str(turn[5] or ""),
                "completed_at": str(turn[6] or ""),
                "duration_seconds": float(turn[7] or 0.0),
                "meta": _loads_json_object(turn[8]),
                "error": _loads_json_object(turn[9]),
            },
            "events": [
                {
                    "seq": int(row[0] or 0),
                    "event_type": str(row[1] or ""),
                    "item_type": str(row[2] or ""),
                    "item_name": str(row[3] or ""),
                    "status": str(row[4] or ""),
                    "content": str(row[5] or ""),
                    "payload": _loads_json_object(row[6]),
                    "duration_seconds": float(row[7] or 0.0),
                }
                for row in events
            ],
            "items": [
                {
                    "seq": int(row[0] or 0),
                    "item_type": str(row[1] or ""),
                    "item_name": str(row[2] or ""),
                    "status": str(row[3] or ""),
                    "input": _loads_json_object(row[4]),
                    "output": _loads_json_object(row[5]),
                    "artifact_paths": _loads_json_list(row[6]),
                    "duration_seconds": float(row[7] or 0.0),
                }
                for row in items
            ],
            "artifacts": [
                {
                    "artifact_id": str(row[0] or ""),
                    "kind": str(row[1] or ""),
                    "title": str(row[2] or ""),
                    "path": str(row[3] or ""),
                    "mime_type": str(row[4] or ""),
                    "summary": str(row[5] or ""),
                    "meta": _loads_json_object(row[6]),
                    "created_at": str(row[7] or ""),
                }
                for row in artifacts
            ],
        }

    def complete_chat_turn(
        self,
        turn_id: str,
        *,
        status: str = "done",
        intent: str = "",
        symbols: list[str] | tuple[str, ...] = (),
        trade_date: str | None = None,
        data_names: list[str] | tuple[str, ...] = (),
        skill_names: list[str] | tuple[str, ...] = (),
        model_name: str = "",
        tool_call_count: int = 0,
        user_message_id: str = "",
        assistant_message_id: str = "",
        duration_seconds: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        clean_turn_id = _clean_required(turn_id, "turn_id")
        with self.storage.connect() as con:
            con.execute(
                """
                UPDATE chat_turns
                SET user_message_id = ?,
                    assistant_message_id = ?,
                    intent = ?,
                    status = ?,
                    symbols_json = ?,
                    trade_date = ?,
                    data_names_json = ?,
                    skill_names_json = ?,
                    model_name = ?,
                    tool_call_count = ?,
                    completed_at = CURRENT_TIMESTAMP,
                    duration_seconds = ?,
                    error_json = NULL,
                    meta_json = ?
                WHERE turn_id = ?
                """,
                [
                    str(user_message_id or "") or None,
                    str(assistant_message_id or "") or None,
                    str(intent or ""),
                    str(status or "done"),
                    _json(list(symbols)),
                    str(trade_date or "") or None,
                    _json(list(data_names)),
                    _json(list(skill_names)),
                    str(model_name or ""),
                    int(tool_call_count or 0),
                    duration_seconds,
                    _json(meta or {}),
                    clean_turn_id,
                ],
            )

    def fail_chat_turn(
        self,
        turn_id: str,
        *,
        status: str = "error",
        error: dict[str, Any] | None = None,
        duration_seconds: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        clean_turn_id = _clean_required(turn_id, "turn_id")
        with self.storage.connect() as con:
            con.execute(
                """
                UPDATE chat_turns
                SET status = ?,
                    completed_at = CURRENT_TIMESTAMP,
                    duration_seconds = ?,
                    error_json = ?,
                    meta_json = ?
                WHERE turn_id = ?
                """,
                [str(status or "error"), duration_seconds, _json(error or {}), _json(meta or {}), clean_turn_id],
            )

    def list_recent_messages(self, session_id: str, *, limit: int = 12) -> list[dict[str, str]]:
        if limit <= 0:
            return []
        self.storage.initialize()
        with self.storage.connect() as con:
            rows = con.execute(
                """
                SELECT role, content
                FROM chat_messages
                WHERE session_id = ?
                ORDER BY created_at DESC, message_id DESC
                LIMIT ?
                """,
                [session_id, int(limit)],
            ).fetchall()
        return [{"role": str(role), "content": str(content)} for role, content in reversed(rows)]

    def count_session_messages(self, session_id: str) -> int:
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?",
                [session_id],
            ).fetchone()
        return int(row[0] or 0)

    def add_memory(
        self,
        *,
        content: str,
        memory_type: str = "fact",
        tags: tuple[str, ...] | list[str] = (),
        importance: float = 0.5,
        source_session_id: str = "",
    ) -> str:
        clean_content = _clean_required(content, "content")
        clean_type = str(memory_type or "fact").strip() or "fact"
        clean_tags = _clean_tags(tags)
        clean_importance = _clamp_importance(importance)
        self.storage.initialize()
        with self.storage.connect() as con:
            existing = con.execute(
                """
                SELECT memory_id
                FROM chat_memories
                WHERE archived = FALSE AND lower(content) = lower(?)
                LIMIT 1
                """,
                [clean_content],
            ).fetchone()
            if existing:
                memory_id = str(existing[0])
                con.execute(
                    """
                    UPDATE chat_memories
                    SET memory_type = ?,
                        tags_json = ?,
                        importance = ?,
                        source_session_id = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE memory_id = ?
                    """,
                    [
                        clean_type,
                        json.dumps(clean_tags, ensure_ascii=False),
                        clean_importance,
                        str(source_session_id or ""),
                        memory_id,
                    ],
                )
                return memory_id

            memory_id = _new_id("mem")
            con.execute(
                """
                INSERT INTO chat_memories
                    (memory_id, memory_type, content, tags_json, importance, source_session_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    memory_id,
                    clean_type,
                    clean_content,
                    json.dumps(clean_tags, ensure_ascii=False),
                    clean_importance,
                    str(source_session_id or ""),
                ],
            )
        return memory_id

    def list_memories(self, *, include_archived: bool = False) -> list[MemoryRecord]:
        self.storage.initialize()
        where = "" if include_archived else "WHERE archived = FALSE"
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT memory_id, memory_type, content, tags_json, importance,
                       source_session_id, created_at, updated_at, last_used_at
                FROM chat_memories
                {where}
                ORDER BY updated_at DESC, created_at DESC, memory_id ASC
                """
            ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def search_memories(self, query: str, *, limit: int = 5) -> list[MemoryRecord]:
        terms = _query_terms(query)
        if not terms:
            return self.list_memories()[:limit]
        scored: list[tuple[float, MemoryRecord]] = []
        for memory in self.list_memories():
            score = _memory_score(memory, terms, str(query or ""))
            if score > 0:
                scored.append((score, memory))
        scored.sort(key=lambda item: (-item[0], -item[1].importance, item[1].memory_id))
        return [memory for _, memory in scored[:limit]]

    def touch_memories(self, memory_ids: list[str] | tuple[str, ...]) -> None:
        clean_ids = [str(item).strip() for item in memory_ids if str(item).strip()]
        if not clean_ids:
            return
        self.storage.initialize()
        placeholders = ", ".join("?" for _ in clean_ids)
        with self.storage.connect() as con:
            con.execute(
                f"""
                UPDATE chat_memories
                SET last_used_at = CURRENT_TIMESTAMP
                WHERE memory_id IN ({placeholders})
                """,
                clean_ids,
            )

    def archive_memory(self, memory_id: str) -> bool:
        clean_id = _clean_required(memory_id, "memory_id")
        self.storage.initialize()
        with self.storage.connect() as con:
            exists = con.execute(
                "SELECT 1 FROM chat_memories WHERE memory_id = ? AND archived = FALSE",
                [clean_id],
            ).fetchone()
            if not exists:
                return False
            con.execute(
                """
                UPDATE chat_memories
                SET archived = TRUE, updated_at = CURRENT_TIMESTAMP
                WHERE memory_id = ?
                """,
                [clean_id],
            )
        return True

    def clear_all_memory(self) -> int:
        self.storage.initialize()
        with self.storage.connect() as con:
            memory_count = int(
                con.execute("SELECT COUNT(*) FROM chat_memories WHERE archived = FALSE").fetchone()[0] or 0
            )
            message_count = int(con.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] or 0)
            con.execute(
                """
                UPDATE chat_memories
                SET archived = TRUE, updated_at = CURRENT_TIMESTAMP
                WHERE archived = FALSE
                """
            )
            con.execute("DELETE FROM chat_messages")
            con.execute("UPDATE chat_sessions SET summary = '', updated_at = CURRENT_TIMESTAMP")
        return memory_count + message_count


class MemoryRetriever:
    def __init__(self, store: ChatMemoryStore, *, limit: int = 5) -> None:
        self.store = store
        self.limit = limit

    def retrieve(self, message: str) -> list[MemoryRecord]:
        return self.store.search_memories(message, limit=self.limit)


class MemoryExtractor:
    def extract(self, user_message: str, assistant_message: str, *, llm: Any) -> list[MemoryCandidate]:
        prompt = [
            {
                "role": "system",
                "content": (
                    "你负责从 SATS 聊天中抽取长期记忆。只保存稳定事实、用户偏好、项目决策、约束。"
                    "不要保存短期寒暄、临时数字、无意义重复。"
                    "输出 JSON：{\"memories\":[{\"type\":\"preference|fact|decision|constraint\","
                    "\"content\":\"...\",\"tags\":[\"...\"],\"importance\":0.1到1.0}]}"
                ),
            },
            {
                "role": "user",
                "content": f"用户消息：{user_message}\n\n助手回复：{assistant_message}",
            },
        ]
        if hasattr(llm, "chat_validated"):
            parsed = llm.chat_validated(prompt, _parse_required_json_response)
        else:
            response = llm.chat(prompt)
            parsed = extract_json_object(str(response.content or ""))
        if not parsed:
            return []
        memories = parsed.get("memories")
        if not isinstance(memories, list):
            return []
        candidates = []
        for item in memories:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            candidates.append(
                MemoryCandidate(
                    memory_type=str(item.get("type") or item.get("memory_type") or "fact").strip() or "fact",
                    content=content,
                    tags=tuple(str(tag).strip() for tag in tags if str(tag).strip()),
                    importance=_clamp_importance(item.get("importance", 0.5)),
                )
            )
        return candidates

    def summarize(self, existing_summary: str, messages: list[dict[str, str]], *, llm: Any) -> str:
        transcript = "\n".join(f"{item.get('role')}: {item.get('content')}" for item in messages)
        prompt = [
            {
                "role": "system",
                "content": (
                    "请为 SATS 聊天会话生成简洁滚动摘要，保留用户目标、偏好、关键决策、未完成事项。"
                    "只输出摘要正文，不要输出 JSON。"
                ),
            },
            {
                "role": "user",
                "content": f"已有摘要：{existing_summary or '无'}\n\n最近对话：\n{transcript}",
            },
        ]
        response = llm.chat(prompt)
        return str(response.content or "").strip()


def format_memory_list(memories: list[MemoryRecord]) -> str:
    if not memories:
        return "无记忆"
    lines = []
    for index, memory in enumerate(memories, start=1):
        tag_text = f" tags={','.join(memory.tags)}" if memory.tags else ""
        lines.append(
            f"{index}. {memory.memory_id} [{memory.memory_type}] "
            f"{memory.importance:.2f}{tag_text} {memory.content}"
        )
    return "\n".join(lines)


def _parse_required_json_response(response: Any) -> dict[str, Any]:
    parsed = extract_json_object(str(getattr(response, "content", "") or ""))
    if not isinstance(parsed, dict):
        raise ValueError("LLM response did not contain a JSON object")
    return parsed


def _memory_from_row(row: Any) -> MemoryRecord:
    tags = _loads_tags(row[3])
    return MemoryRecord(
        memory_id=str(row[0]),
        memory_type=str(row[1] or "fact"),
        content=str(row[2] or ""),
        tags=tuple(tags),
        importance=float(row[4] or 0.5),
        source_session_id=str(row[5] or ""),
        created_at=str(row[6] or ""),
        updated_at=str(row[7] or ""),
        last_used_at=str(row[8] or ""),
    )


def _loads_tags(raw: Any) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _loads_json_object(raw: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _loads_json_list(raw: Any) -> list[Any]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _clean_required(value: str, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _clean_tags(tags: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen = set()
    clean = []
    for tag in tags:
        text = str(tag or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            clean.append(text)
    return tuple(clean)


def _clamp_importance(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.5
    return max(0.0, min(1.0, number))


def _query_terms(query: str) -> list[str]:
    text = str(query or "").strip().lower()
    if not text:
        return []
    terms = [text]
    terms.extend(re.findall(r"[a-z0-9_.-]+", text))
    terms.extend(part for part in re.split(r"[\s,，。；;:：]+", text) if part)
    clean = []
    seen = set()
    for term in terms:
        if term not in seen:
            seen.add(term)
            clean.append(term)
    return clean


def _memory_score(memory: MemoryRecord, terms: list[str], original_query: str) -> float:
    content = memory.content.lower()
    tags = [tag.lower() for tag in memory.tags]
    query = original_query.strip().lower()
    score = memory.importance
    if query and query in content:
        score += 4
    for term in terms:
        if term in content:
            score += 3
        if any(term in tag or tag in term for tag in tags):
            score += 5
    return score if score > memory.importance else 0
