from __future__ import annotations

import json
import html as html_lib
import re
from http.cookiejar import CookieJar
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from sats.config import Settings, load_settings
from sats.web.cache import cache_dir, read_cache, write_cache


SUPPORTED_PLATFORMS: tuple[tuple[str, str], ...] = (
    ("weibo", "微博热搜"),
    ("zhihu", "知乎热榜"),
    ("baidu", "百度热搜"),
    ("douyin", "抖音热点"),
    ("toutiao", "头条热榜"),
    ("bilibili", "B站热搜"),
    ("xueqiu_stock", "雪球热股"),
    ("xueqiu_spot", "雪球热点"),
)
PLATFORM_NAMES = {key: name for key, name in SUPPORTED_PLATFORMS}
PLATFORM_ALIASES = {
    "b站": "bilibili",
    "bilibili": "bilibili",
    "微博": "weibo",
    "知乎": "zhihu",
    "百度": "baidu",
    "抖音": "douyin",
    "头条": "toutiao",
    "雪球热股": "xueqiu_stock",
    "雪球股票": "xueqiu_stock",
    "xueqiustock": "xueqiu_stock",
    "雪球热点": "xueqiu_spot",
    "xueqiuspot": "xueqiu_spot",
}
PLATFORM_EXPANSIONS = {
    "雪球": ("xueqiu_stock", "xueqiu_spot"),
    "xueqiu": ("xueqiu_stock", "xueqiu_spot"),
}

_UA_PC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_UA_MOBILE = "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1"


def social_hot(
    *,
    platforms: list[str] | tuple[str, ...] | str | None = None,
    limit: int = 20,
    settings: Settings | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    settings = settings or load_settings()
    selected = _platforms(platforms)
    max_items = _limit(limit)
    fetched_at = _now()
    results = []
    ok_count = 0
    for platform in selected:
        result = _platform_hot(platform, limit=max_items, settings=settings, use_cache=use_cache)
        if result.get("status") == "ok":
            ok_count += 1
        results.append(result)
    return {
        "status": "ok" if ok_count else "error",
        "platforms": results,
        "platforms_ok": ok_count,
        "platforms_checked": len(selected),
        "fetched_at": fetched_at,
        "error": "" if ok_count else "all social hot platforms failed",
    }


def hot_mentions(
    keyword: str,
    *,
    platforms: list[str] | tuple[str, ...] | str | None = None,
    limit: int = 50,
    extra_keywords: list[str] | tuple[str, ...] | None = None,
    settings: Settings | None = None,
    use_cache: bool = True,
) -> dict[str, Any]:
    clean_keyword = str(keyword or "").strip()
    keywords = _mention_keywords(clean_keyword, extra_keywords or ())
    hot = social_hot(platforms=platforms, limit=limit, settings=settings, use_cache=use_cache)
    mentions: dict[str, list[dict[str, Any]]] = {}
    by_platform_count: dict[str, int] = {}
    for platform_result in hot.get("platforms") or []:
        platform = str(platform_result.get("platform") or "")
        hits = []
        for item in platform_result.get("items") if isinstance(platform_result.get("items"), list) else []:
            title = str(item.get("title") or "")
            if any(token and token in title for token in keywords):
                hits.append(item)
        mentions[platform] = hits
        by_platform_count[platform] = len(hits)
    return {
        "status": hot.get("status", "error"),
        "keyword": clean_keyword,
        "keywords_used": keywords,
        "mentions": mentions,
        "total_hits": sum(by_platform_count.values()),
        "by_platform_count": by_platform_count,
        "platforms_ok": hot.get("platforms_ok", 0),
        "platforms_checked": hot.get("platforms_checked", 0),
        "fetched_at": hot.get("fetched_at") or _now(),
        "source": "social_hot",
        "error": hot.get("error", ""),
    }


def _platform_hot(platform: str, *, limit: int, settings: Settings, use_cache: bool) -> dict[str, Any]:
    platform = _platform_id(platform)
    fetched_at = _now()
    if platform not in _FETCHERS:
        return _platform_error(platform, "unsupported platform", fetched_at=fetched_at)
    ttl = max(0, int(getattr(settings, "social_hot_cache_ttl_seconds", 300) or 0))
    path = cache_dir(settings, "social_hot") / f"{platform}.json"
    if use_cache:
        cached = read_cache(path, ttl)
        if cached is not None:
            cached_items = list(cached.get("items") or [])
            if cached.get("cached_full_result") or len(cached_items) >= limit:
                cached["items"] = cached_items[:limit]
                return cached
    try:
        items = _FETCHERS[platform](settings)
    except Exception as exc:
        return _platform_error(platform, f"{type(exc).__name__}: {str(exc)[:160]}", fetched_at=fetched_at)
    if not items:
        return _platform_error(platform, "fetch failed or response could not be parsed", fetched_at=fetched_at)
    all_items = list(items)
    cache_payload = {
        "status": "ok",
        "platform": platform,
        "platform_cn": PLATFORM_NAMES.get(platform, platform),
        "items": all_items,
        "fetched_at": fetched_at,
        "from_cache": False,
        "cached_full_result": True,
        "available_items": len(all_items),
        "error": "",
    }
    write_cache(path, cache_payload)
    payload = dict(cache_payload)
    payload["items"] = all_items[:limit]
    return payload


def _http_json(url: str, *, settings: Settings, ua: str = _UA_PC, headers: dict[str, str] | None = None) -> dict[str, Any] | None:
    timeout = max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10))
    request_headers = {
        "User-Agent": ua,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    request_headers.update(headers or {})
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            return None
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8", errors="replace"))


def _http_json_with_seed(url: str, *, seed_url: str, settings: Settings, ua: str = _UA_PC) -> dict[str, Any] | None:
    timeout = max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10))
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    seed_request = Request(
        seed_url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    try:
        opener.open(seed_request, timeout=timeout).close()
    except Exception:
        pass
    request = Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": seed_url,
            "Origin": "https://xueqiu.com",
        },
    )
    with opener.open(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            return None
        raw = response.read()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8", errors="replace"))


def _http_text(url: str, *, settings: Settings, ua: str = _UA_PC) -> str | None:
    timeout = max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10))
    request = Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://xueqiu.com/",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        if getattr(response, "status", 200) != 200:
            return None
        raw = response.read()
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace")


