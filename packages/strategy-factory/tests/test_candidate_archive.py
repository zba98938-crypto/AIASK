from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from strategy_factory.application.candidate_archive import (
    read_candidate_archive,
    recording_artifact,
    write_candidate_archive,
)
from strategy_factory.application.cycle_runner import FactoryCycleRunner, FactoryRunContext
from strategy_factory.application.research.runner import ResearchGenerationResult


def _candidates(count=20):
    return [
        {
            "candidate_id": f"candidate-{index}",
            "strategy_type": "momentum",
            "candidate_origin": "local_rule",
            "source": "test_full_recording",
            "params": {"lookback": index + 5, "nested": {"values": list(range(1500))}},
            "strategy_spec": {"entry": {"all": [{"feature": f"f{i}", "gt": i} for i in range(30)]}},
        }
        for index in range(count)
    ]


@pytest.fixture
def archive_dir(tmp_path, monkeypatch):
    directory = tmp_path / "candidate_archives"
    monkeypatch.setenv("AIASK_FACTORY_CANDIDATE_ARCHIVE_DIR", str(directory))
    return directory


class RecordingDB:
    def __init__(self):
        self.artifacts = []

    def __getattr__(self, name):
        from strategy_factory.application.runtime_boundary import REQUIRED_REPOSITORY_METHODS

        if name not in REQUIRED_REPOSITORY_METHODS:
            raise AttributeError(name)

        async def _method(*args, **kwargs):
            return []

        return _method

    async def save_strategy_factory_run_artifact(self, payload):
        self.artifacts.append(copy.deepcopy(payload))
        return self.artifacts[-1]


class ScoringFilter:
    async def filter(self, candidates, db):
        for index, candidate in enumerate(candidates):
            candidate["backtest_outcome"] = {
                "passed": index < 5,
                "reason_code": "passed" if index < 5 else f"weak_backtest_{index}",
                "thresholds": {"trade_count": 8},
            }
            candidate["params"]["evaluation_marker"] = True
        self.candidates = candidates
        return candidates[:5]

    def get_last_report(self):
        return {
            "summary": {"input_count": len(self.candidates), "passed_count": 5, "failed_count": len(self.candidates) - 5},
            "passed": self.candidates[:5],
            "failed": self.candidates[5:],
        }


class RecordingDeduplicator:
    async def deduplicate(self, candidates, db):
        for index, candidate in enumerate(candidates):
            candidate["dedup_result"] = {
                "duplicate": index == len(candidates) - 1,
                "reason_code": "duplicate_tail" if index == len(candidates) - 1 else "unique",
            }
        self.last = candidates[-1]
        return candidates[:-1]

    def get_last_report(self):
        return {"dropped": [self.last], "summary": {"dropped_count": 1}}


class RecordingSubmitter:
    async def submit(self, candidates, snapshot, db, *, read_only=False):
        return {
            "created_total": len(candidates),
            "submitted": len(candidates),
            "strategies": [
                {"strategy_id": f"strategy-{i}", "candidate_id": item["candidate_id"], "status": "submitted"}
                for i, item in enumerate(candidates)
            ],
        }


class RecordingScheduler:
    def __init__(self):
        self.deduplicator = RecordingDeduplicator()
        self.submitter = RecordingSubmitter()

    def _now(self):
        return datetime(2026, 11, 4, 9, tzinfo=timezone.utc)

    def _with_stage_meta(self, stage, trace_id, payload, **kwargs):
        return {**payload, "stage": stage, "trace_id": trace_id, **kwargs}

    def _apply_run_audit(self, results, persistence_failures=None):
        return results

    def _get_factor_research_gateway(self):
        return object()

    def _compact_factor_research_snapshot(self, payload):
        return {"summary": payload.get("summary", {})}

    def _build_deduplicator(self, pkg):
        return self.deduplicator

    def _build_submitter(self, pkg):
        return self.submitter

    def _aggregate_vector_submission_metrics(self, payload):
        return {}

    def _aggregate_backtest_audit_metrics(self, payload):
        return {}

    def _aggregate_submission_audit_metrics(self, payload):
        return {}


