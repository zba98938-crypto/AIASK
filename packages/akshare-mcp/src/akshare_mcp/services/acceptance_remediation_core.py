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
from aiask_quant_core.storage.trade_audit_writer import record_trade_fill_from_order_and_trade

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

class _RemediationCoreMixin:
    def __init__(self) -> None:
        self.incubation_service = get_strategy_incubation_service()

    async def remediate_failed_metrics_strategy(self, db, strategy_id: str) -> dict:
        strategy = await db.get_strategy(strategy_id)
        if not strategy:
            return {"strategy_id": strategy_id, "updated": False, "reason": "strategy_not_found"}
        positions = await db.list_strategy_trade_positions(strategy_id=strategy_id, limit=5000)
        code_stats = summarize_code_performance(positions)
        patch = build_failed_metrics_filter_patch(strategy, code_stats)
        if not patch:
            return {
                "strategy_id": strategy_id,
                "updated": False,
                "reason": "no_runtime_filter_patch",
                "code_stats": code_stats,
            }
        updated_payload = dict(strategy)
        updated_payload["params"] = patch["updated_params"]
        await db.save_strategy(updated_payload)
        if hasattr(db, "save_strategy_domain_event"):
            await db.save_strategy_domain_event(
                {
                    "strategy_id": strategy_id,
                    "aggregate_type": "strategy",
                    "aggregate_id": strategy_id,
                    "event_type": "strategy.runtime_remediation_applied",
                    "source": "strategy_acceptance_remediation",
                    "severity": "info",
                    "payload": {
                        "reason": "failed_metrics",
                        "kept_codes": patch["kept_codes"],
                        "excluded_codes": patch["excluded_codes"],
                        "code_stats": code_stats,
                    },
                }
            )
        return {
            "strategy_id": strategy_id,
            "updated": True,
            "kept_codes": patch["kept_codes"],
            "excluded_codes": patch["excluded_codes"],
            "code_stats": code_stats,
            "family_hardening": patch.get("family_hardening") or {},
        }

    async def rebuild_strategy_signal_cache(
        self,
        db,
        strategy: dict | str,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        history_limit: int = 1500,
    ) -> dict[str, Any]:
        if isinstance(strategy, str):
            strategy_id = str(strategy).strip()
            strategy_payload = await db.get_strategy(strategy_id)
        else:
            strategy_payload = dict(strategy or {})
            strategy_id = str(strategy_payload.get("id") or "").strip()
        if not strategy_payload or not strategy_id:
            return {"strategy_id": strategy_id, "rebuilt": False, "reason": "strategy_not_found"}

        runtime_params = _strategy_runtime_params(strategy_payload)
        strategy_type = str(
            strategy_payload.get("strategy_type") or runtime_params.get("strategy_type") or ""
        ).strip().lower()
        if not strategy_type:
            return {"strategy_id": strategy_id, "rebuilt": False, "reason": "strategy_type_missing"}

        signal_rows = await db.get_signals(
            strategy_id,
            start_date=start_date,
            end_date=end_date,
            limit=max(2000, int(history_limit or 1500) * 20),
        )
        signal_dates = sorted(
            {
                item.get("signal_date")
                for item in list(signal_rows or [])
                if isinstance(item.get("signal_date"), date)
            }
        )
        allowed_codes = sorted(_resolve_strategy_target_codes(strategy_payload))
        if not allowed_codes:
            allowed_codes = sorted(
                {
                    str(item.get("code") or "").strip()
                    for item in list(signal_rows or [])
                    if str(item.get("code") or "").strip()
                }
            )
        kline_start: Optional[date] = None
        kline_end: Optional[date] = None
        if allowed_codes:
            for code in allowed_codes[:1]:
                try:
                    klines = await db.get_klines(code, limit=max(250, int(history_limit or 1500)))
                except TypeError:
                    klines = await db.get_klines(code, None, None)
                kline_dates = [
                    _coerce_trade_date((row or {}).get("date") or (row or {}).get("time"))
                    for row in list(klines or [])
                ]
                kline_dates = [item for item in kline_dates if item is not None]
                if not kline_dates:
                    continue
                kline_start = kline_dates[0]
                kline_end = kline_dates[-1]
                break
        resolved_start = start_date or min(
            [item for item in (signal_dates[0] if signal_dates else None, kline_start) if item is not None],
            default=None,
        )
        resolved_end = end_date or max(
            [item for item in (signal_dates[-1] if signal_dates else None, kline_end) if item is not None],
            default=None,
        )

        if resolved_start is None or resolved_end is None or resolved_end < resolved_start:
            return {
                "strategy_id": strategy_id,
                "rebuilt": False,
                "reason": "signal_rebuild_window_missing",
                "candidate_codes": allowed_codes,
            }

        async with db.acquire() as conn:
            deleted_rows = _parse_affected_rows(
                await conn.execute(
                    """
                    DELETE FROM strategy_signals
                    WHERE strategy_id = $1
                      AND signal_date >= $2
                      AND signal_date <= $3
                    """,
                    strategy_id,
                    resolved_start,
                    resolved_end,
                )
            )

        signal_batches: dict[date, list[dict[str, Any]]] = defaultdict(list)
        candidate_event_count = 0
        active_codes: list[str] = []
        for code in allowed_codes:
            try:
                rows = await db.get_klines(
                    code,
                    start_date=str(resolved_start),
                    end_date=str(resolved_end),
                )
            except TypeError:
                rows = await db.get_klines(code, limit=max(250, int(history_limit or 1500)))
            normalized = [
                dict(item)
                for item in list(rows or [])
                if isinstance(item, dict) and item.get("close") is not None
            ]
            if not normalized:
                continue
            instance, execution_semantic_mode = StrategyRegistry.create_runtime_strategy(
                strategy_type,
                runtime_params,
            )
            if instance is None:
                continue
            active_codes.append(code)
            artifacts = _build_signal_tracking_artifacts(
                instance,
                normalized,
                execution_semantic_mode=execution_semantic_mode,
            )
            for event in list(artifacts.get("events") or []):
                signal_day = _coerce_trade_date(event.get("date"))
                signal = _safe_int(event.get("signal"), 0)
                if signal_day is None or signal == 0:
                    continue
                if signal_day < resolved_start or signal_day > resolved_end:
                    continue
                candidate_event_count += 1
                signal_batches[signal_day].append(
                    {
                        "code": code,
                        "signal": signal,
                        "score": float(signal),
                        "execution_semantic_mode": execution_semantic_mode,
                        "action_source": str(event.get("action_source") or "").strip() or None,
                        "event_action": str(event.get("action") or "").strip() or None,
                        "action_reason": str(event.get("reason") or "").strip() or None,
                        "signal_metadata": {
                            "event_index": int(event.get("index") or 0),
                            "event_date": signal_day.isoformat(),
                            "rebuild_source": "strategy_acceptance_remediation_signal_cache_v1",
                            "strategy_type": strategy_type,
                            "action_source": str(event.get("action_source") or "").strip() or None,
                            "event_action": str(event.get("action") or "").strip() or None,
                            "action_reason": str(event.get("reason") or "").strip() or None,
                        },
                    }
                )

        saved_rows = 0
        for signal_day in sorted(signal_batches.keys()):
            saved_rows += await db.save_signals(strategy_id, signal_day, signal_batches[signal_day])

        if hasattr(db, "save_strategy_domain_event"):
            await db.save_strategy_domain_event(
                {
                    "strategy_id": strategy_id,
                    "aggregate_type": "strategy",
                    "aggregate_id": strategy_id,
                    "event_type": "strategy.signal_cache_rebuilt",
                    "source": "strategy_acceptance_remediation",
                    "severity": "info",
                    "payload": {
                        "strategy_type": strategy_type,
                        "start_date": resolved_start.isoformat(),
                        "end_date": resolved_end.isoformat(),
                        "candidate_codes": allowed_codes,
                        "active_codes": active_codes,
                        "deleted_rows": deleted_rows,
                        "saved_rows": saved_rows,
                        "candidate_event_count": candidate_event_count,
                    },
                }
            )
        return {
            "strategy_id": strategy_id,
            "rebuilt": True,
            "strategy_type": strategy_type,
            "start_date": resolved_start.isoformat(),
            "end_date": resolved_end.isoformat(),
            "candidate_codes": allowed_codes,
            "active_codes": active_codes,
            "deleted_rows": deleted_rows,
            "saved_rows": saved_rows,
            "signal_days": len(signal_batches),
            "candidate_event_count": candidate_event_count,
        }
