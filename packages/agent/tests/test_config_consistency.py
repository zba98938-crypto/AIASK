"""Configuration must agree across code, template, Makefile and monitoring.

env.example previously documented AIASK_AGENT_TOKEN (no reader anywhere) and
port 8000, while the code reads AIASK_AGENT_API_TOKEN and defaults to 8765, the
Makefile smoke target probed 8767, and the monitoring stack probed 8766 — a port
nothing ever served. Anyone following the template got an unauthenticated agent
on the wrong port.

These tests fail on divergence so the four sources cannot drift apart again.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

AIASK_ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = AIASK_ROOT / "env.example"
MAKEFILE = AIASK_ROOT / "Makefile"
MONITORING = AIASK_ROOT / "monitoring"

CANONICAL_PORT = 8765
CANONICAL_API_TOKEN = "AIASK_AGENT_API_TOKEN"
DEAD_NAMES = ("AIASK_AGENT_TOKEN", "AIASK_API_TOKEN", "AIASK_LLM_PROVIDER", "AIASK_LLM_MODEL")


def _env_example_keys() -> set[str]:
    keys: set[str] = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def test_code_default_port_is_canonical() -> None:
    from aiask_agent.server import build_server

    import inspect

    signature = inspect.signature(build_server)
    assert signature.parameters["port"].default == CANONICAL_PORT


def test_env_example_documents_the_canonical_port() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    match = re.search(r"^AIASK_AGENT_PORT=(\d+)$", text, re.MULTILINE)
    assert match, "env.example must document AIASK_AGENT_PORT"
    assert int(match.group(1)) == CANONICAL_PORT


def test_makefile_smoke_target_uses_the_canonical_port() -> None:
    text = MAKEFILE.read_text(encoding="utf-8")
    match = re.search(r"AIASK_AGENT_PORT:-(\d+)", text)
    assert match, "the Makefile smoke target must default AIASK_AGENT_PORT"
    assert int(match.group(1)) == CANONICAL_PORT


def test_monitoring_probes_the_canonical_port() -> None:
    stale: list[str] = []
    for path in sorted(MONITORING.rglob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for port in re.findall(r"host\.docker\.internal:(\d+)", line):
                if int(port) != CANONICAL_PORT:
                    stale.append(f"{path.relative_to(AIASK_ROOT)}:{number} probes port {port}")
    assert not stale, "monitoring probes a port the agent does not serve: " + "; ".join(stale)


def test_env_example_uses_the_token_name_the_code_reads() -> None:
    keys = _env_example_keys()
    assert CANONICAL_API_TOKEN in keys, f"env.example must document {CANONICAL_API_TOKEN}"


@pytest.mark.parametrize("name", DEAD_NAMES)
def test_env_example_has_no_dead_variables(name: str) -> None:
    """A documented variable with no reader silently does nothing."""
    assert name not in _env_example_keys(), f"{name} is not read by any code path"


@pytest.mark.parametrize("name", DEAD_NAMES)
def test_dead_variable_names_are_absent_from_source(name: str) -> None:
    offenders: list[str] = []
    for path in sorted((AIASK_ROOT / "packages").rglob("*.py")):
        if "/tests/" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # AIASK_AGENT_TOKEN is a prefix of AIASK_AGENT_TOKEN_*; match the whole word.
        if re.search(rf"\b{name}\b", text):
            offenders.append(str(path.relative_to(AIASK_ROOT)))
    assert not offenders, f"{name} should not be referenced: {offenders}"


def test_documented_agent_variables_have_a_reader() -> None:
    """Every AIASK_AGENT_* key in the template must be read somewhere in source."""
    sources = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in (AIASK_ROOT / "packages").rglob("*.py")
        if "/tests/" not in str(path)
    ]
    sources.extend(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (AIASK_ROOT / "scripts").rglob("*.py")
    )
    blob = "\n".join(sources)
    unread = [
        key
        for key in sorted(_env_example_keys())
        if key.startswith("AIASK_AGENT_") and key not in blob
    ]
    assert not unread, f"env.example documents variables nothing reads: {unread}"


def test_factory_root_resolution_finds_this_tree() -> None:
    """Root helpers must not assume the old integrations/aiask nesting depth."""
    import sys

    sys.path.insert(0, str(AIASK_ROOT / "scripts"))
    try:
        import _integration_paths
    finally:
        sys.path.pop(0)

    root = _integration_paths.aiask_root()
    assert (root / "packages").is_dir(), f"aiask_root() resolved to {root}"
    assert (root / "pyproject.toml").is_file()


def test_install_hermes_plugin_root_stays_inside_the_tree() -> None:
    """parents[3] used to escape above the repo once the tree became standalone."""
    import importlib.util

    path = AIASK_ROOT / "scripts" / "install_hermes_plugin.py"
    spec = importlib.util.spec_from_file_location("_install_hermes_plugin_probe", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = module._repo_root()
    assert root == AIASK_ROOT, f"_repo_root() resolved outside the tree: {root}"
    assert (root / "packages").is_dir()
