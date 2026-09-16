from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
from argparse import Namespace
from datetime import datetime
from types import MethodType
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[3]
SCRIPT_DIR = ROOT / "scripts" / "factories"


def _source_env(*, extra_paths: tuple[Path, ...] = ()) -> dict[str, str]:
    """Environment for a child process that must import the packages from source.

    Tests further down this file already do this by hand. The two subprocess
    launches that did not pass ``env`` only worked when the packages happened to
    be pip-installed; from a source checkout (the pytest.ini pythonpath layout)
    the child died with ModuleNotFoundError before reaching the assertion it was
    meant to exercise.
    """
    env = os.environ.copy()
    roots = [
        ROOT / "packages" / "strategy-factory" / "src",
        ROOT / "packages" / "aiask-quant-core" / "src",
        ROOT / "packages" / "akshare-mcp" / "src",
        *extra_paths,
    ]
    existing = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]
    env["PYTHONPATH"] = os.pathsep.join([*existing, *(str(path) for path in roots)])
    return env


def test_maintained_runner_no_longer_dispatches_through_strategy_manager() -> None:
    text = (SCRIPT_DIR / "run_strategy_factory.py").read_text(encoding="utf-8", errors="ignore")

    assert "from akshare_mcp.tools.managers.strategy_manager import strategy_manager" not in text
    assert 'strategy_manager(action="factory_run_once"' not in text
    assert "target_codes=self.target_codes" in text
    assert "list_strategy_factory_dispatches" in text


def test_maintained_runner_uses_canonical_runtime_kwargs_before_akshare_fallback() -> None:
    text = (ROOT / "scripts" / "factories" / "run_strategy_factory.py").read_text(
        encoding="utf-8",
        errors="ignore",
    )

    assert "akshare_mcp.adapters.strategy_factory_runtime" not in text
    assert "build_scheduler_runtime_kwargs" in text
    assert "ensure_default_runtime_services" in text


def test_root_runner_accepts_scheduler_native_result() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_strategy_factory import _normalize_cycle_result
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    result = _normalize_cycle_result(
        {
            "status": "success",
            "run_id": "factory_run_test",
            "summary": {"submitted_count": 1},
        },
        elapsed_seconds=1.25,
    )

    assert result["success"] is True
    assert result["data"]["status"] == "success"
    assert result["data"]["elapsed_seconds"] == 1.25


def test_root_runner_resolves_interval_from_environment(monkeypatch) -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_strategy_factory import _resolve_runner_interval_sec
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    monkeypatch.setenv("STRATEGY_FACTORY_MARKET_HOURS_INTERVAL_SEC", "301")
    monkeypatch.setenv("STRATEGY_FACTORY_OFF_HOURS_INTERVAL_SEC", "1801")
    tz = ZoneInfo("Asia/Shanghai")

    assert _resolve_runner_interval_sec(17) == 17
    assert _resolve_runner_interval_sec(None, now=datetime(2026, 5, 21, 10, 0, tzinfo=tz)) == 301
    assert _resolve_runner_interval_sec(None, now=datetime(2026, 5, 21, 23, 0, tzinfo=tz)) == 1801


