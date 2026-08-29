from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

from aiask_quant_core.storage import close_db, get_db


JSON_LIMIT = 64 * 1024
PARAMS_LIMIT = 32 * 1024
SNAPSHOT_LIMIT = 8 * 1024


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


def _heavy_factor_research() -> dict:
    allocation = {
        f"{i:06d}": {
            "families": {
                "momentum": i % 97 / 100,
                "reversal": i % 89 / 100,
                "event": i % 83 / 100,
                "quality": i % 79 / 100,
            },
            "raw_features": list(range(40)),
        }
        for i in range(1600)
    }
    return {
        "summary": {
            "factor_count": 12,
            "allocation_concentration_level": "balanced",
            "notes": "x" * 2000,
        },
        "active_factors": [f"factor_{i}" for i in range(30)],
        "positive_rising_factors": [f"rising_{i}" for i in range(20)],
        "active_candidate_pool": {
            "count": 500,
            "family_count": 8,
            "top_candidates": [
                {"name": f"candidate_{i}", "family": "momentum", "score": 1.0 - i / 1000}
                for i in range(80)
            ],
        },
        "stock_family_allocation": allocation,
        "source_chain": [{"source": "unit", "rank": i} for i in range(50)],
        "degraded": False,
    }


def _heavy_strategy_params() -> dict:
    return {
        "dsl": {"entry": "close > ma20", "exit": "close < ma10"},
        "target_symbols": [f"{i:06d}" for i in range(120)],
        "risk_rules": {"max_drawdown": 0.12, "stop_loss": 0.05},
        "trade_plan": {"horizon": "swing_5_20d", "position": "equal_weight"},
        "factory_run_id": "run-json-cap",
        "submission_lane": "observe_incubation",
        "planned_submission_lane": "formal_incubation",
        "final_status": "submitted",
        "planned_final_status": "incubating",
        "observe_first_intake": True,
        "formal_track_requested": True,
        "runtime_family_data_source": "price_proxy_runtime",
        "execution_readiness_tier": "observe_diagnostic_only",
        "trade_prediction_contract_status": "ready",
        "candidate_provenance": {
            "source_candidate_artifact_id": "factor-1",
            "source_validation_artifact_id": "factor-1",
            "candidate_family": "liquidity",
            "generator_mode": "factor_pool",
            "alpha_source": "factor_mining_active_pool",
        },
        "resolved_candidate_envelope": {
            "records": [{"symbol": f"{i:06d}", "features": list(range(60))} for i in range(700)]
        },
        "candidate_contract_snapshot": {
            "candidates": [{"id": i, "contract": "x" * 200} for i in range(250)]
        },
        "research_task": {
            "task_id": "task-heavy",
            "target_symbols": [f"{i:06d}" for i in range(1000)],
            "market_context": "y" * 30000,
        },
        "incubation_budget": {
            "track": "observe_incubation",
            "rank": 7,
            "priority_score": 91.5,
            "exploration_candidate": True,
            "raw": ["z" * 200 for _ in range(300)],
        },
    }


def _heavy_quality_report_summary() -> dict:
    payload = {
        "strategy_id": "strategy-quality-summary",
        "submission_lane": "observe_incubation",
        "planned_submission_lane": "formal_incubation",
        "final_status": "submitted",
        "planned_final_status": "incubating",
        "formal_track_requested": True,
        "formal_track_auto_corrected": False,
        "formal_track_eligible": False,
        "formal_track_blockers": [
            "default_profile_not_allowed_for_single_name_runtime",
            "diagnostic_only_not_allowed_for_incubation",
        ],
        "observe_first_intake_requested": True,
        "incubation_budget_track": "formal_incubation",
        "incubation_budget_rank": 3,
        "incubation_budget_priority_score": 98.25,
        "runtime_bootstrap_reason": "proxy_runtime_observe_only",
        "runtime_family_data_source": "price_proxy_runtime",
        "proxy_runtime_used": True,
        "diagnostic_only": True,
        "execution_readiness_tier": "observe_diagnostic_only",
        "trade_prediction_contract_status": "ready",
        "trade_prediction_contract_observation_gap": False,
        "submission_action_type": "paper",
        "submission_action_trigger": "formal_lane_requires_semantic_runtime_match",
        "candidate_contract_snapshot": {
            "candidates": [{"id": i, "contract": "x" * 300} for i in range(220)]
        },
    }
    for i in range(120):
        payload[f"filler_{i:03d}"] = "x" * 600
    return payload


