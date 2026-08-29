from __future__ import annotations

from strategy_factory.api.contracts import (
    ExecutableStrategy,
    ExecutionUniverseContract,
    ExecutionUniverseQuery,
    ExecutionUniverseStrategy,
)
from strategy_factory.contracts.execution_universe import (
    ExecutableStrategy as CanonicalExecutableStrategy,
)


def test_execution_universe_contract_is_owned_by_strategy_factory() -> None:
    assert ExecutionUniverseContract.__module__ == "strategy_factory.contracts.execution_universe"
    assert ExecutionUniverseQuery.__module__ == "strategy_factory.contracts.execution_universe"
    assert ExecutionUniverseStrategy.__module__ == "strategy_factory.contracts.execution_universe"


def test_execution_universe_aliases_match_canonical_contract() -> None:
    assert ExecutableStrategy is ExecutionUniverseStrategy
    assert CanonicalExecutableStrategy is ExecutionUniverseStrategy
