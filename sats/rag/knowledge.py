from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sats.config import Settings
from sats.stock_basic_lookup import load_stock_basic_frame, stock_basic_rows_to_documents
from sats.storage.duckdb import DuckDBStorage

DEFAULT_COLLECTIONS = {
    "chan": {
        "name": "chan",
        "description": "缠论规则、买卖点、中枢、背驰和风险控制知识库。",
        "paths": ("knowledge/chan/rules",),
        "tags": ("chan", "缠论"),
    },
    "technical": {
        "name": "technical",
        "description": "技术指标、K线、成交量、波动率和短线技术分析 skills。",
        "paths": (
            "skills/technical-basic/SKILL.md",
            "skills/candlestick/SKILL.md",
            "skills/volatility/SKILL.md",
            "skills/minute-analysis/SKILL.md",
            "skills/bull-trend/SKILL.md",
            "skills/shrink-pullback/SKILL.md",
            "skills/ma-golden-cross/SKILL.md",
            "skills/volume-breakout/SKILL.md",
            "skills/box-oscillation/SKILL.md",
            "skills/bottom-volume/SKILL.md",
            "skills/one-yang-three-yin/SKILL.md",
            "skills/elliott-wave/SKILL.md",
        ),
        "tags": ("technical", "技术指标"),
    },
    "signals": {
        "name": "signals",
        "description": "SATS 信号分析、筛选规则、缠论信号和机会发现知识库。",
        "paths": (
            "skills/sats-market-assistant/SKILL.md",
            "skills/workflow-templates/SKILL.md",
            "skills/quant-factor-screener/SKILL.md",
            "skills/small-cap-growth-identifier/SKILL.md",
            "skills/bull-trend/SKILL.md",
            "skills/shrink-pullback/SKILL.md",
            "skills/ma-golden-cross/SKILL.md",
            "skills/volume-breakout/SKILL.md",
            "skills/box-oscillation/SKILL.md",
            "skills/bottom-volume/SKILL.md",
            "skills/one-yang-three-yin/SKILL.md",
            "skills/dragon-head/SKILL.md",
            "sats/signals",
            "sats/screening/rules",
        ),
        "tags": ("signals", "信号分析"),
    },
    "sentiment": {
        "name": "sentiment",
        "description": "A 股市场情绪、热点板块、市场微结构和资金行为知识库。",
        "paths": (
            "skills/sentiment-analysis/SKILL.md",
            "skills/market-microstructure/SKILL.md",
            "skills/sector-rotation/SKILL.md",
            "skills/insider-trading-analyzer/SKILL.md",
            "skills/event-driven-detector/SKILL.md",
            "skills/sentiment-reality-gap/SKILL.md",
            "skills/hot-theme/SKILL.md",
            "skills/emotion-cycle/SKILL.md",
            "skills/expectation-repricing/SKILL.md",
        ),
        "tags": ("sentiment", "A股情绪"),
    },
    "market": {
        "name": "market",
        "description": "A 股大盘、数据路由、行情数据源和市场助手知识库。",
        "paths": (
            "skills/sats-market-assistant/SKILL.md",
            "skills/data-routing/SKILL.md",
            "skills/tickflow/SKILL.md",
            "skills/tushare-data/SKILL.md",
            "skills/sector-rotation/SKILL.md",
            "skills/dragon-head/SKILL.md",
            "skills/hot-theme/SKILL.md",
            "skills/emotion-cycle/SKILL.md",
        ),
        "tags": ("market", "大盘"),
    },
    "fundamental": {
        "name": "fundamental",
        "description": "基本面、财报、估值、财务筛选和公司事件知识库。",
        "paths": (
            "skills/fundamental-filter/SKILL.md",
            "skills/financial-statement/SKILL.md",
            "skills/valuation-model/SKILL.md",
            "skills/corporate-events/SKILL.md",
            "skills/quant-factor-screener/SKILL.md",
            "skills/high-dividend-strategy/SKILL.md",
            "skills/undervalued-stock-screener/SKILL.md",
            "skills/small-cap-growth-identifier/SKILL.md",
            "skills/esg-screener/SKILL.md",
            "skills/tech-hype-vs-fundamentals/SKILL.md",
            "skills/expectation-repricing/SKILL.md",
            "skills/growth-quality/SKILL.md",
        ),
        "tags": ("fundamental", "基本面"),
    },
    "risk": {
        "name": "risk",
        "description": "风险分析、监管知识和 A 股 ST/退市/合规约束知识库。",
        "paths": (
            "skills/risk-analysis/SKILL.md",
            "skills/regulatory-knowledge/SKILL.md",
            "skills/ashare-pre-st-filter/SKILL.md",
            "skills/portfolio-health-check/SKILL.md",
            "skills/risk-adjusted-return-optimizer/SKILL.md",
            "skills/suitability-report-generator/SKILL.md",
        ),
        "tags": ("risk", "风险"),
    },
    "stock-basic": {
        "name": "stock-basic",
        "description": "Tushare/TickFlow stock_basic A 股股票名称、代码、行业和交易所映射知识库。",
        "paths": (),
        "tags": ("stock-basic", "股票列表"),
    },
}

