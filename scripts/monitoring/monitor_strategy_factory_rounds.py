#!/usr/bin/env python3
"""
策略工厂四工厂逐轮监控脚本
每轮执行完成后自动记录到MD文档
"""
import sqlite3
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import os
import sys

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _integration_paths import default_sqlite_path, ensure_runtime_dirs, runtime_home

def _aiask_root() -> Path:
    override = os.getenv("AIASK_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    current = Path(__file__).resolve()
    for parent in (current.parent, *current.parents):
        if (parent / "packages").is_dir() and (parent / "scripts").is_dir():
            return parent
    return Path.cwd()


def _aiask_db_path() -> str:
    override = os.getenv("AIASK_SQLITE_PATH") or os.getenv("AKSHARE_MCP_SQLITE_PATH")
    if override:
        return str(Path(override).expanduser().resolve())
    return str(default_sqlite_path())

# 配置
DB_PATH = _aiask_db_path()
REPORT_PATH = str((runtime_home() / "reports" / "strategy_factory_rounds_v10_20260613.md").resolve())
REPORT_PATH = "策略工厂实时运行记录-v10-20260613.md"
CHECK_INTERVAL = 60  # 每60秒检查一次

REPORT_PATH = str((runtime_home() / "reports" / "strategy_factory_rounds_v10_20260613.md").resolve())


class StrategyFactoryMonitor:
    def __init__(self, db_path: str, report_path: str):
        self.db_path = db_path
        self.report_path = report_path
        self.last_run_id = None
        self.round_number = 0

    def connect_db(self):
        """连接数据库"""
        return sqlite3.connect(self.db_path)

    def get_latest_run(self) -> Optional[Dict]:
        """获取最新的工厂运行记录"""
        conn = self.connect_db()
        cur = conn.cursor()

        cur.execute("""
            SELECT run_id, started_at, completed_at, status, summary
            FROM strategy_factory_runs
            ORDER BY started_at DESC
            LIMIT 1
        """)

        row = cur.fetchone()
        conn.close()

        if not row:
            return None

        summary = json.loads(row[4]) if row[4] else {}

        return {
            'run_id': row[0],
            'started_at': row[1],
            'completed_at': row[2],
            'status': row[3],
            'summary': summary
        }

    def get_round_details(self, run_id: str) -> Dict:
        """获取单轮详细数据"""
        conn = self.connect_db()
        cur = conn.cursor()

        # 策略状态统计
        cur.execute("""
            SELECT status, COUNT(*) as cnt
            FROM strategies
            GROUP BY status
        """)
        status_dist = {row[0]: row[1] for row in cur.fetchall()}

        # 交易预测命中率
        cur.execute("""
            SELECT
                COUNT(*) as total,
                SUM(CASE WHEN json_extract(outcome_json, '$.direction_hit') = 1 THEN 1 ELSE 0 END) as hits,
                ROUND(AVG(trade_prediction_score), 3) as avg_score
            FROM strategy_trade_prediction_outcomes
            WHERE score_status = 'ok'
        """)
        prediction_row = cur.fetchone()
        hit_rate = (prediction_row[1] / prediction_row[0] * 100) if prediction_row[0] > 0 else 0

        # 孵化池统计
        cur.execute("""
            SELECT COUNT(*) FROM strategies WHERE status = 'incubating'
        """)
        incubating_count = cur.fetchone()[0]

        conn.close()

        return {
            'status_distribution': status_dist,
            'prediction_total': prediction_row[0] if prediction_row else 0,
            'prediction_hits': prediction_row[1] if prediction_row else 0,
            'hit_rate': hit_rate,
            'avg_score': prediction_row[2] if prediction_row else 0,
            'incubating_count': incubating_count
        }

    def format_round_record(self, run_data: Dict, details: Dict) -> str:
        """格式化单轮记录"""
        summary = run_data['summary']
        started = run_data['started_at'][:19] if run_data['started_at'] else 'N/A'
        completed = run_data['completed_at'][:19] if run_data['completed_at'] else '运行中'

        # 计算耗时
        if run_data['completed_at']:
            start_time = datetime.fromisoformat(run_data['started_at'].replace('+08:00', ''))
            end_time = datetime.fromisoformat(run_data['completed_at'].replace('+08:00', ''))
            elapsed = (end_time - start_time).total_seconds()
            elapsed_str = f"{int(elapsed)}秒 ({elapsed/60:.1f}分钟)"
        else:
            elapsed_str = "运行中..."

        # 问题标记
        issues = []
        if details['hit_rate'] < 35:
            issues.append("⚠️ 命中率偏低")
        if summary.get('gate_3_passed', 0) == 0:
            issues.append("⚠️ G3全部未通过")
        if details['incubating_count'] == 0:
            issues.append("⚠️ 无孵化策略")

        issues_str = " | ".join(issues) if issues else "[OK] 正常"

        record = f"""
### 第 {self.round_number} 轮
- **运行ID**: `{run_data['run_id'][:40]}...`
- **开始时间**: {started}
- **结束时间**: {completed}
- **耗时**: {elapsed_str}
- **状态**: {run_data['status']}

**工厂漏斗**:
- spawned: {summary.get('spawned', 'N/A')}
- submitted: {summary.get('submitted', 'N/A')}
- gate_3_passed: {summary.get('gate_3_passed', 'N/A')}

**质量指标**:
- 命中率: {details['hit_rate']:.1f}% ({details['prediction_hits']}/{details['prediction_total']})
- 平均分: {details['avg_score']:.3f}
- 孵化中: {details['incubating_count']}

**策略分布**:
- submitted: {details['status_distribution'].get('submitted', 0)}
- incubating: {details['status_distribution'].get('incubating', 0)}
- rejected: {details['status_distribution'].get('rejected', 0)}

**问题标记**: {issues_str}

---
"""
        return record

    def initialize_report(self):
        """初始化报告文件"""
        if Path(self.report_path).exists():
            return

        header = f"""# 策略工厂实时运行记录 (v10)

**会话ID**: full24h_v10_20260613
**启动时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**监控频率**: 每{CHECK_INTERVAL}秒检查一次
**自动更新**: 是

## 📊 累计统计

将在运行过程中自动更新...

## 🔄 逐轮记录

"""
        with open(self.report_path, 'w', encoding='utf-8') as f:
            f.write(header)

    def append_round(self, record: str):
        """追加单轮记录"""
        with open(self.report_path, 'a', encoding='utf-8') as f:
            f.write(record)

    def update_summary(self, all_runs: List[Dict]):
        """更新累计统计（重写文件前半部分）"""
        # 读取现有的逐轮记录
        rounds_content = ""
        if Path(self.report_path).exists():
            with open(self.report_path, 'r', encoding='utf-8') as f:
                content = f.read()
                if '## 🔄 逐轮记录' in content:
                    rounds_content = content.split('## 🔄 逐轮记录')[1]

        # 计算累计统计
        total_spawned = sum(r['summary'].get('spawned', 0) for r in all_runs)
        total_submitted = sum(r['summary'].get('submitted', 0) for r in all_runs)
        total_g3 = sum(r['summary'].get('gate_3_passed', 0) for r in all_runs)

        # 重写整个文件
        header = f"""# 策略工厂实时运行记录 (v10)

**会话ID**: full24h_v10_20260613
**启动时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**监控频率**: 每{CHECK_INTERVAL}秒检查一次
**自动更新**: 是
**累计轮数**: {len(all_runs)}

## 📊 累计统计

| 指标 | 数值 |
|------|-----:|
| 总轮数 | {len(all_runs)} |
| 累计spawned | {total_spawned} |
| 累计submitted | {total_submitted} |
| 累计G3通过 | {total_g3} |
| G3通过率 | {(total_g3/total_spawned*100 if total_spawned > 0 else 0):.1f}% |

**最后更新**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## 🔄 逐轮记录

{rounds_content}
"""

        with open(self.report_path, 'w', encoding='utf-8') as f:
            f.write(header)

    def monitor_loop(self):
        """主监控循环"""
        print(f"[START] 策略工厂监控启动")
        print(f"   数据库: {self.db_path}")
        print(f"   报告: {self.report_path}")
        print(f"   检查间隔: {CHECK_INTERVAL}秒")
        print()

        self.initialize_report()
        all_runs = []

        while True:
            try:
                latest_run = self.get_latest_run()

                if not latest_run:
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] 等待工厂运行...")
                    time.sleep(CHECK_INTERVAL)
                    continue

                run_id = latest_run['run_id']

                # 检测到新轮次
                if run_id != self.last_run_id:
                    # 记录已完成的轮次（包括 partial_infra）
                    if latest_run['status'] in ('success', 'partial_infra') and latest_run['completed_at']:
                        self.round_number += 1
                        self.last_run_id = run_id

                        print(f"\n[OK] 第 {self.round_number} 轮完成: {run_id[:40]}...")

                        # 获取详细数据
                        details = self.get_round_details(run_id)

                        # 格式化并追加记录
                        record = self.format_round_record(latest_run, details)
                        self.append_round(record)

                        # 更新汇总
                        all_runs.append(latest_run)
                        self.update_summary(all_runs)

                        print(f"   - spawned: {latest_run['summary'].get('spawned', 'N/A')}")
                        print(f"   - submitted: {latest_run['summary'].get('submitted', 'N/A')}")
                        print(f"   - G3通过: {latest_run['summary'].get('gate_3_passed', 'N/A')}")
                        print(f"   - 命中率: {details['hit_rate']:.1f}%")
                        print(f"   [WRITE] 记录已写入: {self.report_path}")
                    else:
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] 第 {self.round_number+1} 轮运行中... (status={latest_run['status']})")

                time.sleep(CHECK_INTERVAL)

            except KeyboardInterrupt:
                print("\n\n[STOP] 监控停止")
                break
            except Exception as e:
                print(f"\n[ERROR] 错误: {e}")
                time.sleep(CHECK_INTERVAL)

if __name__ == '__main__':
    ensure_runtime_dirs()
    Path(REPORT_PATH).parent.mkdir(parents=True, exist_ok=True)
    monitor = StrategyFactoryMonitor(DB_PATH, REPORT_PATH)
    monitor.monitor_loop()
