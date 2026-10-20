from __future__ import annotations

import pytest

from strategy_factory.application.signal_tracker.orchestrator import (
    SignalTrackerOrchestrator,
)


@pytest.mark.asyncio
async def test_open_position_strategies_are_added_even_when_loader_caps_exclude_them():
    class DB:
        async def list_strategy_trade_positions(self, *, status=None, limit=1000):
            assert status == "open"
            return [
                {"strategy_id": "held-submitted"},
                {"strategy_id": "held-rejected"},
                {"strategy_id": "held-missing"},
                {"strategy_id": "held-submitted"},
            ]

        async def get_strategy(self, strategy_id):
            if strategy_id == "held-missing":
                return None
            return {
                "id": strategy_id,
                "status": "rejected" if strategy_id == "held-rejected" else "submitted",
            }

    strategies = await SignalTrackerOrchestrator(None)._load_open_position_strategies(DB())

    assert [item["id"] for item in strategies] == ["held-submitted"]


@pytest.mark.asyncio
async def test_open_position_strategy_loader_is_optional():
    class DB:
        pass

    strategies = await SignalTrackerOrchestrator(None)._load_open_position_strategies(DB())

    assert strategies == []
