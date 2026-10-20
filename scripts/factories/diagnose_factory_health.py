#!/usr/bin/env python3
"""
策略工厂健康诊断工具

自动执行 docs/factory-architecture/06-运行与诊断手册.md 中的诊断流程。

Usage:
    uv run python scripts/factories/diagnose_factory_health.py
    uv run python scripts/factories/diagnose_factory_health.py --verbose
    uv run python scripts/factories/diagnose_factory_health.py --output report.json
"""

import argparse
import json
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

# 添加项目路径
ROOT = Path(__file__).parent.parent.parent
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(ROOT / "packages" / "aiask-quant-core" / "src"))
sys.path.insert(0, str(ROOT / "packages" / "akshare-mcp" / "src"))

from _integration_paths import default_sqlite_path
import logging

logger = logging.getLogger(__name__)

try:
    from aiask_quant_core.storage.sqlite import get_db
    from aiask_quant_core.strategy_lifecycle_ledger import StrategyLifecycleLedger, BusinessLifecycleStage
    from aiask_quant_core.config import get_settings
except ImportError:
    get_db = None
    StrategyLifecycleLedger = None
    BusinessLifecycleStage = None
    get_settings = None


def _as_instant(value) -> Optional[datetime]:
    """Parse a stored timestamp so two of them can be ordered.

    Comparing the raw strings does not order them. `paper_trades.trade_time` is
    written as `2026-10-18T00:00:00+00:00` while the audit snapshot's `updated_at`
    is `2026-10-18 08:17:14` — they differ at index 10, where `T` outranks a space,
    so a trade from midnight sorts *after* a verdict from eight in the morning and
    every same-day pair reads as stale. Both formats parse with `fromisoformat`
    once an offset is present, and the offset is dropped here because the
    comparison is between two instants already stored in the same frame.
    """

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


