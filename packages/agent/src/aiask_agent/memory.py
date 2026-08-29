from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import default_state_db_path
from .session_store import now_iso


class FinancialMemoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS financial_memories (
                memory_id TEXT PRIMARY KEY,
                user_id TEXT,
                symbol TEXT,
                strategy_id TEXT,
                research_topic TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_financial_memories_scope
            ON financial_memories(user_id, symbol, strategy_id, research_topic)
            """
        )
        conn.commit()
        return conn

    def add(
        self,
        *,
        content: str,
        user_id: str | None = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        research_topic: str | None = None,
    ) -> dict[str, Any]:
        item = {
            "memory_id": f"mem_{uuid4().hex}",
            "user_id": user_id,
            "symbol": symbol,
            "strategy_id": strategy_id,
            "research_topic": research_topic,
            "content": str(content or "").strip(),
            "created_at": now_iso(),
        }
        if not item["content"]:
            raise ValueError("memory content is required")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO financial_memories
                    (memory_id, user_id, symbol, strategy_id, research_topic, content, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["memory_id"],
                    user_id,
                    symbol,
                    strategy_id,
                    research_topic,
                    item["content"],
                    item["created_at"],
                ),
            )
            conn.commit()
        return item

    def search(
        self,
        *,
        query: str | None = None,
        user_id: str | None = None,
        symbol: str | None = None,
        strategy_id: str | None = None,
        research_topic: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        for key, value in {
            "user_id": user_id,
            "symbol": symbol,
            "strategy_id": strategy_id,
            "research_topic": research_topic,
        }.items():
            if value:
                clauses.append(f"{key} = ?")
                values.append(value)
        if query:
            clauses.append("content LIKE ?")
            values.append(f"%{query}%")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, min(int(limit or 20), 200)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT memory_id, user_id, symbol, strategy_id, research_topic, content, created_at
                FROM financial_memories
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                tuple(values),
            ).fetchall()
        return [dict(row) for row in rows]
