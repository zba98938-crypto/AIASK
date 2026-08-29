#!/usr/bin/env python3
"""分析策略质量"""

import asyncio
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parents[1]
STRATEGY_FACTORY_SRC = PROJECT_ROOT / "packages" / "strategy-factory" / "src"
AKSHARE_MCP_SRC = PROJECT_ROOT / "packages" / "akshare-mcp" / "src"

if str(STRATEGY_FACTORY_SRC) not in sys.path:
    sys.path.insert(0, str(STRATEGY_FACTORY_SRC))
if str(AKSHARE_MCP_SRC) not in sys.path:
    sys.path.insert(0, str(AKSHARE_MCP_SRC))

from strategy_factory.runtime_bootstrap import ensure_factory_runtime

ensure_factory_runtime(
    project_root=PROJECT_ROOT,
    script_path=Path(__file__).resolve(),
    argv=[],
    editable_packages=(
        "packages/strategy-factory",
        "packages/aiask-quant-core",
        "packages/akshare-mcp",
    ),
)


async def main():
    from akshare_mcp.storage import get_db, close_db

    db = get_db()
    await db.initialize()

    print('=== 策略质量分析 (从数据库) ===\n')

    # 1. 各状态策略数量
    print('1. 策略生命周期分布:')
    lifecycle_counts = {}
    for status in ['incubating', 'paper_observation', 'diagnostic_observation', 'formal', 'archived']:
        strategies = await db.list_strategies(status, limit=5000)
        count = len(strategies)
        lifecycle_counts[status] = count
        print(f'   {status}: {count}')

    print(f'\n   总计: {sum(lifecycle_counts.values())} 个策略')

    # 2. 孵化中策略的详细质量
    print('\n2. Incubating 策略质量样本 (前5个):')
    incubating = await db.list_strategies('incubating', limit=5)
    for i, s in enumerate(incubating, 1):
        print(f'   [{i}] {s["id"][:25]}...')
        print(f'       Type: {s.get("strategy_type", "unknown")}')
        print(f'       Created: {s.get("created_at", "unknown")[:10]}')
        params = s.get('params', {}) or {}
        print(f'       Execution Semantic Mode: {params.get("execution_semantic_mode", "N/A")}')
        print(f'       Revision Required: {params.get("revision_required", False)}')

        # 显示目标标的
        target_symbols = params.get('target_symbols', [])
        if target_symbols:
            print(f'       Target Symbols: {", ".join(target_symbols[:3])}{"..." if len(target_symbols) > 3 else ""}')

    # 3. Paper observation 策略质量和命中率
    print('\n3. Paper Observation 策略质量 (前3个，包含命中率):')
    paper = await db.list_strategies('paper_observation', limit=3)
    for i, s in enumerate(paper, 1):
        print(f'   [{i}] {s["id"][:25]}...')
        print(f'       Type: {s.get("strategy_type", "unknown")}')
        print(f'       Created: {s.get("created_at", "unknown")[:10]}')

        # 查询最新孵化指标
        if hasattr(db, 'list_strategy_incubation_metrics'):
            metrics = await db.list_strategy_incubation_metrics(s['id'], limit=1)
            if metrics:
                m = metrics[0]
                print(f'       Latest Metrics ({m.get("as_of", "unknown")[:10]}):')
                print(f'         Hit Rate: {m.get("hit_rate", 0)*100:.1f}%')
                print(f'         Skill: {m.get("skill", 0):.4f}')
                print(f'         Avg 5D Return: {m.get("avg_forward_return_5d", 0)*100:.2f}%')
                print(f'         Signal Count: {m.get("signal_count", 0)}')
            else:
                print(f'       No metrics yet')

    # 4. 语义修复原因分布
    print('\n4. 语义修复原因分布 (Incubating):')
    all_incubating = await db.list_strategies('incubating', limit=500)
    revision_reasons = {}
    revision_count = 0

    for s in all_incubating:
        params = s.get('params', {}) or {}
        if params.get('revision_required'):
            revision_count += 1
            gap_reasons = params.get('execution_semantic_gap_reasons', [])
            if gap_reasons:
                for reason in gap_reasons:
                    revision_reasons[reason] = revision_reasons.get(reason, 0) + 1

    print(f'   需要修订的策略: {revision_count}/{len(all_incubating)} ({revision_count/len(all_incubating)*100:.1f}%)')
    print(f'\n   原因分布 (Top 10):')
    for reason, count in sorted(revision_reasons.items(), key=lambda x: x[1], reverse=True)[:10]:
        print(f'     - {reason}: {count}')

    # 5. 策略类型分布
    print('\n5. 策略类型分布 (Incubating + Paper):')
    all_active = await db.list_strategies('incubating', limit=500)
    all_active += await db.list_strategies('paper_observation', limit=1500)

    strategy_types = {}
    for s in all_active:
        stype = s.get('strategy_type', 'unknown')
        strategy_types[stype] = strategy_types.get(stype, 0) + 1

    for stype, count in sorted(strategy_types.items(), key=lambda x: x[1], reverse=True):
        pct = count / len(all_active) * 100
        print(f'   {stype}: {count} ({pct:.1f}%)')

    # 6. 命中率统计 (Paper observation)
    print('\n6. 命中率统计 (Paper Observation, 最近指标):')
    paper_all = await db.list_strategies('paper_observation', limit=1500)

    hit_rates = []
    skills = []

    for s in paper_all[:100]:  # 采样前100个
        if hasattr(db, 'list_strategy_incubation_metrics'):
            metrics = await db.list_strategy_incubation_metrics(s['id'], limit=1)
            if metrics:
                m = metrics[0]
                hr = m.get('hit_rate', 0)
                sk = m.get('skill', 0)
                if hr > 0:
                    hit_rates.append(hr)
                if sk != 0:
                    skills.append(sk)

    if hit_rates:
        avg_hit_rate = sum(hit_rates) / len(hit_rates)
        print(f'   样本数: {len(hit_rates)}')
        print(f'   平均命中率: {avg_hit_rate*100:.1f}%')
        print(f'   最高命中率: {max(hit_rates)*100:.1f}%')
        print(f'   最低命中率: {min(hit_rates)*100:.1f}%')

    if skills:
        avg_skill = sum(skills) / len(skills)
        print(f'\n   Skill 统计:')
        print(f'     平均 Skill: {avg_skill:.4f}')
        print(f'     最高 Skill: {max(skills):.4f}')
        print(f'     最低 Skill: {min(skills):.4f}')
        positive_skill = sum(1 for s in skills if s > 0)
        print(f'     正Skill策略: {positive_skill}/{len(skills)} ({positive_skill/len(skills)*100:.1f}%)')

    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
