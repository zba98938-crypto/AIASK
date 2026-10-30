"""Lifecycle overview builders and service-level composition."""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from .common import (
    DEPRECATION_THRESHOLDS,
    PROMOTION_THRESHOLDS,
    _EARLY_SIGNAL_STAGES,
    _EARLY_STAGE_PROMOTION_MDD_TOLERANCE,
    _EXECUTION_AUDIT_PROMOTION_BLOCKING_STAGES,
    _TREND_EXECUTABLE_DSL_TYPES,
    _confidence_diagnostics_enabled,
    _promotion_cross_regime_enabled,
    evaluate_cross_regime_skill,
    _quality_report_bool,
    _quality_report_field,
    _safe_float,
    _safe_int,
    _string,
    metric_bucket_value,
)
from .execution_quality import (
    EXPECTED_FORWARD_DAYS,
    _build_confidence_diagnostics,
    _build_execution_quality_snapshot,
    _build_position_cycle_evidence,
    _build_signal_quality_snapshot,
    _normalize_execution_quality_for_contract,
    _resolve_high_precision_overview_context,
    build_execution_quality,
    derive_signal_quality,
)
from .incubation import (
    _coerce_date,
    get_latest_quality_report,
    resolve_incubation_action_plan,
    resolve_incubation_pipeline_stage,
)
from .execution_audit_snapshot import (
    snapshot_verdict_payload,
    with_execution_audit_snapshot_metadata,
)
from .prediction_trace import (
    _build_prediction_trace_ledger_view,
    _build_execution_lineage,
    _extract_runtime_playbook_provenance,
    _extract_semantic_lineage,
    _load_prediction_trace_entity_chain,
)
from .overview_helpers import (
    _CLOSURE_SNAPSHOT_DROP_FIELDS,
    _persist_and_finalize_overview,
    _quality_report_timestamp,
    _resolve_risk_hard_gate,
    _trim_closure_snapshot,
)

logger = logging.getLogger(__name__)


def _signal_stats_cache_signature(signal_stats: dict | None) -> dict[str, Any]:
    stats = dict(signal_stats or {})
    signature: dict[str, Any] = {
        "raw_signal_count": _safe_int(stats.get("raw_signal_count") or stats.get("total_signals")),
        "signals_with_forward_returns_count": _safe_int(
            stats.get("signals_with_forward_returns_count")
        ),
        "observed_forward_return_count": _safe_int(stats.get("observed_forward_return_count")),
        "coverage_ratio": _safe_float(stats.get("coverage_ratio")),
        "window_end_date": _string(stats.get("window_end_date")) or None,
    }
    by_horizon: dict[str, Any] = {}
    for days in EXPECTED_FORWARD_DAYS:
        horizon: dict[str, Any] = {}
        for field in (
            "sample_count",
            "effective_n",
            "hit_rate",
            "hit_rate_lcb",
            "skill_lcb",
            "forward_ic",
            "forward_sharpe",
        ):
            value = metric_bucket_value(stats.get(field), days)
            if value is None:
                continue
            if field in {"sample_count", "effective_n"}:
                horizon[field] = _safe_int(value)
            else:
                horizon[field] = _safe_float(value)
        if horizon:
            by_horizon[str(days)] = horizon
    signature["by_horizon"] = by_horizon
    return signature


async def _execution_state_cache_signature(db, strategy_id: str) -> dict[str, Any]:
    signature: dict[str, Any] = {
        "account_id": None,
        "order_count": 0,
        "filled_order_count": 0,
        "pending_order_count": 0,
        "trade_count": 0,
        "nav_row_count": 0,
        "latest_order_updated_at": None,
        "latest_order_filled_at": None,
        "latest_trade_time": None,
        "latest_trade_created_at": None,
        "latest_nav_date": None,
        "latest_nav_created_at": None,
    }
    account = None
    if hasattr(db, "get_paper_account_by_strategy"):
        try:
            account = await db.get_paper_account_by_strategy(strategy_id)
        except Exception:
            account = None
    if not account and hasattr(db, "acquire"):
        try:
            async with db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM paper_accounts WHERE strategy_id = $1 ORDER BY created_at LIMIT 1",
                    strategy_id,
                )
            account = dict(row) if row else None
        except Exception:
            account = None
    account_id = _string((account or {}).get("id") or (account or {}).get("account_id"))
    signature["account_id"] = account_id or None
    if not account_id:
        return signature

    if hasattr(db, "acquire"):
        try:
            async with db.acquire() as conn:
                order_row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS order_count,
                        COALESCE(SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END), 0) AS filled_order_count,
                        COALESCE(SUM(CASE WHEN status IN ('pending', 'submitted') THEN 1 ELSE 0 END), 0) AS pending_order_count,
                        MAX(updated_at) AS latest_order_updated_at,
                        MAX(filled_at) AS latest_order_filled_at
                    FROM paper_orders
                    WHERE account_id = $1
                    """,
                    account_id,
                )
                trade_row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS trade_count,
                        MAX(trade_time) AS latest_trade_time,
                        MAX(created_at) AS latest_trade_created_at
                    FROM paper_trades
                    WHERE account_id = $1
                    """,
                    account_id,
                )
                nav_row = await conn.fetchrow(
                    """
                    SELECT
                        COUNT(*) AS nav_row_count,
                        MAX(nav_date) AS latest_nav_date,
                        MAX(created_at) AS latest_nav_created_at
                    FROM paper_nav
                    WHERE account_id = $1
                    """,
                    account_id,
                )
            rows = [dict(order_row or {}), dict(trade_row or {}), dict(nav_row or {})]
            merged: dict[str, Any] = {}
            for row in rows:
                merged.update(row)
            for key in (
                "order_count",
                "filled_order_count",
                "pending_order_count",
                "trade_count",
                "nav_row_count",
            ):
                signature[key] = _safe_int(merged.get(key))
            for key in (
                "latest_order_updated_at",
                "latest_order_filled_at",
                "latest_trade_time",
                "latest_trade_created_at",
                "latest_nav_date",
                "latest_nav_created_at",
            ):
                signature[key] = _string(merged.get(key)) or None
            return signature
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/overview.py:199: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass

    if hasattr(db, "get_paper_order_summary"):
        try:
            order_summary = dict(await db.get_paper_order_summary(account_id) or {})
            signature["order_count"] = _safe_int(order_summary.get("total_orders"))
            signature["filled_order_count"] = _safe_int(order_summary.get("filled_orders"))
            signature["trade_count"] = _safe_int(order_summary.get("total_trades"))
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/overview.py:208: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    if hasattr(db, "get_paper_nav_rows"):
        try:
            nav_rows = [dict(row or {}) for row in list(await db.get_paper_nav_rows(account_id, limit=1) or [])]
            signature["nav_row_count"] = 1 if nav_rows else 0
            if nav_rows:
                signature["latest_nav_date"] = _string(nav_rows[0].get("nav_date")) or None
                signature["latest_nav_created_at"] = _string(nav_rows[0].get("created_at")) or None
        except Exception as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/overview.py:217: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    return signature


def _signature_text(value: Any) -> str:
    try:
        return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(value or "")


