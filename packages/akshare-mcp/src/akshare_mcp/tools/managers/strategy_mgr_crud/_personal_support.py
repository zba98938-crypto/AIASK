"""Strategy manager CRUD action handlers."""

import asyncio
import os
import random
import time
from datetime import datetime, timezone
from uuid import uuid4

from ....storage import get_db
from ....utils import fail, ok
from ....services.strategy_lifecycle_shared.presentation import (
    build_favorite_state,
    build_owner_state,
    build_paper_session_state,
    build_strategy_presentation,
    is_admin_actor,
    is_personal_strategy,
    normalize_actor_roles,
)
from ..strategy_mgr_helpers import (
    build_factory_capability_health,
    build_incubation_overview,
    compute_nav_series,
    get_latest_quality_report,
    list_quality_reports,
    normalize_quality_report_contract,
    normalize_status_alias,
    parse_bool,
    normalize_time_filter,
    update_status,
)

import logging

logger = logging.getLogger(__name__)

from ._support import (
    _PERSONAL_STRATEGY_FOCUS_FIELDS,
    _actor_context,
    _build_strategy_incubation_surface,
    _build_strategy_market_summary,
    _clean_string_list,
    _closure_snapshot_overview_payload,
    _enrich_rank_strategy,
    _ensure_personal_strategy_mutation_allowed,
    _execution_audit_entity_chain_available,
    _extract_strategy_market_summary_value,
    _incubation_surface_bool,
    _incubation_surface_issue_count,
    _load_latest_vector_index_snapshot,
    _load_personal_strategy_surface_state,
    _load_signal_quality_registry_snapshot,
    _load_similar_vector_profiles,
    _load_strategy_incubation_surface,
    _load_vector_profiles,
    _normalize_personal_strategy_focus_fields,
    _normalize_strategy_status_value,
    _resolve_incubation_surface_stage,
    _resolve_strategy_incubation_overview,
    _resolve_strategy_status_filter,
    _resolved,
    _sanitize_personal_strategy_snapshot,
    _strategy_source_strategy_id,
    _trimmed,
)

def _build_personal_strategy_context(
    strategy: dict | None,
    *,
    actor_id: str | None,
    actor_roles: list[str],
    owner_state: dict | None,
    favorite_state: dict | None,
    paper_session_state: dict | None,
) -> dict:
    snapshot = _sanitize_personal_strategy_snapshot(strategy)
    mutation_error = _ensure_personal_strategy_mutation_allowed(
        strategy,
        actor_id=actor_id,
        actor_roles=actor_roles,
    )
    factor_weights = dict(snapshot.get("factor_weights") or {})
    numeric_factor_weights = {
        key: float(value)
        for key, value in factor_weights.items()
        if isinstance(value, (int, float))
    }
    source_strategy_id = _trimmed(snapshot.get("metadata", {}).get("source_strategy_id")) or None
    default_focus = ["description", "factor_weights", "tags"]
    return {
        "strategy_id": snapshot.get("id"),
        "strategy_name": snapshot.get("name"),
        "strategy_type": snapshot.get("strategy_type"),
        "status": snapshot.get("status"),
        "actor_id": actor_id,
        "actor_roles": list(actor_roles or []),
        "owner_state": dict(owner_state or {}),
        "favorite_state": dict(favorite_state or {}),
        "paper_session_state": dict(paper_session_state or {}),
        "personal_strategy": is_personal_strategy(strategy or {}),
        "editable": bool(dict(owner_state or {}).get("editable")),
        "source_strategy_id": source_strategy_id,
        "draft_snapshot": snapshot,
        "draft_stats": {
            "description_present": bool(snapshot.get("description")),
            "tag_count": len(list(snapshot.get("tags") or [])),
            "param_key_count": len(dict(snapshot.get("params") or {})),
            "factor_weight_key_count": len(factor_weights),
            "factor_weight_abs_sum": round(sum(abs(value) for value in numeric_factor_weights.values()), 6),
        },
        "mutation_guard": {
            "allowed": mutation_error is None,
            "reason": mutation_error,
        },
        "action_modes": [
            {
                "action_kind": "view",
                "effect": "readonly",
                "available": True,
                "label": "查看当前个人策略上下文",
            },
            {
                "action_kind": "generate_update_suggestion",
                "effect": "advisory",
                "available": mutation_error is None,
                "label": "生成修改建议",
                "reason": mutation_error,
                "default_focus_fields": default_focus,
            },
            {
                "action_kind": "optimize",
                "effect": "stateful",
                "available": mutation_error is None,
                "label": "执行 AI 优化",
                "reason": mutation_error,
            },
            {
                "action_kind": "persist_update",
                "effect": "stateful",
                "available": mutation_error is None,
                "label": "保存到个人策略草稿",
                "reason": mutation_error,
            },
        ],
    }


