import argparse
import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any


NOISE_LABELS = {
    "Any",
    "str",
    "int",
    "bool",
    "float",
    "bytes",
    "object",
    "type",
    "list",
    "dict",
    "set",
    "tuple",
    "None",
    "Path",
    "datetime",
    "date",
    "DataFrame",
    "Series",
    "ndarray",
    "Connection",
    "Callable",
    "Awaitable",
    "Optional",
    "AST",
    "BaseHTTPRequestHandler",
    "BaseModel",
    "Counter",
    "Enum",
    "FastAPI",
    "HTTPException",
    "JSONResponse",
    "Request",
    "RuntimeError",
    "StreamingResponse",
    "ThreadingHTTPServer",
    "ZoneInfo",
    "enum",
    "timedelta",
}

NOISE_LABEL_PREFIXES = (
    ".",
)

ROOT_RUNNERS = {
    "run_all_factories.py",
    "run_strategy_factory.py",
    "run_factor_mining_factory.py",
    "run_incubation_factory.py",
}

PACKAGE_PREFIXES = {
    "agent": "packages/agent/src/",
    "akshare-mcp": "packages/akshare-mcp/src/",
    "strategy-factory": "packages/strategy-factory/src/",
    "aiask-quant-core": "packages/aiask-quant-core/src/",
    "desktop": "desktop/src/",
    "root-runners": "",
}


