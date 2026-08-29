from __future__ import annotations

from ._base import *  # noqa: F401,F403
from ._base import (
    _coerce_ts,
    _fallback_execution_audit_gate,
    _safe_float,
    _safe_int,
    _safe_rules_dict,
    _string,
)


class _ExecutionAcceptanceMixin:
    async def run_execution_audit_acceptance(
        self,
        strategy_id: Optional[str] = None,
        *,
        backfill: bool = True,
    ) -> dict:
        strategy_filter = str(strategy_id or "").strip() or None
        backfill_result = None
        if backfill:
            backfill_result = {}
            if hasattr(self, "backfill_trade_position_links"):
                backfill_result["trade_position_links"] = await self.backfill_trade_position_links(
                    strategy_filter
                )
            if hasattr(self, "backfill_strategy_signal_evidence_native"):
                backfill_result["native_signal_evidence"] = await self.backfill_strategy_signal_evidence_native(
                    strategy_filter
                )

        verification = await self.get_execution_audit_verification(strategy_filter)
        coverage = dict(verification.get("coverage") or {})
        orders = dict(coverage.get("paper_orders") or {})
        trades = dict(coverage.get("paper_trades") or {})
        lineage_source = dict(verification.get("lineage_source") or {})
        round_trip = dict(verification.get("trade_round_trip") or {})
        audit_summary = dict(round_trip.get("audit_summary") or {})

        def _is_full_ratio(value) -> bool:
            try:
                return float(value) >= 0.999999
            except Exception:
                return False

        realized_trade_count = int(audit_summary.get("realized_trade_count") or 0)
        incomplete_position_count = int(audit_summary.get("incomplete_position_count") or 0)
        acceptance_matrix = {
            "schema_ready": bool(
                dict(verification.get("schema") or {}).get("all_required_tables_present")
                and dict(verification.get("schema") or {}).get("all_required_columns_present")
            ),
            "migration_ready": bool(
                dict(verification.get("migrations") or {}).get("all_required_keys_applied")
            ),
            "orders_position_link_ready": int(orders.get("total") or 0) == 0 or _is_full_ratio(orders.get("position_id_ratio")),
            "trades_position_link_ready": int(trades.get("total") or 0) == 0 or _is_full_ratio(trades.get("position_id_ratio")),
            "native_lineage_ready": str(lineage_source.get("status") or "") in {
                "native_ready",
                "native_compile_stable",
                "native_backfilled",
            },
            "fill_round_trip_ready": int(round_trip.get("fill_count") or 0) >= int(round_trip.get("position_count") or 0),
            "bootstrap_gate_ready": bool(audit_summary.get("bootstrap_gate_ready")),
            "hard_gate_ready": bool(audit_summary.get("audit_ready_for_hard_gate")),
            "trade_evidence_ready": realized_trade_count > 0 and incomplete_position_count == 0,
        }
        acceptance_matrix["overall_ready"] = all(
            acceptance_matrix[key]
            for key in (
                "schema_ready",
                "migration_ready",
                "orders_position_link_ready",
                "trades_position_link_ready",
                "native_lineage_ready",
                "fill_round_trip_ready",
                "hard_gate_ready",
                "trade_evidence_ready",
            )
        )

        blockers = []
        if not acceptance_matrix["schema_ready"]:
            blockers.append("execution_audit_schema_incomplete")
        if not acceptance_matrix["migration_ready"]:
            blockers.append("execution_audit_migrations_unverified")
        if not acceptance_matrix["orders_position_link_ready"]:
            blockers.append("paper_orders_position_link_incomplete")
        if not acceptance_matrix["trades_position_link_ready"]:
            blockers.append("paper_trades_position_link_incomplete")
        if not acceptance_matrix["native_lineage_ready"]:
            blockers.append("native_signal_evidence_lineage_missing")
        if not acceptance_matrix["fill_round_trip_ready"]:
            blockers.append("trade_position_fill_round_trip_incomplete")
        if not acceptance_matrix["trade_evidence_ready"]:
            blockers.append("realized_trade_evidence_insufficient")
        if not acceptance_matrix["hard_gate_ready"]:
            gate_status = str(audit_summary.get("execution_audit_gate_status") or "execution_audit_gate_not_passed")
            blockers.append("promotion_hard_gate_pending" if gate_status == "bootstrap_ready" else gate_status)

        status = (
            "ready"
            if acceptance_matrix["overall_ready"]
            else "pending_data"
            if realized_trade_count <= 0 and not blockers[:1] == ["execution_audit_schema_incomplete"]
            else "needs_attention"
        )
        recommendations = list(verification.get("recommendations") or [])
        if not acceptance_matrix["overall_ready"] and "run execution_audit_acceptance after production backfill" not in recommendations:
            recommendations.append("run execution_audit_acceptance after production backfill")
        blocker_details = self._build_execution_audit_blocker_details(
            verification=verification,
            acceptance_matrix=acceptance_matrix,
            audit_summary=audit_summary,
        )
        actionable_todos = list(
            dict.fromkeys(
                item.get("todo")
                for item in blocker_details
                if _string(item.get("todo"))
            )
        )
        gap_categories = sorted(
            {
                _string(item.get("category"))
                for item in blocker_details
                if _string(item.get("category"))
            }
        )
        result = {
            "status": status,
            "strategy_id": strategy_filter,
            "method": "execution_audit_acceptance_v1",
            "backfill_executed": bool(backfill),
            "backfill_result": backfill_result,
            "acceptance_matrix": acceptance_matrix,
            "blockers": blockers,
            "blocker_details": blocker_details,
            "gap_categories": gap_categories,
            "actionable_todos": actionable_todos,
            "verification": verification,
            "trade_audit_summary": audit_summary or None,
            "recommendations": recommendations,
            "execution_audit_gate_status": _string(
                audit_summary.get("execution_audit_gate_status")
            )
            or None,
            "execution_audit_gate_reasons": list(
                audit_summary.get("execution_audit_gate_reasons") or []
            ),
            "execution_hard_gate_passed": bool(
                audit_summary.get("audit_ready_for_hard_gate")
            ),
        }
        if strategy_filter and hasattr(self, "upsert_execution_audit_snapshot"):
            snapshot_builder = get_execution_audit_snapshot_builder()
            snapshot_metadata = get_execution_audit_snapshot_metadata()
            try:
                if callable(snapshot_builder):
                    snapshot = await self.upsert_execution_audit_snapshot(
                        snapshot_builder(
                            strategy_id=strategy_filter,
                            verification=verification,
                            acceptance=result,
                            audit_summary=audit_summary,
                            verdict_status=_string(audit_summary.get("execution_audit_gate_status")) or "missing",
                            verdict_reasons=list(audit_summary.get("execution_audit_gate_reasons") or []),
                            execution_hard_gate_passed=bool(
                                audit_summary.get("audit_ready_for_hard_gate")
                            ),
                            as_of=date.today().isoformat(),
                            correlation_id=_string(strategy_filter),
                            source_action="execution_audit_acceptance",
                            metadata={
                                "acceptance_status": status,
                                "backfill_executed": bool(backfill),
                                "gap_categories": list(gap_categories),
                            },
                        )
                    )
                else:
                    snapshot = None
            except Exception as exc:
                logger.warning(
                    "execution audit acceptance snapshot persist failed for %s: %s",
                    strategy_filter,
                    exc,
                )
                snapshot = None
            if snapshot:
                result["snapshot"] = snapshot
                result["as_of"] = snapshot.get("as_of")
                result["correlation_id"] = snapshot.get("correlation_id")
                result["factory_run_id"] = snapshot.get("factory_run_id")
                if callable(snapshot_metadata):
                    result = snapshot_metadata(result, snapshot=snapshot)
        return result
