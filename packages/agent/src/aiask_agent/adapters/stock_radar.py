from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable

from ..numeric import bounded_float
from ._runtime_paths import configured_sqlite_path, default_aiask_sqlite_path


DEFAULT_SQLITE_PATH = default_aiask_sqlite_path()


def _ensure_monorepo_paths() -> None:
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent / "aiask-quant-core" / "src",
            parent / "akshare-mcp" / "src",
        ]
        added = False
        for candidate in candidates:
            if candidate.exists() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
                added = True
        if added:
            return


def _sqlite_status() -> dict[str, Any]:
    path, path_sources = configured_sqlite_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".aiask_stock_radar_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        writable = True
    except Exception:
        writable = False
    return {
        "database_backend": "sqlite",
        "database_path": str(path),
        "database_configured": True,
        "database_writable": writable,
        "database_config_sources": path_sources,
    }


def _missing_dependency(exc: BaseException) -> dict[str, Any]:
    return {
        "success": False,
        "data": {"configured": False, "dependency": "akshare_mcp", "detail": str(exc), **_sqlite_status()},
        "error": str(exc),
        "error_code": "MISSING_STOCK_RADAR_BACKEND",
    }


def _unavailable(exc: BaseException) -> dict[str, Any]:
    return {
        "success": False,
        "data": {"configured": False, "dependency": "stock_radar", "detail": str(exc), **_sqlite_status()},
        "error": str(exc),
        "error_code": "STOCK_RADAR_UNAVAILABLE",
    }


async def _call_db_handler(
    loader: Callable[[], Callable[[Any, dict[str, Any]], Any]],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        _ensure_monorepo_paths()
        from akshare_mcp.storage import get_db
    except ModuleNotFoundError as exc:
        return _missing_dependency(exc)

    try:
        payload = dict(params or {})
        timeout = bounded_float(
            payload.pop("_timeout_seconds", os.getenv("AIASK_STOCK_RADAR_TOOL_TIMEOUT", "30")),
            default=30.0,
            minimum=1.0,
            maximum=3600.0,
        )
        db = get_db()
        await db.initialize()
        handler = loader()
        result = await asyncio.wait_for(handler(db, payload), timeout=timeout)
        return result if isinstance(result, dict) else {"success": True, "data": result, "error": None}
    except TimeoutError as exc:
        return _unavailable(exc)
    except asyncio.TimeoutError as exc:
        return _unavailable(exc)
    except ModuleNotFoundError as exc:
        return _missing_dependency(exc)
    except Exception as exc:
        return _unavailable(exc)


def _load_status_handler():
    from akshare_mcp.services.stock_radar import stock_radar_status

    return stock_radar_status


def _load_candidates_handler():
    from akshare_mcp.services.stock_radar import stock_radar_candidates

    return stock_radar_candidates


def _load_digest_handler():
    from akshare_mcp.services.stock_radar import stock_radar_digest

    return stock_radar_digest


async def status(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _call_db_handler(_load_status_handler, arguments)


async def candidates(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _call_db_handler(_load_candidates_handler, arguments)


async def digest(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _call_db_handler(_load_digest_handler, arguments)
