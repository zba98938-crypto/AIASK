#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable


REQUIRED_FRONTMATTER_FIELDS = (
    "capability_tier",
    "runtime_status",
    "product_surfaces",
    "backing_tools",
    "role_tags",
    "last_runtime_verified_at",
)


@dataclass
class SkillCoverage:
    skill: str
    path: str
    frontmatter: dict[str, Any]
    backing_tools: list[str]
    missing_tools: list[str]
    missing_frontmatter_fields: list[str]


def _strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_items = value.replace("\n", ",").split(",")
    elif isinstance(value, Iterable):
        raw_items = list(value)
    else:
        raw_items = [value]
    return [str(item).strip().strip("'\"") for item in raw_items if str(item).strip().strip("'\"")]


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if not raw:
        return ""
    if raw.startswith("[") and raw.endswith("]"):
        return _strings(raw[1:-1])
    if raw.lower() in {"true", "false"}:
        return raw.lower() == "true"
    return raw.strip("'\"")


def _parse_frontmatter(path: Path) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    values: dict[str, Any] = {}
    current_key: str | None = None
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- ") and current_key:
            if not isinstance(values.get(current_key), list):
                values[current_key] = []
            values[current_key].append(_parse_scalar(stripped[2:]))
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        parsed = _parse_scalar(value)
        values[current_key] = parsed
    return values


def _public_function_names(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            names.add(node.name)
    return names


def discover_runtime_tools(repo_root: Path, server_file: Path, tools_dir: Path):
    runtime_tools: set[str] = set()
    tool_source: dict[str, str] = {}
    for path in [server_file, *sorted(tools_dir.rglob("*.py"))]:
        for name in _public_function_names(path):
            runtime_tools.add(name)
            try:
                tool_source.setdefault(name, str(path.resolve().relative_to(repo_root.resolve())))
            except Exception:
                tool_source.setdefault(name, str(path))
    return sorted(runtime_tools), {"tool_count": len(runtime_tools)}, tool_source


def collect_skill_coverages(skills_dir: Path, runtime_tools: set[str], repo_root: Path) -> list[SkillCoverage]:
    if not skills_dir.is_dir():
        return []
    coverages: list[SkillCoverage] = []
    for skill_path in sorted(skills_dir.glob("*/SKILL.md")):
        skill_id = skill_path.parent.name
        if skill_id.startswith("_"):
            continue
        frontmatter = _parse_frontmatter(skill_path)
        backing_tools = _strings(frontmatter.get("backing_tools"))
        missing_tools = sorted(tool for tool in backing_tools if tool not in runtime_tools)
        missing_fields = [field for field in REQUIRED_FRONTMATTER_FIELDS if not frontmatter.get(field)]
        try:
            rel_path = str(skill_path.resolve().relative_to(repo_root.resolve()))
        except Exception:
            rel_path = str(skill_path)
        coverages.append(
            SkillCoverage(
                skill=skill_id,
                path=rel_path,
                frontmatter=frontmatter,
                backing_tools=backing_tools,
                missing_tools=missing_tools,
                missing_frontmatter_fields=missing_fields,
            )
        )
    return coverages


def _literal_string_keys_from_assignment(path: Path, assignment_name: str) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    keys: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == assignment_name for target in node.targets):
            continue
        if isinstance(node.value, ast.Dict):
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append(key.value)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and func.attr == "update"
            and isinstance(func.value, ast.Name)
            and func.value.id == assignment_name
        ):
            continue
        for arg in node.args:
            if not isinstance(arg, ast.Dict):
                continue
            for key in arg.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    keys.append(key.value)
    return sorted(set(keys))


def discover_skill_executors(skills_file: Path) -> dict[str, Any]:
    executable_skill_ids = _literal_string_keys_from_assignment(skills_file, "_SKILL_EXECUTORS")
    return {
        "executable_skill_ids": executable_skill_ids,
        "runtime_executor_count": len(executable_skill_ids),
    }


def discover_runtime_skill_contracts(repo_root: Path, skill_ids: list[str]) -> list[str]:
    registry_path = repo_root / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "tools" / "skills_registry.py"
    contract_ids = _literal_string_keys_from_assignment(registry_path, "_SKILL_CONTRACTS")
    requested = set(skill_ids)
    return sorted(skill_id for skill_id in contract_ids if skill_id in requested)