TEXT_SUFFIXES = {".md", ".txt", ".py", ".json"}
PDF_SUFFIXES = {".pdf"}


@dataclass(frozen=True, slots=True)
class KnowledgeBase:
    knowledge_id: str
    name: str
    description: str
    collection_name: str
    tags: tuple[str, ...]
    archived: bool = False


@dataclass(frozen=True, slots=True)
class KnowledgeFile:
    file_id: str
    filename: str
    path: str
    content_hash: str
    content_type: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class KnowledgeChunk:
    content: str
    title: str = ""
    source_path: str = ""
    page_number: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    tags: tuple[str, ...] = ()
    meta: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeSearchResult:
    chunk_id: str
    knowledge_id: str
    knowledge_name: str
    collection_name: str
    file_id: str
    source_path: str
    title: str
    content: str
    score: float
    tags: tuple[str, ...]
    page_number: int | None = None
    line_start: int | None = None
    line_end: int | None = None

    def to_source(self) -> dict[str, Any]:
        source = {
            "type": "knowledge",
            "knowledge": self.knowledge_name,
            "collection": self.collection_name,
            "source_path": self.source_path,
            "title": self.title,
            "score": round(self.score, 3),
            "tags": list(self.tags),
        }
        if self.page_number is not None:
            source["page"] = self.page_number
        if self.line_start is not None:
            source["line_start"] = self.line_start
        if self.line_end is not None:
            source["line_end"] = self.line_end
        return source


