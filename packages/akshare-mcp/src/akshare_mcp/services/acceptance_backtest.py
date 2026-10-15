from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import NAMESPACE_URL, uuid4, uuid5

from strategy_factory.api.semantic_contract import build_signal_evidence_records
from strategy_factory.api.constants import (
    BACKTEST_TYPE_THRESHOLDS,
    PROVISIONAL_PASS_THRESHOLDS,
)

from .backtest import BacktestEngine, StrategyRegistry
from .incubation import (
    _build_position_id,
    _parse_datetime,
    _resolve_strategy_target_codes,
    _runtime_action_lineage,
    get_strategy_incubation_service,
)
from .signal_tracker import _build_signal_tracking_artifacts
from aiask_quant_core.storage.trade_audit_writer import record_trade_fill_from_order_and_trade

from .acceptance_helpers import (
    _RoundTrip,
    _RoundTripSelection,
    _apply_failed_metrics_family_hardening,
    _bootstrap_lineage_token,
    _bootstrap_trade_floor,
    _build_bootstrap_lineage_fallback,
    _coerce_trade_date,
    _coerce_trade_ts,
    _dedupe_strings,
    _group_backtest_round_trips,
    _is_bootstrap_proxy_lineage_id,
    _merge_bootstrap_lineage,
    _parse_affected_rows,
    _round_trip_selection,
    _safe_float,
    _safe_int,
    _select_bootstrap_round_trips,
    _strategy_runtime_params,
    _sync_runtime_params_container,
    build_failed_metrics_filter_patch,
    summarize_code_performance,
)

