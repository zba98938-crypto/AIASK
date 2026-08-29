from __future__ import annotations


def test_factory_mcp_is_owned_by_strategy_factory() -> None:
    import strategy_factory.mcp_server as module

    assert module.mcp is not None
    assert module.READ_ONLY_META["contract_version"] == "aiask.factory_run.v1"

