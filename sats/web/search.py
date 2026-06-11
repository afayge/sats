from __future__ import annotations

import concurrent.futures
from datetime import datetime, timezone
from typing import Any

from sats.config import Settings, load_settings
from sats.web.cache import cache_dir, cache_key, read_cache, write_cache


_GARBAGE_PATTERNS = (
    "拼音",
    "汉语",
    "通用规范汉字",
    "常用字",
    "甲骨文",
    "部首",
    "笔画",
    "Unicode",
    "字形演变",
    "释义",
)


def search(
    query: str,
    *,
    limit: int = 5,
    trusted_domains: list[str] | tuple[str, ...] | None = None,
    freshness: str = "",
    settings: Settings | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    settings = settings or load_settings()
    clean_query = " ".join(str(query or "").split())
    max_results = _clamp_limit(limit, settings)
    domains = tuple(_clean_domain(item) for item in trusted_domains or () if _clean_domain(item))
    freshness = _clean_freshness(freshness)
    if not clean_query:
        return _error_payload(clean_query, "query is required")
    effective_query = _domain_query(clean_query, domains)
    ttl = max(0, int(getattr(settings, "web_search_cache_ttl_seconds", 43200) or 0))
    path = cache_dir(settings, "web_search") / f"{cache_key(effective_query, max_results, freshness)}.json"
    if use_cache:
        cached = read_cache(path, ttl)
        if cached is not None:
            return cached
    fetched_at = _now()
    try:
        raw_results = _ddg_search(
            effective_query,
            max_results=max_results,
            timeout_seconds=max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10)),
            freshness=freshness,
        )
    except ImportError as exc:
        return _error_payload(clean_query, str(exc), fetched_at=fetched_at, effective_query=effective_query)
    except Exception as exc:
        return _error_payload(clean_query, str(exc), fetched_at=fetched_at, effective_query=effective_query)
    results = [_normalize_result(item, fetched_at=fetched_at) for item in raw_results]
    results = [item for item in results if item and not _is_garbage_result(item)][:max_results]
    payload = {
        "status": "ok",
        "query": clean_query,
        "effective_query": effective_query,
        "trusted_domains": list(domains),
        "freshness": freshness,
        "results": results,
        "fetched_at": fetched_at,
        "from_cache": False,
    }
    write_cache(path, payload)
    return payload


def _ddg_search(query: str, *, max_results: int, timeout_seconds: int, freshness: str) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise ImportError("ddgs is not installed; run: pip install ddgs>=9") from exc

    def run() -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"region": "cn-zh", "safesearch": "off", "max_results": max_results}
        if freshness:
            kwargs["timelimit"] = freshness
        with DDGS() as client:
            return list(client.text(query, **kwargs))

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = pool.submit(run)
    try:
        return future.result(timeout=timeout_seconds)
    except concurrent.futures.TimeoutError as exc:
        raise TimeoutError(f"web search timed out after {timeout_seconds}s") from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _normalize_result(item: Any, *, fetched_at: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    title = str(item.get("title") or "").strip()
    url = str(item.get("href") or item.get("url") or "").strip()
    snippet = str(item.get("body") or item.get("snippet") or item.get("content") or "").strip()
    if not title and not url and not snippet:
        return {}
    return {
        "title": title[:180],
        "url": url,
        "snippet": snippet[:500],
        "source": str(item.get("source") or "ddgs"),
        "fetched_at": fetched_at,
    }


def _clamp_limit(limit: int, settings: Settings) -> int:
    configured = int(getattr(settings, "web_search_max_results", 10) or 10)
    try:
        requested = int(limit)
    except (TypeError, ValueError):
        requested = 5
    return max(1, min(requested, max(1, configured), 10))


def _clean_domain(value: Any) -> str:
    domain = str(value or "").strip().lower()
    domain = domain.removeprefix("https://").removeprefix("http://").strip("/")
    if "/" in domain or " " in domain or not domain:
        return ""
    return domain


def _clean_freshness(value: Any) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "day": "d",
        "daily": "d",
        "week": "w",
        "weekly": "w",
        "month": "m",
        "monthly": "m",
        "year": "y",
        "yearly": "y",
    }
    text = aliases.get(text, text)
    return text if text in {"d", "w", "m", "y"} else ""


def _domain_query(query: str, domains: tuple[str, ...]) -> str:
    if not domains:
        return query
    sites = " OR ".join(f"site:{domain}" for domain in domains[:6])
    return f"({sites}) {query}"


def _is_garbage_result(item: dict[str, Any]) -> bool:
    text = f"{item.get('title') or ''} {item.get('snippet') or ''}"
    return sum(1 for pattern in _GARBAGE_PATTERNS if pattern in text) >= 2


def _error_payload(query: str, error: str, *, fetched_at: str | None = None, effective_query: str = "") -> dict[str, Any]:
    return {
        "status": "error",
        "query": query,
        "effective_query": effective_query or query,
        "results": [],
        "error": error,
        "fetched_at": fetched_at or _now(),
        "from_cache": False,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
