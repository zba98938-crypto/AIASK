"""Read-only control-plane views for the four AIASK factories."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aiask_quant_core.storage import get_db
from strategy_factory.api import FactoryName, normalize_factory_name


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
    return Path(__file__).resolve().parents[5]


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
    rows = await db.list_factory_runs(
        factory=resolved_factory,
        status=status,
        limit=max(1, min(int(limit or 20), 200)),
    )
    return {
        "object": "aiask.factory.runs",
        "factory": resolved_factory,
        "status_filter": status or None,
        "count": len(rows),
        "runs": rows,
        "read_only": True,
    }


async def factory_run_detail(run_id: str) -> dict[str, Any]:
    resolved = str(run_id or "").strip()
    if not resolved:
        raise ValueError("run_id is required")
    db = get_db()
    await db.initialize()
    row = await db.get_factory_run(resolved)
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
    return {
        "object": "aiask.factory.status",
        "factory": resolved,
        "descriptor": dict(FACTORY_DESCRIPTORS[resolved]),
        "status": latest.get("status") if latest else "never_run",
        "latest_run": latest,
        "active_runs": active,
        "queued_runs": queued,
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
