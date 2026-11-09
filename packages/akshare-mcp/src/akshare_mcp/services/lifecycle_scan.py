"""Bounded lifecycle reviews with durable attempt scheduling."""
from __future__ import annotations

import asyncio
import logging
import time
from uuid import uuid4

from .promotion_pipeline import get_strategy_promotion_pipeline_service
from .strategy_lifecycle_shared import build_incubation_overview, update_status

logger = logging.getLogger(__name__)


async def _review_strategy(db, strategy, promotion_service):
    if strategy["status"] == "incubating":
        result = await promotion_service.review(db, strategy, source="lifecycle_scan", auto_apply=True)
        transition = result.get("applied_transition")
        return {
            "review": result.get("review") or {},
            "review_id": dict(result.get("review") or {}).get("id"),
            "blockers": dict(result.get("overview") or {}).get("blockers") or [],
            "transition": ({"id": strategy["id"], **transition,
                            "reason": "incubation_promoted" if transition.get("to") == "listed" else "incubation_failed"}
                           if transition else None),
        }
    overview = await build_incubation_overview(db, strategy)
    transition = None
    blockers = []
    if overview["deprecation_risk"]:
        if hasattr(db, "list_strategy_incubation_metrics"):
            metrics = await db.list_strategy_incubation_metrics(strategy["id"], limit=3)
            confirmed = len(metrics) >= 2 and sum(str(m.get("decision") or "") == "halt" for m in metrics) >= 2
        else:
            confirmed = True
        if confirmed:
            await update_status(db, strategy["id"], "deprecated", actor_id="lifecycle_scan",
                                reason="listed_degraded", metadata=overview)
            transition = {"id": strategy["id"], "from": "listed", "to": "deprecated", "reason": "listed_degraded"}
        else:
            blockers = ["deprecation_risk_unconfirmed"]
    return {"review_id": None, "blockers": blockers, "transition": transition}


async def run_lifecycle_scan(db, *, budget_seconds: float = 40, max_items: int = 100) -> dict:
    """Review a fair slice within the tracker's unchanged 45-second limit.

    The persisted cursor advances on reservation. A timed-out attempt remains
    unresolved; it is retried in a later sweep rather than blocking the tail.
    """
    required = ("claim_lifecycle_scan", "reserve_lifecycle_scan_item", "finish_lifecycle_scan_item",
                "lifecycle_scan_progress", "release_lifecycle_scan")
    if not all(callable(getattr(db, name, None)) for name in required):
        raise RuntimeError("lifecycle_scan_storage_unavailable")
    owner = uuid4().hex
    claim = await db.claim_lifecycle_scan(owner)
    if not claim.get("acquired"):
        return {"status": "partial", "reason": claim.get("reason"), "coverage_complete": False,
                "scanned": 0, "attempted": 0, "transitions": None,
                "transitions_reason": claim.get("reason"), "completed_transitions": []}
    deadline = time.monotonic() + max(0, min(float(budget_seconds), 40))
    attempted = reviewed = ineligible = 0
    transitions = []
    reviews = []
    blocked = []
    failures = []
    reason = None
    active_attempt = None
    try:
        promotion_service = get_strategy_promotion_pipeline_service()
        while attempted < max(1, int(max_items)):
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                reason = "lifecycle_scan_budget_exhausted"
                break
            active_attempt = await db.reserve_lifecycle_scan_item(owner)
            if active_attempt is None:
                break
            attempted += 1
            sid = active_attempt["strategy_id"]
            strategy = await db.get_strategy(sid)
            if not strategy or strategy.get("status") != active_attempt["expected_status"]:
                await db.finish_lifecycle_scan_item(owner, active_attempt["attempt_id"], status="ineligible",
                                                    result={"reason": "lifecycle_scan_eligibility_changed",
                                                            "current_status": (strategy or {}).get("status")})
                active_attempt = None
                ineligible += 1
                continue
            try:
                result = await asyncio.wait_for(_review_strategy(db, strategy, promotion_service),
                                                timeout=max(0, deadline - time.monotonic()))
            except Exception as exc:
                error = "lifecycle_scan_item_timeout" if isinstance(exc, asyncio.TimeoutError) else "lifecycle_scan_item_error"
                await db.finish_lifecycle_scan_item(owner, active_attempt["attempt_id"], status="failed",
                                                    result={"reason": error, "partial_writes_possible": True},
                                                    error=f"{error}:{type(exc).__name__}:{exc}")
                failures.append({"strategy_id": sid, "attempt_id": active_attempt["attempt_id"], "reason": error})
                active_attempt = None
                if isinstance(exc, asyncio.TimeoutError):
                    reason = "lifecycle_scan_budget_exhausted"
                    break
                continue
            review = result.pop("review", None)
            await db.finish_lifecycle_scan_item(owner, active_attempt["attempt_id"], status="completed", result=result)
            active_attempt = None
            reviewed += 1
            if review is not None:
                reviews.append(review)
            if result["transition"]:
                transitions.append(result["transition"])
            elif result["blockers"] or strategy["status"] == "incubating":
                blocked.append({"id": sid, "status": strategy["status"], "blockers": result["blockers"]})
        progress = await db.lifecycle_scan_progress(owner)
        complete = progress["coverage_complete"]
        return {"status": "completed" if complete else "partial",
                "reason": None if complete else reason or "lifecycle_scan_coverage_incomplete",
                "scanned": reviewed, "attempted": attempted, "ineligible": ineligible,
                "transitions": None if failures else transitions,
                "transitions_reason": "lifecycle_scan_partial_writes_unknown" if failures else None,
                "completed_transitions": transitions, "reviews": reviews,
                "blocked": blocked, "failures": failures, **progress}
    except BaseException:
        if active_attempt is not None:
            try:
                await asyncio.shield(db.finish_lifecycle_scan_item(
                    owner, active_attempt["attempt_id"], status="interrupted",
                    result={"reason": "lifecycle_scan_interrupted", "partial_writes_possible": True},
                    error="lifecycle_scan_interrupted",
                ))
            except Exception:
                logger.exception("Lifecycle attempt checkpoint failed; persisted reservation remains pending")
        raise
    finally:
        await asyncio.shield(db.release_lifecycle_scan(owner))
