"""Root runners must not be structurally bound to one data provider.

Every runner used to hardcode ``packages/akshare-mcp/src`` on ``sys.path`` and
import ``akshare_mcp.env_loader`` directly. That was the last place the extraction
plan's "root runners must not go through AKShare" condition stayed open: the
runtime contract they drive is provider-neutral, but the scripts around it were
not.

They now go through ``scripts/factories/_runner_bootstrap.py``, which injects
whichever packages are present and loads env through whichever provider is
installed. A runner works with a data provider, with only the standalone provider
set, and in a standalone repository where no provider was ever installed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


AIASK_ROOT = Path(__file__).resolve().parents[3]
FACTORIES = AIASK_ROOT / "scripts" / "factories"
BOOTSTRAP = FACTORIES / "_runner_bootstrap.py"

#: The five runners the supervisors and the CLI launch.
RUNNERS = (
    "run_strategy_factory.py",
    "run_factor_mining_factory.py",
    "run_incubation_factory.py",
    "run_market_event_ingest.py",
    "run_signal_tracker.py",
)


pytestmark = pytest.mark.skipif(
    not FACTORIES.is_dir(), reason="runner scripts are not present"
)


def _source(name: str) -> str:
    return (FACTORIES / name).read_text(encoding="utf-8-sig")


# --- the shared bootstrap ------------------------------------------------------


def test_the_shared_bootstrap_exists() -> None:
    assert BOOTSTRAP.is_file(), (
        "runners need a shared, provider-neutral bootstrap; without it each one "
        "re-invents provider discovery"
    )


def test_the_bootstrap_names_no_single_provider_as_required() -> None:
    """It may *know* provider names; it must not depend on one being present."""
    source = BOOTSTRAP.read_text(encoding="utf-8-sig")

    assert "_PROVIDER_PACKAGES" in source
    # More than one candidate, so no single provider is structural.
    assert source.count('("akshare-mcp"') + source.count('("data-mcp"') >= 2


def test_the_bootstrap_reports_when_no_provider_is_installed() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8-sig")
    assert "no_data_provider_installed" in source, (
        "a missing provider must be reported, not silently treated as loaded"
    )


# --- no hardcoded provider paths ----------------------------------------------


@pytest.mark.parametrize("runner", RUNNERS)
def test_runner_does_not_hardcode_a_provider_source_path(runner: str) -> None:
    source = _source(runner)

    assert '"akshare-mcp" / "src"' not in source, (
        f"{runner} still injects a specific provider's source path"
    )
    assert '"packages" / "akshare-mcp"' not in source


@pytest.mark.parametrize("runner", RUNNERS)
def test_runner_does_not_import_a_provider_env_loader_directly(runner: str) -> None:
    source = _source(runner)

    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "``" in line:
            continue  # prose may name what it replaced
        assert "from akshare_mcp.env_loader" not in stripped, (
            f"{runner} imports a provider env loader directly; use "
            "_runner_bootstrap.load_provider_env"
        )


@pytest.mark.parametrize("runner", RUNNERS)
def test_runner_uses_the_shared_bootstrap(runner: str) -> None:
    source = _source(runner)
    assert "_runner_bootstrap" in source, (
        f"{runner} must route path and env setup through the shared bootstrap"
    )


# --- runners still start ------------------------------------------------------


@pytest.mark.parametrize("runner", RUNNERS)
def test_runner_still_starts(runner: str) -> None:
    """A neutral bootstrap must not break the entry point."""
    completed = subprocess.run(
        [sys.executable, str(FACTORIES / runner), "--help"],
        capture_output=True,
        text=True,
        timeout=180,
        env={
            "PATH": "/usr/bin:/bin",
            "AIASK_PROJECT_ROOT": str(AIASK_ROOT),
            "PYTHONPATH": ":".join(
                str(AIASK_ROOT / "packages" / package / "src")
                for package in ("aiask-quant-core", "strategy-factory", "akshare-mcp")
            ),
        },
    )

    assert completed.returncode == 0, f"{runner} --help failed:\n{completed.stderr[-1500:]}"
    assert "usage" in completed.stdout.lower()


# --- behaviour with no provider ----------------------------------------------


def test_bootstrap_degrades_when_every_provider_is_absent() -> None:
    """The honest outcome is "no provider", not a crash."""
    code = (
        "import importlib.abc, json, sys\n"
        "class B(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if str(fullname).split('.')[0] in ('akshare_mcp', 'aiask_data_mcp'):\n"
        "            raise ModuleNotFoundError('blocked', name=fullname)\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        f"sys.path.insert(0, {str(FACTORIES)!r})\n"
        "from _runner_bootstrap import load_provider_env, bootstrap_paths\n"
        f"paths = bootstrap_paths(__import__('pathlib').Path({str(AIASK_ROOT)!r}))\n"
        "env = load_provider_env()\n"
        "print(json.dumps({'env': env, 'injected': paths['injected']}))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr

    import json

    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["env"]["loaded"] is False
    assert payload["env"]["provider"] is None
    assert payload["env"]["reason"] == "no_data_provider_installed"
    # Core packages are still injected; only the provider is missing.
    assert "strategy-factory" in payload["injected"]


def test_bootstrap_prefers_a_present_provider() -> None:
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(FACTORIES)!r})\n"
        + "".join(
            f"sys.path.insert(0, {str(AIASK_ROOT / 'packages' / package / 'src')!r})\n"
            for package in ("aiask-quant-core", "strategy-factory", "akshare-mcp")
        )
        + "from _runner_bootstrap import load_provider_env\n"
        "print(json.dumps(load_provider_env()))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr

    import json

    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["loaded"] is True
    assert payload["provider"] is not None
