"""Adapter bridging legacy IncubationFactoryRunner to new IncubationRuntimeSupport Protocol.

This adapter allows Strategy Factory's new application-layer orchestrator to work
with the existing akshare-mcp incubation factory implementation until it can be
fully refactored to implement the Protocol directly.
"""

from __future__ import annotations

from typing import Any


class IncubationFactoryRunnerAdapter:
    """Adapts legacy IncubationFactoryRunner to IncubationRuntimeSupport Protocol."""

    def __init__(self, runner: Any):
        self._runner = runner
        self._intake = getattr(runner, "_intake", None)
        self._signal_generator = getattr(runner, "_signal_generator", None)
        self._forward_verifier = getattr(runner, "_forward_verifier", None)
        self._metrics_recorder = getattr(runner, "_metrics_recorder", None)
        self._reporter = getattr(runner, "_reporter", None)
        self._feedback_writer = getattr(runner, "_feedback_writer", None)
        self._trade_prediction_verifier = getattr(runner, "_trade_prediction_verifier", None)
        self._accelerator = getattr(runner, "_accelerator", None)
        self._alert_monitor = getattr(runner, "_alert_monitor", None)

    @property
    def dry_run(self) -> bool:
        return bool(getattr(self._runner, "dry_run", False))

    async def _get_db(self) -> Any:
        """Get DB connection from runner."""
        return await self._runner._get_db()

    async def scan_and_accept_strategies(self, db: Any) -> dict[str, Any]:
        """Phase 1: Intake."""
        if self._intake is None:
            return {"accepted_count": 0, "rejected_count": 0}
        result = await self._intake.scan_and_accept(db)
        return {
            "accepted_count": result.get("accepted", 0),
            "rejected_count": result.get("rejected", 0),
            "strategies_processed": result.get("accepted", 0) + result.get("rejected", 0),
        }

    async def list_incubating_strategies(self, db: Any, limit: int = 100) -> list[dict[str, Any]]:
        """List strategies in incubating stage."""
        return await self._runner._list_incubating(db)

    async def list_paper_observation_strategies(self, db: Any, limit: int = 100) -> list[dict[str, Any]]:
        """List strategies in paper observation stage."""
        return await self._runner._list_paper_observation(db)

    async def list_diagnostic_observation_strategies(self, db: Any, limit: int = 50) -> list[dict[str, Any]]:
        """List strategies in diagnostic observation stage."""
        return await self._runner._list_diagnostic_observation(db)

    async def generate_signals(self, db: Any, strategy: dict[str, Any]) -> dict[str, Any]:
        """Generate signals for a strategy."""
        if self._signal_generator is None:
            return {"signals_generated": 0}
        return await self._signal_generator.generate(db, strategy)

    async def verify_forward_returns(
        self, db: Any, strategy: dict[str, Any]
    ) -> dict[str, Any]:
        """Verify forward returns."""
        if self._forward_verifier is None:
            return {"verified_count": 0}
        return await self._forward_verifier.verify(db, strategy)

    async def record_metrics(
        self,
        db: Any,
        strategy: dict[str, Any],
        verification: dict[str, Any],
    ) -> dict[str, Any]:
        """Record metrics."""
        if self._metrics_recorder is None:
            raise RuntimeError("incubation_metrics_recorder_unavailable")
        result = await self._metrics_recorder.record(db, strategy, verification)
        if result is None:
            raise RuntimeError("incubation_metric_write_failed: no saved record")
        return result

    async def get_read_only_db(self) -> Any:
        return await self._runner._get_read_only_db()

    async def run_pipeline(self, db: Any, strategies: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._runner._run_pipeline(db, strategies=strategies)

    async def generate_hit_rate_report(
        self, db: Any, strategies: list[dict[str, Any]],
        verifications: dict[str, dict[str, Any]], pipeline_result: dict[str, Any],
        *, trade_prediction_result: dict[str, Any], report_date: Any = None, persist: bool = True,
    ) -> dict[str, Any]:
        if self._reporter is None:
            raise RuntimeError("incubation_reporter_unavailable")
        return await self._reporter.generate(
            db, strategies, verifications, pipeline_result,
            trade_prediction_result=trade_prediction_result, report_date=report_date, persist=persist,
        )

    async def write_feedback(self, db: Any, report: dict[str, Any]) -> dict[str, Any]:
        if self._feedback_writer is None:
            raise RuntimeError("incubation_feedback_writer_unavailable")
        return await self._feedback_writer.write(db, report)

    async def settle_strategy_orders(
        self,
        db: Any,
        strategy: dict[str, Any],
        signal_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Settle orders."""
        return await self._runner._settle_strategy_orders(db, strategy, signal_result=signal_result)

    async def run_recompile_remediation(self, db: Any) -> dict[str, Any]:
        """Run recompile remediation."""
        return await self._runner._run_recompile_remediation(db)

    async def cleanup_rejected_positions(self, db: Any) -> dict[str, Any]:
        """Close paper positions held by strategies outside the execution universe."""
        return await self._runner._run_rejected_strategy_position_cleanup(db)

    async def close_stale_paper_positions(
        self, db: Any, strategies: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Honor declared holding horizons on the daemon path."""
        return await self._runner._run_stale_paper_position_closure(
            db, strategies=strategies
        )

    async def run_execution_audit_acceptance(
        self, db: Any, strategies: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Refresh execution audit verdicts after paper closes."""
        return await self._runner._run_execution_audit_acceptance(
            db, strategies=strategies
        )

    async def verify_trade_predictions(self, db: Any) -> dict[str, Any]:
        """Verify trade predictions."""
        if self._trade_prediction_verifier is None:
            return {"verified_count": 0}
        verifier = self._trade_prediction_verifier
        if hasattr(verifier, "verify_pending"):
            return await verifier.verify_pending(db)
        if hasattr(verifier, "verify"):
            return await verifier.verify(db)
        return {"verified_count": 0, "error": "verifier_method_missing"}

    async def write_daily_predictions(
        self,
        db: Any,
        strategy: dict[str, Any],
        signal_result: dict[str, Any],
    ) -> dict[str, Any]:
        # Keep the prediction view on the same provider boundary as the rest of
        # the incubation hooks; this reads already-persisted signals and never
        # runs a second signal generator.
        from strategy_factory.runtime.provider_adapters import LegacyIncubationProvider

        return await LegacyIncubationProvider(self).write_daily_predictions(
            db, strategy, signal_result
        )

    async def score_horizon_predictions(self, db: Any) -> dict[str, Any]:
        from akshare_mcp.services.incubation_factory.horizon_scorer import HorizonScorer

        return await HorizonScorer().verify_pending(db, persist=True)

    async def run_calibration(self, db: Any) -> dict[str, Any]:
        from akshare_mcp.services.incubation_factory.calibration import (
            CalibrationService,
            Observation,
        )
        from akshare_mcp.services.incubation_factory.directional_scorecard import load_scored_predictions

        predictions = await load_scored_predictions(db)
        observations = [
            Observation(
                as_of=item.as_of,
                horizon=item.horizon or 1,
                confidence=float(item.confidence),
                hit=bool(item.direction_hit),
            )
            for item in predictions
            if item.as_of is not None
            and item.direction_hit is not None
            and item.confidence is not None
        ]
        result = await CalibrationService().try_fit(
            db,
            observations=observations,
            family="default",
        )
        result.setdefault("n_observations", len(observations))
        return result

    async def evaluate_selective_gate(self, db: Any, *, signal_date: Any) -> dict[str, Any]:
        from strategy_factory.runtime.provider_adapters import LegacyIncubationProvider

        return await LegacyIncubationProvider(self).evaluate_selective_gate(
            db, signal_date=signal_date
        )

    async def run_accelerator(
        self, db: Any, strategies: list[dict[str, Any]],
        verifications: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        if self._accelerator is None:
            raise RuntimeError("incubation_accelerator_unavailable")
        return await self._accelerator.evaluate_batch(db, strategies, verifications)

    async def monitor_alerts(self, db: Any) -> dict[str, Any]:
        """Monitor alerts."""
        if self._alert_monitor is None:
            return {"alerts_processed": 0}
        monitor = self._alert_monitor
        if hasattr(monitor, "check"):
            return await monitor.check(db)
        if hasattr(monitor, "monitor"):
            return await monitor.monitor(db)
        return {"alerts_processed": 0, "error": "alert_monitor_method_missing"}

    async def _start_paper_trading_daemons(self) -> None:
        """Start paper trading daemons."""
        await self._runner._start_paper_trading_daemons()

    async def _stop_paper_trading_daemons(self) -> None:
        """Stop paper trading daemons."""
        await self._runner._stop_paper_trading_daemons()


def build_incubation_support(**kwargs: Any) -> IncubationFactoryRunnerAdapter:
    """Build incubation support by adapting the legacy runner.

    Runtime options (notably ``dry_run``) must reach the runner; otherwise a
    dry-run runtime could still mutate paper positions through the adapter.
    """
    from ..services.incubation_factory.runner import IncubationFactoryRunner

    runner_kwargs: dict[str, Any] = {
        "dry_run": bool(kwargs.get("dry_run", False)),
        "auto_apply_review": bool(kwargs.get("auto_apply_review", True)),
        "owns_paper_trading": kwargs.get("owns_paper_trading"),
    }
    if kwargs.get("run_time") is not None:
        runner_kwargs["run_time"] = kwargs["run_time"]
    runner = IncubationFactoryRunner(**runner_kwargs)
    return IncubationFactoryRunnerAdapter(runner)


__all__ = ["IncubationFactoryRunnerAdapter", "build_incubation_support"]
