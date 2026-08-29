#!/usr/bin/env python3
"""
四工厂启动前置条件检查

检查是否满足运行四工厂的所有条件
"""
import sqlite3
from pathlib import Path
import sys

from _paths import DB_PATH

def check_database():
    """检查数据库状态"""
    print("\n[1] 数据库检查")
    print("-" * 80)

    if not DB_PATH.exists():
        print("  ❌ 数据库不存在")
        return False

    conn = sqlite3.connect(str(DB_PATH))

    # 检查 incubating 列
    cursor = conn.execute("PRAGMA table_info(strategies)")
    cols = {row[1] for row in cursor}

    if 'incubating' not in cols:
        print("  ❌ strategies 表缺少 incubating 列")
        print("     需要先运行 Schema 迁移")
        conn.close()
        return False

    print("  ✅ 数据库结构正常")

    # 检查策略数量
    cursor = conn.execute("SELECT COUNT(*) FROM strategies WHERE incubating = 'observe_incubation'")
    observe_count = cursor.fetchone()[0]

    print(f"  ✅ observe_incubation 策略: {observe_count}")

    conn.close()
    return True

def check_quality_session():
    """检查是否有 Quality Session 正在运行"""
    print("\n[2] Quality Session 检查")
    print("-" * 80)

    sessions_dir = Path("logs/strategy_factory_quality_sessions")
    if not sessions_dir.exists():
        print("  ⚠️  没有 Quality Session 历史")
        return True  # 不是必须的

    # 查找最新的会话
    sessions = sorted(sessions_dir.glob("strategy_factory_quality_*"),
                     key=lambda p: p.stat().st_mtime, reverse=True)

    if sessions:
        latest = sessions[0]
        print(f"  ℹ️  最新 Quality Session: {latest.name}")

        lock_file = latest / "session.lock.json"
        if lock_file.exists():
            print("  ℹ️  Quality Session 可能正在运行")
            print("     建议: 让 Quality Session 运行完成或停止后再启动四工厂")
            return True

    print("  ✅ 无冲突的 Quality Session")
    return True

def check_scripts():
    """检查必需的脚本文件"""
    print("\n[3] 脚本文件检查")
    print("-" * 80)

    required = [
        "scripts/factories/run_strategy_factory.py",
        "scripts/factories/run_factor_mining_factory.py",
        "scripts/factories/run_incubation_factory.py",
        "scripts/factories/run_market_event_ingest.py",
        "scripts/factories/run_three_factories.py",
    ]

    all_exist = True
    for script in required:
        path = Path(script)
        if path.exists():
            print(f"  ✅ {script}")
        else:
            print(f"  ❌ {script} 不存在")
            all_exist = False

    return all_exist

def check_environment():
    """检查环境变量"""
    print("\n[4] 环境变量检查")
    print("-" * 80)

    import os

    # 检查语义契约
    semantic_contracts = [
        'STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED',
        'STRATEGY_FACTORY_PREDICTION_CONTRACT_ENABLED',
        'STRATEGY_FACTORY_CONFIDENCE_CONTRACT_ENABLED',
    ]

    for key in semantic_contracts:
        value = os.environ.get(key)
        if value == '1':
            print(f"  ✅ {key}=1")
        else:
            print(f"  ⚠️  {key} 未设置（建议设置为 1）")

    # 检查 API key（如果需要）
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if api_key:
        print(f"  ✅ ANTHROPIC_API_KEY 已设置")
    else:
        print(f"  ⚠️  ANTHROPIC_API_KEY 未设置（如果使用 LLM 生成因子，需要设置）")

    return True

def check_data_freshness():
    """检查数据新鲜度"""
    print("\n[5] 数据新鲜度检查")
    print("-" * 80)

    conn = sqlite3.connect(str(DB_PATH))

    # 检查最近的信号
    cursor = conn.execute("""
        SELECT MAX(signal_date) FROM strategy_signals
    """)
    latest_signal = cursor.fetchone()[0]

    if latest_signal:
        print(f"  ✅ 最新信号日期: {latest_signal}")
    else:
        print(f"  ⚠️  没有信号数据")

    # 检查前向收益
    cursor = conn.execute("SELECT COUNT(*) FROM signal_forward_returns")
    forward_count = cursor.fetchone()[0]

    print(f"  ✅ 前向收益记录: {forward_count}")

    conn.close()
    return True

def main():
    print("="*80)
    print("四工厂启动前置条件检查")
    print("="*80)

    checks = [
        ("数据库", check_database),
        ("Quality Session", check_quality_session),
        ("脚本文件", check_scripts),
        ("环境变量", check_environment),
        ("数据新鲜度", check_data_freshness),
    ]

    all_passed = True
    for name, check_func in checks:
        try:
            passed = check_func()
            if not passed:
                all_passed = False
        except Exception as e:
            print(f"\n  ❌ {name} 检查失败: {e}")
            all_passed = False

    # 总结
    print("\n" + "="*80)
    print("检查结果总结")
    print("="*80)

    if all_passed:
        print("\n  ✅ 所有前置条件满足，可以启动四工厂")
        print("\n  启动命令:")
        print("    python scripts/factories/run_three_factories.py")
        print("\n  建议:")
        print("    1. 如果 Quality Session 正在运行，建议先停止")
        print("    2. 四工厂会持续运行，建议在后台或 screen/tmux 中启动")
        print("    3. 启动后监控日志: logs/three_factories/")
        return 0
    else:
        print("\n  ❌ 部分前置条件不满足")
        print("\n  请先解决上述问题，然后重新检查")
        return 1

if __name__ == '__main__':
    sys.exit(main())