class _BacktestMixin:
    async def _load_market_data(
        self,
        db,
        strategy: dict,
        *,
        history_limit: int = 1200,
    ) -> dict[str, list[dict[str, Any]]]:
        market_data: dict[str, list[dict[str, Any]]] = {}
        for code in await self._load_bootstrap_candidate_codes(db, strategy):
            try:
                rows = await db.get_klines(code, limit=max(250, int(history_limit or 1200)))
            except TypeError:
                rows = await db.get_klines(code, None, None)
            normalized = [dict(item) for item in list(rows or []) if isinstance(item, dict) and item.get("close") is not None]
            if normalized:
                market_data[code] = normalized
        return market_data

    async def _load_bootstrap_candidate_codes(
        self,
        db,
        strategy: dict,
        *,
        limit: int = 20,
    ) -> list[str]:
        strategy_id = str(strategy.get("id") or "").strip()
        codes = list(sorted(_resolve_strategy_target_codes(strategy)))
        if not strategy_id:
            return codes

        async def _collect_rows(method_name: str):
            method = getattr(db, method_name, None)
            if not callable(method):
                return []
            try:
                if method_name == "list_strategy_trade_positions":
                    return list(
                        await method(
                            strategy_id=strategy_id,
                            limit=max(1, int(limit or 20)),
                        )
                    )
                if method_name == "list_strategy_paper_orders":
                    return list(
                        await method(
                            strategy_id=strategy_id,
                            limit=max(1, int(limit or 20)),
                        )
                    )
                return list(await method(strategy_id, limit=max(1, int(limit or 20))))
            except TypeError:
                try:
                    return list(await method(strategy_id))
                except Exception:
                    return []
            except Exception:
                return []

        inferred_codes: list[str] = []
        for method_name in (
            "list_strategy_trade_positions",
            "list_strategy_paper_trades",
            "list_strategy_paper_orders",
        ):
            rows = await _collect_rows(method_name)
            for row in rows:
                if not isinstance(row, dict):
                    continue
                code = str(row.get("code") or row.get("stock_code") or "").strip()
                if code:
                    inferred_codes.append(code)
        return _dedupe_strings([*codes, *inferred_codes])

    async def _find_existing_backtest_id(self, db, strategy: dict) -> Optional[str]:
        strategy_id = str(strategy.get("id") or "").strip()
        backtest_artifact_id = str(strategy.get("backtest_artifact_id") or "").strip()
        if not hasattr(db, "acquire"):
            return None
        async with db.acquire() as conn:
            if strategy_id:
                row = await conn.fetchrow(
                    """
                    SELECT id
                    FROM backtest_results
                    WHERE params LIKE $1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    f'%\"strategy_id\": \"{strategy_id}\"%',
                )
                if row:
                    return str(row.get("id") or "").strip() or None
            if backtest_artifact_id:
                row = await conn.fetchrow(
                    """
                    SELECT id
                    FROM backtest_results
                    WHERE params LIKE $1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    f'%\"artifact_id\": \"{backtest_artifact_id}\"%',
                )
                if row:
                    return str(row.get("id") or "").strip() or None
        return None

    async def _persist_generated_backtest(
        self,
        db,
        *,
        strategy: dict,
        market_data: dict[str, list[dict[str, Any]]],
        result_payload: dict[str, Any],
        trades: list[dict],
    ) -> str:
        params = _strategy_runtime_params(strategy)
        codes = list(market_data.keys())
        first_code = codes[0] if codes else str(strategy.get("id") or "portfolio")
        start_date = min(
            _coerce_trade_date((rows[0] or {}).get("date") if rows else None) or date.today()
            for rows in market_data.values()
            if rows
        )
        end_date = max(
            _coerce_trade_date((rows[-1] or {}).get("date") if rows else None) or date.today()
            for rows in market_data.values()
            if rows
        )
        backtest_id = f"bt_bootstrap_{uuid4().hex[:12]}"
        initial_capital = _safe_float(params.get("initial_capital"), 100000.0)
        backtest_params = {
            **params,
            "strategy_id": strategy.get("id"),
            "strategy_name": strategy.get("name"),
            "bootstrap_source": "generated_backtest_for_incubation_bootstrap_v1",
            "target_symbols": codes,
        }
        async with db.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO backtest_results
                    (id, code, strategy, params, stocks, start_date, end_date, initial_capital, final_capital,
                     total_return, annual_return, max_drawdown, sharpe_ratio, sortino_ratio, win_rate,
                     profit_factor, avg_win, avg_loss, expectancy, avg_holding_days, exposure_rate,
                     max_consecutive_loss, trades_count, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                        $10, $11, $12, $13, $14, $15,
                        $16, $17, $18, $19, $20, $21,
                        $22, $23, CURRENT_TIMESTAMP)
                """,
                backtest_id,
                first_code,
                str(strategy.get("strategy_type") or ""),
                json.dumps(backtest_params, ensure_ascii=False, default=str),
                json.dumps(codes, ensure_ascii=False, default=str),
                start_date,
                end_date,
                initial_capital,
                _safe_float(result_payload.get("final_capital"), initial_capital),
                _safe_float(result_payload.get("total_return"), 0.0),
                _safe_float(result_payload.get("annual_return"), 0.0),
                _safe_float(result_payload.get("max_drawdown"), 0.0),
                _safe_float(result_payload.get("sharpe_ratio"), 0.0),
                _safe_float(result_payload.get("sortino_ratio"), 0.0),
                _safe_float(result_payload.get("win_rate"), 0.0),
                _safe_float(result_payload.get("profit_factor"), 0.0),
                _safe_float(result_payload.get("avg_win"), 0.0),
                _safe_float(result_payload.get("avg_loss"), 0.0),
                _safe_float(result_payload.get("expectancy"), 0.0),
                _safe_float(result_payload.get("avg_holding_days"), 0.0),
                _safe_float(result_payload.get("exposure_rate"), 0.0),
                _safe_int(result_payload.get("max_consecutive_loss"), 0),
                _safe_int(result_payload.get("trades_count"), len(_group_backtest_round_trips(trades))),
            )
            running_cash = initial_capital
            for index, trade in enumerate(list(trades or []), start=1):
                code = str(trade.get("code") or trade.get("stock_code") or "").strip()
                shares = max(0, _safe_int(trade.get("shares"), 0))
                price = _safe_float(trade.get("price"), 0.0)
                gross_value = round(price * float(shares), 4)
                action = "buy" if _safe_int(trade.get("signal"), 0) > 0 else "sell"
                fee = _safe_float(trade.get("fee"), 0.0)
                slippage = _safe_float(trade.get("slippage"), 0.0)
                if action == "buy":
                    net_value = gross_value + fee + slippage
                    running_cash -= net_value
                else:
                    net_value = gross_value - fee - slippage
                    running_cash += net_value
                trade_date = _coerce_trade_date(trade.get("time") or trade.get("trade_date")) or date.today()
                await conn.execute(
                    """
                    INSERT INTO backtest_trades
                        (id, backtest_id, stock_code, action, price, shares, gross_value, fee, slippage,
                         net_value, cash_balance, equity, trade_date, reason, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9,
                            $10, $11, $12, $13, $14, CURRENT_TIMESTAMP)
                    """,
                    f"{backtest_id}_trade_{index:04d}",
                    backtest_id,
                    code,
                    action,
                    price,
                    shares,
                    gross_value,
                    fee,
                    slippage,
                    net_value,
                    round(running_cash, 4),
                    round(running_cash, 4),
                    trade_date,
                    str(trade.get("reason") or trade.get("action") or action),
                )
        return backtest_id

    async def _get_or_create_bootstrap_backtest(
        self,
        db,
        strategy: dict,
        *,
        history_limit: int = 1200,
    ) -> tuple[str, list[dict[str, Any]]]:
        existing_backtest_id = await self._find_existing_backtest_id(db, strategy)
        if existing_backtest_id:
            async with db.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT id, stock_code, action, price, shares, fee, slippage, trade_date, reason
                    FROM backtest_trades
                    WHERE backtest_id = $1
                    ORDER BY trade_date, id
                    """,
                    existing_backtest_id,
                )
            existing_trades = [
                {
                    "id": row.get("id"),
                    "code": row.get("stock_code"),
                    "signal": 1 if str(row.get("action") or "").strip().lower() == "buy" else -1,
                    "action": row.get("action"),
                    "price": row.get("price"),
                    "shares": row.get("shares"),
                    "fee": row.get("fee"),
                    "slippage": row.get("slippage"),
                    "time": row.get("trade_date"),
                    "reason": row.get("reason"),
                }
                for row in list(rows or [])
            ]
            if _group_backtest_round_trips(existing_trades):
                return existing_backtest_id, existing_trades

        market_data = await self._load_market_data(db, strategy, history_limit=history_limit)
        if not market_data:
            raise RuntimeError("bootstrap_backtest_market_data_missing")
        params = _strategy_runtime_params(strategy)
        strategy_type = str(strategy.get("strategy_type") or "").strip()
        if len(market_data) == 1:
            code, rows = next(iter(market_data.items()))
            raw = BacktestEngine.run_backtest(code, rows, strategy_type, params, return_trades=True)
        else:
            raw = BacktestEngine.run_portfolio_backtest(market_data, strategy_type, params, return_trades=True)
        if not raw.get("success"):
            raise RuntimeError(str(raw.get("error") or "bootstrap_backtest_failed"))
        result_payload = dict(raw.get("data") or raw)
        trades = list(result_payload.get("trades") or [])
        round_trips = _group_backtest_round_trips(trades)
        if not round_trips:
            raise RuntimeError("bootstrap_backtest_no_round_trips")
        backtest_id = await self._persist_generated_backtest(
            db,
            strategy=strategy,
            market_data=market_data,
            result_payload=result_payload,
            trades=trades,
        )
        return backtest_id, trades
