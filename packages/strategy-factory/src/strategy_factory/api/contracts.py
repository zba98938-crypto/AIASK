"""Typed contracts for the strategy_factory migration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from ..contracts.execution_universe import (
    ExecutableStrategy,
    ExecutionUniverseContract,
    ExecutionUniverseQuery,
    ExecutionUniverseStrategy,
)
import logging

logger = logging.getLogger(__name__)


JSONDict = dict[str, Any]
JSONLike = Any
_EMPTY_VALUES = (None, "", [], {})


def _as_dict(value: Any) -> JSONDict:
    return dict(value) if isinstance(value, Mapping) else {}


def _string(value: Any) -> str:
    return str(value or "").strip()


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        fallback = float(default)
    except (TypeError, ValueError):
        fallback = 0.0
    if not math.isfinite(fallback):
        fallback = 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if math.isfinite(parsed) else fallback


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        fallback = int(default)
    except (TypeError, ValueError):
        fallback = 0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed):
        return fallback
    return int(parsed)


def _safe_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return bool(value)


def normalize_strategy_preferences(*values: Any, limit: int = 6) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, (list, tuple, set)):
            candidates = list(value)
        elif value in _EMPTY_VALUES:
            candidates = []
        else:
            candidates = [value]
        for item in candidates:
            token = _string(item).lower()
            if not token or token in seen:
                continue
            seen.add(token)
            ordered.append(token)
            if len(ordered) >= max(1, int(limit or 6)):
                return ordered
    return ordered


def _derive_expected_turnover_band(
    *,
    holding_horizon: Mapping[str, Any] | None = None,
    fallback: Optional[str] = None,
) -> str:
    candidate = _string(fallback).lower()
    if candidate:
        return candidate
    max_days = max(1, _safe_int(_as_dict(holding_horizon).get("max_days"), 10))
    if max_days >= 24:
        return "low"
    if max_days >= 12:
        return "medium"
    return "high"


def _derive_turnover_cost_class(
    *,
    expected_turnover_band: str,
    capacity_bucket: str,
    slippage_bps: float,
    market_impact_bps: float,
) -> Optional[str]:
    if expected_turnover_band == "very_high" or slippage_bps >= 10.0 or market_impact_bps >= 4.0:
        return "high_touch"
    if expected_turnover_band == "high" or slippage_bps >= 5.0 or capacity_bucket == "small":
        return "medium_touch"
    if expected_turnover_band in {"medium", "low"} or capacity_bucket:
        return "low_touch"
    return None


def normalize_execution_assumptions(
    payload: Optional[Mapping[str, Any]],
    *,
    portfolio_spec: Optional[Mapping[str, Any]] = None,
    capacity_assumption: Optional[Mapping[str, Any]] = None,
    holding_horizon: Optional[Mapping[str, Any]] = None,
    cost_sensitivity_grid: Optional[Mapping[str, Any]] = None,
) -> JSONDict:
    execution = _as_dict(payload)
    portfolio = _as_dict(portfolio_spec)
    capacity = _as_dict(capacity_assumption)
    horizon = _as_dict(holding_horizon)
    cost_base_case = _as_dict(_as_dict(cost_sensitivity_grid).get("base_case"))

    expected_turnover_band = _derive_expected_turnover_band(
        holding_horizon=horizon,
        fallback=(
            execution.get("expected_turnover_band")
            or portfolio.get("expected_turnover_band")
            or horizon.get("expected_turnover_band")
        ),
    )
    capacity_bucket = _string(
        execution.get("capacity_bucket")
        or portfolio.get("capacity_bucket")
        or capacity.get("capacity_bucket")
        or capacity.get("bucket")
        or ("mid" if max(1, _safe_int(horizon.get("max_days"), 10)) >= 10 else "small")
    ).lower()
    slippage_bps = _safe_float(
        execution.get("slippage_bps"),
        _safe_float(
            execution.get("slippage"),
            _safe_float(cost_base_case.get("slippage_bps"), 5.0) / 10000.0,
        ) * 10000.0,
    )
    market_impact_bps = _safe_float(
        execution.get("market_impact_bps"),
        _safe_float(cost_base_case.get("market_impact_bps"), 0.0),
    )

    return {
        "commission_rate": _safe_float(
            execution.get("commission_rate"),
            _safe_float(cost_base_case.get("commission_rate"), 0.00025),
        ),
        "slippage_bps": slippage_bps,
        "slippage_model": _string(
            execution.get("slippage_model")
            or cost_base_case.get("slippage_model")
            or "fixed"
        ) or "fixed",
        "market_impact_bps": market_impact_bps,
        "tradability_filter": _safe_bool(
            execution.get("tradability_filter"),
            cost_base_case.get("tradability_filter")
            if cost_base_case.get("tradability_filter") is not None
            else True,
        ),
        "capacity_participation_rate": _safe_float(
            execution.get("capacity_participation_rate"),
            _safe_float(capacity.get("capacity_participation_rate"), 0.0),
        ),
        "adv_ratio_limit": _safe_float(
            execution.get("adv_ratio_limit"),
            _safe_float(capacity.get("adv_ratio_limit"), 0.0),
        ),
        "capacity_bucket": capacity_bucket or None,
        "margin_rate": (
            _safe_float(execution.get("margin_rate"))
            if execution.get("margin_rate") is not None
            else None
        ),
        "contract_multiplier": (
            _safe_int(execution.get("contract_multiplier"))
            if execution.get("contract_multiplier") is not None
            else None
        ),
        "liquidity_bucket": _string(execution.get("liquidity_bucket")) or None,
        "max_contracts_per_rebalance": (
            _safe_int(execution.get("max_contracts_per_rebalance"))
            if execution.get("max_contracts_per_rebalance") is not None
            else None
        ),
        "expected_turnover_band": expected_turnover_band or None,
        "turnover_cost_class": (
            _string(execution.get("turnover_cost_class")).lower()
            or _derive_turnover_cost_class(
                expected_turnover_band=expected_turnover_band,
                capacity_bucket=capacity_bucket,
                slippage_bps=slippage_bps,
                market_impact_bps=market_impact_bps,
            )
        ),
        "position_sizing_rationale": _string(
            execution.get("position_sizing_rationale")
            or portfolio.get("position_sizing_rationale")
        ) or None,
    }


def resolve_refresh_existing_contract(
    *,
    candidate: Optional[Mapping[str, Any]] = None,
    dedup_result: Optional[Mapping[str, Any]] = None,
    existing_strategy: Optional[Mapping[str, Any]] = None,
) -> JSONDict:
    payload = _as_dict(candidate)
    dedup = _as_dict(dedup_result if dedup_result is not None else payload.get("dedup_result"))
    existing = _as_dict(existing_strategy)
    refresh_mode = _string(dedup.get("refresh_mode")).lower()
    matched_strategy_id = _string(dedup.get("matched_strategy_id") or existing.get("id")) or None
    refresh_existing = bool(dedup.get("refresh_existing"))
    if refresh_mode == "spawn_revision_from_existing":
        refresh_existing = False
    if refresh_existing and not matched_strategy_id:
        refresh_existing = False
    return {
        "refresh_existing": refresh_existing,
        "refresh_mode": refresh_mode or None,
        "matched_strategy_id": matched_strategy_id,
        "existing_strategy_loaded": bool(existing),
    }


# ---------------------------------------------------------------------------
# Strategy object protocol types (WP0)
# ---------------------------------------------------------------------------


@dataclass
class StrategyResearchContract:
    """Research task binding contract for a candidate."""

    task_id: str = ""
    task_source: str = ""
    opportunity_type: str = ""
    preferred_strategy_types: list[str] = field(default_factory=list)
    allowed_strategy_types: list[str] = field(default_factory=list)
    preference_strength: str = "soft"
    target_symbols: list[str] = field(default_factory=list)
    stock_pool: JSONDict = field(default_factory=dict)
    target_symbol_policy: str = "prefer_intersection"
    universe_expansion_policy: str = "allow_market_fallback"
    validation_focus: str = "target_plus_representative"
    event_window: JSONDict = field(default_factory=dict)
    estimation_window: JSONDict = field(default_factory=dict)
    holding_window: JSONDict = field(default_factory=dict)
    task_signature: str = ""


@dataclass
class StrategyTargetingPolicy:
    """Relationship between candidate and research-task target pools."""

    target_symbol_policy: str = "prefer_intersection"
    universe_expansion_policy: str = "allow_market_fallback"
    validation_focus: str = "target_plus_representative"
    constraint_violation: bool = False
    expansion_applied: bool = False
    expansion_reason: Optional[str] = None
    expansion_source: Optional[str] = None
    coverage_ratio: float = 0.0
    intersection_ratio: float = 0.0


@dataclass
class StrategyPortfolioSpec:
    """Portfolio / position sizing semantics."""

    position_assumption: str = "single_name_full_notional"
    target_weight_scheme: str = "single_name"
    max_position_pct: Optional[float] = None
    target_weight_map: JSONDict = field(default_factory=dict)


@dataclass
class StrategyExecutionAssumptions:
    """Execution and cost assumptions for a candidate."""

    commission_rate: float = 0.00025
    slippage_bps: float = 0.0
    slippage_model: str = "fixed"
    market_impact_bps: float = 0.0
    tradability_filter: bool = True
    capacity_participation_rate: float = 0.0
    adv_ratio_limit: float = 0.0
    capacity_bucket: Optional[str] = None
    margin_rate: Optional[float] = None
    contract_multiplier: Optional[int] = None
    liquidity_bucket: Optional[str] = None
    max_contracts_per_rebalance: Optional[int] = None


@dataclass
class StrategyInstrumentProfile:
    """Instrument semantics for non-equity candidates."""

    asset_class: str = "equity"
    underlying: Optional[str] = None
    curve_legs: list[JSONDict] = field(default_factory=list)
    roll_rule: JSONDict = field(default_factory=dict)


@dataclass
class StrategyValidationProfile:
    """Gate-1 / Gate-2 / Gate-3 primary validation protocol."""

    profile: str = "trade_rule_validation"
    validation_focus: str = "target_plus_representative"
    primary_validation_layer: str = "target"


@dataclass
class ResearchValidationContract:
    """Normalized research admission contract shared across factory flows."""

    contract_version: str = "strategy_factory.research_protocol.v2"
    walk_forward_config: JSONDict = field(default_factory=dict)
    baseline_reference: JSONDict = field(default_factory=dict)
    cash_sleeve_policy: JSONDict = field(default_factory=dict)
    cost_sensitivity_grid: JSONDict = field(default_factory=dict)
    capacity_execution: JSONDict = field(default_factory=dict)
    multiple_testing: JSONDict = field(default_factory=dict)
    admission_thresholds: JSONDict = field(default_factory=dict)
    family_holding_bucket: JSONDict = field(default_factory=dict)
    effective_contract: JSONDict = field(default_factory=dict)
    recommended_defaults: JSONDict = field(default_factory=dict)
    field_provenance: dict[str, str] = field(default_factory=dict)
    field_provenance_summary: JSONDict = field(default_factory=dict)
    spec_completeness: str = "complete"
    completion_issues: list[JSONDict] = field(default_factory=list)
    hard_failures: list[JSONDict] = field(default_factory=list)


@dataclass
class StrategySubmissionAudit:
    """Submission and audit trail for a candidate."""

    constraint_check: JSONDict = field(default_factory=dict)
    attempt_adjustment: JSONDict = field(default_factory=dict)
    task_signature: str = ""
    refresh_mode: str = ""
    primary_validation_layer: str = "target"
    event_window_config: JSONDict = field(default_factory=dict)
    cost_assumptions: JSONDict = field(default_factory=dict)
    position_assumption: str = ""
    candidate_provenance: JSONDict = field(default_factory=dict)


@dataclass(frozen=True)
class FactoryBacktestAssumptions:
    """Normalized execution assumptions shared by the factory backtest path."""

    initial_capital: float = 100000.0
    commission_rate: float = 0.00025
    slippage_bps: float = 0.0
    market_impact_bps: float = 0.0
    arrival_price_policy: str = "next_open_proxy"
    implementation_shortfall_proxy: float = 0.0
    tradability_filter: bool = True
    slippage_model: str = "fixed"
    max_position_pct: Optional[float] = None
    capacity_participation_rate: float = 0.0
    adv_ratio_limit: float = 0.0
    capacity_bucket: Optional[str] = None
    margin_rate: Optional[float] = None
    contract_multiplier: Optional[int] = None
    liquidity_bucket: Optional[str] = None
    max_contracts_per_rebalance: Optional[int] = None
    position_assumption: str = "single_name_full_notional"
    target_weight_scheme: str = "single_name"
    target_weight_map: JSONDict = field(default_factory=dict)
    turnover_cost_class: Optional[str] = None
    position_sizing_rationale: Optional[str] = None
    expected_turnover_band: Optional[str] = None
    market_regime_assumption: Optional[JSONLike] = None
    market_ruleset: str = "cn_equity"
    sell_tax_rate: float = 0.001
    min_trade_lot: int = 100
    t_plus_one: bool = True
    validation_focus: str = "target_plus_representative"

    def to_backtest_kwargs(self) -> JSONDict:
        return {
            "initial_capital": float(self.initial_capital),
            "commission": float(self.commission_rate),
            "slippage": float(self.slippage_bps) / 10000.0,
            "market_impact_bps": float(self.market_impact_bps),
            "arrival_price_policy": str(self.arrival_price_policy or "next_open_proxy"),
            "implementation_shortfall_proxy": float(self.implementation_shortfall_proxy),
            "tradability_filter": bool(self.tradability_filter),
            "slippage_model": str(self.slippage_model or "fixed"),
            "max_position_pct": float(self.max_position_pct) if self.max_position_pct is not None else None,
            "capacity_participation_rate": float(self.capacity_participation_rate),
            "adv_ratio_limit": float(self.adv_ratio_limit),
            "capacity_bucket": self.capacity_bucket,
            "margin_rate": float(self.margin_rate) if self.margin_rate is not None else None,
            "contract_multiplier": int(self.contract_multiplier) if self.contract_multiplier is not None else None,
            "liquidity_bucket": self.liquidity_bucket,
            "max_contracts_per_rebalance": int(self.max_contracts_per_rebalance) if self.max_contracts_per_rebalance is not None else None,
            "position_assumption": str(self.position_assumption or "single_name_full_notional"),
            "target_weight_scheme": str(self.target_weight_scheme or "single_name"),
            "target_weight_map": dict(self.target_weight_map or {}),
            "market_ruleset": str(self.market_ruleset or "cn_equity"),
            "sell_tax_rate": float(self.sell_tax_rate),
            "min_trade_lot": int(self.min_trade_lot),
            "t_plus_one": bool(self.t_plus_one),
            "validation_focus": str(self.validation_focus or "target_plus_representative"),
        }

    def to_audit_dict(self) -> JSONDict:
        return {
            "initial_capital": float(self.initial_capital),
            "commission_rate": float(self.commission_rate),
            "slippage_bps": float(self.slippage_bps),
            "market_impact_bps": float(self.market_impact_bps),
            "arrival_price_policy": str(self.arrival_price_policy or "next_open_proxy"),
            "implementation_shortfall_proxy": float(self.implementation_shortfall_proxy),
            "tradability_filter": bool(self.tradability_filter),
            "slippage_model": str(self.slippage_model or "fixed"),
            "max_position_pct": float(self.max_position_pct) if self.max_position_pct is not None else None,
            "capacity_participation_rate": float(self.capacity_participation_rate),
            "adv_ratio_limit": float(self.adv_ratio_limit),
            "capacity_bucket": self.capacity_bucket,
            "margin_rate": float(self.margin_rate) if self.margin_rate is not None else None,
            "contract_multiplier": int(self.contract_multiplier) if self.contract_multiplier is not None else None,
            "liquidity_bucket": self.liquidity_bucket,
            "max_contracts_per_rebalance": int(self.max_contracts_per_rebalance) if self.max_contracts_per_rebalance is not None else None,
            "position_assumption": str(self.position_assumption or "single_name_full_notional"),
            "target_weight_scheme": str(self.target_weight_scheme or "single_name"),
            "target_weight_map": dict(self.target_weight_map or {}),
            "turnover_cost_class": self.turnover_cost_class,
            "position_sizing_rationale": self.position_sizing_rationale,
            "expected_turnover_band": self.expected_turnover_band,
            "market_regime_assumption": self.market_regime_assumption,
            "market_ruleset": str(self.market_ruleset or "cn_equity"),
            "sell_tax_rate": float(self.sell_tax_rate),
            "min_trade_lot": int(self.min_trade_lot),
            "t_plus_one": bool(self.t_plus_one),
            "validation_focus": str(self.validation_focus or "target_plus_representative"),
        }


@runtime_checkable
class MarketDataRepository(Protocol):
    """Raw market-data reads, owned by the data provider.

    Local fallback definition; replaced below by the shared quant-core protocol
    when that package is installed.
    """

    async def get_klines(self, code: str, limit: int = 500) -> list[Mapping[str, Any]]: ...

    async def get_limit_up_stats(self) -> Mapping[str, Any]: ...

    async def get_factor_ic_history(
        self, factor_name: str, horizon: str, limit: int
    ) -> list[Mapping[str, Any]]: ...

    async def list_stock_universe(
        self, limit: int = 200, offset: int = 0
    ) -> list[Mapping[str, Any]]: ...


@runtime_checkable
class StrategyFactoryRepository(Protocol):
    """Repository contract observed across the migrated strategy factory modules."""

    async def get_klines(self, code: str, limit: int = 500) -> list[Mapping[str, Any]]: ...

    async def get_limit_up_stats(self) -> Mapping[str, Any]: ...

    async def get_factor_ic_history(self, factor_name: str, horizon: str, limit: int) -> list[Mapping[str, Any]]: ...

    async def count_strategies_by_type(self, status: str) -> Mapping[str, int]: ...

    async def save_daily_snapshot(self, snapshot_date: Any, snapshot: Mapping[str, Any]) -> Any: ...

    async def list_strategies(self, status: str, limit: int = 500) -> list[Mapping[str, Any]]: ...

    async def get_strategy(self, strategy_id: str) -> Optional[Mapping[str, Any]]: ...

    async def get_strategy_metrics(self, strategy_id: str) -> list[Mapping[str, Any]]: ...

    async def get_signal_stats(
        self,
        strategy_id: str,
        lookback_days: int | None = None,
        eps: float | None = None,
    ) -> Mapping[str, Any]: ...

    async def save_strategy(self, data: Mapping[str, Any]) -> Any: ...

    async def save_strategy_quality_report(self, strategy_id: str, report_type: str, report: Mapping[str, Any]) -> Any: ...

    async def update_strategy_status(self, strategy_id: str, status: str, **kwargs: Any) -> Any: ...

    async def save_strategy_lineage(
        self,
        strategy_id: str,
        parent_strategy_id: Optional[str],
        reason: str,
        snapshot: Mapping[str, Any],
    ) -> Any: ...

    async def save_strategy_metrics(self, strategy_id: str, period: str, payload: Mapping[str, Any]) -> Any: ...

    async def save_elimination_log(self, strategy_id: str, log_date: Any, red_flags: list[str], reason: str) -> Any: ...

    async def get_strategy_generation_experiment(self, experiment_id: str) -> Optional[Mapping[str, Any]]: ...

    async def save_strategy_generation_experiment(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_factory_task_evidence(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_strategy_candidate_evidence(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_strategy_signal_evidence(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_strategy_task_run(self, payload: Mapping[str, Any]) -> Any: ...

    async def update_strategy_task_run(self, task_run_id: Any, **kwargs: Any) -> Any: ...

    async def list_stock_universe(self, limit: int = 200, offset: int = 0) -> list[Mapping[str, Any]]: ...

    async def list_factory_event_clusters(self, status: Optional[str] = None, limit: int = 200) -> list[Mapping[str, Any]]: ...

    async def save_factory_theme_definition(self, payload: Mapping[str, Any]) -> Any: ...

    async def save_strategy_factory_run(self, results: Mapping[str, Any]) -> Any: ...

    async def list_strategy_factory_runs(self, limit: int = 20) -> list[Mapping[str, Any]]: ...

    async def get_strategy_factory_run(self, run_id: str) -> Optional[Mapping[str, Any]]: ...

    async def get_latest_strategy_factory_run(self) -> Optional[Mapping[str, Any]]: ...

    async def save_strategy_factory_run_artifact(self, payload: Mapping[str, Any]) -> Any: ...

    async def list_strategy_factory_run_artifacts(self, run_id: str) -> list[Mapping[str, Any]]: ...

    async def create_strategy_factory_dispatch(self, payload: Mapping[str, Any]) -> Any: ...

    async def update_strategy_factory_dispatch(self, dispatch_id: str, **kwargs: Any) -> Any: ...

    async def get_strategy_factory_dispatch(self, dispatch_id: str) -> Optional[Mapping[str, Any]]: ...

    async def list_strategy_factory_dispatches(
        self,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list[Mapping[str, Any]]: ...


try:
    from aiask_quant_core.storage.contracts import (
        MarketDataRepository as _SharedMarketDataRepository,
        StrategyFactoryRepository as _SharedStrategyFactoryRepository,
    )
except ModuleNotFoundError as _swallowed:
    logger.warning("packages/strategy-factory/src/strategy_factory/api/contracts.py:585: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
    pass
else:
    StrategyFactoryRepository = _SharedStrategyFactoryRepository
    # Raw market-data reads are a separate, provider-owned contract. Exposed here
    # so callers that only read market data can depend on the narrow protocol
    # instead of the union that also carries factory-domain persistence.
    MarketDataRepository = _SharedMarketDataRepository


@runtime_checkable
class VectorSearchGateway(Protocol):
    """Gateway for vector-pattern lookup used by dedup/vector layers."""

    @property
    def last_backend_used(self) -> str: ...

    @property
    def last_meta(self) -> Mapping[str, Any]: ...

    def find_similar_patterns(
        self,
        query_klines: list[Mapping[str, Any]],
        candidate_klines_dict: Mapping[str, list[Mapping[str, Any]]],
        top_k: int = 10,
        method: str = "price_volume",
        metric: str = "cosine",
        backend: Optional[str] = None,
        allow_fallback: Optional[bool] = None,
    ) -> list[JSONDict]: ...


@runtime_checkable
class AutonomyGateway(Protocol):
    """Gateway for the external strategy autonomy service."""

    async def generate_factory_candidates(
        self,
        db: StrategyFactoryRepository,
        snapshot: Mapping[str, Any],
        *,
        limit: int = 4,
        research_task: Optional[Mapping[str, Any]] = None,
        source: str = "",
    ) -> JSONDict: ...


@runtime_checkable
class FactorResearchGateway(Protocol):
    """Gateway for factor scheduler / factor-research metadata."""

    async def build_artifact(
        self,
        db: StrategyFactoryRepository,
        snapshot: Mapping[str, Any],
    ) -> JSONDict: ...

    def status(self) -> Mapping[str, Any]: ...

    async def refresh(self) -> JSONDict: ...


@runtime_checkable
class FactorPoolGateway(Protocol):
    """Gateway for factor mining factory's active pool.

    Provides strategy-factory access to the active factor pool
    maintained by the factor mining factory.
    """

    async def get_active_factors(
        self,
        *,
        families: Optional[list[str]] = None,
        min_grade: str = "B",
        limit: int = 50,
        include_quarantine: bool = False,
    ) -> list[JSONDict]: ...

    async def get_factor_weights(
        self,
        factor_ids: list[str],
        method: str = "dynamic",
    ) -> dict[str, float]: ...

    async def get_pool_status(self) -> JSONDict: ...

    async def report_factor_performance(
        self,
        factor_id: str,
        strategy_id: str,
        metrics: JSONDict,
    ) -> None: ...

    async def trigger_mining_cycle(
        self,
        *,
        trigger: str = "api",
        engines: Optional[list[str]] = None,
    ) -> JSONDict: ...

@runtime_checkable
class IncubationGateway(Protocol):
    """Gateway for incubation account binding and pipeline warm-up."""

    async def ensure_account(
        self,
        db: StrategyFactoryRepository,
        strategy: Mapping[str, Any],
        *,
        source_run_id: Optional[Any] = None,
        stage: str = "warmup",
    ) -> JSONDict: ...

    async def run_pipeline(
        self,
        db: StrategyFactoryRepository,
        strategy: Mapping[str, Any],
        *,
        source: str = "strategy_factory_submit",
        auto_apply_review: bool = False,
    ) -> JSONDict: ...

    async def submit(
        self,
        db: StrategyFactoryRepository,
        strategy: Mapping[str, Any],
        *,
        source_run_id: Optional[Any] = None,
        source: str = "strategy_factory_submit",
        auto_apply_review: bool = False,
        stage: str = "warmup",
    ) -> JSONDict: ...


@runtime_checkable
class ValidationGateway(Protocol):
    """Gateway for validation reports derived from strategy panels."""

    async def run_validation_report(self, strategy_type: str, params: Mapping[str, Any], db: Any) -> Optional[JSONDict]: ...


@runtime_checkable
class RiskGateway(Protocol):
    """Gateway for risk reports derived from strategy panels."""

    async def run_risk_report(self, strategy_type: str, params: Mapping[str, Any], db: Any) -> Optional[JSONDict]: ...


__all__ = [
    "AutonomyGateway",
    "ExecutableStrategy",
    "ExecutionUniverseContract",
    "ExecutionUniverseQuery",
    "ExecutionUniverseStrategy",
    "FactorPoolGateway",
    "FactorResearchGateway",
    "FactoryBacktestAssumptions",
    "IncubationGateway",
    "MarketDataRepository",
    "ResearchValidationContract",
    "RiskGateway",
    "StrategyExecutionAssumptions",
    "StrategyFactoryRepository",
    "StrategyPortfolioSpec",
    "StrategyResearchContract",
    "StrategySubmissionAudit",
    "StrategyTargetingPolicy",
    "StrategyValidationProfile",
    "ValidationGateway",
    "VectorSearchGateway",
    "normalize_execution_assumptions",
    "normalize_strategy_preferences",
    "resolve_refresh_existing_contract",
]
