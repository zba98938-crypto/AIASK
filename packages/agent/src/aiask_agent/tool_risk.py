from __future__ import annotations

import json
from typing import Any


READ_ONLY_STRATEGY_ACTIONS = frozenset(
    {
        "help",
        "list",
        "detail",
        "review_report",
        "events",
        "review",
        "my_subscriptions",
        "my_strategies",
        "personal_strategy_context",
        "personal_strategy_suggestions",
        "paper_session_get",
        "rank",
        "capabilities",
        "daily_snapshot",
        "daily_snapshots",
        "get_signals",
        "get_forward_returns",
        "get_signal_stats",
        "lifecycle_scan",
        "incubation_overview",
        "closure_review",
        "factory_status",
        "factory_dispatch_status",
        "factory_runs",
        "factory_run_detail",
        "factory_topn_latest",
        "factory_run_topn",
        "execution_audit_verification",
        "incubation_accounts",
        "incubation_metrics",
        "paper_account",
        "paper_orders",
        "paper_nav",
        "incubation_pipeline",
        "risk_events",
        "risk_snapshots",
        "runtime_alerts",
        "runtime_control",
        "promotion_reviews",
        "runtime_cycle_status",
        "domain_events",
        "domain_projection",
        "domain_projection_snapshot",
        "vector_profiles",
        "vector_indexes",
        "vector_index_snapshots",
        "vector_ann_search",
        "vector_health",
        "ai_experiments",
        "task_runs",
        # PR-F (Phase 4, 2026-05-24): event-driven manager actions.
        # ``factory_event_list`` and ``factory_event_preview_tasks`` are
        # idempotent reads — listing existing injections / dry-running
        # propagation should never need ActionIntent confirmation.
        "factory_event_list",
        "factory_event_preview_tasks",
        "factory_event_lineage",
        "factory_theme_exposure_status",
        "factory_event_outbox_status",
    }
)


CONFIRM_REQUIRED_STRATEGY_ACTIONS = frozenset(
    {
        "create",
        "publish",
        "archive",
        "update_metrics",
        "subscribe",
        "unsubscribe",
        "fork_strategy",
        "update_strategy",
        "delete_personal_strategy",
        "paper_session_get_or_create",
        "review_report_recheck",
        "submission_replay",
        "submit",
        "factory_run_once",
        "factory_dispatch_run",
        "execution_audit_acceptance",
        "incubation_sync_run",
        "incubation_pipeline_run",
        "risk_scan_run",
        "risk_recovery",
        "resolve_risk_event",
        "runtime_alert_dispatch_run",
        "runtime_alert_ack",
        "runtime_control_set",
        "promotion_review_run",
        "runtime_cycle_run",
        "domain_projection_rebuild",
        "vector_reconcile",
        "vector_rebuild",
        "vector_cleanup",
        "ai_generate",
        "ai_optimize_personal_strategy",
        # PR-F (Phase 4, 2026-05-24): event-driven write actions must
        # flow through ActionIntent so Desktop / TUI cannot bypass the
        # dual-person review and self-approval guard implemented in
        # ``strategy_mgr_factory_events.handle_factory_event_*``.
        "factory_event_create",
        "factory_event_update",
        "factory_event_approve",
        "factory_event_record_outcome",
        "factory_event_bootstrap",
        "factory_theme_exposure_refresh",
        "factory_event_outbox_drain",
        "factory_theme_regression_run",
    }
)

MCP_CONTRACT_METADATA_FIELDS = (
    "input_schema",
    "output_schema",
    "freshness",
    "examples",
    "contract_version",
    "contract_source",
    "source_policy",
    "standard_model",
    "provider_choices",
    "provider_status",
    "quality_gate",
    "reconciliation",
    "form_schema",
)

LIVE_BROKER_MCP_TOOL_NAMES = frozenset(
    {
        "ths_place_order",
        "ths_cancel_order",
        "qmt_place_order",
        "qmt_cancel_order",
    }
)

LIVE_BROKER_NAME_PATTERNS = (
    "place_order",
    "submit_order",
    "send_order",
    "new_order",
    "order_insert",
    "order_submit",
    "cancel_order",
    "order_cancel",
    "live_order",
    "live_trade",
)

LIVE_BROKER_DESCRIPTION_PATTERNS = (
    "place order",
    "order placement",
    "submit order",
    "send order",
    "cancel order",
    "order cancellation",
    "live broker order",
    "broker write",
    "\u4e0b\u5355",
    "\u64a4\u5355",
    "\u59d4\u6258\u4e70\u5165",
    "\u59d4\u6258\u5356\u51fa",
)

READ_ONLY_ORDER_QUERY_PATTERNS = (
    "query_order",
    "query_orders",
    "list_order",
    "list_orders",
    "get_order",
    "get_orders",
    "order_query",
    "orders_query",
    "broker_order_query",
    "broker_orders_query",
    "broker_query_order",
    "broker_query_orders",
    "query_broker_order",
    "query_broker_orders",
    "order_history",
    "orders_history",
    "read_order",
    "read_orders",
)

