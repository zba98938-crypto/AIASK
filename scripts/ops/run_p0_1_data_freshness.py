"""P0-1 数据新鲜度运维任务执行脚本

执行步骤：
1. 检查 K 线数据新鲜度
2. 同步过期股票数据
3. 运行因子调度器回填 IC 历史
4. 验证结果
"""
import asyncio
import sys
from pathlib import Path
from datetime import datetime

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(REPO / "packages" / "akshare-mcp" / "src"))

from akshare_mcp.env_loader import load_mcp_env
load_mcp_env(override=False)


async def step1_check_freshness():
    """步骤1：检查 K 线数据新鲜度"""
    print("\n" + "="*60)
    print("Step 1: Check K-line Data Freshness")
    print("="*60)

    try:
        from akshare_mcp.tools.db_freshness import check_freshness
        from akshare_mcp.storage import get_db

        # 获取数据库中所有股票代码
        db = get_db()
        async with db.acquire() as conn:
            rows = await conn.fetch("SELECT DISTINCT code FROM kline_1d ORDER BY code")
            codes = [row["code"] for row in rows]

        if not codes:
            print("\n数据库中暂无 K 线数据")
            return []

        result = await check_freshness(codes, max_stale_days=5)

        if result:
            print(f"\n检查完成：")
            print(f"  总股票数：{result['total']}")
            print(f"  过期股票数：{result['stale_count']}")
            print(f"  缺失数据：{result['missing_count']}")

            if result['stale_count'] > 0:
                print(f"\n过期股票列表（前10个）：")
                for stock in result.get('stale', [])[:10]:
                    print(f"    {stock['code']}: 最后更新 {stock['last_date']}, 过期 {stock['staleness_days']} 天")

                return result['stale']
            else:
                print("\n[OK] 所有股票数据都是新鲜的")
                return []
        else:
            print("[ERROR] 检查失败")
            return None

    except Exception as e:
        print(f"[ERROR] 执行失败：{e}")
        import traceback
        traceback.print_exc()
        return None


async def step2_sync_stale_data(stale_stocks):
    """步骤2：同步过期股票数据"""
    print("\n" + "="*60)
    print("Step 2: Sync Stale K-line Data")
    print("="*60)

    if not stale_stocks:
        print("\n[SKIP] 没有需要同步的过期数据")
        return True

    try:
        from akshare_mcp.tools.db_freshness import sync_stale

        codes = [s['code'] for s in stale_stocks]
        print(f"\n准备同步 {len(codes)} 只股票的数据...")

        result = await sync_stale(codes=codes, max_stale_days=5)

        if result:
            print(f"\n同步完成：")
            print(f"  成功：{result['synced']}")
            print(f"  失败：{result['failed']}")
            print(f"  总计：{result['need_sync']}")

            if result.get('detail'):
                failed_items = [d for d in result['detail'] if d['status'] == 'failed']
                if failed_items:
                    print(f"\n错误列表：")
                    for item in failed_items[:5]:
                        print(f"    {item['code']}: {item.get('error', 'Unknown error')}")

            return result['failed'] == 0
        else:
            print(f"[ERROR] 同步失败")
            return False

    except Exception as e:
        print(f"[ERROR] 执行失败：{e}")
        import traceback
        traceback.print_exc()
        return False


async def step3_run_factor_scheduler():
    """步骤3：运行因子调度器回填 IC 历史"""
    print("\n" + "="*60)
    print("Step 3: Run Factor Scheduler (IC History)")
    print("="*60)

    try:
        from akshare_mcp.services.factor_scheduler import FactorScheduler

        scheduler = FactorScheduler()
        print("\n启动因子调度器...")

        # run_once 不返回 dict，直接执行
        await scheduler.run_once()

        print(f"\n调度完成")
        return True

    except Exception as e:
        print(f"[ERROR] 执行失败：{e}")
        import traceback
        traceback.print_exc()
        return False


async def step4_verify_results():
    """步骤4：验证结果"""
    print("\n" + "="*60)
    print("Step 4: Verify Results")
    print("="*60)

    try:
        from akshare_mcp.storage import get_db, close_db

        db = get_db()
        async with db.acquire() as conn:
            # Use raw sqlite connection to avoid _prepare_sql transformations
            raw_conn = conn._conn

            # 检查过期 K 线数 (stock_quotes uses 'time' not 'date')
            cursor = raw_conn.execute("""
                SELECT COUNT(*) as stale_count
                FROM (
                    SELECT code, MAX(time) as last_date
                    FROM stock_quotes
                    GROUP BY code
                    HAVING julianday('now') - julianday(MAX(time)) > 5
                ) AS stale_stocks
            """)
            stale_count = cursor.fetchone()[0]

            # 检查 factor_ic_history 最新日期 (uses 'ic_date' and 'factor_name')
            cursor = raw_conn.execute("""
                SELECT MAX(ic_date) as max_date, COUNT(DISTINCT factor_name) as factor_count
                FROM factor_ic_history
            """)
            result = cursor.fetchone()
            max_ic_date = result[0] if result else None
            factor_count = result[1] if result else 0

            print(f"\n验证结果：")
            print(f"  过期 K 线数：{stale_count}")
            print(f"  factor_ic_history 最新日期：{max_ic_date or 'N/A'}")
            print(f"  factor_ic_history 因子数：{factor_count}")

            success = stale_count == 0 and max_ic_date is not None

            if success:
                print(f"\n[OK] 数据新鲜度验证通过")
            else:
                print(f"\n[WARNING] 数据可能需要进一步检查")

            return success

    except Exception as e:
        print(f"[ERROR] 验证失败：{e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        await close_db()


async def main():
    """执行所有步骤"""
    print("="*60)
    print(f"P0-1 数据新鲜度运维任务")
    print(f"执行时间：{datetime.now().isoformat()}")
    print("="*60)

    # Step 1: 检查新鲜度
    stale_stocks = await step1_check_freshness()
    if stale_stocks is None:
        print("\n[ABORT] 无法检查数据新鲜度")
        return 1

    # Step 2: 同步过期数据
    if stale_stocks:
        sync_ok = await step2_sync_stale_data(stale_stocks)
        if not sync_ok:
            print("\n[WARNING] 数据同步未完全成功，继续执行...")

    # Step 3: 运行因子调度器
    scheduler_ok = await step3_run_factor_scheduler()
    if not scheduler_ok:
        print("\n[WARNING] 因子调度器执行失败")

    # Step 4: 验证结果
    verify_ok = await step4_verify_results()

    # 总结
    print("\n" + "="*60)
    print("执行总结")
    print("="*60)
    if verify_ok:
        print("[OK] P0-1 数据新鲜度运维任务完成")
        return 0
    else:
        print("[WARNING] 部分任务未成功，请检查日志")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
