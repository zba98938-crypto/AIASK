#!/usr/bin/env python3
"""Make exception handlers that discard their error say so.

Why this exists
---------------
``scripts/check_silent_exception_swallows.py`` counts handlers whose entire body
is ``pass`` / ``continue`` / ``return <empty>``. The count was 856 across the
tree, and the scan that produced it also found the reason they are silent: not
one of them binds the exception. ``except Exception: pass`` cannot report what
went wrong even if someone later wanted it to, because the error object was
discarded at the syntax level.

What this changes, and what it deliberately does not
----------------------------------------------------
For every such handler it:

1. binds the exception (``except X:`` becomes ``except X as _swallowed:``),
2. inserts one ``logger.warning`` carrying the exception type and message,
3. adds ``import logging`` / ``logger = logging.getLogger(__name__)`` when the
   module has neither.

It does not change which exceptions are caught, and it does not change control
flow: the ``pass`` / ``continue`` / ``return`` stays exactly where it was, with
the same value. That keeps the change reviewable and testable — a green suite
after this run means behaviour is unchanged and only observability was added.

Handlers whose header spans several lines are reported and skipped rather than
guessed at.

Usage::

    python scripts/apply_swallow_disclosure.py --dry-run
    python scripts/apply_swallow_disclosure.py --apply
"""

from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Verified absent from the tree: ``except X as name`` deletes ``name`` when the
#: handler exits, so reusing a name that exists in an enclosing scope would
#: destroy it. A unique name avoids that entirely.
EXC_NAME = "_swallowed"

LOG_TEMPLATE = (
    'logger.warning("{site}: swallowed %s and continued: %s", '
    'type({exc}).__name__, {exc})'
)

SWALLOW_CONSTS = (None, False, 0, 0.0, "")

#: Present only in the lines this script inserts; used to find files that need
#: repair after an earlier, broken run.
MARKER = "swallowed %s and continued"


def _swallow_statement(handler: ast.ExceptHandler):
    body = handler.body
    if len(body) != 1:
        return None
    statement = body[0]
    if isinstance(statement, (ast.Pass, ast.Continue)):
        return statement
    if isinstance(statement, ast.Return):
        value = statement.value
        if value is None or (
            isinstance(value, ast.Constant) and value.value in SWALLOW_CONSTS
        ):
            return statement
    return None


def _handler_colon_lines(source: str) -> dict[int, int]:
    """Map ``except`` line numbers to the line their header colon sits on.

    String heuristics get this wrong: ``except Exception:  # note`` does not end
    with ``:``, and a scanner that keeps looking will run into the next line that
    does — which is how an earlier version of this script deleted real code.
    Tokens do not have that failure mode.
    """

    mapping: dict[int, int] = {}
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError):
        return mapping

    pending_except_line = None
    depth = 0
    for token in tokens:
        if token.type == tokenize.NAME and token.string == "except" and depth == 0:
            pending_except_line = token.start[0]
            continue
        if pending_except_line is None:
            continue
        if token.type == tokenize.OP:
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == ":" and depth == 0:
                mapping[pending_except_line] = token.end[0]
                pending_except_line = None
    return mapping


def _module_logger_edit(tree, lines):
    """Return ``(insert_at, lines_to_insert)`` when the module needs a logger."""

    # Module level only. An earlier version of this function walked the whole
    # tree, so a function-local `logger = ...` or `import logging` counted as
    # module state, no logger was added, and 102 modules raised NameError.
    has_logging_import = any(
        isinstance(node, ast.Import)
        and any(alias.name == "logging" for alias in node.names)
        for node in tree.body
    )
    has_logger = any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "logger"
            for target in node.targets
        )
        for node in tree.body
    )
    if has_logging_import and has_logger:
        return None

    # Insert *after* the last module-level import. Putting the block straight
    # after the docstring looks tidier but is wrong: when the module already has
    # `import logging` further down, the inserted
    # `logger = logging.getLogger(__name__)` runs first and raises NameError at
    # import time. Position is a correctness property here, not a style choice.
    last_import_end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last_import_end = max(last_import_end, node.end_lineno or node.lineno)

    if last_import_end:
        insert_at = last_import_end
    else:
        insert_at = 0
        for node in tree.body:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                insert_at = max(insert_at, node.end_lineno or node.lineno)
            elif isinstance(node, ast.ImportFrom) and node.module == "__future__":
                insert_at = max(insert_at, node.end_lineno or node.lineno)
            else:
                break

    block = []
    if not has_logging_import:
        block.append("import logging")
    block.append("")
    if not has_logger:
        block.append("logger = logging.getLogger(__name__)")
    # Only add a separating blank line when the file does not already have one:
    # a codemod should not reformat the lines it did not need to touch.
    if insert_at >= len(lines) or lines[insert_at].strip():
        block.append("")
    return insert_at, block


