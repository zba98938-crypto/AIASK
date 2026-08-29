from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .paths import default_state_db_path
from .session_store import now_iso


class MessageOutbox:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS message_outbox (
                message_id TEXT PRIMARY KEY,
                platform TEXT NOT NULL,
                target TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                result_json TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    def send(self, *, platform: str, target: str, message: str) -> dict[str, Any]:
        if not str(platform or "").strip():
            raise ValueError("platform is required")
        if not str(target or "").strip():
            raise ValueError("target is required")
        if not str(message or "").strip():
            raise ValueError("message is required")
        message_id = f"msg_{uuid4().hex}"
        result: dict[str, Any] = {"delivered": False, "transport": "outbox"}
        status = "queued"
        webhook = str(os.getenv("AIASK_AGENT_MESSAGE_WEBHOOK_URL", "")).strip()
        if webhook:
            payload = json.dumps({"platform": platform, "target": target, "message": message}).encode("utf-8")
            request = Request(webhook, data=payload, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=15) as response:
                status_code = bounded_int(getattr(response, "status", None), default=0, minimum=0, maximum=999)
                result = {"delivered": 200 <= status_code < 300, "status": response.status, "transport": "webhook"}
                status = "sent" if result["delivered"] else "failed"
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO message_outbox
                    (message_id, platform, target, message, status, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (message_id, platform, target, message, status, json.dumps(result, ensure_ascii=False), now_iso()),
            )
            conn.commit()
        return {"message_id": message_id, "platform": platform, "target": target, "status": status, "result": result}


