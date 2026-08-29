from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SERVICES_ROOT = ROOT / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "services"

_ALLOWED_PREFIXES = (
    "strategy_factory.api",
    "strategy_factory.runtime",
)


def _iter_service_python_files():
    yield from SERVICES_ROOT.rglob("*.py")


def test_strategy_services_use_only_public_strategy_factory_modules() -> None:
    violations: list[str] = []
    for file_path in _iter_service_python_files():
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        if "strategy_factory" not in text:
            continue
        try:
            tree = ast.parse(text, filename=str(file_path))
        except SyntaxError:
            # ``services`` contains fragment files loaded into composite modules at runtime.
            # For those files we fall back to the import regex gate below.
            if "from strategy_factory import" in text or "import strategy_factory" in text:
                violations.append(f"{file_path.relative_to(ROOT)}: raw root-package import text")
            continue
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
