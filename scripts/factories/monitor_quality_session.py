#!/usr/bin/env python3
"""
Quality Session 监控仪表板

实时监控运行状态、关键指标和异常情况
"""
import json
import sys
from pathlib import Path
from datetime import datetime

def find_latest_session():
    """查找最新的 Quality Session"""
    sessions_dir = Path("logs/strategy_factory_quality_sessions")
    if not sessions_dir.exists():
        return None

    sessions = sorted(sessions_dir.glob("strategy_factory_quality_*"), key=lambda p: p.stat().st_mtime)
    return sessions[-1] if sessions else None

def parse_state(state_file):
    """解析 state.json"""
    if not state_file.exists():
        return None

    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return None

def parse_session_lock(lock_file):
    """解析 session.lock.json"""
    if not lock_file.exists():
        return None

    try:
        with open(lock_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return None

def get_latest_rounds(rounds_dir, limit=5):
    """获取最近的轮次"""
    if not rounds_dir.exists():
        return []

    rounds = sorted(rounds_dir.glob("round_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    results = []
    for round_file in rounds[:limit]:
        try:
            with open(round_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                results.append((round_file.stem, data))
        except:
            continue

    return results

def check_compensation_logic(log_file):
    """检查是否有补偿逻辑触发"""
    if not log_file.exists():
        return []

    issues = []
    try:
        with open(log_file, 'r', encoding='utf-8', errors='ignore') as f:
            for i, line in enumerate(f, 1):
                line_lower = line.lower()
                if 'compensation' in line_lower or 'backfill' in line_lower:
                    issues.append(f"Line {i}: {line.strip()[:100]}")
                if 'stale_paper_position_closure' in line_lower:
                    issues.append(f"Line {i}: [WARN] Stale position closure: {line.strip()[:80]}")
    except:
        pass

    return issues

def main():
    session_dir = find_latest_session()

    if not session_dir:
        print("未找到 Quality Session")
        return

    print("="*80)
    print(f"Quality Session 监控仪表板")
    print("="*80)
    print(f"\n会话目录: {session_dir.name}")
    print(f"启动时间: {datetime.fromtimestamp(session_dir.stat().st_mtime).strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. 会话锁信息
    lock_file = session_dir / "session.lock.json"
    lock_data = parse_session_lock(lock_file)

    if lock_data:
        print(f"\n[1] 会话状态")
        print("-" * 80)
        print(f"  Session ID: {lock_data.get('session_id', 'N/A')}")
        print(f"  启动时间: {lock_data.get('started_at', 'N/A')}")
        print(f"  预计运行: {lock_data.get('hours', 'N/A')} 小时")

        deadline = lock_data.get('deadline')
        if deadline:
            try:
                deadline_dt = datetime.fromisoformat(deadline.replace('Z', '+00:00'))
                now = datetime.now(deadline_dt.tzinfo)
                remaining = (deadline_dt - now).total_seconds() / 3600
                print(f"  剩余时间: {remaining:.1f} 小时")
            except:
                pass

    # 2. 状态统计
    state_file = session_dir / "state.json"
    state = parse_state(state_file)

    if state:
        print(f"\n[2] 运行统计")
        print("-" * 80)
        entries = state.get('entries', [])
        print(f"  完成轮次: {len(entries)}")

        if entries:
            latest = entries[-1]
            print(f"  最新轮次: {latest.get('round_no', 'N/A')}")
            print(f"  最新时间: {latest.get('timestamp', 'N/A')}")

    # 3. 最近轮次详情
    rounds_dir = session_dir / "rounds"
    recent_rounds = get_latest_rounds(rounds_dir, limit=3)

    if recent_rounds:
        print(f"\n[3] 最近 {len(recent_rounds)} 轮详情")
        print("-" * 80)

        for round_name, data in recent_rounds:
            print(f"\n  {round_name}:")

            # 候选生成
            submission = data.get('submission_factory', {})
            if submission:
                print(f"    候选生成: {submission.get('candidate_count', 0)} 个")
                print(f"    提交成功: {submission.get('submitted_count', 0)} 个")

            # 孵化统计
            incubation = data.get('incubation_factory', {})
            if incubation:
                print(f"    孵化处理: {incubation.get('processed_count', 0)} 个")
                print(f"    转正数量: {incubation.get('promoted_count', 0)} 个")

            # Signal Tracker
            signal = data.get('signal_tracker', {})
            if signal:
                print(f"    信号生成: {signal.get('signal_count', 0)} 个")

            # 检查异常
            if data.get('errors'):
                print(f"    [WARN] 错误: {len(data['errors'])} 个")

    # 4. 检查补偿逻辑
    log_file = session_dir / "session.log"
    compensation_issues = check_compensation_logic(log_file)

    print(f"\n[4] P0-P2 修复验证")
    print("-" * 80)

    if compensation_issues:
        print(f"  [WARN] 发现 {len(compensation_issues)} 个补偿逻辑相关日志:")
        for issue in compensation_issues[:5]:
            print(f"    {issue}")
        if len(compensation_issues) > 5:
            print(f"    ... 还有 {len(compensation_issues) - 5} 条")
    else:
        print("  [OK] 未发现补偿逻辑触发")

    # 5. 日志文件大小
    if log_file.exists():
        log_size = log_file.stat().st_size / 1024 / 1024
        print(f"\n[5] 日志文件")
        print("-" * 80)
        print(f"  大小: {log_size:.2f} MB")
        print(f"  路径: {log_file}")

    print("\n" + "="*80)
    print("监控完成")
    print("="*80)

    # 提示查看详细日志
    print(f"\n查看实时日志:")
    print(f"  tail -f {log_file}")
    print(f"\n查看最新轮次:")
    print(f"  cat {rounds_dir}/round_*.json | tail -100")

if __name__ == '__main__':
    main()
