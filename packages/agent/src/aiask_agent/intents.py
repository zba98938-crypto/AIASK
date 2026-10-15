from __future__ import annotations

import inspect
import json
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .financial_safety import (
    FINANCIAL_MANAGER_CONFIRMED_ACTIONS,
    normalize_financial_confirmed_action_params,
)
from .paths import default_intent_db_path
from .session_store_utils import redact_audit_text, sanitize_for_audit
from .tool_risk import CONFIRM_REQUIRED_STRATEGY_ACTIONS
import logging

logger = logging.getLogger(__name__)


INTENT_STATUSES = (
    "awaiting_confirmation",
    "confirmed",
    "denied",
    "executing",
    "succeeded",
    "failed",
    "execution_unknown",
    "expired",
)

DEFAULT_EXECUTING_STALE_AFTER_SECONDS = 2 * 60 * 60

ALLOWED_STRATEGY_ACTIONS = set(CONFIRM_REQUIRED_STRATEGY_ACTIONS)

ALLOWED_ACTIONS = {
    f"strategy_manager.{action}": {"tool": "strategy_manager", "action": action}
    for action in sorted(ALLOWED_STRATEGY_ACTIONS)
}
ALLOWED_ACTIONS.update(
    {
        "data_sync.sync": {"tool": "data_sync", "action": "sync"},
        "data_sync.maintenance": {"tool": "data_sync", "action": "maintenance"},
        "data_sync.run_due_schedules": {"tool": "data_sync", "action": "run_due_schedules"},
        **{
            action: {"tool": "financial_manager", "action": action}
            for action in FINANCIAL_MANAGER_CONFIRMED_ACTIONS
        },
        "factor_factory.run_once": {"tool": "factor_factory", "action": "run_once"},
        "factor_factory.maintenance": {"tool": "factor_factory", "action": "maintenance"},
        "incubation_factory.run_once": {"tool": "incubation_factory", "action": "run_once"},
        "incubation_factory.dry_run": {"tool": "incubation_factory", "action": "dry_run"},
        "incubation_factory.maintenance": {"tool": "incubation_factory", "action": "maintenance"},
        "factory.run": {"tool": "factory_control", "action": "run"},
        "stock_radar.run_once": {"tool": "stock_radar", "action": "run_once"},
        "stock_radar.push_digest": {"tool": "stock_radar", "action": "push_digest"},
        "stock_radar.schedule_update": {"tool": "stock_radar", "action": "schedule_update"},
        "gateway.send_message": {"tool": "gateway", "action": "send_message"},
        "gateway.direct_deliver": {"tool": "gateway", "action": "direct_deliver"},
        "gateway.retry": {"tool": "gateway", "action": "retry"},
        "gateway.pairing_create": {"tool": "gateway", "action": "pairing_create"},
        "gateway.directory_refresh": {"tool": "gateway", "action": "directory_refresh"},
        "gateway.directory_upsert": {"tool": "gateway", "action": "directory_upsert"},
        "gateway.platform_start": {"tool": "gateway", "action": "platform_start"},
        "gateway.platform_stop": {"tool": "gateway", "action": "platform_stop"},
        "webhook.subscribe": {"tool": "webhook", "action": "subscribe"},
        "webhook.remove": {"tool": "webhook", "action": "remove"},
        "webhook.trigger": {"tool": "webhook", "action": "trigger"},
        "feishu_drive.reply_comment": {"tool": "feishu_drive", "action": "reply_comment"},
        "feishu_drive.add_comment": {"tool": "feishu_drive", "action": "add_comment"},
        "discord.channel_send": {"tool": "discord", "action": "channel_send"},
        "discord.pin_message": {"tool": "discord", "action": "pin_message"},
        "discord.unpin_message": {"tool": "discord", "action": "unpin_message"},
        "discord.create_thread": {"tool": "discord", "action": "create_thread"},
        "discord.add_role": {"tool": "discord", "action": "add_role"},
        "discord.remove_role": {"tool": "discord", "action": "remove_role"},
        "homeassistant.call_service": {"tool": "homeassistant", "action": "call_service"},
        "skill.save": {"tool": "skill", "action": "save"},
        "skill.install": {"tool": "skill", "action": "install"},
        "skill.update": {"tool": "skill", "action": "update"},
        "skill.uninstall": {"tool": "skill", "action": "uninstall"},
        "skill.pin": {"tool": "skill", "action": "pin"},
        "skill.unpin": {"tool": "skill", "action": "unpin"},
        "skill.set_enabled": {"tool": "skill", "action": "set_enabled"},
        "skill.archive": {"tool": "skill", "action": "archive"},
        "skill.restore": {"tool": "skill", "action": "restore"},
        "skill.rollback": {"tool": "skill", "action": "rollback"},
        "skill.snapshot": {"tool": "skill", "action": "snapshot"},
        "skill.install_finance_templates": {"tool": "skill", "action": "install_finance_templates"},
        "skill_pack.install": {"tool": "skill_pack", "action": "install"},
        "plugin.set_enabled": {"tool": "plugin", "action": "set_enabled"},
        "plugin.enable": {"tool": "plugin", "action": "enable"},
        "plugin.disable": {"tool": "plugin", "action": "disable"},
        "plugin.upsert": {"tool": "plugin", "action": "upsert"},
        "plugin.tool_call": {"tool": "plugin", "action": "tool_call"},
        "plugin.command_call": {"tool": "plugin", "action": "command_call"},
        "acp.register_mcp_server": {"tool": "acp", "action": "register_mcp_server"},
        "acp.remove_mcp_server": {"tool": "acp", "action": "remove_mcp_server"},
        "mcp.server_upsert": {"tool": "mcp", "action": "server_upsert"},
        "mcp.server_update": {"tool": "mcp", "action": "server_update"},
        "mcp.server_remove": {"tool": "mcp", "action": "server_remove"},
        "mcp.register_local": {"tool": "mcp", "action": "register_local"},
        "mcp.discover_update": {"tool": "mcp", "action": "discover_update"},
        "mcp.oauth_callback": {"tool": "mcp", "action": "oauth_callback"},
        "desktop_asset.strategy_create": {"tool": "desktop_asset", "action": "strategy_create"},
        "desktop_asset.strategy_update": {"tool": "desktop_asset", "action": "strategy_update"},
        "desktop_asset.strategy_delete": {"tool": "desktop_asset", "action": "strategy_delete"},
        "desktop_asset.strategy_reorder": {"tool": "desktop_asset", "action": "strategy_reorder"},
        "desktop_asset.stock_pool_create": {"tool": "desktop_asset", "action": "stock_pool_create"},
        "desktop_asset.stock_pool_update": {"tool": "desktop_asset", "action": "stock_pool_update"},
        "desktop_asset.stock_pool_delete": {"tool": "desktop_asset", "action": "stock_pool_delete"},
        "desktop_asset.stock_pool_reorder": {"tool": "desktop_asset", "action": "stock_pool_reorder"},
        "desktop_asset.stock_pool_add_stock": {"tool": "desktop_asset", "action": "stock_pool_add_stock"},
        "desktop_asset.stock_pool_remove_stock": {"tool": "desktop_asset", "action": "stock_pool_remove_stock"},
        "desktop_asset.stock_pool_batch_remove": {"tool": "desktop_asset", "action": "stock_pool_batch_remove"},
        "desktop_asset.file_upload": {"tool": "desktop_asset", "action": "file_upload"},
        "desktop_asset.file_save": {"tool": "desktop_asset", "action": "file_save"},
        "desktop_asset.file_delete": {"tool": "desktop_asset", "action": "file_delete"},
        "memory.save": {"tool": "memory", "action": "save"},
        "learning.apply": {"tool": "learning", "action": "apply"},
        "learning.reflect_skill": {"tool": "learning", "action": "reflect_skill"},
        "rl.select_environment": {"tool": "rl", "action": "select_environment"},
        "rl.edit_config": {"tool": "rl", "action": "edit_config"},
        "rl.start_training": {"tool": "rl", "action": "start_training"},
        "rl.stop_training": {"tool": "rl", "action": "stop_training"},
        "rl.test_inference": {"tool": "rl", "action": "test_inference"},
        "job.create": {"tool": "job", "action": "create"},
        "job.update": {"tool": "job", "action": "update"},
        "job.pause": {"tool": "job", "action": "pause"},
        "job.resume": {"tool": "job", "action": "resume"},
        "job.remove": {"tool": "job", "action": "remove"},
        "file.write": {"tool": "file", "action": "write"},
        "file.patch": {"tool": "file", "action": "patch"},
        "file.checkpoint": {"tool": "file", "action": "checkpoint"},
        "file.rollback": {"tool": "file", "action": "rollback"},
        "terminal.run": {"tool": "terminal", "action": "run"},
        "process.kill": {"tool": "process", "action": "kill"},
        "process.wait": {"tool": "process", "action": "wait"},
        "process.watch": {"tool": "process", "action": "watch"},
        "code.execute_python": {"tool": "code", "action": "execute_python"},
        "browser.navigate": {"tool": "browser", "action": "navigate"},
        "browser.click": {"tool": "browser", "action": "click"},
        "browser.type": {"tool": "browser", "action": "type"},
        "browser.scroll": {"tool": "browser", "action": "scroll"},
        "browser.back": {"tool": "browser", "action": "back"},
        "browser.press": {"tool": "browser", "action": "press"},
        "browser.cdp": {"tool": "browser", "action": "cdp"},
        "browser.computer_use": {"tool": "browser", "action": "computer_use"},
    }
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def _parse_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception as _swallowed:
        logger.warning("packages/agent/src/aiask_agent/intents.py:180: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _execution_unknown_result(intent: dict[str, Any], *, stale_after_seconds: int) -> dict[str, Any]:
    return {
        "object": "aiask.action_intent.execution_reconciliation",
        "status": "execution_unknown",
        "reason": "executing intent exceeded stale timeout before a final result was recorded",
        "stale_after_seconds": max(1, int(stale_after_seconds or DEFAULT_EXECUTING_STALE_AFTER_SECONDS)),
        "intent_id": str(intent.get("intent_id") or ""),
        "target_tool": str(intent.get("target_tool") or ""),
        "target_action": str(intent.get("target_action") or ""),
        "previous_status": "executing",
        "reconciliation_required": True,
    }


def _is_stale_executing(intent: dict[str, Any], *, stale_after_seconds: int) -> bool:
    if str(intent.get("status") or "") != "executing":
        return False
    updated_at = _parse_datetime(intent.get("updated_at"))
    if updated_at is None:
        return False
    stale_after = max(1, int(stale_after_seconds or DEFAULT_EXECUTING_STALE_AFTER_SECONDS))
    return updated_at + timedelta(seconds=stale_after) < now_utc()


def normalize_action(action: str) -> tuple[str, dict[str, str]]:
    token = str(action or "").strip()
    if token in ALLOWED_ACTIONS:
        return token, dict(ALLOWED_ACTIONS[token])
    prefixed = f"strategy_manager.{token}"
    if prefixed in ALLOWED_ACTIONS:
        return prefixed, dict(ALLOWED_ACTIONS[prefixed])
    raise ValueError(f"action is not allowed for confirmation execution: {token}")


def public_intent(intent: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a model/API-safe intent view without mutating stored execution params."""

    if intent is None:
        return None
    public = dict(intent)
    for key in ("params", "result"):
        if key in public:
            public[key] = sanitize_for_audit(public.get(key), max_depth=5, max_items=80, max_text=2000)
            if isinstance(public[key], dict):
                public[key] = {
                    param_key: param_value
                    for param_key, param_value in public[key].items()
                    if not str(param_key).startswith("_")
                }
    for key in ("rationale", "error"):
        if public.get(key) is not None:
            public[key] = redact_audit_text(public.get(key), limit=2000)
    public["secrets_redacted"] = True
    return public


def public_intents(intents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for intent in intents if (item := public_intent(intent)) is not None]


def _scope_blocked_result(
    intent: dict[str, Any],
    *,
    reason: str,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any]:
    target_tool = str(intent.get("target_tool") or "")
    target_action = str(intent.get("target_action") or "")
    return {
        "success": False,
        "data": {
            "intent_id": str(intent.get("intent_id") or ""),
            "action": str(intent.get("action") or ""),
            "target_tool": target_tool,
            "target_action": target_action,
            "expected": dict(expected or {}),
            "reason": reason,
        },
        "error": reason,
        "error_code": "INTENT_EXECUTION_SCOPE_BLOCKED",
        "meta": {
            "side_effect": {
                "level": "stateful",
                "target": f"{target_tool}.{target_action}".strip(".") or "action_intent",
                "confirmation_required": True,
                "idempotent": False,
            },
            "action_intent_guard": {
                "blocked": True,
                "reason": "execution_scope_mismatch",
            },
        },
        "secrets_redacted": True,
    }


def _validated_execution_params(intent: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    action_key = str(intent.get("action") or "").strip()
    try:
        _normalized_action, expected = normalize_action(action_key)
    except ValueError as exc:
        return {}, _scope_blocked_result(intent, reason=str(exc))

    target_tool = str(intent.get("target_tool") or "").strip()
    target_action = str(intent.get("target_action") or "").strip()
    if target_tool != expected["tool"] or target_action != expected["action"]:
        return {}, _scope_blocked_result(
            intent,
            reason="stored intent target no longer matches the allowed action scope",
            expected=expected,
        )

    params = dict(intent.get("params") or {})
    if expected["tool"] == "financial_manager":
        try:
            params = normalize_financial_confirmed_action_params(expected["action"], params)
        except ValueError as exc:
            return {}, _scope_blocked_result(
                intent,
                reason=str(exc),
                expected=expected,
            )
    return params, None


@dataclass(frozen=True)
class IntentTransition:
    changed: bool
    intent: dict[str, Any] | None


class ActionIntentStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_intent_db_path()

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
            CREATE TABLE IF NOT EXISTS action_intents (
                intent_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                target_tool TEXT NOT NULL,
                target_action TEXT NOT NULL,
                params_json TEXT NOT NULL,
                status TEXT NOT NULL,
                user_id TEXT,
                rationale TEXT,
                result_json TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                confirmed_at TEXT,
                denied_at TEXT,
                executed_at TEXT
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_action_intents_status ON action_intents(status, expires_at)"
        )
        conn.commit()

    @staticmethod
    def _row_to_intent(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["params"] = _loads(item.pop("params_json", None), {})
        item["result"] = _loads(item.pop("result_json", None), None)
        return item

    def create(
        self,
        *,
        action: str,
        params: dict[str, Any] | None = None,
        user_id: str | None = None,
        rationale: str | None = None,
        ttl_seconds: int = 24 * 60 * 60,
    ) -> dict[str, Any]:
        action_key, target = normalize_action(action)
        normalized_params = dict(params or {})
        if target["tool"] == "financial_manager":
            normalized_params = normalize_financial_confirmed_action_params(action_key, normalized_params)
        ts = now_utc()
        intent_id = f"intent_{ts.strftime('%Y%m%dT%H%M%S')}_{uuid4().hex[:10]}"
        expires_at = ts + timedelta(seconds=max(60, int(ttl_seconds or 86400)))
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO action_intents
                    (intent_id, action, target_tool, target_action, params_json, status,
                     user_id, rationale, created_at, updated_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    intent_id,
                    action_key,
                    target["tool"],
                    target["action"],
                    _dumps(normalized_params),
                    "awaiting_confirmation",
                    user_id,
                    rationale,
                    ts.isoformat(),
                    ts.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            conn.commit()
        return self.get(intent_id) or {"intent_id": intent_id, "status": "awaiting_confirmation"}

    def get(self, intent_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT * FROM action_intents WHERE intent_id = ?",
                (str(intent_id or "").strip(),),
            ).fetchone()
        item = self._row_to_intent(row)
        if item and item.get("status") == "awaiting_confirmation":
            expires_at = datetime.fromisoformat(str(item["expires_at"]))
            if expires_at < now_utc():
                # Use transition() (guarded UPDATE) instead of update_status(),
                # which would re-enter get() and recurse infinitely on an
                # already-expired intent (the row stays awaiting_confirmation
                # until the UPDATE lands). transition() flips the row directly
                # and returns the refreshed (now "expired") intent.
                transition = self.transition(
                    item["intent_id"],
                    expected_status="awaiting_confirmation",
                    next_status="expired",
                    error="intent expired",
                )
                return transition.intent if transition.intent is not None else item
        if item and _is_stale_executing(item, stale_after_seconds=DEFAULT_EXECUTING_STALE_AFTER_SECONDS):
            transition = self.transition(
                item["intent_id"],
                expected_status="executing",
                next_status="execution_unknown",
                result=_execution_unknown_result(
                    item,
                    stale_after_seconds=DEFAULT_EXECUTING_STALE_AFTER_SECONDS,
                ),
                error="intent execution result is unknown after stale executing timeout",
            )
            return transition.intent if transition.intent is not None else item
        return item

    def list(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self.reconcile_stale_executing()
        clauses: list[str] = []
        values: list[Any] = []
        normalized_status = str(status or "").strip()
        if normalized_status:
            clauses.append("status = ?")
            values.append(normalized_status)
        values.append(max(1, min(int(limit or 100), 500)))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"SELECT * FROM action_intents {where} ORDER BY updated_at DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [item for row in rows if (item := self._row_to_intent(row)) is not None]

    def reconcile_stale_executing(
        self,
        *,
        stale_after_seconds: int = DEFAULT_EXECUTING_STALE_AFTER_SECONDS,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit or 100), 1000))
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT * FROM action_intents
                WHERE status = 'executing'
                ORDER BY updated_at ASC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        reconciled: list[dict[str, Any]] = []
        for row in rows:
            intent = self._row_to_intent(row)
            if not intent or not _is_stale_executing(intent, stale_after_seconds=stale_after_seconds):
                continue
            transition = self.transition(
                intent["intent_id"],
                expected_status="executing",
                next_status="execution_unknown",
                result=_execution_unknown_result(intent, stale_after_seconds=stale_after_seconds),
                error="intent execution result is unknown after stale executing timeout",
            )
            if transition.changed and transition.intent is not None:
                reconciled.append(transition.intent)
        return reconciled

    def transition(
        self,
        intent_id: str,
        *,
        expected_status: str,
        next_status: str,
        result: Any = None,
        error: str | None = None,
    ) -> IntentTransition:
        if expected_status not in INTENT_STATUSES or next_status not in INTENT_STATUSES:
            raise ValueError("invalid intent status")
        ts = now_iso()
        fields = ["status = ?", "updated_at = ?"]
        values: list[Any] = [next_status, ts]
        if result is not None:
            fields.append("result_json = ?")
            values.append(_dumps(result))
        if error is not None:
            fields.append("error = ?")
            values.append(error)
        if next_status == "confirmed":
            fields.append("confirmed_at = ?")
            values.append(ts)
        if next_status == "denied":
            fields.append("denied_at = ?")
            values.append(ts)
        if next_status in {"succeeded", "failed", "execution_unknown"}:
            fields.append("executed_at = ?")
            values.append(ts)
        values.extend([str(intent_id or "").strip(), expected_status])
        with closing(self._connect()) as conn:
            cur = conn.execute(
                f"""
                UPDATE action_intents
                SET {", ".join(fields)}
                WHERE intent_id = ? AND status = ?
                """,
                tuple(values),
            )
            conn.commit()
            changed = cur.rowcount == 1
        return IntentTransition(changed=changed, intent=self.get(intent_id))

    def update_status(
        self,
        intent_id: str,
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> dict[str, Any] | None:
        current = self.get(intent_id)
        if current is None:
            return None
        return self.transition(
            intent_id,
            expected_status=str(current["status"]),
            next_status=status,
            result=result,
            error=error,
        ).intent

    def finalize_execution(
        self,
        intent_id: str,
        status: str,
        *,
        result: Any = None,
        error: str | None = None,
    ) -> IntentTransition:
        if status not in {"succeeded", "failed"}:
            raise ValueError("execution finalization status must be succeeded or failed")
        return self.transition(
            intent_id,
            expected_status="executing",
            next_status=status,
            result=result,
            error=error,
        )


ConfirmedActionExecutor = Callable[[str, str, dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


class IntentExecutor:
    def __init__(
        self,
        store: ActionIntentStore | None = None,
        confirmed_action_executor: ConfirmedActionExecutor | None = None,
    ) -> None:
        self.store = store or ActionIntentStore()
        self._confirmed_action_executor = confirmed_action_executor

    def set_confirmed_action_executor(self, executor: ConfirmedActionExecutor | None) -> None:
        self._confirmed_action_executor = executor

    async def _execute_confirmed_action(
        self,
        target_tool: str,
        target_action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if self._confirmed_action_executor is not None:
            result = self._confirmed_action_executor(target_tool, target_action, params)
            if inspect.isawaitable(result):
                result = await result
            return result
        from .adapters.confirmed_actions import execute_confirmed_action

        return await execute_confirmed_action(target_tool, target_action, params)

    async def confirm(self, intent_id: str) -> dict[str, Any]:
        current = self.store.get(intent_id)
        if current is None:
            return {
                "success": False,
                "error": redact_audit_text(f"intent not found: {intent_id}"),
                "error_code": "NOT_FOUND",
                "secrets_redacted": True,
            }

        current_status = str(current.get("status") or "")
        if current_status == "awaiting_confirmation":
            transition = self.store.transition(
                intent_id,
                expected_status="awaiting_confirmation",
                next_status="confirmed",
            )
            if not transition.changed or transition.intent is None:
                return {
                    "success": False,
                    "error": redact_audit_text(f"intent cannot be confirmed from status={current.get('status')}"),
                    "error_code": "INVALID_STATUS",
                    "data": {"intent": public_intent(current)},
                    "secrets_redacted": True,
                }
            confirmed_intent = transition.intent
        elif current_status == "confirmed":
            confirmed_intent = current
        else:
            return {
                "success": False,
                "error": redact_audit_text(f"intent cannot be confirmed from status={current.get('status')}"),
                "error_code": "INVALID_STATUS",
                "data": {"intent": public_intent(current)},
                "secrets_redacted": True,
            }

        execution_params, scope_error = _validated_execution_params(confirmed_intent)
        if scope_error is not None:
            final_transition = self.store.transition(
                intent_id,
                expected_status="confirmed",
                next_status="failed",
                result=scope_error,
                error=str(scope_error.get("error") or "intent execution scope blocked"),
            )
            final_intent = final_transition.intent or self.store.get(intent_id) or confirmed_intent
            return {
                "success": False,
                "error": redact_audit_text(scope_error.get("error") or "intent execution scope blocked"),
                "error_code": "INTENT_EXECUTION_SCOPE_BLOCKED",
                "data": {
                    "intent": public_intent(final_intent),
                    "execution_result": sanitize_for_audit(scope_error, max_depth=5, max_items=80, max_text=2000),
                },
                "secrets_redacted": True,
            }

        executing = self.store.transition(intent_id, expected_status="confirmed", next_status="executing")
        if not executing.changed or executing.intent is None:
            current_after_confirm = executing.intent or self.store.get(intent_id) or confirmed_intent
            return {
                "success": False,
                "error": redact_audit_text(
                    f"intent cannot enter executing from status={current_after_confirm.get('status') if current_after_confirm else 'unknown'}"
                ),
                "error_code": "INVALID_STATUS",
                "data": {"intent": public_intent(current_after_confirm)},
                "secrets_redacted": True,
            }
        intent = executing.intent
        try:
            if str(intent.get("target_tool") or "") == "factory_control":
                execution_params = {
                    **execution_params,
                    "_intent_id": intent_id,
                    "_intent_db_path": str(self.store.path),
                }
            result = await self._execute_confirmed_action(
                str(intent["target_tool"]),
                str(intent["target_action"]),
                execution_params,
            )
        except Exception as exc:
            final_transition = self.store.finalize_execution(intent_id, "failed", error=str(exc))
            final_intent = final_transition.intent or self.store.get(intent_id)
            safe_error = redact_audit_text(str(exc))
            return {
                "success": False,
                "error": safe_error,
                "error_code": "EXECUTION_FAILED" if final_transition.changed else "INVALID_STATUS",
                "data": {"intent": public_intent(final_intent)},
                "secrets_redacted": True,
            }

        success = not isinstance(result, dict) or result.get("success") is not False
        final_transition = self.store.finalize_execution(
            intent_id,
            "succeeded" if success else "failed",
            result=result,
            error=None if success else str(result.get("error") if isinstance(result, dict) else "execution failed"),
        )
        final_intent = final_transition.intent or self.store.get(intent_id)
        if not final_transition.changed:
            return {
                "success": False,
                "error": redact_audit_text(f"intent execution result could not be finalized from status={final_intent.get('status') if final_intent else 'unknown'}"),
                "error_code": "INVALID_STATUS",
                "data": {
                    "intent": public_intent(final_intent),
                    "execution_result": sanitize_for_audit(result, max_depth=5, max_items=80, max_text=2000),
                },
                "secrets_redacted": True,
            }
        return {
            "success": success,
            "data": {
                "intent": public_intent(final_intent),
                "execution_result": sanitize_for_audit(result, max_depth=5, max_items=80, max_text=2000),
            },
            "error": None if success else redact_audit_text("intent execution failed"),
            "secrets_redacted": True,
        }

    async def deny(self, intent_id: str, reason: str | None = None) -> dict[str, Any]:
        current = self.store.get(intent_id)
        if current is None:
            return {
                "success": False,
                "error": redact_audit_text(f"intent not found: {intent_id}"),
                "error_code": "NOT_FOUND",
                "secrets_redacted": True,
            }
        transition = self.store.transition(
            intent_id,
            expected_status="awaiting_confirmation",
            next_status="denied",
            error=reason or "denied",
        )
        if not transition.changed:
            return {
                "success": False,
                "error": redact_audit_text(f"intent cannot be denied from status={current.get('status')}"),
                "error_code": "INVALID_STATUS",
                "data": {"intent": public_intent(current)},
                "secrets_redacted": True,
            }
        return {
            "success": True,
            "data": {"intent": public_intent(transition.intent)},
            "error": None,
            "secrets_redacted": True,
        }
