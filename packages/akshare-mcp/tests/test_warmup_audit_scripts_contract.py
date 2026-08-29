"""Contract tests for the warmup audit scripts.

These tests pin the interface that ``data_sync_manager`` relies on when
loading ``scripts/audit_sync_core_market_data.py`` and
``scripts/audit_sync_factor_context_data.py`` via
``importlib.util.spec_from_file_location`` followed by
``await module._main(args)``.

The tests are intentionally cheap: they only verify file presence, module
loadability, the ``async _main`` signature, and that the warmup-mode path
of each script does not invoke blocking synchronous network I/O. The full
ingestion smoke test still lives in ``test_market_text_source_ingest.py``;
this file is the static contract guard.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import os
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"
CORE_MARKET_SCRIPT = SCRIPTS_DIR / "audit_sync_core_market_data.py"
FACTOR_CONTEXT_SCRIPT = SCRIPTS_DIR / "audit_sync_factor_context_data.py"


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -----------------------------------------------------------------------------
# T1.1 / T1.2: file presence + loadability
# -----------------------------------------------------------------------------

def test_audit_sync_core_market_data_script_exists() -> None:
    assert CORE_MARKET_SCRIPT.is_file(), (
        f"audit_sync_core_market_data.py not found at {CORE_MARKET_SCRIPT}"
    )


def test_audit_sync_factor_context_data_script_exists() -> None:
    assert FACTOR_CONTEXT_SCRIPT.is_file(), (
        f"audit_sync_factor_context_data.py not found at {FACTOR_CONTEXT_SCRIPT}"
    )


def test_audit_sync_core_market_data_loadable_via_spec() -> None:
    module = _load_module(CORE_MARKET_SCRIPT, "audit_sync_core_market_data_runtime")
    assert hasattr(module, "_main"), "module must export _main"


def test_audit_sync_factor_context_data_loadable_via_spec() -> None:
    module = _load_module(FACTOR_CONTEXT_SCRIPT, "audit_sync_factor_context_runtime")
    assert hasattr(module, "_main"), "module must export _main"


# -----------------------------------------------------------------------------
# T1.3 / T1.4: async _main signature
# -----------------------------------------------------------------------------

def test_audit_sync_core_market_main_is_async_coroutine_function() -> None:
    module = _load_module(CORE_MARKET_SCRIPT, "audit_sync_core_market_data_runtime_signature")
    assert inspect.iscoroutinefunction(module._main), (
        "_main must be an async def coroutine function"
    )


def test_audit_sync_factor_context_main_is_async_coroutine_function() -> None:
    module = _load_module(FACTOR_CONTEXT_SCRIPT, "audit_sync_factor_context_runtime_signature")
    assert inspect.iscoroutinefunction(module._main), (
        "_main must be an async def coroutine function"
    )


# -----------------------------------------------------------------------------
# T1.5: warmup mode does not call blocking sync IO (requests.*)
# -----------------------------------------------------------------------------

class _RequestsForbiddenError(RuntimeError):
    """Raised by the test stub to assert no blocking requests.* call happened."""


def _patch_blocking_requests(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Replace requests.get / requests.post with stubs that raise.

    Returns a counter dict the caller can inspect to confirm no calls were
    made. Raising from the stub gives a hard stack trace if the script
    accidentally goes through a blocking path.
    """
    counter = {"calls": 0}
    try:
        import requests  # type: ignore[import-not-found]
    except ImportError:
        return counter

    def _forbidden(*_args: object, **_kwargs: object) -> object:
        counter["calls"] += 1
        raise _RequestsForbiddenError(
            "warmup mode must not call requests.get / requests.post"
        )

    monkeypatch.setattr(requests, "get", _forbidden, raising=False)
    monkeypatch.setattr(requests, "post", _forbidden, raising=False)
    monkeypatch.setattr(requests, "request", _forbidden, raising=False)
    if hasattr(requests, "Session"):
        monkeypatch.setattr(requests.Session, "request", _forbidden, raising=False)
        monkeypatch.setattr(requests.Session, "get", _forbidden, raising=False)
        monkeypatch.setattr(requests.Session, "post", _forbidden, raising=False)
    return counter


def test_audit_sync_factor_context_warmup_mode_does_not_call_requests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """In warmup mode the factor_context script must take the probe-only
    branch and never hit requests.get / requests.post."""
    # Point at a throwaway DB so we don't pollute the real one.
    db_path = tmp_path / "warmup_contract.sqlite3"
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))

    counter = _patch_blocking_requests(monkeypatch)

    module = _load_module(
        FACTOR_CONTEXT_SCRIPT,
        "audit_sync_factor_context_warmup_no_requests",
    )

    args = argparse.Namespace(
        codes="600519",
        scope_sources="explicit",
        active_pool_limit=2,
        task_run_limit=2,
        news_days=7,
        notice_days=7,
        item_limit=2,
    )

    exit_code = asyncio.run(module._main(args))
    assert isinstance(exit_code, int), "_main must return an int exit code"
    # warmup probe-only path must not hit requests.* even once.
    assert counter["calls"] == 0, (
        f"warmup mode hit requests.* {counter['calls']} time(s); "
        "the probe-only path must skip the network ingest"
    )


