"""Factor Mining orchestrator - mining cycle execution logic.

This module owns the orchestration of factor mining cycles: search, evolve,
validate, admit. Concrete implementations (engines, evolution, QC) are
delegated to the provider interface.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Any
from uuid import uuid4

from .contracts import FactorMiningResult
from .provider import FactorMiningProvider

logger = logging.getLogger(__name__)


class FactorMiningOrchestrator:
    """Orchestrates factor mining cycles.

    Owns the cycle sequencing, quality aggregation, and result reporting.
    Does not own concrete implementations - those are injected via provider.
    """

    def __init__(self, provider: FactorMiningProvider):
        self._provider = provider

    async def run_cycle(
        self,
        *,
        trigger: str = "scheduled",
        engines: list[str] | None = None,
        candidate_count: int = 30,
        evolution_generations: int = 5,
        codes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Execute one complete factor mining cycle.

        Args:
            trigger: What triggered this cycle
            engines: Which engines to use (None = all)
            candidate_count: Number of candidates to generate
            evolution_generations: Number of evolution generations
            codes: Stock codes to use for validation

        Returns:
            Complete cycle result
        """
        db = await self._provider.get_db()
        await self._provider.ensure_persistent_pool(db)

        run_id = f"mining_{int(datetime.now().timestamp())}_{uuid4().hex[:8]}"
        started_at = datetime.now(timezone.utc)
        logger.info("FactorMiningOrchestrator: starting cycle run_id=%s trigger=%s", run_id, trigger)

        try:
            # Build mining context
            context = await self._provider.build_mining_context(db=db, codes=codes)

            # Check validation universe health
            validation_codes = getattr(context, "validation_codes", []) or []
            if len(validation_codes) < 120:
                return self._build_skipped_result(
                    run_id=run_id,
                    trigger=trigger,
                    started_at=started_at,
                    reason="data_universe_insufficient",
                    validation_universe_health=getattr(context, "validation_universe_health", {}),
                )

            # Install quick IC evaluators for evolution
            quick_ic_evaluator = self._provider.install_quick_evidence_evaluators(db, context)

            # Phase 1: Search for raw candidates
            raw_candidates = await self._provider.search_candidates(
                context=context,
                engines=engines,
                candidate_count=candidate_count,
            )
            logger.info("FactorMiningOrchestrator: raw candidates=%d", len(raw_candidates))

            # Phase 2: Evolve candidates
            evolved = await self._provider.evolve_candidates(
                candidates=raw_candidates,
                context=context,
                generations=evolution_generations,
                ic_evaluator=quick_ic_evaluator,
            )
            logger.info("FactorMiningOrchestrator: evolved candidates=%d", len(evolved))

            # Phase 3: Quick evidence filter
            quick_passed = await self._provider.quick_filter_candidates(evolved, context)
            logger.info(
                "FactorMiningOrchestrator: quick evidence passed=%d/%d",
                len(quick_passed),
                len(evolved),
            )

            # Phase 4: Full validation
            validated = await self._provider.validate_batch(db, quick_passed, context)
            logger.info("FactorMiningOrchestrator: validated candidates=%d", len(validated))

            # Phase 5: Admission to active pool
            admitted = await self._provider.admit_batch(validated)
            await self._provider.persist_admitted_factors(db, admitted)
            logger.info(
                "FactorMiningOrchestrator: admitted=%d pool_size=%d",
                len(admitted),
                self._provider.get_active_pool_size(),
            )

            # Record feedback for engine tuning
            await self._provider.record_feedback(
                run_id,
                raw_candidates,
                evolved,
                validated,
                admitted,
            )

            # Build quality summary (orchestrator owns this aggregation)
            quality_summary = self._build_quality_summary(
                raw_candidates,
                evolved,
                validated,
                admitted,
                context,
            )

            # Phase 6: Reappraise quarantine factors
            reappraisal: dict[str, Any] = {}
            try:
                reappraisal = await self._provider.reappraise_quarantine_factors(db, limit=200)
            except Exception as exc:
                logger.warning("FactorMiningOrchestrator: quarantine reappraisal failed: %s", exc)
                reappraisal = {"error": f"{type(exc).__name__}: {exc}"}

            # Update quality summary with reappraisal results
            reappraisal_promoted_count = int(reappraisal.get("promoted") or 0)
            cycle_active_promoted_count = int(quality_summary.get("active_promoted_count") or 0)
            total_active_promoted_count = cycle_active_promoted_count + max(0, reappraisal_promoted_count)

            quality_summary["reappraisal_promoted_count"] = max(0, reappraisal_promoted_count)
            quality_summary["active_promoted_count"] = total_active_promoted_count

            quality_funnel = dict(quality_summary.get("quality_funnel") or {})
            quality_funnel["promoted"] = total_active_promoted_count
            quality_funnel["reappraisal_promoted"] = max(0, reappraisal_promoted_count)
            quality_summary["quality_funnel"] = quality_funnel

            # Build final report
            completed_at = datetime.now(timezone.utc)
            report = {
                "success": True,
                "run_id": run_id,
                "trigger": trigger,
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
                "raw_candidate_count": len(raw_candidates),
                "evolved_count": len(evolved),
                "validated_count": len(validated),
                "admitted_count": len(admitted),
                "quarantine_count": quality_summary.get("quarantine_count", 0),
                "active_promoted_count": total_active_promoted_count,
                "cycle_active_promoted_count": cycle_active_promoted_count,
                "reappraisal_promoted_count": max(0, reappraisal_promoted_count),
                "pool_size": self._provider.get_active_pool_size(),
                "engines_used": self._provider.get_last_engines_used(),
                "validation_universe_health": getattr(context, "validation_universe_health", {}),
                "quality_summary": quality_summary,
                "quarantine_reappraisal": {
                    "scanned": reappraisal.get("scanned", 0),
                    "promoted": reappraisal.get("promoted", 0),
                    "kept_quarantine": reappraisal.get("kept_quarantine", 0),
                },
            }

            await self._provider.persist_mining_run(db, report)
            return report

        except Exception as exc:
            logger.error("FactorMiningOrchestrator: cycle failed: %s", exc, exc_info=True)
            completed_at = datetime.now(timezone.utc)
            report = {
                "success": False,
                "run_id": run_id,
                "trigger": trigger,
                "error": str(exc),
                "started_at": started_at.isoformat(),
                "completed_at": completed_at.isoformat(),
            }
            await self._provider.persist_mining_run(db, report)
            return report

    def _build_skipped_result(
        self,
        *,
        run_id: str,
        trigger: str,
        started_at: datetime,
        reason: str,
        validation_universe_health: dict[str, Any],
    ) -> dict[str, Any]:
        """Build result for skipped cycle."""
        return {
            "success": True,
            "skipped": True,
            "reason": reason,
            "run_id": run_id,
            "trigger": trigger,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "raw_candidate_count": 0,
            "evolved_count": 0,
            "validated_count": 0,
            "admitted_count": 0,
            "quarantine_count": 0,
            "active_promoted_count": 0,
            "pool_size": self._provider.get_active_pool_size(),
            "engines_used": [],
            "validation_universe_health": validation_universe_health,
            "quality_summary": {
                "reject_reasons": {reason: 1},
            },
        }

    def _build_quality_summary(
        self,
        raw_candidates: list[Any],
        evolved: list[Any],
        validated: list[Any],
        admitted: list[Any],
        context: Any,
    ) -> dict[str, Any]:
        """Build quality summary aggregating metrics across phases.

        This is owned by the orchestrator, not the provider.
        """
        # Calculate rejection counts
        rejected_after_evolution = len(raw_candidates) - len(evolved)
        rejected_after_quick = len(evolved) - len(validated)
        rejected_after_validation = len(validated) - len(admitted)

        # Extract metadata from candidates
        quarantine_count = sum(
            1
            for factor in validated
            if getattr(factor, "status", None) == "quarantine"
        )
        active_promoted_count = sum(
            1
            for factor in admitted
            if getattr(factor, "status", None) == "active"
        )

        return {
            "quality_funnel": {
                "raw": len(raw_candidates),
                "evolved": len(evolved),
                "quick_passed": len(validated),
                "validated": len(validated),
                "admitted": len(admitted),
                "promoted": active_promoted_count,
            },
            "rejection_counts": {
                "evolution": rejected_after_evolution,
                "quick_evidence": rejected_after_quick,
                "validation": rejected_after_validation,
            },
            "quarantine_count": quarantine_count,
            "active_promoted_count": active_promoted_count,
        }
