"""Event-driven task generator (PR-6).

Bridges the event injection system (PR-2) with the factory's existing
task generation pipeline. Reads active events, propagates through the
theme graph, resolves target baskets, and produces research tasks
compatible with the existing `_build_event_driven_tasks` output format.

This module is called by MarketOpportunityScanner.scan() when
STRATEGY_FACTORY_MANUAL_EVENT_ENABLED=true.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .theme_graph import NormalizedEvent, ThemeImpact, propagate_event_to_themes
from .target_basket import TargetBasket, resolve_target_basket, resolve_target_count

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.getenv(name) or "").strip().lower()
    return raw in ("1", "true", "yes", "on") if raw else default


MANUAL_EVENT_ENABLED = _env_bool("STRATEGY_FACTORY_MANUAL_EVENT_ENABLED", False)
THEME_GRAPH_ENABLED = _env_bool("STRATEGY_FACTORY_THEME_GRAPH_ENABLED", False)
DYNAMIC_TARGET_COUNT_ENABLED = _env_bool("STRATEGY_FACTORY_DYNAMIC_TARGET_COUNT_ENABLED", False)
TARGET_COUNT_MAX = int(os.getenv("STRATEGY_FACTORY_TARGET_COUNT_MAX") or 30)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_symbols(task: dict[str, Any]) -> list[str]:
    symbols = task.get("target_symbols")
    if not symbols and isinstance(task.get("stock_pool"), dict):
        symbols = dict(task.get("stock_pool") or {}).get("symbols")
    values: list[str] = []
    for item in list(symbols or []):
        token = str(item or "").strip()
        if token and token not in values:
            values.append(token)
        if len(values) >= 12:
            break
    return values


def _task_event_context(task: dict[str, Any]) -> dict[str, Any]:
    context = dict(task.get("event_context") or {})
    if not context and isinstance(task.get("research_task"), dict):
        context = dict((task.get("research_task") or {}).get("event_context") or {})
    return context


def _task_dedupe_key(task: dict[str, Any]) -> str:
    context = _task_event_context(task)
    existing = str(context.get("dedupe_key") or "").strip()
    if existing:
        return existing
    event_id = str(context.get("event_id") or task.get("event_id") or "").strip()
    theme_code = str(
        context.get("theme_code")
        or task.get("theme_code")
        or task.get("candidate_family")
        or task.get("factor_name")
        or ""
    ).strip()
    symbols_signature = "-".join(_task_symbols(task))[:64]
    if not event_id or not theme_code or not symbols_signature:
        return ""
    return f"{event_id}:{theme_code}:{symbols_signature}"


def _direction_to_lineage(value: Any) -> str:
    token = str(value or "").strip().lower()
    if token in {"negative", "bearish", "cost_up", "down"}:
        return "negative"
    if token in {"neutral", "flat"}:
        return "neutral"
    return "positive"


def _lineage_record_from_task(task: dict[str, Any]) -> dict[str, Any] | None:
    context = _task_event_context(task)
    dedupe_key = _task_dedupe_key(task)
    event_id = str(context.get("event_id") or task.get("event_id") or "").strip()
    theme_code = str(
        context.get("theme_code")
        or task.get("theme_code")
        or task.get("candidate_family")
        or task.get("factor_name")
        or ""
    ).strip()
    symbols = _task_symbols(task)
    if not dedupe_key or not event_id or not theme_code or not symbols:
        return None
    score_summary = dict((task.get("evidence_bundle") or {}).get("score_summary") or {})
    impact_magnitude = (
        score_summary.get("avg_final_score")
        or context.get("reliability_score")
        or task.get("reliability_score")
        or 0.0
    )
    try:
        magnitude = float(impact_magnitude or 0.0)
    except Exception:
        magnitude = 0.0
    return {
        "dedupe_key": dedupe_key,
        "event_id": event_id,
        "task_id": task.get("task_id"),
        "theme_code": theme_code,
        "impact_direction": _direction_to_lineage(context.get("direction") or task.get("direction")),
        "impact_magnitude": magnitude,
        "target_symbols": symbols,
        "target_count": len(symbols),
        "breadth_resolved": "verified_event_signal",
    }


def _with_dedupe_context(task: dict[str, Any]) -> dict[str, Any]:
    payload = dict(task or {})
    dedupe_key = _task_dedupe_key(payload)
    if not dedupe_key:
        return payload
    event_context = dict(payload.get("event_context") or {})
    event_context["dedupe_key"] = dedupe_key
    payload["event_context"] = event_context
    if isinstance(payload.get("research_task"), dict):
        research_task = dict(payload.get("research_task") or {})
        research_context = dict(research_task.get("event_context") or {})
        research_context["dedupe_key"] = dedupe_key
        research_task["event_context"] = research_context
        payload["research_task"] = research_task
    return payload


async def _generate_tasks_from_verified_event_clusters(
    db: Any,
    snapshot: dict[str, Any] | None,
    *,
    event_limit: int,
) -> dict[str, Any]:
    if not callable(getattr(db, "list_factory_event_clusters", None)):
        return {
            "enabled": False,
            "tasks": [],
            "lineage_records": [],
            "reason": "factory_event_clusters_unavailable",
        }
    try:
        from ..collect import DataCollector
        from ..opportunity import MarketOpportunityScanner

        event_payload, status, reason, details = await DataCollector._collect_event_driven_snapshot(db)
        event_payload = dict(event_payload or {})
        events = list(event_payload.get("events") or [])[: max(1, int(event_limit or 20))]
        if not events:
            return {
                "enabled": True,
                "tasks": [],
                "lineage_records": [],
                "event_count": 0,
                "event_snapshot_status": status,
                "event_snapshot_reason": reason,
                "event_snapshot_details": dict(details or {}) if isinstance(details, dict) else details,
                "diagnostic_event_count": len(list(event_payload.get("diagnostic_events") or [])),
                "source": "verified_event_clusters",
            }
        task_snapshot = {
            **dict(snapshot or {}),
            "event_driven": {
                **event_payload,
                "events": events,
            },
        }
        raw_tasks = MarketOpportunityScanner._build_event_driven_tasks(task_snapshot, [])
        tasks = [
            _with_dedupe_context(task)
            for task in MarketOpportunityScanner._deduplicate_tasks(list(raw_tasks or []))
        ]
        lineage_records = [
            record for record in (_lineage_record_from_task(task) for task in tasks) if record
        ]
        return {
            "enabled": True,
            "tasks": tasks,
            "lineage_records": lineage_records,
            "event_count": len(events),
            "impact_count": len(tasks),
            "task_count": len(tasks),
            "lineage_count": len(lineage_records),
            "event_snapshot_status": status,
            "event_snapshot_reason": reason,
            "event_snapshot_details": dict(details or {}) if isinstance(details, dict) else details,
            "source": "verified_event_clusters",
        }
    except Exception as exc:
        logger.debug("event_task_generator: verified event cluster fallback failed: %s", exc)
        return {
            "enabled": False,
            "tasks": [],
            "lineage_records": [],
            "reason": f"{type(exc).__name__}: {exc}",
            "source": "verified_event_clusters",
        }


async def _claim_fallback_outbox(
    db: Any,
    tasks: list[dict[str, Any]],
    lineage_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    lineage_by_key = {
        str(item.get("dedupe_key") or "").strip(): dict(item or {})
        for item in lineage_records
        if str(item.get("dedupe_key") or "").strip()
    }
    outbox_claimed = 0
    outbox_processed = 0
    outbox_skipped = 0
    outbox_failed = 0
    outbox_details: list[dict[str, Any]] = []
    emitted_tasks: list[dict[str, Any]] = []
    emitted_lineage: list[dict[str, Any]] = []
    required_methods = (
        "claim_event_outbox",
        "upsert_event_task_lineage",
        "mark_event_outbox_processed",
        "mark_event_outbox_failed",
    )
    if not all(hasattr(db, name) for name in required_methods):
        for task in tasks:
            dedupe_key = _task_dedupe_key(task)
            outbox_failed += 1
            outbox_details.append({
                "dedupe_key": dedupe_key,
                "status": "failed",
                "reason": "outbox_dao_unsupported",
            })
        return emitted_tasks, emitted_lineage, {
            "outbox_claimed": outbox_claimed,
            "outbox_processed": outbox_processed,
            "outbox_skipped": outbox_skipped,
            "outbox_failed": outbox_failed,
            "outbox_details": outbox_details,
        }
    for task in tasks:
        dedupe_key = _task_dedupe_key(task)
        record = lineage_by_key.get(dedupe_key)
        if not dedupe_key or not record:
            outbox_skipped += 1
            outbox_details.append({
                "dedupe_key": dedupe_key,
                "status": "skipped",
                "reason": "missing_lineage_record",
            })
            continue
        context = _task_event_context(task)
        try:
            claim = await db.claim_event_outbox({
                "dedupe_key": dedupe_key,
                "source_event_id": record.get("event_id") or context.get("event_id"),
                "theme_code": record.get("theme_code") or task.get("candidate_family"),
                "event_type": context.get("event_type") or task.get("event_type") or task.get("opportunity_type"),
            })
            if not claim.get("claimed"):
                outbox_skipped += 1
                outbox_details.append({
                    "dedupe_key": dedupe_key,
                    "status": "skipped",
                    "claim_status": claim.get("status"),
                })
                continue
            outbox_claimed += 1
            await db.upsert_event_task_lineage(record)
            await db.mark_event_outbox_processed(dedupe_key)
            outbox_processed += 1
            outbox_details.append({
                "dedupe_key": dedupe_key,
                "status": "processed",
                "event_id": record.get("event_id"),
                "task_id": record.get("task_id"),
                "theme_code": record.get("theme_code"),
            })
            emitted_tasks.append(task)
            emitted_lineage.append(record)
        except Exception as exc:
            outbox_failed += 1
            try:
                await db.mark_event_outbox_failed(dedupe_key, error=str(exc))
            except Exception:
                pass
            outbox_details.append({
                "dedupe_key": dedupe_key,
                "status": "failed",
                "error": str(exc),
            })
    return emitted_tasks, emitted_lineage, {
        "outbox_claimed": outbox_claimed,
        "outbox_processed": outbox_processed,
        "outbox_skipped": outbox_skipped,
        "outbox_failed": outbox_failed,
        "outbox_details": outbox_details,
    }


def _build_event_task(
    event: NormalizedEvent,
    impact: ThemeImpact,
    symbols: list[str],
    *,
    event_source: str = "manual",
    dedupe_key: str | None = None,
) -> dict[str, Any]:
    """Build a research task dict compatible with existing factory pipeline.

    PR-C (Phase 1, 2026-05-24): all event-driven tasks unified under
    ``task_source="event_driven"``; the 5 origins (manual / news_llm /
    macro_shock / market_anomaly / price_inference) are distinguished by
    ``event_source``. Downstream code that previously branched on
    ``manual_event``/``auto_event`` must read ``event_source`` instead.

    Output format matches `_opportunity_event.py:_finalize_task()`.
    """
    direction = "bullish" if impact.direction_sign > 0 else "bearish" if impact.direction_sign < 0 else "neutral"

    short_window = len(symbols) <= 5
    validation_focus = "event_target_only" if short_window else "target_plus_representative"
    target_symbol_policy = "event_target_only" if short_window else "target_plus_representative"

    event_context: dict[str, Any] = {
        "event_id": event.event_id,
        "event_name": event.event_name,
        "event_type": event.event_type,
        "event_source": event_source,
        "direction": direction,
        "confidence": round(impact.confidence, 4),
        "intensity": round(impact.magnitude, 4),
        "horizon": event.horizon,
        "lag_days": impact.lag_days,
        "propagation_depth": impact.depth,
        "source_path": impact.source_path,
        # PR-C optional metadata. None values are acceptable for backwards
        # compatibility but downstream Phase 4/5 (Desktop preview, lineage)
        # is expected to fill them in.
        "valid_from": event.valid_from if hasattr(event, "valid_from") else None,
        "valid_until": event.valid_until if hasattr(event, "valid_until") else None,
        "dedupe_key": dedupe_key,
    }

    research_task: dict[str, Any] = {
        # PR-C: unified task_source + new event_source field
        "task_source": "event_driven",
        "event_source": event_source,
        "opportunity_type": f"event_{event.event_type}",
        "preferred_strategy_types": _preferred_types_for_direction(direction),
        "target_symbols": symbols,
        "target_symbol_policy": target_symbol_policy,
        "validation_focus": validation_focus,
        "event_window": {
            "horizon": event.horizon,
            "lag_days": impact.lag_days,
        },
    }

    return {
        "task_id": f"event_{event.event_id}_{impact.theme_code}_{uuid4().hex[:8]}",
        "task_key": f"event:{event.event_id}:{impact.theme_code}",
        # PR-C: top-level task_source unified
        "task_source": "event_driven",
        "event_source": event_source,
        "opportunity_type": f"event_{event.event_type}",
        "candidate_family": impact.theme_code,
        "factor_name": impact.theme_code,
        "generation_limit": 4,
        "target_symbols": symbols,
        "stock_pool": {
            "selection_mode": "event_theme_exposure",
            "symbols": symbols,
            "theme_code": impact.theme_code,
        },
        "event_context": event_context,
        "research_task": research_task,
        "priority": _compute_task_priority(event, impact),
    }


_VALID_EVENT_SOURCES = {
    "manual",
    "news_llm",
    "macro_shock",
    "market_anomaly",
    "price_inference",
}


def _normalize_event_source(raw: str | None) -> str:
    """Map a `NormalizedEvent.source` string to the canonical event_source enum.

    Defaults to ``"manual"`` when the input is empty so that legacy events
    that pre-date the event_source field still get a sensible bucket.
    """
    token = str(raw or "").strip().lower()
    if token in _VALID_EVENT_SOURCES:
        return token
    # Common legacy aliases
    if token in ("auto", "automated"):
        return "price_inference"
    if token in ("news", "headline"):
        return "news_llm"
    if token in ("macro", "shock", "macro_event"):
        return "macro_shock"
    if token in ("anomaly", "market"):
        return "market_anomaly"
    return "manual"


def _preferred_types_for_direction(direction: str) -> list[str]:
    """Map event direction to preferred strategy types."""
    if direction == "bullish":
        return ["momentum", "event_structure_breakout", "ma_cross"]
    if direction == "bearish":
        return ["mean_reversion_short", "rsi", "gap_fill"]
    return ["multi_factor", "macro_timing"]


def _compute_task_priority(event: NormalizedEvent, impact: ThemeImpact) -> int:
    """Compute task priority (lower = higher priority)."""
    base = 50
    # Manual events get higher priority
    if event.source == "manual":
        base -= 20
    # Higher confidence/intensity = higher priority
    evidence = impact.confidence * 0.5 + impact.magnitude * 0.5
    base -= int(evidence * 20)
    # Deeper propagation = lower priority
    base += impact.depth * 5
    return max(1, min(100, base))


async def generate_tasks_from_active_events(
    db: Any,
    snapshot: dict[str, Any] | None = None,
    *,
    force_enabled: bool = False,
    persist_lineage: bool = True,
    claim_outbox: bool = False,
    event_limit: int = 20,
) -> dict[str, Any]:
    """Main entry point: load active events, propagate, generate tasks.

    Called by MarketOpportunityScanner.scan() when manual events are enabled.

    Returns:
        Dict with 'tasks' list and metadata.
    """
    if not force_enabled and not MANUAL_EVENT_ENABLED:
        return {"enabled": False, "tasks": [], "reason": "MANUAL_EVENT_ENABLED=false"}

    if not force_enabled and not THEME_GRAPH_ENABLED:
        return {"enabled": False, "tasks": [], "reason": "THEME_GRAPH_ENABLED=false"}

    # Load active events
    events_raw = []
    if hasattr(db, "list_event_injections"):
        events_raw = await db.list_event_injections(status="active", limit=max(1, int(event_limit or 20)))

    if not events_raw:
        fallback = await _generate_tasks_from_verified_event_clusters(
            db,
            snapshot,
            event_limit=max(1, int(event_limit or 20)),
        )
        fallback_tasks = list(fallback.get("tasks") or [])
        fallback_lineage = list(fallback.get("lineage_records") or [])
        outbox_meta = {
            "outbox_claimed": 0,
            "outbox_processed": 0,
            "outbox_skipped": 0,
            "outbox_failed": 0,
            "outbox_details": [],
        }
        if fallback_tasks and claim_outbox:
            fallback_tasks, fallback_lineage, outbox_meta = await _claim_fallback_outbox(
                db,
                fallback_tasks,
                fallback_lineage,
            )
        elif fallback_lineage and persist_lineage and hasattr(db, "upsert_event_task_lineage"):
            persisted_lineage: list[dict[str, Any]] = []
            for record in fallback_lineage:
                try:
                    await db.upsert_event_task_lineage(record)
                    persisted_lineage.append(record)
                except Exception as exc:
                    logger.debug("event_task_generator: fallback lineage persist failed: %s", exc)
            fallback_lineage = persisted_lineage
        if fallback_tasks or fallback.get("enabled"):
            return {
                "enabled": True,
                "tasks": fallback_tasks,
                "event_count": int(fallback.get("event_count") or 0),
                "impact_count": len(fallback_tasks),
                "task_count": len(fallback_tasks),
                "lineage_count": len(fallback_lineage),
                "lineage_records": fallback_lineage,
                "persist_lineage": bool(persist_lineage),
                "claim_outbox": bool(claim_outbox),
                "event_source_mode": "verified_event_clusters",
                "active_injection_event_count": 0,
                "verified_event_fallback": {
                    key: value
                    for key, value in dict(fallback or {}).items()
                    if key not in {"tasks", "lineage_records"}
                },
                **outbox_meta,
                "feature_flag_target_max": TARGET_COUNT_MAX if DYNAMIC_TARGET_COUNT_ENABLED else 12,
            }
        return {"enabled": True, "tasks": [], "event_count": 0, "event_source_mode": "event_injections"}

    # Filter to currently valid events
    now = _now_iso()
    active_events = []
    for raw in events_raw:
        valid_until = str(raw.get("valid_until") or "").strip()
        if valid_until and valid_until < now:
            continue
        active_events.append(NormalizedEvent.from_dict(raw))

    if not active_events:
        return {"enabled": True, "tasks": [], "event_count": 0, "all_expired": True}

    # Generate tasks for each event
    all_tasks: list[dict[str, Any]] = []
    lineage_records: list[dict[str, Any]] = []
    outbox_claimed = 0
    outbox_processed = 0
    outbox_skipped = 0
    outbox_failed = 0
    outbox_details: list[dict[str, Any]] = []
    feature_flag_max = TARGET_COUNT_MAX if DYNAMIC_TARGET_COUNT_ENABLED else 12

    for event in active_events:
        # PR-C: derive canonical event_source enum from raw event payload
        event_source = _normalize_event_source(event.source)

        # Propagate through theme graph
        impacts = await propagate_event_to_themes(db, event)

        for impact in impacts:
            # Resolve target basket
            basket = await resolve_target_basket(
                db, impact,
                feature_flag_target_max=feature_flag_max,
            )

            if not basket.symbols:
                continue

            # Determine target count.
            # PR-D (Phase 2): canonical task_source is now "event_driven";
            # the resolver still understands legacy "manual_event" /
            # "auto_event" but new code paths feed the unified value.
            target_count = resolve_target_count(
                confidence=impact.confidence,
                intensity=impact.magnitude,
                theme_breadth=impact.breadth,
                task_source="event_driven",
                feature_flag_target_max=feature_flag_max,
            )

            symbols = basket.symbols[:target_count]
            if not symbols:
                continue

            # PR-C: stable dedupe_key for outbox / scan idempotency.
            # Format: event_id:theme_code:N-symbols-checksum.
            symbols_signature = "-".join(symbols)[:64]
            dedupe_key = f"{event.event_id}:{impact.theme_code}:{symbols_signature}"

            # Build task with unified task_source="event_driven" + event_source
            task = _build_event_task(
                event,
                impact,
                symbols,
                event_source=event_source,
                dedupe_key=dedupe_key,
            )
            # Record lineage
            lineage_record = {
                "dedupe_key": dedupe_key,
                "event_id": event.event_id,
                "task_id": task["task_id"],
                "theme_code": impact.theme_code,
                "impact_direction": "positive" if impact.direction_sign > 0 else "negative",
                "impact_magnitude": impact.magnitude,
                "target_symbols": symbols,
                "target_count": len(symbols),
                "breadth_resolved": impact.breadth,
            }

            if claim_outbox:
                required_methods = (
                    "claim_event_outbox",
                    "upsert_event_task_lineage",
                    "mark_event_outbox_processed",
                    "mark_event_outbox_failed",
                )
                if not all(hasattr(db, name) for name in required_methods):
                    outbox_failed += 1
                    outbox_details.append({
                        "dedupe_key": dedupe_key,
                        "status": "failed",
                        "reason": "outbox_dao_unsupported",
                    })
                    continue

                claim_payload = {
                    "dedupe_key": dedupe_key,
                    "source_event_id": event.event_id,
                    "theme_code": impact.theme_code,
                    "event_type": event.event_type,
                }
                try:
                    claim = await db.claim_event_outbox(claim_payload)
                    if not claim.get("claimed"):
                        outbox_skipped += 1
                        outbox_details.append({
                            "dedupe_key": dedupe_key,
                            "status": "skipped",
                            "claim_status": claim.get("status"),
                        })
                        continue
                    outbox_claimed += 1
                    await db.upsert_event_task_lineage(lineage_record)
                    await db.mark_event_outbox_processed(dedupe_key)
                    outbox_processed += 1
                    outbox_details.append({
                        "dedupe_key": dedupe_key,
                        "status": "processed",
                        "event_id": event.event_id,
                        "task_id": task["task_id"],
                        "theme_code": impact.theme_code,
                    })
                except Exception as exc:
                    outbox_failed += 1
                    with_failed = getattr(db, "mark_event_outbox_failed", None)
                    if with_failed is not None:
                        try:
                            await with_failed(dedupe_key, error=str(exc))
                        except Exception:
                            pass
                    outbox_details.append({
                        "dedupe_key": dedupe_key,
                        "status": "failed",
                        "error": str(exc),
                    })
                    continue

            all_tasks.append(task)
            lineage_records.append(lineage_record)

    # Persist lineage
    if not claim_outbox and persist_lineage and lineage_records and hasattr(db, "upsert_event_task_lineage"):
        for record in lineage_records:
            try:
                await db.upsert_event_task_lineage(record)
            except Exception as exc:
                logger.debug("event_task_generator: lineage persist failed: %s", exc)

    return {
        "enabled": True,
        "tasks": all_tasks,
        "event_count": len(active_events),
        "impact_count": sum(1 for _ in all_tasks),
        "task_count": len(all_tasks),
        "lineage_count": len(lineage_records),
        "lineage_records": lineage_records,
        "persist_lineage": bool(persist_lineage),
        "claim_outbox": bool(claim_outbox),
        "event_source_mode": "event_injections",
        "active_injection_event_count": len(active_events),
        "outbox_claimed": outbox_claimed,
        "outbox_processed": outbox_processed,
        "outbox_skipped": outbox_skipped,
        "outbox_failed": outbox_failed,
        "outbox_details": outbox_details,
        "feature_flag_target_max": feature_flag_max,
    }


__all__ = [
    "generate_tasks_from_active_events",
    "MANUAL_EVENT_ENABLED",
    "THEME_GRAPH_ENABLED",
]