class KnowledgeStore:
    def __init__(self, db_path: Path | str | None = None, *, storage: DuckDBStorage | None = None) -> None:
        self.storage = storage or DuckDBStorage(db_path or "data/sats.duckdb")

    def add_knowledge_base(
        self,
        *,
        name: str,
        description: str = "",
        tags: Iterable[str] = (),
        collection_name: str | None = None,
    ) -> KnowledgeBase:
        clean_name = _clean_required(name, "knowledge name")
        clean_collection = _clean_collection(collection_name or clean_name)
        clean_tags = _clean_tags(tags)
        self.storage.initialize()
        with self.storage.connect() as con:
            existing = con.execute(
                """
                SELECT knowledge_id
                FROM knowledge_bases
                WHERE name = ? OR collection_name = ?
                LIMIT 1
                """,
                [clean_name, clean_collection],
            ).fetchone()
            if existing:
                knowledge_id = str(existing[0])
                con.execute(
                    """
                    UPDATE knowledge_bases
                    SET name = ?, description = ?, collection_name = ?, tags_json = ?,
                        archived = FALSE, updated_at = CURRENT_TIMESTAMP
                    WHERE knowledge_id = ?
                    """,
                    [
                        clean_name,
                        str(description or ""),
                        clean_collection,
                        json.dumps(clean_tags, ensure_ascii=False),
                        knowledge_id,
                    ],
                )
            else:
                knowledge_id = _new_id("kb")
                con.execute(
                    """
                    INSERT INTO knowledge_bases
                        (knowledge_id, name, description, collection_name, tags_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        knowledge_id,
                        clean_name,
                        str(description or ""),
                        clean_collection,
                        json.dumps(clean_tags, ensure_ascii=False),
                    ],
                )
        return self.get_knowledge_base(clean_name)

    def get_knowledge_base(self, name_or_id: str) -> KnowledgeBase:
        key = _clean_required(name_or_id, "knowledge")
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                """
                SELECT knowledge_id, name, description, collection_name, tags_json, archived
                FROM knowledge_bases
                WHERE knowledge_id = ? OR name = ? OR collection_name = ?
                LIMIT 1
                """,
                [key, key, _clean_collection(key)],
            ).fetchone()
        if row is None:
            raise ValueError(f"unknown knowledge base: {name_or_id}")
        return _knowledge_from_row(row)

    def list_knowledge_bases(self, *, include_archived: bool = False) -> list[KnowledgeBase]:
        self.storage.initialize()
        where = "" if include_archived else "WHERE archived = FALSE"
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT knowledge_id, name, description, collection_name, tags_json, archived
                FROM knowledge_bases
                {where}
                ORDER BY name ASC
                """
            ).fetchall()
        return [_knowledge_from_row(row) for row in rows]

    def ingest_path(
        self,
        name_or_id: str,
        path: Path | str,
        *,
        tags: Iterable[str] = (),
        project_root: Path | None = None,
    ) -> int:
        knowledge = self.get_knowledge_base(name_or_id)
        root = Path(project_root or ".").resolve()
        paths = _expand_paths(Path(path), root=root)
        total = 0
        for file_path in paths:
            chunks = load_document_chunks(file_path, tags=(*knowledge.tags, *_clean_tags(tags)), project_root=root)
            if not chunks:
                continue
            file_record = self._upsert_file(file_path)
            self._link_file(knowledge.knowledge_id, file_record.file_id)
            total += self._replace_file_chunks(knowledge, file_record, chunks)
        return total

    def search(
        self,
        query: str,
        *,
        knowledge: str | None = None,
        collections: Iterable[str] = (),
        limit: int = 6,
    ) -> list[KnowledgeSearchResult]:
        terms = _query_terms(query)
        if not terms:
            return []
        self.storage.initialize()
        params: list[Any] = []
        filters = ["kb.archived = FALSE"]
        if knowledge:
            kb = self.get_knowledge_base(knowledge)
            filters.append("kc.knowledge_id = ?")
            params.append(kb.knowledge_id)
        clean_collections = tuple(_clean_collection(item) for item in collections if str(item or "").strip())
        if clean_collections:
            placeholders = ", ".join("?" for _ in clean_collections)
            filters.append(f"kc.collection_name IN ({placeholders})")
            params.extend(clean_collections)
        where = " AND ".join(filters)
        with self.storage.connect() as con:
            rows = con.execute(
                f"""
                SELECT kc.chunk_id, kc.knowledge_id, kb.name, kc.collection_name,
                       kc.file_id, kc.source_path, kc.title, kc.content, kc.tags_json,
                       kc.page_number, kc.line_start, kc.line_end
                FROM knowledge_chunks kc
                JOIN knowledge_bases kb ON kb.knowledge_id = kc.knowledge_id
                WHERE {where}
                ORDER BY kc.updated_at DESC, kc.chunk_index ASC
                """
                ,
                params,
            ).fetchall()
        scored: list[KnowledgeSearchResult] = []
        for row in rows:
            tags = tuple(_loads_json_list(row[8]))
            score = _score_chunk(
                query,
                terms,
                content=str(row[7] or ""),
                title=str(row[6] or ""),
                tags=tags,
                collection=str(row[3] or ""),
            )
            if score <= 0:
                continue
            scored.append(
                KnowledgeSearchResult(
                    chunk_id=str(row[0]),
                    knowledge_id=str(row[1]),
                    knowledge_name=str(row[2]),
                    collection_name=str(row[3]),
                    file_id=str(row[4]),
                    source_path=str(row[5] or ""),
                    title=str(row[6] or ""),
                    content=str(row[7] or ""),
                    score=score,
                    tags=tags,
                    page_number=_maybe_int(row[9]),
                    line_start=_maybe_int(row[10]),
                    line_end=_maybe_int(row[11]),
                )
            )
        scored.sort(key=lambda item: (-item.score, item.collection_name, item.source_path, item.title))
        return scored[: max(1, int(limit or 6))]

    def ensure_default_knowledge(self, *, settings: Settings) -> int:
        project_root = Path(getattr(settings, "project_root", ".")).resolve()
        total = 0
        for item in DEFAULT_COLLECTIONS.values():
            kb = self.add_knowledge_base(
                name=item["name"],
                description=item["description"],
                tags=item["tags"],
                collection_name=item["name"],
            )
            chunk_count = self.count_chunks(kb.knowledge_id)
            if item["name"] == "stock-basic":
                if chunk_count == 0:
                    total += self.sync_stock_basic(settings=settings)
                continue
            for relative in item["paths"]:
                path = project_root / relative
                if path.exists():
                    if chunk_count > 0 and path.is_file() and self._linked_file_exists(kb.knowledge_id, path):
                        continue
                    if chunk_count > 0 and path.is_dir():
                        continue
                    total += self.ingest_path(kb.knowledge_id, path, tags=item["tags"], project_root=project_root)
        return total

    def sync_stock_basic(self, *, settings: Settings) -> int:
        kb = self.add_knowledge_base(
            name="stock-basic",
            description=DEFAULT_COLLECTIONS["stock-basic"]["description"],
            tags=DEFAULT_COLLECTIONS["stock-basic"]["tags"],
            collection_name="stock-basic",
        )
        frame = load_stock_basic_frame(settings)
        documents = stock_basic_rows_to_documents(frame)
        file_record = self._upsert_virtual_file(
            filename="stock_basic",
            path="duckdb://stock_basic",
            content="\n\n".join(item["content"] for item in documents),
            content_type="application/x-sats-stock-basic",
        )
        self._link_file(kb.knowledge_id, file_record.file_id)
        chunks = [
            KnowledgeChunk(
                content=item["content"],
                title=item["title"],
                source_path="duckdb://stock_basic",
                tags=("stock-basic", "股票列表", item["ts_code"], item["name"]),
                meta={
                    "ts_code": item["ts_code"],
                    "name": item["name"],
                    "symbol": item["symbol"],
                    "industry": item["industry"],
                    "market": item["market"],
                    "exchange": item["exchange"],
                },
            )
            for item in documents
        ]
        return self._replace_file_chunks(kb, file_record, chunks)

    def count_chunks(self, knowledge: str | None = None) -> int:
        self.storage.initialize()
        params: list[Any] = []
        where = ""
        if knowledge:
            kb = self.get_knowledge_base(knowledge)
            where = "WHERE knowledge_id = ?"
            params.append(kb.knowledge_id)
        with self.storage.connect() as con:
            row = con.execute(f"SELECT COUNT(*) FROM knowledge_chunks {where}", params).fetchone()
        return int(row[0] or 0) if row else 0

    def _upsert_file(self, path: Path) -> KnowledgeFile:
        file_path = path.resolve()
        content = file_path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        file_id = _new_id("file")
        content_type = _content_type(file_path)
        self.storage.initialize()
        with self.storage.connect() as con:
            existing = con.execute(
                """
                SELECT file_id
                FROM knowledge_files
                WHERE path = ?
                LIMIT 1
                """,
                [str(file_path)],
            ).fetchone()
            if existing:
                file_id = str(existing[0])
                con.execute(
                    """
                    UPDATE knowledge_files
                    SET filename = ?, content_hash = ?, content_type = ?, size_bytes = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE file_id = ?
                    """,
                    [file_path.name, digest, content_type, len(content), file_id],
                )
            else:
                con.execute(
                    """
                    INSERT INTO knowledge_files
                        (file_id, filename, path, content_hash, content_type, size_bytes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [file_id, file_path.name, str(file_path), digest, content_type, len(content)],
                )
        return KnowledgeFile(
            file_id=file_id,
            filename=file_path.name,
            path=str(file_path),
            content_hash=digest,
            content_type=content_type,
            size_bytes=len(content),
        )

    def _upsert_virtual_file(
        self,
        *,
        filename: str,
        path: str,
        content: str,
        content_type: str,
    ) -> KnowledgeFile:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        file_id = _new_id("file")
        self.storage.initialize()
        with self.storage.connect() as con:
            existing = con.execute(
                """
                SELECT file_id
                FROM knowledge_files
                WHERE path = ?
                LIMIT 1
                """,
                [path],
            ).fetchone()
            if existing:
                file_id = str(existing[0])
                con.execute(
                    """
                    UPDATE knowledge_files
                    SET filename = ?, content_hash = ?, content_type = ?, size_bytes = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE file_id = ?
                    """,
                    [filename, digest, content_type, len(content.encode("utf-8")), file_id],
                )
            else:
                con.execute(
                    """
                    INSERT INTO knowledge_files
                        (file_id, filename, path, content_hash, content_type, size_bytes)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    [file_id, filename, path, digest, content_type, len(content.encode("utf-8"))],
                )
        return KnowledgeFile(
            file_id=file_id,
            filename=filename,
            path=path,
            content_hash=digest,
            content_type=content_type,
            size_bytes=len(content.encode("utf-8")),
        )

    def _link_file(self, knowledge_id: str, file_id: str) -> None:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO knowledge_file_links
                    (knowledge_id, file_id, created_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                [knowledge_id, file_id],
            )

    def _linked_file_exists(self, knowledge_id: str, path: Path) -> bool:
        self.storage.initialize()
        with self.storage.connect() as con:
            row = con.execute(
                """
                SELECT 1
                FROM knowledge_file_links kfl
                JOIN knowledge_files kf ON kf.file_id = kfl.file_id
                WHERE kfl.knowledge_id = ? AND kf.path = ?
                LIMIT 1
                """,
                [knowledge_id, str(path.resolve())],
            ).fetchone()
        return row is not None

    def _replace_file_chunks(
        self,
        knowledge: KnowledgeBase,
        file_record: KnowledgeFile,
        chunks: list[KnowledgeChunk],
    ) -> int:
        self.storage.initialize()
        with self.storage.connect() as con:
            con.execute(
                "DELETE FROM knowledge_chunks WHERE knowledge_id = ? AND file_id = ?",
                [knowledge.knowledge_id, file_record.file_id],
            )
            for index, chunk in enumerate(chunks):
                content_hash = hashlib.sha256(chunk.content.encode("utf-8")).hexdigest()
                con.execute(
                    """
                    INSERT INTO knowledge_chunks
                        (chunk_id, knowledge_id, file_id, collection_name, chunk_index,
                         content, title, source_path, page_number, line_start, line_end,
                         tags_json, content_hash, meta_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    """,
                    [
                        _new_id("chk"),
                        knowledge.knowledge_id,
                        file_record.file_id,
                        knowledge.collection_name,
                        index,
                        chunk.content,
                        chunk.title,
                        chunk.source_path,
                        chunk.page_number,
                        chunk.line_start,
                        chunk.line_end,
                        json.dumps(_clean_tags(chunk.tags), ensure_ascii=False),
                        content_hash,
                        json.dumps(chunk.meta or {}, ensure_ascii=False, default=str),
                    ],
                )
        return len(chunks)


