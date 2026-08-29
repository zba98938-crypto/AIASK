"""PR-DQ2: 从 seed 因子 IC 历史回填经典因子 IC。

seed 因子（如 factor_memory_seed_momentum）和经典因子（momentum）本质相同，
但策略工厂查询的是经典名称。本脚本将 seed 因子的 60 天 IC 历史复制到经典因子名下。

Usage:
    python scripts/backfill_classic_factor_ic.py
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))

env_path = ROOT / ".env"
for line in env_path.read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())

# Mapping: classic factor name → seed factor name
CLASSIC_TO_SEED = {
    "momentum": "factor_candidate:factor_memory_seed_momentum",
    "value": "factor_candidate:factor_memory_seed_value",
    "quality": "factor_candidate:factor_memory_seed_quality",
    "growth": "factor_candidate:factor_memory_seed_growth",
    "volatility": "factor_candidate:factor_memory_seed_volatility",
    "reversal": "factor_candidate:factor_memory_seed_reversal",
}


async def main():
    from akshare_mcp.storage.sqlite import get_db

    db = get_db()
    await db.initialize()

    print("=== PR-DQ2: 回填经典因子 IC 历史 ===")
    print()

    total_inserted = 0

    async with db.acquire() as conn:
        for classic_name, seed_name in CLASSIC_TO_SEED.items():
            # Check existing classic IC count
            existing = await conn.fetch(
                "SELECT COUNT(*) as cnt FROM factor_ic_history WHERE factor_name = $1",
                classic_name,
            )
            existing_count = existing[0]["cnt"] if existing else 0

            # Get seed IC history
            seed_rows = await conn.fetch(
                "SELECT * FROM factor_ic_history WHERE factor_name = $1 ORDER BY ic_date",
                seed_name,
            )

            if not seed_rows:
                print(f"  {classic_name}: seed 数据为空，跳过")
                continue

            inserted = 0
            for row in seed_rows:
                try:
                    await conn.execute(
                        """
                        INSERT OR IGNORE INTO factor_ic_history
                            (factor_name, period, ic_date, ic_value, rank_ic, stock_count, computed_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                        classic_name,
                        row["period"],
                        row["ic_date"],
                        row["ic_value"],
                        row.get("rank_ic"),
                        row.get("stock_count"),
                        row.get("computed_at"),
                    )
                    inserted += 1
                except Exception:
                    pass  # Duplicate, skip

            total_inserted += inserted
            print(f"  {classic_name}: 已有 {existing_count} 条, 从 seed 回填 {inserted} 条 (seed 共 {len(seed_rows)} 条)")

    print()
    print(f"总计回填: {total_inserted} 条")

    # Verify
    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT factor_name, COUNT(*) as cnt, ROUND(AVG(ic_value), 4) as avg_ic
            FROM factor_ic_history
            WHERE factor_name IN ('momentum','value','quality','growth','volatility','reversal')
            GROUP BY factor_name
            ORDER BY factor_name
            """
        )
    print()
    print("验证结果:")
    for row in rows:
        print(f"  {row['factor_name']}: {row['cnt']} 条, 平均 IC={row['avg_ic']}")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
