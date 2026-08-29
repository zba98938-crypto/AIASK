"""SQLite 适配器 — 策略孵化 Mixin (fragment loader retired)."""

from .strategy_incubation_repos._base import *  # noqa: F401,F403
from .strategy_incubation_repos.reads import _IncReadsMixin
from .strategy_incubation_repos.writes import _IncWritesMixin
from .strategy_incubation_repos.trade_positions import _TradePositionsMixin
from .strategy_incubation_repos.signal_evidence import _SignalEvidenceMixin
from .strategy_incubation_repos.closure_snapshots import _ClosureSnapshotsMixin
from .strategy_incubation_repos.execution_acceptance import _ExecutionAcceptanceMixin
from .strategy_incubation_repos.incubation_metrics import _IncubationMetricsMixin
from .strategy_incubation_repos.trade_audit import _TradeAuditMixin
from .strategy_incubation_repos.mappers import _IncMappersMixin


class StrategyIncubationMixin(
    _IncReadsMixin,
    _IncWritesMixin,
    _TradePositionsMixin,
    _SignalEvidenceMixin,
    _ClosureSnapshotsMixin,
    _ExecutionAcceptanceMixin,
    _IncubationMetricsMixin,
    _TradeAuditMixin,
    _IncMappersMixin,
):
    """Composed strategy-incubation storage mixin (fragment loader retired)."""
