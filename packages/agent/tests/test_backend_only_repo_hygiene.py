"""This tree is backend-only; nothing may still point at the removed frontend.

The Desktop workbench was deleted when this tree was extracted, but readiness
gates, the code graph, skills docs and API references kept pointing at
``desktop/**`` and ``/v1/desktop/*``. A dangling pointer is worse than a missing
one: it tells a reader (or an agent following a skill) to open a file that does
not exist, and it made readiness report ``product_ready: false`` forever.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

AIASK_ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = AIASK_ROOT / "docs" / "archive" / "frontend"

# Directories whose content must not reference the removed frontend. The archive
# is exempt: it is historical by definition and says so in its README.
SCANNED = ("packages", "scripts", "skills", "tools", "monitoring")


def _files(suffixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for name in SCANNED:
        root = AIASK_ROOT / name
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in suffixes:
                continue
            if ARCHIVE in path.parents or "__pycache__" in path.parts:
                continue
            found.append(path)
    return found


def test_no_desktop_directory_exists() -> None:
    assert not (AIASK_ROOT / "desktop").exists(), "this tree is backend-only"


def test_frontend_docs_are_archived_with_a_warning() -> None:
    assert ARCHIVE.is_dir(), "frontend docs must be archived, not deleted outright"
    assert not (AIASK_ROOT / "docs" / "frontend-v1").exists(), "old location must be gone"
    readme = (ARCHIVE / "README.md").read_text(encoding="utf-8")
    assert "归档" in readme
    assert "不可执行" in readme, "the archive must state that its instructions cannot be run"


def test_no_source_file_references_removed_frontend_paths() -> None:
    """Nothing may point a reader at a frontend file that no longer exists.

    Comments explaining *why* something was removed are allowed; what must not
    survive is a path a tool or reader would try to open.
    """
    offenders: list[str] = []
    for path in _files((".py", ".md", ".yml", ".yaml", ".sh", ".json")):
        if path.name == Path(__file__).name:
            continue  # this file names the patterns it forbids
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "desktop/src" in line or "desktop/package.json" in line:
                offenders.append(f"{path.relative_to(AIASK_ROOT)}:{number}")
    assert not offenders, "references to removed frontend files: " + ", ".join(offenders)


def test_no_live_reference_to_the_removed_endpoint_family() -> None:
    """No code may still *call* /v1/desktop.

    Prose that documents what a route replaced is fine and useful. What must not
    survive is a live usage: a request, a route registration, or a path constant.
    """
    allowed_names = {
        "api_envelope.py",       # defines the old -> new mapping
        "removed.py",            # the catch-all router
        "fallback_server.py",    # the legacy interceptor
        "test_neutral_api_contract.py",
        "test_mutating_route_guardrails.py",
        "test_workbench_contracts.py",   # asserts the 404 behaviour
        "test_backend_only_repo_hygiene.py",
    }
    # A usage is a Python/shell string literal or a route registration. RST-style
    # double backticks in a docstring are prose, so they are not a usage.
    usage = re.compile(r"""(?<!`)["']/v1/desktop""")
    offenders: list[str] = []
    for path in _files((".py", ".yml", ".yaml", ".sh")):
        if path.name in allowed_names:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith(("#", "``", "- ")) or "``/v1/desktop" in line:
                continue
            if usage.search(line):
                offenders.append(f"{path.relative_to(AIASK_ROOT)}:{number}")
    assert not offenders, "live references to the removed endpoint family: " + ", ".join(offenders)


def test_readiness_does_not_block_on_a_missing_frontend() -> None:
    source = (AIASK_ROOT / "scripts" / "aiask_readiness.py").read_text(encoding="utf-8")
    assert "REQUIRED_DESKTOP_WORKBENCH_FILES" not in source
    assert "_desktop_workbench_status" not in source
    assert "desktop_workbench_missing" not in source, "absence of a frontend is the expected state"


def test_code_graph_no_longer_scans_a_frontend() -> None:
    source = (AIASK_ROOT / "scripts" / "code_graph" / "curate_aiask_graph.py").read_text(encoding="utf-8")
    assert 'workspace / "desktop"' not in source, "dead frontend scanner must be gone"
    assert '"desktop": "desktop/src/"' not in source


def test_gitignore_covers_generated_state() -> None:
    patterns = {
        line.strip()
        for line in (AIASK_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }
    for required in ("__pycache__/", ".pytest_cache/", "logs/", "*.sqlite3", "node_modules/", ".venv/"):
        assert required in patterns, f".gitignore must cover {required}"


def test_tool_call_path_classification_matches_renamed_modules() -> None:
    payload = json.loads(
        (AIASK_ROOT / "docs" / "architecture" / "tool-call-path-classification.json").read_text(encoding="utf-8")
    )
    blob = json.dumps(payload)
    assert "desktop_ops.py" not in blob, "modules were renamed to confirmed_actions.py"
    assert "desktop_payloads.py" not in blob
    for entry in payload["entries"]:
        module = entry["key"].split("::", 1)[0]
        assert (AIASK_ROOT / module).is_file(), f"classification names a missing file: {module}"


@pytest.mark.parametrize(
    "module",
    [
        "packages/agent/src/aiask_agent/adapters/confirmed_actions.py",
        "packages/agent/src/aiask_agent/settings_payloads.py",
        "packages/agent/src/aiask_agent/capabilities_payloads.py",
        "packages/agent/src/aiask_agent/api_envelope.py",
        "packages/agent/src/aiask_agent/routes/removed.py",
        "packages/agent/src/aiask_agent/routes/finance.py",
        "packages/agent/src/aiask_agent/routes/research.py",
        "packages/agent/src/aiask_agent/routes/users.py",
        "packages/agent/src/aiask_agent/routes/data_settings.py",
        "packages/agent/src/aiask_agent/routes/workbench.py",
    ],
)
def test_renamed_and_new_modules_exist(module: str) -> None:
    assert (AIASK_ROOT / module).is_file(), module


def test_old_desktop_named_modules_are_gone() -> None:
    for stale in (
        "packages/agent/src/aiask_agent/adapters/desktop_ops.py",
        "packages/agent/src/aiask_agent/desktop_payloads.py",
        "packages/agent/src/aiask_agent/desktop_capabilities_payloads.py",
        "packages/agent/src/aiask_agent/routes/desktop_data.py",
        "packages/agent/src/aiask_agent/routes/desktop_finance.py",
        "packages/agent/src/aiask_agent/routes/desktop_user.py",
        "packages/agent/src/aiask_agent/routes/desktop_assets.py",
        "packages/agent/src/aiask_agent/routes/desktop_runs.py",
        "packages/agent/src/aiask_agent/routes/desktop_workbench.py",
        "server/tests/contract/test_desktop_contracts.py",
    ):
        assert not (AIASK_ROOT / stale).exists(), f"{stale} should have been removed"