def _json_len(value) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def test_shared_sqlite_strategy_factory_json_fields_are_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / "json_caps.sqlite3"))

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
            assert len(artifact_json.encode("utf-8")) < JSON_LIMIT
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
                assert len(encoded.encode("utf-8")) < JSON_LIMIT
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
                assert len(encoded.encode("utf-8")) < JSON_LIMIT
                assert "equity_curve_summary" in encoded or "storage_mode" in encoded
        finally:
            await close_db()

    asyncio.run(_run())


def test_strategy_factory_primary_bloat_paths_are_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / "primary_bloat.sqlite3"))

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            heavy_params = _heavy_strategy_params()
            await db.save_strategy(
                {
                    "id": "strategy-heavy-params",
                    "name": "Heavy Params",
                    "status": "draft",
                    "strategy_type": "momentum",
                    "params": heavy_params,
                }
            )
            strategy = await db.get_strategy("strategy-heavy-params")
            assert strategy is not None
            params = strategy["params"]
            assert _json_len(params) < PARAMS_LIMIT
            assert "resolved_candidate_envelope" not in params
            assert "candidate_contract_snapshot" not in params
            assert "research_task" not in params
            assert params["submission_lane"] == "observe_incubation"
            assert params["planned_submission_lane"] == "formal_incubation"
            assert params["final_status"] == "submitted"
            assert params["planned_final_status"] == "incubating"
            assert params["observe_first_intake"] is True
            assert params["candidate_provenance"]["source_candidate_artifact_id"] == "factor-1"
            assert params["candidate_provenance"]["candidate_family"] == "liquidity"
            assert params["candidate_provenance"]["generator_mode"] == "factor_pool"
            assert params["incubation_budget"]["track"] == "observe_incubation"
            assert params["incubation_budget"]["rank"] == 7
            assert params["incubation_budget"]["priority_score"] == 91.5
            assert params["incubation_budget"]["exploration_candidate"] is True
            assert params["_storage_audit"]["payload_hash"]
            assert "incubation_budget" not in params["_storage_audit"].get("dropped_large_nodes", {})
            assert "candidate_provenance" not in params["_storage_audit"].get("dropped_large_nodes", {})
            assert set(params["_storage_audit"]["dropped_large_nodes"]) >= {
                "resolved_candidate_envelope",
                "candidate_contract_snapshot",
                "research_task",
            }

            await db.save_daily_snapshot(
                "2026-05-23",
                {
                    "fear_greed_index": 55,
                    "factor_research": _heavy_factor_research(),
                    "summary": {"ok": True},
                },
            )
            snapshot = await db.get_daily_snapshot("2026-05-23")
            assert snapshot is not None
            factor_research = snapshot["factor_research"]
            assert _json_len(factor_research) < JSON_LIMIT
            assert "stock_family_allocation" not in factor_research
            assert "stock_family_allocation_summary" in factor_research
            assert factor_research["payload_hash"]

            await db.update_strategy_status(
                "strategy-heavy-params",
                "rejected",
                actor_id="test",
                reason="cap-test",
                metadata={"task_run_id": "task-heavy", "details": _large_payload()},
            )
            status_events = await db.list_strategy_status_events("strategy-heavy-params")
            assert status_events
            assert _json_len(status_events[0]["metadata"]) < JSON_LIMIT
            domain_events = await db.list_strategy_domain_events(strategy_id="strategy-heavy-params")
            assert domain_events
            assert _json_len(domain_events[0]["payload"]) < JSON_LIMIT

            direct_domain_event = await db.save_strategy_domain_event(
                {
                    "strategy_id": "strategy-heavy-params",
                    "aggregate_type": "strategy_lifecycle_transition",
                    "aggregate_id": "strategy-heavy-params",
                    "event_type": "strategy.lifecycle_transition",
                    "source": "cap-test",
                    "severity": "info",
                    "correlation_id": "direct-domain-event-cap",
                    "payload": {"trace": _large_payload()},
                }
            )
            assert _json_len(direct_domain_event["payload"]) < JSON_LIMIT
            assert "equity_curve_summary" in json.dumps(
                direct_domain_event["payload"],
                ensure_ascii=False,
                default=str,
            ) or direct_domain_event["payload"].get("storage_mode")

            task_run = await db.save_strategy_task_run(
                {
                    "strategy_id": "strategy-heavy-params",
                    "task_name": "cap_task",
                    "status": "completed",
                    "payload": {"request": _large_payload()},
                    "result": {"result": _large_payload()},
                }
            )
            assert _json_len(task_run["payload"]) < JSON_LIMIT
            assert _json_len(task_run["result"]) < JSON_LIMIT

            evidence = await db.save_factory_task_evidence(
                {
                    "task_key": "task-heavy",
                    "evidence_type": "research",
                    "evidence_payload": {"evidence": _large_payload()},
                }
            )
            assert _json_len(evidence["evidence_payload"]) < JSON_LIMIT
        finally:
            await close_db()

    asyncio.run(_run())


