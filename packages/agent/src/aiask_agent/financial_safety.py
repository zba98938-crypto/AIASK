from __future__ import annotations

import os
from typing import Any


FINANCIAL_MANAGER_GROUPS: tuple[dict[str, str], ...] = (
    {"id": "overview", "label": "Overview", "description": "Readiness, manager coverage, and recent operating context."},
    {"id": "market-research", "label": "Market & Research", "description": "Stock analysis, research, sector, sentiment, technical, options, and trading-data reads."},
    {"id": "portfolio-watchlist", "label": "Portfolio & Watchlist", "description": "Portfolio, holdings, and watchlist reads plus intent-only mutations."},
    {"id": "risk-performance", "label": "Risk & Performance", "description": "Portfolio risk, VaR, exposure, stress, attribution, and benchmark comparison."},
    {"id": "quant-backtest", "label": "Quant & Backtest", "description": "Quant research, data gates, and backtest suite operations."},
    {"id": "paper-execution", "label": "Paper & Execution Planning", "description": "Paper trading and execution plan reads plus intent-only task changes."},
    {"id": "broker-readonly", "label": "Broker Read-only", "description": "THS/QMT account, position, order, and deal queries without live order placement."},
)

FINANCIAL_MANAGER_ACTIONS: tuple[dict[str, Any], ...] = (
    {"capability_id": "stock-analysis", "action_id": "analyze_stock", "group": "market-research", "label": "Analyze stock", "mode": "read_only", "tool": "agent_analyze_stock", "default_params": {"code": "600519", "include_decision": False}},
    {"capability_id": "research", "action_id": "reports", "group": "market-research", "label": "Research reports", "mode": "read_only", "mcp_tool": "research_manager", "mcp_action": "get_reports", "default_params": {"code": "600519", "limit": 5}},
    {"capability_id": "sector", "action_id": "sector_performance", "group": "market-research", "label": "Sector performance", "mode": "read_only", "mcp_tool": "sector_manager", "mcp_action": "sector_performance", "default_params": {"limit": 10}},
    {"capability_id": "sentiment", "action_id": "market_sentiment", "group": "market-research", "label": "Market sentiment", "mode": "read_only", "mcp_tool": "sentiment_manager", "mcp_action": "market_sentiment", "default_params": {}},
    {"capability_id": "technical", "action_id": "calculate", "group": "market-research", "label": "Technical indicators", "mode": "read_only", "mcp_tool": "technical_analysis_manager", "mcp_action": "calculate", "default_params": {"code": "600519", "indicators": ["ma", "rsi"]}},
    {"capability_id": "options", "action_id": "calculate_greeks", "group": "market-research", "label": "Option Greeks", "mode": "read_only", "mcp_tool": "options_manager", "mcp_action": "calculate_greeks", "default_params": {}},
    {"capability_id": "trading-data", "action_id": "dragon_tiger", "group": "market-research", "label": "Dragon tiger list", "mode": "read_only", "mcp_tool": "trading_data_manager", "mcp_action": "dragon_tiger", "default_params": {"limit": 20}},
    {"capability_id": "screener", "action_id": "screen", "group": "market-research", "label": "Stock screener", "mode": "read_only", "mcp_tool": "screener_manager", "mcp_action": "screen", "default_params": {"limit": 20}},
    {"capability_id": "portfolio", "action_id": "risk", "group": "risk-performance", "label": "Portfolio risk", "mode": "read_only", "tool": "agent_portfolio_risk", "default_params": {"codes": ["600519", "000001"], "weights": [0.5, 0.5]}},
    {"capability_id": "portfolio", "action_id": "list", "group": "portfolio-watchlist", "label": "Portfolio list", "mode": "read_only", "mcp_tool": "portfolio_manager", "mcp_action": "list", "default_params": {}},
    {"capability_id": "portfolio", "action_id": "get_holdings", "group": "portfolio-watchlist", "label": "Portfolio holdings", "mode": "read_only", "mcp_tool": "portfolio_manager", "mcp_action": "get_holdings", "default_params": {"portfolio_id": 1}},
    {"capability_id": "portfolio", "action_id": "create", "group": "portfolio-watchlist", "label": "Create portfolio intent", "mode": "stateful_intent", "intent_action": "portfolio_manager.create", "default_params": {"name": "Desktop portfolio"}},
    {"capability_id": "portfolio", "action_id": "add_holding", "group": "portfolio-watchlist", "label": "Add holding intent", "mode": "stateful_intent", "intent_action": "portfolio_manager.add_holding", "default_params": {"portfolio_id": 1, "code": "600519", "shares": 100, "cost_price": 1800}},
    {"capability_id": "watchlist", "action_id": "list", "group": "portfolio-watchlist", "label": "Watchlist groups", "mode": "read_only", "mcp_tool": "watchlist_manager", "mcp_action": "list", "default_params": {}},
    {"capability_id": "watchlist", "action_id": "add", "group": "portfolio-watchlist", "label": "Add watchlist stock intent", "mode": "stateful_intent", "intent_action": "watchlist_manager.add", "default_params": {"group": "default", "code": "600519"}},
    {"capability_id": "watchlist", "action_id": "remove", "group": "portfolio-watchlist", "label": "Remove watchlist stock intent", "mode": "stateful_intent", "intent_action": "watchlist_manager.remove", "default_params": {"group": "default", "code": "600519"}},
    {"capability_id": "risk", "action_id": "var", "group": "risk-performance", "label": "Risk VaR", "mode": "read_only", "mcp_tool": "risk_manager", "mcp_action": "var", "default_params": {"codes": ["600519", "000001"], "weights": [0.5, 0.5]}},
    {"capability_id": "risk", "action_id": "exposure", "group": "risk-performance", "label": "Risk exposure", "mode": "read_only", "mcp_tool": "risk_manager", "mcp_action": "exposure", "default_params": {"codes": ["600519", "000001"], "weights": [0.5, 0.5]}},
    {"capability_id": "performance", "action_id": "calculate_metrics", "group": "risk-performance", "label": "Performance metrics", "mode": "read_only", "mcp_tool": "performance_manager", "mcp_action": "calculate_metrics", "default_params": {"portfolio_id": 1}},
    {"capability_id": "decision", "action_id": "portfolio_advice", "group": "risk-performance", "label": "Portfolio advice", "mode": "read_only", "mcp_tool": "decision_manager", "mcp_action": "portfolio_advice", "default_params": {"codes": ["600519", "000001"], "weights": [0.5, 0.5]}},
    {"capability_id": "quant", "action_id": "data_gate", "group": "quant-backtest", "label": "Quant data gate", "mode": "read_only", "tool": "agent_quant_data_gate", "default_params": {"codes": ["600519", "000001"], "max_stale_days": 5}},
    {"capability_id": "quant", "action_id": "research_run", "group": "quant-backtest", "label": "Quant research run", "mode": "read_only", "tool": "agent_quant_research_run", "default_params": {"universe": ["600519", "000001"], "factors": ["momentum"], "benchmark": "000300"}},
    {"capability_id": "backtest", "action_id": "suite", "group": "quant-backtest", "label": "Backtest suite", "mode": "read_only", "tool": "agent_backtest_suite", "default_params": {"codes": ["600519", "000001"], "strategy": "ma_cross"}},
    {"capability_id": "backtest", "action_id": "list", "group": "quant-backtest", "label": "Saved backtests", "mode": "read_only", "mcp_tool": "backtest_manager", "mcp_action": "list", "default_params": {}},
    {"capability_id": "paper", "action_id": "status", "group": "paper-execution", "label": "Paper trading status", "mode": "read_only", "mcp_tool": "paper_trading_manager", "mcp_action": "summary", "default_params": {}},
    {"capability_id": "paper", "action_id": "orders", "group": "paper-execution", "label": "Paper orders", "mode": "read_only", "mcp_tool": "paper_trading_manager", "mcp_action": "orders", "default_params": {"limit": 20}},
    {"capability_id": "paper", "action_id": "submit_order", "group": "paper-execution", "label": "Paper order intent", "mode": "stateful_intent", "intent_action": "paper_trading_manager.submit_order", "default_params": {"code": "600519", "side": "buy", "quantity": 100, "dry_run": True}},
    {"capability_id": "execution", "action_id": "plan", "group": "paper-execution", "label": "Execution tasks", "mode": "read_only", "mcp_tool": "execution_manager", "mcp_action": "list", "default_params": {}},
    {"capability_id": "execution", "action_id": "create_plan", "group": "paper-execution", "label": "Create execution plan intent", "mode": "stateful_intent", "intent_action": "execution_manager.create_plan", "default_params": {"code": "600519", "side": "buy", "quantity": 1000, "dry_run": True}},
    {"capability_id": "broker-ths", "action_id": "balance", "group": "broker-readonly", "label": "THS balance", "mode": "read_only", "mcp_tool": "ths_query_balance", "default_params": {}},
    {"capability_id": "broker-ths", "action_id": "positions", "group": "broker-readonly", "label": "THS positions", "mode": "read_only", "mcp_tool": "ths_query_position", "default_params": {}},
    {"capability_id": "broker-ths", "action_id": "orders", "group": "broker-readonly", "label": "THS orders", "mode": "read_only", "mcp_tool": "ths_query_orders", "default_params": {}},
    {"capability_id": "broker-ths", "action_id": "deals", "group": "broker-readonly", "label": "THS deals", "mode": "read_only", "mcp_tool": "ths_query_deals", "default_params": {}},
    {"capability_id": "broker-qmt", "action_id": "account", "group": "broker-readonly", "label": "QMT account", "mode": "read_only", "mcp_tool": "qmt_query_account", "default_params": {}},
    {"capability_id": "broker-qmt", "action_id": "positions", "group": "broker-readonly", "label": "QMT positions", "mode": "read_only", "mcp_tool": "qmt_query_position", "default_params": {}},
    {"capability_id": "broker-qmt", "action_id": "orders", "group": "broker-readonly", "label": "QMT orders", "mode": "read_only", "mcp_tool": "qmt_query_orders", "default_params": {}},
    {"capability_id": "broker-live", "action_id": "place_order", "group": "broker-readonly", "label": "Live place order", "mode": "blocked", "blocked_reason": "Live broker order placement is disabled in Financial Manager V1."},
    {"capability_id": "broker-live", "action_id": "cancel_order", "group": "broker-readonly", "label": "Live cancel order", "mode": "blocked", "blocked_reason": "Live broker cancellation is disabled in Financial Manager V1."},
)

FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE: dict[str, dict[str, Any]] = {
    "watchlist_manager.add": {"execution_mode": "confirmed_execute"},
    "watchlist_manager.remove": {"execution_mode": "confirmed_execute"},
    "portfolio_manager.create": {"execution_mode": "confirmed_execute"},
    "portfolio_manager.add_holding": {"execution_mode": "confirmed_execute"},
    "execution_manager.create_plan": {
        "execution_mode": "confirmed_execute_dry_run_only",
        "dry_run_required": True,
    },
    "paper_trading_manager.submit_order": {
        "execution_mode": "confirmed_execute_dry_run_only",
        "dry_run_required": True,
    },
}

FINANCIAL_MANAGER_DRY_RUN_ONLY_ACTIONS: tuple[str, ...] = tuple(
    action_name
    for action_name, policy in FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE.items()
    if bool(policy.get("dry_run_required"))
)
FINANCIAL_MANAGER_CONFIRMED_ACTIONS: tuple[str, ...] = tuple(FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE.keys())
FINANCIAL_MANAGER_DRY_RUN_REQUIRED_ERROR = "FINANCIAL_MANAGER_DRY_RUN_REQUIRED"

BROKER_LIVE_TOKEN_ENVS: dict[str, str] = {
    "tonghuashun": "AIASK_FINANCE_THS_BROKER_TOKEN",
    "qmt": "AIASK_FINANCE_QMT_BROKER_TOKEN",
}

