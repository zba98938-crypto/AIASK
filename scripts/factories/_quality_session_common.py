"""Shared formatting helpers for the strategy-factory quality session script."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

LOGGER = logging.getLogger("strategy_factory_quality_session")
MARKET_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_EXECUTION_MODE = "stock_first_observe_primary"


def _now() -> datetime:
    return datetime.now(MARKET_TZ)


def _iso_now() -> str:
    return _now().isoformat()


def _format_dt(value: str | None) -> str:
    if not value:
        return "-"
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return str(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MARKET_TZ)
    else:
        dt = dt.astimezone(MARKET_TZ)
    return dt.strftime("%Y-%m-%d %H:%M:%S %z")


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return "-"


def _json_dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_dump(payload), encoding="utf-8")


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        synchronize = 0x00100000
        process = kernel32.OpenProcess(synchronize, False, int(pid))
        if not process:
            return False
        wait_result = kernel32.WaitForSingleObject(process, 0)
        kernel32.CloseHandle(process)
        return wait_result == 0x00000102
    except Exception:
        return True


_LEGACY_BUDGET_MISMATCH_FLAGS = {
    "budget_queue_without_track_assignment",
}

_LEGACY_BUDGET_MISMATCH_NOTE_FRAGMENTS = (
    "incubation budget summary shows candidates staying in deferred_budget_queue with no formal/observe budget assignment",
)

_RECOMPUTED_AUDIT_FLAGS = {
    "execution_audit_needs_attention",
    "execution_audit_bootstrap_pending",
}

_RECOMPUTED_AUDIT_NOTE_FRAGMENTS = (
    "execution audit verification still reports needs_attention on sampled strategies",
    "execution audit verification still reports missing or needs_attention on sampled strategies",
    "execution audit has native evidence but is still bootstrap_pending because realized trade samples are insufficient",
)
