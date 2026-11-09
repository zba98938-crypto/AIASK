from __future__ import annotations

from datetime import date

import pytest

from strategy_factory.application.incubation.orchestrator import IncubationOrchestrator


class _HitrateSupport:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.signal_calls = 0

    async def get_db(self):
        return object()

    async def get_read_only_db(self):
        return object()

    async def run_pipeline(self, _db, _strategies):
        return {}

    async def generate_hit_rate_report(self, *_args, **_kwargs):
        return {}

    async def write_feedback(self, _db, _report):
        return {}

    async def scan_and_accept_strategies(self, _db):
        return {"accepted_count": 0}

    async def run_recompile_remediation(self, _db):
        return {}

    async def list_incubating_strategies(self, _db):
        return [{"id": "s-1", "_intake_stage": "diagnostic"}]

    async def list_paper_observation_strategies(self, _db):
        return []

    async def list_diagnostic_observation_strategies(self, _db):
        return []

    async def generate_signals(self, _db, _strategy):
        self.signal_calls += 1
        return {"signal_date": "2026-10-23", "signals_generated": 1}

    async def write_daily_predictions(self, _db, _strategy, signal_result):
        self.calls.append(("writer", signal_result["signal_date"]))
        return {
            "status": "ok",
            "written": 1,
            "predictions": [{"target_trading_date": "2026-10-26"}],
        }

    async def settle_strategy_orders(self, _db, _strategy, _signals):
        return {"filled_count": 0, "rejected_count": 0}

    async def verify_forward_returns(self, _db, _strategy):
        return {}

    async def record_metrics(self, _db, _strategy, _verification):
        return None

    async def verify_trade_predictions(self, _db):
        self.calls.append(("verify",))
        return {"verified_count": 1}

    async def score_horizon_predictions(self, _db):
        self.calls.append(("horizon",))
        return {"status": "ok", "scored": 1}

    async def run_calibration(self, _db):
        self.calls.append(("calibration",))
        return {"status": "unmeasurable"}

    async def evaluate_selective_gate(self, _db, *, signal_date):
        self.calls.append(("gate", signal_date))
        return {"status": "ok", "signal_date": signal_date.isoformat()}

    async def run_accelerator(self, _db, _strategies, _verifications):
        return {}

    async def monitor_alerts(self, _db):
        return {}


@pytest.mark.asyncio
async def test_hitrate_hooks_follow_toggles_and_reuse_generated_signal(monkeypatch):
    for name in (
        "STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED",
        "STRATEGY_FACTORY_HORIZON_SCORING_ENABLED",
        "STRATEGY_FACTORY_CALIBRATION_ENABLED",
        "STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED",
    ):
        monkeypatch.setenv(name, "1")

    support = _HitrateSupport()
    result = await IncubationOrchestrator(support).run_cycle(trigger="test")

    assert support.signal_calls == 1
    assert result["session_source"] == "wall_clock_store_unreadable"
    assert result["warnings"] == ["session_not_synced"]
    assert [item[0] for item in support.calls] == [
        "writer",
        "verify",
        "horizon",
        "calibration",
        "gate",
    ]
    assert support.calls[0][1] == "2026-10-23"
    assert support.calls[-1][1] == date(2026, 10, 26)
    assert result["daily_predictions"]["written"] == 1
    assert result["daily_predictions"]["target_dates"] == ["2026-10-26"]
    assert result["horizon_scoring"]["scored"] == 1
    assert result["calibration"]["status"] == "unmeasurable"
    assert result["selective_gate"]["signal_date"] == "2026-10-26"


@pytest.mark.asyncio
async def test_hitrate_hooks_are_disabled_and_dry_run_is_write_free(monkeypatch):
    for name in (
        "STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED",
        "STRATEGY_FACTORY_HORIZON_SCORING_ENABLED",
        "STRATEGY_FACTORY_CALIBRATION_ENABLED",
        "STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    support = _HitrateSupport()
    result = await IncubationOrchestrator(support).run_cycle(trigger="test", dry_run=True)
    assert support.signal_calls == 0
    assert support.calls == []
    assert not [item for item in support.calls if item[0] in {"writer", "horizon", "calibration", "gate"}]
    assert result["daily_predictions"]["enabled"] is False
    assert result["horizon_scoring"]["status"] == "disabled"

    for name in (
        "STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED",
        "STRATEGY_FACTORY_HORIZON_SCORING_ENABLED",
        "STRATEGY_FACTORY_CALIBRATION_ENABLED",
        "STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED",
    ):
        monkeypatch.setenv(name, "1")
    support = _HitrateSupport()
    result = await IncubationOrchestrator(support).run_cycle(trigger="test", dry_run=True)
    assert not [item for item in support.calls if item[0] in {"writer", "horizon", "calibration", "gate"}]
    assert result["horizon_scoring"]["status"] == "skipped_dry_run"
    assert result["selective_gate"]["status"] == "skipped_dry_run"
