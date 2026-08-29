from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import default_state_db_path
from .session_store import now_iso
from .session_store_utils import redact_audit_text, sanitize_for_audit


DANGEROUS_COMMAND_RE = re.compile(
    r"(^|\s)(sudo|su|rm\s+-[^\n]*[rf]|mkfs|dd\s+if=|shutdown|reboot|killall|chmod\s+-R|chown\s+-R)\b",
    re.IGNORECASE,
)


class ApprovalStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                tool_name TEXT NOT NULL,
                action TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    def create(self, *, tool_name: str, action: str, arguments: dict[str, Any], reason: str | None = None) -> dict[str, Any]:
        approval_id = f"approval_{uuid4().hex}"
        ts = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO approvals
                    (approval_id, tool_name, action, arguments_json, status, reason, result_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (approval_id, tool_name, action, json.dumps(arguments, ensure_ascii=False, sort_keys=True), "pending", reason, "{}", ts, ts),
            )
            conn.commit()
        item = self.get(approval_id)
        assert item is not None
        return item

    def get(self, approval_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM approvals WHERE approval_id = ?",
                (str(approval_id or "").strip(),),
            ).fetchone()
        return self._row(row)

    def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        values: list[Any] = []
        where = ""
        if status:
            where = "WHERE status = ?"
            values.append(status)
        values.append(max(1, min(int(limit or 100), 1000)))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM approvals {where} ORDER BY created_at DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def decide(self, approval_id: str, *, approved: bool, reason: str | None = None, result: dict[str, Any] | None = None) -> dict[str, Any] | None:
        status = "approved" if approved else "denied"
        with closing(self._connect()) as conn:
            conn.execute(
                "UPDATE approvals SET status = ?, reason = ?, result_json = ?, updated_at = ? WHERE approval_id = ?",
                (status, reason, json.dumps(dict(result or {}), ensure_ascii=False, sort_keys=True), now_iso(), str(approval_id or "").strip()),
            )
            conn.commit()
        return self.get(approval_id)

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        for key, default in (("arguments_json", {}), ("result_json", {})):
            raw = item.pop(key, None)
            try:
                item[key.removesuffix("_json")] = json.loads(raw or "{}")
            except Exception:
                item[key.removesuffix("_json")] = default
        return item


def command_requires_approval(command: str) -> bool:
    if str(command or "").strip().lower().startswith("aiask-allow "):
        return False
    return bool(DANGEROUS_COMMAND_RE.search(str(command or "")))


def public_approval(approval: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return an API/model-safe approval view without mutating stored values."""

    if approval is None:
        return None
    public = dict(approval)
    for key in ("arguments", "result"):
        if key in public:
            public[key] = sanitize_for_audit(public.get(key), max_depth=5, max_items=80, max_text=2000)
    if public.get("reason") is not None:
        public["reason"] = redact_audit_text(public.get("reason"), limit=2000)
    public["secrets_redacted"] = True
    return public


def public_approvals(approvals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for approval in approvals if (item := public_approval(approval)) is not None]
