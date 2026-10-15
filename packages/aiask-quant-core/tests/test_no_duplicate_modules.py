"""Critical measurement modules must have exactly one implementation.

Why this exists
---------------
``services/_validation_support.py`` in akshare-mcp was a byte-identical
848-line copy of ``aiask_quant_core/_validation_support.py``; they differed
only by a leading BOM. Any fix applied to one copy would silently miss the
other. The same shape produced four competing stress-test implementations that
disagree on the sign convention of the number they return.

Two guards, both structural rather than textual:

1. A curated list of measurement modules must resolve to a single file.
2. No two source files in different packages may be identical once the BOM and
   line endings are normalised. That is the general form of the defect above.

This is an import-graph/inventory assertion, not a source-substring assertion:
it compares real files on disk and does not care how the code is written.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PACKAGES_SRC = ROOT / "packages"

# Modules that compute a reported metric, a gate verdict, or a threshold.
# A second copy of any of these is a drift hazard, not a convenience.
SINGLE_SOURCE_MODULES = (
    "_validation_support",
)

# Files too small to carry an implementation. A duplicate empty or near-empty
# file is noise; the defect this guards against is a copied body of code.
MIN_COMPARABLE_BYTES = 500


def _iter_package_sources():
    for path in sorted(PACKAGES_SRC.glob("*/src/**/*.py")):
        if path.name == "__init__.py":
            continue
        yield path


def _normalised_bytes(path: Path) -> bytes:
    """Read source with the BOM and line endings neutralised."""

    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def test_measurement_modules_have_a_single_implementation() -> None:
    found: dict[str, list[str]] = defaultdict(list)
    for path in _iter_package_sources():
        if path.stem in SINGLE_SOURCE_MODULES:
            found[path.stem].append(str(path.relative_to(ROOT)))

    duplicates = {name: paths for name, paths in found.items() if len(paths) > 1}
    assert not duplicates, (
        "these measurement modules exist in more than one package; keep one and "
        f"import it from the other side: {duplicates}"
    )
    for name in SINGLE_SOURCE_MODULES:
        assert found.get(name), f"{name} is not present anywhere; update this list"


def test_no_identical_source_files_across_packages() -> None:
    by_digest: dict[str, list[str]] = defaultdict(list)
    for path in _iter_package_sources():
        payload = _normalised_bytes(path)
        if len(payload) < MIN_COMPARABLE_BYTES:
            continue
        by_digest[hashlib.sha256(payload).hexdigest()].append(
            str(path.relative_to(ROOT))
        )

    duplicated = {digest: paths for digest, paths in by_digest.items() if len(paths) > 1}
    assert not duplicated, (
        "identical source files found in more than one place (the BOM/line-ending "
        f"normalised copies count as identical): {list(duplicated.values())}"
    )