def _fetch_weibo(settings: Settings) -> list[dict[str, Any]] | None:
    data = _http_json(
        "https://weibo.com/ajax/side/hotSearch",
        settings=settings,
        headers={"Referer": "https://s.weibo.com/top/summary"},
    )
    if not data:
        return None
    rows = (data.get("data") or {}).get("realtime") or []
    return [
        _item(index, str(row.get("word") or ""), platform="weibo", url=f"https://s.weibo.com/weibo?q={quote(str(row.get('word') or ''))}", hot_score=row.get("num"), extra=row.get("category"))
        for index, row in enumerate(rows[:50], start=1)
        if row.get("word")
    ]


def _fetch_zhihu(settings: Settings) -> list[dict[str, Any]] | None:
    data = _http_json("https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=50&desktop=true", settings=settings)
    if not data:
        return None
    items = []
    for index, row in enumerate((data.get("data") or [])[:50], start=1):
        target = row.get("target") or {}
        title = ((target.get("title_area") or {}).get("text") or target.get("title") or "").strip()
        if not title:
            continue
        items.append(_item(index, title, platform="zhihu", url=((target.get("link") or {}).get("url") or ""), extra=(target.get("metrics_area") or {}).get("text")))
    return items


def _fetch_baidu(settings: Settings) -> list[dict[str, Any]] | None:
    data = _http_json("https://top.baidu.com/api/board?platform=wise&tab=realtime", settings=settings, ua=_UA_MOBILE)
    if not data:
        return None
    rows = _baidu_rows(data)
    return [
        _item(
            index,
            str(row.get("word") or row.get("query") or ""),
            platform="baidu",
            url=str(row.get("url") or ""),
            hot_score=row.get("hotScore"),
            extra=row.get("newHotName") or row.get("labelTagName") or row.get("hotTag"),
        )
        for index, row in enumerate((rows or [])[:50], start=1)
        if row.get("word") or row.get("query")
    ]


def _baidu_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    cards = (data.get("data") or {}).get("cards") or []
    rows = []
    for card in cards if isinstance(cards, list) else []:
        if not isinstance(card, dict):
            continue
        content = card.get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("word") or item.get("query"):
                rows.append(item)
                continue
            nested = item.get("content")
            for child in nested if isinstance(nested, list) else []:
                if isinstance(child, dict) and (child.get("word") or child.get("query")):
                    rows.append(child)
    return rows


