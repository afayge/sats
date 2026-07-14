from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


ENDPOINT = "https://api.anysearch.com/mcp"
AVAILABLE_DOMAINS = (
    "general",
    "resource",
    "social_media",
    "finance",
    "academic",
    "legal",
    "health",
    "business",
    "security",
    "ip",
    "code",
    "energy",
    "environment",
    "agriculture",
    "travel",
    "film",
    "gaming",
)


class AnySearchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AnySearchResponse:
    text: str
    items: tuple[dict[str, Any], ...] = ()
    meta: dict[str, Any] | None = None


def search(
    query: str,
    *,
    settings: Any,
    max_results: int,
    domain: str = "",
    sub_domain: str = "",
    sub_domain_params: dict[str, Any] | None = None,
) -> AnySearchResponse:
    arguments: dict[str, Any] = {"query": query, "max_results": max(1, min(10, int(max_results)))}
    if domain:
        arguments["domain"] = domain
        arguments["sub_domain"] = sub_domain
        if sub_domain_params is not None:
            arguments["sub_domain_params"] = sub_domain_params
    text, meta = _call_tool("search", arguments, settings=settings)
    return AnySearchResponse(text=text, items=tuple(parse_search_results(text)), meta=meta)


def batch_search(queries: list[dict[str, Any]], *, settings: Any) -> list[AnySearchResponse]:
    if not 1 <= len(queries) <= 5:
        raise ValueError("AnySearch batch_search accepts 1 to 5 queries")
    arguments = {"queries": [_normalize_query_item(item) for item in queries]}
    text, meta = _call_tool("batch_search", arguments, settings=settings)
    sections = _parse_batch_sections(text)
    responses = []
    for index, item in enumerate(queries, start=1):
        section = sections.get(index, "")
        if not section and len(queries) == 1:
            section = text
        responses.append(
            AnySearchResponse(
                text=section,
                items=tuple(parse_search_results(section)),
                meta=meta,
            )
        )
    return responses


def get_sub_domains(domains: list[str] | tuple[str, ...], *, settings: Any) -> dict[str, Any]:
    cleaned = list(dict.fromkeys(str(item or "").strip().lower() for item in domains if str(item or "").strip()))
    if not 1 <= len(cleaned) <= 5:
        raise ValueError("get_sub_domains accepts 1 to 5 domains")
    unknown = [item for item in cleaned if item not in AVAILABLE_DOMAINS]
    if unknown:
        raise ValueError(f"unsupported AnySearch domain: {', '.join(unknown)}")
    arguments = {"domain": cleaned[0]} if len(cleaned) == 1 else {"domains": cleaned}
    text, meta = _call_tool("get_sub_domains", arguments, settings=settings)
    return {"raw_markdown": text, "items": parse_sub_domains(text), "meta": meta or {}}


def extract(url: str, *, settings: Any) -> dict[str, Any]:
    text, meta = _call_tool("extract", {"url": url}, settings=settings)
    title_match = re.search(r"^##\s+(.+?)\s*$", text, flags=re.M)
    title = title_match.group(1).strip() if title_match else url
    content = re.sub(r"^##\s+.+?\s*$", "", text, count=1, flags=re.M).strip()
    content = re.sub(r"^\*\*Source\*\*:\s*\S+\s*$", "", content, count=1, flags=re.M).strip()
    content = re.sub(r"^---\s*$", "", content, count=1, flags=re.M).strip()
    if not content:
        raise AnySearchError("AnySearch extract returned empty content")
    return {"title": title, "content": content[:50000], "raw_markdown": text, "meta": meta or {}}


