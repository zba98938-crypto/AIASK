"""Idempotent default theme graph seed helpers.

The production bootstrap path needs a code-owned seed entrypoint rather than a
standalone script. The seed is conservative: it only writes the default graph
when the theme graph is empty, so operator-curated nodes or regression-updated
edges are never overwritten by maintenance.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any


DEFAULT_THEME_NODES: list[dict[str, Any]] = [
    {
        "theme_code": "upstream_oil_gas",
        "theme_name": "Upstream Oil & Gas",
        "breadth": "narrow",
        "default_horizon": "swing_1_5d",
        "industry_tags": ["oil", "gas", "petrochemical", "energy"],
        "shock_detection_profile": "fast",
    },
    {
        "theme_code": "coal",
        "theme_name": "Coal",
        "breadth": "narrow",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["coal", "coking coal", "thermal coal"],
        "shock_detection_profile": "medium",
    },
    {
        "theme_code": "gold",
        "theme_name": "Gold",
        "breadth": "narrow",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["gold", "precious metals"],
        "shock_detection_profile": "fast",
    },
    {
        "theme_code": "shipping_trade",
        "theme_name": "Shipping Trade",
        "breadth": "narrow",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["shipping", "port", "logistics"],
        "shock_detection_profile": "fast",
    },
    {
        "theme_code": "airlines",
        "theme_name": "Airlines",
        "breadth": "narrow",
        "default_horizon": "swing_1_5d",
        "industry_tags": ["airline", "aviation", "air transport"],
        "shock_detection_profile": "fast",
    },
    {
        "theme_code": "refinery",
        "theme_name": "Refinery",
        "breadth": "narrow",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["refinery", "petrochemical", "chemical"],
        "shock_detection_profile": "fast",
    },
    {
        "theme_code": "new_energy_chain",
        "theme_name": "New Energy Chain",
        "breadth": "broad",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["battery", "solar", "wind", "new energy"],
        "shock_detection_profile": "medium",
    },
    {
        "theme_code": "photovoltaic",
        "theme_name": "Photovoltaic",
        "breadth": "medium",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["photovoltaic", "solar", "pv"],
        "shock_detection_profile": "medium",
    },
    {
        "theme_code": "liquor_consumption",
        "theme_name": "Liquor Consumption",
        "breadth": "narrow",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["liquor", "beverage", "consumer"],
        "shock_detection_profile": "slow",
    },
    {
        "theme_code": "appliance",
        "theme_name": "Appliance",
        "breadth": "medium",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["appliance", "home appliance", "consumer durable"],
        "shock_detection_profile": "slow",
    },
    {
        "theme_code": "chip_domestic",
        "theme_name": "Domestic Chip",
        "breadth": "medium",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["semiconductor", "chip", "integrated circuit"],
        "shock_detection_profile": "fast",
    },
    {
        "theme_code": "ai_compute",
        "theme_name": "AI Compute",
        "breadth": "medium",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["ai", "compute", "server", "software"],
        "shock_detection_profile": "fast",
    },
    {
        "theme_code": "high_dividend_banks",
        "theme_name": "High Dividend Banks",
        "breadth": "narrow",
        "default_horizon": "macro_1m",
        "industry_tags": ["bank", "dividend"],
        "shock_detection_profile": "slow",
        "benchmark_index_code": "000922",
    },
    {
        "theme_code": "insurance",
        "theme_name": "Insurance",
        "breadth": "narrow",
        "default_horizon": "swing_5_20d",
        "industry_tags": ["insurance"],
        "shock_detection_profile": "slow",
    },
    {
        "theme_code": "real_estate_dev",
        "theme_name": "Real Estate Development",
        "breadth": "medium",
        "default_horizon": "macro_1m",
        "industry_tags": ["real estate", "property developer"],
        "shock_detection_profile": "slow",
    },
]


DEFAULT_THEME_EDGES: list[dict[str, Any]] = [
    {"source_theme_code": "upstream_oil_gas", "target_theme_code": "airlines", "relation_type": "supply_shock", "direction_sign": -1, "magnitude_factor": 0.70, "lag_days": 1, "confidence": 0.65},
    {"source_theme_code": "upstream_oil_gas", "target_theme_code": "shipping_trade", "relation_type": "amplifies", "direction_sign": 1, "magnitude_factor": 0.55, "lag_days": 1, "confidence": 0.60},
    {"source_theme_code": "upstream_oil_gas", "target_theme_code": "refinery", "relation_type": "cost_transmits", "direction_sign": -1, "magnitude_factor": 0.50, "lag_days": 1, "confidence": 0.60},
    {"source_theme_code": "upstream_oil_gas", "target_theme_code": "high_dividend_banks", "relation_type": "amplifies", "direction_sign": 1, "magnitude_factor": 0.25, "lag_days": 2, "confidence": 0.40},
    {"source_theme_code": "high_dividend_banks", "target_theme_code": "real_estate_dev", "relation_type": "substitutes", "direction_sign": -1, "magnitude_factor": 0.40, "lag_days": 5, "confidence": 0.50},
    {"source_theme_code": "new_energy_chain", "target_theme_code": "upstream_oil_gas", "relation_type": "substitutes", "direction_sign": -1, "magnitude_factor": 0.30, "lag_days": 10, "confidence": 0.45},
    {"source_theme_code": "photovoltaic", "target_theme_code": "new_energy_chain", "relation_type": "amplifies", "direction_sign": 1, "magnitude_factor": 0.60, "lag_days": 0, "confidence": 0.70},
    {"source_theme_code": "chip_domestic", "target_theme_code": "ai_compute", "relation_type": "amplifies", "direction_sign": 1, "magnitude_factor": 0.50, "lag_days": 3, "confidence": 0.60},
    {"source_theme_code": "liquor_consumption", "target_theme_code": "appliance", "relation_type": "amplifies", "direction_sign": 1, "magnitude_factor": 0.35, "lag_days": 5, "confidence": 0.45},
    {"source_theme_code": "insurance", "target_theme_code": "high_dividend_banks", "relation_type": "amplifies", "direction_sign": 1, "magnitude_factor": 0.50, "lag_days": 0, "confidence": 0.55},
]


async def seed_default_theme_graph(db: Any, *, overwrite: bool = False, updated_by: str = "bootstrap") -> dict[str, Any]:
    """Seed the default 15-node / 10-edge graph when storage is empty."""

    start = perf_counter()
    existing_nodes = await db.list_theme_nodes(is_active=True, limit=1) if hasattr(db, "list_theme_nodes") else []
    existing_edges = await db.list_theme_edges(is_active=True, limit=1) if hasattr(db, "list_theme_edges") else []
    if (existing_nodes or existing_edges) and not overwrite:
        return {
            "status": "skipped",
            "reason": "theme_graph_not_empty",
            "nodes_inserted": 0,
            "edges_inserted": 0,
            "node_count": len(await db.list_theme_nodes(is_active=True, limit=500)) if hasattr(db, "list_theme_nodes") else None,
            "edge_count": len(await db.list_theme_edges(is_active=True, limit=500)) if hasattr(db, "list_theme_edges") else None,
            "elapsed_seconds": round(perf_counter() - start, 4),
        }

    nodes_inserted = 0
    edges_inserted = 0
    for node in DEFAULT_THEME_NODES:
        before = await db.get_theme_node(node["theme_code"]) if hasattr(db, "get_theme_node") else None
        payload = {
            "benchmark_index_code": "000300",
            "aliases": [],
            "manual_locked": 0,
            "is_active": 1,
            "updated_by": updated_by,
            **node,
        }
        await db.upsert_theme_node(payload)
        if before is None:
            nodes_inserted += 1

    for edge in DEFAULT_THEME_EDGES:
        before_matches = await db.list_theme_edges(
            source=edge["source_theme_code"],
            target=edge["target_theme_code"],
            is_active=True,
            limit=20,
        )
        before = any(str(item.get("relation_type") or "") == edge["relation_type"] for item in before_matches)
        await db.upsert_theme_edge({
            "confidence_source": "manual",
            "manual_locked": 0,
            "is_active": 1,
            "updated_by": updated_by,
            **edge,
        })
        if not before:
            edges_inserted += 1

    return {
        "status": "seeded",
        "nodes_inserted": nodes_inserted,
        "edges_inserted": edges_inserted,
        "node_count": len(await db.list_theme_nodes(is_active=True, limit=500)) if hasattr(db, "list_theme_nodes") else len(DEFAULT_THEME_NODES),
        "edge_count": len(await db.list_theme_edges(is_active=True, limit=500)) if hasattr(db, "list_theme_edges") else len(DEFAULT_THEME_EDGES),
        "default_node_count": len(DEFAULT_THEME_NODES),
        "default_edge_count": len(DEFAULT_THEME_EDGES),
        "elapsed_seconds": round(perf_counter() - start, 4),
    }


__all__ = ["DEFAULT_THEME_EDGES", "DEFAULT_THEME_NODES", "seed_default_theme_graph"]
