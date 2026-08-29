"""Signal Tracker orchestrator - Phase A-H execution logic.

This module owns the orchestration of signal tracking phases A through H.
Concrete implementations (data access, signal generation, risk scanning) are
delegated to the provider interface.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime
import logging
from typing import Any
from uuid import uuid4

from .contracts import SignalTrackerResult
from .provider import SignalTrackerProvider

logger = logging.getLogger(__name__)


class SignalTrackerOrchestrator:
    """Orchestrates signal tracking phases A-H.

    Owns the phase sequencing, error handling, and result aggregation.
    Does not own concrete implementations - those are injected via provider.
    """

    def __init__(self, provider: SignalTrackerProvider):
        self._provider = provider

    async def run_cycle(self, *, as_of: date | None = None) -> dict[str, Any]:
        """Execute one complete signal tracking cycle (Phase A-H).

        Args:
            as_of: Signal date (defaults to today)

        Returns:
            Complete cycle result with all phase outputs
        """
        db = await self._provider.get_db()
        if hasattr(db, "initialize"):
            await db.initialize()

        today = as_of or date.today()
        start = datetime.now()
        trace_id = uuid4().hex[:12]

        # Initialize task run record
        task_run = (
            await db.save_strategy_task_run(
                {
                    "task_name": "strategy_runtime_cycle",
                    "task_scope": "signal_tracker",
                    "task_key": str(today),
                    "status": "running",
                    "trace_id": trace_id,
                    "payload": {"signal_date": str(today)},
                }
            )
            if hasattr(db, "save_strategy_task_run")
            else {"id": None, "trace_id": None}
        )

        # Initialize result counters
        result = SignalTrackerResult(
            signals_generated=0,
            signal_event_snapshots=0,
            forward_returns_computed=0,
            incubation_orders=0,
            incubation_orders_filled=0,
            incubation_nav_snapshots=0,
            incubation_metrics=0,
            incubation_pipeline_snapshots=0,
            incubation_auto_promotions=0,
            submitted_runtime_pipeline_snapshots=0,
            risk_events=0,
            risk_actions=0,
            transitions=0,
            vector_registry_updates=0,
            projection_snapshots=0,
            skipped_runtime_controls=0,
            task_run_id=task_run.get("id"),
            errors=[],
            phase_results={},
        )

        strategies: list[dict[str, Any]] = []
        executable_strategies: list[dict[str, Any]] = []
        submitted_runtime_strategies: list[dict[str, Any]] = []
        paper_runtime_strategies: list[dict[str, Any]] = []

        # Phase execution wrapper with timeout and error handling
        async def _run_phase(phase_name: str, phase_coro):
            """Execute a phase with timeout and error tracking."""
            before_counters = {
                key: int(value or 0)
                for key, value in vars(result).items()
                if isinstance(value, (int, float)) and key != "task_run_id"
            }
            before_errors = len(result.errors)
            timeout_sec = self._provider.phase_timeout_seconds(phase_name)
            phase_started = datetime.now()
            status = "completed"
            error = None
            payload: dict[str, Any] = {}
            phase_value: Any = None

            try:
                raw_payload = await asyncio.wait_for(phase_coro, timeout=timeout_sec)
                phase_value = raw_payload
                if isinstance(raw_payload, dict):
                    payload = raw_payload
                elif isinstance(raw_payload, list):
                    payload = {"count": len(raw_payload)}
            except asyncio.TimeoutError:
                status = "timeout"
                error = f"phase_{phase_name}_timeout"
                result.errors.append(error)
                result.phase_timeout_count += 1
                result.phase_timeouts.append(phase_name)
                logger.error("SignalTracker phase %s: timeout after %.0fs", phase_name, timeout_sec)
            except Exception as exc:
                status = "error"
                error = f"Phase {phase_name}: {exc}"
                result.errors.append(error)
                result.phase_error_count += 1
                result.phase_errors.append(f"{phase_name}: {type(exc).__name__}")
                logger.exception("SignalTracker phase %s: failed", phase_name)

            elapsed = (datetime.now() - phase_started).total_seconds()
            after_counters = {
                key: int(value or 0)
                for key, value in vars(result).items()
                if isinstance(value, (int, float)) and key != "task_run_id"
            }

            # Calculate delta
            delta = {
                key: after_counters.get(key, 0) - before_counters.get(key, 0)
                for key in set(before_counters) | set(after_counters)
            }
            new_errors = len(result.errors) - before_errors

            result.phase_results[phase_name] = {
                "status": status,
                "error": error,
                "elapsed_seconds": round(elapsed, 2),
                "delta": delta,
                "new_errors": new_errors,
                "payload": payload,
            }
            return phase_value

        # Phase A: Load execution universe
        logger.info("SignalTracker [%s] Phase A: Load execution universe", trace_id)
        try:
            default_universe = list(self._provider.get_default_universe())
            loaded_strategies = await _run_phase(
                "phase_a_load_universe",
                self._provider.load_executable_strategies(db, limit=500),
            )
            executable_strategies = loaded_strategies or []

            if not executable_strategies:
                submitted_runtime_strategies = await _run_phase(
                    "phase_a_load_submitted_runtime",
                    self._provider.load_runtime_submitted_strategies(db, limit=200),
                ) or []
                paper_runtime_strategies = await _run_phase(
                    "phase_a_load_paper_runtime",
                    self._provider.load_runtime_observation_strategies(db, limit=200),
                ) or []

            strategies = self._merge_unique_strategies(
                executable_strategies,
                submitted_runtime_strategies,
                paper_runtime_strategies,
                default_universe,
            )

            result.runtime_universe = {
                "strategies": len(strategies),
                "executable": len(executable_strategies),
                "submitted_runtime": len(submitted_runtime_strategies),
                "paper_runtime": len(paper_runtime_strategies),
            }
            logger.info(
                "SignalTracker [%s] Phase A: loaded %d strategies (executable=%d, submitted=%d, paper=%d)",
                trace_id,
                len(strategies),
                len(executable_strategies),
                len(submitted_runtime_strategies),
                len(paper_runtime_strategies),
            )
        except Exception as exc:
            logger.exception("SignalTracker [%s] Phase A failed: %s", trace_id, exc)
            result.errors.append(f"Phase A: {exc}")

        # Phase B: Generate signals and create signal event snapshots
        logger.info("SignalTracker [%s] Phase B: Generate signals for %d strategies", trace_id, len(strategies))
        for strategy in strategies:
            strategy_id = str(strategy.get("id") or "").strip()
            if not strategy_id:
                continue

            try:
                # Generate signals
                signal_result = await asyncio.wait_for(
                    self._provider.generate_signals(db, strategy, as_of=today),
                    timeout=self._provider.phase_timeout_seconds("signal_generation"),
                )
                result.signals_generated += int(signal_result.get("signals_generated") or 0)

                # Create signal event snapshot
                if signal_result.get("signals_generated"):
                    snapshot_result = await asyncio.wait_for(
                        self._provider.create_signal_event_snapshot(
                            db, strategy, signal_result, as_of=today
                        ),
                        timeout=self._provider.phase_timeout_seconds("signal_snapshot"),
                    )
                    if snapshot_result.get("snapshot_created"):
                        result.signal_event_snapshots += 1

            except asyncio.TimeoutError:
                logger.warning("SignalTracker [%s] Phase B: timeout for strategy %s", trace_id, strategy_id)
            except Exception as exc:
                logger.warning(
                    "SignalTracker [%s] Phase B: failed for strategy %s: %s",
                    trace_id,
                    strategy_id,
                    exc,
                )

        # Phase C: Backfill forward returns
        logger.info("SignalTracker [%s] Phase C: Backfill forward returns", trace_id)
        forward_return_result = await _run_phase(
            "phase_c_forward_returns",
            self._provider.backfill_forward_returns(
                db,
                forward_days_list=[1, 5, 10, 20],
                batch_limit=2000,
                max_rounds=100,
            ),
        ) or {}
        result.forward_returns_computed = int(forward_return_result.get("computed_count") or 0)

        # Phase D: Sync incubation orders, NAV, and metrics
        logger.info("SignalTracker [%s] Phase D: Sync incubation data", trace_id)
        incubating = [s for s in strategies if str(s.get("status") or "") == "incubating"]
        paper = [s for s in strategies if str(s.get("status") or "") == "paper"]
        incubation_strategies = incubating + paper

        if incubation_strategies:
            orders_result = await _run_phase(
                "phase_d_incubation_orders",
                self._provider.sync_incubation_orders(db, incubation_strategies),
            ) or {}
            result.incubation_orders = int(orders_result.get("orders_synced") or 0)
            result.incubation_orders_filled = int(orders_result.get("filled_count") or 0)

            nav_result = await _run_phase(
                "phase_d_incubation_nav",
                self._provider.sync_incubation_nav_snapshots(db, incubation_strategies),
            ) or {}
            result.incubation_nav_snapshots = int(nav_result.get("snapshots_created") or 0)

            metrics_result = await _run_phase(
                "phase_d_incubation_metrics",
                self._provider.sync_incubation_metrics(db, incubation_strategies),
            ) or {}
            result.incubation_metrics = int(metrics_result.get("metrics_recorded") or 0)

        # Phase E: Run incubation pipeline
        logger.info("SignalTracker [%s] Phase E: Run incubation pipeline", trace_id)
        if incubation_strategies:
            pipeline_result = await _run_phase(
                "phase_e_incubation_pipeline",
                self._provider.run_incubation_pipeline(db, incubation_strategies),
            ) or {}
            result.incubation_pipeline_snapshots = int(pipeline_result.get("snapshots_created") or 0)
            result.incubation_auto_promotions = int(pipeline_result.get("auto_promoted") or 0)

        # Phase E2: Run submitted runtime pipeline
        if submitted_runtime_strategies:
            runtime_pipeline_result = await _run_phase(
                "phase_e_submitted_runtime_pipeline",
                self._provider.run_submitted_runtime_pipeline(db, submitted_runtime_strategies),
            ) or {}
            result.submitted_runtime_pipeline_snapshots = int(
                runtime_pipeline_result.get("snapshots_created") or 0
            )

        # Phase F: Runtime risk scan
        logger.info("SignalTracker [%s] Phase F: Runtime risk scan", trace_id)
        risk_result = await _run_phase(
            "phase_f_risk_scan",
            self._provider.run_runtime_risk_scan(db, strategies),
        ) or {}
        result.risk_events = int(risk_result.get("events_detected") or 0)
        result.risk_actions = int(risk_result.get("actions_taken") or 0)

        # Phase G: Lifecycle scan
        logger.info("SignalTracker [%s] Phase G: Lifecycle scan", trace_id)
        lifecycle_result = await _run_phase(
            "phase_g_lifecycle_scan",
            self._provider.run_lifecycle_scan(db, strategies),
        ) or {}
        result.transitions = int(lifecycle_result.get("transitions") or 0)

        # Phase H: Vector registry reconciliation
        logger.info("SignalTracker [%s] Phase H: Vector registry reconciliation", trace_id)
        vector_result = await _run_phase(
            "phase_h_vector_registry",
            self._provider.reconcile_vector_registry(db, strategies),
        ) or {}
        result.vector_registry_updates = int(vector_result.get("updates") or 0)

        # Phase I: Domain projection snapshot
        logger.info("SignalTracker [%s] Phase I: Domain projection snapshot", trace_id)
        projection_result = await _run_phase(
            "phase_i_domain_projection",
            self._provider.snapshot_domain_projections(db, strategies),
        ) or {}
        result.projection_snapshots = int(projection_result.get("snapshots_created") or 0)

        # Finalize
        result.elapsed_seconds = (datetime.now() - start).total_seconds()

        if hasattr(db, "save_strategy_task_run") and task_run.get("id"):
            await db.save_strategy_task_run(
                {
                    "id": task_run["id"],
                    "status": "completed" if not result.errors else "partial",
                    "result": vars(result),
                }
            )

        logger.info(
            "SignalTracker [%s]: completed in %.1fs (signals=%d, snapshots=%d, errors=%d)",
            trace_id,
            result.elapsed_seconds,
            result.signals_generated,
            result.signal_event_snapshots,
            len(result.errors),
        )

        return vars(result)

    @staticmethod
    def _merge_unique_strategies(*groups: list[dict]) -> list[dict]:
        """Merge strategy groups, deduplicating by strategy ID."""
        merged: list[dict] = []
        seen: set[str] = set()
        for group in groups:
            for strategy in list(group or []):
                if not isinstance(strategy, dict):
                    continue
                strategy_id = str((strategy or {}).get("id") or "").strip()
                if not strategy_id or strategy_id in seen:
                    continue
                seen.add(strategy_id)
                merged.append(strategy)
        return merged
