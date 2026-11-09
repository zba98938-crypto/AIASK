"""Fixed-interval incubation is wired through the real CLI and supervisor."""

import argparse
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

ROOT = Path(__file__).resolve().parents[3]


def load_script(name):
    spec = importlib.util.spec_from_file_location(f"interval_{name}", ROOT / "scripts/factories" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
@pytest.mark.parametrize("interval", [0, 900])
async def test_cli_daemon_forwards_interval_to_canonical_runtime(monkeypatch, interval):
    module = load_script("run_incubation_factory")
    run_daemon = AsyncMock()
    monkeypatch.setattr(module, "_build_runtime", lambda args: SimpleNamespace(run_daemon=run_daemon))
    await module._run_daemon(argparse.Namespace(run_time="18:30", dry_run=False, interval=interval))
    run_daemon.assert_awaited_once_with(interval_sec=interval)


def test_real_cli_parses_interval_mode(monkeypatch):
    module = load_script("run_incubation_factory")
    run_daemon = AsyncMock()
    monkeypatch.setattr(module, "_run_daemon", run_daemon)
    monkeypatch.setattr(module, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(module, "_load_project_env", lambda: None)
    monkeypatch.setattr(module, "_setup_logging", lambda **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["run_incubation_factory.py", "--daemon", "--interval", "900"])
    assert module.main() == 0
    assert run_daemon.await_args.args[0].interval == 900


@pytest.mark.parametrize("interval", [0, 900])
def test_supervisor_forwards_interval_without_duplicate_catchup(interval):
    module = load_script("run_three_factories")
    args = module.parse_args(["--python", sys.executable, "--incubation-interval", str(interval)])
    spec = next(row for row in module._build_specs(args, {}) if row.name == "incubation_factory")
    if interval:
        index = spec.command.index("--interval")
        assert spec.command[index + 1] == str(interval)
        assert not module._should_run_incubation_catchup(args)
    else:
        assert "--interval" not in spec.command
        assert module._should_run_incubation_catchup(args)


def test_supervisor_rejects_negative_interval():
    module = load_script("run_three_factories")
    with pytest.raises(SystemExit) as exc:
        module.parse_args(["--incubation-interval", "-1"])
    assert exc.value.code == 2


def test_cli_rejects_negative_interval_before_runtime_setup(monkeypatch):
    module = load_script("run_incubation_factory")
    monkeypatch.setattr(sys, "argv", ["run_incubation_factory.py", "--daemon", "--interval", "-1"])
    def setup_must_not_run():
        raise AssertionError("invalid CLI must fail before runtime setup")
    monkeypatch.setattr(module, "ensure_runtime_dirs", setup_must_not_run)
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2
