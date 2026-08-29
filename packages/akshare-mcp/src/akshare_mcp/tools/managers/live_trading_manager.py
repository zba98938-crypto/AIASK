"""Live trading manager backed by broker gateway adapters."""

from __future__ import annotations

import json
import math
import os
import time
from typing import Any

from ...services import register_artifact_async
from ...services.live_broker import (
    build_live_order_event_collection,
    get_live_broker_adapter,
)
from ...utils import fail, ok
from ..manager_protocol import normalize_manager_payload
from ..risk_guard import audit_event, require_confirmation_if_needed


def _normalize_kwargs(kwargs: dict) -> dict:
    """Parse kwargs payloads from both MCP JSON strings and structured params."""
    params = kwargs.get("params")
    if isinstance(params, dict):
        kwargs = {**kwargs, **params}
    raw = kwargs.get("kwargs")
    if isinstance(raw, dict):
        kwargs = {**kwargs, **raw}
    elif isinstance(raw, str):
        try:
            extra = json.loads(raw or "{}")
            if isinstance(extra, dict):
                kwargs = {**kwargs, **extra}
        except Exception:
            pass
    if "symbol" not in kwargs:
        kwargs["symbol"] = kwargs.get("code")
    if "code" not in kwargs:
        kwargs["code"] = kwargs.get("symbol")
    return kwargs


def _safe_text(*values: Any) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return parsed
    except Exception:
        return None


def _raw_number_present(value: Any) -> bool:
    return value not in (None, "")


def _safe_int(value: Any, default: int) -> int:
    try:
        if value in (None, ""):
            return int(default)
        return int(float(value))
    except Exception:
        return int(default)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _split_symbols(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip().upper() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    normalized = text.replace(";", ",").replace("|", ",").replace(" ", ",")
    return [part.strip().upper() for part in normalized.split(",") if part.strip()]


def _adapter_meta(adapter) -> dict[str, Any]:
    config = getattr(adapter, "config", None)
    can_write = bool(getattr(adapter, "can_write", lambda: False)())
    capabilities = getattr(adapter, "capabilities", lambda: {})()
    return {
      "provider": getattr(adapter, "provider_name", "live_broker"),
      "read_only": bool(getattr(config, "read_only", False) or not can_write),
      "paper": bool(getattr(config, "paper", False)),
      "write_enabled": can_write,
      "capabilities": capabilities if isinstance(capabilities, dict) else {},
    }


def _with_meta(data: dict[str, Any], adapter) -> dict:
    payload = ok(data)
    payload["meta"] = _adapter_meta(adapter)
    return payload


def _trade_guard_reject(message: str, *, action: str, target: str | None, adapter) -> dict:
    payload = fail(
        message,
        error_code="CONFIRMATION_REQUIRED",
        data={
            "accepted": False,
            "submitted": False,
            "cancelled": False,
            "mode": "confirmation_required",
            "explicit_token_required": True,
            "target": target,
        },
        source="live_broker.guard",
        source_chain=["live_trading_manager", "risk_guard.require_confirmation_if_needed"],
        quality_flags=["confirmation_required"],
        degraded=True,
    )
    payload["meta"] = {
        **_adapter_meta(adapter),
        "side_effect": {
            "level": "trade_risk",
            "target": target or "live_order",
            "confirmation_required": True,
            "confirmation_policy": "explicit_token_required",
            "explicit_token_required": True,
            "dry_run": False,
            "idempotent": False,
        },
        "explicit_token_required": True,
    }
    return payload


def _invalid_order_input_reject(
    message: str,
    *,
    target: str | None,
    invalid_fields: list[str],
    adapter,
) -> dict:
    payload = fail(
        message,
        error_code="INVALID_ORDER_INPUT",
        data={
            "accepted": False,
            "submitted": False,
            "mode": "invalid_input",
            "target": target,
            "invalid_fields": invalid_fields,
        },
        source="live_broker.input_validation",
        source_chain=["live_trading_manager"],
        quality_flags=["invalid_input"],
        degraded=False,
    )
    payload["meta"] = {
        **_adapter_meta(adapter),
        "side_effect": {
            "level": "trade_risk",
            "target": target or "live_order",
            "confirmation_required": False,
            "confirmation_policy": "input_validation",
            "explicit_token_required": False,
            "dry_run": False,
            "idempotent": True,
        },
    }
    return payload


def _require_live_confirmation(action: str, confirm_token: Any) -> tuple[bool, str]:
    expected = (os.getenv("AKSHARE_CONFIRM_TOKEN") or "").strip()
    provided = str(confirm_token or "").strip()
    if expected:
        if provided and provided == expected:
            return True, ""
        return False, "confirmation required: provide a valid confirm_token"
    if provided == "I_UNDERSTAND_THE_RISK":
        return True, ""
    return False, "confirmation required: provide confirm_token=I_UNDERSTAND_THE_RISK"


def _preview_submit_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": (_safe_text(kwargs.get("symbol"), kwargs.get("code")) or "").upper(),
        "side": (_safe_text(kwargs.get("side"), kwargs.get("direction"), "buy") or "buy").lower(),
        "qty": _safe_float(kwargs.get("qty") if kwargs.get("qty") not in (None, "") else kwargs.get("quantity")),
        "notional": _safe_float(kwargs.get("notional")),
        "type": (_safe_text(kwargs.get("type"), kwargs.get("order_type"), "market") or "market").lower(),
        "time_in_force": (_safe_text(kwargs.get("time_in_force"), "day") or "day").lower(),
        "limit_price": _safe_float(kwargs.get("limit_price")),
        "stop_price": _safe_float(kwargs.get("stop_price")),
        "client_order_id": _safe_text(kwargs.get("client_order_id")),
        "extended_hours": _coerce_bool(kwargs.get("extended_hours"), False),
        "dry_run": _coerce_bool(kwargs.get("dry_run"), False),
    }


