"""Migration SQL is split on semicolons, with no understanding of comments.

``_split_sql`` in ``schema_base.py`` scans character by character and treats
every ``;`` outside quotes as a statement terminator. It does not skip ``--``
comment lines, and the compatibility pass that runs first only strips
Postgres-specific blocks, not comments.

So a semicolon inside a comment splits the file mid-sentence and the remainder
is executed as SQL. An earlier revision of ``006_quality_report_provenance.sql``
contained "-- every existing row as unverified; rows written from now on ...",
which produced a statement beginning with "rows" and failed every schema
initialisation with ``near "rows": syntax error``.

The five migrations that predate this one carry no comments at all, which is why
the constraint had never been exercised.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS_DIR = (
    ROOT
    / "packages"
    / "aiask-quant-core"
    / "src"
    / "aiask_quant_core"
    / "storage"
    / "sqlite"
    / "app_core_migrations"
)


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def test_the_migration_directory_is_not_empty() -> None:
    files = _migration_files()
    assert files, f"no migrations found under {MIGRATIONS_DIR}"


def test_no_comment_line_contains_a_semicolon() -> None:
    offenders: list[str] = []
    for path in _migration_files():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            stripped = line.strip()
            if not stripped.startswith("--"):
                continue
            body = stripped[2:]
            if ";" in body:
                offenders.append(f"{path.name}:{number}: {stripped}")

    assert not offenders, (
        "the splitter terminates a statement at every ';', including inside a "
        "comment, so the text after it becomes SQL. Remove the semicolon or "
        "move the explanation out of the .sql file:\n  " + "\n  ".join(offenders)
    )


def test_every_statement_after_splitting_parses_as_sql() -> None:
    """Reproduce the splitter's own view and reject comment-only fragments."""

    from aiask_quant_core.storage.sqlite.schema_base import _split_sql

    for path in _migration_files():
        sql = path.read_text(encoding="utf-8")
        for statement in _split_sql(sql):
            stripped = statement.strip()
            if not stripped or stripped.startswith("--"):
                continue
            first_word = re.split(r"\s+", stripped, maxsplit=1)[0].upper()
            assert first_word in {
                "ALTER",
                "CREATE",
                "DROP",
                "INSERT",
                "UPDATE",
                "DELETE",
                "PRAGMA",
                "WITH",
                "INDEX",
            }, (
                f"{path.name}: a fragment does not begin with a SQL keyword "
                f"(likely a comment tail) -> {stripped[:120]!r}"
            )
