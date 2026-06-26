from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sats.config import Settings
from sats.memory import ChatMemoryStore


@dataclass(frozen=True, slots=True)
class ThreadSummary:
    session_id: str
    title: str = ""
    summary: str = ""
    archived: bool = False
    pinned: bool = False
    message_count: int = 0
    turn_count: int = 0
    created_at: str = ""
    updated_at: str = ""
    meta: dict[str, Any] | None = None


def create_thread(settings: Settings, *, title: str = "") -> ThreadSummary:
    store = ChatMemoryStore(settings.db_path)
    session_id = _new_thread_id()
    store.create_session(session_id, title=title)
    return get_thread(settings, session_id)


def list_threads(settings: Settings, *, include_archived: bool = False, limit: int = 20) -> list[ThreadSummary]:
    store = ChatMemoryStore(settings.db_path)
    store.storage.initialize()
    where = "" if include_archived else "WHERE COALESCE(archived, FALSE) = FALSE"
    with store.storage.connect() as con:
        rows = con.execute(
            f"""
            SELECT s.session_id, COALESCE(s.title, ''), COALESCE(s.summary, ''),
                   COALESCE(s.archived, FALSE), COALESCE(s.pinned, FALSE),
                   COALESCE(msg.message_count, 0), COALESCE(turns.turn_count, 0),
                   s.created_at, s.updated_at, COALESCE(s.meta_json, '{{}}')
            FROM chat_sessions s
            LEFT JOIN (
                SELECT session_id, COUNT(*) AS message_count
                FROM chat_messages
                GROUP BY session_id
            ) msg ON msg.session_id = s.session_id
            LEFT JOIN (
                SELECT session_id, COUNT(*) AS turn_count
                FROM chat_turns
                GROUP BY session_id
            ) turns ON turns.session_id = s.session_id
            {where}
            ORDER BY COALESCE(s.pinned, FALSE) DESC, s.updated_at DESC, s.created_at DESC
            LIMIT ?
            """,
            [max(1, int(limit or 20))],
        ).fetchall()
    return [_thread_from_row(row) for row in rows]


def get_thread(settings: Settings, session_id: str) -> ThreadSummary:
    store = ChatMemoryStore(settings.db_path)
    store.storage.initialize()
    with store.storage.connect() as con:
        row = con.execute(
            """
            SELECT s.session_id, COALESCE(s.title, ''), COALESCE(s.summary, ''),
                   COALESCE(s.archived, FALSE), COALESCE(s.pinned, FALSE),
                   COALESCE(msg.message_count, 0), COALESCE(turns.turn_count, 0),
                   s.created_at, s.updated_at, COALESCE(s.meta_json, '{}')
            FROM chat_sessions s
            LEFT JOIN (
                SELECT session_id, COUNT(*) AS message_count
                FROM chat_messages
                GROUP BY session_id
            ) msg ON msg.session_id = s.session_id
            LEFT JOIN (
                SELECT session_id, COUNT(*) AS turn_count
                FROM chat_turns
                GROUP BY session_id
            ) turns ON turns.session_id = s.session_id
            WHERE s.session_id = ?
            """,
            [session_id],
        ).fetchone()
    if not row:
        raise ValueError(f"thread not found: {session_id}")
    return _thread_from_row(row)


def read_thread(settings: Settings, session_id: str, *, limit: int = 20) -> dict[str, Any]:
    thread = get_thread(settings, session_id)
    store = ChatMemoryStore(settings.db_path)
    messages = store.list_recent_messages(session_id, limit=limit)
    store.storage.initialize()
    with store.storage.connect() as con:
        turns = con.execute(
            """
            SELECT turn_id, request, intent, status, started_at, completed_at,
                   tool_call_count, data_names_json
            FROM chat_turns
            WHERE session_id = ?
            ORDER BY started_at DESC, turn_id DESC
            LIMIT ?
            """,
            [session_id, max(1, int(limit or 20))],
        ).fetchall()
    return {
        "thread": thread,
        "messages": messages,
        "turns": [
            {
                "turn_id": str(row[0] or ""),
                "request": str(row[1] or ""),
                "intent": str(row[2] or ""),
                "status": str(row[3] or ""),
                "started_at": str(row[4] or ""),
                "completed_at": str(row[5] or ""),
                "tool_call_count": int(row[6] or 0),
                "data_names": _loads_json_list(row[7]),
            }
            for row in reversed(turns)
        ],
    }


