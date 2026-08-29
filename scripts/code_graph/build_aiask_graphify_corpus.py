import argparse
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any


INCLUDE_PATHS = (
    "AGENT.md",
    "Makefile",
    "pytest.ini",
    "run_all_factories.py",
    "run_strategy_factory.py",
    "run_factor_mining_factory.py",
    "run_incubation_factory.py",
    "run_signal_tracker.py",
    "packages/agent/pyproject.toml",
    "packages/agent/src",
    "packages/agent/tests",
    "packages/akshare-mcp/pyproject.toml",
    "packages/akshare-mcp/src/akshare_mcp",
    "packages/akshare-mcp/tests",
    "packages/strategy-factory/pyproject.toml",
    "packages/strategy-factory/src",
    "packages/strategy-factory/tests",
    "packages/aiask-quant-core/pyproject.toml",
    "packages/aiask-quant-core/src",
    "desktop/package.json",
    "desktop/vite.config.ts",
    "desktop/tsconfig.json",
    "desktop/src",
    "desktop/tests",
    "docs/README.md",
    "docs/architecture",
    "docs/plans",
    "docs/desktop",
    "docs/event-driven",
    "docs/factor-mining",
    "docs/incubation-factory",
    "docs/strategy-factory",
)

ALLOWED_SUFFIXES = {
    ".cfg",
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rs",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

EXCLUDED_DIR_NAMES = {
    ".aiask_backups",
    ".benchmarks",
    ".claude",
    ".codex",
    ".git",
    ".kiro",
    ".mcp_cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "artifacts",
    "cache",
    "data",
    "dist",
    "logs",
    "node_modules",
    "playwright-report",
    "red-team-reports",
    "reports",
    "target",
    "test-results",
    "vendor",
    "venv",
}

EXCLUDED_FILE_PREFIXES = (
    ".env",
    ".DS_Store",
)

MAX_FILE_BYTES = 2_000_000


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _assert_safe_clean_target(target: Path, workspace: Path) -> None:
    resolved = target.resolve()
    workspace = workspace.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if resolved == workspace or resolved == workspace.parent or resolved == Path.home():
        raise ValueError(f"Refusing to clean unsafe output directory: {resolved}")
    if _is_inside(resolved, workspace):
        rel = resolved.relative_to(workspace)
        if len(rel.parts) < 2 or rel.parts[0] != "reports":
            raise ValueError(f"Refusing to clean workspace directory outside reports/*: {resolved}")
        return
    if _is_inside(resolved, temp_root) and resolved.name == "corpus":
        return
    raise ValueError(f"Refusing to clean output outside workspace reports or temp corpus: {resolved}")


def _is_allowed_file(path: Path, workspace: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if any(part in EXCLUDED_DIR_NAMES for part in path.relative_to(workspace).parts[:-1]):
        return False
    if path.name.startswith(EXCLUDED_FILE_PREFIXES):
        return False
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        return False
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return False
    except OSError:
        return False
    return True


def _iter_files(root: Path, workspace: Path):
    if root.is_file():
        if _is_allowed_file(root, workspace):
            yield root
        return
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        if _is_allowed_file(path, workspace):
            yield path


def build_corpus(workspace: Path, out_dir: Path, *, clean: bool = False) -> dict[str, Any]:
    workspace = workspace.resolve()
    out_dir = out_dir.resolve()
    if clean and out_dir.exists():
        _assert_safe_clean_target(out_dir, workspace)
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    copied: list[dict[str, Any]] = []
    skipped_missing: list[str] = []
    seen: set[str] = set()
    total_bytes = 0

    for item in INCLUDE_PATHS:
        source = (workspace / item).resolve()
        if not source.exists():
            skipped_missing.append(item)
            continue
        if not _is_inside(source, workspace):
            continue
        for file_path in _iter_files(source, workspace):
            rel = _relative(file_path, workspace)
            if rel in seen:
                continue
            seen.add(rel)
            dest = out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(file_path, dest)
            size = file_path.stat().st_size
            total_bytes += size
            copied.append({"path": rel, "bytes": size})

    summary = {
        "workspace": str(workspace),
        "corpus": str(out_dir),
        "files": len(copied),
        "bytes": total_bytes,
        "include_paths": list(INCLUDE_PATHS),
        "skipped_missing": skipped_missing,
        "copied": copied,
    }
    (out_dir / "CORPUS_MANIFEST.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a bounded, secret-safe AIASK corpus for Graphify.")
    parser.add_argument("--workspace", default=Path(__file__).resolve().parents[2])
    parser.add_argument("--out", required=True)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()

    summary = build_corpus(Path(args.workspace), Path(args.out), clean=args.clean)
    print(json.dumps({k: v for k, v in summary.items() if k != "copied"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
