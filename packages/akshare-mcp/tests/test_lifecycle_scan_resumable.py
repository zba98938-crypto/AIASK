"""Real isolated storage proves attempts, fairness and cancellation semantics."""
import asyncio
import json
from types import SimpleNamespace

import pytest
import pytest_asyncio

from aiask_quant_core.storage.sqlite import SQLiteAdapter
from strategy_factory.infrastructure.factory_schema import register_factory_schema
from akshare_mcp.services import lifecycle_scan as scanner

STATE_KEY = "signal_tracker.lifecycle_scan.v1"
TASK = "strategy_lifecycle_scan_item"


@pytest_asyncio.fixture
async def db(tmp_path):
    register_factory_schema()
    adapter = SQLiteAdapter(tmp_path / "lifecycle.sqlite3")
    await adapter.initialize()
    yield adapter
    await adapter.close()


async def seed(db, sid, status="incubating"):
    await db.save_strategy({"id": sid, "name": sid, "strategy_type": "momentum", "status": status, "params": {}})


def attempts(db):
    return [dict(row) for row in db.connection.execute(
        "SELECT id,json_extract(payload,'$.strategy_id') strategy_id,status,payload,result,error "
        "FROM strategy_task_runs WHERE task_name=? ORDER BY id", (TASK,)
    )]


def install_review(monkeypatch, callback=None):
    async def review(db, strategy, **kwargs):
        assert kwargs == {"source": "lifecycle_scan", "auto_apply": True}
        persisted = await db.save_strategy_promotion_review({
            "strategy_id": strategy["id"], "review_source": "lifecycle_scan", "status": "watch",
            "recommendation": "observe", "blockers": ["insufficient_oos_evidence"],
        })
        if callback:
            await callback(db, strategy)
        return {"review": persisted, "overview": {"blockers": ["insufficient_oos_evidence"]}, "applied_transition": None}
    monkeypatch.setattr(scanner, "get_strategy_promotion_pipeline_service", lambda: SimpleNamespace(review=review))


@pytest.mark.asyncio
async def test_all_240_candidates_reached_across_slices_with_real_storage(db, monkeypatch):
    install_review(monkeypatch)
    for i in range(240):
        await seed(db, f"s{i:03}")
    # Mutable timestamps are irrelevant to the immutable-ID scheduling order.
    db.connection.execute("UPDATE strategies SET updated_at='2026-01-01'")
    db.connection.commit()
    results = [await scanner.run_lifecycle_scan(db, max_items=37) for _ in range(7)]
    rows = attempts(db)
    assert [r["strategy_id"] for r in rows] == [f"s{i:03}" for i in range(240)]
    assert all(r["status"] == "completed" for r in rows)
    assert all(r["coverage_complete"] is False for r in results[:-1])
    assert results[-1]["coverage_complete"] is True
    assert results[-1]["unresolved_strategy_count"] == 0
    assert results[-1]["transitions"] == []
    assert db.connection.execute("SELECT COUNT(*) FROM strategies WHERE status='incubating'").fetchone()[0] == 240


@pytest.mark.asyncio
async def test_status_lanes_alternate_and_listed_gate_is_preserved(db, monkeypatch):
    install_review(monkeypatch)
    await seed(db, "a", "incubating")
    await seed(db, "b", "incubating")
    await seed(db, "z", "listed")
    async def overview(*args):
        return {"deprecation_risk": True}
    async def metrics(*args, **kwargs):
        return [{"decision": "halt"}]
    monkeypatch.setattr(scanner, "build_incubation_overview", overview)
    monkeypatch.setattr(db, "list_strategy_incubation_metrics", metrics)
    await scanner.run_lifecycle_scan(db, max_items=2)
    assert [r["strategy_id"] for r in attempts(db)] == ["a", "z"]
    assert (await db.get_strategy("z"))["status"] == "listed"
    assert json.loads(attempts(db)[1]["result"])["blockers"] == ["deprecation_risk_unconfirmed"]


