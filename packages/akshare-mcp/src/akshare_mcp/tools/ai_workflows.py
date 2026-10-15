"""AI-facing narrow MCP workflow tools."""


from __future__ import annotations

import asyncio
import math
import time
from typing import Any
from uuid import uuid4

from ..services.artifact_registry import get_artifact_async, register_artifact_async
from ..services.lineage_tracker import LineageContext
from ..services.stock_deep_analysis import run_stock_deep_analysis
from ..utils import normalize_code, resolve_existing_security_code_async
from ._decision_unified import get_unified_decision_summary
from .data_quality import build_quality_meta, infer_missing_fields
from .finance import get_financials
from .manager_protocol import fail_with_meta, ok_with_meta
from .managers.quant_manager import quant_manager
from .managers.strategy_manager import strategy_manager
from .market.kline import get_kline
from .pit_middleware import build_pit_meta_simple
from .skills_strategy_workflows import build_strategy_review_workflow_payload
from .tool_catalog import build_tool_meta
import logging

logger = logging.getLogger(__name__)


def _normalize_codes(code: str | None = None, codes: list[str] | None = None) -> list[str]:
    values = list(codes or [])
    if code:
        values.append(code)
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        token = normalize_code(str(raw or ""))
        if not token or token in seen:
            continue
        seen.add(token)
        normalized.append(token)
    return normalized


def _response_success(response: Any) -> bool:
    return not isinstance(response, dict) or response.get("success", True) is not False


def _response_data(response: Any) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return dict(data) if isinstance(data, dict) else {}


def _nested_dict(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    nested = value.get(key)
    return dict(nested) if isinstance(nested, dict) else {}


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/ai_workflows.py:65: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None
    return parsed if math.isfinite(parsed) else None


_BINARY_LABEL_TRUE = {"1", "true", "t", "yes", "y", "hit", "correct", "success", "positive"}
_BINARY_LABEL_FALSE = {"0", "false", "f", "no", "n", "miss", "incorrect", "failure", "negative"}


def _normalize_binary_outcomes(values: list[Any] | None) -> list[float]:
    normalized: list[float] = []
    for index, item in enumerate(list(values or [])):
        if isinstance(item, bool):
            normalized.append(1.0 if item else 0.0)
            continue
        numeric = _safe_float(item)
        if numeric is not None:
            if numeric in {0.0, 1.0}:
                normalized.append(float(numeric))
                continue
            raise ValueError(
                f"labels/outcomes must contain binary observed outcomes (0/1); "
                f"received {item!r} at index {index}"
            )
        text = str(item or "").strip().lower()
        if text in _BINARY_LABEL_TRUE:
            normalized.append(1.0)
            continue
        if text in _BINARY_LABEL_FALSE:
            normalized.append(0.0)
            continue
        raise ValueError(
            "labels/outcomes must be binary observed outcomes aligned 1:1 with probabilities. "
            f"Unsupported label at index {index}: {item!r}. "
            "Class-name labels like ['up', 'flat', 'down'] are not supported by this workflow."
        )
    return normalized


def _normalize_registry_status(registry_stage: Any) -> str:
    stage = str(registry_stage or "").strip().lower()
    if stage in {"governed", "challenger", "champion"}:
        return "active"
    if stage == "retired":
        return "retired"
    if stage in {"draft", "validated"}:
        return "candidate"
    return "unregistered"


def _derive_registry_status(
    *,
    generation_data: dict[str, Any],
    validation_data: dict[str, Any],
    registry_item: dict[str, Any],
) -> str:
    candidate_resolution = _nested_dict(validation_data, "candidate_resolution")
    rating = _nested_dict(validation_data, "rating")
    governance = _nested_dict(validation_data, "governance")
    rating_governance = _nested_dict(rating, "governance")
    registry_lineage = _nested_dict(registry_item, "lineage")

    for stage in (
        registry_item.get("registry_stage"),
        _nested_dict(registry_item, "governance").get("registry_stage"),
        governance.get("registry_stage"),
        rating_governance.get("registry_stage"),
        validation_data.get("registry_stage"),
        candidate_resolution.get("registry_stage"),
    ):
        normalized = _normalize_registry_status(stage)
        if normalized != "unregistered":
            return normalized

    if registry_item or validation_data or generation_data:
        return "candidate"
    return "unregistered"


def _extract_existing_factor_pool(
    *,
    registry_pool_data: dict[str, Any],
    registry_active_pool_data: dict[str, Any],
    exclude_artifact_id: str | None = None,
) -> list[str]:
    exclude_id = str(exclude_artifact_id or "").strip()
    values: list[str] = []
    seen: set[str] = set()

    for item in list(registry_pool_data.get("items") or []):
        if not isinstance(item, dict):
            continue
        if exclude_id and str(item.get("artifact_id") or "").strip() == exclude_id:
            continue
        candidate = _nested_dict(item, "candidate")
        descriptor = (
            str(candidate.get("expression_dsl") or "").strip()
            or " ".join(
                part
                for part in (
                    str(candidate.get("family") or "").strip(),
                    str(candidate.get("name") or "").strip(),
                )
                if part
            ).strip()
        )
        if descriptor and descriptor not in seen:
            seen.add(descriptor)
            values.append(descriptor)

    if values:
        return values

    active_pool = _nested_dict(registry_active_pool_data, "active_pool")
    for item in list(active_pool.get("top_candidates") or []):
        if not isinstance(item, dict):
            continue
        if exclude_id and str(item.get("artifact_id") or "").strip() == exclude_id:
            continue
        descriptor = " ".join(
            part
            for part in (
                str(item.get("family") or "").strip(),
                str(item.get("name") or "").strip(),
            )
            if part
        ).strip()
        if descriptor and descriptor not in seen:
            seen.add(descriptor)
            values.append(descriptor)
    return values


def _resolve_memory_record_id(
    *,
    validation_data: dict[str, Any],
    registry_item: dict[str, Any],
) -> str | None:
    validation_lineage = _nested_dict(validation_data, "lineage")
    memory_record = _nested_dict(validation_data, "memory_record")
    registry_lineage = _nested_dict(registry_item, "lineage")
    for value in (
        memory_record.get("artifact_id"),
        validation_lineage.get("memory_record_id"),
        registry_item.get("memory_record_id"),
        registry_lineage.get("memory_record_id"),
    ):
        token = str(value or "").strip()
        if token:
            return token
    return None


def _extract_rank_ic_history(validation_data: dict[str, Any]) -> list[float]:
    factor_validation_report = _nested_dict(validation_data, "factor_validation_report")
    cross_section = _nested_dict(factor_validation_report, "cross_section")
    history: list[float] = []
    for row in list(cross_section.get("dates") or []):
        if not isinstance(row, dict):
            continue
        rank_ic = _safe_float(row.get("rank_ic"))
        if rank_ic is None:
            continue
        history.append(rank_ic)
    return history


def _derive_decay_monitor_status(
    *,
    validation_data: dict[str, Any],
    memory_item: dict[str, Any],
) -> str:
    runtime_feedback = [
        dict(item)
        for item in list(memory_item.get("runtime_feedback") or [])
        if isinstance(item, dict)
    ]
    latest_feedback = runtime_feedback[0] if runtime_feedback else {}
    latest_recommended_action = str(
        latest_feedback.get("recommended_action")
        or memory_item.get("last_feedback_recommended_action")
        or ""
    ).strip().lower()
    memory_status = str(memory_item.get("status") or "").strip().lower()

    decay_detected = bool(latest_feedback.get("decay_detected"))
    regime_shift_detected = bool(latest_feedback.get("regime_shift_detected"))
    if decay_detected or regime_shift_detected:
        if latest_recommended_action in {"deprecate", "retire", "retired", "replace"}:
            return "decayed"
        return "decaying"
    if memory_status == "degraded":
        if latest_recommended_action in {"deprecate", "retire", "retired", "replace"}:
            return "decayed"
        return "decaying"
    if runtime_feedback:
        return "stable"

    ic_history = _extract_rank_ic_history(validation_data)
    if len(ic_history) >= 4:
        try:
            from ..services.governance_monitor import check_factor_decay

            decay_report = check_factor_decay("factor_candidate", ic_history)
            decay_status = str(decay_report.get("decay_status") or "").strip().lower()
            if decay_status in {"stable", "decaying", "decayed"}:
                return decay_status
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/ai_workflows.py:272: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

    return "not_monitored"


def _step(step: str, response: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "step": step,
        "success": _response_success(response),
    }
    if isinstance(response, dict):
        payload["output"] = response
    else:
        payload["output"] = {"value": response}
    return payload


def _collect_failed_steps(steps: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("step") or "") for item in steps if not item.get("success")]


