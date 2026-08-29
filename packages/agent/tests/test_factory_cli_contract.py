"""Contract tests for the host-neutral ``aiask factory`` control plane.

These pin the properties any host depends on: the governed envelope shape, exit
code semantics, four-factory topology (sidecar excluded by default), and the
refusal to enable live trading in spawned runners.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from aiask_agent import factory_cli
from strategy_factory.contracts.run_envelope import REQUIRED_ENVELOPE_FIELDS


def _args(**overrides):
    import argparse

    defaults = {
        "only": None,
        "with_sidecar": False,
        "timeout": 30.0,
        "venv_python": "",
        "stop_on_failure": False,
        "sqlite_path": "",
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# --- topology -----------------------------------------------------------------


def test_four_factories_membership_is_exact() -> None:
    assert set(factory_cli.FACTORY_RUNNERS) == {
        "strategy_factory",
        "factor_mining",
        "incubation",
        "market_event_ingest",
    }


def test_signal_tracker_is_a_sidecar_not_a_factory() -> None:
    """Conflating these has caused topology drift before; pin it."""
    assert "signal_tracker" in factory_cli.SIDECAR_RUNNERS
    assert "signal_tracker" not in factory_cli.FACTORY_RUNNERS


def test_default_targets_exclude_the_sidecar() -> None:
    assert factory_cli.resolve_targets(None, include_sidecar=False) == [
        "strategy_factory",
        "factor_mining",
        "incubation",
        "market_event_ingest",
    ]


def test_sidecar_is_opt_in() -> None:
    targets = factory_cli.resolve_targets(None, include_sidecar=True)
    assert targets[-1] == "signal_tracker"


def test_unknown_runtime_is_rejected_loudly() -> None:
    with pytest.raises(SystemExit) as excinfo:
        factory_cli.resolve_targets(["nope"], include_sidecar=False)
    assert "unknown runtime" in str(excinfo.value)


def test_explicit_selection_is_deduplicated_and_order_preserving() -> None:
    targets = factory_cli.resolve_targets(
        ["incubation", "strategy_factory", "incubation"], include_sidecar=False
    )
    assert targets == ["incubation", "strategy_factory"]


# --- envelope shape -----------------------------------------------------------


def test_preflight_reports_factory_domain_storage_readiness() -> None:
    """A missing storage capability must surface before a cycle starts."""
    envelope = factory_cli.cmd_preflight(_args())
    storage = envelope.data.get("storage")

    assert storage is not None, "preflight must report storage readiness"
    assert storage["status"] in {"ready", "incomplete", "skipped", "error"}
    if storage["status"] == "ready":
        assert storage["missing"] == []
        assert storage["available"] == storage["declared"]


def test_storage_report_skips_cleanly_when_the_db_provider_is_absent() -> None:
    """An unresolved provider is reported elsewhere; storage must not guess."""
    report = factory_cli._storage_capability_report(["db_provider"])

    assert report["status"] == "skipped"
    assert report["reason"] == "db_provider_missing"


def test_storage_gaps_degrade_the_envelope(monkeypatch) -> None:
    monkeypatch.setattr(
        factory_cli,
        "_storage_capability_report",
        lambda _missing: {
            "status": "incomplete",
            "missing": ["save_scheduler_state"],
            "declared": 32,
            "available": 31,
            "ready": False,
        },
    )

    envelope = factory_cli.cmd_preflight(_args())

    assert any(
        "storage_capability_missing:save_scheduler_state" in reason
        for reason in envelope.degraded_reasons
    )
    assert envelope.ok is False, "an incomplete storage surface is not a clean success"


def test_status_emits_every_governed_field() -> None:
    payload = factory_cli.cmd_status(_args()).to_dict()
    for field_name in REQUIRED_ENVELOPE_FIELDS:
        assert field_name in payload, f"missing governed field {field_name}"
    assert payload["runtime"] == "factory_status"
    assert payload["data"]["four_factories"] == sorted(factory_cli.FACTORY_RUNNERS)


def test_schedule_template_installs_nothing_and_says_so() -> None:
    envelope = factory_cli.cmd_schedule_template(_args())
    assert envelope.data["installed"] is False
    assert "template_only_nothing_installed" in envelope.degraded_reasons
    # Every sample line must be commented out.
    for line in envelope.data["crontab"].splitlines():
        assert line.startswith("#"), f"schedule template line is live: {line}"
    assert envelope.exit_code() == 2, "template-only must not read as clean success"


def test_evidence_blocks_when_the_ledger_is_absent(tmp_path) -> None:
    envelope = factory_cli.cmd_evidence(_args(sqlite_path=str(tmp_path / "missing.sqlite3")))
    assert envelope.status == "blocked"
    assert envelope.exit_code() == 1
    assert any("sqlite_missing" in error for error in envelope.errors)


def test_evidence_counts_rows_from_a_real_ledger(tmp_path) -> None:
    import sqlite3

    db = tmp_path / "ledger.sqlite3"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE strategy_factory_runs (id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO strategy_factory_runs (id) VALUES (1), (2)")
    connection.commit()
    connection.close()

    envelope = factory_cli.cmd_evidence(_args(sqlite_path=str(db)))

    assert envelope.counts["strategy_factory_runs"] == 2
    assert any("strategy_factory_runs" in ref for ref in envelope.evidence_refs)
    # Tables that do not exist are reported as degraded, not fatal.
    assert any("unreadable" in reason for reason in envelope.degraded_reasons)


# --- subprocess behaviour ------------------------------------------------------


def test_missing_runner_blocks_instead_of_crashing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(factory_cli, "project_root", lambda: tmp_path)
    envelope = factory_cli._run_runner("strategy_factory", timeout=5)

    assert envelope.status == "blocked"
    assert any("runner_missing" in error for error in envelope.errors)


def test_child_env_forces_live_trading_off(monkeypatch) -> None:
    monkeypatch.setenv("LIVE_TRADING_ENABLED", "1")
    env = factory_cli._child_env()
    assert env["LIVE_TRADING_ENABLED"] == "0", (
        "the control plane is research/paper only; live trading must never be "
        "inherited into a spawned runner"
    )


def test_runner_failure_is_reported_as_failed(monkeypatch, tmp_path) -> None:
    script = tmp_path / "scripts" / "factories" / "run_strategy_factory.py"
    script.parent.mkdir(parents=True)
    script.write_text("import sys\nsys.stderr.write('boom\\n')\nsys.exit(3)\n")
    monkeypatch.setattr(factory_cli, "project_root", lambda: tmp_path)

    envelope = factory_cli._run_runner("strategy_factory", timeout=30)

    assert envelope.status == "failed"
    assert any("exit_code=3" in error for error in envelope.errors)
    assert envelope.exit_code() == 1


def test_runner_json_is_lifted_into_the_envelope(monkeypatch, tmp_path) -> None:
    script = tmp_path / "scripts" / "factories" / "run_strategy_factory.py"
    script.parent.mkdir(parents=True)
    # Prose banner followed by JSON — the real runners do exactly this.
    script.write_text(
        "print('=' * 20)\n"
        "print('Strategy Factory Result')\n"
        "import json\n"
        "print(json.dumps({'status': 'completed', 'run_id': 'sf-42', "
        "'totals': {'submitted': 5}}))\n"
    )
    monkeypatch.setattr(factory_cli, "project_root", lambda: tmp_path)

    envelope = factory_cli._run_runner("strategy_factory", timeout=30)

    assert envelope.run_id == "sf-42", "runner-supplied run_id must be preserved"
    assert envelope.status == "success"
    assert envelope.counts["submitted"] == 5


def test_runner_without_json_is_flagged_as_degraded(monkeypatch, tmp_path) -> None:
    script = tmp_path / "scripts" / "factories" / "run_factor_mining_factory.py"
    script.parent.mkdir(parents=True)
    script.write_text("print('done, no json here')\n")
    monkeypatch.setattr(factory_cli, "project_root", lambda: tmp_path)

    envelope = factory_cli._run_runner("factor_mining", timeout=30)

    assert "runner_emitted_no_json" in envelope.degraded_reasons
    assert envelope.exit_code() == 2


def test_timeout_is_reported_not_raised(monkeypatch, tmp_path) -> None:
    script = tmp_path / "scripts" / "factories" / "run_incubation_factory.py"
    script.parent.mkdir(parents=True)
    script.write_text("import time\ntime.sleep(30)\n")
    monkeypatch.setattr(factory_cli, "project_root", lambda: tmp_path)

    envelope = factory_cli._run_runner("incubation", timeout=1)

    assert envelope.status == "failed"
    assert any("timeout_after" in error for error in envelope.errors)


# --- aggregation --------------------------------------------------------------


def test_partial_success_is_not_reported_as_success(monkeypatch, tmp_path) -> None:
    base = tmp_path / "scripts" / "factories"
    base.mkdir(parents=True)
    (base / "run_strategy_factory.py").write_text(
        "import json\nprint(json.dumps({'status': 'completed'}))\n"
    )
    (base / "run_factor_mining_factory.py").write_text("import sys\nsys.exit(1)\n")
    monkeypatch.setattr(factory_cli, "project_root", lambda: tmp_path)

    envelope = factory_cli.cmd_cycle(
        _args(only=["strategy_factory", "factor_mining"])
    )

    assert envelope.status == "partial"
    assert envelope.counts["succeeded"] == 1
    assert envelope.counts["failed"] == 1
    assert envelope.ok is False
    assert envelope.exit_code() == 2
    # Child failures must be attributed, not flattened.
    assert any(error.startswith("factor_mining:") for error in envelope.errors)


def test_stop_on_failure_halts_the_remaining_runtimes(monkeypatch, tmp_path) -> None:
    base = tmp_path / "scripts" / "factories"
    base.mkdir(parents=True)
    (base / "run_strategy_factory.py").write_text("import sys\nsys.exit(1)\n")
    (base / "run_incubation_factory.py").write_text(
        "import json\nprint(json.dumps({'status': 'completed'}))\n"
    )
    monkeypatch.setattr(factory_cli, "project_root", lambda: tmp_path)

    envelope = factory_cli.cmd_cycle(
        _args(only=["strategy_factory", "incubation"], stop_on_failure=True)
    )

    assert envelope.counts["total"] == 1, "the second runtime must not have started"
    assert envelope.status == "failed"


def test_single_target_returns_the_child_envelope_directly(monkeypatch, tmp_path) -> None:
    base = tmp_path / "scripts" / "factories"
    base.mkdir(parents=True)
    (base / "run_incubation_factory.py").write_text(
        "import json\nprint(json.dumps({'status': 'completed'}))\n"
    )
    monkeypatch.setattr(factory_cli, "project_root", lambda: tmp_path)

    envelope = factory_cli.cmd_once(_args(only=["incubation"]))

    assert envelope.runtime == "incubation", "no needless aggregation wrapper"


# --- end-to-end CLI wiring ----------------------------------------------------


def _cli(*argv: str) -> subprocess.CompletedProcess:
    # tests/ -> agent/ -> packages/ -> <aiask root>
    root = Path(__file__).resolve().parents[3]
    return subprocess.run(
        [sys.executable, "-m", "aiask_agent.cli", *argv],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/bin:/bin",
            "AIASK_PROJECT_ROOT": str(root),
            "PYTHONPATH": ":".join(
                str(root / "packages" / pkg / "src")
                for pkg in ("agent", "strategy-factory", "aiask-quant-core")
            ),
        },
    )


def test_cli_exposes_factory_and_exits_zero_on_success() -> None:
    completed = _cli("factory", "status")
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    for field_name in REQUIRED_ENVELOPE_FIELDS:
        assert field_name in payload


def test_cli_exit_code_two_for_completed_but_not_clean() -> None:
    completed = _cli("factory", "schedule-template")
    assert completed.returncode == 2, (
        "a degraded result must be distinguishable from clean success"
    )
