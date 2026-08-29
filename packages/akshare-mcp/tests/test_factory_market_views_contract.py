from __future__ import annotations

import asyncio

from akshare_mcp.tools.managers import strategy_mgr_lifecycle as lifecycle_module
from akshare_mcp.tools.managers import strategy_mgr_helpers as helpers_module


def test_factory_run_contracts_surface_research_window_and_topn():
    raw = {
        "run_id": "factory_run_contract",
        "summary": {
            "bulk_stock_matrix_loaded_stock_count": 5505,
            "bulk_stock_matrix_eligible_stock_count": 5167,
            "bulk_stock_matrix_planned_task_count": 11704,
            "bulk_stock_task_count": 20,
            "bulk_stock_matrix_effective_task_budget": 20,
            "bulk_stock_matrix_next_task_offset": 20,
            "full_market_topn": {
                "available": True,
                "snapshot_id": "fmt_factory_run_contract",
                "run_id": "factory_run_contract",
                "topn_n": 20,
                "metadata": {
                    "score_contract_version": "strategy_factory.full_market_topn.v2",
                    "score_quality": "healthy",
                    "tie_cluster_summary": {"top10_distinct_score_count": 5},
                },
                "constituents": [{"code": "600000", "name": "浦发银行", "rank": 1}],
            },
        },
        "stages": {
            "autonomy": {
                "status": "completed",
                "full_market_topn": {
                    "available": True,
                    "snapshot_id": "fmt_factory_run_contract",
                    "run_id": "factory_run_contract",
                    "topn_n": 20,
                    "metadata": {
                        "score_contract_version": "strategy_factory.full_market_topn.v2",
                        "score_quality": "healthy",
                    },
                },
            }
        },
    }

    summary = helpers_module.normalize_factory_run_summary_contract(raw)
    detail = helpers_module.normalize_factory_run_detail_contract(raw)

    assert summary["research_window"]["planned_bulk_task_count"] == 11704
    assert summary["research_window"]["selected_bulk_task_count"] == 20
    assert summary["full_market_topn"]["snapshot_id"] == "fmt_factory_run_contract"
    assert summary["full_market_topn"]["score_contract_version"] == "strategy_factory.full_market_topn.v2"
    assert detail["research_window"]["loaded_stock_count"] == 5505
    assert detail["full_market_topn"]["topn_n"] == 20
    assert detail["full_market_topn"]["score_quality"] == "healthy"


class _FactoryTopnDb:
    async def get_latest_strategy_factory_topn_snapshot(self):
        return {
            "available": True,
            "snapshot_id": "fmt_factory_run_latest",
            "run_id": "factory_run_latest",
            "topn_n": 20,
            "metadata": {
                "score_contract_version": "strategy_factory.full_market_topn.v2",
                "score_quality": "degraded",
                "tie_cluster_summary": {"top10_distinct_score_count": 2},
            },
            "constituents": [{"code": "600000", "rank": 1}],
        }

    async def get_strategy_factory_topn_snapshot(self, run_id: str):
        if run_id != "factory_run_latest":
            return None
        return {
            "available": True,
            "snapshot_id": "fmt_factory_run_latest",
            "run_id": run_id,
            "topn_n": 20,
            "metadata": {
                "score_contract_version": "strategy_factory.full_market_topn.v2",
                "score_quality": "degraded",
                "tie_cluster_summary": {"top10_distinct_score_count": 2},
            },
            "constituents": [{"code": "600000", "rank": 1}],
        }

    async def list_strategy_factory_full_market_scores(self, run_id: str, limit: int = 20):
        assert run_id == "factory_run_latest"
        return [
            {
                "code": "600000",
                "rank": 1,
                "composite_score": 98.2,
                "industry": "银行",
            }
        ][:limit]

    async def count_strategy_factory_full_market_scores(self, run_id: str):
        assert run_id == "factory_run_latest"
        return 5167


def test_factory_topn_handlers_return_snapshot_and_scores():
    db = _FactoryTopnDb()

    latest = asyncio.run(lifecycle_module.handle_factory_topn_latest(db, {"limit": 5}))
    by_run = asyncio.run(
        lifecycle_module.handle_factory_run_topn(
            db,
            {"run_id": "factory_run_latest", "limit": 5},
        )
    )

    assert latest["success"] is True
    assert latest["data"]["snapshot"]["snapshot_id"] == "fmt_factory_run_latest"
    assert latest["data"]["snapshot"]["score_quality"] == "degraded"
    assert latest["data"]["score_row_count"] == 5167
    assert latest["data"]["requested_limit"] == 5
    assert latest["data"]["top_scores"][0]["code"] == "600000"

    assert by_run["success"] is True
    assert by_run["data"]["snapshot"]["run_id"] == "factory_run_latest"
    assert by_run["data"]["snapshot"]["score_contract_version"] == "strategy_factory.full_market_topn.v2"
    assert by_run["data"]["top_scores"][0]["rank"] == 1


class _FactoryRunDetailDb:
    async def get_strategy_factory_run(self, run_id: str):
        assert run_id == "run-artifacts"
        return {
            "run_id": run_id,
            "summary": {"candidates_spawned": 1},
            "stages": {"quality_gate": {"status": "completed", "passed_count": 1}},
            "artifact_refs": [],
        }

    async def list_strategy_factory_run_artifact_refs(self, run_id: str):
        assert run_id == "run-artifacts"
        return [
            {
                "id": 1,
                "run_id": run_id,
                "artifact_type": "quality_gate",
                "artifact_version": "1",
                "payload_hash": "hash-1",
                "storage_mode": "dropped_large_payload",
                "created_at": "2026-05-22T00:00:00Z",
            }
        ]

    async def list_strategy_factory_run_artifacts(self, run_id: str):
        raise AssertionError("default factory_run_detail must not hydrate artifact payloads")


def test_factory_run_detail_defaults_to_artifact_refs_without_payload(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_ENABLE_FULL_ARTIFACT_DETAIL", raising=False)

    result = asyncio.run(
        lifecycle_module.handle_factory_run_detail(
            _FactoryRunDetailDb(),
            {"run_id": "run-artifacts"},
        )
    )

    assert result["success"] is True
    detail = result["data"]
    assert detail["artifact_mode"] == "summary"
    assert detail["artifact_refs"][0]["artifact_type"] == "quality_gate"
    assert "payload_json" not in detail["artifacts"][0]


def test_factory_run_detail_full_mode_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("STRATEGY_FACTORY_ENABLE_FULL_ARTIFACT_DETAIL", raising=False)

    result = asyncio.run(
        lifecycle_module.handle_factory_run_detail(
            _FactoryRunDetailDb(),
            {"run_id": "run-artifacts", "artifact_mode": "full"},
        )
    )

    assert result["success"] is True
    detail = result["data"]
    assert detail["artifact_mode"] == "full"
    assert detail["full_artifact_payload_disabled"] is True
    assert "payload_json" not in detail["artifacts"][0]
