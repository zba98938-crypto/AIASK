from __future__ import annotations

import pytest

from strategy_factory.runtime.factor_mining import FactorMiningRuntime


@pytest.mark.asyncio
async def test_factor_mining_runtime_missing_support_returns_stable_error_shape() -> None:
    runtime = FactorMiningRuntime(None)
    result = await runtime.run_once(trigger="test")

    assert result["success"] is False
    assert result["error"] == "factor_mining_support_missing"
    assert result["trigger"] == "test"
