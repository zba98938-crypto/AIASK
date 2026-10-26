"""Adapters from legacy host supports to Strategy Factory provider protocols.

The application layer only consumes public provider methods.  Older hosts
still expose the pre-migration private support surface; these adapters contain
that compatibility in one runtime-boundary module.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from ..infrastructure.runtime_services import (
    get_db_provider,
    get_strategy_domain_projection_service,
    get_strategy_incubation_pipeline_service,
    get_strategy_incubation_service,
    get_strategy_lifecycle_scan_runner,
    get_strategy_runtime_risk_service,
    get_strategy_vector_governance_service,
)


class LegacyIncubationProvider:
    def __init__(self, support: Any):
        self.support = support
        self.settlements: dict[str, dict[str, Any]] = {}

    async def get_db(self) -> Any:
        """Public name required by ``IncubationRuntimeSupport``."""
        public = getattr(self.support, "get_db", None)
        if callable(public):
            return await public()
        return await self.support._get_db()

    # Retained so hosts and tests that still call the underscored name keep
    # working; both route through the same resolution.
    async def _get_db(self) -> Any:
        return await self.get_db()

    async def scan_and_accept_strategies(self, db: Any) -> dict[str, Any]:
        return await self.support._intake.scan_and_accept(db)

    async def run_recompile_remediation(self, db: Any) -> dict[str, Any]:
        return await self.support._run_recompile_remediation(db)

    async def list_incubating_strategies(self, db: Any, limit: int = 100) -> list[dict[str, Any]]:
        del limit
        return await self.support._list_incubating(db)

    async def list_paper_observation_strategies(self, db: Any, limit: int = 100) -> list[dict[str, Any]]:
        del limit
        return await self.support._list_paper_observation(db)

    async def list_diagnostic_observation_strategies(self, db: Any, limit: int = 50) -> list[dict[str, Any]]:
        del limit
        return await self.support._list_diagnostic_observation(db)

    async def generate_signals(self, db: Any, strategy: dict[str, Any]) -> dict[str, Any]:
        return await self.support._signal_generator.generate(db, strategy)

    async def settle_strategy_orders(
        self, db: Any, strategy: dict[str, Any], signal_result: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self.support._settle_strategy_orders(
            db, strategy, signal_result=signal_result
        )
        payload = dict(result or {})
        strategy_id = str(strategy.get("id") or len(self.settlements))
        self.settlements[strategy_id] = payload
        return payload

    async def verify_forward_returns(self, db: Any, strategy: dict[str, Any]) -> dict[str, Any]:
        return await self.support._forward_verifier.verify(db, strategy)

    async def record_metrics(
        self, db: Any, strategy: dict[str, Any], verification: dict[str, Any]
    ) -> dict[str, Any]:
        result = await self.support._metrics_recorder.record(db, strategy, verification)
        return dict(result or {})

    async def verify_trade_predictions(self, db: Any) -> dict[str, Any]:
        return await self.support._trade_prediction_verifier.verify_pending(db)

    async def write_daily_predictions(
        self,
        db: Any,
        strategy: dict[str, Any],
        signal_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Write predictions from signals already persisted by Phase 3.

        This hook deliberately reads the signal ledger instead of generating a
        second signal pass.  The incubation service owns signal generation and
        paper-order settlement; the prediction writer only creates the
        prediction view for those same signals.
        """
        from ..application.research.daily_prediction_writer import DailyPredictionWriter

        strategy_id = str(strategy.get("id") or "").strip()
        get_signals = getattr(db, "get_signals", None)
        get_klines = getattr(db, "get_klines", None)
        save = getattr(db, "save_strategy_trade_prediction", None)
        if not callable(get_signals) or not callable(get_klines) or not callable(save):
            return {"status": "degraded", "reason": "prediction_storage_unavailable", "written": 0}

        # The signal generator normally returns the closed-bar date.  Older
        # providers sometimes omit it or return the wall-clock Saturday/Sunday
        # date.  Never use that date to label a prediction: recover the latest
        # date that is actually present in the persisted signal ledger.
        raw_day = signal_result.get("signal_date")
        try:
            requested_day = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day)[:10])
        except (TypeError, ValueError):
            requested_day = None

        signals = []
        signal_day: date | None = requested_day
        if signal_day is not None:
            signals = await get_signals(
                strategy_id, start_date=signal_day, end_date=signal_day, limit=500
            )
        if not signals:
            recent = await get_signals(strategy_id, limit=500)
            dated: list[tuple[date, dict[str, Any]]] = []
            for payload in list(recent or []):
                item = dict(payload or {})
                token = str(item.get("signal_date") or "")[:10]
                try:
                    day = date.fromisoformat(token)
                except ValueError:
                    continue
                dated.append((day, item))
            if dated:
                signal_day = max(day for day, _ in dated)
                signals = [item for day, item in dated if day == signal_day]
        if signal_day is None:
            return {"status": "degraded", "reason": "signal_date_unavailable", "written": 0}

        writer = DailyPredictionWriter()
        written: list[dict[str, Any]] = []
        errors: list[str] = []
        for signal in list(signals or []):
            item = dict(signal or {})
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            try:
                bars = list(await get_klines(code, limit=400) or [])
                # A sync can already contain bars newer than the signal being
                # materialised.  Bound the input to the signal date so the
                # writer cannot label a 10-21 signal with a 10-23 bar.
                bounded: list[dict[str, Any]] = []
                for bar in bars:
                    raw_bar_day = str(
                        bar.get("date") or bar.get("trade_date") or bar.get("time") or ""
                    )[:10]
                    try:
                        if date.fromisoformat(raw_bar_day) <= signal_day:
                            bounded.append(dict(bar))
                    except (TypeError, ValueError):
                        continue
                result = await writer.write(
                    db,
                    strategy=strategy,
                    code=code,
                    klines=bounded,
                    signal=int(item.get("signal") or 0),
                    # ``bounded`` already excludes bars newer than the signal;
                    # use the current closed-session clock so the signal-day
                    # bar itself is retained (midnight would be treated as an
                    # in-progress bar by the writer).
                    now=datetime.now().astimezone(),
                )
                if result.get("status") == "ok":
                    written.append(result)
                elif result.get("status") not in {"skipped", "degraded"}:
                    errors.append(f"{code}:{result.get('reason') or result.get('status')}")
                elif result.get("status") == "degraded":
                    errors.append(f"{code}:{result.get('reason') or 'degraded'}")
            except Exception as exc:  # one symbol must not abort the cycle
                errors.append(f"{code}:{type(exc).__name__}:{exc}")
        return {
            "status": "ok" if not errors else "degraded",
            "strategy_id": strategy_id,
            "signal_date": signal_day.isoformat(),
            "written": len(written),
            "predictions": written,
            "errors": errors[:20],
        }

    # Horizon scoring and calibration are implemented by the host (akshare-mcp
    # owns HorizonScorer / CalibrationService); strategy-factory must not import
    # it.  Delegate to the host support and say so when it lacks the hook.
    async def score_horizon_predictions(self, db: Any) -> dict[str, Any]:
        hook = getattr(self.support, "score_horizon_predictions", None)
        if not callable(hook):
            return {"status": "degraded", "reason": "score_horizon_predictions_unavailable"}
        return await hook(db)

    async def run_calibration(self, db: Any) -> dict[str, Any]:
        hook = getattr(self.support, "run_calibration", None)
        if not callable(hook):
            return {"status": "degraded", "reason": "run_calibration_unavailable"}
        return await hook(db)

    async def evaluate_selective_gate(self, db: Any, *, signal_date: Any) -> dict[str, Any]:
        from strategy_factory.application.selective_gate import SelectiveGate

        if not isinstance(signal_date, date):
            signal_date = date.fromisoformat(str(signal_date)[:10])
        # The legacy incubation adapter has no executor contract.  Passing no
        # executor is intentional: an ordered decision is persisted, while the
        # gate remains fail-closed until a calibrated execution adapter exists.
        return await SelectiveGate().evaluate_session(
            db,
            signal_date=signal_date,
            persist=True,
            executor=None,
        )

    async def run_accelerator(self, db: Any) -> dict[str, Any]:
        return await self.support._accelerator.evaluate_batch(db, [], {})

    async def monitor_alerts(self, db: Any) -> dict[str, Any]:
        return await self.support._alert_monitor.check(db, run_result=None)