def load_document_chunks(
    path: Path,
    *,
    tags: Iterable[str] = (),
    project_root: Path | None = None,
    chunk_size: int = 1600,
    chunk_overlap: int = 180,
) -> list[KnowledgeChunk]:
    file_path = path.resolve()
    suffix = file_path.suffix.lower()
    clean_tags = _clean_tags(tags)
    source = _display_path(file_path, project_root)
    if suffix in TEXT_SUFFIXES:
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".json":
            text = _json_to_text(text)
        return _split_text_chunks(text, source_path=source, tags=clean_tags, chunk_size=chunk_size, overlap=chunk_overlap)
    if suffix in PDF_SUFFIXES:
        return _pdf_chunks(file_path, source_path=source, tags=clean_tags, chunk_size=chunk_size, overlap=chunk_overlap)
    return []


def infer_stock_collections(message: str, *, explicit: Iterable[str] = ()) -> tuple[str, ...]:
    collections = list(explicit)
    text = str(message or "").lower()
    terms = {
        "stock-basic": ("股票名称", "股票代码", "代码", "名称", "简称"),
        "chan": ("缠论", "缠中说禅", "一买", "二买", "三买", "背驰", "中枢", "chan"),
        "technical": ("技术", "指标", "macd", "kdj", "rsi", "均线", "金叉", "k线", "k 线", "量价", "放量", "回踩", "箱体", "一阳夹三阴"),
        "signals": ("信号", "筛选", "选股", "机会", "上涨", "突破", "回撤", "形态", "多因子", "因子", "小盘成长", "底部放量", "龙头"),
        "sentiment": ("情绪", "情绪周期", "热点", "涨停", "跌停", "板块", "题材", "资金流", "赚钱效应", "事件驱动", "董监高", "错杀", "预期重估"),
        "market": ("大盘", "指数", "市场", "宽度", "上证", "创业板", "沪深300", "北证", "行业轮动", "经济周期", "热点题材", "龙头"),
        "fundamental": ("财报", "财务", "估值", "pe", "pb", "roe", "利润", "现金流", "基本面", "高股息", "低估值", "esg", "科技泡沫", "成长质量", "预期差"),
        "risk": ("风险", "止损", "监管", "退市", "st", "仓位", "风控", "组合诊断", "组合优化", "适当性"),
    }
    for name, needles in terms.items():
        if any(term in text for term in needles):
            collections.append(name)
    if not collections and _looks_stock_related(text):
        collections.extend(["stock-basic", "technical", "signals", "sentiment", "market", "risk"])
    return tuple(dict.fromkeys(_clean_collection(item) for item in collections if item))