def _runner(monkeypatch, candidates, *, db=None, read_only=False, execution_mode="stock_first_observe_primary"):
    class Collector:
        async def collect(self, db):
            return {"date": "2026-11-03", "fg_level": "neutral"}

    class Eliminator:
        async def check(self, db, level):
            return []

    db = db or RecordingDB()
    adapters = SimpleNamespace(**{
        name: object()
        for name in ("repository", "vector_search", "autonomy", "factor_research", "incubation", "validation", "risk")
    })
    scheduler = RecordingScheduler()
    runner = FactoryCycleRunner(scheduler, FactoryRunContext(
        db=db,
        factory_pkg=SimpleNamespace(DataCollector=Collector, BacktestFilter=ScoringFilter, EliminationChecker=Eliminator),
        runtime_adapters=adapters,
        start=scheduler._now(),
        trace_id="trace-full-recording",
        run_id="run-full-recording",
        read_only=read_only,
        execution_mode=execution_mode,
    ))

    async def generation(db, snapshot):
        return ResearchGenerationResult(local_candidates=candidates, generated_candidates=candidates)

    async def factor_research(gateway, db, snapshot):
        return {"summary": {"active_factor_count": 1}}

    monkeypatch.setattr(runner._research_runner, "run_generation", generation)
    monkeypatch.setattr(runner, "_build_factor_research_artifact", factor_research)
    monkeypatch.setattr(runner, "_build_factory_readiness", lambda *args: {"can_proceed": True})
    monkeypatch.setattr("strategy_factory.application.cycle_runner.is_factory_runtime_enabled", lambda: True)
    return runner, db


def test_canonical_cycle_records_all_generated_and_rejected_candidates(archive_dir, monkeypatch):
    candidates = _candidates()
    expected = copy.deepcopy(candidates)
    runner, db = _runner(monkeypatch, candidates)

    outcome = asyncio.run(runner.run())

    assert outcome.result["status"] == "success", outcome.result.get("error")
    recording = outcome.result["candidate_recording"]
    assert recording["complete"] is True
    generated = read_candidate_archive(recording["generated"])
    evaluated = read_candidate_archive(recording["evaluated"])
    assert generated["candidates"] == expected
    assert len(evaluated["candidates"]) == 20
    assert evaluated["candidates"][-1]["candidate_id"] == "candidate-19"
    assert evaluated["candidates"][-1]["backtest_outcome"]["reason_code"] == "weak_backtest_19"
    assert evaluated["candidates"][-1]["dedup_result"]["reason_code"] == "duplicate_tail"
    assert evaluated["candidates"][-1]["params"]["nested"] == expected[-1]["params"]["nested"]
    assert len(evaluated["backtest_report"]["failed"]) == 15
    assert evaluated["dedup_report"]["dropped"][0]["candidate_id"] == "candidate-19"
    assert len(evaluated["submit_result"]["strategies"]) == 19
    assert len(outcome.result["research_plane"]["candidate_artifact"]["candidate_briefs"]) == 12
    assert len(outcome.result["backtest_report"]["diagnostics"]["failed_preview"]) == 12
    assert outcome.result["summary"]["recorded_candidate_count"] == 20
    assert len(db.artifacts) == 2
    assert db.artifacts[0]["payload_json"]["complete"] is False
    assert db.artifacts[1]["payload_json"]["complete"] is True
    manifest = next(item for item in outcome.result["artifacts"] if item["artifact_type"] == "candidate_recording")
    assert manifest["storage_mode"] == "external_gzip_json"
    assert len(json.dumps(manifest).encode()) < 4096
    assert "candidate_recording_payload" not in outcome.result


def test_generated_checkpoint_survives_pipeline_failure(archive_dir, monkeypatch):
    runner, db = _runner(monkeypatch, _candidates())

    async def broken_filter(self, candidates, db):
        raise RuntimeError("evaluation_unavailable")

    monkeypatch.setattr(ScoringFilter, "filter", broken_filter)
    outcome = asyncio.run(runner.run())

    assert outcome.result["status"] == "failed"
    assert outcome.result["summary"]["candidate_recording_complete"] is False
    assert len(db.artifacts) == 1
    recording = db.artifacts[0]["payload_json"]
    assert len(read_candidate_archive(recording["generated"])["candidates"]) == 20
    assert "evaluated" not in recording


@pytest.mark.parametrize("read_only,mode", [(True, "stock_first_observe_primary"), (False, "stock_first_observe_shadow"), (False, "shadow_readonly")])
def test_readonly_and_shadow_do_not_write_candidate_archives(archive_dir, monkeypatch, read_only, mode):
    runner, db = _runner(monkeypatch, _candidates(), read_only=read_only, execution_mode=mode)
    outcome = asyncio.run(runner.run())

    assert outcome.result["status"] == "success", outcome.result.get("error")
    assert "candidate_recording" not in outcome.result
    assert db.artifacts == []
    assert not archive_dir.exists()


