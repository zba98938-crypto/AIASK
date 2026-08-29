from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from strategy_factory.application.factory_task_board import FactoryTaskBoard

JSON_LIMIT = 64 * 1024


def _large_board_payload() -> dict:
    return {
        "resolved_candidate_envelope": {
            "records": [
                {"symbol": f"{idx:06d}", "features": list(range(80))}
                for idx in range(900)
            ]
        },
        "quality_gate": {
            "passed_candidates": [
                {
                    "strategy_type": "momentum",
                    "equity_curve": list(range(800)),
                    "trades": [{"i": i, "price": i * 0.1} for i in range(120)],
                }
                for _ in range(30)
            ]
        },
        "research_task": {
            "task_id": "task-board-heavy",
            "target_symbols": [f"{idx:06d}" for idx in range(2000)],
        },
    }


def _json_len(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def test_factory_task_board_claim_heartbeat_complete(tmp_path) -> None:
    board = FactoryTaskBoard(tmp_path / "board.sqlite3")
    task = board.create_task(task_type="research", title="Research task", payload={"symbol": "600519"})

    claimed = board.claim_task(task["task_id"], worker_id="worker-a", ttl_seconds=60)
    assert claimed is not None
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1
    assert claimed["claim_token"]

    heartbeat = board.heartbeat(task["task_id"], claimed["claim_token"], ttl_seconds=120)
    assert heartbeat is not None
    assert heartbeat["status"] == "running"
    assert heartbeat["last_heartbeat_at"]

    completed = board.complete_task(
        task["task_id"],
        claim_token=claimed["claim_token"],
        artifact_refs=[{"artifact_type": "report", "artifact_id": "r1"}],
        result={"ok": True},
    )
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["artifact_refs"][0]["artifact_id"] == "r1"


def test_factory_task_board_reclaims_stale_tasks(tmp_path) -> None:
    board = FactoryTaskBoard(tmp_path / "board.sqlite3")
    task = board.create_task(task_type="backtest", title="Backtest task", max_attempts=2)
    claimed = board.claim_task(task["task_id"], worker_id="worker-a", ttl_seconds=1)
    assert claimed is not None

    reclaimed = board.reclaim_stale(now=datetime.now(timezone.utc) + timedelta(seconds=2))
    assert len(reclaimed) == 1
    assert reclaimed[0]["status"] == "ready"

    second = board.claim_task(task["task_id"], worker_id="worker-b", ttl_seconds=1)
    assert second is not None
    board.reclaim_stale(now=datetime.now(timezone.utc) + timedelta(seconds=2))
    final = board.get_task(task["task_id"])
    assert final is not None
    assert final["status"] == "blocked"


def test_factory_task_board_can_block_one_shot_stale_tasks(tmp_path) -> None:
    board = FactoryTaskBoard(tmp_path / "board.sqlite3")
    task = board.create_task(task_type="research", title="Strategy factory run")
    claimed = board.claim_task(task["task_id"], worker_id="worker-a", ttl_seconds=1)
    assert claimed is not None

    reclaimed = board.reclaim_stale(
        now=datetime.now(timezone.utc) + timedelta(seconds=2),
        block_task_types=("research",),
        block_reason="stale one-shot run",
    )

    assert len(reclaimed) == 1
    assert reclaimed[0]["status"] == "blocked"
    assert reclaimed[0]["blocked_reason"] == "stale one-shot run"


def test_factory_task_board_lists_active_tasks(tmp_path) -> None:
    board = FactoryTaskBoard(tmp_path / "board.sqlite3")
    research = board.create_task(
        task_type="research",
        title="Strategy factory run_once",
        payload={"owner_pid": 123},
    )
    backtest = board.create_task(task_type="backtest", title="Backtest")
    claimed = board.claim_task(research["task_id"], worker_id="worker-a", ttl_seconds=60)
    assert claimed is not None
    board.claim_task(backtest["task_id"], worker_id="worker-a", ttl_seconds=60)

    active_research = board.list_tasks(
        statuses=("ready", "running"),
        task_type="research",
        title="Strategy factory run_once",
    )

    assert [item["task_id"] for item in active_research] == [research["task_id"]]
    assert active_research[0]["payload"]["owner_pid"] == 123


def test_factory_task_board_caps_large_payload_and_result_json(tmp_path) -> None:
    board = FactoryTaskBoard(tmp_path / "board.sqlite3")
    task = board.create_task(
        task_type="research",
        title="Heavy task board payload",
        payload=_large_board_payload(),
        artifact_refs=[{"artifact_type": "raw", "payload": _large_board_payload()}],
    )
    assert _json_len(task["payload"]) < JSON_LIMIT
    assert "resolved_candidate_envelope" not in task["payload"]
    assert "resolved_candidate_envelope_summary" in task["payload"]

    claimed = board.claim_task(task["task_id"], worker_id="worker-heavy", ttl_seconds=60)
    assert claimed is not None
    completed = board.complete_task(
        task["task_id"],
        claim_token=claimed["claim_token"],
        artifact_refs=[{"artifact_type": "result", "payload": _large_board_payload()}],
        result={"result": _large_board_payload()},
    )
    assert completed is not None
    assert _json_len(completed["artifact_refs"]) < JSON_LIMIT

    conn = sqlite3.connect(board.path)
    try:
        payload_json, artifact_refs_json = conn.execute(
            "SELECT payload_json, artifact_refs_json FROM factory_tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()
        (result_json,) = conn.execute(
            "SELECT result_json FROM factory_task_attempts WHERE task_id = ? AND status = 'completed'",
            (task["task_id"],),
        ).fetchone()
    finally:
        conn.close()

    payload = json.loads(payload_json)
    artifact_refs = json.loads(artifact_refs_json)
    result = json.loads(result_json)
    assert _json_len(payload) < JSON_LIMIT
    assert _json_len(artifact_refs) < JSON_LIMIT
    assert _json_len(result) < JSON_LIMIT
    assert "resolved_candidate_envelope" not in payload
    assert "resolved_candidate_envelope_summary" in payload
    result_payload = result.get("result") if isinstance(result.get("result"), dict) else result
    assert "resolved_candidate_envelope" not in result_payload
    assert "resolved_candidate_envelope_summary" in result_payload


def test_factory_task_board_migrates_legacy_tasks_and_attempts_once(
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    runtime_home = tmp_path / "runtime"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AIASK_RUNTIME_HOME", str(runtime_home))
    monkeypatch.delenv("STRATEGY_FACTORY_TASK_BOARD_PATH", raising=False)

    legacy_path = home / ".aiask" / "strategy_factory_task_board.sqlite3"
    legacy_board = FactoryTaskBoard(legacy_path)
    task = legacy_board.create_task(
        task_type="research",
        title="Legacy durable task",
        payload={"symbol": "600519"},
    )
    claimed = legacy_board.claim_task(task["task_id"], worker_id="legacy-worker")
    assert claimed is not None
    collision_id = "factory_task_existing_destination"
    legacy_collision = legacy_board.create_task(
        task_type="research",
        title="Legacy collision",
        task_id=collision_id,
    )
    assert legacy_board.claim_task(
        legacy_collision["task_id"],
        worker_id="legacy-collision-worker",
    ) is not None

    new_path = runtime_home / "data" / "db" / "strategy_factory_task_board.sqlite3"
    destination_board = FactoryTaskBoard(new_path)
    destination_board.create_task(
        task_type="research",
        title="Destination remains authoritative",
        task_id=collision_id,
    )

    def load_migrated_task() -> dict | None:
        return FactoryTaskBoard.from_env().get_task(task["task_id"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        migrated = list(pool.map(lambda _: load_migrated_task(), range(2)))

    assert all(item is not None for item in migrated)
    assert all(item["status"] == "running" for item in migrated if item is not None)
    assert new_path.is_file()
    assert legacy_path.is_file()
    collision = FactoryTaskBoard.from_env().get_task(collision_id)
    assert collision is not None
    assert collision["title"] == "Destination remains authoritative"

    conn = sqlite3.connect(new_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM factory_tasks WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM factory_task_attempts WHERE task_id = ?",
            (task["task_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM factory_task_attempts WHERE task_id = ?",
            (collision_id,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM factory_task_board_migrations"
        ).fetchone()[0] == 1
    finally:
        conn.close()

    # A later connection observes the marker and leaves both datasets unchanged.
    assert len(FactoryTaskBoard.from_env().list_tasks()) == 2


def test_factory_task_board_explicit_path_does_not_import_legacy_state(
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    legacy_path = home / ".aiask" / "strategy_factory_task_board.sqlite3"
    legacy_board = FactoryTaskBoard(legacy_path)
    legacy = legacy_board.create_task(task_type="research", title="Legacy task")

    explicit_path = tmp_path / "custom" / "board.sqlite3"
    monkeypatch.setenv("STRATEGY_FACTORY_TASK_BOARD_PATH", str(explicit_path))
    explicit_board = FactoryTaskBoard.from_env()

    assert explicit_board.path == explicit_path.resolve()
    assert explicit_board.get_task(legacy["task_id"]) is None
