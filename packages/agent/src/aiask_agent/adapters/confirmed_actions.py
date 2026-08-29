from __future__ import annotations

import asyncio
import inspect
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..financial_safety import (
    FINANCIAL_MANAGER_CONFIRMED_ACTIONS,
    FINANCIAL_MANAGER_DRY_RUN_REQUIRED_ERROR,
    normalize_financial_confirmed_action_params,
)
from ..control_context import internal_control_context
from ..intents import ALLOWED_ACTIONS
from ..acp import ACPManager
from ..mcp_client import MCPAggregator
from ..mcp_payloads import classify_mcp_error
from ..native_skill_store import SkillStore
from ..native_utils import _safe_slug
from ..numeric import bounded_float, bounded_int
from ..plugin_runtime import NativePluginManager
from ..session_store_utils import redact_audit_text, sanitize_public_response
from ..skill_packs import SkillPackManager


def _confirmed_action_allowed(target_tool: str, action: str) -> bool:
    return any(
        target.get("tool") == target_tool and target.get("action") == action
        for target in ALLOWED_ACTIONS.values()
    )


def _confirmed_scope_blocked(target_tool: str, action: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "target_tool": target_tool,
            "target_action": action,
            "params": dict(params or {}),
            "detail": "Confirmed action execution is restricted to the ActionIntent allowlist.",
        },
        "error": "confirmed action execution is outside the ActionIntent allowlist",
        "error_code": "CONFIRMED_ACTION_SCOPE_BLOCKED",
        "meta": {
            "side_effect": {
                "level": "stateful",
                "target": f"{target_tool}.{action}".strip(".") or "confirmed_action",
                "confirmation_required": True,
                "idempotent": False,
            },
            "action_intent_guard": {
                "blocked": True,
                "reason": "confirmed_action_scope_mismatch",
            },
        },
    }


def _ensure_monorepo_paths() -> None:
    for parent in Path(__file__).resolve().parents:
        candidates = [
            parent / "akshare-mcp" / "src",
            parent / "aiask-quant-core" / "src",
            parent / "strategy-factory" / "src",
        ]
        added = False
        for candidate in candidates:
            if candidate.exists() and str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
                added = True
        if added:
            return


def _dependency_missing(exc: BaseException, *, dependency: str) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "configured": False,
            "dependency": dependency,
            "detail": str(exc),
        },
        "error": str(exc),
        "error_code": f"MISSING_{dependency.upper()}",
    }


def _execution_failed(exc: BaseException, *, action: str, dependency: str) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "configured": False,
            "dependency": dependency,
            "action": action,
            "detail": str(exc),
        },
        "error": str(exc),
        "error_code": f"{dependency.upper()}_EXECUTION_FAILED",
    }


def _public_desktop_result(result: Any) -> dict[str, Any]:
    """Return an HTTP/model-safe Desktop payload."""

    safe = sanitize_public_response(result, max_depth=10, max_items=300, max_text=4000)
    if isinstance(safe, dict):
        if safe.get("error") is not None:
            safe["error"] = redact_audit_text(safe.get("error"))
        safe["secrets_redacted"] = True
        return safe
    return {
        "success": True,
        "data": safe,
        "error": None,
        "secrets_redacted": True,
    }


def _public_confirmed_action_result(result: Any) -> dict[str, Any]:
    return _public_desktop_result(result)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def factor_factory_status(limit: int = 50) -> dict[str, Any]:
    try:
        _ensure_monorepo_paths()
        from akshare_mcp.services.factor_mining_factory.api import get_factor_pool_gateway
    except ModuleNotFoundError as exc:
        return _public_desktop_result(
            {
                "object": "aiask.desktop.factor_factory_status",
                "status": "unconfigured",
                "configured": False,
                "active_factors": [],
                "engine_health": {},
                "pool_health": {},
                "error": str(exc),
                "error_code": "MISSING_FACTOR_FACTORY",
                "secrets_redacted": True,
            }
        )

    try:
        gateway = get_factor_pool_gateway()
        pool_status = await asyncio.wait_for(gateway.get_pool_status(), timeout=15)
        active_factors = await asyncio.wait_for(
            gateway.get_active_factors(limit=bounded_int(limit, default=50, minimum=1, maximum=200)),
            timeout=15,
        )
        return _public_desktop_result(
            {
                "object": "aiask.desktop.factor_factory_status",
                "status": "ready",
                "configured": True,
                "factory": pool_status,
                "active_factors": list(active_factors or []),
                "engine_health": dict((pool_status or {}).get("engines") or {}),
                "pool_health": dict((pool_status or {}).get("pool_health") or {}),
                "secrets_redacted": True,
            }
        )
    except Exception as exc:
        return _public_desktop_result(
            {
                "object": "aiask.desktop.factor_factory_status",
                "status": "degraded",
                "configured": False,
                "active_factors": [],
                "engine_health": {},
                "pool_health": {},
                "error": str(exc),
                "error_code": "FACTOR_FACTORY_STATUS_FAILED",
                "secrets_redacted": True,
            }
        )