def _load_graph(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_graph(path: Path, nodes: list[dict[str, Any]], links: list[dict[str, Any]], *, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "directed": True,
        "multigraph": False,
        "graph": meta,
        "nodes": nodes,
        "links": links,
        "hyperedges": [],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _source_file(node: dict[str, Any]) -> str:
    return str(node.get("source_file") or "").replace("\\", "/")


def _label(node: dict[str, Any]) -> str:
    return str(node.get("label") or "")


def _is_test_source(source_file: str) -> bool:
    return (
        "/tests/" in source_file
        or source_file.startswith("desktop/tests/")
        or source_file.endswith(".test.ts")
        or source_file.endswith(".test.tsx")
        or source_file.endswith("_test.py")
        or "/test_" in source_file
        or source_file.split("/")[-1].startswith("test_")
    )


def _is_doc_source(source_file: str) -> bool:
    return source_file == "AGENT.md" or source_file.startswith("docs/")


def _is_root_runner(source_file: str) -> bool:
    return source_file in ROOT_RUNNERS


def _package_for_source(source_file: str) -> str:
    if source_file.startswith("packages/agent/src/"):
        return "agent"
    if source_file.startswith("packages/akshare-mcp/src/"):
        return "akshare-mcp"
    if source_file.startswith("packages/strategy-factory/src/"):
        return "strategy-factory"
    if source_file.startswith("packages/aiask-quant-core/src/"):
        return "aiask-quant-core"
    if source_file.startswith("desktop/src/"):
        return "desktop"
    if _is_root_runner(source_file):
        return "root-runners"
    if _is_test_source(source_file):
        return "tests"
    if _is_doc_source(source_file):
        return "docs"
    return "other"


def _is_core_source(source_file: str) -> bool:
    return _package_for_source(source_file) in {
        "agent",
        "akshare-mcp",
        "strategy-factory",
        "aiask-quant-core",
        "desktop",
        "root-runners",
    }


def _is_noise_node(node: dict[str, Any]) -> bool:
    label = _label(node)
    source_file = _source_file(node)
    if not source_file:
        return True
    if label in NOISE_LABELS:
        return True
    if any(label.startswith(prefix) for prefix in NOISE_LABEL_PREFIXES):
        return True
    if node.get("file_type") == "rationale":
        return True
    return False


def _subgraph(
    nodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    keep_node,
    *,
    name: str,
    original_counts: dict[str, int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    kept_nodes = [dict(node) for node in nodes if keep_node(node)]
    kept_ids = {node["id"] for node in kept_nodes}
    kept_links = [
        dict(link)
        for link in links
        if link.get("source") in kept_ids and link.get("target") in kept_ids
    ]
    _assign_components(kept_nodes, kept_links)
    meta = {
        "name": name,
        "mode": "aiask_curated_graph",
        "node_count": len(kept_nodes),
        "edge_count": len(kept_links),
        "original_counts": original_counts,
    }
    return kept_nodes, kept_links, meta


def _assign_components(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> None:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for link in links:
        source = str(link.get("source") or "")
        target = str(link.get("target") or "")
        if source and target:
            adjacency[source].add(target)
            adjacency[target].add(source)

    node_ids = [str(node.get("id")) for node in nodes]
    visited: set[str] = set()
    component_id = 0
    for node_id in node_ids:
        if node_id in visited:
            continue
        queue = deque([node_id])
        visited.add(node_id)
        while queue:
            current = queue.popleft()
            for nxt in adjacency.get(current, set()):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        component_id += 1

    component_by_node: dict[str, int] = {}
    visited.clear()
    component_id = 0
    for node_id in node_ids:
        if node_id in visited:
            continue
        queue = deque([node_id])
        visited.add(node_id)
        while queue:
            current = queue.popleft()
            component_by_node[current] = component_id
            for nxt in adjacency.get(current, set()):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        component_id += 1

    for node in nodes:
        node["community"] = component_by_node.get(str(node.get("id")), -1)


def _degree_report(nodes: list[dict[str, Any]], links: list[dict[str, Any]], limit: int = 25) -> list[dict[str, Any]]:
    degree = Counter()
    for link in links:
        degree[str(link.get("source"))] += 1
        degree[str(link.get("target"))] += 1
    by_id = {str(node.get("id")): node for node in nodes}
    rows = []
    for node_id, count in degree.most_common(limit):
        node = by_id.get(node_id)
        if not node:
            continue
        rows.append(
            {
                "id": node_id,
                "label": node.get("label"),
                "source_file": node.get("source_file"),
                "source_location": node.get("source_location"),
                "degree": count,
            }
        )
    return rows


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower() or "root"


def _endpoint_path(value: str) -> str:
    return value.split("?", 1)[0].strip()


def _index_nodes(nodes: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[tuple[str, str], list[str]]]:
    by_id = {str(node.get("id")): node for node in nodes}
    by_source_label: dict[tuple[str, str], list[str]] = defaultdict(list)
    for node in nodes:
        by_source_label[(_source_file(node), _label(node))].append(str(node.get("id")))
    return by_id, by_source_label


def _file_node_id(nodes: list[dict[str, Any]], source_file: str) -> str | None:
    basename = source_file.rsplit("/", 1)[-1]
    for node in nodes:
        if _source_file(node) == source_file and _label(node) == basename:
            return str(node.get("id"))
    return None


def _ensure_endpoint_node(nodes_by_id: dict[str, dict[str, Any]], nodes: list[dict[str, Any]], path: str) -> str:
    endpoint_id = f"endpoint_{_slug(path)}"
    if endpoint_id not in nodes_by_id:
        node = {
            "id": endpoint_id,
            "label": path,
            "norm_label": path.lower(),
            "file_type": "endpoint",
            "source_file": "aiask:endpoints",
            "source_location": None,
            "community": -1,
        }
        nodes.append(node)
        nodes_by_id[endpoint_id] = node
    return endpoint_id


def _append_unique_link(links: list[dict[str, Any]], seen: set[tuple[str, str, str, str]], link: dict[str, Any]) -> None:
    key = (
        str(link.get("source") or ""),
        str(link.get("target") or ""),
        str(link.get("relation") or ""),
        str(link.get("source_location") or ""),
    )
    if key in seen:
        return
    seen.add(key)
    links.append(link)


def _augment_endpoint_edges(
    nodes: list[dict[str, Any]],
    links: list[dict[str, Any]],
    *,
    workspace: Path,
) -> dict[str, Any]:
    """Add AIASK-specific Desktop/API route edges that AST cannot infer."""
    nodes_by_id, by_source_label = _index_nodes(nodes)
    seen_links = {
        (
            str(link.get("source") or ""),
            str(link.get("target") or ""),
            str(link.get("relation") or ""),
            str(link.get("source_location") or ""),
        )
        for link in links
    }

    route_re = re.compile(r"@\w+\.(get|post|patch|delete|put)\(\s*['\"]([^'\"]+)['\"]")
    def_re = re.compile(r"\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    string_endpoint_re = re.compile(r"['\"]((?:/v1|/health|/intents)[^'\"]*)['\"]")

    endpoint_rows: dict[str, dict[str, Any]] = {}
    server_files = [workspace / "packages" / "agent" / "src" / "aiask_agent" / "server.py"]
    routes_dir = workspace / "packages" / "agent" / "src" / "aiask_agent" / "routes"
    if routes_dir.exists():
        server_files.extend(sorted(routes_dir.glob("*.py")))
    for server_file in server_files:
        if not server_file.exists():
            continue
        server_source = server_file.relative_to(workspace).as_posix()
        lines = server_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        for index, line in enumerate(lines):
            match = route_re.search(line)
            if not match:
                continue
            method = match.group(1).upper()
            path = _endpoint_path(match.group(2))
            handler_name = ""
            for lookahead in range(index + 1, min(index + 10, len(lines))):
                def_match = def_re.match(lines[lookahead])
                if def_match:
                    handler_name = def_match.group(1)
                    break
            handler_id = None
            if handler_name:
                candidates = by_source_label.get((server_source, f"{handler_name}()"), [])
                handler_id = candidates[0] if candidates else None
            handler_id = handler_id or _file_node_id(nodes, server_source)
            if not handler_id:
                continue
            endpoint_id = _ensure_endpoint_node(nodes_by_id, nodes, path)
            _append_unique_link(
                links,
                seen_links,
                {
                    "source": handler_id,
                    "target": endpoint_id,
                    "relation": "calls",
                    "endpoint_relation": "serves_endpoint",
                    "confidence": "EXTRACTED",
                    "confidence_score": 1.0,
                    "weight": 1.0,
                    "source_file": server_source,
                    "source_location": f"L{index + 1}",
                    "method": method,
                },
            )
            row = endpoint_rows.setdefault(path, {"path": path, "server": [], "desktop": []})
            row["server"].append({"method": method, "handler": handler_name, "line": index + 1})

    desktop_src = workspace / "desktop" / "src"
    aiask_api_id = by_source_label.get(("desktop/src/services/aiaskApi.ts", "AiaskApi"), [None])[0]
    if desktop_src.exists():
        for path_obj in desktop_src.rglob("*"):
            if not path_obj.is_file() or path_obj.suffix.lower() not in {".ts", ".tsx"}:
                continue
            rel = path_obj.relative_to(workspace).as_posix()
            if _is_test_source(rel):
                continue
            source_text = path_obj.read_text(encoding="utf-8", errors="ignore")
            source_id = aiask_api_id if rel == "desktop/src/services/aiaskApi.ts" and aiask_api_id else _file_node_id(nodes, rel)
            if not source_id:
                continue
            for line_no, line in enumerate(source_text.splitlines(), start=1):
                for match in string_endpoint_re.finditer(line):
                    literal = match.group(1)
                    endpoint_path = _endpoint_path(literal)
                    endpoint_id = _ensure_endpoint_node(nodes_by_id, nodes, endpoint_path)
                    _append_unique_link(
                        links,
                        seen_links,
                        {
                            "source": source_id,
                            "target": endpoint_id,
                            "relation": "calls",
                            "endpoint_relation": "calls_endpoint",
                            "confidence": "EXTRACTED",
                            "confidence_score": 1.0,
                            "weight": 1.0,
                            "source_file": rel,
                            "source_location": f"L{line_no}",
                            "endpoint_literal": literal,
                        },
                    )
                    row = endpoint_rows.setdefault(endpoint_path, {"path": endpoint_path, "server": [], "desktop": []})
                    row["desktop"].append({"source_file": rel, "line": line_no, "literal": literal})

    _assign_components(nodes, links)
    endpoint_map = sorted(endpoint_rows.values(), key=lambda item: item["path"])
    return {
        "endpoint_count": len(endpoint_map),
        "matched_count": sum(1 for item in endpoint_map if item["server"] and item["desktop"]),
        "server_only_count": sum(1 for item in endpoint_map if item["server"] and not item["desktop"]),
        "desktop_only_count": sum(1 for item in endpoint_map if item["desktop"] and not item["server"]),
        "endpoints": endpoint_map,
    }


def _cross_package_edges(nodes: list[dict[str, Any]], links: list[dict[str, Any]], limit: int = 300) -> list[dict[str, Any]]:
    by_id = {str(node.get("id")): node for node in nodes}
    rows: list[dict[str, Any]] = []
    for link in links:
        source = by_id.get(str(link.get("source")))
        target = by_id.get(str(link.get("target")))
        if not source or not target:
            continue
        source_pkg = _package_for_source(_source_file(source))
        target_pkg = _package_for_source(_source_file(target))
        if source_pkg == target_pkg:
            continue
        if source_pkg not in PACKAGE_PREFIXES or target_pkg not in PACKAGE_PREFIXES:
            continue
        if _is_noise_node(source) or _is_noise_node(target):
            continue
        rows.append(
            {
                "source_package": source_pkg,
                "target_package": target_pkg,
                "source": source.get("label"),
                "target": target.get("label"),
                "relation": link.get("relation"),
                "confidence": link.get("confidence"),
                "source_file": link.get("source_file"),
                "source_location": link.get("source_location"),
            }
        )
    return rows[:limit]


def _write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# AIASK Graphify Curated Graph Report",
        "",
        "## Outputs",
    ]
    for key, value in report["outputs"].items():
        lines.append(f"- `{key}`: `{value}`")

    lines += [
        "",
        "## Counts",
        f"- Original: {report['original']['nodes']} nodes, {report['original']['edges']} edges",
        f"- Core curated: {report['core']['nodes']} nodes, {report['core']['edges']} edges",
        f"- Tests-only: {report['tests']['nodes']} nodes, {report['tests']['edges']} edges",
        f"- Docs-only: {report['docs']['nodes']} nodes, {report['docs']['edges']} edges",
        f"- Endpoint map: {report['endpoint_map']['endpoint_count']} endpoints, "
        f"{report['endpoint_map']['matched_count']} Desktop+Agent matches",
        "",
        "## Package Subgraphs",
    ]
    for name, item in report["packages"].items():
        lines.append(f"- `{name}`: {item['nodes']} nodes, {item['edges']} edges")

    lines += ["", "## Core Hub Nodes"]
    for row in report["core_top_nodes"]:
        lines.append(
            f"- `{row['label']}` ({row['degree']}): `{row['source_file']}:{row['source_location']}`"
        )

    lines += ["", "## Cross-Package Edge Samples"]
    for row in report["cross_package_edges"][:40]:
        lines.append(
            f"- `{row['source_package']}` -> `{row['target_package']}`: "
            f"`{row['source']}` --{row['relation']}--> `{row['target']}` "
            f"({row['confidence']}, `{row['source_file']}:{row['source_location']}`)"
        )

    lines += ["", "## Endpoint Samples"]
    for row in report["endpoint_map"]["endpoints"][:30]:
        server = ", ".join(f"{item['method']}:{item['handler']}" for item in row["server"][:3]) or "-"
        desktop = ", ".join(f"{item['source_file']}:{item['line']}" for item in row["desktop"][:3]) or "-"
        lines.append(f"- `{row['path']}`: server `{server}`, desktop `{desktop}`")

    lines += [
        "",
        "## Notes",
        "- Core graph excludes tests, docs, rationale nodes, and obvious Python/TypeScript builtin type hubs.",
        "- Subgraph `community` values are weak connected component ids after filtering, not Leiden communities.",
        "- Keep using the original full graph for forensic detail; use curated outputs for Agent/tool integration experiments.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Curate the AIASK Graphify output into usable subgraphs.")
    parser.add_argument("--graph", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    graph_path = Path(args.graph).resolve()
    out_dir = Path(args.out).resolve()
    workspace = Path(args.workspace).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    graph = _load_graph(graph_path)
    nodes = list(graph.get("nodes") or [])
    links = list(graph.get("links") or graph.get("edges") or [])
    original_counts = {"nodes": len(nodes), "edges": len(links)}

    def core_keep(node: dict[str, Any]) -> bool:
        source_file = _source_file(node)
        return _is_core_source(source_file) and not _is_test_source(source_file) and not _is_noise_node(node)

    core_nodes, core_links, core_meta = _subgraph(
        nodes,
        links,
        core_keep,
        name="aiask-core-curated",
        original_counts=original_counts,
    )
    endpoint_report = _augment_endpoint_edges(core_nodes, core_links, workspace=workspace)
    core_meta["endpoint_count"] = endpoint_report["endpoint_count"]
    core_meta["endpoint_matched_count"] = endpoint_report["matched_count"]
    _write_graph(out_dir / "core.graph.json", core_nodes, core_links, meta=core_meta)

    packages: dict[str, dict[str, int]] = {}
    for package in ["agent", "akshare-mcp", "strategy-factory", "aiask-quant-core", "desktop", "root-runners"]:
        pkg_nodes, pkg_links, pkg_meta = _subgraph(
            nodes,
            links,
            lambda node, package=package: _package_for_source(_source_file(node)) == package and not _is_noise_node(node),
            name=f"aiask-{package}",
            original_counts=original_counts,
        )
        _write_graph(out_dir / f"{package}.graph.json", pkg_nodes, pkg_links, meta=pkg_meta)
        packages[package] = {"nodes": len(pkg_nodes), "edges": len(pkg_links)}

    tests_nodes, tests_links, tests_meta = _subgraph(
        nodes,
        links,
        lambda node: _is_test_source(_source_file(node)) and not _is_noise_node(node),
        name="aiask-tests",
        original_counts=original_counts,
    )
    _write_graph(out_dir / "tests.graph.json", tests_nodes, tests_links, meta=tests_meta)

    docs_nodes, docs_links, docs_meta = _subgraph(
        nodes,
        links,
        lambda node: _is_doc_source(_source_file(node)) and not _is_noise_node(node),
        name="aiask-docs",
        original_counts=original_counts,
    )
    _write_graph(out_dir / "docs.graph.json", docs_nodes, docs_links, meta=docs_meta)

    cross_edges = _cross_package_edges(core_nodes, core_links)
    (out_dir / "cross-package-edges.json").write_text(
        json.dumps(cross_edges, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "endpoint-map.json").write_text(
        json.dumps(endpoint_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = {
        "original": original_counts,
        "core": {"nodes": len(core_nodes), "edges": len(core_links)},
        "tests": {"nodes": len(tests_nodes), "edges": len(tests_links)},
        "docs": {"nodes": len(docs_nodes), "edges": len(docs_links)},
        "packages": packages,
        "core_top_nodes": _degree_report(core_nodes, core_links),
        "cross_package_edges": cross_edges,
        "endpoint_map": {
            **{k: v for k, v in endpoint_report.items() if k != "endpoints"},
            "endpoints": endpoint_report["endpoints"][:100],
        },
        "outputs": {
            "core": str(out_dir / "core.graph.json"),
            "agent": str(out_dir / "agent.graph.json"),
            "akshare-mcp": str(out_dir / "akshare-mcp.graph.json"),
            "strategy-factory": str(out_dir / "strategy-factory.graph.json"),
            "aiask-quant-core": str(out_dir / "aiask-quant-core.graph.json"),
            "desktop": str(out_dir / "desktop.graph.json"),
            "root-runners": str(out_dir / "root-runners.graph.json"),
            "tests": str(out_dir / "tests.graph.json"),
            "docs": str(out_dir / "docs.graph.json"),
            "cross_package_edges": str(out_dir / "cross-package-edges.json"),
            "endpoint_map": str(out_dir / "endpoint-map.json"),
        },
    }
    (out_dir / "CURATED_SUMMARY.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_markdown_report(out_dir / "CURATED_REPORT.md", report)
    if args.quiet:
        print(
            json.dumps(
                {
                    "original": report["original"],
                    "core": report["core"],
                    "endpoint_map": {
                        key: report["endpoint_map"][key]
                        for key in ("endpoint_count", "matched_count", "server_only_count", "desktop_only_count")
                    },
                    "outputs": report["outputs"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
