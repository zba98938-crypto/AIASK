from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Callable

from ..numeric import bounded_float, bounded_int
from ._runtime_paths import configured_sqlite_path, default_aiask_sqlite_path

DEFAULT_SQLITE_PATH = default_aiask_sqlite_path()
SQLITE_ENV_KEYS = ("AKSHARE_MCP_SQLITE_PATH", "AIASK_SQLITE_PATH", "SQLITE_BUSY_TIMEOUT_MS", "SQLITE_JOURNAL_MODE")


def _missing_factory_dependency(exc: BaseException) -> dict[str, Any]:
    missing_name = getattr(exc, "name", None)
    dependency = "strategy_factory" if missing_name == "strategy_factory" else "akshare_mcp"
    error_code = "MISSING_STRATEGY_FACTORY" if dependency == "strategy_factory" else "MISSING_AKSHARE_MCP"
    return {
        "success": False,
        "data": {
            "configured": False,
            "dependency": dependency,
            "detail": str(exc),
        },
        "error": str(exc),
        "error_code": error_code,
    }


def _looks_like_database_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "database",
        "sqlite",
        "database is locked",
        "unable to open database file",
        "sqlalchemy",
        "connection refused",
        "could not connect",
        "connection timed out",
        "timeout expired",
    )
    return any(marker in text for marker in markers)


def _database_config_status() -> tuple[bool, list[str]]:
    configured = [key for key in SQLITE_ENV_KEYS if str(os.getenv(key) or "").strip()]
    return True, configured or ["default"]


def _sqlite_status() -> dict[str, Any]:
    path, path_sources = configured_sqlite_path()
    configured, sources = _database_config_status()
    if path_sources == ["AIASK_RUNTIME_HOME"]:
        sources = path_sources
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / ".aiask_sqlite_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        writable = True
    except Exception:
        writable = False
    return {
        "database_backend": "sqlite",
        "database_path": str(path),
        "database_configured": configured,
        "database_writable": writable,
        "database_config_sources": sources,
    }


def _looks_like_database_recovery(exc: BaseException) -> bool:
    text = str(exc).lower()
    markers = (
        "database is locked",
        "sqlite_busy",
        "busy",
        "database system is in recovery mode",
        "the database system is in recovery mode",
        "recovery mode",
        "database is starting up",
        "database system is starting up",
    )
    return any(marker in text for marker in markers)


def _unavailable_factory(exc: BaseException) -> dict[str, Any]:
    database = _sqlite_status()
    database_configured = bool(database.get("database_configured"))
    if _looks_like_database_recovery(exc):
        error_code = "STRATEGY_FACTORY_DATABASE_RECOVERY"
    elif isinstance(exc, TimeoutError | asyncio.TimeoutError):
        error_code = "STRATEGY_FACTORY_TIMEOUT"
    elif database_configured and _looks_like_database_error(exc):
        error_code = "STRATEGY_FACTORY_DATABASE_UNAVAILABLE"
    else:
        error_code = "STRATEGY_FACTORY_UNAVAILABLE"
    return {
        "success": False,
        "data": {
            "configured": False,
            "dependency": "strategy_factory",
            **database,
            "detail": str(exc),
        },
        "error": str(exc),
        "error_code": error_code,
    }


def _ensure_monorepo_paths() -> None:
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent / "aiask-quant-core" / "src",
            parent / "akshare-mcp" / "src",
            parent / "strategy-factory" / "src",
        ]
        added = False
        for candidate in candidates:
            if not candidate.exists():
                continue
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            added = True
        if added:
            return