BROKER_LIVE_TOOL_NAMES: tuple[str, ...] = (
    "ths_place_order",
    "ths_cancel_order",
    "qmt_place_order",
    "qmt_cancel_order",
)


def _financial_action_key(capability_id: str, action_id: str) -> str:
    return f"{str(capability_id or '').strip()}::{str(action_id or '').strip()}"


def _financial_action_map() -> dict[str, dict[str, Any]]:
    return {
        _financial_action_key(str(item.get("capability_id") or ""), str(item.get("action_id") or "")): dict(item)
        for item in FINANCIAL_MANAGER_ACTIONS
    }


def _explicit_false(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value is False
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "n", "off", "disabled"}
    return not bool(value)


def financial_confirmed_action_policy(action: str) -> dict[str, Any] | None:
    return FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE.get(str(action or "").strip())


def normalize_financial_confirmed_action_params(action: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Apply Financial Manager confirmed-action safety policy before intent persistence/execution."""

    action_key = str(action or "").strip()
    policy = financial_confirmed_action_policy(action_key)
    if policy is None:
        raise ValueError(f"financial manager action is outside the confirmed scope: {action_key}")
    normalized = dict(params or {})
    if bool(policy.get("dry_run_required")):
        if _explicit_false(normalized.get("dry_run")):
            raise ValueError(FINANCIAL_MANAGER_DRY_RUN_REQUIRED_ERROR)
        normalized["dry_run"] = True
    return normalized


def _financial_execution_mode(action: dict[str, Any]) -> str:
    mode = str(action.get("mode") or "read_only")
    if mode == "blocked":
        return "blocked"
    if mode == "read_only":
        return "read_only"
    intent_action = str(action.get("intent_action") or "").strip()
    if intent_action and intent_action in FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE:
        return str(FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE[intent_action]["execution_mode"])
    return "intent_only"


def _financial_risk_level(action: dict[str, Any]) -> str:
    mode = str(action.get("mode") or "read_only")
    if mode == "read_only":
        return "read_only"
    if mode == "stateful_intent":
        return "stateful"
    return "trade_risk"


def _broker_live_env_presence() -> dict[str, dict[str, Any]]:
    return {
        provider: {
            "env_var": env_var,
            "configured": bool(str(os.getenv(env_var, "")).strip()),
        }
        for provider, env_var in BROKER_LIVE_TOKEN_ENVS.items()
    }


def financial_safety_matrix(*, control_token_ready: bool | None = None) -> dict[str, Any]:
    """Return the auditable Financial Manager safety contract.

    Values are policy and presence-only metadata. Secret values are never read
    beyond a boolean configured check and are never returned.
    """

    if control_token_ready is None:
        from .route_auth import control_token_configured

        control_ready = control_token_configured()
    else:
        control_ready = bool(control_token_ready)
    action_rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "read_only": 0,
        "stateful_intent": 0,
        "trade_risk_blocked": 0,
        "dry_run_only": 0,
    }
    for raw in FINANCIAL_MANAGER_ACTIONS:
        action = dict(raw)
        mode = str(action.get("mode") or "read_only")
        intent_action = str(action.get("intent_action") or "").strip()
        execution_mode = _financial_execution_mode(action)
        risk_level = _financial_risk_level(action)
        dry_run_required = bool(
            intent_action
            and intent_action in FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE
            and FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE[intent_action].get("dry_run_required")
        )
        broker_live = str(action.get("capability_id") or "") == "broker-live"
        if mode == "read_only":
            counts["read_only"] += 1
        elif mode == "stateful_intent":
            counts["stateful_intent"] += 1
        elif broker_live or mode == "blocked":
            counts["trade_risk_blocked"] += 1
        if dry_run_required:
            counts["dry_run_only"] += 1
        action_rows.append(
            {
                "operation_id": f"{action.get('capability_id')}.{action.get('action_id')}",
                "capability_id": action.get("capability_id"),
                "action_id": action.get("action_id"),
                "group": action.get("group"),
                "label": action.get("label"),
                "mode": mode,
                "execution_mode": execution_mode,
                "risk_level": risk_level,
                "read_only": mode == "read_only",
                "direct_execution_allowed": mode == "read_only",
                "action_intent_required": mode == "stateful_intent",
                "control_token_required": mode == "stateful_intent",
                "control_token_configured": control_ready,
                "dry_run_required": dry_run_required,
                "broker_token_required": broker_live,
                "live_trading_blocked": broker_live or mode == "blocked",
                "stateful_confirmation_required": mode != "read_only",
                "endpoint": (
                    "/v1/desktop/financial-manager/query"
                    if mode == "read_only"
                    else "/v1/desktop/financial-manager/intent"
                    if mode == "stateful_intent"
                    else None
                ),
                "intent_action": intent_action or None,
                "blocked_reason": action.get("blocked_reason") if mode == "blocked" else None,
            }
        )
    broker_env_presence = _broker_live_env_presence()
    return {
        "object": "aiask.desktop.financial_manager.safety_matrix",
        "schema_version": 1,
        "mode": "read_only_plus_intents",
        "live_trading_enabled": False,
        "live_trading_policy": "blocked_in_financial_manager_v1",
        "stateful_execution": "allowlisted_confirmed_actions",
        "action_intent_required": True,
        "control_token_configured": control_ready,
        "broker_token_policy": {
            "explicit_token_required_for_live_mcp": True,
            "live_tools_enabled_by_default": False,
            "guarded_tools": list(BROKER_LIVE_TOOL_NAMES),
            "env_presence": broker_env_presence,
        },
        "confirmed_action_scope": list(FINANCIAL_MANAGER_CONFIRMED_ACTION_SCOPE.keys()),
        "dry_run_only_actions": list(FINANCIAL_MANAGER_DRY_RUN_ONLY_ACTIONS),
        "summary": {
            **counts,
            "total_actions": len(action_rows),
            "control_token_ready": control_ready,
            "broker_token_env_configured": sum(1 for item in broker_env_presence.values() if item.get("configured")),
        },
        "actions": action_rows,
        "secrets_redacted": True,
    }
