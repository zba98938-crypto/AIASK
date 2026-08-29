from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .paths import default_state_db_path
from .session_store import now_iso


VALID_STATUSES = {"pending", "in_progress", "completed", "cancelled"}


class FinancialTodoStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS todos (
                session_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                user_id TEXT,
                content TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, item_id)
            )
            """
        )
        conn.commit()
        return conn

    @staticmethod
    def _normalize_item(item: dict[str, Any]) -> dict[str, str]:
        item_id = str(item.get("id") or item.get("item_id") or "").strip() or "?"
        content = str(item.get("content") or "").strip() or "(no description)"
        status = str(item.get("status") or "pending").strip().lower()
        if status not in VALID_STATUSES:
            status = "pending"
        return {"item_id": item_id, "content": content, "status": status}

    def set_items(
        self,
        *,
        session_id: str,
        items: list[dict[str, Any]],
        user_id: str | None = None,
        merge: bool = False,
    ) -> list[dict[str, Any]]:
        normalized = [self._normalize_item(item) for item in items]
        with closing(self._connect()) as conn:
            if not merge:
                conn.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
            for item in normalized:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO todos
                        (session_id, item_id, user_id, content, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        item["item_id"],
                        user_id,
                        item["content"],
                        item["status"],
                        now_iso(),
                    ),
                )
            conn.commit()
        return self.list_items(session_id=session_id)

    def list_items(self, *, session_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT item_id, content, status, updated_at
                FROM todos
                WHERE session_id = ?
                ORDER BY rowid ASC
                """,
                (session_id,),
            ).fetchall()
        return [dict(row) for row in rows]
