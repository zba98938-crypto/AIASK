from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_script_module(module_name: str, script_name: str):
    script_path = REPO_ROOT / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_acceptance_script_rejects_blocker_taxonomy_drift():
    acceptance_mod = _load_script_module(
        "strategy_execution_audit_acceptance_test",
        "strategy-execution-audit-acceptance.py",
    )

    with pytest.raises(ValueError, match="contract mismatch"):
        acceptance_mod._normalize_detail_dimension(
            top_level_values=["insufficient_samples"],
            blocker_details=[{"blocker": "promotion_hard_gate_pending"}],
            detail_key="blocker",
            field_name="blockers",
            strategy_id="strategy-gap",
        )


def test_replay_loader_selects_sample_gap_rows_from_acceptance_report(tmp_path: Path):
    replay_mod = _load_script_module(
        "strategy_incubation_history_replay_test",
        "strategy-incubation-history-replay.py",
    )
    report_path = tmp_path / "execution_audit_acceptance.json"
    report_path.write_text(
        json.dumps(
            {
                "report_type": "execution_audit_acceptance",
                "schema_version": "execution_audit_acceptance.v2",
                "strategy_results": [
                    {
                        "strategy_id": "strategy-gap",
                        "has_sample_gap": True,
                        "blockers": ["promotion_hard_gate_pending"],
                        "gap_categories": ["sample_gap"],
                        "blocker_details": [
                            {
                                "blocker": "promotion_hard_gate_pending",
                                "category": "sample_gap",
                            }
                        ],
                    },
                    {
                        "strategy_id": "strategy-metrics",
                        "has_sample_gap": False,
                        "blockers": ["failed_metrics"],
                        "gap_categories": ["performance_gap"],
                        "blocker_details": [
                            {
                                "blocker": "failed_metrics",
                                "category": "performance_gap",
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    assert replay_mod._load_strategy_ids_from_acceptance_report(
        report_path,
        sample_gap_only=True,
    ) == ["strategy-gap"]


def test_replay_acceptance_summary_recognizes_bootstrap_ready_as_sample_gap():
    replay_mod = _load_script_module(
        "strategy_incubation_history_replay_summary_test",
        "strategy-incubation-history-replay.py",
    )

    summary = replay_mod._summarize_acceptance(
        {
            "status": "needs_attention",
            "acceptance_matrix": {"overall_ready": False},
            "blockers": ["promotion_hard_gate_pending"],
            "gap_categories": ["sample_gap"],
            "trade_audit_summary": {
                "execution_audit_gate_status": "bootstrap_ready",
                "realized_trade_count": 5,
            },
        }
    )

    assert summary["overall_ready"] is False
    assert summary["has_sample_gap"] is True
    assert summary["execution_audit_gate_status"] == "bootstrap_ready"
    assert summary["realized_trade_count"] == 5
    assert summary["blockers"] == ["promotion_hard_gate_pending"]


def test_replay_result_summary_surfaces_gate_counts_and_strategy_acceptance():
    replay_mod = _load_script_module(
        "strategy_incubation_history_replay_result_summary_test",
        "strategy-incubation-history-replay.py",
    )

    summary = replay_mod._summarize_replay_result(
        {
            "count": 2,
            "replayed_days": 40,
            "non_empty_days": 6,
            "orders_created": 4,
            "orders_filled": 3,
            "rejected_orders": 1,
            "metrics_recorded": 2,
            "acceptance_status_counts": {"ready": 1, "pending_data": 1},
            "execution_audit_gate_status_counts": {"passed": 1, "bootstrap_pending": 1},
            "execution_hard_gate_passed_count": 1,
            "acceptance_overall_ready_count": 1,
            "acceptance_sample_gap_count": 1,
            "acceptance_realized_trade_count_total": 8,
            "items": [
                {
                    "strategy_id": "strategy-ready",
                    "acceptance": {
                        "status": "ready",
                        "acceptance_matrix": {"overall_ready": True},
                        "execution_hard_gate_passed": True,
                        "trade_audit_summary": {
                            "execution_audit_gate_status": "passed",
                            "realized_trade_count": 8,
                        },
                    },
                },
                {
                    "strategy_id": "strategy-gap",
                    "error": "boom",
                    "acceptance": {
                        "status": "pending_data",
                        "gap_categories": ["sample_gap"],
                        "trade_audit_summary": {
                            "execution_audit_gate_status": "bootstrap_pending",
                            "realized_trade_count": 0,
                        },
                    },
                },
            ],
        }
    )

    assert summary["execution_hard_gate_passed_count"] == 1
    assert summary["acceptance_realized_trade_count_total"] == 8
    assert summary["error_strategy_ids"] == ["strategy-gap"]
    assert summary["acceptance_by_strategy"]["strategy-ready"]["overall_ready"] is True
    assert summary["acceptance_by_strategy"]["strategy-ready"]["execution_audit_gate_status"] == "passed"
    assert summary["acceptance_by_strategy"]["strategy-gap"]["has_sample_gap"] is True


def test_replay_script_can_copy_sqlite_database_for_shadow_replay(tmp_path: Path):
    replay_mod = _load_script_module(
        "strategy_incubation_history_replay_shadow_copy_test",
        "strategy-incubation-history-replay.py",
    )
    source_db = tmp_path / "source.sqlite3"
    shadow_db = tmp_path / "shadow.sqlite3"
    with sqlite3.connect(source_db) as conn:
        conn.execute("create table marker(id text primary key, value integer)")
        conn.execute("insert into marker(id, value) values('ok', 42)")

    result = replay_mod._copy_sqlite_database(source_db, shadow_db)

    assert result["status"] == "copied"
    assert result["copied"] is True
    with sqlite3.connect(shadow_db) as conn:
        assert conn.execute("select value from marker where id='ok'").fetchone()[0] == 42

    existing = replay_mod._copy_sqlite_database(source_db, shadow_db)
    assert existing["status"] == "exists"
    assert existing["copied"] is False


def test_replay_script_execute_runs_selected_sample_gap_ids(tmp_path: Path, monkeypatch, capsys):
    replay_mod = _load_script_module(
        "strategy_incubation_history_replay_execute_test",
        "strategy-incubation-history-replay.py",
    )
    report_path = tmp_path / "execution_audit_acceptance.json"
    report_path.write_text(
        json.dumps(
            {
                "strategy_results": [
                    {
                        "strategy_id": "strategy-gap",
                        "gap_categories": ["sample_gap"],
                    },
                    {
                        "strategy_id": "strategy-metrics",
                        "gap_categories": ["performance_gap"],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    calls = []

    async def _execute(strategy_ids, **kwargs):
        calls.append((list(strategy_ids), dict(kwargs)))
        return {
            "strategy_ids": list(strategy_ids),
            "missing_strategy_ids": [],
            "replay_result": {
                "count": len(strategy_ids),
                "acceptance_status_counts": {"pending_data": 1},
            },
        }

    monkeypatch.setattr(replay_mod, "_execute_history_replay", _execute)

    assert replay_mod.main([str(report_path), "--sample-gap-only", "--execute", "--start-date", "2026-04-01"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["strategy_ids"] == ["strategy-gap"]
    assert payload["replay_result"]["acceptance_status_counts"] == {"pending_data": 1}
    assert payload["replay_summary"]["acceptance_status_counts"] == {"pending_data": 1}
    assert payload["replay_summary"]["execution_hard_gate_passed_count"] == 0
    assert calls == [
        (
            ["strategy-gap"],
            {
                "start_date": replay_mod.date(2026, 4, 1),
                "end_date": None,
                "include_market_days": True,
                "max_dates": 1500,
                "force_close_open_positions": False,
                "run_acceptance": True,
                "source_db": None,
                "shadow_db": None,
                "overwrite_shadow_db": False,
            },
        )
    ]


def test_replay_script_execute_accepts_direct_strategy_ids(monkeypatch, capsys):
    replay_mod = _load_script_module(
        "strategy_incubation_history_replay_direct_execute_test",
        "strategy-incubation-history-replay.py",
    )
    calls = []

    async def _execute(strategy_ids, **kwargs):
        calls.append((list(strategy_ids), dict(kwargs)))
        return {
            "strategy_ids": list(strategy_ids),
            "missing_strategy_ids": ["missing"],
            "replay_result": {"count": 1},
        }

    monkeypatch.setattr(replay_mod, "_execute_history_replay", _execute)

    assert replay_mod.main(
        [
            "--strategy-id",
            "direct-strategy",
            "--strategy-id",
            "direct-strategy",
            "--execute",
            "--no-run-acceptance",
            "--no-include-market-days",
            "--force-close-open-positions",
            "--max-dates",
            "30",
            "--source-db",
            "source.sqlite3",
            "--shadow-db",
            "shadow.sqlite3",
            "--overwrite-shadow-db",
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["strategy_ids"] == ["direct-strategy"]
    assert payload["missing_strategy_ids"] == ["missing"]
    assert calls[0][0] == ["direct-strategy"]
    assert calls[0][1]["run_acceptance"] is False
    assert calls[0][1]["include_market_days"] is False
    assert calls[0][1]["force_close_open_positions"] is True
    assert calls[0][1]["max_dates"] == 30
    assert calls[0][1]["source_db"] == "source.sqlite3"
    assert calls[0][1]["shadow_db"] == "shadow.sqlite3"
    assert calls[0][1]["overwrite_shadow_db"] is True
