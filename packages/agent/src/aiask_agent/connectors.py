"""统一连接器管理 — 聚合 MCP Server、Gateway Platform、Plugin 为统一视图。

提供 /v1/connectors API 的后端逻辑。
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import uuid4

from .connector_safety import financial_connector_safety_metadata, safe_financial_connector_capabilities
from .gateway import GatewayConfigStore, PLATFORM_ENV_KEYS, HERMES_PLATFORM_MATRIX, normalize_platform
from .mcp_client import MCPAggregator
from .plugin_runtime import NativePluginManager
from .session_store import now_iso


# Connector types
CONNECTOR_TYPE_PLATFORM = "platform"
CONNECTOR_TYPE_FINANCE = "finance"
CONNECTOR_TYPE_MCP = "mcp"
CONNECTOR_TYPE_PLUGIN = "plugin"

# Known finance connectors
FINANCE_CONNECTORS = {
    "tongdaxin": {
        "display_name": "通达信",
        "description": "通达信行情与交易",
        "mcp_server": "tongdaxin",
        "required_env": ["TDX_SERVER_IP"],
        "optional_env": ["TDX_SERVER_PORT", "TDX_TRADE_SERVER", "TDX_TRADE_ACCOUNT"],
        "capabilities": {
            "realtime_quote": True,
            "kline_history": True,
            "minute_data": True,
            "tick_data": True,
            "finance_info": True,
            "place_order": False,
            "cancel_order": False,
            "query_position": True,
            "live_trading": False,
        },
        "tools_read": ["tdx_realtime_quote", "tdx_kline_history", "tdx_minute_data", "tdx_tick_data", "tdx_finance_info", "tdx_market_snapshot"],
        "tools_trade": ["tdx_place_order", "tdx_cancel_order", "tdx_query_position", "tdx_query_orders", "tdx_query_balance"],
    },
    "tonghuashun": {
        "display_name": "同花顺",
        "description": "同花顺行情与交易（easytrader）",
        "mcp_server": "tonghuashun",
        "required_env": ["THS_CLIENT_PATH"],
        "optional_env": ["THS_TRADE_ACCOUNT", "THS_BROKER"],
        "capabilities": {
            "realtime_quote": False,
            "query_balance": True,
            "query_position": True,
            "place_order": False,
            "cancel_order": False,
            "live_trading": False,
        },
        "tools_read": ["ths_query_balance", "ths_query_position", "ths_query_orders", "ths_query_deals"],
        "tools_trade": ["ths_place_order", "ths_cancel_order"],
    },
    "eastmoney": {
        "display_name": "东方财富",
        "description": "东方财富数据服务（efinance）",
        "mcp_server": "eastmoney",
        "required_env": [],
        "optional_env": ["EM_API_TOKEN"],
        "capabilities": {
            "realtime_quote": True,
            "kline_history": True,
            "fund_info": True,
            "bond_data": True,
            "futures_data": True,
            "news_flow": True,
            "dragon_tiger": True,
            "live_trading": False,
        },
        "tools_read": ["em_realtime_quote", "em_kline_history", "em_fund_info", "em_fund_nav", "em_bond_data", "em_futures_data", "em_news_flow", "em_dragon_tiger_list"],
        "tools_trade": [],
    },
    "qmt": {
        "display_name": "MiniQMT",
        "description": "迅投 QMT 量化交易",
        "mcp_server": "qmt",
        "required_env": ["QMT_PATH", "QMT_ACCOUNT"],
        "optional_env": ["QMT_ACCOUNT_TYPE", "QMT_SESSION_ID"],
        "capabilities": {
            "query_account": True,
            "query_position": True,
            "place_order": False,
            "cancel_order": False,
            "query_stock_data": True,
            "live_trading": False,
        },
        "tools_read": ["qmt_query_account", "qmt_query_position", "qmt_query_orders", "qmt_query_stock_data"],
        "tools_trade": ["qmt_place_order", "qmt_cancel_order"],
    },
}


class ConnectorManager:
    """统一连接器管理器。"""

    def __init__(
        self,
        *,
        gateway_config: GatewayConfigStore | None = None,
        mcp: MCPAggregator | None = None,
        plugins: NativePluginManager | None = None,
    ) -> None:
        self.gateway_config = gateway_config or GatewayConfigStore()
        self.mcp = mcp or MCPAggregator()
        self.plugins = plugins or NativePluginManager()

    def list_all(self) -> list[dict[str, Any]]:
        """列出所有连接器。"""
        connectors: list[dict[str, Any]] = []
        connectors.extend(self._list_platforms())
        connectors.extend(self._list_finance())
        connectors.extend(self._list_mcp())
        connectors.extend(self._list_plugins())
        return connectors

    def summary(self) -> dict[str, Any]:
        """连接器概览。"""
        all_connectors = self.list_all()
        by_type: dict[str, list[dict[str, Any]]] = {}
        for c in all_connectors:
            by_type.setdefault(c["type"], []).append(c)

        return {
            "object": "aiask.connectors_summary",
            "total": len(all_connectors),
            "connected": sum(1 for c in all_connectors if c["status"] == "connected"),
            "configured": sum(1 for c in all_connectors if c["status"] in ("connected", "configured")),
            "by_type": {
                t: {"count": len(items), "connected": sum(1 for i in items if i["status"] == "connected")}
                for t, items in by_type.items()
            },
            "connectors": all_connectors,
        }

    def get(self, connector_type: str, name: str) -> dict[str, Any] | None:
        """获取单个连接器详情。"""
        for c in self.list_all():
            if c["type"] == connector_type and c["name"] == name:
                return c
        return None

    def test_connection(self, connector_type: str, name: str) -> dict[str, Any]:
        """测试连接器连接。"""
        connector = self.get(connector_type, name)
        if connector is None:
            return {"ok": False, "error": f"Connector not found: {connector_type}/{name}"}

        if connector["status"] == "unconfigured":
            return {
                "ok": False,
                "error": "Connector is not configured",
                "required_env": connector.get("required_env", []),
            }

        # For MCP servers, try to list tools
        if connector_type == CONNECTOR_TYPE_MCP:
            try:
                servers = self.mcp.servers_summary(include_all=True)
                for s in servers:
                    if s.get("name") == name:
                        return {"ok": True, "status": "connected", "tools": s.get("tools", [])}
                return {"ok": False, "error": "MCP server not found in registry"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        return {"ok": True, "status": connector["status"]}

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _list_platforms(self) -> list[dict[str, Any]]:
        """列出通信平台连接器。"""
        results = []
        for platform_info in self.gateway_config.platforms():
            name = str(platform_info.get("name") or "")
            if name in ("local", "api_server"):
                continue
            results.append({
                "type": CONNECTOR_TYPE_PLATFORM,
                "name": name,
                "display_name": name.replace("_", " ").title(),
                "status": self._platform_status(platform_info),
                "enabled": bool(platform_info.get("enabled")),
                "configured": bool(platform_info.get("configured")),
                "required_env": list(PLATFORM_ENV_KEYS.get(name, ())),
            })
        return results

    def _list_finance(self) -> list[dict[str, Any]]:
        """列出金融软件连接器。"""
        results = []
        mcp_servers = {s.get("name"): s for s in self.mcp.servers_summary(include_all=True)}

        for name, info in FINANCE_CONNECTORS.items():
            required_env = info.get("required_env", [])
            configured = all(
                str(os.getenv(key) or "").strip()
                for key in required_env
            ) if required_env else True

            mcp_server = mcp_servers.get(info.get("mcp_server", ""))
            mcp_running = bool(mcp_server and mcp_server.get("status") == "running")

            status = "connected" if (configured and mcp_running) else ("configured" if configured else "unconfigured")
            safety_metadata = financial_connector_safety_metadata(
                tools_read=info.get("tools_read", []),
                tools_trade=info.get("tools_trade", []),
            )

            results.append({
                "type": CONNECTOR_TYPE_FINANCE,
                "name": name,
                "display_name": info["display_name"],
                "description": info["description"],
                "status": status,
                "configured": configured,
                "mcp_server": info.get("mcp_server"),
                "mcp_running": mcp_running,
                "required_env": required_env,
                "capabilities": safe_financial_connector_capabilities(info.get("capabilities", {})),
                **safety_metadata,
            })
        return results

    def _list_mcp(self) -> list[dict[str, Any]]:
        """列出 MCP Server 连接器（排除金融类）。"""
        finance_mcp_names = {info.get("mcp_server") for info in FINANCE_CONNECTORS.values()}
        results = []
        for server in self.mcp.servers_summary(include_all=True):
            name = str(server.get("name") or "")
            if name in finance_mcp_names:
                continue
            status_val = str(server.get("status") or "unknown")
            results.append({
                "type": CONNECTOR_TYPE_MCP,
                "name": name,
                "display_name": name.replace("-", " ").replace("_", " ").title(),
                "status": "connected" if status_val == "running" else status_val,
                "tools_count": len(server.get("tools") or []),
                "transport": server.get("transport"),
            })
        return results

    def _list_plugins(self) -> list[dict[str, Any]]:
        """列出插件连接器。"""
        results = []
        for plugin in self.plugins.list():
            name = str(plugin.get("name") or "")
            enabled = bool(plugin.get("enabled"))
            results.append({
                "type": CONNECTOR_TYPE_PLUGIN,
                "name": name,
                "display_name": name.replace("-", " ").replace("_", " ").title(),
                "status": "connected" if enabled else "disabled",
                "enabled": enabled,
                "hooks_count": len(plugin.get("hooks") or []),
                "tools_count": len(plugin.get("tools") or []),
            })
        return results

    @staticmethod
    def _platform_status(info: dict[str, Any]) -> str:
        if info.get("enabled") and info.get("configured"):
            return "connected"
        if info.get("configured"):
            return "configured"
        return "unconfigured"
