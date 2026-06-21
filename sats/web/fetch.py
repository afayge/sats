from __future__ import annotations

import io
import ipaddress
import re
import socket
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 4
ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/pdf",
)


def fetch_page(
    url: str,
    *,
    settings: Any,
    trusted_domains: tuple[str, ...] = (),
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    current = str(url or "").strip()
    timeout = max(1, int(getattr(settings, "web_search_timeout_seconds", 10) or 10))
    for redirect_count in range(MAX_REDIRECTS + 1):
        validate_public_url(current, trusted_domains=trusted_domains)
        status, headers, content = _http_get_once(current, timeout_seconds=timeout, max_bytes=max_bytes)
        if status in {301, 302, 303, 307, 308}:
            location = str(headers.get("location") or "").strip()
            if not location:
                raise RuntimeError(f"redirect response missing Location header: {current}")
            if redirect_count >= MAX_REDIRECTS:
                raise RuntimeError(f"too many redirects while fetching {url}")
            current = urljoin(current, location)
            continue
        if status < 200 or status >= 300:
            raise RuntimeError(f"HTTP {status} while fetching {current}")
        content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if content_type and not any(content_type == allowed for allowed in ALLOWED_CONTENT_TYPES):
            raise RuntimeError(f"unsupported web content type: {content_type}")
        extracted = extract_content(content, content_type=content_type, url=current)
        return {
            "url": current,
            "title": extracted["title"],
            "content": extracted["content"],
            "content_type": content_type or extracted["content_type"],
            "extraction_method": extracted["extraction_method"],
            "published_at": extracted.get("published_at"),
            "redirect_count": redirect_count,
        }
    raise RuntimeError(f"unable to fetch {url}")


def validate_public_url(url: str, *, trusted_domains: tuple[str, ...] = ()) -> None:
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError as exc:
        raise ValueError(f"invalid URL: {url}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("URL hostname is required")
    if trusted_domains and not any(host == domain or host.endswith(f".{domain}") for domain in trusted_domains):
        raise ValueError(f"URL is outside trusted domains: {host}")
    addresses = _resolve_addresses(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    if not addresses:
        raise ValueError(f"URL hostname could not be resolved: {host}")
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if _is_non_public_ip(ip):
            raise ValueError(f"URL resolves to a non-public address: {address}")


def extract_content(content: bytes, *, content_type: str, url: str) -> dict[str, Any]:
    if content_type == "application/pdf" or content.startswith(b"%PDF"):
        return _extract_pdf(content)
    text = _decode_text(content)
    if content_type in {"text/html", "application/xhtml+xml"} or "<html" in text[:1000].lower():
        return _extract_html(text, url=url)
    return {
        "title": urlparse(url).hostname or url,
        "content": text.strip(),
        "content_type": content_type or "text/plain",
        "extraction_method": "plain_text",
    }


def _http_get_once(url: str, *, timeout_seconds: int, max_bytes: int) -> tuple[int, dict[str, str], bytes]:
    headers = {
        "Accept": "text/html,application/xhtml+xml,text/plain,application/pdf;q=0.9,*/*;q=0.1",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "User-Agent": "Mozilla/5.0 (compatible; SATS-Web-RAG/1.0)",
    }
    with httpx.Client(follow_redirects=False, timeout=timeout_seconds) as client:
        with client.stream("GET", url, headers=headers) as response:
            length = _safe_int(response.headers.get("content-length"))
            if length is not None and length > max_bytes:
                raise RuntimeError(f"web response exceeds {max_bytes} bytes")
            chunks = []
            total = 0
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError(f"web response exceeds {max_bytes} bytes")
                chunks.append(chunk)
            return response.status_code, {key.lower(): value for key, value in response.headers.items()}, b"".join(chunks)


def _extract_html(text: str, *, url: str) -> dict[str, Any]:
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, flags=re.I | re.S)
    fallback_title = _clean_html_text(title_match.group(1)) if title_match else (urlparse(url).hostname or url)
    try:
        import trafilatura

        content = trafilatura.extract(
            text,
            url=url,
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("trafilatura is required for HTML extraction") from exc
    if not content:
        content = _clean_html_text(re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S))
    if not content.strip():
        raise RuntimeError("web page contains no readable text")
    published = ""
    published_match = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:article:published_time|date|pubdate)["\'][^>]+content=["\']([^"\']+)',
        text,
        flags=re.I,
    )
    if published_match:
        published = published_match.group(1).strip()
    return {
        "title": fallback_title,
        "content": content.strip(),
        "content_type": "text/html",
        "extraction_method": "trafilatura",
        "published_at": published or None,
    }


def _extract_pdf(content: bytes) -> dict[str, Any]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("pypdf is required for PDF extraction") from exc
    reader = PdfReader(io.BytesIO(content))
    pages = [(page.extract_text() or "").strip() for page in reader.pages]
    text = "\n\n".join(page for page in pages if page)
    if not text:
        raise RuntimeError("PDF contains no extractable text")
    title = str((reader.metadata or {}).get("/Title") or "PDF document").strip()
    return {
        "title": title,
        "content": text,
        "content_type": "application/pdf",
        "extraction_method": "pypdf",
    }


def _resolve_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        ipaddress.ip_address(host)
        return (host,)
    except ValueError:
        pass
    try:
        rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return ()
    return tuple(dict.fromkeys(str(row[4][0]) for row in rows if row[4]))


def _is_non_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8", "gb18030", "big5", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _clean_html_text(value: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", value).replace("&nbsp;", " ").split())


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
