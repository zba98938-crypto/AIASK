from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

from akshare_mcp.storage import close_db, get_db


def _large_payload() -> dict:
    return {
        "summary": {"passed_count": 1},
        "passed_candidates": [
            {
                "strategy_type": "momentum",
                "equity_curve": list(range(5000)),
                "trades": [{"i": i, "price": i * 0.1} for i in range(1000)],
            }
        ],
        "backtest_metrics": {
            "sharpe_ratio": 1.1,
            "event_window_metrics": {
                "event_time_anchors": list(range(5000)),
                "raw_events": [{"i": i} for i in range(1000)],
            },
        },
    }


def test_sqlite_strategy_factory_json_fields_are_capped(tmp_path, monkeypatch):
    db_path = str(tmp_path / "json_caps.sqlite3")
    monkeypatch.setenv("AIASK_SQLITE_PATH", db_path)
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", db_path)

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            large = _large_payload()
            await db.save_strategy(
                {
                    "id": "strategy-json-cap",
                    "name": "Strategy JSON Cap",
                    "status": "draft",
                    "strategy_type": "momentum",
                }
            )

            artifact = await db.save_strategy_factory_run_artifact(
                {
                    "run_id": "run-json-cap",
                    "artifact_type": "quality_gate",
                    "artifact_version": "1",
                    "payload_json": large,
                    "payload_hash": "raw-hash",
                }
            )
            artifact_json = json.dumps(artifact["payload_json"], ensure_ascii=False, default=str)
            assert len(artifact_json.encode("utf-8")) < 64 * 1024
            assert artifact["storage_mode"] in {"inline_compact_json", "dropped_large_payload"}
            assert "equity_curve_summary" in artifact_json or "storage_mode" in artifact_json

            experiment = await db.save_strategy_generation_experiment(
                {
                    "experiment_id": "exp-json-cap",
                    "strategy_id": "strategy-json-cap",
                    "source": "strategy_factory",
                    "generator_type": "rule",
                    "status": "submitted",
                    "parameters": large,
                    "strategy_spec": large,
                    "evaluation": large,
                    "result": large,
                }
            )
            for field in ("parameters", "strategy_spec", "evaluation", "result"):
                encoded = json.dumps(experiment[field], ensure_ascii=False, default=str)
                assert len(encoded.encode("utf-8")) < 64 * 1024
                assert "equity_curve_summary" in encoded or "storage_mode" in encoded

            await db.save_strategy_quality_report(
                "strategy-json-cap",
                "submission",
                {
                    "passed": True,
                    "summary": large,
                    "quality_gate": large,
                    "backtest_metrics": large,
                    "snapshot": large,
                },
            )
            report = await db.get_strategy_quality_report("strategy-json-cap", "submission")
            assert report is not None
            for field in ("summary", "quality_gate", "backtest_metrics", "snapshot"):
                encoded = json.dumps(report[field], ensure_ascii=False, default=str)
                assert len(encoded.encode("utf-8")) < 64 * 1024
                assert "equity_curve_summary" in encoded or "storage_mode" in encoded
        finally:
            await close_db()

    asyncio.run(_run())


def _load_cleanup_script():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "compact_strategy_factory_json.py"
    )
    spec = importlib.util.spec_from_file_location("compact_strategy_factory_json", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_compact_strategy_factory_json_script_dry_run_and_apply(tmp_path):
    module = _load_cleanup_script()
    db_path = tmp_path / "bloated.sqlite3"
    output_path = tmp_path / "compacted.sqlite3"
    large = _large_payload()

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE strategy_factory_runs (id INTEGER PRIMARY KEY, stages TEXT, summary TEXT, snapshot_summary TEXT)"
        )
        conn.execute(
            "INSERT INTO strategy_factory_runs (stages, summary, snapshot_summary) VALUES (?, ?, ?)",
            (
                json.dumps({"quality_gate": large}, ensure_ascii=False),
                json.dumps(large, ensure_ascii=False),
                json.dumps(large, ensure_ascii=False),
            ),
        )
        conn.commit()

    dry_run = module.compact_database(db_path, apply=False)
    assert dry_run["updated_cells"] >= 1
    assert dry_run["output"] is None

    applied = module.compact_database(db_path, apply=True, output=output_path)
    assert applied["integrity_check"] == "ok"
    assert output_path.exists()

    with sqlite3.connect(output_path) as conn:
        row = conn.execute("SELECT stages FROM strategy_factory_runs LIMIT 1").fetchone()
        payload = json.loads(row[0])
        encoded = json.dumps(payload, ensure_ascii=False)
        assert len(encoded.encode("utf-8")) < 128 * 1024
        assert "equity_curve_summary" in encoded or "storage_mode" in encoded