def test_audit_sync_core_market_warmup_disables_external_gap_fill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """In warmup mode the core_market script must force ENABLE_EXTERNAL_GAP_FILL
    off, regardless of what the surrounding shell set."""
    db_path = tmp_path / "warmup_contract.sqlite3"
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("ENABLE_EXTERNAL_GAP_FILL", "1")
    monkeypatch.setenv("TDX_ENABLE_EXTERNAL_GAP_FILL", "1")

    module = _load_module(
        CORE_MARKET_SCRIPT,
        "audit_sync_core_market_warmup_no_gap_fill",
    )

    # We don't need TdxSyncService to do real work; we just want to verify
    # that the env override path runs and external_gap_fill_enabled() returns
    # False inside _run_tdx_core_tasks. We call the helper directly with an
    # empty universe and a stubbed TdxSyncService.
    captured: dict[str, str] = {}

    class _FakeTdxSyncService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def run_all(self) -> dict[str, object]:
            captured["ENABLE_EXTERNAL_GAP_FILL"] = os.environ.get(
                "ENABLE_EXTERNAL_GAP_FILL", "<unset>"
            )
            captured["TDX_ENABLE_EXTERNAL_GAP_FILL"] = os.environ.get(
                "TDX_ENABLE_EXTERNAL_GAP_FILL", "<unset>"
            )
            return {"summary": {"ok": 0, "skipped": 0, "failed": 0, "total": 0},
                    "tasks": []}

    # Patch the TdxSyncService import at the runtime path the module uses.
    fake_module = types.ModuleType("akshare_mcp.services.tdx_sync_service")
    fake_module.TdxSyncService = _FakeTdxSyncService  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "akshare_mcp.services.tdx_sync_service", fake_module
    )

    asyncio.run(module._run_tdx_core_tasks([], warmup_mode=True))

    assert captured.get("ENABLE_EXTERNAL_GAP_FILL") == "0", (
        f"warmup mode failed to override ENABLE_EXTERNAL_GAP_FILL: "
        f"got {captured.get('ENABLE_EXTERNAL_GAP_FILL')!r}"
    )
    assert captured.get("TDX_ENABLE_EXTERNAL_GAP_FILL") == "0", (
        f"warmup mode failed to override TDX_ENABLE_EXTERNAL_GAP_FILL: "
        f"got {captured.get('TDX_ENABLE_EXTERNAL_GAP_FILL')!r}"
    )

    # And the env override must be restored after the call returns.
    assert os.environ.get("ENABLE_EXTERNAL_GAP_FILL") == "1"
    assert os.environ.get("TDX_ENABLE_EXTERNAL_GAP_FILL") == "1"


def test_audit_sync_core_market_full_mode_does_not_override_gap_fill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """In full mode the script must respect whatever the env says."""
    db_path = tmp_path / "warmup_contract.sqlite3"
    monkeypatch.setenv("AKSHARE_MCP_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("AIASK_SQLITE_PATH", str(db_path))
    monkeypatch.setenv("ENABLE_EXTERNAL_GAP_FILL", "1")

    module = _load_module(
        CORE_MARKET_SCRIPT,
        "audit_sync_core_market_full_passthrough",
    )

    captured: dict[str, str] = {}

    class _FakeTdxSyncService:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def run_all(self) -> dict[str, object]:
            captured["ENABLE_EXTERNAL_GAP_FILL"] = os.environ.get(
                "ENABLE_EXTERNAL_GAP_FILL", "<unset>"
            )
            return {"summary": {"ok": 0, "skipped": 0, "failed": 0, "total": 0},
                    "tasks": []}

    fake_module = types.ModuleType("akshare_mcp.services.tdx_sync_service")
    fake_module.TdxSyncService = _FakeTdxSyncService  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "akshare_mcp.services.tdx_sync_service", fake_module
    )

    asyncio.run(module._run_tdx_core_tasks([], warmup_mode=False))

    assert captured.get("ENABLE_EXTERNAL_GAP_FILL") == "1", (
        "full mode must not override the user-set ENABLE_EXTERNAL_GAP_FILL"
    )


# -----------------------------------------------------------------------------
# T3 helper: structured error message format (R1.5)
# -----------------------------------------------------------------------------

def test_format_error_emits_grep_friendly_prefix() -> None:
    """The structured error format must always start with the audit prefix
    and carry a reason= and task_type= field so operators can grep without
    being confused by free-form messages."""
    for script_path, task_type in [
        (CORE_MARKET_SCRIPT, "core_market"),
        (FACTOR_CONTEXT_SCRIPT, "factor_context"),
    ]:
        module = _load_module(script_path, f"_format_error_{task_type}")
        assert hasattr(module, "_format_error"), (
            f"{script_path.name} must expose _format_error()"
        )
        msg = module._format_error(
            task_type=task_type,
            reason="something_went_wrong",
            db_path="/tmp/x.sqlite3",
            mode="warmup",
        )
        assert msg.startswith(f"[audit_sync_{task_type}]"), msg
        assert "reason=something_went_wrong" in msg, msg
        assert f"task_type={task_type}" in msg, msg
        assert "db_path=/tmp/x.sqlite3" in msg, msg
        assert "mode=warmup" in msg, msg
