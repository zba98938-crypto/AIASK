from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "packages" / "agent" / "src" / "aiask_agent"
CLASSIFICATION_PATH = REPO_ROOT / "docs" / "architecture" / "tool-call-path-classification.json"


def _attribute_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return list(reversed(parts))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            mapping[child] = node
    return mapping


def _qualname(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    current = node
    while current in parents:
        current = parents[current]
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
    return ".".join(reversed(names)) or "<module>"


def _first_tool_argument(node: ast.Call) -> str:
    if not node.args:
        return "<missing>"
    first = node.args[0]
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        return first.value
    if isinstance(first, ast.Name):
        return f"${first.id}"
    return ast.unparse(first)[:120]


def _direct_tool_registry_calls() -> set[str]:
    keys: set[str] = set()
    for path in sorted(SRC_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "tool_registry.call_tool" not in text:
            continue
        tree = ast.parse(text)
        parents = _parents(tree)
        relative = path.relative_to(REPO_ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            chain = _attribute_chain(node.func)
            if len(chain) >= 2 and chain[-2:] == ["tool_registry", "call_tool"]:
                keys.add(f"{relative}::{_qualname(node, parents)}::{_first_tool_argument(node)}")
    return keys


def _classification_payload() -> dict[str, Any]:
    return json.loads(CLASSIFICATION_PATH.read_text(encoding="utf-8"))


def test_direct_tool_registry_call_paths_are_classified() -> None:
    payload = _classification_payload()
    allowed = set(payload.get("allowed_classifications") or [])
    entries = list(payload.get("entries") or [])
    classified = {str(item.get("key") or ""): item for item in entries}

    assert payload.get("version") == "aiask_tool_call_path_classification_v1"
    assert len(classified) == len(entries), "duplicate direct-call classification keys"
    for key, item in classified.items():
        assert key, "classification key is required"
        assert item.get("classification") in allowed, f"bad classification for {key}"
        assert str(item.get("reason") or "").strip(), f"classification reason is required for {key}"

    actual = _direct_tool_registry_calls()
    missing = sorted(actual - set(classified))
    stale = sorted(set(classified) - actual)

    assert missing == [], "direct tool_registry.call_tool path needs classification: " + ", ".join(missing)
    assert stale == [], "stale direct-call classification should be removed or updated: " + ", ".join(stale)