async def _execute_data_sync(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        _ensure_monorepo_paths()
        from akshare_mcp.tools.managers.data_sync_manager import register_data_sync_manager
    except ModuleNotFoundError as exc:
        return _dependency_missing(exc, dependency="data_sync")

    class _CaptureMcp:
        def __init__(self) -> None:
            self.tools: dict[str, Callable[..., Any]] = {}

        def tool(self, *args: Any, **kwargs: Any) -> Callable[..., Any]:
            if args and callable(args[0]) and len(args) == 1 and not kwargs:
                fn = args[0]
                self.tools[fn.__name__] = fn
                return fn

            def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                self.tools[fn.__name__] = fn
                return fn

            return _decorator

    try:
        capture = _CaptureMcp()
        register_data_sync_manager(capture)
        fn = capture.tools.get("data_sync_manager")
        if not callable(fn):
            raise RuntimeError("data_sync_manager was not registered")
        normalized = "run_due_schedules" if action == "maintenance" else action
        if normalized not in {"sync", "run_due_schedules", "schedule", "cancel_task", "cancel_schedule"}:
            raise ValueError(f"unsupported data sync action: {action}")
        result = await asyncio.wait_for(
            _maybe_await(
                fn(
                    action=normalized,
                    params=dict(params or {}),
                    codes=params.get("codes"),
                    task_type=params.get("task_type") or params.get("type"),
                    period=params.get("period"),
                    force=params.get("force"),
                    limit=params.get("limit"),
                    priority=params.get("priority"),
                )
            ),
            timeout=bounded_float(params.get("_timeout_seconds"), default=120.0, minimum=1.0, maximum=3600.0),
        )
        return result if isinstance(result, dict) else {"success": True, "data": result, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="data_sync")


async def _execute_factor_factory(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        _ensure_monorepo_paths()
        from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
        from strategy_factory.runtime.factor_mining import get_factor_mining_runtime
    except ModuleNotFoundError as exc:
        return _dependency_missing(exc, dependency="factor_factory")

    try:
        ensure_default_runtime_services()
        runtime = get_factor_mining_runtime()
        timeout = bounded_float(params.get("_timeout_seconds"), default=300.0, minimum=1.0, maximum=3600.0)
        if action == "run_once":
            result = await asyncio.wait_for(
                runtime.run_once(
                    trigger=str(params.get("trigger") or "desktop_intent"),
                    engines=[str(item) for item in list(params.get("engines") or [])] or None,
                    candidate_count=bounded_int(params.get("candidate_count") or params.get("candidates"), default=10, minimum=1, maximum=1000),
                    evolution_generations=bounded_int(params.get("evolution_generations") or params.get("generations"), default=2, minimum=0, maximum=100),
                    codes=[str(item) for item in list(params.get("codes") or [])] or None,
                ),
                timeout=timeout,
            )
        elif action == "maintenance":
            result = await asyncio.wait_for(runtime.run_maintenance(), timeout=timeout)
        else:
            raise ValueError(f"unsupported factor factory action: {action}")
        return {"success": bool(not isinstance(result, dict) or result.get("success") is not False), "data": result, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="factor_factory")


async def _execute_incubation_factory(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        _ensure_monorepo_paths()
        from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
        from strategy_factory.runtime.incubation import build_incubation_runtime
    except ModuleNotFoundError as exc:
        return _dependency_missing(exc, dependency="incubation_factory")

    try:
        ensure_default_runtime_services()
        timeout = bounded_float(params.get("_timeout_seconds"), default=600.0, minimum=1.0, maximum=3600.0)
        if action == "dry_run":
            runtime = build_incubation_runtime(dry_run=True)
            result = await asyncio.wait_for(runtime.run_once(), timeout=timeout)
        elif action == "run_once":
            runtime = build_incubation_runtime()
            result = await asyncio.wait_for(runtime.run_once(), timeout=timeout)
        elif action == "maintenance":
            runtime = build_incubation_runtime()
            result = {
                "status": "ready",
                "detail": "Incubation factory has no separate maintenance pass; status was refreshed.",
                "runner": runtime.status(),
            }
        else:
            raise ValueError(f"unsupported incubation factory action: {action}")
        return {"success": bool(not isinstance(result, dict) or result.get("success") is not False), "data": result, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="incubation_factory")


STOCK_RADAR_SCHEDULE_JOB_NAME = "AIASK Stock Radar Safe Daily Scan"


def _bool_param(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on", "enabled"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", "disabled"}:
            return False
    return bool(value)


def _stock_radar_run_params(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": str(payload.get("mode") or "run_once"),
        "days": bounded_int(payload.get("days"), default=3, minimum=1, maximum=30),
        "limit": bounded_int(payload.get("limit"), default=80, minimum=1, maximum=500),
        "stock_codes": payload.get("stock_codes") or payload.get("codes"),
        "allow_network": _bool_param(payload.get("allow_network"), default=False),
        "allow_llm": _bool_param(payload.get("allow_llm"), default=False),
        "embed": _bool_param(payload.get("embed"), default=False),
        "parse_pdf": _bool_param(payload.get("parse_pdf"), default=True),
        "include_rss": _bool_param(payload.get("include_rss"), default=True),
        "ingest_market_text": _bool_param(payload.get("ingest_market_text"), default=True),
    }


def _stock_radar_schedule_prompt(run_params: dict[str, Any]) -> str:
    return (
        "Run the AIASK stock radar once using the approved safe schedule payload. "
        f"Parameters: days={run_params.get('days')}, limit={run_params.get('limit')}, "
        f"allow_network={run_params.get('allow_network')}, allow_llm={run_params.get('allow_llm')}, "
        f"parse_pdf={run_params.get('parse_pdf')}, ingest_market_text={run_params.get('ingest_market_text')}. "
        "Persist the radar run and digest through Agent-owned facades. "
        "Do not include buy, sell, position sizing, or live trading instructions."
    )


def _find_stock_radar_job(store: Any) -> dict[str, Any] | None:
    for job in store.list():
        payload = dict(job.get("payload") or {})
        if payload.get("stock_radar") is True and payload.get("action") == "run_once":
            return job
        if str(job.get("name") or "") == STOCK_RADAR_SCHEDULE_JOB_NAME:
            return job
    return None


def _schedule_stock_radar_update(payload: dict[str, Any]) -> dict[str, Any]:
    from ..scheduler import AgentJobStore

    store = AgentJobStore()
    schedule_raw = str(payload.get("schedule") or "").strip()
    schedule_value = None if schedule_raw.lower() in {"", "manual", "none", "off"} else schedule_raw
    interval_seconds = payload.get("interval_seconds")
    if interval_seconds is not None:
        interval_seconds = bounded_int(interval_seconds, default=86400, minimum=60, maximum=604800)
    enabled = _bool_param(payload.get("enabled"), default=bool(schedule_value or interval_seconds))
    if enabled and not schedule_value and not interval_seconds:
        interval_seconds = 86400
    delete_requested = _bool_param(payload.get("delete") or payload.get("remove"), default=False) or schedule_raw.lower() in {"delete", "remove"}
    existing = _find_stock_radar_job(store)
    if delete_requested:
        deleted = bool(existing and store.delete(str(existing.get("job_id") or "")))
        return {
            "success": True,
            "data": {
                "object": "stock_radar.schedule_update",
                "status": "deleted" if deleted else "not_found",
                "deleted": deleted,
                "job_id": existing.get("job_id") if existing else None,
                "preview": False,
                "auto_push": False,
                "source_chain": ["ActionIntent", "aiask_agent.scheduler"],
            },
            "error": None,
            "meta": {"side_effect": {"level": "stateful", "target": "agent_jobs"}},
        }

    run_params = _stock_radar_run_params(payload)
    job_payload = {
        "stock_radar": True,
        "action": "run_once",
        "run_params": run_params,
        "source": "stock_radar.schedule_update",
        "no_trade_instructions": True,
        "auto_push": False,
    }
    prompt = _stock_radar_schedule_prompt(run_params)
    if existing:
        job = store.update(
            str(existing.get("job_id") or ""),
            name=STOCK_RADAR_SCHEDULE_JOB_NAME,
            prompt=prompt,
            schedule=schedule_value,
            interval_seconds=interval_seconds,
            toolset="finance_safe",
            enabled=enabled,
            payload=job_payload,
        )
        status = "updated" if enabled else "disabled"
    else:
        job = store.create(
            name=STOCK_RADAR_SCHEDULE_JOB_NAME,
            prompt=prompt,
            schedule=schedule_value,
            interval_seconds=interval_seconds,
            toolset="finance_safe",
            enabled=enabled,
            payload=job_payload,
        )
        status = "scheduled" if enabled else "disabled"
    return {
        "success": True,
        "data": {
            "object": "stock_radar.schedule_update",
            "status": status,
            "job": job,
            "job_id": job.get("job_id") if isinstance(job, dict) else None,
            "schedule": schedule_value,
            "interval_seconds": interval_seconds,
            "enabled": enabled,
            "preview": False,
            "auto_push": False,
            "source_chain": ["ActionIntent", "aiask_agent.scheduler"],
        },
        "error": None,
        "meta": {"side_effect": {"level": "stateful", "target": "agent_jobs"}},
    }


async def _execute_stock_radar(action: str, params: dict[str, Any]) -> dict[str, Any]:
    if action == "schedule_update":
        try:
            return _schedule_stock_radar_update(dict(params or {}))
        except Exception as exc:
            return _execution_failed(exc, action=action, dependency="stock_radar")

    try:
        _ensure_monorepo_paths()
        from akshare_mcp.storage import get_db
        from akshare_mcp.services.stock_radar import (
            push_stock_radar_digest,
            run_stock_radar,
        )
    except ModuleNotFoundError as exc:
        return _dependency_missing(exc, dependency="stock_radar")

    async def _record_gateway_delivery_log(db: Any, payload: dict[str, Any], data: dict[str, Any], delivery: dict[str, Any]) -> dict[str, Any] | None:
        recorder = getattr(db, "save_stock_radar_push_log", None)
        if not callable(recorder):
            return None
        push_logs = list(data.get("push_logs") or [])
        first_log = next((item for item in push_logs if isinstance(item, dict)), {})
        channels = data.get("channels") or payload.get("channels") or []
        if isinstance(channels, str):
            channels = [item.strip() for item in channels.split(",") if item.strip()]
        targets = payload.get("targets") if isinstance(payload.get("targets"), dict) else {}
        channel = str(delivery.get("channel") or "")
        target = payload.get("target")
        if target is None:
            target = targets.get(channel)
        try:
            return await recorder(
                {
                    "run_id": payload.get("run_id") or first_log.get("run_id"),
                    "channel": channel,
                    "platform": delivery.get("platform") or channel,
                    "target": target,
                    "status": "delivered" if delivery.get("ok") else "failed",
                    "message_preview": payload.get("message") or data.get("message_preview"),
                    "candidate_count": bounded_int(first_log.get("candidate_count") or data.get("candidate_count"), default=0, minimum=0),
                    "error": None if delivery.get("ok") else delivery.get("error") or delivery.get("status"),
                    "sent_at": datetime.now(timezone.utc).isoformat() if delivery.get("ok") else None,
                    "metadata": {
                        "dry_run": False,
                        "gateway_delivery": delivery,
                        "gateway_message_id": delivery.get("message_id"),
                        "no_trade_instructions": True,
                        "upstream_push_log_ids": [item.get("push_id") for item in push_logs if isinstance(item, dict) and item.get("push_id")],
                    },
                }
            )
        except Exception as exc:
            return {
                "status": "failed",
                "error": str(exc),
                "error_type": type(exc).__name__,
                "channel": channel,
            }

    async def _deliver_stock_radar_digest(payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        if payload.get("dry_run", True) or result.get("success") is False:
            return result
        channels = data.get("channels") or payload.get("channels") or ["wecom", "telegram"]
        if isinstance(channels, str):
            channels = [item.strip() for item in channels.split(",") if item.strip()]
        message = str(payload.get("message") or data.get("message_preview") or "").strip()
        if not message:
            return {
                **result,
                "data": {**data, "gateway_status": "blocked_empty_message", "gateway_deliveries": []},
                "success": False,
                "error": "stock radar digest message is empty",
                "error_code": "STOCK_RADAR_GATEWAY_MESSAGE_EMPTY",
            }
        try:
            from ..gateway import DeliveryRouter, GatewayChannelDirectoryStore, GatewayMessageStore
            from ..paths import default_state_db_path

            state_path = default_state_db_path()
            router = DeliveryRouter(
                messages=GatewayMessageStore(state_path),
                directory=GatewayChannelDirectoryStore(state_path),
            )
            deliveries: list[dict[str, Any]] = []
            for channel in list(channels or []):
                target = payload.get("target")
                targets = payload.get("targets") if isinstance(payload.get("targets"), dict) else {}
                if target is None:
                    target = targets.get(str(channel))
                try:
                    routed = await router.send(
                        platform=str(channel),
                        target=str(target or ""),
                        message=message,
                        thread_id=payload.get("thread_id"),
                        session_id=payload.get("session_id"),
                        user_id=payload.get("user_id"),
                    )
                    adapter = routed.get("adapter") if isinstance(routed, dict) else {}
                    deliveries.append(
                        {
                            "channel": channel,
                            "status": adapter.get("status") if isinstance(adapter, dict) else "unknown",
                            "ok": bool(adapter.get("ok")) if isinstance(adapter, dict) else False,
                            "message_id": (routed.get("message") or {}).get("message_id") if isinstance(routed, dict) and isinstance(routed.get("message"), dict) else None,
                            "configured": adapter.get("configured") if isinstance(adapter, dict) else None,
                            "platform": (routed.get("platform") or {}).get("name") if isinstance(routed, dict) and isinstance(routed.get("platform"), dict) else str(channel),
                        }
                    )
                except Exception as exc:
                    deliveries.append(
                        {
                            "channel": channel,
                            "status": "failed",
                            "ok": False,
                            "error": str(exc),
                            "error_type": type(exc).__name__,
                        }
                    )
            ok_count = sum(1 for item in deliveries if item.get("ok"))
            audit_logs = [item for delivery in deliveries if (item := await _record_gateway_delivery_log(db, payload, data, delivery)) is not None]
            gateway_status = "delivered" if ok_count == len(deliveries) and deliveries else "partial_or_failed"
            return {
                **result,
                "success": bool(result.get("success") and ok_count == len(deliveries) and deliveries),
                "data": {
                    **data,
                    "gateway_status": gateway_status,
                    "gateway_deliveries": deliveries,
                    "gateway_delivered_count": ok_count,
                    "gateway_push_logs": audit_logs,
                },
                "error": None if ok_count == len(deliveries) and deliveries else "one or more gateway deliveries failed or are unconfigured",
                "error_code": None if ok_count == len(deliveries) and deliveries else "STOCK_RADAR_GATEWAY_DELIVERY_FAILED",
            }
        except Exception as exc:
            return {
                **result,
                "success": False,
                "data": {**data, "gateway_status": "failed", "gateway_deliveries": []},
                "error": str(exc),
                "error_code": "STOCK_RADAR_GATEWAY_DELIVERY_FAILED",
            }

    try:
        payload = dict(params or {})
        timeout = bounded_float(
            payload.pop("_timeout_seconds", 300 if action == "run_once" else 60),
            default=300.0 if action == "run_once" else 60.0,
            minimum=1.0,
            maximum=3600.0,
        )
        db = get_db()
        await db.initialize()
        if action == "run_once":
            result = await asyncio.wait_for(
                run_stock_radar(
                    db,
                    mode=str(payload.get("mode") or "run_once"),
                    days=payload.get("days", 3),
                    limit=payload.get("limit", 80),
                    stock_codes=payload.get("stock_codes") or payload.get("codes"),
                    allow_network=payload.get("allow_network", False),
                    allow_llm=payload.get("allow_llm", False),
                    embed=payload.get("embed", False),
                    parse_pdf=payload.get("parse_pdf", True),
                    include_rss=payload.get("include_rss", True),
                    ingest_market_text=payload.get("ingest_market_text", True),
                ),
                timeout=timeout,
            )
        elif action == "push_digest":
            payload.setdefault("dry_run", True)
            result = await asyncio.wait_for(push_stock_radar_digest(db, payload), timeout=timeout)
            result = await _deliver_stock_radar_digest(payload, result if isinstance(result, dict) else {"success": True, "data": result, "error": None})
        else:
            raise ValueError(f"unsupported stock radar action: {action}")
        return result if isinstance(result, dict) else {"success": True, "data": result, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="stock_radar")


async def _execute_gateway(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        from ..gateway import (
            DeliveryRouter,
            GatewayChannelDirectoryStore,
            GatewayConfigStore,
            GatewayMessageStore,
            adapter_for,
            normalize_platform,
        )
        from ..paths import default_state_db_path

        payload = dict(params or {})
        state_path = Path(str(payload.get("_state_path") or default_state_db_path()))
        directory = GatewayChannelDirectoryStore(state_path)
        router = DeliveryRouter(
            messages=GatewayMessageStore(state_path),
            directory=directory,
        )
        if action == "retry":
            message_id = str(payload.get("message_id") or "").strip()
            if not message_id:
                raise ValueError("message_id is required for gateway retry")
            data = await router.retry(message_id)
            return {"success": True, "data": data, "error": None}
        if action == "pairing_create":
            data = {
                "action": "create",
                "platform": payload.get("platform"),
                "user_id": payload.get("user_id"),
                "session_id": payload.get("session_id"),
                "configured": True,
            }
            return {"success": True, "data": data, "error": None}
        if action == "directory_refresh":
            data = directory.refresh(config=GatewayConfigStore())
            return {"success": True, "data": data, "error": None}
        if action == "directory_upsert":
            data = {
                "item": directory.upsert(
                    platform=str(payload.get("platform") or "local"),
                    name=str(payload.get("name") or ""),
                    target=str(payload.get("target") or ""),
                    kind=str(payload.get("kind") or "channel"),
                    thread_id=payload.get("thread_id"),
                    metadata=dict(payload.get("metadata") or {}),
                )
            }
            return {"success": True, "data": data, "error": None}
        if action in {"platform_start", "platform_stop"}:
            platform = normalize_platform(str(payload.get("platform") or ""))
            if not platform:
                raise ValueError("platform is required for gateway platform control")
            status = GatewayConfigStore().platform_status(platform)
            adapter = adapter_for(status)
            data = await (adapter.start() if action == "platform_start" else adapter.stop())
            return {"success": True, "data": data, "error": None}
        if action not in {"send_message", "direct_deliver"}:
            raise ValueError(f"unsupported gateway action: {action}")
        data = await router.send(
            platform=str(payload.get("platform") or "local"),
            target=str(payload.get("target") or ""),
            message=str(payload.get("message") or ""),
            thread_id=payload.get("thread_id"),
            session_id=payload.get("session_id"),
            user_id=payload.get("user_id"),
            media_paths=[str(item) for item in list(payload.get("media_paths") or [])],
        )
        if action == "direct_deliver":
            data["deliver_mode"] = "direct_platform"
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="gateway")


async def _execute_webhook(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        from ..gateway import DeliveryRouter, GatewayChannelDirectoryStore, GatewayMessageStore
        from ..paths import default_state_db_path
        from ..webhooks import WebhookStore

        payload = dict(params or {})
        state_path = Path(str(payload.get("_state_path") or default_state_db_path()))
        webhooks = WebhookStore(state_path)
        if action == "subscribe":
            data = {
                "webhook": webhooks.subscribe(
                    name=str(payload.get("name") or ""),
                    events=[str(item) for item in list(payload.get("events") or [])],
                    prompt=str(payload.get("prompt") or ""),
                    deliver=str(payload.get("deliver") or "desktop_inbox"),
                    secret=payload.get("secret"),
                )
            }
            return {"success": True, "data": data, "error": None}
        if action == "remove":
            data = {"deleted": webhooks.remove(str(payload.get("webhook_id") or ""))}
            return {"success": True, "data": data, "error": None}
        if action != "trigger":
            raise ValueError(f"unsupported webhook action: {action}")
        data = webhooks.render_trigger(
            str(payload.get("webhook_id") or ""),
            event=str(payload.get("event") or "event"),
            payload=dict(payload.get("payload") or {}),
            signature=payload.get("signature"),
        )
        deliver_config = data.get("deliver") if isinstance(data.get("deliver"), dict) else {}
        if isinstance(deliver_config, dict) and deliver_config.get("mode") == "direct_platform":
            routed = await DeliveryRouter(
                messages=GatewayMessageStore(state_path),
                directory=GatewayChannelDirectoryStore(state_path),
            ).send(
                platform=str(deliver_config.get("platform") or "local"),
                target=str(deliver_config.get("target") or ""),
                thread_id=deliver_config.get("thread_id"),
                message=str(data.get("prompt") or ""),
            )
            data["direct_delivery"] = routed
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="webhook")


async def _execute_feishu_drive(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        from ..platform_apis import FeishuClient

        payload = dict(params or {})
        client = FeishuClient(domain=str(payload.get("domain") or "feishu"))
        if action == "reply_comment":
            data = client.reply_comment(
                file_token=str(payload.get("file_token") or ""),
                comment_id=str(payload.get("comment_id") or ""),
                message=str(payload.get("message") or ""),
            )
        elif action == "add_comment":
            data = client.add_comment(
                file_token=str(payload.get("file_token") or ""),
                message=str(payload.get("message") or ""),
            )
        else:
            raise ValueError(f"unsupported Feishu drive action: {action}")
        response = dict(data.get("response") or {})
        success = bool(data.get("configured")) and (not response or bool(response.get("ok")))
        return {
            "success": success,
            "data": data,
            "error": None if success else str(response.get("error") or "Feishu API call failed or is unconfigured"),
        }
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="feishu_drive")


async def _execute_discord(action: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = dict(params or {})
    if action == "channel_send":
        return await _execute_gateway(
            "send_message",
            {
                "platform": "discord",
                "target": str(payload.get("channel_id") or ""),
                "message": str(payload.get("message") or ""),
                "thread_id": payload.get("thread_id"),
                "session_id": payload.get("session_id"),
                "user_id": payload.get("user_id"),
            },
        )
    if action not in {"pin_message", "unpin_message", "create_thread", "add_role", "remove_role"}:
        return _execution_failed(ValueError(f"unsupported Discord action: {action}"), action=action, dependency="discord")
    try:
        from ..platform_apis import DiscordServerClient

        data = DiscordServerClient().call(
            action=action,
            guild_id=str(payload.get("guild_id") or ""),
            channel_id=str(payload.get("channel_id") or ""),
            user_id=str(payload.get("discord_user_id") or payload.get("user_id") or ""),
            role_id=str(payload.get("role_id") or ""),
            message_id=str(payload.get("message_id") or ""),
            name=str(payload.get("name") or ""),
            auto_archive_duration=bounded_int(payload.get("auto_archive_duration"), default=1440, minimum=60, maximum=10080),
        )
        success = bool(data.get("configured")) and data.get("ok") is not False
        return {
            "success": success,
            "data": data,
            "error": None if success else str(data.get("error") or "Discord API call failed or is unconfigured"),
        }
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="discord")


async def _execute_homeassistant(action: str, params: dict[str, Any]) -> dict[str, Any]:
    if action != "call_service":
        return _execution_failed(ValueError(f"unsupported Home Assistant action: {action}"), action=action, dependency="homeassistant")
    try:
        from .. import homeassistant as ha_native

        payload = dict(params or {})
        data = ha_native.call_service(
            domain=str(payload.get("domain") or ""),
            service=str(payload.get("service") or ""),
            entity_id=payload.get("entity_id"),
            data=dict(payload.get("data") or {}),
            confirmed=True,
        )
        success = bool(data.get("configured"))
        return {
            "success": success,
            "data": data,
            "error": None if success else "Home Assistant is not configured or service call failed",
        }
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="homeassistant")


async def _execute_skill(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(params or {})
        skills = SkillStore()
        if action == "save":
            data = {
                "skill": skills.save(
                    str(payload.get("name") or ""),
                    str(payload.get("content") or ""),
                    description=payload.get("description"),
                )
            }
        elif action in {"install", "update"}:
            content = str(payload.get("content") or "").strip()
            if not content:
                name = str(payload.get("name") or "skill").strip() or "skill"
                description = str(
                    payload.get("description")
                    or f"Runtime skill registered from {payload.get('path') or 'Agent API'}."
                ).strip()
                content = f"---\ndescription: {description}\n---\n\n# {name}\n"
            data = {
                "skill": skills.save(
                    str(payload.get("name") or ""),
                    content,
                    description=payload.get("description"),
                )
            }
        elif action == "uninstall":
            name = _safe_slug(str(payload.get("name") or ""))
            path = skills.root / name
            archived = skills.archive(name, reason="uninstall") if path.exists() else None
            data = {"name": name, "deleted": bool(path.exists() or archived), "archived": archived}
        elif action == "pin":
            data = {"skill": skills.pin(str(payload.get("name") or ""), True)}
        elif action == "unpin":
            data = {"skill": skills.pin(str(payload.get("name") or ""), False)}
        elif action == "set_enabled":
            data = {"skill": skills.set_enabled(str(payload.get("name") or ""), bool(payload.get("enabled", True)))}
        elif action == "archive":
            data = {"skill": skills.archive(str(payload.get("name") or ""), reason=payload.get("reason"))}
        elif action == "restore":
            data = {"skill": skills.restore(str(payload.get("name") or ""))}
        elif action == "rollback":
            data = {"rollback": skills.rollback(str(payload.get("backup_id") or "").strip() or None)}
        elif action == "snapshot":
            data = {"skills": skills.list(), "root": str(skills.root)}
            if bool(payload.get("create_backup")):
                data["backup"] = skills.backup(reason=str(payload.get("reason") or "snapshot"))
        elif action == "install_finance_templates":
            data = skills.install_finance_templates(overwrite=bool(payload.get("overwrite")))
        else:
            raise ValueError(f"unsupported skill action: {action}")
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="skill")


async def _execute_skill_pack(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(params or {})
        if action != "install":
            raise ValueError(f"unsupported skill pack action: {action}")
        manager = SkillPackManager(skill_store=SkillStore())
        data = manager.install(
            str(payload.get("pack") or payload.get("name") or ""),
            overwrite=bool(payload.get("overwrite")),
        )
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="skill_pack")


def _plugin_scope_blocked(action: str, params: dict[str, Any], detail: str) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "target_tool": "plugin",
            "target_action": action,
            "params": dict(params or {}),
            "detail": detail,
        },
        "error": "confirmed plugin action is outside the ActionIntent plugin guard",
        "error_code": "PLUGIN_INTENT_SCOPE_BLOCKED",
        "meta": {
            "side_effect": {
                "level": "stateful",
                "target": str((params or {}).get("wrapped_name") or (params or {}).get("plugin") or action),
                "confirmation_required": True,
                "idempotent": False,
            },
            "action_intent_guard": {
                "blocked": True,
                "reason": "plugin_intent_scope_mismatch",
            },
        },
    }


async def _execute_plugin(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(params or {})
        manager = NativePluginManager()
        if action == "set_enabled":
            data = {
                "plugin": manager.set_enabled(
                    str(payload.get("name") or ""),
                    bool(payload.get("enabled", True)),
                    description=payload.get("description"),
                )
            }
        elif action in {"enable", "disable"}:
            data = {
                "plugin": manager.set_enabled(
                    str(payload.get("name") or ""),
                    action == "enable",
                    description=payload.get("description"),
                )
            }
        elif action == "upsert":
            data = {
                "plugin": manager.update(
                    str(payload.get("name") or ""),
                    manifest=dict(payload.get("manifest") or {}),
                )
            }
        elif action in {"tool_call", "command_call"}:
            expected_kind = "command" if action == "command_call" else "tool"
            wrapped_name = str(payload.get("wrapped_name") or "").strip()
            created_by = str(payload.pop("_created_by_agent_plugin_tool", "") or "").strip()
            if not wrapped_name.startswith("agent_plugin_"):
                return _plugin_scope_blocked(action, payload, "plugin runner intent is missing a valid wrapped agent_plugin_* tool name")
            if created_by != wrapped_name:
                return _plugin_scope_blocked(
                    action,
                    payload,
                    "plugin runner ActionIntent execution requires an intent created by the matching agent_plugin_* tool",
                )
            if str(payload.get("kind") or expected_kind).strip() != expected_kind:
                return _plugin_scope_blocked(action, payload, f"plugin runner intent kind must be {expected_kind}")
            with internal_control_context("action_intent.plugin_runner"):
                data = {"plugin_call": await manager.call_tool(wrapped_name, dict(payload.get("arguments") or {}))}
        else:
            raise ValueError(f"unsupported plugin action: {action}")
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="plugin")


async def _execute_acp(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(params or {})
        manager = ACPManager()
        if action == "register_mcp_server":
            data = manager.register_server(payload)
        elif action == "remove_mcp_server":
            data = manager.remove_server(str(payload.get("name") or ""))
        else:
            raise ValueError(f"unsupported ACP action: {action}")
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="acp")


async def _execute_mcp(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(params or {})
        manager = MCPAggregator()
        if action in {"server_upsert", "server_update"}:
            payload_to_save = dict(payload)
            if action == "server_update":
                name = str(payload.get("name") or "")
                current = dict(manager._server_by_name(name))
                payload_to_save = {**current, **payload, "name": name}
            data = {
                "server": manager.register_client_server(
                    name=str(payload_to_save.get("name") or ""),
                    transport=payload_to_save.get("transport") or ("stdio" if payload_to_save.get("command") else "streamable_http"),
                    url=payload_to_save.get("url"),
                    command=payload_to_save.get("command"),
                    args=payload_to_save.get("args"),
                    domain=payload_to_save.get("domain"),
                    tools=payload_to_save.get("tools"),
                    resources=payload_to_save.get("resources"),
                    prompts=payload_to_save.get("prompts"),
                    oauth=payload_to_save.get("oauth"),
                    headers_from_env=payload_to_save.get("headers_from_env"),
                    enabled=bool(payload_to_save.get("enabled", True)),
                    description=payload_to_save.get("description"),
                )
            }
        elif action == "server_remove":
            name = str(payload.get("name") or "")
            removed = manager.remove_server(name)
            if not removed:
                raise ValueError(f"MCP server not found: {name}")
            data = {"id": name, "deleted": bool(removed)}
        elif action == "register_local":
            data = manager.register_local_server(
                name=payload.get("name"),
                url=payload.get("url"),
                transport=payload.get("transport"),
                domain=payload.get("domain"),
            )
        elif action == "discover_update":
            server_name = str(payload.get("server") or "")
            try:
                data = await manager.discover_and_update(server_name)
            except Exception as exc:
                error_code, detail = classify_mcp_error(exc)
                try:
                    auth = manager.auth_readiness(server_name)
                except Exception:
                    auth = {}
                discovery_status = "auth_missing" if error_code == "MCP_DISCOVERY_AUTH_REQUIRED" else "discovery_failed"
                return {
                    "success": False,
                    "data": {
                        "server": server_name,
                        "configured": False,
                        "discovery_status": discovery_status,
                        "detail": detail,
                        **dict(auth or {}),
                    },
                    "error": detail,
                    "error_code": error_code,
                }
        elif action == "oauth_callback":
            data = manager.oauth_callback(
                str(payload.get("server") or ""),
                dict(payload.get("token") or payload),
            )
        else:
            raise ValueError(f"unsupported MCP action: {action}")
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="mcp")


def _state_path_from_payload(payload: dict[str, Any]) -> Path | None:
    raw = str(payload.pop("_state_path", "") or "").strip()
    return Path(raw) if raw else None


_FILE_ACTION_TO_TOOL = {
    "write": "agent_file_write",
    "patch": "agent_file_patch",
    "checkpoint": "agent_file_checkpoint",
    "rollback": "agent_file_rollback",
}

_BROWSER_ACTION_TO_TOOL = {
    "navigate": "agent_browser_navigate",
    "click": "agent_browser_click",
    "type": "agent_browser_type",
    "scroll": "agent_browser_scroll",
    "back": "agent_browser_back",
    "press": "agent_browser_press",
    "cdp": "agent_browser_cdp",
    "computer_use": "agent_computer_use",
}

_PROCESS_ACTION_TO_TOOL = {
    "kill": "agent_process",
    "wait": "agent_process",
    "watch": "agent_process",
}


def _file_scope_blocked(action: str, params: dict[str, Any], detail: str) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "target_tool": "file",
            "target_action": action,
            "params": dict(params or {}),
            "detail": detail,
        },
        "error": "confirmed file action is outside the ActionIntent file guard",
        "error_code": "FILE_INTENT_SCOPE_BLOCKED",
        "meta": {
            "side_effect": {
                "level": "filesystem_write",
                "target": str((params or {}).get("path") or (params or {}).get("checkpoint_id") or action),
                "confirmation_required": True,
                "idempotent": False,
            },
            "action_intent_guard": {
                "blocked": True,
                "reason": "file_intent_scope_mismatch",
            },
        },
    }


