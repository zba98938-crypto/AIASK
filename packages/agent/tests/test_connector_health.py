from __future__ import annotations

import asyncio

import pytest

from aiask_agent.connector_health import ConnectorHealthMonitor


class FakeMcpAggregator:
    def status(self) -> dict:
        return {
            "finance": {
                "connected": True,
                "tool_count": 3,
            }
        }


@pytest.mark.asyncio
async def test_connector_health_monitor_start_is_idempotent_and_stop_awaits_task() -> None:
    monitor = ConnectorHealthMonitor(mcp_aggregator=FakeMcpAggregator(), check_interval_sec=60)

    await monitor.start()
    first_task = monitor._task
    assert first_task is not None
    assert monitor._running is True

    await monitor.start()
    assert monitor._task is first_task

    await monitor.stop()
    assert monitor._running is False
    assert monitor._task is None
    assert first_task.done()


@pytest.mark.asyncio
async def test_connector_health_monitor_records_report() -> None:
    monitor = ConnectorHealthMonitor(mcp_aggregator=FakeMcpAggregator(), check_interval_sec=60)

    report = await monitor.check_all()

    assert report.total_checked == 1
    assert report.healthy_count == 1
    assert monitor.get_metrics()["status"] == "healthy"
