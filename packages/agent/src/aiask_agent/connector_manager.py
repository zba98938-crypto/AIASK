"""Unified Connector Manager — 统一连接器生命周期管理。

提供统一的"连接器"概念，将 MCP Server、Plugin、Gateway Platform
三类外部连接统一管理，支持：
  - 列出所有连接器及其状态
  - 启用/禁用连接器
  - 查看连接器健康状态
  - 统一配置管理

连接器类型：
  - mcp: MCP Server（金融数据、工具服务）
  - plugin: Native Plugin（扩展功能）
  - platform: Gateway Platform（通信平台）
  - financial: 金融软件连接器（通达信/同花顺/东方财富/QMT）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .connector_safety import financial_connector_safety_metadata
from .paths import aiask_agent_home
from .session_store import now_iso


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class ConnectorInfo:
    """Unified connector information."""

    id: str                     # Unique identifier
    name: str                   # Display name
    type: str                   # "mcp" | "plugin" | "platform" | "financial"
    category: str               # "data" | "trading" | "communication" | "tool"
    enabled: bool               # Whether the connector is enabled
    configured: bool            # Whether credentials/config are present
    connected: bool             # Whether currently connected
    status: str                 # "ready" | "disconnected" | "error" | "unconfigured"
    description: str = ""
    icon: str = ""              # Icon identifier for UI
    env_keys: list[str] = field(default_factory=list)
    missing_env: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_activity: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Financial connector definitions
# ---------------------------------------------------------------------------

FINANCIAL_CONNECTORS: list[dict[str, Any]] = [
    {
        "id": "tongdaxin",
        "name": "通达信",
        "category": "trading",
        "description": "通达信行情数据 + 交易接口（pytdx）",
        "icon": "tdx",
        "env_keys": ["TDX_SERVER_IP"],
        "optional_env": ["TDX_SERVER_PORT", "TDX_TRADE_SERVER", "TDX_TRADE_ACCOUNT"],
        "mcp_server": "tongdaxin",
        "tools_read": ["tdx_realtime_quote", "tdx_kline_history", "tdx_minute_data", "tdx_tick_data", "tdx_finance_info", "tdx_market_snapshot"],
        "tools_trade": ["tdx_place_order", "tdx_cancel_order", "tdx_query_position", "tdx_query_orders", "tdx_query_balance"],
    },
    {
        "id": "tonghuashun",
        "name": "同花顺",
        "category": "trading",
        "description": "同花顺行情 + 交易接口（easytrader / iFinD）",
        "icon": "ths",
        "env_keys": ["THS_CLIENT_PATH"],
        "optional_env": ["THS_TRADE_ACCOUNT", "THS_BROKER", "THS_IFIND_USERNAME", "THS_IFIND_PASSWORD"],
        "mcp_server": "tonghuashun",
        "tools_read": ["ths_query_balance", "ths_query_position", "ths_query_orders", "ths_query_deals"],
        "tools_trade": ["ths_place_order", "ths_cancel_order"],
    },
    {
        "id": "eastmoney",
        "name": "东方财富",
        "category": "data",
        "description": "东方财富数据接口（efinance）",
        "icon": "em",
        "env_keys": [],  # No auth required for public data
        "optional_env": ["EM_API_TOKEN"],
        "mcp_server": "eastmoney",
        "tools_read": ["em_realtime_quote", "em_kline_history", "em_fund_info", "em_fund_nav", "em_bond_data", "em_futures_data", "em_news_flow", "em_dragon_tiger_list"],
        "tools_trade": [],
    },
    {
        "id": "qmt",
        "name": "迅投 QMT / MiniQMT",
        "category": "trading",
        "description": "迅投量化交易接口（XtQuant SDK）",
        "icon": "qmt",
        "env_keys": ["QMT_PATH", "QMT_ACCOUNT"],
        "optional_env": ["QMT_ACCOUNT_TYPE", "QMT_SESSION_ID"],
        "mcp_server": "qmt",
        "tools_read": ["qmt_query_account", "qmt_query_position", "qmt_query_orders", "qmt_query_stock_data"],
        "tools_trade": ["qmt_place_order", "qmt_cancel_order"],
    },
]


# ---------------------------------------------------------------------------
# Connector Manager
# ---------------------------------------------------------------------------

class ConnectorManager:
    """统一连接器管理器。"""

    def __init__(
        self,
        *,
        mcp_aggregator: Any = None,
        plugin_manager: Any = None,
        gateway_config: Any = None,
        gateway_daemon: Any = None,
    ):
        self._mcp = mcp_aggregator
        self._plugins = plugin_manager
        self._gateway_config = gateway_config
        self._daemon = gateway_daemon

    def list_all(self) -> list[ConnectorInfo]:
        """List all connectors across all types."""
        connectors: list[ConnectorInfo] = []
        connectors.extend(self._list_financial_connectors())
        connectors.extend(self._list_mcp_connectors())
        connectors.extend(self._list_plugin_connectors())
        connectors.extend(self._list_platform_connectors())
        return connectors

    def list_by_type(self, connector_type: str) -> list[ConnectorInfo]:
        """List connectors of a specific type."""
        return [c for c in self.list_all() if c.type == connector_type]

    def list_by_category(self, category: str) -> list[ConnectorInfo]:
        """List connectors of a specific category."""
        return [c for c in self.list_all() if c.category == category]

    def get(self, connector_id: str) -> ConnectorInfo | None:
        """Get a specific connector by ID."""
        for c in self.list_all():
            if c.id == connector_id:
                return c
        return None

    def summary(self) -> dict[str, Any]:
        """Get a summary of all connectors."""
        all_connectors = self.list_all()
        return {
            "total": len(all_connectors),
            "enabled": len([c for c in all_connectors if c.enabled]),
            "connected": len([c for c in all_connectors if c.connected]),
            "by_type": {
                t: len([c for c in all_connectors if c.type == t])
                for t in {"mcp", "plugin", "platform", "financial"}
            },
            "by_status": {
                s: len([c for c in all_connectors if c.status == s])
                for s in {"ready", "disconnected", "error", "unconfigured"}
            },
        }

    # -----------------------------------------------------------------------
    # Financial connectors
    # -----------------------------------------------------------------------

    def _list_financial_connectors(self) -> list[ConnectorInfo]:
        """List financial software connectors."""
        results: list[ConnectorInfo] = []
        for defn in FINANCIAL_CONNECTORS:
            env_keys = defn.get("env_keys", [])
            missing = [k for k in env_keys if not os.getenv(k, "").strip()]
            configured = not missing  # All required env vars present (or none required)
            enabled = configured  # Auto-enable if configured

            # Check if MCP server is registered
            connected = False
            if self._mcp and configured:
                server_name = defn.get("mcp_server", "")
                connected = server_name in (self._mcp.server_names() if hasattr(self._mcp, "server_names") else [])

            if not env_keys:
                # No auth required (e.g., eastmoney public data)
                configured = True
                status = "ready"
            elif missing:
                status = "unconfigured"
            elif connected:
                status = "ready"
            else:
                status = "disconnected"

            safety_metadata = financial_connector_safety_metadata(
                tools_read=defn.get("tools_read", []),
                tools_trade=defn.get("tools_trade", []),
            )
            results.append(ConnectorInfo(
                id=f"financial:{defn['id']}",
                name=defn["name"],
                type="financial",
                category=defn["category"],
                enabled=enabled,
                configured=configured,
                connected=connected,
                status=status,
                description=defn["description"],
                icon=defn.get("icon", ""),
                env_keys=env_keys,
                missing_env=missing,
                metadata={
                    "mcp_server": defn.get("mcp_server"),
                    **safety_metadata,
                },
            ))
        return results

    # -----------------------------------------------------------------------
    # MCP connectors
    # -----------------------------------------------------------------------

    def _list_mcp_connectors(self) -> list[ConnectorInfo]:
        """List MCP Server connectors."""
        if not self._mcp:
            return []

        results: list[ConnectorInfo] = []
        try:
            servers = self._mcp.status() if hasattr(self._mcp, "status") else {}
            for name, info in servers.items():
                results.append(ConnectorInfo(
                    id=f"mcp:{name}",
                    name=name,
                    type="mcp",
                    category="tool",
                    enabled=info.get("enabled", True),
                    configured=True,
                    connected=info.get("connected", False),
                    status="ready" if info.get("connected") else "disconnected",
                    description=info.get("description", ""),
                    metadata={"tool_count": info.get("tool_count", 0)},
                ))
        except Exception:
            pass
        return results

    # -----------------------------------------------------------------------
    # Plugin connectors
    # -----------------------------------------------------------------------

    def _list_plugin_connectors(self) -> list[ConnectorInfo]:
        """List Plugin connectors."""
        if not self._plugins:
            return []

        results: list[ConnectorInfo] = []
        try:
            plugins = self._plugins.list_manifests() if hasattr(self._plugins, "list_manifests") else []
            for plugin in plugins:
                name = plugin.get("name", "unknown")
                results.append(ConnectorInfo(
                    id=f"plugin:{name}",
                    name=name,
                    type="plugin",
                    category="tool",
                    enabled=plugin.get("enabled", True),
                    configured=True,
                    connected=plugin.get("enabled", True),
                    status="ready" if plugin.get("enabled") else "disconnected",
                    description=plugin.get("description", ""),
                    metadata={"version": plugin.get("version", "")},
                ))
        except Exception:
            pass
        return results

    # -----------------------------------------------------------------------
    # Platform connectors
    # -----------------------------------------------------------------------

    def _list_platform_connectors(self) -> list[ConnectorInfo]:
        """List Gateway Platform connectors."""
        from .gateway import PLATFORM_ENV_KEYS

        results: list[ConnectorInfo] = []
        daemon_status = self._daemon.status if self._daemon and self._daemon.is_running else {}
        listeners = daemon_status.get("listeners", {}) if isinstance(daemon_status, dict) else {}

        for platform, env_keys in PLATFORM_ENV_KEYS.items():
            missing = [k for k in env_keys if not os.getenv(k, "").strip()]
            configured = not missing
            listener_info = listeners.get(platform, {})
            connected = listener_info.get("state") == "running" if listener_info else False

            if not configured:
                status = "unconfigured"
            elif connected:
                status = "ready"
            else:
                status = "disconnected"

            results.append(ConnectorInfo(
                id=f"platform:{platform}",
                name=platform,
                type="platform",
                category="communication",
                enabled=configured,
                configured=configured,
                connected=connected,
                status=status,
                env_keys=list(env_keys),
                missing_env=missing,
                metadata={
                    "message_count": listener_info.get("message_count", 0),
                    "last_message_at": listener_info.get("last_message_at"),
                },
            ))
        return results
