from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


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