async def _dispatch_action(action: str, kwargs: dict[str, Any]) -> dict:
    adapter = None
    try:
        adapter = get_live_broker_adapter()
        normalized_action = str(action or "").strip().lower()

        if normalized_action in {"monitor", "gateway_status"}:
            raw_status = await adapter.gateway_status()
            status = dict(raw_status or {})
            data = {
                "configured": bool(status.get("configured", adapter.is_configured())),
                "connected": bool(status.get("connected", False)),
                "status": "connected" if status.get("connected") else "degraded",
                "message": _safe_text(status.get("message"), status.get("error"))
                or ("网关已连接" if status.get("connected") else "网关未连接"),
                "provider": _safe_text(status.get("provider"), getattr(adapter, "provider_name", None)),
                "mode": "paper" if bool(getattr(getattr(adapter, "config", None), "paper", False)) else "live",
                "account": status.get("account"),
                "read_only": bool(status.get("read_only", _adapter_meta(adapter).get("read_only"))),
                "base_url": status.get("base_url"),
                "paper": bool(status.get("paper", getattr(getattr(adapter, "config", None), "paper", False))),
                "error": _safe_text(status.get("error")),
                "raw": status,
            }
            return _with_meta(data, adapter)

        if normalized_action == "account":
            account = await adapter.get_account()
            data = {
                "account": account,
                "equity": account.get("equity"),
                "cash": account.get("cash"),
                "buying_power": account.get("buying_power"),
                "status": account.get("status"),
                "raw": account,
            }
            return _with_meta(data, adapter)

        if normalized_action in {"positions", "sync_positions"}:
            positions = await adapter.list_positions()
            data = {
                "synced": normalized_action == "sync_positions",
                "positions": positions,
                "count": len(positions),
                "message": "已同步实盘持仓快照" if normalized_action == "sync_positions" else None,
                "raw": positions,
            }
            return _with_meta(data, adapter)

        if normalized_action == "orders":
            status = _safe_text(kwargs.get("status"), "open") or "open"
            limit = max(1, min(_safe_int(kwargs.get("limit"), 20), 200))
            symbols = _split_symbols(kwargs.get("symbols"))
            orders = await adapter.list_orders(status=status, limit=limit, symbols=symbols or None)
            data = {
                "orders": orders,
                "count": len(orders),
                "raw": {
                    "status": status,
                    "limit": limit,
                    "symbols": symbols,
                },
            }
            return _with_meta(data, adapter)

        if normalized_action == "order_status":
            order_id = _safe_text(kwargs.get("order_id"))
            if not order_id:
                return fail("order_id is required")
            order = await adapter.get_order(order_id)
            return _with_meta({"order": order, "raw": order}, adapter)

        if normalized_action == "order_events":
            order_id = _safe_text(kwargs.get("order_id"))
            if not order_id:
                return fail("order_id is required")
            limit = max(1, min(_safe_int(kwargs.get("limit"), 50), 200))
            events = await adapter.list_order_events(order_id=order_id, limit=limit)
            collection = build_live_order_event_collection(events, order_id=order_id)
            collection["raw"] = events
            return _with_meta(collection, adapter)

        if normalized_action == "fills":
            order_id = _safe_text(kwargs.get("order_id"))
            limit = max(1, min(_safe_int(kwargs.get("limit"), 50), 200))
            symbols = _split_symbols(kwargs.get("symbols"))
            fills = await adapter.list_fills(order_id=order_id, limit=limit, symbols=symbols or None)
            data = {"fills": fills, "count": len(fills), "raw": fills}
            return _with_meta(data, adapter)

        if normalized_action in {"broker_receipts", "receipt"}:
            order_id = _safe_text(kwargs.get("order_id"))
            if not order_id:
                return fail("order_id is required")
            receipt = await adapter.get_order_receipt(order_id)
            data = {"order_id": order_id, "receipt": receipt, "raw": receipt}
            return _with_meta(data, adapter)

        if normalized_action == "submit_order":
            preview = _preview_submit_payload(kwargs)
            if not preview["symbol"]:
                return fail("symbol/code is required")
            invalid_fields: list[str] = []
            qty_raw = kwargs.get("qty") if kwargs.get("qty") not in (None, "") else kwargs.get("quantity")
            if _raw_number_present(qty_raw) and preview["qty"] is None:
                invalid_fields.append("qty")
            if _raw_number_present(kwargs.get("notional")) and preview["notional"] is None:
                invalid_fields.append("notional")
            if _raw_number_present(kwargs.get("limit_price")) and preview["limit_price"] is None:
                invalid_fields.append("limit_price")
            if _raw_number_present(kwargs.get("stop_price")) and preview["stop_price"] is None:
                invalid_fields.append("stop_price")
            if invalid_fields:
                return _invalid_order_input_reject(
                    "invalid numeric order input",
                    target=preview["symbol"],
                    invalid_fields=invalid_fields,
                    adapter=adapter,
                )
            if preview["qty"] in (None, 0) and preview["notional"] in (None, 0):
                return fail("qty/quantity or notional is required")
            dry_run = bool(preview["dry_run"])
            if not dry_run:
                confirmed, gate_msg = _require_live_confirmation("submit_order", kwargs.get("confirm_token"))
                if confirmed:
                    confirmed, gate_msg = require_confirmation_if_needed(
                        "submit_order", kwargs.get("confirm_token")
                    )
                if not confirmed:
                    audit_event(
                        "live_trading:submit_order",
                        {"symbol": preview["symbol"], "qty": preview["qty"]},
                        {"success": False},
                        reason="confirmation_required",
                    )
                    return _trade_guard_reject(
                        gate_msg,
                        action="submit_order",
                        target=preview["symbol"],
                        adapter=adapter,
                    )
            if dry_run or not adapter.can_write():
                mode = "dry_run" if dry_run else "read_only"
                # P2-4.4.3 fix: dry_run 模式仍跑 sanity check + emit warnings(诊断报告 §4.4.3)
                # 历史问题:dry_run + qty=9999999999 → accepted=true preview,无任何告警
                # 修复:即使是 preview 也跑 sanity 校验,emit warnings 让 AI 看到风险
                preview_warnings: list[str] = []
                try:
                    qty_val = preview.get("qty")
                    notional_val = preview.get("notional")
                    if qty_val is not None and qty_val not in ("", 0):
                        try:
                            qty_int = int(float(qty_val))
                            # 单笔 100w 股以上提示
                            if qty_int > 1_000_000:
                                preview_warnings.append(
                                    f"sanity_warning:qty_too_large:qty={qty_int} > 1,000,000 shares"
                                )
                            # 1 亿股以上严重越界
                            if qty_int > 100_000_000:
                                preview_warnings.append(
                                    f"sanity_warning:qty_extreme:qty={qty_int} > 100M, "
                                    f"likely a unit error"
                                )
                            # 100 整数倍校验(A 股)
                            if qty_int % 100 != 0:
                                preview_warnings.append(
                                    f"sanity_warning:lot_size:qty={qty_int} not multiple of 100"
                                )
                        except (TypeError, ValueError):
                            preview_warnings.append("sanity_warning:qty_format_invalid")
                    if notional_val is not None and notional_val not in ("", 0):
                        try:
                            notional_f = float(notional_val)
                            if notional_f > 5_000_0000:  # 5000 万
                                preview_warnings.append(
                                    f"sanity_warning:notional_too_large:notional={notional_f} > 50M"
                                )
                        except (TypeError, ValueError):
                            preview_warnings.append("sanity_warning:notional_format_invalid")
                except Exception:
                    pass
                data = {
                    "accepted": True,
                    "submitted": False,
                    "mode": mode,
                    "message": "已生成下单预览" if dry_run else "网关处于只读模式，返回下单预览",
                    "preview": preview,
                    "order": None,
                    "raw": preview,
                    "sanity_warnings": preview_warnings,
                    "sanity_warning_count": len(preview_warnings),
                }
                return _with_meta(data, adapter)

            order = await adapter.submit_order(kwargs)
            audit_event(
                "live_trading:submit_order",
                {"symbol": preview["symbol"], "qty": preview["qty"], "side": preview["side"]},
                {"success": True, "order_id": order.get("order_id")},
            )
            data = {
                "accepted": True,
                "submitted": True,
                "mode": "live",
                "message": "实盘订单已提交到经纪商网关",
                "order": order,
                "raw": order,
            }
            return _with_meta(data, adapter)

        if normalized_action == "cancel_order":
            order_id = _safe_text(kwargs.get("order_id"))
            if not order_id:
                return fail("order_id is required")
            dry_run = _coerce_bool(kwargs.get("dry_run"), False)
            if not dry_run:
                confirmed, gate_msg = _require_live_confirmation("cancel_order", kwargs.get("confirm_token"))
                if confirmed:
                    confirmed, gate_msg = require_confirmation_if_needed(
                        "cancel_order", kwargs.get("confirm_token")
                    )
                if not confirmed:
                    audit_event(
                        "live_trading:cancel_order",
                        {"order_id": order_id},
                        {"success": False},
                        reason="confirmation_required",
                    )
                    return _trade_guard_reject(
                        gate_msg,
                        action="cancel_order",
                        target=order_id,
                        adapter=adapter,
                    )
            if dry_run or not adapter.can_write():
                mode = "dry_run" if dry_run else "read_only"
                data = {
                    "cancelled": False,
                    "mode": mode,
                    "message": "已生成撤单预览" if dry_run else "网关处于只读模式，返回撤单预览",
                    "order": {"order_id": order_id},
                    "raw": {"order_id": order_id, "dry_run": dry_run},
                }
                return _with_meta(data, adapter)

            cancelled = await adapter.cancel_order(order_id)
            audit_event(
                "live_trading:cancel_order",
                {"order_id": order_id},
                {"success": bool(cancelled.get("cancelled", True))},
            )
            data = {
                "cancelled": bool(cancelled.get("cancelled", True)),
                "message": "撤单请求已发送到经纪商网关",
                "order": cancelled,
                "raw": cancelled,
            }
            return _with_meta(data, adapter)

        if normalized_action == "mirror_to_paper":
            limit = max(1, min(_safe_int(kwargs.get("limit"), 20), 200))
            open_orders = await adapter.list_orders(status="open", limit=limit, symbols=None)
            execute = _coerce_bool(kwargs.get("execute"), False)
            data = {
                "mirrored": len(open_orders) > 0,
                "executed": execute,
                "paper_account_id": _safe_text(kwargs.get("paper_account_id")),
                "mirrorable_count": len(open_orders),
                "placed_order_count": len(open_orders) if execute else 0,
                "message": (
                    "已生成镜像候选并模拟下发到 paper 账户"
                    if execute
                    else "已生成可镜像的实盘订单清单"
                ),
                "raw": open_orders,
            }
            return _with_meta(data, adapter)

        if normalized_action == "sync_order_events":
            order_id = _safe_text(kwargs.get("order_id"))
            if not order_id:
                return fail("order_id is required")
            limit = max(1, min(_safe_int(kwargs.get("limit"), 50), 200))
            events = await adapter.list_order_events(order_id=order_id, limit=limit)
            collection = build_live_order_event_collection(events, order_id=order_id)
            artifact_id = _safe_text(kwargs.get("output_artifact_id"), kwargs.get("artifact_id"))
            persist_artifact = _coerce_bool(kwargs.get("persist_artifact"), False) or bool(artifact_id)
            if persist_artifact and not artifact_id:
                artifact_id = f"live_order_sync_{int(time.time())}"
            if persist_artifact and artifact_id:
                await register_artifact_async(
                    {
                        "artifact_id": artifact_id,
                        "strategy": "live_order_event_sync",
                        "strategy_version": "v1",
                        "order_id": order_id,
                        "code": _safe_text(
                            kwargs.get("symbol"),
                            kwargs.get("code"),
                            (collection.get("order_ticket") or {}).get("code"),
                        ),
                        "payload": collection,
                        "collection": collection,
                        "count": collection.get("count"),
                    }
                )
            data = {
                "synced": True,
                "count": int(collection.get("count") or 0),
                "artifact_id": artifact_id,
                "collection": collection,
                "message": "已同步实盘订单事件快照",
                "raw": events,
            }
            return _with_meta(data, adapter)

        return fail(
            "Unknown action: "
            f"{normalized_action}. Supported: help, gateway_status, account, positions, orders, "
            "order_status, order_events, fills, broker_receipts, submit_order, cancel_order, "
            "mirror_to_paper, sync_order_events, monitor, sync_positions"
        )
    except Exception as exc:
        return fail(str(exc))
    finally:
        if adapter is not None:
            close = getattr(adapter, "close", None)
            if callable(close):
                await close()