def _build_personal_strategy_change_plan(
    strategy: dict | None,
    params: dict,
    *,
    mode: str,
    actor_id: str | None,
    actor_roles: list[str],
    owner_state: dict | None,
    favorite_state: dict | None,
    paper_session_state: dict | None,
) -> dict:
    snapshot = _sanitize_personal_strategy_snapshot(strategy)
    before = {
        "name": snapshot.get("name"),
        "description": snapshot.get("description"),
        "params": dict(snapshot.get("params") or {}),
        "factor_weights": dict(snapshot.get("factor_weights") or {}),
        "tags": list(snapshot.get("tags") or []),
    }
    after = {
        "name": before.get("name"),
        "description": before.get("description"),
        "params": dict(before.get("params") or {}),
        "factor_weights": dict(before.get("factor_weights") or {}),
        "tags": list(before.get("tags") or []),
    }
    focus_fields = _normalize_personal_strategy_focus_fields(params.get("focus_fields"))
    focus_enabled = set(focus_fields or _PERSONAL_STRATEGY_FOCUS_FIELDS)
    objective = _trimmed(params.get("objective") or params.get("goal") or params.get("target"))
    instructions = _trimmed(params.get("instructions") or params.get("prompt") or params.get("reason"))
    advisory_label = "AI优化" if mode == "optimize" else "AI建议"
    advisory_tail = objective or instructions or "补齐风险约束、执行纪律和样本跟踪。"
    change_reasons: dict[str, str] = {}

    if "description" in focus_enabled:
        current_description = _trimmed(before.get("description"))
        next_description = current_description
        advisory_note = f"{advisory_label}：{advisory_tail}"
        if not current_description:
            next_description = advisory_note
        elif advisory_note not in current_description:
            next_description = f"{current_description}｜{advisory_note}"
        if next_description != current_description:
            after["description"] = next_description
            change_reasons["description"] = "补齐当前草稿的 AI 说明与执行提示。"

    if "factor_weights" in focus_enabled:
        factor_weights = dict(before.get("factor_weights") or {})
        numeric_factor_weights = {
            key: float(value)
            for key, value in factor_weights.items()
            if isinstance(value, (int, float))
        }
        if numeric_factor_weights:
            total = sum(abs(value) for value in numeric_factor_weights.values()) or 0.0
            if total > 0:
                normalized_factor_weights = {
                    key: round(value / total, 6)
                    for key, value in numeric_factor_weights.items()
                }
                if any(
                    abs(normalized_factor_weights[key] - numeric_factor_weights.get(key, 0.0)) > 1e-6
                    for key in normalized_factor_weights
                ):
                    after["factor_weights"] = {
                        **factor_weights,
                        **normalized_factor_weights,
                    }
                    change_reasons["factor_weights"] = "将数值因子权重归一化，便于后续比较与执行。"

    if "params" in focus_enabled:
        params_payload = dict(before.get("params") or {})
        metadata = dict(params_payload.get("metadata") or {})
        metadata_changed = False
        if mode == "optimize":
            next_metadata = {
                **metadata,
                "ai_optimized_at": datetime.now(timezone.utc).isoformat(),
                "ai_optimizer": "strategy_manager.personal_strategy_optimizer",
                "source_strategy_id": metadata.get("source_strategy_id") or snapshot.get("id"),
            }
            metadata_changed = next_metadata != metadata
            metadata = next_metadata
        elif objective or instructions:
            suggestion_meta = {
                "objective": objective or None,
                "instructions": instructions or None,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "strategy_manager.personal_strategy_suggestions",
            }
            next_metadata = dict(metadata)
            next_metadata["ai_pending_suggestion"] = suggestion_meta
            metadata_changed = next_metadata != metadata
            metadata = next_metadata
        if metadata_changed:
            params_payload["metadata"] = metadata
            after["params"] = params_payload
            change_reasons["params"] = "补齐 AI 处理上下文，便于后续继续优化或人工复核。"

    if "tags" in focus_enabled:
        tag_marker = "ai_optimized" if mode == "optimize" else "ai_suggested_update"
        next_tags = _clean_string_list([
            *list(before.get("tags") or []),
            "personal_strategy",
            tag_marker,
        ])
        if next_tags != list(before.get("tags") or []):
            after["tags"] = next_tags
            change_reasons["tags"] = "为个人策略补齐 AI 处理标签，便于后续筛选。"

    changed_fields = [
        field for field in ("name", "description", "params", "factor_weights", "tags")
        if before.get(field) != after.get(field)
    ]
    apply_payload = {
        field: after.get(field)
        for field in changed_fields
    }
    suggestions = [
        {
            "field": field,
            "action_kind": "persist_update" if mode == "optimize" else "generate_update_suggestion",
            "effect": "stateful" if mode == "optimize" else "advisory",
            "reason": change_reasons.get(field) or "根据当前个人策略草稿生成的 AI 调整建议。",
            "before": before.get(field),
            "after": after.get(field),
        }
        for field in changed_fields
    ]
    if changed_fields:
        summary = (
            f"{'已生成' if mode == 'suggest' else '将执行'}个人策略调整方案："
            f"{'、'.join(changed_fields)}。"
        )
    else:
        summary = "当前个人策略草稿未发现需要低风险自动调整的字段。"
    risk_notes = [
        "当前结果基于页面已有草稿字段生成，不替代人工研究结论。",
        "默认建议态不会写库；只有显式保存、应用建议、执行 AI 优化，或以 persist 模式调用建议动作时才会落库更新。",
    ]
    if mode == "optimize":
        risk_notes.append("AI 优化会直接写回当前个人策略草稿，请在执行前确认这是你的个人版本。")

    return {
        "strategy_id": snapshot.get("id"),
        "mode": mode,
        "objective": objective or None,
        "instructions": instructions or None,
        "focus_fields": focus_fields,
        "before": before,
        "after": after,
        "apply_payload": apply_payload,
        "changed_fields": changed_fields,
        "summary": summary,
        "suggestions": suggestions,
        "risk_notes": risk_notes,
        "context": _build_personal_strategy_context(
            strategy,
            actor_id=actor_id,
            actor_roles=actor_roles,
            owner_state=owner_state,
            favorite_state=favorite_state,
            paper_session_state=paper_session_state,
        ),
    }


