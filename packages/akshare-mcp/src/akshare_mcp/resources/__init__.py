"""MCP resource registrations for high-value read-only objects."""

from __future__ import annotations

from .analysis import register as register_analysis_resources
from .catalog import register as register_catalog_resources
from .lineage import register as register_lineage_resources
from .research_objects import register as register_research_object_resources
from .stock_and_watchlist import register as register_stock_and_watchlist_resources
from .strategy import register as register_strategy_resources


def register(mcp) -> None:
    """Register concrete MCP resources."""
    register_analysis_resources(mcp)
    register_catalog_resources(mcp)
    register_lineage_resources(mcp)
    register_research_object_resources(mcp)
    register_stock_and_watchlist_resources(mcp)
    register_strategy_resources(mcp)