def build_skill_capability_audit(
    repo_root: Path,
    *,
    skill_coverages: list[SkillCoverage],
    runtime_contract_skill_ids: list[str],
    runtime_executor_skill_ids: list[str],
    runtime_tools: list[str],
    tool_coverage_source: dict[str, str],
) -> dict[str, Any]:
    actual_local_skills = sorted(item.skill for item in skill_coverages)
    runtime_contract_skills = sorted(set(runtime_contract_skill_ids))
    runtime_executor_skills = sorted(skill_id for skill_id in set(runtime_executor_skill_ids) if skill_id in set(actual_local_skills))
    missing_frontmatter = {
        item.skill: item.missing_frontmatter_fields
        for item in skill_coverages
        if item.missing_frontmatter_fields
    }
    missing_runtime_tools = {
        item.skill: item.missing_tools
        for item in skill_coverages
        if item.missing_tools
    }
    meta_conflicts: list[dict[str, Any]] = []
    if actual_local_skills != runtime_contract_skills:
        meta_conflicts.append({"type": "runtime_contract_mismatch", "actual": actual_local_skills, "runtime": runtime_contract_skills})
    if actual_local_skills != runtime_executor_skills:
        meta_conflicts.append({"type": "runtime_executor_mismatch", "actual": actual_local_skills, "runtime": runtime_executor_skills})
    if missing_runtime_tools:
        meta_conflicts.append({"type": "missing_runtime_tools", "skills": missing_runtime_tools})
    return {
        "actual_local_skills": actual_local_skills,
        "runtime_contract_skills": runtime_contract_skills,
        "runtime_executor_skills": runtime_executor_skills,
        "missing_from_meta": {
            "frontmatter_fields": missing_frontmatter,
            "runtime_tools": missing_runtime_tools,
        },
        "stale_meta_detected": bool(meta_conflicts or missing_frontmatter),
        "meta_conflicts": meta_conflicts,
        "live_validation_failures": [],
        "runtime_tool_count": len(runtime_tools),
        "tool_coverage_source_count": len(tool_coverage_source),
    }


def detect_module_name_collisions(package_root: Path) -> list[dict[str, Any]]:
    by_name: dict[str, list[str]] = {}
    for path in sorted(package_root.rglob("*.py")):
        by_name.setdefault(path.stem, []).append(str(path))
    return [
        {"module": name, "paths": paths}
        for name, paths in sorted(by_name.items())
        if len(paths) > 1 and name != "__init__"
    ]


def compute_report(
    repo_root: Path,
    runtime_tools: list[str],
    server_files: list[Path],
    skill_coverages: list[SkillCoverage],
    module_name_collisions: list[dict[str, Any]],
    skill_executor_audit: dict[str, Any],
    tool_source: dict[str, str],
    capability_audit: dict[str, Any],
) -> dict[str, Any]:
    return {
        "repo_local_skill_count": len(capability_audit.get("actual_local_skills") or []),
        "runtime_contract_count": len(capability_audit.get("runtime_contract_skills") or []),
        "runtime_executor_count": len(capability_audit.get("runtime_executor_skills") or []),
        "stale_meta_detected": bool(capability_audit.get("stale_meta_detected")),
        "meta_conflicts": list(capability_audit.get("meta_conflicts") or []),
        "runtime_tool_count": len(runtime_tools),
        "server_files": [str(path) for path in server_files],
        "module_name_collisions": module_name_collisions,
        "executors": skill_executor_audit,
        "tool_coverage_source": tool_source,
        "capability_audit": capability_audit,
        "skills": [item.__dict__ for item in skill_coverages],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit AKShare skill coverage against runtime tools and executors.")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()
    server_file = repo_root / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "server.py"
    tools_dir = repo_root / "packages" / "akshare-mcp" / "src" / "akshare_mcp" / "tools"
    package_root = repo_root / "packages" / "akshare-mcp" / "src" / "akshare_mcp"
    runtime_tools, _, tool_source = discover_runtime_tools(repo_root, server_file, tools_dir)
    skill_coverages = collect_skill_coverages(repo_root / ".codex" / "skills", set(runtime_tools), repo_root)
    executor_audit = discover_skill_executors(tools_dir / "skills.py")
    contracts = discover_runtime_skill_contracts(repo_root, [item.skill for item in skill_coverages])
    capability_audit = build_skill_capability_audit(
        repo_root,
        skill_coverages=skill_coverages,
        runtime_contract_skill_ids=contracts,
        runtime_executor_skill_ids=executor_audit["executable_skill_ids"],
        runtime_tools=runtime_tools,
        tool_coverage_source=tool_source,
    )
    report = compute_report(
        repo_root,
        runtime_tools,
        [server_file],
        skill_coverages,
        detect_module_name_collisions(package_root),
        executor_audit,
        tool_source,
        capability_audit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["stale_meta_detected"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
