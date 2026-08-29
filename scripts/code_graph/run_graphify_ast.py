import argparse
import json
from pathlib import Path

from graphify.analyze import god_nodes, surprising_connections, suggest_questions
from graphify.build import build_from_json
from graphify.cluster import cluster, score_all
from graphify.detect import detect
from graphify.export import to_html, to_json
from graphify.extract import _get_extractor, extract
from graphify.report import generate


def _community_labels(communities: dict[int, list[str]], graph) -> dict[int, str]:
    labels: dict[int, str] = {}
    for cid, nodes in communities.items():
        best = ""
        for node_id in nodes:
            label = str(graph.nodes[node_id].get("label") or node_id)
            if not best or len(label) < len(best):
                best = label
        labels[cid] = best or f"Community {cid}"
    return labels


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a graphify AST-only evaluation on a bounded AIASK corpus."
    )
    parser.add_argument("--corpus", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    corpus = Path(args.corpus).resolve()
    graph_out = Path(args.out).resolve()
    graph_out.mkdir(parents=True, exist_ok=True)

    detection = detect(corpus)
    code_files = [Path(p) for p in detection.get("files", {}).get("code", [])]
    for doc_file in detection.get("files", {}).get("document", []):
        p = Path(doc_file)
        if _get_extractor(p) is not None:
            code_files.append(p)

    result = extract(code_files, cache_root=corpus, parallel=False)
    raw_graph = {
        "nodes": result.get("nodes", []),
        "edges": result.get("edges", []),
        "links": result.get("edges", []),
        "hyperedges": result.get("hyperedges", []),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "directed": True,
        "meta": {
            "mode": "ast_only_internal_eval",
            "corpus": str(corpus),
            "code_files": len(code_files),
            "detected": {k: len(v) for k, v in detection.get("files", {}).items()},
        },
    }
    raw_path = graph_out / "graph.raw.json"
    raw_path.write_text(json.dumps(raw_graph, ensure_ascii=False, indent=2), encoding="utf-8")

    graph = build_from_json(result, directed=True, root=corpus)
    communities = cluster(graph)
    cohesion = score_all(graph, communities)
    labels = _community_labels(communities, graph)
    try:
        gods = god_nodes(graph)
    except Exception:
        gods = []
    try:
        surprises = surprising_connections(graph, communities)
    except Exception:
        surprises = []
    try:
        questions = suggest_questions(graph, communities, labels)
    except Exception:
        questions = []

    to_json(graph, communities, str(graph_out / "graph.json"), force=True)
    try:
        to_html(graph, communities, str(graph_out / "graph.html"))
    except Exception as exc:
        (graph_out / "graph_html_error.txt").write_text(str(exc), encoding="utf-8")
    try:
        report = generate(
            graph,
            communities,
            cohesion,
            labels,
            gods,
            surprises,
            detection,
            {"input": 0, "output": 0, "cost": 0.0},
            str(corpus),
            suggested_questions=questions,
        )
        (graph_out / "GRAPH_REPORT.md").write_text(report, encoding="utf-8")
    except Exception as exc:
        (graph_out / "report_error.txt").write_text(str(exc), encoding="utf-8")

    summary = {
        "corpus": str(corpus),
        "graph_out": str(graph_out),
        "detected": raw_graph["meta"]["detected"],
        "code_files_extracted": len(code_files),
        "raw_nodes": len(result.get("nodes", [])),
        "raw_edges": len(result.get("edges", [])),
        "graph_nodes": graph.number_of_nodes(),
        "graph_edges": graph.number_of_edges(),
        "communities": len(communities),
        "top_god_nodes": gods[:10] if isinstance(gods, list) else [],
        "top_surprises": surprises[:10] if isinstance(surprises, list) else [],
        "suggested_questions": questions[:10] if isinstance(questions, list) else [],
        "outputs": {
            "graph_json": str(graph_out / "graph.json"),
            "raw_graph_json": str(raw_path),
            "graph_html": str(graph_out / "graph.html"),
            "report": str(graph_out / "GRAPH_REPORT.md"),
        },
    }
    (graph_out / "EVAL_SUMMARY.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
