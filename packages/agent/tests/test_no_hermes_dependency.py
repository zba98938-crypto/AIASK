from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


PACKAGES_ROOT = Path(__file__).resolve().parents[2]
THIS_FILE = Path(__file__).resolve()
SKIPPED_DIRS = {
    ".mcp_cache",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "cache",
    "logs",
}
TEXT_FILE_NAMES = {
    ".env.example",
    "Makefile",
    "pyproject.toml",
    "requirements.txt",
    "requirements.lock.txt",
    "uv.lock",
}
TEXT_SUFFIXES = {
    ".cfg",
    ".ini",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}


def _active_package_files() -> Iterable[Path]:
    for path in PACKAGES_ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == THIS_FILE:
            continue
        relative_parts = path.relative_to(PACKAGES_ROOT).parts
        if any(part in SKIPPED_DIRS for part in relative_parts):
            continue
        if path.name in TEXT_FILE_NAMES or path.suffix in TEXT_SUFFIXES:
            yield path


def test_active_packages_have_no_hermes_runtime_dependency() -> None:
    forbidden = (
        "hermes-agent",
        "run_agent.py",
        "model_tools.py",
        "tools.mcp_tool",
        "gateway.platforms",
        "hermes_cli",
        "vendor/hermes-agent-upstream",
        "vendor/hermes",
    )
    for path in sorted(_active_package_files()):
        text = path.read_text(encoding="utf-8", errors="ignore")
        lowered = text.lower()
        for token in forbidden:
            assert token.lower() not in lowered, f"{path} references forbidden Hermes runtime token {token!r}"
