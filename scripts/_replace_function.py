#!/usr/bin/env python3
"""Replace a top-level function's source with given text (used once, by hand).

Not a general-purpose tool: it exists so the akshare cleanup could replace whole
akshare-only functions without hand-copying their boundaries, and it verifies the
result parses before writing.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


def replace(path: str, name: str, stub_path: str) -> None:
    source = Path(path).read_bytes().decode("utf-8-sig")
    lines = source.splitlines()
    tree = ast.parse(source)
    target = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            target = node
            break
    if target is None:
        raise SystemExit(f"{name} not found at module level in {path}")

    first = target.lineno
    # Include decorators above the def.
    for decorator in target.decorator_list:
        first = min(first, decorator.lineno)
    last = target.end_lineno

    stub = Path(stub_path).read_text(encoding="utf-8").rstrip("\n").splitlines()
    out = lines[: first - 1] + stub + lines[last:]
    text = "\n".join(out) + ("\n" if source.endswith("\n") else "")
    ast.parse(text)
    Path(path).write_bytes(text.encode("utf-8"))


if __name__ == "__main__":
    replace(sys.argv[1], sys.argv[2], sys.argv[3])
