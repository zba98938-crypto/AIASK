"""Durable lifecycle scheduling; attempts never substitute for quality reviews."""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import os
import time
from uuid import uuid4

_STATE_KEY = "signal_tracker.lifecycle_scan.v1"
_TASK_NAME = "strategy_lifecycle_scan_item"
_LANES = ("incubating", "listed")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class LifecycleScanMixin:
    @asynccontextmanager
    async def _lifecycle_state_transaction(self):
        async with self.acquire() as conn:
            async with conn.transaction():
                # The first statement is a write, acquiring SQLite's writer
                # lock before reading the cursor. No review runs in this txn.
                await conn.execute(
                    "INSERT OR IGNORE INTO strategy_factory_scheduler_state (state_key, payload_json) VALUES ($1, $2)",
                    _STATE_KEY, json.dumps({"version": 1}),
                )
                raw = await conn.fetchval(
                    "SELECT payload_json FROM strategy_factory_scheduler_state WHERE state_key=$1", _STATE_KEY,
                )
                try:
                    state = json.loads(raw)
                    if not isinstance(state, dict) or state.get("version") != 1:
                        raise ValueError()
                    if "lanes" in state:
                        if not isinstance(state["lanes"], dict) or set(state["lanes"]) != set(_LANES):
                            raise ValueError()
                        for lane in state["lanes"].values():
                            if not isinstance(lane, dict) or not isinstance(lane.get("after"), str) or not isinstance(lane.get("upper"), str) or not isinstance(lane.get("exhausted"), bool):
                                raise ValueError()
                        if state.get("next_lane") not in (0, 1) or not isinstance(state.get("sweep_id"), str):
                            raise ValueError()
                except (TypeError, ValueError, KeyError):
                    raise ValueError("lifecycle_scan_state_invalid") from None
                yield conn, state

    async def _save_lifecycle_state(self, conn, state):
        await conn.execute(
            "UPDATE strategy_factory_scheduler_state SET payload_json=$1, updated_at=CURRENT_TIMESTAMP WHERE state_key=$2",
            json.dumps(state, ensure_ascii=False), _STATE_KEY,
        )

    @staticmethod
    def _require_lifecycle_owner(state, owner):
        if state.get("owner") != owner:
            raise RuntimeError("lifecycle_scan_lease_lost")

    async def claim_lifecycle_scan(self, owner: str, *, lease_seconds: float = 90) -> dict:
        if not owner:
            raise ValueError("lifecycle_scan_owner_required")
        async with self._lifecycle_state_transaction() as (conn, state):
            previous = state.get("owner")
            if previous and previous != owner:
                pid = int(state.get("owner_pid") or 0)
                # Expiry alone cannot establish that synchronous review work
                # stopped. An alive owner must release its lease explicitly.
                if pid <= 0 or _pid_alive(pid):
                    return {"acquired": False, "reason": "lifecycle_scan_lease_held", "sweep_id": state.get("sweep_id")}
            if previous != owner:
                await conn.execute(
                    "UPDATE strategy_task_runs SET status='interrupted', completed_at=CURRENT_TIMESTAMP, error=$1 "
                    "WHERE task_name=$2 AND status='running'",
                    "lifecycle_scan_owner_lost", _TASK_NAME,
                )
            if not state.get("lanes") or all(x["exhausted"] for x in state["lanes"].values()):
                lanes = {}
                for status in _LANES:
                    upper = await conn.fetchval("SELECT MAX(id) FROM strategies WHERE status=$1", status)
                    lanes[status] = {"after": "", "upper": str(upper or ""), "exhausted": not bool(upper)}
                state.update(sweep_id=uuid4().hex, lanes=lanes, next_lane=0)
            state.update(owner=owner, owner_pid=os.getpid(), lease_until=time.time() + max(1, lease_seconds))
            await self._save_lifecycle_state(conn, state)
            return {"acquired": True, "sweep_id": state["sweep_id"]}

    async def reserve_lifecycle_scan_item(self, owner: str) -> dict | None:
        async with self._lifecycle_state_transaction() as (conn, state):
            self._require_lifecycle_owner(state, owner)
            for _ in _LANES:
                lane_index = int(state["next_lane"]) % len(_LANES)
                status = _LANES[lane_index]
                state["next_lane"] = (lane_index + 1) % len(_LANES)
                lane = state["lanes"][status]
                if lane["exhausted"]:
                    continue
                sid = await conn.fetchval(
                    "SELECT id FROM strategies WHERE status=$1 AND id>$2 AND id<=$3 ORDER BY id LIMIT 1",
                    status, lane["after"], lane["upper"],
                )
                if not sid:
                    lane["exhausted"] = True
                    continue
                payload = {"sweep_id": state["sweep_id"], "strategy_id": str(sid),
                           "owner": owner, "expected_status": status}
                row = await conn.fetchrow(
                    # Keep logical identity in the immutable payload: the
                    # optional strategy_id FK cascades on strategy deletion.
                    "INSERT INTO strategy_task_runs (task_name, task_scope, task_key, status, payload) "
                    "VALUES ($1,'signal_tracker_lifecycle_scan',$2,'running',$3) RETURNING id",
                    _TASK_NAME, state["sweep_id"], json.dumps(payload),
                )
                lane["after"] = str(sid)
                if lane["after"] == lane["upper"]:
                    lane["exhausted"] = True
                state["lease_until"] = time.time() + 90
                await self._save_lifecycle_state(conn, state)
                return {"attempt_id": row["id"], "strategy_id": sid, **payload}
            await self._save_lifecycle_state(conn, state)
            return None

    async def finish_lifecycle_scan_item(self, owner: str, attempt_id: int, *, status: str, result: dict, error: str | None = None) -> None:
        if status not in {"completed", "failed", "interrupted", "ineligible"}:
            raise ValueError("lifecycle_scan_attempt_status_invalid")
        async with self._lifecycle_state_transaction() as (conn, state):
            self._require_lifecycle_owner(state, owner)
            row = await conn.fetchrow(
                "UPDATE strategy_task_runs SET status=$1,result=$2,error=$3,completed_at=CURRENT_TIMESTAMP "
                "WHERE id=$4 AND task_name=$5 AND status='running' AND json_extract(payload,'$.owner')=$6 RETURNING id",
                status, json.dumps(result, ensure_ascii=False, default=str), error, attempt_id, _TASK_NAME, owner,
            )
            if not row:
                raise RuntimeError("lifecycle_scan_attempt_not_owned")

    async def lifecycle_scan_progress(self, owner: str) -> dict:
        async with self._lifecycle_state_transaction() as (conn, state):
            self._require_lifecycle_owner(state, owner)
            rows = await conn.fetch(
                "SELECT status,COUNT(*) n FROM strategy_task_runs WHERE task_name=$1 AND task_key=$2 GROUP BY status",
                _TASK_NAME, state["sweep_id"],
            )
            counts = {row["status"]: row["n"] for row in rows}
            remaining = 0
            for status, lane in state["lanes"].items():
                remaining += int(await conn.fetchval(
                    "SELECT COUNT(*) FROM strategies WHERE status=$1 AND id>$2 AND id<=$3",
                    status, lane["after"], lane["upper"],
                ) or 0)
            unresolved = sum(counts.get(x, 0) for x in ("running", "failed", "interrupted"))
            unresolved_history = int(await conn.fetchval(
                "WITH history AS (SELECT json_extract(payload,'$.strategy_id') sid, "
                "MAX(CASE WHEN status IN ('running','failed','interrupted') THEN id END) unresolved_id, "
                "MAX(CASE WHEN status='completed' THEN id END) completed_id "
                "FROM strategy_task_runs WHERE task_name=$1 GROUP BY sid) "
                "SELECT COUNT(*) FROM history WHERE unresolved_id IS NOT NULL "
                "AND (completed_id IS NULL OR completed_id<unresolved_id)", _TASK_NAME,
            ) or 0)
            unreviewed_eligible = int(await conn.fetchval(
                "SELECT COUNT(*) FROM strategies s WHERE s.status IN ('incubating','listed') "
                "AND NOT EXISTS (SELECT 1 FROM strategy_task_runs t WHERE t.task_name=$1 AND t.task_key=$2 "
                "AND json_extract(t.payload,'$.strategy_id')=s.id AND t.status='completed')", _TASK_NAME, state["sweep_id"],
            ) or 0)
            return {"sweep_id": state["sweep_id"], "attempt_counts": counts, "remaining": remaining,
                    "unresolved": unresolved, "unresolved_strategy_count": unresolved_history,
                    "unreviewed_eligible": unreviewed_eligible,
                    "coverage_complete": remaining == 0 and unresolved == 0 and unresolved_history == 0 and unreviewed_eligible == 0,
                    "sweep_exhausted": all(x["exhausted"] for x in state["lanes"].values())}

    async def release_lifecycle_scan(self, owner: str) -> None:
        async with self._lifecycle_state_transaction() as (conn, state):
            self._require_lifecycle_owner(state, owner)
            state.update(owner=None, owner_pid=None, lease_until=None)
            await self._save_lifecycle_state(conn, state)
