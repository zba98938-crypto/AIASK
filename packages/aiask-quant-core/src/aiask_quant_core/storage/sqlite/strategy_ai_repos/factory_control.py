"""Persistent cross-factory run ledger and lease locks."""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional


_FACTORIES = frozenset(
    {"strategy_factory", "factor_mining", "incubation", "market_event_ingest"}
)
_TERMINAL_STATES = frozenset({"succeeded", "partial", "blocked", "failed", "cancelled"})
_TRANSITIONS = {
    "queued": frozenset({"running", "blocked", "cancelled"}),
    "running": frozenset({"queued", *_TERMINAL_STATES}),
    **{state: frozenset() for state in _TERMINAL_STATES},
}
_JSON_LIMIT = 64 * 1024
# Diagnostic stderr is kept bounded: enough to identify a startup failure or
# traceback, small enough that a chatty runner cannot bloat the ledger.
_STDERR_TAIL_LIMIT = 4 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip_text(value: Any, limit: int) -> Optional[str]:
    """Keep the tail of a diagnostic string, which is where failures show up."""
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return text[-limit:]


def _json_text(value: Any, fallback: Any) -> str:
    try:
        text = json.dumps(value if value is not None else fallback, ensure_ascii=False, default=str)
    except Exception:
        text = json.dumps(fallback, ensure_ascii=False)
    if len(text.encode("utf-8")) <= _JSON_LIMIT:
        return text
    return json.dumps(
        {"truncated": True, "reason": "factory_control_json_limit", "item_type": type(value).__name__},
        ensure_ascii=False,
    )


def _normalize_factory(value: Any) -> str:
    aliases = {
        "strategy": "strategy_factory",
        "strategy_factory": "strategy_factory",
        "factor": "factor_mining",
        "factor_factory": "factor_mining",
        "factor_mining": "factor_mining",
        "factor_mining_factory": "factor_mining",
        "incubation": "incubation",
        "incubation_factory": "incubation",
        "event": "market_event_ingest",
        "event_ingest": "market_event_ingest",
        "market_event_ingest": "market_event_ingest",
    }
    token = str(value or "").strip().lower().replace("-", "_")
    factory = aliases.get(token)
    if factory not in _FACTORIES:
        raise ValueError(f"unsupported factory: {value}")
    return factory


def _normalize_state(value: Any) -> str:
    aliases = {
        "pending": "queued",
        "accepted": "queued",
        "queued": "queued",
        "running": "running",
        "executing": "running",
        "success": "succeeded",
        "succeeded": "succeeded",
        "completed": "succeeded",
        "partial": "partial",
        "partial_infra": "partial",
        "partial_llm": "partial",
        "degraded": "partial",
        "skipped": "blocked",
        "blocked": "blocked",
        "failed": "failed",
        "error": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }
    state = aliases.get(str(value or "").strip().lower())
    if state is None:
        raise ValueError(f"unsupported factory run state: {value}")
    return state


