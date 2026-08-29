#!/usr/bin/env python3
"""
批量为 observe_incubation 策略生成语义契约字段

这是临时的修复脚本，用于补全 Schema 迁移后缺失的契约字段。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages/akshare-mcp/src"))
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages/strategy-factory/src"))

import json
import sqlite3
from typing import Any, Mapping

# 导入 strategy-factory 的契约生成函数
from strategy_factory.application.semantic_contract_parts.policy import (
    synthesize_confidence_contract,
)

from _paths import DB_PATH


def generate_minimal_evidence_chain(params: dict) -> dict[str, Any]:
    """生成最小化的 evidence_chain"""
    return {
        "primary_evidence": [],
        "supporting_evidence": [],
        "contradicting_evidence": [],
        "evidence_quality": "bootstrapped",
        "evidence_completeness": 0.0,
        "note": "Generated during Schema migration backfill",
    }


def generate_minimal_prediction_contract(params: dict) -> dict[str, Any]:
    """生成最小化的 prediction_contract"""
    trade_plan = params.get("trade_plan", {})

    return {
        "claims": [
            {
                "claim_type": "directional",
                "direction": trade_plan.get("direction", "neutral"),
                "confidence": 0.5,
                "horizon": params.get("holding_horizon", "medium_term"),
            }
        ],
        "methodology": "bootstrapped_from_trade_plan",
        "quality": "minimal",
        "note": "Generated during Schema migration backfill",
    }


def main():
    print("=" * 80)
    print("批量生成语义契约字段")
    print("=" * 80)

    conn = sqlite3.connect(str(DB_PATH))

    # 1. 统计需要修复的策略
    cursor = conn.execute("""
        SELECT COUNT(*)
        FROM strategies
        WHERE incubating = 'observe_incubation'
        AND (
            json_extract(params, '$.evidence_chain') IS NULL
            OR json_extract(params, '$.confidence_contract') IS NULL
        )
    """)
    total_need_fix = cursor.fetchone()[0]

    print(f"\n需要修复的策略数: {total_need_fix:,}")

    if total_need_fix == 0:
        print("  没有需要修复的策略")
        conn.close()
        return

    # 2. 获取需要修复的策略
    cursor = conn.execute("""
        SELECT id, params
        FROM strategies
        WHERE incubating = 'observe_incubation'
        AND (
            json_extract(params, '$.evidence_chain') IS NULL
            OR json_extract(params, '$.confidence_contract') IS NULL
        )
        LIMIT 100
    """)

    strategies_to_fix = []
    for row in cursor:
        strategy_id = row[0]
        params = json.loads(row[1]) if row[1] else {}
        strategies_to_fix.append((strategy_id, params))

    print(f"\n第一批修复: {len(strategies_to_fix)} 个策略")

    # 3. 生成契约字段并更新
    updated_count = 0
    for strategy_id, params in strategies_to_fix:
        try:
            # 生成 evidence_chain
            if 'evidence_chain' not in params or not params['evidence_chain']:
                params['evidence_chain'] = generate_minimal_evidence_chain(params)

            # 生成 prediction_contract
            if 'prediction_contract' not in params or not params['prediction_contract']:
                params['prediction_contract'] = generate_minimal_prediction_contract(params)

            # 生成 confidence_contract（使用 strategy-factory 的标准函数）
            if 'confidence_contract' not in params or not params['confidence_contract']:
                params['confidence_contract'] = synthesize_confidence_contract(params)

            # 更新数据库
            conn.execute(
                "UPDATE strategies SET params = ? WHERE id = ?",
                (json.dumps(params, ensure_ascii=False), strategy_id)
            )
            updated_count += 1

        except Exception as e:
            print(f"  [ERROR] 策略 {strategy_id[:30]}... 失败: {e}")

    conn.commit()

    print(f"\n成功更新: {updated_count} 个策略")

    # 4. 验证
    cursor = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN json_extract(params, '$.evidence_chain') IS NOT NULL THEN 1 ELSE 0 END) as has_evidence,
            SUM(CASE WHEN json_extract(params, '$.confidence_contract') IS NOT NULL THEN 1 ELSE 0 END) as has_confidence
        FROM strategies
        WHERE incubating = 'observe_incubation'
    """)
    row = cursor.fetchone()

    print("\n验证结果:")
    print(f"  总数: {row[0]:,}")
    print(f"  有 evidence_chain: {row[1]:,} ({row[1]/row[0]*100:.1f}%)")
    print(f"  有 confidence_contract: {row[2]:,} ({row[2]/row[0]*100:.1f}%)")

    conn.close()

    print("\n" + "=" * 80)
    print("第一批完成")
    print("=" * 80)
    print("\n提示: 如果还有更多策略需要修复,请再次运行此脚本")


if __name__ == '__main__':
    main()