def test_runner_runtime_bootstrap_does_not_require_risk_model() -> None:
    code = textwrap.dedent(
        f"""
        import importlib.abc
        import sys

        class _BlockRiskModel(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname == "aiask_quant_core.risk_model":
                    raise ModuleNotFoundError("blocked risk model import", name=fullname)
                return None

        sys.meta_path.insert(0, _BlockRiskModel())
        sys.path.insert(0, {str(SCRIPT_DIR)!r})

        import run_strategy_factory as runner
        from strategy_factory.infrastructure.runtime_services import configure_runtime_services, clear_runtime_services

        class _Db:
            pass

        clear_runtime_services()
        configure_runtime_services(
            db_provider=lambda: _Db(),
            factor_scheduler=lambda: None,
            factor_mining_factory=lambda: None,
            factor_mining_support_factory=lambda: None,
            factor_pool_gateway=lambda: None,
            quant_manager_callable=lambda *args, **kwargs: None,
            runtime_warmup_runner=lambda *args, **kwargs: None,
            signal_tracker_runtime_factory=lambda *args, **kwargs: None,
            signal_tracker_runtime_support_factory=lambda *args, **kwargs: None,
            incubation_runtime_factory=lambda *args, **kwargs: None,
            incubation_runtime_support_factory=lambda *args, **kwargs: None,
            market_event_ingest_support_factory=lambda: None,
            strategy_promotion_pipeline_service=lambda: None,
            strategy_runtime_control_service=lambda: None,
            event_context_builder=lambda *args, **kwargs: {{}},
            strategy_vector_platform_factory=lambda: None,
            execution_audit_snapshot_builder=lambda *args, **kwargs: {{}},
            closure_review_builder=lambda *args, **kwargs: {{}},
            strategy_llm_provider_loader=lambda *args, **kwargs: None,
        )

        kwargs = runner._runner._load_strategy_factory_runtime_kwargs()
        assert callable(kwargs["db_provider"])
        assert type(kwargs["runtime_adapters"]).__name__ == "RuntimeAdapters"
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=_source_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr


def test_runner_uses_preconfigured_runtime_services_without_akshare_imports() -> None:
    code = textwrap.dedent(
        f"""
        import builtins
        import importlib.abc
        import sys

        class _BlockAkshare(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if str(fullname).startswith("akshare_mcp"):
                    raise ModuleNotFoundError("blocked akshare import", name=fullname)
                return None

        sys.meta_path.insert(0, _BlockAkshare())
        sys.path.insert(0, {str(SCRIPT_DIR)!r})
        sys.path.insert(0, {str(ROOT / "packages" / "strategy-factory" / "src")!r})
        sys.path.insert(0, {str(ROOT / "packages" / "aiask-quant-core" / "src")!r})

        import run_strategy_factory as runner
        from strategy_factory.infrastructure.runtime_services import configure_runtime_services, clear_runtime_services

        class _Db:
            pass

        clear_runtime_services()
        configure_runtime_services(
            db_provider=lambda: _Db(),
            factor_scheduler=lambda: None,
            factor_mining_factory=lambda: None,
            factor_mining_support_factory=lambda: None,
            factor_pool_gateway=lambda: None,
            quant_manager_callable=lambda *args, **kwargs: None,
            runtime_warmup_runner=lambda *args, **kwargs: None,
            signal_tracker_runtime_factory=lambda *args, **kwargs: None,
            signal_tracker_runtime_support_factory=lambda *args, **kwargs: None,
            incubation_runtime_factory=lambda *args, **kwargs: None,
            incubation_runtime_support_factory=lambda *args, **kwargs: None,
            market_event_ingest_support_factory=lambda: None,
            strategy_promotion_pipeline_service=lambda: None,
            strategy_runtime_control_service=lambda: None,
            event_context_builder=lambda *args, **kwargs: {{}},
            sentiment_analyzer=object(),
            financial_semantic_service_factory=lambda: None,
            index_kline_provider=lambda *args, **kwargs: {{}},
            strategy_dsl_compiler=lambda *args, **kwargs: None,
            strategy_vector_platform_factory=lambda: None,
            execution_audit_snapshot_builder=lambda *args, **kwargs: {{}},
            closure_review_builder=lambda *args, **kwargs: {{}},
            strategy_llm_provider_loader=lambda *args, **kwargs: None,
        )

        kwargs = runner._runner._load_strategy_factory_runtime_kwargs()
        assert callable(kwargs["db_provider"])
        assert type(kwargs["runtime_adapters"]).__name__ == "RuntimeAdapters"
        print("ok")
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=_source_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert completed.returncode == 0, completed.stderr
    assert "ok" in completed.stdout


def test_public_runtime_api_keeps_mcp_aliases_compatible() -> None:
    sys.path.insert(0, str(ROOT / "packages" / "strategy-factory" / "src"))
    try:
        from strategy_factory.api.runtime import (
            MCPRuntimeAdapters,
            RuntimeAdapters,
            build_mcp_runtime_adapters,
            build_runtime_adapters,
        )
    finally:
        try:
            sys.path.remove(str(ROOT / "packages" / "strategy-factory" / "src"))
        except ValueError:
            pass

    assert RuntimeAdapters is MCPRuntimeAdapters
    assert build_runtime_adapters is build_mcp_runtime_adapters


def test_supervisor_does_not_force_strategy_interval_by_default() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_all_factories import _build_specs
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    args = Namespace(
        no_strategy=False,
        strategy_interval=None,
        strategy_codes=None,
        strategy_silent_restart=None,
        silent_restart=2400,
        no_factor=True,
        factor_interval=0,
        factor_candidates=None,
        factor_generations=None,
        factor_engines=None,
        factor_codes=None,
        factor_silent_restart=0,
        no_incubation=True,
        incubation_run_time="18:30",
        incubation_dry_run=False,
        incubation_verbose=False,
        incubation_silent_restart=0,
        no_signal_tracker=True,
        signal_tracker_run_time="17:00",
        signal_tracker_verbose=False,
        signal_tracker_silent_restart=0,
    )

    specs = _build_specs(args)

    assert len(specs) == 1
    assert specs[0].name == "strategy_factory"
    assert "--interval" not in specs[0].args


def test_supervisor_preserves_explicit_strategy_interval() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_all_factories import _build_specs
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    args = Namespace(
        no_strategy=False,
        strategy_interval=600,
        strategy_codes=["600519"],
        strategy_silent_restart=None,
        silent_restart=2400,
        no_factor=True,
        factor_interval=0,
        factor_candidates=None,
        factor_generations=None,
        factor_engines=None,
        factor_codes=None,
        factor_silent_restart=0,
        no_incubation=True,
        incubation_run_time="18:30",
        incubation_dry_run=False,
        incubation_verbose=False,
        incubation_silent_restart=0,
        no_signal_tracker=True,
        signal_tracker_run_time="17:00",
        signal_tracker_verbose=False,
        signal_tracker_silent_restart=0,
    )

    specs = _build_specs(args)

    assert specs[0].args[:2] == ("--interval", "600")
    assert specs[0].args[2:] == ("--codes", "600519")


def test_supervisor_console_queue_decouples_child_output(monkeypatch) -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import run_all_factories
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    async def _exercise():
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        monkeypatch.setattr(run_all_factories, "_CONSOLE_QUEUE", queue)
        monkeypatch.setattr(run_all_factories, "_CONSOLE_DROPPED", 0)
        run_all_factories._console("first")
        run_all_factories._console("second")
        assert queue.qsize() == 1
        assert run_all_factories._CONSOLE_DROPPED == 1
        line = queue.get_nowait()
        assert "first" in line

    try:
        asyncio.run(_exercise())
    finally:
        monkeypatch.setattr(run_all_factories, "_CONSOLE_QUEUE", None)


def test_supervisor_child_output_reader_accepts_long_lines(monkeypatch) -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        import run_all_factories
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    class _LongLineStream:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self._offset = 0

        async def read(self, n: int = -1) -> bytes:
            await asyncio.sleep(0)
            if self._offset >= len(self._payload):
                return b""
            if n is None or n < 0:
                n = len(self._payload) - self._offset
            end = min(len(self._payload), self._offset + n)
            chunk = self._payload[self._offset:end]
            self._offset = end
            return chunk

    async def _exercise():
        messages: list[str] = []
        monkeypatch.setattr(run_all_factories, "_console", messages.append)
        monkeypatch.setattr(run_all_factories, "CHILD_OUTPUT_READ_CHUNK_SIZE", 1024)
        spec = run_all_factories.FactorySpec(
            name="strategy_factory",
            script=SCRIPT_DIR / "run_strategy_factory.py",
        )
        state = run_all_factories.FactoryState(name=spec.name)
        long_text = "x" * 70000
        log_file = io.StringIO()

        await run_all_factories._tee_child_output(
            spec,
            _LongLineStream((long_text + "\n").encode("utf-8")),
            log_file,
            stream_logs=True,
            state=state,
        )

        assert log_file.getvalue() == long_text + "\n"
        assert state.last_output_at is not None
        assert len(messages) > 1
        assert all("strategy_factory" in item for item in messages)

    asyncio.run(_exercise())


def test_root_runner_reports_scheduler_native_failure() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_strategy_factory import _normalize_cycle_result
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    result = _normalize_cycle_result(
        {
            "status": "failed",
            "summary": {"error": "readiness failed"},
        },
        elapsed_seconds=2.0,
    )

    assert result["success"] is False
    assert result["error"] == "readiness failed"
    assert result["data"]["status"] == "failed"


def test_root_runner_reports_scheduler_native_partial_as_degraded() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_strategy_factory import _normalize_cycle_result
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    result = _normalize_cycle_result(
        {
            "status": "partial",
            "run_id": "factory_run_partial",
            "summary": {"error": "paper cycle failed"},
        },
        elapsed_seconds=3.5,
    )

    assert result["success"] is False
    assert result["error"] == "paper cycle failed"
    assert result["data"]["status"] == "partial"
    assert result["data"]["degraded"] is True


def test_root_runner_separates_partial_from_success_count() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_strategy_factory import StrategyFactoryRunner
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    runner = StrategyFactoryRunner(run_once=True)

    async def _partial_cycle(self):
        return {"status": "partial", "summary": {"error": "degraded"}}

    runner._execute_cycle = MethodType(_partial_cycle, runner)

    asyncio.run(runner.run())

    assert runner._run_count == 1
    assert runner._success_count == 0
    assert runner._partial_count == 1
    assert runner._failure_count == 0


def test_root_runner_claims_dispatch_before_run_once() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_strategy_factory import StrategyFactoryRunner
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    class _Db:
        def __init__(self):
            self.updated: list[dict] = []

        async def list_strategy_factory_dispatches(self, status: str, limit: int = 1):
            return [
                {
                    "dispatch_id": "dispatch_test",
                    "status": status,
                    "execution_mode": "shadow_readonly",
                    "metadata": {"target_codes": ["600000"]},
                }
            ]

        async def update_strategy_factory_dispatch(self, dispatch_id: str, **kwargs):
            payload = {"dispatch_id": dispatch_id, **kwargs}
            self.updated.append(payload)
            return payload

    db = _Db()
    runner = StrategyFactoryRunner(run_once=False)
    claimed = asyncio.run(runner._claim_queued_dispatch({"db_provider": lambda: db}))

    assert claimed["dispatch_id"] == "dispatch_test"
    assert claimed["status"] == "running"
    assert claimed["metadata"]["runner"] == "standalone"
    assert runner._active_dispatch["dispatch_id"] == "dispatch_test"


def test_root_runner_dispatch_run_loads_default_universe_when_codes_omitted() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_strategy_factory import StrategyFactoryRunner
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    class _Db:
        async def list_stock_universe(self, *, limit: int, offset: int = 0):
            assert limit == 5
            assert offset == 0
            return [
                {"code": "600519.SH"},
                {"code": "000001.SZ"},
                {"code": "300750.SZ"},
                {"code": "601318.SH"},
                {"code": "600036.SH"},
            ]

    class _Scheduler:
        def __init__(self):
            self._dispatch_tasks: dict[str, asyncio.Task] = {}
            self.calls: list[list[str]] = []

        async def dispatch_run(self, db=None, *, execution_mode=None, target_codes=None):
            self.calls.append(list(target_codes or []))
            return {
                "dispatch_id": f"dispatch_{len(self.calls)}",
                "status": "success",
                "run_id": f"run_{len(self.calls)}",
            }

    scheduler = _Scheduler()
    runner = StrategyFactoryRunner(
        run_once=True,
        dispatch_run_mode=True,
        dispatch_concurrency_limit=5,
        dispatch_shard_size=1,
        dispatch_default_universe=True,
        dispatch_default_universe_limit=5,
    )

    result = asyncio.run(
        runner._execute_dispatch_run_batch(
            scheduler,
            {"db_provider": lambda: _Db()},
        )
    )

    assert scheduler.calls == [["600519"], ["000001"], ["300750"], ["601318"], ["600036"]]
    assert result["status"] == "success"
    assert result["summary"]["dispatch_count"] == 5
    assert result["summary"]["default_universe_dispatch"] is True
    assert result["summary"]["default_universe_code_count"] == 5


def test_root_runner_marks_active_dispatch_failed_on_cancel() -> None:
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from run_strategy_factory import StrategyFactoryRunner
    finally:
        try:
            sys.path.remove(str(SCRIPT_DIR))
        except ValueError:
            pass

    class _Db:
        def __init__(self):
            self.updated: list[dict] = []

        async def update_strategy_factory_dispatch(self, dispatch_id: str, **kwargs):
            payload = {"dispatch_id": dispatch_id, **kwargs}
            self.updated.append(payload)
            return payload

    db = _Db()
    runner = StrategyFactoryRunner(run_once=False)
    runner._active_dispatch = {
        "dispatch_id": "dispatch_cancelled",
        "metadata": {"source_action": "factory_run_once"},
        "_db": db,
    }

    async def _cancel_cycle(self):
        raise asyncio.CancelledError()

    runner._execute_cycle = MethodType(_cancel_cycle, runner)

    asyncio.run(runner.run())

    assert db.updated
    update = db.updated[-1]
    assert update["dispatch_id"] == "dispatch_cancelled"
    assert update["status"] == "failed"
    assert update["error"] == "cancelled"
    assert update["metadata"]["cancelled"] is True
    assert update["metadata"]["error_type"] == "CancelledError"
    assert runner._active_dispatch is None
    assert runner._failure_count == 1


def test_strategy_factory_source_has_no_akshare_imports() -> None:
    source_root = ROOT / "packages" / "strategy-factory" / "src" / "strategy_factory"
    offenders: list[str] = []
    pattern = re.compile(r"^\s*(?:from|import)\s+akshare_mcp\b", re.MULTILINE)
    for path in source_root.rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if pattern.search(text):
            offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_strategy_factory_new_report_paths_use_public_quality_reporting_api() -> None:
    checked_paths = [
        ROOT / "packages/strategy-factory/src/strategy_factory/application/_submitter_actions/runner.py",
        ROOT / "packages/strategy-factory/src/strategy_factory/application/submitter.py",
        ROOT / "packages/strategy-factory/src/strategy_factory/application/_submitter_helpers.py",
        ROOT / "packages/strategy-factory/src/strategy_factory/application/_submitter_policy.py",
        ROOT / "packages/strategy-factory/src/strategy_factory/infrastructure/mcp_adapters.py",
    ]
    forbidden_tokens = ("factory_pkg._run_validation_report", "factory_pkg._run_risk_report")
    violations: list[str] = []
    for path in checked_paths:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in forbidden_tokens:
            if token in text:
                violations.append(f"{path.relative_to(ROOT)}: {token}")

    assert violations == []


def test_strategy_factory_without_akshare_reports_runtime_provider_error() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(ROOT / "packages" / "aiask-quant-core" / "src"),
            str(ROOT / "packages" / "strategy-factory" / "src"),
        ]
    )
    script = r'''
import builtins
real_import = builtins.__import__

def guard(name, globals=None, locals=None, fromlist=(), level=0):
    if str(name).startswith("akshare_mcp"):
        raise ModuleNotFoundError("blocked " + str(name), name=name)
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guard
from strategy_factory import get_strategy_factory_scheduler

scheduler = get_strategy_factory_scheduler()
print("scheduler_create=ok")
try:
    scheduler._load_db()
except RuntimeError as exc:
    print(str(exc))
else:
    raise AssertionError("expected runtime provider error")
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "scheduler_create=ok" in result.stdout
    assert "strategy-factory requires runtime providers for full cycle execution" in result.stdout
    assert "missing_provider=db_provider" in result.stdout


def test_public_submodule_imports_do_not_eagerly_require_quant_core() -> None:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "packages" / "strategy-factory" / "src")
    script = r'''
import builtins
real_import = builtins.__import__

def guard(name, globals=None, locals=None, fromlist=(), level=0):
    if str(name).startswith("aiask_quant_core"):
        raise ModuleNotFoundError("blocked " + str(name), name=name)
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guard
from strategy_factory.api.semantic_contract import build_signal_evidence_records
from strategy_factory.api.market_views import extract_bulk_stock_cursor

print(build_signal_evidence_records.__name__)
print(extract_bulk_stock_cursor({"bulk_stock_matrix_next_task_offset": 3}, source="test").get("next_task_offset"))
'''
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "build_signal_evidence_records" in result.stdout