def _workspace_roots_from_file_payload(payload: dict[str, Any]) -> tuple[str, ...]:
    raw_roots = payload.pop("_workspace_roots", None)
    if not isinstance(raw_roots, list):
        raise ValueError("file intent is missing workspace roots")
    roots = tuple(str(item).strip() for item in raw_roots if str(item).strip())
    if not roots:
        raise ValueError("file intent has no workspace roots")
    return tuple(str(Path(root).expanduser().resolve()) for root in roots)


async def _execute_file(action: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = dict(params or {})
    expected_tool = _FILE_ACTION_TO_TOOL.get(action)
    if not expected_tool:
        return _file_scope_blocked(action, payload, f"unsupported file action: {action}")
    created_by = str(payload.pop("_created_by_agent_file_tool", "") or "").strip()
    if created_by != expected_tool:
        return _file_scope_blocked(
            action,
            payload,
            "file ActionIntent execution requires an intent created by the matching agent_file_* tool",
        )
    try:
        state_path = _state_path_from_payload(payload)
        workspace_roots = _workspace_roots_from_file_payload(payload)
        from ..general_tools import build_general_tool_handlers
        from ..tools.policy import ToolPolicy

        handlers = build_general_tool_handlers(
            ToolPolicy("general_full", True, workspace_roots),
            state_path=state_path,
        )
        handler = handlers[expected_tool]
        with internal_control_context("action_intent.file_mutation"):
            return await _maybe_await(handler(payload))
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="file")