def _personal_strategy_pipeline_warning(step: str, error: str) -> str:
    label_map = {
        "recompile": "DSL/运行契约重编译",
        "backtest": "回测重跑",
        "review_report_recheck": "质检重算",
        "submission_replay": "提交回放",
        "execution_audit_acceptance": "执行审计",
    }
    label = label_map.get(step) or step
    return f"{label}未完成：{error}"


def _extract_personal_strategy_backtest_metrics(result_payload: dict, *, backtest_id: str | None) -> dict:
    payload = dict(result_payload or {})

    def _safe_float(value, default: float = 0.0) -> float:
        try:
            if value is None:
                return float(default)
            return float(value)
        except Exception:
            return float(default)

    def _safe_int(value, default: int = 0) -> int:
        try:
            if value is None:
                return int(default)
            return int(float(value))
        except Exception:
            return int(default)

    trade_count = _safe_int(
        payload.get("trade_count")
        if payload.get("trade_count") is not None
        else payload.get("trades_count"),
        0,
    )
    return {
        "backtest_id": backtest_id,
        "total_return": round(_safe_float(payload.get("total_return"), 0.0), 6),
        "annual_return": round(_safe_float(payload.get("annual_return"), 0.0), 6),
        "sharpe_ratio": round(_safe_float(payload.get("sharpe_ratio"), 0.0), 6),
        "max_drawdown": round(_safe_float(payload.get("max_drawdown"), 0.0), 6),
        "win_rate": round(_safe_float(payload.get("win_rate"), 0.0), 6),
        "calmar_ratio": round(_safe_float(payload.get("calmar_ratio"), 0.0), 6),
        "trade_count": trade_count,
        "trades_count": trade_count,
        "final_capital": round(_safe_float(payload.get("final_capital"), 0.0), 4),
        "initial_capital": round(_safe_float(payload.get("initial_capital"), 100000.0), 4),
        "avg_holding_days": round(_safe_float(payload.get("avg_holding_days"), 0.0), 4),
        "sortino_ratio": round(_safe_float(payload.get("sortino_ratio"), 0.0), 6),
        "execution_summary": dict(payload.get("execution_summary") or {}),
        "cost_assumptions": dict(payload.get("cost_assumptions") or {}),
        "backtest_assumptions": {
            "strategy_type": _trimmed(payload.get("strategy")),
            "portfolio_backtest": bool(payload.get("portfolio_backtest")),
        },
        "recomputed_at": datetime.now(timezone.utc).isoformat(),
        "source": "strategy_manager.personal_strategy_post_update_pipeline",
    }


