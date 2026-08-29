#!/usr/bin/env python3
"""
Schema 迁移脚本: status → incubating 列

将旧架构的 status 列数据迁移到新架构的 incubating 列
"""
import sqlite3
import sys
from pathlib import Path
from datetime import datetime
import shutil

from _paths import DB_PATH
BACKUP_DIR = DB_PATH.parent / "backups"

def backup_database():
    """备份数据库"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"akshare_mcp.sqlite3.before_schema_migration_{timestamp}.sqlite3"

    print(f"正在备份数据库...")
    print(f"  源: {DB_PATH}")
    print(f"  目标: {backup_path}")

    shutil.copy2(DB_PATH, backup_path)
    backup_size = backup_path.stat().st_size / 1024 / 1024

    print(f"  [OK] 备份完成 ({backup_size:.2f} MB)")
    return backup_path

def check_current_schema(conn):
    """检查当前 schema"""
    cursor = conn.execute("PRAGMA table_info(strategies)")
    cols = {row[1] for row in cursor}

    print("\n当前 strategies 表结构:")
    print(f"  列数: {len(cols)}")
    print(f"  有 status 列: {'status' in cols}")
    print(f"  有 incubating 列: {'incubating' in cols}")

    return cols

def add_incubating_column(conn):
    """添加 incubating 列"""
    cursor = conn.execute("PRAGMA table_info(strategies)")
    cols = {row[1] for row in cursor}

    if 'incubating' in cols:
        print("\n[SKIP] incubating 列已存在")
        return False

    print("\n[1] 添加 incubating 列...")
    conn.execute("ALTER TABLE strategies ADD COLUMN incubating TEXT")
    conn.commit()
    print("  [OK] 列已添加")
    return True

def migrate_data(conn, dry_run=True):
    """迁移数据"""
    print(f"\n[2] 迁移数据 ({'DRY RUN' if dry_run else 'REAL RUN'})...")

    # 查看当前 status 分布
    cursor = conn.execute("""
        SELECT status, COUNT(*) as cnt
        FROM strategies
        GROUP BY status
        ORDER BY cnt DESC
    """)

    print("\n  当前 status 分布:")
    status_counts = {}
    for row in cursor:
        status, cnt = row
        status_counts[status] = cnt
        print(f"    {status}: {cnt}")

    # 定义迁移映射
    migration_map = {
        'incubating': 'observe_incubation',  # 默认先进 observe
        'listed': 'production',
        'submitted': 'observe_incubation',  # 修改：让 submitted 也进入孵化（包含 678 个高 skill 策略）
        'rejected': None,
        'draft': None,
        'archived': None,
        'diagnostic': 'observe_diagnostic_only',
        'deprecated': None,
    }

    print("\n  迁移规则:")
    for old_status, new_incubating in migration_map.items():
        if old_status in status_counts:
            print(f"    {old_status} ({status_counts[old_status]}) → {new_incubating or '(NULL)'}")

    if dry_run:
        print("\n  [DRY RUN] 不执行实际迁移")
        return

    # 执行迁移
    print("\n  执行迁移...")
    for old_status, new_incubating in migration_map.items():
        if new_incubating:
            conn.execute("""
                UPDATE strategies
                SET incubating = ?
                WHERE status = ? AND incubating IS NULL
            """, (new_incubating, old_status))

    conn.commit()
    print("  [OK] 迁移完成")

def verify_migration(conn):
    """验证迁移结果"""
    print("\n[3] 验证迁移结果...")

    cursor = conn.execute("""
        SELECT incubating, COUNT(*) as cnt
        FROM strategies
        GROUP BY incubating
        ORDER BY cnt DESC
    """)

    print("\n  迁移后 incubating 分布:")
    total_migrated = 0
    for row in cursor:
        incubating, cnt = row
        if incubating:
            total_migrated += cnt
        print(f"    {incubating or '(NULL)'}: {cnt}")

    print(f"\n  已迁移记录: {total_migrated}")

def main():
    """主流程"""
    import argparse

    parser = argparse.ArgumentParser(description="Schema 迁移: status → incubating")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="仅模拟迁移，不实际修改数据库（默认）")
    parser.add_argument("--execute", action="store_true",
                        help="执行实际迁移（覆盖 --dry-run）")
    parser.add_argument("--no-backup", action="store_true",
                        help="跳过备份（不推荐）")

    args = parser.parse_args()

    dry_run = not args.execute

    print("="*80)
    print("Schema 迁移: status → incubating")
    print("="*80)

    if not DB_PATH.exists():
        print(f"\n[ERROR] 数据库不存在: {DB_PATH}")
        return 1

    # 1. 备份
    if not args.no_backup and not dry_run:
        backup_path = backup_database()
        print(f"\n  备份路径: {backup_path}")
    elif dry_run:
        print("\n[DRY RUN] 跳过备份")
    else:
        print("\n[WARN] 跳过备份（--no-backup）")

    # 2. 连接数据库
    conn = sqlite3.connect(str(DB_PATH))

    try:
        # 3. 检查当前 schema
        cols = check_current_schema(conn)

        if 'status' not in cols:
            print("\n[ERROR] strategies 表没有 status 列")
            return 1

        # 4. 添加 incubating 列
        added = add_incubating_column(conn)

        # 5. 迁移数据
        migrate_data(conn, dry_run=dry_run)

        # 6. 验证
        if not dry_run:
            verify_migration(conn)

        print("\n" + "="*80)
        if dry_run:
            print("DRY RUN 完成 - 未修改数据库")
            print("\n要执行实际迁移，请运行:")
            print(f"  python {Path(__file__).name} --execute")
        else:
            print("迁移完成！")
        print("="*80)

        return 0

    except Exception as e:
        print(f"\n[ERROR] 迁移失败: {e}")
        import traceback
        traceback.print_exc()
        return 1

    finally:
        conn.close()

if __name__ == '__main__':
    sys.exit(main())
