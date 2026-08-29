from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import default_quant_research_db_path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def research_id() -> str:
    return f"research_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:10]}"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def quant_research_db_path(state_path: Path | str | None = None) -> Path:
    if state_path:
        return Path(state_path).expanduser().parent / "quant_research.sqlite3"
    return default_quant_research_db_path()


class QuantResearchStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = quant_research_db_path(path)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        self._ensure_schema(conn)
        return conn

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quant_research_runs (
                research_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                report_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_quant_research_runs_updated ON quant_research_runs(updated_at)"
        )
        conn.commit()

    @staticmethod
    def _row_to_item(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json", None), {})
        item["report"] = _loads(item.pop("report_json", None), {})
        return item

    def upsert(self, *, research_id: str, status: str, payload: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
        ts = now_iso()
        with closing(self._connect()) as conn:
            existing = conn.execute(
                "SELECT created_at FROM quant_research_runs WHERE research_id = ?",
                (research_id,),
            ).fetchone()
            created_at = str(existing["created_at"]) if existing else ts
            conn.execute(
                """
                INSERT INTO quant_research_runs
                    (research_id, status, payload_json, report_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(research_id) DO UPDATE SET
                    status = excluded.status,
                    payload_json = excluded.payload_json,
                    report_json = excluded.report_json,
                    updated_at = excluded.updated_at
                """,
                (research_id, status, _dumps(payload), _dumps(report), created_at, ts),
            )
            conn.commit()
        return self.get(research_id) or {
            "research_id": research_id,
            "status": status,
            "payload": payload,
            "report": report,
            "created_at": created_at,
            "updated_at": ts,
        }

    def get(self, research_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM quant_research_runs WHERE research_id = ?",
                (str(research_id or "").strip(),),
            ).fetchone()
        return self._row_to_item(row)

    def report(self, research_id: str) -> dict[str, Any] | None:
        item = self.get(research_id)
        if item is None:
            return None
        report = dict(item.get("report") or {})
        report.setdefault("research_id", item["research_id"])
        report.setdefault("status", item["status"])
        report.setdefault("updated_at", item["updated_at"])
        return report

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM quant_research_runs ORDER BY updated_at DESC LIMIT ?",
                (max(1, min(int(limit or 50), 500)),),
            ).fetchall()
        return [item for row in rows if (item := self._row_to_item(row)) is not None]
