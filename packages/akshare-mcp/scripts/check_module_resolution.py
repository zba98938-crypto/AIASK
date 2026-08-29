#!/usr/bin/env python3
from __future__ import annotations

import importlib
import sys
from pathlib import Path


THIS_DIR = Path(__file__).resolve().parent
SRC_DIR = THIS_DIR.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


EXPECTED_PACKAGE_TARGETS = {
    "akshare_mcp.tools.managers": "akshare_mcp/tools/managers/__init__.py",
    "akshare_mcp.tools.market": "akshare_mcp/tools/market/__init__.py",
    "akshare_mcp.tools.news": "akshare_mcp/tools/news/__init__.py",
    "akshare_mcp.tools.semantic": "akshare_mcp/tools/semantic/__init__.py",
    "akshare_mcp.services.backtest": "akshare_mcp/services/backtest/__init__.py",
    "akshare_mcp.services.factor_calculator": "akshare_mcp/services/factor_calculator/__init__.py",
    "akshare_mcp.data_source": "akshare_mcp/data_source/__init__.py",
}


def main() -> int:
    failures: list[str] = []

    for module_name, expected_suffix in EXPECTED_PACKAGE_TARGETS.items():
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            failures.append(f"{module_name}: import failed: {exc}")
            continue

        module_file = str(Path(getattr(module, "__file__", "")).as_posix())
        if not module_file.endswith(expected_suffix):
            failures.append(
                f"{module_name}: resolved to {module_file}, expected suffix {expected_suffix}"
            )
        else:
            print(f"[OK] {module_name} -> {module_file}")

    if failures:
        for line in failures:
            print(f"[ERROR] {line}", file=sys.stderr)
        return 1

    print("[OK] module resolution check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
