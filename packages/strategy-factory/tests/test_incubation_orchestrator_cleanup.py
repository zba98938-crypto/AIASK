from __future__ import annotations

import pytest

from strategy_factory.application.incubation.orchestrator import IncubationOrchestrator


class _Support:
    def __init__(self) -> None:
        self.rejected_cleanup_calls = 0
        self.stale_cleanup_calls: list[list[dict]] = []
        self.audit_acceptance_calls: list[list[dict]] = []

    async def get_db(self):
        return object()

    async def scan_and_accept_strategies(self, _db):
        return {"accepted_count": 0}

    async def run_recompile_remediation(self, _db):
        return {}

    async def list_incubating_strategies(self, _db):
        return []

    async def list_paper_observation_strategies(self, _db):
        return []

    async def list_diagnostic_observation_strategies(self, _db):
        return []

    async def generate_signals(self, _db, _strategy):
        return {"signals_generated": 0}

    async def settle_strategy_orders(self, _db, _strategy, _signal_result):
        return {"filled_count": 0, "rejected_count": 0}

    async def verify_forward_returns(self, _db, _strategy):
        return {}

    async def record_metrics(self, _db, _strategy, _verification):
        return None

    async def verify_trade_predictions(self, _db):
        return {}

    async def run_accelerator(self, _db):
        return {}

    async def monitor_alerts(self, _db):
        return {}

    async def cleanup_rejected_positions(self, _db):
        self.rejected_cleanup_calls += 1
        return {"status": "ok", "evaluated": 0}

    async def close_stale_paper_positions(self, _db, strategies):
        self.stale_cleanup_calls.append(list(strategies))
        return {"status": "ok", "evaluated": 0}

    async def run_execution_audit_acceptance(self, _db, strategies):
        self.audit_acceptance_calls.append(list(strategies))
        return {"status": "ok", "evaluated": 0}


@pytest.mark.asyncio
async def test_orchestrator_runs_both_position_cleanup_phases():
    support = _Support()
    result = await IncubationOrchestrator(support).run_cycle(trigger="test", dry_run=True)

    assert support.rejected_cleanup_calls == 1
    assert support.stale_cleanup_calls == [[]]
    assert support.audit_acceptance_calls == [[]]
    phase_names = {phase["phase_name"] for phase in result["phases"]}
    assert "rejected_strategy_position_cleanup" in phase_names
    assert "stale_paper_position_closure" in phase_names
    assert "execution_audit_acceptance" in phase_names
