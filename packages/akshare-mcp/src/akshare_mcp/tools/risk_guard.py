"""高风险工具确认门禁与最小审计工具。"""

from __future__ import annotations

import datetime as _dt
import inspect
import json
import os
from pathlib import Path
from functools import wraps
from typing import Any, Callable

from .manager_protocol import generate_audit_event_id


def _is_true(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def require_confirmation_if_needed(action: str, confirm_token: str | None = None) -> tuple[bool, str]:
    """按环境开关判断是否需要确认 token。"""
    required = _is_true(os.getenv("AKSHARE_REQUIRE_CONFIRMATION"), default=False)
    if not required:
        return True, ""

    expected = (os.getenv("AKSHARE_CONFIRM_TOKEN") or "").strip()
    provided = (confirm_token or "").strip()
    if expected:
        if provided and provided == expected:
            return True, ""
        return False, "需要确认：请提供有效 confirm_token"

    # 未配置预期 token 时，采用固定短语，避免误触发。
    if provided == "I_UNDERSTAND_THE_RISK":
        return True, ""
    return False, "需要确认：请提供 confirm_token=I_UNDERSTAND_THE_RISK"


def audit_event(
    action: str,
    params: dict[str, Any],
    result: dict[str, Any],
    reason: str | None = None,
    audit_event_id: str | None = None,
) -> str | None:
    """最小审计：JSONL 追加写入。返回 audit_event_id。"""
    resolved_id = audit_event_id or generate_audit_event_id("risk_guard", action)
    try:
        log_path = os.getenv("AKSHARE_AUDIT_LOG", "logs/risk_audit.jsonl")
        path = Path(log_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": _dt.datetime.now().isoformat(),
            "audit_event_id": resolved_id,
            "action": action,
            "params": params,
            "success": bool(result.get("success")) if isinstance(result, dict) else False,
            "reason": reason,
            "result": result,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        # 审计不应影响主流程
        pass
    return resolved_id


def risk_audited(action: str) -> Callable[[Callable[..., dict]], Callable[..., dict]]:
    """装饰器：统一确认门禁 + 审计记录。"""

    def _decorator(func: Callable[..., dict]) -> Callable[..., dict]:
        sig = inspect.signature(func)

        @wraps(func)
        def _wrapper(*args: Any, **kwargs: Any) -> dict:
            bound = sig.bind_partial(*args, **kwargs)
            params = {k: v for k, v in bound.arguments.items() if k != "confirm_token"}
            confirm_token = bound.arguments.get("confirm_token")

            allowed, message = require_confirmation_if_needed(action, confirm_token)
            if not allowed:
                rejected = {"success": False, "message": message}
                event_id = audit_event(action, params, rejected, reason="confirmation_required")
                rejected["audit_event_id"] = event_id
                return rejected

            result = func(*args, **kwargs)
            if isinstance(result, dict):
                event_id = audit_event(action, params, result)
                result.setdefault("audit_event_id", event_id)
                return result

            wrapped = {"success": False, "message": "工具返回格式异常"}
            event_id = audit_event(action, params, wrapped, reason="invalid_result")
            wrapped["audit_event_id"] = event_id
            return wrapped

        return _wrapper

    return _decorator


def risk_audited_async(action: str) -> Callable:
    """Async decorator: confirmation gate + audit record for async tool functions."""

    def _decorator(func: Callable) -> Callable:
        sig = inspect.signature(func)

        @wraps(func)
        async def _wrapper(*args: Any, **kwargs: Any) -> dict:
            bound = sig.bind_partial(*args, **kwargs)
            params = {k: v for k, v in bound.arguments.items() if k != "confirm_token"}
            confirm_token = bound.arguments.get("confirm_token")

            allowed, message = require_confirmation_if_needed(action, confirm_token)
            if not allowed:
                rejected = {"success": False, "message": message}
                event_id = audit_event(action, params, rejected, reason="confirmation_required")
                rejected["audit_event_id"] = event_id
                return rejected

            result = await func(*args, **kwargs)
            if isinstance(result, dict):
                event_id = audit_event(action, params, result)
                result.setdefault("audit_event_id", event_id)
                return result

            wrapped = {"success": False, "message": "工具返回格式异常"}
            event_id = audit_event(action, params, wrapped, reason="invalid_result")
            wrapped["audit_event_id"] = event_id
            return wrapped

        return _wrapper

    return _decorator

