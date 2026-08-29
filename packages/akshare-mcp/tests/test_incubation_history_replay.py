from __future__ import annotations

import asyncio
from datetime import date

import pytest

from akshare_mcp.services.incubation import StrategyIncubationService


class _HistoricalPriceDb:
    def __init__(self):
        self.calls: list[tuple[str, str | None, int | None]] = []

    async def get_klines(self, code: str, start_date=None, end_date=None, limit=None):
        self.calls.append((code, end_date, limit))
        if end_date == "2026-04-17":
            return [{"date": "2026-04-17", "close": 11.2}]
        return [{"date": "2026-04-18", "close": 13.4}]


class _ReplayDb:
    def __init__(self):
        self.signal_rows = [
            {"strategy_id": "strategy-replay", "code": "600000", "signal_date": date(2026, 4, 17), "signal": 1},
            {"strategy_id": "strategy-replay", "code": "600000", "signal_date": date(2026, 4, 21), "signal": -1},
        ]

    async def get_signals(self, strategy_id: str, start_date=None, end_date=None, limit: int = 100):
        rows = [dict(item) for item in self.signal_rows if item["strategy_id"] == strategy_id]
        if start_date is not None:
            rows = [item for item in rows if item["signal_date"] >= start_date]
        if end_date is not None:
            rows = [item for item in rows if item["signal_date"] <= end_date]
        rows.sort(key=lambda item: item["signal_date"], reverse=True)
        return rows[:limit]

    async def get_klines(self, code: str, start_date=None, end_date=None, limit=None):
        assert code == "600000"
        rows = [
            {"date": "2026-04-17", "close": 10.0},
            {"date": "2026-04-18", "close": 10.5},
            {"date": "2026-04-21", "close": 10.8},
        ]
        if start_date:
            rows = [row for row in rows if row["date"] >= str(start_date)]
        if end_date:
            rows = [row for row in rows if row["date"] <= str(end_date)]
        if limit is not None:
            rows = rows[-int(limit):]
        return rows


class _RecordingReplayService(StrategyIncubationService):
    def __init__(self):
        super().__init__()
        self.replayed_dates: list[date] = []
        self.force_close_calls: list[date] = []

    async def process_strategies(self, db, strategies: list[dict], signal_date=None):
        self.replayed_dates.append(signal_date)
        return {
            "count": len(strategies),
            "accounts_bound": 0,
            "orders_created": 1,
            "orders_filled": 1,
            "rejected_orders": 0,
            "nav_snapshots": 1,
            "metrics_recorded": 1,
            "items": [{"strategy_id": strategies[0]["id"], "signal_date": str(signal_date)}],
        }

    async def force_close_open_positions(self, db, strategy: dict, signal_date: date, **kwargs):
        self.force_close_calls.append(signal_date)
        return {
            "strategy_id": strategy["id"],
            "account_id": "inc-test",
            "created_count": 1,
            "skipped_count": 0,
            "orders": [{"id": 1, "code": "600000"}],
            "reason": kwargs.get("reason") or "replay_window_end_forced_exit",
        }

    async def settle_orders(self, db, strategy: dict, signal_date=None):
        return {
            "strategy_id": strategy["id"],
            "account_id": "inc-test",
            "filled_count": 1,
            "rejected_count": 0,
            "nav_snapshot": {"total_value": 100000.0},
        }

    async def record_metrics(self, db, strategy: dict, metric_date=None):
        return {"strategy_id": strategy["id"], "metric_date": str(metric_date)}


def test_price_on_or_before_uses_historical_close_before_falling_back():
    service = StrategyIncubationService()
    db = _HistoricalPriceDb()

    historical_price = asyncio.run(
        service._price_on_or_before(db, "600000", date(2026, 4, 17))
    )
    latest_price = asyncio.run(service._price_on_or_before(db, "600000", None))

    assert historical_price == 11.2
    assert latest_price == 13.4
    assert db.calls[0] == ("600000", "2026-04-17", 1)