def _select_personal_strategy_update_fields(payload: dict | None) -> dict:
    data = dict(payload or {})
    allowed_fields = (
        "name",
        "description",
        "params",
        "factor_weights",
        "tags",
        "backtest_artifact_id",
    )
    return {
        field: data.get(field)
        for field in allowed_fields
        if field in data
    }


def _personal_strategy_update_changed(strategy: dict | None, updates: dict | None) -> bool:
    current = dict(strategy or {})
    payload = dict(updates or {})
    if not payload:
        return False
    for field, value in payload.items():
        if current.get(field) != value:
            return True
    return False


async def _rerun_personal_strategy_backtest(
    db,
    strategy: dict,
    *,
    history_limit: int = 1200,
) -> dict:
    from ...services.backtest import BacktestEngine
    from ...services.strategy_acceptance_remediation import (
        StrategyAcceptanceRemediationService,
        _strategy_runtime_params,
    )

    strategy_payload = dict(strategy or {})
    strategy_id = _trimmed(strategy_payload.get("id"))
    strategy_type = _trimmed(strategy_payload.get("strategy_type"))
    if not strategy_id:
        return {"status": "skipped", "reason": "strategy_id_missing"}
    if not strategy_type:
        return {"status": "skipped", "reason": "strategy_type_missing"}

    remediation = StrategyAcceptanceRemediationService()
    market_data = await remediation._load_market_data(
        db,
        strategy_payload,
        history_limit=max(250, int(history_limit or 1200)),
    )
    if not market_data:
        return {"status": "skipped", "reason": "market_data_missing"}

    runtime_params = {
        **dict(_strategy_runtime_params(strategy_payload) or {}),
        "strategy_id": strategy_id,
        "strategy_name": strategy_payload.get("name"),
        "target_symbols": list(market_data.keys()),
    }

    raw_result = (
        BacktestEngine.run_backtest(
            next(iter(market_data.keys())),
            next(iter(market_data.values())),
            strategy_type,
            runtime_params,
            return_trades=True,
        )
        if len(market_data) == 1
        else BacktestEngine.run_portfolio_backtest(
            market_data,
            strategy_type,
            runtime_params,
            return_trades=True,
        )
    )
    if not bool(dict(raw_result or {}).get("success")):
        return {
            "status": "failed",
            "reason": _trimmed(dict(raw_result or {}).get("error")) or "backtest_failed",
        }

    result_payload = dict(dict(raw_result or {}).get("data") or raw_result or {})
    trades = list(result_payload.get("trades") or [])
    backtest_id = None
    if hasattr(db, "acquire"):
        try:
            backtest_id = await remediation._persist_generated_backtest(
                db,
                strategy=strategy_payload,
                market_data=market_data,
                result_payload=result_payload,
                trades=trades,
            )
        except Exception as exc:
            logger.warning(
                "strategy_manager personal strategy backtest persistence failed for %s: %s",
                strategy_id,
                exc,
            )
    metrics = _extract_personal_strategy_backtest_metrics(
        result_payload,
        backtest_id=backtest_id,
    )
    if hasattr(db, "save_strategy_metrics"):
        await db.save_strategy_metrics(strategy_id, "backtest", metrics)
    return {
        "status": "completed",
        "backtest_id": backtest_id,
        "target_symbols": list(market_data.keys()),
        "portfolio_backtest": len(market_data) > 1,
        "metrics": metrics,
        "result": result_payload,
    }