def test_strategy_quality_report_summary_compaction_preserves_priority_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / "quality_summary_caps.sqlite3"))

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            await db.save_strategy(
                {
                    "id": "strategy-quality-summary",
                    "name": "Strategy Quality Summary",
                    "status": "draft",
                    "strategy_type": "momentum",
                }
            )
            summary = _heavy_quality_report_summary()
            assert _json_len(summary) > JSON_LIMIT

            await db.save_strategy_quality_report(
                "strategy-quality-summary",
                "submission",
                {
                    "passed": True,
                    "summary": summary,
                },
            )
            report = await db.get_strategy_quality_report("strategy-quality-summary", "submission")
            assert report is not None
            stored = dict(report["summary"] or {})
            assert _json_len(stored) < JSON_LIMIT
            assert stored["storage_mode"] == "compact_json"
            assert stored["submission_lane"] == "observe_incubation"
            assert stored["planned_submission_lane"] == "formal_incubation"
            assert stored["final_status"] == "submitted"
            assert stored["planned_final_status"] == "incubating"
            assert stored["incubation_budget_track"] == "formal_incubation"
            assert stored["runtime_family_data_source"] == "price_proxy_runtime"
            assert stored["proxy_runtime_used"] is True
            assert stored["diagnostic_only"] is True
            assert stored["execution_readiness_tier"] == "observe_diagnostic_only"
            assert stored["trade_prediction_contract_status"] == "ready"
            assert stored["submission_action_type"] == "paper"
            assert stored["submission_action_trigger"] == "formal_lane_requires_semantic_runtime_match"
            assert stored["truncated_key_count"] > 0
        finally:
            await close_db()

    asyncio.run(_run())


