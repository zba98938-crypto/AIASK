#!/usr/bin/env python
"""Export the live Agent HTTP route set to ``reports/code-graph/live-routes.json``.

The endpoint-drift gate used to compare two committed static files against
each other, which made it impossible to ever report new server-side drift.
This exporter produces the ground-truth route list by importing the actual
FastAPI app (``aiask_agent.server.create_app``) and dumping every
``METHOD /path`` it registers, so the drift gate can check the allowlist
against the real, current HTTP surface instead of a stale snapshot.

Usage:
    python scripts/code_graph/export_live_routes.py
    python scripts/code_graph/export_live_routes.py --out <path>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "reports" / "code-graph" / "live-routes.json"

# FastAPI framework-provided introspection routes; these are not part of the
# application's own endpoint contract and are excluded from the drift gate.
FRAMEWORK_PREFIXES = ("/docs", "/redoc", "/openapi")


def _package_paths() -> list[str]:
    here = Path(__file__).resolve()
    out = []
    for anchor in ("agent", "aiask-quant-core", "akshare-mcp"):
        out.append(str(here.parents[2] / "packages" / anchor / "src"))
    return out


def export_live_routes(out_path: Path) -> dict:
    import os
    for p in _package_paths():
        if p not in os.environ.get("PYTHONPATH", "").split(os.pathsep):
            os.environ["PYTHONPATH"] = p + os.pathsep + os.environ.get("PYTHONPATH", "")
    for p in _package_paths():
        if p not in sys.path:
            sys.path.insert(0, p)

    from aiask_agent.server import create_app

    app = create_app()
    routes: list[dict] = []
    for r in app.routes:
        path = getattr(r, "path", "")
        if not path:
            continue
        for m in sorted(set(getattr(r, "methods", None) or [])):
            routes.append({"method": m, "path": path})

    application = [r for r in routes if not any(r["path"].startswith(p) for p in FRAMEWORK_PREFIXES)]
    framework = [r for r in routes if any(r["path"].startswith(p) for p in FRAMEWORK_PREFIXES)]

    payload = {
        "route_count": len(application),
        "application_routes": application,
        "framework_routes": framework,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export the live Agent HTTP route set")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)
    payload = export_live_routes(args.out)
    print(f"exported {payload['route_count']} application routes -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
