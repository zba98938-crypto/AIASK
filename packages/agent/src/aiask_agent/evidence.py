from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .paths import aiask_agent_home
from .session_store import AgentSessionStore, now_iso, sanitize_for_audit
from .tools.policy import build_policy_from_env


URL_KEYS = ("url", "link", "source_url", "href", "web_url")
TITLE_KEYS = ("title", "headline", "name", "subject")
PUBLISHED_KEYS = ("published_at", "publish_time", "published_time", "datetime", "date", "time")
EXCERPT_KEYS = ("excerpt", "summary", "description", "content", "text")
PATH_KEYS = ("path", "file_path", "output_path", "report_path", "artifact_path", "saved_path")
QUOTE_KEYS = ("price", "last", "latest", "close", "current", "change", "change_pct", "volume", "amount")
NEWS_TOOL_HINTS = ("news", "web_search", "web_extract")
FILE_TOOL_HINTS = ("file_write", "file_patch", "file_mutation", "quant_research")
TERMINAL_TOOL_HINTS = ("terminal", "process", "execute_python")


def extract_tool_evidence(
    store: AgentSessionStore,
    *,
    user_id: str | None,
    session_id: str,
    run_id: str,
    trace_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    result: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Persist first-class source and artifact records derived from one tool result."""
    context = {
        "user_id": user_id,
        "session_id": session_id,
        "run_id": run_id,
        "trace_id": trace_id,
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
    }
    safe_args = sanitize_for_audit(dict(arguments or {}))
    data = result.get("data") if isinstance(result, dict) else None
    meta = result.get("meta") if isinstance(result, dict) else None
    payload = data if isinstance(data, dict) else {}
    created_sources: list[dict[str, Any]] = []
    created_artifacts: list[dict[str, Any]] = []

    source_chain = _source_chain(result)
    if source_chain or _looks_like_quote(tool_name, payload):
        source = _record_provider_source(store, context=context, tool_name=tool_name, payload=payload, meta=meta, source_chain=source_chain)
        if source is not None:
            created_sources.append(source)

    for item in _iter_dicts(data):
        if _has_url(item):
            source = _record_url_source(store, context=context, tool_name=tool_name, item=item, source_chain=source_chain)
            if source is not None:
                created_sources.append(source)

    if _looks_like_quote(tool_name, payload):
        artifact = store.record_artifact(
            {
                **context,
                "artifact_id": _stable_id("art", run_id, tool_call_id, tool_name, "quote", _canonical_symbol(payload)),
                "kind": "quote_snapshot",
                "title": _quote_title(payload, tool_name),
                "preview_json": sanitize_for_audit(payload, max_depth=4, max_items=40, max_text=1000),
                "metadata": {
                    "arguments": safe_args,
                    "source_chain": source_chain,
                    "fallback_reason": payload.get("fallback_reason") or _nested_get(result, ("meta", "fallback_reason")),
                    "data_timestamp": payload.get("data_timestamp") or payload.get("time") or payload.get("trade_time"),
                },
            }
        )
        created_artifacts.append(artifact)

    if _looks_like_news(tool_name, data):
        news_preview = _news_preview(data)
        if news_preview:
            artifact = store.record_artifact(
                {
                    **context,
                    "artifact_id": _stable_id("art", run_id, tool_call_id, tool_name, "news"),
                    "kind": "news_digest",
                    "title": f"{tool_name} news digest",
                    "preview_json": {"items": sanitize_for_audit(news_preview, max_depth=3, max_items=20, max_text=1000)},
                    "metadata": {"arguments": safe_args, "source_chain": source_chain},
                }
            )
            created_artifacts.append(artifact)

    for path_value in _candidate_paths(tool_name=tool_name, arguments=arguments or {}, data=data):
        artifact = _record_file_artifact(store, context=context, path_value=path_value, tool_name=tool_name, arguments=safe_args)
        if artifact is not None:
            created_artifacts.append(artifact)

    terminal_artifact = _record_terminal_artifact(store, context=context, tool_name=tool_name, arguments=safe_args, result=result)
    if terminal_artifact is not None:
        created_artifacts.append(terminal_artifact)

    code_artifact = _record_python_snippet_artifact(store, context=context, tool_name=tool_name, arguments=arguments or {})
    if code_artifact is not None:
        created_artifacts.append(code_artifact)

    return {"sources": _dedupe_records(created_sources, "source_id"), "artifacts": _dedupe_records(created_artifacts, "artifact_id")}


def _record_provider_source(
    store: AgentSessionStore,
    *,
    context: dict[str, Any],
    tool_name: str,
    payload: dict[str, Any],
    meta: Any,
    source_chain: list[str],
) -> dict[str, Any] | None:
    provider = _first_text(
        payload.get("provider"),
        payload.get("source"),
        _nested_get(payload, ("meta", "provider_used")),
        _nested_get(meta, ("provider_used",)),
        source_chain[-1] if source_chain else None,
    )
    if not provider and not source_chain:
        return None
    data_timestamp = _first_text(payload.get("data_timestamp"), payload.get("time"), payload.get("trade_time"), _nested_get(meta, ("data_timestamp",)))
    return store.record_source(
        {
            **context,
            "source_id": _stable_id("src", context["run_id"], context["tool_call_id"], tool_name, "provider", provider or "source_chain"),
            "provider": provider,
            "source_type": "market_quote" if _looks_like_quote(tool_name, payload) else "data_provider",
            "title": f"{provider or tool_name} data source",
            "fetched_at": now_iso(),
            "data_timestamp": data_timestamp,
            "metadata": {
                "source_chain": source_chain,
                "attempted_sources": payload.get("attempted_sources"),
                "fallback_reason": payload.get("fallback_reason") or _nested_get(meta, ("fallback_reason",)),
            },
        }
    )


def _record_url_source(
    store: AgentSessionStore,
    *,
    context: dict[str, Any],
    tool_name: str,
    item: dict[str, Any],
    source_chain: list[str],
) -> dict[str, Any] | None:
    url = _first_url(item)
    if not url:
        return None
    provider = _first_text(item.get("provider"), item.get("source"), item.get("site"), _domain(url))
    title = _first_text(*[item.get(key) for key in TITLE_KEYS]) or url
    excerpt = _first_text(*[item.get(key) for key in EXCERPT_KEYS])
    published_at = _first_text(*[item.get(key) for key in PUBLISHED_KEYS])
    source_type = "news" if _looks_like_news(tool_name, item) else "web_search"
    return store.record_source(
        {
            **context,
            "source_id": _stable_id("src", context["run_id"], context["tool_call_id"], tool_name, url),
            "provider": provider,
            "source_type": source_type,
            "title": title,
            "url": url,
            "published_at": published_at,
            "fetched_at": now_iso(),
            "excerpt": excerpt,
            "metadata": {"source_chain": source_chain, "raw": sanitize_for_audit(item, max_depth=3, max_items=30, max_text=1000)},
        }
    )


def _record_file_artifact(
    store: AgentSessionStore,
    *,
    context: dict[str, Any],
    path_value: Any,
    tool_name: str,
    arguments: Any,
) -> dict[str, Any] | None:
    path_text = str(path_value or "").strip()
    if not path_text:
        return None
    path = Path(path_text).expanduser()
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    exists = resolved.exists()
    is_allowed = _path_allowed_for_artifact_read(resolved)
    is_file = exists and resolved.is_file()
    stat = resolved.stat() if is_file and is_allowed else None
    preview = None
    if is_file and is_allowed:
        preview = _read_preview(resolved)
    return store.record_artifact(
        {
            **context,
            "artifact_id": _stable_id("art", context["run_id"], context["tool_call_id"], tool_name, str(resolved)),
            "kind": _artifact_kind_for_path(tool_name, resolved),
            "title": resolved.name or str(resolved),
            "path": str(resolved),
            "mime_type": mimetypes.guess_type(str(resolved))[0],
            "size_bytes": stat.st_size if stat else None,
            "sha256": _sha256_file(resolved) if is_file and is_allowed else None,
            "preview_text": preview,
            "status": "ready" if exists and is_allowed else "blocked" if exists else "missing",
            "metadata": {"arguments": arguments, "exists": exists, "is_file": is_file, "read_allowed": is_allowed},
        }
    )


def _record_terminal_artifact(
    store: AgentSessionStore,
    *,
    context: dict[str, Any],
    tool_name: str,
    arguments: Any,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    if not any(hint in tool_name for hint in TERMINAL_TOOL_HINTS):
        return None
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, dict):
        return None
    stdout = _first_text(data.get("stdout"), data.get("output"), data.get("text"))
    stderr = _first_text(data.get("stderr"), data.get("error_output"))
    command = _first_text(data.get("command"), _nested_get(arguments, ("command",))) if isinstance(arguments, dict) else _first_text(data.get("command"))
    if not stdout and not stderr and not command:
        return None
    preview_json = {
        "command": command,
        "exit_code": data.get("exit_code") or data.get("returncode") or data.get("status"),
        "stdout": _trim(stdout, 3000),
        "stderr": _trim(stderr, 2000),
    }
    return store.record_artifact(
        {
            **context,
            "artifact_id": _stable_id("art", context["run_id"], context["tool_call_id"], tool_name, "terminal"),
            "kind": "terminal_output",
            "title": f"{tool_name} output",
            "preview_text": _trim("\n".join(part for part in [stdout, stderr] if part), 4000),
            "preview_json": preview_json,
            "metadata": {"arguments": arguments, "success": result.get("success")},
        }
    )


def _record_python_snippet_artifact(
    store: AgentSessionStore,
    *,
    context: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    if tool_name != "agent_execute_python":
        return None
    code = str(arguments.get("code") or "")
    if not code:
        return None
    artifact_dir = aiask_agent_home() / "artifacts" / str(context["session_id"]) / str(context["run_id"])
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / f"{context['tool_call_id']}_snippet.py"
    if not path.exists() or path.read_text(encoding="utf-8", errors="ignore") != code:
        path.write_text(code, encoding="utf-8")
    return store.record_artifact(
        {
            **context,
            "artifact_id": _stable_id("art", context["run_id"], context["tool_call_id"], tool_name, "python_snippet"),
            "kind": "script",
            "title": path.name,
            "path": str(path),
            "mime_type": "text/x-python",
            "size_bytes": path.stat().st_size,
            "sha256": _sha256_file(path),
            "preview_text": _trim(code, 4000),
            "metadata": {"language": "python", "persisted_from": "agent_execute_python"},
        }
    )


def _source_chain(result: dict[str, Any]) -> list[str]:
    chain: list[Any] = []
    if isinstance(result.get("meta"), dict):
        chain.extend(list(result["meta"].get("source_chain") or []))
    data = result.get("data")
    if isinstance(data, dict):
        chain.extend(list(data.get("source_chain") or []))
        chain.extend(list(data.get("attempted_sources") or []))
    return [str(item) for item in dict.fromkeys(chain) if str(item).strip()]


def _iter_dicts(value: Any, *, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 4:
        return []
    if isinstance(value, dict):
        items = [value]
        for child in value.values():
            items.extend(_iter_dicts(child, depth=depth + 1))
        return items
    if isinstance(value, list):
        items: list[dict[str, Any]] = []
        for child in value[:100]:
            items.extend(_iter_dicts(child, depth=depth + 1))
        return items
    return []


def _candidate_paths(*, tool_name: str, arguments: dict[str, Any], data: Any) -> list[str]:
    values: list[str] = []
    if any(hint in tool_name for hint in FILE_TOOL_HINTS):
        for key in PATH_KEYS:
            if arguments.get(key):
                values.append(str(arguments.get(key)))
    for item in _iter_dicts(data):
        for key in PATH_KEYS:
            if item.get(key):
                values.append(str(item.get(key)))
        mutation = item.get("mutation_verification")
        if isinstance(mutation, dict) and mutation.get("path"):
            values.append(str(mutation.get("path")))
    return [item for item in dict.fromkeys(values) if item.strip()]


def _looks_like_quote(tool_name: str, payload: Any) -> bool:
    if "quote" in tool_name or "stock_live" in tool_name or "market_snapshot" in tool_name:
        return True
    if not isinstance(payload, dict):
        return False
    lowered_keys = {str(key).lower() for key in payload}
    return bool(lowered_keys.intersection(QUOTE_KEYS)) and bool({"code", "symbol", "name", "stock_code"}.intersection(lowered_keys))


def _looks_like_news(tool_name: str, value: Any) -> bool:
    if any(hint in tool_name for hint in NEWS_TOOL_HINTS):
        return True
    if isinstance(value, dict):
        lowered = {str(key).lower() for key in value}
        if "news" in lowered or "articles" in lowered:
            return True
        if _has_url(value) and bool({"title", "headline", "published_at", "publish_time"}.intersection(lowered)):
            return True
    return False


def _news_preview(data: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in _iter_dicts(data):
        if _has_url(item) or _looks_like_news("", item):
            rows.append(
                {
                    "title": _first_text(*[item.get(key) for key in TITLE_KEYS]),
                    "url": _first_url(item),
                    "provider": _first_text(item.get("provider"), item.get("source"), item.get("site")),
                    "published_at": _first_text(*[item.get(key) for key in PUBLISHED_KEYS]),
                    "excerpt": _first_text(*[item.get(key) for key in EXCERPT_KEYS]),
                }
            )
    return [row for row in rows[:30] if any(row.values())]


def _has_url(item: dict[str, Any]) -> bool:
    return bool(_first_url(item))


def _first_url(item: dict[str, Any]) -> str | None:
    for key in URL_KEYS:
        value = item.get(key)
        if isinstance(value, str) and _is_url(value):
            return value.strip()
    for value in item.values():
        if isinstance(value, str) and _is_url(value):
            return value.strip()
    return None


def _is_url(value: str) -> bool:
    parsed = urlparse(str(value).strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _domain(url: str) -> str | None:
    parsed = urlparse(url)
    return parsed.netloc or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _nested_get(value: Any, keys: tuple[str, ...]) -> Any:
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _quote_title(payload: dict[str, Any], tool_name: str) -> str:
    symbol = _canonical_symbol(payload)
    name = _first_text(payload.get("name"), payload.get("stock_name"))
    return " ".join(part for part in [symbol, name, "quote snapshot"] if part) or f"{tool_name} quote snapshot"


def _canonical_symbol(payload: dict[str, Any]) -> str:
    return _first_text(payload.get("code"), payload.get("symbol"), payload.get("stock_code"), payload.get("secid")) or "unknown"


def _artifact_kind_for_path(tool_name: str, path: Path) -> str:
    suffix = path.suffix.lower()
    if "patch" in tool_name:
        return "patch"
    if suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".sql", ".sh", ".ps1"}:
        return "code"
    if suffix in {".csv", ".xlsx", ".xls", ".json", ".parquet"}:
        return "table"
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
        return "chart"
    if suffix in {".log", ".txt"}:
        return "terminal_output" if "terminal" in tool_name else "file"
    return "file"


def _path_allowed_for_artifact_read(path: Path) -> bool:
    roots: list[Path] = []
    try:
        roots.append(aiask_agent_home().expanduser().resolve())
    except Exception:
        pass
    try:
        roots.extend(Path(root).expanduser().resolve() for root in build_policy_from_env().workspace_roots)
    except Exception:
        pass
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_preview(path: Path, *, limit: int = 4000) -> str | None:
    try:
        raw = path.read_bytes()[:limit]
    except Exception:
        return None
    if b"\x00" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def _trim(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<truncated {len(text) - limit} chars>"


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256("::".join(str(part or "") for part in parts).encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"{prefix}_{digest or uuid4().hex[:24]}"


def _dedupe_records(records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for record in records:
        value = str(record.get(key) or "")
        if not value or value in seen:
            continue
        seen.add(value)
        items.append(record)
    return items
