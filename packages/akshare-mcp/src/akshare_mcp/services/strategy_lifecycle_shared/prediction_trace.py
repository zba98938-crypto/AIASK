"""Prediction-trace lineage helpers."""

from __future__ import annotations

from typing import Any, Optional

from .common import _safe_float, _safe_int, _string
from .execution_quality import _unique_tokens

def _extract_runtime_playbook_provenance(strategy: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    runtime_playbook = dict(payload.get("runtime_playbook") or params.get("runtime_playbook") or {})
    provenance = dict(runtime_playbook.get("_provenance") or {})
    source_claim_ids = [
        _string(item)
        for item in list(runtime_playbook.get("source_claim_ids") or provenance.get("source_claim_ids") or [])
        if _string(item)
    ]
    source_trade_step_ids = [
        _string(item)
        for item in list(runtime_playbook.get("source_trade_step_ids") or provenance.get("source_trade_step_ids") or [])
        if _string(item)
    ]
    derivation_labels = [
        _string(item)
        for item in list(runtime_playbook.get("derivation_labels") or provenance.get("derivation_labels") or [])
        if _string(item)
    ]
    if not (source_claim_ids or source_trade_step_ids or derivation_labels or runtime_playbook):
        return {}
    derived_from_defaults = runtime_playbook.get("derived_from_defaults")
    if derived_from_defaults is None:
        derived_from_defaults = provenance.get("derived_from_defaults")
    return {
        "source_claim_ids": source_claim_ids,
        "source_trade_step_ids": source_trade_step_ids,
        "derived_from_defaults": bool(derived_from_defaults) if derived_from_defaults is not None else None,
        "derivation_labels": derivation_labels,
        "source_priority": dict(provenance.get("source_priority") or {}),
        "runtime_playbook_source": _string(provenance.get("runtime_playbook_source")) or None,
    }


def _extract_semantic_lineage(strategy: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(strategy or {})
    params = dict(payload.get("params") or {})
    claim_to_trade_plan_map = dict(
        payload.get("claim_to_trade_plan_map") or params.get("claim_to_trade_plan_map") or {}
    )
    trade_plan_to_dsl_map = dict(
        payload.get("trade_plan_to_dsl_map") or params.get("trade_plan_to_dsl_map") or {}
    )
    evidence_alignment_audit = dict(
        payload.get("evidence_alignment_audit") or params.get("evidence_alignment_audit") or {}
    )
    runtime_playbook_provenance = _extract_runtime_playbook_provenance(payload)
    if not (claim_to_trade_plan_map or trade_plan_to_dsl_map or evidence_alignment_audit or runtime_playbook_provenance):
        return {}
    trade_step_sections = dict(trade_plan_to_dsl_map.get("trade_step_to_dsl_sections") or {})
    return {
        "claim_to_trade_plan_map": claim_to_trade_plan_map,
        "trade_plan_to_dsl_map": trade_plan_to_dsl_map,
        "runtime_playbook_provenance": runtime_playbook_provenance,
        "evidence_alignment_status": _string(
            evidence_alignment_audit.get("evidence_alignment_status")
            or evidence_alignment_audit.get("alignment_status")
        ) or None,
        "evidence_alignment_score": _safe_float(evidence_alignment_audit.get("evidence_alignment_score")),
        "semantic_integrity_score": _safe_float(evidence_alignment_audit.get("semantic_integrity_score")),
        "hard_fail_reasons": [
            _string(item)
            for item in list(evidence_alignment_audit.get("hard_fail_reasons") or [])
            if _string(item)
        ],
        "claim_count": len(dict(claim_to_trade_plan_map.get("claim_to_trade_step_ids") or {})),
        "mapped_trade_step_count": sum(1 for value in trade_step_sections.values() if list(value or [])),
    }


async def _build_execution_lineage(db, strategy_id: str) -> dict[str, Any]:
    list_method = getattr(db, "list_strategy_signal_evidence", None)
    if not callable(list_method):
        return {}

    try:
        rows = await list_method(strategy_id=strategy_id, limit=500)
    except TypeError:
        rows = await list_method(strategy_id=strategy_id)
    except Exception:
        return {}

    evidence_rows = [dict(item or {}) for item in list(rows or []) if isinstance(item, dict)]
    if not evidence_rows:
        return {}

    def _lineage_status(row: dict[str, Any]) -> str:
        payload = dict(row.get("payload") or {})
        explicit = _string(row.get("lineage_status") or payload.get("lineage_status"))
        if explicit:
            return explicit
        if _string(row.get("runtime_action_reason")) or _string(row.get("runtime_action_source")):
            return "mapped_runtime_action" if _string(row.get("applied_trade_step_id")) else "unmapped_runtime_action"
        if _string(row.get("applied_trade_step_id")):
            return "mapped_trade_step"
        if _string(row.get("applied_claim_id")):
            return "claim_only"
        return "missing"

    ordered_rows = sorted(
        evidence_rows,
        key=lambda item: (
            _string(item.get("signal_date")),
            _string(item.get("signal_ts")),
            _string(item.get("created_at")),
            _string(item.get("evidence_id")),
            _string(item.get("applied_trade_step_id")),
        ),
        reverse=True,
    )
    runtime_rows = [
        item
        for item in ordered_rows
        if _string(item.get("runtime_action_reason")) or _string(item.get("runtime_action_source"))
    ]
    preview_rows = runtime_rows[:8] if runtime_rows else ordered_rows[:8]
    runtime_action_reason_counts: dict[str, int] = {}
    runtime_action_source_counts: dict[str, int] = {}
    lineage_status_counts: dict[str, int] = {}
    for item in ordered_rows:
        status = _lineage_status(item)
        lineage_status_counts[status] = lineage_status_counts.get(status, 0) + 1
        reason = _string(item.get("runtime_action_reason"))
        if reason:
            runtime_action_reason_counts[reason] = runtime_action_reason_counts.get(reason, 0) + 1
        source = _string(item.get("runtime_action_source"))
        if source:
            runtime_action_source_counts[source] = runtime_action_source_counts.get(source, 0) + 1

    return {
        "signal_evidence_count": len(ordered_rows),
        "claim_count": len({_string(item.get("applied_claim_id")) for item in ordered_rows if _string(item.get("applied_claim_id"))}),
        "trade_step_count": len({_string(item.get("applied_trade_step_id")) for item in ordered_rows if _string(item.get("applied_trade_step_id"))}),
        "mapped_trade_step_count": sum(
            1 for item in ordered_rows if _string(item.get("applied_trade_step_id"))
        ),
        "runtime_action_count": len(runtime_rows),
        "unmapped_runtime_action_count": sum(
            1 for item in runtime_rows if _lineage_status(item) == "unmapped_runtime_action"
        ),
        "recent_signal_ids": _unique_tokens(
            [_string(item.get("signal_id")) for item in ordered_rows if _string(item.get("signal_id"))],
            limit=8,
        ),
        "latest_signal_date": _string(ordered_rows[0].get("signal_date")) or None,
        "latest_signal_event_at": (
            _string(ordered_rows[0].get("signal_ts"))
            or _string(ordered_rows[0].get("created_at"))
            or None
        ),
        "lineage_status_counts": lineage_status_counts,
        "runtime_action_reason_counts": runtime_action_reason_counts,
        "runtime_action_source_counts": runtime_action_source_counts,
        "recent_runtime_actions": [
            {
                "signal_id": _string(item.get("signal_id")) or None,
                "signal_date": _string(item.get("signal_date")) or None,
                "code": _string(item.get("code")) or _string(dict(item.get("payload") or {}).get("code")) or None,
                "applied_claim_id": _string(item.get("applied_claim_id")) or None,
                "applied_trade_step_id": _string(item.get("applied_trade_step_id")) or None,
                "runtime_action_reason": _string(item.get("runtime_action_reason")) or None,
                "runtime_action_source": _string(item.get("runtime_action_source")) or None,
                "lineage_status": _lineage_status(item),
            }
            for item in preview_rows
        ],
    }


async def _load_prediction_trace_entity_chain(
    db,
    *,
    strategy_id: str,
    account_id: str | None = None,
) -> dict[str, Any]:
    orders: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    positions: list[dict[str, Any]] = []
    fills: list[dict[str, Any]] = []
    nav_rows: list[dict[str, Any]] = []

    list_orders = getattr(db, "list_strategy_paper_orders", None)
    if callable(list_orders):
        try:
            orders = [dict(item or {}) for item in list(await list_orders(strategy_id, limit=500) or [])]
        except TypeError:
            orders = [dict(item or {}) for item in list(await list_orders(strategy_id) or [])]
        except Exception:
            orders = []

    list_trades = getattr(db, "list_strategy_paper_trades", None)
    if callable(list_trades):
        try:
            if account_id:
                trades = [
                    dict(item or {})
                    for item in list(await list_trades(strategy_id, account_id=account_id, limit=500) or [])
                ]
            else:
                trades = [dict(item or {}) for item in list(await list_trades(strategy_id, limit=500) or [])]
        except TypeError:
            trades = [dict(item or {}) for item in list(await list_trades(strategy_id) or [])]
        except Exception:
            trades = []

    list_positions = getattr(db, "list_strategy_trade_positions", None)
    if callable(list_positions):
        try:
            positions = [
                dict(item or {})
                for item in list(await list_positions(strategy_id=strategy_id, limit=500) or [])
            ]
        except TypeError:
            positions = [dict(item or {}) for item in list(await list_positions(strategy_id=strategy_id) or [])]
        except Exception:
            positions = []

    list_fills = getattr(db, "list_strategy_trade_position_fills", None)
    if callable(list_fills):
        try:
            fills = [
                dict(item or {})
                for item in list(await list_fills(strategy_id=strategy_id, limit=1000) or [])
            ]
        except TypeError:
            fills = [dict(item or {}) for item in list(await list_fills(strategy_id=strategy_id) or [])]
        except Exception:
            fills = []

    nav_rows_method = getattr(db, "get_paper_nav_rows", None)
    if account_id and callable(nav_rows_method):
        try:
            nav_rows = [dict(item or {}) for item in list(await nav_rows_method(account_id, limit=120) or [])]
        except TypeError:
            nav_rows = [dict(item or {}) for item in list(await nav_rows_method(account_id) or [])]
        except Exception:
            nav_rows = []

    return {
        "orders": orders,
        "trades": trades,
        "positions": positions,
        "fills": fills,
        "nav_rows": nav_rows,
    }


def _prediction_trace_node(
    *,
    available: bool,
    source_mode: str,
    count: Any = 0,
    ids: list[Any] | None = None,
    status: Any = None,
    as_of: Any = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "available": bool(available),
        "source_mode": _string(source_mode) or "entity_backed",
        "count": _safe_int(count),
        "ids": _unique_tokens(list(ids or []), limit=8),
    }
    if _string(status):
        payload["status"] = _string(status)
    if _string(as_of):
        payload["as_of"] = _string(as_of)
    for key, value in extra.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            payload[key] = _unique_tokens(value, limit=8)
        else:
            payload[key] = value
    return payload


def _build_prediction_trace_ledger_view(
    strategy: dict[str, Any],
    *,
    quality_report: Optional[dict[str, Any]],
    signal_quality_snapshot: Optional[dict[str, Any]],
    execution_quality_snapshot: Optional[dict[str, Any]],
    execution_lineage: Optional[dict[str, Any]],
    entity_chain: Optional[dict[str, Any]],
    latest_signal_snapshot: Optional[dict[str, Any]],
    hard_gate_result: Optional[dict[str, Any]],
) -> dict[str, Any]:
    strategy_payload = dict(strategy or {})
    params = dict(strategy_payload.get("params") or {})
    report_payload = dict(quality_report or {})
    report_summary = dict(report_payload.get("summary") or {})
    signal_snapshot = dict(signal_quality_snapshot or {})
    execution_snapshot = dict(execution_quality_snapshot or {})
    lineage = dict(execution_lineage or {})
    chain = dict(entity_chain or {})
    latest_snapshot = dict(latest_signal_snapshot or {})
    hard_gate = dict(hard_gate_result or {})
    orders = [dict(item or {}) for item in list(chain.get("orders") or []) if isinstance(item, dict)]
    trades = [dict(item or {}) for item in list(chain.get("trades") or []) if isinstance(item, dict)]
    positions = [dict(item or {}) for item in list(chain.get("positions") or []) if isinstance(item, dict)]
    fills = [dict(item or {}) for item in list(chain.get("fills") or []) if isinstance(item, dict)]
    nav_rows = [dict(item or {}) for item in list(chain.get("nav_rows") or []) if isinstance(item, dict)]
    prediction_trace_id = (
        _string(strategy_payload.get("prediction_trace_id"))
        or _string(strategy_payload.get("trace_id"))
        or _string(params.get("prediction_trace_id"))
        or _string(params.get("trace_id"))
        or _string(report_summary.get("prediction_trace_id"))
        or _string(report_summary.get("trace_id"))
    )
    paper_account_id = _string(strategy_payload.get("paper_account_id")) or None
    signal_ids = _unique_tokens(
        [
            *list(lineage.get("recent_signal_ids") or []),
            _string(latest_snapshot.get("signal_id")),
            _string(latest_snapshot.get("id")),
        ],
        limit=8,
    )
    order_ids = _unique_tokens(
        [
            _string(item.get("id") or item.get("order_id"))
            for item in orders
            if _string(item.get("id") or item.get("order_id"))
        ],
        limit=8,
    )
    order_status_counts: dict[str, int] = {}
    for item in orders:
        order_status = _string(item.get("status")) or "unknown"
        order_status_counts[order_status] = order_status_counts.get(order_status, 0) + 1
    trade_ids = _unique_tokens(
        [
            _string(item.get("id") or item.get("trade_id"))
            for item in trades
            if _string(item.get("id") or item.get("trade_id"))
        ],
        limit=8,
    )
    position_ids = _unique_tokens(
        [
            _string(item.get("position_id") or item.get("id"))
            for item in positions
            if _string(item.get("position_id") or item.get("id"))
        ],
        limit=8,
    )
    linked_signal_count = len(
        {
            _string(item.get("signal_id"))
            for item in [*trades, *fills]
            if _string(item.get("signal_id"))
        }
    )
    linked_position_count = len(
        {
            _string(item.get("position_id"))
            for item in [*trades, *fills]
            if _string(item.get("position_id"))
        }
    )
    position_count = len(positions)
    mapped_position_count = _safe_int(execution_snapshot.get("mapped_position_count"))
    closed_position_count = sum(
        1 for item in positions if _string(item.get("status")) == "closed" or _string(item.get("closed_at"))
    )
    incomplete_position_count = _safe_int(execution_snapshot.get("incomplete_position_count"))
    trade_count = len(trades)
    realized_trade_count = _safe_int(execution_snapshot.get("realized_trade_count"))
    nav_row_count = len(nav_rows)
    realized_pnl_total = _safe_float(execution_snapshot.get("realized_pnl_total"))
    trade_expectancy = _safe_float(execution_snapshot.get("trade_expectancy"))
    execution_audit_gate_status = _string(execution_snapshot.get("execution_audit_gate_status")) or "missing"
    signal_event_available = bool(latest_snapshot or _safe_int(lineage.get("signal_evidence_count")) > 0)
    intended_order_available = len(order_ids) > 0 or len(orders) > 0
    actual_fill_available = trade_count > 0 or realized_trade_count > 0
    position_round_trip_available = position_count > 0 or mapped_position_count > 0 or closed_position_count > 0
    pnl_audit_available = nav_row_count > 0 or realized_pnl_total is not None or trade_expectancy is not None
    evidence_gap_codes: list[str] = []
    if not prediction_trace_id:
        evidence_gap_codes.append("missing_prediction_trace_id")
    if not signal_event_available:
        evidence_gap_codes.append("missing_signal_event")
    if not intended_order_available:
        evidence_gap_codes.append("missing_intended_order")
        if paper_account_id:
            evidence_gap_codes.append("missing_order_linkage")
    if not actual_fill_available:
        evidence_gap_codes.append("missing_actual_fill")
        evidence_gap_codes.append("missing_trade_linkage")
    if not position_round_trip_available:
        evidence_gap_codes.append("missing_position_round_trip")
        evidence_gap_codes.append("missing_position_linkage")
    elif incomplete_position_count > 0:
        evidence_gap_codes.append("incomplete_round_trip_coverage")
    if not pnl_audit_available:
        evidence_gap_codes.append("missing_pnl_audit_summary")
        evidence_gap_codes.append("missing_pnl_audit")
    if list(execution_snapshot.get("evidence_gap_codes") or []):
        evidence_gap_codes.extend(list(execution_snapshot.get("evidence_gap_codes") or []))
    if execution_snapshot.get("realized_vs_modeled_cost_gap") is None:
        evidence_gap_codes.append("missing_realized_vs_modeled_cost_gap")
    gate_reasons = _unique_tokens(hard_gate.get("reasons") or [], limit=16)
    return {
        "contract_version": "strategy_factory.prediction_trace_ledger.v2",
        "prediction_trace_id": prediction_trace_id or None,
        "strategy_id": _string(strategy_payload.get("id")) or None,
        "hypothesis_spec": _prediction_trace_node(
            available=bool(
                _string(strategy_payload.get("research_protocol_version") or params.get("research_protocol_version"))
                or _string(strategy_payload.get("candidate_contract_version") or params.get("candidate_contract_version"))
                or _string(params.get("dsl_signature"))
                or dict(params.get("research_validation_contract") or {})
            ),
            source_mode="entity_backed",
            count=1,
            status="ready",
            research_protocol_version=_string(
                strategy_payload.get("research_protocol_version") or params.get("research_protocol_version")
            ) or None,
            candidate_contract_version=_string(
                strategy_payload.get("candidate_contract_version") or params.get("candidate_contract_version")
            ) or None,
            dsl_signature=_string(params.get("dsl_signature")) or None,
        ),
        "signal_event": _prediction_trace_node(
            available=signal_event_available,
            source_mode="entity_backed",
            count=_safe_int(lineage.get("signal_evidence_count")),
            ids=signal_ids,
            status=_string(signal_snapshot.get("status")) or None,
            as_of=(
                _string(latest_snapshot.get("as_of_date"))
                or _string(latest_snapshot.get("created_at"))
                or _string(lineage.get("latest_signal_event_at"))
                or _string(lineage.get("latest_signal_date"))
                or None
            ),
            latest_signal_snapshot_id=_string(latest_snapshot.get("id")) or None,
            signal_evidence_count=_safe_int(lineage.get("signal_evidence_count")),
            runtime_action_count=_safe_int(lineage.get("runtime_action_count")),
            recent_signal_ids=signal_ids,
        ),
        "intended_order": _prediction_trace_node(
            available=intended_order_available,
            source_mode="entity_backed",
            count=len(orders),
            ids=order_ids,
            status="available" if intended_order_available else "missing",
            as_of=(
                _string(orders[0].get("filled_at"))
                if orders
                else None
            ) or (
                _string(orders[0].get("updated_at"))
                if orders
                else None
            ) or (
                _string(orders[0].get("created_at"))
                if orders
                else None
            ) or None,
            paper_account_id=paper_account_id,
            order_count=len(orders),
            order_status_counts=order_status_counts,
            order_ids=order_ids,
        ),
        "actual_fill": _prediction_trace_node(
            available=actual_fill_available,
            source_mode="entity_backed",
            count=trade_count or realized_trade_count,
            ids=trade_ids,
            status=execution_audit_gate_status,
            as_of=(
                _string(trades[0].get("trade_time"))
                if trades
                else None
            ) or (
                _string(trades[0].get("created_at"))
                if trades
                else None
            ) or None,
            trade_count=trade_count,
            realized_trade_count=realized_trade_count,
            trade_ids=trade_ids,
            linked_signal_count=linked_signal_count,
            linked_position_count=linked_position_count,
        ),
        "position_round_trip": _prediction_trace_node(
            available=position_round_trip_available,
            source_mode="entity_backed",
            count=mapped_position_count or position_count,
            ids=position_ids,
            status=(
                "incomplete"
                if incomplete_position_count > 0
                else ("available" if position_round_trip_available else "missing")
            ),
            as_of=(
                _string(positions[0].get("closed_at"))
                if positions
                else None
            ) or (
                _string(positions[0].get("last_trade_time"))
                if positions
                else None
            ) or (
                _string(positions[0].get("opened_at"))
                if positions
                else None
            ) or None,
            position_count=position_count,
            mapped_position_count=mapped_position_count,
            closed_position_count=closed_position_count,
            round_trip_close_rate=execution_snapshot.get("round_trip_close_rate"),
            incomplete_position_count=incomplete_position_count,
            position_ids=position_ids,
        ),
        "pnl_audit_summary": _prediction_trace_node(
            available=pnl_audit_available,
            source_mode="entity_backed",
            count=nav_row_count or realized_trade_count,
            ids=_unique_tokens(
                [_string(item.get("id") or item.get("nav_id") or item.get("as_of_date")) for item in nav_rows],
                limit=8,
            ),
            status=execution_audit_gate_status,
            as_of=(
                _string(nav_rows[0].get("as_of_date"))
                if nav_rows
                else None
            ) or (
                _string(nav_rows[0].get("created_at"))
                if nav_rows
                else None
            ) or None,
            nav_row_count=nav_row_count,
            realized_pnl_total=realized_pnl_total,
            trade_expectancy=trade_expectancy,
            pnl_conversion_efficiency=execution_snapshot.get("pnl_conversion_efficiency"),
            execution_conversion_efficiency=execution_snapshot.get("execution_conversion_efficiency"),
            execution_audit_gate_status=execution_audit_gate_status,
            realized_vs_modeled_cost_gap=execution_snapshot.get("realized_vs_modeled_cost_gap"),
            entry_quality=execution_snapshot.get("entry_quality"),
            exit_discipline=execution_snapshot.get("exit_discipline"),
            regime_mismatch_events=list(execution_snapshot.get("regime_mismatch_events") or []),
        ),
        "gate_decisions": {
            "execution_audit_gate_status": execution_audit_gate_status,
            "promotion_ready": bool(hard_gate.get("promotion_ready")),
            "hard_gate_passed": bool(hard_gate.get("passed")),
            "failure_reasons": gate_reasons,
        },
        "evidence_gap_codes": _unique_tokens(evidence_gap_codes, limit=16),
    }