def _fetch_douyin(settings: Settings) -> list[dict[str, Any]] | None:
    data = _http_json("https://www.douyin.com/aweme/v1/web/hot/search/list/", settings=settings)
    if not data:
        return None
    rows = (data.get("data") or {}).get("word_list") or []
    return [
        _item(index, str(row.get("word") or ""), platform="douyin", url=f"https://www.douyin.com/search/{quote(str(row.get('word') or ''))}", hot_score=row.get("hot_value"))
        for index, row in enumerate(rows[:50], start=1)
        if row.get("word")
    ]


def _fetch_toutiao(settings: Settings) -> list[dict[str, Any]] | None:
    data = _http_json("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", settings=settings)
    if not data:
        return None
    items = []
    for index, row in enumerate((data.get("data") or [])[:50], start=1):
        title = str(row.get("Title") or row.get("title") or "").strip()
        if not title:
            continue
        cid = str(row.get("ClusterIdStr") or row.get("ClusterId") or "")
        items.append(_item(index, title, platform="toutiao", url=f"https://www.toutiao.com/trending/{cid}/" if cid else "", hot_score=row.get("HotValue"), extra=row.get("LabelDesc")))
    return items


def _fetch_bilibili(settings: Settings) -> list[dict[str, Any]] | None:
    data = _http_json("https://s.search.bilibili.com/main/hotword?limit=50", settings=settings)
    if not data:
        return None
    return [
        _item(index, str(row.get("keyword") or row.get("show_name") or ""), platform="bilibili", url=f"https://search.bilibili.com/all?keyword={quote(str(row.get('keyword') or row.get('show_name') or ''))}", hot_score=row.get("heat_score"))
        for index, row in enumerate((data.get("list") or [])[:50], start=1)
        if row.get("keyword") or row.get("show_name")
    ]


def _fetch_xueqiu_stock(settings: Settings) -> list[dict[str, Any]] | None:
    data = _http_json_with_seed(
        "https://stock.xueqiu.com/v5/stock/hot_stock/list.json?size=50&_type=10&type=10",
        seed_url="https://xueqiu.com/hot/stock",
        settings=settings,
    )
    if data:
        items = _xueqiu_stock_api_items(data)
        if items:
            return items
    text = _http_text("https://xueqiu.com/hot/stock", settings=settings)
    if not text:
        return None
    items = _xueqiu_stock_items(text)
    return items or None


def _fetch_xueqiu_spot(settings: Settings) -> list[dict[str, Any]] | None:
    text = _http_text("https://xueqiu.com/hot/spot", settings=settings)
    if not text:
        return None
    items = _xueqiu_spot_items(text)
    return items or None


_FETCHERS: dict[str, Callable[[Settings], list[dict[str, Any]] | None]] = {
    "weibo": _fetch_weibo,
    "zhihu": _fetch_zhihu,
    "baidu": _fetch_baidu,
    "douyin": _fetch_douyin,
    "toutiao": _fetch_toutiao,
    "bilibili": _fetch_bilibili,
    "xueqiu_stock": _fetch_xueqiu_stock,
    "xueqiu_spot": _fetch_xueqiu_spot,
}


def _item(index: int, title: str, *, platform: str, url: str = "", hot_score: Any = 0, extra: Any = "") -> dict[str, Any]:
    try:
        score = int(float(hot_score or 0))
    except (TypeError, ValueError):
        score = 0
    return {
        "rank": index,
        "title": title[:180],
        "url": url,
        "hot_score": score,
        "platform": platform,
        "platform_cn": PLATFORM_NAMES.get(platform, platform),
        "extra": str(extra or "")[:200],
    }


def _platforms(value: list[str] | tuple[str, ...] | str | None) -> list[str]:
    if value is None or value == "" or value == "all":
        return [key for key, _ in SUPPORTED_PLATFORMS]
    if isinstance(value, str):
        raw = [item.strip() for item in value.split(",")]
    else:
        raw = [str(item or "").strip() for item in value]
    selected = []
    for item in raw:
        for platform in _platform_ids(item):
            if platform and platform in PLATFORM_NAMES and platform not in selected:
                selected.append(platform)
    return selected or [key for key, _ in SUPPORTED_PLATFORMS]


def _platform_id(value: str) -> str:
    ids = _platform_ids(value)
    if ids:
        return ids[0]
    text = str(value or "").strip().lower().replace(" ", "")
    return text


