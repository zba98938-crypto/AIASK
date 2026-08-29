"""Idempotent seed script for strategy_factory theme graph (PR-1).

Seeds 15 theme nodes + 10 edges as defined in §19.2 of the upgrade plan.
Safe to run multiple times — uses INSERT OR IGNORE for idempotency.

Usage:
    python scripts/seed_strategy_factory_theme_graph.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure packages are importable
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
for pkg_src in [ROOT / "packages" / "akshare-mcp" / "src", ROOT / "packages" / "strategy-factory" / "src"]:
    if str(pkg_src) not in sys.path:
        sys.path.insert(0, str(pkg_src))

from _integration_paths import default_sqlite_path


THEME_NODES = [
    # 资源/上游 (3)
    {"theme_code": "upstream_oil_gas", "theme_name": "上游油气", "breadth": "narrow", "default_horizon": "swing_1_5d", "industry_tags": '["石油开采","油服工程"]', "shock_detection_profile": "fast", "benchmark_index_code": "000300"},
    {"theme_code": "coal", "theme_name": "煤炭", "breadth": "narrow", "default_horizon": "swing_5_20d", "industry_tags": '["煤炭开采","焦炭"]', "shock_detection_profile": "medium", "benchmark_index_code": "000300"},
    {"theme_code": "gold", "theme_name": "黄金", "breadth": "narrow", "default_horizon": "swing_5_20d", "industry_tags": '["贵金属"]', "shock_detection_profile": "fast", "benchmark_index_code": "000300"},
    # 制造/中游 (3)
    {"theme_code": "shipping_trade", "theme_name": "航运贸易", "breadth": "narrow", "default_horizon": "swing_5_20d", "industry_tags": '["航运","港口"]', "shock_detection_profile": "fast", "benchmark_index_code": "000300"},
    {"theme_code": "airlines", "theme_name": "航空", "breadth": "narrow", "default_horizon": "swing_1_5d", "industry_tags": '["航空运输"]', "shock_detection_profile": "fast", "benchmark_index_code": "000300"},
    {"theme_code": "refinery", "theme_name": "炼化", "breadth": "narrow", "default_horizon": "swing_5_20d", "industry_tags": '["石油化工","精细化工"]', "shock_detection_profile": "fast", "benchmark_index_code": "000300"},
    # 能源转型 (2)
    {"theme_code": "new_energy_chain", "theme_name": "新能源产业链", "breadth": "broad", "default_horizon": "swing_5_20d", "industry_tags": '["电池","光伏设备","风电设备"]', "shock_detection_profile": "medium", "benchmark_index_code": "000300"},
    {"theme_code": "photovoltaic", "theme_name": "光伏", "breadth": "medium", "default_horizon": "swing_5_20d", "industry_tags": '["光伏设备","电池"]', "shock_detection_profile": "medium", "benchmark_index_code": "000300"},
    # 消费 (2)
    {"theme_code": "liquor_consumption", "theme_name": "白酒消费", "breadth": "narrow", "default_horizon": "swing_5_20d", "industry_tags": '["白酒","饮料制造"]', "shock_detection_profile": "slow", "benchmark_index_code": "000300"},
    {"theme_code": "appliance", "theme_name": "家电", "breadth": "medium", "default_horizon": "swing_5_20d", "industry_tags": '["白色家电","小家电","厨卫电器"]', "shock_detection_profile": "slow", "benchmark_index_code": "000300"},
    # 科技 (2)
    {"theme_code": "chip_domestic", "theme_name": "国产芯片", "breadth": "medium", "default_horizon": "swing_5_20d", "industry_tags": '["半导体","集成电路"]', "shock_detection_profile": "fast", "benchmark_index_code": "000300"},
    {"theme_code": "ai_compute", "theme_name": "AI算力", "breadth": "medium", "default_horizon": "swing_5_20d", "industry_tags": '["计算机设备","IT服务","软件开发"]', "shock_detection_profile": "fast", "benchmark_index_code": "000300"},
    # 金融/防御 (2)
    {"theme_code": "high_dividend_banks", "theme_name": "高股息银行", "breadth": "narrow", "default_horizon": "macro_1m", "industry_tags": '["银行"]', "shock_detection_profile": "slow", "benchmark_index_code": "000922"},
    {"theme_code": "insurance", "theme_name": "保险", "breadth": "narrow", "default_horizon": "swing_5_20d", "industry_tags": '["保险"]', "shock_detection_profile": "slow", "benchmark_index_code": "000300"},
    # 地产 (1)
    {"theme_code": "real_estate_dev", "theme_name": "房地产开发", "breadth": "medium", "default_horizon": "macro_1m", "industry_tags": '["房地产开发"]', "shock_detection_profile": "slow", "benchmark_index_code": "000300"},
]

THEME_EDGES = [
    # source_theme_code, target_theme_code, relation_type, direction_sign, magnitude_factor, lag_days, confidence
    ("upstream_oil_gas", "airlines", "supply_shock", -1, 0.70, 1, 0.65),
    ("upstream_oil_gas", "shipping_trade", "amplifies", +1, 0.55, 1, 0.60),
    ("upstream_oil_gas", "refinery", "cost_transmits", -1, 0.50, 1, 0.60),
    ("upstream_oil_gas", "high_dividend_banks", "amplifies", +1, 0.25, 2, 0.40),
    ("high_dividend_banks", "real_estate_dev", "substitutes", -1, 0.40, 5, 0.50),
    ("new_energy_chain", "upstream_oil_gas", "substitutes", -1, 0.30, 10, 0.45),
    ("photovoltaic", "new_energy_chain", "amplifies", +1, 0.60, 0, 0.70),
    ("chip_domestic", "ai_compute", "amplifies", +1, 0.50, 3, 0.60),
    ("liquor_consumption", "appliance", "amplifies", +1, 0.35, 5, 0.45),
    ("insurance", "high_dividend_banks", "amplifies", +1, 0.50, 0, 0.55),
]


async def seed(db_path: str | None = None):
    """Run the seed against the SQLite database."""
    import sqlite3

    resolved_path = db_path or os.getenv("AKSHARE_MCP_SQLITE_PATH") or os.getenv("AIASK_SQLITE_PATH") or str(default_sqlite_path())
    resolved_path = str(Path(resolved_path).expanduser())

    print(f"[seed] Connecting to: {resolved_path}")

    conn = sqlite3.connect(resolved_path)
    try:
        # Seed theme nodes
        inserted_nodes = 0
        for node in THEME_NODES:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO strategy_factory_theme_nodes
                    (theme_code, theme_name, breadth, default_horizon, aliases, industry_tags,
                     shock_detection_profile, benchmark_index_code, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node["theme_code"],
                    node["theme_name"],
                    node["breadth"],
                    node["default_horizon"],
                    node.get("aliases", "[]"),
                    node.get("industry_tags", "[]"),
                    node.get("shock_detection_profile", "fast"),
                    node.get("benchmark_index_code", "000300"),
                    node.get("description"),
                ),
            )
            if cursor.rowcount > 0:
                inserted_nodes += 1

        # Seed theme edges
        inserted_edges = 0
        for src, tgt, rel, direction, mag, lag, conf in THEME_EDGES:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO strategy_factory_theme_edges
                    (source_theme_code, target_theme_code, relation_type,
                     direction_sign, magnitude_factor, lag_days, confidence,
                     confidence_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'manual')
                """,
                (src, tgt, rel, direction, mag, lag, conf),
            )
            if cursor.rowcount > 0:
                inserted_edges += 1

        conn.commit()
    finally:
        conn.close()

    print(f"[seed] Done: {inserted_nodes} nodes inserted, {inserted_edges} edges inserted")
    print(f"[seed] Total in seed: {len(THEME_NODES)} nodes, {len(THEME_EDGES)} edges")
    if inserted_nodes == 0 and inserted_edges == 0:
        print("[seed] All data already existed (idempotent)")


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(seed(db_path))
