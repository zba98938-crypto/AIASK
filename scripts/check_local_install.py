#!/usr/bin/env python3
"""Diagnose the install layer of this AIASK checkout.

Why this exists
---------------
``pytest.ini`` injects every package's ``src`` directory into ``sys.path``, so
the full test suite passes even when the installed distributions are broken.
That is not hypothetical. Seven editable installs pointed at
``/Users/mac/Desktop/AIASK``, a directory that no longer exists, and
``aiask-data-mcp`` was never installed at all. A root ``pytest -q`` reported
``2788 passed`` in the same session that ``akshare-mcp --help`` raised
``ModuleNotFoundError``.

This script tests the install layer instead of the source tree. Probes run in a
subprocess with ``PYTHONPATH`` removed, so a passing result means the packages
are genuinely importable from outside the checkout.

Each item reports one of three states, never a bare boolean:

    ok       resolvable, and located inside this checkout
    stale    resolvable, but pointing outside this checkout
    missing  not resolvable at all

Named ``check_*`` on purpose: pytest must not collect it, because it needs an
interpreter with the packages installed and a populated runtime home.

Usage::

    python scripts/check_local_install.py
    python scripts/check_local_install.py --python /path/to/python
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

SCRIPT = Path(__file__).resolve()
REPO_ROOT = SCRIPT.parents[1]

# distribution name -> import name, in dependency order.
PACKAGES: tuple[tuple[str, str], ...] = (
    ("aiask-quant-core", "aiask_quant_core"),
    ("strategy-factory", "strategy_factory"),
    ("akshare-mcp", "akshare_mcp"),
    ("aiask-data-mcp", "aiask_data_mcp"),
    ("aiask-finance-mcp-servers", "aiask_finance_mcp"),
    ("aiask-skills", "aiask_skills"),
    ("aiask-agent-reference", "aiask_reference_host"),
    ("aiask-agent", "aiask_agent"),
)

PROBE_TIMEOUT_SECONDS = 90


def _probe(python: Path, source: str) -> tuple[int, str, str]:
    """Run ``source`` under ``python`` with PYTHONPATH stripped."""

    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    try:
        completed = subprocess.run(
            [str(python), "-B", "-c", source],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(REPO_ROOT.parent),  # deliberately outside the checkout
            timeout=PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return 1, "", f"probe timed out after {PROBE_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return 1, "", f"{type(exc).__name__}: {exc}"
    return completed.returncode, completed.stdout, completed.stderr


def _classify(module_file: str | None) -> str:
    if not module_file:
        return "missing"
    try:
        Path(module_file).resolve().relative_to(REPO_ROOT)
    except ValueError:
        return "stale"
    return "ok"


def check_packages(python: Path) -> list[tuple[str, str, str]]:
    """Import each package in a clean subprocess and locate it on disk."""

    rows: list[tuple[str, str, str]] = []
    for dist_name, import_name in PACKAGES:
        source = (
            "import importlib, json\n"
            f"m = importlib.import_module({import_name!r})\n"
            "print(json.dumps({'file': getattr(m, '__file__', None)}))\n"
        )
        code, out, err = _probe(python, source)
        if code != 0:
            detail = (err or out).strip().splitlines()
            rows.append((dist_name, "missing", detail[-1] if detail else "import failed"))
            continue
        try:
            payload = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            rows.append((dist_name, "missing", "probe produced no parsable output"))
            continue
        module_file = payload.get("file")
        rows.append((dist_name, _classify(module_file), module_file or "no __file__"))
    return rows


def check_entry_points(python: Path) -> list[tuple[str, str, str]]:
    """Resolve every console script this checkout declares, without running it.

    Discovery and import both happen inside the *target* interpreter. Reading
    entry points from this process's own metadata would silently describe the
    wrong environment whenever ``--python`` names a different one.
    """

    dist_names = [dist_name for dist_name, _import_name in PACKAGES]
    source = (
        "import importlib, json\n"
        "from importlib import metadata\n"
        f"dist_names = {dist_names!r}\n"
        "wanted = {}\n"
        "for dist_name in dist_names:\n"
        "    try:\n"
        "        dist = metadata.distribution(dist_name)\n"
        "    except metadata.PackageNotFoundError:\n"
        "        continue\n"
        "    for entry in dist.entry_points:\n"
        "        if entry.group == 'console_scripts':\n"
        "            wanted[entry.name] = entry.value.split(':')[0].strip()\n"
        "rows = []\n"
        "for script_name in sorted(wanted):\n"
        "    module = wanted[script_name]\n"
        "    try:\n"
        "        importlib.import_module(module)\n"
        "        rows.append([script_name, 'ok', 'import ' + module])\n"
        "    except Exception as exc:\n"
        "        rows.append([script_name, 'missing', type(exc).__name__ + ': ' + str(exc)])\n"
        "print(json.dumps(rows))\n"
    )
    code, out, err = _probe(python, source)
    if code != 0:
        detail = (err or out).strip().splitlines()
        return [("entry-point discovery", "missing", detail[-1] if detail else "probe failed")]
    try:
        payload = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [("entry-point discovery", "missing", "probe produced no parsable output")]
    return [(str(name), str(state), str(detail)) for name, state, detail in payload]


def check_isolated_venv() -> list[tuple[str, str, str]]:
    """Report on the isolated runtime venv that run_mcp_stdio.py defaults to.

    Read from disk only. Nothing is created or repaired here; an audit must not
    mutate what it measures.
    """

    runtime_home = Path(
        os.getenv("AIASK_RUNTIME_HOME") or (Path.home() / ".aiask")
    ).expanduser()
    venv = runtime_home / "venv"
    rows: list[tuple[str, str, str]] = []

    if not venv.is_dir():
        rows.append(("isolated venv directory", "missing", str(venv)))
        return rows

    interpreter = venv / "bin" / "python"
    if interpreter.exists():
        rows.append(("isolated venv interpreter", "ok", str(interpreter)))
    else:
        rows.append(
            (
                "isolated venv interpreter",
                "missing",
                f"{interpreter} does not exist; run_mcp_stdio.py fails closed here",
            )
        )

    config = venv / "pyvenv.cfg"
    if config.is_file():
        for line in config.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("home"):
                base = line.split("=", 1)[-1].strip()
                state = "ok" if Path(base).exists() else "stale"
                rows.append(("isolated venv base interpreter", state, base))

    for site_packages in sorted(venv.glob("lib/python*/site-packages")):
        for pth in sorted(site_packages.glob("_editable_impl_*.pth")):
            target = pth.read_text(encoding="utf-8", errors="replace").strip()
            state = "ok" if Path(target).is_dir() else "stale"
            rows.append((f"editable: {pth.name}", state, target))
    return rows


def check_aiask_interpreter() -> list[tuple[str, str, str]]:
    """Report which interpreter AIASK resolves to, and the rule that chose it.

    This is the section that decides whether ``run_mcp_stdio.py`` can launch a
    server at all. It comes first because the isolated venv below is only one of
    several accepted sources: a conda env is an equally valid answer, and
    reporting "venv missing" without naming the conda fallback made a working
    runtime look broken.
    """

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import _integration_paths
    finally:
        sys.path.pop(0)

    interpreter, source = _integration_paths.resolve_python_interpreter()
    state = "ok" if interpreter.exists() else "missing"
    rows: list[tuple[str, str, str]] = [
        ("resolved AIASK interpreter", state, f"{interpreter}  (source: {source})")
    ]

    env_name = _integration_paths.conda_env_name()
    conda_python = _integration_paths.conda_env_python()
    rows.append(
        (
            f"conda env '{env_name}'",
            "ok" if conda_python is not None else "missing",
            str(conda_python)
            if conda_python is not None
            else "no interpreter found; set AIASK_VENV_PYTHON or AIASK_CONDA_ENV",
        )
    )
    return rows


def check_databases() -> list[tuple[str, str, str]]:
    """Report every candidate runtime database, so a wrong path is visible."""

    import sqlite3

    runtime_home = Path(
        os.getenv("AIASK_RUNTIME_HOME") or (Path.home() / ".aiask")
    ).expanduser()
    candidates = [
        runtime_home / "data" / "db" / "akshare_mcp.sqlite3",  # canonical default
        runtime_home / "akshare_mcp.sqlite3",  # pre-runtime-layout leftover
        runtime_home / "strategy_factory_task_board.sqlite3",
    ]
    rows: list[tuple[str, str, str]] = []
    for path in candidates:
        if not path.is_file():
            rows.append((f"db: {path.name}", "missing", str(path)))
            continue
        size_mb = path.stat().st_size / (1024 * 1024)
        detail = f"{path} ({size_mb:.1f} MB)"
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
            try:
                rows_in = conn.execute(
                    "SELECT COUNT(*) FROM strategy_quality_reports"
                ).fetchone()[0]
                detail += f", strategy_quality_reports={rows_in}"
            except sqlite3.Error as _swallowed:
                logger.warning("scripts/check_local_install.py:267: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                pass
            finally:
                conn.close()
        except sqlite3.Error as exc:
            detail += f", unreadable: {exc}"
        rows.append((f"db: {path.name}", "ok", detail))
    return rows


def _render(title: str, rows: list[tuple[str, str, str]]) -> int:
    bad = sum(1 for _name, state, _detail in rows if state != "ok")
    suffix = f"  [{bad} not ok]" if bad else ""
    print(f"\n{title}{suffix}")
    print("-" * (len(title) + len(suffix)))
    if not rows:
        print("  (nothing to check)")
        return 0
    for name, state, detail in rows:
        marker = {"ok": "  ok     ", "stale": "  STALE  ", "missing": "  MISSING"}[state]
        print(f"{marker} {name:44s} {detail}")
    return bad


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter whose install layer is inspected (default: this one)",
    )
    args = parser.parse_args()
    python = Path(args.python).expanduser().resolve()

    print(f"checkout : {REPO_ROOT}")
    print(f"probe    : {python}")
    print("probes run outside the checkout with PYTHONPATH removed")

    # Blocking: describes the interpreter named by --python.
    blocking = 0
    blocking += _render("Packages", check_packages(python))
    blocking += _render("Console entry points", check_entry_points(python))

    # Informational: describes the deployment target, which may legitimately be
    # a different interpreter. Reported in full, but does not decide the exit
    # code, so "your conda install is fine" and "the runtime venv is broken"
    # stay distinguishable verdicts.
    _render("AIASK interpreter (informational)", check_aiask_interpreter())
    _render("Isolated runtime venv (informational)", check_isolated_venv())
    _render("Runtime databases (informational)", check_databases())

    print()
    if blocking:
        print(f"RESULT: {blocking} install-layer item(s) not ok for {python}.")
        print("Note: a green root `pytest -q` does not contradict this — pytest.ini")
        print("injects src paths and therefore bypasses the install layer entirely.")
        return 1
    print(f"RESULT: install layer intact for {python}.")
    print("The informational sections above are not part of this verdict;")
    print("read them separately before trusting the deployment path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
