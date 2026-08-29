"""Compatibility wrapper for the shared Strategy Factory JSON compactor."""

from __future__ import annotations

import runpy
from pathlib import Path


_SHARED_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "aiask-quant-core"
    / "scripts"
    / "compact_strategy_factory_json.py"
)


def __getattr__(name: str):
    namespace = runpy.run_path(str(_SHARED_SCRIPT))
    try:
        return namespace[name]
    except KeyError as exc:
        raise AttributeError(name) from exc


def main() -> None:
    runpy.run_path(str(_SHARED_SCRIPT), run_name="__main__")


if __name__ == "__main__":
    main()
