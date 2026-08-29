#!/usr/bin/env python3
"""Plan (and optionally perform) the AIASK standalone repository extraction.

Phase 6 of the extraction plan. This script is **dry-run by default**: it prints
the plan and validates preconditions without touching anything. Creating the new
repository requires ``--execute`` plus an explicit ``--target`` directory.

Two history strategies:

``--history=none`` (default)
    Copy the tracked tree into a fresh repository with a single initial commit.
    Simple, no extra tooling, loses per-file history.

``--history=preserve``
    Requires ``git-filter-repo``. Clones this repository, rewrites it down to
    ``integrations/aiask`` with paths lifted to the root, keeping the 13 commits
    that touched the tree.

Neither mode modifies the source repository.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


AIASK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = AIASK_ROOT.parents[1]
SUBDIR = "integrations/aiask"

#: Install order; a consumer must never precede its dependency.
PACKAGE_ORDER = (
    "packages/aiask-quant-core",
    "packages/strategy-factory",
    "packages/akshare-mcp",
    "packages/data-mcp",
    "packages/finance-mcp-servers",
    "packages/aiask-skills",
    "packages/reference-host",
    "packages/agent",
)

#: Paths that must exist in the extracted tree for it to be usable.
REQUIRED_PATHS = (
    "pyproject.toml",
    "docs/architecture/runtime-boundary.json",
    "scripts/run_mcp_stdio.py",
    "scripts/factories/run_strategy_factory.py",
    *PACKAGE_ORDER,
)

#: Directories that belong to the host, not to AIASK. Excluded from the copy.
HOST_OWNED = ("hermes-plugin",)

#: Individual files that only make sense inside the host repository.
#:
#: The extraction planner tests the planner itself, which does not travel with the
#: extracted tree — leaving them in produced 12 failures in a repository that was
#: otherwise entirely green, which is worse than no test at all.
HOST_ONLY_FILES = (
    "packages/strategy-factory/tests/test_extraction_planner.py",
)

#: Trees that live outside ``integrations/aiask`` in this repository but ship
#: with the standalone product. Mapped as ``host path -> path in the new repo``.
#:
#: ``skills/finance`` is the single authoritative Skills source. It sits at the
#: Hermes root for host discovery, so a subtree-only copy would silently produce
#: a standalone repository with no Skills at all. The stale duplicate under
#: ``.codex/skills`` is untracked and deliberately not used.
EXTERNAL_SOURCES: dict[str, str] = {
    "skills/finance": "skills/finance",
}

#: Untracked duplicates that must never be treated as a Skills source.
STALE_SKILL_COPIES = (".codex/skills",)


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def tracked_files() -> list[str]:
    """Repository-relative paths git tracks under the AIASK subtree."""
    out = _git("ls-files", "-z", SUBDIR)
    return [name for name in out.split("\0") if name]


def relative_files() -> list[str]:
    prefix = f"{SUBDIR}/"
    return [name[len(prefix) :] for name in tracked_files() if name.startswith(prefix)]


def external_files() -> dict[str, str]:
    """Tracked files from outside the subtree, mapped to their target path.

    Keys are repository-relative source paths; values are paths in the new
    repository. Only git-tracked files are included, matching the subtree rule.
    """
    mapping: dict[str, str] = {}
    for host_path, target_root in EXTERNAL_SOURCES.items():
        out = _git("ls-files", "-z", host_path)
        for name in out.split("\0"):
            if not name:
                continue
            suffix = name[len(host_path) :].lstrip("/")
            mapping[name] = f"{target_root}/{suffix}" if suffix else target_root
    return mapping


def commit_count() -> int:
    return len([line for line in _git("log", "--oneline", "--", SUBDIR).splitlines() if line])


def check_preconditions() -> dict[str, object]:
    """Validate that the tree is in a state worth extracting."""
    problems: list[str] = []
    warnings: list[str] = []

    files = relative_files()
    if not files:
        problems.append(f"no tracked files under {SUBDIR}")

    present = set(files)
    for required in REQUIRED_PATHS:
        if not any(name == required or name.startswith(f"{required}/") for name in present):
            problems.append(f"required path missing from the tracked tree: {required}")

    # A dirty tree means the copy would not match any commit.
    if _git("status", "--porcelain", SUBDIR).strip():
        warnings.append(
            f"{SUBDIR} has uncommitted changes; extract from a clean tree so the "
            "new repository corresponds to a known commit"
        )

    host_owned_present = sorted(
        name for name in files if any(name.startswith(f"{d}/") for d in HOST_OWNED)
    )

    # Skills are the one product surface that lives outside the subtree. A
    # subtree-only copy yields a repository with no Skills, so treat an empty
    # external mapping as a blocker rather than letting it pass quietly.
    external = external_files()
    skill_manifests = [
        target for target in external.values() if target.endswith("/SKILL.md")
    ]
    if not skill_manifests:
        problems.append(
            "no Skills resolved from EXTERNAL_SOURCES; the extracted repository "
            "would ship without any skill bundle"
        )

    for stale in STALE_SKILL_COPIES:
        stale_path = AIASK_ROOT / stale
        if stale_path.is_dir():
            warnings.append(
                f"{SUBDIR}/{stale} exists but is untracked and stale; "
                "skills/finance is the authoritative source and is used instead"
            )

    return {
        "tracked_file_count": len(files),
        "external_file_count": len(external),
        "skill_count": len(skill_manifests),
        "commit_count": commit_count(),
        "problems": problems,
        "warnings": warnings,
        "host_owned_excluded": len(host_owned_present),
        "package_order": list(PACKAGE_ORDER),
    }


def build_plan(target: Path, history: str) -> dict[str, object]:
    checks = check_preconditions()
    return {
        "source_repo": str(REPO_ROOT),
        "source_subdir": SUBDIR,
        "target": str(target),
        "history_strategy": history,
        "install_order": list(PACKAGE_ORDER),
        "excluded_host_owned": list(HOST_OWNED),
        "external_sources": dict(EXTERNAL_SOURCES),
        **checks,
        "notes": [
            "Package interdependencies use relative [tool.uv.sources] paths, so no "
            "package configuration changes are required by the move.",
            "node_modules is git-ignored; the tracked tree is small.",
            "hermes-plugin/ stays with the host: it exists to install AIASK into "
            "Hermes profiles and has no meaning standalone.",
            "skills/finance lives at the host root for discovery but ships with the "
            "product; it is copied in as the single authoritative Skills source.",
        ],
    }


def _copy_tracked(target: Path) -> dict[str, int]:
    """Copy the subtree plus every external source into ``target``."""
    subtree = 0
    for relative in relative_files():
        if any(relative.startswith(f"{d}/") for d in HOST_OWNED):
            continue
        if relative in HOST_ONLY_FILES:
            continue
        source = AIASK_ROOT / relative
        if not source.is_file():
            continue
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        subtree += 1

    external = 0
    for host_relative, target_relative in external_files().items():
        source = REPO_ROOT / host_relative
        if not source.is_file():
            continue
        destination = target / target_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        external += 1

    return {"subtree": subtree, "external": external, "total": subtree + external}


def execute_none(target: Path) -> dict[str, object]:
    """Fresh repository, single initial commit, no prior history."""
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise SystemExit(f"target is not empty: {target}")

    copied = _copy_tracked(target)
    baseline = _git("rev-parse", "--short", "HEAD").strip()

    _git("init", "-q", cwd=target)
    _git("add", "-A", cwd=target)
    _git(
        "commit",
        "-q",
        "-m",
        f"chore: extract AIASK standalone tree from host {baseline}",
        cwd=target,
    )
    return {
        "copied_files": copied["total"],
        "copied_from_subtree": copied["subtree"],
        "copied_from_external": copied["external"],
        "baseline": baseline,
        "history": "single commit",
    }


def execute_preserve(target: Path) -> dict[str, object]:
    """Rewrite a clone down to the subtree, keeping its commits."""
    if shutil.which("git-filter-repo") is None:
        raise SystemExit(
            "git-filter-repo is required for --history=preserve. "
            "Install it, or use --history=none."
        )
    if target.exists() and any(target.iterdir()):
        raise SystemExit(f"target is not empty: {target}")

    _git("clone", "--no-local", str(REPO_ROOT), str(target))
    # --subdirectory-filter lifts integrations/aiask/* to the repository root and
    # drops every commit that did not touch the subtree.
    subprocess.run(
        ["git-filter-repo", "--subdirectory-filter", SUBDIR],
        cwd=str(target),
        check=True,
    )
    for host_dir in HOST_OWNED:
        subprocess.run(
            ["git-filter-repo", "--invert-paths", "--path", host_dir, "--force"],
            cwd=str(target),
            check=True,
        )

    # --subdirectory-filter keeps only the subtree, so external sources (Skills)
    # are absent from the rewritten history. Add them as a follow-up commit
    # rather than leaving the repository silently Skills-less.
    external = 0
    for host_relative, target_relative in external_files().items():
        source = REPO_ROOT / host_relative
        if not source.is_file():
            continue
        destination = target / target_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        external += 1
    if external:
        _git("add", "-A", cwd=target)
        _git(
            "commit",
            "-q",
            "-m",
            "chore: add skill bundle from the host tree",
            cwd=target,
        )

    return {
        "history": "preserved",
        "commits": commit_count(),
        "external_added": external,
        "note": (
            "external sources have no prior history in the rewrite; they were "
            "added as one follow-up commit"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", help="destination directory for the new repository")
    parser.add_argument(
        "--history",
        choices=("none", "preserve"),
        default="none",
        help="none: single initial commit (default). preserve: rewrite with git-filter-repo.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually create the repository; omit for a dry run",
    )
    parser.add_argument("--json", action="store_true", help="emit the plan as JSON")
    args = parser.parse_args(argv)

    target = Path(args.target).expanduser().resolve() if args.target else Path("<target>")
    plan = build_plan(target, args.history)

    if args.json:
        print(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"source      : {plan['source_repo']}/{SUBDIR}")
        print(f"target      : {plan['target']}")
        print(f"history     : {plan['history_strategy']}")
        print(f"files       : {plan['tracked_file_count']} tracked in the subtree")
        print(
            f"external    : {plan['external_file_count']} files from "
            f"{', '.join(EXTERNAL_SOURCES)} ({plan['skill_count']} skills)"
        )
        print(f"commits     : {plan['commit_count']} touching the subtree")
        print(f"excluded    : {', '.join(HOST_OWNED)} ({plan['host_owned_excluded']} files)")
        for warning in plan["warnings"]:
            print(f"WARNING     : {warning}")
        for problem in plan["problems"]:
            print(f"BLOCKER     : {problem}")

    if plan["problems"]:
        return 1
    if not args.execute:
        if not args.json:
            print("\ndry run only; pass --execute --target <dir> to create the repository")
        return 0
    if not args.target:
        raise SystemExit("--execute requires --target")

    outcome = execute_none(target) if args.history == "none" else execute_preserve(target)
    print(json.dumps(outcome, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