async def _recompile_personal_strategy_runtime(db, strategy: dict) -> dict:
    from ...services.strategy_recompile_backfill import build_trend_strategy_recompile_backfill

    strategy_payload = dict(strategy or {})
    current_status = _trimmed(strategy_payload.get("status")).lower()
    synthetic_input = {
        **strategy_payload,
        "status": current_status if current_status in {"submitted", "incubating"} else "submitted",
    }
    result = build_trend_strategy_recompile_backfill(
        synthetic_input,
        backtest_metrics=dict(strategy_payload.get("backtest_metrics") or {}),
        force=True,
    )
    if str(result.get("status") or "").strip().lower() == "skipped":
        return {
            "status": "skipped",
            "reason": result.get("reason"),
            "deterministic_recompile_eligible": bool(result.get("deterministic_recompile_eligible")),
        }
    updated_payload = _select_personal_strategy_update_fields(result.get("updated_payload"))
    if _personal_strategy_update_changed(strategy_payload, updated_payload):
        updated = await db.update_strategy_fields(
            _trimmed(strategy_payload.get("id")),
            updated_payload,
        ) if hasattr(db, "update_strategy_fields") else None
    else:
        updated = strategy_payload
    return {
        "status": str(result.get("status") or "completed"),
        "reason": result.get("reason"),
        "deterministic_recompile_eligible": bool(result.get("deterministic_recompile_eligible")),
        "applied_param_fields": list(result.get("applied_param_fields") or []),
        "preserved_param_fields": list(result.get("preserved_param_fields") or []),
        "updated_strategy": updated or strategy_payload,
        "generated_candidate": dict(result.get("generated_candidate") or {}),
    }