def _platform_ids(value: str) -> list[str]:
    text = str(value or "").strip().lower().replace(" ", "")
    if not text:
        return []
    expanded = PLATFORM_EXPANSIONS.get(text)
    if expanded:
        return list(expanded)
    return [PLATFORM_ALIASES.get(text, text)]


def _limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 20
    return max(1, min(limit, 50))


def _mention_keywords(keyword: str, extra_keywords: list[str] | tuple[str, ...]) -> list[str]:
    candidates = [keyword]
    if len(keyword) >= 3:
        candidates.extend([keyword[:2], keyword[-2:]])
    candidates.extend(str(item or "").strip() for item in extra_keywords)
    seen = set()
    out = []
    for item in candidates:
        token = str(item or "").strip()
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _platform_error(platform: str, error: str, *, fetched_at: str) -> dict[str, Any]:
    return {
        "status": "error",
        "platform": platform,
        "platform_cn": PLATFORM_NAMES.get(platform, platform),
        "items": [],
        "fetched_at": fetched_at,
        "from_cache": False,
        "error": error,
    }


def _xueqiu_stock_items(text: str) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for row in _json_rows_from_html(text):
        symbol = _first_text(row, ("symbol", "stockCode", "stock_code", "code", "ticker", "ts_code"))
        name = _first_text(row, ("name", "stockName", "stock_name", "cn_name"))
        if not symbol or not name:
            continue
        key = (symbol, name)
        if key in seen:
            continue
        seen.add(key)
        title = f"{name} {symbol}"
        url = _first_text(row, ("url", "link", "href")) or f"https://xueqiu.com/S/{quote(symbol)}"
        rows.append(
            _item(
                len(rows) + 1,
                title,
                platform="xueqiu_stock",
                url=url,
                hot_score=_first_number(row, ("hot_score", "hotScore", "heat", "score", "value", "rank_score", "rankScore")),
                extra=_xueqiu_extra(row),
            )
        )
        if len(rows) >= 50:
            break
    return rows or _xueqiu_stock_text_items(text)


def _xueqiu_spot_items(text: str) -> list[dict[str, Any]]:
    rows = []
    seen = set()
    for row in _json_rows_from_html(text):
        title = _first_text(row, ("title", "topic", "spotName", "spot_name", "word", "keyword"))
        if not title or title in seen:
            continue
        seen.add(title)
        rows.append(
            _item(
                len(rows) + 1,
                title,
                platform="xueqiu_spot",
                url=_first_text(row, ("url", "link", "href")) or "https://xueqiu.com/hot/spot",
                hot_score=_first_number(row, ("hot_score", "hotScore", "heat", "score", "value", "rank_score", "rankScore")),
                extra=_xueqiu_extra(row),
            )
        )
        if len(rows) >= 50:
            break
    return rows or _xueqiu_spot_text_items(text)


def _xueqiu_stock_api_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    rows = []
    for row in items[:50]:
        if not isinstance(row, dict):
            continue
        symbol = _first_text(row, ("symbol", "code", "stockCode", "stock_code"))
        name = _first_text(row, ("name", "stockName", "stock_name"))
        if not symbol or not name:
            continue
        rows.append(
            _item(
                len(rows) + 1,
                f"{name} {symbol}",
                platform="xueqiu_stock",
                url=_first_text(row, ("url", "link", "href")) or f"https://xueqiu.com/S/{quote(symbol)}",
                hot_score=_first_number(row, ("value", "hot_score", "hotScore", "heat", "score", "followers")),
                extra=_xueqiu_extra(row),
            )
        )
    return rows


def _xueqiu_stock_text_items(text: str) -> list[dict[str, Any]]:
    plain = _visible_text(text)
    pattern = re.compile(r"(?:^|\s)(?P<rank>\d{1,3})\s+(?:\d{1,3}\s+)?(?P<name>.+?)\s+(?P<symbol>(?:SH|SZ|BJ)?\d{5,6}|0\d{4,5}|[A-Z][A-Z0-9.\-]{0,9})\s+(?P<heat>\d+(?:\.\d+)?\s*万?)热度")
    rows = []
    seen = set()
    for match in pattern.finditer(plain):
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        symbol = match.group("symbol").strip()
        if not name or (name, symbol) in seen:
            continue
        seen.add((name, symbol))
        tail = plain[match.end() : match.end() + 80]
        percent = re.search(r"[-+]?\d+(?:\.\d+)?\s*%", tail)
        rows.append(
            _item(
                len(rows) + 1,
                f"{name} {symbol}",
                platform="xueqiu_stock",
                url=f"https://xueqiu.com/S/{quote(symbol)}",
                hot_score=_heat_score(match.group("heat")),
                extra=f"percent={percent.group(0).replace(' ', '')}" if percent else "",
            )
        )
        if len(rows) >= 50:
            break
    return rows