class _FactoryControlMixin:
    @staticmethod
    def _decode_factory_control_run(row: dict[str, Any] | None) -> Optional[dict[str, Any]]:
        if not row:
            return None
        result = dict(row)
        for column, public_name, fallback in (
            ("params_json", "params", {}),
            ("summary_json", "summary", {}),
            ("evidence_refs_json", "evidence_refs", []),
            ("quality_flags_json", "quality_flags", []),
            ("blockers_json", "blockers", []),
        ):
            raw = result.pop(column, None)
            try:
                result[public_name] = json.loads(raw) if raw else fallback
            except Exception:
                result[public_name] = fallback
        result["factory"] = result.pop("factory_name", None)
        result["finished_at"] = result.get("finished_at")
        result["terminal"] = str(result.get("status") or "") in _TERMINAL_STATES
        return result

    async def request_factory_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = dict(payload or {})
        factory = _normalize_factory(data.get("factory") or data.get("factory_name"))
        idempotency_key = str(data.get("idempotency_key") or "").strip()
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        run_id = str(data.get("run_id") or f"factory_run_{uuid.uuid4().hex}").strip()
        requested_at = str(data.get("requested_at") or _now_iso())
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO factory_run_ledger
                    (run_id, factory_name, status, mode, request_id, idempotency_key,
                     execution_owner, requested_at, params_json, contract_version)
                VALUES ($1, $2, 'queued', $3, $4, $5, $6, $7, $8, 'aiask.factory_run.v1')
                ON CONFLICT(factory_name, idempotency_key) DO UPDATE SET
                    updated_at = factory_run_ledger.updated_at
                RETURNING *
                """,
                run_id,
                factory,
                str(data.get("mode") or "once"),
                str(data.get("request_id") or "").strip() or None,
                idempotency_key,
                str(data.get("execution_owner") or "aiask_factory_worker"),
                requested_at,
                _json_text(data.get("params") or {}, {}),
            )
        decoded = self._decode_factory_control_run(dict(row or {}))
        if decoded is None:
            raise RuntimeError("factory run request was not persisted")
        decoded["deduplicated"] = decoded["run_id"] != run_id
        return decoded

    async def get_factory_run(self, run_id: str) -> Optional[dict[str, Any]]:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM factory_run_ledger WHERE run_id = $1 LIMIT 1",
                str(run_id or "").strip(),
            )
        return self._decode_factory_control_run(dict(row)) if row else None

    async def list_factory_runs(
        self,
        *,
        factory: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        if factory:
            args.append(_normalize_factory(factory))
            clauses.append(f"factory_name = ${len(args)}")
        if status:
            args.append(_normalize_state(status))
            clauses.append(f"status = ${len(args)}")
        args.append(max(1, min(int(limit or 20), 200)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT * FROM factory_run_ledger {where} ORDER BY requested_at DESC LIMIT ${len(args)}",
                *args,
            )
        return [self._decode_factory_control_run(dict(row)) or {} for row in rows]

    async def get_latest_factory_run(self, factory: str) -> Optional[dict[str, Any]]:
        rows = await self.list_factory_runs(factory=factory, limit=1)
        return rows[0] if rows else None

    async def transition_factory_run(
        self,
        run_id: str,
        target_status: str,
        *,
        expected_status: str | None = None,
        **updates: Any,
    ) -> Optional[dict[str, Any]]:
        current = await self.get_factory_run(run_id)
        if current is None:
            return None
        source = _normalize_state(current.get("status"))
        target = _normalize_state(target_status)
        if expected_status is not None and source != _normalize_state(expected_status):
            return current
        if source != target and target not in _TRANSITIONS[source]:
            raise ValueError(f"invalid factory run transition: {source} -> {target}")
        started_at = updates.get("started_at") or current.get("started_at")
        finished_at = updates.get("finished_at") or current.get("finished_at")
        if target == "running" and not started_at:
            started_at = _now_iso()
        if target in _TERMINAL_STATES and not finished_at:
            finished_at = _now_iso()
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE factory_run_ledger SET
                    status = $2,
                    owner_id = $3,
                    phase = $4,
                    started_at = $5,
                    finished_at = $6,
                    heartbeat_at = $7,
                    attempt_count = $8,
                    pid = $9,
                    exit_code = $10,
                    summary_json = $11,
                    evidence_refs_json = $12,
                    quality_flags_json = $13,
                    blockers_json = $14,
                    error = $15,
                    raw_status = $16,
                    worker_started_at = $17,
                    worker_command = $18,
                    failure_kind = $19,
                    stderr_tail = $20,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = $1 AND status = $21
                RETURNING *
                """,
                str(run_id),
                target,
                updates.get("owner_id", current.get("owner_id")),
                updates.get("phase", current.get("phase")),
                started_at,
                finished_at,
                updates.get("heartbeat_at", current.get("heartbeat_at")),
                int(updates.get("attempt_count", current.get("attempt_count") or 0)),
                updates.get("pid", current.get("pid")),
                updates.get("exit_code", current.get("exit_code")),
                _json_text(updates.get("summary", current.get("summary") or {}), {}),
                _json_text(updates.get("evidence_refs", current.get("evidence_refs") or []), []),
                _json_text(updates.get("quality_flags", current.get("quality_flags") or []), []),
                _json_text(updates.get("blockers", current.get("blockers") or []), []),
                updates.get("error", current.get("error")),
                updates.get("raw_status", current.get("raw_status")),
                updates.get("worker_started_at", current.get("worker_started_at")),
                updates.get("worker_command", current.get("worker_command")),
                updates.get("failure_kind", current.get("failure_kind")),
                _clip_text(updates.get("stderr_tail", current.get("stderr_tail")), _STDERR_TAIL_LIMIT),
                source,
            )
        if row is None:
            return await self.get_factory_run(run_id)
        return self._decode_factory_control_run(dict(row))

    async def claim_factory_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: int = 900,
        pid: int | None = None,
    ) -> Optional[dict[str, Any]]:
        current = await self.get_factory_run(run_id)
        if current is None or current.get("status") != "queued":
            return None
        now = _now_iso()
        expires_at = time.time() + max(30, int(lease_seconds or 900))
        async with self.acquire() as conn:
            lock = await conn.fetchrow(
                """
                INSERT INTO factory_run_locks
                    (factory_name, run_id, owner_id, acquired_at, heartbeat_at, expires_at_epoch)
                VALUES ($1, $2, $3, $4, $4, $5)
                ON CONFLICT(factory_name) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    owner_id = EXCLUDED.owner_id,
                    acquired_at = EXCLUDED.acquired_at,
                    heartbeat_at = EXCLUDED.heartbeat_at,
                    expires_at_epoch = EXCLUDED.expires_at_epoch
                WHERE factory_run_locks.expires_at_epoch <= $6
                RETURNING *
                """,
                current["factory"],
                str(run_id),
                str(owner_id),
                now,
                expires_at,
                time.time(),
            )
        if lock is None or str(lock.get("run_id")) != str(run_id):
            return None
        claimed = await self.transition_factory_run(
            run_id,
            "running",
            expected_status="queued",
            owner_id=owner_id,
            heartbeat_at=now,
            attempt_count=int(current.get("attempt_count") or 0) + 1,
            pid=pid,
            phase="claimed",
            worker_started_at=now,
        )
        if claimed is None or claimed.get("status") != "running":
            await self.release_factory_lock(run_id, owner_id=owner_id)
            return None
        return claimed

    async def record_factory_worker_launch(
        self,
        run_id: str,
        *,
        worker_started_at: str,
        worker_command: str,
        pid: int | None = None,
    ) -> Optional[dict[str, Any]]:
        """Attach launch diagnostics without touching the run state machine.

        The dispatcher records how it started the worker, but the worker races
        ahead on its own: a fast run can already be terminal by the time the
        dispatcher gets here. Going through transition_factory_run would then
        attempt an illegal ``partial -> queued`` move, so write the diagnostic
        columns directly and leave ``status`` alone.
        """
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE factory_run_ledger SET
                    worker_started_at = $2,
                    worker_command = $3,
                    pid = COALESCE($4, pid),
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = $1
                RETURNING *
                """,
                str(run_id),
                str(worker_started_at),
                str(worker_command),
                int(pid) if pid is not None else None,
            )
        return self._decode_factory_control_run(dict(row)) if row else None

    async def heartbeat_factory_run(
        self,
        run_id: str,
        *,
        owner_id: str,
        lease_seconds: int = 900,
        phase: str | None = None,
    ) -> bool:
        now = _now_iso()
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE factory_run_locks SET heartbeat_at = $3, expires_at_epoch = $4
                WHERE run_id = $1 AND owner_id = $2
                RETURNING run_id
                """,
                str(run_id),
                str(owner_id),
                now,
                time.time() + max(30, int(lease_seconds or 900)),
            )
        if row is None:
            return False
        await self.transition_factory_run(
            run_id,
            "running",
            expected_status="running",
            heartbeat_at=now,
            phase=phase,
        )
        return True

    async def release_factory_lock(self, run_id: str, *, owner_id: str) -> bool:
        async with self.acquire() as conn:
            row = await conn.fetchrow(
                """
                DELETE FROM factory_run_locks
                WHERE run_id = $1 AND owner_id = $2
                RETURNING run_id
                """,
                str(run_id),
                str(owner_id),
            )
        return row is not None

    async def recover_stale_factory_runs(self, *, stale_after_seconds: int = 900) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max(30, int(stale_after_seconds)))).isoformat()
        async with self.acquire() as conn:
            await conn.execute(
                "DELETE FROM factory_run_locks WHERE expires_at_epoch <= $1",
                time.time(),
            )
            rows = await conn.fetch(
                """
                UPDATE factory_run_ledger SET
                    status = 'queued', owner_id = NULL, pid = NULL,
                    phase = 'recovered_after_stale_worker', updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running' AND COALESCE(heartbeat_at, started_at, requested_at) < $1
                RETURNING *
                """,
                cutoff,
            )
        return [self._decode_factory_control_run(dict(row)) or {} for row in rows]


__all__ = ["_FactoryControlMixin"]
