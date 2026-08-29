from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _load_script_module(name: str, relative_path: str):
    path = PROJECT_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_market_event_ingest_runner_uses_notice_only_defaults(monkeypatch) -> None:
    runner = _load_script_module(
        "_aiask_test_market_event_ingest_runner_defaults",
        "scripts/factories/run_market_event_ingest.py",
    )
    monkeypatch.setenv("MARKET_EVENT_INGEST_INTERVAL_SEC", "42")
    monkeypatch.setenv("MARKET_EVENT_INGEST_ERROR_SLEEP_SEC", "7")

    args = runner.parse_args([])
    kwargs = runner._build_ingest_kwargs(args)

    assert args.interval == 42
    assert args.error_sleep == 7
    assert kwargs["doc_types"] == ["notice"]
    assert kwargs["official_notice_limit"] == 30
    assert kwargs["notice_limit"] == 40
    assert kwargs["code_notice_limit"] == 2
    assert kwargs["news_limit"] == 0
    assert kwargs["research_code_limit"] == 0
    assert kwargs["embed"] is False
    assert kwargs["build_snapshot"] is False
    assert kwargs["activate_snapshot"] is False
    assert kwargs["allow_network"] is True


def test_market_event_ingest_runner_cli_overrides_are_normalized() -> None:
    runner = _load_script_module(
        "_aiask_test_market_event_ingest_runner_cli",
        "scripts/factories/run_market_event_ingest.py",
    )

    args = runner.parse_args(
        [
            "--once",
            "--codes",
            "000001,600519",
            "300750",
            "--official-notice-limit",
            "10",
            "--notice-limit",
            "11",
            "--code-notice-limit",
            "1",
            "--notice-days",
            "14",
            "--no-network",
        ]
    )
    kwargs = runner._build_ingest_kwargs(args)

    assert args.once is True
    assert kwargs["stock_codes"] == ["000001", "600519", "300750"]
    assert kwargs["official_notice_limit"] == 10
    assert kwargs["notice_limit"] == 11
    assert kwargs["code_notice_limit"] == 1
    assert kwargs["notice_days"] == 14
    assert kwargs["allow_network"] is False


def test_three_factory_supervisor_adds_market_event_ingest_and_can_disable_it() -> None:
    supervisor = _load_script_module(
        "_aiask_test_run_three_factories",
        "scripts/factories/run_three_factories.py",
    )

    args = supervisor.parse_args(
        ["--python", "python", "--no-strategy", "--no-factor", "--no-incubation"]
    )
    specs = supervisor._build_specs(args, {})

    assert [spec.name for spec in specs] == ["market_event_ingest"]
    assert specs[0].log_name == "market_event_ingest.log"
    assert specs[0].command[-1].endswith("run_market_event_ingest.py")

    env_disabled_specs = supervisor._build_specs(args, {"MARKET_EVENT_INGEST_ENABLED": "0"})
    assert env_disabled_specs == []

    cli_disabled_args = supervisor.parse_args(
        [
            "--python",
            "python",
            "--no-strategy",
            "--no-factor",
            "--no-incubation",
            "--no-event-ingest",
        ]
    )
    assert supervisor._build_specs(cli_disabled_args, {}) == []


def test_three_factory_supervisor_writes_ingest_startup_manifest(tmp_path) -> None:
    supervisor = _load_script_module(
        "_aiask_test_run_three_factories_manifest",
        "scripts/factories/run_three_factories.py",
    )

    args = supervisor.parse_args(
        [
            "--python",
            "python",
            "--log-dir",
            str(tmp_path),
            "--no-strategy",
            "--no-factor",
            "--no-incubation",
        ]
    )
    specs = supervisor._build_specs(args, {})

    supervisor._write_startup_manifest(log_dir=tmp_path, specs=specs, args=args, env={})

    manifest = json.loads((tmp_path / "supervisor_startup.json").read_text(encoding="utf-8"))
    planned_names = [item["name"] for item in manifest["factories"]]

    assert planned_names == ["market_event_ingest"]
    assert manifest["market_event_ingest_enabled"] is True
    assert manifest["factories"][0]["log"].endswith("market_event_ingest.log")
    assert (tmp_path / "market_event_ingest.log").exists()
    assert "supervisor planned market_event_ingest" in (
        tmp_path / "market_event_ingest.log"
    ).read_text(encoding="utf-8")


def test_three_factory_supervisor_requires_explicit_aiask_interpreter(monkeypatch) -> None:
    supervisor = _load_script_module(
        "_aiask_test_run_three_factories_python_default",
        "scripts/factories/run_three_factories.py",
    )

    monkeypatch.delenv("AIASK_FACTORY_PYTHON", raising=False)
    args = supervisor.parse_args([])

    with pytest.raises(SystemExit, match="does not fall back to the current interpreter"):
        supervisor._python_path(args)


def test_three_factory_supervisor_python_override_wins(monkeypatch) -> None:
    supervisor = _load_script_module(
        "_aiask_test_run_three_factories_python_override",
        "scripts/factories/run_three_factories.py",
    )

    monkeypatch.setenv("AIASK_FACTORY_PYTHON", "C:/custom/python.exe")
    args = supervisor.parse_args([])

    assert supervisor._python_path(args) == "C:/custom/python.exe"
