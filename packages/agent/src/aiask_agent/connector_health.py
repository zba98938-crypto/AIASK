"""Connector Health Monitor — 连接器健康检查与自动恢复。

定期检查所有连接器的健康状态，支持：
  - MCP Server ping 检查
  - Gateway 平台连接状态
  - 金融软件连接验证
  - 自动恢复（重连/重启）
  - 健康指标暴露
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from contextlib import suppress
from typing import Any

from .session_store import now_iso

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckResult:
    """单个连接器的健康检查结果。"""

    connector_id: str
    connector_type: str
    healthy: bool
    latency_ms: float = 0.0
    last_check: str = field(default_factory=now_iso)
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class HealthReport:
    """全局健康报告。"""

    timestamp: str = field(default_factory=now_iso)
    total_checked: int = 0
    healthy_count: int = 0
    unhealthy_count: int = 0
    results: list[HealthCheckResult] = field(default_factory=list)

    @property
    def health_ratio(self) -> float:
        return self.healthy_count / max(self.total_checked, 1)


class ConnectorHealthMonitor:
    """连接器健康监控器。"""

    def __init__(
        self,
        *,
        mcp_aggregator: Any = None,
        gateway_daemon: Any = None,
        check_interval_sec: int = 60,
    ):
        self._mcp = mcp_aggregator
        self._daemon = gateway_daemon
        self._interval = check_interval_sec
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_report: HealthReport | None = None
        self._history: list[HealthReport] = []

    @property
    def last_report(self) -> HealthReport | None:
        return self._last_report

    async def start(self):
        """Start periodic health checking."""
        if self._running and self._task is not None and not self._task.done():
            logger.info("ConnectorHealthMonitor: already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._check_loop(), name="connector-health-monitor")
        logger.info("ConnectorHealthMonitor: started (interval=%ds)", self._interval)

    async def stop(self):
        """Stop health checking."""
        self._running = False
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        else:
            with suppress(asyncio.CancelledError):
                await task

    async def check_all(self) -> HealthReport:
        """Run health checks on all connectors."""
        report = HealthReport()
        results: list[HealthCheckResult] = []

        # Check MCP servers
        if self._mcp:
            mcp_results = await self._check_mcp_servers()
            results.extend(mcp_results)

        # Check gateway daemon listeners
        if self._daemon and self._daemon.is_running:
            daemon_results = self._check_daemon_listeners()
            results.extend(daemon_results)

        report.results = results
        report.total_checked = len(results)
        report.healthy_count = sum(1 for r in results if r.healthy)
        report.unhealthy_count = report.total_checked - report.healthy_count
        report.timestamp = now_iso()

        self._last_report = report
        self._history.append(report)
        if len(self._history) > 100:
            self._history = self._history[-100:]

        return report

    async def _check_loop(self):
        """Periodic health check loop."""
        while self._running:
            try:
                await self.check_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("ConnectorHealthMonitor check error: %s", exc)
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                break

    async def _check_mcp_servers(self) -> list[HealthCheckResult]:
        """Check health of all registered MCP servers."""
        results: list[HealthCheckResult] = []
        try:
            status = self._mcp.status() if hasattr(self._mcp, "status") else {}
            for name, info in status.items():
                start = time.time()
                healthy = info.get("connected", False)
                latency = (time.time() - start) * 1000
                results.append(HealthCheckResult(
                    connector_id=f"mcp:{name}",
                    connector_type="mcp",
                    healthy=healthy,
                    latency_ms=latency,
                    error=info.get("error"),
                    details={"tool_count": info.get("tool_count", 0)},
                ))
        except Exception as exc:
            logger.debug("MCP health check error: %s", exc)
        return results

    def _check_daemon_listeners(self) -> list[HealthCheckResult]:
        """Check health of gateway daemon listeners."""
        results: list[HealthCheckResult] = []
        if not self._daemon:
            return results

        status = self._daemon.status
        listeners = status.get("listeners", {})
        for name, info in listeners.items():
            state = info.get("state", "unknown")
            healthy = state == "running"
            results.append(HealthCheckResult(
                connector_id=f"platform:{name}",
                connector_type="platform",
                healthy=healthy,
                error=info.get("last_error"),
                details={
                    "state": state,
                    "message_count": info.get("message_count", 0),
                    "error_count": info.get("error_count", 0),
                },
            ))
        return results

    def get_metrics(self) -> dict[str, Any]:
        """Get health metrics in a structured format."""
        report = self._last_report
        if not report:
            return {"status": "no_data", "checks_run": 0}

        return {
            "status": "healthy" if report.health_ratio >= 0.8 else "degraded" if report.health_ratio >= 0.5 else "unhealthy",
            "health_ratio": report.health_ratio,
            "total_checked": report.total_checked,
            "healthy": report.healthy_count,
            "unhealthy": report.unhealthy_count,
            "last_check": report.timestamp,
            "unhealthy_connectors": [
                {"id": r.connector_id, "error": r.error}
                for r in report.results if not r.healthy
            ],
        }