def format_knowledge_list(items: list[KnowledgeBase]) -> str:
    if not items:
        return "无知识库"
    return "\n".join(
        f"{index}. {item.name} ({item.collection_name}) - {item.description}"
        for index, item in enumerate(items, start=1)
    )


def format_search_results(results: list[KnowledgeSearchResult]) -> str:
    if not results:
        return "无结果"
    lines = []
    for index, row in enumerate(results, start=1):
        source = row.source_path
        if row.page_number is not None:
            source += f":p{row.page_number}"
        elif row.line_start is not None:
            source += f":{row.line_start}"
        title = f" {row.title}" if row.title else ""
        snippet = re.sub(r"\s+", " ", row.content).strip()[:180]
        lines.append(f"{index}. [{row.collection_name}] {source}{title} score={row.score:.2f}\n   {snippet}")
    return "\n".join(lines)


def build_knowledge_context(results: list[KnowledgeSearchResult]) -> str:
    if not results:
        return ""
    payload = {
        "policy": (
            "SATS has loaded local stock-domain RAG evidence. Use it as methodology and source context only; "
            "real prices, indicators, market breadth and trading conclusions must come from structured SATS market data."
        ),
        "evidence": [
            {
                "source": result.to_source(),
                "content": result.content,
            }
            for result in results
        ],
    }
    return (
        "以下是 SATS 本地股票知识库 RAG 证据。它只提供方法论、规则说明和上下文引用；"
        "不得用它编造实时行情、指标或新闻。涉及股票结论仍必须基于本轮真实结构化数据。\n"
        f"{json.dumps(payload, ensure_ascii=False, default=str)}"
    )