def test_ensure_account_is_idempotent_for_same_strategy_and_run():
    class _Db:
        def __init__(self):
            self.accounts: dict[str, dict] = {}
            self.bindings: dict[tuple[str, str], dict] = {}
            self.events: list[dict] = []

        async def get_strategy_incubation_account(self, strategy_id):
            rows = [dict(row) for (sid, _), row in self.bindings.items() if sid == strategy_id]
            return rows[-1] if rows else None

        async def get_paper_account_by_strategy(self, strategy_id):
            for account in self.accounts.values():
                if account.get("strategy_id") == strategy_id:
                    return dict(account)
            return None

        async def save_paper_account(self, account):
            self.accounts[account["id"]] = dict(account)
            return dict(account)

        async def save_strategy_incubation_account(
            self,
            strategy_id,
            account_id,
            stage="warmup",
            status="active",
            source_run_id=None,
            metadata=None,
        ):
            key = (strategy_id, account_id)
            self.bindings[key] = {
                "strategy_id": strategy_id,
                "account_id": account_id,
                "stage": stage,
                "status": status,
                "source_run_id": source_run_id,
                "metadata": dict(metadata or {}),
            }
            return dict(self.bindings[key])

        async def save_strategy_domain_event(self, payload):
            self.events.append(dict(payload))
            return dict(payload)

    db = _Db()
    service = StrategyIncubationService()
    strategy = {"id": "strategy-idempotent", "name": "idempotent", "strategy_type": "momentum"}

    first = asyncio.run(service.ensure_account(db, strategy, stage="warmup", source_run_id="factory-run-1"))
    second = asyncio.run(service.ensure_account(db, strategy, stage="warmup", source_run_id="factory-run-1"))

    assert first["created"] is True
    assert second["created"] is False
    assert first["account"]["id"] == second["account"]["id"]
    assert len(db.accounts) == 1
    assert len(db.bindings) == 1
    assert list(db.bindings.values())[0]["source_run_id"] == "factory-run-1"

    # 事件去重:首次新建发 account_bound,第二次幂等 re-bind(stage 未变)不再发,
    # 避免 strategy_domain_events 表被无变化的重复事件膨胀。
    bound_events = [e for e in db.events if e.get("event_type") == "incubation.account_bound"]
    assert len(bound_events) == 1
    assert bound_events[0]["payload"]["created"] is True

    # stage 真正跃迁时应重新发事件(warmup → paper)。
    asyncio.run(service.ensure_account(db, strategy, stage="paper", source_run_id="factory-run-2"))
    bound_events = [e for e in db.events if e.get("event_type") == "incubation.account_bound"]
    assert len(bound_events) == 2
    assert bound_events[1]["payload"]["stage_changed"] is True
    assert bound_events[1]["payload"]["previous_stage"] == "warmup"
    assert bound_events[1]["payload"]["stage"] == "paper"


