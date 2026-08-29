from __future__ import annotations

from akshare_mcp.tools.managers.strategy_mgr_helpers import build_factory_capability_health


class _FakeDb:
    def save_strategy_factory_run(self, *_args, **_kwargs):
        return None

    def get_latest_strategy_factory_run(self, *_args, **_kwargs):
        return None

    def create_strategy_factory_dispatch(self, *_args, **_kwargs):
        return None

    def get_strategy_factory_dispatch(self, *_args, **_kwargs):
        return None


def test_capability_health_marks_parity_mismatch_unhealthy():
    health = build_factory_capability_health(
        _FakeDb(),
        factory_constants={"STOCK_STRATEGY_MATRIX_ENABLED": True},
        latest_run={
            "status": "success",
            "parity_result": {"status": "mismatch"},
        },
    )

    assert health["factory_runs"]["supported"] is True
    assert health["factory_runs"]["healthy"] is False
    assert health["factory_runs"]["degraded_reason"] == "latest_parity_mismatch"
    assert health["factory_dispatch"]["healthy"] is False
    assert health["factory_bulk_lane"]["enabled"] is True
