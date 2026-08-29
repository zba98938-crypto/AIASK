"""修复数据库表结构 — 解决 stocks 表 code/stock_code 双列冲突"""
import asyncio, sys, os
from pathlib import Path

env_path = Path(__file__).resolve().parents[4] / '.env'
if env_path.exists():
    for line in env_path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            os.environ[k.strip()] = v.strip()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))
from akshare_mcp.storage import run_with_db_cleanup
from akshare_mcp.storage.sqlite import get_db


async def fix():
    db = get_db()
    await db.initialize()
    async with db.acquire() as conn:
        # ============================================================
        # 1. stocks 表：修复 code 列 NOT NULL 冲突
        # ============================================================
        print("[1] 检查 stocks 表结构...")
        cols = await conn.fetch(
            "SELECT name AS column_name, CASE WHEN notnull THEN 'NO' ELSE 'YES' END AS is_nullable "
            "FROM pragma_table_info('stocks') ORDER BY cid"
        )
        col_map = {r['column_name']: r['is_nullable'] for r in cols}
        print(f"  当前列: {list(col_map.keys())}")

        if 'code' in col_map and 'stock_code' in col_map:
            print("  检测到 code + stock_code 双列共存")

            # 1a. 如果 code 列有数据但 stock_code 没有，同步过去
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM stocks WHERE stock_code IS NULL AND code IS NOT NULL"
            )
            if cnt and cnt > 0:
                print(f"  → 同步 {cnt} 条 code → stock_code...")
                await conn.execute("UPDATE stocks SET stock_code = code WHERE stock_code IS NULL AND code IS NOT NULL")

            # 1b. 确认主键列；SQLite 不支持在线重写主键，这里只报告。
            pk = await conn.fetch("SELECT name FROM pragma_table_info('stocks') WHERE pk > 0 ORDER BY pk")
            pk_cols = [r['name'] for r in pk]
            print(f"  当前 PK: {pk_cols}")

            if 'stock_code' not in pk_cols:
                print("  → SQLite 需要表重建才能切换主键；保留现状并清理空 stock_code")
                await conn.execute("DELETE FROM stocks WHERE stock_code IS NULL OR stock_code = ''")

            # 1d. 添加缺失列
            for col, dtype in [
                ('market', 'TEXT'),
                ('sector', 'TEXT'),
                ('kline_sync_attempted', 'TEXT'),
            ]:
                await conn.execute(f"ALTER TABLE stocks ADD COLUMN IF NOT EXISTS {col} {dtype}")

            print("  ✅ stocks 表修复完成")

        elif 'code' in col_map and 'stock_code' not in col_map:
            print("  只有 code 列，添加 stock_code 并迁移...")
            await conn.execute("ALTER TABLE stocks ADD COLUMN stock_code TEXT")
            await conn.execute("UPDATE stocks SET stock_code = code")
            # 添加缺失列
            for col, dtype in [('market', 'TEXT'), ('sector', 'TEXT'), ('kline_sync_attempted', 'TEXT')]:
                await conn.execute(f"ALTER TABLE stocks ADD COLUMN IF NOT EXISTS {col} {dtype}")
            print("  ✅ 已添加 stock_code 并同步数据")

        else:
            print("  ✅ stocks 表结构正常")

        # ============================================================
        # 2. financials 表：确保 stock_code 列存在
        # ============================================================
        print("\n[2] 检查 financials 表...")
        fin_cols = await conn.fetch(
            "SELECT name AS column_name FROM pragma_table_info('financials')"
        )
        fin_col_names = [r['column_name'] for r in fin_cols]

        if 'code' in fin_col_names and 'stock_code' not in fin_col_names:
            print("  → 添加 stock_code 列并同步...")
            await conn.execute("ALTER TABLE financials ADD COLUMN IF NOT EXISTS stock_code TEXT")
            await conn.execute("UPDATE financials SET stock_code = code WHERE stock_code IS NULL")
            await conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_financials_stock_code_report_date ON financials(stock_code, report_date)")
            print("  ✅ UNIQUE 索引已更新")
        else:
            print("  ✅ financials 表结构正常")

        # 补齐财务列
        for col, dtype in [
            ('gross_margin', 'REAL'),
            ('net_margin', 'REAL'),
            ('current_ratio', 'REAL'),
            ('bvps', 'REAL'),
            ('roa', 'REAL'),
        ]:
            await conn.execute(f"ALTER TABLE financials ADD COLUMN IF NOT EXISTS {col} {dtype}")

        # ============================================================
        # 3. 验证
        # ============================================================
        print("\n[3] 验证修复结果...")
        for table in ['stocks', 'financials']:
            cols = await conn.fetch(
                "SELECT name AS column_name, CASE WHEN notnull THEN 'NO' ELSE 'YES' END AS is_nullable "
                "FROM pragma_table_info($1) ORDER BY cid", table
            )
            print(f"  {table}:")
            for c in cols:
                nullable = "NULL" if c['is_nullable'] == 'YES' else "NOT NULL"
                print(f"    {c['column_name']:25s} {nullable}")

        # 检查 stocks PK
        pk = await conn.fetch("SELECT name FROM pragma_table_info('stocks') WHERE pk > 0 ORDER BY pk")
        print(f"\n  stocks PK: {[r['name'] for r in pk]}")

        # 检查 stocks 行数
        cnt = await conn.fetchval("SELECT COUNT(*) FROM stocks")
        print(f"  stocks 行数: {cnt}")

    print("\n✅ 表结构修复完成")


run_with_db_cleanup(fix())
