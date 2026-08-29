from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from strategy_factory.api.semantic_contract import build_signal_evidence_records
from strategy_factory.api.constants import (
    BACKTEST_TYPE_THRESHOLDS,
    PROVISIONAL_PASS_THRESHOLDS,
)

from .backtest import BacktestEngine, StrategyRegistry
from .incubation import (
    _build_position_id,
    _parse_datetime,
    _resolve_strategy_target_codes,
    _runtime_action_lineage,
    get_strategy_incubation_service,
)
from .signal_tracker import _build_signal_tracking_artifacts
from .trade_audit_writer import record_trade_fill_from_order_and_trade

from .acceptance_helpers import (
    _RoundTrip,
    _RoundTripSelection,
    _apply_failed_metrics_family_hardening,
    _bootstrap_lineage_token,
    _bootstrap_trade_floor,
    _build_bootstrap_lineage_fallback,
    _coerce_trade_date,
    _coerce_trade_ts,
    _dedupe_strings,
    _group_backtest_round_trips,
    _is_bootstrap_proxy_lineage_id,
    _merge_bootstrap_lineage,
    _parse_affected_rows,
    _round_trip_selection,
    _safe_float,
    _safe_int,
    _select_bootstrap_round_trips,
    _strategy_runtime_params,
    _sync_runtime_params_container,
    build_failed_metrics_filter_patch,
    summarize_code_performance,
)

