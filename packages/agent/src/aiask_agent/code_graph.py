from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from .tools.policy import ToolPolicy


TOOL_NAME = "agent_code_graph_query"
MAX_LIMIT = 100
MAX_DEPTH = 5


def build_code_graph_query_handler(policy: ToolPolicy):
    async def handler(arguments: dict[str, Any]) -> dict[str, Any]:
        return query_code_graph(arguments, policy=policy)

    return handler


def query_code_graph(arguments: dict[str, Any], *, policy: ToolPolicy) -> dict[str, Any]:
    action = str(arguments.get("action") or "summary").strip().lower()
    limit = _bounded_int(arguments.get("limit"), default=20, minimum=1, maximum=MAX_LIMIT)
    try:
        graph_dir = _resolve_graph_dir(arguments.get("graph_dir"), policy=policy)
        graph = _load_json(graph_dir / "core.graph.json")
        endpoint_map = _load_json_if_exists(graph_dir / "endpoint-map.json")
        summary = _load_json_if_exists(graph_dir / "CURATED_SUMMARY.json")
    except Exception as exc:
        return _envelope(False, data=None, error=str(exc), error_code="CODE_GRAPH_UNAVAILABLE")

    nodes = list(graph.get("nodes") or [])
    links = list(graph.get("links") or graph.get("edges") or [])
    by_id = {str(node.get("id")): node for node in nodes}

    if action == "summary":
        return _envelope(True, data=_summary_payload(graph_dir, nodes, links, endpoint_map, summary), error=None)
    if action == "search":
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _envelope(False, data=None, error="query is required for action=search", error_code="QUERY_REQUIRED")
        return _envelope(True, data={"query": query, "matches": _search_nodes(nodes, query, limit=limit)}, error=None)
    if action == "endpoint":
        query = str(arguments.get("endpoint") or arguments.get("query") or "").strip()
        return _envelope(True, data=_endpoint_payload(endpoint_map, query=query, limit=limit), error=None)
    if action == "explain":
        query = str(arguments.get("node") or arguments.get("query") or "").strip()
        if not query:
            return _envelope(False, data=None, error="node or query is required for action=explain", error_code="NODE_REQUIRED")
        node = _best_node(nodes, query)
        if not node:
            return _envelope(False, data={"query": query}, error=f"node not found: {query}", error_code="NODE_NOT_FOUND")
        return _envelope(True, data=_explain_node(node, by_id, links, limit=limit), error=None)
    if action == "affected":
        query = str(arguments.get("node") or arguments.get("query") or "").strip()
        if not query:
            return _envelope(False, data=None, error="node or query is required for action=affected", error_code="NODE_REQUIRED")
        node = _best_node(nodes, query)
        if not node:
            return _envelope(False, data={"query": query}, error=f"node not found: {query}", error_code="NODE_NOT_FOUND")
        depth = _bounded_int(arguments.get("depth"), default=2, minimum=1, maximum=MAX_DEPTH)
        relation = str(arguments.get("relation") or "calls").strip()
        return _envelope(
            True,
            data=_affected_nodes(node, by_id, links, relation=relation, depth=depth, limit=limit),
            error=None,
        )
    return _envelope(
        False,
        data={"allowed_actions": ["summary", "search", "endpoint", "explain", "affected"]},
        error=f"unsupported code graph action: {action}",
        error_code="UNSUPPORTED_ACTION",
    )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _allowed_roots(policy: ToolPolicy) -> list[Path]:
    roots: list[Path] = []
    for raw in policy.workspace_roots:
        if not raw:
            continue
        try:
            roots.append(Path(raw).resolve())
        except OSError:
            continue
    roots.append(_repo_root())
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).lower()
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _resolve_graph_dir(value: Any, *, policy: ToolPolicy) -> Path:
    roots = _allowed_roots(policy)
    raw = str(value or os.getenv("AIASK_CODE_GRAPH_DIR") or "").strip()
    if raw:
        candidates = _path_candidates(raw, roots)
    else:
        candidates = _default_graph_dir_candidates(roots)
    for candidate in candidates:
        graph_dir = _normalize_graph_dir(candidate)
        if graph_dir and _is_allowed_graph_dir(graph_dir, roots):
            return graph_dir
    raise FileNotFoundError("No curated code graph found. Build it with scripts/code_graph/build_aiask_code_graph.py or set AIASK_CODE_GRAPH_DIR.")


def _path_candidates(raw: str, roots: list[Path]) -> list[Path]:
    path = Path(raw)
    if path.is_absolute():
        return [path.resolve()]
    candidates = [(Path.cwd() / path).resolve()]
    candidates.extend((root / path).resolve() for root in roots)
    return candidates


def _default_graph_dir_candidates(roots: list[Path]) -> list[Path]:
    candidates: list[Path] = []
    for root in roots:
        reports = root / "reports"
        for parent in (reports / "code-graph",):
            if parent.exists():
                candidates.extend(sorted(parent.glob("*/curated"), key=_mtime, reverse=True))
        if reports.exists():
            candidates.extend(sorted(reports.glob("graphify-eval-*/curated"), key=_mtime, reverse=True))
    return candidates


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _normalize_graph_dir(path: Path) -> Path | None:
    path = path.resolve()
    if (path / "core.graph.json").exists():
        return path
    curated = path / "curated"
    if (curated / "core.graph.json").exists():
        return curated
    return None


