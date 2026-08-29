"""Clean-install E2E: the packages must work when actually installed.

Phase 5 of the extraction plan. Every other boundary test manipulates
``sys.path``, which hides packaging defects: an implicit namespace package
resolves fine under a test ``pythonpath`` and then fails after ``pip install``.
That is not hypothetical — this suite is what caught
``strategy_factory.contracts`` missing its ``__init__.py``.

The heavy test builds a throwaway virtualenv, installs quant-core and
strategy-factory into it, and runs a real four-runtime cycle with no host
package present. It is opt-in because it downloads and compiles dependencies:

    AIASK_RUN_CLEAN_INSTALL_E2E=1 pytest tests/test_clean_install_e2e.py

The cheap structural checks always run, so the packaging defect class stays
guarded even when the slow test is skipped.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest


PACKAGES_ROOT = Path(__file__).resolve().parents[2]
SF_ROOT = PACKAGES_ROOT / "strategy-factory"
SF_SRC = SF_ROOT / "src" / "strategy_factory"

#: Subpackages that must be importable after an install.
DECLARED_SUBPACKAGES = (
    "api",
    "application",
    "contracts",
    "domain",
    "infrastructure",
    "runtime",
)


# --- structural checks (always run) --------------------------------------------


@pytest.mark.parametrize("subpackage", DECLARED_SUBPACKAGES)
def test_every_subpackage_declares_itself(subpackage: str) -> None:
    """An implicit namespace package breaks after install, not during tests.

    ``strategy_factory.contracts`` shipped without ``__init__.py`` and resolved
    only because the test configuration adds ``src`` to ``pythonpath``. A clean
    install raised ``ModuleNotFoundError`` on its submodules.
    """
    marker = SF_SRC / subpackage / "__init__.py"
    assert marker.is_file(), (
        f"strategy_factory.{subpackage} has no __init__.py; it will not resolve "
        "after an install even though tests pass"
    )


def test_no_subpackage_is_left_implicit() -> None:
    """Catches a *new* subpackage added without ``__init__.py``."""
    implicit = sorted(
        path.name
        for path in SF_SRC.iterdir()
        if path.is_dir()
        and path.name not in {"__pycache__"}
        and not (path / "__init__.py").is_file()
        and any(path.glob("*.py"))
    )
    assert implicit == [], f"these subpackages are implicit namespaces: {implicit}"


def test_the_wheel_ships_the_whole_package() -> None:
    import tomllib

    config = tomllib.loads((SF_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]

    assert "src/strategy_factory" in packages


def test_sibling_dependency_is_declared_for_installation() -> None:
    import tomllib

    config = tomllib.loads((SF_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = " ".join(config["project"]["dependencies"])
    sources = config["tool"]["uv"]["sources"]

    assert "aiask-quant-core" in dependencies
    assert sources["aiask-quant-core"]["path"] == "../aiask-quant-core"


# --- the real install (opt-in) --------------------------------------------------


CLEAN_INSTALL_ENABLED = os.getenv("AIASK_RUN_CLEAN_INSTALL_E2E", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

#: Executed inside the fresh environment. Must not assume any host package.
_PROBE = """
import asyncio, importlib.util, json, logging, os, tempfile

logging.disable(logging.ERROR)
os.environ["AIASK_SQLITE_PATH"] = tempfile.mktemp(suffix=".sqlite3")

report = {"host_importable": importlib.util.find_spec("akshare_mcp") is not None}

from strategy_factory.runtime.standalone_providers import (
    configure_standalone_runtime_services,
)
from strategy_factory.runtime.default_bootstrap import (
    get_missing_required_runtime_providers,
)
from strategy_factory.runtime import (
    get_factor_mining_runtime,
    get_incubation_runtime,
    get_market_event_ingest_runtime,
    get_signal_tracker_runtime,
)

configure_standalone_runtime_services()
report["missing_providers"] = sorted(get_missing_required_runtime_providers())

cycles = {}
for name, factory in {
    "factor_mining": get_factor_mining_runtime,
    "incubation": get_incubation_runtime,
    "signal_tracker": get_signal_tracker_runtime,
    "market_event_ingest": get_market_event_ingest_runtime,
}.items():
    runtime = factory()
    entry = {
        "preflight": runtime.preflight() is not None,
        "status": runtime.status() is not None,
    }
    result = asyncio.run(runtime.run_once())
    entry["is_mapping"] = isinstance(result, dict)
    entry["error"] = (result or {}).get("error")
    cycles[name] = entry
report["cycles"] = cycles

# The public contract surface must be importable from an installed wheel.
from strategy_factory.api.runtime import RunEnvelope, verify_host_adapter
from strategy_factory.contracts.host_adapter import HostAdapter
report["contracts_importable"] = True

print("PROBE_JSON:" + json.dumps(report))
"""


@pytest.mark.skipif(
    not CLEAN_INSTALL_ENABLED,
    reason="set AIASK_RUN_CLEAN_INSTALL_E2E=1 (builds a venv and installs dependencies)",
)
def test_clean_install_runs_a_real_cycle_without_any_host(tmp_path) -> None:
    """Install into a fresh venv and complete a four-runtime cycle."""
    environment = tmp_path / "venv"
    venv.create(environment, with_pip=True, clear=True)
    python = environment / ("Scripts" if sys.platform == "win32" else "bin") / "python"

    for distribution in ("aiask-quant-core", "strategy-factory"):
        completed = subprocess.run(
            [str(python), "-m", "pip", "install", "-q", "-e", str(PACKAGES_ROOT / distribution)],
            capture_output=True,
            text=True,
            timeout=1800,
        )
        assert completed.returncode == 0, f"installing {distribution} failed:\n{completed.stderr}"

    completed = subprocess.run(
        [str(python), "-c", _PROBE],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert completed.returncode == 0, completed.stderr

    line = next(
        (l for l in completed.stdout.splitlines() if l.startswith("PROBE_JSON:")), None
    )
    assert line, f"probe produced no report:\n{completed.stdout}"
    report = json.loads(line[len("PROBE_JSON:") :])

    assert report["host_importable"] is False, (
        "the clean environment must not have a host package installed"
    )
    assert report["missing_providers"] == [], (
        f"standalone providers incomplete after install: {report['missing_providers']}"
    )
    assert report["contracts_importable"] is True

    for name, entry in report["cycles"].items():
        assert entry["preflight"] is True, f"{name}: preflight returned nothing"
        assert entry["status"] is True, f"{name}: status returned nothing"
        assert entry["is_mapping"] is True, f"{name}: run_once returned no payload"
        assert entry["error"] is None, f"{name}: cycle reported {entry['error']}"
