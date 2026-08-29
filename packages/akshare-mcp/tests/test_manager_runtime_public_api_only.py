from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
MANAGER_ROOT = ROOT / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "tools" / "managers"

_ALLOWED_PREFIXES = (
    "strategy_factory.api",
    "strategy_factory.runtime",
)


def _iter_manager_python_files():
    yield from MANAGER_ROOT.rglob("*.py")


def test_strategy_managers_use_only_public_strategy_factory_modules() -> None:
    violations: list[str] = []
    for file_path in _iter_manager_python_files():
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        if "strategy_factory" not in text:
            continue
        tree = ast.parse(text, filename=str(file_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    module = str(alias.name or "")
                    if module == "strategy_factory":
                        violations.append(
                            f"{file_path.relative_to(ROOT)}: import {module}"
                        )
            elif isinstance(node, ast.ImportFrom):
                module = str(node.module or "")
                if node.level or not module.startswith("strategy_factory"):
                    continue
                if module == "strategy_factory":
                    violations.append(
                        f"{file_path.relative_to(ROOT)}: from {module} import ..."
                    )
                    continue
                if not module.startswith(_ALLOWED_PREFIXES):
                    violations.append(
                        f"{file_path.relative_to(ROOT)}: from {module} import ..."
                    )

    assert violations == []


def test_lifecycle_support_uses_canonical_runtime_kwargs_after_bootstrap() -> None:
    path = MANAGER_ROOT / "strategy_mgr_lifecycle" / "_lifecycle_support.py"
    text = path.read_text(encoding="utf-8", errors="ignore")

    assert "from strategy_factory.api import get_strategy_factory_scheduler" in text
    assert "from strategy_factory.api.runtime import build_scheduler_runtime_kwargs" in text
    assert "strategy_factory.runtime.default_bootstrap" in text
    assert "build_local_strategy_factory_scheduler_kwargs" not in text