def _expand_paths(path: Path, *, root: Path) -> list[Path]:
    resolved = path if path.is_absolute() else root / path
    resolved = resolved.resolve()
    if not resolved.exists():
        raise ValueError(f"knowledge path does not exist: {path}")
    if resolved.is_file():
        return [resolved] if resolved.suffix.lower() in TEXT_SUFFIXES | PDF_SUFFIXES else []
    files = []
    for child in sorted(resolved.rglob("*")):
        if child.is_file() and child.suffix.lower() in TEXT_SUFFIXES | PDF_SUFFIXES:
            files.append(child.resolve())
    return files


def _split_text_chunks(
    text: str,
    *,
    source_path: str,
    tags: tuple[str, ...],
    chunk_size: int,
    overlap: int,
    page_number: int | None = None,
) -> list[KnowledgeChunk]:
    clean = str(text or "").strip()
    if not clean:
        return []
    sections = _markdown_sections(clean)
    chunks = []
    for title, section_text, line_start in sections:
        for content, start_offset, end_offset in _window_text(section_text, chunk_size=chunk_size, overlap=overlap):
            line_delta_start = section_text[:start_offset].count("\n")
            line_delta_end = section_text[:end_offset].count("\n")
            chunks.append(
                KnowledgeChunk(
                    content=content.strip(),
                    title=title,
                    source_path=source_path,
                    page_number=page_number,
                    line_start=line_start + line_delta_start,
                    line_end=line_start + line_delta_end,
                    tags=tags,
                )
            )
    return [chunk for chunk in chunks if chunk.content]


def _markdown_sections(text: str) -> list[tuple[str, str, int]]:
    lines = text.splitlines()
    sections: list[tuple[str, list[str], int]] = []
    current_title = ""
    current_lines: list[str] = []
    current_start = 1
    for index, line in enumerate(lines, start=1):
        match = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if match and current_lines:
            sections.append((current_title, current_lines, current_start))
            current_title = match.group(2).strip()
            current_lines = [line]
            current_start = index
        else:
            if match:
                current_title = match.group(2).strip()
                current_start = index
            current_lines.append(line)
    if current_lines:
        sections.append((current_title, current_lines, current_start))
    return [(title, "\n".join(section_lines), start) for title, section_lines, start in sections]