def _budget_fail_response(
    *,
    step: str,
    message: str,
    timeout_sec: float | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "success": False,
        "error": message,
        "data": {"step": step, "degraded": True},
    }
    if timeout_sec is not None:
        safe_timeout = _safe_float(timeout_sec)
        payload["data"]["timeout_sec"] = round(safe_timeout if safe_timeout is not None else 0.0, 3)
    return payload


async def _run_workflow_stage(
    *,
    step: str,
    coro: Any,
    deadline: float,
    stage_timeout: float,
) -> dict[str, Any]:
    remaining = deadline - time.perf_counter()
    if remaining <= 0.5:
        return _budget_fail_response(step=step, message="workflow_budget_exhausted", timeout_sec=0.0)

    safe_stage_timeout = _safe_float(stage_timeout)
    timeout_sec = max(0.5, min(safe_stage_timeout if safe_stage_timeout is not None else remaining, remaining))
    try:
        return await asyncio.wait_for(coro, timeout=timeout_sec)
    except asyncio.TimeoutError:
        return _budget_fail_response(step=step, message=f"timeout>{timeout_sec:.1f}s", timeout_sec=timeout_sec)
    except Exception as exc:
        return _budget_fail_response(step=step, message=f"{type(exc).__name__}: {exc}", timeout_sec=timeout_sec)


