#!/usr/bin/env python3
"""Stdlib-only bootstrap for the isolated AIASK Python venv.

This script deliberately mirrors the safety rules of ``aiask_readiness.py``:
- No AIASK heavy packages are imported at any point.
- No secret env values are read or emitted.
- No broker software is touched.
- All work happens in subprocesses so the caller's interpreter is unaffected.

Execution order (fail-closed on the first failure):

1. Resolve a base Python interpreter that satisfies ``>=3.12,<3.14``. The
   caller can pin ``--python``; otherwise the launcher chain is tried
   (``py -3.12`` on Windows, ``python3.12`` on POSIX, then the currently
   running interpreter only if it already satisfies the version gate).
2. Create the venv at the resolved target path if it does not exist.
3. Install the AIASK packages in the order pinned by the repository root
   ``pyproject.toml`` ``[tool.hermes-aiask].package_order``.

   The install is a two-pass sequence:

    - Pass 1 uses ``pip install --no-deps -e <package>`` so the in-tree
      editable relationships between the packages are established first
     without letting PyPI-published versions shadow them.
   - Pass 2 re-runs ``pip install -e <package>`` (without ``--no-deps``) so
     each package pulls the remaining third-party wheels it needs.

4. Optionally call ``aiask_readiness.py --json`` and append the payload to
   the response so ``hermes aiask bootstrap --execute`` can report the
   post-bootstrap readiness in a single machine-readable envelope.

By default, the exit code reflects whether venv creation and package
installation succeeded. With ``--strict``, readiness must also report
``summary.ready is True``; ``--skip-readiness`` explicitly omits that check
and ``--dry-run`` never performs installation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
import logging

logger = logging.getLogger(__name__)

try:
    from _integration_paths import default_venv_dir
except ModuleNotFoundError:  # direct file loading in installers/tests
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _integration_paths import default_venv_dir


MIN_PYTHON = (3, 12)
MAX_PYTHON = (3, 14)

SCHEMA_VERSION = 1


def _aiask_root(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    # scripts/aiask_venv_bootstrap.py -> <aiask root>
    return Path(__file__).resolve().parents[1]


def _default_venv_dir() -> Path:
    return default_venv_dir()


def _venv_python(venv_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _read_package_order(aiask_root: Path) -> list[str]:
    """Read the pinned package install order without needing tomllib below 3.11."""

    try:
        import tomllib  # type: ignore[import-not-found]

        payload = tomllib.loads((aiask_root / "pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        # Fallback: hard-coded canonical order matching pyproject.toml.
        return [
            "packages/aiask-quant-core",
            "packages/strategy-factory",
            "packages/akshare-mcp",
            "packages/data-mcp",
            "packages/finance-mcp-servers",
            "packages/aiask-skills",
            "packages/reference-host",
            "packages/agent",
        ]

    order = (
        payload.get("tool", {})
        .get("hermes-aiask", {})
        .get("package_order")
    )
    if isinstance(order, list) and order and all(isinstance(entry, str) for entry in order):
        return list(order)
    return [
        "packages/aiask-quant-core",
        "packages/strategy-factory",
        "packages/akshare-mcp",
        "packages/data-mcp",
        "packages/finance-mcp-servers",
        "packages/aiask-skills",
        "packages/reference-host",
        "packages/agent",
    ]


def _version_supported(info: tuple[int, int]) -> bool:
    return MIN_PYTHON <= info < MAX_PYTHON


def _probe_python_version(executable: str | Path) -> tuple[int, int, int] | None:
    """Return ``(major, minor, micro)`` for ``executable`` or ``None`` on failure."""

    try:
        proc = subprocess.run(
            [
                str(executable),
                "-c",
                "import sys;print('%d %d %d' % sys.version_info[:3])",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except Exception as _swallowed:
        logger.warning("scripts/aiask_venv_bootstrap.py:140: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None
    if proc.returncode != 0:
        return None
    parts = (proc.stdout or "").strip().split()
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError as _swallowed:
        logger.warning("scripts/aiask_venv_bootstrap.py:149: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None


def _candidate_launchers() -> list[list[str]]:
    """Ordered list of subprocess launchers that may resolve a 3.12/3.13 base."""

    launchers: list[list[str]] = []
    if os.name == "nt":
        # `py` launcher is the canonical way to reach a specific CPython on Windows.
        if shutil.which("py"):
            launchers.append(["py", "-3.13"])
            launchers.append(["py", "-3.12"])
        for exe in ("python3.13", "python3.12", "python"):
            if shutil.which(exe):
                launchers.append([exe])
    else:
        for exe in ("python3.13", "python3.12", "python3", "python"):
            if shutil.which(exe):
                launchers.append([exe])
    return launchers


def _resolve_base_python(explicit: str | None) -> dict[str, Any]:
    """Return either ``{"launcher": [...], "version": (...)}`` or ``{"error": ...}``."""

    if explicit:
        version = _probe_python_version(explicit)
        if version is None:
            return {
                "error": f"Could not probe --python {explicit!r} (not runnable).",
            }
        if not _version_supported(version[:2]):
            return {
                "error": (
                    f"--python {explicit!r} reports version "
                    f"{'.'.join(str(part) for part in version)}, but AIASK requires "
                    f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} <= python < {MAX_PYTHON[0]}.{MAX_PYTHON[1]}."
                ),
            }
        return {"launcher": [str(explicit)], "version": version}

    for launcher in _candidate_launchers():
        version = _probe_python_version(launcher[0]) if len(launcher) == 1 else None
        if version is None:
            # ``py -3.12`` style — probe with the full launcher.
            try:
                proc = subprocess.run(
                    launcher + [
                        "-c",
                        "import sys;print('%d %d %d' % sys.version_info[:3])",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except Exception as _swallowed:
                logger.warning("scripts/aiask_venv_bootstrap.py:205: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue
            if proc.returncode != 0:
                continue
            parts = (proc.stdout or "").strip().split()
            if len(parts) != 3:
                continue
            try:
                version = (int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError as _swallowed:
                logger.warning("scripts/aiask_venv_bootstrap.py:214: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue
        if _version_supported(version[:2]):
            return {"launcher": launcher, "version": version}

    return {
        "error": (
            "No Python 3.12 or 3.13 interpreter was found on PATH. "
            "Install Python 3.12 or 3.13 and re-run, or pass --python <path>."
        ),
    }


def _resolve_existing_venv_python(venv_python: Path) -> dict[str, Any] | None:
    """Use an already-created target venv when its Python satisfies the gate."""

    if not venv_python.exists():
        return None
    version = _probe_python_version(venv_python)
    if version is None:
        return {
            "error": f"Existing AIASK venv Python could not be probed: {venv_python}",
        }
    if not _version_supported(version[:2]):
        return {
            "error": (
                f"Existing AIASK venv Python at {venv_python} reports version "
                f"{'.'.join(str(part) for part in version)}, but AIASK requires "
                f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} <= python < {MAX_PYTHON[0]}.{MAX_PYTHON[1]}."
            ),
        }
    return {
        "launcher": [str(venv_python)],
        "version": version,
        "source": "existing_venv",
    }


def _run_step(cmd: list[str], *, timeout: int = 1800) -> dict[str, Any]:
    """Run a subprocess step and capture a structured result."""

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": cmd,
            "returncode": None,
            "ok": False,
            "error": f"Timed out after {timeout}s",
            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
        }
    except Exception as exc:
        return {
            "command": cmd,
            "returncode": None,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "stdout": "",
            "stderr": "",
        }
    return {
        "command": cmd,
        "returncode": proc.returncode,
        "ok": proc.returncode == 0,
        "stdout": (proc.stdout or "")[-8000:],
        "stderr": (proc.stderr or "")[-8000:],
    }


def _venv_state(venv_dir: Path, venv_python: Path) -> str:
    """Describe the target venv as ``absent``, ``incomplete`` or ``present``.

    ``incomplete`` is the state that used to be invisible: the directory exists
    so a directory check reports a usable venv, while the interpreter inside it
    is gone. Observed on this machine at ``~/.aiask/venv``, where ``bin/`` held
    console scripts and activate scripts but no ``python`` at all.
    """

    if not venv_dir.exists():
        return "absent"
    if not venv_python.exists():
        return "incomplete"
    return "present"


def _plan_commands(
    *,
    aiask_root: Path,
    venv_dir: Path,
    venv_python: Path,
    base_launcher: list[str],
    packages: list[str],
) -> list[list[str]]:
    """Return the ordered command list this bootstrap will execute."""

    commands: list[list[str]] = []
    # Decide on the interpreter, not the directory. A half-created venv
    # (directory present, interpreter missing) previously skipped creation and
    # went straight to `[missing_python, "-m", "pip", ...]` — a sequence
    # guaranteed to fail, for a reason the resulting report never named.
    # `python -m venv` over an existing directory repairs the missing
    # interpreter, and the `pip install -e` steps below rewrite the stale
    # editable `.pth` files that point at vanished checkouts.
    if not venv_python.exists():
        commands.append(list(base_launcher) + ["-m", "venv", str(venv_dir)])

    commands.append([str(venv_python), "-m", "pip", "install", "--upgrade", "pip"])

    for rel in packages:
        commands.append(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "-e",
                str(aiask_root / rel),
            ]
        )
    for rel in packages:
        commands.append(
            [
                str(venv_python),
                "-m",
                "pip",
                "install",
                "-e",
                str(aiask_root / rel),
            ]
        )
    return commands


def _run_readiness(
    *,
    aiask_root: Path,
    venv_python: Path,
    strict: bool,
) -> dict[str, Any]:
    script = aiask_root / "scripts" / "aiask_readiness.py"
    if not script.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "summary": {"ready": False},
            "checks": [
                {
                    "name": "readiness_script_missing",
                    "status": "fail",
                    "severity": "critical",
                    "message": f"aiask_readiness.py not found at {script}",
                }
            ],
        }
    cmd = [
        sys.executable,
        str(script),
        "--root",
        str(aiask_root),
        "--venv-python",
        str(venv_python),
        "--json",
    ]
    if strict:
        cmd.append("--strict")
    env = dict(os.environ)
    env.setdefault("AIASK_PROJECT_ROOT", str(aiask_root))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    except Exception as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "summary": {"ready": False},
            "checks": [
                {
                    "name": "readiness_invocation_failed",
                    "status": "fail",
                    "severity": "critical",
                    "message": f"{type(exc).__name__}: {exc}",
                }
            ],
        }
    try:
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "summary": {"ready": False},
            "checks": [],
        }
    if proc.returncode != 0:
        payload.setdefault("summary", {})["ready"] = False
        payload.setdefault("checks", []).append(
            {
                "name": "readiness_process",
                "status": "fail",
                "severity": "critical",
                "message": "AIASK readiness command exited non-zero",
                "returncode": proc.returncode,
                "stderr": (proc.stderr or "").strip()[:1000],
            }
        )
    return payload


def bootstrap(
    *,
    aiask_root: Path,
    venv_dir: Path | None = None,
    base_python: str | None = None,
    dry_run: bool = False,
    strict: bool = False,
    skip_readiness: bool = False,
) -> dict[str, Any]:
    """Run (or plan) the isolated AIASK venv bootstrap and return a report."""

    if not (aiask_root / "pyproject.toml").is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": f"AIASK integration root looks invalid: {aiask_root}",
            "aiask_root": str(aiask_root),
        }

    target_venv = (venv_dir or _default_venv_dir()).resolve()
    venv_python = _venv_python(target_venv)
    packages = _read_package_order(aiask_root)

    venv_state = _venv_state(target_venv, venv_python)
    existing_venv = _resolve_existing_venv_python(venv_python)
    resolution = existing_venv or _resolve_base_python(base_python)
    if "error" in resolution:
        if not dry_run:
            return {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "aiask_root": str(aiask_root),
                "venv_dir": str(target_venv),
                "packages": packages,
                "error": resolution["error"],
            }
        # Dry-run should still show the command shape even without a base
        # interpreter installed. Use a placeholder launcher so the report
        # remains machine-readable for CI contract tests.
        base_launcher = ["<python3.12>"]
        base_version: tuple[int, int, int] = (3, 12, 0)
        base_python_missing: str | None = resolution["error"]
        base_python_source = "placeholder"
    else:
        base_launcher = resolution["launcher"]
        base_version = resolution["version"]
        base_python_missing = None
        base_python_source = str(resolution.get("source") or "resolved_base")

    commands = _plan_commands(
        aiask_root=aiask_root,
        venv_dir=target_venv,
        venv_python=venv_python,
        base_launcher=base_launcher,
        packages=packages,
    )

    if dry_run:
        report = {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "dry_run": True,
            "aiask_root": str(aiask_root),
            "venv_dir": str(target_venv),
            "venv_python": str(venv_python),
            "venv_state": venv_state,
            "base_python_version": ".".join(str(part) for part in base_version),
            "base_python_launcher": base_launcher,
            "base_python_source": base_python_source,
            "packages": packages,
            "planned_commands": commands,
        }
        if base_python_missing is not None:
            report["base_python_missing"] = base_python_missing
        return report

    executed: list[dict[str, Any]] = []
    ok = True
    for cmd in commands:
        result = _run_step(cmd)
        executed.append(result)
        if not result["ok"]:
            ok = False
            break

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": ok,
        "dry_run": False,
        "aiask_root": str(aiask_root),
        "venv_dir": str(target_venv),
        "venv_python": str(venv_python),
        "venv_state": venv_state,
        "base_python_version": ".".join(str(part) for part in base_version),
        "base_python_launcher": base_launcher,
        "base_python_source": base_python_source,
        "packages": packages,
        "steps": executed,
    }

    if not ok or skip_readiness or not venv_python.exists():
        return payload

    payload["readiness"] = _run_readiness(
        aiask_root=aiask_root,
        venv_python=venv_python,
        strict=strict,
    )
    if strict and not payload["readiness"].get("summary", {}).get("ready"):
        payload["ok"] = False
    return payload


def _print_human(payload: dict[str, Any]) -> None:
    print(f"AIASK venv bootstrap  (aiask_root={payload.get('aiask_root')})")
    print(f"  venv_dir:      {payload.get('venv_dir')}")
    print(f"  venv_python:   {payload.get('venv_python')}")
    print(f"  base_python:   {payload.get('base_python_version')}")
    print(f"  packages:      {payload.get('packages')}")
    if payload.get("dry_run"):
        print("Planned commands:")
        for cmd in payload.get("planned_commands", []):
            print("  " + " ".join(cmd))
        return
    for step in payload.get("steps", []):
        marker = "OK" if step.get("ok") else "FAIL"
        print(f"  [{marker}] {' '.join(step.get('command', []))}")
        if not step.get("ok"):
            err = (step.get("stderr") or step.get("error") or "").strip()
            if err:
                print(f"        {err[:400]}")
    readiness = payload.get("readiness")
    if readiness:
        summary = readiness.get("summary") or {}
        print(f"Readiness ready={bool(summary.get('ready'))} "
              f"critical={summary.get('critical_failures', 0)} "
              f"warnings={summary.get('warnings', 0)}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the isolated AIASK Python 3.12+ venv and install editable packages.",
    )
    parser.add_argument("--root", default="", help="AIASK integration root")
    parser.add_argument("--venv", default="", help="Target venv directory")
    parser.add_argument("--python", default="", help="Base Python 3.12/3.13 interpreter path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the planned command sequence, do not execute",
    )
    parser.add_argument("--strict", action="store_true", help="Fail if post-bootstrap readiness is not ready")
    parser.add_argument(
        "--skip-readiness",
        action="store_true",
        help="Do not call aiask_readiness.py after bootstrap",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON on stdout")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    aiask_root = _aiask_root(args.root or None)
    venv_dir = Path(args.venv).expanduser().resolve() if args.venv else None
    payload = bootstrap(
        aiask_root=aiask_root,
        venv_dir=venv_dir,
        base_python=(args.python or None),
        dry_run=bool(args.dry_run),
        strict=bool(args.strict),
        skip_readiness=bool(args.skip_readiness),
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
