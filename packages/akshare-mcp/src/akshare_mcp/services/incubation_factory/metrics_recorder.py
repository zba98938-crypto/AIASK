"""孵化工厂 · 指标记录模块。

负责将每日孵化验证结果写入 strategy_incubation_metrics 表。
"""

from __future__ import annotations

import logging
import math
import os
from datetime import date, datetime, timezone
from typing import Any, Optional

from .observation_lifecycle_policy import ObservationLifecyclePolicy

logger = logging.getLogger(__name__)


def _regime_lifecycle_enabled() -> bool:
    """INVERT-DESIGN P2/P3：observe 决策是否纳入 regime 维度（默认 OFF，零变化）。"""
    return str(
        os.getenv("STRATEGY_FACTORY_PROMOTION_CROSS_REGIME_ENABLED") or ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError) as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/metrics_recorder.py:32: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return None
    return numeric if math.isfinite(numeric) else None


def _safe_float(value: Any, default: Any = 0.0) -> Any:
    numeric = _finite_float(value)
    if numeric is not None:
        return numeric
    if default is None:
        return None
    fallback = _finite_float(default)
    return fallback if fallback is not None else 0.0


def _safe_int(value: Any, default: int = 0) -> int:
    numeric = _finite_float(value)
    if numeric is not None:
        return int(numeric)
    fallback = _finite_float(default)
    return int(fallback if fallback is not None else 0)


def _json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    return value


