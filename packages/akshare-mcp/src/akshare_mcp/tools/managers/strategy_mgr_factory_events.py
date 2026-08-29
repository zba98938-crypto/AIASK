"""Strategy Manager handlers for factory event injection (PR-2).

Actions:
  factory_event_create         — Create a new event injection
  factory_event_list           — List event injections (filterable)
  factory_event_update         — Update event (pause/expire/edit)
  factory_event_approve        — Approve a pending_review event
  factory_event_record_outcome — Record actual outcome after event expires
  factory_event_preview_tasks  — Preview tasks that would be generated (dry-run)
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

_OUTBOX_DRAIN_RUNNING = False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _string(value: Any) -> str:
    return str(value or "").strip()


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off", ""}:
        return False
    return default


async def handle_factory_event_create(db, params: dict[str, Any]) -> dict[str, Any]:
    """Create a new event injection."""
    event_name = _string(params.get("event_name"))
    event_type = _string(params.get("event_type"))
    if not event_name or not event_type:
        return {"success": False, "error": "event_name and event_type are required"}

    primary_themes = params.get("primary_themes") or []
    if not primary_themes:
        return {"success": False, "error": "primary_themes is required (at least one theme)"}

    confidence = float(params.get("confidence") or 0.7)
    intensity = float(params.get("intensity") or 0.5)

    # High intensity or market scope requires approval
    scope = _string(params.get("scope")) or "theme"
    require_approval = (
        intensity >= 0.8
        or scope == "market"
        or len(primary_themes) >= 10
    )
    initial_status = "pending_review" if require_approval else "active"
    if params.get("force_pending"):
        initial_status = "pending_review"

    event_id = _string(params.get("event_id")) or f"manual_{uuid4().hex[:12]}"
    valid_from = _string(params.get("valid_from")) or _now_iso()
    valid_until = _string(params.get("valid_until")) or ""

    # Default valid_until based on horizon
    if not valid_until:
        horizon = _string(params.get("horizon")) or "swing_5_20d"
        from datetime import timedelta
        horizon_days = {
            "intraday": 1,
            "swing_1_5d": 5,
            "swing_5_20d": 20,
            "macro_1m": 30,
            "macro_3m": 90,
        }.get(horizon, 20)
        try:
            start = datetime.fromisoformat(valid_from.replace("Z", "+00:00"))
        except Exception:
            start = datetime.now(timezone.utc)
        valid_until = (start + timedelta(days=horizon_days)).isoformat()

    payload = {
        "event_id": event_id,
        "source": _string(params.get("source") or params.get("event_source")) or "manual",
        "event_name": event_name,
        "event_type": event_type,
        "direction": params.get("direction"),
        "confidence": confidence,
        "intensity": intensity,
        "horizon": _string(params.get("horizon")) or "swing_5_20d",
        "scope": scope,
        "primary_themes": primary_themes,
        "rationale": params.get("rationale"),
        "evidence": params.get("evidence") or {},
        "valid_from": valid_from,
        "valid_until": valid_until,
        "status": initial_status,
        "operator_id": params.get("operator_id"),
    }

    result = await db.upsert_event_injection(payload)
    return {
        "success": True,
        "data": {
            "event_id": event_id,
            "status": initial_status,
            "require_approval": require_approval,
            "valid_from": valid_from,
            "valid_until": valid_until,
        },
    }


async def handle_factory_event_list(db, params: dict[str, Any]) -> dict[str, Any]:
    """List event injections with optional filters."""
    status = params.get("status")
    source = params.get("source") or params.get("event_source")
    limit = int(params.get("limit") or 50)

    events = await db.list_event_injections(
        status=status,
        source=source,
        limit=limit,
    )
    return {
        "success": True,
        "data": {
            "events": events,
            "count": len(events),
            "filters": {"status": status, "source": source},
        },
    }


async def handle_factory_event_update(db, params: dict[str, Any]) -> dict[str, Any]:
    """Update an event (pause, expire, edit fields)."""
    event_id = _string(params.get("event_id") or params.get("id"))
    if not event_id:
        return {"success": False, "error": "event_id is required"}

    action = _string(params.get("action"))
    if action == "pause":
        payload = {"event_id": event_id, "status": "paused"}
    elif action == "expire":
        payload = {"event_id": event_id, "status": "expired"}
    elif action == "activate":
        payload = {"event_id": event_id, "status": "active"}
    elif action == "reject":
        payload = {"event_id": event_id, "status": "rejected"}
    else:
        # General update — pass through allowed fields
        payload = {"event_id": event_id}
        for field in ("event_name", "event_type", "direction", "confidence",
                      "intensity", "horizon", "scope", "primary_themes",
                      "rationale", "evidence", "valid_from", "valid_until",
                      "status", "source"):
            if field in params:
                payload[field] = params[field]
        if "event_source" in params and "source" not in payload:
            payload["source"] = params["event_source"]

    patch_fields = {key: value for key, value in payload.items() if key != "event_id"}
    if hasattr(db, "patch_event_injection"):
        result = await db.patch_event_injection(event_id, patch_fields)
    else:
        result = await db.upsert_event_injection(payload)
    if result.get("missing"):
        return {"success": False, "error": f"Event {event_id} not found"}
    return {
        "success": True,
        "data": {
            "event_id": event_id,
            "updated": bool(result.get("updated", 1)),
            "patch": patch_fields,
        },
    }


async def handle_factory_event_approve(db, params: dict[str, Any]) -> dict[str, Any]:
    """Approve a pending_review event (dual-person review).

    PR-B1 (2026-05-24): approver_id / approved_at 必须真正写入 DB，
    不能像之前那样只在响应 envelope 里返回。DAO 端
    ``upsert_event_injection`` 已支持这两个列。
    """
    event_id = _string(params.get("event_id") or params.get("id"))
    approver_id = _string(params.get("approver_id"))
    if not event_id:
        return {"success": False, "error": "event_id is required"}

    # Fetch current event to verify it's pending
    events = await db.list_event_injections(status="pending_review", limit=200)
    target = next((e for e in events if e.get("event_id") == event_id), None)
    if target is None:
        return {"success": False, "error": f"Event {event_id} not found or not in pending_review status"}

    # Cannot self-approve
    operator = _string(target.get("operator_id"))
    if approver_id and operator and approver_id == operator:
        return {"success": False, "error": "Cannot self-approve: approver must differ from operator"}
    if not approver_id:
        return {"success": False, "error": "approver_id is required to approve a pending event"}

    approved_at = _now_iso()
    payload = {
        "event_id": event_id,
        "status": "active",
        "approver_id": approver_id,
        "approved_at": approved_at,
    }
    if hasattr(db, "patch_event_injection"):
        result = await db.patch_event_injection(event_id, {
            "status": "active",
            "approver_id": approver_id,
            "approved_at": approved_at,
        })
        if result.get("missing"):
            return {"success": False, "error": f"Event {event_id} not found"}
    else:
        await db.upsert_event_injection(payload)
    return {
        "success": True,
        "data": {
            "event_id": event_id,
            "status": "active",
            "approved_by": approver_id,
            "approved_at": approved_at,
        },
    }


async def handle_factory_event_record_outcome(db, params: dict[str, Any]) -> dict[str, Any]:
    """Record actual outcome after event expires."""
    event_id = _string(params.get("event_id") or params.get("id"))
    actual_outcome = _string(params.get("actual_outcome"))
    if not event_id or not actual_outcome:
        return {"success": False, "error": "event_id and actual_outcome are required"}

    if actual_outcome not in ("positive", "negative", "mixed", "no_effect"):
        return {"success": False, "error": f"actual_outcome must be one of: positive, negative, mixed, no_effect"}

    outcome_notes = params.get("outcome_notes")
    result = await db.update_event_outcome(
        event_id,
        actual_outcome=actual_outcome,
        outcome_notes=outcome_notes,
    )
    return {
        "success": True,
        "data": {
            "event_id": event_id,
            "actual_outcome": actual_outcome,
            "recorded_at": _now_iso(),
        },
    }


async def handle_factory_event_preview_tasks(db, params: dict[str, Any]) -> dict[str, Any]:
    """Preview tasks that would be generated from an event (dry-run, no side effects).

    PR-D (Phase 2, 2026-05-24): preview now drives the real BFS
    (``propagate_event_to_themes``) and target basket resolution
    (``resolve_target_basket``), so Desktop can show the propagation path
    and the candidate stock pool before the operator approves the event.
    The handler still degrades gracefully — if strategy-factory is not
    on the import path or the propagation modules raise, we fall back to
    the depth=1 edge listing so the manager keeps working.
    """
    event_id = _string(params.get("event_id") or params.get("id"))
    primary_themes = params.get("primary_themes") or []
    confidence = float(params.get("confidence") or 0.7)
    intensity = float(params.get("intensity") or 0.5)
    direction = params.get("direction")
    horizon = _string(params.get("horizon")) or "swing_5_20d"
    valid_from = _string(params.get("valid_from")) or None
    valid_until = _string(params.get("valid_until")) or None
    source = _string(params.get("source")) or "manual"
    event_type = _string(params.get("event_type"))

    if not primary_themes and event_id:
        # Try to load from DB
        events = await db.list_event_injections(limit=200)
        target = next((e for e in events if e.get("event_id") == event_id), None)
        if target:
            primary_themes = target.get("primary_themes") or []
            confidence = float(target.get("confidence") or confidence)
            intensity = float(target.get("intensity") or intensity)
            direction = target.get("direction") or direction
            horizon = _string(target.get("horizon")) or horizon
            valid_from = _string(target.get("valid_from")) or valid_from
            valid_until = _string(target.get("valid_until")) or valid_until
            source = _string(target.get("source")) or source
            event_type = _string(target.get("event_type")) or event_type

    if not primary_themes:
        return {"success": False, "error": "No primary_themes found for preview"}

    # PR-D: prefer the real propagation + target basket pipeline.
    try:
        from strategy_factory.api import (  # noqa: PLC0415
            NormalizedEvent,
            propagate_event_to_themes,
            resolve_target_basket,
        )
    except Exception as exc:
        # Degraded path: fall back to depth-1 edge listing if strategy-factory
        # cannot be imported (should not happen in production but guards
        # tests / partial deployments).
        return await _preview_tasks_legacy_depth1(
            db=db,
            event_id=event_id,
            primary_themes=primary_themes,
            confidence=confidence,
            intensity=intensity,
            direction=direction,
            fallback_reason=f"strategy_factory_unavailable: {exc}",
        )

    normalized_event = NormalizedEvent(
        event_id=event_id or f"preview_{uuid4().hex[:8]}",
        source=source,
        event_name=_string(params.get("event_name")) or "preview_event",
        event_type=event_type,
        direction=direction,
        confidence=confidence,
        intensity=intensity,
        horizon=horizon,
        scope=_string(params.get("scope")) or "theme",
        primary_themes=list(primary_themes),
        valid_from=valid_from,
        valid_until=valid_until,
    )

    impacts, warnings = await propagate_event_to_themes(
        db,
        normalized_event,
        return_warnings=True,
    )

    # Per-theme basket preview (capped to 8 symbols per theme so the
    # response stays Desktop-friendly; the real factory_run_once will
    # resolve full target_count later).
    impact_payload: list[dict[str, Any]] = []
    candidate_symbols: list[str] = []
    candidate_seen: set[str] = set()
    for impact in impacts:
        try:
            basket = await resolve_target_basket(
                db,
                impact,
                target_count=8,
                feature_flag_target_max=12,
            )
        except Exception as exc:
            basket = None
            warnings.append({
                "type": "target_basket_failed",
                "theme_code": impact.theme_code,
                "error": str(exc),
            })

        item: dict[str, Any] = {
            "theme_code": impact.theme_code,
            "direction_sign": impact.direction_sign,
            "magnitude": round(impact.magnitude, 4),
            "confidence": round(impact.confidence, 4),
            "depth": impact.depth,
            "breadth": impact.breadth,
            "lag_days": impact.lag_days,
            "source_path": impact.source_path,
        }
        if basket is not None:
            item["candidate_symbols"] = list(basket.symbols)
            item["candidate_count"] = len(basket.symbols)
            item["basket_evidence"] = basket.evidence
            for sym in basket.symbols:
                if sym not in candidate_seen:
                    candidate_seen.add(sym)
                    candidate_symbols.append(sym)
        else:
            item["candidate_symbols"] = []
            item["candidate_count"] = 0
        impact_payload.append(item)

    return {
        "success": True,
        "data": {
            "event_id": event_id,
            "primary_theme_count": len(primary_themes),
            "total_impacts": len(impact_payload),
            "impacts": impact_payload,
            "candidate_symbols": candidate_symbols,
            "candidate_count": len(candidate_symbols),
            "warnings": warnings,
            "preview_mode": "real_propagation_v1",
        },
    }


async def handle_factory_event_lineage(db, params: dict[str, Any]) -> dict[str, Any]:
    """Read event -> task -> gate lineage from persisted storage."""

    event_id = _string(params.get("event_id") or params.get("id"))
    task_id = _string(params.get("task_id"))
    limit = max(1, min(int(params.get("limit") or 100), 1000))
    if not hasattr(db, "list_event_task_lineage"):
        return {"success": False, "error": "lineage DAO unsupported"}
    rows = await db.list_event_task_lineage(
        event_id=event_id or None,
        task_id=task_id or None,
        limit=limit,
    )
    return {
        "success": True,
        "data": {
            "lineage": rows,
            "items": rows,
            "count": len(rows),
            "filters": {"event_id": event_id or None, "task_id": task_id or None},
            "source": "strategy_factory_event_task_lineage",
        },
    }


async def handle_factory_theme_exposure_status(db, params: dict[str, Any]) -> dict[str, Any]:
    """Read aggregate status of the TDX-only theme exposure matrix."""

    if hasattr(db, "get_theme_exposure_status"):
        status = await db.get_theme_exposure_status()
    else:
        rows = await db.list_theme_exposure(limit=1) if hasattr(db, "list_theme_exposure") else []
        status = {
            "row_count": len(rows),
            "symbol_count": None,
            "theme_count": None,
            "latest_updated_at": None,
            "source": "strategy_factory_theme_exposure",
            "unsupported": not bool(rows),
        }
    return {"success": True, "data": status}


async def handle_factory_event_outbox_status(db, params: dict[str, Any]) -> dict[str, Any]:
    """Read outbox consumer state."""

    limit = max(1, min(int(params.get("limit") or 50), 200))
    if hasattr(db, "get_event_outbox_status"):
        status = await db.get_event_outbox_status(limit=limit)
    elif hasattr(db, "list_event_outbox_state"):
        rows = await db.list_event_outbox_state(limit=limit)
        counts: dict[str, int] = {}
        for row in rows:
            key = str(row.get("status") or "unknown")
            counts[key] = counts.get(key, 0) + 1
        status = {"source": "strategy_factory_event_outbox_state", "counts": counts, "latest": rows}
    else:
        return {"success": False, "error": "outbox DAO unsupported"}
    return {"success": True, "data": status}


async def handle_factory_theme_exposure_refresh(db, params: dict[str, Any]) -> dict[str, Any]:
    """Confirm-required manual refresh for the TDX-only exposure matrix."""

    try:
        from strategy_factory.api import (  # noqa: PLC0415
            ThemeExposureBuilder,
        )
    except Exception as exc:
        return {"success": False, "error": f"strategy_factory unavailable: {exc}"}

    builder = ThemeExposureBuilder(
        stock_limit=int(params.get("stock_limit") or 0) or None,
        theme_limit=int(params.get("theme_limit") or 0) or None,
        batch_size=max(1, min(int(params.get("batch_size") or 1000), 10000)),
    )
    report = await builder.build(
        db,
        batch_size=max(1, min(int(params.get("batch_size") or 1000), 10000)),
    )
    return {"success": True, "data": report}


async def handle_factory_event_bootstrap(db, params: dict[str, Any]) -> dict[str, Any]:
    """One-click bootstrap for the default theme graph and exposure matrix."""

    try:
        from strategy_factory.api import (  # noqa: PLC0415
            ThemeExposureBuilder,
            seed_default_theme_graph,
        )
    except Exception as exc:
        return {"success": False, "error": f"strategy_factory unavailable: {exc}"}

    seed_report = await seed_default_theme_graph(
        db,
        overwrite=bool(params.get("overwrite_seed") or False),
        updated_by=_string(params.get("operator_id")) or "factory_event_bootstrap",
    )

    exposure_report: dict[str, Any] | None = None
    if bool(params.get("refresh_exposure", True)):
        batch_size = max(1, min(int(params.get("batch_size") or 1000), 10000))
        exposure_report = await ThemeExposureBuilder(
            stock_limit=int(params.get("stock_limit") or 0) or None,
            theme_limit=int(params.get("theme_limit") or 0) or None,
            batch_size=batch_size,
        ).build(db, batch_size=batch_size)

    if hasattr(db, "get_theme_exposure_status"):
        exposure_status = await db.get_theme_exposure_status()
    elif hasattr(db, "list_theme_exposure"):
        exposure_status = {"row_count": len(await db.list_theme_exposure(limit=1))}
    else:
        exposure_status = {}

    node_count = int(seed_report.get("node_count") or 0)
    edge_count = int(seed_report.get("edge_count") or 0)
    row_count = int(
        exposure_status.get("row_count")
        or (exposure_report or {}).get("rows_written")
        or 0
    )
    return {
        "success": True,
        "data": {
            "status": "completed",
            "seed": seed_report,
            "exposure_refresh": exposure_report,
            "counts": {
                "theme_nodes": node_count,
                "theme_edges": edge_count,
                "theme_exposure_rows": row_count,
                "theme_count": exposure_status.get("theme_count") or (exposure_report or {}).get("theme_count"),
                "symbol_count": exposure_status.get("symbol_count") or (exposure_report or {}).get("symbol_count"),
            },
            "availability": {
                "theme_graph_available": node_count > 0 and edge_count > 0,
                "theme_exposure_available": row_count > 0,
            },
        },
    }


async def handle_factory_event_outbox_drain(db, params: dict[str, Any]) -> dict[str, Any]:
    """Single-worker outbox drain for event-driven task lineage.

    The generator is called in non-persisting mode; each candidate task is
    claimed by dedupe_key before lineage is written. Duplicate or terminal
    dedupe slots are skipped.
    """

    global _OUTBOX_DRAIN_RUNNING
    if _OUTBOX_DRAIN_RUNNING:
        return {
            "success": False,
            "error": "factory_event_outbox_drain already running",
            "data": {"single_worker": True},
        }

    limit = max(1, min(int(params.get("limit") or 20), 200))
    event_limit = max(1, min(int(params.get("event_limit") or 20), 200))
    _OUTBOX_DRAIN_RUNNING = True
    try:
        try:
            from strategy_factory.api import (  # noqa: PLC0415
                generate_tasks_from_active_events,
            )
        except Exception as exc:
            return {"success": False, "error": f"strategy_factory unavailable: {exc}"}

        generated = await generate_tasks_from_active_events(
            db,
            snapshot=params.get("snapshot") if isinstance(params.get("snapshot"), dict) else None,
            force_enabled=True,
            persist_lineage=False,
            event_limit=event_limit,
        )
        tasks = list(generated.get("tasks") or [])
        lineage_records = list(generated.get("lineage_records") or [])
        lineage_by_key = {
            str(item.get("dedupe_key") or "").strip(): dict(item or {})
            for item in lineage_records
            if str(item.get("dedupe_key") or "").strip()
        }

        processed = 0
        skipped = 0
        failed = 0
        claimed = 0
        details: list[dict[str, Any]] = []

        for task in tasks[:limit]:
            context = dict(task.get("event_context") or {})
            dedupe_key = _string(context.get("dedupe_key"))
            if not dedupe_key:
                skipped += 1
                details.append({"status": "skipped", "reason": "missing_dedupe_key"})
                continue
            record = lineage_by_key.get(dedupe_key)
            if not record:
                skipped += 1
                details.append({"dedupe_key": dedupe_key, "status": "skipped", "reason": "missing_lineage_record"})
                continue
            payload = {
                "dedupe_key": dedupe_key,
                "source_event_id": record.get("event_id") or context.get("event_id"),
                "theme_code": record.get("theme_code") or task.get("candidate_family"),
                "event_type": context.get("event_type") or task.get("opportunity_type"),
            }
            try:
                claim = await db.claim_event_outbox(payload)
                if not claim.get("claimed"):
                    skipped += 1
                    details.append({
                        "dedupe_key": dedupe_key,
                        "status": "skipped",
                        "claim_status": claim.get("status"),
                    })
                    continue
                claimed += 1
                await db.upsert_event_task_lineage(record)
                await db.mark_event_outbox_processed(dedupe_key)
                processed += 1
                details.append({
                    "dedupe_key": dedupe_key,
                    "status": "processed",
                    "event_id": record.get("event_id"),
                    "task_id": record.get("task_id"),
                    "theme_code": record.get("theme_code"),
                })
            except Exception as exc:
                failed += 1
                try:
                    await db.mark_event_outbox_failed(dedupe_key, error=str(exc))
                except Exception:
                    pass
                details.append({"dedupe_key": dedupe_key, "status": "failed", "error": str(exc)})

            await asyncio.sleep(0)

        return {
            "success": True,
            "data": {
                "status": "completed",
                "single_worker": True,
                "generated_task_count": len(tasks),
                "candidate_lineage_count": len(lineage_records),
                "processed": processed,
                "claimed": claimed,
                "skipped": skipped,
                "failed": failed,
                "limit": limit,
                "details": details,
                "generator": {
                    key: value
                    for key, value in dict(generated or {}).items()
                    if key not in {"tasks", "lineage_records"}
                },
            },
        }
    finally:
        _OUTBOX_DRAIN_RUNNING = False


async def handle_factory_theme_regression_run(db, params: dict[str, Any]) -> dict[str, Any]:
    """Confirm-required manual run for theme-response regression."""

    try:
        from strategy_factory.api.runtime import create_theme_response_regression  # noqa: PLC0415
    except Exception as exc:
        return {"success": False, "error": f"strategy_factory unavailable: {exc}"}

    model = create_theme_response_regression()
    report = await model.run_full_update(
        db,
        apply_updates=_bool(params.get("apply_updates"), False),
    )
    return {"success": True, "data": report}


async def _preview_tasks_legacy_depth1(
    *,
    db,
    event_id: str,
    primary_themes: list,
    confidence: float,
    intensity: float,
    direction: Any,
    fallback_reason: str,
) -> dict[str, Any]:
    """Legacy depth=1 preview, kept only as a graceful-degradation fallback."""

    preview_impacts: list[dict[str, Any]] = []
    for pt in primary_themes:
        theme_code = pt.get("theme_code") if isinstance(pt, dict) else str(pt)
        pt_direction = (pt.get("direction") if isinstance(pt, dict) else direction) or "positive"
        dir_sign = 1 if pt_direction == "positive" else (-1 if pt_direction == "negative" else 0)

        preview_impacts.append({
            "theme_code": theme_code,
            "direction_sign": dir_sign,
            "magnitude": intensity,
            "confidence": confidence,
            "depth": 0,
            "source_path": "primary",
        })

        edges = await db.list_theme_edges(source=theme_code, is_active=True, limit=50)
        for edge in edges:
            new_dir = dir_sign * int(edge.get("direction_sign") or 1)
            new_mag = intensity * float(edge.get("magnitude_factor") or 0.5)
            new_conf = confidence * float(edge.get("confidence") or 0.5)
            if new_mag < 0.15 or new_conf < 0.25:
                continue
            preview_impacts.append({
                "theme_code": edge.get("target_theme_code"),
                "direction_sign": new_dir,
                "magnitude": round(new_mag, 4),
                "confidence": round(new_conf, 4),
                "depth": 1,
                "source_path": f"{theme_code} → {edge.get('relation_type')}",
                "lag_days": int(edge.get("lag_days") or 0),
            })

    return {
        "success": True,
        "data": {
            "event_id": event_id,
            "primary_theme_count": len(primary_themes),
            "total_impacts": len(preview_impacts),
            "impacts": preview_impacts,
            "candidate_symbols": [],
            "candidate_count": 0,
            "warnings": [{
                "type": "preview_degraded",
                "reason": fallback_reason,
            }],
            "preview_mode": "legacy_depth1",
        },
    }
