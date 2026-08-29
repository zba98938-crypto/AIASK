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


class _SignalEvidenceMixin:
    async def backfill_strategy_signal_evidence_native(
        self,
        strategy_id: Optional[str] = None,
        *,
        limit: int = 5000,
    ) -> dict:
        strategy_filter = _string(strategy_id) or None
        limit_value = max(1, min(int(limit or 5000), 10000))
        save_signal_evidence = getattr(self, "save_strategy_signal_evidence", None)
        if not callable(save_signal_evidence):
            return {
                "strategy_id": strategy_filter,
                "status": "unsupported",
                "reason": "save_strategy_signal_evidence_missing",
                "saved_signal_count": 0,
                "saved_row_count": 0,
            }

        if strategy_filter:
            orders = await self.list_strategy_paper_orders(strategy_filter, limit=limit_value)
            trades = await self.list_strategy_paper_trades(strategy_filter, limit=limit_value)
            existing_rows = await self.list_strategy_signal_evidence(
                strategy_id=strategy_filter,
                limit=limit_value,
            )
        else:
            async with self.acquire() as conn:
                order_rows = await conn.fetch(
                    """
                    SELECT *
                    FROM paper_orders
                    WHERE NULLIF(TRIM(COALESCE(signal_id, '')), '') IS NOT NULL
                    ORDER BY COALESCE(filled_at, updated_at, created_at) DESC
                    LIMIT $1
                    """,
                    limit_value,
                )
                trade_rows = await conn.fetch(
                    """
                    SELECT *
                    FROM paper_trades
                    WHERE NULLIF(TRIM(COALESCE(signal_id, '')), '') IS NOT NULL
                    ORDER BY trade_time DESC, created_at DESC
                    LIMIT $1
                    """,
                    limit_value,
                )
                signal_rows = await conn.fetch(
                    """
                    SELECT signal_id
                    FROM strategy_signal_evidence
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    limit_value,
                )
            orders = [dict(row) for row in order_rows]
            trades = [dict(row) for row in trade_rows]
            existing_rows = [dict(row) for row in signal_rows]

        existing_signal_ids = {
            _string(row.get("signal_id"))
            for row in list(existing_rows or [])
            if _string(row.get("signal_id"))
        }
        initial_existing_signal_count = len(existing_signal_ids)
        trades_by_signal_id: dict[str, dict] = {}
        trades_by_order_id: dict[str, dict] = {}
        for trade in list(trades or []):
            signal_id = _string(trade.get("signal_id"))
            order_id = _string(trade.get("source_order_id"))
            if signal_id and signal_id not in trades_by_signal_id:
                trades_by_signal_id[signal_id] = dict(trade)
            if order_id and order_id not in trades_by_order_id:
                trades_by_order_id[order_id] = dict(trade)

        build_signal_evidence_records = get_signal_evidence_builder()

        strategy_cache: dict[str, Optional[dict]] = {}
        strategy_semantic_status_cache: dict[str, dict] = {}
        semantic_gap_strategy_ids: set[str] = set()
        compile_stable_signal_count = 0
        native_generated_unmapped_signal_count = 0
        proxy_backfilled_signal_count = 0
        compile_stable_row_count = 0
        native_generated_unmapped_row_count = 0
        proxy_backfilled_row_count = 0
        skipped_existing_signal_count = 0
        skipped_missing_strategy_count = 0
        saved_signal_ids: set[str] = set()
        saved_row_count = 0

        def _normalized_phase(direction_value: str, trade_type_value: str) -> str:
            token = _string(direction_value or trade_type_value).lower()
            if token in {"sell", "short", "exit", "close", "reduce"}:
                return "exit"
            return "entry"

        def _phase_direction_token(phase: str) -> str:
            return "down" if phase == "exit" else "up"

        def _resolve_semantic_phase_lineage(strategy_payload: dict, phase: str) -> tuple[Optional[str], Optional[str], bool]:
            params = dict(strategy_payload.get("params") or {})
            claim_map = dict(
                strategy_payload.get("claim_to_trade_plan_map")
                or params.get("claim_to_trade_plan_map")
                or {}
            )
            trade_map = dict(
                strategy_payload.get("trade_plan_to_dsl_map")
                or params.get("trade_plan_to_dsl_map")
                or {}
            )
            trade_step_sections = dict(trade_map.get("trade_step_to_dsl_sections") or {})
            trade_step_to_claim_ids = dict(claim_map.get("trade_step_to_claim_ids") or {})
            claim_to_trade_step_ids = dict(claim_map.get("claim_to_trade_step_ids") or {})

            matched_trade_steps = [
                step_id
                for step_id, sections in trade_step_sections.items()
                if phase in {
                    _string(section).lower()
                    for section in list(sections or [])
                    if _string(section)
                }
            ]
            precise_mapping = bool(matched_trade_steps)
            matched_trade_steps = sorted(
                step_id for step_id in matched_trade_steps if _string(step_id)
            )
            if matched_trade_steps:
                applied_trade_step_id = matched_trade_steps[0]
            else:
                applied_trade_step_id = (
                    "exit_step_backfill" if phase == "exit" else "entry_step_backfill"
                )

            claim_candidates = [
                _string(item)
                for item in list(trade_step_to_claim_ids.get(applied_trade_step_id) or [])
                if _string(item)
            ]
            if not claim_candidates and precise_mapping:
                claim_candidates = sorted(
                    claim_id
                    for claim_id, step_ids in claim_to_trade_step_ids.items()
                    if applied_trade_step_id
                    in {
                        _string(step_id)
                        for step_id in list(step_ids or [])
                        if _string(step_id)
                    }
                )
            if claim_candidates:
                applied_claim_id = claim_candidates[0]
            else:
                applied_claim_id = (
                    "legacy_claim_exit" if phase == "exit" else "legacy_claim_entry"
                )
            precise_mapping = precise_mapping and bool(claim_candidates)
            return applied_trade_step_id, applied_claim_id, precise_mapping

        for order in list(orders or []):
            order_payload = dict(order or {})
            signal_id = _string(order_payload.get("signal_id"))
            if not signal_id:
                continue
            if signal_id in existing_signal_ids:
                skipped_existing_signal_count += 1
                continue

            trade_payload = dict(
                trades_by_order_id.get(_string(order_payload.get("id")))
                or trades_by_signal_id.get(signal_id)
                or {}
            )
            resolved_strategy_id = _string(
                order_payload.get("strategy_id")
                or trade_payload.get("strategy_id")
                or strategy_filter
            )
            if not resolved_strategy_id:
                skipped_missing_strategy_count += 1
                continue

            if resolved_strategy_id not in strategy_cache:
                strategy_cache[resolved_strategy_id] = await self.get_strategy(
                    resolved_strategy_id
                )
                strategy_semantic_status_cache[resolved_strategy_id] = (
                    self._execution_lineage_semantic_contract_status(
                        strategy_cache[resolved_strategy_id]
                    )
                )
            strategy_payload = dict(strategy_cache.get(resolved_strategy_id) or {})
            if not strategy_payload:
                skipped_missing_strategy_count += 1
                continue
            semantic_status = dict(
                strategy_semantic_status_cache.get(resolved_strategy_id) or {}
            )
            if not semantic_status.get("compile_stable_ready"):
                semantic_gap_strategy_ids.add(resolved_strategy_id)

            signal_date = (
                order_payload.get("signal_date")
                or trade_payload.get("trade_time")
                or order_payload.get("filled_at")
                or order_payload.get("created_at")
            )
            signal_ts = (
                trade_payload.get("trade_time")
                or order_payload.get("filled_at")
                or order_payload.get("updated_at")
                or order_payload.get("created_at")
            )
            code = _string(order_payload.get("code") or trade_payload.get("stock_code"))
            position_id = _string(
                order_payload.get("position_id") or trade_payload.get("position_id")
            ) or None
            account_id = _string(
                order_payload.get("account_id") or trade_payload.get("account_id")
            ) or None
            phase = _normalized_phase(
                order_payload.get("direction"),
                trade_payload.get("trade_type"),
            )
            runtime_action_reason = (
                "exit_execution_backfill"
                if phase == "exit"
                else "entry_execution_backfill"
            )

            evidence_records: list[dict] = []
            build_mode = "paper_execution_backfill"
            if callable(build_signal_evidence_records):
                try:
                    evidence_records = list(
                        build_signal_evidence_records(
                            strategy_payload,
                            signal_id=signal_id,
                            position_id=position_id,
                            account_id=account_id,
                            signal_date=signal_date,
                            code=code,
                        )
                        or []
                    )
                except Exception as exc:
                    logger.warning(
                        "native signal evidence generation failed for %s/%s: %s",
                        resolved_strategy_id,
                        signal_id,
                        exc,
                    )
                    evidence_records = []

            has_precise_mapping = any(
                _string(item.get("applied_trade_step_id"))
                or _string(item.get("applied_claim_id"))
                for item in evidence_records
            )
            if evidence_records and has_precise_mapping:
                build_mode = "compile_stable_native"
            elif evidence_records:
                build_mode = "native_generated_unmapped"
            else:
                applied_trade_step_id, applied_claim_id, precise_mapping = (
                    _resolve_semantic_phase_lineage(strategy_payload, phase)
                )
                build_mode = (
                    "paper_execution_backfill_mapped"
                    if precise_mapping
                    else "paper_execution_backfill"
                )
                evidence_id = (
                    f"paper_execution_{phase}_{_string(order_payload.get('id')) or signal_id}"
                )
                evidence_records = [
                    {
                        "id": (
                            f"{signal_id}:{evidence_id}:{applied_claim_id or 'unclaimed'}:"
                            f"{applied_trade_step_id or 'unmapped_step'}"
                        ),
                        "strategy_id": resolved_strategy_id,
                        "signal_id": signal_id,
                        "position_id": position_id,
                        "account_id": account_id,
                        "signal_date": signal_date,
                        "signal_ts": signal_ts,
                        "code": code or _string(trade_payload.get("stock_code")) or None,
                        "symbol": code or _string(trade_payload.get("stock_code")) or None,
                        "candidate_artifact_id": (
                            strategy_payload.get("candidate_artifact_id")
                            or dict(strategy_payload.get("params") or {}).get(
                                "candidate_artifact_id"
                            )
                        ),
                        "experiment_id": (
                            strategy_payload.get("experiment_id")
                            or dict(strategy_payload.get("params") or {}).get(
                                "experiment_id"
                            )
                        ),
                        "evidence_id": evidence_id,
                        "applied_claim_id": applied_claim_id,
                        "applied_trade_step_id": applied_trade_step_id,
                        "source_type": "paper_execution_backfill",
                        "direction": _phase_direction_token(phase),
                        "horizon_days": None,
                        "raw_confidence": None,
                        "calibrated_confidence": None,
                        "proxy_only": True,
                        "runtime_action_reason": runtime_action_reason,
                        "runtime_action_source": "paper_orders/paper_trades",
                        "evidence_payload": {
                            "backfill_mode": "paper_execution_native_backfill_v1",
                            "build_mode": build_mode,
                            "strategy_id": resolved_strategy_id,
                            "signal_id": signal_id,
                            "position_id": position_id,
                            "account_id": account_id,
                            "signal_ts": signal_ts,
                            "signal_date": signal_date,
                            "code": code or _string(trade_payload.get("stock_code")) or None,
                            "applied_claim_id": applied_claim_id,
                            "applied_trade_step_id": applied_trade_step_id,
                            "runtime_action_reason": runtime_action_reason,
                            "runtime_action_source": "paper_orders/paper_trades",
                            "semantic_contract_status": semantic_status.get("status"),
                            "semantic_contract_missing_fields": list(
                                semantic_status.get("missing_fields") or []
                            ),
                            "source_order_id": _string(order_payload.get("id")) or None,
                            "source_trade_id": _string(trade_payload.get("id")) or None,
                            "source_trade_type": _string(trade_payload.get("trade_type"))
                            or _string(order_payload.get("direction"))
                            or None,
                            "source_reason": _string(
                                trade_payload.get("reason") or order_payload.get("reason")
                            )
                            or None,
                        },
                    }
                ]

            for evidence in evidence_records:
                payload = {
                    "id": evidence.get("id"),
                    "signal_id": signal_id,
                    "strategy_id": resolved_strategy_id,
                    "signal_date": signal_date,
                    "signal_ts": evidence.get("signal_ts") or signal_ts,
                    "code": evidence.get("code") or evidence.get("symbol") or code,
                    "candidate_artifact_id": evidence.get("candidate_artifact_id"),
                    "experiment_id": evidence.get("experiment_id"),
                    "evidence_id": evidence.get("evidence_id"),
                    "applied_claim_id": evidence.get("applied_claim_id"),
                    "applied_trade_step_id": evidence.get("applied_trade_step_id"),
                    "source_type": evidence.get("source_type") or evidence.get("evidence_type"),
                    "direction": evidence.get("direction")
                    or _phase_direction_token(phase),
                    "horizon_days": evidence.get("horizon_days"),
                    "raw_confidence": evidence.get("raw_confidence"),
                    "calibrated_confidence": evidence.get("calibrated_confidence"),
                    "proxy_only": (
                        bool(evidence.get("proxy_only"))
                        if build_mode == "compile_stable_native"
                        else True
                    ),
                    "doc_uid": evidence.get("doc_uid"),
                    "headline_label_id": evidence.get("headline_label_id"),
                    "runtime_action_reason": evidence.get("runtime_action_reason")
                    or runtime_action_reason,
                    "runtime_action_source": evidence.get("runtime_action_source")
                    or (
                        "paper_orders/paper_trades"
                        if build_mode.startswith("paper_execution_backfill")
                        else "semantic_contract.build_signal_evidence_records"
                    ),
                    "payload": {
                        **dict(
                            evidence.get("payload")
                            or evidence.get("evidence_payload")
                            or {}
                        ),
                        "backfill_mode": "paper_execution_native_backfill_v1",
                        "build_mode": build_mode,
                        "signal_id": signal_id,
                        "strategy_id": resolved_strategy_id,
                        "position_id": position_id,
                        "account_id": account_id,
                        "source_order_id": _string(order_payload.get("id")) or None,
                        "source_trade_id": _string(trade_payload.get("id")) or None,
                        "source_trade_type": _string(trade_payload.get("trade_type"))
                        or _string(order_payload.get("direction"))
                        or None,
                        "semantic_contract_status": semantic_status.get("status"),
                        "semantic_contract_missing_fields": list(
                            semantic_status.get("missing_fields") or []
                        ),
                    },
                }
                await save_signal_evidence(payload)
                saved_row_count += 1

            existing_signal_ids.add(signal_id)
            saved_signal_ids.add(signal_id)
            if build_mode == "compile_stable_native":
                compile_stable_signal_count += 1
                compile_stable_row_count += len(evidence_records)
            elif build_mode == "native_generated_unmapped":
                native_generated_unmapped_signal_count += 1
                native_generated_unmapped_row_count += len(evidence_records)
            else:
                proxy_backfilled_signal_count += 1
                proxy_backfilled_row_count += len(evidence_records)

        status = (
            "ok"
            if saved_signal_ids
            else "no_op"
            if skipped_existing_signal_count or initial_existing_signal_count
            else "empty"
        )
        return {
            "strategy_id": strategy_filter,
            "status": status,
            "method": "strategy_signal_evidence_native_backfill_v1",
            "scanned_order_count": len(list(orders or [])),
            "scanned_trade_count": len(list(trades or [])),
            "initial_existing_signal_count": initial_existing_signal_count,
            "saved_signal_count": len(saved_signal_ids),
            "saved_row_count": saved_row_count,
            "compile_stable_signal_count": compile_stable_signal_count,
            "compile_stable_row_count": compile_stable_row_count,
            "native_generated_unmapped_signal_count": native_generated_unmapped_signal_count,
            "native_generated_unmapped_row_count": native_generated_unmapped_row_count,
            "proxy_backfilled_signal_count": proxy_backfilled_signal_count,
            "proxy_backfilled_row_count": proxy_backfilled_row_count,
            "skipped_existing_signal_count": skipped_existing_signal_count,
            "skipped_missing_strategy_count": skipped_missing_strategy_count,
            "semantic_contract_gap_strategy_ids": sorted(semantic_gap_strategy_ids),
        }
