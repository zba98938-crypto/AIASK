from __future__ import annotations

from typing import Any
from uuid import uuid4

from .session_store_utils import (
    _bounded_text,
    _clean_optional,
    _dumps,
    _iso_days_ago,
    _loads,
    _truthy,
    now_iso,
    sanitize_for_audit,
)


class SessionStoreUserDataMixin:
    def record_feedback(self, feedback: dict[str, Any]) -> dict[str, Any]:
        payload = dict(feedback or {})
        target_type = str(payload.get("target_type") or "").strip()
        feedback_type = str(payload.get("feedback_type") or payload.get("type") or "").strip()
        if not target_type:
            raise ValueError("target_type is required")
        if not feedback_type:
            raise ValueError("feedback_type is required")
        feedback_id = str(payload.get("feedback_id") or f"feedback_{uuid4().hex}").strip()
        rating = payload.get("rating")
        try:
            rating_value = int(rating) if rating is not None and str(rating).strip() != "" else None
        except (TypeError, ValueError):
            rating_value = None
        ts = str(payload.get("created_at") or now_iso())
        safe_payload = sanitize_for_audit(payload.get("payload") if "payload" in payload else {})
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO feedback_events
                    (feedback_id, user_id, session_id, run_id, target_type, target_id,
                     feedback_type, rating, comment, allow_learning, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    _clean_optional(payload.get("user_id")),
                    _clean_optional(payload.get("session_id")),
                    _clean_optional(payload.get("run_id")),
                    target_type[:80],
                    _clean_optional(payload.get("target_id")),
                    feedback_type[:80],
                    rating_value,
                    _bounded_text(payload.get("comment"), limit=2000) if payload.get("comment") is not None else None,
                    1 if _truthy(payload.get("allow_learning")) else 0,
                    _dumps(safe_payload),
                    ts,
                ),
            )
            conn.commit()
        return self.get_feedback(feedback_id) or {"feedback_id": feedback_id}

    def get_feedback(self, feedback_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM feedback_events WHERE feedback_id = ?",
                (str(feedback_id or "").strip(),),
            ).fetchone()
        return self._feedback_row(row)

    def list_feedback(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in {
            "user_id": user_id,
            "session_id": session_id,
            "run_id": run_id,
            "target_type": target_type,
            "target_id": target_id,
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
                FROM feedback_events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._feedback_row(row)) is not None]

    def get_user_data_policy(self, user_id: str) -> dict[str, Any]:
        uid = str(user_id or "local").strip() or "local"
        ts = now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO user_data_policies
                    (user_id, event_ttl_days, audit_ttl_days, run_event_ttl_days,
                     tool_payload_ttl_days, conversation_retention,
                     allow_product_analytics, allow_learning, updated_at)
                VALUES (?, 90, 180, 180, 90, 'keep_until_user_deletes', 1, 0, ?)
                """,
                (uid, ts),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM user_data_policies WHERE user_id = ?", (uid,)).fetchone()
        item = self._policy_row(row)
        assert item is not None
        return item

    def update_user_data_policy(self, user_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        current = self.get_user_data_policy(user_id)
        updates = dict(patch or {})
        allowed_ints = {
            "event_ttl_days": (1, 3650),
            "audit_ttl_days": (1, 3650),
            "run_event_ttl_days": (1, 3650),
            "tool_payload_ttl_days": (1, 3650),
        }
        next_policy = dict(current)
        for key, (minimum, maximum) in allowed_ints.items():
            if key in updates:
                try:
                    next_policy[key] = max(minimum, min(int(updates[key]), maximum))
                except (TypeError, ValueError):
                    pass
        if updates.get("conversation_retention"):
            value = str(updates.get("conversation_retention") or "").strip()
            if value in {"keep_until_user_deletes", "delete_after_ttl", "archive_after_ttl"}:
                next_policy["conversation_retention"] = value
        for key in ("allow_product_analytics", "allow_learning"):
            if key in updates:
                next_policy[key] = _truthy(updates[key])
        ts = now_iso()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE user_data_policies
                SET event_ttl_days = ?,
                    audit_ttl_days = ?,
                    run_event_ttl_days = ?,
                    tool_payload_ttl_days = ?,
                    conversation_retention = ?,
                    allow_product_analytics = ?,
                    allow_learning = ?,
                    updated_at = ?
                WHERE user_id = ?
                """,
                (
                    next_policy["event_ttl_days"],
                    next_policy["audit_ttl_days"],
                    next_policy["run_event_ttl_days"],
                    next_policy["tool_payload_ttl_days"],
                    next_policy["conversation_retention"],
                    1 if next_policy["allow_product_analytics"] else 0,
                    1 if next_policy["allow_learning"] else 0,
                    ts,
                    str(user_id or "local").strip() or "local",
                ),
            )
            conn.commit()
        return self.get_user_data_policy(str(user_id or "local").strip() or "local")

    def user_activity_summary(self, *, user_id: str, limit: int = 20) -> dict[str, Any]:
        uid = str(user_id or "local").strip() or "local"
        return {
            "object": "aiask.user_activity",
            "user_id": uid,
            "sessions": self.list_sessions(user_id=uid, limit=limit),
            "runs": [
                item
                for item in self.list_runs(limit=limit * 5)
                if str((item.get("payload") or {}).get("user_id") or "") == uid
            ][:limit],
            "events": self.list_activity_events(user_id=uid, limit=limit),
            "tool_invocations": self.list_tool_invocations(user_id=uid, limit=limit),
            "context_snapshots": self.list_context_snapshots(user_id=uid, limit=limit),
            "feedback": self.list_feedback(user_id=uid, limit=limit),
            "policy": self.get_user_data_policy(uid),
            "secrets_redacted": True,
        }

    def analytics_summary(self, *, user_id: str | None = None, limit: int = 20) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        clauses: list[str] = []
        values: list[Any] = []
        if uid:
            clauses.append("user_id = ?")
            values.append(uid)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        max_rows = max(1, min(int(limit or 20), 100))
        with self._connection() as conn:
            event_total = int(conn.execute(f"SELECT COUNT(*) FROM user_activity_events {where}", tuple(values)).fetchone()[0])
            feedback_total = int(conn.execute(f"SELECT COUNT(*) FROM feedback_events {where}", tuple(values)).fetchone()[0])
            tool_total = int(conn.execute(f"SELECT COUNT(*) FROM tool_invocations {where}", tuple(values)).fetchone()[0])
            event_rows = conn.execute(
                f"""
                SELECT event_type, COUNT(*) AS count
                FROM user_activity_events
                {where}
                GROUP BY event_type
                ORDER BY count DESC, event_type ASC
                LIMIT ?
                """,
                tuple(values + [max_rows]),
            ).fetchall()
            page_rows = conn.execute(
                f"""
                SELECT COALESCE(page_key, route, 'unknown') AS page_key, COUNT(*) AS count
                FROM user_activity_events
                {where}
                GROUP BY COALESCE(page_key, route, 'unknown')
                ORDER BY count DESC, page_key ASC
                LIMIT ?
                """,
                tuple(values + [max_rows]),
            ).fetchall()
            tool_rows = conn.execute(
                f"""
                SELECT tool_name,
                       COUNT(*) AS count,
                       SUM(CASE WHEN status = 'succeeded' THEN 1 ELSE 0 END) AS succeeded,
                       SUM(CASE WHEN status IN ('failed', 'denied', 'blocked', 'cancelled') THEN 1 ELSE 0 END) AS failed,
                       AVG(duration_ms) AS avg_duration_ms
                FROM tool_invocations
                {where}
                GROUP BY tool_name
                ORDER BY count DESC, tool_name ASC
                LIMIT ?
                """,
                tuple(values + [max_rows]),
            ).fetchall()
            feedback_rows = conn.execute(
                f"""
                SELECT target_type, feedback_type, COUNT(*) AS count, AVG(rating) AS avg_rating
                FROM feedback_events
                {where}
                GROUP BY target_type, feedback_type
                ORDER BY count DESC, target_type ASC, feedback_type ASC
                LIMIT ?
                """,
                tuple(values + [max_rows]),
            ).fetchall()
        return {
            "object": "aiask.analytics_summary",
            "scope": "user" if uid else "aggregate",
            "user_id": uid or None,
            "totals": {
                "events": event_total,
                "tool_invocations": tool_total,
                "feedback": feedback_total,
            },
            "events_by_type": [dict(row) for row in event_rows],
            "pages": [dict(row) for row in page_rows],
            "tools": [
                {
                    **dict(row),
                    "failed": int(row["failed"] or 0),
                    "succeeded": int(row["succeeded"] or 0),
                    "failure_rate": (float(row["failed"] or 0) / float(row["count"] or 1)) if row["count"] else 0.0,
                    "avg_duration_ms": float(row["avg_duration_ms"] or 0.0),
                }
                for row in tool_rows
            ],
            "feedback": [
                {
                    **dict(row),
                    "avg_rating": float(row["avg_rating"]) if row["avg_rating"] is not None else None,
                }
                for row in feedback_rows
            ],
            "secrets_redacted": True,
        }

    def export_user_data(self, *, user_id: str, limit: int = 500) -> dict[str, Any]:
        uid = str(user_id or "local").strip() or "local"
        max_rows = max(1, min(int(limit or 500), 5000))
        session_ids = [str(item.get("session_id")) for item in self.list_sessions(user_id=uid, limit=max_rows, include_archived=True)]
        runs = [
            item
            for item in self.list_runs(limit=max_rows * 5)
            if str((item.get("payload") or {}).get("user_id") or "") == uid or str(item.get("session_id") or "") in session_ids
        ][:max_rows]
        run_ids = [str(item.get("run_id")) for item in runs if item.get("run_id")]
        messages: list[dict[str, Any]] = []
        for session_id in session_ids[:max_rows]:
            messages.extend(self.list_session_messages(session_id, limit=max_rows, include_deleted=True))
            if len(messages) >= max_rows:
                messages = messages[:max_rows]
                break
        run_events: list[dict[str, Any]] = []
        for run_id in run_ids[:max_rows]:
            run_events.extend(self.list_run_events(run_id, limit=max_rows))
            if len(run_events) >= max_rows:
                run_events = run_events[:max_rows]
                break
        return {
            "object": "aiask.user_data_export",
            "user_id": uid,
            "exported_at": now_iso(),
            "profile_policy": self.get_user_data_policy(uid),
            "sessions": self.list_sessions(user_id=uid, limit=max_rows, include_archived=True),
            "messages": messages,
            "runs": runs,
            "run_events": run_events,
            "activity_events": self.list_activity_events(user_id=uid, limit=max_rows),
            "tool_invocations": self.list_tool_invocations(user_id=uid, limit=max_rows),
            "context_snapshots": self.list_context_snapshots(user_id=uid, limit=max_rows),
            "feedback": self.list_feedback(user_id=uid, limit=max_rows),
            "sources": self.list_sources(user_id=uid, limit=max_rows),
            "artifacts": self.list_artifacts(user_id=uid, limit=max_rows),
            "analytics": self.analytics_summary(user_id=uid),
            "secrets_redacted": True,
        }

    def delete_user_data(
        self,
        *,
        user_id: str,
        include_conversations: bool = True,
        include_audit: bool = True,
        hard_delete: bool = False,
        reason: str = "user_data_delete",
        actor: str = "user",
        dry_run: bool = False,
    ) -> dict[str, Any]:
        uid = str(user_id or "local").strip() or "local"
        ts = now_iso()
        anonymous_user = f"deleted:{uid}"
        counts = {
            "sessions": 0,
            "messages": 0,
            "responses": 0,
            "runs": 0,
            "run_events": 0,
            "activity_events": 0,
            "tool_invocations": 0,
            "context_snapshots": 0,
            "feedback": 0,
            "sources": 0,
            "artifacts": 0,
            "search_rows": 0,
        }
        with self._connection() as conn:
            session_rows = conn.execute("SELECT session_id FROM sessions WHERE user_id = ?", (uid,)).fetchall()
            session_ids = [str(row["session_id"]) for row in session_rows]
            run_rows = conn.execute("SELECT run_id, session_id, payload_json FROM runs").fetchall()
            session_id_set = set(session_ids)
            run_ids = [
                str(row["run_id"])
                for row in run_rows
                if str(row["session_id"] or "") in session_id_set or str((_loads(row["payload_json"], {}) or {}).get("user_id") or "") == uid
            ]

            def count_where(table: str, clause: str, params: tuple[Any, ...]) -> int:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {clause}", params).fetchone()[0])

            if include_conversations:
                counts["sessions"] = count_where("sessions", "user_id = ?", (uid,))
                counts["messages"] = count_where("messages", f"session_id IN ({','.join('?' for _ in session_ids)})", tuple(session_ids)) if session_ids else 0
                counts["responses"] = count_where("responses", f"session_id IN ({','.join('?' for _ in session_ids)})", tuple(session_ids)) if session_ids else 0
                counts["runs"] = len(run_ids)
                counts["run_events"] = count_where("run_events", f"run_id IN ({','.join('?' for _ in run_ids)})", tuple(run_ids)) if run_ids else 0
                counts["search_rows"] = count_where("aiask_search_fts", "user_id = ?", (uid,)) if self._fts_available(conn) else 0
            if include_audit:
                counts["activity_events"] = count_where("user_activity_events", "user_id = ?", (uid,))
                counts["tool_invocations"] = count_where("tool_invocations", "user_id = ?", (uid,))
                counts["context_snapshots"] = count_where("context_snapshots", "user_id = ?", (uid,))
                counts["feedback"] = count_where("feedback_events", "user_id = ?", (uid,))
                counts["sources"] = count_where("agent_sources", "user_id = ?", (uid,))
                counts["artifacts"] = count_where("agent_artifacts", "user_id = ?", (uid,))
            if dry_run:
                return {
                    "object": "aiask.user_data_delete",
                    "user_id": uid,
                    "dry_run": True,
                    "hard_delete": bool(hard_delete),
                    "counts": counts,
                    "secrets_redacted": True,
                }
            if include_conversations and session_ids:
                if hard_delete:
                    conn.execute(f"DELETE FROM messages WHERE session_id IN ({','.join('?' for _ in session_ids)})", tuple(session_ids))
                    conn.execute(f"DELETE FROM responses WHERE session_id IN ({','.join('?' for _ in session_ids)})", tuple(session_ids))
                    if run_ids:
                        conn.execute(f"DELETE FROM run_events WHERE run_id IN ({','.join('?' for _ in run_ids)})", tuple(run_ids))
                        conn.execute(f"DELETE FROM runs WHERE run_id IN ({','.join('?' for _ in run_ids)})", tuple(run_ids))
                    conn.execute("DELETE FROM sessions WHERE user_id = ?", (uid,))
                else:
                    conn.execute(
                        f"UPDATE messages SET content = '', payload_json = '{{}}', deleted_at = ?, deleted_reason = ?, deleted_by = ? WHERE session_id IN ({','.join('?' for _ in session_ids)})",
                        tuple([ts, reason, actor] + session_ids),
                    )
                    for session_id in session_ids:
                        row = conn.execute("SELECT metadata_json FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
                        metadata = _loads(row["metadata_json"] if row else None, {})
                        metadata.update({"user_deleted": True, "deleted_at": ts, "deleted_reason": reason})
                        conn.execute(
                            "UPDATE sessions SET user_id = ?, title = ?, metadata_json = ?, updated_at = ? WHERE session_id = ?",
                            (anonymous_user, "Deleted user data", _dumps(metadata), ts, session_id),
                        )
                    run_id_set = set(run_ids)
                    for row in run_rows:
                        if str(row["run_id"]) not in run_id_set:
                            continue
                        payload = _loads(row["payload_json"], {})
                        if isinstance(payload, dict):
                            payload["user_id"] = anonymous_user
                        conn.execute(
                            "UPDATE runs SET payload_json = ?, updated_at = ? WHERE run_id = ?",
                            (_dumps(payload if isinstance(payload, dict) else {"user_id": anonymous_user}), ts, str(row["run_id"])),
                        )
                if self._fts_available(conn):
                    conn.execute("DELETE FROM aiask_search_fts WHERE user_id = ?", (uid,))
            if include_audit:
                if hard_delete:
                    for table in ("user_activity_events", "tool_invocations", "context_snapshots", "feedback_events", "agent_sources", "agent_artifacts"):
                        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (uid,))
                else:
                    conn.execute("UPDATE user_activity_events SET user_id = ?, session_id = NULL, run_id = NULL, trace_id = NULL, payload_json = '{}' WHERE user_id = ?", (anonymous_user, uid))
                    conn.execute("UPDATE tool_invocations SET user_id = ?, session_id = NULL, run_id = NULL, trace_id = NULL, input_summary_json = '{}', output_summary_json = '{}' WHERE user_id = ?", (anonymous_user, uid))
                    conn.execute("UPDATE context_snapshots SET user_id = ?, session_id = '', run_id = NULL, trace_id = NULL, source_message_ids_json = '[]', source_ids_json = '[]', artifact_ids_json = '[]', summary = NULL, metadata_json = '{}' WHERE user_id = ?", (anonymous_user, uid))
                    conn.execute("UPDATE feedback_events SET user_id = ?, session_id = NULL, run_id = NULL, comment = NULL, payload_json = '{}' WHERE user_id = ?", (anonymous_user, uid))
                    conn.execute("UPDATE agent_sources SET user_id = ?, session_id = NULL, run_id = NULL, trace_id = NULL, metadata_json = '{}' WHERE user_id = ?", (anonymous_user, uid))
                    conn.execute("UPDATE agent_artifacts SET user_id = ?, session_id = NULL, run_id = NULL, trace_id = NULL, preview_text = NULL, preview_json = '{}', metadata_json = '{}' WHERE user_id = ?", (anonymous_user, uid))
            conn.commit()
        return {
            "object": "aiask.user_data_delete",
            "user_id": uid,
            "dry_run": False,
            "hard_delete": bool(hard_delete),
            "anonymized_user_id": None if hard_delete else anonymous_user,
            "counts": counts,
            "deleted_at": ts,
            "external_side_effects": "not_rolled_back",
            "secrets_redacted": True,
        }

    def apply_retention_policies(self, *, user_id: str | None = None, dry_run: bool = True) -> dict[str, Any]:
        uid = str(user_id or "").strip()
        policies = [self.get_user_data_policy(uid)] if uid else self._list_user_data_policies()
        if not policies:
            policies = [self.get_user_data_policy("local")]
        deleted: dict[str, int] = {
            "user_activity_events": 0,
            "tool_invocations_payloads": 0,
            "context_snapshot_payloads": 0,
            "run_events": 0,
            "feedback_events": 0,
            "messages": 0,
        }
        with self._connection() as conn:
            for policy in policies:
                policy_uid = str(policy.get("user_id") or "local")
                event_cutoff = _iso_days_ago(int(policy.get("event_ttl_days") or 90))
                audit_cutoff = _iso_days_ago(int(policy.get("audit_ttl_days") or 180))
                run_cutoff = _iso_days_ago(int(policy.get("run_event_ttl_days") or 180))
                tool_payload_cutoff = _iso_days_ago(int(policy.get("tool_payload_ttl_days") or 90))

                deleted["user_activity_events"] += int(conn.execute("SELECT COUNT(*) FROM user_activity_events WHERE user_id = ? AND created_at < ?", (policy_uid, event_cutoff)).fetchone()[0])
                deleted["feedback_events"] += int(conn.execute("SELECT COUNT(*) FROM feedback_events WHERE user_id = ? AND created_at < ?", (policy_uid, audit_cutoff)).fetchone()[0])
                deleted["tool_invocations_payloads"] += int(conn.execute("SELECT COUNT(*) FROM tool_invocations WHERE user_id = ? AND created_at < ? AND (input_summary_json != '{}' OR output_summary_json != '{}')", (policy_uid, tool_payload_cutoff)).fetchone()[0])
                deleted["context_snapshot_payloads"] += int(conn.execute("SELECT COUNT(*) FROM context_snapshots WHERE user_id = ? AND created_at < ? AND (summary IS NOT NULL OR source_message_ids_json != '[]' OR source_ids_json != '[]' OR artifact_ids_json != '[]' OR metadata_json != '{}')", (policy_uid, audit_cutoff)).fetchone()[0])
                run_ids = [
                    str(row["run_id"])
                    for row in conn.execute("SELECT run_id, payload_json FROM runs").fetchall()
                    if str((_loads(row["payload_json"], {}) or {}).get("user_id") or "") == policy_uid
                ]
                if run_ids:
                    deleted["run_events"] += int(conn.execute(f"SELECT COUNT(*) FROM run_events WHERE run_id IN ({','.join('?' for _ in run_ids)}) AND created_at < ?", tuple(run_ids + [run_cutoff])).fetchone()[0])
                if policy.get("conversation_retention") == "delete_after_ttl":
                    session_ids = [
                        str(row["session_id"])
                        for row in conn.execute("SELECT session_id FROM sessions WHERE user_id = ? AND updated_at < ?", (policy_uid, audit_cutoff)).fetchall()
                    ]
                    if session_ids:
                        deleted["messages"] += int(conn.execute(f"SELECT COUNT(*) FROM messages WHERE session_id IN ({','.join('?' for _ in session_ids)}) AND deleted_at IS NULL", tuple(session_ids)).fetchone()[0])
                if dry_run:
                    continue
                conn.execute("DELETE FROM user_activity_events WHERE user_id = ? AND created_at < ?", (policy_uid, event_cutoff))
                conn.execute("DELETE FROM feedback_events WHERE user_id = ? AND created_at < ?", (policy_uid, audit_cutoff))
                conn.execute("UPDATE tool_invocations SET input_summary_json = '{}', output_summary_json = '{}' WHERE user_id = ? AND created_at < ?", (policy_uid, tool_payload_cutoff))
                conn.execute("UPDATE context_snapshots SET source_message_ids_json = '[]', source_ids_json = '[]', artifact_ids_json = '[]', summary = NULL, metadata_json = '{}' WHERE user_id = ? AND created_at < ?", (policy_uid, audit_cutoff))
                if run_ids:
                    conn.execute(f"DELETE FROM run_events WHERE run_id IN ({','.join('?' for _ in run_ids)}) AND created_at < ?", tuple(run_ids + [run_cutoff]))
                if policy.get("conversation_retention") == "delete_after_ttl":
                    session_ids = [
                        str(row["session_id"])
                        for row in conn.execute("SELECT session_id FROM sessions WHERE user_id = ? AND updated_at < ?", (policy_uid, audit_cutoff)).fetchall()
                    ]
                    if session_ids:
                        conn.execute(
                            f"UPDATE messages SET content = '', payload_json = '{{}}', deleted_at = ?, deleted_reason = ?, deleted_by = ? WHERE session_id IN ({','.join('?' for _ in session_ids)}) AND deleted_at IS NULL",
                            tuple([now_iso(), "retention_policy", "retention_sweep"] + session_ids),
                        )
            if not dry_run:
                conn.commit()
        return {
            "object": "aiask.retention_sweep",
            "dry_run": bool(dry_run),
            "user_id": uid or None,
            "counts": deleted,
            "tables": list(deleted.keys()),
            "market_data_affected": False,
            "secrets_redacted": True,
        }

    def learning_dataset(self, *, user_id: str, limit: int = 100) -> dict[str, Any]:
        uid = str(user_id or "local").strip() or "local"
        policy = self.get_user_data_policy(uid)
        if not policy.get("allow_learning"):
            return {
                "object": "aiask.learning_dataset",
                "user_id": uid,
                "allowed": False,
                "items": [],
                "reason": "learning_not_allowed",
                "secrets_redacted": True,
            }
        max_rows = max(1, min(int(limit or 100), 500))
        feedback = [item for item in self.list_feedback(user_id=uid, limit=max_rows) if item.get("allow_learning")]
        items: list[dict[str, Any]] = []
        for item in feedback:
            items.append(
                {
                    "kind": "feedback",
                    "target_type": item.get("target_type"),
                    "target_id": item.get("target_id"),
                    "feedback_type": item.get("feedback_type"),
                    "rating": item.get("rating"),
                    "comment": item.get("comment"),
                    "created_at": item.get("created_at"),
                }
            )
        return {
            "object": "aiask.learning_dataset",
            "user_id": uid,
            "allowed": True,
            "items": items[:max_rows],
            "count": min(len(items), max_rows),
            "secrets_redacted": True,
        }

    def workflow_recommendations(self, *, user_id: str, limit: int = 5) -> dict[str, Any]:
        uid = str(user_id or "local").strip() or "local"
        analytics = self.analytics_summary(user_id=uid)
        recommendations: list[dict[str, Any]] = []
        failed_tools = [tool for tool in analytics.get("tools", []) if float(tool.get("failure_rate") or 0) >= 0.25 and int(tool.get("count") or 0) >= 2]
        for tool in failed_tools[:3]:
            recommendations.append(
                {
                    "id": f"tool_reliability:{tool.get('tool_name')}",
                    "kind": "tool_reliability",
                    "priority": "high",
                    "title": f"Review failing tool {tool.get('tool_name')}",
                    "reason": f"Failure rate {float(tool.get('failure_rate') or 0):.0%} across {tool.get('count')} calls.",
                    "target": {"tool_name": tool.get("tool_name")},
                }
            )
        if not analytics.get("feedback"):
            recommendations.append(
                {
                    "id": "feedback:collect",
                    "kind": "feedback_collection",
                    "priority": "medium",
                    "title": "Collect explicit feedback",
                    "reason": "No feedback events are stored for this user yet.",
                    "target": {"page_key": "workbench"},
                }
            )
        if int((analytics.get("totals") or {}).get("events") or 0) > 0 and not self.get_user_data_policy(uid).get("allow_learning"):
            recommendations.append(
                {
                    "id": "learning:opt_in_review",
                    "kind": "learning_policy",
                    "priority": "low",
                    "title": "Review learning opt-in",
                    "reason": "Behavior data exists, but learning use is disabled.",
                    "target": {"user_id": uid},
                }
            )
        return {
            "object": "aiask.workflow_recommendations",
            "user_id": uid,
            "data_source": "local_user_activity",
            "data": recommendations[: max(1, min(int(limit or 5), 20))],
            "count": min(len(recommendations), max(1, min(int(limit or 5), 20))),
            "secrets_redacted": True,
        }

    def _list_user_data_policies(self) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute("SELECT * FROM user_data_policies ORDER BY user_id ASC").fetchall()
        return [item for row in rows if (item := self._policy_row(row)) is not None]
