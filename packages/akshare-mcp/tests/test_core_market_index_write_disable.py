"""Verify the actual warmup-to-run_all disable guard, without market access."""
import importlib.util
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_warmup_preserves_index_disable_before_actual_task_dispatch(monkeypatch, disabled):
    from akshare_mcp import storage
    from akshare_mcp.services.tdx_sync_service import TdxSyncService

    monkeypatch.setattr(storage, "get_db", lambda: object())
    script = Path(__file__).resolve().parents[3] / "scripts/audit_sync_core_market_data.py"
    spec = importlib.util.spec_from_file_location("core_warmup_guard_audit", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # The wrapper should suppress every non-core task; these are the only
    # enabled task bodies. Real wrapper, constructor and run_all remain intact.
    bodies = {}
    for task in module.CORE_MARKET_TDX_TASKS:
        method = "_" + task
        bodies[task] = AsyncMock(return_value={"audit_fixture": True})
        monkeypatch.setattr(TdxSyncService, method, bodies[task])
    original = "sync_index_klines" if disabled else ""
    monkeypatch.setenv("TDX_SYNC_DISABLE", original)
    result = await module._run_tdx_core_tasks(["600000"], warmup_mode=True)
    tasks = {row["task"]: row for row in result["tasks"]}
    assert tasks["sync_index_klines"].get("skipped", False) is disabled
    assert bodies["sync_index_klines"].await_count == (0 if disabled else 1)
    assert bodies["sync_derived_factory_market_data"].await_count == 1
    assert result["summary"]["failed"] == 0
    assert os.environ["TDX_SYNC_DISABLE"] == original
