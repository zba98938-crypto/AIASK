from __future__ import annotations

import pytest

from strategy_factory.infrastructure.runtime_services import (
    clear_runtime_services,
    configure_runtime_services,
)
from strategy_factory.runtime.incubation import build_incubation_runtime


@pytest.fixture(autouse=True)
def _reset_runtime_services():
    clear_runtime_services()
    yield
    clear_runtime_services()


@pytest.mark.asyncio
async def test_incubation_runtime_contract_keeps_status_and_paper_runtime_shape() -> None:
    async def _scan_and_accept(_db):
        return {"accepted": 0}

    async def _generate(_db, _strategy):
        return {"signals_generated": 0}

    async def _verify(_db, _strategy):
        return {}

    async def _record(_db, _strategy, _verification):
        return None

    async def _verify_pending(_db, **_kwargs):
        return {}

    async def _generate_report(_db, _all, _verifications, _pipeline, **_kwargs):
        return {"hit_rate_dashboard": {"overall": {}}}

    async def _write_feedback(_db, _report):
        return {}

    async def _evaluate_batch(_db, _incubating, _verifications):
        return {}

    async def _check_alerts(_db, run_result=None):
        del run_result
        return {}

    class _Support:
        def __init__(self, **kwargs):
            self.run_time = kwargs.get("run_time")
            self.dry_run = kwargs.get("dry_run", False)
            self.owns_paper_trading = kwargs.get("owns_paper_trading")
            self._strategy_timeout_sec = 1
            self._batch_timeout_sec = 1
            self._error_count = 0
            self._run_count = 0
            self._last_run_at = None
            self._last_result = None
            self._intake = type("Intake", (), {"scan_and_accept": staticmethod(_scan_and_accept)})()
            self._signal_generator = type("Signals", (), {"generate": staticmethod(_generate)})()
            self._forward_verifier = type("Verifier", (), {"verify": staticmethod(_verify)})()
            self._metrics_recorder = type("Metrics", (), {"record": staticmethod(_record)})()
            self._trade_prediction_verifier = type("TradePred", (), {"verify_pending": staticmethod(_verify_pending)})()
            self._reporter = type("Reporter", (), {"generate": staticmethod(_generate_report)})()
            self._feedback_writer = type("Feedback", (), {"write": staticmethod(_write_feedback)})()
            self._accelerator = type("Accel", (), {"evaluate_batch": staticmethod(_evaluate_batch)})()
            self._alert_monitor = type("Alert", (), {"check": staticmethod(_check_alerts)})()

        def status(self):
            return {
                "dry_run": self.dry_run,
                "owns_paper_trading": self.owns_paper_trading,
            }

        async def _get_db(self):
            return object()

        async def _close_db(self, _db):
            return None

        async def _run_recompile_remediation(self, _db):
            return {}

        async def _list_incubating(self, _db):
            return []

        async def _list_paper_observation(self, _db):
            return []

        async def _list_diagnostic_observation(self, _db):
            return []

        async def _run_signal_only_paper_execution_backlog(self, _db, *, strategies=None):
            return {}

        async def _run_exit_signal_paper_execution(self, _db, *, strategies=None, as_of=None):
            return {}

        async def _run_stale_paper_position_closure(self, _db, *, strategies=None):
            return {}

        async def _run_native_execution_evidence_backfill(self, _db, *, strategies=None):
            return {}

        async def _run_execution_audit_acceptance(self, _db, *, strategies=None):
            return {"status": "pending_evidence", "blockers": []}

        async def _run_execution_audit_remediation(self, _db, *, strategies=None, acceptance_result=None):
            return {}

        async def _run_pipeline(self, _db, *, strategies=None):
            return {"count": 0, "auto_promoted": 0, "stage_counts": {}}

        async def _heartbeat(self, _db, _run_id):
            return None

        async def _start_paper_trading_daemons(self):
            return None

        async def _stop_paper_trading_daemons(self):
            return None

    configure_runtime_services(
        incubation_runtime_factory=_Support,
        incubation_runtime_support_factory=_Support,
    )

    runtime = build_incubation_runtime(dry_run=True, owns_paper_trading=False)
    status = runtime.status()
    result = await runtime.run_once()

    assert status["dry_run"] is True
    assert status["owns_paper_trading"] is False
    assert result["status"] == "completed"
    assert "pipeline" in result
    assert "execution_audit_acceptance" in result