class _BootstrapMixin:
    async def _position_exists(self, db, position_id: str) -> bool:
        async with db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT 1 AS present FROM strategy_trade_positions WHERE position_id = $1 LIMIT 1",
                position_id,
            )
        return bool(row)

    async def clear_bootstrap_imports(self, db, strategy_id: str) -> dict[str, Any]:
        async with db.acquire() as conn:
            position_rows = await conn.fetch(
                """
                SELECT DISTINCT position_id
                FROM strategy_trade_positions
                WHERE strategy_id = $1
                  AND position_id LIKE 'btpos_%'
                ORDER BY position_id
                """,
                strategy_id,
            )
            position_ids = [
                str((row or {}).get("position_id") or "").strip()
                for row in list(position_rows or [])
                if str((row or {}).get("position_id") or "").strip()
            ]
            deleted: dict[str, int] = {}
            if position_ids:
                deleted["strategy_trade_position_fills"] = _parse_affected_rows(
                    await conn.execute(
                        """
                        DELETE FROM strategy_trade_position_fills
                        WHERE strategy_id = $1
                          AND position_id IN ($2)
                        """,
                        strategy_id,
                        position_ids,
                    )
                )
                deleted["strategy_trade_positions"] = _parse_affected_rows(
                    await conn.execute(
                        """
                        DELETE FROM strategy_trade_positions
                        WHERE strategy_id = $1
                          AND position_id IN ($2)
                        """,
                        strategy_id,
                        position_ids,
                    )
                )
                deleted["paper_trades"] = _parse_affected_rows(
                    await conn.execute(
                        """
                        DELETE FROM paper_trades
                        WHERE strategy_id = $1
                          AND position_id IN ($2)
                        """,
                        strategy_id,
                        position_ids,
                    )
                )
                deleted["paper_orders"] = _parse_affected_rows(
                    await conn.execute(
                        """
                        DELETE FROM paper_orders
                        WHERE strategy_id = $1
                          AND position_id IN ($2)
                        """,
                        strategy_id,
                        position_ids,
                    )
                )
            deleted["strategy_signal_evidence"] = _parse_affected_rows(
                await conn.execute(
                    """
                    DELETE FROM strategy_signal_evidence
                    WHERE strategy_id = $1
                      AND (
                        signal_id LIKE 'btsig_%'
                        OR source_type IN ('backtest_bootstrap_entry', 'backtest_bootstrap_exit')
                        OR COALESCE(json_extract(payload, '$.bootstrap_source'), '') = 'backtest_to_incubation_v1'
                      )
                    """,
                    strategy_id,
                )
            )
        return {
            "strategy_id": strategy_id,
            "position_ids": position_ids,
            "deleted": deleted,
        }

    async def _save_bootstrap_signal_evidence(
        self,
        db,
        strategy: dict,
        *,
        signal_id: str,
        position_id: str,
        account_id: str,
        signal_date: date,
        code: str,
        backtest_id: str,
        source_type: str,
        trade_payload: dict[str, Any],
        action_reason: Optional[str] = None,
        selection_payload: Optional[dict[str, Any]] = None,
    ) -> None:
        save_method = getattr(db, "save_strategy_signal_evidence", None)
        if not callable(save_method):
            return
        if source_type == "backtest_bootstrap_entry":
            generated_evidences = list(
                build_signal_evidence_records(
                    strategy,
                    signal_id=signal_id,
                    position_id=position_id,
                    account_id=account_id,
                    signal_date=signal_date,
                    code=code,
                )
            )
            if not generated_evidences:
                lineage = _merge_bootstrap_lineage(
                    strategy,
                    code=code,
                    phase="entry",
                    action_reason="bootstrap_backtest_entry",
                )
                await save_method(
                    {
                        "id": f"{signal_id}:backtest_bootstrap_entry",
                        "signal_id": signal_id,
                        "strategy_id": strategy.get("id"),
                        "signal_date": signal_date,
                        "signal_ts": _coerce_trade_ts(signal_date),
                        "code": code,
                        "evidence_id": "backtest_bootstrap_entry",
                        "applied_claim_id": lineage.get("applied_claim_id"),
                        "applied_trade_step_id": lineage.get("applied_trade_step_id"),
                        "source_type": source_type,
                        "direction": "up",
                        "runtime_action_reason": lineage.get("runtime_action_reason"),
                        "runtime_action_source": lineage.get("runtime_action_source"),
                        "payload": {
                            "bootstrap_source": "backtest_to_incubation_v1",
                            "backtest_id": backtest_id,
                            "trade_payload": dict(trade_payload or {}),
                            "bootstrap_selection": dict(selection_payload or {}),
                            "lineage": lineage,
                            "synthetic_bootstrap_lineage": True,
                        },
                    }
                )
                return
            for evidence in generated_evidences:
                lineage = _merge_bootstrap_lineage(
                    strategy,
                    code=code,
                    phase="entry",
                    lineage={
                        "applied_claim_id": evidence.get("applied_claim_id"),
                        "applied_trade_step_id": evidence.get("applied_trade_step_id"),
                    },
                    action_reason="bootstrap_backtest_entry",
                )
                evidence_payload = dict(evidence.get("evidence_payload") or evidence)
                evidence_payload["bootstrap_source"] = "backtest_to_incubation_v1"
                evidence_payload["backtest_id"] = backtest_id
                evidence_payload["trade_payload"] = dict(trade_payload or {})
                evidence_payload["lineage"] = dict(lineage)
                evidence_payload["synthetic_bootstrap_lineage"] = bool(
                    lineage.get("fallback_applied")
                )
                if selection_payload:
                    evidence_payload["bootstrap_selection"] = dict(selection_payload)
                await save_method(
                    {
                        "id": f"{signal_id}:{evidence.get('evidence_id')}:bootstrap_entry",
                        "signal_id": signal_id,
                        "strategy_id": strategy.get("id"),
                        "signal_date": signal_date,
                        "signal_ts": _coerce_trade_ts(signal_date),
                        "code": code,
                        "candidate_artifact_id": evidence.get("candidate_artifact_id"),
                        "experiment_id": evidence.get("experiment_id"),
                        "evidence_id": evidence.get("evidence_id"),
                        "applied_claim_id": lineage.get("applied_claim_id"),
                        "applied_trade_step_id": lineage.get("applied_trade_step_id"),
                        "source_type": evidence.get("source_type") or source_type,
                        "direction": evidence.get("direction"),
                        "horizon_days": evidence.get("horizon_days"),
                        "raw_confidence": evidence.get("raw_confidence"),
                        "calibrated_confidence": evidence.get("calibrated_confidence"),
                        "proxy_only": bool(evidence.get("proxy_only")),
                        "doc_uid": evidence.get("doc_uid"),
                        "headline_label_id": evidence.get("headline_label_id"),
                        "runtime_action_reason": lineage.get("runtime_action_reason"),
                        "runtime_action_source": lineage.get("runtime_action_source"),
                        "payload": evidence_payload,
                    }
                )
            return

        lineage = _merge_bootstrap_lineage(
            strategy,
            code=code,
            phase="exit",
            lineage=_runtime_action_lineage(
                strategy,
                action_reason or "bootstrap_backtest_exit",
            ),
            action_reason=action_reason or "bootstrap_backtest_exit",
        )
        await save_method(
            {
                "id": f"{signal_id}:backtest_bootstrap_exit",
                "signal_id": signal_id,
                "strategy_id": strategy.get("id"),
                "signal_date": signal_date,
                "signal_ts": _coerce_trade_ts(signal_date),
                "code": code,
                "evidence_id": "backtest_bootstrap_exit",
                "applied_claim_id": lineage.get("applied_claim_id"),
                "applied_trade_step_id": lineage.get("applied_trade_step_id"),
                "source_type": source_type,
                "direction": "down",
                "runtime_action_reason": lineage.get("runtime_action_reason") or action_reason,
                "runtime_action_source": lineage.get("runtime_action_source") or "backtest_bootstrap_import",
                "payload": {
                    "bootstrap_source": "backtest_to_incubation_v1",
                    "backtest_id": backtest_id,
                    "trade_payload": dict(trade_payload or {}),
                    "bootstrap_selection": dict(selection_payload or {}),
                    "action_reason": action_reason,
                    "lineage": lineage,
                    "synthetic_bootstrap_lineage": bool(lineage.get("fallback_applied")),
                },
            }
        )

    async def bootstrap_import_strategy(
        self,
        db,
        strategy_id: str,
        *,
        target_trade_count: Optional[int] = None,
        history_limit: int = 1200,
        replace_existing_bootstrap: bool = False,
    ) -> dict:
        strategy = await db.get_strategy(strategy_id)
        if not strategy:
            return {"strategy_id": strategy_id, "imported_round_trips": 0, "reason": "strategy_not_found"}
        bootstrap_floor = int(target_trade_count or _bootstrap_trade_floor(strategy.get("strategy_type")))
        cleanup_summary = None
        if replace_existing_bootstrap:
            cleanup_summary = await self.clear_bootstrap_imports(db, strategy_id)
            strategy = await db.get_strategy(strategy_id)
        existing_positions = await db.list_strategy_trade_positions(strategy_id=strategy_id, status="closed", limit=5000)
        existing_realized = len(list(existing_positions or []))
        shortfall = max(0, bootstrap_floor - existing_realized)
        if shortfall <= 0:
            return {
                "strategy_id": strategy_id,
                "imported_round_trips": 0,
                "bootstrap_trade_floor": bootstrap_floor,
                "existing_realized_trade_count": existing_realized,
                "reason": "bootstrap_floor_already_satisfied",
                "cleanup": cleanup_summary,
            }

        backtest_id, trades = await self._get_or_create_bootstrap_backtest(
            db,
            strategy,
            history_limit=history_limit,
        )
        round_trips = _group_backtest_round_trips(trades)
        candidate_limit = min(
            len(round_trips),
            max(
                shortfall,
                bootstrap_floor + existing_realized + 25,
                shortfall * 4,
                50,
            ),
        )
        selected_round_trips, selection_report = _select_bootstrap_round_trips(
            round_trips,
            candidate_limit,
        )
        ensure = await self.incubation_service.ensure_account(db, strategy, stage="warmup")
        account = dict(ensure.get("account") or {})
        account_id = str(account.get("id") or "").strip()
        imported = 0
        imported_codes: list[str] = []
        skipped_existing_positions = 0
        latest_trade_date: Optional[date] = None

        for selection in selected_round_trips:
            if imported >= shortfall:
                break
            item = selection.round_trip
            entry_ts = _coerce_trade_ts(item.entry.get("time") or item.entry.get("trade_date"))
            exit_ts = _coerce_trade_ts(item.exit.get("time") or item.exit.get("trade_date"))
            round_seed = (
                f"{strategy_id}:{item.code}:{entry_ts.date().isoformat()}:"
                f"{_safe_float(item.entry.get('price'), 0.0):.6f}:{_safe_int(item.entry.get('shares'), 0)}:"
                f"{exit_ts.date().isoformat()}:{_safe_float(item.exit.get('price'), 0.0):.6f}:"
                f"{_safe_int(item.exit.get('shares'), 0)}"
            )
            position_id = f"btpos_{uuid5(NAMESPACE_URL, round_seed).hex[:20]}"
            if await self._position_exists(db, position_id):
                skipped_existing_positions += 1
                continue
            entry_signal_id = f"btsig_{uuid5(NAMESPACE_URL, round_seed + ':entry').hex[:20]}"
            exit_signal_id = f"btsig_{uuid5(NAMESPACE_URL, round_seed + ':exit').hex[:20]}"
            entry_date = entry_ts.date()
            exit_date = exit_ts.date()
            selection_payload = {
                "policy": selection_report.get("policy"),
                "rank": selection.rank,
                "approx_pnl": selection.approx_pnl,
                "approx_return": selection.approx_return,
                "hold_days": selection.hold_days,
                "entry_date": selection.entry_date,
                "exit_date": selection.exit_date,
                "short_horizon": selection.short_horizon,
                "is_positive": selection.is_positive,
            }

            entry_order = await db.save_paper_order(
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "signal_date": entry_date,
                    "source": "backtest_bootstrap_import",
                    "code": item.code,
                    "direction": "buy",
                    "shares": _safe_int(item.entry.get("shares"), 0),
                    "price": round(_safe_float(item.entry.get("price"), 0.0), 4),
                    "order_type": "marketable_limit",
                    "status": "filled",
                    "commission": _safe_float(item.entry.get("fee"), 0.0),
                    "reason": "backtest_bootstrap_entry",
                    "filled_at": entry_ts,
                    "signal_id": entry_signal_id,
                    "position_id": position_id,
                }
            )
            entry_trade = await db.save_paper_trade(
                {
                    "id": f"bttrade_{uuid5(NAMESPACE_URL, round_seed + ':buy').hex[:24]}",
                    "account_id": account_id,
                    "stock_code": item.code,
                    "stock_name": item.code,
                    "trade_type": "buy",
                    "price": round(_safe_float(item.entry.get("price"), 0.0), 4),
                    "quantity": _safe_int(item.entry.get("shares"), 0),
                    "amount": round(
                        _safe_float(item.entry.get("price"), 0.0)
                        * float(_safe_int(item.entry.get("shares"), 0)),
                        4,
                    ),
                    "commission": _safe_float(item.entry.get("fee"), 0.0),
                    "trade_time": entry_ts,
                    "reason": "backtest_bootstrap_entry",
                    "strategy_id": strategy_id,
                    "source_order_id": str(entry_order.get("id")),
                    "signal_id": entry_signal_id,
                    "position_id": position_id,
                }
            )
            await self._save_bootstrap_signal_evidence(
                db,
                strategy,
                signal_id=entry_signal_id,
                position_id=position_id,
                account_id=account_id,
                signal_date=entry_date,
                code=item.code,
                backtest_id=backtest_id,
                source_type="backtest_bootstrap_entry",
                trade_payload=item.entry,
                selection_payload=selection_payload,
            )
            await record_trade_fill_from_order_and_trade(
                db,
                entry_order,
                entry_trade,
                source="backtest_bootstrap_import",
                payload={
                    "backtest_id": backtest_id,
                    "bootstrap_source": "backtest_to_incubation_v1",
                    "bootstrap_selection": selection_payload,
                },
            )

            exit_order = await db.save_paper_order(
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "signal_date": exit_date,
                    "source": "backtest_bootstrap_import",
                    "code": item.code,
                    "direction": "sell",
                    "shares": _safe_int(item.exit.get("shares"), 0),
                    "price": round(_safe_float(item.exit.get("price"), 0.0), 4),
                    "order_type": "marketable_limit",
                    "status": "filled",
                    "commission": _safe_float(item.exit.get("fee"), 0.0),
                    "reason": str(item.exit.get("reason") or "backtest_bootstrap_exit"),
                    "filled_at": exit_ts,
                    "signal_id": exit_signal_id,
                    "position_id": position_id,
                }
            )
            exit_trade = await db.save_paper_trade(
                {
                    "id": f"bttrade_{uuid5(NAMESPACE_URL, round_seed + ':sell').hex[:24]}",
                    "account_id": account_id,
                    "stock_code": item.code,
                    "stock_name": item.code,
                    "trade_type": "sell",
                    "price": round(_safe_float(item.exit.get("price"), 0.0), 4),
                    "quantity": _safe_int(item.exit.get("shares"), 0),
                    "amount": round(
                        _safe_float(item.exit.get("price"), 0.0)
                        * float(_safe_int(item.exit.get("shares"), 0)),
                        4,
                    ),
                    "commission": _safe_float(item.exit.get("fee"), 0.0),
                    "trade_time": exit_ts,
                    "reason": str(item.exit.get("reason") or "backtest_bootstrap_exit"),
                    "strategy_id": strategy_id,
                    "source_order_id": str(exit_order.get("id")),
                    "signal_id": exit_signal_id,
                    "position_id": position_id,
                }
            )
            await self._save_bootstrap_signal_evidence(
                db,
                strategy,
                signal_id=exit_signal_id,
                position_id=position_id,
                account_id=account_id,
                signal_date=exit_date,
                code=item.code,
                backtest_id=backtest_id,
                source_type="backtest_bootstrap_exit",
                trade_payload=item.exit,
                action_reason=str(item.exit.get("reason") or "backtest_bootstrap_exit"),
                selection_payload=selection_payload,
            )
            await record_trade_fill_from_order_and_trade(
                db,
                exit_order,
                exit_trade,
                source="backtest_bootstrap_import",
                payload={
                    "backtest_id": backtest_id,
                    "bootstrap_source": "backtest_to_incubation_v1",
                    "bootstrap_selection": selection_payload,
                },
            )
            imported += 1
            imported_codes.append(item.code)
            latest_trade_date = exit_date

        if imported > 0 and latest_trade_date is not None:
            await self.incubation_service.record_metrics(db, strategy, latest_trade_date)
        if imported > 0 and hasattr(db, "save_strategy_domain_event"):
            await db.save_strategy_domain_event(
                {
                    "strategy_id": strategy_id,
                    "aggregate_type": "strategy",
                    "aggregate_id": strategy_id,
                    "event_type": "incubation.bootstrap_backtest_imported",
                    "source": "strategy_acceptance_remediation",
                    "severity": "info",
                    "payload": {
                        "backtest_id": backtest_id,
                        "bootstrap_trade_floor": bootstrap_floor,
                        "existing_realized_trade_count": existing_realized,
                        "imported_round_trips": imported,
                        "imported_codes": imported_codes,
                        "skipped_existing_positions": skipped_existing_positions,
                        "bootstrap_source": "backtest_to_incubation_v1",
                        "bootstrap_selection": selection_report,
                        "cleanup": cleanup_summary,
                    },
                }
            )
        return {
            "strategy_id": strategy_id,
            "backtest_id": backtest_id,
            "bootstrap_trade_floor": bootstrap_floor,
            "existing_realized_trade_count": existing_realized,
            "imported_round_trips": imported,
            "imported_codes": imported_codes,
            "skipped_existing_positions": skipped_existing_positions,
            "selection": selection_report,
            "cleanup": cleanup_summary,
        }
