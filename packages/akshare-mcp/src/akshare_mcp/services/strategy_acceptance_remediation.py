from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from strategy_factory.api.semantic_contract import build_signal_evidence_records
from strategy_factory.api.constants import (
    BACKTEST_TYPE_THRESHOLDS,
    PROVISIONAL_PASS_THRESHOLDS,
)

from .backtest import BacktestEngine, StrategyRegistry
from .incubation import (
    _build_position_id,
    _parse_datetime,
    _resolve_strategy_target_codes,
    _runtime_action_lineage,
    get_strategy_incubation_service,
)
from .signal_tracker import _build_signal_tracking_artifacts
from .trade_audit_writer import record_trade_fill_from_order_and_trade

from .acceptance_helpers import (
    _RoundTrip,
    _RoundTripSelection,
    _apply_failed_metrics_family_hardening,
    _bootstrap_lineage_token,
    _bootstrap_trade_floor,
    _build_bootstrap_lineage_fallback,
    _coerce_trade_date,
    _coerce_trade_ts,
    _dedupe_strings,
    _group_backtest_round_trips,
    _is_bootstrap_proxy_lineage_id,
    _merge_bootstrap_lineage,
    _parse_affected_rows,
    _round_trip_selection,
    _safe_float,
    _safe_int,
    _select_bootstrap_round_trips,
    _strategy_runtime_params,
    _sync_runtime_params_container,
    build_failed_metrics_filter_patch,
    summarize_code_performance,
)
from .acceptance_remediation_core import _RemediationCoreMixin
from .acceptance_backtest import _BacktestMixin
from .acceptance_bootstrap import _BootstrapMixin


class StrategyAcceptanceRemediationService(
    _RemediationCoreMixin,
    _BacktestMixin,
    _BootstrapMixin,
):
    """Composed acceptance/remediation service facade."""


_strategy_acceptance_remediation_service: Optional[StrategyAcceptanceRemediationService] = None


def get_strategy_acceptance_remediation_service() -> StrategyAcceptanceRemediationService:
    global _strategy_acceptance_remediation_service
    if _strategy_acceptance_remediation_service is None:
        _strategy_acceptance_remediation_service = StrategyAcceptanceRemediationService()
    return _strategy_acceptance_remediation_service


__all__ = [
    "StrategyAcceptanceRemediationService",
    "_select_bootstrap_round_trips",
    "build_failed_metrics_filter_patch",
    "get_strategy_acceptance_remediation_service",
    "summarize_code_performance",
]
