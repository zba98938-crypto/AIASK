"""Alert manager with DB-first lifecycle handling.

CRUD and check operations treat the database as the primary source of truth.
The shared ``_alerts_store`` is kept only as a best-effort process-local cache
for degraded-mode fallback and for sharing evaluated alert state with
``tools.alerts``.
"""

from __future__ import annotations

import logging
from typing import Any

from ...utils import fail, normalize_code, ok, resolve_existing_security_code_sync
from ..manager_protocol import normalize_manager_payload

logger = logging.getLogger(__name__)

_VALID_INDICATORS = ("price", "change_pct", "volume", "ma5", "ma20", "rsi", "macd")
_VALID_CONDITIONS = (">", "<", ">=", "<=", "==")


def _safe_user_id(value: object) -> str:
    text = str(value or "").strip()
    return text or "default"


def _make_alert_id(user_id: str, code: str, indicator: str, condition: str) -> str:
    normalized_user = _safe_user_id(user_id)
    if normalized_user == "default":
        return f"alert_{code}_{indicator}_{condition}"
    safe_user = normalized_user.replace(":", "_").replace("/", "_").replace(" ", "_")
    return f"alert_{safe_user}_{code}_{indicator}_{condition}"


def _belongs_to_user(alert: dict, user_id: str) -> bool:
    return _safe_user_id(alert.get("user_id")) == _safe_user_id(user_id)


def _status_from_active(active: object) -> str:
    return "active" if bool(active) else "inactive"


def _active_from_status(status: object) -> bool:
    return str(status or "active").strip().lower() != "inactive"


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _list_cached_user_alerts(alerts_store: dict, user_id: str) -> list[dict]:
    return [dict(alert) for alert in alerts_store.values() if _belongs_to_user(alert, user_id)]


def _replace_cached_user_alerts(alerts_store: dict, user_id: str, alerts: list[dict]) -> None:
    stale_ids = [aid for aid, alert in alerts_store.items() if _belongs_to_user(alert, user_id)]
    for aid in stale_ids:
        alerts_store.pop(aid, None)
    for alert in alerts:
        aid = str(alert.get("alert_id") or "").strip()
        if aid:
            alerts_store[aid] = dict(alert)


def _store_cached_alert(alerts_store: dict, alert: dict, previous_alert_id: str | None = None) -> None:
    current_id = str(alert.get("alert_id") or "").strip()
    if previous_alert_id and previous_alert_id != current_id:
        alerts_store.pop(previous_alert_id, None)
    if current_id:
        alerts_store[current_id] = dict(alert)


def _alert_from_row(row: Any) -> dict:
    user_id = _safe_user_id(_row_value(row, "user_id", "default"))
    code = normalize_code(str(_row_value(row, "code", "") or ""))
    indicator = str(_row_value(row, "indicator", "price") or "price").strip() or "price"
    condition = str(_row_value(row, "condition", ">") or ">").strip() or ">"
    value = float(_row_value(row, "value", 0) or 0)
    active = _active_from_status(_row_value(row, "status", "active"))
    return {
        "alert_id": _make_alert_id(user_id, code, indicator, condition),
        "db_id": _row_value(row, "id"),
        "user_id": user_id,
        "code": code,
        "indicator": indicator,
        "condition": condition,
        "value": value,
        "active": active,
        "type": "indicator",
        "triggered": False,
    }


async def _fetch_user_alerts_from_db(user_id: str, *, status: str | None = None) -> list[dict] | None:
    try:
        from ...storage import get_db

        db = get_db()
        async with db.acquire() as conn:
            if status in {"active", "inactive"}:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM alerts
                    WHERE user_id = $1 AND status = $2
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                    """,
                    user_id,
                    status,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT *
                    FROM alerts
                    WHERE user_id = $1
                    ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
                    """,
                    user_id,
                )
    except Exception as exc:
        logger.warning("[AlertsManager] DB load failed for user=%s status=%s: %s", user_id, status, exc)
        return None

    return [_alert_from_row(row) for row in rows]


async def _load_user_alerts(user_id: str, *, status: str | None = None) -> list[dict]:
    from ..alerts import _alerts_store

    alerts = await _fetch_user_alerts_from_db(user_id, status=status)
    if alerts is not None:
        _replace_cached_user_alerts(_alerts_store, user_id, alerts)
        return alerts

    cached = _list_cached_user_alerts(_alerts_store, user_id)
    if status == "active":
        return [alert for alert in cached if alert.get("active", True)]
    if status == "inactive":
        return [alert for alert in cached if not alert.get("active", True)]
    return cached


