"""策略工厂候选提交。"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from collections import Counter
from typing import TYPE_CHECKING, Any, List, Optional
from uuid import uuid4

from .quality_gates import build_completed_gate_3_report
from .quality_reporting import build_quality_report, normalize_quality_gate_result
from .submission_gate import run_submission_quality_gate as _local_run_submission_quality_gate
from .utils import (
    _auto_name as _local_auto_name,
    _extract_event_context as _local_extract_event_context,
    _update_strategy_status as _local_update_strategy_status,
    get_strategy_factory_package as _local_get_strategy_factory_package,
)
from ..domain.constants import (
    FACTORY_SUBMISSION_MIN_BACKTEST_TRADES,
    FACTORY_SUBMISSION_MIN_EVENT_TARGET_COVERAGE,
    FACTORY_SUBMISSION_REJECT_GENERIC_AI_NAMES,
    FACTORY_SUBMISSION_REQUIRE_STRICT_PASS_FOR_REFRESH,
    FACTORY_SUBMISSION_REQUIRE_TASK_PREFERENCE_MATCH,
    SUBMIT_CONCURRENCY,
)
from ..domain.targets import _build_task_signature, _normalize_research_task_contract, _normalize_target_codes
from ..infrastructure.mcp_services import build_strategy_vector_profile

if TYPE_CHECKING:
    from ..api.contracts import FactorPoolGateway, IncubationGateway, RiskGateway, ValidationGateway

logger = logging.getLogger(__name__)

def _compat_setting(name: str, default):
    return default


def _auto_name(*args, **kwargs):
    return _local_auto_name(*args, **kwargs)


def _extract_event_context(*args, **kwargs):
    return _local_extract_event_context(*args, **kwargs)


def get_strategy_factory_package():
    return _local_get_strategy_factory_package()


async def _update_strategy_status(*args, **kwargs):
    try:
        from ._submitter_actions import runner as submitter_runner

        patched = getattr(submitter_runner, "_update_strategy_status", None)
        if callable(patched) and patched is not _update_strategy_status:
            return await patched(*args, **kwargs)
    except Exception:
        pass
    return await _local_update_strategy_status(*args, **kwargs)


class _CompatValidationGateway:
    """Resolve validation runner through the legacy patch-point at call time."""

    async def run_validation_report(self, strategy_type: str, params: dict[str, Any], db) -> Optional[dict]:
        from ..api.quality_reporting import run_validation_report

        return await run_validation_report(strategy_type, dict(params or {}), db)


class _CompatRiskGateway:
    """Resolve risk runner through the legacy patch-point at call time."""

    async def run_risk_report(self, strategy_type: str, params: dict[str, Any], db) -> Optional[dict]:
        from ..api.quality_reporting import run_risk_report

        return await run_risk_report(strategy_type, dict(params or {}), db)

from ._submitter_helpers import _StrategySubmitterHelpersMixin
from ._submitter_policy import _StrategySubmitterPolicyMixin
from ._submitter_actions import _StrategySubmitterActionsMixin
from .services.lifecycle_coordinator import StrategyLifecycleCoordinator
from .services.submission_coordinator import SubmissionCoordinator


class StrategySubmitter(_StrategySubmitterHelpersMixin, _StrategySubmitterPolicyMixin, _StrategySubmitterActionsMixin):
        """创建策略记录并提交质检。"""

        _GENERIC_AI_NAME_PATTERNS = (
            re.compile(r"^(?:dsl_rule|ma_cross|momentum|rsi|value_factor|quality_factor|growth_factor|multi_factor|macro_timing|volatility_breakout|event_structure_breakout|gap_fill|mean_reversion_short|sector_rotation|north_capital_track|margin_divergence)策略$", re.I),
            re.compile(r"^(?:strategy|test|demo|sample)[ _-]*\d*$", re.I),
        )

        def __init__(
            self,
            *,
            validation_gateway: Optional["ValidationGateway"] = None,
            risk_gateway: Optional["RiskGateway"] = None,
            incubation_gateway: Optional["IncubationGateway"] = None,
            factor_pool_gateway: Optional["FactorPoolGateway"] = None,
        ):
            self._validation_gateway = validation_gateway
            self._risk_gateway = risk_gateway
            self._incubation_gateway = incubation_gateway
            self._factor_pool_gateway = factor_pool_gateway
            self._submission_coordinator = SubmissionCoordinator(self)
            self._lifecycle_coordinator = StrategyLifecycleCoordinator(self)
            self._update_strategy_status = _update_strategy_status
            # PR-S3: 持久化失败 DLQ —— _save_metric_with_retry 全部 attempts 失败后写入此处
            self._persistence_dlq: list[dict] = []

        def drain_persistence_dlq(self) -> list[dict]:
            """取出并清空持久化失败 DLQ。供 cycle runner 写入 run summary。"""
            items = list(self._persistence_dlq or [])
            self._persistence_dlq = []
            return items
