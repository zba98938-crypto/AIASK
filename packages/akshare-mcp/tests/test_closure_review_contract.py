from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from akshare_mcp.services.strategy_lifecycle_shared import closure_review as closure_review_module


class _ClosureReviewDb:
    async def list_strategy_status_events(self, strategy_id: str, limit: int = 20):
        assert strategy_id == "strategy-closure"
        return [{"id": "evt-1", "status": "submitted"}]

    async def get_strategy_incubation_account(self, strategy_id: str):
        assert strategy_id == "strategy-closure"
        return {"id": "inc-1", "strategy_id": strategy_id}

    async def get_latest_strategy_incubation_metric(self, strategy_id: str):
        return {"metric_date": "2026-04-21", "primary_skill_lcb": 0.04}

    async def get_paper_account_by_strategy(self, strategy_id: str):
        return {"id": "paper-1", "strategy_id": strategy_id}

    async def list_strategy_paper_orders(self, strategy_id: str, limit: int = 20):
        return [{"id": "order-1", "strategy_id": strategy_id}]

    async def list_strategy_incubation_pipeline_snapshots(self, *, strategy_id: str, limit: int = 10):
        return [{"id": "pipe-1", "strategy_id": strategy_id}]

    async def list_strategy_promotion_reviews(self, *, strategy_id: str, limit: int = 10):
        return [{"id": "promo-1", "strategy_id": strategy_id}]

    async def get_strategy_runtime_control(self, strategy_id: str):
        return {"id": "runtime-1", "metadata": {"correlation_id": "corr-overridden"}}

    async def list_strategy_runtime_risk_events(self, *, strategy_id: str, status: str, limit: int = 20):
        return [{"id": "risk-event-1", "strategy_id": strategy_id, "status": status}]

    async def list_strategy_runtime_risk_snapshots(self, *, strategy_id: str, limit: int = 10):
        return [{"id": "risk-snapshot-1", "strategy_id": strategy_id}]

    async def list_strategy_runtime_alerts(self, *, strategy_id: str, limit: int = 20):
        return [{"id": "alert-1", "strategy_id": strategy_id}]

    async def list_strategy_vector_profiles(self, *, strategy_id: str, limit: int = 10):
        return [{"id": "vector-1", "strategy_id": strategy_id}]

    async def list_vector_index_snapshots(self, *, index_name: str, limit: int = 10):
        assert index_name == "strategy_behavior"
        return [{"id": "vector-index-1", "index_name": index_name}]

    async def list_strategy_domain_events(self, *, strategy_id: str, limit: int = 20):
        return [{"id": "domain-1", "strategy_id": strategy_id}]

    async def list_strategy_projection_snapshots(self, strategy_id: str, limit: int = 20):
        return [{"id": "projection-snapshot-1", "strategy_id": strategy_id, "projection": {"status": "ok"}}]

    async def get_latest_strategy_projection_snapshot(self, strategy_id: str):
        return {"id": "projection-snapshot-1", "strategy_id": strategy_id, "projection": {"status": "ok"}}

    async def list_strategy_generation_experiments(self, *, strategy_id: str, limit: int = 10):
        return [{"id": "exp-1", "strategy_id": strategy_id}]

    async def list_strategy_task_runs(self, *, strategy_id: str, limit: int = 10):
        return [{"id": 11, "strategy_id": strategy_id, "task_name": "strategy_lifecycle_transition"}]

    async def list_strategy_factory_runs(self, limit: int = 5):
        return [
            {
                "run_id": "factory-run-1",
                "completed_at": "2026-04-21T08:00:00+00:00",
                "summary": {
                    "bulk_stock_matrix_loaded_stock_count": 5505,
                    "bulk_stock_matrix_eligible_stock_count": 5167,
                    "bulk_stock_matrix_planned_task_count": 11704,
                    "bulk_stock_task_count": 20,
                },
            }
        ]

    async def get_strategy_factory_topn_snapshot(self, run_id: str):
        assert run_id == "factory-run-1"
        return {
            "available": True,
            "snapshot_id": "fmt_factory-run-1",
            "run_id": run_id,
            "topn_n": 20,
            "metadata": {
                "score_contract_version": "strategy_factory.full_market_topn.v2",
                "score_quality": "healthy",
            },
            "constituents": [{"code": "600000", "rank": 1}],
        }

    async def list_paper_positions(self, account_id: str):
        return [{"id": "pos-1", "account_id": account_id}]

    async def get_paper_nav_rows(self, account_id: str, limit: int = 20):
        return [{"id": "nav-1", "account_id": account_id, "nav_date": "2026-04-21"}]

    async def get_paper_order_summary(self, account_id: str):
        return {"account_id": account_id, "total_orders": 1}