@pytest.mark.asyncio
async def test_cancel_after_partial_write_preserves_history_and_moves_to_tail(db, monkeypatch):
    for sid in ("a", "b", "c"):
        await seed(db, sid)
    entered = asyncio.Event()
    async def block(_db, strategy):
        if strategy["id"] == "a":
            await _db.update_strategy_status("a", "listed", reason="test_partial_transition")
            entered.set()
            await asyncio.Event().wait()
    install_review(monkeypatch, block)
    task = asyncio.create_task(scanner.run_lifecycle_scan(db))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert attempts(db)[0]["status"] == "interrupted"
    assert db.connection.execute("SELECT COUNT(*) FROM strategy_promotion_reviews WHERE strategy_id='a'").fetchone()[0] == 1
    assert (await db.get_strategy("a"))["status"] == "listed"
    result = await scanner.run_lifecycle_scan(db, max_items=1)
    assert attempts(db)[-1]["strategy_id"] == "b"
    assert result["unresolved_strategy_count"] == 1
    assert result["coverage_complete"] is False


@pytest.mark.asyncio
async def test_one_slow_strategy_cannot_starve_tail_and_is_retried(db, monkeypatch):
    for sid in ("a", "b", "c"):
        await seed(db, sid)
    async def block(_db, strategy):
        if strategy["id"] == "a":
            await asyncio.Event().wait()
    install_review(monkeypatch, block)
    first = await scanner.run_lifecycle_scan(db, budget_seconds=.02)
    assert first["transitions"] is None
    assert first["transitions_reason"] == "lifecycle_scan_partial_writes_unknown"
    assert attempts(db)[0]["status"] == "failed"
    second = await scanner.run_lifecycle_scan(db)
    assert [r["strategy_id"] for r in attempts(db)] == ["a", "b", "c"]
    assert second["coverage_complete"] is False
    install_review(monkeypatch)
    third = await scanner.run_lifecycle_scan(db)
    assert [r["strategy_id"] for r in attempts(db)][3:] == ["a", "b", "c"]
    assert attempts(db)[0]["status"] == "failed"  # raw history retained
    assert third["coverage_complete"] is True


@pytest.mark.asyncio
async def test_new_ids_behind_cursor_are_deferred_without_false_complete(db, monkeypatch):
    install_review(monkeypatch)
    await seed(db, "b")
    await seed(db, "c")
    await scanner.run_lifecycle_scan(db, max_items=1)
    await seed(db, "a")
    second = await scanner.run_lifecycle_scan(db)
    assert second["unreviewed_eligible"] == 1
    assert second["coverage_complete"] is False
    third = await scanner.run_lifecycle_scan(db)
    assert [r["strategy_id"] for r in attempts(db)] == ["b", "c", "a", "b", "c"]
    assert third["coverage_complete"] is True


@pytest.mark.asyncio
async def test_two_real_connections_cannot_claim_same_scan(db):
    await seed(db, "a")
    other = SQLiteAdapter(db.path)
    await other.initialize()
    try:
        outcomes = await asyncio.gather(
            asyncio.to_thread(lambda: asyncio.run(db.claim_lifecycle_scan("one"))),
            asyncio.to_thread(lambda: asyncio.run(other.claim_lifecycle_scan("two"))),
        )
        assert sorted(x["acquired"] for x in outcomes) == [False, True]
        winner = "one" if outcomes[0]["acquired"] else "two"
        await db.release_lifecycle_scan(winner)
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_dead_owner_reservation_is_interrupted_and_tail_resumes(db, monkeypatch):
    import aiask_quant_core.storage.sqlite.lifecycle_scan as storage
    for sid in ("a", "b"):
        await seed(db, sid)
    await db.claim_lifecycle_scan("dead")
    first = await db.reserve_lifecycle_scan_item("dead")
    monkeypatch.setattr(storage, "_pid_alive", lambda pid: False)
    await db.claim_lifecycle_scan("new")
    second = await db.reserve_lifecycle_scan_item("new")
    assert (first["strategy_id"], second["strategy_id"]) == ("a", "b")
    assert attempts(db)[0]["status"] == "interrupted"
    assert attempts(db)[0]["error"] == "lifecycle_scan_owner_lost"
    await db.release_lifecycle_scan("new")


@pytest.mark.asyncio
async def test_expired_lease_of_alive_owner_does_not_overlap(db):
    await seed(db, "a")
    await db.claim_lifecycle_scan("alive")
    db.connection.execute("UPDATE strategy_factory_scheduler_state SET payload_json=json_set(payload_json,'$.lease_until',0) WHERE state_key=?", (STATE_KEY,))
    db.connection.commit()
    result = await db.claim_lifecycle_scan("other")
    assert result["acquired"] is False
    assert result["reason"] == "lifecycle_scan_lease_held"
    await db.release_lifecycle_scan("alive")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["broken", '{"version":2}', '{"version":1,"lanes":[]}'])
