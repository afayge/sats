from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any


def cache_dir(settings: Any, namespace: str) -> Path:
    root = Path(getattr(settings, "project_root", ".") or ".")
    path = root / "runtime" / "cache" / namespace
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_key(*parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def read_cache(path: Path, ttl_seconds: int) -> dict[str, Any] | None:
    if ttl_seconds <= 0 or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    created_at = float(payload.get("_cached_at") or 0.0)
    if created_at <= 0 or time.time() - created_at > ttl_seconds:
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    data = dict(data)
    data["from_cache"] = True
    return data


def write_cache(path: Path, data: dict[str, Any]) -> None:
    try:
        path.write_text(
            json.dumps({"_cached_at": time.time(), "data": data}, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass
