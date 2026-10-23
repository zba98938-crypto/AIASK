"""PR-D1: 模拟盘自动化桥接。

将通过 Gate-3 的策略自动接入模拟盘验证流程：
1. 策略进入 incubating 状态时，自动创建 paper_account 并绑定
2. 每日收盘后，对所有 incubating 策略生成当日信号
3. 将信号自动转为 paper_orders（通过 paper_trading_manager）
4. 每日计算 NAV 快照，跟踪实际命中率
5. 至少 60 个有效交易日后评估（仅提供候选，不执行实盘交易）

依赖的已有基础设施：
- paper_accounts / paper_orders / paper_trades / paper_nav 表
- paper_trading_manager（订单生命周期 + 风控）
- matching_engine（异步撮合）
- signal_tracker（信号跟踪 + 前向收益验证）
- strategy_signals / signal_forward_returns 表
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import statistics
import uuid
from datetime import date, datetime, time
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# 模拟盘默认配置
_DEFAULT_PAPER_CAPITAL = 100_000.0
_DEFAULT_RISK_RULES = {
    "max_position_pct": 30.0,
    "max_drawdown_pct": 20.0,
    "stop_loss_pct": 10.0,
}
_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_INCUBATION_MIN_DAYS = 60
_PROMOTION_THRESHOLDS = {
    "min_win_rate": 0.45,
    "min_profit_factor": 1.5,
    "max_drawdown": 0.25,
    "min_sharpe": 0.5,
    "min_trade_count": 30,
    "win_rate_vs_backtest_ratio": 0.80,  # 实际命中率 ≥ 回测的 80%
}
_PAPER_METRICS = (
    "actual_win_rate", "profit_factor", "total_return", "net_pnl", "sharpe",
    "max_drawdown", "net_return_ci_lower", "net_return_ci_upper",
)


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return dict(result) if isinstance(result, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _timestamp(value: Any) -> Optional[datetime]:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return result.replace(tzinfo=_MARKET_TZ) if result.tzinfo is None else result.astimezone(_MARKET_TZ)
    except (TypeError, ValueError, OverflowError):
        return None


def _trading_day(value: Any) -> Optional[date]:
    stamp = _timestamp(value)
    return stamp.date() if stamp else None


def _is_closed_day(day: date, now: datetime) -> bool:
    return day.weekday() < 5 and (day < now.date() or (day == now.date() and now.time() >= time(15)))


def _target_symbols(strategy: dict[str, Any]) -> list[str]:
    params = _mapping(strategy.get("params"))
    values = params.get("target_symbols") or strategy.get("target_symbols") or []
    if isinstance(values, str):
        values = [values]
    return list(dict.fromkeys(str(value).strip() for value in values if value is not None and str(value).strip()))


def _paper_identity(strategy: dict[str, Any]) -> dict[str, Optional[str]]:
    params = _mapping(strategy.get("params"))
    envelope = _mapping(strategy.get("resolved_candidate_envelope") or params.get("resolved_candidate_envelope"))
    return {
        key: str(strategy.get(key) or params.get(key) or envelope.get(key) or "").strip() or None
        for key in ("candidate_contract_hash", "tested_object_hash")
    }


def _signal_key(strategy_id: str, code: str, signal_date: str, identity: dict[str, Any]) -> str:
    payload = [strategy_id, code, signal_date, identity.get("candidate_contract_hash"), identity.get("tested_object_hash")]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _closed_bars(klines: list[dict[str, Any]], *, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Daily bars are only final after the exchange close, never by wall-clock date alone."""
    now = now or datetime.now(_MARKET_TZ)
    by_day: dict[date, dict[str, Any]] = {}
    for item in klines:
        bar = dict(item)
        day = _trading_day(bar.get("date") or bar.get("trade_date") or bar.get("time") or bar.get("datetime"))
        if day is None or not _is_closed_day(day, now):
            continue
        if any(str(bar.get(key)).lower() in {"false", "0"} for key in ("is_closed", "closed", "is_complete", "complete") if key in bar):
            continue
        bar["date"] = day.isoformat()
        by_day[day] = bar
    return [by_day[day] for day in sorted(by_day)]


def _closed_round_trips(trades: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reconstruct long-only positions from fills. Notional/claimed profit is never PnL."""
    positions: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, int] = {}
    seen: dict[str, dict[str, Any]] = {}

    def invalid(reason: str) -> None:
        errors[reason] = errors.get(reason, 0) + 1

    for trade in trades:
        trade_id = str(trade.get("id") or "").strip()
        if trade_id and trade_id in seen:
            if seen[trade_id] != trade:
                invalid("conflicting_fill_id")
            continue
        if trade_id:
            seen[trade_id] = trade
        position_id = str(trade.get("position_id") or "").strip()
        if not position_id:
            invalid("missing_position_id")
            continue
        positions.setdefault(position_id, []).append(trade)

    trips: list[dict[str, Any]] = []
    open_count = 0
    for position_id, fills in positions.items():
        normalized = []
        bad = False
        codes = {str(fill.get("stock_code") or fill.get("code") or "").strip() for fill in fills}
        if len(codes) != 1 or "" in codes:
            invalid("inconsistent_position_symbol")
            continue
        for fill in fills:
            side = str(fill.get("trade_type") or fill.get("fill_side") or "").lower()
            quantity = _number(fill.get("quantity"))
            price = _number(fill.get("price"))
            commission = _number(fill.get("commission"))
            stamp = _timestamp(fill.get("trade_time"))
            if side not in {"buy", "sell"} or quantity is None or quantity <= 0 or price is None or price <= 0:
                invalid("missing_actual_fill")
                bad = True
            elif commission is None or commission < 0:
                invalid("missing_fill_fees")
                bad = True
            elif stamp is None:
                invalid("missing_trade_time")
                bad = True
            else:
                normalized.append((stamp, str(fill.get("id") or ""), side, quantity, price, commission))
        if bad:
            continue
        balance = entry_cost = exit_value = 0.0
        closed = False
        closed_at = None
        for stamp, _, side, quantity, price, commission in sorted(normalized):
            if closed or (side == "sell" and quantity > balance + 1e-8):
                invalid("unpaired_exit" if not closed else "reused_position_id")
                bad = True
                break
            if side == "buy":
                balance += quantity
                entry_cost += quantity * price + commission
            else:
                balance -= quantity
                exit_value += quantity * price - commission
                if abs(balance) < 1e-8:
                    closed = True
                    closed_at = stamp
        if bad:
            continue
        if not closed:
            open_count += 1
            continue
        pnl = exit_value - entry_cost
        if not all(math.isfinite(value) for value in (entry_cost, exit_value, pnl)) or entry_cost <= 0:
            invalid("invalid_net_pnl")
            continue
        trips.append({
            "position_id": position_id,
            "net_pnl": pnl,
            "net_return": pnl / entry_cost,
            "closed_date": closed_at.date().isoformat(),
        })
    return trips, {"invalid_trade_counts": errors, "open_position_count": open_count}


def _net_return_ci(returns: list[float]) -> Optional[tuple[float, float]]:
    if len(returns) < 2:
        return None
    # Conservative two-sided Student-t 95% critical values; no optional scipy dependency.
    critical_values = {1: 12.707, 2: 4.303, 3: 3.183, 4: 2.777, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.307,
                       9: 2.263, 10: 2.229, 15: 2.132, 20: 2.086, 25: 2.060, 29: 2.046, 30: 2.043,
                       40: 2.022, 60: 2.001, 120: 1.980}
    df = len(returns) - 1
    critical = critical_values[max(key for key in critical_values if key <= df)]
    margin = critical * statistics.stdev(returns) / math.sqrt(len(returns))
    mean = statistics.mean(returns)
    return mean - margin, mean + margin


class PaperTradingBridge:
    """策略工厂 → 模拟盘的自动化桥接。

    生命周期：
    1. on_strategy_incubated() — 策略进入孵化时调用
    2. run_daily_signals() — 每日收盘后调用（由 SignalTracker 触发）
    3. evaluate_promotion() — 只评估模拟盘证据，不改变状态或执行实盘交易
    """

    def __init__(self, db: Any):
        self._db = db

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _execute(self, query: str, *args: Any) -> Any:
        execute = getattr(self._db, "execute", None)
        if callable(execute):
            return await self._maybe_await(execute(query, *args))
        acquire = getattr(self._db, "acquire", None)
        if callable(acquire):
            async with acquire() as conn:
                return await conn.execute(query, *args)
        raise AttributeError(f"{type(self._db).__name__} does not expose execute/acquire")

    async def _fetchrow(self, query: str, *args: Any) -> Optional[dict[str, Any]]:
        fetchrow = getattr(self._db, "fetchrow", None)
        if callable(fetchrow):
            row = await self._maybe_await(fetchrow(query, *args))
            return dict(row) if row else None
        acquire = getattr(self._db, "acquire", None)
        if callable(acquire):
            async with acquire() as conn:
                row = await conn.fetchrow(query, *args)
                return dict(row) if row else None
        raise AttributeError(f"{type(self._db).__name__} does not expose fetchrow/acquire")

    async def _fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        fetch = getattr(self._db, "fetch", None)
        if callable(fetch):
            rows = await self._maybe_await(fetch(query, *args))
            return [dict(row) for row in (rows or [])]
        acquire = getattr(self._db, "acquire", None)
        if callable(acquire):
            async with acquire() as conn:
                rows = await conn.fetch(query, *args)
                return [dict(row) for row in (rows or [])]
        raise AttributeError(f"{type(self._db).__name__} does not expose fetch/acquire")

    # ─── 1. 策略进入孵化时自动创建模拟盘账户 ───────────────────────────

    async def on_strategy_incubated(
        self,
        strategy: dict[str, Any],
        *,
        initial_capital: float = _DEFAULT_PAPER_CAPITAL,
    ) -> dict[str, Any]:
        """策略进入 incubating 状态时，自动创建并绑定 paper_account。"""
        strategy_id = str(strategy.get("id") or "").strip()
        strategy_name = str(strategy.get("name") or strategy.get("strategy_type") or "").strip()
        if not strategy_id:
            return {"success": False, "error": "missing_strategy_id"}

        # 检查是否已有绑定的 paper_account
        existing = await self._find_paper_account(strategy_id)
        if existing:
            logger.info(
                "PaperTradingBridge: strategy %s already has paper_account %s",
                strategy_id, existing.get("id"),
            )
            return {"success": True, "account_id": existing.get("id"), "reused": True}

        # 创建新的 paper_account
        account_id = f"paper_{strategy_id[:8]}_{uuid.uuid4().hex[:6]}"
        account_name = f"模拟盘·{strategy_name[:20]}"

        try:
            await self._execute(
                """INSERT INTO paper_accounts
                   (id, user_id, name, initial_capital, current_capital, total_value,
                    strategy_id, account_type, incubation_stage, status, risk_rules)
                   VALUES ($1, 'system', $2, $3, $4, $5, $6, 'strategy_incubation', 'warmup', 'active', $7)
                """,
                account_id,
                account_name,
                initial_capital,
                initial_capital,
                initial_capital,
                strategy_id,
                json.dumps(_DEFAULT_RISK_RULES),
            )
        except Exception as exc:
            logger.error("PaperTradingBridge: failed to create account: %s", exc)
            return {"success": False, "error": str(exc)}

        # 绑定 session
        try:
            await self._execute(
                """INSERT OR IGNORE INTO strategy_paper_sessions
                   (id, strategy_id, user_id, account_id, session_type)
                   VALUES ($1, $2, 'system', $3, 'incubation')
                """,
                f"session_{strategy_id[:8]}_{uuid.uuid4().hex[:8]}",
                strategy_id,
                account_id,
            )
        except Exception as exc:
            logger.debug("PaperTradingBridge: session binding note: %s", exc)

        logger.info(
            "PaperTradingBridge: created paper_account %s for strategy %s (capital=%.0f)",
            account_id, strategy_id, initial_capital,
        )
        return {"success": True, "account_id": account_id, "reused": False}

    # ─── 2. 每日信号生成 → 订单下达 ──────────────────────────────────────

    async def run_daily_signals(
        self,
        strategy: dict[str, Any],
        klines: list[dict[str, Any]] | dict[str, list[dict[str, Any]]],
        *,
        signal_date: Optional[str] = None,
        code: Optional[str] = None,
        place_orders: bool = True,
    ) -> dict[str, Any]:
        """Generate paper signals from closed bars, one independently loaded series per symbol.

        The legacy single-symbol list is supported. Multi-symbol callers must supply
        ``code`` or a symbol -> bars mapping; one symbol's prices must never price another.
        An explicit signal_date remains supported; the default is the latest closed bar.

        ``place_orders=False`` runs the signal path up to persistence and stops.
        The selective gate uses it to write the prediction first and only then
        decide whether the session may trade.
        """
        import numpy as np
        from aiask_quant_core.backtest import StrategyRegistry

        target_symbols = _target_symbols(strategy)
        if isinstance(klines, dict):
            results = []
            for symbol in target_symbols:
                results.append(await self.run_daily_signals(strategy, klines.get(symbol, []), signal_date=signal_date, code=symbol, place_orders=place_orders))
            return {"success": bool(results) and all(result.get("success") for result in results), "results": results}

        strategy_id = str(strategy.get("id") or "").strip()
        strategy_type = str(strategy.get("strategy_type") or "").strip()
        params = _mapping(strategy.get("params"))
        if not strategy_id or not strategy_type or not klines:
            return {"success": False, "error": "missing_required_fields"}
        target_code = str(code or (target_symbols[0] if len(target_symbols) == 1 else "")).strip()
        if not target_code:
            return {"success": False, "error": "symbol_required_for_multi_symbol"}
        if target_code not in target_symbols:
            return {"success": False, "error": "symbol_not_in_targets", "code": target_code}
        identity = _paper_identity(strategy)
        for key, value in identity.items():
            if not value:
                return {"success": False, "error": f"missing_{key}", "code": target_code}
        bars = _closed_bars(klines)
        if not bars:
            return {"success": False, "error": "no_closed_bars", "code": target_code}
        latest_bar_date = bars[-1]["date"]
        day = _trading_day(signal_date) if signal_date is not None else _trading_day(latest_bar_date)
        if day is None or day.weekday() >= 5:
            return {"success": False, "error": "invalid_signal_date", "code": target_code}
        signal_date = day.isoformat()
        key = _signal_key(strategy_id, target_code, signal_date, identity)

        # 获取策略实例
        try:
            inst, _ = StrategyRegistry.create_runtime_strategy(strategy_type, params)
        except Exception as exc:
            return {"success": False, "error": f"strategy_instantiation_failed: {exc}"}

        if inst is None:
            return {"success": False, "error": f"unknown_strategy_type: {strategy_type}"}

        # 生成信号
        close_values = [_number(bar.get("close")) for bar in bars]
        volume_values = [_number(bar.get("volume")) for bar in bars]
        if any(value is None or value <= 0 for value in close_values) or any(value is None or value < 0 for value in volume_values):
            return {"success": False, "error": "invalid_bar_values", "code": target_code}
        closes = np.array(close_values, dtype=float)
        volumes = np.array(volume_values, dtype=float)
        signals = inst.generate_signals(closes, volumes)
        if len(signals) != len(bars) or _number(signals[-1]) is None:
            return {"success": False, "error": "invalid_strategy_signals", "code": target_code}

        # 取最后一根 bar 的信号
        latest_signal = int(signals[-1])
        if latest_signal == 0:
            return {
                "success": True,
                "signal": 0,
                "action": "hold",
                "signal_date": signal_date,
                "code": target_code,
                **identity,
            }

        # 保存信号到 strategy_signals 表
        try:
            signal_metadata = {
                "latest_bar_date": latest_bar_date,
                "latest_signal_index": max(0, len(signals) - 1),
                "latest_nonzero_signal_date": latest_bar_date if latest_signal != 0 else None,
                "latest_nonzero_signal": latest_signal if latest_signal != 0 else None,
                "action_source": "paper_trading_bridge",
                "idempotency_key": key,
                **identity,
            }
            await self._execute(
                """INSERT OR IGNORE INTO strategy_signals
                   (strategy_id, signal_date, code, signal, score, action_source, signal_metadata)
                   VALUES ($1, $2, $3, $4, $5, 'paper_trading_bridge', $6)
                """,
                strategy_id,
                signal_date,
                target_code,
                latest_signal,
                float(signals[-1]),
                json.dumps(signal_metadata),
            )
            saved_signal = await self._fetchrow(
                "SELECT * FROM strategy_signals WHERE strategy_id = $1 AND signal_date = $2 AND code = $3",
                strategy_id, signal_date, target_code,
            )
            if not saved_signal or _mapping(saved_signal.get("signal_metadata")).get("idempotency_key") != key:
                return {"success": False, "error": "signal_identity_conflict", "code": target_code, "signal_date": signal_date}
            if int(saved_signal.get("signal") or 0) != latest_signal:
                return {"success": False, "error": "signal_changed_for_same_identity", "code": target_code, "signal_date": signal_date}
        except Exception as exc:
            logger.warning("PaperTradingBridge: cannot persist signal: %s", exc)
            return {"success": False, "error": "signal_persistence_failed", "code": target_code}

        # 找到绑定的 paper_account
        if not place_orders:
            return {
                "success": True,
                "signal": latest_signal,
                "action": "signal_only",
                "signal_date": signal_date,
                "code": target_code,
                "order_result": {"placed": False, "reason": "place_orders_disabled"},
                "idempotency_key": key,
                **identity,
            }
        account = await self._find_paper_account(strategy_id)
        if not account:
            return {
                "success": True,
                "signal": latest_signal,
                "action": "signal_only_no_account",
                "signal_date": signal_date,
            }

        account_id = str(account.get("id") or "")

        # 下单到模拟盘
        latest_price = float(closes[-1]) if len(closes) > 0 else 0.0
        order_result = await self._place_paper_order(
            account_id=account_id,
            code=target_code,
            signal=latest_signal,
            strategy_id=strategy_id,
            signal_date=signal_date,
            latest_price=latest_price,
            candidate_contract_hash=identity["candidate_contract_hash"],
            tested_object_hash=identity["tested_object_hash"],
            signal_id=str(saved_signal.get("id") or key),
        )

        return {
            "success": True,
            "signal": latest_signal,
            "action": "buy" if latest_signal > 0 else "sell",
            "signal_date": signal_date,
            "code": target_code,
            "order_result": order_result,
            "idempotency_key": key,
            **identity,
        }

    async def place_order_for_execution(
        self,
        *,
        strategy: dict[str, Any],
        code: str,
        signal_date: str,
        prediction_id: str,
        direction: str,
    ) -> dict[str, Any]:
        """Turn one ``ordered`` gate decision into a paper order.

        The gate (WP-C) records *what* it decided; this is the *execution*
        half it is missing. It reuses the same order path as the signal loop
        (same idempotency key, same position pairing), but is keyed by the
        prediction, and every refusal is returned — not raised — so the
        gate can record the refusal on the execution row instead of
        pretending the order happened.
        """
        strategy_id = str(strategy.get("id") or "").strip()
        signal_date = str(signal_date or "").strip()
        code = str(code or "").strip()
        # The gate passes the prediction's own direction; reading it back off the
        # strategy (``gate_direction``) coupled execution to a field the caller
        # had to stuff in, and a missing field silently refused with
        # ``direction_required`` even when the decision carried one.
        direction = str(direction or "").strip().lower()
        if not strategy_id or not signal_date or not code:
            return {"placed": False, "reason": "missing_required_fields"}
        if direction == "up":
            signal = 1
        elif direction == "down":
            signal = -1
        else:
            return {"placed": False, "reason": "direction_required"}
        identity = _paper_identity(strategy)
        for key, value in identity.items():
            if not value:
                return {"placed": False, "reason": f"missing_{key}"}
        account = await self._find_paper_account(strategy_id)
        if not account:
            return {"placed": False, "reason": "paper_account_unavailable"}
        account_id = str(account.get("id") or "")
        latest_price: Optional[float] = None
        if signal > 0:
            # Sizing the buy lot needs a real closed close; a missing price is
            # a refusal, never a default.
            try:
                bars = _closed_bars(await self._get_recent_closed_bars(code))
                latest_price = _number(bars[-1].get("close")) if bars else None
            except Exception:
                latest_price = None
            if latest_price is None or latest_price <= 0:
                return {"placed": False, "reason": "no_closed_price"}
        result = await self._place_paper_order(
            account_id=account_id,
            code=code,
            signal=signal,
            strategy_id=strategy_id,
            signal_date=signal_date,
            latest_price=latest_price or 0.0,
            candidate_contract_hash=identity["candidate_contract_hash"],
            tested_object_hash=identity["tested_object_hash"],
            action_source="selective_gate",
        )
        result["prediction_id"] = prediction_id
        return result

    async def _get_recent_closed_bars(self, code: str, limit: int = 120) -> list[dict[str, Any]]:
        try:
            get_klines = getattr(self._db, "get_klines", None)
            if callable(get_klines):
                payload = await get_klines(code, limit=limit)
                rows = payload if isinstance(payload, list) else (
                    payload.get("data") or payload.get("klines") or []
                    if isinstance(payload, dict) else []
                )
                return [dict(item) for item in rows if isinstance(item, dict)]
        except Exception:
            pass
        try:
            rows = await self._fetch(
                "SELECT * FROM kline_1d WHERE code = $1 ORDER BY time DESC LIMIT $2",
                code, limit,
            )
            return [dict(row) for row in (rows or [])]
        except Exception:
            return []

    # ─── 3. 模拟盘晋级证据评估（没有实盘执行路径） ──────────────────────

    async def evaluate_promotion(
        self,
        strategy: dict[str, Any],
    ) -> dict[str, Any]:
        """Fail-closed paper-only assessment using net, completely closed positions.

        NAV supplies unique observation dates and mark-to-market drawdown (risk).
        Return, win rate, profit factor, CI and Sharpe use realized fills net of fees,
        not sell notional, claimed profit, or unrealized NAV gains.
        """
        strategy_id = str(strategy.get("id") or "").strip()
        account = await self._find_paper_account(strategy_id)
        if not account:
            return {
                "eligible": False, "reason": "no_paper_account", "reasons": ["no_paper_account"],
                **dict.fromkeys(_PAPER_METRICS),
                "metric_reasons": dict.fromkeys(_PAPER_METRICS, "no_paper_account"),
            }

        account_id = str(account.get("id") or "")

        identity = _paper_identity(strategy)
        reasons: list[str] = []
        try:
            nav_rows = await self._get_nav_history(account_id)
        except Exception as exc:
            logger.warning("PaperTradingBridge: NAV evidence unavailable for %s: %s", account_id, exc)
            nav_rows = []
            reasons.append("nav_history_unavailable")
        try:
            trades = await self._get_trades(account_id)
        except Exception as exc:
            logger.warning("PaperTradingBridge: fill evidence unavailable for %s: %s", account_id, exc)
            trades = []
            reasons.append("trade_history_unavailable")
        metric_reasons: dict[str, str] = {}
        for name, value in identity.items():
            if value is None:
                reasons.append(f"missing_{name}")
        identity_errors = 0
        if all(identity.values()):
            for trade in trades:
                metadata = _mapping(trade.get("order_metadata") or trade.get("reason"))
                if any((trade.get(name) or metadata.get(name)) != value for name, value in identity.items()):
                    identity_errors += 1
            if identity_errors:
                reasons.append("unbound_or_mismatched_trade_identity")

        now = datetime.now(_MARKET_TZ)
        nav_by_day: dict[str, float] = {}
        invalid_nav_count = 0
        for row in nav_rows:
            day = _trading_day(row.get("nav_date"))
            value = _number(row.get("total_value"))
            if day is None or value is None or value <= 0:
                invalid_nav_count += 1
            elif _is_closed_day(day, now) and row.get("is_trading_day") not in (False, 0):
                nav_by_day[day.isoformat()] = value
        dates = sorted(nav_by_day)
        navs = [nav_by_day[day] for day in dates]
        if invalid_nav_count:
            reasons.append("invalid_nav_rows")
        if len(dates) < _INCUBATION_MIN_DAYS:
            reasons.append("insufficient_valid_trading_days")

        trips, diagnostics = _closed_round_trips(trades)
        trade_error = "incomplete_trade_data" if diagnostics["invalid_trade_counts"] else None
        if identity_errors:
            trade_error = "unbound_or_mismatched_trade_identity"
        if trade_error:
            reasons.append(trade_error)
        if len(trips) < _PROMOTION_THRESHOLDS["min_trade_count"]:
            reasons.append("insufficient_closed_round_trips")

        metrics: dict[str, Optional[float]] = dict.fromkeys(_PAPER_METRICS)
        trade_metric_names = set(metrics) - {"max_drawdown"}
        capital = _number(account.get("initial_capital"))
        if not trips or trade_error:
            for name in trade_metric_names:
                metric_reasons[name] = trade_error or "no_closed_round_trips"
        else:
            pnls = [trip["net_pnl"] for trip in trips]
            returns = [trip["net_return"] for trip in trips]
            metrics["net_pnl"] = sum(pnls)
            metrics["actual_win_rate"] = sum(pnl > 0 for pnl in pnls) / len(pnls)
            losses = -sum(pnl for pnl in pnls if pnl < 0)
            if losses > 0:
                metrics["profit_factor"] = sum(pnl for pnl in pnls if pnl > 0) / losses
            else:
                metric_reasons["profit_factor"] = "no_losing_round_trips"
            ci = _net_return_ci(returns)
            if ci is None:
                metric_reasons["net_return_ci_lower"] = metric_reasons["net_return_ci_upper"] = "insufficient_return_samples"
            else:
                metrics["net_return_ci_lower"], metrics["net_return_ci_upper"] = ci
            if capital is None or capital <= 0:
                metric_reasons["total_return"] = metric_reasons["sharpe"] = "missing_initial_capital"
            else:
                metrics["total_return"] = sum(pnls) / capital
                if any(trip["closed_date"] not in nav_by_day for trip in trips):
                    metric_reasons["sharpe"] = "missing_nav_for_closed_trade"
                elif len(dates) < 3:
                    metric_reasons["sharpe"] = "insufficient_daily_returns"
                else:
                    daily_pnl = dict.fromkeys(dates, 0.0)
                    for trip in trips:
                        daily_pnl[trip["closed_date"]] += trip["net_pnl"]
                    equity = capital
                    daily_returns = []
                    for day in dates:
                        if equity <= 0:
                            break
                        daily_returns.append(daily_pnl[day] / equity)
                        equity += daily_pnl[day]
                    deviation = statistics.stdev(daily_returns) if len(daily_returns) > 1 else 0.0
                    if equity <= 0:
                        metric_reasons["sharpe"] = "nonpositive_realized_equity"
                    elif deviation <= 0:
                        metric_reasons["sharpe"] = "zero_return_variance"
                    else:
                        metrics["sharpe"] = statistics.mean(daily_returns) / deviation * math.sqrt(252)

        if len(navs) < 2:
            metric_reasons["max_drawdown"] = "insufficient_valid_nav"
        else:
            peak = navs[0]
            max_dd = 0.0
            for value in navs:
                peak = max(peak, value)
                max_dd = max(max_dd, (peak - value) / peak)
            metrics["max_drawdown"] = max_dd

        for name, value in metrics.items():
            if value is not None and not math.isfinite(value):
                metrics[name] = None
                metric_reasons[name] = "nonfinite_metric"
                reasons.append(f"missing_{name}")

        params = _mapping(strategy.get("params"))
        backtest_win_rate = _number(params.get("backtest_win_rate", params.get("win_rate")))
        if backtest_win_rate is None or not 0 <= backtest_win_rate <= 1:
            backtest_win_rate = None
            metric_reasons["backtest_win_rate"] = "missing_backtest_win_rate"
            reasons.append("missing_backtest_win_rate")

        thresholds = dict(_PROMOTION_THRESHOLDS)
        risk_rules = _mapping(account.get("risk_rules"))
        risk_dd = _number(risk_rules.get("max_drawdown_pct"))
        if "max_drawdown_pct" in risk_rules:
            if risk_dd is not None and 0 <= risk_dd <= 100:
                thresholds["max_drawdown"] = min(thresholds["max_drawdown"], risk_dd / 100)
            else:
                reasons.append("invalid_risk_drawdown_threshold")
        for name, threshold, maximum in (
            ("actual_win_rate", thresholds["min_win_rate"], False),
            ("profit_factor", thresholds["min_profit_factor"], False),
            ("sharpe", thresholds["min_sharpe"], False),
            ("max_drawdown", thresholds["max_drawdown"], True),
        ):
            value = metrics[name]
            if value is None:
                reasons.append(f"missing_{name}")
            elif (value > threshold if maximum else value < threshold):
                reasons.append(f"weak_{name}")
        if metrics["net_return_ci_lower"] is None:
            reasons.append("missing_net_return_ci")
        elif metrics["net_return_ci_lower"] <= 0:
            reasons.append("nonpositive_net_return_ci_lower")
        if metrics["total_return"] is None:
            reasons.append("missing_total_return")
        if backtest_win_rate is not None and metrics["actual_win_rate"] is not None:
            if metrics["actual_win_rate"] < backtest_win_rate * thresholds["win_rate_vs_backtest_ratio"]:
                reasons.append("weak_win_rate_vs_backtest")

        reasons = list(dict.fromkeys(reasons))
        paper_evidence = {
            "trial_started": bool(dates or trades),
            "evidence_track": "real",
            "candidate_hash_bound": bool(all(identity.values()) and identity_errors == 0 and trade_error is None),
            "valid_trading_days": len(dates),
            "closed_round_trip_count": len(trips),
            "nav_observations": [
                {"date": day, "nav": nav_by_day[day], "derived": False}
                for day in dates
            ],
            "closed_round_trips": [
                {**trip, "status": "closed", "derived": False}
                for trip in trips
            ],
            "evidence_refs": [
                f"paper_fill:{str(trade.get('id') or '').strip()}"
                for trade in trades
                if str(trade.get("id") or "").strip()
            ],
            "evidence_ref": f"paper_account:{account_id}",
            "candidate_contract_hash": identity.get("candidate_contract_hash"),
            "tested_object_hash": identity.get("tested_object_hash"),
            "invalid_nav_count": invalid_nav_count,
            "identity_errors": identity_errors,
            "trade_diagnostics": diagnostics,
        }
        return {
            "eligible": not reasons,
            "reason": reasons[0] if reasons else None,
            "reasons": reasons,
            "days_active": len(dates),
            "trade_count": len(trips),
            "closed_round_trip_count": len(trips),
            "backtest_win_rate": backtest_win_rate,
            **metrics,
            "metric_reasons": metric_reasons,
            "invalid_nav_count": invalid_nav_count,
            "unbound_or_mismatched_trade_count": identity_errors,
            **diagnostics,
            "thresholds": {**thresholds, "min_valid_trading_days": _INCUBATION_MIN_DAYS, "net_return_ci_lower": 0.0},
            "net_return_ci_basis": "mean_closed_round_trip_net_return_student_t_95",
            "sharpe_basis": "daily_closed_round_trip_net_pnl",
            "paper_evidence": paper_evidence,
            **identity,
        }

    # ─── 内部方法 ─────────────────────────────────────────────────────────

    async def _find_paper_account(self, strategy_id: str) -> Optional[dict[str, Any]]:
        """查找策略绑定的 paper_account。"""
        try:
            row = await self._fetchrow(
                "SELECT * FROM paper_accounts WHERE strategy_id = $1 AND status = 'active' LIMIT 1",
                strategy_id,
            )
            return dict(row) if row else None
        except Exception as _swallowed:
            logger.warning("packages/strategy-factory/src/strategy_factory/application/research/paper_trading_bridge.py:388: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            return None

    async def _find_existing_bridge_order(
        self,
        *,
        strategy_id: str,
        signal_date: str,
        code: str,
        direction: str = "",
    ) -> Optional[dict[str, Any]]:
        strategy_id = str(strategy_id or "").strip()
        signal_date = str(signal_date or "").strip()
        code = str(code or "").strip()
        if not strategy_id or not signal_date or not code:
            return None
        return await self._fetchrow(
            """SELECT * FROM paper_orders
                   WHERE source = 'paper_trading_bridge'
                     AND strategy_id = $1
                     AND signal_date = $2
                     AND code = $3
                   ORDER BY created_at ASC, id ASC
                   LIMIT 1
            """,
            strategy_id,
            signal_date,
            code,
        )

    @staticmethod
    def _reused_order_result(row: dict[str, Any], *, direction: str) -> dict[str, Any]:
        return {
            "placed": False,
            "reused": True,
            "order_id": str(row.get("id") or ""),
            "direction": str(row.get("direction") or direction),
            "shares": int(row.get("shares") or 0),
            "status": row.get("status"),
        }

    async def _place_paper_order(
        self,
        *,
        account_id: str,
        code: str,
        signal: int,
        strategy_id: str,
        signal_date: str,
        latest_price: float = 0.0,
        candidate_contract_hash: Optional[str] = None,
        tested_object_hash: Optional[str] = None,
        signal_id: Optional[str] = None,
        action_source: str = "paper_trading_bridge",
    ) -> dict[str, Any]:
        """向模拟盘下单。"""
        account_id = str(account_id or "").strip()
        code = str(code or "").strip()
        strategy_id = str(strategy_id or "").strip()
        signal_date = str(signal_date or "").strip()
        direction = "buy" if signal > 0 else "sell"
        identity = {"candidate_contract_hash": candidate_contract_hash, "tested_object_hash": tested_object_hash}
        key = _signal_key(strategy_id, code, signal_date, identity)
        metadata = {**identity, "idempotency_key": key, "action_source": action_source}
        try:
            existing = await self._find_existing_bridge_order(strategy_id=strategy_id, signal_date=signal_date, code=code)
        except Exception as exc:
            logger.warning("PaperTradingBridge: cannot check order idempotency: %s", exc)
            return {"placed": False, "reason": "idempotency_lookup_failed"}
        if existing:
            # The legacy unique index has no hash column. Never reinterpret an old
            # order as evidence for a different candidate, or overwrite its binding.
            existing_key = _mapping(existing.get("reason")).get("idempotency_key")
            if existing_key != key:
                return {"placed": False, "reason": "order_identity_conflict"}
            return self._reused_order_result(existing, direction=direction)

        position_id = f"paper_pos_{key}"
        if direction == "sell":
            # 检查是否有持仓可卖
            try:
                pos = await self._fetchrow(
                    "SELECT quantity FROM paper_positions WHERE account_id = $1 AND stock_code = $2",
                    account_id, code,
                )
                if not pos or int(pos.get("quantity") or 0) <= 0:
                    return {"placed": False, "reason": "no_position_to_sell"}
                shares = int(pos["quantity"])
                linked = await self._fetch(
                    """SELECT position_id, remaining_shares FROM strategy_trade_positions
                       WHERE account_id = $1 AND strategy_id = $2 AND code = $3
                         AND status = 'open' AND remaining_shares > 0""",
                    account_id, strategy_id, code,
                )
                if len(linked) != 1 or int(linked[0].get("remaining_shares") or 0) != shares:
                    return {"placed": False, "reason": "unpaired_position_to_sell"}
                position_id = str(linked[0]["position_id"])
            except Exception:
                return {"placed": False, "reason": "position_check_failed"}
        else:
            # 买入：用账户 30% 资金，按最新价估算可买股数（A 股 100 股整手）
            try:
                held = await self._fetchrow(
                    "SELECT quantity FROM paper_positions WHERE account_id = $1 AND stock_code = $2",
                    account_id, code,
                )
                if held and int(held.get("quantity") or 0) > 0:
                    return {"placed": False, "reason": "already_open_position"}
                acct = await self._fetchrow(
                    "SELECT current_capital FROM paper_accounts WHERE id = $1",
                    account_id,
                )
                capital = float(acct.get("current_capital") or 0) if acct else 0
                if capital <= 0:
                    return {"placed": False, "reason": "no_capital"}
                price = _number(latest_price)
                if price is None or price <= 0:
                    return {"placed": False, "reason": "no_price"}
                budget = capital * 0.30
                shares = int(budget / price) // 100 * 100
                if shares < 100:
                    return {"placed": False, "reason": "insufficient_cash_for_one_lot"}
            except Exception:
                return {"placed": False, "reason": "capital_check_failed"}

        # 插入 pending 订单（由 matching_engine 撮合）
        try:
            row = await self._fetchrow(
                """INSERT INTO paper_orders
                   (account_id, code, direction, shares, status, order_type,
                    strategy_id, signal_date, source, signal_id, position_id, reason)
                   VALUES ($1, $2, $3, $4, 'pending', 'market', $5, $6, 'paper_trading_bridge', $7, $8, $9)
                   RETURNING *
                """,
                account_id, code, direction, shares,
                strategy_id, signal_date,
                signal_id or key, position_id, json.dumps(metadata),
            )
            order = dict(row or {})
            order_id = str(order.get("id") or "")
            if not order_id:
                raise ValueError("missing_persisted_order")
            return {"placed": True, "order_id": order_id, "direction": direction, "shares": shares, "idempotency_key": key, **identity}
        except Exception as exc:
            try:
                existing = await self._find_existing_bridge_order(strategy_id=strategy_id, signal_date=signal_date, code=code)
            except Exception:
                existing = None
            if existing and _mapping(existing.get("reason")).get("idempotency_key") == key:
                return self._reused_order_result(existing, direction=direction)
            logger.warning("PaperTradingBridge: order persistence failed: %s", exc)
            return {"placed": False, "reason": "order_persistence_failed"}

    async def _get_nav_history(self, account_id: str) -> list[dict[str, Any]]:
        """获取 NAV 历史。"""
        return await self._fetch(
            "SELECT * FROM paper_nav WHERE account_id = $1 ORDER BY nav_date",
            account_id,
        )

    async def _get_trades(self, account_id: str) -> list[dict[str, Any]]:
        """获取交易记录。"""
        return await self._fetch(
            """SELECT t.*, o.reason AS order_metadata
                   FROM paper_trades t LEFT JOIN paper_orders o ON CAST(o.id AS TEXT) = t.source_order_id
                   WHERE t.account_id = $1 ORDER BY t.trade_time, t.id""",
            account_id,
        )


__all__ = ["PaperTradingBridge"]
