from __future__ import annotations

import concurrent.futures
import html
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

import httpx


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


@dataclass(frozen=True, slots=True)
class ProviderResult:
    provider: str
    status: str
    items: tuple[dict[str, Any], ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "provider": self.provider,
            "status": self.status,
            "result_count": len(self.items),
        }
        if self.error:
            payload["error"] = self.error
        return payload


def configured_provider_names(settings: Any, providers: list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    if providers is None:
        raw = getattr(settings, "web_search_providers", None)
        if raw is None:
            raw = "ddgs"
        values = str(raw or "").split(",")
    else:
        values = providers
    names = []
    for value in values:
        name = str(value or "").strip().lower()
        if name in {"ddgs", "bing", "tavily", "bocha", "querit"} and name not in names:
            names.append(name)
    return tuple(names or ("ddgs",))


def search_many(
    queries: list[str],
    *,
    provider_names: tuple[str, ...],
    settings: Any,
    max_results: int,
    freshness: str,
    domains: tuple[str, ...],
    ddgs_searcher: Callable[..., list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tasks: list[tuple[str, str]] = [(query, provider) for query in queries for provider in provider_names]
    results: list[ProviderResult] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(8, len(tasks)))) as pool:
        futures = {
            pool.submit(
                _search_provider,
                provider,
                query,
                settings=settings,
                max_results=max_results,
                freshness=freshness,
                domains=domains,
                ddgs_searcher=ddgs_searcher,
            ): (query, provider)
            for query, provider in tasks
        }
        for future in concurrent.futures.as_completed(futures):
            query, provider = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover - defensive isolation
                result = ProviderResult(provider=provider, status="error", error=str(exc))
            items = tuple({**item, "query": query, "provider": provider} for item in result.items)
            results.append(ProviderResult(provider=provider, status=result.status, items=items, error=result.error))

    provider_status = _aggregate_provider_status(results, provider_names)
    merged = _rrf_merge(results)
    return merged, provider_status


def _search_provider(
    provider: str,
    query: str,
    *,
    settings: Any,
    max_results: int,
    freshness: str,
    domains: tuple[str, ...],
    ddgs_searcher: Callable[..., list[dict[str, Any]]],
) -> ProviderResult:
    try:
        if provider == "ddgs":
            effective = _domain_query(query, domains)
            rows = ddgs_searcher(
                effective,
                max_results=max_results,
                timeout_seconds=max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10)),
                freshness=freshness,
            )
            items = tuple(_normalize_item(item, provider="ddgs") for item in rows)
        elif provider == "bing":
            items = tuple(_bing_search(query, settings=settings, max_results=max_results, domains=domains))
        elif provider == "tavily":
            items = tuple(_tavily_search(query, settings=settings, max_results=max_results, domains=domains))
        elif provider == "bocha":
            items = tuple(_bocha_search(query, settings=settings, max_results=max_results, domains=domains))
        elif provider == "querit":
            items = tuple(_querit_search(query, settings=settings, max_results=max_results, freshness=freshness))
        else:
            raise ValueError(f"unsupported web search provider: {provider}")
        clean = tuple(
            item
            for item in items
            if item.get("url")
            and not _is_garbage_item(item)
            and (not domains or _url_matches_domains(str(item.get("url") or ""), domains))
        )
        return ProviderResult(provider=provider, status="ok", items=clean[:max_results])
    except Exception as exc:
        return ProviderResult(provider=provider, status="error", error=str(exc))


def _bing_search(query: str, *, settings: Any, max_results: int, domains: tuple[str, ...]) -> list[dict[str, Any]]:
    effective = _domain_query(query, domains)
    response = httpx.get(
        "https://www.bing.com/search",
        params={"q": effective, "count": max_results},
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "User-Agent": _user_agent(),
        },
        timeout=max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10)),
        follow_redirects=True,
    )
    response.raise_for_status()
    rows = []
    for block in re.findall(r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(.*?)</li>', response.text, flags=re.I | re.S):
        match = re.search(r"<h2[^>]*>\s*<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", block, flags=re.I | re.S)
        if not match:
            continue
        url = html.unescape(match.group(1)).strip()
        title = _strip_html(match.group(2))
        snippet_match = re.search(r'<p[^>]*>(.*?)</p>', block, flags=re.I | re.S)
        snippet = _strip_html(snippet_match.group(1)) if snippet_match else ""
        rows.append({"title": title, "url": url, "snippet": snippet, "source": "bing"})
        if len(rows) >= max_results:
            break
    return rows


def _tavily_search(query: str, *, settings: Any, max_results: int, domains: tuple[str, ...]) -> list[dict[str, Any]]:
    api_key = str(getattr(settings, "web_tavily_api_key", "") or "")
    if not api_key:
        raise RuntimeError("Tavily provider selected but WEB_TAVILY_API_KEY is not configured")
    response = httpx.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_domains": list(domains),
        },
        timeout=max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10)),
    )
    response.raise_for_status()
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("content") or ""),
            "source": "tavily",
        }
        for item in response.json().get("results") or []
    ]


