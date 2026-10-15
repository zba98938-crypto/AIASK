"""Bootstrap planning must repair a half-created venv, not skip it.

Regression this guards
----------------------
``_plan_commands`` decided whether to run ``python -m venv`` from
``venv_dir.exists()``. On this machine ``~/.aiask/venv`` existed while
``bin/python`` did not (``bin/`` held console scripts and activate scripts
only), so the planner skipped creation and produced

    [<missing python>, "-m", "pip", "install", "--upgrade", "pip"]

a sequence that cannot succeed, and whose failure the resulting report never
explained as "the venv has no interpreter".

These tests call the real planner and assert on the command list it returns.
They never execute a command, so no venv is created or modified.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _load_bootstrap():
    scripts_dir = ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    spec = importlib.util.spec_from_file_location(
        "aiask_venv_bootstrap_under_test",
        scripts_dir / "aiask_venv_bootstrap.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _plan(module, venv_dir: Path, venv_python: Path, packages=("agent",)):
    return module._plan_commands(
        aiask_root=ROOT,
        venv_dir=venv_dir,
        venv_python=venv_python,
        base_launcher=["/usr/bin/python3.12"],
        packages=list(packages),
    )


def _creates_venv(commands, venv_dir: Path) -> bool:
    return any(
        "-m" in cmd and "venv" in cmd and str(venv_dir) in cmd for cmd in commands
    )


def test_absent_venv_is_created(tmp_path: Path) -> None:
    module = _load_bootstrap()
    venv_dir = tmp_path / "venv"  # never created
    commands = _plan(module, venv_dir, venv_dir / "bin" / "python")
    assert _creates_venv(commands, venv_dir), commands
    assert module._venv_state(venv_dir, venv_dir / "bin" / "python") == "absent"


def test_half_created_venv_is_repaired_not_skipped(tmp_path: Path) -> None:
    """The regression: directory present, interpreter missing."""

    module = _load_bootstrap()
    venv_dir = tmp_path / "venv"
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "pip").write_text("#!/bin/sh\n", encoding="utf-8")
    (venv_dir / "pyvenv.cfg").write_text("home = /gone\n", encoding="utf-8")
    venv_python = venv_dir / "bin" / "python"
    assert not venv_python.exists()

    assert module._venv_state(venv_dir, venv_python) == "incomplete"

    commands = _plan(module, venv_dir, venv_python)
    assert _creates_venv(commands, venv_dir), (
        "a venv directory without an interpreter must still be created/repaired; "
        f"planning only `python -m pip` against a missing interpreter cannot work: {commands}"
    )


def test_present_venv_is_not_recreated(tmp_path: Path) -> None:
    module = _load_bootstrap()
    venv_dir = tmp_path / "venv"
    (venv_dir / "bin").mkdir(parents=True)
    venv_python = venv_dir / "bin" / "python"
    venv_python.write_text("#!/bin/sh\n", encoding="utf-8")

    assert module._venv_state(venv_dir, venv_python) == "present"

    commands = _plan(module, venv_dir, venv_python)
    assert not _creates_venv(commands, venv_dir), commands
    # A usable venv still gets the package installs.
    assert any("pip" in cmd for cmd in commands), commands
