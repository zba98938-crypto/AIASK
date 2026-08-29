from __future__ import annotations

import inspect
from pathlib import Path
import sys
from typing import Any


def _ensure_monorepo_package_path(package_name: str) -> None:
    if package_name != "strategy_factory":
        return
    current = Path(__file__).resolve()
    candidate = current.parents[3] / "strategy-factory" / "src"
    if candidate.exists():
        candidate_str = str(candidate)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)


def _load_mcp_and_helpers():
    try:
        from akshare_mcp.server import mcp
    except ModuleNotFoundError as exc:
        if exc.name != "strategy_factory":
            raise
        _ensure_monorepo_package_path(exc.name)
        from akshare_mcp.server import mcp
    from akshare_mcp.tools.search import _infer_tool_category, _iter_registered_tools

    return mcp, _iter_registered_tools, _infer_tool_category


def _safe_source_path(obj: Any) -> str | None:
    try:
        path = inspect.getsourcefile(obj) or inspect.getfile(obj)
    except Exception:
        return None
    return str(Path(path).resolve()) if path else None


def _safe_signature(obj: Any) -> str | None:
    try:
        return str(inspect.signature(obj))
    except Exception:
        return None


def _collect_parameter_metadata(fn: Any) -> list[dict[str, Any]]:
    try:
        signature = inspect.signature(fn)
    except Exception:
        return []

    items: list[dict[str, Any]] = []
    for name, param in signature.parameters.items():
        annotation = None
        if param.annotation is not inspect.Signature.empty:
            annotation = getattr(param.annotation, "__name__", None) or str(param.annotation)
        has_default = param.default is not inspect.Signature.empty
        default_repr = None if not has_default else repr(param.default)
        items.append(
            {
                "name": name,
                "kind": str(param.kind).replace("Parameter.", "").lower(),
                "required": not has_default,
                "annotation": annotation,
                "default": default_repr,
            }
        )
    return items


def build_tool_registry(mcp=None) -> list[dict[str, Any]]:
    if mcp is None:
        mcp, _iter_registered_tools, _infer_tool_category = _load_mcp_and_helpers()
    else:
        from akshare_mcp.tools.search import _infer_tool_category, _iter_registered_tools

    rows: list[dict[str, Any]] = []
    for name, tool in sorted(_iter_registered_tools(mcp), key=lambda item: str(item[0] or "")):
        if not name:
            continue

        fn = getattr(tool, "fn", None)
        unwrapped = inspect.unwrap(fn) if fn else None
        doc = inspect.getdoc(unwrapped or fn or tool) or ""
        doc_lines = [line.strip() for line in doc.splitlines() if line.strip()]
        wrapper_path = _safe_source_path(fn)
        implementation_path = _safe_source_path(unwrapped or fn)
        wrapper_line = None
        implementation_line = None
        try:
            if fn:
                wrapper_line = inspect.getsourcelines(fn)[1]
        except Exception:
            wrapper_line = None
        try:
            if unwrapped or fn:
                implementation_line = inspect.getsourcelines(unwrapped or fn)[1]
        except Exception:
            implementation_line = None

        rows.append(
            {
                "name": str(name),
                "category": _infer_tool_category(str(name), tool),
                "description": getattr(tool, "description", None),
                "doc_first_line": doc_lines[0] if doc_lines else None,
                "module": str(getattr(unwrapped or fn, "__module__", "") or ""),
                "wrapper_module": str(getattr(fn, "__module__", "") or ""),
                "wrapper_path": wrapper_path,
                "wrapper_line": wrapper_line,
                "implementation_path": implementation_path,
                "implementation_line": implementation_line,
                "signature": _safe_signature(unwrapped or fn),
                "is_async": bool(inspect.iscoroutinefunction(unwrapped or fn)),
                "has_docstring": bool(doc_lines),
                "unwrap_applied": bool(fn and unwrapped and fn is not unwrapped),
                "parameters": _collect_parameter_metadata(unwrapped or fn),
            }
        )
    return rows


def summarize_tool_registry(rows: list[dict[str, Any]]) -> dict[str, Any]:
    category_counts: dict[str, int] = {}
    decorated_tools = 0
    missing_impl = 0
    missing_doc = 0
    for row in rows:
        category = str(row.get("category") or "general")
        category_counts[category] = category_counts.get(category, 0) + 1
        if row.get("unwrap_applied"):
            decorated_tools += 1
        if not row.get("implementation_path"):
            missing_impl += 1
        if not row.get("has_docstring"):
            missing_doc += 1

    return {
        "tool_count": len(rows),
        "category_counts": dict(sorted(category_counts.items())),
        "decorated_tool_count": decorated_tools,
        "missing_implementation_path_count": missing_impl,
        "missing_docstring_count": missing_doc,
    }
