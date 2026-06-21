from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sats.storage.duckdb import DuckDBStorage
from sats.web.providers import canonicalize_url


class WebIndex:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.storage = DuckDBStorage(getattr(settings, "db_path", "data/sats.duckdb"))

    def get_document(self, url: str) -> dict[str, Any] | None:
        canonical = canonicalize_url(url)
        if not canonical:
            return None
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                """
                SELECT document_id, url, canonical_url, title, content, content_hash,
                       content_type, extraction_method, published_at, fetched_at, expires_at, meta_json
                FROM web_documents
                WHERE canonical_url = ?
                LIMIT 1
                """,
                [canonical],
            ).fetchone()
        return _document_from_row(row) if row else None

    def put_document(
        self,
        *,
        url: str,
        title: str,
        content: str,
        content_type: str,
        extraction_method: str,
        published_at: Any = None,
        meta: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
    ) -> dict[str, Any]:
        canonical = canonicalize_url(url)
        if not canonical:
            raise ValueError(f"invalid web document URL: {url}")
        clean_content = str(content or "").strip()
        if not clean_content:
            raise ValueError("web document content is empty")
        fetched_at = datetime.now(timezone.utc)
        ttl = max(60, int(ttl_seconds or getattr(self.settings, "web_page_cache_ttl_seconds", 86400) or 86400))
        expires_at = fetched_at + timedelta(seconds=ttl)
        content_hash = hashlib.sha256(clean_content.encode("utf-8")).hexdigest()
        document_id = f"web_{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:24]}"
        chunks = split_web_content(clean_content, title=title)
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT INTO web_documents
                    (document_id, url, canonical_url, title, content, content_hash,
                     content_type, extraction_method, published_at, fetched_at, expires_at, meta_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, TRY_CAST(? AS TIMESTAMP), ?, ?, ?)
                ON CONFLICT (document_id) DO UPDATE SET
                    url = EXCLUDED.url,
                    canonical_url = EXCLUDED.canonical_url,
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    content_hash = EXCLUDED.content_hash,
                    content_type = EXCLUDED.content_type,
                    extraction_method = EXCLUDED.extraction_method,
                    published_at = EXCLUDED.published_at,
                    fetched_at = EXCLUDED.fetched_at,
                    expires_at = EXCLUDED.expires_at,
                    meta_json = EXCLUDED.meta_json
                """,
                [
                    document_id,
                    url,
                    canonical,
                    str(title or "")[:500],
                    clean_content,
                    content_hash,
                    str(content_type or ""),
                    str(extraction_method or ""),
                    str(published_at or "") or None,
                    fetched_at,
                    expires_at,
                    json.dumps(meta or {}, ensure_ascii=False, default=str),
                ],
            )
            con.execute("DELETE FROM web_chunk_embeddings WHERE chunk_id IN (SELECT chunk_id FROM web_chunks WHERE document_id = ?)", [document_id])
            con.execute("DELETE FROM web_chunks WHERE document_id = ?", [document_id])
            for index, chunk in enumerate(chunks):
                chunk_hash = hashlib.sha256(chunk.encode("utf-8")).hexdigest()
                chunk_id = f"wch_{hashlib.sha256(f'{document_id}:{index}:{chunk_hash}'.encode()).hexdigest()[:24]}"
                con.execute(
                    """
                    INSERT INTO web_chunks
                        (chunk_id, document_id, chunk_index, title, content, content_hash, token_estimate, meta_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        chunk_id,
                        document_id,
                        index,
                        str(title or "")[:500],
                        chunk,
                        chunk_hash,
                        max(1, len(chunk) // 4),
                        "{}",
                    ],
                )
        return self.get_document(canonical) or {}

    def chunks_for_documents(self, document_ids: list[str]) -> list[dict[str, Any]]:
        if not document_ids:
            return []
        self.storage.initialize()
        placeholders = ", ".join("?" for _ in document_ids)
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT wc.chunk_id, wc.document_id, wc.chunk_index, wc.title, wc.content,
                       wd.url, wd.canonical_url, wd.content_type, wd.extraction_method,
                       wd.published_at, wd.fetched_at, wd.expires_at, wd.meta_json
                FROM web_chunks wc
                JOIN web_documents wd ON wd.document_id = wc.document_id
                WHERE wc.document_id IN ({placeholders})
                ORDER BY wc.document_id, wc.chunk_index
                """,
                document_ids,
            ).fetchall()
        return [_chunk_from_row(row) for row in rows]

    def get_embeddings(self, chunk_ids: list[str], *, provider: str, model: str) -> dict[str, list[float]]:
        if not chunk_ids:
            return {}
        self.storage.initialize()
        placeholders = ", ".join("?" for _ in chunk_ids)
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT chunk_id, vector
                FROM web_chunk_embeddings
                WHERE provider = ? AND model = ? AND chunk_id IN ({placeholders})
                """,
                [provider, model, *chunk_ids],
            ).fetchall()
        return {str(row[0]): [float(value) for value in row[1]] for row in rows}

    def put_embeddings(self, vectors: dict[str, list[float]], *, provider: str, model: str) -> None:
        if not vectors:
            return
        self.storage.initialize()
        with self.storage.connect() as con:
            for chunk_id, vector in vectors.items():
                con.execute(
                    """
                    INSERT OR REPLACE INTO web_chunk_embeddings
                        (chunk_id, provider, model, vector, created_at)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    [chunk_id, provider, model, vector],
                )

    def clear(
        self,
        *,
        expired_only: bool = False,
        exclude_document_ids: list[str] | tuple[str, ...] = (),
    ) -> dict[str, int]:
        self.storage.initialize()
        filters = ["expires_at <= CURRENT_TIMESTAMP"] if expired_only else []
        params: list[Any] = []
        if exclude_document_ids:
            placeholders = ", ".join("?" for _ in exclude_document_ids)
            filters.append(f"document_id NOT IN ({placeholders})")
            params.extend(exclude_document_ids)
        where = f"WHERE {' AND '.join(filters)}" if filters else ""
        with self.storage.connect() as con:
            document_ids = [
                str(row[0])
                for row in con.execute(
                    f"SELECT document_id FROM web_documents {where}",
                    params,
                ).fetchall()
            ]
            if not document_ids:
                return {"documents": 0, "chunks": 0, "embeddings": 0}
            placeholders = ", ".join("?" for _ in document_ids)
            chunk_ids = [
                str(row[0])
                for row in con.execute(
                    f"SELECT chunk_id FROM web_chunks WHERE document_id IN ({placeholders})",
                    document_ids,
                ).fetchall()
            ]
            embeddings = 0
            if chunk_ids:
                chunk_placeholders = ", ".join("?" for _ in chunk_ids)
                embeddings = int(
                    con.execute(
                        f"SELECT COUNT(*) FROM web_chunk_embeddings WHERE chunk_id IN ({chunk_placeholders})",
                        chunk_ids,
                    ).fetchone()[0]
                )
                con.execute(
                    f"DELETE FROM web_chunk_embeddings WHERE chunk_id IN ({chunk_placeholders})",
                    chunk_ids,
                )
            con.execute(f"DELETE FROM web_chunks WHERE document_id IN ({placeholders})", document_ids)
            con.execute(f"DELETE FROM web_documents WHERE document_id IN ({placeholders})", document_ids)
        return {"documents": len(document_ids), "chunks": len(chunk_ids), "embeddings": embeddings}


def split_web_content(text: str, *, title: str = "", chunk_size: int = 1600, overlap: int = 180) -> list[str]:
    clean = "\n".join(line.rstrip() for line in str(text or "").splitlines()).strip()
    if not clean:
        return []
    prefix = f"{title}\n\n" if title and title not in clean[:300] else ""
    value = prefix + clean
    if len(value) <= chunk_size:
        return [value]
    chunks = []
    start = 0
    while start < len(value):
        end = min(len(value), start + chunk_size)
        if end < len(value):
            split_at = max(
                value.rfind("\n\n", start, end),
                value.rfind("\n", start, end),
                value.rfind("。", start, end),
                value.rfind(". ", start, end),
            )
            if split_at > start + chunk_size // 2:
                end = split_at + 1
        chunk = value[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(value):
            break
        start = max(start + 1, end - overlap)
    return chunks


def is_expired(document: dict[str, Any]) -> bool:
    expires_at = document.get("expires_at")
    if not isinstance(expires_at, datetime):
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


def _document_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "document_id": str(row[0]),
        "url": str(row[1]),
        "canonical_url": str(row[2]),
        "title": str(row[3] or ""),
        "content": str(row[4] or ""),
        "content_hash": str(row[5] or ""),
        "content_type": str(row[6] or ""),
        "extraction_method": str(row[7] or ""),
        "published_at": row[8],
        "fetched_at": row[9],
        "expires_at": row[10],
        "meta": _loads_dict(row[11]),
    }


def _chunk_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "chunk_id": str(row[0]),
        "document_id": str(row[1]),
        "chunk_index": int(row[2]),
        "title": str(row[3] or ""),
        "content": str(row[4] or ""),
        "url": str(row[5] or ""),
        "canonical_url": str(row[6] or ""),
        "content_type": str(row[7] or ""),
        "extraction_method": str(row[8] or ""),
        "published_at": row[9],
        "fetched_at": row[10],
        "expires_at": row[11],
        "meta": _loads_dict(row[12]),
    }


def _loads_dict(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