def test_strategy_factory_sqlite_raw_sql_json_fields_are_capped(tmp_path, monkeypatch):
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / "raw_sql_caps.sqlite3"))

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            heavy_params = _heavy_strategy_params()
            heavy_factor_research = _heavy_factor_research()

            async with db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO strategies (id, name, strategy_type, params, factor_weights, status)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    "strategy-raw-sql",
                    "Raw SQL Cap",
                    "momentum",
                    heavy_params,
                    {"weights": _large_payload()},
                    "draft",
                )
                raw_strategy = await conn.fetchrow(
                    "SELECT params, factor_weights FROM strategies WHERE id = $1",
                    "strategy-raw-sql",
                )
                assert raw_strategy is not None
                params = json.loads(raw_strategy["params"])
                factor_weights = json.loads(raw_strategy["factor_weights"])
                assert _json_len(params) < PARAMS_LIMIT
                assert "resolved_candidate_envelope" not in params
                assert params["_storage_audit"]["payload_hash"]
                assert _json_len(factor_weights) < JSON_LIMIT

                inserted = await conn.fetchrow(
                    """
                    INSERT INTO daily_snapshot_history (snapshot_date, factor_research)
                    VALUES ($1, $2)
                    RETURNING factor_research
                    """,
                    "2026-05-24",
                    heavy_factor_research,
                )
                assert inserted is not None
                inserted_factor_research = json.loads(inserted["factor_research"])
                assert _json_len(inserted_factor_research) < JSON_LIMIT
                assert "stock_family_allocation" not in inserted_factor_research
                assert "stock_family_allocation_summary" in inserted_factor_research

                await conn.execute(
                    """
                    UPDATE daily_snapshot_history
                    SET factor_research = $1
                    WHERE snapshot_date = $2
                    """,
                    json.dumps(heavy_factor_research, ensure_ascii=False, default=str),
                    "2026-05-24",
                )
                updated = await conn.fetchrow(
                    "SELECT factor_research FROM daily_snapshot_history WHERE snapshot_date = $1",
                    "2026-05-24",
                )
                assert updated is not None
                updated_factor_research = json.loads(updated["factor_research"])
                assert _json_len(updated_factor_research) < JSON_LIMIT
                assert "stock_family_allocation" not in updated_factor_research

                await conn.executemany(
                    """
                    INSERT INTO strategy_task_runs (strategy_id, task_name, status, payload, result)
                    VALUES ($1, $2, $3, $4, $5)
                    """,
                    [
                        (
                            "strategy-raw-sql",
                            "raw-cap-a",
                            "completed",
                            {"payload": _large_payload()},
                            {"result": _large_payload()},
                        ),
                        (
                            "strategy-raw-sql",
                            "raw-cap-b",
                            "completed",
                            {"payload": _large_payload()},
                            {"result": _large_payload()},
                        ),
                    ],
                )
                rows = await conn.fetch(
                    """
                    SELECT payload, result
                    FROM strategy_task_runs
                    WHERE strategy_id = $1 AND task_name LIKE 'raw-cap-%'
                    ORDER BY task_name
                    """,
                    "strategy-raw-sql",
                )
                assert len(rows) == 2
                for row in rows:
                    payload = json.loads(row["payload"])
                    result = json.loads(row["result"])
                    assert _json_len(payload) < JSON_LIMIT
                    assert _json_len(result) < JSON_LIMIT
                    assert "equity_curve_summary" in json.dumps(payload, ensure_ascii=False) or "storage_mode" in payload
        finally:
            await close_db()

    asyncio.run(_run())


def test_strategy_factory_full_market_scores_keep_latest_run(tmp_path, monkeypatch):
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(tmp_path / "full_market_retention.sqlite3"))
    monkeypatch.setenv("STRATEGY_FACTORY_FULL_MARKET_SCORE_RETENTION_RUNS", "1")

    def _rows(prefix: str) -> list[dict]:
        return [
            {
                "code": f"{prefix}{i:03d}",
                "rank": i,
                "composite_score": 1.0 / (i + 1),
                "component_scores": {"score": i, "raw": list(range(200))},
                "family_candidates": [{"family": "momentum", "score": 0.9} for _ in range(20)],
                "eligible": True,
            }
            for i in range(5)
        ]

    async def _run() -> None:
        db = get_db()
        try:
            await db.initialize()
            inserted_one = await db.replace_strategy_factory_full_market_scores(
                run_id="run-old",
                snapshot_id="snap-old",
                as_of_date="2026-05-22",
                trace_id=None,
                correlation_id=None,
                rows=_rows("old"),
            )
            inserted_two = await db.replace_strategy_factory_full_market_scores(
                run_id="run-new",
                snapshot_id="snap-new",
                as_of_date="2026-05-23",
                trace_id=None,
                correlation_id=None,
                rows=_rows("new"),
            )
            assert inserted_one == 5
            assert inserted_two == 5
            assert await db.count_strategy_factory_full_market_scores("run-old") == 0
            assert await db.count_strategy_factory_full_market_scores("run-new") == 5
        finally:
            await close_db()

    asyncio.run(_run())


