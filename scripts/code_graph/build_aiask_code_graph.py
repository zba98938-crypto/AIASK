import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _run(command: list[str], *, cwd: Path) -> None:
    print("+ " + " ".join(f'"{part}"' if " " in part else part for part in command), flush=True)
    subprocess.run(command, cwd=str(cwd), check=True)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_safe_clean_target(target: Path, workspace: Path) -> None:
    resolved = target.resolve()
    code_graph_root = workspace.resolve() / "reports" / "code-graph"
    if not _is_inside(resolved, code_graph_root) or resolved == code_graph_root:
        raise ValueError(f"Refusing to clean output outside reports/code-graph/*: {resolved}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the AIASK Graphify code graph and curated subgraphs.")
    parser.add_argument("--workspace", default=str(REPO_ROOT))
    parser.add_argument("--out", default="")
    parser.add_argument("--corpus", default="")
    parser.add_argument("--graph", default="", help="Existing Graphify graph.json to curate instead of extracting.")
    parser.add_argument("--graphify-package", default="graphifyy")
    parser.add_argument("--clean", action="store_true", help="Clean the selected output/corpus directories first.")
    parser.add_argument("--keep-corpus", action="store_true")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    run_dir = Path(args.out).resolve() if args.out else workspace / "reports" / "code-graph" / _timestamp()
    graphify_out = run_dir / "graphify-out"
    curated_out = run_dir / "curated"
    corpus = Path(args.corpus).resolve() if args.corpus else Path(tempfile.gettempdir()) / f"aiask-code-graph-{run_dir.name}" / "corpus"

    if args.clean and run_dir.exists():
        _assert_safe_clean_target(run_dir, workspace)
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    corpus_builder = SCRIPT_DIR / "build_aiask_graphify_corpus.py"
    graphify_runner = SCRIPT_DIR / "run_graphify_ast.py"
    curator = SCRIPT_DIR / "curate_aiask_graph.py"

    corpus_command = [sys.executable, str(corpus_builder), "--workspace", str(workspace), "--out", str(corpus)]
    if args.clean or not args.corpus:
        corpus_command.append("--clean")
    _run(corpus_command, cwd=workspace)

    if args.graph:
        graph_json = Path(args.graph).resolve()
    else:
        uvx = shutil.which("uvx")
        if not uvx:
            raise RuntimeError("uvx is required to run Graphify. Install uv/uvx or pass --graph to curate an existing graph.")
        _run(
            [
                uvx,
                "--from",
                args.graphify_package,
                "python",
                str(graphify_runner),
                "--corpus",
                str(corpus),
                "--out",
                str(graphify_out),
            ],
            cwd=workspace,
        )
        graph_json = graphify_out / "graph.json"

    _run(
        [
            sys.executable,
            str(curator),
            "--graph",
            str(graph_json),
            "--out",
            str(curated_out),
            "--workspace",
            str(workspace),
            "--quiet",
        ],
        cwd=workspace,
    )

    summary = {
        "workspace": str(workspace),
        "run_dir": str(run_dir),
        "corpus": str(corpus),
        "graph_json": str(graph_json),
        "curated": str(curated_out),
        "curated_summary": str(curated_out / "CURATED_SUMMARY.json"),
        "curated_report": str(curated_out / "CURATED_REPORT.md"),
        "endpoint_map": str(curated_out / "endpoint-map.json"),
    }
    curated_summary_path = curated_out / "CURATED_SUMMARY.json"
    if curated_summary_path.exists():
        curated_summary = _read_json(curated_summary_path)
        summary["counts"] = {
            "original": curated_summary.get("original"),
            "core": curated_summary.get("core"),
            "endpoint_map": {
                key: curated_summary.get("endpoint_map", {}).get(key)
                for key in ("endpoint_count", "matched_count", "server_only_count", "desktop_only_count")
            },
        }
    if not args.keep_corpus and not args.corpus:
        shutil.rmtree(corpus.parent, ignore_errors=True)
        summary["corpus_removed"] = True

    (run_dir / "RUN_SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
