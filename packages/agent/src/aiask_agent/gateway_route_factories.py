from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .gateway import (
    DeliveryRouter,
    GatewayChannelDirectoryStore,
    GatewayConfigStore,
    GatewayMessageStore,
    GatewayRuntime,
    adapter_for as gateway_adapter_for,
    normalize_platform as normalize_gateway_platform,
)
from .runtime import AgentRuntime
from .webhooks import WebhookStore


class GatewayRouteFactories:
    adapter_for = staticmethod(gateway_adapter_for)
    normalize_platform = staticmethod(normalize_gateway_platform)

    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        app: Any,
        daemon_getter: Callable[[], Any],
    ) -> None:
        self._runtime = runtime
        self._app = app
        self._daemon_getter = daemon_getter

    def message_store(self) -> GatewayMessageStore:
        return GatewayMessageStore(self._runtime.session_store.path)

    def directory_store(self) -> GatewayChannelDirectoryStore:
        return GatewayChannelDirectoryStore(self._runtime.session_store.path)

    def runtime(self) -> GatewayRuntime:
        return GatewayRuntime(messages=self.message_store())

    def config_store(self) -> GatewayConfigStore:
        return GatewayConfigStore()

    def delivery_router(
        self,
        *,
        messages: GatewayMessageStore | None = None,
        directory: GatewayChannelDirectoryStore | None = None,
    ) -> DeliveryRouter:
        return DeliveryRouter(
            config=self.config_store(),
            messages=messages or self.message_store(),
            directory=directory or self.directory_store(),
        )

    def daemon_status_payload(self) -> dict[str, Any]:
        from .gateway_daemon import daemon_enabled

        daemon = self._daemon_getter()
        if daemon is None:
            return {"object": "gateway.daemon", "data": {"enabled": daemon_enabled(), "running": False, "listeners": {}}}
        return {"object": "gateway.daemon", "data": daemon.status()}

    def connector_manager(self, *, include_daemon: bool = False) -> Any:
        from .connector_manager import ConnectorManager

        return ConnectorManager(
            mcp_aggregator=getattr(self._runtime, "_mcp_aggregator", None),
            plugin_manager=getattr(self._runtime, "_plugin_manager", None),
            gateway_config=GatewayConfigStore(),
            gateway_daemon=getattr(self._app.state, "_daemon", None) if include_daemon and hasattr(self._app, "state") else None,
        )

    def webhook_store(self) -> WebhookStore:
        return WebhookStore(self._runtime.session_store.path)
