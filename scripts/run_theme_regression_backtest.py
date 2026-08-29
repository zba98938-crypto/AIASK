"""PR-7 历史回放验证脚本。

用本地 3 年 K 线数据跑一次全量回归，输出 Top 20 edges 的拟合结果
供人工审核方向符号一致性。

Usage:
    python scripts/run_theme_regression_backtest.py

输出:
    - 每条 edge 的 beta / R² / p-value / n_samples / direction_sign
    - sign_conflict 标记（回归结果与人工标注方向不一致）
    - 汇总统计：覆盖率、一致率、平均 R²
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for pkg_src in [ROOT / "packages" / "akshare-mcp" / "src", ROOT / "packages" / "strategy-factory" / "src"]:
    if str(pkg_src) not in sys.path:
        sys.path.insert(0, str(pkg_src))


async def main():
    from akshare_mcp.storage.sqlite import get_db

    db = get_db()
    await db.initialize()

    print("=" * 70)
    print("  PR-7 历史回放验证 — 主题响应回归模型")
    print("=" * 70)
    print()

    # Load edges
    async with db.acquire() as conn:
        edge_rows = await conn.fetch(
            "SELECT * FROM strategy_factory_theme_edges WHERE is_active = 1 ORDER BY source_theme_code"
        )
    edges = [dict(row) for row in edge_rows]
    print(f"Active edges: {len(edges)}")
    print()

    # Run regression
    from strategy_factory.application.research.theme_response_regression import ThemeResponseRegression

    model = ThemeResponseRegression()

    # We need a db adapter that has get_klines and list_theme_exposure
    class RegressionDbAdapter:
        def __init__(self, db_instance):
            self._db = db_instance

        async def get_klines(self, code: str, limit: int = 750):
            async with self._db.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM klines WHERE code = $1 ORDER BY date DESC LIMIT $2",
                    code, limit,
                )
            return [dict(r) for r in reversed(rows)]

        async def list_theme_exposure(self, theme_code: str = None, min_exposure: float = 0.3, limit: int = 30):
            # Use concept_detail or stocks table as proxy
            async with self._db.acquire() as conn:
                # Fallback: use stocks in the same industry as the theme
                node_row = await conn.fetchrow(
                    "SELECT * FROM strategy_factory_theme_nodes WHERE theme_code = $1",
                    theme_code,
                )
            if not node_row:
                return []

            industry_tags = []
            raw_tags = node_row.get("industry_tags") or "[]"
            if isinstance(raw_tags, str):
                try:
                    industry_tags = json.loads(raw_tags)
                except Exception:
                    pass

            if not industry_tags:
                return []

            # Find stocks matching industry
            results = []
            async with self._db.acquire() as conn:
                for tag in industry_tags[:3]:
                    rows = await conn.fetch(
                        "SELECT code as symbol, name, industry FROM stocks WHERE industry LIKE $1 LIMIT $2",
                        f"%{tag}%", limit,
                    )
                    for r in rows:
                        results.append({**dict(r), "exposure_score": 0.7})
                    if len(results) >= limit:
                        break

            return results[:limit]

        async def list_theme_edges(self, source: str = None, is_active: bool = True, limit: int = 200):
            async with self._db.acquire() as conn:
                if source:
                    rows = await conn.fetch(
                        "SELECT * FROM strategy_factory_theme_edges WHERE source_theme_code = $1 AND is_active = $2 LIMIT $3",
                        source, 1 if is_active else 0, limit,
                    )
                else:
                    rows = await conn.fetch(
                        "SELECT * FROM strategy_factory_theme_edges WHERE is_active = $1 LIMIT $2",
                        1 if is_active else 0, limit,
                    )
            return [dict(r) for r in rows]

        async def get_theme_node(self, theme_code: str):
            async with self._db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM strategy_factory_theme_nodes WHERE theme_code = $1",
                    theme_code,
                )
            return dict(row) if row else None

        async def upsert_theme_edge(self, payload: dict):
            # Dry-run: don't actually update
            pass

    adapter = RegressionDbAdapter(db)

    print("Running regression on all active edges...")
    print("-" * 70)

    results = []
    for edge in edges:
        source = edge["source_theme_code"]
        target = edge["target_theme_code"]
        manual_dir = edge["direction_sign"]

        print(f"\n  {source} → {target} ({edge['relation_type']})")

        try:
            source_returns = await model.build_theme_returns(adapter, source)
            target_returns = await model.build_theme_returns(adapter, target)

            if source_returns.empty or target_returns.empty:
                print(f"    ⚠️ Insufficient data (source={len(source_returns)}, target={len(target_returns)})")
                results.append({"source": source, "target": target, "status": "no_data"})
                continue

            min_len = min(len(source_returns), len(target_returns))
            source_returns = source_returns.iloc[:min_len]
            target_returns = target_returns.iloc[:min_len]

            shocks = model.detect_shocks(source_returns)
            result = model.fit_edge(source_returns, target_returns, shocks)
            result.source_theme = source
            result.target_theme = target

            # Check sign conflict
            if result.direction_sign != 0 and manual_dir != 0:
                if result.direction_sign != manual_dir:
                    result.sign_conflict = True

            status_icon = "✓" if result.status == "fitted" else "⚠️"
            conflict_icon = " ⚠️ SIGN CONFLICT" if result.sign_conflict else ""
            print(f"    {status_icon} beta={result.beta:.4f} R²={result.r_squared:.3f} "
                  f"p={result.p_value:.3f} n={result.n_samples} "
                  f"dir={result.direction_sign:+d} (manual={manual_dir:+d}){conflict_icon}")

            results.append(result.to_dict())

        except Exception as exc:
            print(f"    ❌ Error: {exc}")
            results.append({"source": source, "target": target, "status": "error", "error": str(exc)})

    # Summary
    print("\n" + "=" * 70)
    print("  汇总统计")
    print("=" * 70)

    fitted = [r for r in results if r.get("status") == "fitted"]
    conflicts = [r for r in results if r.get("sign_conflict")]
    no_data = [r for r in results if r.get("status") == "no_data"]

    print(f"  总 edges: {len(edges)}")
    print(f"  成功拟合: {len(fitted)}")
    print(f"  数据不足: {len(no_data)}")
    print(f"  符号冲突: {len(conflicts)}")
    if fitted:
        avg_r2 = sum(r.get("r_squared", 0) for r in fitted) / len(fitted)
        avg_samples = sum(r.get("n_samples", 0) for r in fitted) / len(fitted)
        print(f"  平均 R²: {avg_r2:.4f}")
        print(f"  平均样本数: {avg_samples:.1f}")
        consistency = 1.0 - (len(conflicts) / max(len(fitted), 1))
        print(f"  方向一致率: {consistency:.1%}")
        print()
        if consistency >= 0.75:
            print("  ✅ 达标：方向符号一致率 >= 75%，可以开启 THEME_REGRESSION_ENABLED")
        else:
            print("  ❌ 未达标：方向符号一致率 < 75%，需要人工审核冲突 edges")

    if conflicts:
        print("\n  符号冲突 edges（需人工审核）:")
        for r in conflicts:
            print(f"    {r['source_theme']} → {r['target_theme']}: "
                  f"regression={r['direction_sign']:+d} vs manual, "
                  f"beta={r['beta']:.4f}")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