@pytest.mark.parametrize("failure", ["disk", "serialization", "checkpoint"])
def test_recording_failure_fails_cycle_and_is_audited(archive_dir, monkeypatch, failure):
    candidates = _candidates()
    runner, db = _runner(monkeypatch, candidates)
    if failure == "disk":
        archive_dir.write_text("not a directory")
    elif failure == "serialization":
        candidates[-1]["unsupported"] = object()
    else:
        async def broken_checkpoint(payload):
            raise OSError("checkpoint_unavailable")
        monkeypatch.setattr(db, "save_strategy_factory_run_artifact", broken_checkpoint)

    outcome = asyncio.run(runner.run())

    assert outcome.result["status"] == "failed"
    assert outcome.result["summary"]["candidate_recording_complete"] is False
    assert outcome.result["candidate_recording"]["failed_phase"] == "generated"
    assert len(outcome.persistence_failures) == 1
    assert outcome.persistence_failures[0]["stage"] == "candidate_recording"
    assert "submit" not in outcome.result["stages"]


def test_archive_ref_round_trips_through_capped_sqlite_artifact(archive_dir, tmp_path):
    from aiask_quant_core.storage.sqlite import SQLiteAdapter
    candidates = _candidates()
    reference = write_candidate_archive("run-sqlite", "generated", {"candidates": candidates})
    assert reference["uncompressed_bytes"] > 64 * 1024

    async def run():
        db = SQLiteAdapter(path=tmp_path / "candidate_records.sqlite3")
        try:
            await db.initialize()
            await db.save_strategy_factory_run_artifact({
                "run_id": "run-sqlite",
                **recording_artifact({"generated": reference, "complete": False}),
            })
            rows = await db.list_strategy_factory_run_artifacts("run-sqlite")
            assert len(rows) == 1
            assert len(json.dumps(rows[0]["payload_json"]).encode()) < 4096
            assert rows[0]["storage_mode"] == "external_gzip_json"
            assert read_candidate_archive(rows[0]["payload_json"]["generated"])["candidates"] == candidates
        finally:
            await db.close()

    asyncio.run(run())


def test_evaluated_write_failure_keeps_generated_checkpoint(archive_dir, monkeypatch):
    runner, db = _runner(monkeypatch, _candidates())

    def fail_evaluated(run_id, phase, payload):
        if phase == "evaluated":
            raise OSError("evaluation_archive_disk_full")
        return write_candidate_archive(run_id, phase, payload)

    monkeypatch.setattr("strategy_factory.application.cycle_runner.write_candidate_archive", fail_evaluated)
    outcome = asyncio.run(runner.run())

    assert outcome.result["status"] == "failed"
    assert outcome.result["candidate_recording"]["complete"] is False
    assert outcome.persistence_failures[0]["phase"] == "evaluated"
    assert len(db.artifacts) == 1
    assert len(read_candidate_archive(db.artifacts[0]["payload_json"]["generated"])["candidates"]) == 20