class FactoryHealthDiagnostics:
    """策略工厂健康诊断器"""

    def __init__(self, db_path: str | None = None, verbose: bool = False):
        self.verbose = verbose
        # 优先级: 1. 命令行参数  2. 配置中心  3. 默认路径
        if db_path:
            self.db_path = db_path
        elif get_settings:
            try:
                settings = get_settings()
                self.db_path = settings.sqlite_path
            except Exception:
                # 配置中心不可用时降级到默认路径
                self.db_path = str(default_sqlite_path())
        else:
            self.db_path = str(default_sqlite_path())
        self.conn = None
        self.ledger = None  # StrategyLifecycleLedger 实例
        self.results = {
            "timestamp": datetime.now().isoformat(),
            "checks": [],
            "summary": {
                "total": 0,
                "passed": 0,
                "warning": 0,
                "failed": 0,
                "blocked": 0,
            },
            "overall_status": "unknown",
        }

    def _connect_db(self) -> bool:
        """连接数据库"""
        try:
            if get_db:
                import asyncio
                db = get_db()
                asyncio.run(db.initialize())
                self.conn = db.connection
                # 初始化 StrategyLifecycleLedger
                if StrategyLifecycleLedger:
                    self.ledger = StrategyLifecycleLedger(self.conn)
            else:
                self.conn = sqlite3.connect(self.db_path)
                self.conn.row_factory = sqlite3.Row
                if StrategyLifecycleLedger:
                    self.ledger = StrategyLifecycleLedger(self.conn)
            return True
        except Exception as e:
            self._add_check(
                "database_connection",
                "blocked",
                f"无法连接数据库: {e}",
                {"error": str(e)},
            )
            return False

    def _add_check(
        self, name: str, status: str, message: str, details: dict[str, Any] | None = None
    ):
        """添加检查结果"""
        check = {
            "name": name,
            "status": status,
            "message": message,
            "details": details or {},
        }
        self.results["checks"].append(check)
        self.results["summary"]["total"] += 1
        self.results["summary"][status] += 1

        # 打印结果（兼容 Windows 控制台）
        icons = {
            "passed": "[OK]",
            "warning": "[WARN]",
            "failed": "[FAIL]",
            "blocked": "[BLOCK]",
        }
        colors = {
            "passed": "\033[92m",  # 绿色
            "warning": "\033[93m",  # 黄色
            "failed": "\033[91m",  # 红色
            "blocked": "\033[95m",  # 紫色
        }
        reset = "\033[0m"

        icon = icons.get(status, "?")
        color = colors.get(status, "")

        try:
            print(f"{color}{icon}{reset} {message}")
        except UnicodeEncodeError:
            # Windows 控制台编码问题，使用纯 ASCII
            print(f"{icon} {message}")

        if self.verbose and details:
            for key, value in details.items():
                print(f"    {key}: {value}")

    def _query_one(self, sql: str, params: tuple = ()) -> dict | None:
        """执行查询并返回单行结果"""
        try:
            cursor = self.conn.execute(sql, params)
            row = cursor.fetchone()
            return dict(row) if row else None
        except Exception as e:
            if self.verbose:
                print(f"    查询失败: {e}")
                print(f"    SQL: {sql}")
            return None

    def _query_all(self, sql: str, params: tuple = ()) -> list[dict]:
        """执行查询并返回所有结果"""
        try:
            cursor = self.conn.execute(sql, params)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            if self.verbose:
                print(f"    查询失败: {e}")
                print(f"    SQL: {sql}")
            return []

    @staticmethod
    def _find_processes_via_ps(target_scripts: list[str]) -> list[dict[str, Any]]:
        """Portable process scan without a psutil dependency."""
        try:
            completed = subprocess.run(
                ["ps", "-axo", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if completed.returncode != 0:
            return []
        found: list[dict[str, Any]] = []
        for line in completed.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_text, _, command = line.partition(" ")
            if not pid_text.isdigit() or not command:
                continue
            if not any(script in command for script in target_scripts):
                continue
            parts = command.split()
            found.append(
                {
                    "pid": int(pid_text),
                    "name": Path(parts[0]).name if parts else "python",
                    "cmdline": " ".join(parts[:5]),
                }
            )
        return found

    def check_supervisor_processes(self):
        """检查 1: 四工厂 supervisor、sidecar 与 Agent 进程是否存活"""
        print("\n[1] 检查四工厂 supervisor / sidecar / Agent 进程...")

        target_scripts = [
            "run_three_factories.py",
            "run_strategy_factory.py",
            "run_factor_mining_factory.py",
            "run_incubation_factory.py",
            "run_market_event_ingest.py",
            "run_signal_tracker.py",
            "run_factor_scheduler.py",
            "run_sidecars.py",
            "aiask_agent.server",
        ]

        found_processes: list[dict[str, Any]] = []
        scan_backend = "ps"
        try:
            import psutil
        except ImportError:
            found_processes = self._find_processes_via_ps(target_scripts)
        else:
            scan_backend = "psutil"
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    cmdline = proc.info.get("cmdline", [])
                    if cmdline and any(script in " ".join(cmdline) for script in target_scripts):
                        found_processes.append(
                            {
                                "pid": proc.info["pid"],
                                "name": proc.info["name"],
                                "cmdline": " ".join(cmdline[:5]),
                            }
                        )
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        if not found_processes:
            self._add_check(
                "supervisor_processes",
                "warning",
                "未找到四工厂 supervisor、sidecar 或 Agent 进程",
                {
                    "hint": "运行 uv run python scripts/factories/run_three_factories.py",
                    "scan_backend": scan_backend,
                },
            )
        else:
            self._add_check(
                "supervisor_processes",
                "passed",
                f"找到 {len(found_processes)} 个工厂/sidecar/Agent 相关进程",
                {
                    "processes": found_processes,
                    "scan_backend": scan_backend,
                },
            )

    def check_agent_http(self):
        """检查 1b: Agent HTTP 是否真的健康（进程在 != 服务可用）"""
        print("\n[1b] 检查 Agent HTTP...")
        url = "http://127.0.0.1:8765/health"
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except (OSError, urllib.error.URLError, ValueError) as exc:
            self._add_check(
                "agent_http",
                "warning",
                f"Agent HTTP 不可达: {url}",
                {"error": f"{type(exc).__name__}: {exc}"},
            )
            return
        if str(payload.get("status") or "").lower() == "ok":
            self._add_check(
                "agent_http",
                "passed",
                f"Agent HTTP 正常: {payload.get('service') or 'aiask-agent'}",
                {"url": url},
            )
        else:
            self._add_check(
                "agent_http",
                "warning",
                f"Agent HTTP 返回非 ok: {payload}",
                {"url": url},
            )

    def check_signal_tracker_recent_run(self):
        """检查 2: SignalTracker 是否近期运行"""
        print("\n[2] 检查 SignalTracker sidecar...")

        # 检查 strategy_signals 表最新记录
        result = self._query_one(
            """
            SELECT
                MAX(signal_date) as last_signal_date,
                COUNT(*) as total_signals
            FROM strategy_signals
            """
        )

        if not result or not result["last_signal_date"]:
            self._add_check(
                "signal_tracker_recent_run",
                "blocked",
                "strategy_signals 表为空，SignalTracker 可能从未运行",
                {"hint": "运行 uv run python scripts/factories/run_signal_tracker.py --once"},
            )
            return

        last_signal_date = datetime.fromisoformat(result["last_signal_date"])
        days_ago = (datetime.now() - last_signal_date).days

        if days_ago > 2:
            self._add_check(
                "signal_tracker_recent_run",
                "failed",
                f"SignalTracker 最后运行时间：{days_ago} 天前（超过 2 天）",
                {
                    "last_signal_date": result["last_signal_date"],
                    "total_signals": result["total_signals"],
                },
            )
        elif days_ago > 1:
            self._add_check(
                "signal_tracker_recent_run",
                "warning",
                f"SignalTracker 最后运行时间：{days_ago} 天前（超过 1 天）",
                {
                    "last_signal_date": result["last_signal_date"],
                    "total_signals": result["total_signals"],
                },
            )
        else:
            self._add_check(
                "signal_tracker_recent_run",
                "passed",
                f"SignalTracker 最近运行：{days_ago} 天前",
                {
                    "last_signal_date": result["last_signal_date"],
                    "total_signals": result["total_signals"],
                },
            )

    def check_signal_to_order_conversion(self):
        """检查 3: 非零信号是否转成 paper order"""
        print("\n[3] 检查信号到订单的转换...")

        # 只看买入信号：卖出信号在没有持仓时本来就不会产生订单，不是 backlog。
        result = self._query_one(
            """
            SELECT COUNT(*) as signal_only_backlog
            FROM (
                SELECT s.strategy_id
                FROM strategy_signals s
                LEFT JOIN paper_orders o ON o.strategy_id = s.strategy_id
                WHERE s.signal > 0
                GROUP BY s.strategy_id
                HAVING COUNT(o.id) = 0
            )
            """
        )

        signal_only_backlog = result["signal_only_backlog"] if result else 0

        # 查询总共有多少策略产生过买入信号
        total_with_signals = self._query_one(
            """
            SELECT COUNT(DISTINCT strategy_id) as count
            FROM strategy_signals
            WHERE signal > 0
            """
        )
        total = total_with_signals["count"] if total_with_signals else 0

        if total == 0:
            self._add_check(
                "signal_to_order_conversion",
                "warning",
                "没有策略产生过非零信号",
                {"hint": "检查 SignalTracker 和策略运行器"},
            )
        elif signal_only_backlog == 0:
            self._add_check(
                "signal_to_order_conversion",
                "passed",
                f"所有产生买入信号的策略 ({total}) 都有 paper order",
                {"total_with_buy_signals": total},
            )
        elif signal_only_backlog / total > 0.5:
            self._add_check(
                "signal_to_order_conversion",
                "failed",
                f"entry-signal backlog: {signal_only_backlog}/{total} 策略",
                {
                    "signal_only_backlog": signal_only_backlog,
                    "total_with_buy_signals": total,
                    "hint": "检查 execution universe、price、account、shares 规则",
                },
            )
        else:
            self._add_check(
                "signal_to_order_conversion",
                "warning",
                f"entry-signal backlog: {signal_only_backlog}/{total} 策略",
                {
                    "signal_only_backlog": signal_only_backlog,
                    "total_with_buy_signals": total,
                },
            )

    def check_order_to_trade_conversion(self):
        """检查 4: paper order 是否转成 trade"""
        print("\n[4] 检查订单到成交的转换...")

        orders_stats = self._query_one(
            """
            SELECT
                COUNT(*) as total_orders,
                SUM(CASE WHEN status = 'filled' THEN 1 ELSE 0 END) as filled_orders
            FROM paper_orders
            """
        )

        trades_count = self._query_one("SELECT COUNT(*) as count FROM paper_trades")

        total_orders = orders_stats["total_orders"] if orders_stats else 0
        filled_orders = orders_stats["filled_orders"] if orders_stats else 0
        trades = trades_count["count"] if trades_count else 0

        if total_orders == 0:
            self._add_check(
                "order_to_trade_conversion",
                "warning",
                "没有 paper order 记录",
                {"hint": "先检查信号到订单的转换"},
            )
        elif trades == 0:
            self._add_check(
                "order_to_trade_conversion",
                "blocked",
                f"有 {total_orders} 个订单但没有成交记录",
                {"hint": "检查 settlement 是否运行"},
            )
        elif trades < filled_orders:
            self._add_check(
                "order_to_trade_conversion",
                "warning",
                f"订单成交率：{trades}/{filled_orders} (filled 订单)",
                {
                    "total_orders": total_orders,
                    "filled_orders": filled_orders,
                    "trades": trades,
                },
            )
        else:
            self._add_check(
                "order_to_trade_conversion",
                "passed",
                f"paper orders 正常转成 trades: {trades} 笔成交",
                {
                    "total_orders": total_orders,
                    "filled_orders": filled_orders,
                    "trades": trades,
                },
            )

    def check_position_status(self):
        """检查 5: 持仓状态分布"""
        print("\n[5] 检查持仓状态...")

        positions = self._query_all(
            """
            SELECT status, COUNT(*) as count
            FROM strategy_trade_positions
            GROUP BY status
            """
        )

        if not positions:
            self._add_check(
                "position_status",
                "warning",
                "没有 strategy_trade_positions 记录",
                {"hint": "先检查 paper trades 和 settlement"},
            )
            return

        position_stats = {row["status"]: row["count"] for row in positions}
        open_count = position_stats.get("open", 0)
        closed_count = position_stats.get("closed", 0)
        total = open_count + closed_count

        if total == 0:
            self._add_check("position_status", "warning", "没有持仓记录", {})
        elif closed_count == 0:
            self._add_check(
                "position_status",
                "failed",
                f"所有持仓均为 open ({open_count})，缺少 closed round-trip",
                {
                    "open": open_count,
                    "closed": closed_count,
                    "hint": "检查 stale close policy 和退出信号",
                },
            )
        elif open_count / total > 0.8:
            self._add_check(
                "position_status",
                "warning",
                f"持仓状态：{open_count} open / {closed_count} closed ({open_count/total*100:.0f}% open)",
                {
                    "open": open_count,
                    "closed": closed_count,
                    "hint": "open 持仓占比过高，检查 aging/exit policy",
                },
            )
        else:
            self._add_check(
                "position_status",
                "passed",
                f"持仓状态：{open_count} open / {closed_count} closed",
                {"open": open_count, "closed": closed_count},
            )

    def check_forward_returns(self):
        """检查 6: 前向收益证据"""
        print("\n[6] 检查前向收益证据...")

        forward_returns_count = self._query_one(
            "SELECT COUNT(*) as count FROM signal_forward_returns"
        )
        count = forward_returns_count["count"] if forward_returns_count else 0

        # 同时检查 incubation metrics。总行数与“有有效样本”的行数是
        # 两个不同事实；只报后者会让 1,383 行看起来像 0 行。
        metrics_total_count = self._query_one(
            "SELECT COUNT(*) as count FROM strategy_incubation_metrics"
        )
        metrics_total = metrics_total_count["count"] if metrics_total_count else 0
        metrics_count = self._query_one(
            """
            SELECT COUNT(*) as count
            FROM strategy_incubation_metrics
            WHERE COALESCE(effective_n_5d, 0) > 0
            """
        )
        metrics = metrics_count["count"] if metrics_count else 0

        if count == 0 and metrics_total == 0:
            self._add_check(
                "forward_returns",
                "blocked",
                "没有前向收益证据（signal_forward_returns 和 incubation_metrics 均为空）",
                {"hint": "检查 SignalTracker Phase B 和 Incubation Phase 3"},
            )
        elif count > 0:
            self._add_check(
                "forward_returns",
                "passed",
                (
                    f"前向收益证据：{count} 条 signal_forward_returns，"
                    f"{metrics_total} 条 incubation_metrics（{metrics} 条有有效样本）"
                ),
                {
                    "signal_forward_returns": count,
                    "incubation_metrics": metrics,
                    "incubation_metrics_total": metrics_total,
                },
            )
        else:
            self._add_check(
                "forward_returns",
                "warning",
                (
                    f"只有 incubation_metrics ({metrics_total} 条，"
                    f"{metrics} 条有有效样本)，缺少原始 signal_forward_returns"
                ),
                {
                    "incubation_metrics": metrics,
                    "incubation_metrics_total": metrics_total,
                },
            )

    def check_execution_audit_gate(self):
        """检查 7: execution audit gate 状态

        `strategy_execution_audit_snapshots` used to be counted with `GROUP BY
        verdict_status`, which answers "what verdicts are on file" — not "is this
        strategy currently audited". Snapshots are written on a lifecycle
        transition and never swept, so a strategy that has traded since its last
        transition keeps a months-old verdict and one that never transitioned has
        no row at all. Counting rows therefore reported `missing` for 283
        strategies when 16 of the 36 with trades had never been audited and 11 more
        held a July verdict.

        The three cases are separated here because they need different actions, and
        the strategy's own trade history decides which is which — no age threshold
        is invented: a verdict is **stale** when evidence exists that it never saw.
        """

        print("\n[7] 检查 execution audit gate...")

        snapshots = {
            str(row["strategy_id"]): row
            for row in self._query_all(
                "SELECT strategy_id, verdict_status, updated_at "
                "FROM strategy_execution_audit_snapshots"
            )
        }
        newest_trade = {
            str(row["strategy_id"]): _as_instant(row["newest"])
            for row in self._query_all(
                "SELECT strategy_id, MAX(trade_time) AS newest FROM paper_trades GROUP BY strategy_id"
            )
        }

        # The gate's subject is every strategy with execution evidence, plus any it
        # already holds a verdict for — a verdict with no evidence is worth seeing.
        universe = sorted(set(snapshots) | set(newest_trade))
        fresh: dict[str, int] = {}
        not_audited: list[str] = []
        stale: list[str] = []

        for strategy_id in universe:
            row = snapshots.get(strategy_id)
            if row is None:
                not_audited.append(strategy_id)
                continue
            trade_at = newest_trade.get(strategy_id)
            verdict_at = _as_instant(row.get("updated_at"))
            if trade_at and verdict_at and trade_at > verdict_at:
                stale.append(strategy_id)
                continue
            status = str(row.get("verdict_status") or "missing")
            fresh[status] = fresh.get(status, 0) + 1

        details = {
            "strategies_considered": len(universe),
            "with_execution_evidence": len(newest_trade),
            "fresh_verdicts": fresh,
            "not_audited": len(not_audited),
            "stale_audit": len(stale),
            "stale_or_missing_sample": (not_audited + stale)[:5],
        }

        if not universe:
            self._add_check(
                "execution_audit_gate", "blocked", "没有 execution audit snapshots",
                {**details, "hint": "检查 Incubation Phase 3f 是否运行"},
            )
            return

        passing = fresh.get("passed", 0)
        reasons: list[str] = []
        if not_audited:
            reasons.append(f"{len(not_audited)} 个策略从未被审计")
        if stale:
            reasons.append(f"{len(stale)} 个策略的判决早于其最新成交")
        if fresh.get("missing"):
            reasons.append(f"{fresh['missing']} 个策略审计结论为证据不足")

        if not passing:
            self._add_check(
                "execution_audit_gate",
                "blocked",
                "无任何策略通过 execution audit：" + "；".join(reasons or ["没有有效判决"]),
                {
                    **details,
                    # The old message said every audit "was missing", which was true
                    # of a subset and hid the two cases that are about coverage
                    # rather than about evidence.
                    "hint": "从未审计与判决定 missing 是两件事；前者需要审计覆盖，后者需要执行证据",
                },
            )
        elif reasons:
            self._add_check(
                "execution_audit_gate", "warning",
                f"{passing} 个策略通过；另有 " + "；".join(reasons), details,
            )
        else:
            self._add_check(
                "execution_audit_gate", "ok",
                f"所有 {len(universe)} 个策略都有当期判决，{passing} 个通过", details,
            )

    def check_strategy_lifecycle_state(self):
        """检查 8: 策略生命周期状态分布（使用 StrategyLifecycleLedger）"""
        print("\n[8] 检查策略生命周期状态...")

        # 先查询所有策略
        strategies = self._query_all("SELECT id FROM strategies")

        if not strategies:
            self._add_check(
                "strategy_lifecycle_state",
                "warning",
                "strategies 表为空",
                {"hint": "Strategy Factory 可能未生成任何策略"},
            )
            return

        # 如果有 StrategyLifecycleLedger，使用统一账本
        if self.ledger:
            strategy_ids = [row["id"] for row in strategies]
            snapshots = self.ledger.batch_get_snapshots(strategy_ids)

            # 统计物理状态
            physical_stats = {}
            for snapshot in snapshots.values():
                status = snapshot.physical_status
                physical_stats[status] = physical_stats.get(status, 0) + 1

            # 统计业务状态
            business_stats = {}
            for snapshot in snapshots.values():
                stage = snapshot.business_stage.value
                business_stats[stage] = business_stats.get(stage, 0) + 1

            total = len(snapshots)
            listed = physical_stats.get("listed", 0)
            incubating = physical_stats.get("incubating", 0)

            details = {
                "total": total,
                "physical_stats": physical_stats,
                "business_stats": business_stats,
                "ledger_enabled": True,
            }

            if listed > 0:
                self._add_check(
                    "strategy_lifecycle_state",
                    "passed",
                    f"策略状态：{listed} listed, {incubating} incubating, 共 {total} 个",
                    details,
                )
            elif incubating > 0:
                self._add_check(
                    "strategy_lifecycle_state",
                    "warning",
                    f"有 {incubating} 个 incubating 策略，但尚无 listed",
                    details,
                )
            else:
                self._add_check(
                    "strategy_lifecycle_state",
                    "warning",
                    f"共 {total} 个策略，但无 incubating 或 listed",
                    details,
                )
        else:
            # 降级：没有 StrategyLifecycleLedger，使用传统查询
            strategies = self._query_all(
                """
                SELECT status, COUNT(*) as count
                FROM strategies
                GROUP BY status
                """
            )

            stats = {row["status"]: row["count"] for row in strategies}
            details = {"ledger_enabled": False, **dict(stats)}

            incubating = stats.get("incubating", 0)
            listed = stats.get("listed", 0)
            total = sum(stats.values())

            if listed > 0:
                self._add_check(
                    "strategy_lifecycle_state",
                    "passed",
                    f"策略状态分布：{listed} listed, {incubating} incubating, 共 {total} 个策略",
                    details,
                )
            elif incubating > 0:
                self._add_check(
                    "strategy_lifecycle_state",
                    "warning",
                    f"有 {incubating} 个 incubating 策略，但尚无 listed",
                    details,
                )
            else:
                self._add_check(
                    "strategy_lifecycle_state",
                    "warning",
                    f"共 {total} 个策略，但无 incubating 或 listed",
                    details,
                )

    def determine_overall_status(self):
        """确定整体健康状态"""
        summary = self.results["summary"]

        if summary["blocked"] > 0:
            return "blocked"
        elif summary["failed"] > 0:
            return "degraded"
        elif summary["warning"] > 2:
            return "pending_evidence"
        elif summary["passed"] == summary["total"]:
            return "healthy"
        else:
            return "pending_evidence"

    def run_diagnostics(self):
        """运行所有诊断"""
        print("=" * 80)
        print("策略工厂健康诊断")
        print("=" * 80)

        if not self._connect_db():
            return False

        try:
            self.check_supervisor_processes()
            self.check_agent_http()
            self.check_signal_tracker_recent_run()
            self.check_signal_to_order_conversion()
            self.check_order_to_trade_conversion()
            self.check_position_status()
            self.check_forward_returns()
            self.check_execution_audit_gate()
            self.check_strategy_lifecycle_state()
        finally:
            if self.conn:
                self.conn.close()

        self.results["overall_status"] = self.determine_overall_status()

        # 打印总结
        print("\n" + "=" * 80)
        print("诊断总结")
        print("=" * 80)
        summary = self.results["summary"]
        print(f"总检查项: {summary['total']}")
        print(f"[OK] 通过: {summary['passed']}")
        print(f"[WARN] 警告: {summary['warning']}")
        print(f"[FAIL] 失败: {summary['failed']}")
        print(f"[BLOCK] 阻塞: {summary['blocked']}")
        print(f"\n整体状态: {self.results['overall_status'].upper()}")

        return True

    def save_report(self, output_path: str):
        """保存诊断报告"""
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(self.results, f, indent=2, ensure_ascii=False)
        print(f"\n诊断报告已保存到: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="策略工厂健康诊断工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 运行完整诊断
  uv run python scripts/factories/diagnose_factory_health.py

  # 详细模式
  uv run python scripts/factories/diagnose_factory_health.py --verbose

  # 保存报告到 JSON
  uv run python scripts/factories/diagnose_factory_health.py --output report.json

参考规范：
  docs/factory-architecture/06-运行与诊断手册.md
        """,
    )
    parser.add_argument("--db", help="数据库路径（默认：AIASK_SQLITE_PATH / AKSHARE_MCP_SQLITE_PATH / AIASK_RUNTIME_HOME）")
    parser.add_argument("--verbose", action="store_true", help="显示详细信息")
    parser.add_argument("--output", help="保存诊断报告到 JSON 文件")

    args = parser.parse_args()

    diagnostics = FactoryHealthDiagnostics(db_path=args.db, verbose=args.verbose)
    success = diagnostics.run_diagnostics()

    if args.output:
        diagnostics.save_report(args.output)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
