"""Incubation orchestration logic owned by Strategy Factory.

This orchestrator coordinates the complete incubation cycle:
1. Intake new strategies
2. Generate signals
3. Verify forward returns
4. Record metrics
5. Generate reports
6. Write feedback
7. Handle paper trading settlements
8. Run accelerator and remediation

The actual implementation of each phase is delegated to the IncubationRuntimeSupport
provider, allowing Strategy Factory to own the orchestration while host provides
the concrete services.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .contracts import (
    IncubationPhaseResult,
    IncubationRunSummary,
)
from .provider import IncubationRuntimeSupport

logger = logging.getLogger(__name__)

# Timeout constants
STRATEGY_TIMEOUT_SEC = 30
BATCH_TIMEOUT_SEC = 600


class IncubationOrchestrator:
    """Orchestrator for incubation factory cycles.

    Owns the phase coordination logic while delegating actual implementation
    to the support provider.
    """

    def __init__(self, support: IncubationRuntimeSupport):
        self._support = support

    async def run_cycle(
        self,
        *,
        trigger: str = "scheduled",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Execute one complete incubation cycle.

        Args:
            trigger: What triggered this run (scheduled, manual, etc.)
            dry_run: If True, skip writes and paper trading settlements

        Returns:
            IncubationRunSummary as dict
        """
        run_id = uuid4().hex[:12]
        start_time = datetime.now(timezone.utc)
        logger.info("IncubationOrchestrator: starting run %s (trigger=%s)", run_id, trigger)

        phases: list[IncubationPhaseResult] = []
        phase_failures: list[dict[str, Any]] = []

        async def _run_phase(
            name: str,
            coro_factory,
            *,
            timeout: float = STRATEGY_TIMEOUT_SEC,
        ) -> dict[str, Any] | None:
            """Run a single phase with timeout and error capture."""
            phase_start = datetime.now(timezone.utc)
            try:
                result = await asyncio.wait_for(coro_factory(), timeout=timeout)
                phase_end = datetime.now(timezone.utc)
                duration = (phase_end - phase_start).total_seconds()
                phases.append(
                    IncubationPhaseResult(
                        phase_name=name,
                        success=True,
                        strategies_processed=result.get("strategies_processed", 0),
                        duration_sec=duration,
                        metadata=result,
                    )
                )
                return result
            except asyncio.TimeoutError:
                phase_end = datetime.now(timezone.utc)
                duration = (phase_end - phase_start).total_seconds()
                logger.error(
                    "IncubationOrchestrator [%s] %s: timeout after %.0fs",
                    run_id,
                    name,
                    timeout,
                )
                phase_failures.append(
                    {"phase": name, "error": "timeout", "timeout_sec": timeout}
                )
                phases.append(
                    IncubationPhaseResult(
                        phase_name=name,
                        success=False,
                        strategies_processed=0,
                        duration_sec=duration,
                        error="timeout",
                    )
                )
            except Exception as exc:
                phase_end = datetime.now(timezone.utc)
                duration = (phase_end - phase_start).total_seconds()
                logger.exception(
                    "IncubationOrchestrator [%s] %s: failed: %s", run_id, name, exc
                )
                phase_failures.append(
                    {
                        "phase": name,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )
                phases.append(
                    IncubationPhaseResult(
                        phase_name=name,
                        success=False,
                        strategies_processed=0,
                        duration_sec=duration,
                        error=str(exc),
                    )
                )
            return None

        # Get DB connection (assumed to be provided by support)
        db = await self._get_db()

        try:
            # Phase 1: Intake
            logger.info("IncubationOrchestrator [%s] Phase 1: Intake", run_id)
            intake_result = (
                await _run_phase(
                    "intake",
                    lambda: self._support.scan_and_accept_strategies(db),
                    timeout=BATCH_TIMEOUT_SEC,
                )
                or {}
            )

            # Phase 1.5: Recompile remediation
            logger.info("IncubationOrchestrator [%s] Phase 1.5: Recompile Remediation", run_id)
            remediation_result = (
                await _run_phase(
                    "recompile_remediation",
                    lambda: self._support.run_recompile_remediation(db),
                    timeout=BATCH_TIMEOUT_SEC,
                )
                or {}
            )

            # Phase 1.6: Rejected/archived strategy position cleanup.
            # These strategies are outside the execution universe, so a held
            # position can never receive an exit signal from the runtime.
            cleanup_rejected = getattr(self._support, "cleanup_rejected_positions", None)
            rejected_cleanup_result: dict[str, Any] = {}
            if callable(cleanup_rejected):
                logger.info(
                    "IncubationOrchestrator [%s] Phase 1.6: Rejected strategy position cleanup",
                    run_id,
                )
                rejected_cleanup_result = (
                    await _run_phase(
                        "rejected_strategy_position_cleanup",
                        lambda: cleanup_rejected(db),
                        timeout=BATCH_TIMEOUT_SEC,
                    )
                    or {}
                )

            # Phase 2: Load strategies
            logger.info("IncubationOrchestrator [%s] Phase 2: Load Strategies", run_id)
            incubating = await self._support.list_incubating_strategies(db)
            paper_observation = await self._support.list_paper_observation_strategies(db)
            diagnostic_observation = await self._support.list_diagnostic_observation_strategies(db)

            # Mark stages
            for s in incubating:
                s.setdefault("_intake_stage", "incubating")
            for s in paper_observation:
                s.setdefault("_intake_stage", "paper")
            for s in diagnostic_observation:
                s.setdefault("_intake_stage", "diagnostic")

            all_strategies = list(incubating) + list(paper_observation) + list(diagnostic_observation)
            logger.info(
                "IncubationOrchestrator [%s] Loaded %d incubating + %d paper + %d diagnostic",
                run_id,
                len(incubating),
                len(paper_observation),
                len(diagnostic_observation),
            )

            # Phase 3: Signal generation, verification, and metrics
            logger.info("IncubationOrchestrator [%s] Phase 3: Verification Cycle", run_id)
            verifications: dict[str, dict[str, Any]] = {}
            metrics_recorded = 0
            signals_generated_total = 0
            orders_filled_total = 0
            orders_rejected_total = 0

            for strategy in all_strategies:
                sid = str(strategy.get("id") or "").strip()
                if not sid:
                    continue

                try:
                    # Generate signals
                    signal_result = await asyncio.wait_for(
                        self._support.generate_signals(db, strategy),
                        timeout=STRATEGY_TIMEOUT_SEC,
                    )
                    signals_generated_total += int(
                        signal_result.get("signals_generated") or 0
                    )

                    # Settle orders (paper trading)
                    if (
                        not dry_run
                        and str(strategy.get("_intake_stage") or "")
                        in {"incubating", "paper"}
                    ):
                        try:
                            settlement = await asyncio.wait_for(
                                self._support.settle_strategy_orders(
                                    db, strategy, signal_result
                                ),
                                timeout=STRATEGY_TIMEOUT_SEC,
                            )
                            orders_filled_total += int(
                                settlement.get("filled_count") or 0
                            )
                            orders_rejected_total += int(
                                settlement.get("rejected_count") or 0
                            )
                        except Exception as exc:
                            logger.warning(
                                "IncubationOrchestrator [%s]: order settlement failed for %s: %s",
                                run_id,
                                sid,
                                exc,
                            )

                    # Verify forward returns
                    verification = await asyncio.wait_for(
                        self._support.verify_forward_returns(db, strategy),
                        timeout=STRATEGY_TIMEOUT_SEC,
                    )
                    verifications[sid] = verification

                    # Record metrics
                    if not dry_run:
                        await asyncio.wait_for(
                            self._support.record_metrics(db, strategy, verification),
                            timeout=STRATEGY_TIMEOUT_SEC,
                        )
                        metrics_recorded += 1

                except asyncio.TimeoutError:
                    logger.warning(
                        "IncubationOrchestrator [%s]: timeout processing strategy %s",
                        run_id,
                        sid,
                    )
                except Exception as exc:
                    logger.exception(
                        "IncubationOrchestrator [%s]: failed processing strategy %s: %s",
                        run_id,
                        sid,
                        exc,
                    )

            # Phase 3.5: Declared holding-period exit.  Only strategies that
            # declare a max_holding_days are affected; undeclared strategies
            # remain on the signal-driven exit path.
            close_stale = getattr(self._support, "close_stale_paper_positions", None)
            stale_cleanup_result: dict[str, Any] = {}
            if callable(close_stale):
                logger.info(
                    "IncubationOrchestrator [%s] Phase 3.5: Declared holding-period exit",
                    run_id,
                )
                stale_cleanup_result = (
                    await _run_phase(
                        "stale_paper_position_closure",
                        lambda: close_stale(db, all_strategies),
                        timeout=BATCH_TIMEOUT_SEC,
                    )
                    or {}
                )

            # Phase 4: Trade prediction verification
            logger.info("IncubationOrchestrator [%s] Phase 4: Trade Prediction Verification", run_id)
            trade_pred_result = (
                await _run_phase(
                    "trade_prediction_verification",
                    lambda: self._support.verify_trade_predictions(db),
                    timeout=BATCH_TIMEOUT_SEC,
                )
                or {}
            )

            # Phase 5: Accelerator
            logger.info("IncubationOrchestrator [%s] Phase 5: Accelerator", run_id)
            accelerator_result = (
                await _run_phase(
                    "accelerator",
                    lambda: self._support.run_accelerator(db),
                    timeout=BATCH_TIMEOUT_SEC,
                )
                or {}
            )

            # Phase 6: Alert monitoring
            logger.info("IncubationOrchestrator [%s] Phase 6: Alert Monitoring", run_id)
            alert_result = (
                await _run_phase(
                    "alert_monitoring",
                    lambda: self._support.monitor_alerts(db),
                    timeout=BATCH_TIMEOUT_SEC,
                )
                or {}
            )

            # Phase 7: Refresh execution audit verdicts.  Without this the
            # daemon path would close paper positions but leave the hard gate
            # reading stale/missing verdicts.
            run_audit = getattr(self._support, "run_execution_audit_acceptance", None)
            execution_audit_result: dict[str, Any] = {}
            if callable(run_audit):
                logger.info(
                    "IncubationOrchestrator [%s] Phase 7: Execution audit acceptance",
                    run_id,
                )
                execution_audit_result = (
                    await _run_phase(
                        "execution_audit_acceptance",
                        lambda: run_audit(db, all_strategies),
                        timeout=BATCH_TIMEOUT_SEC,
                    )
                    or {}
                )

            # Determine overall status
            end_time = datetime.now(timezone.utc)
            duration_sec = (end_time - start_time).total_seconds()

            if phase_failures:
                status = "partial"
            else:
                status = "completed"

            summary = IncubationRunSummary(
                run_id=run_id,
                trigger=trigger,
                start_time=start_time,
                end_time=end_time,
                duration_sec=duration_sec,
                status=status,
                phases=phases,
                strategies_intake=intake_result.get("accepted_count", 0),
                strategies_verified=len(verifications),
                strategies_promoted=accelerator_result.get("promoted_count", 0),
                paper_orders_filled=orders_filled_total,
                paper_orders_rejected=orders_rejected_total,
                phase_failures=phase_failures,
                error=None,
            )

            logger.info(
                "IncubationOrchestrator [%s]: completed in %.1fs (status=%s, verified=%d, promoted=%d)",
                run_id,
                duration_sec,
                status,
                len(verifications),
                accelerator_result.get("promoted_count", 0),
            )

            return self._summary_to_dict(summary)

        except Exception as exc:
            end_time = datetime.now(timezone.utc)
            duration_sec = (end_time - start_time).total_seconds()
            logger.exception(
                "IncubationOrchestrator [%s]: cycle failed: %s", run_id, exc
            )

            summary = IncubationRunSummary(
                run_id=run_id,
                trigger=trigger,
                start_time=start_time,
                end_time=end_time,
                duration_sec=duration_sec,
                status="failed",
                phases=phases,
                strategies_intake=0,
                strategies_verified=0,
                strategies_promoted=0,
                paper_orders_filled=0,
                paper_orders_rejected=0,
                phase_failures=phase_failures,
                error=str(exc),
            )

            return self._summary_to_dict(summary)

    async def _get_db(self) -> Any:
        """Resolve the database connection through the provider protocol.

        Prefers the public ``get_db()`` declared on ``IncubationProvider``. The
        underscored fallback keeps hosts that predate the public method working;
        it is the last private reach-through on this path and should disappear
        once every host exposes ``get_db``.
        """
        public = getattr(self._support, "get_db", None)
        if callable(public):
            return await public()
        legacy = getattr(self._support, "_get_db", None)
        if callable(legacy):
            return await legacy()
        raise RuntimeError(
            "IncubationProvider must expose get_db() (or legacy _get_db())"
        )

    def _summary_to_dict(self, summary: IncubationRunSummary) -> dict[str, Any]:
        """Convert IncubationRunSummary to dict for compatibility."""
        return {
            "run_id": summary.run_id,
            "trigger": summary.trigger,
            "start_time": summary.start_time.isoformat(),
            "end_time": summary.end_time.isoformat(),
            "duration_sec": summary.duration_sec,
            "status": summary.status,
            "phases": [
                {
                    "phase_name": p.phase_name,
                    "success": p.success,
                    "strategies_processed": p.strategies_processed,
                    "duration_sec": p.duration_sec,
                    "error": p.error,
                    "metadata": p.metadata,
                }
                for p in summary.phases
            ],
            "strategies_intake": summary.strategies_intake,
            "strategies_verified": summary.strategies_verified,
            "strategies_promoted": summary.strategies_promoted,
            "paper_orders_filled": summary.paper_orders_filled,
            "paper_orders_rejected": summary.paper_orders_rejected,
            "phase_failures": summary.phase_failures,
            "error": summary.error,
        }


__all__ = ["IncubationOrchestrator"]
