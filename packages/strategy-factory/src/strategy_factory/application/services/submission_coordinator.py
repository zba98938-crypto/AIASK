"""Submission orchestration helpers for strategy factory candidate handling."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Optional

from ...infrastructure.factory_repository import FactoryRepository
from .._runtime_toggles import diagnostic_observation_final_status


async def _persist(db: Any, method: str, *args: Any, **kwargs: Any) -> Any:
    """Route a required factory-domain write through the repository facade.

    These writes must not degrade silently — a missing capability on the
    submission path is a real failure. Going through the facade turns an
    ``AttributeError`` deep in the call stack into a ``MissingCapability`` that
    names the method, and enforces that only factory-domain methods are reachable
    from here.
    """
    return await FactoryRepository(db).call(method, *args, **kwargs)


@dataclass(slots=True)
class SubmissionExecutionOptions:
    read_only: bool = False
    source: str = "strategy_factory_submit"
    record_only: bool = False


class StrategyUpsertService:
    """Owns strategy persistence before post-gate lifecycle actions."""

    def __init__(self, submitter: Any) -> None:
        self._submitter = submitter

    async def persist_candidate(
        self,
        *,
        strategy_id: str,
        candidate: dict[str, Any],
        data: dict[str, Any],
        metrics: dict[str, Any],
        validation_report: Optional[dict[str, Any]],
        risk_report: Optional[dict[str, Any]],
        gate: dict[str, Any],
        db: Any,
        refresh_existing: bool,
        read_only: bool,
        source: str = "strategy_factory_submit",
    ) -> bool:
        # A-class promotion gates were removed: the quality verdict no longer
        # suppresses persistence of an existing strategy being refreshed.
        should_persist_strategy = True
        if read_only or not should_persist_strategy:
            return should_persist_strategy
        initial_status = None
        if not refresh_existing:
            is_diagnostic_observation = bool(candidate.get("diagnostic_observation"))
            initial_status = (
                diagnostic_observation_final_status()
                if is_diagnostic_observation
                else "submitted"
            )
            data = {**dict(data or {}), "status": initial_status}
        await _persist(db, "save_strategy", data)
        await self._persist_trade_prediction(
            strategy_id=strategy_id,
            candidate=candidate,
            data=data,
            gate=gate,
            db=db,
            source=source,
        )
        await self._submitter._persist_metrics(strategy_id, metrics, validation_report, risk_report, db)
        if not refresh_existing:
            strategy_explanation = dict(
                (data.get("params") or {}).get("strategy_explanation")
                or candidate.get("strategy_explanation")
                or {}
            )
            await self._submitter._update_strategy_status(
                db,
                strategy_id,
                initial_status,
                actor_id="strategy_factory",
                reason=(
                    "factory_submit_diagnostic_observation"
                    if is_diagnostic_observation
                    else "factory_submit"
                ),
                metadata={
                    "spawn_reason": candidate.get("spawn_reason"),
                    "strategy_explanation": strategy_explanation,
                    "strategy_explanation_summary": strategy_explanation.get("summary"),
                    "strategy_explanation_labels": list(strategy_explanation.get("labels") or []),
                    "dedup_result": candidate.get("dedup_result") or {},
                    "incubation_budget": dict(candidate.get("incubation_budget") or {}),
                    "diagnostic_observation": bool(candidate.get("diagnostic_observation")),
                    "diagnostic_fingerprint": candidate.get("diagnostic_fingerprint"),
                    "diagnostic_reason": candidate.get("diagnostic_reason"),
                },
            )
        return should_persist_strategy

    async def _persist_trade_prediction(
        self,
        *,
        strategy_id: str,
        candidate: dict[str, Any],
        data: dict[str, Any],
        gate: dict[str, Any],
        db: Any,
        source: str,
    ) -> bool:
        params = dict((data or {}).get("params") or {})
        contract = dict(
            params.get("trade_prediction_contract")
            or (candidate or {}).get("trade_prediction_contract")
            or {}
        )
        status = str(
            params.get("trade_prediction_contract_status")
            or (candidate or {}).get("trade_prediction_contract_status")
            or ""
        ).strip().lower()
        contract_hash = str(
            params.get("trade_prediction_contract_hash")
            or contract.get("contract_hash")
            or (candidate or {}).get("trade_prediction_contract_hash")
            or ""
        ).strip()
        if status != "ready" or not contract or not contract_hash:
            return False
        resolver = getattr(self._submitter, "_get_optional_db_method", None)
        save_method = (
            resolver(db, "save_strategy_trade_prediction")
            if callable(resolver)
            else getattr(db, "save_strategy_trade_prediction", None)
        )
        if save_method is not None and not callable(save_method):
            save_method = None
        if save_method is None:
            return False
        contract = {
            **contract,
            "strategy_id": strategy_id,
            "contract_hash": contract_hash,
        }
        prediction_id = str(
            params.get("trade_prediction_id")
            or contract.get("prediction_id")
            or f"tp_{strategy_id}_{contract_hash[:16]}"
        ).strip()
        payload = {
            "prediction_id": prediction_id,
            "strategy_id": strategy_id,
            "stock_code": contract.get("stock_code"),
            "prediction_as_of": contract.get("prediction_as_of"),
            "target_trading_date": contract.get("target_trading_date"),
            "direction": contract.get("direction"),
            "confidence": contract.get("confidence"),
            "horizon": contract.get("horizon"),
            "contract_version": contract.get("contract_version"),
            "contract_source": contract.get("contract_source"),
            "contract_hash": contract_hash,
            "contract_json": contract,
            "prediction_status": "pending",
            "metadata": {
                "source": source or "strategy_factory_submit",
                "candidate_id": (candidate or {}).get("id") or (candidate or {}).get("candidate_id"),
                "submission_lane": (gate or {}).get("submission_lane"),
                "trade_prediction_contract_status": status,
            },
        }
        try:
            result = save_method(payload)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception as exc:
            dlq = getattr(self._submitter, "_persistence_dlq", None)
            if dlq is None:
                dlq = []
                setattr(self._submitter, "_persistence_dlq", dlq)
            dlq.append(
                {
                    "strategy_id": strategy_id,
                    "period": "trade_prediction",
                    "payload": payload,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "attempts": 1,
                }
            )
            return False


class ExperimentRecorder:
    """Handles submission quality report persistence and experiment recording."""

    def __init__(self, submitter: Any) -> None:
        self._submitter = submitter

    async def save_quality_report(
        self,
        db: Any,
        strategy_id: str,
        quality_report: dict[str, Any],
        *,
        read_only: bool,
        record_only: bool = False,
    ) -> None:
        if read_only or record_only:
            return
        if self._submitter._get_optional_db_method(db, "save_strategy_quality_report") is None:
            return
        await db.save_strategy_quality_report(strategy_id, "submission", quality_report)


class ExistingRefreshLaneHandler:
    def __init__(self, submitter: Any) -> None:
        self._submitter = submitter

    async def handle(
        self,
        *,
        strategy_id: str,
        name: str,
        candidate: dict[str, Any],
        gate: dict[str, Any],
        quality_report: dict[str, Any],
        backtest_metrics: Optional[dict[str, Any]],
        snapshot: dict[str, Any],
        validation_report: Optional[dict[str, Any]],
        risk_report: Optional[dict[str, Any]],
        db: Any,
        existing_status: str,
        submission_lane: str,
        submission_action: Optional[dict[str, Any]],
        read_only: bool,
    ) -> dict[str, Any]:
        if read_only:
            self._submitter._apply_submission_action_audit(
                quality_report,
                final_status=existing_status,
                submission_lane=submission_lane,
                submission_audit=dict(submission_action or {}),
            )
            return {
                "refreshed_existing": True,
                "reused_existing_strategy_id": strategy_id,
                "existing_status": existing_status,
                "submission_lane": submission_lane,
                "final_status": existing_status,
                "read_only": True,
                **dict(submission_action or {}),
            }
        return await self._submitter._handle_existing_refresh(
            strategy_id,
            name,
            candidate,
            gate,
            quality_report,
            backtest_metrics,
            snapshot,
            validation_report,
            risk_report,
            db,
            existing_status=existing_status,
            submission_lane=submission_lane,
            submission_action=submission_action,
        )


class FormalIncubationLaneHandler:
    def __init__(self, submitter: Any) -> None:
        self._submitter = submitter

    async def handle(self, **kwargs: Any) -> dict[str, Any]:
        return await self._submitter._handle_post_gate(**kwargs)


class LiveReadyReviewLaneHandler:
    def __init__(self, submitter: Any) -> None:
        self._submitter = submitter

    async def handle(self, **kwargs: Any) -> dict[str, Any]:
        return await self._submitter._handle_post_gate(**kwargs)


class ObserveIncubationLaneHandler:
    def __init__(self, submitter: Any) -> None:
        self._submitter = submitter

    async def handle(self, **kwargs: Any) -> dict[str, Any]:
        return await self._submitter._handle_post_gate(**kwargs)


class SubmissionCoordinator:
    """Coordinates individual candidate submission decisions and lane handling."""

    def __init__(self, submitter: Any) -> None:
        self._submitter = submitter
        self._upsert_service = StrategyUpsertService(submitter)
        self._experiment_recorder = ExperimentRecorder(submitter)
        self._existing_refresh_handler = ExistingRefreshLaneHandler(submitter)
        self._formal_incubation_handler = FormalIncubationLaneHandler(submitter)
        self._live_ready_handler = LiveReadyReviewLaneHandler(submitter)
        self._observe_incubation_handler = ObserveIncubationLaneHandler(submitter)

    async def persist_candidate(
        self,
        *,
        strategy_id: str,
        candidate: dict[str, Any],
        data: dict[str, Any],
        metrics: dict[str, Any],
        validation_report: Optional[dict[str, Any]],
        risk_report: Optional[dict[str, Any]],
        gate: dict[str, Any],
        db: Any,
        refresh_existing: bool,
        options: SubmissionExecutionOptions,
    ) -> bool:
        return await self._upsert_service.persist_candidate(
            strategy_id=strategy_id,
            candidate=candidate,
            data=data,
            metrics=metrics,
            validation_report=validation_report,
            risk_report=risk_report,
            gate=gate,
            db=db,
            refresh_existing=refresh_existing,
            read_only=options.read_only,
            source=options.source,
        )

    async def handle_existing_refresh(
        self,
        *,
        strategy_id: str,
        name: str,
        candidate: dict[str, Any],
        gate: dict[str, Any],
        quality_report: dict[str, Any],
        backtest_metrics: Optional[dict[str, Any]],
        snapshot: dict[str, Any],
        validation_report: Optional[dict[str, Any]],
        risk_report: Optional[dict[str, Any]],
        db: Any,
        existing_status: str,
        submission_lane: str,
        submission_action: Optional[dict[str, Any]],
        options: SubmissionExecutionOptions,
    ) -> dict[str, Any]:
        return await self._existing_refresh_handler.handle(
            strategy_id=strategy_id,
            name=name,
            candidate=candidate,
            gate=gate,
            quality_report=quality_report,
            backtest_metrics=backtest_metrics,
            snapshot=snapshot,
            validation_report=validation_report,
            risk_report=risk_report,
            db=db,
            existing_status=existing_status,
            submission_lane=submission_lane,
            submission_action=submission_action,
            read_only=options.read_only,
        )

    async def handle_new_candidate(
        self,
        *,
        strategy_id: str,
        name: str,
        candidate: dict[str, Any],
        data: dict[str, Any],
        gate: dict[str, Any],
        quality_report: dict[str, Any],
        backtest_metrics: Optional[dict[str, Any]],
        snapshot: dict[str, Any],
        validation_report: Optional[dict[str, Any]],
        risk_report: Optional[dict[str, Any]],
        db: Any,
        submission_lane: str,
        submission_action: Optional[dict[str, Any]],
        options: SubmissionExecutionOptions,
    ) -> dict[str, Any]:
        if options.read_only:
            final_status = str((submission_action or {}).get("final_status") or "submitted")
            audit = dict(submission_action or {})
            self._submitter._apply_submission_action_audit(
                quality_report,
                final_status=final_status,
                submission_lane=submission_lane,
                submission_audit={
                    **audit,
                    "submission_action_completed": False,
                    "diagnostic_only": True,
                },
            )
            return {
                "submission_lane": submission_lane,
                "final_status": final_status,
                "submission_action": dict(audit.get("submission_action") or {}),
                "submission_action_type": audit.get("submission_action_type"),
                "submission_action_trigger": audit.get("submission_action_trigger"),
                "submission_action_gaps": list(audit.get("submission_action_gaps") or []),
                "submission_action_fallback_conditions": list(
                    audit.get("submission_action_fallback_conditions") or []
                ),
                "submission_action_next_step": audit.get("submission_action_next_step"),
                "submission_action_completed": False,
                "diagnostic_only": True,
                "read_only": True,
            }

        if submission_lane == "formal_incubation":
            handler = self._formal_incubation_handler
        elif submission_lane == "live_ready_review":
            handler = self._live_ready_handler
        else:
            handler = self._observe_incubation_handler
        return await handler.handle(
            strategy_id=strategy_id,
            name=name,
            candidate=candidate,
            data=data,
            gate=gate,
            quality_report=quality_report,
            backtest_metrics=backtest_metrics,
            snapshot=snapshot,
            validation_report=validation_report,
            risk_report=risk_report,
            db=db,
            submission_lane=submission_lane,
            submission_action=submission_action,
        )

    async def save_quality_report(
        self,
        db: Any,
        strategy_id: str,
        quality_report: dict[str, Any],
        *,
        options: SubmissionExecutionOptions,
    ) -> None:
        await self._experiment_recorder.save_quality_report(
            db,
            strategy_id,
            quality_report,
            read_only=options.read_only,
            record_only=options.record_only,
        )


__all__ = [
    "ExperimentRecorder",
    "ExistingRefreshLaneHandler",
    "FormalIncubationLaneHandler",
    "LiveReadyReviewLaneHandler",
    "ObserveIncubationLaneHandler",
    "SubmissionCoordinator",
    "SubmissionExecutionOptions",
    "StrategyUpsertService",
]