def parse_sub_domain_params(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return {str(key).strip(): item for key, item in value.items() if str(key).strip()}
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return {str(key).strip(): item for key, item in payload.items() if str(key).strip()}
    if text.startswith("{") and text.endswith("}"):
        text = text[1:-1]
        separator = ":"
    else:
        separator = "="
    result: dict[str, Any] = {}
    for pair in text.split(","):
        if separator not in pair:
            continue
        key, item = pair.split(separator, 1)
        key = key.strip().strip("'\"")
        if key:
            result[key] = item.strip().strip("'\"")
    if not result:
        raise ValueError("sub_domain_params must be a JSON object or comma-separated key=value pairs")
    return result


def parse_search_results(text: str) -> list[dict[str, Any]]:
    value = str(text or "").strip()
    if not value or re.search(r"Search Results\s*\(0 results", value, flags=re.I):
        return []
    matches = list(re.finditer(r"^###\s+(\d+)\.\s+(.+?)\s*$", value, flags=re.M))
    rows = []
    for index, match in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        block = value[match.end() : block_end].strip()
        url_match = re.search(r"^-\s*\*\*URL\*\*:\s*(https?://\S+)\s*$", block, flags=re.M | re.I)
        if not url_match:
            continue
        url = url_match.group(1).rstrip(".,，。；;")
        snippet = (block[: url_match.start()] + block[url_match.end() :]).strip()
        snippet = re.sub(r"^[-*]\s+", "", snippet).strip()
        rows.append(
            {
                "title": match.group(2).strip(),
                "url": url,
                "snippet": snippet[:5000],
                "source": "anysearch",
            }
        )
    return rows


def parse_sub_domains(text: str) -> list[dict[str, Any]]:
    value = str(text or "")
    matches = list(re.finditer(r"^###\s+([a-z][a-z0-9_]*\.[a-z0-9_.-]+)\s*$", value, flags=re.M | re.I))
    rows = []
    for index, match in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        block = value[match.end() : block_end].strip()
        description = block.split("\n", 1)[0].strip() if block else ""
        params = []
        for param in re.finditer(r"^-\s+`([^`]+)`\s*(\(required\))?\s*:\s*(.+?)\s*$", block, flags=re.M | re.I):
            params.append(
                {
                    "name": param.group(1).strip(),
                    "required": bool(param.group(2)),
                    "description": param.group(3).strip(),
                }
            )
        rows.append(
            {
                "domain": match.group(1).split(".", 1)[0].lower(),
                "sub_domain": match.group(1).lower(),
                "description": description,
                "params": params,
            }
        )
    return rows


def _call_tool(tool_name: str, arguments: dict[str, Any], *, settings: Any) -> tuple[str, dict[str, Any]]:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }
    headers = {"Content-Type": "application/json", "X-Anysearch-Client": "sats/0.1"}
    api_key = str(getattr(settings, "anysearch_api_key", "") or "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10))
    try:
        response = httpx.post(ENDPOINT, json=payload, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    except httpx.TimeoutException as exc:
        raise TimeoutError(f"AnySearch request timed out after {timeout}s") from exc
    except httpx.HTTPStatusError as exc:
        raise AnySearchError(f"AnySearch HTTP {exc.response.status_code}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise AnySearchError(f"AnySearch request failed: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise AnySearchError("AnySearch returned a malformed response")
    if isinstance(data.get("error"), dict):
        raise AnySearchError(str(data["error"].get("message") or "AnySearch API error")[:500])
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    content = result.get("content") if isinstance(result.get("content"), list) else []
    text = "\n".join(str(item.get("text") or "") for item in content if isinstance(item, dict) and item.get("type") == "text").strip()
    if result.get("isError"):
        raise AnySearchError(text[:500] or "AnySearch API error")
    if not text:
        raise AnySearchError("AnySearch returned no content")
    meta = result.get("_meta") if isinstance(result.get("_meta"), dict) else {}
    return text, meta


def _normalize_query_item(item: dict[str, Any]) -> dict[str, Any]:
    query = " ".join(str(item.get("query") or "").split())
    if not query:
        raise ValueError("batch query is required")
    result: dict[str, Any] = {"query": query}
    domain = str(item.get("domain") or "").strip().lower()
    sub_domain = str(item.get("sub_domain") or "").strip().lower()
    if domain:
        result["domain"] = domain
        result["sub_domain"] = sub_domain
        params = parse_sub_domain_params(item.get("sub_domain_params"))
        if params:
            result["sub_domain_params"] = params
    limit = item.get("max_results", item.get("limit"))
    if limit is not None:
        result["max_results"] = max(1, min(10, int(limit)))
    return result


def _parse_batch_sections(text: str) -> dict[int, str]:
    value = str(text or "")
    matches = list(re.finditer(r"^##\s+Query\s+(\d+)\s*:\s*.*$", value, flags=re.M | re.I))
    sections = {}
    for index, match in enumerate(matches):
        block_end = matches[index + 1].start() if index + 1 < len(matches) else len(value)
        sections[int(match.group(1))] = value[match.end() : block_end].strip()
    return sections
