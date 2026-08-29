"""策略工厂调度器实现。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import logging
import os
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from ..domain.constants import (
    AUTONOMY_CANDIDATES_PER_TASK,
    AUTONOMY_STARTUP_DELAY_SEC,
    FACTORY_DAILY_RUN_TIME,
    FACTORY_ERROR_BACKOFF_SEC,
    FACTORY_EVENT_RUNTIME_MODE,
    FACTORY_FACTOR_AUTO_REFRESH,
    FACTORY_FACTOR_REFRESH_TIMEOUT_SEC,
    FACTORY_MARKET_HOURS_INTERVAL_SEC,
    FACTORY_MAX_DAILY_RUNS,
    FACTORY_OFF_HOURS_INTERVAL_SEC,
    FACTORY_READINESS_HARD_BLOCK,
    FACTORY_READINESS_MIN_COMPLETION_RATIO,
    FACTORY_READINESS_MIN_SCORE,
    FACTORY_RUNTIME_ENABLED,
    FACTORY_SCHEDULE_MODE,
    FACTORY_STARTUP_WARMUP_ENABLED,
    FACTORY_STARTUP_WARMUP_FORCE,
    FACTORY_STARTUP_WARMUP_LIMIT,
    FACTORY_STARTUP_WARMUP_TASK_TYPE,
    RESEARCH_TASK_CONCURRENCY,
    is_factory_factor_auto_refresh_enabled,
    is_factory_readiness_hard_block_enabled,
    is_factory_runtime_enabled,
    resolve_event_runtime_mode,
)
from .cycle_runner import FactoryCycleRunner, FactoryRunContext
from .runtime import (
    _call_optional_async as _runtime_call_optional_async,
    get_strategy_factory_package as _runtime_get_strategy_factory_package,
)
from .run_models import (
    FactoryRunStatus,
    StageStatus,
    build_stage_result,
    resolve_run_status,
    summarize_stage_results,
)
from .utils import _extract_event_context as _local_extract_event_context
from ..domain.targets import _extract_target_codes_from_payload, _normalize_target_codes
from ..infrastructure.mcp_services import get_autonomy_lifecycle_runtime, get_runtime_warmup_runner

if TYPE_CHECKING:
    from ..api.contracts import (
        AutonomyGateway,
        FactorResearchGateway,
        IncubationGateway,
        RiskGateway,
        ValidationGateway,
        VectorSearchGateway,
    )
    from ..infrastructure.runtime_adapters import RuntimeAdapters

logger = logging.getLogger(__name__)

_MARKET_TIMEZONE_NAME = str(os.getenv("STRATEGY_MARKET_TIMEZONE") or "Asia/Shanghai").strip() or "Asia/Shanghai"
try:
    _MARKET_TIMEZONE = ZoneInfo(_MARKET_TIMEZONE_NAME)
except Exception:
    _MARKET_TIMEZONE = timezone(timedelta(hours=8))

_FALLBACK_AUTONOMY_PHASE_ORDER = (
    "prepared",
    "generating",
    "reviewing",
    "recording",
    "submitting",
    "completed",
)

_RUNTIME_PROVIDER_ERROR = (
    "strategy-factory requires runtime providers for full cycle execution; "
    "pass db_provider=... and runtime_adapters=... or configure the AKShare runtime bridge"
)


def _runtime_provider_error(provider: str, exc: BaseException) -> RuntimeError:
    return RuntimeError(f"{_RUNTIME_PROVIDER_ERROR}; missing_provider={provider}")


def _summarize_autonomy_lifecycle_fallback(lifecycle: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = dict(lifecycle or {})
    return {
        "state": payload.get("state"),
        "current_phase": payload.get("current_phase"),
        "failed_phase": payload.get("failed_phase"),
        "terminal_phase": payload.get("terminal_phase"),
        "phase_status_counts": dict(payload.get("phase_status_counts") or {}),
        "completed_phase_count": int(payload.get("completed_phase_count") or 0),
        "event_count": int(payload.get("event_count") or len(payload.get("events") or [])),
        "phase_order": list(payload.get("phase_order") or _FALLBACK_AUTONOMY_PHASE_ORDER),
    }


def _load_autonomy_lifecycle_runtime():
    try:
        return get_autonomy_lifecycle_runtime()
    except Exception:
        return SimpleNamespace(
            AUTONOMY_PHASE_ORDER=_FALLBACK_AUTONOMY_PHASE_ORDER,
            summarize_autonomy_lifecycle=_summarize_autonomy_lifecycle_fallback,
        )


_AUTONOMY_LIFECYCLE_RUNTIME = _load_autonomy_lifecycle_runtime()
AUTONOMY_PHASE_ORDER = _AUTONOMY_LIFECYCLE_RUNTIME.AUTONOMY_PHASE_ORDER
summarize_autonomy_lifecycle = _AUTONOMY_LIFECYCLE_RUNTIME.summarize_autonomy_lifecycle

def _extract_event_context(*args, **kwargs):
    return _local_extract_event_context(*args, **kwargs)


def get_strategy_factory_package():
    return _runtime_get_strategy_factory_package()


async def _call_optional_async(target: Any, method_name: str, *args, default=None, **kwargs):
    return await _runtime_call_optional_async(target, method_name, *args, default=default, **kwargs)


class _StrategyFactorySchedulerRuntimeMixin:
        @staticmethod
        def _filter_supported_injection_kwargs(factory: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
            if not kwargs:
                return {}
            try:
                signature = inspect.signature(factory)
            except (TypeError, ValueError):
                return dict(kwargs)
            if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
                return dict(kwargs)
            allowed = {
                name
                for name, parameter in signature.parameters.items()
                if parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            }
            return {key: value for key, value in kwargs.items() if key in allowed}

        @classmethod
        def _call_factory_with_supported_kwargs(cls, factory: Any, kwargs: dict[str, Any]):
            filtered_kwargs = cls._filter_supported_injection_kwargs(factory, kwargs)
            return factory(**filtered_kwargs) if filtered_kwargs else factory()

        def _load_db(self):
            if callable(self._db_provider):
                return self._db_provider()
            from ..infrastructure.mcp_services import get_db_provider

            return get_db_provider()()

        def _adapt_gateway_repository(self, db):
            runtime_repo = getattr(self._runtime_adapters, "repository", None) if self._runtime_adapters is not None else None
            if runtime_repo is not None and getattr(runtime_repo, "raw", None) is db:
                return runtime_repo
            return db

        def _build_deduplicator(self, factory_pkg):
            deduplicator_cls = factory_pkg.Deduplicator
            kwargs = {}
            if self._vector_gateway is not None:
                kwargs["vector_gateway"] = self._vector_gateway
            return self._call_factory_with_supported_kwargs(deduplicator_cls, kwargs)

        def _build_submitter(self, factory_pkg):
            submitter_cls = factory_pkg.StrategySubmitter
            kwargs = {}
            if self._validation_gateway is not None:
                kwargs["validation_gateway"] = self._validation_gateway
            if self._risk_gateway is not None:
                kwargs["risk_gateway"] = self._risk_gateway
            if self._incubation_gateway is not None:
                kwargs["incubation_gateway"] = self._incubation_gateway
            return self._call_factory_with_supported_kwargs(submitter_cls, kwargs)

        def _get_autonomy_gateway(self) -> "AutonomyGateway":
            if self._autonomy_gateway is None:
                from ..infrastructure.runtime_adapters import AutonomyGatewayImpl

                self._autonomy_gateway = AutonomyGatewayImpl()
            return self._autonomy_gateway

        def _get_factor_research_gateway(self) -> "FactorResearchGateway":
            if self._factor_research_gateway is None:
                from ..infrastructure.runtime_adapters import FactorResearchGatewayImpl

                factory_pkg = get_strategy_factory_package()
                self._factor_research_gateway = FactorResearchGatewayImpl(
                    builder=getattr(factory_pkg, "FactorResearchBuilder", None),
                )
            return self._factor_research_gateway

        async def _run_startup_warmup(self) -> dict[str, Any]:
            if not FACTORY_STARTUP_WARMUP_ENABLED:
                return {
                    "ok": True,
                    "status": "disabled",
                    "task_type": FACTORY_STARTUP_WARMUP_TASK_TYPE,
                    "force": FACTORY_STARTUP_WARMUP_FORCE,
                    "matched": 0,
                    "executed": 0,
                    "failed": 0,
                    "executed_task_ids": [],
                    "failed_schedule_ids": [],
                    "schedules": [],
                }

            from . import factory_scheduler as scheduler_module

            runner = scheduler_module.get_runtime_warmup_runner()
            try:
                return await runner(
                    task_type=FACTORY_STARTUP_WARMUP_TASK_TYPE,
                    force=FACTORY_STARTUP_WARMUP_FORCE,
                    limit=FACTORY_STARTUP_WARMUP_LIMIT,
                    source="strategy_factory",
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "status": "failed",
                    "task_type": FACTORY_STARTUP_WARMUP_TASK_TYPE,
                    "force": FACTORY_STARTUP_WARMUP_FORCE,
                    "matched": 0,
                    "executed": 0,
                    "failed": 0,
                    "executed_task_ids": [],
                    "failed_schedule_ids": [],
                    "schedules": [],
                    "error": str(exc),
                }

        @staticmethod
        def _aggregate_vector_submission_metrics(submission_result: Optional[dict]) -> dict:
            items = list((submission_result or {}).get("strategies") or [])
            backend_counts: dict[str, int] = {}
            requested_backend_counts: dict[str, int] = {}
            fallback_count = 0
            latencies: list[float] = []
            profile_count = 0
            for item in items:
                profile_id = item.get("vector_profile_id")
                if profile_id:
                    profile_count += 1
                backend = str(item.get("vector_backend_used") or item.get("vector_backend") or "").strip()
                if backend:
                    backend_counts[backend] = backend_counts.get(backend, 0) + 1
                requested_backend = str(item.get("vector_backend_requested") or "").strip()
                if requested_backend:
                    requested_backend_counts[requested_backend] = requested_backend_counts.get(requested_backend, 0) + 1
                if item.get("vector_fallback_used"):
                    fallback_count += 1
                latency = item.get("vector_latency_ms")
                if latency is not None:
                    try:
                        latencies.append(float(latency))
                    except (TypeError, ValueError):
                        pass
            return {
                "vector_profile_count": profile_count,
                "vector_backend_counts": backend_counts,
                "vector_backend_requested_counts": requested_backend_counts,
                "vector_fallback_count": fallback_count,
                "vector_latency_ms_avg": round(sum(latencies) / len(latencies), 3) if latencies else None,
                "vector_latency_ms_max": round(max(latencies), 3) if latencies else None,
            }
