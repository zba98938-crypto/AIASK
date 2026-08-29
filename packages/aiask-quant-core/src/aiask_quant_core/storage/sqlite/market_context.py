"""SQLite 适配器 — 市场上下文 Mixin (split into base + domain repos)."""

from .market_context_repos.base import _BaseMixin
from .market_context_repos.headline_labels import _HeadlineMixin
from .market_context_repos.stock_radar_repo import _StockRadarMixin
from .market_context_repos.market_events_repo import _EventsMixin
from .market_context_repos.market_documents_repo import _DocumentsMixin
from .market_context_repos.vectors_fund_flow_repo import _VectorsFundFlowMixin


class MarketContextMixin(
    _BaseMixin,
    _HeadlineMixin,
    _StockRadarMixin,
    _EventsMixin,
    _DocumentsMixin,
    _VectorsFundFlowMixin,
):
    """Composed market-context storage mixin."""
