from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]


def test_missing_runtime_modules_reports_unavailable_specs(monkeypatch):
    from strategy_factory import runtime_bootstrap as rb

    monkeypatch.setattr(
        rb.importlib.util,
        "find_spec",
        lambda name: None if name in {"numpy", "httpx"} else object(),
    )

    assert rb.missing_runtime_modules(("numpy", "pandas", "httpx")) == ["numpy", "httpx"]


def test_missing_runtime_distributions_reports_uninstalled_packages(monkeypatch):
    from strategy_factory import runtime_bootstrap as rb

    def _fake_distribution(name: str):
        if name == "strategy-factory":
            raise rb.importlib.metadata.PackageNotFoundError
        return object()

    monkeypatch.setattr(rb.importlib.metadata, "distribution", _fake_distribution)

    assert rb.missing_runtime_distributions(("strategy-factory", "aiask-quant-core")) == [
        "strategy-factory",
    ]


def test_direct_script_detection_skips_pytest_import_context(monkeypatch):
    from strategy_factory import runtime_bootstrap as rb

    monkeypatch.setattr(rb.sys, "argv", ["pytest", "packages/strategy-factory/tests/test_runtime_provider_boundary.py"])

    assert rb._is_direct_script_invocation(Path("C:/repo/scripts/factories/run_strategy_factory.py")) is False


def test_build_uv_reexec_command_includes_editable_packages(monkeypatch):
    from strategy_factory import runtime_bootstrap as rb

    monkeypatch.setenv(rb.BOOTSTRAP_PYTHON_ENV_KEY, "3.12")
    monkeypatch.setenv(rb.BOOTSTRAP_UV_ENV_KEY, "uv")
    monkeypatch.setenv(rb.BOOTSTRAP_PROJECT_ENV_KEY, "packages/agent")

    command = rb.build_uv_reexec_command(
        project_root=Path("C:/repo"),
        script_path=Path("C:/repo/run_strategy_factory.py"),
        argv=["--once", "--codes", "601288"],
    )

    assert command[:4] == ["uv", "run", "--python", "3.12"]
    assert command[4:6] == ["--project", str(Path("C:/repo/packages/agent").resolve())]
    assert "--with-editable" in command
    assert "packages/strategy-factory" in command
    assert command[-5:] == [
        "python",
        str(Path("C:/repo/run_strategy_factory.py").resolve()),
        "--once",
        "--codes",
        "601288",
    ]


def test_default_editable_packages_exclude_akshare_host_package() -> None:
    from strategy_factory import runtime_bootstrap as rb

    assert rb.DEFAULT_EDITABLE_PACKAGES == (
        "packages/strategy-factory",
        "packages/aiask-quant-core",
    )


def test_ensure_factory_runtime_reexecs_with_uv_when_modules_missing(monkeypatch):
    from strategy_factory import runtime_bootstrap as rb

    monkeypatch.setattr(rb, "_is_direct_script_invocation", lambda *args, **kwargs: True)
    monkeypatch.setattr(rb, "missing_runtime_modules", lambda module_names=None: ["numpy"])
    monkeypatch.setattr(rb, "missing_runtime_distributions", lambda distribution_names=None: [])
    monkeypatch.setattr(rb.shutil, "which", lambda name: "uv" if name == "uv" else None)

    captured: dict[str, object] = {}

    def _fake_run(command, *, cwd, env, check):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = env
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(rb.subprocess, "run", _fake_run)

    with pytest.raises(SystemExit) as exc:
        rb.ensure_factory_runtime(
            project_root=Path("C:/repo"),
            script_path=Path("C:/repo/run_strategy_factory.py"),
            argv=["--once"],
        )

    assert exc.value.code == 0
    assert captured["cwd"] == str(Path("C:/repo").resolve())
    assert captured["env"][rb.BOOTSTRAP_ENV_KEY] == "1"
    assert captured["command"][:4] == ["uv", "run", "--python", "3.12"]


def test_ensure_factory_runtime_reexecs_with_uv_when_distributions_missing(monkeypatch):
    from strategy_factory import runtime_bootstrap as rb

    monkeypatch.setattr(rb, "_is_direct_script_invocation", lambda *args, **kwargs: True)
    monkeypatch.setattr(rb, "missing_runtime_modules", lambda module_names=None: [])
    monkeypatch.setattr(
        rb,
        "missing_runtime_distributions",
        lambda distribution_names=None: ["strategy-factory"],
    )
    monkeypatch.setattr(rb.shutil, "which", lambda name: "uv" if name == "uv" else None)

    captured: dict[str, object] = {}

    def _fake_run(command, *, cwd, env, check):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["env"] = env
        captured["check"] = check
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(rb.subprocess, "run", _fake_run)

    with pytest.raises(SystemExit) as exc:
        rb.ensure_factory_runtime(
            project_root=Path("C:/repo"),
            script_path=Path("C:/repo/run_signal_tracker.py"),
            argv=["--once"],
            uv_project="packages/agent",
        )

    assert exc.value.code == 0
    assert "--project" in captured["command"]
    assert str(Path("C:/repo/packages/agent").resolve()) in captured["command"]


def test_signal_tracker_launchers_preflight_runtime_before_akshare_imports():
    launchers = [
        ROOT / "scripts" / "factories" / "run_signal_tracker.py",
        ROOT / "packages" / "akshare-mcp" / "scripts" / "run_signal_tracker.py",
    ]

    for path in launchers:
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "from strategy_factory.runtime_bootstrap import ensure_factory_runtime" in text
        assert "ensure_factory_runtime(" in text
        if "from akshare_mcp" in text:
            assert text.index("ensure_factory_runtime(") < text.index("from akshare_mcp")


def test_root_launchers_use_canonical_strategy_factory_runtimes() -> None:
    launcher_expectations = {
        ROOT / "scripts" / "factories" / "run_factor_mining_factory.py": [
            "from strategy_factory.runtime.factor_mining import get_factor_mining_runtime",
        ],
        ROOT / "scripts" / "factories" / "run_incubation_factory.py": [
            "from strategy_factory.runtime.incubation import build_incubation_runtime",
        ],
        ROOT / "scripts" / "factories" / "run_market_event_ingest.py": [
            "from strategy_factory.runtime.market_event_ingest import get_market_event_ingest_runtime",
        ],
        ROOT / "scripts" / "factories" / "run_signal_tracker.py": [
            "from strategy_factory.runtime.signal_tracker import get_signal_tracker_runtime",
        ],
    }

    forbidden_tokens = (
        "from akshare_mcp.services.factor_mining_factory",
        "from run_incubation_factory import main as target_main",
        "run_market_text_source_ingest(",
        "from run_signal_tracker import main as target_main",
        'packages/akshare-mcp/scripts/run_incubation_factory.py',
        'packages/akshare-mcp/scripts/run_signal_tracker.py',
    )

    for path, required_tokens in launcher_expectations.items():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in required_tokens:
            assert token in text
        for token in forbidden_tokens:
            assert token not in text
