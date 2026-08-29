from __future__ import annotations

import html
import ipaddress
import json
import os
import re
import socket
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .numeric import bounded_float, bounded_int


def _is_private_target(url: str) -> bool:
    host = urlparse(url).hostname
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            return True
    return False


def _validate_public_url(url: str) -> str:
    normalized = str(url or "").strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must be an absolute http(s) URL")
    if os.getenv("AIASK_AGENT_ALLOW_PRIVATE_WEB", "").strip().lower() not in {"1", "true", "yes", "on"}:
        if _is_private_target(normalized):
            raise PermissionError("private, loopback, and link-local web targets are blocked")
    return normalized


def _fetch_url(url: str, *, max_bytes: int = 262144, timeout: float = 15.0) -> tuple[str, str, int | None]:
    normalized = _validate_public_url(url)
    request = Request(
        normalized,
        headers={
            "User-Agent": "AIASK-Agent/0.1 (+native-web-tool)",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
        },
    )
    with urlopen(request, timeout=bounded_float(timeout, default=15.0, minimum=1.0, maximum=60.0)) as response:
        raw = response.read(bounded_int(max_bytes, default=262144, minimum=1, maximum=1024 * 1024))
        content_type = str(response.headers.get("Content-Type") or "")
        status = getattr(response, "status", None)
    return raw.decode("utf-8", errors="replace"), content_type, status


def _fetch_binary_url(url: str, *, max_bytes: int = 25 * 1024 * 1024, timeout: float = 60.0) -> tuple[bytes, str, int | None]:
    normalized = _validate_public_url(url)
    limit = bounded_int(max_bytes, default=25 * 1024 * 1024, minimum=1, maximum=100 * 1024 * 1024)
    request = Request(normalized, headers={"User-Agent": "AIASK-Agent/0.1 (+native-provider-tool)", "Accept": "*/*"})
    with urlopen(request, timeout=bounded_float(timeout, default=60.0, minimum=1.0, maximum=300.0)) as response:
        raw = response.read(limit + 1)
        content_type = str(response.headers.get("Content-Type") or "application/octet-stream").split(";", 1)[0].strip()
        status = getattr(response, "status", None)
    if len(raw) > limit:
        raise ValueError(f"remote file exceeds max_bytes={limit}")
    return raw, content_type, status


def _json_request(method: str, url: str, payload: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None, timeout: float = 30.0) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json", **dict(headers or {})})
    try:
        with urlopen(request, timeout=bounded_float(timeout, default=30.0, minimum=1.0, maximum=300.0)) as response:
            raw = response.read(1024 * 1024)
            text = raw.decode("utf-8", errors="replace")
            parsed = json.loads(text) if text else {}
            return {"ok": 200 <= getattr(response, "status", 200) < 300, "status_code": getattr(response, "status", None), "body": parsed}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


def _response_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if direct:
        return str(direct)
    parts: list[str] = []
    for item in list(getattr(response, "output", []) or []):
        for content in list(getattr(item, "content", []) or []):
            text = getattr(content, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(parts).strip()


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._skip_depth += 1
        if tag.lower() in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"} and self._skip_depth:
            self._skip_depth -= 1
        if tag.lower() in {"p", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        text = html.unescape(" ".join(self.parts))
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        text = re.sub(r"\n\s+", "\n", text)
        return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_text(content: str, content_type: str = "") -> str:
    if "html" not in content_type.lower() and "<html" not in content[:1000].lower():
        return content.strip()
    parser = _TextExtractor()
    parser.feed(content)
    return parser.text()


def _extract_links(content: str, base_url: str, limit: int = 10) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    for href, label in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', content, flags=re.I | re.S):
        clean_label = re.sub(r"<[^>]+>", " ", label)
        clean_label = re.sub(r"\s+", " ", html.unescape(clean_label)).strip()
        clean_href = html.unescape(href)
        if clean_href.startswith("//"):
            clean_href = f"{urlparse(base_url).scheme}:{clean_href}"
        elif clean_href.startswith("/"):
            parsed = urlparse(base_url)
            clean_href = f"{parsed.scheme}://{parsed.netloc}{clean_href}"
        if clean_href.startswith("http") and clean_label:
            links.append({"title": clean_label[:240], "url": clean_href})
        if len(links) >= limit:
            break
    return links


