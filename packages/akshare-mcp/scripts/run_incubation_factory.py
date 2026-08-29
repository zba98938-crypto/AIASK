#!/usr/bin/env python3
"""孵化工厂独立运行入口。

孵化工厂的两大核心使命：
1. 审查策略工厂生成的策略的质量与准确性
2. 体现策略工厂生成策略的命中率，从而体现 AI 生成交易策略的胜率

用法：
    # 单次运行（适合手动触发或 cron）
    python scripts/run_incubation_factory.py

    # 守护进程模式（每日 18:30 自动运行）
    python scripts/run_incubation_factory.py --daemon

    # 干跑模式（不写入数据库，仅验证逻辑）
    python scripts/run_incubation_factory.py --dry-run

    # 自定义运行时间
    python scripts/run_incubation_factory.py --daemon --run-time 19:00

    # 查看状态
    python scripts/run_incubation_factory.py --status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import time as dt_time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
STRATEGY_FACTORY_SRC = PROJECT_ROOT / "packages" / "strategy-factory" / "src"
if str(STRATEGY_FACTORY_SRC) not in sys.path:
    sys.path.insert(0, str(STRATEGY_FACTORY_SRC))

from strategy_factory.runtime_bootstrap import ensure_factory_runtime

ensure_factory_runtime(
    project_root=PROJECT_ROOT,
    script_path=Path(__file__).resolve(),
    argv=sys.argv[1:],
    editable_packages=(
        "packages/strategy-factory",
        "packages/aiask-quant-core",
        "packages/akshare-mcp",
    ),
)


def _ensure_src_path() -> None:
    """确保 src 目录在 Python 路径中。"""
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _load_project_env() -> None:
    _ensure_src_path()
    try:
        from akshare_mcp.env_loader import load_mcp_env

        load_mcp_env(override=False)
    except Exception:
        pass


def _setup_logging(verbose: bool = False) -> None:
    """配置日志。"""
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO
    handlers = [
        logging.FileHandler(log_dir / "incubation_factory.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    # 降低第三方库日志级别
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _parse_time(value: str) -> dt_time:
    """解析 HH:MM 格式的时间。"""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(f"无效的时间格式: {value}，应为 HH:MM")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        return dt_time(hour, minute)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError(f"无效的时间格式: {value}") from exc


async def _run_once(args: argparse.Namespace) -> None:
    """单次运行。"""
    _ensure_src_path()
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.incubation import build_incubation_runtime

    ensure_default_runtime_services()
    runtime = build_incubation_runtime()

    result = await runtime.run_once(
        trigger="manual",
        dry_run=args.dry_run,
    )

    # 输出结果
    print("\n" + "=" * 60)
    print("孵化工厂运行结果")
    print("=" * 60)

    status = result.get("status", "unknown")
    print(f"状态: {status}")
    print(f"耗时: {result.get('elapsed_seconds', 0):.1f}s")

    if status == "completed":
        intake = result.get("intake", {})
        verification = result.get("verification", {})
        pipeline = result.get("pipeline", {})
        report = result.get("report", {})

        print(f"\n接纳新策略: {intake.get('accepted', 0)}")
        print(f"验证策略数: {verification.get('verified', 0)}")
        print(f"指标已记录: {verification.get('metrics_recorded', 0)}")
        print(f"自动晋升: {pipeline.get('auto_promoted', 0)}")
        print(f"阶段分布: {pipeline.get('stage_counts', {})}")

        hit_rate = report.get("overall_hit_rate")
        skill_lcb = report.get("overall_skill_lcb")
        if hit_rate is not None:
            print(f"\n整体命中率: {hit_rate * 100:.2f}%")
        if skill_lcb is not None:
            print(f"技能下界: {skill_lcb:.4f}")
    elif status == "failed":
        print(f"错误: {result.get('error', 'unknown')}")

    print("=" * 60)

    if args.json:
        print("\n完整 JSON 结果:")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


async def _run_daemon(args: argparse.Namespace) -> None:
    """守护进程模式。"""
    _ensure_src_path()
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.incubation import build_incubation_runtime

    ensure_default_runtime_services()
    runtime = build_incubation_runtime()

    print(f"孵化工厂守护进程启动 (运行时间: {args.run_time}, dry_run: {args.dry_run})")
    print("按 Ctrl+C 停止")
    print("\n注意: daemon 模式暂不支持，请使用 cron 定时调用 run_once")
    print("示例: crontab -e")
    print("      30 18 * * * cd /path/to/aiask && python packages/akshare-mcp/scripts/run_incubation_factory.py")

    # TODO: 实现真正的 daemon 模式，需要在 runtime 层添加 scheduler
    # For now, just run once
    result = await runtime.run_once(
        trigger="daemon",
        dry_run=args.dry_run,
    )
    print(f"\n单次运行完成，状态: {result.get('status')}")


async def _show_status(args: argparse.Namespace) -> None:
    """显示运行状态。"""
    _ensure_src_path()
    from akshare_mcp.storage import close_db, get_db

    db = get_db()
    await db.initialize()

    print("孵化工厂状态")
    print("=" * 60)

    # 查询孵化中的策略数量
    try:
        incubating = await db.list_strategies("incubating", limit=500)
        print(f"孵化中策略数: {len(incubating)}")
    except Exception as exc:
        print(f"查询失败: {exc}")
        incubating = []

    # 查询最近的心跳
    if hasattr(db, "list_strategy_domain_events"):
        try:
            events = await db.list_strategy_domain_events(
                event_type="incubation_factory.heartbeat",
                limit=1,
            )
            if events:
                last_heartbeat = events[0]
                payload = dict(last_heartbeat.get("payload") or {})
                print(f"最近心跳: {payload.get('timestamp', 'unknown')}")
                print(f"累计运行: {payload.get('run_count', 0)} 次")
                print(f"累计错误: {payload.get('error_count', 0)} 次")
            else:
                print("最近心跳: 无记录（孵化工厂可能从未运行）")
        except Exception:
            print("最近心跳: 查询失败")

    # 查询最近的命中率报告
    if hasattr(db, "list_strategy_domain_events"):
        try:
            reports = await db.list_strategy_domain_events(
                event_type="incubation_factory.hit_rate_report_generated",
                limit=1,
            )
            if reports:
                report_payload = dict(reports[0].get("payload") or {})
                overall = dict(
                    (report_payload.get("hit_rate_dashboard") or {}).get("overall") or {}
                )
                print(f"\n最近命中率报告: {report_payload.get('report_date', 'unknown')}")
                print(f"  整体命中率: {float(overall.get('hit_rate') or 0) * 100:.2f}%")
                print(f"  技能下界: {overall.get('avg_skill_lcb', 0):.4f}")
                print(f"  信号总数: {overall.get('total_signals', 0)}")
            else:
                print("\n最近命中率报告: 无记录")
        except Exception:
            print("\n最近命中率报告: 查询失败")

    await close_db()
    print("=" * 60)


def main() -> None:
    """主入口。"""
    parser = argparse.ArgumentParser(
        description="孵化工厂独立运行",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scripts/run_incubation_factory.py              # 单次运行
  python scripts/run_incubation_factory.py --daemon     # 守护进程
  python scripts/run_incubation_factory.py --dry-run    # 干跑模式
  python scripts/run_incubation_factory.py --status     # 查看状态
        """,
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="守护进程模式（每日定时运行）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="干跑模式（不写入数据库）",
    )
    parser.add_argument(
        "--run-time",
        default="18:30",
        help="每日运行时间，格式 HH:MM（默认 18:30）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="显示孵化工厂运行状态",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="输出完整 JSON 结果",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细日志输出",
    )

    args = parser.parse_args()
    _load_project_env()
    _setup_logging(verbose=args.verbose)

    if args.status:
        asyncio.run(_show_status(args))
    elif args.daemon:
        asyncio.run(_run_daemon(args))
    else:
        asyncio.run(_run_once(args))


if __name__ == "__main__":
    main()
