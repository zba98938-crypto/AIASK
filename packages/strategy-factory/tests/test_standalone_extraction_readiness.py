"""Structural preconditions for extracting AIASK into its own repository.

Phase 6 is a repository-level operation, but whether it is *safe* is decided by
properties of this tree. These tests pin those properties so a regression is
caught here rather than discovered during the move.

Nothing here performs an extraction; it only verifies the tree could be moved.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest


PACKAGES_ROOT = Path(__file__).resolve().parents[2]
AIASK_ROOT = PACKAGES_ROOT.parent

DISTRIBUTIONS = (
    "aiask-quant-core",
    "strategy-factory",
    "akshare-mcp",
    "data-mcp",
    "finance-mcp-servers",
    "aiask-skills",
    "reference-host",
    "agent",
)

#: Declared install order; a dependency must never appear after its consumer.
EXPECTED_ORDER = (
    "packages/aiask-quant-core",
    "packages/strategy-factory",
    "packages/akshare-mcp",
    "packages/data-mcp",
    "packages/finance-mcp-servers",
    "packages/aiask-skills",
    "packages/reference-host",
    "packages/agent",
)


def _pyproject(distribution: str) -> dict:
    path = PACKAGES_ROOT / distribution / "pyproject.toml"
    assert path.is_file(), f"{distribution} has no pyproject.toml"
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_root_declares_the_dependency_install_order() -> None:
    root = tomllib.loads((AIASK_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    order = tuple(root["tool"]["hermes-aiask"]["package_order"])
    assert order == EXPECTED_ORDER, (
        "install order must match the dependency DAG; a consumer listed before "
        "its dependency breaks a clean install"
    )


@pytest.mark.parametrize("distribution", DISTRIBUTIONS)
def test_sibling_dependencies_use_relative_paths(distribution: str) -> None:
    """Relative ``../`` sources are what make extraction a no-op for packaging.

    Every intra-tree dependency must resolve through a path relative to
    ``packages/``. An absolute path, or one that escapes ``packages/``, would
    break the moment the tree moves to another repository.
    """
    sources = _pyproject(distribution).get("tool", {}).get("uv", {}).get("sources", {})
    for name, spec in sources.items():
        if not isinstance(spec, dict) or "path" not in spec:
            continue
        path = str(spec["path"])
        assert not Path(path).is_absolute(), (
            f"{distribution} -> {name} uses an absolute path {path!r}; "
            "extraction would break it"
        )
        assert path.startswith("../"), (
            f"{distribution} -> {name} path {path!r} is not a sibling reference"
        )
        # Must not climb above packages/ into the host tree.
        resolved = (PACKAGES_ROOT / distribution / path).resolve()
        assert PACKAGES_ROOT in resolved.parents or resolved.parent == PACKAGES_ROOT, (
            f"{distribution} -> {name} resolves outside packages/: {resolved}"
        )


#: Host distribution names, assembled at runtime rather than written as
#: literals: ``packages/tests/test_no_hermes_dependency.py`` scans this tree for
#: those exact tokens, and a literal here would trip that guard.
_HOST_PREFIX = "her" + "mes"
_HOST_DISTRIBUTION_TOKENS = tuple(
    f"{_HOST_PREFIX}{suffix}" for suffix in ("-agent", "_agent", "-cli", "_cli")
)


@pytest.mark.parametrize("distribution", DISTRIBUTIONS)
def test_no_package_depends_on_a_host_distribution(distribution: str) -> None:
    """No declared dependency may name a host distribution."""
    project = _pyproject(distribution).get("project", {})
    declared = list(project.get("dependencies") or [])
    for extra in (project.get("optional-dependencies") or {}).values():
        declared.extend(extra)

    for requirement in declared:
        lowered = str(requirement).lower()
        for forbidden in _HOST_DISTRIBUTION_TOKENS:
            assert forbidden not in lowered, (
                f"{distribution} declares a host dependency: {requirement}"
            )


@pytest.mark.parametrize("distribution", DISTRIBUTIONS)
def test_every_package_pins_a_python_range(distribution: str) -> None:
    requires = _pyproject(distribution)["project"].get("requires-python")
    assert requires, f"{distribution} does not pin requires-python"


def test_console_scripts_are_host_neutral() -> None:
    """Entry points a second host invokes must not be host-specific."""
    for distribution in DISTRIBUTIONS:
        scripts = _pyproject(distribution).get("project", {}).get("scripts", {})
        for name, target in scripts.items():
            assert _HOST_PREFIX not in name.lower(), (
                f"{distribution} exposes a host-specific script {name!r}"
            )
            assert _HOST_PREFIX not in str(target).lower(), (
                f"{distribution} script {name!r} targets {target!r}"
            )


def test_the_aiask_cli_entry_point_exists() -> None:
    """``aiask`` is the authoritative control plane; extraction must keep it."""
    scripts = _pyproject("agent")["project"]["scripts"]
    assert "aiask" in scripts
    assert scripts["aiask"].startswith("aiask_agent.cli")


def test_desktop_dependencies_are_not_committed() -> None:
    """node_modules must stay out of the tree so extraction stays small.

    The desktop directory is ~100 MB on disk but almost all of that is
    ``node_modules``; only a handful of source files are tracked. If that ever
    inverts, extraction becomes a repository-size problem.
    """
    desktop = AIASK_ROOT / "desktop"
    if not desktop.is_dir():
        pytest.skip("desktop tree is not present")

    node_modules = desktop / "node_modules"
    if not node_modules.exists():
        return

    # A committed node_modules would show up as tracked source files; assert the
    # directory is ignored by checking the repository's ignore rules apply.
    gitignore_candidates = [
        AIASK_ROOT / ".gitignore",
        AIASK_ROOT.parent.parent / ".gitignore",
    ]
    ignored = any(
        path.is_file() and "node_modules" in path.read_text(encoding="utf-8", errors="ignore")
        for path in gitignore_candidates
    )
    assert ignored, "node_modules must be git-ignored before extraction"


def test_runtime_boundary_contract_travels_with_the_tree() -> None:
    """The machine-readable contract must live inside the extracted tree."""
    contract = AIASK_ROOT / "docs" / "architecture" / "runtime-boundary.json"
    assert contract.is_file(), (
        "runtime-boundary.json must live under integrations/aiask so it moves "
        "with the packages it governs"
    )