class LegacyFactorMiningProvider:
    def __init__(self, support: Any):
        self.support = support

    async def get_db(self) -> Any:
        ensure = getattr(self.support, "_ensure_initialized", None)
        if callable(ensure):
            ensure()
        return await self.support._get_db()

    async def ensure_persistent_pool(self, db: Any) -> None:
        await self.support._ensure_persistent_pool(db)

    async def build_mining_context(self, db: Any, *, codes=None) -> Any:
        return await self.support._build_mining_context(db=db, codes=codes)

    def install_quick_evidence_evaluators(self, db: Any, context: Any) -> Any:
        return self.support._install_quick_evidence_evaluators(db, context)

    async def search_candidates(self, context: Any, *, engines=None, candidate_count=30) -> list[Any]:
        return await self.support._engine_scheduler.search(
            context=context, engines=engines, candidate_count=candidate_count
        )

    async def evolve_candidates(self, candidates, context, *, generations=5, ic_evaluator=None):
        return await self.support._evolutionary_optimizer.evolve(
            candidates=candidates,
            context=context,
            generations=generations,
            ic_evaluator=ic_evaluator,
        )

    async def quick_filter_candidates(self, candidates, context):
        return await self.support._quick_filter_candidates(candidates, context)

    async def validate_batch(self, db, candidates, context):
        return await self.support._validate_batch(db, candidates, context)

    async def admit_batch(self, validated):
        return await self.support._active_pool.admit_batch(validated)

    async def persist_admitted_factors(self, db, admitted) -> None:
        await self.support._persist_admitted_factors(db, admitted)

    async def record_feedback(self, run_id, raw, evolved, validated, admitted) -> None:
        await self.support._record_feedback(run_id, raw, evolved, validated, admitted)

    async def reappraise_quarantine_factors(self, db, *, limit=200):
        return await self.support._reappraise_quarantine_factors(db, limit=limit)

    async def persist_mining_run(self, db, report) -> None:
        await self.support._persist_mining_run(db, report)

    def get_active_pool_size(self) -> int:
        return int(self.support._active_pool.size)

    def get_last_engines_used(self) -> list[str]:
        return list(getattr(self.support._engine_scheduler, "last_engines_used", []) or [])

    # --- maintenance ----------------------------------------------------------
    # Every private reach-through the maintenance cycle needs is contained here,
    # so the orchestrator can depend on the public protocol alone.

    async def run_decay_check(self, db: Any) -> dict[str, Any]:
        monitor = getattr(self.support, "_decay_monitor", None)
        if monitor is None or not callable(getattr(monitor, "daily_check", None)):
            return {"status": "skipped", "reason": "decay_monitor_unavailable"}
        return dict(
            await monitor.daily_check(self.support._active_pool, db=db) or {}
        )

    async def persist_decay_report(self, db: Any, report: dict[str, Any]) -> None:
        persist = getattr(self.support, "_persist_decay_report", None)
        if callable(persist):
            await persist(db, report)

    async def persist_decay_updates(self, db: Any, report: dict[str, Any]) -> None:
        persist = getattr(self.support, "_persist_decay_updates", None)
        if callable(persist):
            await persist(db, report)

    async def promote_quarantine_factors(self, db: Any) -> dict[str, Any]:
        promote = getattr(self.support, "_promote_quarantine_factors", None)
        if not callable(promote):
            return {"status": "skipped", "reason": "promotion_unavailable"}
        return dict(await promote(db) or {})

    async def run_qc_pipeline(self, db: Any) -> dict[str, Any]:
        pipeline = getattr(self.support, "_run_qc_pipeline", None)
        if not callable(pipeline):
            return {"status": "skipped", "reason": "qc_pipeline_unavailable"}
        return dict(await pipeline(db) or {})

    # --- QC / validation seams ------------------------------------------------

    async def _call_host_analysis(
        self,
        attribute: str,
        db: Any,
        factor: dict[str, Any],
        codes: list[str] | None,
    ) -> dict[str, Any]:
        runner = getattr(self.support, attribute, None)
        if not callable(runner):
            return {"status": "skipped", "reason": f"{attribute}_unavailable"}
        return dict(await runner(db, factor, codes) or {})

    async def run_factor_oos_validation(self, db, factor, codes=None):
        return await self._call_host_analysis("_run_factor_oos_validation", db, factor, codes)

    async def run_factor_group_backtest(self, db, factor, codes=None):
        return await self._call_host_analysis("_run_factor_group_backtest", db, factor, codes)

    async def run_factor_robustness_check(self, db, factor, codes=None):
        return await self._call_host_analysis("_run_factor_robustness_check", db, factor, codes)

    async def validate_factor_candidate(self, db, candidate, **kwargs):
        runner = getattr(self.support, "_validate_factor_candidate", None)
        if not callable(runner):
            return {"status": "skipped", "reason": "validation_pipeline_unavailable"}
        return dict(await runner(db, candidate, **kwargs) or {})

    def compile_factor_candidate(self, candidate: dict[str, Any]) -> dict[str, Any]:
        compiler = getattr(self.support, "_compile_factor_candidate", None)
        if not callable(compiler):
            return {"ok": False, "reason": "compiler_unavailable"}
        return dict(compiler(candidate) or {})

    # --- pool / gateway seams -------------------------------------------------

    async def load_factor_pool(self, db, *, statuses=None, limit=200):
        loader = getattr(self.support, "_load_factor_pool", None)
        if not callable(loader):
            return []
        return list(await loader(db, statuses=statuses, limit=limit) or [])

    def optimize_factor_weights(self, metrics, *, method="max_sharpe"):
        optimizer = getattr(self.support, "_optimize_factor_weights", None)
        if not callable(optimizer):
            return {"weights": {}, "reason": "optimizer_unavailable"}
        return dict(optimizer(metrics, method=method) or {})

    async def write_factor_performance(self, db, payload) -> None:
        writer = getattr(self.support, "_write_factor_performance", None)
        if callable(writer):
            await writer(db, payload)

    def get_factor_metrics(self, factor_ids: list[str]) -> dict[str, Any]:
        getter = getattr(self.support, "_get_factor_metrics", None)
        if callable(getter):
            return dict(getter(factor_ids) or {})
        # Fall back to the legacy two-level private access, contained here only.
        pool = getattr(self.support, "_active_pool", None)
        factors = getattr(pool, "_factors", None)
        if not isinstance(factors, dict):
            return {}
        return {
            factor_id: factors[factor_id]
            for factor_id in factor_ids
            if factor_id in factors
        }

    def get_factor_llm_provider(self) -> Any:
        loader = getattr(self.support, "_get_factor_llm_provider", None)
        return loader() if callable(loader) else None


