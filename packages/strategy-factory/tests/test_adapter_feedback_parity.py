"""Behavioral parity: feedback evidence reads survive adapt_repository().

The live research-plane path builds the factor artifact through
``MCPFactorResearchGatewayImpl.build_artifact(adapter_db, snapshot)`` where
``adapter_db`` is a ``StrategyFactoryRepositoryAdapter`` around the raw
``SQLiteAdapter``. Evidence reads inside ``load_budget_feedback`` go through
_call_optional_async, which silently returns the default when a method is
missing on the receiver. A missing ``list_strategy_incubation_metrics`` on the
adapter therefore drops ``min_days_remaining`` for every strategy, zeroes the
maturity counters, and turns the cohort gate into a permanent hard block even
though the raw database has the data.

These tests pin that adapter-path and raw-path feedback summaries are equal,
so the gate can never diverge between the two again.
"""

from __future__ import annotations

import asyncio

import pytest

from strategy_factory.infrastructure.mcp_adapters import (
    StrategyFactoryRepositoryAdapter,
    adapt_repository,
)


def _seed_strategy(adapter, strategy_id: str, metadata: dict) -> None:
    async def _run():
        await adapter.save_strategy(
            {
                "id": strategy_id,
                "name": strategy_id,
                "description": "",
                "strategy_type": "multi_factor",
                "params": {"incubation_budget": {"track": "observe_incubation"}},
                "status": "submitted",
                "tags": [],
            }
        )
    asyncio.run(_run())


def test_adapter_exposes_incubation_evidence_methods() -> None:
    """_call_optional_async must not silently degrade on the adapter."""
    sqlite_module = pytest.importorskip(
        "aiask_quant_core.storage.sqlite", reason="quant-core not installed"
    )
    raw = sqlite_module.SQLiteAdapter(":memory:")
    adapter = adapt_repository(raw)
    assert isinstance(adapter, StrategyFactoryRepositoryAdapter)
    for method in (
        "list_strategy_incubation_metrics",
        "list_strategy_runtime_risk_events",
        "list_strategy_runtime_alerts",
        "get_latest_strategy_promotion_review",
        "list_strategy_quality_reports",
        "get_latest_strategy_quality_report",
    ):
        assert hasattr(adapter, method), (
            f"{method} is missing on the repository adapter; "
            "_call_optional_async feedback reads would silently return defaults"
        )


def test_list_strategies_offset_forwarding_does_not_degrade() -> None:
    """The fallback list_strategies call must not silently drop source strategies."""
    sqlite_module = pytest.importorskip(
        "aiask_quant_core.storage.sqlite", reason="quant-core not installed"
    )
    raw = sqlite_module.SQLiteAdapter(":memory:")
    adapter = adapt_repository(raw)
    from strategy_factory.application.research import _feedback_metrics as fmd

    async def _run():
        await adapter.save_strategy(
            {
                "id": "parity_a",
                "name": "a",
                "description": "",
                "strategy_type": "multi_factor",
                "params": {"incubation_budget": {"track": "observe_incubation"}},
                "status": "submitted",
                "tags": [],
            }
        )
        rows = await fmd.list_feedback_source_strategies(
            __import__("strategy_factory").FactorResearchBuilder, adapter, limit=180
        )
        return rows

    rows = asyncio.run(_run())
    assert [r.get("id") for r in rows] == ["parity_a"]


def test_adapter_path_budget_feedback_parity_with_raw() -> None:
    """min_days_remaining must survive the adapter hop (was the live-block bug)."""
    sqlite_module = pytest.importorskip(
        "aiask_quant_core.storage.sqlite", reason="quant-core not installed"
    )
    raw = sqlite_module.SQLiteAdapter(":memory:")
    adapter = adapt_repository(raw)
    builder_cls = __import__("strategy_factory").FactorResearchBuilder

    async def _run():
        await adapter.save_strategy(
            {
                "id": "parity_b",
                "name": "b",
                "description": "",
                "strategy_type": "multi_factor",
                "params": {"incubation_budget": {"track": "observe_incubation"}},
                "status": "submitted",
                "tags": [],
            }
        )
        # write an incubation metric row with min_days_remaining via the raw db
        assert hasattr(raw, "save_strategy_incubation_metric"), (
            "seed helper missing on the raw adapter; test cannot pin parity"
        )
        await raw.save_strategy_incubation_metric(
            "parity_b",
            "2026-10-21",
            {"metadata": {"min_days_remaining": 45}},
        )
        from strategy_factory.application.runtime import _call_optional_async
        raw_row = await _call_optional_async(
            raw, "list_strategy_incubation_metrics", "parity_b", limit=1, default=[]
        )
        adapter_row = await _call_optional_async(
            adapter, "list_strategy_incubation_metrics", "parity_b", limit=1, default=[]
        )
        assert raw_row, "seed row must be readable via the raw adapter"
        assert adapter_row == raw_row, (
            f"adapter dropped the incubation metric row: raw={raw_row} adapter={adapter_row}"
        )
        raw_fb = await builder_cls._load_budget_feedback(raw, {"date": "2026-10-21"})
        adapter_fb = await builder_cls._load_budget_feedback(adapter, {"date": "2026-10-21"})
        raw_s = dict(raw_fb.get("summary") or {})
        adapt_s = dict(adapter_fb.get("summary") or {})
        assert raw_s.get("strategy_count") == adapt_s.get("strategy_count") == 1
        assert adapt_s.get("pending_maturity_not_ready_strategy_count") == 1, (
            "adapter path lost min_days_remaining; maturity counter would "
            "degrade the cohort gate to a permanent hard block"
        )
        assert raw_s == adapt_s

    asyncio.run(_run())