async def _load_alert_by_id(user_id: str, alert_id: str) -> dict | None:
    resolved_alert_id = str(alert_id or "").strip()
    if not resolved_alert_id:
        return None
    alerts = await _load_user_alerts(user_id)
    for alert in alerts:
        if str(alert.get("alert_id") or "").strip() == resolved_alert_id:
            return alert
    return None


async def _persist_new_alert(user_id: str, code: str, indicator: str, condition: str, value: float) -> dict | None:
    try:
        from ...storage import get_db

        db = get_db()
        async with db.acquire() as conn:
            fetchrow = getattr(conn, "fetchrow", None)
            if callable(fetchrow):
                row = await fetchrow(
                    """
                    INSERT INTO alerts (user_id, code, indicator, condition, value, status)
                    VALUES ($1, $2, $3, $4, $5, 'active')
                    RETURNING id, user_id, code, indicator, condition, value, status
                    """,
                    user_id,
                    code,
                    indicator,
                    condition,
                    value,
                )
                if row is not None:
                    return _alert_from_row(row)

            await conn.execute(
                """
                INSERT INTO alerts (user_id, code, indicator, condition, value, status)
                VALUES ($1, $2, $3, $4, $5, 'active')
                """,
                user_id,
                code,
                indicator,
                condition,
                value,
            )
    except Exception as exc:
        logger.warning("[AlertsManager] DB persist failed for %s/%s/%s: %s", code, indicator, condition, exc)
        return None

    return None


async def _persist_updated_alert(previous_alert: dict, updated_alert: dict) -> None:
    try:
        from ...storage import get_db

        db = get_db()
        async with db.acquire() as conn:
            db_id = updated_alert.get("db_id") or previous_alert.get("db_id")
            if db_id is not None:
                await conn.execute(
                    """
                    UPDATE alerts
                    SET code = $1,
                        indicator = $2,
                        condition = $3,
                        value = $4,
                        status = $5,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = $6
                    """,
                    updated_alert.get("code", ""),
                    updated_alert.get("indicator", ""),
                    updated_alert.get("condition", ""),
                    float(updated_alert.get("value", 0) or 0),
                    _status_from_active(updated_alert.get("active", True)),
                    db_id,
                )
                return

            await conn.execute(
                """
                UPDATE alerts
                SET code = $1,
                    indicator = $2,
                    condition = $3,
                    value = $4,
                    status = $5,
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = $6 AND code = $7 AND indicator = $8 AND condition = $9
                """,
                updated_alert.get("code", ""),
                updated_alert.get("indicator", ""),
                updated_alert.get("condition", ""),
                float(updated_alert.get("value", 0) or 0),
                _status_from_active(updated_alert.get("active", True)),
                _safe_user_id(previous_alert.get("user_id")),
                previous_alert.get("code", ""),
                previous_alert.get("indicator", ""),
                previous_alert.get("condition", ""),
            )
    except Exception as exc:
        logger.warning(
            "[AlertsManager] DB update failed for alert_id=%s: %s",
            previous_alert.get("alert_id"),
            exc,
        )


async def _delete_alert_from_db(alert: dict) -> None:
    try:
        from ...storage import get_db

        db = get_db()
        async with db.acquire() as conn:
            db_id = alert.get("db_id")
            if db_id is not None:
                await conn.execute("DELETE FROM alerts WHERE id = $1", db_id)
                return

            await conn.execute(
                """
                DELETE FROM alerts
                WHERE user_id = $1 AND code = $2 AND indicator = $3 AND condition = $4
                """,
                _safe_user_id(alert.get("user_id", "default")),
                alert.get("code", ""),
                alert.get("indicator", ""),
                alert.get("condition", ""),
            )
    except Exception as exc:
        logger.warning("[AlertsManager] DB delete failed for alert_id=%s: %s", alert.get("alert_id"), exc)


async def _delete_combo_alert_from_db(alert_id: str) -> bool:
    name = str(alert_id or "").strip()
    if not name.startswith("combo_"):
        return False
    combo_name = name[len("combo_"):]
    if not combo_name:
        return False
    try:
        from ...storage import get_db

        db = get_db()
        async with db.acquire() as conn:
            result = await conn.execute("DELETE FROM combo_alerts WHERE name = $1", combo_name)
        return str(result or "").strip().endswith("1")
    except Exception as exc:
        logger.warning("[AlertsManager] combo DB delete failed for alert_id=%s: %s", alert_id, exc)
        return False