async def test_invalid_state_fails_closed(db, monkeypatch, payload):
    install_review(monkeypatch)
    await seed(db, "a")
    db.connection.execute("INSERT INTO strategy_factory_scheduler_state (state_key,payload_json) VALUES (?,?)", (STATE_KEY, payload))
    db.connection.commit()
    with pytest.raises(ValueError, match="lifecycle_scan_state_invalid"):
        await scanner.run_lifecycle_scan(db)
    assert attempts(db) == []


@pytest.mark.asyncio
async def test_reservation_failure_rolls_back_cursor_and_creates_no_review(db, monkeypatch):
    install_review(monkeypatch)
    await seed(db, "a")
    db.connection.execute("CREATE TRIGGER reject_attempt BEFORE INSERT ON strategy_task_runs "
                          "WHEN NEW.task_name='strategy_lifecycle_scan_item' "
                          "BEGIN SELECT RAISE(ABORT,'attempt_write_failed'); END")
    with pytest.raises(Exception, match="attempt_write_failed"):
        await scanner.run_lifecycle_scan(db)
    assert attempts(db) == []
    assert db.connection.execute("SELECT COUNT(*) FROM strategy_promotion_reviews").fetchone()[0] == 0
    state = json.loads(db.connection.execute("SELECT payload_json FROM strategy_factory_scheduler_state WHERE state_key=?", (STATE_KEY,)).fetchone()[0])
    assert state["lanes"]["incubating"]["after"] == ""
    assert state["owner"] is None


@pytest.mark.asyncio
async def test_completion_checkpoint_failure_preserves_raw_review_and_pending_attempt(db, monkeypatch):
    install_review(monkeypatch)
    await seed(db, "a")
    await seed(db, "b")
    db.connection.execute("CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON strategy_task_runs "
                          "WHEN NEW.task_name='strategy_lifecycle_scan_item' "
                          "BEGIN SELECT RAISE(ABORT,'checkpoint_write_failed'); END")
    with pytest.raises(Exception, match="checkpoint_write_failed"):
        await scanner.run_lifecycle_scan(db)
    assert attempts(db)[0]["status"] == "running"
    assert db.connection.execute("SELECT COUNT(*) FROM strategy_promotion_reviews WHERE strategy_id='a'").fetchone()[0] == 1
    db.connection.execute("DROP TRIGGER reject_checkpoint")
    await scanner.run_lifecycle_scan(db, max_items=1)
    assert [r["strategy_id"] for r in attempts(db)] == ["a", "b"]
    assert attempts(db)[0]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_deleted_strategy_does_not_delete_attempt_history(db):
    await seed(db, "a")
    await db.claim_lifecycle_scan("owner")
    attempt = await db.reserve_lifecycle_scan_item("owner")
    db.connection.execute("DELETE FROM strategies WHERE id='a'")
    db.connection.commit()
    assert attempts(db)[0]["strategy_id"] == "a"
    await db.finish_lifecycle_scan_item("owner", attempt["attempt_id"], status="ineligible", result={"reason": "deleted"})
    await db.release_lifecycle_scan("owner")


@pytest.mark.asyncio
async def test_both_provider_adapters_preserve_partial_unknown_counts(monkeypatch):
    from strategy_factory.runtime import provider_adapters
    from akshare_mcp.adapters.signal_tracker.provider import AKShareSignalTrackerProvider
    payload = {"status": "partial", "reason": "lifecycle_scan_budget_exhausted", "coverage_complete": False,
               "remaining": 235, "unresolved": 1, "transitions": None,
               "transitions_reason": "lifecycle_scan_partial_writes_unknown"}
    async def scan(*args):
        return payload
    monkeypatch.setattr(provider_adapters, "get_strategy_lifecycle_scan_runner", lambda: scan)
    canonical = await provider_adapters.LegacySignalTrackerProvider(object()).run_lifecycle_scan(object(), [])
    legacy = await AKShareSignalTrackerProvider(SimpleNamespace(_run_lifecycle_scan=scan)).run_lifecycle_scan(object(), [])
    assert canonical == payload
    assert legacy == payload