async def _run_personal_strategy_post_update_pipeline(
    db,
    strategy: dict,
) -> tuple[dict, dict, list[str]]:
    from .strategy_mgr_lifecycle import (
        handle_execution_audit_acceptance,
        handle_review_report_recheck,
        handle_submission_replay,
    )

    strategy_payload = dict(strategy or {})
    risk_notes: list[str] = []
    pipeline = {
        "requested": True,
        "overall_status": "completed",
        "recompile": {"status": "skipped", "reason": "not_requested"},
        "backtest": {"status": "skipped", "reason": "not_requested"},
        "review_report_recheck": {"status": "skipped", "reason": "not_requested"},
        "submission_replay": {"status": "skipped", "reason": "not_requested"},
        "execution_audit_acceptance": {"status": "skipped", "reason": "not_requested"},
    }

    try:
        recompile = await _recompile_personal_strategy_runtime(db, strategy_payload)
        pipeline["recompile"] = {
            key: value
            for key, value in dict(recompile or {}).items()
            if key != "updated_strategy"
        }
        strategy_payload = dict(recompile.get("updated_strategy") or strategy_payload)
        if str(recompile.get("status") or "").strip().lower() == "failed":
            risk_notes.append(
                _personal_strategy_pipeline_warning(
                    "recompile",
                    _trimmed(recompile.get("reason")) or "runtime_recompile_failed",
                )
            )
    except Exception as exc:
        pipeline["recompile"] = {"status": "failed", "reason": str(exc)}
        risk_notes.append(_personal_strategy_pipeline_warning("recompile", str(exc)))

    try:
        backtest = await _rerun_personal_strategy_backtest(db, strategy_payload)
        pipeline["backtest"] = backtest
        if str(backtest.get("status") or "").strip().lower() == "failed":
            risk_notes.append(
                _personal_strategy_pipeline_warning(
                    "backtest",
                    _trimmed(backtest.get("reason")) or "backtest_failed",
                )
            )
        elif str(backtest.get("status") or "").strip().lower() == "completed":
            strategy_payload = {
                **strategy_payload,
                "backtest_metrics": dict(backtest.get("metrics") or {}),
            }
    except Exception as exc:
        pipeline["backtest"] = {"status": "failed", "reason": str(exc)}
        risk_notes.append(_personal_strategy_pipeline_warning("backtest", str(exc)))

    try:
        review_report = await handle_review_report_recheck(
            db,
            {"strategy_id": _trimmed(strategy_payload.get("id"))},
        )
        if bool(review_report.get("success")):
            review_payload = dict(review_report.get("data") or {})
            pipeline["review_report_recheck"] = {
                "status": "completed",
                "report_type": review_payload.get("report_type"),
                "quality_gate": dict(review_payload.get("quality_gate") or {}),
                "closure_review": dict(review_payload.get("closure_review") or {}),
            }
        else:
            pipeline["review_report_recheck"] = {
                "status": "failed",
                "reason": review_report.get("error"),
            }
            risk_notes.append(
                _personal_strategy_pipeline_warning(
                    "review_report_recheck",
                    _trimmed(review_report.get("error")) or "review_report_recheck_failed",
                )
            )
    except Exception as exc:
        pipeline["review_report_recheck"] = {"status": "failed", "reason": str(exc)}
        risk_notes.append(_personal_strategy_pipeline_warning("review_report_recheck", str(exc)))

    try:
        replay = await handle_submission_replay(
            db,
            {
                "strategy_id": _trimmed(strategy_payload.get("id")),
                "recheck_reports": False,
            },
        )
        if bool(replay.get("success")):
            replay_payload = dict(replay.get("data") or {})
            pipeline["submission_replay"] = {
                "status": "completed",
                "summary": dict(replay_payload.get("summary") or {}),
                "items": list(replay_payload.get("items") or []),
            }
        else:
            pipeline["submission_replay"] = {
                "status": "failed",
                "reason": replay.get("error"),
            }
            risk_notes.append(
                _personal_strategy_pipeline_warning(
                    "submission_replay",
                    _trimmed(replay.get("error")) or "submission_replay_failed",
                )
            )
    except Exception as exc:
        pipeline["submission_replay"] = {"status": "failed", "reason": str(exc)}
        risk_notes.append(_personal_strategy_pipeline_warning("submission_replay", str(exc)))

    try:
        acceptance = await handle_execution_audit_acceptance(
            db,
            {
                "strategy_id": _trimmed(strategy_payload.get("id")),
                "backfill": True,
            },
        )
        if bool(acceptance.get("success")):
            acceptance_payload = dict(acceptance.get("data") or {})
            pipeline["execution_audit_acceptance"] = {
                "status": "completed",
                "overall_ready": bool(acceptance_payload.get("overall_ready")),
                "acceptance_matrix": dict(acceptance_payload.get("acceptance_matrix") or {}),
                "recommendations": list(acceptance_payload.get("recommendations") or []),
            }
        else:
            pipeline["execution_audit_acceptance"] = {
                "status": "failed",
                "reason": acceptance.get("error"),
            }
            risk_notes.append(
                _personal_strategy_pipeline_warning(
                    "execution_audit_acceptance",
                    _trimmed(acceptance.get("error")) or "execution_audit_acceptance_failed",
                )
            )
    except Exception as exc:
        pipeline["execution_audit_acceptance"] = {"status": "failed", "reason": str(exc)}
        risk_notes.append(_personal_strategy_pipeline_warning("execution_audit_acceptance", str(exc)))

    step_statuses = [
        str(dict(pipeline.get(step) or {}).get("status") or "skipped").strip().lower()
        for step in (
            "recompile",
            "backtest",
            "review_report_recheck",
            "submission_replay",
            "execution_audit_acceptance",
        )
    ]
    if any(status == "failed" for status in step_statuses):
        pipeline["overall_status"] = (
            "completed_with_warnings"
            if any(status == "completed" for status in step_statuses)
            else "failed"
        )
    elif all(status == "skipped" for status in step_statuses):
        pipeline["overall_status"] = "skipped"

    refreshed = await db.get_strategy(_trimmed(strategy_payload.get("id"))) if hasattr(db, "get_strategy") else strategy_payload
    return pipeline, refreshed or strategy_payload, risk_notes