def _bocha_search(query: str, *, settings: Any, max_results: int, domains: tuple[str, ...]) -> list[dict[str, Any]]:
    api_key = str(getattr(settings, "web_bocha_api_key", "") or "")
    if not api_key:
        raise RuntimeError("BoCha provider selected but WEB_BOCHA_API_KEY is not configured")
    last_error: Exception | None = None
    for endpoint in ("https://api.bocha.cn/v1/web-search", "https://api.bochaai.com/v1/web-search"):
        try:
            response = httpx.post(
                endpoint,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"query": _domain_query(query, domains), "freshness": "noLimit", "summary": True, "count": max_results},
                timeout=max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10)),
            )
            response.raise_for_status()
            payload = response.json()
            code = payload.get("code")
            if code is not None and str(code) != "200":
                raise RuntimeError(str(payload.get("msg") or payload.get("message") or f"BoCha API error: {code}"))
            values = ((payload.get("data") or {}).get("webPages") or {}).get("value")
            if values is None:
                values = (payload.get("webPages") or {}).get("value")
            if not isinstance(values, list):
                raise RuntimeError("BoCha API malformed payload: webPages.value is not an array")
            return [
                {
                    "title": str(item.get("name") or ""),
                    "url": str(item.get("url") or ""),
                    "snippet": str(item.get("summary") or item.get("snippet") or ""),
                    "source": "bocha",
                }
                for item in values
            ]
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError("BoCha API request failed")


def _querit_search(query: str, *, settings: Any, max_results: int, freshness: str) -> list[dict[str, Any]]:
    api_key = str(getattr(settings, "web_querit_api_key", "") or "")
    if not api_key:
        raise RuntimeError("Querit provider selected but WEB_QUERIT_API_KEY is not configured")
    body: dict[str, Any] = {"query": query, "count": max_results}
    if freshness:
        body["filters"] = {"timeRange": {"date": freshness}}
    response = httpx.post(
        "https://api.querit.ai/v1/search",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=body,
        timeout=max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10)),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error_code") not in (None, 200):
        raise RuntimeError(str(payload.get("error") or f"Querit API error: {payload.get('error_code')}"))
    values = ((payload.get("results") or {}).get("result") or [])
    return [
        {
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
            "snippet": str(item.get("snippet") or ""),
            "source": "querit",
        }
        for item in values
    ]


def _rrf_merge(results: list[ProviderResult]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for result in results:
        if result.status != "ok":
            continue
        for rank, item in enumerate(result.items, start=1):
            url = canonicalize_url(str(item.get("url") or ""))
            if not url:
                continue
            row = rows.setdefault(
                url,
                {
                    **item,
                    "url": url,
                    "providers": [],
                    "queries": [],
                    "search_score": 0.0,
                },
            )
            row["search_score"] += 1.0 / (60.0 + rank)
            provider = str(item.get("provider") or result.provider)
            query = str(item.get("query") or "")
            if provider and provider not in row["providers"]:
                row["providers"].append(provider)
            if query and query not in row["queries"]:
                row["queries"].append(query)
            if len(str(item.get("snippet") or "")) > len(str(row.get("snippet") or "")):
                row["snippet"] = item.get("snippet") or ""
            if not row.get("title") and item.get("title"):
                row["title"] = item["title"]
    return sorted(rows.values(), key=lambda item: (-float(item.get("search_score") or 0.0), str(item.get("url") or "")))


def _aggregate_provider_status(results: list[ProviderResult], provider_names: tuple[str, ...]) -> list[dict[str, Any]]:
    rows = []
    for provider in provider_names:
        matches = [result for result in results if result.provider == provider]
        count = sum(len(result.items) for result in matches if result.status == "ok")
        errors = [result.error for result in matches if result.error]
        rows.append(
            {
                "provider": provider,
                "status": "ok" if count else ("error" if errors else "empty"),
                "result_count": count,
                **({"error": "; ".join(dict.fromkeys(errors))[:500]} if errors else {}),
            }
        )
    return rows


def _normalize_item(item: Any, *, provider: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {}
    url = str(item.get("href") or item.get("url") or "").strip()
    if provider == "ddgs":
        url = _unwrap_ddg_url(url)
    return {
        "title": str(item.get("title") or "").strip()[:300],
        "url": url,
        "snippet": str(item.get("body") or item.get("snippet") or item.get("content") or "").strip()[:2000],
        "source": provider,
    }


def canonicalize_url(url: str) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname.lower().rstrip(".")
    port = f":{parsed.port}" if parsed.port and parsed.port not in {80, 443} else ""
    path = parsed.path or "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme.lower()}://{host}{port}{path}{query}"


def _unwrap_ddg_url(url: str) -> str:
    parsed = urlparse(url)
    if "duckduckgo.com" not in (parsed.hostname or ""):
        return url
    target = parse_qs(parsed.query).get("uddg")
    return unquote(target[0]) if target else url


def _domain_query(query: str, domains: tuple[str, ...]) -> str:
    if not domains:
        return query
    return f"({' OR '.join(f'site:{domain}' for domain in domains[:6])}) {query}"


def _url_matches_domains(url: str, domains: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return bool(host) and any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _strip_html(value: str) -> str:
    clean = re.sub(r"<[^>]+>", " ", value)
    return " ".join(html.unescape(clean).split())


def _is_garbage_item(item: dict[str, Any]) -> bool:
    text = f"{item.get('title') or ''} {item.get('snippet') or ''}"
    return sum(1 for pattern in _GARBAGE_PATTERNS if pattern in text) >= 2


def _user_agent() -> str:
    return "Mozilla/5.0 (compatible; SATS-Web-RAG/1.0; +https://github.com/)"