def _meta_quality(
    *,
    workflow_name: str,
    steps: list[dict[str, Any]],
    minimum_quality_passed: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    failed_steps = _collect_failed_steps(steps)
    quality = {
        "status": "good" if not failed_steps else "partial_failed",
        "failed_steps": failed_steps,
        "step_count": len(steps),
        "successful_steps": len(steps) - len(failed_steps),
        "workflow": workflow_name,
    }
    if minimum_quality_passed is not None:
        quality["minimum_quality_passed"] = minimum_quality_passed
    if extra:
        quality.update(extra)
    return quality


async def _persist_optional_artifact(
    *,
    enabled: bool,
    artifact_id: str | None,
    strategy: str,
    payload: dict[str, Any],
) -> str | None:
    if not enabled and not artifact_id:
        return None
    resolved_artifact_id = str(artifact_id or f"{strategy}_{int(time.time())}_{uuid4().hex[:8]}")
    artifact_payload = dict(payload or {})
    artifact_payload["artifact_id"] = resolved_artifact_id
    artifact_payload.setdefault("strategy", strategy)
    artifact_payload.setdefault("strategy_version", "workflow_v1")
    await register_artifact_async(
        artifact_payload
    )
    return resolved_artifact_id


def register(mcp) -> None:
    @mcp.tool(
        title="Analyze Stock Workflow",
        description="AI-facing stock snapshot workflow with profile, kline, financial and decision context.",
        structured_output=True,
        meta=build_tool_meta("analyze_stock_workflow"),
    )
# --- ai_workflows_parts/parsers.py ---
    async def analyze_stock_workflow(
        code: str,
        investment_style: str = "balanced",
        include_kline: bool = True,
        include_financials: bool = True,
        include_decision: bool = True,
        kline_limit: int = 90,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        validated_code, _, validation_error = await resolve_existing_security_code_async(code=code)
        resolved_code = normalize_code(code)
        lineage = LineageContext.create(
            "analyze_stock_workflow",
            security_code=resolved_code,
        )
        try:
            if validation_error:
                return fail_with_meta(
                    validation_error,
                    tool_name="analyze_stock_workflow",
                    action="run",
                    started_at=started_at,
                    source_chain=["workflow.analyze_stock"],
                    extra_meta={
                        "side_effect": {"level": "read_only", "target": resolved_code, "confirmation_required": False},
                        "pit": build_pit_meta_simple(as_of),
                        "lineage": lineage.to_meta(),
                        "quality": {"status": "invalid_params", "workflow": "analyze_stock_workflow"},
                        "degraded": True,
                    },
                )
            resolved_code = str(validated_code)
            from ..resources.stock_and_watchlist import build_stock_profile_resource_payload

            steps: list[dict[str, Any]] = []
            source_chain = ["workflow.analyze_stock"]

            child_profile = lineage.child("stock_profile")
            profile_payload = await build_stock_profile_resource_payload(resolved_code)
            steps.append(_step("stock_profile", {"success": bool(profile_payload.get("found", True)), "data": profile_payload}))
            source_chain.append("resource.stock_profile")

            kline_payload: dict[str, Any] | None = None
            if include_kline:
                child_kline = lineage.child("daily_kline")
                kline_payload = await get_kline(stock_code=resolved_code, period="daily", limit=max(20, min(int(kline_limit or 90), 240)))
                steps.append(_step("daily_kline", kline_payload))
                source_chain.append("tool.get_kline")

            financial_payload: dict[str, Any] | None = None
            if include_financials:
                child_fin = lineage.child("financials")
                financial_payload = await get_financials(stock_code=resolved_code)
                steps.append(_step("financials", financial_payload))
                source_chain.append("tool.get_financials")

            decision_payload: dict[str, Any] | None = None
            if include_decision:
                child_decision = lineage.child("decision_summary")
                decision_payload = await get_unified_decision_summary(code=resolved_code, investment_style=investment_style)
                steps.append(_step("decision_summary", decision_payload))
                source_chain.append("tool.get_unified_decision_summary")

            failed_steps = _collect_failed_steps(steps)
            degraded = bool(failed_steps)
            result = {
                "workflow": "analyze_stock_workflow",
                "code": resolved_code,
                "steps": steps,
                "summary": {
                    "profile_found": bool(profile_payload.get("found", False)),
                    "quote_price": ((profile_payload.get("realtime_quote") or {}).get("price")),
                    "decision_action": _response_data(decision_payload).get("action"),
                    "failed_steps": failed_steps,
                },
                "artifacts": {
                    "stock_profile_resource": f"resource://stock/{resolved_code}/profile",
                },
            }
            return ok_with_meta(
                result,
                tool_name="analyze_stock_workflow",
                action="run",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta={
                    "quality": _meta_quality(workflow_name="analyze_stock_workflow", steps=steps),
                    "side_effect": {
                        "level": "read_only",
                        "target": resolved_code,
                        "confirmation_required": False,
                        "idempotent": True,
                    },
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage.to_meta(),
                    "degraded": degraded,
                },
            )
        except Exception as exc:
            return fail_with_meta(
                str(exc),
                tool_name="analyze_stock_workflow",
                action="run",
                started_at=started_at,
                source_chain=["workflow.analyze_stock"],
                error_code="INTERNAL_ERROR",
                extra_meta={
                    "side_effect": {"level": "read_only", "target": resolved_code, "confirmation_required": False},
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage.to_meta(),
                    "quality": {"status": "failed", "workflow": "analyze_stock_workflow"},
                    "degraded": True,
                },
            )

    @mcp.tool(
        title="Analyze Stock Product Workflow",
        description="Unified stock deep-analysis workflow with protocolized stages and persisted report artifacts.",
        structured_output=True,
        meta=build_tool_meta("analyze_stock_product_workflow"),
    )
    async def analyze_stock_product_workflow(
        code: str = "",
        task: str = "deep_analysis",
        investment_style: str = "balanced",
        user_id: str | None = None,
        market: str = "cn",
        run_id: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        source_chain = ["workflow.stock_deep_analysis", "service.stock_deep_analysis"]
        validated_code, _, validation_error = await resolve_existing_security_code_async(code=code)
        lineage = LineageContext.create(
            "analyze_stock_product_workflow",
            security_code=normalize_code(code),
            run_id=run_id,
        )
        try:
            if validation_error:
                return fail_with_meta(
                    validation_error,
                    tool_name="analyze_stock_product_workflow",
                    action=str(task or "deep_analysis"),
                    started_at=started_at,
                    source_chain=source_chain,
                    error_code="INVALID_PARAMS",
                    extra_meta={
                        "side_effect": {"level": "stateful", "target": normalize_code(code), "confirmation_required": False},
                        "pit": build_pit_meta_simple(as_of),
                        "lineage": lineage.to_meta(),
                        "quality": {"status": "invalid_params", "workflow": "analyze_stock_product_workflow"},
                        "degraded": True,
                    },
                )
            payload = await run_stock_deep_analysis(
                code=str(validated_code),
                task=task,
                user_id=user_id,
                investment_style=investment_style,
                market=market,
                run_id=run_id,
            )
            stage_steps = [
                {
                    "step": str(item.get("stage") or ""),
                    "success": bool(item.get("success")),
                    "output": dict(item),
                }
                for item in list(payload.get("steps") or [])
                if isinstance(item, dict)
            ]
            return ok_with_meta(
                payload,
                tool_name="analyze_stock_product_workflow",
                action=str(task or "deep_analysis"),
                started_at=started_at,
                source_chain=source_chain,
                extra_meta={
                    "quality": _meta_quality(
                        workflow_name="analyze_stock_product_workflow",
                        steps=stage_steps,
                        extra={
                            "run_id": payload.get("run_id"),
                            "report_ready": bool((payload.get("summary") or {}).get("report_ready")),
                        },
                    ),
                    "side_effect": {
                        "level": "stateful",
                        "target": str(validated_code),
                        "confirmation_required": False,
                        "idempotent": False,
                    },
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage.to_meta(),
                    "degraded": payload.get("status") != "completed",
                },
            )
        except Exception as exc:
            return fail_with_meta(
                str(exc),
                tool_name="analyze_stock_product_workflow",
                action=str(task or "deep_analysis"),
                started_at=started_at,
                source_chain=source_chain,
                error_code="INTERNAL_ERROR",
                extra_meta={
                    "side_effect": {"level": "stateful", "target": normalize_code(code), "confirmation_required": False},
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage.to_meta(),
                    "quality": {"status": "failed", "workflow": "analyze_stock_product_workflow"},
                    "degraded": True,
                },
            )
# --- ai_workflows_parts/payloads.py ---

    @mcp.tool(
        title="Factor Candidate Workflow",
        description="AI-facing factor candidate workflow for generation, validation, registry review and scheduler checks.",
        structured_output=True,
        meta=build_tool_meta("factor_candidate_workflow"),
    )
    async def factor_candidate_workflow(
        task: str = "pipeline",
        code: str | None = None,
        codes: list[str] | None = None,
        artifact_id: str | None = None,
        candidate_index: int = 0,
        candidate_count: int = 6,
        lookback_bars: int | None = None,
        horizon_days: int | None = None,
        max_dates: int | None = None,
        allow_fallback: bool = True,
        persist_artifact: bool = True,
        write_memory: bool = True,
        run_scheduler_now: bool = False,
        idempotency_key: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        workflow_deadline = started_at + 38.0
        workflow_task = str(task or "pipeline").strip().lower()
        normalized_codes = _normalize_codes(code=code, codes=codes) or ["600519"]
        steps: list[dict[str, Any]] = []
        source_chain = ["workflow.factor_candidate", "manager.quant_manager"]
        lineage_ctx = LineageContext.create(
            "factor_candidate_workflow",
            security_codes=list(normalized_codes),
        )
        try:
            generation_response: dict[str, Any] | None = None
            validation_response: dict[str, Any] | None = None
            registry_response: dict[str, Any] | None = None
            registry_pool_response: dict[str, Any] | None = None
            registry_item_response: dict[str, Any] | None = None
            memory_response: dict[str, Any] | None = None
            memory_item_response: dict[str, Any] | None = None

            if workflow_task in {"pipeline", "generate"}:
                generation_response = await _run_workflow_stage(
                    step="quant_manager.llm_factor_mining",
                    deadline=workflow_deadline,
                    stage_timeout=12.0,
                    coro=quant_manager(
                        action="llm_factor_mining",
                        params={
                            "codes": normalized_codes,
                            "artifact_id": artifact_id,
                            "candidate_count": max(1, min(int(candidate_count or 6), 24)),
                            "lookback_bars": int(lookback_bars) if lookback_bars is not None else None,
                            "allow_fallback": bool(allow_fallback),
                            "workflow_fast_mode": True,
                            "startup_warmup": False,
                            "explain": False,
                            "persist_artifact": bool(persist_artifact),
                        },
                    ),
                )
                steps.append(_step("quant_manager.llm_factor_mining", generation_response))
                gen_artifact_id = _response_data(generation_response).get("artifact_id")
                if gen_artifact_id:
                    lineage_ctx.set_artifact(str(gen_artifact_id))
                    lineage_ctx.extra["generation_artifact_id"] = str(gen_artifact_id)

            resolved_artifact_id = str(
                artifact_id
                or lineage_ctx.artifact_id
                or ""
            ).strip() or None

            if workflow_task in {"pipeline", "validate"} and resolved_artifact_id:
                validation_response = await _run_workflow_stage(
                    step="quant_manager.validate_factor_candidate",
                    deadline=workflow_deadline,
                    stage_timeout=10.0,
                    coro=quant_manager(
                        action="validate_factor_candidate",
                        params={
                            "artifact_id": resolved_artifact_id,
                            "candidate_index": max(0, int(candidate_index or 0)),
                            "codes": normalized_codes,
                            "lookback_bars": int(lookback_bars) if lookback_bars is not None else None,
                            "horizon_days": int(horizon_days) if horizon_days is not None else None,
                            "max_dates": int(max_dates) if max_dates is not None else None,
                            "persist_artifact": bool(persist_artifact),
                            "write_memory": bool(write_memory),
                        },
                    ),
                )
                steps.append(_step("quant_manager.validate_factor_candidate", validation_response))
                val_artifact_id = _response_data(validation_response).get("artifact_id")
                if val_artifact_id:
                    resolved_artifact_id = str(val_artifact_id)
                    lineage_ctx.set_validation(str(val_artifact_id))
                    lineage_ctx.extra["validation_artifact_id"] = str(val_artifact_id)

            if workflow_task in {"pipeline", "registry_review"}:
                registry_response = await _run_workflow_stage(
                    step="quant_manager.factor_candidate_registry",
                    deadline=workflow_deadline,
                    stage_timeout=3.0,
                    coro=quant_manager(
                        action="factor_candidate_registry",
                        params={
                            "op": "active_pool",
                            "artifact_id": resolved_artifact_id,
                            "codes": normalized_codes,
                            "limit": 20,
                        },
                    ),
                )
                steps.append(_step("quant_manager.factor_candidate_registry", registry_response))

                registry_pool_response = await _run_workflow_stage(
                    step="quant_manager.factor_candidate_registry.list",
                    deadline=workflow_deadline,
                    stage_timeout=3.0,
                    coro=quant_manager(
                        action="factor_candidate_registry",
                        params={
                            "op": "list",
                            "codes": normalized_codes,
                            "limit": 20,
                            "only_active": True,
                        },
                    ),
                )
                steps.append(_step("quant_manager.factor_candidate_registry.list", registry_pool_response))

                if resolved_artifact_id:
                    registry_item_response = await _run_workflow_stage(
                        step="quant_manager.factor_candidate_registry.get",
                        deadline=workflow_deadline,
                        stage_timeout=3.0,
                        coro=quant_manager(
                            action="factor_candidate_registry",
                            params={
                                "op": "get",
                                "artifact_id": resolved_artifact_id,
                            },
                        ),
                    )
                    steps.append(_step("quant_manager.factor_candidate_registry.get", registry_item_response))

                memory_response = await _run_workflow_stage(
                    step="quant_manager.factor_research_memory",
                    deadline=workflow_deadline,
                    stage_timeout=3.0,
                    coro=quant_manager(
                        action="factor_research_memory",
                        params={
                            "op": "stats",
                            "artifact_id": resolved_artifact_id,
                            "codes": normalized_codes,
                            "limit": 20,
                        },
                    ),
                )
                steps.append(_step("quant_manager.factor_research_memory", memory_response))

                memory_record_id = _resolve_memory_record_id(
                    validation_data=_response_data(validation_response),
                    registry_item=_nested_dict(_response_data(registry_item_response), "item"),
                )
                if memory_record_id:
                    memory_item_response = await _run_workflow_stage(
                        step="quant_manager.factor_research_memory.get",
                        deadline=workflow_deadline,
                        stage_timeout=3.0,
                        coro=quant_manager(
                            action="factor_research_memory",
                            params={
                                "op": "get",
                                "artifact_id": memory_record_id,
                            },
                        ),
                    )
                    steps.append(_step("quant_manager.factor_research_memory.get", memory_item_response))

            scheduler_response = await _run_workflow_stage(
                step="quant_manager.scheduler_status",
                deadline=workflow_deadline,
                stage_timeout=2.0,
                coro=quant_manager(action="scheduler_status", params={}),
            )
            steps.append(_step("quant_manager.scheduler_status", scheduler_response))
            if run_scheduler_now:
                scheduler_run_response = await _run_workflow_stage(
                    step="quant_manager.scheduler_run_now",
                    deadline=workflow_deadline,
                    stage_timeout=2.0,
                    coro=quant_manager(action="scheduler_run_now", params={}),
                )
                steps.append(_step("quant_manager.scheduler_run_now", scheduler_run_response))

            failed_steps = _collect_failed_steps(steps)
            degraded = bool(failed_steps) or bool(
                _response_data(generation_response).get("fallback_used")
            )

            # P1-4: Factor enrichment
            factor_enrichment_payload: dict[str, Any] | None = None
            try:
                from ..services.factor_enrichment import build_factor_enrichment

                gen_data = _response_data(generation_response)
                expression = gen_data.get("expression") or gen_data.get("factor_expression") or ""
                hypothesis = gen_data.get("hypothesis") or gen_data.get("description") or ""
                val_data = _response_data(validation_response) if validation_response else {}
                registry_item = _nested_dict(_response_data(registry_item_response), "item")
                memory_item = (
                    _nested_dict(_response_data(memory_item_response), "item")
                    or _nested_dict(val_data, "memory_record")
                )
                existing_pool = _extract_existing_factor_pool(
                    registry_pool_data=_response_data(registry_pool_response),
                    registry_active_pool_data=_response_data(registry_response),
                    exclude_artifact_id=resolved_artifact_id,
                )
                enrichment = build_factor_enrichment(
                    expression=str(expression),
                    hypothesis=str(hypothesis) if hypothesis else None,
                    existing_pool=existing_pool or None,
                    category=gen_data.get("category"),
                    validation_result=val_data if val_data else None,
                    registry_status=_derive_registry_status(
                        generation_data=gen_data,
                        validation_data=val_data,
                        registry_item=registry_item,
                    ),
                    decay_monitor_status=_derive_decay_monitor_status(
                        validation_data=val_data,
                        memory_item=memory_item,
                    ),
                )
                factor_enrichment_payload = enrichment.to_dict()
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/ai_workflows.py:839: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass

            result_payload: dict[str, Any] = {
                "workflow": "factor_candidate_workflow",
                "task": workflow_task,
                "codes": normalized_codes,
                "steps": steps,
                "summary": {
                    "artifact_id": resolved_artifact_id,
                    "failed_steps": failed_steps,
                    "fallback_used": bool(_response_data(generation_response).get("fallback_used")),
                    "generation_mode": _response_data(generation_response).get("generation_mode"),
                },
            }
            if factor_enrichment_payload:
                result_payload["factor_enrichment"] = factor_enrichment_payload

            completed_stages = [s["step"] for s in steps if s.get("success")]
            result_payload["workflow_stage"] = {
                "completed_stages": completed_stages,
                "last_completed_stage": completed_stages[-1] if completed_stages else None,
                "recoverable": True,
                "resume_hint": (
                    "validate" if workflow_task == "pipeline" and generation_response and not validation_response
                    else None
                ),
                "resume_artifact_id": (
                    str(_response_data(generation_response).get("artifact_id") or "").strip() or None
                    if generation_response and not validation_response
                    else None
                ),
            }
            return ok_with_meta(
                result_payload,
                tool_name="factor_candidate_workflow",
                action=workflow_task,
                started_at=started_at,
                source_chain=source_chain,
                extra_meta={
                    "quality": _meta_quality(
                        workflow_name="factor_candidate_workflow",
                        steps=steps,
                        extra={"fallback_used": bool(_response_data(generation_response).get("fallback_used"))},
                    ),
                    "side_effect": {
                        "level": "stateful" if (persist_artifact or write_memory or run_scheduler_now) else "read_only",
                        "target": "quant_manager",
                        "confirmation_required": False,
                        "idempotent": False,
                    },
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage_ctx.to_meta(),
                    "idempotency_key": idempotency_key,
                    "degraded": degraded,
                },
            )
        except Exception as exc:
            completed_stages = [s["step"] for s in steps if s.get("success")]
            return fail_with_meta(
                str(exc),
                tool_name="factor_candidate_workflow",
                action=workflow_task,
                started_at=started_at,
                source_chain=source_chain,
                error_code="INTERNAL_ERROR",
                extra_meta={
                    "quality": {"status": "failed", "workflow": "factor_candidate_workflow"},
                    "side_effect": {"level": "stateful", "target": "quant_manager", "confirmation_required": False},
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage_ctx.to_meta(),
                    "idempotency_key": idempotency_key,
                    "degraded": True,
                    "workflow_stage": {
                        "completed_stages": completed_stages,
                        "last_completed_stage": completed_stages[-1] if completed_stages else None,
                        "recoverable": True,
                        "failed_at": "factor_candidate_workflow",
                    },
                },
            )

    @mcp.tool(
        title="Strategy Review Workflow",
        description="AI-facing strategy review workflow with lifecycle, runtime and promotion context.",
        structured_output=True,
        meta=build_tool_meta("strategy_review_workflow"),
    )
    async def strategy_review_workflow(
        strategy_id: str,
        include_factory_status: bool = True,
        include_review_report: bool = True,
        include_runtime_alerts: bool = True,
        run_factory_once: bool = False,
        run_runtime_cycle: bool = False,
        idempotency_key: str | None = None,
        as_of: str | None = None,
        actor_id: str | None = None,
        actor_roles: list[str] | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        resolved_strategy_id = str(strategy_id or "").strip()
        steps: list[dict[str, Any]] = []
        source_chain = ["workflow.strategy_review", "resource.strategy_review", "manager.strategy_manager"]
        lineage_ctx = LineageContext.create("strategy_review_workflow", strategy_id=resolved_strategy_id)
        try:
            result_payload = await build_strategy_review_workflow_payload(
                resolved_strategy_id,
                runtime_strategy_manager=strategy_manager,
                include_factory_status=include_factory_status,
                include_review_report=include_review_report,
                include_runtime_alerts=include_runtime_alerts,
                run_factory_once=run_factory_once,
                run_runtime_cycle=run_runtime_cycle,
                runtime_alert_limit=20,
                as_of=as_of,
                actor_id=actor_id,
                actor_roles=actor_roles,
            )
            steps = list(result_payload.get("steps") or [])
            failed_steps = _collect_failed_steps(steps)

            return ok_with_meta(
                result_payload,
                tool_name="strategy_review_workflow",
                action="review",
                started_at=started_at,
                source_chain=source_chain,
                extra_meta={
                    "quality": _meta_quality(workflow_name="strategy_review_workflow", steps=steps),
                    "side_effect": {
                        "level": "stateful" if (run_factory_once or run_runtime_cycle) else "read_only",
                        "target": resolved_strategy_id,
                        "confirmation_required": False,
                        "idempotent": False if (run_factory_once or run_runtime_cycle) else True,
                    },
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage_ctx.to_meta(),
                    "idempotency_key": idempotency_key,
                    "degraded": bool(failed_steps),
                },
            )
        except Exception as exc:
            completed_stages = [s["step"] for s in steps if s.get("success")]
            return fail_with_meta(
                str(exc),
                tool_name="strategy_review_workflow",
                action="review",
                started_at=started_at,
                source_chain=source_chain,
                error_code="INTERNAL_ERROR",
                extra_meta={
                    "quality": {"status": "failed", "workflow": "strategy_review_workflow"},
                    "side_effect": {"level": "stateful" if (run_factory_once or run_runtime_cycle) else "read_only", "target": resolved_strategy_id},
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage_ctx.to_meta(),
                    "idempotency_key": idempotency_key,
                    "degraded": True,
                    "workflow_stage": {
                        "completed_stages": completed_stages,
                        "last_completed_stage": completed_stages[-1] if completed_stages else None,
                        "recoverable": True,
                        "failed_at": "strategy_review_workflow",
                    },
                },
            )
# --- ai_workflows_parts/formatters.py ---

    @mcp.tool(
        title="Prediction Diagnosis Workflow",
        description="Diagnose predicted probabilities with calibration, uncertainty and lineage-ready metadata.",
        structured_output=True,
        meta=build_tool_meta("prediction_diagnosis_workflow"),
    )
    async def prediction_diagnosis_workflow(
        probabilities: list[float] | None = None,
        labels: list[Any] | None = None,
        outcomes: list[Any] | None = None,
        raw_scores: list[float] | None = None,
        method: str = "raw",
        platt_a: float = 1.0,
        platt_b: float = 0.0,
        coverage_target: float = 0.9,
        dataset_id: str | None = None,
        run_id: str | None = None,
        persist_artifact: bool = False,
        output_artifact_id: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        from ..services.probability_calibration import (
            build_calibration_quality_report,
            calibrate_probability_series,
            estimate_prediction_interval,
            isotonic_calibrate,
            platt_scale,
        )
        from ..services.adapters.mapie_adapter import get_conformal_adapter

        started_at = time.perf_counter()
        workflow_method = str(method or "raw").strip().lower()
        source_chain = ["workflow.prediction_diagnosis", "service.probability_calibration"]
        lineage_ctx = LineageContext.create(
            "prediction_diagnosis_workflow",
            dataset_id=dataset_id,
        )
        try:
            # F-N43-1 fix (诊断报告 §N43): 空数组/缺失 probabilities 返回标准化 PARAM_ERROR，
            # 而非暴露裸 Pydantic 'Field required' 校验栈（签名已改为 Optional 让请求进入业务层）。
            if not probabilities:
                return fail_with_meta(
                    "probabilities is required and must be a non-empty array of numbers in [0, 1]",
                    tool_name="prediction_diagnosis_workflow",
                    action=workflow_method,
                    started_at=started_at,
                    source_chain=source_chain,
                    error_code="PARAM_ERROR",
                    extra_meta={
                        "quality": {"status": "failed", "workflow": "prediction_diagnosis_workflow"},
                        "side_effect": {"level": "read_only", "target": "prediction_inputs", "confirmation_required": False},
                        "lineage": {"dataset_id": dataset_id, "run_id": run_id},
                        "degraded": True,
                    },
                )
            probs = [float(item) for item in list(probabilities or [])]
            if any((value < 0.0 or value > 1.0) for value in probs):
                return fail_with_meta(
                    "probabilities must stay within [0, 1]",
                    tool_name="prediction_diagnosis_workflow",
                    action=workflow_method,
                    started_at=started_at,
                    source_chain=source_chain,
                    error_code="PARAM_ERROR",
                    extra_meta={
                        "quality": {"status": "failed", "workflow": "prediction_diagnosis_workflow"},
                        "side_effect": {"level": "read_only", "target": "prediction_inputs", "confirmation_required": False},
                        "lineage": {"dataset_id": dataset_id, "run_id": run_id},
                        "degraded": True,
                    },
                )
            label_values = outcomes if outcomes is not None else labels
            ys = _normalize_binary_outcomes(label_values)
            if not probs or len(probs) != len(ys):
                return fail_with_meta(
                    "probabilities and labels/outcomes must be non-empty and share the same length",
                    tool_name="prediction_diagnosis_workflow",
                    action=workflow_method,
                    started_at=started_at,
                    source_chain=source_chain,
                    error_code="PARAM_ERROR",
                    extra_meta={
                        "quality": {"status": "failed", "workflow": "prediction_diagnosis_workflow"},
                        "side_effect": {"level": "read_only", "target": "prediction_inputs", "confirmation_required": False},
                        "lineage": {"dataset_id": dataset_id, "run_id": run_id},
                        "degraded": True,
                    },
                )

            # P2-4.3.7 fix(诊断报告 §4.3.7):sample_size<30 强制 reject
            # 历史问题:method=platt 但 sample=5 时 sklearn+mapie 双重降级 silent 输出 platt_a/b 校准建议
            # 修复:sample<30 时 fail-graceful,不输出不可信的 calibration 建议
            if len(probs) < 30:
                return fail_with_meta(
                    f"insufficient_sample_size={len(probs)}<30: calibration unreliable, "
                    f"refusing to compute platt/isotonic. Increase samples or use method='raw'.",
                    tool_name="prediction_diagnosis_workflow",
                    action=workflow_method,
                    started_at=started_at,
                    source_chain=source_chain,
                    error_code="INSUFFICIENT_SAMPLES",
                    extra_meta={
                        "quality": {
                            "status": "rejected_sample_too_small",
                            "sample_size": len(probs),
                            "minimum_required": 30,
                        },
                        "side_effect": {"level": "read_only", "confirmation_required": False},
                        "lineage": {"dataset_id": dataset_id, "run_id": run_id},
                        "degraded": True,
                    },
                )

            calibration_result = None
            report_method = workflow_method
            # F-N43-2 fix (诊断报告 §N43): 当用户显式传入 platt_a/platt_b 且 method='platt' 时，
            # 应用「固定系数」Platt 缩放 sigmoid(a*x+b)，而非让 builtin 自拟 sigmoid 无视用户系数。
            # 历史问题: method=platt 下传任意 platt_a/platt_b 输出逐位相同 → 参数为 no-op。
            user_supplied_platt = (
                abs(float(platt_a) - 1.0) > 1e-9 or abs(float(platt_b) - 0.0) > 1e-9
            )
            if workflow_method == "platt" and user_supplied_platt:
                score_basis = (
                    raw_scores
                    if raw_scores and len(raw_scores) == len(probs)
                    else probs
                )
                calibrated = [
                    platt_scale(float(score_basis[idx]), a=float(platt_a), b=float(platt_b))
                    for idx in range(len(probs))
                ]
                report_method = "platt_fixed_coefficients"
            elif workflow_method in {"platt", "sigmoid", "calibrated_sigmoid", "calibrated_isotonic", "isotonic", "auto"}:
                target_method = (
                    "sigmoid"
                    if workflow_method in {"platt", "sigmoid", "calibrated_sigmoid", "auto"}
                    else "isotonic"
                )
                calibration_result = calibrate_probability_series(
                    probs,
                    ys,
                    raw_scores=raw_scores,
                    method=target_method,
                )
                calibrated = list(calibration_result.probabilities)
                report_method = calibration_result.method
            elif workflow_method == "legacy_platt":
                calibrated = [platt_scale(raw_scores[idx] if raw_scores and idx < len(raw_scores) else probs[idx], a=platt_a, b=platt_b) for idx in range(len(probs))]
            elif workflow_method == "legacy_isotonic":
                calibration_table = [(float(i) / max(len(probs) - 1, 1), probs[i]) for i in range(len(probs))]
                calibrated = [isotonic_calibrate(raw_scores[idx] if raw_scores and idx < len(raw_scores) else probs[idx], calibration_table) for idx in range(len(probs))]
            else:
                calibrated = list(probs)

            report = build_calibration_quality_report(
                calibrated,
                ys,
                calibration_method=report_method,
                calibration_version="workflow_v2",
                calibration_backend=calibration_result.backend_used if calibration_result else "builtin_lightweight",
                backend_requested=calibration_result.backend_requested if calibration_result else "raw",
                backend_used=calibration_result.backend_used if calibration_result else "raw",
                fallback_used=bool(calibration_result.fallback_used) if calibration_result else False,
                fallback_reason=calibration_result.fallback_reason if calibration_result else None,
                cv_folds=calibration_result.cv_folds if calibration_result else None,
            )
            interval_examples = [
                estimate_prediction_interval(
                    calibrated_probability=probability,
                    sample_size=max(20, len(calibrated)),
                    coverage_target=coverage_target,
                    calibrated=workflow_method != "raw",
                ).to_dict()
                for probability in calibrated[: min(5, len(calibrated))]
            ]
            conformal_result = get_conformal_adapter(prefer_mapie=True).predict_set(
                calibration_scores=calibrated,
                calibration_labels=ys,
                test_scores=calibrated[: min(5, len(calibrated))],
                alpha=max(0.001, min(0.499, 1.0 - float(coverage_target or 0.9))),
                n_classes=max(2, len(set(ys))),
            )
            # P1-3: Build uncertainty report
            uncertainty_payload: dict[str, Any] | None = None
            try:
                from ..services.uncertainty_contract import build_uncertainty_report

                avg_calibrated = sum(calibrated) / len(calibrated) if calibrated else None
                avg_raw = sum(probs) / len(probs) if probs else None
                uncertainty = build_uncertainty_report(
                    raw_probability=avg_raw,
                    calibrated_probability=avg_calibrated if report_method != "raw" else None,
                    calibration_method=report_method,
                    sample_size=len(calibrated),
                    ece=report.ece,
                    brier_score=report.brier_score,
                    coverage_target=coverage_target,
                    calibration_report=report,
                )
                uncertainty_payload = uncertainty.to_dict()
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/tools/ai_workflows.py:1207: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass

            diagnosis_payload: dict[str, Any] = {
                "workflow": "prediction_diagnosis_workflow",
                "method": workflow_method,
                "effective_method": report_method,
                "sample_size": len(calibrated),
                "probabilities": calibrated,
                "labels": ys,
                "label_source": "outcomes" if outcomes is not None else "labels",
                "calibration_report": report.to_dict(),
                "interval_examples": interval_examples,
                "conformal_prediction": conformal_result.to_dict(),
                "recommendations": list(report.notes),
            }
            if uncertainty_payload:
                diagnosis_payload["uncertainty"] = uncertainty_payload

            persisted_artifact_id = await _persist_optional_artifact(
                enabled=bool(persist_artifact),
                artifact_id=output_artifact_id,
                strategy="prediction_diagnosis",
                payload={
                    "artifact_type": "prediction_diagnosis",
                    "dataset_id": dataset_id,
                    "run_id": run_id,
                    "payload": diagnosis_payload,
                },
            )
            if persisted_artifact_id:
                diagnosis_payload["artifact_id"] = persisted_artifact_id

            if persisted_artifact_id:
                lineage_ctx.set_artifact(persisted_artifact_id)

            return ok_with_meta(
                diagnosis_payload,
                tool_name="prediction_diagnosis_workflow",
                action=workflow_method,
                started_at=started_at,
                source_chain=source_chain,
                extra_meta={
                    "quality": {
                        "status": report.quality_band,
                        "brier_score": report.brier_score,
                        "ece": report.ece,
                        "sample_size": report.sample_size,
                        "conformal_backend_used": conformal_result.backend_used,
                    },
                    "side_effect": {
                        "level": "stateful" if persisted_artifact_id else "read_only",
                        "target": persisted_artifact_id or "prediction_inputs",
                        "confirmation_required": False,
                        "idempotent": not bool(persisted_artifact_id),
                    },
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage_ctx.to_meta(),
                    "degraded": report.quality_band in {"poor", "unknown"} or bool(conformal_result.fallback_used),
                },
            )
        except Exception as exc:
            return fail_with_meta(
                str(exc),
                tool_name="prediction_diagnosis_workflow",
                action=workflow_method,
                started_at=started_at,
                source_chain=source_chain,
                error_code="INTERNAL_ERROR",
                extra_meta={
                    "quality": {"status": "failed", "workflow": "prediction_diagnosis_workflow"},
                    "side_effect": {"level": "stateful" if persist_artifact or output_artifact_id else "read_only", "target": "prediction_inputs"},
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage_ctx.to_meta(),
                    "degraded": True,
                },
            )

    @mcp.tool(
        title="Data Quality Workflow",
        description="Assess dataset completeness and minimum quality gates with machine-readable diagnostics.",
        structured_output=True,
        meta=build_tool_meta("data_quality_workflow"),
    )
    async def data_quality_workflow(
        dataset_id: str | None = None,
        records: list[dict[str, Any]] | None = None,
        required_fields: list[str] | None = None,
        as_of_field: str | None = None,
        as_of_value: str | None = None,
        source: str = "workflow.input",
        source_chain: list[str] | None = None,
        minimum_quality_threshold: float = 0.95,
        persist_artifact: bool = False,
        output_artifact_id: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        rows = list(records or [])
        required = [str(item).strip() for item in list(required_fields or []) if str(item).strip()]
        chain = [str(item).strip() for item in list(source_chain or ["workflow.data_quality"]) if str(item).strip()]
        lineage_ctx = LineageContext.create("data_quality_workflow", dataset_id=dataset_id)
        missing_counter: dict[str, int] = {field: 0 for field in required}
        failed_row_indices: list[int] = []

        try:
            import json as _json
            from ..services.adapters.data_validation_adapter import get_data_validation_adapter

            # F-N43-5 fix (诊断报告 §N43): 空 records 跨工具语义统一。
            # 历史问题: data_quality_workflow(records=[]) → success=true/accepted_ratio=1.0
            # （真空通过，误导），而 data_validation(records=[]) → 干净 PARAM_ERROR。
            # 修复: 与 data_validation 对齐，空 records 返回标准化 PARAM_ERROR（拒绝而非真空通过）。
            if not rows:
                return fail_with_meta(
                    "records is required and must be a non-empty array of objects",
                    tool_name="data_quality_workflow",
                    action="validate",
                    started_at=started_at,
                    source_chain=chain,
                    error_code="PARAM_ERROR",
                    extra_meta={
                        "quality": {"status": "failed", "workflow": "data_quality_workflow", "reason": "empty_records"},
                        "side_effect": {"level": "read_only", "target": dataset_id or "dataset", "confirmation_required": False},
                        "lineage": lineage_ctx.to_meta(),
                        "degraded": True,
                    },
                )

            for idx, row in enumerate(rows):
                missing = infer_missing_fields(row, required)
                if missing:
                    failed_row_indices.append(idx)
                for field in missing:
                    missing_counter[field] = missing_counter.get(field, 0) + 1

            accepted_count = len(rows) - len(failed_row_indices)
            ratio = 1.0 if not rows else accepted_count / len(rows)
            adapter = get_data_validation_adapter(prefer_gx=True)
            validation_result = adapter.validate_dataset(
                rows,
                {
                    "required_fields": required,
                    # F-N43-4 fix: 让校验子系统也逐行校验 required 字段的内容非空，
                    # 与上面字段级 accepted_ratio 保持一致，消除「0.6 失败 vs 1.0 通过」互斥。
                    "non_null_fields": required,
                    "min_record_count": 1 if rows else 0,
                    "min_quality_threshold": minimum_quality_threshold,
                },
            )
            checkpoint = adapter.create_checkpoint(
                checkpoint_name=f"{dataset_id or 'dataset'}_runtime_checkpoint",
                validation_results=[validation_result],
            )
            minimum_quality_passed = ratio >= float(minimum_quality_threshold) and bool(validation_result.passed)
            representative_asof = as_of_value
            if representative_asof is None and as_of_field:
                for row in rows:
                    if isinstance(row, dict) and row.get(as_of_field):
                        representative_asof = row.get(as_of_field)
                        break

            quality_meta = build_quality_meta(
                source=source,
                source_chain=chain,
                asof_value=representative_asof,
                missing_fields=[field for field, count in missing_counter.items() if count > 0],
                degraded=not minimum_quality_passed,
                success=True,
                accepted_count=accepted_count,
                rejected_count=len(failed_row_indices),
                minimum_quality_threshold=minimum_quality_threshold,
                minimum_quality_passed=minimum_quality_passed,
            )
            quality_meta["validation_backend_requested"] = validation_result.backend_requested
            quality_meta["validation_backend_used"] = validation_result.backend_used
            quality_meta["validation_fallback_used"] = validation_result.fallback_used
            quality_meta["validation_fallback_reason"] = validation_result.fallback_reason

            validation_payload = validation_result.to_dict()
            checkpoint_payload = dict(checkpoint or {})
            checkpoint_payload["validations"] = list(checkpoint_payload.get("validations") or [])[:3]

            payload = {
                "workflow": "data_quality_workflow",
                "dataset_id": dataset_id,
                "row_count": len(rows),
                "required_fields": required,
                "failed_row_indices": failed_row_indices[:50],
                "missing_by_field": missing_counter,
                "accepted_ratio": round(ratio, 6),
                "quality_meta": quality_meta,
                "validation_result": validation_payload,
                "checkpoint": checkpoint_payload,
                "remediation_hints": [
                    "补齐 required_fields 中缺失最多的字段",
                    "为每条记录补充统一的 as_of 时间或日期字段",
                    "若当前快照仅为抽样，请在写入下游前补做全量校验",
                ],
            }
            payload = _json.loads(_json.dumps(payload, ensure_ascii=False, default=str))

            persisted_artifact_id = await _persist_optional_artifact(
                enabled=bool(persist_artifact),
                artifact_id=output_artifact_id,
                strategy="dataset_quality_snapshot",
                payload={
                    "artifact_type": "dataset_quality_snapshot",
                    "dataset_id": dataset_id,
                    "quality": quality_meta,
                    "payload": payload,
                },
            )
            if persisted_artifact_id:
                payload["artifact_id"] = persisted_artifact_id

            if persisted_artifact_id:
                lineage_ctx.set_artifact(persisted_artifact_id)

            return ok_with_meta(
                payload,
                tool_name="data_quality_workflow",
                action="validate",
                started_at=started_at,
                source_chain=chain,
                extra_meta={
                    "quality": quality_meta,
                    "side_effect": {
                        "level": "stateful" if persisted_artifact_id else "read_only",
                        "target": persisted_artifact_id or (dataset_id or "dataset"),
                        "confirmation_required": False,
                        "idempotent": not bool(persisted_artifact_id),
                    },
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage_ctx.to_meta(),
                    "degraded": not minimum_quality_passed or bool(validation_result.fallback_used),
                },
            )
        except Exception as exc:
            return fail_with_meta(
                str(exc),
                tool_name="data_quality_workflow",
                action="validate",
                started_at=started_at,
                source_chain=chain,
                error_code="INTERNAL_ERROR",
                extra_meta={
                    "quality": {"status": "failed", "workflow": "data_quality_workflow"},
                    "side_effect": {"level": "stateful" if persist_artifact or output_artifact_id else "read_only", "target": dataset_id or "dataset"},
                    "pit": build_pit_meta_simple(as_of),
                    "lineage": lineage_ctx.to_meta(),
                    "degraded": True,
                },
            )

    @mcp.tool()
    async def ai_workflow_artifact(artifact_id: str) -> dict[str, Any]:
        """Inspect a persisted AI workflow artifact by artifact_id."""
        started_at = time.perf_counter()
        artifact = await get_artifact_async(artifact_id)
        if artifact is None:
            return fail_with_meta(
                f"artifact not found: {artifact_id}",
                tool_name="ai_workflow_artifact",
                action="get",
                started_at=started_at,
                source_chain=["services.artifact_registry"],
                error_code="NOT_FOUND",
                extra_meta={
                    "quality": {"status": "not_found"},
                    "side_effect": {"level": "read_only", "target": artifact_id, "confirmation_required": False},
                    "lineage": {"artifact_id": artifact_id},
                    "degraded": True,
                },
            )
        return ok_with_meta(
            {"artifact": artifact},
            tool_name="ai_workflow_artifact",
            action="get",
            started_at=started_at,
            source_chain=["services.artifact_registry"],
            extra_meta={
                "quality": {"status": "available"},
                "side_effect": {"level": "read_only", "target": artifact_id, "confirmation_required": False},
                "lineage": {"artifact_id": artifact_id},
                "degraded": False,
            },
        )
