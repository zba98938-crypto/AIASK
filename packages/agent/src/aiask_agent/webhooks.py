from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import default_state_db_path
from .session_store import now_iso


class WebhookStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_state_db_path()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS webhook_subscriptions (
                webhook_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                events_json TEXT NOT NULL,
                prompt TEXT NOT NULL,
                deliver TEXT NOT NULL,
                secret TEXT,
                enabled INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
        return conn

    def subscribe(
        self,
        *,
        name: str,
        events: list[str],
        prompt: str,
        deliver: str = "desktop_inbox",
        secret: str | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        if not str(name or "").strip():
            raise ValueError("webhook name is required")
        if not str(prompt or "").strip():
            raise ValueError("webhook prompt is required")
        webhook_id = f"wh_{uuid4().hex}"
        ts = now_iso()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO webhook_subscriptions
                    (webhook_id, name, events_json, prompt, deliver, secret, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    webhook_id,
                    name,
                    json.dumps([str(item) for item in events if str(item).strip()], ensure_ascii=False),
                    prompt,
                    deliver or "desktop_inbox",
                    secret,
                    1 if enabled else 0,
                    ts,
                    ts,
                ),
            )
            conn.commit()
        item = self.get(webhook_id)
        assert item is not None
        return item

    def get(self, webhook_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM webhook_subscriptions WHERE webhook_id = ?",
                (str(webhook_id or "").strip(),),
            ).fetchone()
        return self._row(row)

    def list(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM webhook_subscriptions ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit or 100), 1000)),),
            ).fetchall()
        return [item for row in rows if (item := self._row(row)) is not None]

    def remove(self, webhook_id: str) -> bool:
        with closing(self._connect()) as conn:
            cur = conn.execute("DELETE FROM webhook_subscriptions WHERE webhook_id = ?", (str(webhook_id or "").strip(),))
            conn.commit()
            return cur.rowcount > 0

    def render_trigger(self, webhook_id: str, *, event: str, payload: dict[str, Any], signature: str | None = None) -> dict[str, Any]:
        item = self.get(webhook_id)
        if not item:
            raise ValueError(f"webhook not found: {webhook_id}")
        if not item["enabled"]:
            raise PermissionError("webhook is disabled")
        if item["events"] and event not in item["events"]:
            raise PermissionError(f"event is not subscribed: {event}")
        secret = str(item.get("secret") or "")
        if secret:
            expected = hmac.new(secret.encode("utf-8"), json.dumps(payload, sort_keys=True).encode("utf-8"), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(str(signature or ""), expected):
                raise PermissionError("invalid webhook signature")
        deliver_value: Any = item["deliver"]
        if isinstance(deliver_value, str) and deliver_value.strip().startswith("{"):
            try:
                deliver_value = json.loads(deliver_value)
            except Exception:
                pass
        return {
            "webhook": {k: v for k, v in item.items() if k != "secret"},
            "event": event,
            "prompt": item["prompt"].format(event=event, payload=json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            "payload": payload,
            "deliver": deliver_value,
        }

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        try:
            item["events"] = json.loads(item.pop("events_json", "[]") or "[]")
        except Exception:
            item["events"] = []
        item["enabled"] = bool(item["enabled"])
        item["secret_configured"] = bool(item.get("secret"))
        return item
