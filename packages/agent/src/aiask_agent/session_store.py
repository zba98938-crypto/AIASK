from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import default_state_db_path
from .session_store_broker import SessionStoreBrokerMixin
from .session_store_evidence import SessionStoreEvidenceMixin
from .session_store_handoff import SessionStoreHandoffMixin
from .session_store_rows import (
    _activity_event_row,
    _artifact_row,
    _broker_account_row,
    _broker_analytics_row,
    _broker_deal_row,
    _broker_order_row,
    _broker_position_row,
    _broker_profile_row,
    _context_snapshot_row,
    _feedback_row,
    _handoff_row,
    _policy_row,
    _session_is_archived,
    _session_row,
    _session_user_id,
    _source_row,
    _subgoal_row,
    _tool_invocation_row,
)
from .session_store_search import _fts_available, _index_search_row, _search_like, _search_row
from .session_store_schema import ensure_session_store_schema
from .session_store_user_data import SessionStoreUserDataMixin
from .session_store_utils import (
    _bounded_text,
    _clean_optional,
    _dumps,
    _loads,
    _metadata_archived,
    _side_effect_text,
    now_iso,
    redact_audit_text,
    sanitize_for_audit,
)


class AgentSessionStore(SessionStoreBrokerMixin, SessionStoreEvidenceMixin, SessionStoreHandoffMixin, SessionStoreUserDataMixin):
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()
        self._conn: sqlite3.Connection | None = None
        self._conn_lock = threading.RLock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not self._schema_ready:
            conn.execute("PRAGMA journal_mode=WAL")
            self._ensure_schema(conn)
            self._schema_ready = True
        return conn

    @contextmanager
    def _connection(self) -> Any:
        with self._conn_lock:
            if self._conn is None:
                self._conn = self._connect()
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise

    def ensure_ready(self) -> None:
        with self._connection():
            pass

    def close(self) -> None:
        with self._conn_lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        ensure_session_store_schema(conn)

    def create_session(
        self,
        *,
        session_id: str | None = None,
        user_id: str | None = None,
        title: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        sid = str(session_id or uuid4()).strip()
        ts = now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO sessions
                    (session_id, user_id, title, created_at, updated_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (sid, user_id, title, ts, ts, _dumps(dict(metadata or {}))),
            )
            conn.commit()
        return sid

    def touch_session(self, session_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now_iso(), session_id),
            )
            conn.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT session_id, user_id, title, created_at, updated_at, metadata_json
                FROM sessions
                WHERE session_id = ?
                """,
                (str(session_id or "").strip(),),
            ).fetchone()
        return self._session_row(row)

    def update_session_metadata(
        self,
        session_id: str,
        updates: dict[str, Any],
        *,
        remove_keys: list[str] | None = None,
    ) -> dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        ts = now_iso()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT session_id, user_id, title, created_at, updated_at, metadata_json FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
            if row is None:
                raise FileNotFoundError(f"session not found: {sid}")
            metadata = _loads(row["metadata_json"], {})
            for key in list(remove_keys or []):
                metadata.pop(str(key), None)
            metadata.update(dict(updates or {}))
            conn.execute(
                "UPDATE sessions SET metadata_json = ?, updated_at = ? WHERE session_id = ?",
                (_dumps(metadata), ts, sid),
            )
            conn.commit()
            updated = conn.execute(
                "SELECT session_id, user_id, title, created_at, updated_at, metadata_json FROM sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
        item = self._session_row(updated)
        assert item is not None
        return item

    def set_session_handoff_state(
        self,
        session_id: str,
        *,
        status: str,
        handoff_id: str | None = None,
        target: str | None = None,
        source_run_id: str | None = None,
        source_tool_call_id: str | None = None,
        context_snapshot_id: str | None = None,
        summary: str | None = None,
        reason: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_status = str(status or "pending").strip().lower() or "pending"
        handoff_state = {
            "status": normalized_status,
            "handoff_id": _clean_optional(handoff_id),
            "target": _clean_optional(target),
            "source_run_id": _clean_optional(source_run_id),
            "source_tool_call_id": _clean_optional(source_tool_call_id),
            "context_snapshot_id": _clean_optional(context_snapshot_id),
            "summary": _bounded_text(summary, limit=4000) if summary else None,
            "reason": _bounded_text(reason, limit=1000) if reason else None,
            "updated_at": now_iso(),
            "metadata": sanitize_for_audit(dict(metadata or {})),
        }
        session = self.update_session_metadata(
            session_id,
            {
                "handoff_state": handoff_state,
                "handoff_status": normalized_status,
                "handoff_target": _clean_optional(target),
                "handoff_id": _clean_optional(handoff_id),
                "handoff_context_snapshot_id": _clean_optional(context_snapshot_id),
            },
        )
        return dict(session.get("metadata") or {}).get("handoff_state") or handoff_state

    def consume_session_handoff_state(
        self,
        session_id: str,
        *,
        run_id: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any] | None:
        session = self.get_session(session_id)
        metadata = dict((session or {}).get("metadata") or {})
        state = dict(metadata.get("handoff_state") or {})
        if str(state.get("status") or "").strip().lower() != "pending":
            return None
        consumed = {
            **state,
            "status": "active",
            "active_run_id": _clean_optional(run_id),
            "active_trace_id": _clean_optional(trace_id),
            "activated_at": now_iso(),
        }
        updated = self.update_session_metadata(
            session_id,
            {
                "handoff_state": consumed,
                "handoff_status": "active",
                "active_agent": _clean_optional(state.get("target")) or "handoff_target",
                "last_handoff_id": _clean_optional(state.get("handoff_id")),
                "active_context_snapshot_id": _clean_optional(state.get("context_snapshot_id")),
            },
        )
        return dict(updated.get("metadata") or {}).get("handoff_state") or consumed

    def set_session_archived(
        self,
        session_id: str,
        archived: bool = True,
        *,
        reason: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        ts = now_iso()
        cleaned_reason = str(reason or ("archived" if archived else "unarchived")).strip()[:500]
        cleaned_actor = str(actor or "control_token").strip()[:200] or "control_token"
        updates: dict[str, Any] = {
            "archived": bool(archived),
            "archived_updated_at": ts,
            "archived_updated_by": cleaned_actor,
        }
        if archived:
            updates.update(
                {
                    "archived_at": ts,
                    "archived_reason": cleaned_reason or "archived",
                    "archived_by": cleaned_actor,
                }
            )
        else:
            updates.update(
                {
                    "archived_at": None,
                    "archived_reason": None,
                    "unarchived_at": ts,
                    "unarchived_reason": cleaned_reason or "unarchived",
                    "unarchived_by": cleaned_actor,
                }
            )
        session = self.update_session_metadata(session_id, updates)
        return {
            "session_id": session["session_id"],
            "archived": bool(archived),
            "archived_at": (session.get("metadata") or {}).get("archived_at"),
            "archived_reason": (session.get("metadata") or {}).get("archived_reason"),
            "session": session,
        }

    def append_message(self, session_id: str, message: dict[str, Any]) -> None:
        payload = dict(message or {})
        role = str(payload.get("role") or "").strip() or "user"
        content = payload.get("content")
        if not isinstance(content, str):
            content = _dumps(content)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO messages
                    (session_id, role, content, name, tool_call_id, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    role,
                    content,
                    payload.get("name"),
                    payload.get("tool_call_id"),
                    _dumps(payload),
                    now_iso(),
                ),
            )
            message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            self._index_search_row(
                conn,
                kind="message",
                object_id=str(message_id),
                session_id=session_id,
                user_id=self._session_user_id(conn, session_id),
                content=str(content or ""),
                payload=payload,
            )
            conn.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                (now_iso(), session_id),
            )
            conn.commit()

    def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM messages WHERE session_id = ? AND deleted_at IS NULL ORDER BY id ASC",
                (session_id,),
            ).fetchall()
        return [_loads(row["payload_json"], {}) for row in rows]

    def undo_last_turns(
        self,
        session_id: str,
        turns: int = 1,
        *,
        reason: str | None = None,
        deleted_by: str | None = None,
    ) -> dict[str, Any]:
        sid = str(session_id or "").strip()
        try:
            turns_requested = int(turns or 1)
        except (TypeError, ValueError):
            turns_requested = 1
        turns_requested = max(1, min(turns_requested, 100))
        ts = now_iso()
        cleaned_reason = str(reason or "hermes_undo").strip()[:500] or "hermes_undo"
        cleaned_deleted_by = str(deleted_by or "control_token").strip()[:200] or "control_token"

        with self._connection() as conn:
            turn_rows = conn.execute(
                """
                SELECT id
                FROM messages
                WHERE session_id = ? AND deleted_at IS NULL AND role = 'user'
                ORDER BY id DESC
                LIMIT ?
                """,
                (sid, turns_requested),
            ).fetchall()
            if not turn_rows:
                return {
                    "session_id": sid,
                    "turns_requested": turns_requested,
                    "turns_undone": 0,
                    "message_ids": [],
                    "message_count": 0,
                    "deleted_at": ts,
                    "deleted_reason": cleaned_reason,
                    "deleted_by": cleaned_deleted_by,
                    "soft_deleted": True,
                    "side_effects_rolled_back": False,
                    "external_side_effects": "not_rolled_back",
                }

            cutoff_id = min(int(row["id"]) for row in turn_rows)
            rows = conn.execute(
                """
                SELECT id, role, content, created_at
                FROM messages
                WHERE session_id = ? AND deleted_at IS NULL AND id >= ?
                ORDER BY id ASC
                """,
                (sid, cutoff_id),
            ).fetchall()
            message_ids = [int(row["id"]) for row in rows]
            if message_ids:
                placeholders = ", ".join("?" for _ in message_ids)
                conn.execute(
                    f"""
                    UPDATE messages
                    SET deleted_at = ?, deleted_reason = ?, deleted_by = ?
                    WHERE id IN ({placeholders})
                    """,
                    (ts, cleaned_reason, cleaned_deleted_by, *message_ids),
                )
                try:
                    conn.executemany(
                        "DELETE FROM aiask_search_fts WHERE kind = ? AND object_id = ?",
                        [("message", str(message_id)) for message_id in message_ids],
                    )
                except sqlite3.OperationalError:
                    pass
                conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
                    (ts, sid),
                )
            conn.commit()

        return {
            "session_id": sid,
            "turns_requested": turns_requested,
            "turns_undone": len(turn_rows),
            "message_ids": message_ids,
            "message_count": len(message_ids),
            "deleted_at": ts,
            "deleted_reason": cleaned_reason,
            "deleted_by": cleaned_deleted_by,
            "soft_deleted": True,
            "side_effects_rolled_back": False,
            "external_side_effects": "not_rolled_back",
        }

    def save_response(self, response_id: str, session_id: str, payload: dict[str, Any]) -> None:
        ts = now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO responses
                    (response_id, session_id, payload_json, created_at, accessed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (response_id, session_id, _dumps(payload), ts, ts),
            )
            self._index_search_row(
                conn,
                kind="response",
                object_id=response_id,
                session_id=session_id,
                user_id=self._session_user_id(conn, session_id),
                content=str(payload.get("content") or payload.get("output_text") or ""),
                payload=payload,
            )
            conn.commit()

    def get_response(self, response_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM responses WHERE response_id = ?",
                (response_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE responses SET accessed_at = ? WHERE response_id = ?",
                    (now_iso(), response_id),
                )
                conn.commit()
        return _loads(row["payload_json"], {}) if row else None

    def delete_response(self, response_id: str) -> bool:
        with self._connection() as conn:
            cur = conn.execute("DELETE FROM responses WHERE response_id = ?", (response_id,))
            conn.commit()
            return cur.rowcount > 0

    def create_run(self, session_id: str, payload: dict[str, Any] | None = None) -> str:
        run_id = f"run_{uuid4().hex}"
        ts = now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (run_id, session_id, status, payload_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, session_id, "queued", _dumps(dict(payload or {})), ts, ts),
            )
            conn.commit()
        return run_id

    def update_run(self, run_id: str, *, status: str, payload: dict[str, Any] | None = None) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, payload_json = ?, updated_at = ? WHERE run_id = ?",
                (status, _dumps(dict(payload or {})), now_iso(), run_id),
            )
            conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT run_id, session_id, status, payload_json, created_at, updated_at FROM runs WHERE run_id = ?",
                (str(run_id or "").strip(),),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json", None), {})
        return item

    def list_runs(
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
            values.append(str(session_id).strip())
        if status:
            clauses.append("status = ?")
            values.append(str(status).strip())
        values.append(max(1, min(int(limit or 100), 1000)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT run_id, session_id, status, payload_json, created_at, updated_at
                FROM runs
                {where}
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json", None), {})
            items.append(item)
        return items

    def append_run_event(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        ts = now_iso()
        safe_payload = sanitize_for_audit(dict(payload or {}))
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO run_events (run_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (run_id, event_type, _dumps(safe_payload), ts),
            )
            event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()
        return {
            "id": str(event_id),
            "event": event_type,
            "run_id": run_id,
            "created_at": ts,
            "data": safe_payload,
        }

    def list_run_events(self, run_id: str, *, after_event_id: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT event_id, run_id, event_type, payload_json, created_at
                FROM run_events
                WHERE run_id = ? AND event_id > ?
                ORDER BY event_id ASC
                LIMIT ?
                """,
                (str(run_id or "").strip(), int(after_event_id or 0), max(1, min(int(limit or 500), 5000))),
            ).fetchall()
        return [
            {
                "id": str(row["event_id"]),
                "event": row["event_type"],
                "run_id": row["run_id"],
                "created_at": row["created_at"],
                "data": _loads(row["payload_json"], {}),
            }
            for row in rows
        ]

    def count_run_events(self, run_id: str) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM run_events WHERE run_id = ?",
                (str(run_id or "").strip(),),
            ).fetchone()
        return int((row["total"] if row else 0) or 0)

    def latest_message_at(self, session_id: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT created_at
                FROM messages
                WHERE session_id = ? AND deleted_at IS NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (str(session_id or "").strip(),),
            ).fetchone()
        return str(row["created_at"]) if row and row["created_at"] else None

    def count_session_messages(self, session_id: str) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM messages WHERE session_id = ? AND deleted_at IS NULL",
                (str(session_id or "").strip(),),
            ).fetchone()
        return int((row["total"] if row else 0) or 0)

    def list_session_messages(self, session_id: str, *, limit: int = 200, include_deleted: bool = False) -> list[dict[str, Any]]:
        deleted_filter = "" if include_deleted else "AND deleted_at IS NULL"
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, role, content, name, tool_call_id, payload_json, created_at, deleted_at, deleted_reason, deleted_by
                FROM messages
                WHERE session_id = ? {deleted_filter}
                ORDER BY id ASC
                LIMIT ?
                """,
                (str(session_id or "").strip(), max(1, min(int(limit or 200), 2000))),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json", None), {})
            items.append(item)
        return items

    def list_sessions(
        self,
        *,
        user_id: str | None = None,
        limit: int = 100,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        max_rows = max(1, min(int(limit or 100), 1000))
        if user_id:
            clauses.append("user_id = ?")
            values.append(user_id)
        values.append(5000 if not include_archived else max_rows)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT session_id, user_id, title, created_at, updated_at, metadata_json
                FROM sessions
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = self._session_row(row)
            if item is None:
                continue
            if not include_archived and _metadata_archived(item.get("metadata")):
                continue
            items.append(item)
            if len(items) >= max_rows:
                break
        return items

    def record_activity_event(self, event: dict[str, Any]) -> dict[str, Any]:
        payload = dict(event or {})
        event_type = str(payload.get("event_type") or payload.get("type") or "").strip()
        if not event_type:
            raise ValueError("event_type is required")
        ts = str(payload.get("created_at") or now_iso())
        safe_payload = sanitize_for_audit(payload.get("payload") if "payload" in payload else payload.get("payload_json", {}))
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO user_activity_events
                    (user_id, session_id, run_id, trace_id, page_key, route, event_type,
                     target_type, target_id, target_label, target_testid, payload_json, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _clean_optional(payload.get("user_id")),
                    _clean_optional(payload.get("session_id")),
                    _clean_optional(payload.get("run_id")),
                    _clean_optional(payload.get("trace_id")),
                    _clean_optional(payload.get("page_key")),
                    _clean_optional(payload.get("route")),
                    event_type[:120],
                    _clean_optional(payload.get("target_type")),
                    _clean_optional(payload.get("target_id")),
                    _clean_optional(payload.get("target_label")),
                    _clean_optional(payload.get("target_testid")),
                    _dumps(safe_payload),
                    str(payload.get("source") or "desktop").strip()[:80] or "desktop",
                    ts,
                ),
            )
            event_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.commit()
        return {
            "id": event_id,
            "user_id": _clean_optional(payload.get("user_id")),
            "session_id": _clean_optional(payload.get("session_id")),
            "run_id": _clean_optional(payload.get("run_id")),
            "trace_id": _clean_optional(payload.get("trace_id")),
            "page_key": _clean_optional(payload.get("page_key")),
            "route": _clean_optional(payload.get("route")),
            "event_type": event_type[:120],
            "target_type": _clean_optional(payload.get("target_type")),
            "target_id": _clean_optional(payload.get("target_id")),
            "target_label": _clean_optional(payload.get("target_label")),
            "target_testid": _clean_optional(payload.get("target_testid")),
            "payload": safe_payload,
            "source": str(payload.get("source") or "desktop").strip()[:80] or "desktop",
            "created_at": ts,
        }

    def record_activity_events(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.record_activity_event(dict(event or {})) for event in list(events or [])[:200]]

    def list_activity_events(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        page_key: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in {
            "user_id": user_id,
            "session_id": session_id,
            "run_id": run_id,
            "page_key": page_key,
        }.items():
            if value:
                clauses.append(f"{column} = ?")
                values.append(str(value).strip())
        values.append(max(1, min(int(limit or 100), 1000)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM user_activity_events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [self._activity_event_row(row) for row in rows]

    def start_tool_invocation(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        invocation_id: str | None = None,
        capability: str | None = None,
        category: str | None = None,
        side_effect: Any = None,
        approval_id: str | None = None,
        action_intent_id: str | None = None,
        source_chain: list[str] | None = None,
    ) -> dict[str, Any]:
        cleaned_tool = str(tool_name or "").strip()
        if not cleaned_tool:
            raise ValueError("tool_name is required")
        iid = str(invocation_id or f"tool_{uuid4().hex}").strip()
        ts = now_iso()
        side_effect_text = _side_effect_text(side_effect)
        source_chain_value = [str(item) for item in list(source_chain or []) if str(item).strip()]
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO tool_invocations
                    (invocation_id, user_id, session_id, run_id, trace_id, tool_name,
                     capability, category, side_effect, status, input_summary_json,
                     output_summary_json, error_code, error_summary, duration_ms,
                     approval_id, action_intent_id, source_chain_json, secrets_redacted,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    iid,
                    _clean_optional(user_id),
                    _clean_optional(session_id),
                    _clean_optional(run_id),
                    _clean_optional(trace_id),
                    cleaned_tool,
                    _clean_optional(capability),
                    _clean_optional(category),
                    side_effect_text,
                    "running",
                    _dumps(sanitize_for_audit(arguments or {})),
                    _dumps({}),
                    None,
                    None,
                    None,
                    _clean_optional(approval_id),
                    _clean_optional(action_intent_id),
                    _dumps(source_chain_value),
                    1,
                    ts,
                    ts,
                ),
            )
            conn.commit()
        return self.get_tool_invocation(iid) or {"invocation_id": iid, "tool_name": cleaned_tool, "status": "running"}

    def finish_tool_invocation(
        self,
        invocation_id: str,
        *,
        status: str,
        result: Any = None,
        error_code: str | None = None,
        error_summary: str | None = None,
        duration_ms: int | None = None,
        approval_id: str | None = None,
        action_intent_id: str | None = None,
    ) -> dict[str, Any] | None:
        iid = str(invocation_id or "").strip()
        if not iid:
            return None
        normalized_status = str(status or "completed").strip().lower()
        if normalized_status not in {"queued", "running", "succeeded", "failed", "denied", "cancelled", "blocked"}:
            normalized_status = "succeeded" if normalized_status in {"success", "completed"} else "failed"
        existing = self.get_tool_invocation(iid)
        if existing is None:
            return None
        resolved_approval_id = _clean_optional(approval_id) or existing.get("approval_id")
        resolved_intent_id = _clean_optional(action_intent_id) or existing.get("action_intent_id")
        output_summary = sanitize_for_audit(result if result is not None else {})
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE tool_invocations
                SET status = ?,
                    output_summary_json = ?,
                    error_code = ?,
                    error_summary = ?,
                    duration_ms = ?,
                    approval_id = ?,
                    action_intent_id = ?,
                    updated_at = ?
                WHERE invocation_id = ?
                """,
                (
                    normalized_status,
                    _dumps(output_summary),
                    _clean_optional(error_code),
                    redact_audit_text(error_summary, limit=1000) if error_summary else None,
                    duration_ms,
                    resolved_approval_id,
                    resolved_intent_id,
                    now_iso(),
                    iid,
                ),
            )
            conn.commit()
        return self.get_tool_invocation(iid)

    def get_tool_invocation(self, invocation_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM tool_invocations WHERE invocation_id = ?",
                (str(invocation_id or "").strip(),),
            ).fetchone()
        return self._tool_invocation_row(row)

    def list_tool_invocations(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        tool_name: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in {
            "user_id": user_id,
            "session_id": session_id,
            "run_id": run_id,
            "tool_name": tool_name,
        }.items():
            if value:
                clauses.append(f"{column} = ?")
                values.append(str(value).strip())
        values.append(max(1, min(int(limit or 100), 1000)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT *
                FROM tool_invocations
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._tool_invocation_row(row)) is not None]

    def search(
        self,
        *,
        query: str,
        session_id: str | None = None,
        user_id: str | None = None,
        limit: int = 20,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        token = str(query or "").strip()
        max_rows = max(1, min(int(limit or 20), 200))
        if not token:
            return []
        with self._connection() as conn:
            try:
                clauses = ["aiask_search_fts MATCH ?"]
                values: list[Any] = [token]
                if session_id:
                    clauses.append("session_id = ?")
                    values.append(session_id)
                if user_id:
                    clauses.append("user_id = ?")
                    values.append(user_id)
                values.append(max_rows)
                rows = conn.execute(
                    f"""
                    SELECT kind, object_id, session_id, user_id, content, payload_json
                    FROM aiask_search_fts
                    WHERE {" AND ".join(clauses)}
                    LIMIT ?
                    """,
                    tuple(values),
                ).fetchall()
                return [
                    item
                    for row in rows
                    if (item := self._search_row(row)) is not None
                    and (include_archived or not self._session_is_archived(conn, str(item.get("session_id") or "")))
                ]
            except sqlite3.OperationalError:
                return self._search_like(conn, token=token, session_id=session_id, limit=max_rows, include_archived=include_archived)

    _fts_available = staticmethod(_fts_available)
    _index_search_row = staticmethod(_index_search_row)
    _search_row = staticmethod(_search_row)
    _search_like = staticmethod(_search_like)
    _session_row = staticmethod(_session_row)
    _session_is_archived = staticmethod(_session_is_archived)
    _activity_event_row = staticmethod(_activity_event_row)
    _tool_invocation_row = staticmethod(_tool_invocation_row)
    _source_row = staticmethod(_source_row)
    _artifact_row = staticmethod(_artifact_row)
    _context_snapshot_row = staticmethod(_context_snapshot_row)
    _feedback_row = staticmethod(_feedback_row)
    _policy_row = staticmethod(_policy_row)
    _broker_profile_row = staticmethod(_broker_profile_row)
    _broker_account_row = staticmethod(_broker_account_row)
    _broker_position_row = staticmethod(_broker_position_row)
    _broker_order_row = staticmethod(_broker_order_row)
    _broker_deal_row = staticmethod(_broker_deal_row)
    _broker_analytics_row = staticmethod(_broker_analytics_row)
    _handoff_row = staticmethod(_handoff_row)
    _subgoal_row = staticmethod(_subgoal_row)
    _session_user_id = staticmethod(_session_user_id)
