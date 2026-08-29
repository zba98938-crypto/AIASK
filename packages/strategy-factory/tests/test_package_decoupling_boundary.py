"""Decoupling boundary tests for the strategy-factory package.

Repository decision (recorded in docs/event-driven/事件驱动主题联动-结合当前代码升级方案-2026-05-24.md §8):
the public surface of ``strategy-factory`` must depend only on the
shared ``aiask-quant-core`` library plus stdlib / numpy / pandas. No
module under ``packages/strategy-factory/src`` may import from
``akshare_mcp`` or any other host package.

Cross-package capabilities (factor mining factory, factor pool gateway,
incubation runtime, quant manager callable, …) are wired in via
``strategy_factory.infrastructure.runtime_services.configure_runtime_services``
and consumed through ``_required("name", call=True)``. The host process
(``akshare-mcp/server.py`` or test fixtures) is responsible for the
registration.

If a future PR violates this boundary the strategy-factory package
loses the ability to be deployed independently. These tests are the
last line of defence before review.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "strategy_factory"
TESTS_ROOT = Path(__file__).resolve().parents[1] / "tests"


def _python_files() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def _test_python_files() -> list[Path]:
    """tests 目录中所有可执行测试文件。

    本守门测试自己会把 ``akshare_mcp`` 写在守门字符串/注释里，因此被显式
    排除。除此以外，strategy-factory 的所有测试都不应该 import akshare_mcp
    （那种测试要么搬到 akshare-mcp/tests，要么用 mock）。
    """

    self_path = Path(__file__).resolve()
    return sorted(
        path for path in TESTS_ROOT.rglob("test_*.py") if path.resolve() != self_path
    )


# --------------------------------------------------------------------- #
# Forbidden modules. ``akshare_mcp`` is the obvious one; the path
# fragments cover both ``from akshare_mcp...`` imports and any direct
# package-relative import that would resolve to it.
# --------------------------------------------------------------------- #
_FORBIDDEN_IMPORT_PATTERNS = (
    re.compile(r"^\s*from\s+akshare_mcp(\.|\s+import)", re.MULTILINE),
    re.compile(r"^\s*import\s+akshare_mcp(\s|\.|$)", re.MULTILINE),
    re.compile(r"^\s*from\s+packages\.akshare_mcp", re.MULTILINE),
)


def test_strategy_factory_src_does_not_import_akshare_mcp() -> None:
    """No source file under ``packages/strategy-factory/src`` may import akshare_mcp."""

    offenders: list[tuple[Path, str]] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _FORBIDDEN_IMPORT_PATTERNS:
            for match in pattern.finditer(text):
                offenders.append((path.relative_to(SRC_ROOT), match.group(0).strip()))

    assert not offenders, (
        "strategy-factory must not import from akshare_mcp. "
        "Use configure_runtime_services(...) to inject host-provided runtime. "
        f"violations: {offenders[:10]}"
    )


def test_strategy_factory_pyproject_does_not_depend_on_akshare_mcp() -> None:
    """``strategy-factory`` 必须只在 aiask-quant-core/numpy/pandas 上声明依赖."""

    pyproject = SRC_ROOT.parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")

    # pyproject.toml is small — string check is enough.
    deps_block_match = re.search(r"dependencies\s*=\s*\[(.*?)\]", text, re.DOTALL)
    assert deps_block_match, "dependencies block missing in pyproject.toml"
    deps_block = deps_block_match.group(1).lower()

    assert "akshare-mcp" not in deps_block, (
        "strategy-factory/pyproject.toml must not declare akshare-mcp as a dependency"
    )
    assert "akshare_mcp" not in deps_block, (
        "strategy-factory/pyproject.toml must not declare akshare_mcp as a dependency"
    )


def test_runtime_services_registry_uses_lazy_required_lookup() -> None:
    """``runtime_services._required`` must be the single resolution path for host providers.

    This catches accidental ``from akshare_mcp.services... import factory`` style
    refactors that bypass the configure_runtime_services boundary.
    """

    services_path = SRC_ROOT / "infrastructure" / "runtime_services.py"
    text = services_path.read_text(encoding="utf-8")

    assert "configure_runtime_services" in text
    assert "_required(" in text
    # Anti-pattern: importing factor_mining or incubation runtime statically.
    assert "from akshare_mcp" not in text, (
        "runtime_services.py must remain free of host-side imports; "
        "all host capabilities flow through configure_runtime_services()."
    )


def test_execution_universe_contract_imports_without_akshare() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(ROOT / "packages" / "strategy-factory" / "src"),
        ]
    )
    script = r'''
import builtins
real_import = builtins.__import__

def guard(name, globals=None, locals=None, fromlist=(), level=0):
    if str(name).startswith("akshare_mcp"):
        raise ModuleNotFoundError("blocked " + str(name), name=name)
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guard
from strategy_factory.contracts.execution_universe import (
    ExecutableStrategy,
    ExecutionUniverseContract,
    ExecutionUniverseQuery,
    ExecutionUniverseStrategy,
)

query = ExecutionUniverseQuery(limit=3)
contract = ExecutionUniverseContract()
record = ExecutionUniverseStrategy(
    strategy_id="s1",
    strategy_name="demo",
    strategy_type="momentum",
    status="incubating",
    incubation_stage="warmup",
    incubation_status="active",
    account_id="acc1",
    created_at=None,
)
assert ExecutableStrategy is ExecutionUniverseStrategy
print(type(contract).__name__)
print(query.limit)
print(record.strategy_id)
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "ExecutionUniverseContract" in result.stdout


def test_event_driven_modules_avoid_host_imports() -> None:
    """事件驱动主题联动 §8: ThemeExposureBuilder / event_task_generator /
    theme_graph / theme_response_regression must only reach for shared
    libraries. They must never directly call into akshare_mcp."""

    offenders: list[tuple[Path, str]] = []
    target_dir = SRC_ROOT / "application" / "research"
    for path in target_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "akshare_mcp" in text or "tushare" in text or "baostock" in text:
            offenders.append((path.relative_to(SRC_ROOT), "host-or-external-source mention"))

    # Allow string mentions inside comments by re-checking against import patterns.
    real_violations = []
    for path_rel, _ in offenders:
        full = (SRC_ROOT / path_rel).read_text(encoding="utf-8", errors="ignore")
        for pattern in _FORBIDDEN_IMPORT_PATTERNS:
            if pattern.search(full):
                real_violations.append((str(path_rel), "akshare_mcp import"))
        if re.search(r"^\s*(from|import)\s+(tushare|baostock|efinance)", full, re.MULTILINE):
            real_violations.append((str(path_rel), "external data source import"))

    assert not real_violations, (
        "event-driven research modules must not import akshare_mcp / tushare / "
        f"baostock / efinance directly. violations: {real_violations}"
    )


def test_strategy_factory_tests_do_not_import_akshare_mcp() -> None:
    """``packages/strategy-factory/tests`` 不能 import akshare_mcp。

    历史上 ``test_llm_pipeline_diagnostics.py`` 错放在这里并 import
    ``akshare_mcp.services._strategy_generators_generate``，违反了
    "strategy-factory 不依赖 akshare-mcp"的解耦约束（被测对象其实归
    akshare-mcp，应该把测试搬过去）。本守门保证后续不再出现同类错放。
    本守门测试自己被显式排除（它把 ``akshare_mcp`` 当字符串写进规则）。
    """

    offenders: list[tuple[Path, str]] = []
    for path in _test_python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in _FORBIDDEN_IMPORT_PATTERNS:
            for match in pattern.finditer(text):
                offenders.append(
                    (path.relative_to(TESTS_ROOT), match.group(0).strip())
                )

    assert not offenders, (
        "strategy-factory tests must not import from akshare_mcp. "
        "Move the test under packages/akshare-mcp/tests/ instead. "
        f"violations: {offenders[:10]}"
    )
