from __future__ import annotations

import ast
import re
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]

RUNTIME_PATHS = [
    ROOT / "packages/strategy-factory/src",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/services/incubation_factory",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/tools/managers/backtest_manager.py",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/tools/managers/_paper_trading_manager_support.py",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/tools/managers/quant_manager.py",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/tools/managers/quant_mgr_classic.py",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/tools/managers/quant_mgr_generation.py",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/tools/managers/quant_mgr_helpers.py",
    ROOT / "packages/akshare-mcp/src/akshare_mcp/tools/managers/quant_mgr_validation.py",
]

FORBIDDEN_PATTERNS = [
    re.compile(r"from\s+(?:\.+|akshare_mcp\.)data_source\s+import\b"),
    re.compile(r"\bdata_source\.get_[A-Za-z0-9_]+\b"),
    re.compile(r"^\s*import\s+akshare\b", re.MULTILINE),
    re.compile(r"^\s*import\s+tushare\b", re.MULTILINE),
    re.compile(r"^\s*import\s+efinance\b", re.MULTILINE),
    re.compile(r"\bget_realtime_quote\b"),
    re.compile(r"\bget_batch_quotes_compat\b"),
    re.compile(r"\bget_north_fund\s*\("),
    re.compile(r"\bget_sector_fund_flow\s*\("),
    re.compile(r"akshare_mcp\.tools\.market\.kline"),
    re.compile(r"akshare_mcp\.tools\.fund_flow"),
]


def _iter_python_files(path: Path):
    if not path.exists():
        return
    if path.is_file():
        if path.suffix == ".py":
            yield path
        return
    yield from path.rglob("*.py")


def test_factory_runtime_paths_are_db_only() -> None:
    violations: list[str] = []
    for root in RUNTIME_PATHS:
        for file_path in _iter_python_files(root):
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            for pattern in FORBIDDEN_PATTERNS:
                match = pattern.search(text)
                if match:
                    rel = file_path.relative_to(ROOT)
                    violations.append(f"{rel}: forbidden {pattern.pattern!r} at {match.start()}")
    assert not violations, "\n".join(violations)


def test_akshare_storage_does_not_import_strategy_application_layer() -> None:
    storage_root = ROOT / "packages/akshare-mcp/src/akshare_mcp/storage"
    violations: list[str] = []
    for file_path in _iter_python_files(storage_root):
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        if any(
            token in text
            for token in (
                "strategy_factory.application",
                "strategy_factory.domain",
                "strategy_factory.infrastructure",
            )
        ):
            violations.append(str(file_path.relative_to(ROOT)))

    assert violations == []


def test_akshare_uses_only_strategy_factory_public_api() -> None:
    akshare_root = ROOT / "packages/akshare-mcp/src/akshare_mcp"
    forbidden_tokens = (
        "strategy_factory.application",
        "strategy_factory.domain",
        "strategy_factory.infrastructure",
    )
    violations: list[str] = []
    for file_path in _iter_python_files(akshare_root):
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        for token in forbidden_tokens:
            if token in text:
                violations.append(f"{file_path.relative_to(ROOT)}: {token}")

    assert violations == []


def test_akshare_does_not_import_strategy_factory_private_api() -> None:
    akshare_root = ROOT / "packages/akshare-mcp/src/akshare_mcp"
    violations: list[str] = []
    for file_path in _iter_python_files(akshare_root):
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        if "strategy_factory" not in text:
            continue
        try:
            tree = ast.parse(text, filename=str(file_path))
        except SyntaxError:
            tree = ast.parse(textwrap.dedent(text), filename=str(file_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            if node.level or not module.startswith("strategy_factory"):
                continue
            for alias in node.names:
                if alias.name.startswith("_"):
                    violations.append(
                        f"{file_path.relative_to(ROOT)}: from {module} import {alias.name}"
                    )

    assert violations == []


def test_akshare_does_not_reference_strategy_factory_underscore_public_api() -> None:
    akshare_root = ROOT / "packages/akshare-mcp/src/akshare_mcp"
    forbidden_tokens = (
        "_run_validation_report",
        "_run_risk_report",
        "_apply_target_symbol_policy",
        "_normalize_research_task_contract",
        "_call_optional_async",
    )
    violations: list[str] = []
    for file_path in _iter_python_files(akshare_root):
        text = file_path.read_text(encoding="utf-8", errors="ignore")
        if "strategy_factory" not in text:
            continue
        for token in forbidden_tokens:
            if token in text:
                violations.append(f"{file_path.relative_to(ROOT)}: {token}")

    assert violations == []


def test_strategy_factory_scheduler_kwargs_contract(monkeypatch) -> None:
    from akshare_mcp.adapters import strategy_factory_runtime as bridge

    fake_kwargs = {"db_provider": object(), "runtime_adapters": object()}

    monkeypatch.setattr(
        "strategy_factory.runtime.default_bootstrap.build_default_scheduler_kwargs",
        lambda db=None: fake_kwargs,
    )

    kwargs = bridge.build_strategy_factory_scheduler_kwargs()

    assert kwargs is fake_kwargs


def test_repo_does_not_use_akshare_strategy_factory_compat_facade() -> None:
    compat_root = ROOT / "packages/akshare-mcp/src/akshare_mcp/services/strategy_factory"
    forbidden_token = ".".join(("akshare_mcp", "services", "strategy_factory"))
    violations: list[str] = []
    for root in (ROOT / "packages", ROOT / "scripts"):
        for file_path in _iter_python_files(root):
            try:
                file_path.relative_to(compat_root)
                continue
            except ValueError:
                pass
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            if forbidden_token in text:
                violations.append(str(file_path.relative_to(ROOT)))

    assert violations == []


def test_akshare_strategy_factory_compat_facade_has_been_removed() -> None:
    compat_root = ROOT / "packages/akshare-mcp/src/akshare_mcp/services/strategy_factory"
    assert not compat_root.exists()