def _browser_scope_blocked(action: str, params: dict[str, Any], detail: str) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "target_tool": "browser",
            "target_action": action,
            "params": dict(params or {}),
            "detail": detail,
        },
        "error": "confirmed browser action is outside the ActionIntent browser guard",
        "error_code": "BROWSER_INTENT_SCOPE_BLOCKED",
        "meta": {
            "side_effect": {
                "level": "browser_state",
                "target": str((params or {}).get("selector") or (params or {}).get("url") or (params or {}).get("method") or action),
                "confirmation_required": True,
                "idempotent": False,
            },
            "action_intent_guard": {
                "blocked": True,
                "reason": "browser_intent_scope_mismatch",
            },
        },
    }


async def _execute_browser(action: str, params: dict[str, Any], *, runtime: Any | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    expected_tool = _BROWSER_ACTION_TO_TOOL.get(action)
    if not expected_tool:
        return _browser_scope_blocked(action, payload, f"unsupported browser action: {action}")
    created_by = str(payload.pop("_created_by_agent_browser_tool", "") or "").strip()
    if created_by != expected_tool:
        return _browser_scope_blocked(
            action,
            payload,
            "browser ActionIntent execution requires an intent created by the matching agent_browser_* tool",
        )
    if runtime is None:
        return _browser_scope_blocked(
            action,
            payload,
            "browser ActionIntent execution requires an active full runtime browser session",
        )
    try:
        with internal_control_context("action_intent.browser_automation"):
            return await runtime.tool_registry.call_tool(expected_tool, payload)
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="browser")


