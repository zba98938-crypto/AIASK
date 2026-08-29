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


class _IncubationMetricsMixin:
    async def list_strategy_incubation_metrics(
        self,
        strategy_id: str,
        limit: int = 30,
        start_date = None,
        end_date = None,
    ) -> List[dict]:
        async with self.acquire() as conn:
            sql = "SELECT * FROM strategy_incubation_metrics WHERE strategy_id = $1"
            params: list = [strategy_id]
            idx = 2
            if start_date is not None:
                sql += f" AND metric_date >= ${idx}"
                params.append(start_date)
                idx += 1
            if end_date is not None:
                sql += f" AND metric_date <= ${idx}"
                params.append(end_date)
                idx += 1
            sql += f" ORDER BY metric_date DESC LIMIT ${idx}"
            params.append(max(1, min(int(limit or 30), 365)))
            rows = await conn.fetch(sql, *params)
        return [self._decode_incubation_metric(dict(row)) for row in rows]

    # ── 孵化流水线快照 ──

    def _decode_incubation_pipeline_snapshot(self, row: dict) -> dict:
        result = dict(row)
        result["blockers"] = self._decode_json_field(result.get("blockers"), [])
        result["risk_flags"] = self._decode_json_field(result.get("risk_flags"), [])
        result["summary"] = self._decode_json_field(result.get("summary"), {})
        result["metadata"] = self._decode_json_field(result.get("metadata"), {})
        if result.get("priority_score") is None:
            result["priority_score"] = result["summary"].get("priority_score", result.get("readiness_score"))
        if result.get("gate_status") is None:
            result["gate_status"] = result["summary"].get("gate_status") or result["metadata"].get("gate_status")
        if result.get("gate_reasons") is None:
            result["gate_reasons"] = list(result["summary"].get("gate_reasons") or result["metadata"].get("gate_reasons") or [])
        return result