def _execution_signature_is_empty(signature: dict[str, Any]) -> bool:
    return not signature.get("account_id") and not any(
        _safe_int(signature.get(key))
        for key in (
            "order_count",
            "filled_order_count",
            "pending_order_count",
            "trade_count",
            "nav_row_count",
        )
    )


# NAV<0.90 is the incubation scorecard's risk de-escalation line, reused as the
# promotion floor rather than introducing a new threshold.
PAPER_NAV_PROMOTION_FLOOR = 0.90


async def _load_latest_paper_evidence(db, strategy_id: str) -> dict[str, Any]:
    """Latest paper NAV/MDD from ``strategy_incubation_metrics``.

    ``status`` separates "this host has no metrics store" (``not_supported``),
    "no metrics row yet" (``missing``) and "the read failed" (``error``) so the
    gate below can refuse promotion on a failed read instead of waving it
    through as if the evidence were clean.
    """
    if not hasattr(db, "acquire"):
        return {"status": "not_supported", "nav": None, "max_drawdown": None}
    try:
        async with db.acquire() as conn:
            exclusions_present = bool(await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_evidence_exclusions')"
            ))
            exclusion_filter = (
                "AND (m.account_id IS NULL OR NOT EXISTS ("
                "SELECT 1 FROM paper_evidence_exclusions e WHERE e.account_id = m.account_id))"
                if exclusions_present else ""
            )
            row = await conn.fetchrow(
                f"""
                SELECT m.max_drawdown, m.nav
                FROM strategy_incubation_metrics m
                WHERE m.strategy_id = $1
                  {exclusion_filter}
                ORDER BY m.metric_date DESC, m.id DESC
                LIMIT 1
                """,
                strategy_id,
            )
    except Exception as exc:
        logger.warning("paper promotion evidence read failed for %s: %s: %s", strategy_id, type(exc).__name__, exc)
        return {"status": "error", "nav": None, "max_drawdown": None, "error": f"{type(exc).__name__}: {exc}"}
    if row is None:
        return {"status": "missing", "nav": None, "max_drawdown": None}
    return {
        "status": "ok",
        "nav": _safe_float(row.get("nav")),
        "max_drawdown": _safe_float(row.get("max_drawdown")),
    }


def apply_paper_promotion_gate(
    promotion_ready: bool,
    blockers: list[str],
    paper_evidence: dict[str, Any],
) -> bool:
    """Veto promotion on the strategy's own paper trail.

    The 2026-10-19 cohort promoted three lsmap strategies at NAV 0.781 / MDD 22%
    because the Sharpe/MDD checks read ``strategy_metrics`` (backtest period),
    which those strategies had no rows in.  Only a still-promotable strategy is
    vetoed here; reasons are appended to ``blockers`` (mutated in place).
    """
    if not promotion_ready:
        return False
    reasons: list[str] = []
    status = paper_evidence.get("status")
    if status == "error":
        reasons.append("paper_evidence_unavailable")
    paper_mdd = paper_evidence.get("max_drawdown")
    paper_nav = paper_evidence.get("nav")
    if paper_mdd is not None and paper_mdd > PROMOTION_THRESHOLDS["mdd_max"]:
        reasons.append(
            f"paper_nav_mdd_exceeds_promotion_ceiling: {paper_mdd:.1%} > {PROMOTION_THRESHOLDS['mdd_max']:.0%}"
        )
    if paper_nav is not None and paper_nav < PAPER_NAV_PROMOTION_FLOOR:
        reasons.append(f"paper_nav_below_promotion_floor: {paper_nav:.3f} < {PAPER_NAV_PROMOTION_FLOOR:.3f}")
    blockers.extend(reason for reason in reasons if reason not in blockers)
    return not reasons