async def _call_db_facade(
    handler_loader: Callable[[], Callable[[Any, dict[str, Any]], Any]],
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        _ensure_monorepo_paths()
        from akshare_mcp.storage import get_db
    except ModuleNotFoundError as exc:
        return _missing_factory_dependency(exc)

    try:
        call_params = dict(params or {})
        timeout = bounded_float(
            call_params.pop("_timeout_seconds", os.getenv("AIASK_STRATEGY_FACTORY_TOOL_TIMEOUT", "15")),
            default=15.0,
            minimum=1.0,
            maximum=3600.0,
        )
        db = get_db()
        await db.initialize()
        handler = handler_loader()
        return await asyncio.wait_for(handler(db, call_params), timeout=timeout)
    except TimeoutError as exc:
        return _unavailable_factory(exc)
    except asyncio.TimeoutError as exc:
        return _unavailable_factory(exc)
    except ModuleNotFoundError as exc:
        return _missing_factory_dependency(exc)
    except Exception as exc:
        return _unavailable_factory(exc)


def _load_factory_status_handler():
    from akshare_mcp.tools.managers.strategy_mgr_lifecycle import handle_factory_status

    return handle_factory_status


def _load_factory_runs_handler():
    from akshare_mcp.tools.managers.strategy_mgr_lifecycle import handle_factory_runs

    return handle_factory_runs


def _load_promotion_reviews_handler():
    from akshare_mcp.tools.managers.strategy_mgr_runtime import handle_promotion_reviews

    return handle_promotion_reviews


def _load_domain_events_handler():
    from akshare_mcp.tools.managers.strategy_mgr_runtime import handle_domain_events

    return handle_domain_events


def _load_factory_dispatch_handler():
    from akshare_mcp.tools.managers.strategy_mgr_lifecycle import handle_factory_dispatch_run

    return handle_factory_dispatch_run


def _load_factory_event_handler(action: str):
    """Resolve a ``factory_event_*`` write handler from the strategy manager
    dispatch table.

    PR-F (Phase 4, 2026-05-24): instead of importing each handler by name we
    delegate to ``ACTION_HANDLERS`` so the agent stays in lock-step with the
    contract registry — if a new ``factory_event_*`` write action lands in
    the manager later, only the white-lists in ``tool_risk.py`` need to grow,
    the executor automatically discovers the handler.

    Closes Phase 4 §"execute_confirmed_action() 增加 strategy_manager event
    action 执行分支" while satisfying the §"执行路径复用 AKShare MCP manager
    handler，不新增 model-visible agent_strategy_manager" requirement.
    """

    from akshare_mcp.tools.managers.strategy_manager import ACTION_HANDLERS

    handler = ACTION_HANDLERS.get(action)
    if handler is None:
        raise LookupError(f"strategy_manager has no handler for action: {action}")
    return handler


def _safe_limit(value: Any, *, default: int = 100, maximum: int = 1000) -> int:
    return bounded_int(value, default=default, minimum=1, maximum=maximum)


def _string_or_none(value: Any) -> str | None:
    token = str(value or "").strip()
    return token or None


def _trade_prediction_meta(tool_name: str) -> dict[str, Any]:
    return {
        "tool": tool_name,
        "side_effect": {
            "level": "read_only",
            "confirmation_required": False,
            "idempotent": True,
        },
    }


def _trade_prediction_envelope(tool_name: str, data: dict[str, Any] | list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "success": True,
        "data": data,
        "error": None,
        "meta": _trade_prediction_meta(tool_name),
    }


def _load_trade_prediction_status_handler():
    async def handler(db, params: dict[str, Any]) -> dict[str, Any]:
        data = await db.summarize_strategy_trade_predictions(
            strategy_id=_string_or_none(params.get("strategy_id")),
            stock_code=_string_or_none(params.get("stock_code")),
            limit=_safe_limit(params.get("limit"), default=1000, maximum=5000),
        )
        payload = dict(data or {})
        payload.setdefault("status", "ready")
        payload.setdefault("configured", True)
        return _trade_prediction_envelope("agent_trade_prediction_status", payload)

    return handler


def _load_trade_prediction_outcomes_handler():
    async def handler(db, params: dict[str, Any]) -> dict[str, Any]:
        outcomes = await db.list_strategy_trade_prediction_outcomes(
            prediction_id=_string_or_none(params.get("prediction_id")),
            strategy_id=_string_or_none(params.get("strategy_id")),
            stock_code=_string_or_none(params.get("stock_code")),
            score_version=_string_or_none(params.get("score_version")),
            score_status=_string_or_none(params.get("score_status")),
            data_quality_status=_string_or_none(params.get("data_quality_status")),
            actual_trading_date_lte=_string_or_none(params.get("actual_trading_date_lte")),
            actual_trading_date_gte=_string_or_none(params.get("actual_trading_date_gte")),
            limit=_safe_limit(params.get("limit"), default=100, maximum=1000),
        )
        data = {
            "object": "trade_prediction.outcomes",
            "status": "ready",
            "configured": True,
            "items": [dict(item or {}) for item in list(outcomes or [])],
            "count": len(list(outcomes or [])),
        }
        return _trade_prediction_envelope("agent_trade_prediction_outcomes", data)

    return handler


def _load_trade_prediction_matrix_handler():
    async def handler(db, params: dict[str, Any]) -> dict[str, Any]:
        raw_dimensions = params.get("dimensions")
        if isinstance(raw_dimensions, str):
            dimensions = [item.strip() for item in raw_dimensions.split(",") if item.strip()]
        elif isinstance(raw_dimensions, (list, tuple)):
            dimensions = [str(item or "").strip() for item in raw_dimensions if str(item or "").strip()]
        else:
            dimensions = ["family", "stage", "regime", "event", "factor"]
        data = await db.aggregate_trade_prediction_matrix(
            strategy_id=_string_or_none(params.get("strategy_id")),
            stock_code=_string_or_none(params.get("stock_code")),
            score_version=_string_or_none(params.get("score_version")),
            dimensions=dimensions,
            limit=_safe_limit(params.get("limit"), default=1000, maximum=5000),
        )
        payload = dict(data or {})
        payload.setdefault("status", "ready")
        payload.setdefault("configured", True)
        return _trade_prediction_envelope("agent_trade_prediction_matrix", payload)

    return handler


async def factory_status(arguments: dict[str, Any]) -> dict[str, Any]:
    if str(arguments.get("factory") or "").strip():
        from .factory_control import status as unified_factory_status

        result = await unified_factory_status(arguments)
        result.setdefault("meta", {})["tool"] = "agent_factory_status"
        return result
    result = await _call_db_facade(_load_factory_status_handler, arguments)
    return _read_only_fallback("factory_status", result)


async def factory_runs(arguments: dict[str, Any]) -> dict[str, Any]:
    if str(arguments.get("run_id") or "").strip():
        from .factory_control import run_detail as unified_run_detail

        result = await unified_run_detail(arguments)
        result.setdefault("meta", {})["tool"] = "agent_factory_runs"
        return result
    if str(arguments.get("factory") or arguments.get("status") or "").strip():
        from .factory_control import runs as unified_factory_runs

        result = await unified_factory_runs(arguments)
        result.setdefault("meta", {})["tool"] = "agent_factory_runs"
        return result
    result = await _call_db_facade(_load_factory_runs_handler, arguments)
    return _read_only_fallback("factory_runs", result)


async def strategy_review_snapshot(arguments: dict[str, Any]) -> dict[str, Any]:
    params = dict(arguments or {})
    if params.get("strategy_id") and not params.get("id"):
        params["id"] = params["strategy_id"]
    return await _call_db_facade(_load_promotion_reviews_handler, params)


async def strategy_domain_events(arguments: dict[str, Any]) -> dict[str, Any]:
    params = dict(arguments or {})
    params["limit"] = _safe_limit(params.get("limit"), default=50, maximum=200)
    result = await _call_db_facade(_load_domain_events_handler, params)
    return _read_only_fallback("domain_events", result)


async def _factory_event_read(action: str, arguments: dict[str, Any]) -> dict[str, Any]:
    params = dict(arguments or {})
    result = await _call_db_facade(lambda action=action: _load_factory_event_handler(action), params)
    return _read_only_fallback(action, result)


async def factory_event_list(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _factory_event_read("factory_event_list", arguments)


async def factory_event_preview_tasks(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _factory_event_read("factory_event_preview_tasks", arguments)


async def factory_event_lineage(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _factory_event_read("factory_event_lineage", arguments)


async def factory_theme_exposure_status(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _factory_event_read("factory_theme_exposure_status", arguments)


async def factory_event_outbox_status(arguments: dict[str, Any]) -> dict[str, Any]:
    return await _factory_event_read("factory_event_outbox_status", arguments)


async def incubation_factory_status(arguments: dict[str, Any]) -> dict[str, Any]:
    """Read-only adapter to the incubation factory runner status."""
    try:
        _ensure_monorepo_paths()
        from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
        from strategy_factory.runtime.incubation import build_incubation_runtime
    except ModuleNotFoundError as exc:
        return _missing_factory_dependency(exc)

    try:
        ensure_default_runtime_services()
        runtime = build_incubation_runtime()
        snapshot = runtime.status()
    except Exception as exc:
        return _unavailable_factory(exc)

    return {
        "success": True,
        "data": dict(snapshot or {}),
        "error": None,
        "meta": {
            "tool": "agent_incubation_factory_status",
            "side_effect": {
                "level": "read_only",
                "confirmation_required": False,
                "idempotent": True,
            },
        },
    }


async def trade_prediction_status(arguments: dict[str, Any]) -> dict[str, Any]:
    result = await _call_db_facade(_load_trade_prediction_status_handler, arguments)
    return _read_only_fallback("trade_prediction_status", result)


async def trade_prediction_outcomes(arguments: dict[str, Any]) -> dict[str, Any]:
    result = await _call_db_facade(_load_trade_prediction_outcomes_handler, arguments)
    return _read_only_fallback("trade_prediction_outcomes", result)


async def trade_prediction_matrix(arguments: dict[str, Any]) -> dict[str, Any]:
    result = await _call_db_facade(_load_trade_prediction_matrix_handler, arguments)
    return _read_only_fallback("trade_prediction_matrix", result)


async def execute_confirmed_action(action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    normalized = str(action or "").strip()
    if normalized in {"factory_run_once", "factory_dispatch_run"}:
        return await _call_db_facade(_load_factory_dispatch_handler, dict(params or {}))
    # PR-F (Phase 4, 2026-05-24): route confirmed event-driven write actions
    # through the existing AKShare MCP manager handlers so Desktop / TUI
    # writes share the same dual-person review, self-approval guard and
    # outbox/lineage persistence as direct manager calls.
    #
    # ``factory_event_list`` and ``factory_event_preview_tasks`` are
    # read-only and live in ``tool_risk.READ_ONLY_STRATEGY_ACTIONS`` —
    # they never reach this executor (no ActionIntent is created), so we
    # only handle the four write actions here.
    if normalized in {
        "factory_event_create",
        "factory_event_update",
        "factory_event_approve",
        "factory_event_record_outcome",
        "factory_event_bootstrap",
        "factory_theme_exposure_refresh",
        "factory_event_outbox_drain",
        "factory_theme_regression_run",
    }:
        return await _call_db_facade(
            lambda action=normalized: _load_factory_event_handler(action),
            dict(params or {}),
        )
    return {
        "success": False,
        "data": {
            "action": normalized,
            "execution_owner": "external_service",
            "detail": "Agent confirmed Strategy Factory actions no longer execute in-process.",
        },
        "error": f"Strategy Factory action must be handled by the owning service: {normalized}",
        "error_code": "STRATEGY_FACTORY_EXTERNAL_RUNNER_REQUIRED",
    }


def _read_only_fallback(action: str, result: dict[str, Any]) -> dict[str, Any]:
    if result.get("success") is not False:
        return result
    error_code = str(result.get("error_code") or "")
    if error_code not in {
        "STRATEGY_FACTORY_TIMEOUT",
        "STRATEGY_FACTORY_DATABASE_RECOVERY",
        "STRATEGY_FACTORY_DATABASE_UNAVAILABLE",
        "STRATEGY_FACTORY_UNAVAILABLE",
    }:
        return result
    data = dict(result.get("data") or {})
    database = _sqlite_status()
    database_ready = bool(database.get("database_configured") and database.get("database_writable"))
    data.update(
        {
            "configured": database_ready,
            "status": "partial" if database_ready else "unconfigured",
            "fallback": True,
            "action": action,
            **database,
        }
    )
    if action == "factory_runs":
        data.setdefault("runs", [])
    return {**result, "data": data}