def _is_allowed_graph_dir(path: Path, roots: list[Path]) -> bool:
    if not (path / "core.graph.json").exists():
        return False
    return any(_is_relative_to(path, root) for root in roots)


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_if_exists(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _load_json(path)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _node_payload(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": node.get("id"),
        "label": node.get("label"),
        "file_type": node.get("file_type"),
        "source_file": node.get("source_file"),
        "source_location": node.get("source_location"),
        "community": node.get("community"),
    }


def _node_score(node: dict[str, Any], query: str) -> int:
    q = query.lower()
    node_id = str(node.get("id") or "")
    label = str(node.get("label") or "")
    source_file = str(node.get("source_file") or "")
    fields = [node_id.lower(), label.lower(), source_file.lower()]
    if node_id == query:
        return 1000
    if label == query:
        return 950
    if node_id.lower() == q:
        return 900
    if label.lower() == q:
        return 850
    if q in fields[0]:
        return 700
    if q in fields[1]:
        return 650
    if q in fields[2]:
        return 500
    return 0


def _search_nodes(nodes: list[dict[str, Any]], query: str, *, limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        ((score, index, node) for index, node in enumerate(nodes) if (score := _node_score(node, query)) > 0),
        key=lambda item: (-item[0], item[1]),
    )
    return [{**_node_payload(node), "score": score} for score, _, node in ranked[:limit]]


def _best_node(nodes: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    matches = _search_nodes(nodes, query, limit=1)
    if not matches:
        return None
    node_id = str(matches[0].get("id"))
    return next((node for node in nodes if str(node.get("id")) == node_id), None)


def _explain_node(
    node: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    node_id = str(node.get("id"))
    neighbors: list[dict[str, Any]] = []
    degree = 0
    for link in links:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source != node_id and target != node_id:
            continue
        degree += 1
        if len(neighbors) >= limit:
            continue
        direction = "out" if source == node_id else "in"
        neighbor_id = target if source == node_id else source
        neighbors.append(
            {
                "direction": direction,
                "relation": link.get("relation"),
                "endpoint_relation": link.get("endpoint_relation"),
                "confidence": link.get("confidence"),
                "source_file": link.get("source_file"),
                "source_location": link.get("source_location"),
                "node": _node_payload(by_id.get(neighbor_id, {"id": neighbor_id})),
            }
        )
    return {"node": _node_payload(node), "degree": degree, "neighbors": neighbors}


def _affected_nodes(
    node: dict[str, Any],
    by_id: dict[str, dict[str, Any]],
    links: list[dict[str, Any]],
    *,
    relation: str,
    depth: int,
    limit: int,
) -> dict[str, Any]:
    start = str(node.get("id"))
    frontier = {start}
    seen = {start}
    affected: list[dict[str, Any]] = []
    for level in range(1, depth + 1):
        next_frontier: set[str] = set()
        for link in links:
            if relation and relation != "*" and str(link.get("relation") or "") != relation:
                continue
            source = str(link.get("source") or "")
            target = str(link.get("target") or "")
            if target not in frontier or source in seen:
                continue
            seen.add(source)
            next_frontier.add(source)
            affected.append(
                {
                    "depth": level,
                    "via": {
                        "relation": link.get("relation"),
                        "endpoint_relation": link.get("endpoint_relation"),
                        "source_file": link.get("source_file"),
                        "source_location": link.get("source_location"),
                    },
                    "node": _node_payload(by_id.get(source, {"id": source})),
                }
            )
            if len(affected) >= limit:
                return {"start": _node_payload(node), "relation": relation, "depth": depth, "nodes": affected}
        frontier = next_frontier
        if not frontier:
            break
    return {"start": _node_payload(node), "relation": relation, "depth": depth, "nodes": affected}


def _endpoint_payload(endpoint_map: dict[str, Any], *, query: str, limit: int) -> dict[str, Any]:
    endpoints = list(endpoint_map.get("endpoints") or [])
    if not query:
        return {
            "endpoint_count": endpoint_map.get("endpoint_count", len(endpoints)),
            "matched_count": endpoint_map.get("matched_count"),
            "server_only_count": endpoint_map.get("server_only_count"),
            "desktop_only_count": endpoint_map.get("desktop_only_count"),
            "endpoints": endpoints[:limit],
        }
    q = query.lower().split("?", 1)[0]
    matches = []
    for item in endpoints:
        path = str(item.get("path") or "")
        desktop_literals = " ".join(str(row.get("literal") or "") for row in item.get("desktop") or [])
        if q in path.lower() or q in desktop_literals.lower():
            matches.append(item)
        if len(matches) >= limit:
            break
    return {"query": query, "matches": matches}


def _summary_payload(
    graph_dir: Path,
    nodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    endpoint_map: dict[str, Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "graph_dir": str(graph_dir),
        "core": summary.get("core") or {"nodes": len(nodes), "edges": len(links)},
        "original": summary.get("original"),
        "packages": summary.get("packages"),
        "endpoint_map": {
            "endpoint_count": endpoint_map.get("endpoint_count"),
            "matched_count": endpoint_map.get("matched_count"),
            "server_only_count": endpoint_map.get("server_only_count"),
            "desktop_only_count": endpoint_map.get("desktop_only_count"),
        },
        "outputs": summary.get("outputs") or {},
    }


def _envelope(success: bool, *, data: Any, error: str | None, error_code: str | None = None) -> dict[str, Any]:
    payload = {
        "success": success,
        "data": data,
        "error": error,
        "meta": {
            "trace_id": f"aiask-agent:{TOOL_NAME}:{int(time.time() * 1000)}:{uuid4().hex[:8]}",
            "source_chain": ["aiask_agent.code_graph"],
            "side_effect": {
                "level": "read_only",
                "target": TOOL_NAME,
                "confirmation_required": False,
                "idempotent": True,
            },
        },
    }
    if error_code:
        payload["error_code"] = error_code
    return payload
