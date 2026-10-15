"""SQLite storage base with async-style connection helpers.

The storage mixins use an async surface (`fetch`, `fetchrow`, `fetchval`,
`execute`, `executemany`, `transaction`).  This layer keeps that shape while
handling SQLite connection management and lightweight SQL compatibility.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from ...runtime_paths import resolve_sqlite_path

logger = logging.getLogger(__name__)

try:
    from .strategy_factory_json_budget import (
        bounded_json_text,
        strategy_factory_sql_json_field_limits,
    )
except Exception:  # pragma: no cover - fallback for partial imports during bootstrap
    bounded_json_text = None
    strategy_factory_sql_json_field_limits = None


_PG_CAST_RE = re.compile(
    r"::\s*(?:jsonb|json|timestamptz|timestamp(?:\s+with\s+time\s+zone)?|date|integer|int|bigint|float|double\s+precision|numeric|real|text|vector(?:\s*\(\s*\d+\s*\))?)",
    re.I,
)
_PG_ARRAY_CAST_RE = re.compile(r"::\s*(?:text|int|integer|bigint|real|double\s+precision)\s*\[\s*\]", re.I)
_DOLLAR_PARAM_RE = re.compile(r"\$(\d+)")
_LIST_PARAM_RE = re.compile(r"__SQLITE_LIST_PARAM_(\d+)__")
_DO_BLOCK_RE = re.compile(r"\bDO\s+\$\$.*?\bEND\s+\$\$\s*;", re.I | re.S)
_CREATE_EXTENSION_RE = re.compile(r"\bCREATE\s+EXTENSION\b.*?;", re.I | re.S)
_PG_VECTOR_INDEX_RE = re.compile(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b.*?\bUSING\s+(?:GIN|HNSW)\b.*?;", re.I | re.S)
_TSVECTOR_INDEX_RE = re.compile(r"\bCREATE\s+(?:UNIQUE\s+)?INDEX\b.*?\bto_tsvector\s*\(.*?;", re.I | re.S)
_ADD_COLUMN_RE = re.compile(
    r"^\s*ALTER\s+TABLE\s+(?P<table>[\w\".]+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(?P<definition>.+?)\s*$",
    re.I | re.S,
)
def default_sqlite_path() -> Path:
    return resolve_sqlite_path()


def _busy_timeout_ms() -> int:
    try:
        return max(100, int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000")))
    except ValueError:
        return 30000


def _journal_mode() -> str:
    value = str(os.getenv("SQLITE_JOURNAL_MODE", "WAL")).strip().upper()
    return value or "WAL"


def _normalize_identifier(value: str) -> str:
    return value.strip().strip('"').split(".")[-1]


def _sqlite_type(definition: str) -> str:
    text = definition
    replacements = [
        (r"\bbigserial\s+primary\s+key\b", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        (r"\bserial\s+primary\s+key\b", "INTEGER PRIMARY KEY AUTOINCREMENT"),
        (r"\bbigserial\b", "INTEGER"),
        (r"\bserial\b", "INTEGER"),
        (r"\bDOUBLE\s+PRECISION\b", "REAL"),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.I)
    text = re.sub(r"\bDATE\b", "TEXT", text, flags=re.I)
    text = re.sub(r"DEFAULT\s+'(\{.*?\}|\[.*?\])'\s*::\s*TEXT", r"DEFAULT '\1'", text, flags=re.I)
    text = re.sub(r"DEFAULT\s+NOW\s*\(\s*\)", "DEFAULT CURRENT_TIMESTAMP", text, flags=re.I)
    text = re.sub(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", text, flags=re.I)
    text = _PG_ARRAY_CAST_RE.sub("", text)
    text = _PG_CAST_RE.sub("", text)
    return text


def _strip_unsupported_blocks(sql: str) -> str:
    text = _DO_BLOCK_RE.sub("", sql)
    text = _CREATE_EXTENSION_RE.sub("", text)
    text = _PG_VECTOR_INDEX_RE.sub("", text)
    text = _TSVECTOR_INDEX_RE.sub("", text)
    return text


def _split_sql(sql: str) -> list[str]:
    text = _strip_unsupported_blocks(sql)
    statements: list[str] = []
    current: list[str] = []
    in_single = False
    in_double = False
    escape = False
    for char in text:
        current.append(char)
        if char == "'" and not in_double and not escape:
            in_single = not in_single
        elif char == '"' and not in_single and not escape:
            in_double = not in_double
        if char == ";" and not in_single and not in_double:
            candidate = "".join(current).strip()
            lowered_candidate = candidate.lower()
            if lowered_candidate.startswith("create trigger") and not re.search(r"\bend\s*;\s*$", lowered_candidate):
                escape = False
                continue
            statement = "".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
        escape = char == "\\" and not escape
        if char != "\\":
            escape = False
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _prepare_sql(sql: str) -> str:
    text = sql.strip().rstrip(";")
    if re.match(r"^\s*(?:CREATE|ALTER)\b", text, flags=re.I):
        text = _sqlite_type(text)
    else:
        text = re.sub(r"\bNOW\s*\(\s*\)", "CURRENT_TIMESTAMP", text, flags=re.I)
    text = _json_extract_sql(text)
    text = re.sub(r"\bjsonb_array_length\s*\(", "json_array_length(", text, flags=re.I)
    text = re.sub(r"\bjsonb_typeof\s*\(", "json_type(", text, flags=re.I)
    text = re.sub(r"\bGREATEST\s*\(", "max(", text, flags=re.I)
    text = _PG_ARRAY_CAST_RE.sub("", text)
    text = re.sub(r"\bILIKE\b", "LIKE", text, flags=re.I)
    text = re.sub(r"\s+NULLS\s+(?:FIRST|LAST)", "", text, flags=re.I)
    text = re.sub(r"\bTRUE\b", "1", text, flags=re.I)
    text = re.sub(r"\bFALSE\b", "0", text, flags=re.I)
    text = re.sub(r"\bCURRENT_DATE\s*-\s*\(\s*(\?)\s*\*\s*INTERVAL\s+'1 day'\s*\)", r"date('now', '-' || \1 || ' days')", text, flags=re.I)
    text = re.sub(r"\bCURRENT_DATE\s*\+\s*(\?)\s*\*\s*INTERVAL\s+'1 day'\s*", r"date('now', '+' || \1 || ' days')", text, flags=re.I)
    text = re.sub(r"(\?)\s*::\s*date\s*\+\s*INTERVAL\s+'1 day'", r"date(\1, '+1 day')", text, flags=re.I)
    text = _PG_CAST_RE.sub("", text)
    return text


def _json_extract_sql(sql: str) -> str:
    text = re.sub(
        r"(?P<expr>\b[A-Za-z_][\w.]*\b)\s*-" + r">>\s*'(?P<key>[A-Za-z_][\w]*)'",
        lambda match: f"json_extract({match.group('expr')}, '$.{match.group('key')}')",
        sql,
    )
    text = re.sub(
        r"(?P<expr>\b[A-Za-z_][\w.]*\b)\s*->\s*'(?P<key>[A-Za-z_][\w]*)'",
        lambda match: f"json_extract({match.group('expr')}, '$.{match.group('key')}')",
        text,
    )
    text = re.sub(
        r"(?P<expr>\b[A-Za-z_][\w.]*\b)\s*\?\s*'(?P<key>[A-Za-z_][\w]*)'",
        lambda match: f"json_type({match.group('expr')}, '$.{match.group('key')}') IS NOT NULL",
        text,
    )
    return text


def _mark_list_params(sql: str) -> str:
    text = re.sub(
        r"NOT\s*\(\s*(?P<expr>[\w.]+)\s*=\s*ANY\s*\(\s*\$(?P<idx>\d+)\s*\)\s*\)",
        lambda match: f"{match.group('expr')} NOT IN (__SQLITE_LIST_PARAM_{match.group('idx')}__)",
        sql,
        flags=re.I,
    )
    text = re.sub(
        r"=\s*ANY\s*\(\s*\$(?P<idx>\d+)\s*\)",
        lambda match: f"IN (__SQLITE_LIST_PARAM_{match.group('idx')}__)",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"\bIN\s*\(\s*\$(?P<idx>\d+)\s*\)",
        lambda match: f"IN (__SQLITE_LIST_PARAM_{match.group('idx')}__)",
        text,
        flags=re.I,
    )
    return text


def _strategy_factory_json_field_limit(field_name: str) -> int | None:
    if strategy_factory_sql_json_field_limits is None:
        return None
    try:
        table, column = str(field_name).split(".", 1)
    except ValueError as _swallowed:
        logger.warning("packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_base.py:199: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None
    limits = strategy_factory_sql_json_field_limits() or {}
    return limits.get((_normalize_identifier(table), _normalize_identifier(column)))


def _coerce_json_field_arg(field_name: str, value: Any) -> Any:
    limit = _strategy_factory_json_field_limit(field_name)
    if bounded_json_text is None or limit is None:
        return _coerce_arg(value)
    if value is None:
        return None
    if isinstance(value, (dict, list, tuple)):
        return bounded_json_text(field_name, value, max_bytes=limit)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[", '"')) or len(value.encode("utf-8")) > limit:
            return bounded_json_text(field_name, value, max_bytes=limit)
    return _coerce_arg(value)


def _prepare_statement(sql: str, args: tuple[Any, ...]) -> tuple[str, tuple[Any, ...]]:
    field_map = _statement_json_field_map(sql)
    text = _mark_list_params(_prepare_sql(sql))
    ordered: list[Any] = []

    def replace_token(match: re.Match[str]) -> str:
        list_match = match.group(1)
        dollar_match = match.group(2)
        if list_match is not None:
            index = int(list_match) - 1
            values = args[index] if 0 <= index < len(args) else []
            if values is None:
                return "NULL"
            if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
                values = [values]
            values = list(values)
            if not values:
                return "NULL"
            ordered.extend(_coerce_scalar_arg(value) for value in values)
            return ", ".join("?" for _ in values)
        position = int(dollar_match)
        if 1 <= position <= len(args):
            field_name = field_map.get(position)
            if field_name:
                ordered.append(_coerce_json_field_arg(field_name, args[position - 1]))
            else:
                ordered.append(_coerce_arg(args[position - 1]))
        return "?"

    pattern = re.compile(r"__SQLITE_LIST_PARAM_(\d+)__|\$(\d+)")
    return pattern.sub(replace_token, text), tuple(ordered)


def _coerce_scalar_arg(value: Any) -> Any:
    if isinstance(value, tuple):
        return _coerce_arg(list(value))
    if isinstance(value, list):
        return _coerce_arg(value)
    return _coerce_arg(value)


def _coerce_arg(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        import json

        return json.dumps(value, ensure_ascii=False, default=str)
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    return value


def _split_top_level_commas(text: str) -> list[str]:
    items: list[str] = []
    current: list[str] = []
    depth = 0
    in_single = False
    in_double = False
    escape = False
    for char in text:
        if char == "'" and not in_double and not escape:
            in_single = not in_single
        elif char == '"' and not in_single and not escape:
            in_double = not in_double
        elif char == "(" and not in_single and not in_double:
            depth += 1
        elif char == ")" and not in_single and not in_double and depth > 0:
            depth -= 1
        if char == "," and not in_single and not in_double and depth == 0:
            item = "".join(current).strip()
            if item:
                items.append(item)
            current = []
            continue
        current.append(char)
        escape = char == "\\" and not escape
        if char != "\\":
            escape = False
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


def _find_matching_paren(text: str, start_index: int) -> int:
    depth = 0
    in_single = False
    in_double = False
    escape = False
    for index in range(start_index, len(text)):
        char = text[index]
        if char == "'" and not in_double and not escape:
            in_single = not in_single
        elif char == '"' and not in_single and not escape:
            in_double = not in_double
        elif char == "(" and not in_single and not in_double:
            depth += 1
        elif char == ")" and not in_single and not in_double:
            depth -= 1
            if depth == 0:
                return index
        escape = char == "\\" and not escape
        if char != "\\":
            escape = False
    return -1


def _statement_json_field_map(sql: str) -> dict[int, str]:
    if strategy_factory_sql_json_field_limits is None:
        return {}
    limits = strategy_factory_sql_json_field_limits() or {}
    if not limits:
        return {}

    def field_name(table: str, column: str) -> str | None:
        key = (_normalize_identifier(table), _normalize_identifier(column))
        return f"{key[0]}.{key[1]}" if key in limits else None

    def register_placeholders(expr: str, target_field: str, field_map: dict[int, str]) -> None:
        for match in re.finditer(r"\$(\d+)", expr):
            field_map[int(match.group(1))] = target_field

    text = sql
    lowered = text.lower()
    field_map: dict[int, str] = {}

    insert_match = re.search(r"\b(?:insert\s+(?:or\s+\w+\s+)?into|replace\s+into)\b", text, flags=re.I)
    if insert_match:
        after_insert = insert_match.end()
        table_start = after_insert
        while table_start < len(text) and text[table_start].isspace():
            table_start += 1
        table_end = table_start
        while table_end < len(text) and not text[table_end].isspace() and text[table_end] != "(":
            table_end += 1
        table = text[table_start:table_end].strip()
        columns_start = text.find("(", table_end)
        columns_end = _find_matching_paren(text, columns_start) if columns_start != -1 else -1
        values_idx = lowered.find("values", columns_end if columns_end != -1 else table_end)
        values_start = text.find("(", values_idx) if values_idx != -1 else -1
        values_end = _find_matching_paren(text, values_start) if values_start != -1 else -1
        if table and columns_start != -1 and columns_end != -1 and values_start != -1 and values_end != -1:
            columns = [_normalize_identifier(item.split()[0]) for item in _split_top_level_commas(text[columns_start + 1 : columns_end])]
            values = _split_top_level_commas(text[values_start + 1 : values_end])
            for column, expr in zip(columns, values):
                target = field_name(table, column)
                if target:
                    register_placeholders(expr, target, field_map)
        conflict_idx = lowered.find("do update set", values_end if values_end != -1 else table_end)
        if conflict_idx != -1:
            clause_start = conflict_idx + len("do update set")
            clause_end = len(text)
            for stop_keyword in (" returning ", " where ", " limit ", " order by ", " group by "):
                stop_idx = lowered.find(stop_keyword, clause_start)
                if stop_idx != -1:
                    clause_end = min(clause_end, stop_idx)
            for assignment in _split_top_level_commas(text[clause_start:clause_end]):
                if "=" not in assignment:
                    continue
                left, right = assignment.split("=", 1)
                column = _normalize_identifier(left.strip().split()[-1])
                target = field_name(table, column)
                if target:
                    register_placeholders(right, target, field_map)

    if lowered.startswith("update ") or " update " in lowered[:40]:
        update_idx = lowered.find("update ")
        set_idx = lowered.find(" set ", update_idx)
        if update_idx != -1 and set_idx != -1:
            table_start = update_idx + len("update ")
            table_end = set_idx
            table = text[table_start:table_end].strip().split()[0]
            clause_start = set_idx + len(" set ")
            clause_end = len(text)
            for stop_keyword in (" returning ", " where ", " from ", " limit ", " order by ", " group by "):
                stop_idx = lowered.find(stop_keyword, clause_start)
                if stop_idx != -1:
                    clause_end = min(clause_end, stop_idx)
            for assignment in _split_top_level_commas(text[clause_start:clause_end]):
                if "=" not in assignment:
                    continue
                left, right = assignment.split("=", 1)
                column = _normalize_identifier(left.strip().split()[-1])
                target = field_name(table, column)
                if target:
                    register_placeholders(right, target, field_map)

    return field_map


class _SQLiteTransaction:
    def __init__(self, connection: "SQLiteConnection"):
        self._connection = connection

    async def __aenter__(self) -> "SQLiteConnection":
        self._connection._begin()
        return self._connection

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self._connection._commit()
        else:
            self._connection._rollback()


class SQLiteConnection:
    """Small async wrapper around one sqlite3 connection."""

    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock):
        self._conn = conn
        self._lock = lock
        self._transaction_depth = 0

    def transaction(self) -> _SQLiteTransaction:
        return _SQLiteTransaction(self)

    def _begin(self) -> None:
        with self._lock:
            if self._transaction_depth == 0:
                self._conn.execute("BEGIN")
            self._transaction_depth += 1

    def _commit(self) -> None:
        with self._lock:
            self._transaction_depth = max(0, self._transaction_depth - 1)
            if self._transaction_depth == 0:
                self._conn.commit()

    def _rollback(self) -> None:
        with self._lock:
            self._transaction_depth = 0
            self._conn.rollback()

    def _maybe_commit(self) -> None:
        if self._transaction_depth == 0:
            self._conn.commit()

    async def execute(self, sql: str, *args: Any) -> str:
        count = 0
        with self._lock:
            for statement in _split_sql(sql):
                count += self._execute_one(statement, args)
            self._maybe_commit()
        return f"SQLITE {count}"

    async def executemany(self, sql: str, args_iter: Iterable[Iterable[Any]]) -> str:
        prepared_rows = [_prepare_statement(sql, tuple(row)) for row in args_iter]
        statement = prepared_rows[0][0] if prepared_rows else _prepare_statement(sql, ())[0]
        rows = [row for _, row in prepared_rows]
        with self._lock:
            if rows:
                self._conn.executemany(statement, rows)
            self._maybe_commit()
        return f"SQLITE {len(rows)}"

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        special = self._special_fetch(sql, args, scalar=False)
        if special is not None:
            return special
        with self._lock:
            statement, ordered_args = _prepare_statement(sql, args)
            cursor = self._conn.execute(statement, ordered_args)
            rows = [dict(row) for row in cursor.fetchall()]
            self._maybe_commit()
            return rows

    async def fetchrow(self, sql: str, *args: Any) -> Optional[dict[str, Any]]:
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql: str, *args: Any) -> Any:
        special = self._special_fetch(sql, args, scalar=True)
        if special is not None:
            return special
        row = await self.fetchrow(sql, *args)
        if not row:
            return None
        return next(iter(row.values()))

    def _execute_one(self, statement: str, args: tuple[Any, ...]) -> int:
        if not statement:
            return 0
        lowered = statement.lower()
        if "pg_advisory_lock" in lowered or "pg_advisory_unlock" in lowered:
            return 0
        add_column = _ADD_COLUMN_RE.match(statement)
        if add_column:
            self._add_column_if_missing(add_column.group("table"), add_column.group("definition"))
            return 1
        prepared, ordered_args = _prepare_statement(statement, args)
        try:
            self._conn.execute(prepared, ordered_args)
            return 1
        except sqlite3.OperationalError as exc:
            if self._is_safe_schema_skip(prepared, exc):
                logger.debug("Skipped SQLite-incompatible schema SQL: %s (%s)", prepared[:160], exc)
                return 0
            raise

    def _add_column_if_missing(self, table_name: str, definition: str) -> None:
        table = _normalize_identifier(table_name)
        column = _normalize_identifier(definition.strip().split()[0])
        if not self._table_exists(table):
            return
        existing = {str(row["name"]) for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column in existing:
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {_sqlite_type(definition)}")

    @staticmethod
    def _is_safe_schema_skip(statement: str, exc: sqlite3.OperationalError) -> bool:
        text = str(exc).lower()
        lowered = statement.lower()
        safe_markers = (
            "already exists",
            "duplicate column name",
            "near \"using\"",
            "near \"do\"",
            "near \"constraint\"",
            "near \"if\"",
            "unrecognized token",
        )
        if any(marker in text for marker in safe_markers):
            return True
        if "no such table" in text and statement.lower().startswith("create index"):
            return True
        # Match on statement shape, not the driver's error wording: SQLite phrases
        # the same rejection differently across versions (3.51 says near "CONSTRAINT",
        # 3.53 says near "EXISTS"), so a message whitelist silently stops working on
        # upgrade. SQLite supports neither ADD nor DROP CONSTRAINT, so both are
        # Postgres-only DDL this compat layer is meant to ignore.
        if lowered.startswith("alter table") and ("add constraint" in lowered or "drop constraint" in lowered):
            return True
        if lowered.startswith("create index") and ("to_tsvector" in lowered or " using " in lowered):
            return True
        return False

    def _special_fetch(self, sql: str, args: tuple[Any, ...], *, scalar: bool) -> Any:
        lowered = sql.lower()
        if "pg_advisory_lock" in lowered or "pg_advisory_unlock" in lowered:
            return 1 if scalar else [{"ok": 1}]
        return None

    def _extract_literal_or_arg(self, sql: str, args: tuple[Any, ...], name: str) -> Optional[str]:
        match = re.search(rf"{name}\s*=\s*\$(\d+)", sql, flags=re.I)
        if match:
            index = int(match.group(1)) - 1
            if 0 <= index < len(args):
                return str(args[index])
        match = re.search(rf"{name}\s*=\s*'([^']+)'", sql, flags=re.I)
        if match:
            return match.group(1)
        return None

    def _table_exists(self, table: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ? LIMIT 1",
                (table,),
            ).fetchone()
        return row is not None

    def _columns(self, table: str) -> list[str]:
        if not table:
            return []
        with self._lock:
            return [str(row["name"]) for row in self._conn.execute(f"PRAGMA table_info({_normalize_identifier(table)})").fetchall()]


class SchemaBase:
    """SQLite connection management and schema bootstrap."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path is not None else default_sqlite_path()
        self.connection: Optional[sqlite3.Connection] = None
        self._connection_wrapper: Optional[SQLiteConnection] = None
        self._initialized = False
        self._init_lock: Optional[asyncio.Lock] = None
        self._init_lock_loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = threading.RLock()

    def _get_init_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._init_lock is None or self._init_lock_loop is not loop:
            self._init_lock = asyncio.Lock()
            self._init_lock_loop = loop
        return self._init_lock

    def _reset_connection_state(self) -> None:
        self.connection = None
        self._connection_wrapper = None
        self._initialized = False
        self._init_lock = None
        self._init_lock_loop = None

    async def initialize(self) -> None:
        if self._initialized:
            return
        lock = self._get_init_lock()
        async with lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=_busy_timeout_ms() / 1000, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {_busy_timeout_ms()}")
            # PR-S5: 让 SQLite 周期性把 freelist 上的 page 截断还给操作系统。
            # auto_vacuum 必须在写入第一个 page 之前 / 任何 journal_mode 切换之前设置才生效。
            # 新建空 DB 会立即生效；旧 DB 需要做一次 VACUUM 才会切换 auto_vacuum 模式。
            try:
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            except Exception:
                logger.warning("PRAGMA auto_vacuum=INCREMENTAL failed; existing DB may need VACUUM to switch")
            conn.execute(f"PRAGMA journal_mode = {_journal_mode()}")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = NORMAL")
            self.connection = conn
            self._connection_wrapper = SQLiteConnection(conn, self._lock)
            self._initialized = True
            logger.info("Connected to SQLite database %s", self.path)
            await self._init_tables()
            # PR-S5: 新建 DB 时强制做一次 VACUUM 锁定 auto_vacuum=INCREMENTAL。
            # 因为 SQLite 的 auto_vacuum 设置在 DB header 的固定 byte，
            # 必须在写入第一个 page 之前（即 sqlite3.connect 创建文件那一瞬间）
            # 设置才生效。但 Python 的 sqlite3 模块默认会 implicit-begin 事务，
            # 导致 PRAGMA auto_vacuum 静默失败。最稳妥的方式是初始化后做一次
            # VACUUM——只对刚创建的小 DB（< 100 MB）才执行，避免对大 DB 卡死。
            try:
                page_count_row = self.connection.execute("PRAGMA page_count").fetchone()
                page_size_row = self.connection.execute("PRAGMA page_size").fetchone()
                auto_vacuum_row = self.connection.execute("PRAGMA auto_vacuum").fetchone()
                page_count = int(page_count_row[0]) if page_count_row else 0
                page_size = int(page_size_row[0]) if page_size_row else 4096
                current_mode = int(auto_vacuum_row[0]) if auto_vacuum_row else 0
                size_mb = page_count * page_size / 1024 / 1024
                if current_mode != 2 and size_mb < 100:
                    logger.info("Switching auto_vacuum to INCREMENTAL via VACUUM (size=%.1fMB)", size_mb)
                    self.connection.execute("PRAGMA auto_vacuum = INCREMENTAL")
                    self.connection.execute("VACUUM")
            except Exception as exc:
                logger.warning("auto_vacuum upgrade failed: %s", exc)

    async def _init_tables(self) -> None:
        from .schema_app_core import (
            record_schema_namespace_checkpoint,
            run_app_core_migrations,
        )
        from .schema_market import init_market_tables
        from .schema_providers import init_domain_schema
        from .schema_strategy import init_strategy_tables
        from .schema_vector import init_vector_tables

        async with self.acquire() as conn:
            await run_app_core_migrations(conn)
            await init_market_tables(conn)

            # Factory-domain DDL is looked up through the registry so its owning
            # package (Strategy Factory) can supply it. The bundled copy remains
            # the fallback, which keeps this working with no host present and
            # makes the ownership move incremental instead of a flag day.
            strategy_outcome = await init_domain_schema(
                "strategy_runtime",
                conn,
                sqlite_python_enabled=False,
                fallback=init_strategy_tables,
                fallback_source="aiask_quant_core.storage.sqlite.schema_strategy",
            )

            await init_vector_tables(conn, False)
            await record_schema_namespace_checkpoint(
                conn,
                namespace="market_runtime",
                migration_key="sqlite_bootstrap_v1",
                source_module="aiask_quant_core.storage.sqlite.schema_market",
            )
            await record_schema_namespace_checkpoint(
                conn,
                namespace="strategy_runtime",
                migration_key="sqlite_bootstrap_v1",
                # Record who actually created the tables, not who used to.
                source_module=str(
                    strategy_outcome.get("source_module")
                    or "aiask_quant_core.storage.sqlite.schema_strategy"
                ),
            )
            await record_schema_namespace_checkpoint(
                conn,
                namespace="vector_runtime",
                migration_key="sqlite_bootstrap_v1",
                source_module="aiask_quant_core.storage.sqlite.schema_vector",
            )

        logger.info(
            "SQLite tables initialized successfully (strategy schema from %s)",
            strategy_outcome.get("source_module") or "bundled",
        )

    def supports_sqlite_python(self) -> bool:
        return False

    def get_vector_backend(self) -> str:
        return "sqlite_python"

    def status(self) -> dict[str, Any]:
        writable = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            probe = self.path.parent / ".sqlite_write_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            writable = True
        except Exception:
            writable = False
        return {
            "backend": "sqlite",
            "path": str(self.path),
            "configured": True,
            "writable": writable,
            "journal_mode": _journal_mode(),
            "busy_timeout_ms": _busy_timeout_ms(),
        }

    async def close(self) -> None:
        if self.connection is not None:
            with self._lock:
                self.connection.commit()
                self.connection.close()
            self._reset_connection_state()
            logger.info("SQLite connection closed")

    @asynccontextmanager
    async def acquire(self):
        if not self._initialized:
            await self.initialize()
        if self._connection_wrapper is None:
            raise RuntimeError("SQLite connection is not initialized")
        yield self._connection_wrapper
