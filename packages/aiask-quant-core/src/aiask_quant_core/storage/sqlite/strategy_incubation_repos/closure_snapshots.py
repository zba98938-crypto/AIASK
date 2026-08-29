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


class _ClosureSnapshotsMixin:
    def _decode_execution_audit_snapshot(self, row: dict) -> dict:
        result = dict(row)
        for key in (
            "verdict_reasons",
            "verification",
            "acceptance",
            "audit_summary",
            "snapshot",
            "metadata",
        ):
            default = [] if key == "verdict_reasons" else {}
            result[key] = self._decode_json_field(result.get(key), default)
        result["verdict"] = {
            "status": _string(result.get("verdict_status")) or "missing",
            "reasons": list(result.get("verdict_reasons") or []),
            "hard_gate_passed": bool(result.get("execution_hard_gate_passed")),
        }
        result["as_of"] = (
            result.get("as_of_date").isoformat()
            if isinstance(result.get("as_of_date"), date)
            else _string(result.get("as_of_date")) or None
        )
        return result

    def _coerce_optional_date(self, value):
        if isinstance(value, date):
            return value
        raw = _string(value)
        if not raw:
            return None
        try:
            return date.fromisoformat(raw[:10])
        except Exception:
            return None

    async def get_latest_execution_audit_snapshot(self, strategy_id: str) -> Optional[dict]:
        strategy_filter = _string(strategy_id)
        if not strategy_filter:
            return None
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM strategy_execution_audit_snapshots
                WHERE strategy_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                strategy_filter,
            )
        if not row:
            return None
        return self._decode_execution_audit_snapshot(dict(row))

    async def upsert_execution_audit_snapshot(self, snapshot: dict) -> Optional[dict]:
        payload = dict(snapshot or {})
        strategy_id = _string(payload.get("strategy_id"))
        if not strategy_id:
            return None
        verdict = dict(payload.get("verdict") or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_execution_audit_snapshots
                    (strategy_id, snapshot_id, as_of_date, source_run_id, factory_run_id, correlation_id, trace_id,
                     submission_lane, parent_task_run_id, source_action, verdict_status, verdict_reasons,
                     execution_hard_gate_passed, verification, acceptance, audit_summary, snapshot, metadata,
                     created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14,
                        $15, $16, $17, $18, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (strategy_id) DO UPDATE SET
                    snapshot_id = EXCLUDED.snapshot_id,
                    as_of_date = EXCLUDED.as_of_date,
                    source_run_id = EXCLUDED.source_run_id,
                    factory_run_id = EXCLUDED.factory_run_id,
                    correlation_id = EXCLUDED.correlation_id,
                    trace_id = EXCLUDED.trace_id,
                    submission_lane = EXCLUDED.submission_lane,
                    parent_task_run_id = EXCLUDED.parent_task_run_id,
                    source_action = EXCLUDED.source_action,
                    verdict_status = EXCLUDED.verdict_status,
                    verdict_reasons = EXCLUDED.verdict_reasons,
                    execution_hard_gate_passed = EXCLUDED.execution_hard_gate_passed,
                    verification = EXCLUDED.verification,
                    acceptance = EXCLUDED.acceptance,
                    audit_summary = EXCLUDED.audit_summary,
                    snapshot = EXCLUDED.snapshot,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                strategy_id,
                _string(payload.get("snapshot_id")) or f"eas_{strategy_id}",
                self._coerce_optional_date(payload.get("as_of")),
                payload.get("source_run_id"),
                payload.get("factory_run_id"),
                payload.get("correlation_id"),
                payload.get("trace_id"),
                payload.get("submission_lane"),
                payload.get("parent_task_run_id"),
                payload.get("source_action"),
                _string(verdict.get("status") or payload.get("verdict_status")) or "missing",
                json.dumps(
                    list(verdict.get("reasons") or payload.get("verdict_reasons") or []),
                    ensure_ascii=False,
                    default=str,
                ),
                bool(
                    verdict.get("hard_gate_passed")
                    if verdict.get("hard_gate_passed") is not None
                    else payload.get("execution_hard_gate_passed")
                ),
                json.dumps(payload.get("verification") or {}, ensure_ascii=False, default=str),
                json.dumps(payload.get("acceptance") or {}, ensure_ascii=False, default=str),
                json.dumps(payload.get("audit_summary") or {}, ensure_ascii=False, default=str),
                json.dumps(payload.get("snapshot") or {}, ensure_ascii=False, default=str),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
            )
        if not row:
            return None
        return self._decode_execution_audit_snapshot(dict(row))

    def _decode_strategy_closure_snapshot(self, row: dict) -> dict:
        result = dict(row or {})
        for key in ("snapshot", "metadata"):
            result[key] = self._decode_json_field(result.get(key), {})
        result["as_of"] = (
            result.get("as_of_date").isoformat()
            if isinstance(result.get("as_of_date"), date)
            else _string(result.get("as_of_date")) or None
        )
        return result

    async def get_latest_strategy_closure_snapshot(
        self,
        strategy_id: str,
        snapshot_type: str = "incubation_overview",
    ) -> Optional[dict]:
        strategy_filter = _string(strategy_id)
        snapshot_type_filter = _string(snapshot_type) or "incubation_overview"
        if not strategy_filter:
            return None
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT *
                FROM strategy_closure_snapshots
                WHERE strategy_id = $1
                  AND snapshot_type = $2
                ORDER BY as_of_date DESC NULLS LAST, updated_at DESC
                LIMIT 1
                """,
                strategy_filter,
                snapshot_type_filter,
            )
        if not row:
            return None
        return self._decode_strategy_closure_snapshot(dict(row))

    async def upsert_strategy_closure_snapshot(self, snapshot: dict) -> Optional[dict]:
        payload = dict(snapshot or {})
        strategy_id = _string(payload.get("strategy_id"))
        snapshot_type = _string(payload.get("snapshot_type")) or "incubation_overview"
        if not strategy_id:
            return None
        snapshot_id = _string(payload.get("snapshot_id")) or f"cls_{strategy_id}_{snapshot_type}"
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_closure_snapshots
                    (strategy_id, snapshot_type, snapshot_id, as_of_date, source_run_id, factory_run_id,
                     correlation_id, trace_id, submission_lane, parent_task_run_id, source_action,
                     snapshot, metadata, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT (strategy_id, snapshot_type) DO UPDATE SET
                    snapshot_id = EXCLUDED.snapshot_id,
                    as_of_date = EXCLUDED.as_of_date,
                    source_run_id = EXCLUDED.source_run_id,
                    factory_run_id = EXCLUDED.factory_run_id,
                    correlation_id = EXCLUDED.correlation_id,
                    trace_id = EXCLUDED.trace_id,
                    submission_lane = EXCLUDED.submission_lane,
                    parent_task_run_id = EXCLUDED.parent_task_run_id,
                    source_action = EXCLUDED.source_action,
                    snapshot = EXCLUDED.snapshot,
                    metadata = EXCLUDED.metadata,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING *
                """,
                strategy_id,
                snapshot_type,
                snapshot_id,
                self._coerce_optional_date(payload.get("as_of")),
                payload.get("source_run_id"),
                payload.get("factory_run_id"),
                payload.get("correlation_id"),
                payload.get("trace_id"),
                payload.get("submission_lane"),
                payload.get("parent_task_run_id"),
                payload.get("source_action"),
                json.dumps(payload.get("snapshot") or {}, ensure_ascii=False, default=str),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
            )
        if not row:
            return None
        return self._decode_strategy_closure_snapshot(dict(row))