def _execution_scope_blocked(
    *,
    target_tool: str,
    action: str,
    params: dict[str, Any],
    level: str,
    detail: str,
    error_code: str,
) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "target_tool": target_tool,
            "target_action": action,
            "params": dict(params or {}),
            "detail": detail,
        },
        "error": f"confirmed {target_tool} action is outside the ActionIntent execution guard",
        "error_code": error_code,
        "meta": {
            "side_effect": {
                "level": level,
                "target": str(
                    (params or {}).get("process_id")
                    or (params or {}).get("cwd")
                    or (params or {}).get("command")
                    or action
                ),
                "confirmation_required": True,
                "idempotent": False,
            },
            "action_intent_guard": {
                "blocked": True,
                "reason": f"{target_tool}_intent_scope_mismatch",
            },
        },
    }


async def _execute_terminal(action: str, params: dict[str, Any], *, runtime: Any | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    if action != "run":
        return _execution_scope_blocked(
            target_tool="terminal",
            action=action,
            params=payload,
            level="process_execution",
            detail=f"unsupported terminal action: {action}",
            error_code="TERMINAL_INTENT_SCOPE_BLOCKED",
        )
    created_by = str(payload.pop("_created_by_agent_terminal_tool", "") or "").strip()
    if created_by != "agent_terminal":
        return _execution_scope_blocked(
            target_tool="terminal",
            action=action,
            params=payload,
            level="process_execution",
            detail="terminal ActionIntent execution requires an intent created by agent_terminal",
            error_code="TERMINAL_INTENT_SCOPE_BLOCKED",
        )
    if runtime is None:
        return _execution_scope_blocked(
            target_tool="terminal",
            action=action,
            params=payload,
            level="process_execution",
            detail="terminal ActionIntent execution requires an active full runtime",
            error_code="TERMINAL_INTENT_SCOPE_BLOCKED",
        )
    payload.pop("_state_path", None)
    payload.pop("_workspace_roots", None)
    try:
        with internal_control_context("action_intent.terminal_execution"):
            return await runtime.tool_registry.call_tool("agent_terminal", payload)
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="terminal")


async def _execute_process(action: str, params: dict[str, Any], *, runtime: Any | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    expected_tool = _PROCESS_ACTION_TO_TOOL.get(action)
    if not expected_tool:
        return _execution_scope_blocked(
            target_tool="process",
            action=action,
            params=payload,
            level="process_control",
            detail=f"unsupported process action: {action}",
            error_code="PROCESS_INTENT_SCOPE_BLOCKED",
        )
    created_by = str(payload.pop("_created_by_agent_process_tool", "") or "").strip()
    if created_by != expected_tool:
        return _execution_scope_blocked(
            target_tool="process",
            action=action,
            params=payload,
            level="process_control",
            detail="process ActionIntent execution requires an intent created by agent_process",
            error_code="PROCESS_INTENT_SCOPE_BLOCKED",
        )
    if runtime is None:
        return _execution_scope_blocked(
            target_tool="process",
            action=action,
            params=payload,
            level="process_control",
            detail="process ActionIntent execution requires an active full runtime",
            error_code="PROCESS_INTENT_SCOPE_BLOCKED",
        )
    payload.pop("_state_path", None)
    payload.pop("_workspace_roots", None)
    payload["action"] = action
    try:
        with internal_control_context("action_intent.process_control"):
            return await runtime.tool_registry.call_tool(expected_tool, payload)
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="process")


async def _execute_code(action: str, params: dict[str, Any], *, runtime: Any | None = None) -> dict[str, Any]:
    payload = dict(params or {})
    if action != "execute_python":
        return _execution_scope_blocked(
            target_tool="code",
            action=action,
            params=payload,
            level="code_execution",
            detail=f"unsupported code action: {action}",
            error_code="CODE_INTENT_SCOPE_BLOCKED",
        )
    created_by = str(payload.pop("_created_by_agent_code_tool", "") or "").strip()
    if created_by != "agent_execute_python":
        return _execution_scope_blocked(
            target_tool="code",
            action=action,
            params=payload,
            level="code_execution",
            detail="code ActionIntent execution requires an intent created by agent_execute_python",
            error_code="CODE_INTENT_SCOPE_BLOCKED",
        )
    if runtime is None:
        return _execution_scope_blocked(
            target_tool="code",
            action=action,
            params=payload,
            level="code_execution",
            detail="code ActionIntent execution requires an active full runtime",
            error_code="CODE_INTENT_SCOPE_BLOCKED",
        )
    payload.pop("_state_path", None)
    payload.pop("_workspace_roots", None)
    try:
        with internal_control_context("action_intent.code_execution"):
            return await runtime.tool_registry.call_tool("agent_execute_python", payload)
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="code")


async def _execute_memory(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        if action != "save":
            raise ValueError(f"unsupported memory action: {action}")
        payload = dict(params or {})
        state_path = _state_path_from_payload(payload)
        from ..memory_providers import MemoryProviderManager

        data = {"memory": MemoryProviderManager(path=state_path).save(payload)}
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="memory")


async def _execute_learning(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(params or {})
        state_path = _state_path_from_payload(payload)
        from ..learning_loop import LearningLoop
        from ..session_store import AgentSessionStore

        session_store = AgentSessionStore(state_path)
        learning = LearningLoop(session_store=session_store, state_path=state_path)
        if action == "apply":
            data = {"proposal": learning.apply(str(payload.get("proposal_id") or ""))}
        elif action == "reflect_skill":
            data = {
                "proposal": learning.reflect_skill(
                    name=str(payload.get("name") or ""),
                    observation=str(payload.get("observation") or ""),
                )
            }
        else:
            raise ValueError(f"unsupported learning action: {action}")
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="learning")


async def _execute_rl(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(params or {})
        state_path = _state_path_from_payload(payload)
        from ..rl_atropos import RLAtroposManager

        rl = RLAtroposManager(state_path)
        if action == "select_environment":
            data = rl.select_environment(str(payload.get("environment") or ""))
            success = True
        elif action == "edit_config":
            data = rl.edit_config(dict(payload.get("config") or payload.get("patch") or {}))
            success = True
        elif action == "start_training":
            data = rl.start_training(
                environment=payload.get("environment"),
                config_patch=dict(payload.get("config") or {}),
            )
            success = bool(data.get("started", True)) and data.get("configured", True) is not False
        elif action == "stop_training":
            data = {"run": rl.stop_training(str(payload.get("run_id") or ""))}
            success = True
        elif action == "test_inference":
            data = rl.test_inference(str(payload.get("prompt") or ""))
            success = True
        else:
            raise ValueError(f"unsupported RL action: {action}")
        return {
            "success": success,
            "data": data,
            "error": None if success else "RL training credentials are not configured",
        }
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="rl")


async def _execute_job(action: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = dict(params or {})
        state_path = _state_path_from_payload(payload)
        from ..scheduler import AgentJobStore

        jobs = AgentJobStore(state_path)
        if action == "create":
            data = {
                "job": jobs.create(
                    name=str(payload.get("name") or ""),
                    prompt=str(payload.get("prompt") or ""),
                    schedule=payload.get("schedule"),
                    interval_seconds=payload.get("interval_seconds"),
                    toolset=str(payload.get("toolset") or "finance_safe"),
                    enabled=bool(payload.get("enabled", True)),
                    payload={
                        "script": payload.get("script"),
                        "skills": list(payload.get("skills") or []),
                        "silent_pattern": payload.get("silent_pattern"),
                    },
                )
            }
        elif action in {"pause", "resume"}:
            job = jobs.update(str(payload.get("job_id") or ""), enabled=action == "resume")
            if job is None:
                raise FileNotFoundError(f"job not found: {payload.get('job_id') or ''}")
            data = {"job": job}
        elif action == "update":
            job = jobs.update(
                str(payload.get("job_id") or ""),
                **{
                    key: value
                    for key, value in payload.items()
                    if key in {"name", "prompt", "schedule", "interval_seconds", "toolset", "enabled", "payload"}
                },
            )
            if job is None:
                raise FileNotFoundError(f"job not found: {payload.get('job_id') or ''}")
            data = {"job": job}
        elif action == "remove":
            deleted = jobs.delete(str(payload.get("job_id") or ""))
            if not deleted:
                raise FileNotFoundError(f"job not found: {payload.get('job_id') or ''}")
            data = {"deleted": deleted}
        else:
            raise ValueError(f"unsupported job action: {action}")
        return {"success": True, "data": data, "error": None}
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="job")


def _invalid_financial_params(action: str, detail: str) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "configured": False,
            "action": action,
            "detail": detail,
        },
        "error": detail,
        "error_code": "FINANCIAL_MANAGER_INVALID_PARAMS",
    }


def _financial_scope_blocked(action: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "target_tool": "financial_manager",
            "target_action": action,
            "params": dict(params or {}),
            "detail": "Financial Manager confirmed execution is restricted to the allowlisted Desktop scope.",
        },
        "error": "financial manager confirmed execution is outside the allowlisted scope",
        "error_code": "FINANCIAL_MANAGER_EXECUTOR_SCOPE_BLOCKED",
    }


def _financial_dry_run_required(action: str, params: dict[str, Any]) -> dict[str, Any]:
    return {
        "success": False,
        "data": {
            "target_tool": "financial_manager",
            "target_action": action,
            "params": dict(params or {}),
            "detail": "This Financial Manager confirmed action is limited to dry-run execution.",
        },
        "error": "financial manager confirmed action requires dry_run=true",
        "error_code": "FINANCIAL_MANAGER_DRY_RUN_REQUIRED",
    }


def _financial_manager_capture() -> type:
    class _CaptureMcp:
        def __init__(self) -> None:
            self.tools: dict[str, Callable[..., Any]] = {}

        def tool(self, *args: Any, **kwargs: Any) -> Callable[..., Any]:
            if args and callable(args[0]) and len(args) == 1 and not kwargs:
                fn = args[0]
                self.tools[fn.__name__] = fn
                return fn

            def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
                self.tools[fn.__name__] = fn
                return fn

            return _decorator

    return _CaptureMcp


def _load_manager_callable(
    *,
    module_name: str,
    register_name: str,
    tool_name: str,
) -> Callable[..., Any]:
    _ensure_monorepo_paths()
    module = __import__(module_name, fromlist=[register_name])
    register = getattr(module, register_name)
    capture = _financial_manager_capture()()
    register(capture)
    fn = capture.tools.get(tool_name)
    if not callable(fn):
        raise RuntimeError(f"{tool_name} was not registered from {module_name}")
    return fn


def _normalize_financial_payload(action: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = normalize_financial_confirmed_action_params(action, params)
    if action == "watchlist_manager.add":
        group = payload.get("group") or payload.get("group_id")
        if group:
            payload["group_id"] = str(group)
        if not str(payload.get("code") or "").strip():
            raise KeyError("code")
    elif action == "watchlist_manager.remove":
        group = payload.get("group") or payload.get("group_id")
        if group:
            payload["group_id"] = str(group)
        if not str(payload.get("code") or "").strip():
            raise KeyError("code")
    elif action == "portfolio_manager.create":
        if not str(payload.get("name") or "").strip():
            raise KeyError("name")
    elif action == "portfolio_manager.add_holding":
        if payload.get("shares") is None:
            candidate = payload.get("quantity")
            if candidate is not None:
                payload["shares"] = candidate
        if payload.get("cost_price") is None and payload.get("price") is not None:
            payload["cost_price"] = payload.get("price")
        for required in ("portfolio_id", "code", "shares"):
            if payload.get(required) in {None, ""}:
                raise KeyError(required)
    elif action == "execution_manager.create_plan":
        algorithm = str(payload.get("algorithm") or "twap").strip().lower()
        if algorithm not in {"twap", "vwap"}:
            raise ValueError("algorithm")
        quantity = payload.get("total_quantity", payload.get("quantity", payload.get("total_shares")))
        duration = payload.get("duration_minutes", payload.get("duration", 60))
        if quantity in {None, ""}:
            raise KeyError("quantity")
        if not str(payload.get("code") or "").strip():
            raise KeyError("code")
        payload["algorithm"] = algorithm
        payload["total_quantity"] = quantity
        payload["total_shares"] = quantity
        payload["duration_minutes"] = duration
        payload["duration"] = duration
        payload["dry_run"] = True
    elif action == "paper_trading_manager.submit_order":
        quantity = payload.get("quantity", payload.get("shares"))
        if quantity in {None, ""}:
            raise KeyError("quantity")
        if not str(payload.get("code") or "").strip():
            raise KeyError("code")
        payload["quantity"] = quantity
        payload["shares"] = quantity
        payload["dry_run"] = True
    return payload


async def _execute_financial_manager(action: str, params: dict[str, Any]) -> dict[str, Any]:
    allowlisted = set(FINANCIAL_MANAGER_CONFIRMED_ACTIONS)
    if action not in allowlisted:
        return _financial_scope_blocked(action, params)

    try:
        payload = _normalize_financial_payload(action, params)
    except KeyError as exc:
        return _invalid_financial_params(action, f"missing required parameter: {exc.args[0]}")
    except ValueError as exc:
        if str(exc) == FINANCIAL_MANAGER_DRY_RUN_REQUIRED_ERROR:
            return _financial_dry_run_required(action, params)
        if str(exc) == "algorithm":
            return _invalid_financial_params(action, "algorithm must be twap or vwap")
        return _invalid_financial_params(action, str(exc))

    if action == "paper_trading_manager.submit_order":
        preview = {
            "preview_id": f"paper_preview_{int(time.time() * 1000)}",
            "status": "dry_run_preview",
            "dry_run": True,
            "manager": "paper_trading_manager",
            "action": "place_order",
            "code": str(payload.get("code") or ""),
            "side": str(payload.get("side") or payload.get("direction") or "buy"),
            "quantity": bounded_int(payload.get("quantity") or payload.get("shares"), default=0, minimum=0),
            "order_type": str(payload.get("order_type") or "market"),
            "account_id": payload.get("account_id"),
            "user_id": payload.get("user_id"),
            "note": "Financial Manager Desktop keeps paper order confirmed execution in dry-run preview mode only.",
        }
        return {"success": True, "data": preview, "error": None}

    try:
        if action.startswith("watchlist_manager."):
            fn = _load_manager_callable(
                module_name="akshare_mcp.tools.managers.watchlist_manager",
                register_name="register_watchlist_manager",
                tool_name="watchlist_manager",
            )
            manager_action = "add" if action.endswith(".add") else "remove"
            return await _maybe_await(fn(action=manager_action, params=payload))
        if action.startswith("portfolio_manager."):
            fn = _load_manager_callable(
                module_name="akshare_mcp.tools.managers.portfolio_manager",
                register_name="register_portfolio_manager",
                tool_name="portfolio_manager",
            )
            manager_action = action.split(".", 1)[1]
            return await _maybe_await(fn(action=manager_action, params=payload))
        if action == "execution_manager.create_plan":
            fn = _load_manager_callable(
                module_name="akshare_mcp.tools.managers.execution_manager",
                register_name="register_execution_manager",
                tool_name="execution_manager",
            )
            return await _maybe_await(
                fn(
                    action=str(payload.get("algorithm") or "twap"),
                    params=payload,
                    dry_run=True,
                )
            )
    except ModuleNotFoundError as exc:
        return _dependency_missing(exc, dependency="financial_manager")
    except Exception as exc:
        return _execution_failed(exc, action=action, dependency="financial_manager")

    return _financial_scope_blocked(action, params)


async def execute_confirmed_action(
    target_tool: str,
    action: str,
    params: dict[str, Any] | None = None,
    *,
    runtime: Any | None = None,
) -> dict[str, Any]:
    normalized_tool = str(target_tool or "").strip()
    normalized_action = str(action or "").strip()
    payload = dict(params or {})
    if not _confirmed_action_allowed(normalized_tool, normalized_action):
        return _public_confirmed_action_result(
            _confirmed_scope_blocked(normalized_tool, normalized_action, payload)
        )
    if normalized_tool == "strategy_manager":
        from .strategy_factory import execute_confirmed_action as execute_strategy_action

        return _public_confirmed_action_result(await execute_strategy_action(normalized_action, payload))
    if normalized_tool == "data_sync":
        return _public_confirmed_action_result(await _execute_data_sync(normalized_action, payload))
    if normalized_tool == "factor_factory":
        return _public_confirmed_action_result(await _execute_factor_factory(normalized_action, payload))
    if normalized_tool == "incubation_factory":
        return _public_confirmed_action_result(await _execute_incubation_factory(normalized_action, payload))
    if normalized_tool == "factory_control":
        from ..factory_dispatcher import dispatch_factory_run

        return _public_confirmed_action_result(await dispatch_factory_run(payload))
    if normalized_tool == "stock_radar":
        return _public_confirmed_action_result(await _execute_stock_radar(normalized_action, payload))
    if normalized_tool == "gateway":
        return _public_confirmed_action_result(await _execute_gateway(normalized_action, payload))
    if normalized_tool == "webhook":
        return _public_confirmed_action_result(await _execute_webhook(normalized_action, payload))
    if normalized_tool == "feishu_drive":
        return _public_confirmed_action_result(await _execute_feishu_drive(normalized_action, payload))
    if normalized_tool == "discord":
        return _public_confirmed_action_result(await _execute_discord(normalized_action, payload))
    if normalized_tool == "homeassistant":
        return _public_confirmed_action_result(await _execute_homeassistant(normalized_action, payload))
    if normalized_tool == "skill":
        return _public_confirmed_action_result(await _execute_skill(normalized_action, payload))
    if normalized_tool == "skill_pack":
        return _public_confirmed_action_result(await _execute_skill_pack(normalized_action, payload))
    if normalized_tool == "plugin":
        return _public_confirmed_action_result(await _execute_plugin(normalized_action, payload))
    if normalized_tool == "acp":
        return _public_confirmed_action_result(await _execute_acp(normalized_action, payload))
    if normalized_tool == "mcp":
        return _public_confirmed_action_result(await _execute_mcp(normalized_action, payload))
    if normalized_tool == "memory":
        return _public_confirmed_action_result(await _execute_memory(normalized_action, payload))
    if normalized_tool == "learning":
        return _public_confirmed_action_result(await _execute_learning(normalized_action, payload))
    if normalized_tool == "rl":
        return _public_confirmed_action_result(await _execute_rl(normalized_action, payload))
    if normalized_tool == "job":
        return _public_confirmed_action_result(await _execute_job(normalized_action, payload))
    if normalized_tool == "file":
        return _public_confirmed_action_result(await _execute_file(normalized_action, payload))
    if normalized_tool == "browser":
        return _public_confirmed_action_result(await _execute_browser(normalized_action, payload, runtime=runtime))
    if normalized_tool == "terminal":
        return _public_confirmed_action_result(await _execute_terminal(normalized_action, payload, runtime=runtime))
    if normalized_tool == "process":
        return _public_confirmed_action_result(await _execute_process(normalized_action, payload, runtime=runtime))
    if normalized_tool == "code":
        return _public_confirmed_action_result(await _execute_code(normalized_action, payload, runtime=runtime))
    if normalized_tool == "financial_manager":
        return _public_confirmed_action_result(await _execute_financial_manager(normalized_action, payload))
    return _public_confirmed_action_result(
        {
            "success": False,
            "data": {
                "target_tool": normalized_tool,
                "target_action": normalized_action,
                "detail": "No confirmed-action executor is registered for this target.",
            },
            "error": f"unsupported confirmed action target: {normalized_tool}",
            "error_code": "UNSUPPORTED_INTENT_TARGET",
        }
    )
