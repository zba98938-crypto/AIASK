"""Bundle shape, plus the drift guard between the two skill trees.

``skills/`` is canonical and this bundle is its mirror. Both are real directories
that were kept in step by hand, with no sync step and nothing comparing them, so
editing one and forgetting the other left the suite green. The drift test below
closes that gap.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from aiask_skills import PROTOCOL_VERSION, bundle_path, discover, skill_paths


#: ``skills/`` is the source of truth; the bundle ships a copy inside the wheel.
CANONICAL_TREE = Path(__file__).resolve().parents[3] / "skills" / "finance"


def _skill_dirs(root: Path) -> set[str]:
    """Names of the skill directories under a tree, keyed by their SKILL.md."""
    return {path.parent.name for path in root.glob("*/SKILL.md")}


def _digests(root: Path) -> dict[str, str]:
    """Map each skill file's relative path to its content hash, ignoring build noise.

    Only walks directories that hold a SKILL.md, so the bundle's packaging
    ``__init__.py`` is not mistaken for missing content on the canonical side.
    """
    digests: dict[str, str] = {}
    for name in _skill_dirs(root):
        for path in sorted((root / name).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            digests[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def test_bundle_contains_versioned_host_neutral_skills() -> None:
    paths = skill_paths()
    assert len(paths) >= 19
    assert bundle_path().is_dir()
    records = discover()
    assert len(records) == len(paths)
    assert all(item["protocol_version"] == PROTOCOL_VERSION for item in records)
    assert all(item["name"] for item in records)
    assert any(isinstance(item["capabilities"], list) and item["capabilities"] for item in records)


@pytest.mark.skipif(
    not CANONICAL_TREE.is_dir(),
    reason="canonical skills/ tree is absent, as in an installed wheel",
)
def test_the_bundle_ships_the_same_skills_as_the_canonical_tree() -> None:
    canonical = _skill_dirs(CANONICAL_TREE)
    mirrored = _skill_dirs(bundle_path())
    assert mirrored - canonical == set(), "bundle has skills missing from skills/"
    assert canonical - mirrored == set(), "skills/ has skills missing from the bundle"


@pytest.mark.skipif(
    not CANONICAL_TREE.is_dir(),
    reason="canonical skills/ tree is absent, as in an installed wheel",
)
def test_no_skill_file_has_drifted_between_the_two_trees() -> None:
    canonical = _digests(CANONICAL_TREE)
    mirrored = _digests(bundle_path())
    assert sorted(mirrored) == sorted(canonical), "the two trees hold different files"
    diverged = sorted(path for path, digest in canonical.items() if mirrored[path] != digest)
    assert diverged == [], f"content differs between skills/ and the bundle: {diverged}"
