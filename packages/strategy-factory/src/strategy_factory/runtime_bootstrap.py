"""Lightweight runtime bootstrap for factory runner scripts.

This module must stay stdlib-only so runner scripts can import it before
runtime dependencies are available in the current interpreter.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterable, Sequence

DEFAULT_RUNTIME_MODULES: tuple[str, ...] = ("numpy", "pandas", "httpx", "anyio")
DEFAULT_EDITABLE_PACKAGES: tuple[str, ...] = (
    "packages/strategy-factory",
    "packages/aiask-quant-core",
)
DEFAULT_RUNTIME_DISTRIBUTIONS: tuple[str, ...] = (
    "strategy-factory",
    "aiask-quant-core",
)

BOOTSTRAP_ENV_KEY = "AIASK_FACTORY_RUNTIME_BOOTSTRAPPED"
BOOTSTRAP_PYTHON_ENV_KEY = "AIASK_FACTORY_RUNTIME_PYTHON"
BOOTSTRAP_UV_ENV_KEY = "AIASK_FACTORY_UV_BIN"
BOOTSTRAP_PROJECT_ENV_KEY = "AIASK_FACTORY_RUNTIME_UV_PROJECT"


def _is_direct_script_invocation(script_path: Path, argv: Sequence[str] | None = None) -> bool:
    args = list(argv or sys.argv[1:])
    if not sys.argv:
        return False
    current = Path(sys.argv[0]).resolve()
    target = Path(script_path).resolve()
    if current == target:
        return True
    if current.name.lower().startswith("pytest"):
        return False
    if args:
        first = str(args[0] or "").strip()
        if first.endswith(".py"):
            try:
                return Path(first).resolve() == target
            except Exception:
                return False
    return False


def missing_runtime_modules(module_names: Iterable[str] = DEFAULT_RUNTIME_MODULES) -> list[str]:
    missing: list[str] = []
    for raw_name in module_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        if importlib.util.find_spec(name) is None:
            missing.append(name)
    return missing


def missing_runtime_distributions(
    distribution_names: Iterable[str] = DEFAULT_RUNTIME_DISTRIBUTIONS,
) -> list[str]:
    missing: list[str] = []
    for raw_name in distribution_names:
        name = str(raw_name or "").strip()
        if not name:
            continue
        try:
            importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    return missing


def build_uv_reexec_command(
    project_root: Path,
    script_path: Path,
    argv: Sequence[str] | None = None,
    *,
    python_version: str | None = None,
    editable_packages: Sequence[str] = DEFAULT_EDITABLE_PACKAGES,
    uv_project: str | Path | None = None,
) -> list[str]:
    resolved_project_root = Path(project_root).resolve()
    version = str(
        python_version
        or os.getenv(BOOTSTRAP_PYTHON_ENV_KEY)
        or "3.12"
    ).strip() or "3.12"
    uv_bin = str(
        os.getenv(BOOTSTRAP_UV_ENV_KEY)
        or shutil.which("uv")
        or "uv"
    ).strip() or "uv"
    command = [uv_bin, "run", "--python", version]
    project_value = str(
        uv_project
        or os.getenv(BOOTSTRAP_PROJECT_ENV_KEY)
        or ""
    ).strip()
    if project_value:
        project_path = Path(project_value)
        if not project_path.is_absolute():
            project_path = resolved_project_root / project_path
        command.extend(["--project", str(project_path.resolve())])
    for raw_package in editable_packages:
        package = str(raw_package or "").strip()
        if package:
            command.extend(["--with-editable", package])
    command.extend(["python", str(Path(script_path).resolve())])
    command.extend(str(arg) for arg in list(argv or ()))
    return command


def ensure_factory_runtime(
    project_root: Path,
    script_path: Path,
    argv: Sequence[str] | None = None,
    *,
    module_names: Sequence[str] = DEFAULT_RUNTIME_MODULES,
    editable_packages: Sequence[str] = DEFAULT_EDITABLE_PACKAGES,
    distribution_names: Sequence[str] = DEFAULT_RUNTIME_DISTRIBUTIONS,
    uv_project: str | Path | None = None,
) -> None:
    if str(os.getenv(BOOTSTRAP_ENV_KEY) or "").strip() == "1":
        return
    if not _is_direct_script_invocation(script_path, argv):
        return
    missing_modules = missing_runtime_modules(module_names)
    missing_distributions_list = missing_runtime_distributions(distribution_names)
    if not missing_modules and not missing_distributions_list:
        return
    uv_bin = shutil.which(str(os.getenv(BOOTSTRAP_UV_ENV_KEY) or "uv"))
    if not uv_bin:
        missing_tokens = list(dict.fromkeys([*missing_modules, *missing_distributions_list]))
        sys.stderr.write(
            "AIASK factory runtime bootstrap: missing runtime requirements "
            f"{', '.join(missing_tokens)} and 'uv' was not found; "
            "continuing with the current interpreter may fail.\n"
        )
        return
    command = build_uv_reexec_command(
        Path(project_root).resolve(),
        Path(script_path).resolve(),
        argv=argv,
        editable_packages=editable_packages,
        uv_project=uv_project,
    )
    env = dict(os.environ)
    env[BOOTSTRAP_ENV_KEY] = "1"
    completed = subprocess.run(
        command,
        cwd=str(Path(project_root).resolve()),
        env=env,
        check=False,
    )
    raise SystemExit(int(completed.returncode))
