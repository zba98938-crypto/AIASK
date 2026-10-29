#!/usr/bin/env python3
"""因子挖掘工厂运行脚本。

按调度策略自动执行因子搜索、进化优化、验证入池，
直到收到 SIGINT (Ctrl+C) 或 SIGTERM 信号才停止。

调度策略：
- 每日 18:30：常规挖掘周期（收盘数据就绪后）
- 每日 06:00：因子池维护（衰减检测 + 退役）
- 衰减警报时：紧急补充搜索（事件驱动）
- 每周日 02:00：深度进化优化（GP 大种群）

用法：
    python run_factor_mining_factory.py                  # 调度模式（按时间自动触发）
    python run_factor_mining_factory.py --once           # 立即执行一次挖掘周期
    python run_factor_mining_factory.py --maintenance    # 立即执行一次维护
    python run_factor_mining_factory.py --interval 7200  # 每 2 小时执行一次（忽略时间调度）
    python run_factor_mining_factory.py --status         # 查看工厂状态
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path


def _configure_stdio_utf8() -> None:
    """Force stdout/stderr to UTF-8 so Chinese + box-drawing chars render in
    Windows PowerShell / cmd (default codepage 936)."""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_factor_mining_factory.py:47: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
            continue
        except Exception as _swallowed:
            logger.warning("scripts/factories/run_factor_mining_factory.py:56: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            pass
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            try:
                setattr(
                    sys,
                    stream_name,
                    io.TextIOWrapper(
                        buffer,
                        encoding="utf-8",
                        errors="replace",
                        line_buffering=True,
                    ),
                )
            except Exception as _swallowed:
                logger.warning("scripts/factories/run_factor_mining_factory.py:71: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                continue


_configure_stdio_utf8()

# 项目路径
PROJECT_ROOT = Path(__file__).resolve().parents[2]
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
    distribution_names=(
        "strategy-factory",
        "aiask-quant-core",
        "akshare-mcp",
    ),
    uv_project="packages/agent",
)


def _bootstrap_local_package_paths() -> dict:
    """Provider-neutral bootstrap.

    Injects whichever packages are present rather than hardcoding a data
    provider, so this runner works with a provider, with only the standalone
    provider set, and in a standalone repository.
    """
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    from _runner_bootstrap import bootstrap_runner  # noqa: PLC0415

    return bootstrap_runner(Path(__file__), load_env=False)


BOOTSTRAP_REPORT = _bootstrap_local_package_paths()


def _get_factor_mining_runtime():
    from strategy_factory.runtime.default_bootstrap import ensure_default_runtime_services
    from strategy_factory.runtime.factor_mining import get_factor_mining_runtime

    ensure_default_runtime_services()
    return get_factor_mining_runtime()


def _load_dotenv():
    # AIASK_LOAD_PROJECT_ENV=0 (or the hermetic test-session marker) turns off
    # guessing the repo .env; same contract as akshare_mcp.env_loader.
    if str(os.getenv("AIASK_LOAD_PROJECT_ENV", "")).strip().lower() in {"0", "false", "no", "off"} or (
        "aiask_hermetic_test_session" in sys.modules
    ):
        return
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key:
                os.environ.setdefault(key, value)
    try:
        from strategy_factory.infrastructure.env_loader import load_strategy_llm_env

        load_strategy_llm_env(explicit_path=str(env_path))
    except Exception as _swallowed:
        logger.warning("scripts/factories/run_factor_mining_factory.py:149: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        pass


_load_dotenv()

# 确保关键配置
os.environ.setdefault("FACTOR_MINING_FACTORY_ENABLED", "1")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("factor_mining_factory_runner")


# ═══════════════════════════════════════════════════════════════════════════════
# 调度配置
# ═══════════════════════════════════════════════════════════════════════════════

MINING_TIME = dt_time(18, 30)       # 每日挖掘时间（收盘后）
MAINTENANCE_TIME = dt_time(6, 0)    # 每日维护时间
DEEP_SEARCH_DAY = 6                 # 周日 (0=周一, 6=周日)
DEEP_SEARCH_TIME = dt_time(2, 0)    # 深度搜索时间


class FactorMiningFactoryRunner:
    """因子挖掘工厂运行器。"""

    def __init__(
        self,
        *,
        mode: str = "schedule",       # schedule / interval / once / maintenance
        interval_sec: int = 7200,
        candidate_count: int = 30,
        evolution_generations: int = 5,
        engines: list[str] | None = None,
        codes: list[str] | None = None,
    ):
        self.mode = mode
        self.interval_sec = interval_sec
        self.candidate_count = candidate_count
        self.evolution_generations = evolution_generations
        self.engines = engines
        self.codes = codes
        self._running = True
        self._run_count = 0
        self._admitted_total = 0

    def _setup_signals(self):
        def _stop(signum, frame):
            logger.info("收到 %s，准备停止...", signal.Signals(signum).name)
            self._running = False
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    async def run(self):
        self._setup_signals()
        logger.info("═" * 60)
        logger.info("因子挖掘工厂启动")
        logger.info("  模式: %s", self.mode)
        logger.info("  引擎: %s", self.engines or "全部 (LLM+GP+MCTS+RL+Rule)")
        logger.info("  候选数: %d / 轮", self.candidate_count)
        logger.info("  进化代数: %d", self.evolution_generations)
        logger.info("  LLM: %s", os.getenv("FACTOR_LLM_MODEL", "未配置"))
        logger.info("═" * 60)

        if self.mode == "once":
            await self._run_mining_cycle("manual")
        elif self.mode == "maintenance":
            await self._run_maintenance()
        elif self.mode == "interval":
            await self._run_interval_loop()
        else:
            await self._run_schedule_loop()

        logger.info("═" * 60)
        logger.info("因子挖掘工厂停止 (运行=%d轮, 入池=%d个因子)", self._run_count, self._admitted_total)
        logger.info("═" * 60)

    # ─── 调度模式 ─────────────────────────────────────────────────────

    async def _run_schedule_loop(self):
        """按时间调度运行。"""
        while self._running:
            now = datetime.now()
            next_event, event_type = self._next_scheduled_event(now)
            wait_sec = (next_event - now).total_seconds()

            logger.info("下一事件: %s (%s), 等待 %.0f 秒",
                        event_type, next_event.strftime("%H:%M"), wait_sec)

            if not await self._interruptible_sleep(wait_sec):
                break

            if event_type == "mining":
                await self._run_mining_cycle("scheduled")
            elif event_type == "maintenance":
                await self._run_maintenance()
            elif event_type == "deep_search":
                await self._run_deep_search()

    def _next_scheduled_event(self, now: datetime) -> tuple[datetime, str]:
        """计算下一个调度事件。"""
        today = now.date()
        candidates = []

        # 今日挖掘
        mining_dt = datetime.combine(today, MINING_TIME)
        if mining_dt <= now:
            mining_dt += timedelta(days=1)
        candidates.append((mining_dt, "mining"))

        # 今日维护
        maint_dt = datetime.combine(today, MAINTENANCE_TIME)
        if maint_dt <= now:
            maint_dt += timedelta(days=1)
        candidates.append((maint_dt, "maintenance"))

        # 本周深度搜索
        days_until_sunday = (DEEP_SEARCH_DAY - now.weekday()) % 7
        if days_until_sunday == 0 and now.time() > DEEP_SEARCH_TIME:
            days_until_sunday = 7
        deep_dt = datetime.combine(today + timedelta(days=days_until_sunday), DEEP_SEARCH_TIME)
        candidates.append((deep_dt, "deep_search"))

        return min(candidates, key=lambda x: x[0])

    # ─── 间隔模式 ─────────────────────────────────────────────────────

    async def _run_interval_loop(self):
        """固定间隔运行。"""
        while self._running:
            await self._run_mining_cycle("interval")
            if not self._running:
                break
            logger.info("等待 %d 秒后下一轮...", self.interval_sec)
            if not await self._interruptible_sleep(self.interval_sec):
                break

    # ─── 执行动作 ─────────────────────────────────────────────────────

    async def _run_mining_cycle(self, trigger: str):
        """执行一次挖掘周期。"""
        self._run_count += 1
        logger.info("─" * 50)
        logger.info("第 %d 轮挖掘周期 [trigger=%s]", self._run_count, trigger)

        try:
            runtime = _get_factor_mining_runtime()

            result = await runtime.run_once(
                trigger=trigger,
                engines=self.engines,
                candidate_count=self.candidate_count,
                evolution_generations=self.evolution_generations,
                codes=self.codes,
            )

            if result.get("success"):
                admitted = result.get("admitted_count", 0)
                self._admitted_total += admitted
                logger.info(
                    "第 %d 轮完成 ✅ 原始=%d 进化=%d 验证=%d 入池=%d 池大小=%d",
                    self._run_count,
                    result.get("raw_candidate_count", 0),
                    result.get("evolved_count", 0),
                    result.get("validated_count", 0),
                    admitted,
                    result.get("pool_size", 0),
                )
                engines_used = result.get("engines_used", [])
                if engines_used:
                    logger.info("  引擎: %s", ", ".join(engines_used))
            else:
                logger.warning("第 %d 轮失败 ❌ %s", self._run_count, result.get("error", "unknown"))

        except Exception as exc:
            logger.error("第 %d 轮异常: %s", self._run_count, exc, exc_info=True)

    async def _run_maintenance(self):
        """执行维护任务。"""
        logger.info("─" * 50)
        logger.info("执行因子池维护...")

        try:
            runtime = _get_factor_mining_runtime()
            result = await runtime.run_maintenance()

            decay_report = result.get("decay_report", {})
            decaying = decay_report.get("decaying_count", 0)
            pool_size = result.get("pool_size", 0)

            logger.info("维护完成 ✅ 池大小=%d 衰减因子=%d", pool_size, decaying)

            if decaying > 0:
                alerts = decay_report.get("alerts", [])
                for alert in alerts[:5]:
                    logger.warning(
                        "  衰减: %s (rate=%.2f, severity=%s)",
                        alert.get("name", alert.get("factor_id")),
                        alert.get("decay_rate", 0),
                        alert.get("severity", "?"),
                    )
                # 衰减因子过多时触发补充搜索
                if decaying >= 3:
                    logger.info("衰减因子 >= 3，触发补充搜索...")
                    await self._run_mining_cycle("decay_response")

        except Exception as exc:
            logger.error("维护异常: %s", exc, exc_info=True)

    async def _run_deep_search(self):
        """深度进化优化（周日凌晨）。"""
        logger.info("─" * 50)
        logger.info("执行深度进化优化（GP 大种群 + 高代数）...")

        try:
            runtime = _get_factor_mining_runtime()

            result = await runtime.run_once(
                trigger="weekly_deep_search",
                engines=["gp_classic", "mcts_guided", "rl_alphagen"],
                candidate_count=50,
                evolution_generations=10,
                codes=self.codes,
            )

            if result.get("success"):
                logger.info(
                    "深度搜索完成 ✅ 原始=%d 入池=%d",
                    result.get("raw_candidate_count", 0),
                    result.get("admitted_count", 0),
                )
            else:
                logger.warning("深度搜索失败: %s", result.get("error"))

        except Exception as exc:
            logger.error("深度搜索异常: %s", exc, exc_info=True)

    # ─── 工具方法 ─────────────────────────────────────────────────────

    async def _interruptible_sleep(self, seconds: float) -> bool:
        """可中断等待，返回 True 表示正常唤醒，False 表示被中断。"""
        end = time.time() + seconds
        while self._running and time.time() < end:
            await asyncio.sleep(min(1.0, end - time.time()))
        return self._running


async def show_status():
    """显示工厂状态。"""
    runtime = _get_factor_mining_runtime()
    status = runtime.status()

    print("因子挖掘工厂状态")
    print("─" * 40)
    print(f"  初始化: {status['initialized']}")
    print(f"  运行次数: {status['run_count']}")
    print(f"  上次运行: {status['last_run_at'] or '从未'}")
    print(f"  因子池大小: {status['pool_size']}")
    print()

    engines = status.get("engines", {})
    if engines:
        print("引擎状态:")
        for eid, info in engines.items():
            ready = "✅" if info.get("ready") else "❌"
            print(f"  {ready} {eid} (成功={info.get('success_count', 0)} 失败={info.get('failure_count', 0)})")
    print()


def parse_args():
    parser = argparse.ArgumentParser(description="因子挖掘工厂运行脚本")
    parser.add_argument("--once", action="store_true", help="立即执行一次挖掘周期")
    parser.add_argument("--maintenance", action="store_true", help="立即执行一次维护")
    parser.add_argument("--interval", type=int, default=0, help="固定间隔模式（秒），0=调度模式")
    parser.add_argument("--status", action="store_true", help="查看工厂状态")
    parser.add_argument("--candidates", type=int, default=30, help="每轮候选因子数")
    parser.add_argument("--generations", type=int, default=5, help="进化代数")
    parser.add_argument("--engines", nargs="*", help="指定引擎 (llm_primary gp_classic mcts_guided rl_alphagen rule_seed)")
    parser.add_argument("--codes", nargs="*", help="目标股票代码")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.status:
        asyncio.run(show_status())
        return

    if args.once:
        mode = "once"
    elif args.maintenance:
        mode = "maintenance"
    elif args.interval > 0:
        mode = "interval"
    else:
        mode = "schedule"

    runner = FactorMiningFactoryRunner(
        mode=mode,
        interval_sec=args.interval if args.interval > 0 else 7200,
        candidate_count=args.candidates,
        evolution_generations=args.generations,
        engines=args.engines,
        codes=args.codes,
    )

    try:
        asyncio.run(runner.run())
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt, 退出")


if __name__ == "__main__":
    main()
