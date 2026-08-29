from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from .gateway import DeliveryRouter, GatewayConfigStore, GatewayMessageStore
from .runtime import AgentRuntime


class AgentAppLifecycle:
    def __init__(
        self,
        *,
        runtime: AgentRuntime,
        full_runtime_getter: Callable[[], AgentRuntime | None],
    ) -> None:
        self._runtime = runtime
        self._full_runtime_getter = full_runtime_getter
        self._daemon: Any = None

    def daemon(self) -> Any:
        return self._daemon

    @asynccontextmanager
    async def lifespan(self, _: Any) -> AsyncIterator[None]:
        if os.getenv("AIASK_GATEWAY_DAEMON_ENABLED", "").strip().lower() in {"1", "true", "yes"}:
            from .gateway_daemon import GatewayDaemon

            try:
                self._daemon = GatewayDaemon(
                    runtime=self._runtime,
                    config=GatewayConfigStore(),
                    router=DeliveryRouter(),
                    messages=GatewayMessageStore(),
                )
                await self._daemon.start()
            except Exception as exc:
                logging.getLogger(__name__).warning("Gateway daemon start failed: %s", exc)
                self._daemon = None

        try:
            yield
        finally:
            if self._daemon is not None:
                await self._daemon.stop()
            full_runtime = self._full_runtime_getter()
            if full_runtime is not None:
                await full_runtime.aclose()
            await self._runtime.aclose()
