from __future__ import annotations

import pytest

from akshare_mcp.integration.strategy_factory_support import (
    IncubationFactoryRunnerAdapter,
)


class _Runner:
    def __init__(self) -> None:
        self.rejected_calls = 0
        self.stale_calls: list[list[dict]] = []
        self.audit_calls: list[list[dict]] = []

    async def _run_rejected_strategy_position_cleanup(self, db):
        self.rejected_calls += 1
        return {"status": "ok", "evaluated": 0}

    async def _run_stale_paper_position_closure(self, db, *, strategies=None):
        self.stale_calls.append(list(strategies or []))
        return {"status": "ok", "evaluated": 0}

    async def _run_execution_audit_acceptance(self, db, *, strategies=None):
        self.audit_calls.append(list(strategies or []))
        return {"status": "ok", "evaluated": 0}


@pytest.mark.asyncio
async def test_adapter_exposes_daemon_cleanup_phases():
    runner = _Runner()
    adapter = IncubationFactoryRunnerAdapter(runner)

    rejected = await adapter.cleanup_rejected_positions("db")
    stale = await adapter.close_stale_paper_positions("db", [{"id": "s1"}])
    audit = await adapter.run_execution_audit_acceptance("db", [{"id": "s1"}])

    assert rejected["status"] == "ok"
    assert stale["status"] == "ok"
    assert audit["status"] == "ok"
    assert runner.rejected_calls == 1
    assert runner.stale_calls == [[{"id": "s1"}]]
    assert runner.audit_calls == [[{"id": "s1"}]]
