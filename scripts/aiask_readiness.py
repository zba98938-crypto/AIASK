#!/usr/bin/env python3
"""Stdlib-only readiness checks for the copied AIASK integration.

This script is intentionally safe to run from Hermes' current interpreter. It
does not import AIASK heavy packages, does not read secret values, and does not
touch broker software. Package availability is checked in a subprocess using
the configured AIASK venv Python.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from _integration_paths import default_sqlite_path, default_venv_python, runtime_home


MIN_PYTHON = (3, 12)
MAX_PYTHON = (3, 14)

REQUIRED_IMPORTS = {
    "aiask_quant_core": "packages/aiask-quant-core",
    "strategy_factory": "packages/strategy-factory",
    "akshare_mcp": "packages/akshare-mcp",
    "aiask_finance_mcp": "packages/finance-mcp-servers",
    "aiask_agent": "packages/agent",
}

SECRET_ENV_NAMES = [
    "TUSHARE_TOKEN",
    "TUSHARE_PRO_TOKEN",
    "AIASK_FINANCE_THS_BROKER_TOKEN",
    "AIASK_FINANCE_QMT_BROKER_TOKEN",
]

GRAPHIFY_FILES = [
    "CURATED_SUMMARY.json",
    "endpoint-map.json",
    "cross-package-edges.json",
]

OPTIONAL_MCP_MANIFESTS = [
    "aiask-akshare/manifest.yaml",
    "aiask-eastmoney/manifest.yaml",
    "aiask-qmt/manifest.yaml",
    "aiask-tongdaxin/manifest.yaml",
    "aiask-tonghuashun/manifest.yaml",
]

OPTIONAL_MCP_BRIDGES = [
    "aiask-akshare/run_aiask_mcp_stdio.py",
    "aiask-eastmoney/run_aiask_mcp_stdio.py",
    "aiask-qmt/run_aiask_mcp_stdio.py",
    "aiask-tongdaxin/run_aiask_mcp_stdio.py",
    "aiask-tonghuashun/run_aiask_mcp_stdio.py",
]

REQUIRED_DESKTOP_WORKBENCH_FILES = [
    "desktop/package.json",
    "desktop/src/App.tsx",
    "desktop/src/views.ts",
    "desktop/src/services/aiaskApi.ts",
    "desktop/src/mockApi.ts",
    "desktop/src/features/financial-manager/FinancialManagerWorkspace.tsx",
]


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _version_supported(info: tuple[int, int]) -> bool:
    return MIN_PYTHON <= info < MAX_PYTHON


def _version_label(info: tuple[int, int, int] | None) -> str:
    if not info:
        return "unknown"
    return ".".join(str(part) for part in info)


def _aiask_root() -> Path:
    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[1]


def _hermes_root(aiask_root: Path) -> Path:
    override = os.getenv("HERMES_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if aiask_root.parent.name == "integrations":
        return aiask_root.parent.parent.resolve()
    return aiask_root.parent.resolve()


def _default_venv_python(root: Path) -> Path:
    del root
    return default_venv_python()


def _venv_python_path(value: str | Path) -> Path:
    """Normalize a venv Python path without following symlinks.

    CPython venvs commonly point ``bin/python`` at the base interpreter via a
    symlink. ``Path.resolve()`` follows that link and lands on the base env,
    which does **not** contain the AIASK editable installs. Import probes and
    readiness gates must keep the venv entrypoint path intact so ``pyvenv.cfg``
    still activates the isolated site-packages.
    """

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    # Collapse ``.`` / ``..`` without resolving symlinks (os.path.abspath).
    return Path(os.path.abspath(str(path)))


def _path_is_inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _python_probe(executable: Path | str) -> dict[str, Any]:
    cmd = [
        str(executable),
        "-c",
        (
            "import json,sys;"
            "print(json.dumps({"
            "'executable':sys.executable,"
            "'version':[sys.version_info.major,sys.version_info.minor,sys.version_info.micro]"
            "}))"
        ),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {
            "ok": False,
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:500],
        }
    try:
        data = json.loads(proc.stdout or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"invalid probe output: {exc}"}
    version = tuple(int(part) for part in data.get("version") or [])
    return {
        "ok": True,
        "executable": data.get("executable"),
        "version": list(version),
        "supported": _version_supported(version[:2]) if len(version) >= 2 else False,
    }


def _import_probe(executable: Path | str, root: Path) -> dict[str, Any]:
    code = (
        "import importlib.util,json;"
        f"mods={list(REQUIRED_IMPORTS)!r};"
        "print(json.dumps({m: bool(importlib.util.find_spec(m)) for m in mods}))"
    )
    env = dict(os.environ)
    env.setdefault("AIASK_PROJECT_ROOT", str(root))
    env.setdefault("AIASK_RUNTIME_HOME", str(runtime_home()))
    env.setdefault("AIASK_SQLITE_PATH", str(default_sqlite_path()))
    env.setdefault("AKSHARE_MCP_SQLITE_PATH", env["AIASK_SQLITE_PATH"])
    try:
        proc = subprocess.run(
            [str(executable), "-c", code],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc), "modules": {}}
    if proc.returncode != 0:
        return {
            "ok": False,
            "returncode": proc.returncode,
            "stderr": (proc.stderr or "").strip()[:500],
            "modules": {},
        }
    try:
        modules = json.loads(proc.stdout or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"invalid import probe output: {exc}", "modules": {}}
    missing = [name for name, present in modules.items() if not present]
    return {"ok": not missing, "modules": modules, "missing": missing}


def _read_json_summary(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {
        "endpoint_count": data.get("endpoint_count"),
        "server_only_count": data.get("server_only_count"),
        "desktop_only_count": data.get("desktop_only_count"),
        "matched_count": data.get("matched_count"),
    }


def _desktop_workbench_status(root: Path) -> dict[str, Any]:
    files = {
        rel: {"path": str(root / rel), "exists": (root / rel).is_file()}
        for rel in REQUIRED_DESKTOP_WORKBENCH_FILES
    }
    missing = [rel for rel, item in files.items() if not item["exists"]]
    readme_note = ""
    readme_path = root / "README.md"
    try:
        readme_text = readme_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        readme_text = ""
    if "Frontend / desktop workbench code was intentionally not copied" in readme_text:
        readme_note = "README states frontend / desktop workbench code was intentionally not copied."
    return {
        "included": not missing,
        "root": str(root / "desktop"),
        "files": files,
        "missing": missing,
        "note": readme_note,
    }


def _check(checks: list[dict[str, Any]], name: str, status: str, severity: str, message: str, **extra: Any) -> None:
    item: dict[str, Any] = {
        "name": name,
        "status": status,
        "severity": severity,
        "message": message,
    }
    if extra:
        item.update(extra)
    checks.append(item)


def collect_readiness() -> dict[str, Any]:
    root = _aiask_root()
    hermes_root = _hermes_root(root)
    home = runtime_home()
    sqlite_path = default_sqlite_path()
    checks: list[dict[str, Any]] = []

    root_ok = (root / "pyproject.toml").is_file() and (root / "packages").is_dir()
    _check(
        checks,
        "aiask_root",
        "pass" if root_ok else "fail",
        "critical",
        "AIASK integration root detected" if root_ok else "AIASK integration root is missing pyproject.toml or packages/",
        path=str(root),
    )

    for rel in REQUIRED_IMPORTS.values():
        exists = (root / rel).is_dir()
        _check(
            checks,
            f"package_source:{rel}",
            "pass" if exists else "fail",
            "critical",
            f"{rel} exists" if exists else f"{rel} is missing",
            path=str(root / rel),
        )

    runtime_inside_source = _path_is_inside(home, root)
    sqlite_inside_source = _path_is_inside(sqlite_path, root)
    _check(
        checks,
        "runtime_home_external",
        "fail" if runtime_inside_source else "pass",
        "critical",
        "AIASK_RUNTIME_HOME is outside integrations/aiask" if not runtime_inside_source else "AIASK_RUNTIME_HOME points inside the source copy",
        path=str(home),
    )
    _check(
        checks,
        "sqlite_path_external",
        "fail" if sqlite_inside_source else "pass",
        "critical",
        "SQLite path is outside integrations/aiask" if not sqlite_inside_source else "SQLite path points inside the source copy",
        path=str(sqlite_path),
    )

    runtime_dirs = {
        name: str(home / name)
        for name in ("data/db", "logs", "tmp", "cache")
    }
    _check(
        checks,
        "runtime_dirs",
        "pass",
        "info",
        "Runtime directories are resolved under the external runtime home; launchers create them on demand",
        directories=runtime_dirs,
    )

    manifest_python = shutil.which("python")
    _check(
        checks,
        "manifest_python_command",
        "pass" if manifest_python else "fail",
        "critical",
        "`python` command is available for stdlib wrapper launch" if manifest_python else "`python` command is not on PATH; stdio manifests cannot start the wrapper",
        executable=manifest_python,
    )
    manifest_python_probe = _python_probe(manifest_python) if manifest_python else {"ok": False}

    configured_venv = os.getenv("AIASK_VENV_PYTHON")
    venv_python = (
        _venv_python_path(configured_venv) if configured_venv else _venv_python_path(_default_venv_python(root))
    )
    venv_exists = venv_python.exists()
    _check(
        checks,
        "aiask_venv_python_exists",
        "pass" if venv_exists else "fail",
        "critical",
        "AIASK venv Python exists" if venv_exists else "AIASK venv Python is missing; set AIASK_VENV_PYTHON or create the default venv",
        path=str(venv_python),
        configured=bool(configured_venv),
    )
    venv_probe = _python_probe(venv_python) if venv_exists else {"ok": False}
    venv_version = tuple(int(part) for part in (venv_probe.get("version") or [])[:3])
    venv_supported = bool(venv_probe.get("ok")) and _version_supported(venv_version[:2])
    _check(
        checks,
        "aiask_venv_python_version",
        "pass" if venv_supported else "fail",
        "critical",
        "AIASK venv Python is >=3.12,<3.14" if venv_supported else "AIASK venv Python must be >=3.12,<3.14",
        version=_version_label(venv_version if len(venv_version) == 3 else None),
        executable=venv_probe.get("executable"),
        probe_error=venv_probe.get("error") or venv_probe.get("stderr"),
    )

    allow_current = _truthy(os.getenv("AIASK_ALLOW_CURRENT_PYTHON"))
    current_version = sys.version_info[:3]
    current_supported = _version_supported(current_version[:2])
    _check(
        checks,
        "current_python_dev_fallback",
        "warn" if allow_current else "pass",
        "warning" if allow_current else "info",
        "AIASK_ALLOW_CURRENT_PYTHON is enabled for local development" if allow_current else "Current Python fallback is disabled",
        current_executable=sys.executable,
        current_version=_version_label(current_version),
        current_supported=current_supported,
    )

    import_target: str | Path | None = venv_python if venv_supported else None
    if import_target is None and allow_current and current_supported:
        import_target = sys.executable
    if import_target is None:
        import_probe = {"ok": False, "modules": {}, "missing": list(REQUIRED_IMPORTS)}
        _check(
            checks,
            "aiask_package_imports",
            "fail",
            "critical",
            "Package import probe skipped because no supported AIASK Python is available",
            missing=list(REQUIRED_IMPORTS),
        )
    else:
        import_probe = _import_probe(import_target, root)
        _check(
            checks,
            "aiask_package_imports",
            "pass" if import_probe.get("ok") else "fail",
            "critical",
            "Required AIASK packages are importable" if import_probe.get("ok") else "One or more AIASK packages are not importable in the selected Python",
            executable=str(import_target),
            modules=import_probe.get("modules", {}),
            missing=import_probe.get("missing", []),
            probe_error=import_probe.get("error") or import_probe.get("stderr"),
        )

    manifests: dict[str, Any] = {}
    optional_root = hermes_root / "optional-mcps"
    for rel in OPTIONAL_MCP_MANIFESTS:
        path = optional_root / rel
        manifests[rel] = {"path": str(path), "exists": path.is_file()}
    bridges: dict[str, Any] = {}
    for rel in OPTIONAL_MCP_BRIDGES:
        path = optional_root / rel
        bridges[rel] = {"path": str(path), "exists": path.is_file()}
    _check(
        checks,
        "optional_mcp_manifests",
        "pass"
        if all(item["exists"] for item in manifests.values()) and all(item["exists"] for item in bridges.values())
        else "fail",
        "critical",
        "AIASK optional MCP manifests and launch bridges are present"
        if all(item["exists"] for item in manifests.values()) and all(item["exists"] for item in bridges.values())
        else "One or more AIASK optional MCP manifests or launch bridges are missing",
        manifests=manifests,
        bridges=bridges,
    )

    graph_dir = root / "reports" / "code-graph" / "full-2026-05-29" / "curated"
    graph_files = {name: {"path": str(graph_dir / name), "exists": (graph_dir / name).is_file()} for name in GRAPHIFY_FILES}
    endpoint_summary = _read_json_summary(graph_dir / "endpoint-map.json") if (graph_dir / "endpoint-map.json").is_file() else {}
    _check(
        checks,
        "graphify_artifacts",
        "pass" if all(item["exists"] for item in graph_files.values()) else "warn",
        "warning",
        "Graphify curated artifacts are available" if all(item["exists"] for item in graph_files.values()) else "Graphify curated artifacts are incomplete",
        files=graph_files,
        endpoint_summary=endpoint_summary,
    )

    secrets = {name: {"configured": bool(os.getenv(name))} for name in SECRET_ENV_NAMES}
    _check(
        checks,
        "secret_env_presence",
        "pass",
        "info",
        "Secret environment variables are reported by presence only; values and token fragments are never emitted",
        variables=secrets,
    )

    desktop_status = _desktop_workbench_status(root)
    _check(
        checks,
        "desktop_workbench_source",
        "pass" if desktop_status["included"] else "warn",
        "info" if desktop_status["included"] else "warning",
        "AIASK Desktop workbench source is present"
        if desktop_status["included"]
        else "AIASK Desktop workbench source is not present in this integration copy; backend readiness does not prove product-level delivery",
        root=desktop_status["root"],
        missing=desktop_status["missing"],
        note=desktop_status["note"],
    )

    critical_failures = [item for item in checks if item["severity"] == "critical" and item["status"] == "fail"]
    warnings = [
        item
        for item in checks
        if item["status"] == "warn"
        or (item["severity"] == "warning" and item["status"] != "pass")
    ]
    product_blockers: list[dict[str, Any]] = []
    if not desktop_status["included"]:
        product_blockers.append(
            {
                "code": "desktop_workbench_missing",
                "message": "AIASK financial Desktop workbench source is absent from the current integration copy.",
                "missing": list(desktop_status["missing"]),
            }
        )
    backend_ready = not critical_failures
    product_ready = backend_ready and not product_blockers

    return {
        "schema_version": 1,
        "generated_at": int(time.time()),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": {
                "executable": sys.executable,
                "version": _version_label(sys.version_info[:3]),
            },
        },
        "paths": {
            "aiask_root": str(root),
            "hermes_root": str(hermes_root),
            "runtime_home": str(home),
            "sqlite_path": str(sqlite_path),
            "default_venv_python": str(_default_venv_python(root)),
        },
        "python": {
            "manifest_command": "python",
            "manifest_python": manifest_python_probe,
            "aiask_venv_python": str(venv_python),
            "aiask_venv_configured": bool(configured_venv),
            "aiask_venv_probe": venv_probe,
            "allow_current_python": allow_current,
        },
        "packages": {
            "required_imports": REQUIRED_IMPORTS,
            "import_probe": import_probe,
        },
        "optional_mcp": {
            "manifests": manifests,
            "bridges": bridges,
            "default_policy": (
                "curated read-mostly tools; raw available_tools is not enabled by default; "
                "live order/cancel tools require explicit live opt-in plus broker-token guard"
            ),
        },
        "graphify": {
            "curated_dir": str(graph_dir),
            "files": graph_files,
            "endpoint_summary": endpoint_summary,
        },
        "secrets": secrets,
        "product": {
            "backend_ready": backend_ready,
            "product_ready": product_ready,
            "desktop_workbench": desktop_status,
            "blockers": product_blockers,
        },
        "checks": checks,
        "summary": {
            "ready": backend_ready,
            "product_ready": product_ready,
            "critical_failures": len(critical_failures),
            "warnings": len(warnings),
            "product_blockers": len(product_blockers),
        },
    }


def print_human(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    print("AIASK readiness")
    print(f"  ready: {summary['ready']}")
    print(f"  product_ready: {summary.get('product_ready', False)}")
    print(f"  critical_failures: {summary['critical_failures']}")
    print(f"  warnings: {summary['warnings']}")
    print(f"  product_blockers: {summary.get('product_blockers', 0)}")
    print(f"  aiask_root: {payload['paths']['aiask_root']}")
    print(f"  runtime_home: {payload['paths']['runtime_home']}")
    print(f"  sqlite_path: {payload['paths']['sqlite_path']}")
    print(f"  aiask_venv_python: {payload['python']['aiask_venv_python']}")
    print()
    for item in payload["checks"]:
        marker = item["status"].upper()
        print(f"[{marker}] {item['name']}: {item['message']}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check AIASK integration readiness without importing heavy packages.")
    parser.add_argument("--root", default="", help="AIASK integration root; defaults to AIASK_PROJECT_ROOT or integrations/aiask")
    parser.add_argument("--runtime-home", default="", help="External AIASK runtime home override")
    parser.add_argument("--venv-python", default="", help="AIASK isolated venv Python override")
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero when readiness has critical failures")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.root:
        os.environ["AIASK_PROJECT_ROOT"] = str(Path(args.root).expanduser().resolve())
    if args.runtime_home:
        os.environ["AIASK_RUNTIME_HOME"] = str(Path(args.runtime_home).expanduser().resolve())
    if args.venv_python:
        # Do not Path.resolve(): that follows the venv python symlink into the
        # base interpreter and breaks isolated package import probes.
        os.environ["AIASK_VENV_PYTHON"] = str(_venv_python_path(args.venv_python))

    payload = collect_readiness()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_human(payload)
    if args.strict and not payload["summary"]["ready"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
