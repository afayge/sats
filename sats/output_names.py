from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from sats.analysis.market_llm_context import DEFAULT_MARKET_INDICES
from sats.storage.duckdb import DuckDBStorage


SECURITY_CODE_PATTERN = re.compile(r"\b\d{6}\.[A-Z]{2,4}\b", re.IGNORECASE)
INDEX_NAMES = dict(DEFAULT_MARKET_INDICES)


class SecurityNameResolver:
    def __init__(self, settings: Any, *, db_path: Path | str, provider_factory: Any = None) -> None:
        self.settings = settings
        self.db_path = Path(db_path)
        self.provider_factory = provider_factory
        self._names = dict(INDEX_NAMES)
        self._stock_basic_loaded = False
        self._provider = None
        self._index_lookups: set[str] = set()

    def name_for(self, code: str) -> str:
        normalized = str(code or "").strip().upper()
        if not normalized:
            return ""
        name = self._names.get(normalized, "")
        if name:
            return name
        if normalized.endswith((".SH", ".SZ", ".BJ")):
            self._load_stock_basic()
        if normalized not in self._names:
            self._load_index_name(normalized)
        return self._names.get(normalized, "")

    def remember(self, code: str, name: str) -> None:
        normalized = str(code or "").strip().upper()
        clean_name = str(name or "").strip()
        if normalized and clean_name:
            self._names.setdefault(normalized, clean_name)

    def _load_stock_basic(self) -> None:
        if self._stock_basic_loaded:
            return
        self._stock_basic_loaded = True
        frame = None
        try:
            frame = DuckDBStorage(self.db_path).get_stock_basic()
        except Exception:
            pass
        if frame is None or frame.empty:
            try:
                frame = self._get_provider().load_stock_basic(
                    storage=DuckDBStorage(self.db_path)
                )
            except Exception:
                return
        if "ts_code" not in frame.columns or "name" not in frame.columns:
            return
        for _, row in frame.iterrows():
            self.remember(row.get("ts_code"), row.get("name"))

    def _load_index_name(self, code: str) -> None:
        if code in self._index_lookups:
            return
        self._index_lookups.add(code)
        try:
            provider = self._get_provider()
            payload = provider.fetch_tushare_dataset(
                "index_basic",
                {"ts_code": code},
                fields=["ts_code", "name"],
                limit=10,
            )
        except Exception:
            return
        rows = list(payload.get("rows") or payload.get("head") or [])
        latest = payload.get("latest")
        if isinstance(latest, dict):
            rows.append(latest)
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_code = str(row.get("ts_code") or "").strip().upper()
            if row_code == code:
                self.remember(row_code, row.get("name"))

    def _get_provider(self):
        if self._provider is not None:
            return self._provider
        if self.provider_factory is None:
            from sats.data.astock_provider import AStockDataProvider

            self.provider_factory = AStockDataProvider
        self._provider = self.provider_factory(self.settings)
        return self._provider


class SecurityNameOutput:
    def __init__(self, target: Any, resolver: SecurityNameResolver) -> None:
        self.target = target
        self.resolver = resolver
        self._pending = ""

    def write(self, text: str) -> int:
        value = str(text)
        if not self._pending and _is_complete_json(value):
            self.target.write(enrich_security_names(value, self.resolver))
            return len(text)
        if self._pending:
            self._pending += value
            self._drain_pending()
            return len(text)
        if SECURITY_CODE_PATTERN.search(value) and not value.endswith(("\n", "\r")):
            self._pending = value
            return len(text)
        self.target.write(enrich_security_names(value, self.resolver))
        return len(text)

    def flush(self) -> None:
        if self._pending:
            self.target.write(enrich_security_names(self._pending, self.resolver))
            self._pending = ""
        self.target.flush()

    @property
    def encoding(self):
        return getattr(self.target, "encoding", None)

    @property
    def errors(self):
        return getattr(self.target, "errors", None)

    @property
    def buffer(self):
        return getattr(self.target, "buffer")

    def isatty(self) -> bool:
        handler = getattr(self.target, "isatty", None)
        return bool(handler()) if callable(handler) else False

    def fileno(self) -> int:
        return int(self.target.fileno())

    def writable(self) -> bool:
        handler = getattr(self.target, "writable", None)
        return bool(handler()) if callable(handler) else True

    def __getattr__(self, name: str):
        return getattr(self.target, name)

    def _drain_pending(self) -> None:
        boundary = max(self._pending.rfind("\n"), self._pending.rfind("\r"))
        if boundary < 0:
            return
        ready = self._pending[: boundary + 1]
        self._pending = self._pending[boundary + 1 :]
        self.target.write(enrich_security_names(ready, self.resolver))


def enrich_security_names(text: str, resolver: SecurityNameResolver) -> str:
    if not SECURITY_CODE_PATTERN.search(text):
        return text
    stripped = text.strip()
    if stripped.startswith(("{", "[")):
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            pass
        else:
            enriched = _enrich_json_value(payload, resolver)
            suffix = "\n" if text.endswith("\n") else ""
            return json.dumps(enriched, ensure_ascii=False, indent=2, default=str) + suffix
    return "".join(_enrich_plain_line(line, resolver) for line in text.splitlines(keepends=True))


def _is_complete_json(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        json.loads(stripped)
    except (TypeError, ValueError):
        return False
    return True


def _enrich_json_value(value: Any, resolver: SecurityNameResolver) -> Any:
    if isinstance(value, list):
        return [_enrich_json_value(item, resolver) for item in value]
    if not isinstance(value, dict):
        return value

    result = {key: _enrich_json_value(item, resolver) for key, item in value.items()}
    code = _record_security_code(result)
    existing_name = _record_security_name(result)
    if code and existing_name:
        resolver.remember(code, existing_name)
    elif code:
        name = resolver.name_for(code)
        if name:
            result["name"] = name
    return result


def _record_security_code(record: dict[str, Any]) -> str:
    for key in ("ts_code", "index_code", "security_code", "stock_code"):
        value = str(record.get(key) or "").strip().upper()
        if SECURITY_CODE_PATTERN.fullmatch(value):
            return value
    return ""


def _record_security_name(record: dict[str, Any]) -> str:
    for key in ("name", "stock_name", "index_name", "security_name", "ts_name"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""


def _enrich_plain_line(line: str, resolver: SecurityNameResolver) -> str:
    result = line
    for match in reversed(list(SECURITY_CODE_PATTERN.finditer(line))):
        code = str(match.group(0) or "").upper()
        name = resolver.name_for(code)
        if not name or name in line:
            continue
        start, end = match.span()
        result = f"{result[:start]}{code} {name}{result[end:]}"
    return result
