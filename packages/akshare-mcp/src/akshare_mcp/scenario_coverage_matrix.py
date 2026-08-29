from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def parse_coverage_matrix(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Coverage matrix doc not found: {path}")

    rows: list[dict[str, Any]] = []
    in_matrix = False
    for raw_line in _read_text(path).splitlines():
        line = raw_line.rstrip()
        if re.match(r"^##\s+\d+\s+工具全覆盖矩阵$", line.strip()):
            in_matrix = True
            continue
        if not in_matrix:
            continue
        if line.startswith("## ") and "覆盖率统计" in line:
            break
        match = re.match(r"^\|\s*(\d+)\s*\|\s*`([^`]+)`\s*\|\s*([^|]+?)\s*\|$", line)
        if not match:
            continue
        task_text = match.group(3).strip()
        tasks = [item.strip() for item in re.split(r"[、,，/]+", task_text) if item.strip()]
        rows.append(
            {
                "index": int(match.group(1)),
                "name": match.group(2).strip(),
                "coverage_task_text": task_text,
                "coverage_tasks": tasks,
            }
        )
    return rows


def merge_runtime_with_coverage(
    runtime_rows: list[dict[str, Any]],
    coverage_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    runtime_map = {str(row.get("name")): row for row in list(runtime_rows or []) if row.get("name")}
    coverage_map = {str(row.get("name")): row for row in list(coverage_rows or []) if row.get("name")}

    merged_tools: list[dict[str, Any]] = []
    all_names = sorted(set(runtime_map) | set(coverage_map))
    for name in all_names:
        runtime = runtime_map.get(name, {})
        coverage = coverage_map.get(name, {})
        merged_tools.append(
            {
                "name": name,
                "category": runtime.get("category"),
                "implementation_path": runtime.get("implementation_path"),
                "coverage_index": coverage.get("index"),
                "coverage_task_text": coverage.get("coverage_task_text"),
                "coverage_tasks": coverage.get("coverage_tasks", []),
                "covered": name in coverage_map,
                "runtime_registered": name in runtime_map,
            }
        )

    missing_in_runtime = [row["name"] for row in coverage_rows if row["name"] not in runtime_map]
    uncovered_runtime = [row["name"] for row in runtime_rows if row["name"] not in coverage_map]
    multi_task_tools = [
        row["name"]
        for row in merged_tools
        if len(list(row.get("coverage_tasks") or [])) >= 2
    ]
    return {
        "summary": {
            "runtime_tool_count": len(runtime_rows),
            "coverage_matrix_count": len(coverage_rows),
            "merged_tool_count": len(merged_tools),
            "missing_in_runtime_count": len(missing_in_runtime),
            "uncovered_runtime_count": len(uncovered_runtime),
            "multi_task_tool_count": len(multi_task_tools),
        },
        "missing_in_runtime": missing_in_runtime,
        "uncovered_runtime": uncovered_runtime,
        "tools": merged_tools,
    }
