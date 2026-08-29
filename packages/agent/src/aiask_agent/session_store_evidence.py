from __future__ import annotations

from typing import Any
from uuid import uuid4

from .session_store_utils import (
    _bounded_text,
    _clean_optional,
    _dumps,
    _float_or_none,
    _int_or_none,
    now_iso,
    sanitize_for_audit,
)


class SessionStoreEvidenceMixin:
    def record_source(self, source: dict[str, Any]) -> dict[str, Any]:
        payload = dict(source or {})
        source_id = str(payload.get("source_id") or f"src_{uuid4().hex}").strip()
        source_type = str(payload.get("source_type") or "data_provider").strip()[:80] or "data_provider"
        fetched_at = str(payload.get("fetched_at") or now_iso())
        created_at = str(payload.get("created_at") or fetched_at)
        metadata = sanitize_for_audit(payload.get("metadata") if "metadata" in payload else {})
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_sources
                    (source_id, user_id, session_id, run_id, trace_id, tool_call_id,
                     tool_name, provider, source_type, title, url, published_at,
                     fetched_at, data_timestamp, excerpt, source_tier,
                     credibility_score, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_id,
                    _clean_optional(payload.get("user_id")),
                    _clean_optional(payload.get("session_id")),
                    _clean_optional(payload.get("run_id")),
                    _clean_optional(payload.get("trace_id")),
                    _clean_optional(payload.get("tool_call_id")),
                    _clean_optional(payload.get("tool_name")),
                    _clean_optional(payload.get("provider")),
                    source_type,
                    _bounded_text(payload.get("title"), limit=500) if payload.get("title") is not None else None,
                    _clean_optional(payload.get("url"), limit=2000),
                    _clean_optional(payload.get("published_at"), limit=200),
                    fetched_at,
                    _clean_optional(payload.get("data_timestamp"), limit=200),
                    _bounded_text(payload.get("excerpt"), limit=2000) if payload.get("excerpt") is not None else None,
                    _clean_optional(payload.get("source_tier"), limit=80),
                    _float_or_none(payload.get("credibility_score")),
                    _dumps(metadata),
                    created_at,
                ),
            )
            self._index_search_row(
                conn,
                kind="source",
                object_id=source_id,
                session_id=str(payload.get("session_id") or ""),
                user_id=_clean_optional(payload.get("user_id")),
                content=" ".join(
                    str(item or "")
                    for item in (
                        payload.get("title"),
                        payload.get("url"),
                        payload.get("provider"),
                        payload.get("excerpt"),
                        source_type,
                    )
                ).strip(),
                payload={**payload, "metadata": metadata, "source_id": source_id, "source_type": source_type},
            )
            conn.commit()
        item = self.get_source(source_id)
        assert item is not None
        return item

    def record_artifact(self, artifact: dict[str, Any]) -> dict[str, Any]:
        payload = dict(artifact or {})
        artifact_id = str(payload.get("artifact_id") or f"art_{uuid4().hex}").strip()
        kind = str(payload.get("kind") or "file").strip()[:80] or "file"
        title = str(payload.get("title") or payload.get("path") or artifact_id).strip()[:500] or artifact_id
        status = str(payload.get("status") or "ready").strip()[:80] or "ready"
        created_at = str(payload.get("created_at") or now_iso())
        updated_at = str(payload.get("updated_at") or created_at)
        metadata = sanitize_for_audit(payload.get("metadata") if "metadata" in payload else {})
        preview_json = sanitize_for_audit(payload.get("preview_json") if "preview_json" in payload else {})
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO agent_artifacts
                    (artifact_id, user_id, session_id, run_id, trace_id, tool_call_id,
                     tool_name, kind, title, path, uri, mime_type, size_bytes,
                     sha256, preview_text, preview_json, source_id, status,
                     metadata_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    _clean_optional(payload.get("user_id")),
                    _clean_optional(payload.get("session_id")),
                    _clean_optional(payload.get("run_id")),
                    _clean_optional(payload.get("trace_id")),
                    _clean_optional(payload.get("tool_call_id")),
                    _clean_optional(payload.get("tool_name")),
                    kind,
                    title,
                    _clean_optional(payload.get("path"), limit=2000),
                    _clean_optional(payload.get("uri"), limit=2000),
                    _clean_optional(payload.get("mime_type"), limit=200),
                    _int_or_none(payload.get("size_bytes")),
                    _clean_optional(payload.get("sha256"), limit=128),
                    _bounded_text(payload.get("preview_text"), limit=4000) if payload.get("preview_text") is not None else None,
                    _dumps(preview_json),
                    _clean_optional(payload.get("source_id")),
                    status,
                    _dumps(metadata),
                    created_at,
                    updated_at,
                ),
            )
            self._index_search_row(
                conn,
                kind="artifact",
                object_id=artifact_id,
                session_id=str(payload.get("session_id") or ""),
                user_id=_clean_optional(payload.get("user_id")),
                content=" ".join(
                    str(item or "")
                    for item in (
                        title,
                        payload.get("path"),
                        payload.get("uri"),
                        payload.get("preview_text"),
                        kind,
                    )
                ).strip(),
                payload={
                    **payload,
                    "artifact_id": artifact_id,
                    "kind": kind,
                    "title": title,
                    "status": status,
                    "metadata": metadata,
                    "preview_json": preview_json,
                },
            )
            conn.commit()
        item = self.get_artifact(artifact_id)
        assert item is not None
        return item

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_sources WHERE source_id = ?",
                (str(source_id or "").strip(),),
            ).fetchone()
        return self._source_row(row)

    def get_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM agent_artifacts WHERE artifact_id = ?",
                (str(artifact_id or "").strip(),),
            ).fetchone()
        return self._artifact_row(row)

    def list_sources(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        source_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in {
            "user_id": user_id,
            "session_id": session_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "source_type": source_type,
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
                FROM agent_sources
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._source_row(row)) is not None]

    def list_artifacts(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        kind: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in {
            "user_id": user_id,
            "session_id": session_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
            "kind": kind,
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
                FROM agent_artifacts
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._artifact_row(row)) is not None]

    def record_context_snapshot(
        self,
        *,
        session_id: str,
        user_id: str | None = None,
        run_id: str | None = None,
        trace_id: str | None = None,
        context_summary_id: str | None = None,
        policy: str = "runtime_prepare",
        compacted: bool = False,
        message_count: int = 0,
        source_message_ids: list[Any] | None = None,
        source_ids: list[Any] | None = None,
        artifact_ids: list[Any] | None = None,
        token_estimate_before: int | None = None,
        token_estimate_after: int | None = None,
        summary: str | None = None,
        summary_model: str | None = None,
        risk_flags: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        sid = str(session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required")
        snap_id = str(snapshot_id or f"ctxsnap_{uuid4().hex}").strip()
        ts = now_iso()
        cleaned_source_message_ids = [str(item) for item in list(source_message_ids or []) if str(item).strip()]
        cleaned_source_ids = [str(item) for item in list(source_ids or []) if str(item).strip()]
        cleaned_artifact_ids = [str(item) for item in list(artifact_ids or []) if str(item).strip()]
        cleaned_risk_flags = [str(item)[:120] for item in list(risk_flags or []) if str(item).strip()]
        safe_metadata = sanitize_for_audit(dict(metadata or {}), max_depth=4, max_items=80, max_text=2000)
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO context_snapshots
                    (snapshot_id, user_id, session_id, run_id, trace_id, context_summary_id,
                     policy, compacted, message_count, source_message_ids_json,
                     source_ids_json, artifact_ids_json, token_estimate_before,
                     token_estimate_after, summary, summary_model, risk_flags_json,
                     metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snap_id,
                    _clean_optional(user_id),
                    sid,
                    _clean_optional(run_id),
                    _clean_optional(trace_id),
                    _clean_optional(context_summary_id),
                    str(policy or "runtime_prepare").strip()[:120] or "runtime_prepare",
                    1 if compacted else 0,
                    max(0, int(message_count or 0)),
                    _dumps(cleaned_source_message_ids),
                    _dumps(cleaned_source_ids),
                    _dumps(cleaned_artifact_ids),
                    _int_or_none(token_estimate_before),
                    _int_or_none(token_estimate_after),
                    _bounded_text(summary, limit=12000) if summary else None,
                    _clean_optional(summary_model),
                    _dumps(cleaned_risk_flags),
                    _dumps(safe_metadata),
                    ts,
                ),
            )
            conn.commit()
        item = self.get_context_snapshot(snap_id)
        assert item is not None
        return item

    def get_context_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM context_snapshots WHERE snapshot_id = ?",
                (str(snapshot_id or "").strip(),),
            ).fetchone()
        return self._context_snapshot_row(row)

    def list_context_snapshots(
        self,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in {
            "user_id": user_id,
            "session_id": session_id,
            "run_id": run_id,
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
                FROM context_snapshots
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._context_snapshot_row(row)) is not None]