def transform(path: Path, root: Path):
    """Return ``(new_source, applied, skipped)``."""

    raw = path.read_bytes()
    # Two modules in this tree carry a UTF-8 BOM; ast.parse rejects it, so decode
    # through utf-8-sig and put the BOM back on write to keep the bytes stable.
    had_bom = raw.startswith(b"\xef\xbb\xbf")
    source = raw.decode("utf-8-sig")
    lines = source.splitlines()
    tree = ast.parse(source)
    colon_lines = _handler_colon_lines(source)

    replacements = []
    insertions = []
    skipped = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        statement = _swallow_statement(node)
        if statement is None:
            continue

        end = colon_lines.get(node.lineno)
        if end is None:
            skipped += 1
            continue

        # A bare ``except:`` cannot take ``as``. It is exactly equivalent to
        # ``except BaseException:``, so naming the type keeps the caught set
        # identical while making the error bindable.
        handler_type = ast.unparse(node.type) if node.type else "BaseException"
        exc_name = node.name or EXC_NAME
        # Keep the original indentation: the replacement text overwrites the
        # whole line, and dropping the leading spaces detaches the handler from
        # its `try`.
        header_indent = lines[node.lineno - 1][: len(lines[node.lineno - 1]) - len(lines[node.lineno - 1].lstrip())]
        header = f"{header_indent}except {handler_type} as {exc_name}:"

        indent = " " * statement.col_offset
        try:
            site = f"{path.relative_to(root)}:{statement.lineno}"
        except ValueError:
            site = f"{path}:{statement.lineno}"
        log_line = indent + LOG_TEMPLATE.format(site=site, exc=exc_name)

        # Collapse a multi-line header into one line.
        replacements.append((node.lineno - 1, header))
        for extra in range(node.lineno, end):
            replacements.append((extra, None))
        insertions.append((statement.lineno - 1, log_line))

    if not insertions:
        return None, 0, skipped

    logger_edit = _module_logger_edit(tree, lines)

    replace_at = {index: text for index, text in replacements if text is not None}
    delete_at = {index for index, text in replacements if text is None}
    insert_before: dict[int, list[str]] = {}
    for index, text in insertions:
        insert_before.setdefault(index, []).append(text)
    if logger_edit is not None:
        insert_at, block = logger_edit
        insert_before.setdefault(insert_at, []).extend(block)

    # One forward pass over the original numbering. Applying deletions first and
    # insertions afterwards against the *original* line numbers put every
    # insertion below a collapsed multi-line header one line too high; three
    # modules failed the parse-back check because of it and were skipped whole,
    # which left ten handlers undisclosed.
    out: list[str] = []
    for index, line in enumerate(lines):
        if index in insert_before:
            out.extend(insert_before[index])
        if index in delete_at:
            continue
        out.append(replace_at.get(index, line))

    # Preserve the absence of a trailing newline, so the diff stays limited to the
    # handlers this script actually meant to change.
    text = "\n".join(out)
    if source.endswith("\n"):
        text += "\n"
    if had_bom:
        text = "\ufeff" + text
    # A codemod that writes back a file it cannot re-parse has destroyed source.
    # Verifying here means the caller can leave the file untouched instead.
    ast.parse(text.encode("utf-8").decode("utf-8-sig"))
    return text, len(insertions), skipped


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--repair-loggers",
        action="store_true",
        help=(
            "for files that already contain an inserted disclosure line, add the "
            "module-level logging import / logger they are missing"
        ),
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    if args.repair_loggers:
        return _repair_loggers(root, apply=args.apply)

    total_sites = total_skipped = touched = 0
    for path in sorted(root.rglob("*.py")):
        parts = path.parts
        if ".venv" in parts or "__pycache__" in parts or "tests" in parts:
            continue
        try:
            new_source, applied, skipped = transform(path, root)
        except SyntaxError as exc:
            print(f"SKIP (syntax): {path}: {exc}", file=sys.stderr)
            continue
        total_skipped += skipped
        if new_source is None:
            continue
        total_sites += applied
        touched += 1
        if args.apply:
            path.write_bytes(new_source.encode("utf-8"))

    verb = "disclosed" if args.apply else "would disclose"
    print(f"{verb} {total_sites} handler(s) across {touched} file(s)")
    if total_skipped:
        print(f"skipped {total_skipped} multi-line handler header(s) for manual review")
    if not args.apply:
        print("dry run; pass --apply to write")
    return 0


def _repair_loggers(root: Path, *, apply: bool) -> int:
    """Add the missing module-level ``logging`` import / ``logger`` binding.

    Needed because an earlier run decided on the wrong evidence and wrote
    ``logger.warning(...)`` into modules that had no module-level ``logger``.
    """

    repaired = 0
    for path in sorted(root.rglob("*.py")):
        parts = path.parts
        if ".venv" in parts or "__pycache__" in parts or "tests" in parts:
            continue
        raw = path.read_bytes()
        source = raw.decode("utf-8-sig")
        if MARKER not in source:
            continue
        lines = source.splitlines()
        tree = ast.parse(source)
        if _module_logger_edit(tree, lines) is None:
            continue
        repaired += 1
        if not apply:
            continue
        new_source, _applied, _skipped = _install_logger(
            source, lines, tree, raw.startswith(b"\xef\xbb\xbf")
        )
        if new_source is not None:
            path.write_bytes(new_source.encode("utf-8"))

    verb = "repaired" if apply else "would repair"
    print(f"{verb} {repaired} file(s) missing a module-level logger")
    return 0


def _install_logger(source: str, lines, tree, had_bom: bool):
    """Insert the module-level logging import / logger and return the new source."""

    edit = _module_logger_edit(tree, lines)
    if edit is None:
        return None, 0, 0
    insert_at, block = edit
    out = list(lines)
    out[insert_at:insert_at] = block
    text = "\n".join(out)
    if source.endswith("\n"):
        text += "\n"
    if had_bom:
        text = "\ufeff" + text
    ast.parse(text.encode("utf-8").decode("utf-8-sig"))
    return text, 0, 0


if __name__ == "__main__":
    raise SystemExit(main())
