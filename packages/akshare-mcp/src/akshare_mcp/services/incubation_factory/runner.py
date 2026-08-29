"""孵化工厂 · 独立运行器。

孵化工厂的主循环，负责：
1. 自动识别和接纳策略工厂产出的新策略
2. 前向收益验证
3. 孵化流水线评估与阶段推进
4. 命中率报告生成
5. 反馈写入（供策略工厂读取）

2026-05-28 解耦升级 (P0/A 方案):
    孵化工厂现在 owns paper-trading runtime daemons (MatchingEngine + NavEngine)。
    之前这两个 daemon 仅由 MCP server 启动 (server.py:_launch_sync_background_services),
    导致 supervisor 模式下没有 MCP server 时撮合永不执行。现在守护进程入口在
    run_daemon() 启动时自动拉起 MatchingEngine + NavEngine,优雅退出时关闭。

    Env toggle:
      INCUBATION_FACTORY_OWNS_PAPER_TRADING=1 (默认开)
        启用孵化工厂内嵌的 MatchingEngine + NavEngine。
      MATCHING_ENGINE_ENABLED / NAV_ENGINE_ENABLED 仍按各自子组件 env toggle 走。

    避免双跑:
      MCP server (server.py) 检查 INCUBATION_FACTORY_OWNS_PAPER_TRADING,
      为 1 时 server 不再启动 MatchingEngine / NavEngine,留给孵化工厂启动。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Optional
from uuid import uuid4

from .intake import IncubationIntake, _resolve_db_async_method
from .signal_generator import SignalGenerator
from .forward_verifier import ForwardVerifier
from .metrics_recorder import MetricsRecorder
from .hit_rate_reporter import HitRateReporter
from .feedback_writer import FeedbackWriter
from .trade_prediction_verifier import TradePredictionDailyVerifier
from .accelerator import IncubationAccelerator
from .alert_monitor import AlertMonitor

logger = logging.getLogger(__name__)

# 默认运行时间：18:30（A 股 15:00 收盘 + 数据同步 ~2h + 缓冲）
DEFAULT_RUN_TIME = dt_time(18, 30)

# 单策略处理超时（秒）
STRATEGY_TIMEOUT_SEC = 30

# 批量处理超时（秒）
BATCH_TIMEOUT_SEC = 600

# 错误后等待时间（秒）
ERROR_BACKOFF_SEC = 300

# 健康检查间隔（秒）
HEARTBEAT_INTERVAL_SEC = 3600


def _as_bool(value: Optional[str]) -> bool:
    """ENV bool 解析,与 server.py:_as_bool 等价。"""
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class IncubationFactoryRunner:
    """孵化工厂独立运行器。

    支持两种运行模式：
    - run_once(): 单次执行完整孵化周期
    - run_daemon(): 守护进程模式，每日定时运行
    """

    def __init__(
        self,
        *,
        run_time: dt_time = DEFAULT_RUN_TIME,
        dry_run: bool = False,
        auto_apply_review: bool = True,
        owns_paper_trading: Optional[bool] = None,
    ):
        self.run_time = run_time
        self.dry_run = dry_run
        self.auto_apply_review = auto_apply_review

        # 孵化工厂是否拥有 paper-trading runtime (MatchingEngine + NavEngine)。
        # 默认从 ENV 读 INCUBATION_FACTORY_OWNS_PAPER_TRADING (默认开),
        # dry_run 模式下强制关闭避免误撮合。
        if owns_paper_trading is None:
            owns_paper_trading = _as_bool(
                os.getenv("INCUBATION_FACTORY_OWNS_PAPER_TRADING", "true")
            )
        self.owns_paper_trading = bool(owns_paper_trading) and not dry_run

        # 子模块
        self._intake = IncubationIntake()
        self._signal_generator = SignalGenerator()
        self._forward_verifier = ForwardVerifier()
        self._metrics_recorder = MetricsRecorder()
        self._reporter = HitRateReporter()
        self._feedback_writer = FeedbackWriter()
        self._trade_prediction_verifier = TradePredictionDailyVerifier()
        self._accelerator = IncubationAccelerator()
        self._alert_monitor = AlertMonitor()

        # Paper-trading runtime daemons (lazy 初始化,start/shutdown 动作时按需 import)
        self._matching_engine: Any = None
        self._nav_engine: Any = None
        self._paper_trading_started: bool = False

        # 运行状态
        self._last_run_at: Optional[datetime] = None
        self._last_result: Optional[dict[str, Any]] = None
        self._run_count: int = 0
        self._error_count: int = 0
        self._error_backoff_sec: int = ERROR_BACKOFF_SEC

    async def _start_paper_trading_daemons(self) -> None:
        """启动 MatchingEngine + NavEngine 后台 daemon。

        每个组件遵守自己 ENV toggle:
            MATCHING_ENGINE_ENABLED (默认 true)
            NAV_ENGINE_ENABLED (默认 true)
        失败不抛异常,仅 warn,允许孵化工厂继续运行(只是没撮合)。
        """
        if not self.owns_paper_trading:
            logger.info(
                "IncubationFactory: skipping paper-trading daemons (owns_paper_trading=%s, dry_run=%s)",
                self.owns_paper_trading,
                self.dry_run,
            )
            return
        if self._paper_trading_started:
            return

        # MatchingEngine
        if _as_bool(os.getenv("MATCHING_ENGINE_ENABLED", "true")):
            try:
                from ..matching_engine import get_matching_engine
                self._matching_engine = get_matching_engine()
                self._matching_engine.start()
                logger.info("IncubationFactory: MatchingEngine started (owned by incubation_factory)")
            except Exception as exc:
                logger.warning("IncubationFactory: MatchingEngine failed to start: %s", exc)
                self._matching_engine = None
        else:
            logger.info("IncubationFactory: MatchingEngine disabled by env (MATCHING_ENGINE_ENABLED=0)")

        # NavEngine
        if _as_bool(os.getenv("NAV_ENGINE_ENABLED", "true")):
            try:
                from ..nav_engine import get_nav_engine
                self._nav_engine = get_nav_engine()
                self._nav_engine.start()
                logger.info("IncubationFactory: NavEngine started (owned by incubation_factory)")
            except Exception as exc:
                logger.warning("IncubationFactory: NavEngine failed to start: %s", exc)
                self._nav_engine = None
        else:
            logger.info("IncubationFactory: NavEngine disabled by env (NAV_ENGINE_ENABLED=0)")

        self._paper_trading_started = True

    async def _stop_paper_trading_daemons(self) -> None:
        """优雅关闭 paper-trading daemon。"""
        if not self._paper_trading_started:
            return

        if self._matching_engine is not None:
            try:
                shutdown = getattr(self._matching_engine, "shutdown", None)
                if callable(shutdown):
                    await shutdown(grace_sec=2.0)
                else:
                    self._matching_engine.stop()
                logger.info("IncubationFactory: MatchingEngine stopped")
            except Exception as exc:
                logger.warning("IncubationFactory: MatchingEngine shutdown failed: %s", exc)
            self._matching_engine = None

        if self._nav_engine is not None:
            try:
                shutdown = getattr(self._nav_engine, "shutdown", None)
                if callable(shutdown):
                    await shutdown(grace_sec=2.0)
                else:
                    self._nav_engine.stop()
                logger.info("IncubationFactory: NavEngine stopped")
            except Exception as exc:
                logger.warning("IncubationFactory: NavEngine shutdown failed: %s", exc)
            self._nav_engine = None

        self._paper_trading_started = False

    async def _run_once_impl(self) -> dict[str, Any]:
        """单次执行完整孵化周期。

        流程：
        1. 自动识别和接纳新策略
        2. 前向收益验证
        3. 指标记录
        4. 孵化流水线评估（复用已有实现）
        5. 命中率报告生成
        6. 反馈写入

        每个 phase 都被 ``_run_phase`` 包裹一层超时与错误捕获 — 单个
        phase 异常不再让整轮 failed，而是把错误聚合到
        ``result["phase_failures"]`` 并继续后续 phase。整轮以
        BATCH_TIMEOUT_SEC 为上限，超时也会被记录而不是冒出。
        """
        run_id = uuid4().hex[:12]
        start_time = datetime.now(timezone.utc)
        logger.info("IncubationFactory: starting run %s", run_id)
        phase_failures: list[dict[str, Any]] = []

        async def _run_phase(name: str, coro_factory, *, timeout: float = STRATEGY_TIMEOUT_SEC):
            """Run a single phase with a timeout + error capture.

            Returns the phase result, or None on failure (caller treats None
            as "skipped" and falls back to defaults). Records failures into
            the closed-over phase_failures list so the overall envelope can
            surface them as status='partial'.
            """
            try:
                return await asyncio.wait_for(coro_factory(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.error(
                    "IncubationFactory [%s] %s: timeout after %.0fs",
                    run_id, name, timeout,
                )
                phase_failures.append({"phase": name, "error": "timeout", "timeout_sec": timeout})
            except Exception as exc:
                logger.exception(
                    "IncubationFactory [%s] %s: failed: %s", run_id, name, exc,
                )
                phase_failures.append({"phase": name, "error": str(exc), "error_type": type(exc).__name__})
            return None

        db = await self._get_db()

        try:
            # Phase 1: 自动识别和接纳新策略
            logger.info("IncubationFactory [%s] Phase 1: Intake", run_id)
            intake_result = await _run_phase(
                "intake", lambda: self._intake.scan_and_accept(db),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            # Phase 1.5: observe 池趋势策略重编译 remediation + observe->formal 转正。
            # 默认开启; 设置 INCUBATION_FACTORY_RECOMPILE_REMEDIATION_ENABLED=0 时跳过。
            remediation_result = await _run_phase(
                "recompile_remediation",
                lambda: self._run_recompile_remediation(db),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            # Phase 2: 加载所有孵化中的策略 (cheap, no timeout)
            incubating = await self._list_incubating(db)
            # === DEV-V1 P1: 加载 paper observation 策略 ===
            # 默认开启; 设置 INCUBATION_FACTORY_PAPER_INTAKE_ENABLED=0 时返回空列表。
            paper_observation = await self._list_paper_observation(db)
            diagnostic_observation = await self._list_diagnostic_observation(db)
            # 给两个集合打 stage 标记,便于 Phase 3 阈值差异化(后续优化用)。
            for _s in incubating:
                _s.setdefault("_intake_stage", "incubating")
            for _s in paper_observation:
                _s.setdefault("_intake_stage", "paper")
            for _s in diagnostic_observation:
                _s.setdefault("_intake_stage", "diagnostic")
            # 合并:incubating 优先,paper 其次。limit 由各自独立控制。
            all_strategies = list(incubating) + list(paper_observation) + list(diagnostic_observation)
            logger.info(
                "IncubationFactory [%s] Phase 2: %d incubating + %d paper + %d diagnostic to verify",
                run_id,
                len(incubating),
                len(paper_observation),
                len(diagnostic_observation),
            )

            # Phase 3: 信号生成 + 前向收益验证 + 指标记录
            verifications: dict[str, dict[str, Any]] = {}
            metrics_recorded = 0
            verification_errors = 0
            signals_generated_total = 0
            orders_filled_total = 0
            orders_rejected_total = 0
            order_settlement_errors = 0
            order_settlements: dict[str, dict[str, Any]] = {}

            for strategy in all_strategies:
                sid = str(strategy.get("id") or "").strip()
                if not sid:
                    continue
                try:
                    # 信号生成 — 单策略级超时
                    signal_result = await asyncio.wait_for(
                        self._signal_generator.generate(db, strategy),
                        timeout=STRATEGY_TIMEOUT_SEC,
                    )
                    signals_generated_total += int(
                        signal_result.get("signals_generated") or 0
                    )

                    if (
                        not self.dry_run
                        and str(strategy.get("_intake_stage") or "") in {"incubating", "paper"}
                    ):
                        try:
                            settlement = await asyncio.wait_for(
                                self._settle_strategy_orders(
                                    db,
                                    strategy,
                                    signal_result=signal_result,
                                ),
                                timeout=STRATEGY_TIMEOUT_SEC,
                            )
                            order_settlements[sid] = settlement
                            orders_filled_total += int(settlement.get("filled_count") or 0)
                            orders_rejected_total += int(settlement.get("rejected_count") or 0)
                        except asyncio.TimeoutError:
                            order_settlement_errors += 1
                            logger.warning(
                                "IncubationFactory [%s]: order settlement timeout for %s after %ss",
                                run_id, sid, STRATEGY_TIMEOUT_SEC,
                            )
                        except Exception as exc:
                            order_settlement_errors += 1
                            logger.warning(
                                "IncubationFactory [%s]: order settlement failed for %s: %s",
                                run_id, sid, exc,
                            )

                    # 前向验证 — 单策略级超时
                    verification = await asyncio.wait_for(
                        self._forward_verifier.verify(db, strategy),
                        timeout=STRATEGY_TIMEOUT_SEC,
                    )
                    verifications[sid] = verification

                    # 指标记录 — 单策略级超时
                    if not self.dry_run:
                        metric = await asyncio.wait_for(
                            self._metrics_recorder.record(db, strategy, verification),
                            timeout=STRATEGY_TIMEOUT_SEC,
                        )
                        if metric is not None:
                            metrics_recorded += 1
                        if str(strategy.get("_intake_stage") or "") == "diagnostic":
                            await self._record_diagnostic_processed_event(
                                db,
                                strategy,
                                verification,
                                signal_result,
                            )
                except asyncio.TimeoutError:
                    verification_errors += 1
                    logger.warning(
                        "IncubationFactory [%s]: verify/record timeout for %s after %ss",
                        run_id, sid, STRATEGY_TIMEOUT_SEC,
                    )
                except Exception as exc:
                    verification_errors += 1
                    logger.warning(
                        "IncubationFactory [%s]: verify/record failed for %s: %s",
                        run_id, sid, exc,
                    )

            logger.info(
                "IncubationFactory [%s] Phase 3: signals=%d, filled=%d, rejected=%d, verified=%d, recorded=%d, errors=%d, settlement_errors=%d",
                run_id,
                signals_generated_total,
                orders_filled_total,
                orders_rejected_total,
                len(verifications),
                metrics_recorded,
                verification_errors,
                order_settlement_errors,
            )

            logger.info("IncubationFactory [%s] Phase 3b: Trade prediction outcomes", run_id)
            trade_prediction_result = await _run_phase(
                "trade_prediction_outcomes",
                lambda: self._trade_prediction_verifier.verify_pending(
                    db,
                    include_intraday=True,
                    sync_intraday_before_replay=True,
                    persist=not self.dry_run,
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            logger.info("IncubationFactory [%s] Phase 3c: Signal-only paper execution backlog", run_id)
            paper_execution_backlog_result = await _run_phase(
                "paper_execution_backlog",
                lambda: self._run_signal_only_paper_execution_backlog(
                    db,
                    strategies=list(incubating) + list(paper_observation),
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            logger.info("IncubationFactory [%s] Phase 3c2: Exit signal paper execution", run_id)
            exit_signal_execution_result = await _run_phase(
                "exit_signal_paper_execution",
                lambda: self._run_exit_signal_paper_execution(
                    db,
                    strategies=all_strategies,
                    as_of=as_of,
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            logger.info("IncubationFactory [%s] Phase 3d: Stale paper position closure", run_id)
            stale_position_closure_result = await _run_phase(
                "stale_paper_position_closure",
                lambda: self._run_stale_paper_position_closure(
                    db,
                    strategies=all_strategies,
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            logger.info("IncubationFactory [%s] Phase 3e: Native execution evidence backfill", run_id)
            native_evidence_backfill_result = await _run_phase(
                "native_execution_evidence_backfill",
                lambda: self._run_native_execution_evidence_backfill(
                    db,
                    strategies=list(incubating) + list(paper_observation),
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            # Phase 3f: execution audit acceptance snapshots/backfill
            logger.info("IncubationFactory [%s] Phase 3f: Execution audit acceptance", run_id)
            execution_audit_acceptance_result = await _run_phase(
                "execution_audit_acceptance",
                lambda: self._run_execution_audit_acceptance(
                    db,
                    strategies=list(incubating) + list(paper_observation),
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            # Phase 4: 孵化流水线评估（复用已有实现）
            logger.info("IncubationFactory [%s] Phase 4: Pipeline evaluation", run_id)
            logger.info("IncubationFactory [%s] Phase 3g: Execution audit remediation", run_id)
            execution_audit_remediation_result = await _run_phase(
                "execution_audit_remediation",
                lambda: self._run_execution_audit_remediation(
                    db,
                    strategies=list(incubating) + list(paper_observation),
                    acceptance_result=execution_audit_acceptance_result,
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            pipeline_result = await _run_phase(
                "pipeline",
                lambda: self._run_pipeline(
                    db,
                    strategies=list(incubating) + list(paper_observation),
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            # Phase 5: 命中率报告
            logger.info("IncubationFactory [%s] Phase 5: Hit rate report", run_id)
            report = await _run_phase(
                "hit_rate_report",
                lambda: self._reporter.generate(
                    db,
                    all_strategies,
                    verifications,
                    pipeline_result,
                    trade_prediction_result=trade_prediction_result,
                ),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            # Phase 6: 反馈写入
            feedback_result: dict[str, Any] = {}
            if not self.dry_run:
                logger.info("IncubationFactory [%s] Phase 6: Feedback write", run_id)
                feedback_result = await _run_phase(
                    "feedback_write",
                    lambda: self._feedback_writer.write(db, report),
                    timeout=BATCH_TIMEOUT_SEC,
                ) or {}

            # Phase 7: 加速孵化评估
            logger.info("IncubationFactory [%s] Phase 7: Acceleration check", run_id)
            acceleration_result = await _run_phase(
                "acceleration",
                lambda: self._accelerator.evaluate_batch(db, incubating, verifications),
                timeout=BATCH_TIMEOUT_SEC,
            ) or {}

            # Phase 8: 异常告警检查
            logger.info("IncubationFactory [%s] Phase 8: Alert check", run_id)
            alert_result = await _run_phase(
                "alert_check",
                lambda: self._alert_monitor.check(db, run_result={
                    "verification": {
                        "total": len(incubating),
                        "errors": verification_errors,
                    },
                }),
                timeout=STRATEGY_TIMEOUT_SEC,
            ) or {}

            # Phase 9: 健康检查心跳
            await _run_phase(
                "heartbeat",
                lambda: self._heartbeat(db, run_id),
                timeout=STRATEGY_TIMEOUT_SEC,
            )

            # 汇总结果
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            overall_status = "completed" if not phase_failures else "partial"
            result = {
                "run_id": run_id,
                "status": overall_status,
                "phase_failures": phase_failures,
                "dry_run": self.dry_run,
                "started_at": start_time.isoformat(),
                "elapsed_seconds": round(elapsed, 2),
                "intake": intake_result,
                "recompile_remediation": remediation_result,
                "verification": {
                    "total": len(all_strategies),
                    "incubating_count": len(incubating),
                    "paper_count": len(paper_observation),
                    "diagnostic_count": len(diagnostic_observation),
                    "verified": len(verifications),
                    "metrics_recorded": metrics_recorded,
                    "errors": verification_errors,
                },
                "settlement": {
                    "evaluated": len(order_settlements),
                    "filled": orders_filled_total,
                    "rejected": orders_rejected_total,
                    "errors": order_settlement_errors,
                    "items": list(order_settlements.values())[:50],
                },
                "pipeline": {
                    "count": int(pipeline_result.get("count") or 0),
                    "auto_promoted": int(pipeline_result.get("auto_promoted") or 0),
                    "stage_counts": dict(pipeline_result.get("stage_counts") or {}),
                },
                "trade_predictions": {
                    "status": trade_prediction_result.get("status"),
                    "evaluated": int(trade_prediction_result.get("evaluated") or 0),
                    "intraday_evaluated": int(trade_prediction_result.get("intraday_evaluated") or 0),
                    "score_status_counts": dict(trade_prediction_result.get("score_status_counts") or {}),
                    "data_quality_status_counts": dict(trade_prediction_result.get("data_quality_status_counts") or {}),
                    "intraday_sync": dict(trade_prediction_result.get("intraday_sync") or {}),
                },
                "paper_execution_backlog": paper_execution_backlog_result,
                "exit_signal_paper_execution": exit_signal_execution_result,
                "stale_paper_position_closure": stale_position_closure_result,
                "native_execution_evidence_backfill": native_evidence_backfill_result,
                "execution_audit_acceptance": execution_audit_acceptance_result,
                "execution_audit_remediation": execution_audit_remediation_result,
                "report": {
                    "overall_hit_rate": (
                        (report.get("hit_rate_dashboard") or {}).get("overall") or {}
                    ).get("hit_rate"),
                    "overall_skill_lcb": (
                        (report.get("hit_rate_dashboard") or {}).get("overall") or {}
                    ).get("avg_skill_lcb"),
                    "families_tracked": len(
                        (report.get("hit_rate_dashboard") or {}).get("by_family") or {}
                    ),
                },
                "feedback": feedback_result,
                "acceleration": {
                    "evaluated": int(acceleration_result.get("evaluated") or 0),
                    "accelerated": int(acceleration_result.get("accelerated_count") or 0),
                },
                "alerts": {
                    "count": int(alert_result.get("alert_count") or 0),
                    "items": list(alert_result.get("alerts") or []),
                },
            }

            self._last_run_at = start_time
            self._last_result = result
            self._run_count += 1

            logger.info(
                "IncubationFactory [%s]: %s in %.1fs "
                "(intake=%d, verified=%d, promoted=%d, hit_rate=%.2f%%, phase_failures=%d)",
                run_id,
                overall_status,
                elapsed,
                int(intake_result.get("accepted") or 0),
                len(verifications),
                int(pipeline_result.get("auto_promoted") or 0),
                float(
                    (
                        (report.get("hit_rate_dashboard") or {}).get("overall") or {}
                    ).get("hit_rate") or 0
                )
                * 100,
                len(phase_failures),
            )

            return result

        except Exception as exc:
            self._error_count += 1
            elapsed = (datetime.now(timezone.utc) - start_time).total_seconds()
            logger.error(
                "IncubationFactory [%s]: failed after %.1fs: %s",
                run_id,
                elapsed,
                exc,
                exc_info=True,
            )
            return {
                "run_id": run_id,
                "status": "failed",
                "error": str(exc),
                "elapsed_seconds": round(elapsed, 2),
                "phase_failures": phase_failures,
            }
        finally:
            await self._close_db(db)

    async def run_once(self) -> dict[str, Any]:
        from strategy_factory.runtime.incubation import build_incubation_runtime

        runtime = build_incubation_runtime(
            run_time=self.run_time,
            dry_run=self.dry_run,
            auto_apply_review=self.auto_apply_review,
            owns_paper_trading=self.owns_paper_trading,
            support=self,
        )
        return await runtime.run_once()

    async def run_daemon(self) -> None:
        from strategy_factory.runtime.incubation import build_incubation_runtime

        runtime = build_incubation_runtime(
            run_time=self.run_time,
            dry_run=self.dry_run,
            auto_apply_review=self.auto_apply_review,
            owns_paper_trading=self.owns_paper_trading,
            support=self,
        )
        await runtime.run_daemon()

    async def _settle_strategy_orders(
        self,
        db: Any,
        strategy: dict[str, Any],
        *,
        signal_result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Settle paper orders created by the natural incubation run."""
        sid = str((strategy or {}).get("id") or "").strip()
        raw_signal_date = (signal_result or {}).get("signal_date")
        signal_date = date.today()
        if raw_signal_date:
            try:
                signal_date = date.fromisoformat(str(raw_signal_date)[:10])
            except Exception:
                signal_date = date.today()

        from ..incubation import get_strategy_incubation_service

        settlement = await get_strategy_incubation_service().settle_orders(
            db,
            strategy,
            signal_date,
        )
        result = dict(settlement or {})
        result.setdefault("strategy_id", sid)
        result.setdefault("signal_date", str(signal_date))
        return result

    async def _run_recompile_remediation(self, db: Any) -> dict[str, Any]:
        """P0-b/P1: 对 observe 池(submitted)趋势策略重编译补 compiled_dsl + 测量
        instrument_profile,满足 formal readiness 的样本升级到 formal_incubation。

        默认开启; 设置 INCUBATION_FACTORY_RECOMPILE_REMEDIATION_ENABLED=0 时返回 skipped。
        """
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                recompile_remediation_enabled,
                recompile_remediation_batch_limit,
            )
        except Exception:
            return {"status": "skipped", "reason": "toggle_import_failed"}
        if not recompile_remediation_enabled():
            return {"status": "skipped", "reason": "disabled"}
        try:
            from akshare_mcp.services.strategy_recompile_backfill import (
                backfill_historical_trend_strategies,
            )
            result = await backfill_historical_trend_strategies(
                db,
                statuses=["submitted"],
                limit=recompile_remediation_batch_limit(),
                dry_run=bool(self.dry_run),
                measure_profile=True,
                promote_ready=True,
            )
            result["status"] = "ok"
            logger.info(
                "IncubationFactory recompile remediation: scanned=%d recompiled=%d "
                "promoted_to_formal=%d revision_required=%d updated=%d dry_run=%s",
                int(result.get("scanned") or 0),
                int(result.get("recompiled") or 0),
                int(result.get("promoted_to_formal") or 0),
                int(result.get("revision_required") or 0),
                int(result.get("updated") or 0),
                bool(result.get("dry_run")),
            )
            return result
        except Exception as exc:  # noqa: BLE001 - remediation 失败不得拖垮整轮
            logger.warning("IncubationFactory recompile remediation failed: %s", exc)
            return {"status": "error", "reason": f"{type(exc).__name__}:{exc}"}

    @staticmethod
    def _decode_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except Exception:
                return {}
            return dict(parsed) if isinstance(parsed, dict) else {}
        return {}

    @classmethod
    def _strategy_max_holding_days(cls, strategy: dict[str, Any]) -> int:
        payload = dict(strategy or {})
        params = cls._decode_mapping(payload.get("params"))
        risk_rules = cls._decode_mapping(payload.get("risk_rules")) or cls._decode_mapping(
            params.get("risk_rules")
        )
        runtime_playbook = cls._decode_mapping(
            payload.get("runtime_playbook") or params.get("runtime_playbook")
        )
        exit_policy = cls._decode_mapping(runtime_playbook.get("exit_policy"))
        holding_window = cls._decode_mapping(
            payload.get("holding_window") or params.get("holding_window")
        )
        rule_contract = cls._decode_mapping(
            payload.get("rule_template_contract") or params.get("rule_template_contract")
        )
        default_risk = cls._decode_mapping(rule_contract.get("default_risk_constraints"))
        candidates = (
            exit_policy.get("time_stop_days"),
            exit_policy.get("max_holding_days"),
            holding_window.get("max_days"),
            risk_rules.get("max_holding_days"),
            default_risk.get("max_holding_days"),
        )
        for value in candidates:
            try:
                days = int(float(value))
            except Exception:
                continue
            if days > 0:
                return min(days, 365)
        return 0

    @staticmethod
    def _position_opened_date(position: dict[str, Any]) -> Optional[date]:
        for key in ("opened_at", "entry_ts", "last_trade_time", "created_at"):
            value = position.get(key)
            if not value:
                continue
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
            except Exception:
                continue
        return None

    @staticmethod
    def _coerce_date(value: Any) -> Optional[date]:
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except Exception:
            try:
                return date.fromisoformat(text[:10])
            except Exception:
                return None

    @staticmethod
    def _decode_strategy_candidate(db: Any, row: dict[str, Any]) -> dict[str, Any]:
        decoder = getattr(db, "_decode_strategy_row", None)
        payload = dict(row or {})
        if callable(decoder):
            try:
                return dict(decoder(payload))
            except Exception:
                return payload
        return payload

    async def _select_signal_only_paper_candidates(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int]:
        acquire = getattr(db, "acquire", None)
        if callable(acquire) and "acquire" in dir(db):
            async with acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH signal_stats AS (
                        SELECT
                            strategy_id,
                            COUNT(*) AS total_signals,
                            MAX(signal_date) AS latest_signal_date
                        FROM strategy_signals
                        WHERE COALESCE(signal, 0) <> 0
                        GROUP BY strategy_id
                    ),
                    order_stats AS (
                        SELECT strategy_id, COUNT(*) AS total_orders
                        FROM paper_orders
                        GROUP BY strategy_id
                    ),
                    account_candidates AS (
                        SELECT
                            s.*,
                            a.account_id AS paper_account_id,
                            a.stage AS observation_stage,
                            ss.total_signals AS signal_count,
                            ss.latest_signal_date AS latest_signal_date,
                            COALESCE(os.total_orders, 0) AS total_orders,
                            ROW_NUMBER() OVER (
                                PARTITION BY s.id
                                ORDER BY
                                    CASE a.stage
                                        WHEN 'paper' THEN 0
                                        WHEN 'warmup' THEN 1
                                        WHEN 'observe' THEN 2
                                        WHEN 'candidate' THEN 3
                                        ELSE 9
                                    END,
                                    datetime(COALESCE(a.updated_at, a.bound_at)) DESC
                            ) AS rn
                        FROM strategies s
                        JOIN strategy_incubation_accounts a
                          ON a.strategy_id = s.id
                        JOIN signal_stats ss
                          ON ss.strategy_id = s.id
                        LEFT JOIN order_stats os
                          ON os.strategy_id = s.id
                        LEFT JOIN paper_accounts pa
                          ON pa.id = a.account_id
                        WHERE a.status = 'active'
                          AND a.stage IN ('warmup', 'paper', 'observe', 'candidate')
                          AND COALESCE(pa.status, 'active') = 'active'
                          AND COALESCE(pa.account_type, 'incubation') = 'incubation'
                          AND COALESCE(os.total_orders, 0) = 0
                    ),
                    deduped AS (
                        SELECT *
                        FROM account_candidates
                        WHERE rn = 1
                    )
                    SELECT *, COUNT(*) OVER () AS signal_only_backlog_count
                    FROM deduped
                    ORDER BY
                        date(latest_signal_date) DESC,
                        signal_count DESC,
                        datetime(COALESCE(updated_at, created_at)) DESC
                    LIMIT $1
                    """,
                    int(limit),
                )
            candidates: list[dict[str, Any]] = []
            backlog_count = 0
            for row in list(rows or []):
                payload = dict(row or {})
                backlog_count = max(backlog_count, int(payload.get("signal_only_backlog_count") or 0))
                strategy = self._decode_strategy_candidate(db, payload)
                strategy["_latest_signal_date"] = payload.get("latest_signal_date")
                strategy["_signal_count"] = int(payload.get("signal_count") or 0)
                strategy["_signal_only_backlog_count"] = backlog_count
                candidates.append(strategy)
            return candidates, backlog_count

        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for strategy in list(strategies or []):
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            unique.append(dict(strategy or {}))

        candidates = []
        for strategy in unique:
            sid = str(strategy.get("id") or "").strip()
            get_signals = _resolve_db_async_method(db, "get_signals")
            list_orders = _resolve_db_async_method(db, "list_strategy_paper_orders")
            try:
                signals = list(await get_signals(sid, limit=1) or []) if get_signals is not None else []
                orders = list(await list_orders(sid, limit=1) or []) if list_orders is not None else []
            except Exception:
                continue
            if signals and not orders:
                latest_signal_date = (signals[0] or {}).get("signal_date")
                strategy["_latest_signal_date"] = latest_signal_date
                strategy["_signal_count"] = 1
                candidates.append(strategy)
        return candidates[:limit], len(candidates)

    async def _run_signal_only_paper_execution_backlog(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Convert active signal-only incubation backlog into auditable paper execution."""
        if self.dry_run:
            return {"status": "skipped", "reason": "dry_run", "evaluated": 0}
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                paper_execution_backlog_batch_limit,
                paper_execution_backlog_enabled,
            )
        except Exception:
            return {"status": "skipped", "reason": "toggle_import_failed", "evaluated": 0}
        if not paper_execution_backlog_enabled():
            return {"status": "skipped", "reason": "disabled", "evaluated": 0}
        try:
            from ..incubation import get_strategy_incubation_service
            incubation_service = get_strategy_incubation_service()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "skipped",
                "reason": "incubation_service_unavailable",
                "error": f"{type(exc).__name__}:{exc}",
                "evaluated": 0,
            }

        limit = paper_execution_backlog_batch_limit()
        try:
            selected, backlog_count = await self._select_signal_only_paper_candidates(
                db,
                strategies=strategies,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "reason": "candidate_selection_failed",
                "error": f"{type(exc).__name__}:{exc}",
                "evaluated": 0,
                "limit": limit,
            }

        if not selected:
            return {
                "status": "skipped",
                "reason": "no_signal_only_backlog",
                "candidate_count": int(backlog_count or 0),
                "signal_only_backlog_count": int(backlog_count or 0),
                "selected_count": 0,
                "evaluated": 0,
                "limit": limit,
            }

        grouped: dict[date, list[dict[str, Any]]] = {}
        missing_signal_date_count = 0
        for strategy in selected:
            signal_date = self._coerce_date(strategy.get("_latest_signal_date"))
            if signal_date is None:
                missing_signal_date_count += 1
                continue
            grouped.setdefault(signal_date, []).append(strategy)

        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        orders_created = 0
        orders_filled = 0
        rejected_orders = 0
        metrics_recorded = 0
        skip_reason_counts: dict[str, int] = {}

        for signal_date, batch in sorted(grouped.items(), key=lambda item: item[0], reverse=True):
            try:
                result = await incubation_service.process_strategies(
                    db,
                    batch,
                    signal_date=signal_date,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "signal_date": str(signal_date),
                    "strategy_count": len(batch),
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                continue
            orders_created += int((result or {}).get("orders_created") or 0)
            orders_filled += int((result or {}).get("orders_filled") or 0)
            rejected_orders += int((result or {}).get("rejected_orders") or 0)
            metrics_recorded += int((result or {}).get("metrics_recorded") or 0)
            for reason, count in dict((result or {}).get("skip_reason_counts") or {}).items():
                token = str(reason or "unknown").strip() or "unknown"
                skip_reason_counts[token] = int(skip_reason_counts.get(token) or 0) + int(count or 0)
            for item in list((result or {}).get("items") or []):
                payload = dict(item or {})
                if payload.get("error"):
                    errors.append({
                        "strategy_id": payload.get("strategy_id"),
                        "signal_date": str(signal_date),
                        "error": str(payload.get("error")),
                    })
                payload.setdefault("signal_date", str(signal_date))
                items.append(payload)

        result_status = "ok" if not errors else "partial"
        if orders_created <= 0 and selected:
            result_status = "pending_execution" if result_status == "ok" else result_status
        if not items and errors:
            result_status = "error"
        return {
            "status": result_status,
            "candidate_count": int(backlog_count or len(selected)),
            "signal_only_backlog_count": int(backlog_count or len(selected)),
            "selected_count": len(selected),
            "evaluated": len(items),
            "errors": len(errors),
            "limit": limit,
            "missing_signal_date_count": missing_signal_date_count,
            "orders_created": orders_created,
            "orders_filled": orders_filled,
            "rejected_orders": rejected_orders,
            "metrics_recorded": metrics_recorded,
            "skip_reason_counts": dict(skip_reason_counts),
            "items": items[:50],
            "error_items": errors[:20],
        }

    async def _select_exit_signal_candidates(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
        limit: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        """Select strategies with exit signal + open positions but no exit orders."""
        candidates: list[dict[str, Any]] = []

        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for strategy in list(strategies or []):
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            unique.append(dict(strategy or {}))

        for strategy in unique:
            sid = str(strategy.get("id") or "").strip()
            if not sid:
                continue

            try:
                # Check for exit signals
                cursor = db.connection.execute(
                    "SELECT COUNT(*) FROM strategy_signals WHERE strategy_id = ? AND signal = -1",
                    (sid,)
                )
                exit_signal_count = cursor.fetchone()[0] if cursor else 0

                if exit_signal_count == 0:
                    continue

                # Check for open positions
                cursor = db.connection.execute(
                    "SELECT id, code FROM strategy_trade_positions WHERE strategy_id = ? AND status = 'open'",
                    (sid,)
                )
                open_positions = [{"id": row[0], "code": row[1]} for row in cursor.fetchall()] if cursor else []

                if not open_positions:
                    continue

                # Check if already has exit orders
                cursor = db.connection.execute(
                    "SELECT COUNT(*) FROM paper_orders WHERE strategy_id = ? AND direction IN ('sell', 'exit', 'short')",
                    (sid,)
                )
                exit_order_count = cursor.fetchone()[0] if cursor else 0

                if exit_order_count > 0:
                    continue

                strategy["_exit_signal_count"] = exit_signal_count
                strategy["_open_positions"] = open_positions
                candidates.append(strategy)
            except Exception:  # noqa: BLE001
                continue

        return candidates[:limit], len(candidates)

    async def _run_exit_signal_paper_execution(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        """Convert exit signals with open positions into exit orders."""
        if self.dry_run:
            return {"status": "skipped", "reason": "dry_run", "evaluated": 0}

        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                paper_execution_backlog_batch_limit,
                paper_execution_backlog_enabled,
            )
        except Exception:
            return {"status": "skipped", "reason": "toggle_import_failed", "evaluated": 0}

        if not paper_execution_backlog_enabled():
            return {"status": "skipped", "reason": "disabled", "evaluated": 0}

        try:
            from ..incubation import get_strategy_incubation_service
            incubation_service = get_strategy_incubation_service()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "skipped",
                "reason": "incubation_service_unavailable",
                "error": f"{type(exc).__name__}:{exc}",
                "evaluated": 0,
            }

        as_of_date = as_of or date.today()
        limit = paper_execution_backlog_batch_limit()

        try:
            selected, total_count = await self._select_exit_signal_candidates(
                db,
                strategies=strategies,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "reason": "candidate_selection_failed",
                "error": f"{type(exc).__name__}:{exc}",
                "evaluated": 0,
                "limit": limit,
            }

        if not selected:
            return {
                "status": "skipped",
                "reason": "no_exit_signal_backlog",
                "exit_signal_backlog_count": 0,
                "selected_count": 0,
                "evaluated": 0,
                "limit": limit,
            }

        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        exit_orders_created = 0
        exit_orders_filled = 0
        positions_closed = 0

        for strategy in selected:
            sid = str(strategy.get("id") or "").strip()
            open_positions = list(strategy.get("_open_positions") or [])
            exit_signal_count = int(strategy.get("_exit_signal_count") or 0)

            if not open_positions:
                continue

            codes = [str(p.get("code") or "").strip() for p in open_positions if p.get("code")]
            if not codes:
                continue

            try:
                close_result = await incubation_service.force_close_open_positions(
                    db,
                    strategy,
                    as_of_date,
                    reason="exit_signal_driven_close",
                    source="incubation_factory_exit_signal",
                    codes=codes,
                )

                orders_created = int((close_result or {}).get("orders_created") or 0)
                orders_filled = int((close_result or {}).get("orders_filled") or 0)
                closed_count = int((close_result or {}).get("positions_closed") or 0)

                exit_orders_created += orders_created
                exit_orders_filled += orders_filled
                positions_closed += closed_count

                items.append({
                    "strategy_id": sid,
                    "exit_signal_count": exit_signal_count,
                    "open_position_count": len(open_positions),
                    "codes": codes,
                    "orders_created": orders_created,
                    "orders_filled": orders_filled,
                    "positions_closed": closed_count,
                })
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "strategy_id": sid,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })

        result_status = "ok" if not errors else "partial"
        if exit_orders_created <= 0 and selected:
            result_status = "pending_execution" if result_status == "ok" else result_status
        if not items and errors:
            result_status = "error"

        return {
            "status": result_status,
            "exit_signal_backlog_count": total_count,
            "selected_count": len(selected),
            "evaluated": len(items),
            "errors": len(errors),
            "limit": limit,
            "exit_orders_created": exit_orders_created,
            "exit_orders_filled": exit_orders_filled,
            "positions_closed": positions_closed,
            "items": items[:50],
            "error_items": errors[:20],
        }

    async def _select_native_evidence_backfill_candidates(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
        limit: int,
    ) -> tuple[list[dict[str, Any]], int]:
        acquire = getattr(db, "acquire", None)
        if callable(acquire) and "acquire" in dir(db):
            async with acquire() as conn:
                rows = await conn.fetch(
                    """
                    WITH trade_stats AS (
                        SELECT strategy_id, COUNT(*) AS trade_count
                        FROM paper_trades
                        GROUP BY strategy_id
                    ),
                    position_stats AS (
                        SELECT strategy_id, COUNT(*) AS position_count
                        FROM strategy_trade_positions
                        GROUP BY strategy_id
                    ),
                    evidence_stats AS (
                        SELECT strategy_id, COUNT(*) AS evidence_count
                        FROM strategy_signal_evidence
                        GROUP BY strategy_id
                    ),
                    account_candidates AS (
                        SELECT
                            s.*,
                            a.account_id AS paper_account_id,
                            a.stage AS observation_stage,
                            COALESCE(ts.trade_count, 0) AS trade_count,
                            COALESCE(ps.position_count, 0) AS position_count,
                            COALESCE(es.evidence_count, 0) AS evidence_count,
                            ROW_NUMBER() OVER (
                                PARTITION BY s.id
                                ORDER BY datetime(COALESCE(a.updated_at, a.bound_at)) DESC
                            ) AS rn
                        FROM strategies s
                        JOIN strategy_incubation_accounts a
                          ON a.strategy_id = s.id
                        LEFT JOIN paper_accounts pa
                          ON pa.id = a.account_id
                        LEFT JOIN trade_stats ts
                          ON ts.strategy_id = s.id
                        LEFT JOIN position_stats ps
                          ON ps.strategy_id = s.id
                        LEFT JOIN evidence_stats es
                          ON es.strategy_id = s.id
                        WHERE a.status = 'active'
                          AND a.stage IN ('warmup', 'paper', 'observe', 'candidate')
                          AND COALESCE(pa.status, 'active') = 'active'
                          AND COALESCE(pa.account_type, 'incubation') = 'incubation'
                          AND (COALESCE(ts.trade_count, 0) > 0 OR COALESCE(ps.position_count, 0) > 0)
                          AND COALESCE(es.evidence_count, 0) = 0
                    ),
                    deduped AS (
                        SELECT *
                        FROM account_candidates
                        WHERE rn = 1
                    )
                    SELECT *, COUNT(*) OVER () AS native_evidence_gap_count
                    FROM deduped
                    ORDER BY (trade_count + position_count) DESC,
                             datetime(COALESCE(updated_at, created_at)) DESC
                    LIMIT $1
                    """,
                    int(limit),
                )
            candidates: list[dict[str, Any]] = []
            gap_count = 0
            for row in list(rows or []):
                payload = dict(row or {})
                gap_count = max(gap_count, int(payload.get("native_evidence_gap_count") or 0))
                strategy = self._decode_strategy_candidate(db, payload)
                strategy["_paper_trade_count"] = int(payload.get("trade_count") or 0)
                strategy["_trade_position_count"] = int(payload.get("position_count") or 0)
                candidates.append(strategy)
            return candidates, gap_count

        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for strategy in list(strategies or []):
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            unique.append(dict(strategy or {}))
        candidates = []
        for strategy in unique:
            sid = str(strategy.get("id") or "").strip()
            list_trades = _resolve_db_async_method(db, "list_strategy_paper_trades")
            list_positions = _resolve_db_async_method(db, "list_strategy_trade_positions")
            list_evidence = _resolve_db_async_method(db, "list_strategy_signal_evidence")
            try:
                trades = list(await list_trades(sid, limit=1) or []) if list_trades is not None else []
                positions = (
                    list(await list_positions(strategy_id=sid, limit=1) or [])
                    if list_positions is not None
                    else []
                )
                evidence = (
                    list(await list_evidence(strategy_id=sid, limit=1) or [])
                    if list_evidence is not None
                    else []
                )
            except Exception:
                continue
            if (trades or positions) and not evidence:
                candidates.append(strategy)
        return candidates[:limit], len(candidates)

    async def _run_native_execution_evidence_backfill(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Backfill native signal evidence for strategies that already have paper execution."""
        if self.dry_run:
            return {"status": "skipped", "reason": "dry_run", "evaluated": 0}
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                execution_audit_native_evidence_backfill_batch_limit,
                execution_audit_native_evidence_backfill_enabled,
            )
        except Exception:
            return {"status": "skipped", "reason": "toggle_import_failed", "evaluated": 0}
        if not execution_audit_native_evidence_backfill_enabled():
            return {"status": "skipped", "reason": "disabled", "evaluated": 0}

        backfill = _resolve_db_async_method(db, "backfill_strategy_signal_evidence_native")
        if backfill is None:
            return {"status": "skipped", "reason": "db_method_missing", "evaluated": 0}

        limit = execution_audit_native_evidence_backfill_batch_limit()
        selected, gap_count = await self._select_native_evidence_backfill_candidates(
            db,
            strategies=strategies,
            limit=limit,
        )
        if not selected:
            return {
                "status": "skipped",
                "reason": "no_native_evidence_gaps",
                "trades_without_signal_evidence_count": int(gap_count or 0),
                "candidate_count": int(gap_count or 0),
                "selected_count": 0,
                "evaluated": 0,
                "limit": limit,
            }

        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        saved_signal_evidence_count = 0
        saved_row_count = 0
        proxy_backfilled_signal_count = 0
        compile_stable_signal_count = 0
        initial_existing_signal_count = 0
        for strategy in selected:
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid:
                continue
            try:
                result = await backfill(strategy_id=sid)
            except TypeError:
                try:
                    result = await backfill(sid)
                except Exception as exc:  # noqa: BLE001
                    errors.append({
                        "strategy_id": sid,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    })
                    continue
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "strategy_id": sid,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                continue
            payload = dict(result or {})
            saved_signal_evidence_count += int(payload.get("saved_signal_count") or 0)
            saved_row_count += int(payload.get("saved_row_count") or 0)
            proxy_backfilled_signal_count += int(payload.get("proxy_backfilled_signal_count") or 0)
            compile_stable_signal_count += int(payload.get("compile_stable_signal_count") or 0)
            initial_existing_signal_count += int(payload.get("initial_existing_signal_count") or 0)
            items.append({
                "strategy_id": sid,
                "status": payload.get("status"),
                "saved_signal_count": int(payload.get("saved_signal_count") or 0),
                "saved_row_count": int(payload.get("saved_row_count") or 0),
                "proxy_backfilled_signal_count": int(payload.get("proxy_backfilled_signal_count") or 0),
                "compile_stable_signal_count": int(payload.get("compile_stable_signal_count") or 0),
                "initial_existing_signal_count": int(payload.get("initial_existing_signal_count") or 0),
            })

        result_status = "ok" if not errors else "partial"
        if items and saved_signal_evidence_count <= 0 and initial_existing_signal_count <= 0:
            result_status = "needs_remediation" if result_status == "ok" else result_status
        if not items and errors:
            result_status = "error"
        return {
            "status": result_status,
            "trades_without_signal_evidence_count": int(gap_count or len(selected)),
            "candidate_count": int(gap_count or len(selected)),
            "selected_count": len(selected),
            "evaluated": len(items),
            "errors": len(errors),
            "limit": limit,
            "saved_signal_evidence_count": saved_signal_evidence_count,
            "saved_row_count": saved_row_count,
            "proxy_backfilled_signal_count": proxy_backfilled_signal_count,
            "compile_stable_signal_count": compile_stable_signal_count,
            "initial_existing_signal_count": initial_existing_signal_count,
            "items": items[:50],
            "error_items": errors[:20],
        }

    async def _run_stale_paper_position_closure(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
        as_of: Optional[date] = None,
    ) -> dict[str, Any]:
        """Emit paper exit orders for positions that exceed the strategy time stop."""
        if self.dry_run:
            return {"status": "skipped", "reason": "dry_run", "evaluated": 0}
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                stale_paper_position_closure_batch_limit,
                stale_paper_position_closure_enabled,
                stale_paper_position_closure_grace_days,
            )
        except Exception:
            return {"status": "skipped", "reason": "toggle_import_failed", "evaluated": 0}
        if not stale_paper_position_closure_enabled():
            return {"status": "skipped", "reason": "disabled", "evaluated": 0}

        list_positions = _resolve_db_async_method(db, "list_strategy_trade_positions")
        if list_positions is None:
            return {"status": "skipped", "reason": "db_method_missing", "evaluated": 0}

        try:
            from ..incubation import get_strategy_incubation_service
            incubation_service = get_strategy_incubation_service()
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "skipped",
                "reason": "incubation_service_unavailable",
                "error": f"{type(exc).__name__}:{exc}",
                "evaluated": 0,
            }

        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for strategy in list(strategies or []):
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            unique.append(dict(strategy or {}))

        limit = stale_paper_position_closure_batch_limit()
        if not unique:
            return {
                "status": "skipped",
                "reason": "no_strategies",
                "candidate_count": 0,
                "selected_count": 0,
                "evaluated": 0,
                "limit": limit,
            }

        as_of_date = as_of or datetime.now().date()
        grace_days = stale_paper_position_closure_grace_days()
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        skip_reasons: dict[str, int] = {}
        created_count = 0
        skipped_order_count = 0
        stale_position_count = 0
        orders_filled_count = 0
        rejected_order_count = 0
        metrics_recorded = 0

        def _count(reason: str) -> None:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        stale_profiles: list[dict[str, Any]] = []
        for strategy in unique:
            sid = str(strategy.get("id") or "").strip()
            max_holding_days = self._strategy_max_holding_days(strategy)
            if max_holding_days <= 0:
                _count("missing_time_stop")
                continue
            try:
                positions = await list_positions(strategy_id=sid, status="open", limit=200)
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "strategy_id": sid,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                continue

            stale_codes: list[str] = []
            max_age_days = 0
            for row in list(positions or []):
                position = dict(row or {})
                code = str(position.get("code") or "").strip()
                opened_date = self._position_opened_date(position)
                if not code or opened_date is None:
                    continue
                age_days = max(0, (as_of_date - opened_date).days)
                max_age_days = max(max_age_days, age_days)
                if age_days >= max_holding_days + grace_days:
                    stale_codes.append(code)
            stale_codes = sorted(dict.fromkeys(stale_codes))
            if not stale_codes:
                _count("no_stale_open_positions")
                continue
            stale_profiles.append({
                "strategy": strategy,
                "strategy_id": sid,
                "max_holding_days": max_holding_days,
                "stale_codes": stale_codes,
                "max_age_days": max_age_days,
            })

        stale_profiles.sort(
            key=lambda item: (
                int(item.get("max_age_days") or 0),
                len(list(item.get("stale_codes") or [])),
                str(item.get("strategy_id") or ""),
            ),
            reverse=True,
        )
        close_selected = stale_profiles[:limit]
        if len(stale_profiles) > len(close_selected):
            skip_reasons["batch_limit_deferred"] = len(stale_profiles) - len(close_selected)

        for profile in close_selected:
            strategy = dict(profile.get("strategy") or {})
            sid = str(profile.get("strategy_id") or strategy.get("id") or "").strip()
            max_holding_days = int(profile.get("max_holding_days") or 0)
            stale_codes = list(profile.get("stale_codes") or [])
            max_age_days = int(profile.get("max_age_days") or 0)

            try:
                close_result = await incubation_service.force_close_open_positions(
                    db,
                    strategy,
                    as_of_date,
                    reason="stale_paper_position_time_stop",
                    source="incubation_factory_stale_close",
                    codes=stale_codes,
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "strategy_id": sid,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                continue

            close_created = int((close_result or {}).get("created_count") or 0)
            close_skipped = int((close_result or {}).get("skipped_count") or 0)
            created_count += close_created
            skipped_order_count += close_skipped
            stale_position_count += len(stale_codes)
            settlement: dict[str, Any] = {}
            metric_recorded = False
            if close_created > 0:
                settle_orders = getattr(incubation_service, "settle_orders", None)
                if callable(settle_orders):
                    try:
                        settlement = dict(await settle_orders(db, strategy, as_of_date) or {})
                    except Exception as exc:  # noqa: BLE001
                        errors.append({
                            "strategy_id": sid,
                            "phase": "settle_orders",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        })
                    else:
                        orders_filled_count += int(settlement.get("filled_count") or 0)
                        rejected_order_count += int(settlement.get("rejected_count") or 0)
                record_metrics = getattr(incubation_service, "record_metrics", None)
                if callable(record_metrics):
                    try:
                        metric = await record_metrics(db, strategy, as_of_date)
                    except Exception as exc:  # noqa: BLE001
                        errors.append({
                            "strategy_id": sid,
                            "phase": "record_metrics",
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        })
                    else:
                        metric_recorded = metric is not None
                        if metric_recorded:
                            metrics_recorded += 1
            items.append({
                "strategy_id": sid,
                "max_holding_days": max_holding_days,
                "grace_days": grace_days,
                "max_age_days": max_age_days,
                "stale_codes": stale_codes[:20],
                "created_count": close_created,
                "skipped_count": close_skipped,
                "filled_count": int(settlement.get("filled_count") or 0),
                "rejected_count": int(settlement.get("rejected_count") or 0),
                "metric_recorded": metric_recorded,
            })

        result_status = "ok" if not errors else "partial"
        if not items and errors:
            result_status = "error"
        result = {
            "status": result_status,
            "as_of": str(as_of_date),
            "candidate_count": len(unique),
            "selected_count": len(unique),
            "stale_candidate_count": len(stale_profiles),
            "closure_selected_count": len(close_selected),
            "evaluated": len(items),
            "errors": len(errors),
            "limit": limit,
            "grace_days": grace_days,
            "stale_position_count": stale_position_count,
            "created_count": created_count,
            "skipped_order_count": skipped_order_count,
            "orders_filled": orders_filled_count,
            "rejected_orders": rejected_order_count,
            "metrics_recorded": metrics_recorded,
            "closed_round_trip_candidates": orders_filled_count,
            "skip_reasons": skip_reasons,
            "items": items[:50],
            "error_items": errors[:20],
        }
        logger.info(
            "IncubationFactory stale paper position closure: status=%s evaluated=%d "
            "stale_positions=%d created_orders=%d errors=%d",
            result_status,
            len(items),
            stale_position_count,
            created_count,
            len(errors),
        )
        return result

    async def _run_execution_audit_acceptance(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Backfill execution lineage and persist acceptance snapshots for incubation work."""
        if self.dry_run:
            return {"status": "skipped", "reason": "dry_run", "evaluated": 0}
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                execution_audit_acceptance_backfill_enabled,
                execution_audit_acceptance_batch_limit,
                execution_audit_acceptance_enabled,
            )
        except Exception:
            return {"status": "skipped", "reason": "toggle_import_failed", "evaluated": 0}
        if not execution_audit_acceptance_enabled():
            return {"status": "skipped", "reason": "disabled", "evaluated": 0}

        run_acceptance = _resolve_db_async_method(db, "run_execution_audit_acceptance")
        if run_acceptance is None:
            return {"status": "skipped", "reason": "db_method_missing", "evaluated": 0}

        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for strategy in list(strategies or []):
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            unique.append(dict(strategy or {}))

        def _safe_int(value: Any) -> int:
            try:
                return int(value or 0)
            except Exception:
                return 0

        score_capable = any(
            hasattr(db, name)
            for name in (
                "list_strategy_trade_positions",
                "list_strategy_paper_trades",
                "list_strategy_paper_orders",
                "get_signals",
            )
        )

        async def _evidence_profile(strategy: dict[str, Any]) -> dict[str, Any]:
            sid = str((strategy or {}).get("id") or "").strip()
            if not sid:
                return {
                    "score": 0,
                    "has_audit_evidence": False,
                    "has_order_intent": False,
                    "has_signal": False,
                }
            score = 0
            has_audit_evidence = False
            has_order_intent = False
            has_signal = False
            if hasattr(db, "list_strategy_paper_trades"):
                try:
                    trades = await db.list_strategy_paper_trades(sid, limit=1)
                    if trades:
                        score += 100
                        has_audit_evidence = True
                except Exception:
                    pass
            if hasattr(db, "list_strategy_trade_positions"):
                try:
                    positions = await db.list_strategy_trade_positions(
                        strategy_id=sid,
                        limit=1,
                    )
                    if positions:
                        score += 75
                        has_audit_evidence = True
                except Exception:
                    pass
            if hasattr(db, "list_strategy_paper_orders"):
                try:
                    orders = await db.list_strategy_paper_orders(sid, limit=1)
                    if orders:
                        score += 25
                        has_order_intent = True
                except Exception:
                    pass
            if hasattr(db, "get_signals"):
                try:
                    signals = await db.get_signals(sid, limit=1)
                    if signals:
                        score += 5
                        has_signal = True
                except Exception:
                    pass
            return {
                "score": score,
                "has_audit_evidence": has_audit_evidence,
                "has_order_intent": has_order_intent,
                "has_signal": has_signal,
            }

        scored: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
        awaiting_paper_execution_count = 0
        no_execution_evidence_count = 0
        for index, strategy in enumerate(unique):
            profile = await _evidence_profile(strategy)
            if score_capable and not bool(profile.get("has_audit_evidence")):
                no_execution_evidence_count += 1
                if bool(profile.get("has_order_intent")) or bool(profile.get("has_signal")):
                    awaiting_paper_execution_count += 1
            scored.append((_safe_int(profile.get("score")), -index, strategy, profile))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)

        limit = execution_audit_acceptance_batch_limit()
        execution_scored = (
            [item for item in scored if bool(item[3].get("has_audit_evidence"))]
            if score_capable
            else scored
        )
        selected = [item[2] for item in execution_scored[:limit]]
        if not selected:
            pending_execution = bool(score_capable and awaiting_paper_execution_count > 0)
            return {
                "status": "pending_execution" if pending_execution else "skipped",
                "reason": "no_execution_evidence" if pending_execution else "no_strategies",
                "healthy": False if pending_execution else True,
                "evaluated": 0,
                "candidate_count": len(unique),
                "execution_evidence_candidate_count": len(execution_scored),
                "awaiting_paper_execution_count": awaiting_paper_execution_count,
                "no_execution_evidence_count": no_execution_evidence_count,
                "limit": limit,
            }

        backfill = bool(execution_audit_acceptance_backfill_enabled())
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        status_counts: dict[str, int] = {}
        gate_status_counts: dict[str, int] = {}
        saved_signal_evidence_count = 0
        available_signal_evidence_count = 0
        proxy_backfilled_signal_count = 0
        compile_stable_signal_count = 0
        hard_gate_passed_count = 0
        overall_ready_count = 0
        native_lineage_ready_count = 0
        trade_evidence_ready_count = 0
        real_paper_round_trip_count = 0
        bootstrap_round_trip_count = 0
        closed_round_trip_count = 0
        open_position_count = 0
        estimated_round_trip_sample_debt = 0

        for strategy in selected:
            sid = str(strategy.get("id") or "").strip()
            try:
                acceptance = await run_acceptance(strategy_id=sid, backfill=backfill)
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "strategy_id": sid,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })
                continue

            matrix = dict((acceptance or {}).get("acceptance_matrix") or {})
            backfill_result = dict((acceptance or {}).get("backfill_result") or {})
            native_backfill = dict(backfill_result.get("native_signal_evidence") or {})
            verification = dict((acceptance or {}).get("verification") or {})
            coverage = dict(verification.get("coverage") or {})
            round_trip = dict(verification.get("trade_round_trip") or {})
            audit_summary = dict(
                (acceptance or {}).get("trade_audit_summary")
                or round_trip.get("audit_summary")
                or {}
            )
            status = str((acceptance or {}).get("status") or "unknown").strip() or "unknown"
            gate_status = (
                str((acceptance or {}).get("execution_audit_gate_status") or "").strip()
                or "missing"
            )
            status_counts[status] = status_counts.get(status, 0) + 1
            gate_status_counts[gate_status] = gate_status_counts.get(gate_status, 0) + 1

            signal_saved = _safe_int(native_backfill.get("saved_signal_count"))
            signal_available = max(
                _safe_int(coverage.get("strategy_signal_evidence_count")),
                signal_saved + _safe_int(native_backfill.get("initial_existing_signal_count")),
            )
            proxy_saved = _safe_int(native_backfill.get("proxy_backfilled_signal_count"))
            compile_saved = _safe_int(native_backfill.get("compile_stable_signal_count"))
            saved_signal_evidence_count += signal_saved
            available_signal_evidence_count += signal_available
            proxy_backfilled_signal_count += proxy_saved
            compile_stable_signal_count += compile_saved
            if bool((acceptance or {}).get("execution_hard_gate_passed")):
                hard_gate_passed_count += 1
            if bool(matrix.get("overall_ready")):
                overall_ready_count += 1
            if bool(matrix.get("native_lineage_ready")):
                native_lineage_ready_count += 1
            if bool(matrix.get("trade_evidence_ready")):
                trade_evidence_ready_count += 1
            strategy_real_round_trips = _safe_int(
                audit_summary.get("real_paper_round_trip_count")
                or audit_summary.get("real_paper_round_trips")
                or audit_summary.get("realized_trade_count")
            )
            strategy_bootstrap_round_trips = _safe_int(
                audit_summary.get("bootstrap_round_trip_count")
                or audit_summary.get("bootstrap_round_trips")
            )
            strategy_closed_round_trips = _safe_int(
                audit_summary.get("closed_round_trip_count")
                or audit_summary.get("total_realized_trade_count")
                or strategy_real_round_trips + strategy_bootstrap_round_trips
            )
            strategy_open_positions = _safe_int(
                audit_summary.get("open_position_count")
                or dict(round_trip.get("position_status_counts") or {}).get("open")
            )
            required_trade_count = _safe_int(
                audit_summary.get("required_trade_count")
                or dict(audit_summary.get("hard_gate_metrics") or {}).get("required_trade_count")
                or 20
            )
            real_paper_round_trip_count += strategy_real_round_trips
            bootstrap_round_trip_count += strategy_bootstrap_round_trips
            closed_round_trip_count += strategy_closed_round_trips
            open_position_count += strategy_open_positions
            if gate_status in {"bootstrap_pending", "insufficient_samples", "bootstrap_ready"}:
                estimated_round_trip_sample_debt += max(
                    0,
                    required_trade_count - strategy_real_round_trips,
                )

            items.append({
                "strategy_id": sid,
                "status": status,
                "execution_audit_gate_status": gate_status,
                "execution_hard_gate_passed": bool(
                    (acceptance or {}).get("execution_hard_gate_passed")
                ),
                "overall_ready": bool(matrix.get("overall_ready")),
                "native_lineage_ready": bool(matrix.get("native_lineage_ready")),
                "trade_evidence_ready": bool(matrix.get("trade_evidence_ready")),
                "saved_signal_evidence_count": signal_saved,
                "available_signal_evidence_count": signal_available,
                "real_paper_round_trips": strategy_real_round_trips,
                "bootstrap_round_trips": strategy_bootstrap_round_trips,
                "closed_round_trips": strategy_closed_round_trips,
                "open_positions": strategy_open_positions,
                "required_trade_count": required_trade_count,
                "gap_categories": list((acceptance or {}).get("gap_categories") or [])[:8],
                "blockers": list((acceptance or {}).get("blockers") or [])[:8],
                "execution_audit_snapshot_id": (acceptance or {}).get("execution_audit_snapshot_id"),
            })

        blockers: list[str] = []
        sample_blockers: list[str] = []
        if items and available_signal_evidence_count <= 0:
            blockers.append("signal_evidence_unavailable")
        if items and _safe_int(gate_status_counts.get("missing")) > 0:
            blockers.append("execution_audit_gate_missing")
        if items and hard_gate_passed_count <= 0:
            sample_blockers.append("execution_hard_gate_pending")
        if items and trade_evidence_ready_count <= 0:
            sample_blockers.append("trade_evidence_not_ready")

        result_status = "ok" if not errors else "partial"
        if result_status == "ok" and blockers:
            result_status = "needs_remediation"
        elif result_status == "ok" and sample_blockers:
            result_status = "pending_evidence"
        if not items and errors:
            result_status = "error"
        result = {
            "status": result_status,
            "healthy": result_status == "ok",
            "blockers": blockers,
            "sample_blockers": sample_blockers,
            "backfill": backfill,
            "candidate_count": len(unique),
            "execution_evidence_candidate_count": len(execution_scored),
            "awaiting_paper_execution_count": awaiting_paper_execution_count,
            "no_execution_evidence_count": no_execution_evidence_count,
            "selected_count": len(selected),
            "evaluated": len(items),
            "errors": len(errors),
            "limit": limit,
            "status_counts": status_counts,
            "gate_status_counts": gate_status_counts,
            "hard_gate_passed_count": hard_gate_passed_count,
            "overall_ready_count": overall_ready_count,
            "native_lineage_ready_count": native_lineage_ready_count,
            "trade_evidence_ready_count": trade_evidence_ready_count,
            "saved_signal_evidence_count": saved_signal_evidence_count,
            "available_signal_evidence_count": available_signal_evidence_count,
            "proxy_backfilled_signal_count": proxy_backfilled_signal_count,
            "compile_stable_signal_count": compile_stable_signal_count,
            "real_paper_round_trip_count": real_paper_round_trip_count,
            "bootstrap_round_trip_count": bootstrap_round_trip_count,
            "closed_round_trip_count": closed_round_trip_count,
            "open_position_count": open_position_count,
            "estimated_round_trip_sample_debt": estimated_round_trip_sample_debt,
            "items": items[:50],
            "error_items": errors[:20],
        }
        logger.info(
            "IncubationFactory execution audit acceptance: status=%s evaluated=%d "
            "saved_signal_evidence=%d available_signal_evidence=%d hard_gate_passed=%d errors=%d",
            result_status,
            len(items),
            saved_signal_evidence_count,
            available_signal_evidence_count,
            hard_gate_passed_count,
            len(errors),
        )
        return result

    async def _run_execution_audit_remediation(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
        acceptance_result: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Optionally remediate execution-audit sample/metric gaps using paper/history data."""
        if self.dry_run:
            return {"status": "skipped", "reason": "dry_run", "evaluated": 0}
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                execution_audit_acceptance_backfill_enabled,
                execution_audit_remediation_batch_limit,
                execution_audit_remediation_enabled,
                execution_audit_remediation_target_trade_count,
            )
        except Exception:
            return {"status": "skipped", "reason": "toggle_import_failed", "evaluated": 0}
        if not execution_audit_remediation_enabled():
            return {"status": "skipped", "reason": "disabled", "evaluated": 0}

        try:
            from akshare_mcp.services.strategy_acceptance_remediation import (
                get_strategy_acceptance_remediation_service,
            )
        except Exception as exc:
            return {
                "status": "skipped",
                "reason": "service_import_failed",
                "error": f"{type(exc).__name__}:{exc}",
                "evaluated": 0,
            }

        items = list(dict(acceptance_result or {}).get("items") or [])
        if not items:
            return {"status": "skipped", "reason": "no_acceptance_items", "evaluated": 0}

        limit = execution_audit_remediation_batch_limit()
        target_trade_count = execution_audit_remediation_target_trade_count()
        service = get_strategy_acceptance_remediation_service()
        run_acceptance = _resolve_db_async_method(db, "run_execution_audit_acceptance")
        backfill = bool(execution_audit_acceptance_backfill_enabled())
        sample_gap_statuses = {"bootstrap_pending", "insufficient_samples", "bootstrap_ready"}
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            sid = str((item or {}).get("strategy_id") or "").strip()
            gate_status = str((item or {}).get("execution_audit_gate_status") or "").strip()
            if not sid or sid in seen:
                continue
            if gate_status not in sample_gap_statuses and gate_status != "failed_metrics":
                continue
            seen.add(sid)
            selected.append(dict(item or {}))
            if len(selected) >= limit:
                break
        if not selected:
            return {
                "status": "skipped",
                "reason": "no_remediation_candidates",
                "evaluated": 0,
                "candidate_count": len(items),
                "limit": limit,
            }

        actions: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        bootstrap_imported_round_trips = 0
        remediation_updated_count = 0
        post_acceptance_gate_counts: dict[str, int] = {}
        for item in selected:
            sid = str(item.get("strategy_id") or "").strip()
            gate_status = str(item.get("execution_audit_gate_status") or "").strip()
            try:
                if gate_status in sample_gap_statuses:
                    action_result = await service.bootstrap_import_strategy(
                        db,
                        sid,
                        target_trade_count=target_trade_count,
                    )
                    action = "bootstrap_import"
                    bootstrap_imported_round_trips += int(
                        action_result.get("imported_round_trips") or 0
                    )
                else:
                    action_result = await service.remediate_failed_metrics_strategy(db, sid)
                    action = "failed_metrics_remediation"
                    if bool(action_result.get("updated")):
                        remediation_updated_count += 1

                post_acceptance = None
                if run_acceptance is not None:
                    post_acceptance = await run_acceptance(strategy_id=sid, backfill=backfill)
                    post_gate = (
                        str((post_acceptance or {}).get("execution_audit_gate_status") or "").strip()
                        or "missing"
                    )
                    post_acceptance_gate_counts[post_gate] = post_acceptance_gate_counts.get(post_gate, 0) + 1
                actions.append(
                    {
                        "strategy_id": sid,
                        "action": action,
                        "before_gate_status": gate_status,
                        "imported_round_trips": int(action_result.get("imported_round_trips") or 0),
                        "updated": bool(action_result.get("updated")),
                        "post_gate_status": (
                            str((post_acceptance or {}).get("execution_audit_gate_status") or "").strip()
                            if post_acceptance
                            else None
                        ),
                        "post_hard_gate_passed": bool(
                            (post_acceptance or {}).get("execution_hard_gate_passed")
                        ) if post_acceptance else None,
                    }
                )
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "strategy_id": sid,
                    "gate_status": gate_status,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                })

        result_status = "ok" if not errors else "partial"
        if not actions and errors:
            result_status = "error"
        return {
            "status": result_status,
            "candidate_count": len(items),
            "selected_count": len(selected),
            "evaluated": len(actions),
            "errors": len(errors),
            "limit": limit,
            "target_trade_count": target_trade_count,
            "bootstrap_imported_round_trips": bootstrap_imported_round_trips,
            "remediation_updated_count": remediation_updated_count,
            "post_acceptance_gate_counts": post_acceptance_gate_counts,
            "items": actions[:50],
            "error_items": errors[:20],
        }

    async def _list_incubating(self, db: Any) -> list[dict[str, Any]]:
        """加载所有孵化中的策略。"""
        if hasattr(db, "list_strategies"):
            return await db.list_strategies("incubating", limit=200)
        return []

    async def _list_paper_observation(self, db: Any) -> list[dict[str, Any]]:
        """DEV-V1 P1: 加载所有 paper observation 候选策略.

        与 _list_incubating 并列,但有独立 LIMIT (默认 50) + 优先级排序 + 反 EXISTS 边界。
        若 INCUBATION_FACTORY_PAPER_INTAKE_ENABLED=0 (默认),返回空列表,行为与改造前完全一致。
        """
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                paper_intake_enabled,
                paper_intake_batch_limit,
            )
        except Exception:
            return []
        if not paper_intake_enabled():
            return []
        for method_name in (
            "list_active_paper_observation_strategies",
            "list_paper_observation_strategies",
        ):
            method = _resolve_db_async_method(db, method_name)
            if method is None:
                continue
            try:
                return await method(limit=paper_intake_batch_limit())
            except Exception as exc:
                logger.warning(
                    "IncubationFactory: %s failed: %s", method_name, exc,
                )
                return []
        return []

    async def _list_diagnostic_observation(self, db: Any) -> list[dict[str, Any]]:
        try:
            from akshare_mcp.config._strategy_factory_toggles import (
                diagnostic_intake_enabled,
                diagnostic_intake_batch_limit,
            )
        except Exception:
            return []
        if not diagnostic_intake_enabled():
            return []
        if not hasattr(db, "list_diagnostic_observation_strategies"):
            return []
        try:
            return await db.list_diagnostic_observation_strategies(
                limit=diagnostic_intake_batch_limit(),
            )
        except Exception as exc:
            logger.warning(
                "IncubationFactory: list_diagnostic_observation_strategies failed: %s", exc,
            )
            return []

    async def _record_diagnostic_processed_event(
        self,
        db: Any,
        strategy: dict[str, Any],
        verification: dict[str, Any],
        signal_result: dict[str, Any],
    ) -> None:
        if not hasattr(db, "save_strategy_domain_event"):
            return
        try:
            await db.save_strategy_domain_event({
                "strategy_id": strategy.get("id"),
                "aggregate_type": "incubation_factory",
                "aggregate_id": str(strategy.get("id") or ""),
                "event_type": "incubation_factory.diagnostic_observation_processed",
                "source": "incubation_factory_diagnostic",
                "severity": "info",
                "payload": {
                    "strategy_name": strategy.get("name"),
                    "strategy_type": strategy.get("strategy_type"),
                    "stage": "diagnostic",
                    "diagnostic_observation": True,
                    "signals_generated": int(signal_result.get("signals_generated") or 0),
                    "primary_hit_rate": verification.get("primary_hit_rate"),
                    "primary_skill_lcb": verification.get("primary_skill_lcb"),
                    "coverage_ratio": verification.get("coverage_ratio"),
                },
            })
        except Exception as exc:
            logger.debug("IncubationFactory: diagnostic processed event failed: %s", exc)

    async def _run_pipeline(
        self,
        db: Any,
        *,
        strategies: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        """运行孵化流水线评估（复用已有实现）。"""
        if self.dry_run:
            return {"count": 0, "auto_promoted": 0, "stage_counts": {}, "items": []}

        try:
            from ..incubation_pipeline import get_strategy_incubation_pipeline_service

            pipeline = get_strategy_incubation_pipeline_service()
            return await pipeline.run_batch(
                db,
                statuses=["incubating"],
                limit=200,
                strategies=strategies,
                source="incubation_factory",
                auto_apply_review=self.auto_apply_review,
            )
        except Exception as exc:
            logger.warning("IncubationFactory: pipeline run failed: %s", exc)
            return {"count": 0, "auto_promoted": 0, "stage_counts": {}, "items": []}

    async def _heartbeat(self, db: Any, run_id: str) -> None:
        """写入健康检查心跳。"""
        if not hasattr(db, "save_strategy_domain_event"):
            return
        try:
            await db.save_strategy_domain_event({
                "strategy_id": None,
                "aggregate_type": "incubation_factory",
                "aggregate_id": "heartbeat",
                "event_type": "incubation_factory.heartbeat",
                "source": "incubation_factory",
                "severity": "info",
                "payload": {
                    "run_id": run_id,
                    "run_count": self._run_count,
                    "error_count": self._error_count,
                    "last_run_at": (
                        self._last_run_at.isoformat() if self._last_run_at else None
                    ),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            })
        except Exception as exc:
            logger.debug("IncubationFactory: heartbeat failed: %s", exc)

    async def _get_db(self) -> Any:
        """获取数据库连接。"""
        from akshare_mcp.storage import get_db

        db = get_db()
        await db.initialize()
        return db

    async def _close_db(self, db: Any) -> None:
        """关闭数据库连接。"""
        try:
            from akshare_mcp.storage import close_db
            await close_db()
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        """返回运行器状态。"""
        return {
            "run_time": str(self.run_time),
            "dry_run": self.dry_run,
            "run_count": self._run_count,
            "error_count": self._error_count,
            "last_run_at": (
                self._last_run_at.isoformat() if self._last_run_at else None
            ),
            "last_result_status": (
                self._last_result.get("status") if self._last_result else None
            ),
        }


# 单例
_runner: Optional[IncubationFactoryRunner] = None


def get_incubation_factory_runner() -> IncubationFactoryRunner:
    """获取孵化工厂运行器单例。"""
    global _runner
    if _runner is None:
        _runner = IncubationFactoryRunner()
    return _runner
