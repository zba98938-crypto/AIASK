"""Public market-view helpers for Strategy Factory host integrations."""

from __future__ import annotations

from ..application._bulk_cursor import (
    coerce_non_negative_int,
    extract_bulk_stock_cursor,
    resolve_bulk_stock_matrix_cursor,
)
from ..application.factory_market_views import (
    DEFAULT_FULL_MARKET_TOPN,
    DEFAULT_MAX_PER_INDUSTRY,
    FULL_MARKET_TOPN_CONTRACT_VERSION,
    build_full_market_topn_payload,
    build_portfolio_candidate_from_topn,
    build_research_window_status,
    hydrate_full_market_topn_payload,
    select_full_market_topn_constituents,
)

__all__ = [
    "DEFAULT_FULL_MARKET_TOPN",
    "DEFAULT_MAX_PER_INDUSTRY",
    "FULL_MARKET_TOPN_CONTRACT_VERSION",
    "build_full_market_topn_payload",
    "build_portfolio_candidate_from_topn",
    "build_research_window_status",
    "coerce_non_negative_int",
    "extract_bulk_stock_cursor",
    "hydrate_full_market_topn_payload",
    "resolve_bulk_stock_matrix_cursor",
    "select_full_market_topn_constituents",
]
