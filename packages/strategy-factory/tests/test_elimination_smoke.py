"""Smoke tests for EliminationChecker."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.list_strategies = AsyncMock(return_value=[])
    db.get_strategy_metrics = AsyncMock(return_value=[])
    db.get_signal_stats = AsyncMock(return_value={"hit_rate": {}, "total_signals": 0})
    db.save_elimination_log = AsyncMock(return_value=None)
    db.update_strategy_status = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_elimination_empty_db(mock_db):
    from strategy_factory.application.elimination import EliminationChecker

    checker = EliminationChecker()
    result = await checker.check(mock_db, "neutral")
    assert isinstance(result, list)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_elimination_healthy_strategy_not_eliminated(mock_db):
    from strategy_factory.application.elimination import EliminationChecker

    mock_db.list_strategies = AsyncMock(return_value=[
        {"id": "strat_1", "strategy_type": "momentum"}
    ])
    mock_db.get_strategy_metrics = AsyncMock(return_value=[
        {
            "period": "all",
            "max_drawdown": -0.10,
            "sharpe_ratio": 1.2,
            "win_rate": 0.55,
        }
    ])

    checker = EliminationChecker()
    result = await checker.check(mock_db, "greed")
    assert isinstance(result, list)
    # Healthy strategy should not be eliminated/probated; warnings are allowed.
    hard = [item for item in result if str(item.get("action") or "") in {"deprecated", "probation", "eliminated"}]
    assert hard == []


@pytest.mark.asyncio
async def test_elimination_bad_strategy_eliminated(mock_db):
    from strategy_factory.application.elimination import EliminationChecker

    mock_db.list_strategies = AsyncMock(return_value=[
        {"id": "strat_bad", "strategy_type": "momentum"}
    ])
    mock_db.get_strategy_metrics = AsyncMock(return_value=[
        {
            "period": "all",
            "max_drawdown": -0.45,  # > 30% → fatal
            "sharpe_ratio": -0.5,
            "win_rate": 0.20,
        }
    ])

    checker = EliminationChecker()
    result = await checker.check(mock_db, "neutral")
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["id"] == "strat_bad"
    assert len(result[0]["red_flags"]) >= 1