def test_closure_review_aggregates_snapshot_driven_factory_contract(monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    snapshot = {
        "strategy_id": "strategy-closure",
        "snapshot_id": "eas-closure",
        "as_of": today,
        "factory_run_id": "factory-run-1",
        "correlation_id": "corr-1",
        "verdict": {
            "status": "passed",
            "reasons": [],
            "hard_gate_passed": True,
        },
        "verification": {"status": "verified"},
        "audit_summary": {"realized_trade_count": 28},
    }

    async def _fake_latest_quality_report(db, strategy_id: str):
        assert strategy_id == "strategy-closure"
        return {"summary": {"validation_grade": "A"}}

    async def _fake_build_incubation_overview(db, strategy: dict[str, object]):
        assert strategy["id"] == "strategy-closure"
        return {
            "runtime_cycle_seen_today": True,
            "latest_signal_snapshot": {"as_of_date": today},
            "execution_audit_snapshot": snapshot,
        }

    monkeypatch.setattr(closure_review_module, "get_latest_quality_report", _fake_latest_quality_report)
    monkeypatch.setattr(closure_review_module, "build_incubation_overview", _fake_build_incubation_overview)

    result = asyncio.run(
        closure_review_module.build_closure_review(
            _ClosureReviewDb(),
            {"id": "strategy-closure", "strategy_type": "momentum"},
        )
    )

    acceptance = result["incubation"]["execution_audit_acceptance"]
    assert result["strategy_id"] == "strategy-closure"
    assert result["as_of"] == today
    assert result["correlation_id"] == "corr-1"
    assert result["factory_run_id"] == "factory-run-1"
    assert result["stale"] is False
    assert result["events"]["count"] == 1
    assert result["factory"]["latest_run"]["run_id"] == "factory-run-1"
    assert result["factory"]["research_window"]["planned_bulk_task_count"] == 11704
    assert result["factory"]["full_market_topn"]["snapshot_id"] == "fmt_factory-run-1"
    assert result["factory"]["full_market_topn"]["score_contract_version"] == "strategy_factory.full_market_topn.v2"
    assert result["runtime"]["control"]["id"] == "runtime-1"
    assert result["vectors"]["profiles"][0]["id"] == "vector-1"
    assert acceptance["execution_audit_snapshot_id"] == "eas-closure"
    assert acceptance["execution_audit_gate_status"] == "passed"
    assert acceptance["execution_hard_gate_passed"] is True
    assert acceptance["correlation_id"] == "corr-1"


def test_closure_review_bootstraps_execution_snapshot_when_overview_has_no_snapshot(monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()
    snapshot = {
        "strategy_id": "strategy-closure",
        "snapshot_id": "eas-bootstrap",
        "as_of": today,
        "correlation_id": "corr-bootstrap",
        "factory_run_id": "factory-run-bootstrap",
        "verdict": {
            "status": "bootstrap_ready",
            "reasons": ["need_more_realized_trades"],
            "hard_gate_passed": False,
        },
        "verification": {"status": "needs_attention"},
        "audit_summary": {"realized_trade_count": 12},
    }

    class _BootstrapClosureReviewDb(_ClosureReviewDb):
        def __init__(self):
            self.verification_calls = 0

        async def get_execution_audit_verification(self, strategy_id: str):
            assert strategy_id == "strategy-closure"
            self.verification_calls += 1
            return {"status": "needs_attention"}

        async def get_latest_execution_audit_snapshot(self, strategy_id: str):
            assert strategy_id == "strategy-closure"
            if self.verification_calls <= 0:
                return None
            return snapshot

    async def _fake_latest_quality_report(db, strategy_id: str):
        assert strategy_id == "strategy-closure"
        return {"summary": {"validation_grade": "B"}}

    async def _fake_build_incubation_overview(db, strategy: dict[str, object]):
        assert strategy["id"] == "strategy-closure"
        return {
            "status": "submitted",
            "runtime_cycle_seen_today": False,
            "latest_signal_snapshot": {"as_of_date": today},
        }

    monkeypatch.setattr(closure_review_module, "get_latest_quality_report", _fake_latest_quality_report)
    monkeypatch.setattr(closure_review_module, "build_incubation_overview", _fake_build_incubation_overview)

    db = _BootstrapClosureReviewDb()
    result = asyncio.run(
        closure_review_module.build_closure_review(
            db,
            {"id": "strategy-closure", "strategy_type": "momentum"},
        )
    )

    overview = result["incubation"]["overview"]
    assert db.verification_calls == 1
    assert result["correlation_id"] == "corr-bootstrap"
    assert result["factory_run_id"] == "factory-run-bootstrap"
    assert result["data_freshness"]["execution_audit_as_of"] == today
    assert overview["execution_audit_snapshot_id"] == "eas-bootstrap"
    assert overview["execution_audit_gate_status"] == "bootstrap_ready"
