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


class _IncMappersMixin:
    async def save_strategy_incubation_pipeline_snapshot(self, snapshot: dict) -> dict:
        payload = dict(snapshot or {})
        payload["summary"] = {
            **dict(payload.get("summary") or {}),
            "priority_score": payload.get("priority_score", payload.get("readiness_score")),
            "gate_status": payload.get("gate_status"),
            "gate_reasons": list(payload.get("gate_reasons") or []),
        }
        payload["metadata"] = {
            **dict(payload.get("metadata") or {}),
            "gate_status": payload.get("gate_status"),
            "gate_reasons": list(payload.get("gate_reasons") or []),
        }
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO strategy_incubation_pipeline_snapshots
                    (strategy_id, account_id, pipeline_stage, pipeline_status, observed_days, promote_streak,
                     halt_streak, latest_decision, readiness_score, next_action, auto_review, auto_promoted,
                     blockers, risk_flags, summary, metadata, task_run_id, source, evaluated_at, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                payload.get("strategy_id"),
                payload.get("account_id"),
                str(payload.get("pipeline_stage") or "warmup"),
                str(payload.get("pipeline_status") or "collecting"),
                int(payload.get("observed_days") or 0),
                int(payload.get("promote_streak") or 0),
                int(payload.get("halt_streak") or 0),
                payload.get("latest_decision"),
                float(payload.get("readiness_score") or 0.0),
                payload.get("next_action"),
                bool(payload.get("auto_review")),
                bool(payload.get("auto_promoted")),
                json.dumps(payload.get("blockers") or [], ensure_ascii=False, default=str),
                json.dumps(payload.get("risk_flags") or [], ensure_ascii=False, default=str),
                json.dumps(payload.get("summary") or {}, ensure_ascii=False, default=str),
                json.dumps(payload.get("metadata") or {}, ensure_ascii=False, default=str),
                payload.get("task_run_id"),
                str(payload.get("source") or "system"),
                self._coerce_timestamp(payload.get("evaluated_at")),
            )
        return self._decode_incubation_pipeline_snapshot(dict(row))

    async def save_governance_report_snapshot(self, snapshot: dict) -> dict:
        payload = dict(snapshot or {})
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO governance_report_snapshots
                    (scope_type, scope_id, overall_status, issues, payload_jsonb, generated_at, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP)
                RETURNING *
                """,
                str(payload.get("scope_type") or "system"),
                payload.get("scope_id"),
                str(payload.get("overall_status") or "unknown"),
                json.dumps(payload.get("issues") or [], ensure_ascii=False, default=str),
                json.dumps(payload.get("payload_jsonb") or payload.get("payload") or {}, ensure_ascii=False, default=str),
                self._coerce_timestamp(payload.get("generated_at")),
            )
        return dict(row)

    async def get_latest_governance_report_snapshot(self, scope_type: str, scope_id: Optional[str] = None) -> Optional[dict]:
        rows = await self.list_governance_report_snapshots(scope_type=scope_type, scope_id=scope_id, limit=1)
        return rows[0] if rows else None

    async def list_governance_report_snapshots(
        self,
        scope_type: Optional[str] = None,
        scope_id: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM governance_report_snapshots WHERE 1=1"
            params: list = []
            idx = 1
            if scope_type:
                sql += f" AND scope_type = ${idx}"
                params.append(scope_type)
                idx += 1
            if scope_id is not None:
                sql += f" AND scope_id IS NOT DISTINCT FROM ${idx}"
                params.append(scope_id)
                idx += 1
            sql += f" ORDER BY generated_at DESC, created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 500)))
            rows = await conn.fetch(sql, *params)
        return [dict(row) for row in rows]

    async def get_latest_strategy_incubation_pipeline_snapshot(self, strategy_id: str) -> Optional[dict]:
        rows = await self.list_strategy_incubation_pipeline_snapshots(strategy_id=strategy_id, limit=1)
        return rows[0] if rows else None

    async def list_strategy_incubation_pipeline_snapshots(
        self,
        strategy_id: Optional[str] = None,
        pipeline_stage: Optional[str] = None,
        pipeline_status: Optional[str] = None,
        limit: int = 20,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_incubation_pipeline_snapshots WHERE 1=1"
            params: list = []
            idx = 1
            if strategy_id:
                sql += f" AND strategy_id = ${idx}"
                params.append(strategy_id)
                idx += 1
            if pipeline_stage:
                sql += f" AND pipeline_stage = ${idx}"
                params.append(pipeline_stage)
                idx += 1
            if pipeline_status:
                sql += f" AND pipeline_status = ${idx}"
                params.append(pipeline_status)
                idx += 1
            sql += f" ORDER BY evaluated_at DESC, created_at DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 20), 500)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_incubation_pipeline_snapshot(dict(row)) for row in rows]