class MetricsRecorder:
    """每日孵化指标记录器。

    将前向验证结果和策略运行状态写入 strategy_incubation_metrics 表，
    供策略工厂的 IncubationBudgeter 读取作为反馈。
    """

    async def record(
        self,
        db: Any,
        strategy: dict[str, Any],
        verification: dict[str, Any],
        *,
        metric_date: Optional[date] = None,
    ) -> Optional[dict[str, Any]]:
        """
        记录策略的每日孵化指标。

        Args:
            db: 数据库连接
            strategy: 策略记录
            verification: ForwardVerifier 的验证结果
            metric_date: 指标日期（默认今天）

        Returns:
            保存的指标记录，或 None（如果保存失败）
        """
        sid = str(strategy.get("id") or "").strip()
        if not sid:
            return None

        if metric_date is None:
            from .session_date import session_date_or_today

            metric_date = await session_date_or_today(db)
        today = metric_date

        # 获取孵化账户信息
        account = None
        if hasattr(db, "get_strategy_incubation_account"):
            account = await db.get_strategy_incubation_account(sid)

        account_id = str((account or {}).get("account_id") or "").strip()

        # 获取 paper account 的 NAV 信息
        nav_info = await self._get_nav_info(db, account_id, today)

        # 若 verification 缺 skill/n，用 get_signal_stats 回填，避免 metrics 落空
        verification = dict(verification or {})
        need_stats = any(
            verification.get(k) is None
            for k in (
                'primary_skill_lcb',
                'recent_primary_skill_lcb',
                'primary_effective_n',
                'secondary_effective_n',
                'coverage_ratio',
            )
        )
        if need_stats and hasattr(db, 'get_signal_stats'):
            try:
                stats = await db.get_signal_stats(sid)
                by = dict((stats or {}).get('by_horizon') or {})
                h5 = dict(by.get('5') or {})
                h10 = dict(by.get('10') or {})
                skill_map = (stats or {}).get('skill_lcb') or {}
                if verification.get('primary_skill_lcb') is None:
                    verification['primary_skill_lcb'] = h5.get('skill_lcb') or (skill_map.get(5) if isinstance(skill_map, dict) else None)
                if verification.get('recent_primary_skill_lcb') is None:
                    verification['recent_primary_skill_lcb'] = h5.get('recent_skill_lcb') or verification.get('primary_skill_lcb')
                if verification.get('primary_effective_n') is None:
                    verification['primary_effective_n'] = h5.get('effective_n')
                if verification.get('secondary_skill_lcb') is None:
                    verification['secondary_skill_lcb'] = h10.get('skill_lcb') or (skill_map.get(10) if isinstance(skill_map, dict) else None)
                if verification.get('secondary_effective_n') is None:
                    verification['secondary_effective_n'] = h10.get('effective_n')
                if verification.get('primary_hit_rate') is None:
                    verification['primary_hit_rate'] = h5.get('hit_rate')
                if verification.get('forward_sharpe') is None:
                    verification['forward_sharpe'] = h5.get('forward_sharpe')
                if verification.get('forward_ic') is None:
                    verification['forward_ic'] = h5.get('forward_ic')
                if verification.get('total_signals') is None:
                    verification['total_signals'] = (stats or {}).get('total_signals') or (stats or {}).get('raw_signal_count')
                if verification.get('coverage_ratio') is None:
                    raw = float((stats or {}).get('raw_signal_count') or (stats or {}).get('total_signals') or 0)
                    with_fwd = float((stats or {}).get('signals_with_forward_returns_count') or 0)
                    verification['coverage_ratio'] = (with_fwd / raw) if raw > 0 else None
            except Exception as exc:
                logger.debug('MetricsRecorder: signal stats enrichment failed for %s: %s', sid, exc)

        # 构建指标
        metric = self._build_metric(
            strategy=strategy,
            verification=verification,
            nav_info=nav_info,
            account_id=account_id,
            metric_date=today,
        )

        # 保存到数据库
        if hasattr(db, "save_strategy_incubation_metric"):
            try:
                saved = await db.save_strategy_incubation_metric(
                    sid, today, metric
                )
                logger.debug(
                    "MetricsRecorder: saved metric for %s on %s",
                    sid,
                    today,
                )
                return saved
            except Exception as exc:
                logger.warning(
                    "MetricsRecorder: save failed for %s: %s", sid, exc
                )
                return None

        return metric

    async def record_batch(
        self,
        db: Any,
        strategies: list[dict[str, Any]],
        verifications: dict[str, dict[str, Any]],
        *,
        metric_date: Optional[date] = None,
    ) -> dict[str, Any]:
        """
        批量记录孵化指标。

        Args:
            db: 数据库连接
            strategies: 策略列表
            verifications: {strategy_id: verification_result} 映射
            metric_date: 指标日期

        Returns:
            批量记录结果摘要
        """
        recorded = 0
        failed = 0

        for strategy in strategies:
            sid = str(strategy.get("id") or "").strip()
            verification = verifications.get(sid, {})
            result = await self.record(
                db, strategy, verification, metric_date=metric_date
            )
            if result is not None:
                recorded += 1
            else:
                failed += 1

        return {
            "total": len(strategies),
            "recorded": recorded,
            "failed": failed,
            "metric_date": str(metric_date or date.today()),
        }

    def _build_metric(
        self,
        *,
        strategy: dict[str, Any],
        verification: dict[str, Any],
        nav_info: dict[str, Any],
        account_id: str,
        metric_date: date,
    ) -> dict[str, Any]:
        verification = dict(verification or {})
        nav_info = dict(nav_info or {})
        # Only coerce fields that are present and non-null. Keep missing skill/n as None
        # so DB metrics can distinguish "unknown" from true zero.
        for field in (
            "primary_hit_rate",
            "primary_skill_lcb",
            "recent_primary_skill_lcb",
            "secondary_hit_rate",
            "secondary_skill_lcb",
            "stability_gap",
            "coverage_ratio",
            "forward_sharpe",
            "forward_ic",
            "recent_primary_hit_rate",
        ):
            if field in verification and verification.get(field) is not None:
                verification[field] = _safe_float(verification.get(field), 0.0)
        for field in (
            "primary_effective_n",
            "secondary_effective_n",
            "total_signals",
            "min_days_remaining",
            "min_trades_remaining",
        ):
            if field in verification and verification.get(field) is not None:
                verification[field] = _safe_int(verification.get(field), 0)
        for field, default in (
            ("total_value", 0.0),
            ("cash", 0.0),
            ("market_value", 0.0),
            ("nav", 1.0),
            ("daily_return", 0.0),
            ("max_drawdown", 0.0),
        ):
            if field in nav_info:
                nav_info[field] = _safe_float(nav_info.get(field), default)
        # 从验证结果提取
        def _opt_float(key: str):
            if key not in verification or verification.get(key) is None:
                return None
            try:
                return float(verification.get(key))
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/metrics_recorder.py:276: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                return None

        def _opt_int(key: str):
            if key not in verification or verification.get(key) is None:
                return None
            try:
                return int(verification.get(key))
            except Exception as _swallowed:
                logger.warning("packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/metrics_recorder.py:284: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
                return None

        primary_hit_rate = float(verification.get("primary_hit_rate") or 0.0)
        primary_skill_lcb = _opt_float("primary_skill_lcb")
        recent_primary_skill_lcb = _opt_float("recent_primary_skill_lcb")
        secondary_hit_rate = float(verification.get("secondary_hit_rate") or 0.0)
        secondary_skill_lcb = _opt_float("secondary_skill_lcb")
        stability_gap = _opt_float("stability_gap")
        coverage_ratio = _opt_float("coverage_ratio")
        forward_sharpe = float(verification.get("forward_sharpe") or 0.0)
        forward_ic = float(verification.get("forward_ic") or 0.0)
        recent_primary_hit_rate = (
            float(verification.get("recent_primary_hit_rate"))
            if verification.get("recent_primary_hit_rate") is not None
            else primary_hit_rate
        )
        primary_n = _opt_int("primary_effective_n")
        secondary_n = _opt_int("secondary_effective_n")
        total_signals = int(verification.get("total_signals") or primary_n or 0)

        # 从 NAV 信息提取
        total_value = float(nav_info.get("total_value") or 0.0)
        cash = float(nav_info.get("cash") or 0.0)
        market_value = float(nav_info.get("market_value") or 0.0)
        nav = float(nav_info.get("nav") or 1.0)
        daily_return = float(nav_info.get("daily_return") or 0.0)
        max_drawdown = float(nav_info.get("max_drawdown") or 0.0)

        # 决策判定（缺测字段时用保守默认，不把 None 当 0 写入库）
        decision = self._derive_decision(
            primary_skill_lcb=float(primary_skill_lcb or 0.0),
            recent_primary_skill_lcb=float(recent_primary_skill_lcb or 0.0),
            stability_gap=float(stability_gap or 0.0),
            coverage_ratio=float(coverage_ratio or 0.0),
            primary_n=int(primary_n or 0),
            hit_rate_by_regime=verification.get("hit_rate_by_regime"),
        )

        incubation_account = strategy.get("incubation_account")
        if not isinstance(incubation_account, dict):
            incubation_account = {}
        intake_stage = str(
            strategy.get("_intake_stage")
            or incubation_account.get("stage")
            or strategy.get("status")
            or "incubating"
        )
        diagnostic_observation = intake_stage == "diagnostic"

        return _json_safe({
            "account_id": account_id,
            "stage": intake_stage,
            "total_value": total_value,
            "cash": cash,
            "market_value": market_value,
            "nav": nav,
            "daily_return": daily_return,
            "max_drawdown": max_drawdown,
            "sharpe_ratio": forward_sharpe,
            "hit_rate_5d": primary_hit_rate,
            "hit_rate_lcb_5d": primary_skill_lcb,
            "skill_lcb_5d": primary_skill_lcb,
            "hit_rate_10d": secondary_hit_rate,
            "skill_lcb_10d": secondary_skill_lcb,
            "effective_n_5d": primary_n,
            "recent_hit_rate_5d": recent_primary_hit_rate,
            "recent_skill_lcb_5d": recent_primary_skill_lcb,
            "stability_gap_5d": stability_gap,
            "forward_ic_5d": forward_ic,
            "forward_sharpe_5d": forward_sharpe,
            "total_signals": total_signals,
            "coverage_ratio": coverage_ratio,
            "primary_effective_n": primary_n,
            "secondary_effective_n": secondary_n,
            "decision": decision,
            "total_orders": 0,
            "total_trades": 0,
            "metadata": {
                "source": "incubation_factory",
                "intake_stage": intake_stage,
                "diagnostic_observation": diagnostic_observation,
                "profile": verification.get("profile"),
                "primary_horizon": verification.get("primary_horizon"),
                "secondary_horizon": verification.get("secondary_horizon"),
                "coverage_ratio": coverage_ratio,
                "secondary_hit_rate": secondary_hit_rate,
                "secondary_skill_lcb": secondary_skill_lcb,
                "secondary_effective_n": secondary_n,
                "stability_gap": stability_gap,
                "forward_ic": forward_ic,
                "forward_sharpe": forward_sharpe,
                "min_days_remaining": verification.get("min_days_remaining"),
                "min_trades_remaining": verification.get("min_trades_remaining"),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        })

    def _derive_decision(
        self,
        *,
        primary_skill_lcb: float,
        recent_primary_skill_lcb: float,
        stability_gap: float,
        coverage_ratio: float,
        primary_n: int,
        hit_rate_by_regime: Optional[dict] = None,
    ) -> str:
        """
        根据指标判定当日决策（委托 ObservationLifecyclePolicy，INVERT-DESIGN P2 改动C）。

        - promote: 表现优秀，推荐晋升
        - observe: 表现一般，继续观察
        - halt: 表现差，建议暂停

        阈值默认值与历史内联实现一致；regime 维度默认 OFF（toggle 控制），保证零行为变化。
        """
        # Profile-aware observe promotion sample thresholds.
        try:
            import os as _os
            from ..factor_mining_factory.quality import QUALITY_PROFILE_ACTIVE as _obs_profile
        except Exception:
            _os = os
            _obs_profile = "strict"
        _lite = str(_obs_profile or "strict").lower() in {"lite", "minimum"}
        policy = ObservationLifecyclePolicy(
            regime_enabled=_regime_lifecycle_enabled(),
            min_primary_n=int(_os.getenv("INCUBATION_OBSERVE_MIN_PRIMARY_N", "8" if _lite else "10") or (8 if _lite else 10)),
            promote_skill_lcb=float(_os.getenv("INCUBATION_OBSERVE_PROMOTE_SKILL_LCB", "0.01" if _lite else "0.02") or (0.01 if _lite else 0.02)),
            promote_min_coverage=float(_os.getenv("INCUBATION_OBSERVE_PROMOTE_MIN_COVERAGE", "0.45" if _lite else "0.60") or (0.45 if _lite else 0.60)),
            promote_max_stability_gap=float(_os.getenv("INCUBATION_OBSERVE_PROMOTE_MAX_STABILITY_GAP", "0.08" if _lite else "0.05") or (0.08 if _lite else 0.05)),
        )
        result = policy.decide(
            primary_skill_lcb=primary_skill_lcb,
            recent_primary_skill_lcb=recent_primary_skill_lcb,
            stability_gap=stability_gap,
            coverage_ratio=coverage_ratio,
            primary_n=primary_n,
            hit_rate_by_regime=hit_rate_by_regime,
        )
        return result.decision

    async def _get_nav_info(
        self, db: Any, account_id: str, metric_date: date
    ) -> dict[str, Any]:
        """获取 paper account 的 NAV 信息。"""
        if not account_id:
            return {}

        try:
            account = None
            if hasattr(db, "get_paper_account"):
                account = await db.get_paper_account(account_id)
            nav_rows = []
            if hasattr(db, "get_paper_nav_rows"):
                nav_rows = await db.get_paper_nav_rows(account_id, limit=60)
            if nav_rows:
                latest = dict(nav_rows[0] or {})
                total_value = self._safe_float(latest.get("total_value"))
                cash = self._safe_float(latest.get("cash"))
                market_value = self._safe_float(latest.get("market_value"))
                daily_return = self._safe_float(latest.get("daily_return"), None)
                if daily_return is None and len(nav_rows) >= 2:
                    prev_total = self._safe_float(dict(nav_rows[1] or {}).get("total_value"), None)
                    if prev_total and prev_total > 0:
                        daily_return = (total_value - prev_total) / prev_total
                initial_capital = self._safe_float((account or {}).get("initial_capital"))
                if not initial_capital or initial_capital <= 0:
                    initial_capital = self._safe_float(dict(nav_rows[-1] or {}).get("total_value")) or 100000.0
                return {
                    "total_value": total_value,
                    "cash": cash,
                    "market_value": market_value,
                    "nav": total_value / max(initial_capital, 1.0),
                    "daily_return": daily_return or 0.0,
                    "max_drawdown": self._compute_max_drawdown(nav_rows),
                }

            if account:
                total_value = self._safe_float(account.get("total_value"))
                cash = self._safe_float(account.get("current_capital"))
                initial_capital = self._safe_float(account.get("initial_capital")) or 100000.0
                return {
                    "total_value": total_value,
                    "cash": cash,
                    "market_value": total_value - cash,
                    "nav": total_value / max(initial_capital, 1.0),
                    "daily_return": 0.0,
                    "max_drawdown": 0.0,
                }
        except Exception as exc:
            logger.debug("MetricsRecorder: get NAV failed for %s: %s", account_id, exc)

        return {}

    @staticmethod
    def _safe_float(value: Any, default: Any = 0.0) -> Any:
        return _safe_float(value, default)

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        return _safe_int(value, default)

    @classmethod
    def _compute_max_drawdown(cls, nav_rows: list[dict[str, Any]]) -> float:
        values = [
            cls._safe_float(dict(row or {}).get("total_value"))
            for row in reversed(list(nav_rows or []))
        ]
        values = [value for value in values if value > 0]
        if len(values) < 2:
            return 0.0
        peak = values[0]
        max_drawdown = 0.0
        for value in values:
            peak = max(peak, value)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - value) / peak)
        return max_drawdown
