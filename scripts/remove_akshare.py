#!/usr/bin/env python3
"""Remove the akshare code paths.

Why this exists
---------------
``akshare`` is not declared in any ``pyproject.toml``, so installing this project
never provided it, yet 26 modules imported it behind ``except ImportError``. Every
one of those branches has been dead since the tree was extracted, and because they
degrade to an empty list or ``None`` rather than an error, nothing said so.

What it does:

1. deletes the module-level ``try: import akshare as ak / except: ak = None`` guard;
2. unwraps ``if <condition that is True when ak is None>:`` — that body is what
   actually runs today, so it becomes unconditional;
3. deletes ``if <condition that is False when ak is None>:`` together with the
   akshare call it guarded, which is unreachable code;
4. rewrites ``x if ak is not None else y`` to ``y``.

Conditions it cannot decide are reported rather than guessed at, and any file
whose rewrite does not parse is left alone. The suite afterwards is the behaviour
check: the guarded branches were already the live ones, so green means only dead
code left.

Edits are applied to the original lines. ``ast.unparse`` would reformat the module
and bury a small semantic change inside a whole-file diff.

Usage::

    python scripts/remove_akshare.py --dry-run
    python scripts/remove_akshare.py --apply
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET = "akshare"
BINDING = "ak"


def _mentions_ak(node: ast.AST) -> bool:
    return any(isinstance(n, ast.Name) and n.id == BINDING for n in ast.walk(node))


#: Statements after which the rest of the block cannot run.
TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def _unreachable_ranges(tree: ast.AST) -> list[tuple[int, int]]:
    """Return ``(first_line, last_line)`` spans that sit after a terminator.

    Unwrapping ``if ak is None: return []`` turns that early exit into the only
    path, leaving the old akshare body unreachable below it. Those lines are the
    dead code this whole exercise is about, so they go too.
    """

    blocks: list[list[ast.stmt]] = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list) and block:
                blocks.append(block)

    spans: list[tuple[int, int]] = []
    for block in blocks:
        for index, statement in enumerate(block[:-1]):
            if not isinstance(statement, TERMINATORS):
                continue
            dead = block[index + 1 :]
            if dead and _mentions_ak(ast.Module(body=dead, type_ignores=[])):
                spans.append((dead[0].lineno, dead[-1].end_lineno or dead[-1].lineno))
            break
    return spans


def _is_akshare_import(node: ast.stmt) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == TARGET for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        return node.module == TARGET or (node.module or "").startswith(TARGET + ".")
    return False


def _tri_with_ak_none(node: ast.AST) -> bool | None:
    """Evaluate a test as if ``ak`` were ``None``.

    True / False when the answer is certain, ``None`` when the expression depends
    on something else too and must be left for a human.
    """

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _tri_with_ak_none(node.operand)
        return None if inner is None else (not inner)

    if isinstance(node, ast.BoolOp):
        values = [_tri_with_ak_none(value) for value in node.values]
        if isinstance(node.op, ast.And):
            if any(value is False for value in values):
                return False
            if all(value is True for value in values):
                return True
            return None
        if isinstance(node.op, ast.Or):
            if any(value is True for value in values):
                return True
            if all(value is False for value in values):
                return False
            return None
        return None

    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left, right, op = node.left, node.comparators[0], node.ops[0]
        if (
            isinstance(left, ast.Name)
            and left.id == BINDING
            and isinstance(right, ast.Constant)
            and right.value is None
        ):
            if isinstance(op, ast.Is):
                return True
            if isinstance(op, ast.IsNot):
                return False
        return None

    if isinstance(node, ast.Name) and node.id == BINDING:
        # A bare ``if ak:`` is False when ak is None.
        return False
    return None


class _Rewriter(ast.NodeTransformer):
    def __init__(self) -> None:
        self.unwrap: list[ast.If] = []
        self.delete: list[ast.If] = []
        self.ternaries: list[ast.IfExp] = []
        self.undecided: list[int] = []

    def visit_If(self, node: ast.If) -> object:
        self.generic_visit(node)
        if not _mentions_ak(node.test):
            return node
        verdict = _tri_with_ak_none(node.test)
        if verdict is True:
            self.unwrap.append(node)
            return node.body
        if verdict is False and not node.orelse:
            self.delete.append(node)
            return []
        self.undecided.append(node.lineno)
        return node

    def visit_IfExp(self, node: ast.IfExp) -> object:
        self.generic_visit(node)
        if not _mentions_ak(node.test):
            return node
        verdict = _tri_with_ak_none(node.test)
        if verdict is True:
            self.ternaries.append(node)
            return node.body
        if verdict is False:
            self.ternaries.append(node)
            return node.orelse
        return node


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _render(lines, drop, dedent, replacements, source, had_bom) -> str:
    out: list[str] = []
    for number, line in enumerate(lines, start=1):
        if number in drop:
            if number in replacements:
                out.append(replacements[number])
            continue
        if number in dedent:
            remove = min(dedent[number], len(_indent_of(line)))
            line = line[remove:]
        out.append(line)
    text = "\n".join(out) + ("\n" if source.endswith("\n") else "")
    if had_bom:
        text = "\ufeff" + text
    return text


def transform(path: Path, root: Path):
    """Return ``(new_source, stats, undecided_lines)``."""

    raw = path.read_bytes()
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    source = raw.decode("utf-8-sig")
    lines = source.splitlines()
    tree = ast.parse(source)

    guards = [
        node
        for node in tree.body
        if isinstance(node, ast.Try) and any(_is_akshare_import(s) for s in node.body)
    ]
    rewriter = _Rewriter()
    rewriter.visit(tree)

    dead = _unreachable_ranges(tree)

    if not guards and not rewriter.unwrap and not rewriter.delete and not rewriter.ternaries and not dead:
        return None, {}, rewriter.undecided

    drop: set[int] = set()
    dedent: dict[int, int] = {}
    for node in guards + rewriter.delete:
        drop.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    for first, last in dead:
        drop.update(range(first, last + 1))
    for node in rewriter.unwrap:
        drop.add(node.lineno)
        first = node.body[0].lineno
        last = node.body[-1].end_lineno or node.body[-1].lineno
        header_indent = len(_indent_of(lines[node.lineno - 1]))
        body_indent = min(
            len(_indent_of(lines[index - 1])) for index in range(first, last + 1) if lines[index - 1].strip()
        )
        # The body moves up to the header's level, so it loses exactly the
        # difference between them. Using the absolute body indent would dedent it
        # to column zero and detach it from its enclosing block.
        strip = max(0, body_indent - header_indent)
        for index in range(first, last + 1):
            dedent[index] = strip

    def _parse(candidate: str) -> None:
        ast.parse(candidate.encode("utf-8").decode("utf-8-sig"))

    text = _render(lines, drop, dedent, {}, source, had_bom)
    try:
        _parse(text)
    except SyntaxError:
        # Deleting an `if` that was the only statement in its block leaves the
        # block empty. Retry with an explicit `pass` in its place instead of
        # giving up on the file.
        replacements = {
            node.lineno: _indent_of(lines[node.lineno - 1]) + "pass"
            for node in rewriter.delete
        }
        text = _render(lines, drop, dedent, replacements, source, had_bom)
        _parse(text)
    return text, {
        "guards": len(guards),
        "unwrapped": len(rewriter.unwrap),
        "deleted": len(rewriter.delete),
        "ternaries": len(rewriter.ternaries),
        "unreachable": len(dead),
    }, rewriter.undecided


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--root", default=str(REPO_ROOT))
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    totals = {"guards": 0, "unwrapped": 0, "deleted": 0, "ternaries": 0, "unreachable": 0}
    touched = 0
    undecided: list[str] = []
    for path in sorted(root.rglob("*.py")):
        parts = path.parts
        if ".venv" in parts or "__pycache__" in parts:
            continue
        try:
            new_source, stats, pending = transform(path, root)
        except SyntaxError as exc:
            print(f"SKIP (does not parse after rewrite): {path}: {exc}", file=sys.stderr)
            continue
        if pending:
            undecided.extend(f"{path}:{line}" for line in pending)
        if not stats:
            continue
        touched += 1
        for key, value in stats.items():
            totals[key] += value
        print(f"  {path.relative_to(root)}  {stats}")
        if args.apply and new_source is not None:
            path.write_bytes(new_source.encode("utf-8"))

    print()
    print(f"files: {touched}   {totals}")
    if undecided:
        print(f"left for manual review ({len(undecided)}):")
        for item in undecided[:40]:
            print("   ", item)
    if not args.apply:
        print("dry run; pass --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
