"""Shared constants, CommandSpec, path/db/env helpers for shadow validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _integration_paths import runtime_home

QUANT_CORE_SRC = REPO_ROOT / "packages" / "aiask-quant-core" / "src"
if str(QUANT_CORE_SRC) not in sys.path:
    sys.path.insert(0, str(QUANT_CORE_SRC))

from aiask_quant_core.runtime_paths import resolve_sqlite_path

DEFAULT_SHADOW_DB = runtime_home() / "data" / "validation" / "trade_prediction_shadow.sqlite3"
DEFAULT_REPORT_ROOT = runtime_home() / "reports" / "ops" / "trade_prediction_shadow"
DEFAULT_BATCH_ID = f"trade_prediction_soak_{date.today().isoformat().replace('-', '_')}"
DEFAULT_AGENT_BASE_URL = "http://127.0.0.1:8765"
REQUIRED_TABLES = (
    "kline_intraday",
    "strategy_trade_predictions",
    "strategy_trade_prediction_outcomes",
)
SAFETY_ENV = {
    "LIVE_TRADING_ENABLED": "0",
    "LIVE_TRADING_ALLOW_WRITE": "0",
    "BROKER_ALLOW_WRITE": "0",
    "LIVE_TRADING_READ_ONLY": "1",
    "BROKER_READ_ONLY": "1",
}
TOGGLE_KEYS = (
    "STRATEGY_TRADE_PREDICTION_PROMOTION_GATE_ENABLED",
    "STRATEGY_TRADE_PREDICTION_BUDGET_FEEDBACK_ENABLED",
    "STRATEGY_TRADE_PREDICTION_FACTOR_DECAY_ENABLED",
)
PARTIAL_STATUSES = {
    "partial_daily_only",
    "partial_intraday_missing",
    "insufficient_samples",
    "post_hoc_rejected",
}
DATA_GAP_STATUSES = {
    "daily_bar_missing",
    "intraday_missing",
    "partial_gap",
    "invalid_ohlc",
}


@dataclass(frozen=True)
class CommandSpec:
    name: str
    command: tuple[str, ...]
    cwd: Path = REPO_ROOT
    timeout_seconds: int = 900


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )


def append_pythonpath(env: dict[str, str]) -> None:
    paths = [
        REPO_ROOT / "packages" / "aiask-quant-core" / "src",
        REPO_ROOT / "packages" / "akshare-mcp" / "src",
        REPO_ROOT / "packages" / "strategy-factory" / "src",
        REPO_ROOT / "packages" / "agent" / "src",
    ]
    existing = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]
    for path in paths:
        token = str(path)
        if path.exists() and token not in existing:
            existing.insert(0, token)
    env["PYTHONPATH"] = os.pathsep.join(existing)


def resolve_source_db(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return resolve_sqlite_path()


def resolve_shadow_db(explicit: str | None = None) -> Path:
    return Path(explicit or DEFAULT_SHADOW_DB).expanduser().resolve()


def shadow_report_dir(report_root: str | None, batch_id: str) -> Path:
    return Path(report_root or DEFAULT_REPORT_ROOT).expanduser().resolve() / batch_id


def manifest_path(report_dir: Path) -> Path:
    return report_dir / "manifest.json"


def snapshot_path(report_dir: Path, label: str) -> Path:
    return report_dir / "snapshots" / f"{label}.json"


def command_report_path(report_dir: Path, label: str) -> Path:
    return report_dir / "commands" / f"{label}.json"


def build_shadow_env(
    shadow_db: Path,
    *,
    toggles_enabled: bool = False,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    append_pythonpath(env)
    env.update(SAFETY_ENV)
    env["AKSHARE_MCP_SQLITE_PATH"] = str(shadow_db)
    env["AIASK_SQLITE_PATH"] = str(shadow_db)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    for key in TOGGLE_KEYS:
        env[key] = "1" if toggles_enabled else "0"
    if extra:
        env.update(extra)
    return env


def toggle_phase_for_day(day_index: int | None, explicit_phase: str = "auto") -> bool:
    if explicit_phase == "enabled":
        return True
    if explicit_phase == "disabled":
        return False
    if day_index is None:
        return False
    return int(day_index) >= 11


def copy_shadow_database(source_db: Path, shadow_db: Path, *, overwrite: bool = False) -> dict[str, Any]:
    source = source_db.expanduser().resolve()
    target = shadow_db.expanduser().resolve()
    if source == target:
        raise ValueError("source database and shadow database must be different")
    if not source.exists():
        raise FileNotFoundError(f"source database not found: {source}")
    if target.exists() and not overwrite:
        return {
            "status": "exists",
            "source_db": str(source),
            "shadow_db": str(target),
            "copied": False,
            "size_bytes": target.stat().st_size,
        }
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(str(target), timeout=60)) as dst:
            src.backup(dst)
    for suffix in ("-wal", "-shm"):
        stale = Path(str(target) + suffix)
        if stale.exists():
            stale.unlink()
    return {
        "status": "copied",
        "source_db": str(source),
        "shadow_db": str(target),
        "copied": True,
        "size_bytes": target.stat().st_size,
    }


def check_shadow_schema(shadow_db: Path) -> dict[str, Any]:
    if not shadow_db.exists():
        return {
            "status": "failed",
            "shadow_db": str(shadow_db),
            "missing_tables": list(REQUIRED_TABLES),
            "tables": [],
        }
    with closing(sqlite3.connect(f"file:{shadow_db.as_posix()}?mode=ro", uri=True, timeout=30)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        names = [str(row[0]) for row in rows]
        columns = {
            table: [str(col[1]) for col in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
            for table in REQUIRED_TABLES
            if table in names
        }
    missing = [name for name in REQUIRED_TABLES if name not in names]
    required_columns = {
        "kline_intraday": {"code", "period", "timestamp", "adjust", "open", "high", "low", "close", "data_quality_status"},
        "strategy_trade_predictions": {"prediction_id", "contract_hash", "target_trading_date", "prediction_status"},
        "strategy_trade_prediction_outcomes": {"outcome_id", "prediction_id", "score_version", "score_status", "data_quality_status"},
    }
    missing_columns: dict[str, list[str]] = {}
    for table, expected in required_columns.items():
        if table not in columns:
            continue
        actual = set(columns[table])
        miss = sorted(expected - actual)
        if miss:
            missing_columns[table] = miss
    return {
        "status": "ok" if not missing and not missing_columns else "failed",
        "shadow_db": str(shadow_db),
        "tables": names,
        "missing_tables": missing,
        "columns": columns,
        "missing_columns": missing_columns,
    }


def _fetch_pairs(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, count in conn.execute(sql, params).fetchall():
        token = str(key or "unknown")
        result[token] = int(count or 0)
    return result


def _fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _decode_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return {}
    try:
        return json.loads(str(value))
    except Exception:
        return {}


def _string(value: Any) -> str:
    return str(value or "").strip()


SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(token\s*[:=]\s*)([A-Za-z0-9_\-\.]{4,})"),
    re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)([A-Za-z0-9_\-\.]{4,})"),
    re.compile(r"(?i)(secret\s*[:=]\s*)([A-Za-z0-9_\-\.]{4,})"),
)


def _redact(text: str, *, max_chars: int = 20000) -> str:
    sanitized = str(text or "")
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub(lambda m: (m.group(1) if len(m.groups()) > 1 else "") + "[redacted]", sanitized)
    if len(sanitized) > max_chars:
        return sanitized[:max_chars] + "\n...[truncated]"
    return sanitized
