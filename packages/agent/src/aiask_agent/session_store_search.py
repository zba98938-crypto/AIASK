from __future__ import annotations

import sqlite3
from typing import Any

from .session_store_rows import _session_is_archived
from .session_store_utils import _dumps, _loads


def _fts_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT 1 FROM aiask_search_fts LIMIT 1").fetchone()
        return True
    except sqlite3.OperationalError:
        return False


def _index_search_row(
    conn: sqlite3.Connection,
    *,
    kind: str,
    object_id: str,
    session_id: str,
    user_id: str | None,
    content: str,
    payload: dict[str, Any],
) -> None:
    try:
        conn.execute(
            "DELETE FROM aiask_search_fts WHERE kind = ? AND object_id = ?",
            (kind, object_id),
        )
        conn.execute(
            """
            INSERT INTO aiask_search_fts (kind, object_id, session_id, user_id, content, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (kind, object_id, session_id, user_id, content, _dumps(payload)),
        )
    except sqlite3.OperationalError:
        return


def _search_row(row: sqlite3.Row) -> dict[str, Any]:
    item = dict(row)
    item["payload"] = _loads(item.pop("payload_json", None), {})
    return item


def _search_like(
    conn: sqlite3.Connection,
    *,
    token: str,
    session_id: str | None,
    limit: int,
    include_archived: bool,
) -> list[dict[str, Any]]:
    values: list[Any] = [f"%{token}%"]
    clauses = ["content LIKE ?"]
    if session_id:
        clauses.append("session_id = ?")
        values.append(session_id)
    clauses.append("deleted_at IS NULL")
    values.append(limit)
    rows = conn.execute(
        f"""
        SELECT 'message' AS kind, CAST(id AS TEXT) AS object_id, session_id, NULL AS user_id, content, payload_json
        FROM messages
        WHERE {" AND ".join(clauses)}
        ORDER BY id DESC
        LIMIT ?
        """,
        tuple(values),
    ).fetchall()
    items = [
        item
        for row in rows
        if (item := _search_row(row)) is not None
        and (include_archived or not _session_is_archived(conn, str(item.get("session_id") or "")))
    ]
    if len(items) >= limit:
        return items[:limit]
    remaining = max(1, limit - len(items))
    source_values: list[Any] = [f"%{token}%", f"%{token}%", f"%{token}%", f"%{token}%"]
    source_clauses = ["(title LIKE ? OR url LIKE ? OR provider LIKE ? OR excerpt LIKE ?)"]
    if session_id:
        source_clauses.append("session_id = ?")
        source_values.append(session_id)
    source_values.append(remaining)
    source_rows = conn.execute(
        f"""
        SELECT 'source' AS kind, source_id AS object_id, session_id, user_id,
               COALESCE(title, url, provider, source_type) AS content, metadata_json AS payload_json
        FROM agent_sources
        WHERE {" AND ".join(source_clauses)}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(source_values),
    ).fetchall()
    items.extend(
        item
        for row in source_rows
        if (item := _search_row(row)) is not None
        and (include_archived or not _session_is_archived(conn, str(item.get("session_id") or "")))
    )
    if len(items) >= limit:
        return items[:limit]
    remaining = max(1, limit - len(items))
    artifact_values: list[Any] = [f"%{token}%", f"%{token}%", f"%{token}%", f"%{token}%"]
    artifact_clauses = ["(title LIKE ? OR path LIKE ? OR uri LIKE ? OR preview_text LIKE ?)"]
    if session_id:
        artifact_clauses.append("session_id = ?")
        artifact_values.append(session_id)
    artifact_values.append(remaining)
    artifact_rows = conn.execute(
        f"""
        SELECT 'artifact' AS kind, artifact_id AS object_id, session_id, user_id,
               COALESCE(title, path, uri, kind) AS content, metadata_json AS payload_json
        FROM agent_artifacts
        WHERE {" AND ".join(artifact_clauses)}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        tuple(artifact_values),
    ).fetchall()
    items.extend(
        item
        for row in artifact_rows
        if (item := _search_row(row)) is not None
        and (include_archived or not _session_is_archived(conn, str(item.get("session_id") or "")))
    )
    return items[:limit]
