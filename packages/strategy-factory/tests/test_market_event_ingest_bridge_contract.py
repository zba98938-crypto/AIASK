from __future__ import annotations

import pytest

from strategy_factory.infrastructure.runtime_services import (
    clear_runtime_services,
    configure_runtime_services,
)
from strategy_factory.runtime.market_event_ingest import get_market_event_ingest_runtime


@pytest.fixture(autouse=True)
def _reset_runtime_services():
    clear_runtime_services()
    yield
    clear_runtime_services()


@pytest.mark.asyncio
async def test_market_event_ingest_runtime_missing_support_has_stable_bridge_shape() -> None:
    configure_runtime_services(
        db_provider=lambda: object(),
        market_event_ingest_support_factory=lambda: None,
    )
    runtime = get_market_event_ingest_runtime()
    result = await runtime.run_once()

    assert result["strategy_factory_bridge"] == {}
    assert result["quality_flags"] == ["support_missing"]
    assert result["totals"]["errors"] == 1
