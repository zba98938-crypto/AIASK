from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote_plus, urlencode, urlparse

from .native_utils import _limit
from .native_web_utils import _extract_links, _extract_text, _fetch_url, _json_request
from .numeric import bounded_float, bounded_int
from .stock_data_sources import search_source_by_provider


def _active_json_request() -> Callable[..., dict[str, Any]]:
    try:
        from . import native_capabilities

        return getattr(native_capabilities, "_json_request", _json_request)
    except Exception:
        return _json_request


def _active_fetch_url() -> Callable[..., tuple[str, str, int | None]]:
    try:
        from . import native_capabilities

        return getattr(native_capabilities, "_fetch_url", _fetch_url)
    except Exception:
        return _fetch_url


def build_web_handlers(_envelope: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    async def web_extract(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_web_extract"
        try:
            fetch_url = _active_fetch_url()
            content, content_type, status = fetch_url(
                str(arguments.get("url") or ""),
                max_bytes=bounded_int(arguments.get("max_bytes"), default=262144, minimum=1, maximum=1024 * 1024),
                timeout=bounded_float(arguments.get("timeout_seconds"), default=15.0, minimum=1.0, maximum=60.0),
            )
            text = _extract_text(content, content_type)
            limited, truncated = _limit(text, bounded_int(arguments.get("max_chars"), default=20000, minimum=1, maximum=200000))
            return _envelope(
                True,
                data={
                    "url": str(arguments.get("url") or ""),
                    "status": status,
                    "content_type": content_type,
                    "text": limited,
                    "truncated": truncated,
                    "links": _extract_links(content, str(arguments.get("url") or ""), limit=20),
                },
                tool_name=tool,
                level="read_only",
                target=str(arguments.get("url") or ""),
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def web_search(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_web_search"
        query = str(arguments.get("query") or "").strip()
        try:
            if not query:
                raise ValueError("query is required")
            limit = bounded_int(arguments.get("limit"), default=5, minimum=1, maximum=20)
            configured_source = search_source_by_provider(str(arguments.get("source_id") or arguments.get("provider") or "").strip())
            provider = str((configured_source or {}).get("provider") or "duckduckgo")
            base_url = str((configured_source or {}).get("base_url") or "").strip().rstrip("/")
            api_key = str((configured_source or {}).get("api_key") or (configured_source or {}).get("token") or "").strip()
            timeout = bounded_float((configured_source or {}).get("timeout_seconds"), default=20.0, minimum=1.0, maximum=60.0)
            results: list[dict[str, Any]] = []
            status: int | None = None
            provider_payload: dict[str, Any] = {"source_id": (configured_source or {}).get("id"), "source": (configured_source or {}).get("source")}
            json_request = _active_json_request()

            if provider == "tavily" and api_key:
                payload = {
                    "query": query,
                    "max_results": limit,
                    "search_depth": str(arguments.get("search_depth") or (configured_source or {}).get("search_depth") or "basic"),
                    "include_answer": bool(arguments.get("include_answer") or (configured_source or {}).get("include_answer") or False),
                }
                response = await asyncio.to_thread(
                    json_request,
                    "POST",
                    f"{base_url or 'https://api.tavily.com'}/search",
                    payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=timeout,
                )
                status = response.get("status_code")
                body = dict(response.get("body") or {}) if isinstance(response.get("body"), dict) else {}
                for item in list(body.get("results") or [])[:limit]:
                    if not isinstance(item, dict):
                        continue
                    results.append({"title": str(item.get("title") or item.get("url") or ""), "url": str(item.get("url") or ""), "snippet": str(item.get("content") or item.get("snippet") or "")})
                provider_payload["answer"] = body.get("answer")
            elif provider == "brave_search" and api_key:
                params = urlencode({"q": query, "count": str(limit)})
                response = await asyncio.to_thread(
                    json_request,
                    "GET",
                    f"{base_url or 'https://api.search.brave.com/res/v1'}/web/search?{params}",
                    None,
                    headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                    timeout=timeout,
                )
                status = response.get("status_code")
                body = dict(response.get("body") or {}) if isinstance(response.get("body"), dict) else {}
                web_results = dict(body.get("web") or {}).get("results") if isinstance(body.get("web"), dict) else body.get("results")
                for item in list(web_results or [])[:limit]:
                    if not isinstance(item, dict):
                        continue
                    results.append({"title": str(item.get("title") or item.get("url") or ""), "url": str(item.get("url") or ""), "snippet": str(item.get("description") or item.get("snippet") or "")})
            elif provider == "serpapi" and api_key:
                params = urlencode({"engine": str((configured_source or {}).get("engine") or "google"), "q": query, "api_key": api_key, "num": str(limit)})
                response = await asyncio.to_thread(
                    json_request,
                    "GET",
                    f"{base_url or 'https://serpapi.com/search.json'}?{params}",
                    None,
                    timeout=timeout,
                )
                status = response.get("status_code")
                body = dict(response.get("body") or {}) if isinstance(response.get("body"), dict) else {}
                for item in list(body.get("organic_results") or body.get("news_results") or [])[:limit]:
                    if not isinstance(item, dict):
                        continue
                    results.append({"title": str(item.get("title") or item.get("link") or ""), "url": str(item.get("link") or item.get("url") or ""), "snippet": str(item.get("snippet") or item.get("source") or "")})
            elif provider == "exa" and api_key:
                payload = {"query": query, "numResults": limit, "type": str(arguments.get("search_type") or (configured_source or {}).get("search_type") or "auto")}
                response = await asyncio.to_thread(
                    json_request,
                    "POST",
                    f"{base_url or 'https://api.exa.ai'}/search",
                    payload,
                    headers={"x-api-key": api_key},
                    timeout=timeout,
                )
                status = response.get("status_code")
                body = dict(response.get("body") or {}) if isinstance(response.get("body"), dict) else {}
                for item in list(body.get("results") or [])[:limit]:
                    if not isinstance(item, dict):
                        continue
                    results.append({"title": str(item.get("title") or item.get("url") or ""), "url": str(item.get("url") or ""), "snippet": str(item.get("text") or item.get("summary") or "")})
            else:
                provider = "duckduckgo"
                url = f"{base_url or 'https://duckduckgo.com/html/'}?q={quote_plus(query)}"
                content, content_type, status = _active_fetch_url()(url, max_bytes=524288, timeout=timeout)
                provider_payload["content_type"] = content_type
                links = _extract_links(content, url, limit=limit * 3)
                seen: set[str] = set()
                for item in links:
                    href = item["url"]
                    if "duckduckgo.com" in urlparse(href).netloc and "uddg=" in href:
                        match = re.search(r"uddg=([^&]+)", href)
                        if match:
                            from urllib.parse import unquote

                            href = unquote(match.group(1))
                    if href in seen or not href.startswith("http"):
                        continue
                    seen.add(href)
                    results.append({"title": item["title"], "url": href})
                    if len(results) >= limit:
                        break
            return _envelope(
                True,
                data={"query": query, "results": results, "status": status, "provider": provider, **provider_payload},
                tool_name=tool,
                level="read_only",
                target=query,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def x_search(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_x_search"
        query = str(arguments.get("query") or "").strip()
        token = str(os.getenv("X_BEARER_TOKEN") or os.getenv("X_API_KEY") or "").strip()
        base_url = str(os.getenv("AIASK_X_API_BASE") or "https://api.twitter.com/2").rstrip("/")
        if not query:
            return _envelope(False, error="query is required", tool_name=tool, level="read_only")
        if not token:
            return _envelope(
                False,
                data={
                    "configured": False,
                    "provider": "x_api_v2",
                    "required_env": ["X_BEARER_TOKEN", "X_API_KEY"],
                    "query": query,
                    "results": [],
                    "secrets_redacted": True,
                },
                error="X search credentials are not configured",
                tool_name=tool,
                level="read_only",
                target=query,
            )
        try:
            max_results = bounded_int(arguments.get("max_results") or arguments.get("limit"), default=10, minimum=10, maximum=100)
            params = {"query": query, "max_results": str(max_results), "tweet.fields": "created_at,author_id,public_metrics"}
            if arguments.get("since_id"):
                params["since_id"] = str(arguments.get("since_id"))
            if arguments.get("next_token"):
                params["next_token"] = str(arguments.get("next_token"))
            query_string = "&".join(f"{quote_plus(key)}={quote_plus(value)}" for key, value in params.items())
            result = await asyncio.to_thread(
                _active_json_request(),
                "GET",
                f"{base_url}/tweets/search/recent?{query_string}",
                None,
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            body = result.get("body") if isinstance(result.get("body"), dict) else {}
            tweets = list(dict(body).get("data") or []) if isinstance(body, dict) else []
            meta = dict(dict(body).get("meta") or {}) if isinstance(body, dict) else {}
            data = {
                "configured": True,
                "provider": "x_api_v2",
                "query": query,
                "results": tweets[: bounded_int(arguments.get("limit"), default=len(tweets) or 10, minimum=1, maximum=100)],
                "next_token": meta.get("next_token"),
                "result_count": meta.get("result_count", len(tweets)),
                "response": {key: value for key, value in result.items() if key != "body"},
                "secrets_redacted": True,
            }
            return _envelope(bool(result.get("ok")), data=data, error=None if result.get("ok") else str(result.get("error") or "X API request failed"), tool_name=tool, level="read_only", target=query)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only", target=query)

    return {
        "agent_web_search": web_search,
        "agent_web_extract": web_extract,
        "agent_x_search": x_search,
    }