class LegacySignalTrackerProvider:
    def __init__(self, support: Any):
        self.support = support

    async def get_db(self) -> Any:
        db = get_db_provider()()
        return await db if hasattr(db, "__await__") else db

    def get_default_universe(self) -> list[dict[str, Any]]:
        # Legacy trackers often return stock-code strings here.  Those codes
        # are signal-generation inputs, not strategy records, and must not be
        # merged into the application-layer strategy universe.
        return [
            item
            for item in list(self.support._get_default_universe())
            if isinstance(item, dict)
        ]

    async def load_executable_strategies(self, db, *, limit=500):
        return await self.support._load_executable_strategies_with_fallback(
            db, limit=limit, use_contract=True
        )

    async def load_runtime_submitted_strategies(self, db, *, limit=200):
        method = getattr(self.support, "_load_runtime_submitted_strategies", None)
        return await method(db, limit=limit) if callable(method) else []

    async def load_runtime_observation_strategies(self, db, *, limit=200):
        method = getattr(self.support, "_load_runtime_observation_strategies", None)
        return await method(db, limit=limit) if callable(method) else []

    async def get_klines(self, db, code, *, limit=200):
        method = getattr(self.support, "_get_klines_with_fallback", None)
        return await method(db, code, limit=limit) if callable(method) else []

    async def generate_signals(self, db, strategy, *, as_of: date):
        method = getattr(self.support, "_generate_signals", None)
        if callable(method):
            return await method(db, strategy, as_of=as_of)
        return {"signals_generated": 0}

    async def create_signal_event_snapshot(self, db, strategy, signal_result, *, as_of: date):
        method = getattr(self.support, "_create_signal_event_snapshot", None)
        if callable(method):
            return await method(db, strategy, signal_result, as_of=as_of)
        return {"snapshot_created": False}

    async def backfill_forward_returns(self, db, **kwargs):
        method = getattr(self.support, "backfill_forward_returns", None)
        if not callable(method):
            method = getattr(self.support, "_backfill_forward_returns", None)
        result = await method(db, **kwargs) if callable(method) else {}
        payload = dict(result or {})
        payload.setdefault("computed_count", int(payload.get("computed") or 0))
        return payload

    async def _incubation_result(self, db, strategies, *, as_of=None):
        service = get_strategy_incubation_service()
        if service is None:
            return {}
        # ``as_of`` comes from the orchestrator's requested signal date. Using
        # the wall clock here meant a historical replay generated signals for the
        # requested date but wrote orders/NAV/metrics under today's date.
        return await service.process_strategies(
            db, strategies, signal_date=as_of or date.today()
        )

    async def sync_incubation_orders(self, db, strategies, *, as_of=None):
        result = await self._incubation_result(db, strategies, as_of=as_of)
        return {"orders_synced": result.get("orders_created", 0), "filled_count": result.get("orders_filled", 0)}

    async def sync_incubation_nav_snapshots(self, db, strategies, *, as_of=None):
        result = await self._incubation_result(db, strategies, as_of=as_of)
        return {"snapshots_created": result.get("nav_snapshots", 0)}

    async def sync_incubation_metrics(self, db, strategies, *, as_of=None):
        result = await self._incubation_result(db, strategies, as_of=as_of)
        return {"metrics_recorded": result.get("metrics_recorded", 0)}

    async def run_incubation_pipeline(self, db, strategies):
        service = get_strategy_incubation_pipeline_service()
        result = await service.run_batch(db, strategies=strategies) if service else {}
        return {"snapshots_created": result.get("count", 0), "auto_promoted": result.get("auto_promoted", 0)}

    async def run_submitted_runtime_pipeline(self, db, strategies):
        service = get_strategy_incubation_pipeline_service()
        if service:
            for strategy in strategies:
                await service.run_strategy(db, strategy)
        return {"snapshots_created": len(strategies) if service else 0}

    async def run_runtime_risk_scan(self, db, strategies):
        service = get_strategy_runtime_risk_service()
        result = await service.scan(db, strategies, enforce_actions=True) if service else {}
        return {"events_detected": result.get("event_count", 0), "actions_taken": result.get("action_count", 0)}

    async def run_lifecycle_scan(self, db, strategies):
        del strategies
        runner = get_strategy_lifecycle_scan_runner()
        result = await runner(db) if callable(runner) else {}
        transitions = result.get("transitions", 0)
        return {"transitions": len(transitions) if isinstance(transitions, list) else int(transitions or 0)}

    async def reconcile_vector_registry(self, db, strategies):
        service = get_strategy_vector_governance_service()
        result = await service.reconcile_registry(db, strategies=strategies) if service else {}
        return {"updates": result.get("registry_updated", 0)}

    async def snapshot_domain_projections(self, db, strategies):
        service = get_strategy_domain_projection_service()
        result = await service.rebuild_batch(db, strategies=strategies) if service else {}
        return {"snapshots_created": result.get("count", 0)}

    def phase_timeout_seconds(self, phase_name: str) -> float:
        return float(self.support._phase_timeout_seconds(phase_name))