async def build_incubation_overview(
    db,
    strategy: dict,
    *,
    force_recompute: bool = False,
) -> dict:
    strategy_id = str(strategy["id"])
    quality_report = await get_latest_quality_report(db, strategy_id)
    quality_gate = dict((quality_report or {}).get("quality_gate") or {})
    quality_summary = dict((quality_report or {}).get("summary") or {})
    validation_report = dict((quality_report or {}).get("validation_report") or {})
    validation_rating = dict(validation_report.get("rating") or {})
    validation_profile = dict((quality_report or {}).get("validation_profile") or {})
    quality_report_updated_at = _quality_report_timestamp(quality_report)
    execution_audit_snapshot = None
    get_latest_execution_audit_snapshot = getattr(db, "get_latest_execution_audit_snapshot", None)
    if callable(get_latest_execution_audit_snapshot):
        try:
            execution_audit_snapshot = await get_latest_execution_audit_snapshot(strategy_id)
        except Exception:
            execution_audit_snapshot = None
    signal_stats = await db.get_signal_stats(strategy_id)
    signal_stats_signature = _signal_stats_cache_signature(signal_stats)
    execution_state_signature = await _execution_state_cache_signature(db, strategy_id)
    from aiask_quant_core.market_session import resolve_session_date

    market_session = await resolve_session_date(db)
    evidence_date = market_session["date"].isoformat()
    if not force_recompute:
        get_latest_closure_snapshot = getattr(db, "get_latest_strategy_closure_snapshot", None)
        if callable(get_latest_closure_snapshot):
            try:
                cached_snapshot = await get_latest_closure_snapshot(
                    strategy_id,
                    snapshot_type="incubation_overview",
                )
            except Exception:
                cached_snapshot = None
            cached_metadata = dict((cached_snapshot or {}).get("metadata") or {})
            cached_payload = dict((cached_snapshot or {}).get("snapshot") or {})
            if (
                cached_payload
                and _string((cached_snapshot or {}).get("as_of")) == evidence_date
                and _string(cached_metadata.get("strategy_status")) == _string(strategy.get("status"))
                and _string(cached_metadata.get("quality_report_updated_at")) == _string(quality_report_updated_at)
                and _string(cached_metadata.get("execution_audit_snapshot_id"))
                == _string((execution_audit_snapshot or {}).get("snapshot_id"))
                and _signature_text(cached_metadata.get("signal_stats_signature"))
                == _signature_text(signal_stats_signature)
                and (
                    _signature_text(cached_metadata.get("execution_state_signature"))
                    == _signature_text(execution_state_signature)
                    or (
                        "execution_state_signature" not in cached_metadata
                        and _execution_signature_is_empty(execution_state_signature)
                    )
                )
            ):
                cached_payload["as_of"] = _string(cached_payload.get("as_of")) or _string((cached_snapshot or {}).get("as_of")) or evidence_date
                cached_payload["market_session"] = {
                    "date": evidence_date,
                    "source": market_session.get("source"),
                    "latest_bar_date": str(market_session.get("latest_bar_date"))
                    if market_session.get("latest_bar_date") else None,
                    "coverage": market_session.get("coverage"),
                }
                cached_payload["recomputed"] = False
                cached_payload["cached"] = True
                cached_payload["closure_snapshot_id"] = (cached_snapshot or {}).get("snapshot_id")
                cached_payload["snapshot_source"] = "strategy_closure_snapshots"
                return with_execution_audit_snapshot_metadata(
                    cached_payload,
                    snapshot=execution_audit_snapshot,
                )

    metrics = await db.get_strategy_metrics(strategy_id)
    all_m = next((m for m in metrics if m.get("period") == "all"), {})
    backtest_m = next((m for m in metrics if m.get("period") == "backtest"), all_m)

    sharpe = float((all_m or backtest_m).get("sharpe_ratio") or 0)
    mdd = abs(float((all_m or backtest_m).get("max_drawdown") or 0))
    raw_signal_count = int(signal_stats.get("raw_signal_count") or signal_stats.get("total_signals") or 0)
    signals_with_forward_returns_count = int(signal_stats.get("signals_with_forward_returns_count") or 0)
    observed_forward_return_count = int(signal_stats.get("observed_forward_return_count") or 0)
    total_signals = raw_signal_count
    min_signal_count = 10
    hit_rate_5d = metric_bucket_value(signal_stats.get("hit_rate"), 5)
    forward_ic_5d = metric_bucket_value(signal_stats.get("forward_ic"), 5)
    forward_sharpe_5d = metric_bucket_value(signal_stats.get("forward_sharpe"), 5)

    blockers: list[str] = []
    risk_flags: list[str] = []
    blockers_by_period: dict[str, list[str]] = {}
    risk_flags_by_period: dict[str, list[str]] = {}
    observed_forward_days: list[int] = []
    forward_returns: list[dict] = []
    validation_grade = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "validation_grade") or ""
    ).strip().upper() or None
    raw_validation_grade = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "raw_validation_grade")
        or validation_grade
        or ""
    ).strip().upper() or None
    effective_validation_grade = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "effective_validation_grade")
        or validation_grade
        or ""
    ).strip().upper() or None
    validation_grade_adjustment_reason = str(
        _quality_report_field(
            quality_report,
            quality_gate,
            quality_summary,
            "validation_grade_adjustment_reason",
        ) or ""
    ).strip() or None
    raw_validation_total_score = _safe_float(
        _quality_report_field(quality_report, quality_gate, quality_summary, "raw_validation_total_score")
    )
    if raw_validation_total_score is None:
        raw_validation_total_score = _safe_float(
            validation_rating.get("base_total_score") if validation_rating else None
        )
    if raw_validation_total_score is None:
        raw_validation_total_score = _safe_float(validation_rating.get("total_score") if validation_rating else None)
    validation_total_score = _safe_float(
        _quality_report_field(quality_report, quality_gate, quality_summary, "validation_total_score")
    )
    if validation_total_score is None:
        validation_total_score = _safe_float(validation_rating.get("total_score") if validation_rating else None)
    strict_incubation_ready = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "strict_incubation_ready",
    )
    strict_incubation_blocked = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "strict_incubation_blocked",
    )
    incubation_candidate_ready = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "incubation_candidate_ready",
    )
    live_candidate_ready = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "live_candidate_ready",
    )
    admission_stage = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "admission_stage") or ""
    ).strip().lower() or None
    runtime_bootstrap_eligible = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "runtime_bootstrap_eligible",
    )
    runtime_bootstrap_reason = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "runtime_bootstrap_reason") or ""
    ).strip() or None
    runtime_bootstrap_budget_tier = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "runtime_bootstrap_budget_tier") or ""
    ).strip().lower() or None
    runtime_playbook_present = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "runtime_playbook_present",
    )
    if runtime_playbook_present is None:
        runtime_playbook_present = bool(dict(strategy.get("params") or {}).get("runtime_playbook"))
    execution_semantic_mode = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "execution_semantic_mode")
        or dict(strategy.get("params") or {}).get("execution_semantic_mode")
        or ""
    ).strip().lower() or None
    execution_semantic_gap = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "execution_semantic_gap",
    )
    if execution_semantic_gap is None:
        execution_semantic_gap = bool(dict(strategy.get("params") or {}).get("execution_semantic_gap"))
    execution_semantic_gap_reasons = [
        _string(item)
        for item in list(
            _quality_report_field(quality_report, quality_gate, quality_summary, "execution_semantic_gap_reasons")
            or dict(strategy.get("params") or {}).get("execution_semantic_gap_reasons")
            or []
        )
        if _string(item)
    ]
    dsl_required = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "dsl_required",
    )
    if dsl_required is None:
        dsl_required = bool(dict(strategy.get("params") or {}).get("dsl_required"))
    dsl_compiled = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "dsl_compiled",
    )
    if dsl_compiled is None:
        dsl_compiled = bool(dict(strategy.get("params") or {}).get("dsl_compiled"))
    instrument_profile = dict(
        strategy.get("instrument_profile")
        or dict(strategy.get("params") or {}).get("instrument_profile")
        or {}
    )
    regime_filter_contract = dict(
        strategy.get("regime_filter_contract")
        or dict(strategy.get("params") or {}).get("regime_filter_contract")
        or {}
    )
    parameter_coherence_audit = dict(
        strategy.get("parameter_coherence_audit")
        or dict(strategy.get("params") or {}).get("parameter_coherence_audit")
        or {}
    )
    thesis_invalidation_contract = dict(
        strategy.get("thesis_invalidation_contract")
        or dict(strategy.get("params") or {}).get("thesis_invalidation_contract")
        or {}
    )
    drawdown_invalidation_contract = dict(
        strategy.get("drawdown_invalidation_contract")
        or dict(strategy.get("params") or {}).get("drawdown_invalidation_contract")
        or {}
    )
    semantic_runtime_match = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "semantic_runtime_match",
    )
    if semantic_runtime_match is None:
        semantic_runtime_match = bool(
            dict(strategy.get("params") or {}).get("semantic_runtime_match")
            if dict(strategy.get("params") or {}).get("semantic_runtime_match") is not None
            else True
        )
    runtime_family_data_source = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "runtime_family_data_source")
        or dict(strategy.get("params") or {}).get("runtime_family_data_source")
        or ""
    ).strip().lower() or None
    proxy_runtime_used = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "proxy_runtime_used",
    )
    if proxy_runtime_used is None:
        proxy_runtime_used = bool(dict(strategy.get("params") or {}).get("proxy_runtime_used"))
    strategy_type_token = str(strategy.get("strategy_type") or "").strip().lower()
    if not proxy_runtime_used and strategy_type_token in {"quality_factor", "value_factor", "growth_factor"} and runtime_family_data_source != "fundamental_runtime":
        proxy_runtime_used = True
    diagnostic_only = _quality_report_bool(
        quality_report,
        quality_gate,
        quality_summary,
        "diagnostic_only",
    )
    if diagnostic_only is None:
        diagnostic_only = bool(dict(strategy.get("params") or {}).get("diagnostic_only"))
    execution_readiness_tier = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "execution_readiness_tier")
        or dict(strategy.get("params") or {}).get("execution_readiness_tier")
        or ""
    ).strip().lower() or None
    semantic_contract_missing_fields = [
        _string(item)
        for item in list(
            _quality_report_field(quality_report, quality_gate, quality_summary, "semantic_contract_missing_fields")
            or dict(strategy.get("params") or {}).get("semantic_contract_missing_fields")
            or []
        )
        if _string(item)
    ]
    target_symbols = list(
        strategy.get("target_symbols")
        or dict(strategy.get("params") or {}).get("target_symbols")
        or []
    )
    candidate_family = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "candidate_family")
        or strategy.get("strategy_type")
        or ""
    ).strip().lower() or None
    single_name_trend = candidate_family in _TREND_EXECUTABLE_DSL_TYPES and len(target_symbols) == 1
    if not diagnostic_only and (
        proxy_runtime_used
        or (single_name_trend and (
            str(instrument_profile.get("measurement_source") or "default_board_profile").strip().lower() == "default_board_profile"
            or not bool(instrument_profile.get("measured_profile_complete"))
        ))
        or semantic_contract_missing_fields
    ):
        diagnostic_only = True
    holding_period_bucket = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "holding_period_bucket") or ""
    ).strip().lower() or None
    validation_focus = str(
        _quality_report_field(quality_report, quality_gate, validation_profile, "validation_focus") or ""
    ).strip().lower() or None
    incubation_pass_mode = str(
        _quality_report_field(quality_report, quality_gate, quality_summary, "incubation_pass_mode") or ""
    ).strip().lower() or None
    admission_block_reasons = [
        str(item or "").strip()
        for item in list(
            _quality_report_field(quality_report, quality_gate, quality_summary, "admission_block_reasons") or []
        )
        if str(item or "").strip()
    ]
    has_strict_gate_signal = (
        strict_incubation_ready is not None
        or strict_incubation_blocked is not None
        or incubation_candidate_ready is not None
        or bool(incubation_pass_mode)
    )
    has_live_gate_signal = live_candidate_ready is not None or bool(admission_stage)
    signal_quality = derive_signal_quality(signal_stats, holding_period_bucket=holding_period_bucket)
    signal_quality_snapshot = _build_signal_quality_snapshot(signal_quality)
    primary_horizon = _safe_int(signal_quality.get("primary_horizon"), 5)
    secondary_horizon = _safe_int(signal_quality.get("secondary_horizon"), 10)
    primary_effective_n = _safe_int(signal_quality.get("primary_effective_n"))
    secondary_effective_n = _safe_int(signal_quality.get("secondary_effective_n"))
    primary_skill_lcb = _safe_float(signal_quality.get("primary_skill_lcb"))
    secondary_skill_lcb = _safe_float(signal_quality.get("secondary_skill_lcb"))
    recent_primary_skill_lcb = _safe_float(signal_quality.get("recent_primary_skill_lcb"))
    stability_gap = _safe_float(signal_quality.get("stability_gap"))
    coverage_ratio = _safe_float(signal_quality.get("coverage_ratio")) or 0.0
    signal_coverage_ratio = _safe_float(signal_quality.get("signal_coverage_ratio")) or 0.0
    observed_forward_days = list(signal_quality.get("observed_forward_days") or [])
    missing_forward_days = list(signal_quality.get("missing_forward_days") or [])
    execution_quality = await build_execution_quality(
        db,
        strategy,
        signal_quality=signal_quality,
        total_signals=total_signals,
    )
    execution_quality_contract = _normalize_execution_quality_for_contract(execution_quality)
    execution_quality_snapshot = _build_execution_quality_snapshot(execution_quality)
    high_precision_context = _resolve_high_precision_overview_context(
        strategy,
        quality_report=quality_report,
        quality_gate=quality_gate,
        quality_summary=quality_summary,
    )
    position_cycle_evidence = _build_position_cycle_evidence(
        signal_quality=signal_quality,
        execution_quality=execution_quality,
        context=high_precision_context,
    )
    confidence_contract_status, confidence_diagnostics = _build_confidence_diagnostics(
        strategy,
        quality_report,
        signal_quality,
    )
    audit_summary = dict(execution_quality.get("audit") or {})
    execution_audit_gate_status = _string(
        execution_quality.get("execution_audit_gate_status")
    ) or None
    execution_audit_gate_reasons = [
        _string(item)
        for item in list(execution_quality.get("execution_audit_gate_reasons") or [])
        if _string(item)
    ]
    execution_hard_gate_passed = bool(execution_quality.get("execution_hard_gate_passed"))
    snapshot_verdict = snapshot_verdict_payload(execution_audit_snapshot)
    if execution_audit_snapshot:
        execution_audit_gate_status = _string(snapshot_verdict.get("status")) or execution_audit_gate_status
        execution_audit_gate_reasons = [
            _string(item)
            for item in list(snapshot_verdict.get("reasons") or execution_audit_gate_reasons)
            if _string(item)
        ]
        execution_hard_gate_passed = bool(snapshot_verdict.get("hard_gate_passed"))
        if dict(execution_audit_snapshot.get("audit_summary") or {}):
            audit_summary = dict(execution_audit_snapshot.get("audit_summary") or {})
    signal_stage_without_execution_gate = resolve_incubation_pipeline_stage(
        signal_quality,
        open_risk_count=0,
        execution_audit_gate_status="passed",
    )
    pipeline_stage = resolve_incubation_pipeline_stage(
        signal_quality,
        open_risk_count=0,
        audit_summary=audit_summary,
        execution_audit_gate_status=execution_audit_gate_status,
    )
    high_precision_stage_override = bool(
        _string(high_precision_context.get("objective_profile")).lower() == "high_precision"
        and signal_stage_without_execution_gate == "warmup"
        and _string(position_cycle_evidence.get("status")).lower() in {"candidate", "strong"}
    )
    if high_precision_stage_override:
        signal_stage_without_execution_gate = "observe"
        if pipeline_stage == "warmup":
            pipeline_stage = "observe" if execution_audit_gate_status != "failed_metrics" else "failed"
    action_plan = await resolve_incubation_action_plan(
        db,
        strategy,
        pipeline_stage=pipeline_stage,
        signal_quality=signal_quality,
        execution_quality=execution_quality,
        total_signals=total_signals,
        validation_grade=validation_grade,
        quality_report=quality_report,
    )
    runtime_playbook_provenance = _extract_runtime_playbook_provenance(strategy)
    semantic_lineage = _extract_semantic_lineage(strategy)
    execution_lineage = await _build_execution_lineage(db, strategy["id"])
    prediction_trace_entity_chain = await _load_prediction_trace_entity_chain(
        db,
        strategy_id=str(strategy["id"]),
        account_id=_string(strategy.get("paper_account_id") or execution_quality.get("account_id")) or None,
    )
    latest_signal_snapshot = None
    get_latest_snapshot = getattr(db, "get_latest_strategy_signal_event_snapshot", None)
    if callable(get_latest_snapshot):
        try:
            latest_signal_snapshot = await get_latest_snapshot(strategy["id"])
        except Exception:
            latest_signal_snapshot = None
    if latest_signal_snapshot is None:
        list_snapshots = getattr(db, "list_strategy_signal_event_snapshots", None)
        if callable(list_snapshots):
            try:
                rows = await list_snapshots(strategy_id=strategy["id"], latest_only=True, limit=1)
                latest_signal_snapshot = dict(rows[0]) if rows else None
            except Exception:
                latest_signal_snapshot = None
    latest_signal_snapshot = dict(latest_signal_snapshot or {})
    latest_snapshot_metadata = dict(latest_signal_snapshot.get("metadata") or {})
    latest_snapshot_as_of = _coerce_date(latest_signal_snapshot.get("as_of_date"))
    latest_nonzero_signal_date = _coerce_date(
        latest_snapshot_metadata.get("latest_nonzero_signal_date")
    )
    runtime_cycle_seen_today = bool(latest_snapshot_as_of == evidence_date) if latest_snapshot_as_of else False
    risk_hard_gate = _resolve_risk_hard_gate(strategy, max_drawdown=mdd)
    risk_hard_gate_status = _string(risk_hard_gate.get("status")) or "passed"
    risk_hard_gate_reasons = [
        _string(item)
        for item in list(risk_hard_gate.get("reasons") or [])
        if _string(item)
    ]
    execution_diagnostics = {
        "execution_audit_gate_status": execution_audit_gate_status,
        "execution_audit_gate_reasons": execution_audit_gate_reasons,
        "execution_hard_gate_passed": execution_hard_gate_passed,
        "execution_audit_snapshot_id": (execution_audit_snapshot or {}).get("snapshot_id"),
        "execution_audit_snapshot_as_of": (execution_audit_snapshot or {}).get("as_of"),
        "diagnosis": execution_quality.get("diagnosis"),
        "diagnosis_reasons": list(execution_quality.get("diagnosis_reasons") or []),
        "signal_to_fill_ratio": execution_quality_contract.get("signal_to_fill_ratio"),
        "filled_order_ratio": execution_quality_contract.get("filled_order_ratio"),
        "nav_conversion_proxy": execution_quality_contract.get("nav_conversion_proxy"),
        "execution_conversion_efficiency": execution_quality_contract.get("execution_conversion_efficiency"),
        "remediation_action": action_plan.get("remediation_action"),
        "remediation_reason": action_plan.get("remediation_reason"),
        "diagnostic_only": bool(diagnostic_only or dict(confidence_diagnostics or {}).get("diagnostic_only")),
        "semantic_runtime_match": semantic_runtime_match,
        "runtime_family_data_source": runtime_family_data_source,
        "proxy_runtime_used": bool(proxy_runtime_used),
        "execution_readiness_tier": execution_readiness_tier,
        "semantic_contract_missing_fields": semantic_contract_missing_fields,
        "evidence_gap_codes": list(execution_quality_snapshot.get("evidence_gap_codes") or []),
    }
    early_signal_stage = signal_stage_without_execution_gate in _EARLY_SIGNAL_STAGES
    if risk_hard_gate_status == "kill_switch":
        blockers.extend(item for item in risk_hard_gate_reasons if item not in blockers)
    elif risk_hard_gate_status in {"forced_review", "failed_parameters"}:
        risk_flags.extend(
            item if item.startswith("risk_hard_gate:") else f"risk_hard_gate:{item}"
            for item in risk_hard_gate_reasons
            if (item if item.startswith("risk_hard_gate:") else f"risk_hard_gate:{item}") not in risk_flags
        )
    if sharpe <= PROMOTION_THRESHOLDS["sharpe_min"]:
        sharpe_message = f"Sharpe {sharpe:.2f} \u2264 {PROMOTION_THRESHOLDS['sharpe_min']:.2f}"
        if early_signal_stage:
            risk_flags.append(f"{sharpe_message}\uff08warmup/observe \u89c2\u5bdf\u9879\uff09")
        else:
            blockers.append(sharpe_message)
    if mdd >= PROMOTION_THRESHOLDS["mdd_max"]:
        mdd_message = f"\u6700\u5927\u56de\u64a4 {mdd:.1%} \u2265 {PROMOTION_THRESHOLDS['mdd_max']:.0%}"
        mdd_excess = mdd - PROMOTION_THRESHOLDS["mdd_max"]
        if early_signal_stage and mdd_excess <= _EARLY_STAGE_PROMOTION_MDD_TOLERANCE:
            risk_flags.append(f"{mdd_message}\uff08warmup/observe \u89c2\u5bdf\u5e26\uff09")
        else:
            blockers.append(mdd_message)
    if sharpe < DEPRECATION_THRESHOLDS["sharpe_negative"]:
        risk_flags.append(f"Sharpe {sharpe:.2f} < 0")
    if mdd > DEPRECATION_THRESHOLDS["mdd_critical"]:
        risk_flags.append(f"\u6700\u5927\u56de\u64a4 {mdd:.1%} > {DEPRECATION_THRESHOLDS['mdd_critical']:.0%}")
    try:
        import os as _os
        from ..factor_mining_factory.quality import QUALITY_PROFILE_ACTIVE as _ov_profile
        _min_n = 12 if str(_ov_profile or "strict").lower() in {"lite", "minimum"} else 20
        _min_n = int(_os.getenv("INCUBATION_PRIMARY_EFFECTIVE_N_MIN", str(_min_n)) or _min_n)
    except Exception:
        _min_n = 20
    if primary_effective_n < _min_n:
        message = f"\u4e3b\u7a97\u53e3{primary_horizon}D\u6709\u6548\u6837\u672c {primary_effective_n} < {_min_n}"
        if high_precision_stage_override:
            risk_flags.append(f"{message}\uff08high_precision \u517c\u5bb9\u89c2\u5bdf\u9879\uff09")
        else:
            blockers.append(message)
    if primary_skill_lcb is None or primary_skill_lcb <= 0:
        message = (
            f"\u4e3b\u7a97\u53e3{primary_horizon}D skill LCB "
            f"{(primary_skill_lcb or 0.0):+.2%} \u2264 0"
        )
        if high_precision_stage_override:
            risk_flags.append(f"{message}\uff08high_precision \u517c\u5bb9\u89c2\u5bdf\u9879\uff09")
        else:
            blockers.append(message)
    if coverage_ratio < 0.5:
        blockers.append(f"\u524d\u5411\u7a97\u53e3\u8986\u76d6\u7387 {coverage_ratio:.0%} < 50%")
    if secondary_effective_n >= 30 and secondary_skill_lcb is not None and secondary_skill_lcb <= 0:
        blockers.append(
            f"\u6b21\u7a97\u53e3{secondary_horizon}D skill LCB "
            f"{secondary_skill_lcb:+.2%} \u2264 0"
        )
    if raw_signal_count < min_signal_count:
        risk_flags.append(f"\u539f\u59cb\u4fe1\u53f7\u6570 {raw_signal_count} < {min_signal_count}")
    if signal_coverage_ratio < 0.35 and raw_signal_count >= min_signal_count:
        risk_flags.append(f"\u524d\u5411\u6837\u672c\u8986\u76d6\u7387 {signal_coverage_ratio:.0%} < 35%")
    if stability_gap is not None and stability_gap > 0.08:
        risk_flags.append(f"\u4e3b\u7a97\u53e3\u547d\u4e2d\u7387\u7a33\u5b9a\u6027\u7f3a\u53e3 {stability_gap:.1%} > 8%")
    if recent_primary_skill_lcb is not None and recent_primary_skill_lcb <= 0:
        risk_flags.append(
            f"\u8fd1\u671f\u4e3b\u7a97\u53e3{primary_horizon}D skill LCB "
            f"{recent_primary_skill_lcb:+.2%} \u2264 0"
        )
    if recent_primary_skill_lcb is not None and recent_primary_skill_lcb < -0.03:
        risk_flags.append(
            f"\u8fd1\u671f\u4e3b\u7a97\u53e3{primary_horizon}D skill LCB "
            f"{recent_primary_skill_lcb:+.2%} < -3%"
        )
    if stability_gap is not None and stability_gap > 0.10:
        risk_flags.append(f"\u4e3b\u7a97\u53e3\u7a33\u5b9a\u6027\u65ad\u88c2 {stability_gap:.1%} > 10%")

    for days in EXPECTED_FORWARD_DAYS:
        label = f"{days}D"
        bucket = dict((signal_quality.get("by_horizon") or {}).get(str(days)) or {})
        hit_rate = _safe_float(bucket.get("hit_rate"))
        hit_rate_lcb = _safe_float(bucket.get("hit_rate_lcb"))
        skill_lcb = _safe_float(bucket.get("skill_lcb"))
        recent_hit_rate = _safe_float(bucket.get("recent_hit_rate"))
        recent_skill_lcb = _safe_float(bucket.get("recent_skill_lcb"))
        stability_gap_bucket = _safe_float(bucket.get("stability_gap"))
        sample_count = _safe_int(bucket.get("sample_count"))
        effective_n = _safe_int(bucket.get("effective_n"))
        neutral_count = _safe_int(bucket.get("neutral_count"))
        forward_ic = _safe_float(bucket.get("forward_ic"))
        forward_sharpe = _safe_float(bucket.get("forward_sharpe"))
        if all(
            value is None or value == 0
            for value in (hit_rate, hit_rate_lcb, skill_lcb, recent_hit_rate, recent_skill_lcb, forward_ic, forward_sharpe)
        ) and sample_count <= 0:
            continue
        period_blockers: list[str] = []
        period_risk_flags: list[str] = []
        if days == primary_horizon and primary_effective_n < 20:
            message = f"{label}\u6709\u6548\u6837\u672c {effective_n} < 20"
            if high_precision_stage_override:
                period_risk_flags.append(f"{message}\uff08high_precision \u517c\u5bb9\u89c2\u5bdf\u9879\uff09")
            else:
                period_blockers.append(message)
        if days == primary_horizon and (skill_lcb is None or skill_lcb <= 0):
            message = f"{label} skill LCB {(skill_lcb or 0.0):+.2%} \u2264 0"
            if high_precision_stage_override:
                period_risk_flags.append(f"{message}\uff08high_precision \u517c\u5bb9\u89c2\u5bdf\u9879\uff09")
            else:
                period_blockers.append(message)
        if days == secondary_horizon and secondary_effective_n >= 30 and skill_lcb is not None and skill_lcb <= 0:
            period_blockers.append(f"{label} skill LCB {skill_lcb:+.2%} \u2264 0")
        if stability_gap_bucket is not None and stability_gap_bucket > 0.08:
            period_risk_flags.append(f"{label}\u547d\u4e2d\u7387\u7a33\u5b9a\u6027\u7f3a\u53e3 {stability_gap_bucket:.1%} > 8%")
        if recent_skill_lcb is not None and recent_skill_lcb <= 0:
            period_risk_flags.append(f"{label}\u8fd1\u671f skill LCB {recent_skill_lcb:+.2%} \u2264 0")
        if days >= 10 and forward_ic is not None and forward_ic < 0:
            period_risk_flags.append(f"{label}\u524d\u5411IC {forward_ic:.2f} < 0")
        if days >= 10 and forward_sharpe is not None and forward_sharpe < 0:
            period_risk_flags.append(f"{label}\u524d\u5411Sharpe {forward_sharpe:.2f} < 0")
        if period_blockers:
            blockers_by_period[label] = period_blockers
            blockers.extend(item for item in period_blockers if item not in blockers)
        if period_risk_flags:
            risk_flags_by_period[label] = period_risk_flags
            risk_flags.extend(item for item in period_risk_flags if item not in risk_flags)
        forward_returns.append({
            "forward_days": days,
            "label": label,
            "hit_rate": hit_rate,
            "hit_rate_lcb": hit_rate_lcb,
            "skill_lcb": skill_lcb,
            "recent_hit_rate": recent_hit_rate,
            "recent_skill_lcb": recent_skill_lcb,
            "stability_gap": stability_gap_bucket,
            "sample_count": sample_count,
            "effective_n": effective_n,
            "neutral_count": neutral_count,
            "forward_ic": forward_ic,
            "forward_sharpe": forward_sharpe,
            "blockers": period_blockers,
            "risk_flags": period_risk_flags,
        })

    gate_blockers: list[str] = []
    if validation_grade == "D":
        gate_blockers.append("validation_grade_d_not_allowed_for_promotion")
    if has_strict_gate_signal and (strict_incubation_ready is False or strict_incubation_blocked is True):
        gate_blockers.append("strict_incubation_gate_not_ready")
    if has_live_gate_signal and live_candidate_ready is False:
        gate_blockers.append("live_gate_not_ready")
    blockers.extend(item for item in gate_blockers if item not in blockers)

    strict_live_alignment_gap = bool(strict_incubation_ready) and live_candidate_ready is False
    if strict_incubation_ready is None and live_candidate_ready is None:
        strict_live_alignment_status = "unknown"
    elif bool(strict_incubation_ready) and bool(live_candidate_ready):
        strict_live_alignment_status = "aligned_live_ready"
    elif bool(strict_incubation_ready) and live_candidate_ready is False:
        strict_live_alignment_status = "strict_only_gap"
    elif strict_incubation_ready is False and live_candidate_ready is False:
        strict_live_alignment_status = "aligned_blocked"
    elif strict_incubation_ready is False and bool(live_candidate_ready):
        strict_live_alignment_status = "inconsistent_live_without_strict"
    else:
        strict_live_alignment_status = "unknown"

    promotion_ready = (
        primary_effective_n >= 60
        and secondary_effective_n >= 30
        and (primary_skill_lcb or 0.0) > 0.0
        and (secondary_skill_lcb or 0.0) > 0.0
        and (recent_primary_skill_lcb or 0.0) > 0.0
        and coverage_ratio >= 0.75
        and (stability_gap is None or stability_gap <= 0.05)
        and execution_hard_gate_passed
        and risk_hard_gate_status == "passed"
        and not blockers
    )
    # P0-3 follow-up (2026-10-23): a positive signal LCB alone must not carry a
    # losing paper account into promotion. The 2026-10-19 cohort promoted three
    # lsmap strategies at NAV 0.781 / MDD 22% because ``sharpe``/``mdd`` above
    # are read from ``strategy_metrics`` (backtest period), which these
    # strategies have no rows in — so the Sharpe/MDD checks silently ran on 0.
    # The live execution evidence is the strategy's own paper trail in
    # ``strategy_incubation_metrics``; gate promotion on it directly.
    paper_evidence = await _load_latest_paper_evidence(db, strategy_id)
    promotion_ready = apply_paper_promotion_gate(promotion_ready, blockers, paper_evidence)
    if promotion_ready and mdd > PROMOTION_THRESHOLDS["mdd_max"]:
        promotion_ready = False
        reason = f"strategy_metrics_mdd_exceeds_promotion_ceiling: {mdd:.1%} > {PROMOTION_THRESHOLDS['mdd_max']:.0%}"
        if reason not in blockers:
            blockers.append(reason)
    # INVERT-DESIGN P3 改动B：晋升额外要求"跨主要 regime 都有正 skill"（默认 OFF，零变化）。
    cross_regime_skill = evaluate_cross_regime_skill(signal_stats.get("hit_rate_by_regime"))
    if _promotion_cross_regime_enabled() and promotion_ready and not cross_regime_skill["passed"]:
        promotion_ready = False
        for tag in cross_regime_skill["negative_labels"]:
            reason = f"cross_regime_skill_lcb_non_positive:{tag}"
            if reason not in blockers:
                blockers.append(reason)
    if not execution_hard_gate_passed and execution_audit_gate_status in {
        "missing",
        "bootstrap_pending",
        "insufficient_samples",
        "bootstrap_ready",
        "failed_metrics",
    }:
        execution_gate_reason = f"execution_audit_gate:{execution_audit_gate_status}"
        execution_gate_blocks_promotion = (
            execution_audit_gate_status == "failed_metrics"
            or signal_stage_without_execution_gate in _EXECUTION_AUDIT_PROMOTION_BLOCKING_STAGES
        )
        if execution_gate_blocks_promotion:
            gate_blockers.append(execution_gate_reason)
            blockers.extend(item for item in gate_blockers if item not in blockers)
        else:
            risk_flags.append(execution_gate_reason)
    if execution_semantic_gap:
        risk_flags.append(
            f"execution_semantic_gap:{execution_semantic_mode or 'missing_executable_contract'}"
        )
    promotion_gate_status = "passed" if promotion_ready else (
        execution_audit_gate_status or "missing"
    )
    deprecation_risk = bool(
        (recent_primary_skill_lcb is not None and recent_primary_skill_lcb < -0.03)
        or (stability_gap is not None and stability_gap > 0.10)
        or sharpe < DEPRECATION_THRESHOLDS["sharpe_negative"]
        or mdd > DEPRECATION_THRESHOLDS["mdd_critical"]
    )
    hard_gate_result = {
        "pipeline_stage": pipeline_stage,
        "signal_stage_without_execution_gate": signal_stage_without_execution_gate,
        "execution_audit_gate_status": execution_audit_gate_status or "missing",
        "execution_hard_gate_passed": execution_hard_gate_passed,
        "risk_hard_gate_status": risk_hard_gate_status,
        "risk_hard_gate_reasons": risk_hard_gate_reasons,
        "promotion_ready": promotion_ready,
        "passed": pipeline_stage in {"candidate", "graduation_ready", "promoted"} and risk_hard_gate_status == "passed",
        "reasons": list(dict.fromkeys([*gate_blockers, *execution_audit_gate_reasons, *risk_hard_gate_reasons])),
    }
    prediction_trace_ledger = _build_prediction_trace_ledger_view(
        strategy,
        quality_report=quality_report,
        signal_quality_snapshot=signal_quality_snapshot,
        execution_quality_snapshot=execution_quality_snapshot,
        execution_lineage=execution_lineage,
        entity_chain=prediction_trace_entity_chain,
        latest_signal_snapshot=latest_signal_snapshot,
        hard_gate_result=hard_gate_result,
    )

    result = {
        "strategy_id": strategy["id"],
        "strategy_name": strategy.get("name"),
        "status": strategy.get("status"),
        "strategy_type": strategy.get("strategy_type"),
        "sharpe_ratio": sharpe,
        "max_drawdown": mdd,
        "total_signals": total_signals,
        "raw_signal_count": raw_signal_count,
        "signals_with_forward_returns_count": signals_with_forward_returns_count,
        "observed_forward_return_count": observed_forward_return_count,
        "minimum_signal_count": min_signal_count,
        "hit_rate_5d": hit_rate_5d,
        "forward_ic_5d": forward_ic_5d,
        "forward_sharpe_5d": forward_sharpe_5d,
        "signal_quality": signal_quality,
        "signal_quality_snapshot": signal_quality_snapshot,
        "execution_quality": execution_quality_contract,
        "execution_quality_snapshot": execution_quality_snapshot,
        "execution_diagnostics": execution_diagnostics,
        # PR-S2: 不再把整 ~125 MB 的 audit_snapshot 嵌进 result。
        # closure_snapshots 是生命周期视图，只需要审计的标识 + verdict 摘要；
        # 详情可通过 audit_snapshot_id 反查 strategy_execution_audit_snapshots。
        "execution_audit_snapshot_ref": {
            "snapshot_id": (execution_audit_snapshot or {}).get("snapshot_id"),
            "as_of": (execution_audit_snapshot or {}).get("as_of"),
            "verdict_status": (execution_audit_snapshot or {}).get("verdict_status"),
            "execution_hard_gate_passed": (execution_audit_snapshot or {}).get("execution_hard_gate_passed"),
            "verdict_reasons": list((execution_audit_snapshot or {}).get("verdict_reasons") or [])[:8],
        },
        "objective_profile": high_precision_context.get("objective_profile"),
        "precision_readiness": high_precision_context.get("precision_readiness"),
        "regime_validation_summary": dict(high_precision_context.get("regime_validation_summary") or {}),
        "cost_robustness_summary": dict(high_precision_context.get("cost_robustness_summary") or {}),
        "trade_density_summary": dict(high_precision_context.get("trade_density_summary") or {}),
        "event_prefilter_summary": dict(high_precision_context.get("event_prefilter_summary") or {}),
        "event_anchor_summary": dict(high_precision_context.get("event_anchor_summary") or {}),
        "backtest_metrics_contract_status": high_precision_context.get("backtest_metrics_contract_status"),
        "position_cycle_evidence": position_cycle_evidence,
        "regime_consistency": position_cycle_evidence.get("regime_consistency"),
        "payoff_asymmetry": position_cycle_evidence.get("payoff_asymmetry"),
        "adverse_regime_avoidance": position_cycle_evidence.get("adverse_regime_avoidance"),
        "event_prefilter_passed": position_cycle_evidence.get("event_prefilter_passed"),
        "primary_horizon": primary_horizon,
        "secondary_horizon": secondary_horizon,
        "sample_count": _safe_int(signal_quality.get("primary_sample_count")),
        "effective_n": primary_effective_n,
        "hit_rate_lcb": _safe_float(signal_quality.get("primary_hit_rate_lcb")),
        "skill_lcb": primary_skill_lcb,
        "recent_hit_rate": _safe_float(signal_quality.get("recent_primary_hit_rate")),
        "recent_skill_lcb": recent_primary_skill_lcb,
        "stability_gap": stability_gap,
        "coverage_ratio": coverage_ratio,
        "signal_coverage_ratio": signal_coverage_ratio,
        "hit_rate_lcb_method": signal_quality.get("hit_rate_lcb_method"),
        "effective_n_method": signal_quality.get("effective_n_method"),
        "signal_to_fill_ratio": execution_quality_contract.get("signal_to_fill_ratio"),
        "filled_order_ratio": execution_quality_contract.get("filled_order_ratio"),
        "nav_conversion_proxy": execution_quality_contract.get("nav_conversion_proxy"),
        "paper_nav_return": execution_quality_contract.get("paper_nav_return"),
        **(
            {
                "prediction_quality_label": execution_quality.get("prediction_quality_label"),
                "execution_quality_label": execution_quality.get("execution_quality_label"),
                "quality_diagnosis": execution_quality.get("diagnosis"),
                "quality_diagnosis_reasons": execution_quality.get("diagnosis_reasons"),
                "signal_stage_without_execution_gate": signal_stage_without_execution_gate,
                "execution_audit_gate_status": execution_audit_gate_status,
                "execution_audit_gate_reasons": execution_audit_gate_reasons,
                "execution_hard_gate_passed": execution_hard_gate_passed,
                "promotion_gate_status": promotion_gate_status,
                "confidence_contract_status": confidence_contract_status,
                "confidence_diagnostics": confidence_diagnostics,
            }
            if _confidence_diagnostics_enabled()
            else {}
        ),
        "signal_stage_without_execution_gate": signal_stage_without_execution_gate,
        "execution_audit_gate_status": execution_audit_gate_status,
        "execution_audit_gate_reasons": execution_audit_gate_reasons,
        "execution_hard_gate_passed": execution_hard_gate_passed,
        "promotion_gate_status": promotion_gate_status,
        "pipeline_stage": pipeline_stage,
        "promotion_ready": promotion_ready,
        "deprecation_risk": deprecation_risk,
        "prediction_trace_ledger": prediction_trace_ledger,
        "blockers": blockers,
        "risk_flags": risk_flags,
        "gate_blockers": gate_blockers,
        "admission_block_reasons": admission_block_reasons,
        "observed_forward_days": observed_forward_days,
        "missing_forward_days": missing_forward_days,
        "forward_returns": forward_returns,
        "blockers_by_period": blockers_by_period,
        "risk_flags_by_period": risk_flags_by_period,
        "quality_passed": bool((quality_report or {}).get("passed")),
        "validation_grade": validation_grade,
        "raw_validation_grade": raw_validation_grade,
        "effective_validation_grade": effective_validation_grade,
        "validation_grade_adjustment_reason": validation_grade_adjustment_reason,
        "raw_b_or_above": raw_validation_grade in {"A", "B"},
        "raw_validation_total_score": raw_validation_total_score,
        "validation_total_score": validation_total_score,
        "candidate_family": candidate_family,
        "holding_period_bucket": holding_period_bucket,
        "validation_focus": validation_focus,
        "trade_density": _safe_float(quality_gate.get("trade_density")),
        "post_cost_sharpe": _safe_float(quality_gate.get("post_cost_sharpe")),
        "deflated_sharpe_ratio": _safe_float(quality_gate.get("deflated_sharpe_ratio")),
        "pbo": _safe_float(quality_gate.get("pbo")),
        "strict_incubation_ready": strict_incubation_ready,
        "strict_incubation_blocked": strict_incubation_blocked,
        "incubation_candidate_ready": incubation_candidate_ready,
        "live_candidate_ready": live_candidate_ready,
        "admission_stage": admission_stage,
        "incubation_pass_mode": incubation_pass_mode,
        "strict_live_alignment_gap": strict_live_alignment_gap,
        "strict_live_alignment_status": strict_live_alignment_status,
        "runtime_bootstrap_eligible": runtime_bootstrap_eligible,
        "runtime_bootstrap_reason": runtime_bootstrap_reason,
        "runtime_bootstrap_budget_tier": runtime_bootstrap_budget_tier,
        "runtime_playbook_present": runtime_playbook_present,
        "execution_semantic_mode": execution_semantic_mode,
        "execution_semantic_gap": execution_semantic_gap,
        "execution_semantic_gap_reasons": execution_semantic_gap_reasons,
        "semantic_runtime_match": semantic_runtime_match,
        "runtime_family_data_source": runtime_family_data_source,
        "proxy_runtime_used": bool(proxy_runtime_used),
        "diagnostic_only": bool(diagnostic_only),
        "execution_readiness_tier": execution_readiness_tier,
        "semantic_contract_missing_fields": semantic_contract_missing_fields,
        "dsl_required": dsl_required,
        "dsl_compiled": dsl_compiled,
        "instrument_profile": instrument_profile,
        "regime_filter_contract": regime_filter_contract,
        "parameter_coherence_audit": parameter_coherence_audit,
        "thesis_invalidation_contract": thesis_invalidation_contract,
        "drawdown_invalidation_contract": drawdown_invalidation_contract,
        "risk_hard_gate_status": risk_hard_gate_status,
        "risk_hard_gate_reasons": risk_hard_gate_reasons,
        "runtime_playbook_provenance": runtime_playbook_provenance,
        "semantic_lineage": semantic_lineage,
        "execution_lineage": execution_lineage,
        "latest_bar_signal": int(latest_signal_snapshot.get("latest_bar_signal") or 0) if latest_signal_snapshot else 0,
        "latest_event_action": _string(latest_signal_snapshot.get("latest_event_action")) or None,
        "latest_event_date": _string(latest_signal_snapshot.get("latest_event_date")) or None,
        "latest_nonzero_signal_date": latest_nonzero_signal_date.isoformat() if latest_nonzero_signal_date else None,
        "latest_event_action_source": _string(latest_signal_snapshot.get("latest_event_action_source")) or None,
        "recent_events": list(latest_signal_snapshot.get("recent_events") or []),
        "runtime_cycle_seen_today": runtime_cycle_seen_today,
        "latest_signal_snapshot": latest_signal_snapshot or None,
        "hard_gate_result": hard_gate_result,
        "signal_vacuum_days": _safe_int(action_plan.get("signal_vacuum_days")) if action_plan.get("signal_vacuum_days") is not None else None,
        "stage_clock_days": _safe_int(action_plan.get("stage_clock_days")) if action_plan.get("stage_clock_days") is not None else None,
        "remediation_action": action_plan.get("remediation_action"),
        "remediation_reason": action_plan.get("remediation_reason"),
        "budget_action": action_plan.get("budget_action"),
        "runtime_control_mode": action_plan.get("runtime_control_mode"),
        "revision_required": bool(action_plan.get("revision_required")),
        "cleanup_recommended": bool(action_plan.get("cleanup_recommended")),
    }
    result["as_of"] = evidence_date
    result["market_session"] = {
        "date": evidence_date,
        "source": market_session.get("source"),
        "latest_bar_date": str(market_session.get("latest_bar_date"))
        if market_session.get("latest_bar_date") else None,
        "coverage": market_session.get("coverage"),
    }
    result["recomputed"] = True
    result["cached"] = False
    result["snapshot_source"] = "computed"
    return await _persist_and_finalize_overview(
        db,
        strategy=strategy,
        strategy_id=strategy_id,
        result=result,
        execution_audit_snapshot=execution_audit_snapshot,
        quality_report_updated_at=quality_report_updated_at,
        signal_stats_signature=signal_stats_signature,
        execution_state_signature=execution_state_signature,
    )