def register_alerts_manager(mcp):
    """Register the unified alerts manager tool."""

    @mcp.tool()
    async def alerts_manager(
        action: str,
        params: dict | None = None,
        kwargs: Any = None,
        user_id: str | None = None,
        status: str | None = None,
        code: str | None = None,
        indicator: str | None = None,
        condition: str | None = None,
        value: float | None = None,
        alert_id: str | None = None,
    ):
        """Alert lifecycle manager using the unified action + kwargs protocol."""
        try:
            kwargs = normalize_manager_payload(
                params=params,
                kwargs=kwargs,
                extra={
                    "user_id": user_id,
                    "status": status,
                    "code": code,
                    "indicator": indicator,
                    "condition": condition,
                    "value": value,
                    "alert_id": alert_id,
                },
            )

            from ..alerts import _alerts_store, _evaluate_combo, _evaluate_indicator

            user_id = _safe_user_id(kwargs.get("user_id"))

            if action == "help":
                return ok(
                    {
                        "supported_actions": {
                            "list": "列出告警",
                            "create": "创建告警（需要 code, indicator, condition, value）",
                            "check": "检查告警状态",
                            "update": "更新告警（需要 alert_id）",
                            "delete": "删除告警（需要 alert_id）",
                            "help": "显示帮助信息",
                        }
                    }
                )

            if action == "list":
                resolved_status = str(kwargs.get("status", "active") or "active").strip().lower()
                alerts = await _load_user_alerts(user_id)
                if resolved_status == "active":
                    alerts = [alert for alert in alerts if alert.get("active", True)]
                elif resolved_status == "inactive":
                    alerts = [alert for alert in alerts if not alert.get("active", True)]
                return ok({"alerts": alerts, "count": len(alerts)})

            if action == "create":
                code = kwargs.get("code")
                indicator = kwargs.get("indicator")
                condition = kwargs.get("condition")
                value = kwargs.get("value")
                if not all([code, indicator, condition, value is not None]):
                    return fail("需要提供 code, indicator, condition, value")

                code, _, code_error = resolve_existing_security_code_sync(stock_code=code)
                if code_error:
                    return fail(code_error)
                indicator = str(indicator).strip()
                # P2-4.4.5 fix: condition 字段先 html.unescape(诊断报告 §4.4.5)
                # 历史问题:condition='<' 经 JSON 编码变成 '&lt;' 直接 reject
                # 修复:入口统一 unescape,接受常见 HTML 实体
                import html as _html
                condition = _html.unescape(str(condition)).strip()
                if indicator not in _VALID_INDICATORS:
                    return fail(f"不支持的指标: {indicator}. 支持: {', '.join(_VALID_INDICATORS)}")
                if condition not in _VALID_CONDITIONS:
                    return fail(f"不支持的条件: {condition}. 支持: {', '.join(_VALID_CONDITIONS)}")

                try:
                    threshold = float(value)
                except Exception:
                    return fail("value 必须是数字")

                existing_alert_id = _make_alert_id(user_id, code, indicator, condition)
                if await _load_alert_by_id(user_id, existing_alert_id):
                    return fail(f"告警已存在: {existing_alert_id}。如需修改请使用 update")

                persisted = await _persist_new_alert(user_id, code, indicator, condition, threshold)
                alert = persisted or {
                    "alert_id": existing_alert_id,
                    "user_id": user_id,
                    "code": code,
                    "indicator": indicator,
                    "condition": condition,
                    "value": threshold,
                    "active": True,
                    "type": "indicator",
                    "triggered": False,
                }
                _store_cached_alert(_alerts_store, alert)
                logger.info("[AlertsManager] created alert_id=%s", alert["alert_id"])
                return ok(
                    {
                        "alert_id": alert["alert_id"],
                        "code": alert["code"],
                        "indicator": alert["indicator"],
                        "condition": alert["condition"],
                        "value": alert["value"],
                        "status": "created",
                    }
                )

            if action == "check":
                alerts = [alert for alert in await _load_user_alerts(user_id, status="active") if alert.get("active", True)]
                triggered = []
                quote_cache: dict[str, Any] = {}

                for alert in alerts:
                    try:
                        if alert.get("type") == "combo":
                            evaluated = await _evaluate_combo(alert, quote_cache)
                        else:
                            evaluated = await _evaluate_indicator(alert, quote_cache)
                    except Exception as exc:
                        logger.debug("[AlertsManager] check failed for alert_id=%s: %s", alert.get("alert_id"), exc)
                        continue

                    evaluated["user_id"] = _safe_user_id(evaluated.get("user_id") or user_id)
                    _store_cached_alert(_alerts_store, evaluated)

                    if evaluated.get("triggered") is True:
                        code = str(evaluated.get("code") or "")
                        indicator = str(evaluated.get("indicator") or "")
                        condition = str(evaluated.get("condition") or "")
                        target_value = evaluated.get("value")
                        current_value = evaluated.get("current_value")
                        triggered.append(
                            {
                                "alert_id": evaluated.get("alert_id"),
                                "code": code,
                                "indicator": indicator,
                                "condition": condition,
                                "target_value": target_value,
                                "current_value": current_value,
                                "message": f"{code} {indicator} {condition} {target_value} (当前: {current_value})",
                            }
                        )

                return ok({"triggered": triggered, "count": len(triggered)})

            if action == "update":
                resolved_alert_id = str(kwargs.get("alert_id") or "").strip()
                if not resolved_alert_id:
                    return fail("需要提供 alert_id")

                alert = await _load_alert_by_id(user_id, resolved_alert_id)
                if not alert:
                    return fail(f"告警不存在: {resolved_alert_id}")

                updated_alert = dict(alert)

                if kwargs.get("code"):
                    normalized_code, _, code_error = resolve_existing_security_code_sync(stock_code=kwargs.get("code"))
                    if code_error:
                        return fail(code_error)
                    updated_alert["code"] = normalized_code

                if kwargs.get("indicator"):
                    candidate_indicator = str(kwargs.get("indicator")).strip()
                    if candidate_indicator not in _VALID_INDICATORS:
                        return fail(f"不支持的指标: {candidate_indicator}. 支持: {', '.join(_VALID_INDICATORS)}")
                    updated_alert["indicator"] = candidate_indicator

                if kwargs.get("condition"):
                    candidate_condition = str(kwargs.get("condition")).strip()
                    # P2-4.4.5 fix: 同 create,update 也支持 html.unescape
                    import html as _html
                    candidate_condition = _html.unescape(candidate_condition).strip()
                    if candidate_condition not in _VALID_CONDITIONS:
                        return fail(f"不支持的条件: {candidate_condition}. 支持: {', '.join(_VALID_CONDITIONS)}")
                    updated_alert["condition"] = candidate_condition

                if "value" in kwargs and kwargs.get("value") is not None:
                    try:
                        updated_alert["value"] = float(kwargs.get("value"))
                    except Exception:
                        return fail("value 必须是数字")

                if kwargs.get("status") is not None:
                    updated_alert["active"] = str(kwargs.get("status")).strip().lower() == "active"
                elif kwargs.get("active") is not None:
                    updated_alert["active"] = bool(kwargs.get("active"))

                updated_alert["user_id"] = user_id
                updated_alert["alert_id"] = _make_alert_id(
                    user_id,
                    updated_alert.get("code", ""),
                    updated_alert.get("indicator", ""),
                    updated_alert.get("condition", ""),
                )
                updated_alert["type"] = "indicator"
                updated_alert["triggered"] = False

                await _persist_updated_alert(alert, updated_alert)
                _store_cached_alert(_alerts_store, updated_alert, previous_alert_id=resolved_alert_id)

                response = {
                    "alert_id": updated_alert["alert_id"],
                    "status": _status_from_active(updated_alert.get("active", True)),
                    "alert": updated_alert,
                }
                if updated_alert["alert_id"] != resolved_alert_id:
                    response["previous_alert_id"] = resolved_alert_id
                return ok(response)

            if action == "delete":
                resolved_alert_id = str(kwargs.get("alert_id") or "").strip()
                if not resolved_alert_id:
                    return fail("需要提供 alert_id")

                if resolved_alert_id.startswith("combo_"):
                    deleted = await _delete_combo_alert_from_db(resolved_alert_id)
                    _alerts_store.pop(resolved_alert_id, None)
                    if deleted:
                        return ok({"alert_id": resolved_alert_id, "deleted": True})
                    return fail(f"告警不存在: {resolved_alert_id}")

                alert = await _load_alert_by_id(user_id, resolved_alert_id)
                if not alert:
                    return fail(f"告警不存在: {resolved_alert_id}")

                await _delete_alert_from_db(alert)
                _alerts_store.pop(resolved_alert_id, None)
                return ok({"alert_id": resolved_alert_id, "deleted": True})

            return fail(f"Unknown action: {action}. Supported: help, list, create, check, update, delete")
        except Exception as exc:
            logger.error("[AlertsManager] Error: %s", exc)
            return fail(str(exc))
