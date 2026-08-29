"""Neutral entry point for the AIASK data-provider MCP server.

Delegates to the backing implementation so ``aiask-data-mcp`` and the legacy
``akshare-mcp`` console script start the same process. No behaviour is
reimplemented here — a second entry point that drifted from the first would be
worse than one badly named entry point.
"""

from __future__ import annotations

import sys
from typing import Any

from . import BackingImplementationMissing, resolve


def main(argv: list[str] | None = None) -> Any:
    """Start the data-provider MCP server."""
    try:
        server = resolve("server")
    except BackingImplementationMissing as exc:
        print(f"aiask-data-mcp: {exc}", file=sys.stderr)
        return 1

    entry = getattr(server, "main", None)
    if not callable(entry):
        print(
            "aiask-data-mcp: the backing provider exposes no server.main()",
            file=sys.stderr,
        )
        return 1

    # Forward argv only when the backing entry point accepts it, so this facade
    # keeps working whether or not that signature changes.
    import inspect

    try:
        accepts_argv = bool(inspect.signature(entry).parameters)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        accepts_argv = False

    return entry(argv) if accepts_argv and argv is not None else entry()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
