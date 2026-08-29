from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

from akshare_mcp.tools.managers import strategy_mgr_lifecycle as lifecycle


class _DispatchDb:
    def __init__(self) -> None:
        self.dispatches: dict[str, dict] = {}

    async def create_strategy_factory_dispatch(self, payload: dict) -> dict:
        item = dict(payload)
        self.dispatches[item["dispatch_id"]] = item
        return item

    async def get_strategy_factory_dispatch(self, dispatch_id: str) -> dict | None:
        return self.dispatches.get(dispatch_id)


class _StatusDb:
    async def get_latest_strategy_factory_run(self):
        return {
            "run_id": "factory_run_latest",
            "status": "success",
            "started_at": "2026-05-21T00:00:00+00:00",
            "completed_at": "2026-05-21T00:01:00+00:00",
            "summary": {"submitted": 1},
            "stages": {},
            "parity_result": {},
        }

    async def list_strategy_factory_runs(self, limit: int = 5):
        return [await self.get_latest_strategy_factory_run()]


def test_factory_run_once_queues_dispatch_by_default(monkeypatch) -> None:
    monkeypatch.delenv("STRATEGY_FACTORY_INLINE_EXECUTION_ENABLED", raising=False)

    def _boom(_db):
        raise AssertionError("scheduler must not be constructed by default")

    monkeypatch.setattr(lifecycle, "_get_strategy_factory_scheduler_with_runtime", _boom)
    db = _DispatchDb()

    result = asyncio.run(
        lifecycle.handle_factory_run_once(
            db,
            {"execution_mode": "shadow_readonly", "target_codes": ["600000"]},
        )
    )

    assert result["success"] is True
    assert result["data"]["queued"] is True
    assert result["data"]["execution_owner"] == "strategy_factory_runner"
    dispatch_id = result["data"]["dispatch_id"]
    assert db.dispatches[dispatch_id]["status"] == "queued"
    assert db.dispatches[dispatch_id]["metadata"]["target_codes"] == ["600000"]


def test_factory_dispatch_status_reads_storage_without_scheduler(monkeypatch) -> None:
    monkeypatch.delenv("STRATEGY_FACTORY_INLINE_EXECUTION_ENABLED", raising=False)

    def _boom(_db):
        raise AssertionError("scheduler must not be constructed for dispatch status")

    monkeypatch.setattr(lifecycle, "_get_strategy_factory_scheduler_with_runtime", _boom)
    db = _DispatchDb()
    db.dispatches["dispatch_1"] = {"dispatch_id": "dispatch_1", "status": "queued", "metadata": {}}

    result = asyncio.run(lifecycle.handle_factory_dispatch_status(db, {"dispatch_id": "dispatch_1"}))

    assert result["success"] is True
    assert result["data"]["dispatch_id"] == "dispatch_1"
    assert result["data"]["status"] == "queued"


def test_factory_dispatch_run_inline_passes_target_codes(monkeypatch) -> None:
    monkeypatch.setenv("STRATEGY_FACTORY_INLINE_EXECUTION_ENABLED", "1")
    calls: list[dict] = []

    class _Scheduler:
        async def dispatch_run(self, db=None, *, execution_mode=None, target_codes=None):
            calls.append(
                {
                    "db": db,
                    "execution_mode": execution_mode,
                    "target_codes": list(target_codes or []),
                }
            )
            return {
                "dispatch_id": "dispatch_inline",
                "status": "queued",
                "accepted": True,
                "queued": True,
                "already_running": False,
            }

    db = _DispatchDb()
    monkeypatch.setattr(
        lifecycle._lifecycle_support,
        "_get_strategy_factory_scheduler_with_runtime",
        lambda resolved_db: _Scheduler(),
    )

    result = asyncio.run(
        lifecycle.handle_factory_dispatch_run(
            db,
            {"execution_mode": "shadow_readonly", "target_codes": ["600000", "000001"]},
        )
    )

    assert result["success"] is True
    assert result["data"]["dispatch_id"] == "dispatch_inline"
    assert calls == [
        {
            "db": db,
            "execution_mode": "shadow_readonly",
            "target_codes": ["600000", "000001"],
        }
    ]