def test_missing_evaluated_candidate_cannot_claim_complete_recording(archive_dir, monkeypatch):
    from strategy_factory.application.services.candidate_pipeline import CandidatePipeline
    runner, db = _runner(monkeypatch, _candidates())
    original = CandidatePipeline.run

    async def drop_tail(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        result.candidate_recording_payload["candidates"] = result.candidate_recording_payload["candidates"][:-1]
        return result

    monkeypatch.setattr(CandidatePipeline, "run", drop_tail)
    outcome = asyncio.run(runner.run())

    assert outcome.result["status"] == "failed"
    assert outcome.result["error"] == "candidate_recording_count_mismatch"
    assert outcome.result["summary"]["candidate_recording_complete"] is False
    assert len(db.artifacts) == 1


def test_archive_hash_and_count_are_validated(archive_dir):
    reference = write_candidate_archive("../../unsafe-run", "generated", {"candidates": _candidates(1)})
    assert Path(reference["path"]).parent == archive_dir
    with pytest.raises(ValueError, match="candidate_archive_contract_mismatch"):
        read_candidate_archive({**reference, "candidate_count": 2})
    path = Path(reference["path"])
    payload = json.loads(gzip.decompress(path.read_bytes()))
    payload["candidates"][0]["params"]["lookback"] = 99
    path.write_bytes(gzip.compress(json.dumps(payload).encode()))
    with pytest.raises(ValueError, match="candidate_archive_hash_mismatch"):
        read_candidate_archive(reference)


def test_archive_preserves_nonfinite_measurements_explicitly(archive_dir):
    reference = write_candidate_archive("run-nan", "evaluated", {"candidates": [{"metric": float("nan")} ]})
    assert read_candidate_archive(reference)["candidates"][0]["metric"] == {"$nonfinite_float": "nan"}


def test_real_backtest_filter_preserves_raw_tail_evidence_for_cycle_archive(archive_dir, monkeypatch):
    from strategy_factory.application.backtest_filter import BacktestFilter

    candidates = _candidates()
    for candidate in candidates:
        candidate["target_symbols"] = ["002415", "300308", "601888"]
    runner, _db = _runner(monkeypatch, candidates)
    runner._context.factory_pkg.BacktestFilter = BacktestFilter
    monkeypatch.setattr(BacktestFilter, "_collect_preload_codes", lambda *args: [])
    monkeypatch.setattr("strategy_factory.application.backtest_filter.get_backtest_engine_class", lambda: object)

    async def evaluate(self, candidate, db, engine):
        return {
            "passed": False,
            "reason_code": "insufficient_samples",
            "failed_codes": [{"code": f"{i:06d}", "reason": "missing_history"} for i in range(50)],
            "equity_curve": list(range(100)),
            "backtest_assumptions": {f"assumption_{i}": i for i in range(40)},
            "target_quality_summary": {f"diagnostic_{i}": i for i in range(40)},
            "layers": {"target": {"metrics": {f"metric_{i}": i for i in range(40)}}},
        }

    monkeypatch.setattr(BacktestFilter, "_test_one", evaluate)
    outcome = asyncio.run(runner.run())

    assert outcome.result["status"] == "success", outcome.result.get("error")
    evaluated = read_candidate_archive(outcome.result["candidate_recording"]["evaluated"])
    raw = evaluated["backtest_candidate_results"]
    assert len(raw) == 20
    assert raw[-1]["candidate_index"] == 19
    assert raw[-1]["backtest_outcome"]["reason_code"] == "insufficient_samples"
    assert len(raw[-1]["backtest_outcome"]["failed_codes"]) == 50
    assert raw[-1]["backtest_outcome"]["equity_curve"] == list(range(100))
    candidate = evaluated["candidates"][-1]
    assert candidate["backtest_outcome"] == raw[-1]["backtest_outcome"]
    assert candidate["backtest_outcome"]["equity_curve"] == list(range(100))
    assert candidate["backtest_metrics"]["backtest_assumptions"]["assumption_39"] == 39
    assert candidate["backtest_metrics"]["target_quality_summary"]["diagnostic_39"] == 39
    assert candidate["backtest_metrics"]["target_layer_metrics"]["metric_39"] == 39
    from strategy_factory.application.oos_freeze import require_complete_json
    require_complete_json(candidate)
    # Submission and normal run previews keep their bounded representation.
    assert "equity_curve" not in evaluated["unique_candidates"][-1]["backtest_outcome"]
    from strategy_factory.application.oos_freeze import freeze_candidates_from_archive
    frozen = freeze_candidates_from_archive(
        archive_dir.parent / "seals.sqlite3", outcome.result["candidate_recording"]["evaluated"],
        selection_data_end="2026-11-03", candidate_ids=["candidate-19"],
    )
    assert frozen["created_count"] == 1
    assert frozen["measured_count"] == 0
    assert frozen["seals"][0]["quality_admission_granted"] is False


def test_incomplete_raw_result_mapping_fails_archive_audit(archive_dir, monkeypatch):
    from strategy_factory.application.backtest_filter import BacktestFilter

    runner, _db = _runner(monkeypatch, _candidates(2))
    runner._context.factory_pkg.BacktestFilter = BacktestFilter
    monkeypatch.setattr(BacktestFilter, "_collect_preload_codes", lambda *args: [])
    monkeypatch.setattr("strategy_factory.application.backtest_filter.get_backtest_engine_class", lambda: object)

    async def evaluate(self, candidate, db, engine):
        return {"passed": False, "reason_code": "insufficient_samples"}

    monkeypatch.setattr(BacktestFilter, "_test_one", evaluate)
    original = BacktestFilter.get_candidate_recording_results
    monkeypatch.setattr(BacktestFilter, "get_candidate_recording_results", lambda self: original(self)[:-1])
    outcome = asyncio.run(runner.run())
    assert outcome.result["status"] == "failed"
    assert outcome.result["error"] == "candidate_recording_result_identity_mismatch"
    assert outcome.result["summary"]["candidate_recording_complete"] is False