def _window_text(text: str, *, chunk_size: int, overlap: int) -> list[tuple[str, int, int]]:
    if len(text) <= chunk_size:
        return [(text, 0, len(text))]
    windows = []
    step = max(1, chunk_size - max(0, overlap))
    start = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        if end < len(text):
            split_at = max(text.rfind("\n\n", start, end), text.rfind("\n", start, end), text.rfind("。", start, end))
            if split_at > start + chunk_size // 2:
                end = split_at + 1
        windows.append((text[start:end], start, end))
        if end >= len(text):
            break
        start = max(end - overlap, start + step)
    return windows


def _pdf_chunks(
    path: Path,
    *,
    source_path: str,
    tags: tuple[str, ...],
    chunk_size: int,
    overlap: int,
) -> list[KnowledgeChunk]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("pypdf is required to ingest PDF knowledge files") from exc
    chunks: list[KnowledgeChunk] = []
    reader = PdfReader(str(path))
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        chunks.extend(
            _split_text_chunks(
                text,
                source_path=source_path,
                tags=tags,
                chunk_size=chunk_size,
                overlap=overlap,
                page_number=index,
            )
        )
    return chunks


def _json_to_text(raw: str) -> str:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def _score_chunk(
    query: str,
    terms: list[str],
    *,
    content: str,
    title: str,
    tags: tuple[str, ...],
    collection: str,
) -> float:
    haystack = f"{title}\n{content}".lower()
    tag_text = " ".join(tags).lower()
    score = 0.0
    raw = str(query or "").lower().strip()
    if raw and raw in haystack:
        score += 8.0
    for term in terms:
        tf = haystack.count(term)
        if tf:
            score += (1.0 + math.log(1 + tf)) * (2.0 + min(len(term), 8) / 4)
        if term in str(title or "").lower():
            score += 5.0
        if term in tag_text or any(tag in term for tag in tags):
            score += 4.0
        if term == collection:
            score += 2.0
    if _collection_matches_query(collection, raw):
        score += 3.0
    return score


def _collection_matches_query(collection: str, query: str) -> bool:
    mapping = {
        "chan": ("缠论", "三买", "背驰", "中枢", "chan"),
        "technical": ("技术", "指标", "macd", "均线", "金叉", "k线", "回踩", "箱体"),
        "signals": ("信号", "筛选", "机会", "选股", "突破", "底部放量", "龙头"),
        "sentiment": ("情绪", "情绪周期", "热点", "涨停", "板块", "题材", "预期重估"),
        "market": ("大盘", "指数", "宽度", "市场", "热点题材", "龙头"),
        "fundamental": ("财报", "估值", "基本面", "财务", "成长质量", "预期差"),
        "risk": ("风险", "止损", "监管", "st"),
    }
    return any(term in query for term in mapping.get(collection, ()))


def _query_terms(query: str) -> list[str]:
    text = str(query or "").strip().lower()
    tokens = re.findall(r"[a-z0-9_.-]+", text)
    tokens.extend(part for part in re.split(r"[\s,，、。；;:：/|]+", text) if len(part) >= 2)
    tokens.extend(re.findall(r"[\u4e00-\u9fff]{2,}", text))
    return list(dict.fromkeys(token for token in tokens if token))


def _looks_stock_related(text: str) -> bool:
    return bool(re.search(r"\b[036]\d{5}\b", text)) or any(
        term in text for term in ("股票", "a股", "A股".lower(), "买点", "卖点", "走势")
    )


def _knowledge_from_row(row: Any) -> KnowledgeBase:
    return KnowledgeBase(
        knowledge_id=str(row[0]),
        name=str(row[1]),
        description=str(row[2] or ""),
        collection_name=str(row[3] or ""),
        tags=tuple(_loads_json_list(row[4])),
        archived=bool(row[5]),
    )


def _display_path(path: Path, project_root: Path | None) -> str:
    if project_root is None:
        return str(path)
    try:
        return str(path.relative_to(project_root.resolve()))
    except ValueError:
        return str(path)


def _content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".md":
        return "text/markdown"
    if suffix == ".json":
        return "application/json"
    if suffix == ".pdf":
        return "application/pdf"
    return "text/plain"


def _clean_required(value: str, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _clean_collection(value: str) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff.-]+", "-", text)
    return text.strip("-") or "knowledge"


def _clean_tags(tags: Iterable[str]) -> tuple[str, ...]:
    result = []
    seen = set()
    for tag in tags:
        text = str(tag or "").strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return tuple(result)


def _loads_json_list(raw: Any) -> list[str]:
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