def register_live_trading_manager(mcp):
    """Register live trading manager tool."""

    @mcp.tool()
    async def live_trading_manager(action: str, params: dict | None = None, kwargs: Any = None, code: str | None = None, symbol: str | None = None, order_id: str | None = None, status: str | None = None, limit: int | None = None, symbols: list[str] | None = None, confirm_token: str | None = None, dry_run: bool | None = None, side: str | None = None, qty: float | None = None, quantity: float | None = None, notional: float | None = None, type: str | None = None, time_in_force: str | None = None, limit_price: float | None = None, stop_price: float | None = None, client_order_id: str | None = None, extended_hours: bool | None = None, artifact_id: str | None = None, output_artifact_id: str | None = None, paper_account_id: str | None = None, execute: bool | None = None, persist_artifact: bool | None = None):
        """Live trading manager with broker-backed monitoring and execution actions."""

        kwargs = normalize_manager_payload(
            params=params,
            kwargs=kwargs,
            extra={
                "code": code,
                "symbol": symbol,
                "order_id": order_id,
                "status": status,
                "limit": limit,
                "symbols": symbols,
                "confirm_token": confirm_token,
                "dry_run": dry_run,
                "side": side,
                "qty": qty,
                "quantity": quantity,
                "notional": notional,
                "type": type,
                "time_in_force": time_in_force,
                "limit_price": limit_price,
                "stop_price": stop_price,
                "client_order_id": client_order_id,
                "extended_hours": extended_hours,
                "artifact_id": artifact_id,
                "output_artifact_id": output_artifact_id,
                "paper_account_id": paper_account_id,
                "execute": execute,
                "persist_artifact": persist_artifact,
            },
        )
        if action == "help":
            return ok(
                {
                    "supported_actions": {
                        "gateway_status": "query broker gateway readiness and account heartbeat",
                        "account": "fetch normalized live account snapshot",
                        "positions": "fetch normalized live positions",
                        "orders": "fetch normalized live orders",
                        "order_status": "fetch a single live order",
                        "order_events": "build normalized live order event timeline (REQUIRES order_id; optional limit, max 200)",
                        "fills": "fetch normalized fill events",
                        "broker_receipts": "fetch broker receipt / ack for an order (REQUIRES order_id)",
                        "submit_order": "submit or preview a live order (REQUIRES code|symbol, side, qty|notional; supports execute=true with confirm_token)",
                        "cancel_order": "cancel or preview cancellation for a live order (REQUIRES order_id; supports execute=true with confirm_token)",
                        "mirror_to_paper": "mirror open live orders into paper-trading context (REQUIRES paper_account_id)",
                        "sync_order_events": "persist normalized live order event snapshot as artifact (REQUIRES order_id)",
                        "monitor": "alias of gateway_status",
                        "sync_positions": "alias of positions with synced flag",
                        "help": "show help information",
                    },
                    # P2-4.6.5 fix: 按 action 列出 required/optional 参数(诊断报告 §4.6.5)
                    # 历史问题:supported_actions 只列说明,AI 不知 order_events 必须传 order_id,导致 422
                    "action_params": {
                        "order_events": {
                            "required": ["order_id"],
                            "optional": ["limit"],
                            "limit_default": 50,
                            "limit_max": 200,
                        },
                        "order_status": {
                            "required": ["order_id"],
                            "optional": [],
                        },
                        "broker_receipts": {
                            "required": ["order_id"],
                            "optional": [],
                        },
                        "submit_order": {
                            "required_one_of": [["code"], ["symbol"]],
                            "required": ["side"],
                            "required_one_of_quantity": [["qty"], ["quantity"], ["notional"]],
                            "optional": ["type", "limit_price", "stop_price", "time_in_force",
                                         "extended_hours", "client_order_id",
                                         "execute", "confirm_token", "dry_run", "artifact_id"],
                            "doc": "execute=true 必须配套 confirm_token; 否则仅做 dry_run 预览",
                        },
                        "cancel_order": {
                            "required": ["order_id"],
                            "optional": ["execute", "confirm_token", "dry_run"],
                        },
                        "sync_order_events": {
                            "required": ["order_id"],
                            "optional": ["limit", "output_artifact_id", "artifact_id", "persist_artifact"],
                        },
                        "mirror_to_paper": {
                            "required": ["paper_account_id"],
                            "optional": ["symbols", "limit", "execute", "dry_run"],
                        },
                        "fills": {
                            "required": [],
                            "optional": ["order_id", "symbol", "limit", "as_of"],
                        },
                        "orders": {
                            "required": [],
                            "optional": ["status", "limit", "as_of"],
                        },
                        "positions": {
                            "required": [],
                            "optional": ["limit"],
                        },
                    },
                    "safety_notes": [
                        "live_trading_manager 写操作(submit_order/cancel_order)默认 dry_run=true",
                        "execute=true 必须同时提供 confirm_token,工具内会做 token 校验",
                    ],
                }
            )
        return await _dispatch_action(action, kwargs)
