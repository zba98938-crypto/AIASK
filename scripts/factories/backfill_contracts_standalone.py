#!/usr/bin/env python3
"""
批量为 observe_incubation 策略生成语义契约字段（独立版本）
"""
import json
import sqlite3
from pathlib import Path
from typing import Any

from _paths import DB_PATH


def generate_minimal_evidence_chain(params: dict) -> dict[str, Any]:
    """生成最小化的 evidence_chain"""
    return {
        "primary_evidence": [],
        "supporting_evidence": [],
        "contradicting_evidence": [],
        "evidence_quality": "bootstrapped",
        "evidence_completeness": 0.0,
        "backfilled_at": "2026-06-22",
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
                "backfilled": True,
            }
        ],
        "methodology": "bootstrapped_from_trade_plan",
        "quality": "minimal",
        "backfilled_at": "2026-06-22",
        "note": "Generated during Schema migration backfill",
    }


def generate_minimal_confidence_contract(params: dict) -> dict[str, Any]:
    """生成最小化的 confidence_contract"""
    return {
        "raw_probability": 0.5,
        "calibrated_probability": None,
        "calibration_method": "none",
        "support_samples": 0,
        "quality": "unknown",
        "backfilled_at": "2026-06-22",
        "note": "Generated during Schema migration backfill",
    }


def main():
    print("=" * 80)
    print("批量生成语义契约字段（独立版本）")
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

    # 2. 分批处理
    batch_size = 1000
    cursor = conn.execute("""
        SELECT id, params
        FROM strategies
        WHERE incubating = 'observe_incubation'
        AND (
            json_extract(params, '$.evidence_chain') IS NULL
            OR json_extract(params, '$.confidence_contract') IS NULL
        )
        LIMIT ?
    """, (batch_size,))

    strategies_to_fix = []
    for row in cursor:
        strategy_id = row[0]
        params = json.loads(row[1]) if row[1] else {}
        strategies_to_fix.append((strategy_id, params))

    print(f"\n当前批次: {len(strategies_to_fix)} 个策略")

    # 3. 生成契约字段并更新
    updated_count = 0
    error_count = 0

    for strategy_id, params in strategies_to_fix:
        try:
            modified = False

            # 生成 evidence_chain
            if 'evidence_chain' not in params or not params.get('evidence_chain'):
                params['evidence_chain'] = generate_minimal_evidence_chain(params)
                modified = True

            # 生成 prediction_contract
            if 'prediction_contract' not in params or not params.get('prediction_contract'):
                params['prediction_contract'] = generate_minimal_prediction_contract(params)
                modified = True

            # 生成 confidence_contract
            if 'confidence_contract' not in params or not params.get('confidence_contract'):
                params['confidence_contract'] = generate_minimal_confidence_contract(params)
                modified = True

            if modified:
                # 更新数据库
                conn.execute(
                    "UPDATE strategies SET params = ? WHERE id = ?",
                    (json.dumps(params, ensure_ascii=False), strategy_id)
                )
                updated_count += 1

                if updated_count % 100 == 0:
                    print(f"  进度: {updated_count}/{len(strategies_to_fix)}")

        except Exception as e:
            error_count += 1
            print(f"  [ERROR] 策略 {strategy_id[:30]}... 失败: {e}")

    conn.commit()

    print(f"\n成功更新: {updated_count:,} 个策略")
    if error_count > 0:
        print(f"失败: {error_count} 个策略")

    # 4. 验证
    cursor = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN json_extract(params, '$.evidence_chain') IS NOT NULL THEN 1 ELSE 0 END) as has_evidence,
            SUM(CASE WHEN json_extract(params, '$.prediction_contract') IS NOT NULL THEN 1 ELSE 0 END) as has_prediction,
            SUM(CASE WHEN json_extract(params, '$.confidence_contract') IS NOT NULL THEN 1 ELSE 0 END) as has_confidence
        FROM strategies
        WHERE incubating = 'observe_incubation'
    """)
    row = cursor.fetchone()

    print("\n验证结果 (observe_incubation):")
    total = row[0]
    print(f"  总数: {total:,}")
    print(f"  有 evidence_chain: {row[1]:,} ({row[1]/total*100:.1f}%)")
    print(f"  有 prediction_contract: {row[2]:,} ({row[2]/total*100:.1f}%)")
    print(f"  有 confidence_contract: {row[3]:,} ({row[3]/total*100:.1f}%)")

    # 检查是否还有需要修复的
    cursor = conn.execute("""
        SELECT COUNT(*)
        FROM strategies
        WHERE incubating = 'observe_incubation'
        AND (
            json_extract(params, '$.evidence_chain') IS NULL
            OR json_extract(params, '$.confidence_contract') IS NULL
        )
    """)
    remaining = cursor.fetchone()[0]

    conn.close()

    print("\n" + "=" * 80)
    print(f"当前批次完成: 更新 {updated_count:,} 个策略")
    print("=" * 80)

    if remaining > 0:
        print(f"\n还有 {remaining:,} 个策略需要修复")
        print("请再次运行此脚本继续处理")
    else:
        print("\n✅ 所有策略已修复完成！")


if __name__ == '__main__':
    main()
