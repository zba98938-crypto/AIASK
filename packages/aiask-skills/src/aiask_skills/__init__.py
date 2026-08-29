"""Host-neutral AIASK Skills bundle."""

from __future__ import annotations

from pathlib import Path
from typing import Any

VERSION = "1.0.0"
PROTOCOL_VERSION = "1.0"


def bundle_path() -> Path:
    return Path(__file__).resolve().parent / "bundle"


def skill_paths() -> tuple[Path, ...]:
    return tuple(sorted(bundle_path().glob("*/SKILL.md")))


def _parse_front_matter(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, Any] = {}
    current_list: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        stripped = line.strip()
        if current_list and stripped.startswith("-"):
            values.setdefault(current_list, []).append(stripped[1:].strip().strip('"').strip("'"))
            continue
        if ":" not in line or line[:1].isspace():
            continue
        key, value = line.split(":", 1)
        value = value.strip().strip('"').strip("'")
        key = key.strip()
        if not value:
            values[key] = []
            current_list = key
        else:
            values[key] = value
            current_list = None
    return values


def discover() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for path in skill_paths():
        metadata = _parse_front_matter(path.read_text(encoding="utf-8", errors="replace"))
        result.append({
            "path": str(path),
            "name": str(metadata.get("name") or path.parent.name),
            "version": str(metadata.get("version") or VERSION),
            "protocol_version": str(metadata.get("protocol_version") or PROTOCOL_VERSION),
            "capabilities": metadata.get("capabilities", ""),
            "required_mcp": metadata.get("required_mcp", ""),
            "side_effect": metadata.get("side_effect", "read_only"),
        })
    return result


__all__ = ["PROTOCOL_VERSION", "VERSION", "bundle_path", "discover", "skill_paths"]