BROKER_TOKEN_KEYS = frozenset(
    {
        "broker_token",
        "trade_token",
        "order_token",
        "confirm_token",
    }
)

PLACE_ORDER_REQUIRED_KEYS = (
    {"code", "price", "amount"},
    {"code", "price", "volume"},
    {"code", "price", "quantity"},
    {"symbol", "price", "amount"},
    {"symbol", "price", "volume"},
    {"symbol", "price", "quantity"},
)
CANCEL_ORDER_REQUIRED_KEYS = (
    {"order_id"},
    {"entrust_no"},
    {"entrust_id"},
    {"order_no"},
)


def _normalized_tool_text(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _schema_property_names(schema: Any) -> set[str]:
    if not isinstance(schema, dict):
        return set()
    names: set[str] = set()
    properties = schema.get("properties")
    if isinstance(properties, dict):
        names.update(str(key).strip().lower() for key in properties if str(key).strip())
        for child in properties.values():
            names.update(_schema_property_names(child))
    for key in ("required",):
        value = schema.get(key)
        if isinstance(value, list):
            names.update(str(item).strip().lower() for item in value if str(item).strip())
    for key in ("items", "input_schema", "inputSchema", "parameters", "schema"):
        names.update(_schema_property_names(schema.get(key)))
    return names


def _side_effect_level(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("level") or value.get("side_effect") or "").strip().lower()
    return str(value or "").strip().lower()


def _looks_like_read_only_order_query(tool_name: str) -> bool:
    normalized = _normalized_tool_text(tool_name)
    return any(pattern in normalized for pattern in READ_ONLY_ORDER_QUERY_PATTERNS)


def looks_like_live_broker_mcp_tool(
    tool_name: str | None,
    *,
    description: str | None = None,
    parameters: Any = None,
    side_effect: Any = None,
) -> bool:
    normalized_name = _normalized_tool_text(tool_name)
    if not normalized_name:
        return False
    if normalized_name in LIVE_BROKER_MCP_TOOL_NAMES:
        return True
    if _side_effect_level(side_effect) == "trade_risk":
        return True

    description_text = str(description or "").strip().lower()
    property_names = _schema_property_names(parameters)
    if property_names & BROKER_TOKEN_KEYS:
        return True

    name_has_trade_pattern = any(pattern in normalized_name for pattern in LIVE_BROKER_NAME_PATTERNS)
    if name_has_trade_pattern and not _looks_like_read_only_order_query(normalized_name):
        return True

    description_has_trade_pattern = any(pattern in description_text for pattern in LIVE_BROKER_DESCRIPTION_PATTERNS)
    if description_has_trade_pattern:
        return True

    context_text = f"{normalized_name} {description_text}".lower()
    has_order_context = any(token in context_text for token in ("order", "trade", "broker", "entrust"))
    if has_order_context:
        for required in PLACE_ORDER_REQUIRED_KEYS:
            if required <= property_names and {"direction", "side"} & property_names:
                return True
        if any(required <= property_names for required in CANCEL_ORDER_REQUIRED_KEYS) and any(
            token in context_text for token in ("cancel", "withdraw", "revoke")
        ):
            return True

    return False


STRATEGY_ACTION_SIDE_EFFECTS: dict[str, dict[str, Any]] = {
    **{
        action: {
            "level": "read_only",
            "target": f"strategy_manager.{action}",
            "confirmation_required": False,
            "idempotent": True,
        }
        for action in READ_ONLY_STRATEGY_ACTIONS
    },
    **{
        action: {
            "level": "stateful",
            "target": f"strategy_manager.{action}",
            "confirmation_required": True,
            "idempotent": False,
        }
        for action in CONFIRM_REQUIRED_STRATEGY_ACTIONS
    },
}


def is_live_broker_mcp_tool(tool_name: str | None) -> bool:
    return looks_like_live_broker_mcp_tool(tool_name)


def mcp_tool_is_live_broker_trade_risk(tool: dict[str, Any] | None) -> bool:
    payload = dict(tool or {})
    return looks_like_live_broker_mcp_tool(
        str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        parameters=payload.get("parameters") or payload.get("input_schema") or payload.get("inputSchema"),
        side_effect=payload.get("side_effect"),
    )


def classify_strategy_manager_action(action: str | None) -> dict[str, Any]:
    normalized = str(action or "").strip()
    if normalized in STRATEGY_ACTION_SIDE_EFFECTS:
        return dict(STRATEGY_ACTION_SIDE_EFFECTS[normalized])
    return {
        "level": "stateful",
        "target": f"strategy_manager.{normalized or 'unknown'}",
        "confirmation_required": True,
        "idempotent": False,
        "unknown_action": True,
    }


def normalize_side_effect(value: Any, *, target: str = "tool") -> dict[str, Any]:
    if isinstance(value, dict):
        payload = dict(value)
        payload.setdefault("level", "read_only")
        payload.setdefault("target", target)
        payload.setdefault("confirmation_required", False)
        payload.setdefault("idempotent", payload.get("level") == "read_only")
        return payload
    level = str(value or "read_only").strip() or "read_only"
    return {
        "level": level,
        "target": target,
        "confirmation_required": level in {"stateful", "confirm_required"},
        "idempotent": level == "read_only",
    }


def metadata_side_effect(metadata: dict[str, Any] | None, *, target: str = "tool") -> dict[str, Any]:
    return normalize_side_effect(dict(metadata or {}).get("side_effect"), target=target)


def metadata_is_read_only(metadata: dict[str, Any] | None, *, target: str = "tool") -> bool:
    return metadata_side_effect(metadata, target=target).get("level") == "read_only"


def read_only_desktop_result_guard(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(result or {})
    if payload.get("error_code") == "READ_ONLY_TOOL_RESULT_BLOCKED":
        return payload
    meta = dict(payload.get("meta") or {}) if isinstance(payload.get("meta"), dict) else {}
    side_effect = normalize_side_effect(meta.get("side_effect"), target=tool_name)
    if side_effect.get("level") == "read_only" and not side_effect.get("confirmation_required"):
        return payload
    guard_meta = {
        key: meta[key]
        for key in ("trace_id", "source_chain", "toolset")
        if key in meta
    }
    guard_meta["side_effect"] = side_effect
    guard_meta["read_only_desktop_guard"] = {
        "blocked": True,
        "reason": "non_read_only_tool_result",
    }
    return {
        "success": False,
        "data": {
            "tool": str(tool_name or ""),
            "side_effect": side_effect,
            "reason": "The read-only Desktop tool API blocked a non-read-only tool result.",
        },
        "error": f"tool returned non-read-only side effect through read-only Desktop API: {tool_name}",
        "error_code": "READ_ONLY_TOOL_RESULT_BLOCKED",
        "meta": guard_meta,
    }


def extract_mcp_action(arguments: dict[str, Any] | None) -> str:
    payload = dict(arguments or {})
    action = str(payload.get("action") or "").strip()
    if action:
        return action
    params = payload.get("params")
    if isinstance(params, dict):
        action = str(params.get("action") or "").strip()
        if action:
            return action
    kwargs = payload.get("kwargs")
    if isinstance(kwargs, dict):
        return str(kwargs.get("action") or "").strip()
    if isinstance(kwargs, str) and kwargs.strip().startswith("{"):
        try:
            loaded = json.loads(kwargs)
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            return str(loaded.get("action") or "").strip()
    return ""


def strategy_action_params(arguments: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(arguments or {})
    params = payload.get("params")
    if isinstance(params, dict):
        return dict(params)
    kwargs = payload.get("kwargs")
    if isinstance(kwargs, dict):
        return dict(kwargs)
    if isinstance(kwargs, str) and kwargs.strip().startswith("{"):
        try:
            loaded = json.loads(kwargs)
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            return dict(loaded)
    return {k: v for k, v in payload.items() if k not in {"action", "params", "kwargs"}}


def mcp_tool_metadata(tool: dict[str, Any]) -> dict[str, Any]:
    raw_side_effect = tool.get("side_effect")
    tool_name = str(tool.get("name") or "").strip()
    metadata = {
        "capability": "mcp_financial",
        "category": "mcp_financial",
        "server": tool.get("server"),
    }
    if tool_name == "strategy_manager":
        metadata["side_effect"] = {
            "level": "read_only",
            "target": "strategy_manager",
            "confirmation_required": False,
            "idempotent": True,
            "action_scoped": True,
            "action_levels": {
                action: STRATEGY_ACTION_SIDE_EFFECTS[action]["level"]
                for action in sorted(STRATEGY_ACTION_SIDE_EFFECTS)
            },
            "confirm_required_actions": sorted(CONFIRM_REQUIRED_STRATEGY_ACTIONS),
        }
    elif mcp_tool_is_live_broker_trade_risk(tool):
        metadata["side_effect"] = {
            "level": "trade_risk",
            "target": tool_name,
            "confirmation_required": True,
            "idempotent": False,
            "live_broker_tool": True,
        }
    else:
        metadata["side_effect"] = normalize_side_effect(raw_side_effect, target=tool_name)
    for field in MCP_CONTRACT_METADATA_FIELDS:
        if tool.get(field) is not None:
            metadata[field] = tool.get(field)
    return metadata


def mcp_call_side_effect(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    raw_side_effect: Any = None,
) -> dict[str, Any]:
    if str(tool_name or "").strip() == "strategy_manager":
        return classify_strategy_manager_action(extract_mcp_action(arguments))
    if looks_like_live_broker_mcp_tool(tool_name, side_effect=raw_side_effect):
        return {
            "level": "trade_risk",
            "target": str(tool_name or "").strip(),
            "confirmation_required": True,
            "idempotent": False,
            "live_broker_tool": True,
        }
    return normalize_side_effect(raw_side_effect, target=tool_name)