def test_factory_status_reads_persisted_runs_without_scheduler(monkeypatch) -> None:
    def _boom(_db):
        raise AssertionError("scheduler must not be constructed for factory_status")

    monkeypatch.setattr(lifecycle, "_get_strategy_factory_scheduler_with_runtime", _boom)

    result = asyncio.run(lifecycle.handle_factory_status(_StatusDb(), {"recent_run_limit": 1}))

    assert result["success"] is True
    assert result["data"]["running"] is False
    assert result["data"]["ownership"]["mode"] == "external_runner"
    assert result["data"]["last_result"]["run_id"] == "factory_run_latest"


def test_recent_factory_status_surfaces_strict_incubation_blockers(monkeypatch) -> None:
    from akshare_mcp.tools.managers.strategy_mgr_helpers import (
        build_strict_incubation_blocker_summary,
    )

    summary = build_strict_incubation_blocker_summary(
        [
            {
                "run_id": "factory_run_recent",
                "summary": {"submitted": 3},
                "submission_artifact": {
                    "strategy_briefs": [
                        {
                            "strategy_id": "s1",
                            "candidate_family": "momentum",
                            "raw_validation_grade": "A",
                            "submission_lane": "observe_incubation",
                            "strict_incubation_ready": False,
                            "formal_track_requested": True,
                            "formal_track_blockers": [
                                "diagnostic_only_runtime",
                                "execution_readiness_tier:missing",
                            ],
                            "admission_block_reasons": [
                                "default_profile_not_allowed_for_single_name_runtime"
                            ],
                        },
                        {
                            "strategy_id": "s2",
                            "candidate_family": "quality_factor",
                            "raw_validation_grade": "B",
                            "submission_lane": "diagnostic_observation",
                            "strict_incubation_ready": False,
                            "diagnostic_only": True,
                            "execution_readiness_tier": "missing_executable_contract",
                        },
                    ]
                },
            }
        ],
        {},
        limit=1,
    )

    blockers = {item["reason_code"]: item["count"] for item in summary["top_blockers"]}

    assert summary["status"] == "blocked"
    assert summary["strict_ready_given_raw_b_rate"] == 0.0
    assert blockers["diagnostic_only_not_allowed_for_incubation"] == 2
    assert blockers["execution_readiness_tier:missing_executable_contract"] == 2
    assert blockers["default_profile_not_allowed_for_single_name_runtime"] == 1
    assert summary["sample_blocked_strategies"][0]["strategy_id"] == "s1"


def test_mcp_server_background_env_only_counts_non_factory_services(monkeypatch) -> None:
    monkeypatch.setenv("FACTOR_SCHEDULER_ENABLED", "1")
    monkeypatch.setenv("MATCHING_ENGINE_ENABLED", "1")
    monkeypatch.setenv("NAV_ENGINE_ENABLED", "1")
    monkeypatch.setenv("SIGNAL_TRACKER_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_FACTORY_ENABLED", "1")
    monkeypatch.setenv("DATA_SYNC_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("STARTUP_VALIDATION_ENABLED", "0")
    server = importlib.import_module("akshare_mcp.server")

    assert server._background_services_env_enabled() is False


def test_mcp_server_source_no_longer_embeds_factory_owned_runtimes() -> None:
    server_path = (
        Path(__file__).resolve().parents[1] / "src" / "akshare_mcp" / "server.py"
    )
    text = server_path.read_text(encoding="utf-8", errors="ignore")

    assert '_try_start("FactorScheduler"' not in text
    assert '_try_start("MatchingEngine"' not in text
    assert '_try_start("NavEngine"' not in text
    assert '_try_start("SignalTracker"' not in text
    assert "get_strategy_factory_scheduler(**build_strategy_factory_scheduler_kwargs())" not in text


def test_strategy_mgr_runtime_uses_canonical_signal_tracker_runtime() -> None:
    runtime_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "akshare_mcp"
        / "tools"
        / "managers"
        / "strategy_mgr_runtime.py"
    )
    text = runtime_path.read_text(encoding="utf-8", errors="ignore")

    assert "from ...services.signal_tracker import get_signal_tracker" not in text
    assert "strategy_factory.runtime.default_bootstrap" in text
    assert "from strategy_factory.runtime.signal_tracker import get_signal_tracker_runtime" in text
    assert "ensure_default_runtime_services()" in text


def test_mcp_server_rejects_background_leader_when_lock_unavailable(monkeypatch) -> None:
    server = importlib.import_module("akshare_mcp.server")
    monkeypatch.setattr(server, "fcntl", None)
    monkeypatch.setattr(server, "_background_services_lock_handle", None)

    assert server._acquire_background_services_leader() is False