@pytest.mark.asyncio
async def test_settle_orders_processes_pending_backlog_before_settlement_date(initialized_db):
    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    adapter = SQLiteAdapter(path=initialized_db)
    await adapter.initialize()
    try:
        async def _get_klines(code: str, start_date=None, end_date=None, limit=None):
            rows = [
                {"date": "2026-06-18", "close": 10.0},
                {"date": "2026-06-19", "close": 12.0},
            ]
            if end_date:
                rows = [row for row in rows if row["date"] <= str(end_date)]
            if limit is not None:
                rows = rows[-int(limit):]
            return rows

        adapter.get_klines = _get_klines
        service = StrategyIncubationService()
        strategy = {
            "id": "strategy-backlog-settle",
            "name": "Backlog settle",
            "strategy_type": "momentum",
            "params": {},
        }
        async with adapter.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategies
                    (id, name, strategy_type, params, status, created_at, updated_at)
                VALUES ($1, $2, $3, '{}', 'incubating', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                strategy["id"],
                strategy["name"],
                strategy["strategy_type"],
            )
        account = await service._save_strategy_account(
            adapter,
            {
                "id": "inc_backlog_settle",
                "user_id": "strategy_factory",
                "name": "incubation backlog settle",
                "initial_capital": 100000.0,
                "current_capital": 100000.0,
                "total_value": 100000.0,
                "strategy_id": strategy["id"],
                "account_type": "incubation",
                "incubation_stage": "warmup",
                "status": "active",
            },
        )
        async with adapter.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO paper_orders
                    (account_id, strategy_id, signal_date, source, code, direction, shares,
                     price, order_type, status, signal_id, position_id, created_at, updated_at)
                VALUES ($1, $2, $3, 'strategy_signal', '600000', 'buy', 100,
                        10.0, 'limit', 'pending', 'sig-backlog', 'pos-backlog',
                        '2026-06-18 18:00:00', '2026-06-18 18:00:00')
                """,
                account["id"],
                strategy["id"],
                date(2026, 6, 18),
            )

        from akshare_mcp.services.strategy_lifecycle_shared import build_incubation_overview

        stale_overview = await build_incubation_overview(
            adapter,
            strategy,
            force_recompute=True,
        )
        assert stale_overview["execution_quality"]["trade_count"] == 0

        result = await service.settle_orders(
            adapter,
            strategy,
            signal_date=date(2026, 6, 19),
        )

        assert result["filled_count"] == 1
        assert result["rejected_count"] == 0
        assert result["nav_snapshot"]["nav_date"] == date(2026, 6, 19)
        async with adapter.acquire() as conn:
            order = await conn.fetchrow(
                "SELECT status, filled_at FROM paper_orders WHERE strategy_id=$1",
                strategy["id"],
            )
            trade = await conn.fetchrow(
                "SELECT price, quantity, source_order_id, position_id FROM paper_trades WHERE strategy_id=$1",
                strategy["id"],
            )
            nav = await conn.fetchrow(
                "SELECT total_value, cash, market_value FROM paper_nav WHERE account_id=$1 AND nav_date=$2",
                account["id"],
                date(2026, 6, 19),
            )

        assert dict(order)["status"] == "filled"
        assert dict(order)["filled_at"] is not None
        assert float(dict(trade)["price"]) == 12.0
        assert int(dict(trade)["quantity"]) == 100
        assert dict(trade)["position_id"] == "pos-backlog"
        assert float(dict(nav)["market_value"]) == 1200.0

        refreshed_overview = await build_incubation_overview(adapter, strategy)
        assert refreshed_overview["cached"] is False
        assert refreshed_overview["execution_quality"]["trade_count"] == 1
        assert refreshed_overview["execution_quality"]["nav_observation_days"] == 1
        assert "missing_paper_trades" not in refreshed_overview["execution_quality"]["evidence_gap_codes"]
        assert "missing_paper_nav" not in refreshed_overview["execution_quality"]["evidence_gap_codes"]
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_sync_signals_to_orders_scales_empty_paper_account_for_high_priced_min_lot(
    initialized_db,
    monkeypatch,
):
    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    monkeypatch.setenv("INCUBATION_PAPER_MIN_LOT_CAPITAL_SCALE_ENABLED", "1")
    adapter = SQLiteAdapter(path=initialized_db)
    await adapter.initialize()
    try:
        service = StrategyIncubationService()
        signal_date = date(2026, 6, 20)
        strategy = {
            "id": "strategy-high-price-min-lot",
            "name": "High price min lot",
            "strategy_type": "custom_signal",
            "target_symbols": ["688167"],
            "params": {"target_symbols": ["688167"]},
        }
        async with adapter.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategies
                    (id, name, strategy_type, params, status, created_at, updated_at)
                VALUES ($1, $2, $3, '{}', 'incubating', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                strategy["id"],
                strategy["name"],
                strategy["strategy_type"],
            )
        await adapter.save_klines(
            "688167",
            [
                {
                    "date": "2026-06-20",
                    "code": "688167",
                    "open": 393.0,
                    "high": 393.0,
                    "low": 393.0,
                    "close": 393.0,
                    "volume": 1000,
                    "amount": 393000.0,
                    "turnover": 0.1,
                    "change_pct": 0.0,
                }
            ],
        )
        await adapter.save_signals(
            strategy["id"],
            signal_date,
            [{"code": "688167", "signal": 1, "score": 1.0}],
        )

        result = await service.sync_signals_to_orders(adapter, strategy, signal_date)

        assert result["created_count"] == 1
        assert result["skipped_count"] == 0
        assert result["skip_reason_counts"] == {}
        assert result["capital_scaled_for_min_lot"] is True
        assert result["capital_scale_events"][0]["min_lot_cost"] == 39300.0
        async with adapter.acquire() as conn:
            order = await conn.fetchrow(
                "SELECT code, shares, price, status FROM paper_orders WHERE strategy_id=$1",
                strategy["id"],
            )
        account = await adapter.get_paper_account_by_strategy(strategy["id"])

        assert dict(order)["code"] == "688167"
        assert int(dict(order)["shares"]) == 100
        assert float(dict(order)["price"]) == 393.0
        assert dict(order)["status"] == "pending"
        assert float(account["initial_capital"]) >= 39300.0 / 0.25
        assert float(account["current_capital"]) >= 39300.0 / 0.25
        assert float(account["total_value"]) >= 39300.0 / 0.25
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_sync_signals_to_orders_reports_min_lot_unaffordable_when_scale_disabled(
    initialized_db,
    monkeypatch,
):
    from aiask_quant_core.storage.sqlite import SQLiteAdapter

    monkeypatch.setenv("INCUBATION_PAPER_MIN_LOT_CAPITAL_SCALE_ENABLED", "0")
    adapter = SQLiteAdapter(path=initialized_db)
    await adapter.initialize()
    try:
        service = StrategyIncubationService()
        signal_date = date(2026, 6, 20)
        strategy = {
            "id": "strategy-high-price-min-lot-disabled",
            "name": "High price min lot disabled",
            "strategy_type": "custom_signal",
            "target_symbols": ["688167"],
            "params": {"target_symbols": ["688167"]},
        }
        async with adapter.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO strategies
                    (id, name, strategy_type, params, status, created_at, updated_at)
                VALUES ($1, $2, $3, '{}', 'incubating', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                strategy["id"],
                strategy["name"],
                strategy["strategy_type"],
            )
        await adapter.save_klines(
            "688167",
            [
                {
                    "date": "2026-06-20",
                    "code": "688167",
                    "open": 393.0,
                    "high": 393.0,
                    "low": 393.0,
                    "close": 393.0,
                    "volume": 1000,
                    "amount": 393000.0,
                    "turnover": 0.1,
                    "change_pct": 0.0,
                }
            ],
        )
        await adapter.save_signals(
            strategy["id"],
            signal_date,
            [{"code": "688167", "signal": 1, "score": 1.0}],
        )

        result = await service.sync_signals_to_orders(adapter, strategy, signal_date)

        assert result["created_count"] == 0
        assert result["skipped_count"] == 1
        assert result["skip_reason_counts"] == {"min_lot_unaffordable": 1}
        assert result["capital_scaled_for_min_lot"] is False
        async with adapter.acquire() as conn:
            order_count = await conn.fetchval(
                "SELECT COUNT(*) FROM paper_orders WHERE strategy_id=$1",
                strategy["id"],
            )
        account = await adapter.get_paper_account_by_strategy(strategy["id"])

        assert int(order_count) == 0
        assert float(account["initial_capital"]) == 100000.0
        assert float(account["current_capital"]) == 100000.0
        assert float(account["total_value"]) == 100000.0
    finally:
        await adapter.close()


def test_replay_strategy_history_replays_market_days_in_chronological_order():
    service = _RecordingReplayService()
    db = _ReplayDb()
    strategy = {
        "id": "strategy-replay",
        "target_symbols": ["600000"],
    }

    result = asyncio.run(
        service.replay_strategy_history(
            db,
            strategy,
            start_date=date(2026, 4, 17),
            end_date=date(2026, 4, 21),
            include_market_days=True,
            run_acceptance=False,
        )
    )

    assert service.replayed_dates == [
        date(2026, 4, 17),
        date(2026, 4, 18),
        date(2026, 4, 21),
    ]
    assert result["replayed_days"] == 3
    assert result["non_empty_days"] == 3
    assert result["orders_filled"] == 3
    assert result["start_date"] == "2026-04-17"
    assert result["end_date"] == "2026-04-21"


def test_replay_strategy_history_can_force_close_open_positions_at_window_end():
    service = _RecordingReplayService()
    db = _ReplayDb()
    strategy = {
        "id": "strategy-replay",
        "target_symbols": ["600000"],
    }

    result = asyncio.run(
        service.replay_strategy_history(
            db,
            strategy,
            start_date=date(2026, 4, 17),
            end_date=date(2026, 4, 21),
            include_market_days=True,
            force_close_open_positions=True,
            run_acceptance=False,
        )
    )

    assert service.force_close_calls == [date(2026, 4, 21)]
    assert result["orders_created"] == 4
    assert result["orders_filled"] == 4
    assert result["metrics_recorded"] == 4
    assert result["daily_results"][-1]["window_force_close"] is True


def test_replay_strategies_history_summarizes_acceptance_gate_distribution():
    class _AcceptanceSummaryService(StrategyIncubationService):
        async def replay_strategy_history(self, db, strategy, **kwargs):
            strategy_id = strategy["id"]
            if strategy_id == "ready-strategy":
                acceptance = {
                    "status": "ready",
                    "acceptance_matrix": {"overall_ready": True},
                    "execution_audit_gate_status": "passed",
                    "execution_hard_gate_passed": True,
                    "trade_audit_summary": {
                        "execution_audit_gate_status": "passed",
                        "realized_trade_count": 20,
                    },
                    "gap_categories": [],
                    "blocker_details": [],
                }
            else:
                acceptance = {
                    "status": "pending_data",
                    "acceptance_matrix": {"overall_ready": False},
                    "execution_audit_gate_status": "bootstrap_pending",
                    "execution_hard_gate_passed": False,
                    "trade_audit_summary": {
                        "execution_audit_gate_status": "bootstrap_pending",
                        "realized_trade_count": 0,
                    },
                    "gap_categories": ["sample_gap"],
                    "blocker_details": [
                        {
                            "blocker": "bootstrap_pending",
                            "category": "sample_gap",
                        }
                    ],
                }
            return {
                "strategy_id": strategy_id,
                "replayed_days": 2,
                "non_empty_days": 1,
                "orders_created": 2,
                "orders_filled": 2,
                "rejected_orders": 0,
                "metrics_recorded": 2,
                "acceptance": acceptance,
            }

    service = _AcceptanceSummaryService()
    result = asyncio.run(
        service.replay_strategies_history(
            object(),
            [{"id": "ready-strategy"}, {"id": "pending-strategy"}],
        )
    )

    assert result["count"] == 2
    assert result["acceptance_status_counts"] == {"ready": 1, "pending_data": 1}
    assert result["execution_audit_gate_status_counts"] == {
        "passed": 1,
        "bootstrap_pending": 1,
    }
    assert result["execution_hard_gate_passed_count"] == 1
    assert result["acceptance_overall_ready_count"] == 1
    assert result["acceptance_sample_gap_count"] == 1
    assert result["acceptance_realized_trade_count_total"] == 20
