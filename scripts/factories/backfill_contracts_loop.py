#!/usr/bin/env python3
"""
循环批量生成语义契约字段，直到全部完成
"""
import json
import sqlite3
from pathlib import Path
from typing import Any

from _paths import DB_PATH


def generate_minimal_evidence_chain(params: dict) -> dict[str, Any]:
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
    return {
        "raw_probability": 0.5,
        "calibrated_probability": None,
        "calibration_method": "none",
        "support_samples": 0,
        "quality": "unknown",
        "backfilled_at": "2026-06-22",
        "note": "Generated during Schema migration backfill",
    }


def process_batch(conn, batch_size=1000):
    """处理一批策略，返回处理数量"""
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

    if not strategies_to_fix:
        return 0

    updated_count = 0
    for strategy_id, params in strategies_to_fix:
        try:
            modified = False

            if 'evidence_chain' not in params or not params.get('evidence_chain'):
                params['evidence_chain'] = generate_minimal_evidence_chain(params)
                modified = True

            if 'prediction_contract' not in params or not params.get('prediction_contract'):
                params['prediction_contract'] = generate_minimal_prediction_contract(params)
                modified = True

            if 'confidence_contract' not in params or not params.get('confidence_contract'):
                params['confidence_contract'] = generate_minimal_confidence_contract(params)
                modified = True

            if modified:
                conn.execute(
                    "UPDATE strategies SET params = ? WHERE id = ?",
                    (json.dumps(params, ensure_ascii=False), strategy_id)
                )
                updated_count += 1

        except Exception as e:
            print(f"  [ERROR] {strategy_id[:30]}... : {e}")

    conn.commit()
    return updated_count


def main():
    print("=" * 80)
    print("循环批量生成语义契约字段")
    print("=" * 80)

    conn = sqlite3.connect(str(DB_PATH))

    # 初始统计
    cursor = conn.execute("""
        SELECT COUNT(*)
        FROM strategies
        WHERE incubating = 'observe_incubation'
        AND (
            json_extract(params, '$.evidence_chain') IS NULL
            OR json_extract(params, '$.confidence_contract') IS NULL
        )
    """)
    initial_count = cursor.fetchone()[0]

    print(f"\n需要修复: {initial_count:,} 个策略\n")

    total_updated = 0
    batch_num = 0
    batch_size = 2000

    while True:
        batch_num += 1
        print(f"批次 {batch_num}: 处理中...", end=" ", flush=True)

        updated = process_batch(conn, batch_size)

        if updated == 0:
            print("完成（无更多需要修复）")
            break

        total_updated += updated
        print(f"更新 {updated:,} 个")

        # 检查剩余
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

        if remaining == 0:
            break

        if batch_num % 5 == 0:
            print(f"  进度: {initial_count - remaining:,}/{initial_count:,} ({(initial_count-remaining)/initial_count*100:.1f}%)")

    # 最终验证
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

    print("\n" + "=" * 80)
    print("最终验证结果 (observe_incubation)")
    print("=" * 80)
    total = row[0]
    print(f"  总数: {total:,}")
    print(f"  有 evidence_chain: {row[1]:,} ({row[1]/total*100:.1f}%)")
    print(f"  有 prediction_contract: {row[2]:,} ({row[2]/total*100:.1f}%)")
    print(f"  有 confidence_contract: {row[3]:,} ({row[3]/total*100:.1f}%)")

    print(f"\n总计更新: {total_updated:,} 个策略")

    conn.close()

    print("\n" + "=" * 80)
    print("✅ 全部完成！")
    print("=" * 80)


if __name__ == '__main__':
    main()