def _xueqiu_spot_text_items(text: str) -> list[dict[str, Any]]:
    plain = _visible_text(text)
    pattern = re.compile(r"(?:^|\s)(?P<rank>\d{1,3})\s+#(?P<title>[^#]+)#\s+(?P<stock>.+?)\s+(?P<percent>[-+]?\d+(?:\.\d+)?)\s*%\s+热度值\s+(?P<heat>\d+(?:\.\d+)?\s*万?)")
    rows = []
    seen = set()
    for match in pattern.finditer(plain):
        title = match.group("title").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        stock = re.sub(r"\s+", " ", match.group("stock")).strip()
        rows.append(
            _item(
                len(rows) + 1,
                title,
                platform="xueqiu_spot",
                url="https://xueqiu.com/hot/spot",
                hot_score=_heat_score(match.group("heat")),
                extra=f"{stock} {match.group('percent')}%",
            )
        )
        if len(rows) >= 50:
            break
    return rows


def _json_rows_from_html(text: str) -> list[dict[str, Any]]:
    rows = []
    for payload in _embedded_json_payloads(text):
        rows.extend(row for row in _walk_json(payload) if isinstance(row, dict))
    return rows


def _embedded_json_payloads(text: str) -> list[Any]:
    clean = html_lib.unescape(str(text or ""))
    payloads = []
    stripped = clean.strip()
    if stripped.startswith(("{", "[")):
        parsed = _json_loads(stripped)
        if parsed is not None:
            payloads.append(parsed)
    for match in re.finditer(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", clean, flags=re.IGNORECASE | re.DOTALL):
        parsed = _json_loads(match.group(1).strip())
        if parsed is not None:
            payloads.append(parsed)
    for marker in ("window.__INITIAL_STATE__", "__INITIAL_STATE__", "window.__NUXT__"):
        start = clean.find(marker)
        if start < 0:
            continue
        blob = _balanced_json_blob(clean, start + len(marker))
        parsed = _json_loads(blob) if blob else None
        if parsed is not None:
            payloads.append(parsed)
    return payloads


def _balanced_json_blob(text: str, start: int) -> str:
    while start < len(text) and text[start] not in "{[":
        start += 1
    if start >= len(text):
        return ""
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return ""


def _json_loads(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return None


def _walk_json(value: Any):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _first_text(row: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _first_number(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return 0


def _heat_score(value: Any) -> int:
    text = str(value or "").strip().replace(" ", "")
    multiplier = 10000 if text.endswith("万") else 1
    text = text.removesuffix("万")
    try:
        return int(float(text) * multiplier)
    except (TypeError, ValueError):
        return 0


def _visible_text(text: str) -> str:
    clean = html_lib.unescape(str(text or ""))
    clean = re.sub(r"<script\b[^>]*>.*?</script>", " ", clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<style\b[^>]*>.*?</style>", " ", clean, flags=re.IGNORECASE | re.DOTALL)
    clean = re.sub(r"<[^>]+>", " ", clean)
    return re.sub(r"\s+", " ", clean).strip()


def _xueqiu_extra(row: dict[str, Any]) -> str:
    parts = []
    for key in ("percent", "chg", "change", "current", "value", "increment", "rank_change", "desc", "summary", "content"):
        value = row.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={value}")
    stocks = row.get("stocks") or row.get("stock_list") or row.get("related_stocks")
    if isinstance(stocks, list):
        names = []
        for item in stocks[:5]:
            if isinstance(item, dict):
                name = _first_text(item, ("name", "stockName", "stock_name", "title"))
                symbol = _first_text(item, ("symbol", "stockCode", "stock_code", "code"))
                label = " ".join(part for part in (name, symbol) if part)
                if label:
                    names.append(label)
        if names:
            parts.append("stocks=" + ",".join(names))
    return " | ".join(parts)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