def fork_thread(settings: Settings, session_id: str, *, title: str = "") -> ThreadSummary:
    parent = get_thread(settings, session_id)
    store = ChatMemoryStore(settings.db_path)
    child_id = _new_thread_id()
    store.create_session(child_id, title=title or f"{parent.title or parent.session_id} fork")
    meta = {
        "parent_session_id": parent.session_id,
        "forked_from_turn_id": _latest_turn_id(store, parent.session_id),
    }
    with store.storage.connect() as con:
        con.execute(
            "UPDATE chat_sessions SET summary = ?, meta_json = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
            [parent.summary, json.dumps(meta, ensure_ascii=False), child_id],
        )
    return get_thread(settings, child_id)


def archive_thread(settings: Settings, session_id: str, *, archived: bool = True) -> ThreadSummary:
    return _set_thread_bool(settings, session_id, "archived", archived)


def pin_thread(settings: Settings, session_id: str, *, pinned: bool = True) -> ThreadSummary:
    return _set_thread_bool(settings, session_id, "pinned", pinned)


def rename_thread(settings: Settings, session_id: str, title: str) -> ThreadSummary:
    store = ChatMemoryStore(settings.db_path)
    store.update_session_title(session_id, title)
    return get_thread(settings, session_id)


def format_thread_list(threads: list[ThreadSummary]) -> str:
    if not threads:
        return "没有对话线程。"
    lines = ["SATS threads"]
    for item in threads:
        flags = []
        if item.pinned:
            flags.append("pinned")
        if item.archived:
            flags.append("archived")
        flag_text = f" [{' '.join(flags)}]" if flags else ""
        title = f" {item.title}" if item.title else ""
        lines.append(f"- {item.session_id}{flag_text}{title} messages={item.message_count} turns={item.turn_count}")
    return "\n".join(lines)


def format_thread_detail(payload: dict[str, Any]) -> str:
    thread = payload["thread"]
    lines = [f"SATS thread: {thread.session_id}"]
    if thread.title:
        lines.append(f"标题: {thread.title}")
    if thread.summary:
        lines.append(f"摘要: {thread.summary}")
    if thread.meta:
        parent = thread.meta.get("parent_session_id")
        if parent:
            lines.append(f"Forked from: {parent}")
    lines.append("")
    lines.append("消息:")
    for item in payload.get("messages", []):
        lines.append(f"- {item.get('role')}: {item.get('content')}")
    lines.append("")
    lines.append("Turns:")
    for item in payload.get("turns", []):
        data = ",".join(item.get("data_names") or [])
        suffix = f" data={data}" if data else ""
        lines.append(f"- {item.get('turn_id')} [{item.get('status')}] {item.get('request')}{suffix}")
    return "\n".join(lines)


def _set_thread_bool(settings: Settings, session_id: str, column: str, value: bool) -> ThreadSummary:
    if column not in {"archived", "pinned"}:
        raise ValueError(f"unsupported thread flag: {column}")
    store = ChatMemoryStore(settings.db_path)
    store.ensure_session(session_id)
    with store.storage.connect() as con:
        con.execute(
            f"UPDATE chat_sessions SET {column} = ?, updated_at = CURRENT_TIMESTAMP WHERE session_id = ?",
            [bool(value), session_id],
        )
    return get_thread(settings, session_id)


def _latest_turn_id(store: ChatMemoryStore, session_id: str) -> str:
    try:
        return store.latest_chat_turn_id(session_id)
    except Exception:
        return ""


def _thread_from_row(row: Any) -> ThreadSummary:
    return ThreadSummary(
        session_id=str(row[0] or ""),
        title=str(row[1] or ""),
        summary=str(row[2] or ""),
        archived=bool(row[3]),
        pinned=bool(row[4]),
        message_count=int(row[5] or 0),
        turn_count=int(row[6] or 0),
        created_at=str(row[7] or ""),
        updated_at=str(row[8] or ""),
        meta=_loads_json_object(row[9]),
    )


def _new_thread_id() -> str:
    return f"thread_{uuid.uuid4().hex[:12]}"


def _loads_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _loads_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []
