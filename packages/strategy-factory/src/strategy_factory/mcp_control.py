"""Read-only control-plane views for the four AIASK factories."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from aiask_quant_core.storage import get_db
from strategy_factory.api import FactoryName, normalize_factory_name

logger = logging.getLogger(__name__)


def _ensure_factory_schema_registered() -> None:
    """The read-only MCP process still creates the factory tables on a new DB."""
    try:
        from aiask_quant_core.storage.sqlite.schema_providers import (
            get_schema_initializer,
        )

        if get_schema_initializer("strategy_runtime") is None:
            from .infrastructure.factory_schema import register_factory_schema

            register_factory_schema()
    except Exception as exc:  # noqa: BLE001 - registration is best effort
        logger.debug("factory schema registration skipped: %s", exc)


_ensure_factory_schema_registered()


_STRATEGY_FACTORY_STATUS_MAP = {
    "success": "succeeded",
    "success_observe_only": "partial",
    "partial": "partial",
    "partial_infra": "partial",
    "degraded": "partial",
    "skipped": "blocked",
    "blocked": "blocked",
    "failed": "failed",
    "running": "running",
    "queued": "queued",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}
_TERMINAL_RUN_STATUSES = frozenset({"succeeded", "partial", "blocked", "failed", "cancelled"})


def _json_value(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not value:
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _strategy_factory_run_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Map a strategy_factory_runs row onto the factory-ledger read model."""
    if not row:
        return None
    raw = dict(row)
    raw_status = str(raw.get("status") or "").strip().lower()
    status = _STRATEGY_FACTORY_STATUS_MAP.get(raw_status, "failed")
    started_at = raw.get("started_at") or raw.get("created_at")
    return {
        "run_id": raw.get("run_id"),
        "factory": FactoryName.STRATEGY.value,
        "status": status,
        "raw_status": raw_status or None,
        "mode": raw.get("execution_mode") or "once",
        "requested_at": started_at,
        "started_at": started_at,
        "finished_at": raw.get("completed_at"),
        "elapsed_seconds": raw.get("elapsed_seconds"),
        "execution_mode": raw.get("execution_mode"),
        "engine_version": raw.get("engine_version"),
        "parity_status": raw.get("parity_status"),
        "summary": _json_value(raw.get("summary"), {}),
        "stages": _json_value(raw.get("stages"), {}),
        "evidence_refs": _json_value(raw.get("artifact_refs"), []),
        "quality_flags": [],
        "blockers": [],
        "error": raw.get("error"),
        "terminal": status in _TERMINAL_RUN_STATUSES,
        "contract_version": "aiask.factory_run.v1",
        "source": "strategy_factory_runs",
    }


FACTORY_DESCRIPTORS = {
    FactoryName.STRATEGY.value: {
        "label": "Strategy Factory",
        "runner": "scripts/factories/run_strategy_factory.py",
    },
    FactoryName.FACTOR_MINING.value: {
        "label": "Factor Mining Factory",
        "runner": "scripts/factories/run_factor_mining_factory.py",
    },
    FactoryName.INCUBATION.value: {
        "label": "Incubation Factory",
        "runner": "scripts/factories/run_incubation_factory.py",
    },
    FactoryName.MARKET_EVENT_INGEST.value: {
        "label": "Market Event Ingest",
        "runner": "scripts/factories/run_market_event_ingest.py",
    },
}


def _project_root() -> Path:
    explicit = str(os.getenv("AIASK_PROJECT_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    here = Path(__file__).resolve()
    for base in (here.parent, *here.parents):
        if (base / "scripts" / "factories").is_dir():
            return base
        if (base / "packages" / "strategy-factory").is_dir():
            return base
    return here.parents[4]


def _runtime_home() -> Path:
    explicit = str(os.getenv("AIASK_RUNTIME_HOME") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return Path.home() / ".aiask"


def _sidecar_status_path() -> Path:
    explicit = str(os.getenv("AIASK_SIGNAL_TRACKER_STATUS_PATH") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    return _runtime_home() / "run" / "signal_tracker.status.json"


_SUPERVISOR_FACTORY_ALIASES = {
    FactoryName.FACTOR_MINING.value: "factor_mining_factory",
    FactoryName.INCUBATION.value: "incubation_factory",
    FactoryName.MARKET_EVENT_INGEST.value: "market_event_ingest",
}


def _supervisor_runs(factory: str | None = None) -> list[dict[str, Any]]:
    """Replay supervisor start/exit records as read-only run history.

    These are process-lifecycle records, not business factory runs; the
    ``source`` field and ``mode='supervisor'`` keep that distinction visible.
    """
    targets = (
        {_SUPERVISOR_FACTORY_ALIASES[factory]: factory}
        if factory in _SUPERVISOR_FACTORY_ALIASES
        else dict(_SUPERVISOR_FACTORY_ALIASES)
    )
    path = _runtime_home() / "logs" / "four_runtime_supervisor" / "execution_records.jsonl"
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    open_runs: dict[str, dict[str, Any]] = {}
    runs: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        event = record.get("event_type")
        target = str(record.get("factory") or "")
        if target not in targets:
            continue
        normalized = targets[target]
        if event == "factory_start":
            run = {
                "run_id": f"supervisor:{target}:{record.get('ts')}:{record.get('pid')}",
                "factory": normalized,
                "status": "running",
                "raw_status": "started",
                "mode": "supervisor",
                "requested_at": record.get("ts"),
                "started_at": record.get("ts"),
                "finished_at": None,
                "pid": record.get("pid"),
                "restart_count": record.get("restart_count"),
                "source": "supervisor_execution_records",
                "terminal": False,
            }
            open_runs[target] = run
            runs.append(run)
        elif event == "factory_exit":
            run = open_runs.pop(target, None)
            if run is None:
                continue
            exit_code = record.get("exit_code")
            if exit_code == 0:
                status = "succeeded"
            elif exit_code == -15:
                status = "cancelled"
            else:
                status = "failed"
            run.update({
                "status": status,
                "raw_status": record.get("status"),
                "finished_at": record.get("ts"),
                "exit_code": exit_code,
                "uptime_seconds": record.get("uptime_seconds"),
                "terminal": True,
            })
    runs.sort(key=lambda item: str(item.get("started_at") or ""), reverse=True)
    return runs


def _supervisor_heartbeat(factory: str) -> dict[str, Any] | None:
    """Read the latest supervisor heartbeat for factories without a run ledger.

    The supervisor has no per-run DB writer yet; its heartbeat is the only
    live runtime evidence for those children.  This is a read-only status
    fallback, not a substitute for a persisted run record.
    """
    target = _SUPERVISOR_FACTORY_ALIASES.get(factory)
    if not target:
        return None
    path = _runtime_home() / "logs" / "four_runtime_supervisor" / "execution_records.jsonl"
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines[-200:]):
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if record.get("event_type") != "supervisor_heartbeat":
            continue
        for item in list(record.get("factories") or []):
            if str(item.get("factory") or "") != target:
                continue
            status = str(item.get("status") or "unknown").strip().lower()
            return {
                "run_id": f"supervisor:{target}:{item.get('pid')}",
                "factory": factory,
                "status": "running" if status == "running" else status,
                "raw_status": status,
                "mode": "supervisor",
                "requested_at": record.get("ts"),
                "started_at": record.get("ts"),
                "finished_at": None if status == "running" else record.get("ts"),
                "pid": item.get("pid"),
                "uptime_seconds": item.get("uptime_seconds"),
                "restart_count": item.get("restart_count"),
                "last_exit_code": item.get("last_exit_code"),
                "terminal": status != "running",
                "source": "supervisor_heartbeat",
            }
    return None


def _read_sidecar_status() -> dict[str, Any]:
    path = _sidecar_status_path()
    if not path.is_file():
        return {
            "name": "signal_tracker",
            "configured": False,
            "status": "missing",
            "required": False,
            "path": str(path),
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "name": "signal_tracker",
            "configured": True,
            "status": "invalid",
            "required": False,
            "path": str(path),
            "error": str(exc),
        }
    return {
        "name": "signal_tracker",
        "configured": True,
        "status": str(payload.get("status") or "unknown"),
        "required": False,
        "path": str(path),
        "payload": payload,
    }


async def list_factories() -> dict[str, Any]:
    return {
        "object": "aiask.factory.catalog",
        "contract_version": "aiask.factory_run.v1",
        "factories": [
            {"factory": name, **descriptor}
            for name, descriptor in FACTORY_DESCRIPTORS.items()
        ],
        "sidecars": ["signal_tracker"],
        "read_only": True,
    }


async def factory_runs(
    *,
    factory: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    db = get_db()
    await db.initialize()
    resolved_factory = normalize_factory_name(factory).value if factory else None
    effective_limit = max(1, min(int(limit or 20), 200))
    rows = await db.list_factory_runs(
        factory=resolved_factory,
        status=status,
        limit=effective_limit,
    )
    sources = ["factory_run_ledger"]
    if not rows and resolved_factory != FactoryName.STRATEGY.value:
        supervisor_rows = _supervisor_runs(resolved_factory)
        if supervisor_rows:
            rows.extend(supervisor_rows[:effective_limit])
            sources.append("supervisor_execution_records")
    if resolved_factory in {None, FactoryName.STRATEGY.value}:
        list_strategy_runs = getattr(db, "list_strategy_factory_runs", None)
        if callable(list_strategy_runs):
            strategy_rows = [
                mapped
                for mapped in (
                    _strategy_factory_run_row(row)
                    for row in await list_strategy_runs(limit=effective_limit)
                )
                if mapped
            ]
            status_token = str(status or "").strip().lower()
            if status_token:
                mapped_status = _STRATEGY_FACTORY_STATUS_MAP.get(status_token, status_token)
                strategy_rows = [
                    item
                    for item in strategy_rows
                    if item.get("raw_status") == status_token
                    or item.get("status") == mapped_status
                ]
            seen = {str(item.get("run_id") or "") for item in rows}
            rows.extend(
                item
                for item in strategy_rows
                if str(item.get("run_id") or "") not in seen
            )
            sources.append("strategy_factory_runs")
    rows.sort(
        key=lambda item: str(
            item.get("requested_at") or item.get("started_at") or item.get("created_at") or ""
        ),
        reverse=True,
    )
    rows = rows[:effective_limit]
    return {
        "object": "aiask.factory.runs",
        "factory": resolved_factory,
        "status_filter": status or None,
        "count": len(rows),
        "runs": rows,
        "sources": sources,
        "read_only": True,
    }


async def factory_run_detail(run_id: str) -> dict[str, Any]:
    resolved = str(run_id or "").strip()
    if not resolved:
        raise ValueError("run_id is required")
    db = get_db()
    await db.initialize()
    row = await db.get_factory_run(resolved)
    if row is None:
        get_strategy_run = getattr(db, "get_strategy_factory_run", None)
        if callable(get_strategy_run):
            row = _strategy_factory_run_row(await get_strategy_run(resolved))
    if row is None:
        row = next(
            (item for item in _supervisor_runs() if item.get("run_id") == resolved),
            None,
        )
    return {
        "object": "aiask.factory.run",
        "run_id": resolved,
        "found": row is not None,
        "run": row,
        "read_only": True,
    }


async def factory_status(factory: str) -> dict[str, Any]:
    resolved = normalize_factory_name(factory).value
    db = get_db()
    await db.initialize()
    latest = await db.get_latest_factory_run(resolved)
    active = await db.list_factory_runs(factory=resolved, status="running", limit=20)
    queued = await db.list_factory_runs(factory=resolved, status="queued", limit=20)
    source = "factory_run_ledger"
    if latest is None and resolved != FactoryName.STRATEGY.value:
        heartbeat = _supervisor_heartbeat(resolved)
        if heartbeat:
            latest = heartbeat
            source = "supervisor_heartbeat"
    if resolved == FactoryName.STRATEGY.value:
        list_strategy_runs = getattr(db, "list_strategy_factory_runs", None)
        get_latest_strategy_run = getattr(db, "get_latest_strategy_factory_run", None)
        if callable(get_latest_strategy_run):
            latest = _strategy_factory_run_row(await get_latest_strategy_run()) or latest
        if callable(list_strategy_runs):
            mapped_rows = [
                mapped
                for mapped in (
                    _strategy_factory_run_row(row)
                    for row in await list_strategy_runs(limit=20)
                )
                if mapped
            ]
            active = [item for item in mapped_rows if item.get("status") == "running"] or active
            queued = [item for item in mapped_rows if item.get("status") == "queued"] or queued
        source = "strategy_factory_runs"
    return {
        "object": "aiask.factory.status",
        "factory": resolved,
        "descriptor": dict(FACTORY_DESCRIPTORS[resolved]),
        "status": latest.get("status") if latest else "never_run",
        "latest_run": latest,
        "active_runs": active,
        "queued_runs": queued,
        "source": source,
        "read_only": True,
    }


async def factory_readiness(factory: str | None = None) -> dict[str, Any]:
    names = [normalize_factory_name(factory).value] if factory else list(FACTORY_DESCRIPTORS)
    root = _project_root()
    db = get_db()
    await db.initialize()
    db_status = dict(db.status())
    checks: list[dict[str, Any]] = []
    for name in names:
        descriptor = FACTORY_DESCRIPTORS[name]
        runner = root / descriptor["runner"]
        checks.append(
            {
                "factory": name,
                "runner": str(runner),
                "runner_available": runner.is_file(),
                "ready": runner.is_file() and bool(db_status.get("writable")),
            }
        )
    sidecar = _read_sidecar_status()
    blockers = [f"runner_missing:{item['factory']}" for item in checks if not item["runner_available"]]
    if not db_status.get("writable"):
        blockers.append("factory_ledger_not_writable")
    warnings = [] if sidecar["status"] not in {"missing", "invalid"} else ["signal_tracker_sidecar_missing"]
    return {
        "object": "aiask.factory.readiness",
        "factory": names[0] if factory else None,
        "ready": not blockers,
        "database": db_status,
        "checks": checks,
        "sidecars": [sidecar],
        "blockers": blockers,
        "warnings": warnings,
        "read_only": True,
    }


__all__ = [
    "FACTORY_DESCRIPTORS",
    "factory_readiness",
    "factory_run_detail",
    "factory_runs",
    "factory_status",
    "list_factories",
]