def _load_cleanup_script():
    script = Path(__file__).resolve().parents[1] / "scripts" / "compact_strategy_factory_json.py"
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
    heavy_params = _heavy_strategy_params()
    heavy_factor_research = _heavy_factor_research()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "CREATE TABLE strategy_factory_runs (id INTEGER PRIMARY KEY, stages TEXT, summary TEXT, snapshot_summary TEXT)"
        )
        conn.execute("CREATE TABLE daily_snapshot_history (id INTEGER PRIMARY KEY, factor_research TEXT)")
        conn.execute("CREATE TABLE strategies (id TEXT PRIMARY KEY, params TEXT)")
        conn.execute("CREATE TABLE strategy_status_events (id INTEGER PRIMARY KEY, metadata TEXT)")
        conn.execute("CREATE TABLE strategy_domain_events (id INTEGER PRIMARY KEY, payload TEXT)")
        conn.execute("CREATE TABLE strategy_task_runs (id INTEGER PRIMARY KEY, payload TEXT, result TEXT)")
        conn.execute("CREATE TABLE strategy_incubation_pipeline_snapshots (id INTEGER PRIMARY KEY, metadata TEXT)")
        conn.execute("CREATE TABLE strategy_generation_experiments (id INTEGER PRIMARY KEY, result TEXT)")
        conn.execute("CREATE TABLE strategy_factory_task_evidence (id INTEGER PRIMARY KEY, evidence_payload TEXT)")
        conn.execute("CREATE TABLE strategy_factory_full_market_scores (id INTEGER PRIMARY KEY, run_id TEXT, as_of_date TEXT, created_at TEXT)")
        conn.execute("CREATE TABLE factory_tasks (task_id TEXT PRIMARY KEY, payload_json TEXT, artifact_refs_json TEXT)")
        conn.execute("CREATE TABLE factory_task_attempts (attempt_id TEXT PRIMARY KEY, task_id TEXT, result_json TEXT)")
        conn.execute(
            "INSERT INTO strategy_factory_runs (stages, summary, snapshot_summary) VALUES (?, ?, ?)",
            (
                json.dumps({"quality_gate": large}, ensure_ascii=False),
                json.dumps(large, ensure_ascii=False),
                json.dumps(large, ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO daily_snapshot_history (factor_research) VALUES (?)",
            (json.dumps(heavy_factor_research, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO strategies (id, params) VALUES (?, ?)",
            ("strategy-heavy", json.dumps(heavy_params, ensure_ascii=False)),
        )
        conn.execute(
            "INSERT INTO strategy_status_events (metadata) VALUES (?)",
            (json.dumps({"metadata": large}, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO strategy_domain_events (payload) VALUES (?)",
            (json.dumps({"payload": large}, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO strategy_task_runs (payload, result) VALUES (?, ?)",
            (
                json.dumps({"payload": large}, ensure_ascii=False),
                json.dumps({"result": large}, ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO strategy_incubation_pipeline_snapshots (metadata) VALUES (?)",
            (json.dumps({"metadata": large, "raw_notes": "x" * 50000}, ensure_ascii=False),),
        )
        byte_large_result = json.dumps({"notes": "广" * 22000}, ensure_ascii=False)
        assert len(byte_large_result) < JSON_LIMIT
        assert len(byte_large_result.encode("utf-8")) > JSON_LIMIT
        conn.execute(
            "INSERT INTO strategy_generation_experiments (result) VALUES (?)",
            (byte_large_result,),
        )
        conn.execute(
            "INSERT INTO strategy_factory_task_evidence (evidence_payload) VALUES (?)",
            (json.dumps({"evidence": large}, ensure_ascii=False),),
        )
        conn.execute(
            "INSERT INTO factory_tasks (task_id, payload_json, artifact_refs_json) VALUES (?, ?, ?)",
            (
                "task-heavy",
                json.dumps({"payload": large, "research_task": heavy_params["research_task"]}, ensure_ascii=False),
                json.dumps([{"artifact_type": "raw", "payload": large}], ensure_ascii=False),
            ),
        )
        conn.execute(
            "INSERT INTO factory_task_attempts (attempt_id, task_id, result_json) VALUES (?, ?, ?)",
            (
                "attempt-heavy",
                "task-heavy",
                json.dumps({"result": large, "resolved_candidate_envelope": heavy_params["resolved_candidate_envelope"]}, ensure_ascii=False),
            ),
        )
        for run_id, as_of_date in (("run-old", "2026-05-22"), ("run-new", "2026-05-23")):
            for i in range(3):
                conn.execute(
                    "INSERT INTO strategy_factory_full_market_scores (run_id, as_of_date, created_at) VALUES (?, ?, ?)",
                    (run_id, as_of_date, f"{as_of_date}T00:00:0{i}"),
                )
        conn.commit()
    finally:
        conn.close()

    dry_run = module.compact_database(db_path, apply=False)
    assert dry_run["updated_cells"] >= 1
    assert dry_run["output"] is None
    assert dry_run["full_market_retention"]["deleted_rows"] == 3
    targeted_dry_run = module.compact_database(
        db_path,
        apply=False,
        table_filter="strategy_incubation_pipeline_snapshots",
        column_filter="metadata",
    )
    assert targeted_dry_run["updated_cells"] == 1
    assert targeted_dry_run["fields"][0]["table"] == "strategy_incubation_pipeline_snapshots"
    assert targeted_dry_run["fields"][0]["column"] == "metadata"
    assert "full_market_retention" not in targeted_dry_run
    targeted_estimate = module.compact_database(
        db_path,
        estimate_only=True,
        table_filter="strategy_incubation_pipeline_snapshots",
        column_filter="metadata",
    )
    assert targeted_estimate["mode"] == "estimate_only"
    assert targeted_estimate["updated_cells"] == 1
    assert targeted_estimate["fields"][0]["estimated_total_bytes"] > SNAPSHOT_LIMIT
    assert targeted_estimate["fields"][0]["compacted_bytes"] is None
    assert "full_market_retention" not in targeted_estimate

    applied = module.compact_database(db_path, apply=True, output=output_path)
    assert applied["integrity_check"] == "ok"
    assert output_path.exists()
    assert applied["full_market_retention"]["deleted_rows"] == 3

    conn = sqlite3.connect(output_path)
    try:
        row = conn.execute("SELECT stages FROM strategy_factory_runs LIMIT 1").fetchone()
        payload = json.loads(row[0])
        encoded = json.dumps(payload, ensure_ascii=False)
        assert len(encoded.encode("utf-8")) < 128 * 1024
        assert "equity_curve_summary" in encoded or "storage_mode" in encoded
        factor_research = json.loads(conn.execute("SELECT factor_research FROM daily_snapshot_history").fetchone()[0])
        assert _json_len(factor_research) < JSON_LIMIT
        assert "stock_family_allocation_summary" in factor_research
        params = json.loads(conn.execute("SELECT params FROM strategies").fetchone()[0])
        assert _json_len(params) < PARAMS_LIMIT
        assert "resolved_candidate_envelope" not in params
        task_payload = json.loads(conn.execute("SELECT payload_json FROM factory_tasks").fetchone()[0])
        assert _json_len(task_payload) < JSON_LIMIT
        incubation_metadata = json.loads(
            conn.execute("SELECT metadata FROM strategy_incubation_pipeline_snapshots").fetchone()[0]
        )
        assert _json_len(incubation_metadata) < SNAPSHOT_LIMIT
        assert incubation_metadata["storage_mode"] in {"compact_json", "dropped_large_payload"}
        attempt_result = json.loads(conn.execute("SELECT result_json FROM factory_task_attempts").fetchone()[0])
        assert _json_len(attempt_result) < JSON_LIMIT
        experiment_result = json.loads(conn.execute("SELECT result FROM strategy_generation_experiments").fetchone()[0])
        assert _json_len(experiment_result) < JSON_LIMIT
        assert experiment_result["storage_mode"] == "dropped_large_payload"
        assert "resolved_candidate_envelope" not in attempt_result
        assert "resolved_candidate_envelope_summary" in attempt_result
        assert conn.execute("SELECT COUNT(*) FROM strategy_factory_full_market_scores WHERE run_id='run-old'").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_compact_strategy_factory_json_script_replace_creates_gzip_backup(tmp_path):
    module = _load_cleanup_script()
    db_path = tmp_path / "replace.sqlite3"
    output_path = tmp_path / "replace.compacted.sqlite3"

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE strategies (id TEXT PRIMARY KEY, params TEXT)")
        conn.execute(
            "INSERT INTO strategies (id, params) VALUES (?, ?)",
            ("strategy-heavy", json.dumps(_heavy_strategy_params(), ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()

    report = module.compact_database(db_path, apply=True, output=output_path, replace=True)
    assert report["replaced_source"] is True
    assert Path(report["backup_path"]).exists()
    assert Path(report["backup_path"]).suffix == ".gz"
    assert db_path.exists()
    assert not output_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        params = json.loads(conn.execute("SELECT params FROM strategies").fetchone()[0])
        assert _json_len(params) < PARAMS_LIMIT
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()
