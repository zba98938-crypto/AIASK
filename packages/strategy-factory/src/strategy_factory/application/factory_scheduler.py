"""策略工厂调度器实现。"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import logging
import os
import random
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
from ..domain.trading_calendar import get_trading_calendar
from .cycle_runner import FactoryCycleRunner, FactoryRunContext
from .factory_execution import (
    FACTORY_ENGINE_VERSION,
    resolve_factory_engine_version,
    resolve_factory_execution_mode,
)
from .run_models import (
    FactoryRunStatus,
    StageStatus,
    build_stage_result,
    resolve_run_status,
    summarize_stage_results,
)
from .scheduler_metrics import SchedulerMetrics
from .services.readiness_service import ReadinessService
from .runtime import (
    _call_optional_async as _runtime_call_optional_async,
    get_strategy_factory_package as _runtime_get_strategy_factory_package,
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

from ._factory_scheduler_analysis import _StrategyFactorySchedulerAnalysisMixin
from ._factory_scheduler_runtime import _StrategyFactorySchedulerRuntimeMixin
from ._factory_scheduler_loop import _StrategyFactorySchedulerLoopMixin
from .factory_task_board import FactoryTaskBoard


class StrategyFactoryScheduler(_StrategyFactorySchedulerAnalysisMixin, _StrategyFactorySchedulerRuntimeMixin, _StrategyFactorySchedulerLoopMixin):
        """策略工厂调度器，支持 continuous（24/7循环）和 daily（每日定时）两种模式。"""

        def __init__(
            self,
            run_time: Optional[time] = None,
            *,
            db_provider: Optional[Callable[[], Any]] = None,
            vector_gateway: Optional["VectorSearchGateway"] = None,
            validation_gateway: Optional["ValidationGateway"] = None,
            risk_gateway: Optional["RiskGateway"] = None,
            incubation_gateway: Optional["IncubationGateway"] = None,
            autonomy_gateway: Optional["AutonomyGateway"] = None,
            factor_research_gateway: Optional["FactorResearchGateway"] = None,
            runtime_adapters: Optional["RuntimeAdapters"] = None,
        ):
            self.schedule_mode: str = FACTORY_SCHEDULE_MODE if FACTORY_SCHEDULE_MODE in ("continuous", "daily") else "continuous"
            self._market_timezone = _MARKET_TIMEZONE
            self._readiness_service = ReadinessService()
            # daily 模式的运行时间
            if run_time is not None:
                self.run_time = run_time
            else:
                try:
                    parts = FACTORY_DAILY_RUN_TIME.split(":")
                    self.run_time = time(int(parts[0]), int(parts[1]))
                except Exception:
                    self.run_time = time(19, 0)
            self.max_daily_runs: int = FACTORY_MAX_DAILY_RUNS
            self._task: Optional[asyncio.Task] = None
            self._running = False
            self._shutdown_grace_sec: float = float(
                os.getenv("STRATEGY_FACTORY_SHUTDOWN_GRACE_SEC", "0.25") or 0.25
            )
            self.last_run: Optional[datetime] = None
            self.last_result: Optional[dict] = None
            self._daily_run_count: int = 0
            self._daily_run_date: Optional[str] = None  # "YYYY-MM-DD"
            self._cycle_count: int = 0
            self._run_once_task: Optional[asyncio.Task] = None
            self._run_once_task_lock: Optional[asyncio.Lock] = None
            self._run_once_task_lock_loop: Optional[asyncio.AbstractEventLoop] = None
            self.execution_mode = resolve_factory_execution_mode()
            self.engine_version = resolve_factory_engine_version(
                self.execution_mode,
                default=FACTORY_ENGINE_VERSION,
            )
            self._dispatch_tasks: Dict[str, asyncio.Task] = {}
            self._dispatch_semaphore: Optional[asyncio.Semaphore] = None
            self._dispatch_semaphore_limit: int = 0
            self._dispatch_semaphore_loop: Optional[asyncio.AbstractEventLoop] = None
            self._active_dispatch_id: Optional[str] = None
            self._latest_dispatch_id: Optional[str] = None
            self._last_theme_seed_at: Optional[datetime] = None
            self._last_theme_exposure_refresh_at: Optional[datetime] = None
            self._last_theme_regression_run_at: Optional[datetime] = None
            self._last_event_outbox_drain_at: Optional[datetime] = None
            self._last_event_theme_maintenance_result: Optional[dict[str, Any]] = None
            # Circuit breaker state
            self._consecutive_failures: int = 0
            self._max_consecutive_failures: int = int(
                os.getenv("STRATEGY_FACTORY_MAX_CONSECUTIVE_FAILURES", "5") or 5
            )
            self._circuit_open_until: Optional[datetime] = None
            self._circuit_open_backoff_sec: int = int(
                os.getenv("STRATEGY_FACTORY_CIRCUIT_OPEN_BACKOFF_SEC", "1800") or 1800
            )
            self._circuit_open_backoff_sec_initial: int = self._circuit_open_backoff_sec
            # 优化 2：断路器 Half-Open 状态
            self._circuit_state: str = "closed"  # closed / open / half_open
            # P2-D 孵化反馈：按 family 跟踪 Gate 通过率（指数平滑，α=0.3）
            self._family_gate_feedback: Dict[str, Dict[str, float]] = {}
            # 优化 1：交易日历
            self._trading_calendar = get_trading_calendar()
            # 优化 5：可观测性指标
            self._metrics = SchedulerMetrics(uptime_start=datetime.now(_MARKET_TIMEZONE).isoformat())
            self._db_provider = db_provider
            self._runtime_adapters = runtime_adapters
            self._vector_gateway = vector_gateway
            self._validation_gateway = validation_gateway
            self._risk_gateway = risk_gateway
            self._incubation_gateway = incubation_gateway
            self._autonomy_gateway = autonomy_gateway
            self._factor_research_gateway = factor_research_gateway
            self._task_board = FactoryTaskBoard.from_env()
            if runtime_adapters is not None:
                self._vector_gateway = self._vector_gateway or getattr(runtime_adapters, "vector_search", None)
                self._validation_gateway = self._validation_gateway or getattr(runtime_adapters, "validation", None)
                self._risk_gateway = self._risk_gateway or getattr(runtime_adapters, "risk", None)
                self._incubation_gateway = self._incubation_gateway or getattr(runtime_adapters, "incubation", None)
                self._autonomy_gateway = self._autonomy_gateway or getattr(runtime_adapters, "autonomy", None)
                self._factor_research_gateway = self._factor_research_gateway or getattr(runtime_adapters, "factor_research", None)

        def _get_run_once_task_lock(self) -> asyncio.Lock:
            loop = asyncio.get_running_loop()
            if self._run_once_task_lock is None or self._run_once_task_lock_loop is not loop:
                self._run_once_task_lock = asyncio.Lock()
                self._run_once_task_lock_loop = loop
            return self._run_once_task_lock

        def _now(self) -> datetime:
            return datetime.now(self._market_timezone)

        def _is_trading_day(self, now: datetime) -> bool:
            """判断是否为 A 股交易日（排除节假日，包含调休日）。"""
            return self._trading_calendar.is_trading_day(now.date())

        def _compute_error_backoff(self) -> float:
            """优化 6：指数退避 + 抖动，避免惊群效应。"""
            from ..domain.constants import FACTORY_ERROR_BACKOFF_SEC
            base = FACTORY_ERROR_BACKOFF_SEC
            max_backoff = int(os.getenv("STRATEGY_FACTORY_ERROR_BACKOFF_MAX_SEC", "1800") or 1800)
            # 指数退避：120s → 240s → 480s → 960s（上限 max_backoff）
            backoff = min(base * (2 ** max(self._consecutive_failures - 1, 0)), max_backoff)
            # 添加 ±20% 抖动
            jitter = backoff * random.uniform(-0.2, 0.2)
            return max(30.0, backoff + jitter)

        def get_scheduler_metrics(self) -> dict:
            """暴露结构化调度器指标（供 API/MCP 调用）。"""
            self._metrics.update_family_diversity(self._family_gate_feedback)
            self._metrics.consecutive_failures = self._consecutive_failures
            return self._metrics.to_dict()

        async def _restore_scheduler_state_legacy(self) -> None:
            """PR-S4: 启动时尝试从 DB 恢复 EMA 反馈 / 断路器 / cycle_count 状态。

            DB 未提供 ``load_scheduler_state`` 时静默跳过；任何异常都降级为干净启动。
            """
            try:
                db = self._load_db() if callable(getattr(self, "_load_db", None)) else None
                if db is None:
                    return
                loader = getattr(db, "load_scheduler_state", None)
                if not callable(loader):
                    return
                state = loader()
                if inspect.isawaitable(state):
                    state = await state
                if not state:
                    return
                feedback = state.get("family_gate_feedback")
                if isinstance(feedback, dict):
                    self._family_gate_feedback = {
                        str(k): {"ema_submit_count": float((v or {}).get("ema_submit_count") or 0.0)}
                        for k, v in feedback.items()
                    }
                try:
                    self._cycle_count = max(0, int(state.get("cycle_count") or 0))
                except Exception:
                    pass
                try:
                    self._consecutive_failures = max(0, int(state.get("consecutive_failures") or 0))
                except Exception:
                    pass
                circuit_state = str(state.get("circuit_state") or "closed").strip().lower()
                if circuit_state in {"closed", "open", "half_open"}:
                    self._circuit_state = circuit_state
                logger.info(
                    "StrategyFactory: restored scheduler state, families=%d cycle=%d",
                    len(self._family_gate_feedback),
                    self._cycle_count,
                )
            except Exception as exc:
                logger.debug("StrategyFactory: scheduler state restore failed: %s", exc)

        async def _restore_scheduler_state(self, db=None) -> None:
            try:
                resolved_db = db
                if resolved_db is None and callable(getattr(self, "_load_db", None)):
                    resolved_db = self._load_db()
                if resolved_db is None:
                    return
                loader = getattr(resolved_db, "load_scheduler_state", None)
                if not callable(loader):
                    return
                state = loader()
                if inspect.isawaitable(state):
                    state = await state
                if not isinstance(state, dict) or not state:
                    return

                feedback = state.get("family_gate_feedback")
                if isinstance(feedback, dict):
                    restored: Dict[str, Dict[str, Any]] = {}
                    for family, raw_entry in feedback.items():
                        family_token = str(family or "").strip()
                        if not family_token or not isinstance(raw_entry, dict):
                            continue
                        entry: Dict[str, Any] = {}
                        for key, value in raw_entry.items():
                            key_token = str(key or "").strip()
                            if not key_token:
                                continue
                            if isinstance(value, bool):
                                entry[key_token] = bool(value)
                            elif isinstance(value, (int, float)):
                                entry[key_token] = float(value)
                            elif isinstance(value, str):
                                entry[key_token] = value
                            elif isinstance(value, list):
                                entry[key_token] = list(value)
                            elif isinstance(value, dict):
                                entry[key_token] = dict(value)
                        if entry:
                            restored[family_token] = entry
                    self._family_gate_feedback = restored
                try:
                    self._cycle_count = max(0, int(state.get("cycle_count") or 0))
                except Exception:
                    pass
                try:
                    self._consecutive_failures = max(0, int(state.get("consecutive_failures") or 0))
                except Exception:
                    pass
                circuit_state = str(state.get("circuit_state") or "closed").strip().lower()
                if circuit_state in {"closed", "open", "half_open"}:
                    self._circuit_state = circuit_state
                logger.info(
                    "StrategyFactory: restored scheduler state, families=%d cycle=%d",
                    len(self._family_gate_feedback),
                    self._cycle_count,
                )
            except Exception as exc:
                logger.debug("StrategyFactory: scheduler state restore failed: %s", exc)

        def start(self):
            if self._running:
                logger.warning("StrategyFactory already running")
                return
            self._running = True
            # 启动前异步恢复 EMA / 断路器状态（fire-and-forget，不阻塞 start）
            try:
                asyncio.get_running_loop().create_task(
                    self._restore_scheduler_state(),
                    name="strategy-factory-restore-state",
                )
            except RuntimeError:
                # 没有 running loop（同步 start 场景）—— 跳过自动恢复
                pass
            self._task = asyncio.create_task(self._loop(), name="strategy-factory-scheduler")
            logger.info(
                "StrategyFactory started, mode=%s, market_interval=%ds, off_hours_interval=%ds, max_daily_runs=%d",
                self.schedule_mode,
                FACTORY_MARKET_HOURS_INTERVAL_SEC,
                FACTORY_OFF_HOURS_INTERVAL_SEC,
                self.max_daily_runs,
            )

        def stop(self):
            self._running = False
            if self._task:
                self._task.cancel()
                self._task = None
            logger.info("StrategyFactory stopped")

        async def shutdown(self):
            self._running = False
            self._metrics.current_state = "shutting_down"
            task = self._task
            self._task = None
            if task is None:
                logger.info("StrategyFactory stopped")
                return
            if not task.done():
                # 优化 7：分层优雅关闭
                # 第一阶段：等待当前周期自然完成
                grace_sec = float(os.getenv("STRATEGY_FACTORY_SHUTDOWN_GRACE_SEC", "120") or 120)
                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=max(0.25, grace_sec),
                    )
                except asyncio.TimeoutError:
                    # 第二阶段：发送取消信号，等待清理
                    logger.warning("StrategyFactory: shutdown grace period exceeded, cancelling...")
                    task.cancel()
                    cancel_timeout = float(os.getenv("STRATEGY_FACTORY_SHUTDOWN_CANCEL_TIMEOUT_SEC", "5") or 5)
                    with suppress(asyncio.CancelledError):
                        await asyncio.wait_for(task, timeout=cancel_timeout)
            else:
                with suppress(asyncio.CancelledError):
                    await task
            logger.info("StrategyFactory stopped gracefully")
