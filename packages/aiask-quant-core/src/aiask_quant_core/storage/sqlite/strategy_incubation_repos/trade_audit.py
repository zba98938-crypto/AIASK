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


class _TradeAuditMixin:
    def _execution_lineage_semantic_contract_status(self, strategy: Optional[dict]) -> dict:
        payload = dict(strategy or {})
        params = dict(payload.get("params") or {})
        evidence_chain = dict(payload.get("evidence_chain") or params.get("evidence_chain") or {})
        prediction_contract = dict(
            payload.get("prediction_contract") or params.get("prediction_contract") or {}
        )
        claim_to_trade_plan_map = dict(
            payload.get("claim_to_trade_plan_map")
            or params.get("claim_to_trade_plan_map")
            or {}
        )
        trade_plan_to_dsl_map = dict(
            payload.get("trade_plan_to_dsl_map")
            or params.get("trade_plan_to_dsl_map")
            or {}
        )
        evidence_alignment_audit = dict(
            payload.get("evidence_alignment_audit")
            or params.get("evidence_alignment_audit")
            or {}
        )
        claim_to_trade_step_ids = dict(
            claim_to_trade_plan_map.get("claim_to_trade_step_ids") or {}
        )
        trade_step_to_dsl_sections = dict(
            trade_plan_to_dsl_map.get("trade_step_to_dsl_sections") or {}
        )
        hard_fail_reasons = [
            _string(item)
            for item in list(evidence_alignment_audit.get("hard_fail_reasons") or [])
            if _string(item)
        ]
        missing_fields: list[str] = []
        if not evidence_chain:
            missing_fields.append("evidence_chain")
        if not prediction_contract:
            missing_fields.append("prediction_contract")
        if not claim_to_trade_step_ids:
            missing_fields.append("claim_to_trade_plan_map")
        if not trade_step_to_dsl_sections:
            missing_fields.append("trade_plan_to_dsl_map")
        if not evidence_alignment_audit:
            missing_fields.append("evidence_alignment_audit")
        compile_stable_ready = not missing_fields and not hard_fail_reasons
        status = (
            "compile_stable_ready"
            if compile_stable_ready
            else "legacy_contract_gap"
            if payload
            else "missing_strategy"
        )
        return {
            "status": status,
            "compile_stable_ready": compile_stable_ready,
            "missing_fields": missing_fields,
            "hard_fail_reasons": hard_fail_reasons,
            "evidence_alignment_status": _string(
                evidence_alignment_audit.get("evidence_alignment_status")
                or evidence_alignment_audit.get("alignment_status")
            )
            or None,
            "evidence_alignment_score": _safe_float(
                evidence_alignment_audit.get("evidence_alignment_score")
            ),
            "semantic_integrity_score": _safe_float(
                evidence_alignment_audit.get("semantic_integrity_score")
            ),
            "mapped_claim_count": sum(
                1
                for value in claim_to_trade_step_ids.values()
                if list(value or [])
            ),
            "mapped_trade_step_count": sum(
                1
                for value in trade_step_to_dsl_sections.values()
                if list(value or [])
            ),
            "has_evidence_chain": bool(evidence_chain),
            "has_prediction_contract": bool(prediction_contract),
        }

    def _normalize_trade_audit_summary_counts(
        self,
        payload: Optional[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized = dict(payload or {})
        raw_incomplete_position_count = int(
            normalized.get("incomplete_position_count") or 0
        )
        open_position_count = int(normalized.get("open_position_count") or 0)
        normalized["raw_incomplete_position_count"] = raw_incomplete_position_count
        normalized["open_position_count"] = open_position_count
        normalized["incomplete_position_count"] = max(
            0,
            raw_incomplete_position_count - open_position_count,
        )
        return normalized

    def _build_execution_audit_blocker_details(
        self,
        *,
        verification: dict,
        acceptance_matrix: dict,
        audit_summary: dict,
    ) -> list[dict]:
        coverage = dict(verification.get("coverage") or {})
        orders = dict(coverage.get("paper_orders") or {})
        trades = dict(coverage.get("paper_trades") or {})
        lineage_source = dict(verification.get("lineage_source") or {})
        round_trip = dict(verification.get("trade_round_trip") or {})
        semantic_contract = dict(lineage_source.get("semantic_contract") or {})

        def _append(
            code: str,
            category: str,
            summary: str,
            todo: str,
            *,
            severity: str = "error",
            owner: str = "engineering",
            evidence: Optional[dict] = None,
        ) -> dict:
            payload = {
                "blocker": code,
                "category": category,
                "severity": severity,
                "owner": owner,
                "summary": summary,
                "todo": todo,
            }
            if evidence:
                payload["evidence"] = evidence
            return payload

        blocker_details: list[dict] = []
        if not acceptance_matrix.get("schema_ready"):
            blocker_details.append(
                _append(
                    "execution_audit_schema_incomplete",
                    "schema_gap",
                    "execution audit 依赖表或列仍不完整，当前结果不能作为生产验收依据。",
                    "补齐 Phase 5/6 schema，并重新执行 execution_audit_acceptance 验证表/列覆盖率。",
                )
            )
        if not acceptance_matrix.get("migration_ready"):
            blocker_details.append(
                _append(
                    "execution_audit_migrations_unverified",
                    "migration_gap",
                    "迁移标记或 backfill 标记缺失，无法证明真实生产库已经跑完闭环。",
                    "在目标 SQLite 数据库执行迁移与 backfill，并确认 migration key 全量落库。",
                )
            )
        if not acceptance_matrix.get("orders_position_link_ready"):
            blocker_details.append(
                _append(
                    "paper_orders_position_link_incomplete",
                    "data_gap",
                    "paper_orders 仍存在缺失 position_id 的链路，round-trip 聚合不完整。",
                    "重新运行 trade position linkage backfill，并核对 paper_orders.position_id 覆盖率直到 100%。",
                    evidence={
                        "total_orders": int(orders.get("total") or 0),
                        "position_id_linked": int(orders.get("position_id_linked") or 0),
                    },
                )
            )
        if not acceptance_matrix.get("trades_position_link_ready"):
            blocker_details.append(
                _append(
                    "paper_trades_position_link_incomplete",
                    "data_gap",
                    "paper_trades 仍存在缺失 position_id 的链路，无法完整复算 fill -> position。",
                    "重新运行 trade position linkage backfill，并核对 paper_trades.position_id 覆盖率直到 100%。",
                    evidence={
                        "total_trades": int(trades.get("total") or 0),
                        "position_id_linked": int(trades.get("position_id_linked") or 0),
                    },
                )
            )
        if not acceptance_matrix.get("native_lineage_ready"):
            native_status = _string(lineage_source.get("status")) or "missing"
            blocker_details.append(
                _append(
                    "native_signal_evidence_lineage_missing",
                    "code_gap"
                    if native_status in {"missing", "legacy_only", "candidate_only"}
                    else "data_gap",
                    "策略运行链缺少可审计的 native signal lineage，execution audit 无法解释 signal -> claim -> trade step。",
                    (
                        "先运行 backfill_strategy_signal_evidence_native；"
                        "若策略仍缺 claim_to_trade_plan_map/trade_plan_to_dsl_map/evidence_alignment_audit，"
                        "再执行 strategy_recompile_backfill 补齐 compile-stable semantic contract。"
                    ),
                    evidence={
                        "lineage_status": native_status,
                        "native_signal_evidence_count": int(
                            lineage_source.get("native_signal_evidence_count") or 0
                        ),
                        "native_trade_step_lineage_count": int(
                            lineage_source.get("native_trade_step_lineage_count") or 0
                        ),
                        "semantic_contract_status": semantic_contract.get("status"),
                        "semantic_contract_missing_fields": list(
                            semantic_contract.get("missing_fields") or []
                        ),
                    },
                )
            )
        if not acceptance_matrix.get("fill_round_trip_ready"):
            blocker_details.append(
                _append(
                    "trade_position_fill_round_trip_incomplete",
                    "data_gap",
                    "strategy_trade_position_fills 与 strategy_trade_positions 之间仍存在 round-trip 缺口。",
                    "检查 refresh_strategy_trade_position 聚合结果，确保每个 position 至少有对应 fill 并能完成 round-trip。",
                    evidence={
                        "position_count": int(round_trip.get("position_count") or 0),
                        "fill_count": int(round_trip.get("fill_count") or 0),
                    },
                )
            )

        realized_trade_count = int(audit_summary.get("realized_trade_count") or 0)
        incomplete_position_count = int(audit_summary.get("incomplete_position_count") or 0)
        if not acceptance_matrix.get("trade_evidence_ready"):
            blocker_details.append(
                _append(
                    "realized_trade_evidence_insufficient",
                    "sample_gap",
                    "真实已平仓样本不足，execution audit 还没有形成稳定的交易证据面。",
                    (
                        "继续运行 paper incubation，直到至少形成 1 个完全闭合的 realized position，"
                        "并把 incomplete positions 清零。"
                    ),
                    owner="operations",
                    evidence={
                        "realized_trade_count": realized_trade_count,
                        "incomplete_position_count": incomplete_position_count,
                        "open_position_count": int(
                            audit_summary.get("open_position_count") or 0
                        ),
                        "raw_incomplete_position_count": int(
                            audit_summary.get("raw_incomplete_position_count") or 0
                        ),
                    },
                )
            )

        gate_status = _string(audit_summary.get("execution_audit_gate_status"))
        if not acceptance_matrix.get("hard_gate_ready"):
            gate_reasons = [
                _string(item)
                for item in list(audit_summary.get("execution_audit_gate_reasons") or [])
                if _string(item)
            ]
            if gate_status == "bootstrap_pending":
                blocker_details.append(
                    _append(
                        "bootstrap_pending",
                        "sample_gap",
                        "execution hard gate 仍处于 bootstrap 阶段，当前只有运行痕迹，没有足够的已实现交易审计样本。",
                        "继续累积第一批闭合交易，至少让 realized_trade_count > 0 后再重跑 acceptance。",
                        owner="operations",
                        evidence={
                            "realized_trade_count": realized_trade_count,
                            "open_position_count": int(
                                audit_summary.get("open_position_count") or 0
                            ),
                        },
                    )
                )
            elif gate_status == "insufficient_samples":
                blocker_details.append(
                    _append(
                        "insufficient_samples",
                        "sample_gap",
                        "已实现交易数量未达到 hard gate 最低样本门槛。",
                        "继续孵化并累积到 production hard gate 所需样本量，然后再验证 trade_expectancy / conversion 指标。",
                        owner="operations",
                        evidence={
                            "realized_trade_count": realized_trade_count,
                            "required_trade_count": int(audit_summary.get("required_trade_count") or 20),
                            "bootstrap_trade_floor": int(audit_summary.get("bootstrap_trade_floor") or 0),
                        },
                    )
                )
            elif gate_status == "bootstrap_ready":
                blocker_details.append(
                    _append(
                        "promotion_hard_gate_pending",
                        "sample_gap",
                        "已达到 bootstrap 样本门槛且执行指标通过，但尚未达到 production hard gate 所需样本量。",
                        "继续累积 production hard gate 所需的 realized trades，再申请 promotion-ready。",
                        owner="operations",
                        evidence={
                            "realized_trade_count": realized_trade_count,
                            "required_trade_count": int(audit_summary.get("required_trade_count") or 20),
                            "bootstrap_trade_floor": int(audit_summary.get("bootstrap_trade_floor") or 0),
                        },
                    )
                )
            else:
                blocker_details.append(
                    _append(
                        gate_status or "execution_audit_gate_not_passed",
                        "performance_gap",
                        "execution hard gate 指标未通过，当前策略还不能进入 promotion-ready 区间。",
                        "针对 trade_expectancy、pnl_conversion_efficiency、execution_conversion_efficiency 做专项修复，再重跑 acceptance。",
                        owner="research",
                        evidence={"gate_reasons": gate_reasons},
                    )
                )
        return blocker_details

    async def get_strategy_trade_audit_summary(self, strategy_id: str) -> dict:
        evaluate_execution_audit_gate = (
            get_execution_audit_gate_evaluator() or _fallback_execution_audit_gate
        )
        await self.backfill_trade_position_links(strategy_id)
        strategy = await self.get_strategy(strategy_id)
        strategy_type = _string((strategy or {}).get("strategy_type")) or None
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                WITH classified_positions AS (
                    SELECT
                        p.*,
                        CASE
                            WHEN LOWER(COALESCE(entry_order.source, '')) = 'backtest_bootstrap_import'
                              OR LOWER(COALESCE(exit_order.source, '')) = 'backtest_bootstrap_import'
                              OR LOWER(COALESCE(entry_order.reason, '')) LIKE 'backtest_bootstrap%'
                              OR LOWER(COALESCE(exit_order.reason, '')) LIKE 'backtest_bootstrap%'
                              OR EXISTS (
                                  SELECT 1
                                  FROM strategy_trade_position_fills f
                                  WHERE f.position_id = p.position_id
                                    AND json_valid(COALESCE(f.payload, '{}'))
                                    AND (
                                        COALESCE(json_extract(f.payload, '$.source'), '') = 'backtest_bootstrap_import'
                                        OR COALESCE(json_extract(f.payload, '$.bootstrap_source'), '') = 'backtest_to_incubation_v1'
                                    )
                              )
                            THEN 1
                            ELSE 0
                        END AS bootstrap_round_trip
                    FROM strategy_trade_positions p
                    LEFT JOIN paper_orders entry_order
                      ON CAST(entry_order.id AS TEXT) = CAST(p.entry_order_id AS TEXT)
                    LEFT JOIN paper_orders exit_order
                      ON CAST(exit_order.id AS TEXT) = CAST(p.exit_order_id AS TEXT)
                    WHERE p.strategy_id = $1
                )
                SELECT
                    COALESCE(COUNT(*), 0) AS mapped_position_count,
                    COALESCE(COUNT(*) FILTER (WHERE audit_eligible), 0) AS closed_round_trip_count,
                    COALESCE(
                        COUNT(*) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                        ),
                        0
                    ) AS realized_trade_count,
                    COALESCE(
                        COUNT(*) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                        ),
                        0
                    ) AS real_paper_round_trip_count,
                    COALESCE(
                        COUNT(*) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 1
                        ),
                        0
                    ) AS bootstrap_round_trip_count,
                    COALESCE(
                        COUNT(*) FILTER (
                            WHERE LOWER(COALESCE(status, '')) = 'open'
                        ),
                        0
                    ) AS open_position_count,
                    COALESCE(COUNT(*) FILTER (WHERE NOT audit_eligible), 0) AS incomplete_position_count,
                    COALESCE(
                        AVG(realized_return) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                        ),
                        0
                    ) AS trade_expectancy,
                    COALESCE(
                        SUM(realized_pnl) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                        )
                        / NULLIF(
                            SUM(entry_amount + entry_commission) FILTER (
                                WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                            ),
                            0
                        ),
                        0
                    ) AS pnl_conversion_efficiency,
                    COALESCE(
                        AVG(execution_conversion_efficiency) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                        ),
                        0
                    ) AS execution_conversion_efficiency,
                    COALESCE(
                        AVG(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0.0 END) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                        ),
                        0
                    ) AS execution_win_rate,
                    COALESCE(
                        AVG(CASE WHEN realized_pnl > 0 THEN realized_pnl END) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                        )
                        / NULLIF(
                            ABS(
                                AVG(CASE WHEN realized_pnl < 0 THEN realized_pnl END) FILTER (
                                    WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                                )
                            ),
                            0
                        ),
                        0
                    ) AS avg_win_loss_ratio,
                    COALESCE(
                        SUM(realized_pnl) FILTER (
                            WHERE audit_eligible AND COALESCE(bootstrap_round_trip, 0) = 0
                        ),
                        0
                    ) AS realized_pnl_total
                FROM classified_positions
                """,
                strategy_id,
            )
        payload = self._normalize_trade_audit_summary_counts(dict(row or {}))
        realized_trade_count = int(payload.get("realized_trade_count") or 0)
        closed_round_trip_count = int(
            payload.get("closed_round_trip_count")
            or payload.get("total_realized_trade_count")
            or realized_trade_count
        )
        bootstrap_round_trip_count = int(payload.get("bootstrap_round_trip_count") or 0)
        real_paper_round_trip_count = int(
            payload.get("real_paper_round_trip_count") or realized_trade_count
        )
        trade_expectancy = float(payload.get("trade_expectancy") or 0.0)
        pnl_conversion_efficiency = float(payload.get("pnl_conversion_efficiency") or 0.0)
        execution_conversion_efficiency = float(payload.get("execution_conversion_efficiency") or 0.0)
        gate_status, gate_reasons, metric_passes, hard_gate_metrics = evaluate_execution_audit_gate(
            {
                **payload,
                "strategy_type": strategy_type,
                "realized_trade_count": real_paper_round_trip_count,
                "real_paper_round_trip_count": real_paper_round_trip_count,
                "bootstrap_round_trip_count": bootstrap_round_trip_count,
                "closed_round_trip_count": closed_round_trip_count,
                "trade_expectancy": trade_expectancy,
                "pnl_conversion_efficiency": pnl_conversion_efficiency,
                "execution_conversion_efficiency": execution_conversion_efficiency,
            },
            strategy_type=strategy_type,
        )
        return {
            "approximate": False,
            "audit_grade": realized_trade_count > 0,
            "method": "position_id_round_trip_v1",
            "source_tables": [
                "paper_orders",
                "paper_trades",
                "strategy_trade_positions",
                "strategy_trade_position_fills",
            ],
            "mapped_position_count": int(payload.get("mapped_position_count") or 0),
            "strategy_type": strategy_type,
            "realized_trade_count": real_paper_round_trip_count,
            "real_paper_round_trip_count": real_paper_round_trip_count,
            "real_paper_round_trips": real_paper_round_trip_count,
            "bootstrap_round_trip_count": bootstrap_round_trip_count,
            "bootstrap_round_trips": bootstrap_round_trip_count,
            "closed_round_trip_count": closed_round_trip_count,
            "total_realized_trade_count": closed_round_trip_count,
            "incomplete_position_count": int(payload.get("incomplete_position_count") or 0),
            "raw_incomplete_position_count": int(
                payload.get("raw_incomplete_position_count") or 0
            ),
            "open_position_count": int(payload.get("open_position_count") or 0),
            "trade_expectancy": round(trade_expectancy, 6),
            "pnl_conversion_efficiency": round(pnl_conversion_efficiency, 6),
            "execution_conversion_efficiency": round(execution_conversion_efficiency, 6),
            "execution_win_rate": round(float(payload.get("execution_win_rate") or 0.0), 6),
            "avg_win_loss_ratio": round(float(payload.get("avg_win_loss_ratio") or 0.0), 6),
            "realized_pnl_total": round(float(payload.get("realized_pnl_total") or 0.0), 4),
            "audit_ready_for_hard_gate": gate_status == "passed",
            "bootstrap_gate_ready": gate_status in {"bootstrap_ready", "passed"},
            "execution_audit_gate_status": gate_status,
            "execution_audit_gate_reasons": gate_reasons,
            "hard_gate_metric_passes": metric_passes,
            "hard_gate_metrics": hard_gate_metrics,
            "bootstrap_trade_floor": int(hard_gate_metrics.get("bootstrap_trade_floor") or 0),
            "required_trade_count": int(hard_gate_metrics.get("required_trade_count") or 20),
        }

    async def get_execution_audit_verification(self, strategy_id: Optional[str] = None) -> dict:
        strategy_filter = _string(strategy_id) or None
        async with self.acquire() as conn:
            async def _table_present(table_name: str) -> bool:
                return bool(
                    await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = $1)",
                        table_name,
                    )
                )

            async def _column_present(table_name: str, column_name: str) -> bool:
                return bool(
                    await conn.fetchval(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM pragma_table_info($1)
                            WHERE name = $2
                        )
                        """,
                        table_name,
                        column_name,
                    )
                )

            async def _count_rows(table_name: str, *, column_name: Optional[str] = None) -> int:
                where_clause = "WHERE ($1 IS NULL OR strategy_id = $1)"
                if column_name:
                    where_clause += f" AND NULLIF(TRIM(COALESCE({column_name}, '')), '') IS NOT NULL"
                row = await conn.fetchrow(
                    f"""
                    SELECT COALESCE(COUNT(*), 0) AS count
                    FROM {table_name}
                    {where_clause}
                    """,
                    strategy_filter,
                )
                return int(dict(row or {}).get("count") or 0)

            table_presence = {
                table_name: await _table_present(table_name)
                for table_name in _EXECUTION_AUDIT_REQUIRED_TABLES
            }
            column_presence = {
                table_name: {
                    column_name: await _column_present(table_name, column_name)
                    for column_name in required_columns
                }
                for table_name, required_columns in _EXECUTION_AUDIT_REQUIRED_COLUMNS.items()
            }
            migration_table_present = await _table_present("market_schema_migrations")
            migration_presence = {}
            if migration_table_present:
                for migration_key in _EXECUTION_AUDIT_REQUIRED_MIGRATIONS:
                    migration_presence[migration_key] = bool(
                        await conn.fetchval(
                            """
                            SELECT EXISTS (
                                SELECT 1
                                FROM market_schema_migrations
                                WHERE migration_key = $1
                            )
                            """,
                            migration_key,
                        )
                    )
            else:
                migration_presence = {
                    migration_key: False
                    for migration_key in _EXECUTION_AUDIT_REQUIRED_MIGRATIONS
                }

            orders_total = await _count_rows("paper_orders")
            orders_signal_linked = await _count_rows("paper_orders", column_name="signal_id")
            orders_position_linked = await _count_rows("paper_orders", column_name="position_id")
            trades_total = await _count_rows("paper_trades")
            trades_signal_linked = await _count_rows("paper_trades", column_name="signal_id")
            trades_position_linked = await _count_rows("paper_trades", column_name="position_id")

            candidate_evidence_count = (
                await _count_rows("strategy_candidate_evidence")
                if table_presence["strategy_candidate_evidence"]
                else 0
            )
            signal_evidence_count = (
                await _count_rows("strategy_signal_evidence")
                if table_presence["strategy_signal_evidence"]
                else 0
            )
            signal_evidence_trade_step_column_present = bool(
                table_presence["strategy_signal_evidence"]
                and await _column_present("strategy_signal_evidence", "applied_trade_step_id")
            )
            signal_evidence_trade_step_count = (
                await _count_rows("strategy_signal_evidence", column_name="applied_trade_step_id")
                if signal_evidence_trade_step_column_present
                else 0
            )
            fill_count = (
                await _count_rows("strategy_trade_position_fills")
                if table_presence["strategy_trade_position_fills"]
                else 0
            )
            runtime_action_reason_column_present = bool(
                table_presence["strategy_signal_evidence"]
                and await _column_present("strategy_signal_evidence", "runtime_action_reason")
            )
            runtime_action_signal_count = 0
            if runtime_action_reason_column_present:
                runtime_action_signal_count = await _count_rows(
                    "strategy_signal_evidence",
                    column_name="runtime_action_reason",
                )
            signal_evidence_claim_count = 0
            signal_evidence_backfilled_count = 0
            signal_evidence_backfilled_signal_count = 0
            signal_evidence_compile_stable_count = 0
            signal_evidence_compile_stable_signal_count = 0
            signal_evidence_proxy_only_count = 0
            if table_presence["strategy_signal_evidence"]:
                row = await conn.fetchrow(
                    """
                    SELECT
                        COALESCE(
                            COUNT(*) FILTER (
                                WHERE NULLIF(TRIM(COALESCE(applied_claim_id, '')), '') IS NOT NULL
                            ),
                            0
                        ) AS claim_count,
                        COALESCE(
                            COUNT(*) FILTER (
                                WHERE COALESCE(source_type, '') = 'paper_execution_backfill'
                                   OR COALESCE(json_extract(payload, '$.backfill_mode'), '') = 'paper_execution_native_backfill_v1'
                            ),
                            0
                        ) AS backfilled_count,
                        COALESCE(
                            COUNT(DISTINCT signal_id) FILTER (
                                WHERE COALESCE(source_type, '') = 'paper_execution_backfill'
                                   OR COALESCE(json_extract(payload, '$.backfill_mode'), '') = 'paper_execution_native_backfill_v1'
                            ),
                            0
                        ) AS backfilled_signal_count,
                        COALESCE(
                            COUNT(*) FILTER (
                                WHERE NOT (
                                    COALESCE(source_type, '') = 'paper_execution_backfill'
                                    OR COALESCE(json_extract(payload, '$.backfill_mode'), '') = 'paper_execution_native_backfill_v1'
                                )
                            ),
                            0
                        ) AS compile_stable_count,
                        COALESCE(
                            COUNT(DISTINCT signal_id) FILTER (
                                WHERE NOT (
                                    COALESCE(source_type, '') = 'paper_execution_backfill'
                                    OR COALESCE(json_extract(payload, '$.backfill_mode'), '') = 'paper_execution_native_backfill_v1'
                                )
                            ),
                            0
                        ) AS compile_stable_signal_count,
                        COALESCE(
                            COUNT(*) FILTER (WHERE COALESCE(proxy_only, FALSE)),
                            0
                        ) AS proxy_only_count
                    FROM strategy_signal_evidence
                    WHERE ($1 IS NULL OR strategy_id = $1)
                    """,
                    strategy_filter,
                )
                row_payload = dict(row or {})
                signal_evidence_claim_count = int(row_payload.get("claim_count") or 0)
                signal_evidence_backfilled_count = int(
                    row_payload.get("backfilled_count") or 0
                )
                signal_evidence_backfilled_signal_count = int(
                    row_payload.get("backfilled_signal_count") or 0
                )
                signal_evidence_compile_stable_count = int(
                    row_payload.get("compile_stable_count") or 0
                )
                signal_evidence_compile_stable_signal_count = int(
                    row_payload.get("compile_stable_signal_count") or 0
                )
                signal_evidence_proxy_only_count = int(
                    row_payload.get("proxy_only_count") or 0
                )
            legacy_evidence_count = 0
            legacy_table_present = await _table_present("strategy_factory_task_evidence")
            if legacy_table_present and strategy_filter:
                row = await conn.fetchrow(
                    """
                    SELECT COALESCE(COUNT(*), 0) AS count
                    FROM strategy_factory_task_evidence
                    WHERE COALESCE(json_extract(evidence_payload, '$.strategy_id'), '') = $1
                    """,
                    strategy_filter,
                )
                legacy_evidence_count = int(dict(row or {}).get("count") or 0)
            position_count = 0
            position_status_counts: dict[str, int] = {}
            if table_presence["strategy_trade_positions"]:
                row = await conn.fetchrow(
                    """
                    SELECT COALESCE(COUNT(*), 0) AS count
                    FROM strategy_trade_positions
                    WHERE ($1 IS NULL OR strategy_id = $1)
                    """,
                    strategy_filter,
                )
                position_count = int(dict(row or {}).get("count") or 0)
                status_rows = await conn.fetch(
                    """
                    SELECT COALESCE(status, 'unknown') AS status, COUNT(*) AS count
                    FROM strategy_trade_positions
                    WHERE ($1 IS NULL OR strategy_id = $1)
                    GROUP BY COALESCE(status, 'unknown')
                    ORDER BY status
                    """,
                    strategy_filter,
                )
                position_status_counts = {
                    str(dict(item or {}).get("status") or "unknown"): int(
                        dict(item or {}).get("count") or 0
                    )
                    for item in status_rows
                }
        semantic_contract = (
            self._execution_lineage_semantic_contract_status(
                await self.get_strategy(strategy_filter)
            )
            if strategy_filter
            else None
        )

        schema_ok = all(table_presence.values()) and all(
            present
            for table_columns in column_presence.values()
            for present in table_columns.values()
        )
        migrations_ok = migration_table_present and all(migration_presence.values())

        audit_summary = None
        if strategy_filter and schema_ok:
            try:
                audit_summary = await self.get_strategy_trade_audit_summary(strategy_filter)
            except Exception as exc:
                logger.warning(
                    "execution audit verification summary failed for %s: %s",
                    strategy_filter,
                    exc,
                )
        if strategy_filter and schema_ok and table_presence["strategy_trade_positions"]:
            position_rows = await self.list_strategy_trade_positions(
                strategy_id=strategy_filter,
                limit=5000,
            )
            fill_rows = (
                await self.list_strategy_trade_position_fills(
                    strategy_id=strategy_filter,
                    limit=5000,
                )
                if table_presence["strategy_trade_position_fills"]
                else []
            )
            position_count = len(position_rows)
            fill_count = len(fill_rows)
            position_status_counts = {}
            for row in position_rows:
                status = str((row or {}).get("status") or "unknown")
                position_status_counts[status] = position_status_counts.get(status, 0) + 1

        recommendations: list[str] = []
        missing_tables = [
            table_name for table_name, present in table_presence.items() if not present
        ]
        if missing_tables:
            recommendations.append(
                "missing required execution-audit tables: " + ", ".join(missing_tables)
            )
        missing_columns = [
            f"{table_name}.{column_name}"
            for table_name, columns in column_presence.items()
            for column_name, present in columns.items()
            if not present
        ]
        if missing_columns:
            recommendations.append(
                "missing paper trading linkage columns: " + ", ".join(missing_columns)
            )
        missing_migrations = [
            migration_key
            for migration_key, present in migration_presence.items()
            if not present
        ]
        if missing_migrations:
            recommendations.append(
                "missing migration/backfill markers: " + ", ".join(missing_migrations)
            )
        if orders_total > 0 and orders_position_linked < orders_total:
            recommendations.append(
                "paper_orders position_id coverage is incomplete; verify phase_5/6 backfill on production data"
            )
        if trades_total > 0 and trades_position_linked < trades_total:
            recommendations.append(
                "paper_trades position_id coverage is incomplete; rerun round-trip linkage/backfill verification"
            )
        if audit_summary and int(audit_summary.get("incomplete_position_count") or 0) > 0:
            recommendations.append(
                "round-trip aggregation still has incomplete positions; inspect refresh_strategy_trade_position/backfill outputs"
            )
        lineage_status = "missing"
        if signal_evidence_trade_step_count > 0:
            lineage_status = (
                "native_compile_stable"
                if signal_evidence_compile_stable_signal_count > 0
                else "native_backfilled"
                if signal_evidence_backfilled_signal_count > 0
                else "native_ready"
            )
        elif signal_evidence_count > 0:
            lineage_status = "native_unmapped"
        elif candidate_evidence_count > 0:
            lineage_status = "candidate_only"
        elif legacy_evidence_count > 0:
            lineage_status = "legacy_only"

        if lineage_status == "native_backfilled":
            recommendations.append(
                "native lineage currently depends on paper_execution backfill; run strategy_recompile_backfill for compile-stable claim/trade-step mapping"
            )
        elif lineage_status == "native_unmapped":
            recommendations.append(
                "strategy_signal_evidence exists without trade-step lineage; repair claim_to_trade_plan_map/trade_plan_to_dsl_map and rerun native evidence backfill"
            )
        elif lineage_status in {"missing", "legacy_only", "candidate_only"} and strategy_filter:
            recommendations.append(
                "backfill strategy_signal_evidence from paper execution lineage, then re-run execution_audit_acceptance"
            )
        if semantic_contract and semantic_contract.get("missing_fields"):
            recommendations.append(
                "compile-stable semantic contract fields are incomplete: "
                + ", ".join(list(semantic_contract.get("missing_fields") or []))
            )

        if missing_tables or missing_columns:
            status = "missing_schema"
        elif missing_migrations:
            status = "pending_migration_verification"
        elif recommendations:
            status = "needs_attention"
        else:
            status = "ok"

        def _ratio(numerator: int, denominator: int) -> Optional[float]:
            if denominator <= 0:
                return None
            return round(float(numerator) / float(denominator), 6)

        verification_result = {
            "status": status,
            "strategy_id": strategy_filter,
            "method": "execution_audit_verification_v1",
            "schema": {
                "required_tables": {
                    table_name: {"present": present}
                    for table_name, present in table_presence.items()
                },
                "required_columns": {
                    table_name: {
                        column_name: {"present": present}
                        for column_name, present in columns.items()
                    }
                    for table_name, columns in column_presence.items()
                },
                "all_required_tables_present": all(table_presence.values()),
                "all_required_columns_present": all(
                    present
                    for table_columns in column_presence.values()
                    for present in table_columns.values()
                ),
            },
            "migrations": {
                "tracking_table_present": migration_table_present,
                "required_keys": {
                    migration_key: {"applied": present}
                    for migration_key, present in migration_presence.items()
                },
                "all_required_keys_applied": migrations_ok,
            },
            "coverage": {
                "paper_orders": {
                    "total": orders_total,
                    "signal_id_linked": orders_signal_linked,
                    "position_id_linked": orders_position_linked,
                    "signal_id_ratio": _ratio(orders_signal_linked, orders_total),
                    "position_id_ratio": _ratio(orders_position_linked, orders_total),
                },
                "paper_trades": {
                    "total": trades_total,
                    "signal_id_linked": trades_signal_linked,
                    "position_id_linked": trades_position_linked,
                    "signal_id_ratio": _ratio(trades_signal_linked, trades_total),
                    "position_id_ratio": _ratio(trades_position_linked, trades_total),
                },
                "strategy_candidate_evidence_count": candidate_evidence_count,
                "strategy_signal_evidence_count": signal_evidence_count,
                "strategy_signal_step_lineage_count": signal_evidence_trade_step_count,
                "strategy_signal_claim_lineage_count": signal_evidence_claim_count,
                "strategy_signal_backfilled_count": signal_evidence_backfilled_count,
                "strategy_signal_backfilled_signal_count": signal_evidence_backfilled_signal_count,
                "strategy_signal_compile_stable_count": signal_evidence_compile_stable_count,
                "strategy_signal_compile_stable_signal_count": signal_evidence_compile_stable_signal_count,
                "strategy_signal_proxy_only_count": signal_evidence_proxy_only_count,
                "runtime_action_signal_count": runtime_action_signal_count,
            },
            "lineage_source": {
                "status": lineage_status,
                "native_candidate_evidence_count": candidate_evidence_count,
                "native_signal_evidence_count": signal_evidence_count,
                "native_trade_step_lineage_count": signal_evidence_trade_step_count,
                "native_claim_lineage_count": signal_evidence_claim_count,
                "native_backfilled_count": signal_evidence_backfilled_count,
                "native_backfilled_signal_count": signal_evidence_backfilled_signal_count,
                "native_compile_stable_count": signal_evidence_compile_stable_count,
                "native_compile_stable_signal_count": signal_evidence_compile_stable_signal_count,
                "native_proxy_only_count": signal_evidence_proxy_only_count,
                "runtime_action_signal_count": runtime_action_signal_count,
                "legacy_evidence_count": legacy_evidence_count,
                "semantic_contract": semantic_contract,
            },
            "trade_round_trip": {
                "position_count": position_count,
                "fill_count": fill_count,
                "position_status_counts": position_status_counts,
                "closed_round_trip_count": int(
                    dict(audit_summary or {}).get("closed_round_trip_count") or 0
                ),
                "real_paper_round_trip_count": int(
                    dict(audit_summary or {}).get("real_paper_round_trip_count")
                    or dict(audit_summary or {}).get("realized_trade_count")
                    or 0
                ),
                "bootstrap_round_trip_count": int(
                    dict(audit_summary or {}).get("bootstrap_round_trip_count") or 0
                ),
                "audit_summary": audit_summary,
            },
            "recommendations": recommendations,
        }
        if strategy_filter and hasattr(self, "get_latest_execution_audit_snapshot"):
            snapshot = await self.get_latest_execution_audit_snapshot(strategy_filter)
            if hasattr(self, "upsert_execution_audit_snapshot"):
                snapshot_builder = get_execution_audit_snapshot_builder()
                try:
                    if callable(snapshot_builder):
                        persisted_snapshot = await self.upsert_execution_audit_snapshot(
                            snapshot_builder(
                                strategy_id=strategy_filter,
                                verification=verification_result,
                                acceptance=dict((snapshot or {}).get("acceptance") or {}),
                                audit_summary=audit_summary,
                                verdict_status=_string(
                                    dict(audit_summary or {}).get("execution_audit_gate_status")
                                ) or "missing",
                                verdict_reasons=list(
                                    dict(audit_summary or {}).get("execution_audit_gate_reasons")
                                    or []
                                ),
                                execution_hard_gate_passed=bool(
                                    dict(audit_summary or {}).get("audit_ready_for_hard_gate")
                                ),
                                as_of=date.today().isoformat(),
                                factory_run_id=_string((snapshot or {}).get("factory_run_id")) or None,
                                correlation_id=_string((snapshot or {}).get("correlation_id"))
                                or strategy_filter,
                                trace_id=_string((snapshot or {}).get("trace_id")) or None,
                                submission_lane=_string((snapshot or {}).get("submission_lane"))
                                or None,
                                parent_task_run_id=_string(
                                    (snapshot or {}).get("parent_task_run_id")
                                ) or None,
                                source_action="execution_audit_verification",
                                metadata={
                                    "verification_status": status,
                                    "recommendation_count": len(recommendations),
                                    "lineage_status": lineage_status,
                                },
                            )
                        )
                        if persisted_snapshot:
                            snapshot = persisted_snapshot
                except Exception as exc:
                    logger.warning(
                        "execution audit verification snapshot persist failed for %s: %s",
                        strategy_filter,
                        exc,
                    )
            if snapshot:
                verification_result["snapshot"] = snapshot
                verification_result["as_of"] = snapshot.get("as_of")
                verification_result["correlation_id"] = snapshot.get("correlation_id")
                verification_result["factory_run_id"] = snapshot.get("factory_run_id")
                verification_result["execution_audit_gate_status"] = (
                    dict(snapshot.get("verdict") or {}).get("status")
                    or snapshot.get("verdict_status")
                )
                verification_result["execution_audit_gate_reasons"] = list(
                    dict(snapshot.get("verdict") or {}).get("reasons")
                    or snapshot.get("verdict_reasons")
                    or []
                )
                verification_result["execution_hard_gate_passed"] = bool(
                    dict(snapshot.get("verdict") or {}).get("hard_gate_passed")
                    if dict(snapshot.get("verdict") or {}).get("hard_gate_passed") is not None
                    else snapshot.get("execution_hard_gate_passed")
                )
        return verification_result
