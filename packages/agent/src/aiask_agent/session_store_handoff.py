from __future__ import annotations

from typing import Any
from uuid import uuid4

from .session_store_utils import _dumps, now_iso


class SessionStoreHandoffMixin:
    def request_handoff(
        self,
        *,
        session_id: str,
        user_id: str | None = None,
        target: str | None = None,
        reason: str | None = None,
        summary: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        handoff_id = f"handoff_{uuid4().hex}"
        ts = now_iso()
        sid = str(session_id or "default").strip() or "default"
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions
                    (session_id, user_id, title, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sid, user_id, None, ts, ts, _dumps({})),
            )
            conn.execute(
                """
                INSERT INTO session_handoffs
                    (handoff_id, session_id, user_id, target, status, reason, summary, metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    handoff_id,
                    sid,
                    user_id,
                    target,
                    "requested",
                    reason,
                    summary,
                    _dumps(dict(metadata or {})),
                    ts,
                    ts,
                ),
            )
            conn.commit()
        item = self.get_handoff(handoff_id)
        assert item is not None
        return item

    def get_handoff(self, handoff_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM session_handoffs WHERE handoff_id = ?",
                (str(handoff_id or "").strip(),),
            ).fetchone()
        return self._handoff_row(row)

    def update_handoff(self, handoff_id: str, *, status: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        token = str(handoff_id or "").strip()
        if not token:
            raise ValueError("handoff_id is required")
        current = self.get_handoff(token)
        if current is None:
            raise FileNotFoundError(f"handoff not found: {token}")
        merged = dict(current.get("metadata") or {})
        merged.update(dict(metadata or {}))
        with self._connection() as conn:
            conn.execute(
                "UPDATE session_handoffs SET status = ?, metadata_json = ?, updated_at = ? WHERE handoff_id = ?",
                (str(status or "requested"), _dumps(merged), now_iso(), token),
            )
            conn.commit()
        item = self.get_handoff(token)
        assert item is not None
        return item

    def list_handoffs(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            values.append(str(session_id))
        if status:
            clauses.append("status = ?")
            values.append(str(status))
        values.append(max(1, min(int(limit or 100), 200)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM session_handoffs {where} ORDER BY updated_at DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._handoff_row(row)) is not None]

    def upsert_subgoal(
        self,
        *,
        session_id: str,
        subgoal_id: str | None = None,
        user_id: str | None = None,
        title: str,
        criteria: list[str] | None = None,
        status: str = "pending",
    ) -> dict[str, Any]:
        sid = str(session_id or "default").strip() or "default"
        goal_id = str(subgoal_id or f"subgoal_{uuid4().hex[:12]}").strip()
        if not str(title or "").strip():
            raise ValueError("title is required")
        normalized_status = str(status or "pending").strip().lower()
        if normalized_status not in {"pending", "in_progress", "completed", "cancelled"}:
            normalized_status = "pending"
        ts = now_iso()
        with self._connection() as conn:
            existing = conn.execute("SELECT created_at FROM subgoals WHERE subgoal_id = ?", (goal_id,)).fetchone()
            created_at = existing["created_at"] if existing else ts
            conn.execute(
                """
                INSERT OR REPLACE INTO subgoals
                    (subgoal_id, session_id, user_id, title, criteria_json, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    goal_id,
                    sid,
                    user_id,
                    str(title).strip(),
                    _dumps([str(item) for item in list(criteria or []) if str(item).strip()]),
                    normalized_status,
                    created_at,
                    ts,
                ),
            )
            conn.commit()
        item = self.get_subgoal(goal_id)
        assert item is not None
        return item

    def get_subgoal(self, subgoal_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM subgoals WHERE subgoal_id = ?",
                (str(subgoal_id or "").strip(),),
            ).fetchone()
        return self._subgoal_row(row)

    def list_subgoals(self, *, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM subgoals
                WHERE session_id = ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (str(session_id or "default").strip() or "default", max(1, min(int(limit or 100), 200))),
            ).fetchall()
        return [item for row in rows if (item := self._subgoal_row(row)) is not None]

    def clear_subgoals(self, *, session_id: str) -> list[dict[str, Any]]:
        sid = str(session_id or "default").strip() or "default"
        with self._connection() as conn:
            conn.execute("DELETE FROM subgoals WHERE session_id = ?", (sid,))
            conn.commit()
        return []
