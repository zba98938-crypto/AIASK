"""Enforce the package dependency DAG declared in runtime-boundary.json.

The extraction plan depends on one structural fact: the four AIASK packages form
an acyclic graph, and reverse dependencies reach strategy-factory only through
its public ``api``/``runtime`` surfaces. That fact is what makes the ownership
migration tractable — so it needs a guard, not just a paragraph in a document.

Layering is checked by AST so a dynamic import cannot slip past a regex.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest


PACKAGES_ROOT = Path(__file__).resolve().parents[2]
AIASK_ROOT = PACKAGES_ROOT.parent
BOUNDARY_JSON = AIASK_ROOT / "docs" / "architecture" / "runtime-boundary.json"

#: distribution directory -> import name
PACKAGE_MODULES: dict[str, str] = {
    "aiask-quant-core": "aiask_quant_core",
    "strategy-factory": "strategy_factory",
    "akshare-mcp": "akshare_mcp",
    "aiask-data-mcp": "aiask_data_mcp",
    "aiask-finance-mcp-servers": "aiask_finance_mcp",
    "aiask-skills": "aiask_skills",
    "aiask-agent-reference": "aiask_reference_host",
    "agent": "aiask_agent",
}

#: Layer index. A package may only import from a strictly lower layer.
LAYER: dict[str, int] = {
    "aiask_quant_core": 0,
    "strategy_factory": 1,
    "akshare_mcp": 2,
    "aiask_data_mcp": 3,
    "aiask_finance_mcp": 3,
    "aiask_skills": 0,
    "aiask_reference_host": 2,
    "aiask_agent": 4,
}

#: strategy-factory internals that must not be reached from outside the package.
#: ``contracts`` is included deliberately: ``akshare-mcp``'s own
#: ``test_services_runtime_public_api_only`` allows only ``api`` and ``runtime``,
#: and two guards disagreeing on what "public" means is worse than either rule.
#: Anything in ``contracts`` that outside packages need is re-exported through
#: ``strategy_factory.api.runtime``.
PRIVATE_SUBPACKAGES = ("application", "infrastructure", "domain", "contracts")

#: Documented reverse-dependency exceptions, by filename.
#:
#: ``default_bootstrap.py`` — the dynamic configurator hook, which now prefers a
#: host provider set but falls back to the in-package one.
_ALLOWED_REVERSE_DEPENDENCY_FILES = {"default_bootstrap.py"}


def _source_files(distribution: str) -> list[Path]:
    src = PACKAGES_ROOT / distribution / "src"
    if not src.is_dir():
        return []
    return [
        path
        for path in src.rglob("*.py")
        if ".venv" not in path.parts and "node_modules" not in path.parts
    ]


def _parse(path: Path) -> ast.Module | None:
    """Parse a source file, tolerating a UTF-8 BOM.

    ``utf-8-sig`` matters here: 27 files under ``aiask-quant-core/src`` carry a
    BOM, and reading them as plain utf-8 makes ``ast.parse`` raise
    ``SyntaxError: invalid non-printable character U+FEFF``. Swallowing that
    would silently exclude those files from every check in this module.
    """
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, SyntaxError):
        return None


def _imported_roots(path: Path) -> set[str]:
    """Top-level module names imported by ``path``, static and dynamic."""
    tree = _parse(path)
    if tree is None:
        return set()

    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 is a relative import: intra-package, never a layer break.
            if not node.level and node.module:
                roots.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name not in {"import_module", "__import__"}:
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    roots.add(arg.value.split(".")[0].split(":")[0])
    return roots


def _dotted_targets(path: Path) -> set[str]:
    """Full dotted module paths referenced, for private-subpackage checks."""
    tree = _parse(path)
    if tree is None:
        return set()

    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            targets.add(node.module)
        elif isinstance(node, ast.Call):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    targets.add(arg.value.replace(":", "."))
    return targets


# --- the DAG itself ------------------------------------------------------------


@pytest.mark.parametrize("distribution", sorted(PACKAGE_MODULES))
def test_package_never_imports_a_higher_layer(distribution: str) -> None:
    """No package may depend on one that sits above it in the DAG."""
    own_module = PACKAGE_MODULES[distribution]
    own_layer = LAYER[own_module]
    offenders: list[str] = []

    for path in _source_files(distribution):
        for root in _imported_roots(path):
            if root not in LAYER or root == own_module:
                continue
            if LAYER[root] < own_layer:
                continue
            if path.name in _ALLOWED_REVERSE_DEPENDENCY_FILES:
                continue
            offenders.append(f"{path.relative_to(PACKAGES_ROOT)} -> {root}")

    assert offenders == [], (
        f"{distribution} must not import a package at or above its own layer:\n"
        + "\n".join(sorted(offenders)[:20])
    )


def test_quant_core_is_a_leaf() -> None:
    """The bottom layer must depend on nothing in the AIASK graph.

    The factory-domain DDL moved to its owner, and the shim now resolves it
    through the schema registry, so quant-core has no reverse import left.
    """
    offenders: list[str] = []
    for path in _source_files("aiask-quant-core"):
        if path.name in _ALLOWED_REVERSE_DEPENDENCY_FILES:
            continue
        for root in _imported_roots(path):
            if root in LAYER and root != "aiask_quant_core":
                offenders.append(f"{path.relative_to(PACKAGES_ROOT)} -> {root}")
    assert offenders == [], "aiask-quant-core must be a leaf:\n" + "\n".join(offenders[:20])


def test_the_quant_core_shim_does_not_import_strategy_factory() -> None:
    """The shim must resolve the owner through the registry, not by import."""
    shim = PACKAGES_ROOT / "aiask-quant-core" / "src" / "aiask_quant_core"
    shim = shim / "storage" / "sqlite" / "schema_strategy.py"
    if not shim.is_file():
        pytest.skip("quant-core shim is absent")

    tree = _parse(shim)
    assert tree is not None, "the shim must be parsable"

    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert "strategy_factory" not in imported_roots, (
        "the shim must not import strategy_factory; quant-core is a leaf"
    )


def test_quant_core_remains_usable_without_strategy_factory() -> None:
    """quant-core must stand alone: it is the bottom of the DAG.

    A caller that only wants market data must not be forced to install the
    factory package.
    """
    import subprocess

    code = (
        "import importlib.abc, sys\n"
        "class B(importlib.abc.MetaPathFinder):\n"
        "    def find_spec(self, fullname, path=None, target=None):\n"
        "        if str(fullname).split('.')[0] == 'strategy_factory':\n"
        "            raise ModuleNotFoundError('blocked', name=fullname)\n"
        "        return None\n"
        "sys.meta_path.insert(0, B())\n"
        f"sys.path.insert(0, {str(PACKAGES_ROOT / 'aiask-quant-core' / 'src')!r})\n"
        "import aiask_quant_core.storage.sqlite.schema_strategy as shim\n"
        "print('OK', shim.schema_owner())\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, (
        "quant-core must import with strategy_factory absent:\n" + completed.stderr
    )
    assert "OK" in completed.stdout


def test_strategy_factory_reverse_dependency_is_confined_to_one_file() -> None:
    """The dynamic akshare hook is the only reverse dependency, and it is temporary.

    If this starts failing because the hook was removed, the standalone provider
    set has landed — tighten the test rather than re-adding the exception.
    """
    reaching: set[str] = set()
    for path in _source_files("strategy-factory"):
        if "akshare_mcp" in _imported_roots(path):
            reaching.add(path.name)

    assert reaching <= _ALLOWED_REVERSE_DEPENDENCY_FILES, (
        "unexpected strategy-factory -> akshare_mcp dependency in: "
        f"{sorted(reaching - _ALLOWED_REVERSE_DEPENDENCY_FILES)}"
    )


# --- public surface encapsulation ---------------------------------------------


@pytest.mark.parametrize("distribution", ["akshare-mcp", "agent"])
def test_outside_packages_use_only_the_public_surface(distribution: str) -> None:
    """Downstream packages must not reach into strategy-factory internals."""
    offenders: list[str] = []
    forbidden = tuple(f"strategy_factory.{name}" for name in PRIVATE_SUBPACKAGES)

    for path in _source_files(distribution):
        for target in _dotted_targets(path):
            if target.startswith(forbidden):
                offenders.append(f"{path.relative_to(PACKAGES_ROOT)} -> {target}")

    assert offenders == [], (
        f"{distribution} must reach strategy-factory only through .api/.runtime/.contracts:\n"
        + "\n".join(sorted(offenders)[:20])
    )


# --- declared contract stays in sync -----------------------------------------


def test_boundary_json_declares_the_same_dag() -> None:
    """The machine-readable contract must match what this test enforces."""
    declared = json.loads(BOUNDARY_JSON.read_text(encoding="utf-8"))
    dag = declared["package_dag"]

    assert dag["order"] == [
        "aiask-quant-core",
        "strategy-factory",
        "akshare-mcp",
        "aiask-data-mcp",
        "aiask-finance-mcp-servers",
        "aiask-skills",
        "aiask-agent-reference",
        "aiask-agent",
    ]

    # The JSON uses distribution names; map them to the import names LAYER keys on.
    # "aiask-agent" is the distribution; its source directory is "agent".
    to_module = {
        "aiask-quant-core": "aiask_quant_core",
        "strategy-factory": "strategy_factory",
        "akshare-mcp": "akshare_mcp",
        "aiask-data-mcp": "aiask_data_mcp",
        "aiask-finance-mcp-servers": "aiask_finance_mcp",
        "aiask-skills": "aiask_skills",
        "aiask-agent-reference": "aiask_reference_host",
        "aiask-agent": "aiask_agent",
    }
    assert set(dag["order"]) == set(to_module), "declared order must cover every package"

    for distribution, allowed in dag["allowed_edges"].items():
        own_layer = LAYER[to_module[distribution]]
        for dependency in allowed:
            assert LAYER[to_module[dependency]] < own_layer, (
                f"declared edge {distribution} -> {dependency} contradicts the layering"
            )


def test_boundary_json_pins_four_factories_and_sidecar() -> None:
    declared = json.loads(BOUNDARY_JSON.read_text(encoding="utf-8"))

    assert declared["four_factories"] == [
        "strategy_factory",
        "factor_mining",
        "incubation",
        "market_event_ingest",
    ]
    assert declared["sidecars"] == ["signal_tracker", "daily_market_sync"]
    assert "signal_tracker" not in declared["four_factories"]


@pytest.mark.parametrize("distribution", ["akshare-mcp", "agent"])
def test_no_textual_reference_to_private_subpackages(distribution: str) -> None:
    """Even a comment must not name an internal subpackage.

    ``akshare-mcp/tests/test_factory_db_only_boundary.py`` matches on file text,
    not on the AST, so a docstring mentioning ``strategy_factory.domain`` fails
    there while passing an import-only check. Aligning the two standards here
    stops that mismatch from being discovered one guard at a time.
    """
    offenders: list[str] = []
    forbidden = tuple(f"strategy_factory.{name}" for name in PRIVATE_SUBPACKAGES)

    for path in _source_files(distribution):
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(PACKAGES_ROOT)} -> {token}")

    assert offenders == [], (
        "internal subpackage names must not appear in downstream sources, not "
        "even in comments:\n" + "\n".join(sorted(offenders)[:20])
    )


def test_scan_is_not_silently_skipping_bom_encoded_files() -> None:
    """A parse failure must not quietly shrink the scanned set.

    ``aiask-quant-core`` ships 27 BOM-encoded sources. Read as plain utf-8 they
    raise SyntaxError, and an over-broad ``except`` would drop them from every
    layering check — a guard that looks green because it stopped looking.
    """
    unparsed = [
        path.relative_to(PACKAGES_ROOT).as_posix()
        for distribution in PACKAGE_MODULES
        for path in _source_files(distribution)
        if _parse(path) is None
    ]
    assert unparsed == [], "these files are excluded from the DAG scan:\n" + "\n".join(
        unparsed[:20]
    )


def test_layering_check_detects_a_violation_in_a_bom_encoded_file(tmp_path) -> None:
    """Proves the scan actually reads BOM files rather than skipping them."""
    probe = tmp_path / "bom_module.py"
    probe.write_text(
        "﻿def f():\n    from strategy_factory.api import contracts\n    return contracts\n",
        encoding="utf-8",
    )

    assert "strategy_factory" in _imported_roots(probe), (
        "a BOM-encoded file must still be parsed, otherwise violations hide in it"
    )


def test_boundary_json_envelope_matches_the_implementation() -> None:
    """A drift between the declared envelope and the code is a protocol break."""
    from strategy_factory.contracts.run_envelope import (
        PROTOCOL_VERSION,
        REQUIRED_ENVELOPE_FIELDS,
    )

    declared = json.loads(BOUNDARY_JSON.read_text(encoding="utf-8"))["run_envelope"]

    assert declared["protocol_version"] == PROTOCOL_VERSION
    assert tuple(declared["required_fields"]) == REQUIRED_ENVELOPE_FIELDS
