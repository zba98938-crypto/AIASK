from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


class _FakePaperDb:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE paper_accounts (
                id TEXT PRIMARY KEY, strategy_id TEXT, status TEXT, current_capital REAL,
                initial_capital REAL, total_value REAL, account_type TEXT, risk_rules TEXT
            );
            INSERT INTO paper_accounts VALUES ('paper-1', 'strategy-1', 'active', 100000, 100000, 100000, 'strategy_incubation', '{}');
            CREATE TABLE paper_orders (
                id INTEGER PRIMARY KEY, account_id TEXT, code TEXT, direction TEXT, shares INTEGER,
                status TEXT, order_type TEXT, strategy_id TEXT, signal_date TEXT, source TEXT,
                signal_id TEXT, position_id TEXT, reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE UNIQUE INDEX idx_bridge ON paper_orders(strategy_id, signal_date, code, direction)
                WHERE source = 'paper_trading_bridge';
            CREATE TABLE strategy_signals (
                id INTEGER PRIMARY KEY, strategy_id TEXT, signal_date TEXT, code TEXT, signal INTEGER,
                score REAL, action_source TEXT, signal_metadata TEXT,
                UNIQUE(strategy_id, signal_date, code)
            );
            CREATE TABLE paper_positions (account_id TEXT, stock_code TEXT, quantity INTEGER, current_price REAL);
            CREATE TABLE strategy_trade_positions (
                position_id TEXT, account_id TEXT, strategy_id TEXT, code TEXT, status TEXT, remaining_shares INTEGER
            );
            CREATE TABLE paper_nav (
                account_id TEXT, nav_date TEXT, total_value REAL, cash REAL, market_value REAL, daily_return REAL,
                UNIQUE(account_id, nav_date)
            );
            CREATE TABLE paper_trades (
                id TEXT PRIMARY KEY, account_id TEXT, position_id TEXT, stock_code TEXT, trade_type TEXT,
                quantity REAL, price REAL, commission REAL, amount REAL, trade_time TEXT, source_order_id TEXT
            );
        """)
        self.histories: dict[str, list[dict]] = {}
        self.strategies: list[dict] = []
        self.loaded_symbols: list[str] = []

    @property
    def orders(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM paper_orders ORDER BY id")]

    @property
    def signals(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM strategy_signals ORDER BY id")]

    async def fetchrow(self, query: str, *args):
        row = self.conn.execute(query, self._bindings(query, args)).fetchone()
        return dict(row) if row else None

    async def execute(self, query: str, *args):
        self.conn.execute(query, self._bindings(query, args))

    async def fetch(self, query: str, *args):
        return [dict(row) for row in self.conn.execute(query, self._bindings(query, args)).fetchall()]

    @staticmethod
    def _bindings(query, args):
        return {str(index + 1): value for index, value in enumerate(args)} if "$1" in query else args

    async def list_strategies(self, **_kwargs):
        return self.strategies

    async def get_klines(self, code, **_kwargs):
        self.loaded_symbols.append(code)
        return self.histories.get(code, [])


def _strategy(symbols=None):
    return {
        "id": "strategy-1", "strategy_type": "momentum",
        "params": {"target_symbols": symbols or ["600519"], "backtest_win_rate": 0.5,
                   "candidate_contract_hash": "candidate-1", "tested_object_hash": "tested-1"},
    }


def _buy_signals(monkeypatch):
    from aiask_quant_core.backtest import StrategyRegistry

    class Runtime:
        def generate_signals(self, closes, volumes):
            return [1] * len(closes)

    monkeypatch.setattr(StrategyRegistry, "create_runtime_strategy", lambda *_args, **_kwargs: (Runtime(), {}))


def test_bridge_reuses_same_day_same_strategy_symbol_direction_order() -> None:
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    db = _FakePaperDb()
    bridge = PaperTradingBridge(db)

    async def _run():
        first = await bridge._place_paper_order(
            account_id="paper-1",
            code="600519",
            signal=1,
            strategy_id="strategy-1",
            signal_date="2026-05-21",
            latest_price=200.0,
        )
        second = await bridge._place_paper_order(
            account_id="paper-1",
            code="600519",
            signal=1,
            strategy_id="strategy-1",
            signal_date="2026-05-21",
            latest_price=200.0,
        )
        third = await bridge._place_paper_order(
            account_id=" paper-1 ",
            code=" 600519 ",
            signal=1,
            strategy_id=" strategy-1 ",
            signal_date=" 2026-05-21 ",
            latest_price=200.0,
        )
        return first, second, third

    first, second, third = asyncio.run(_run())

    assert len(db.orders) == 1
    assert first["placed"] is True
    assert first["order_id"] == "1"
    # 30% × 100k = 30k 预算 / 200 元 = 150 股 → 整手取 100 股
    assert first["shares"] == 100
    assert second["placed"] is False
    assert second["reused"] is True
    assert second["order_id"] == first["order_id"]
    assert third["placed"] is False
    assert third["reused"] is True
    assert third["order_id"] == first["order_id"]


def test_bridge_buy_shares_scale_to_latest_price_no_insufficient_cash() -> None:
    """高价股按最新价估算股数，不再固定除数 20 导致 insufficient_cash。"""
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    db = _FakePaperDb()
    bridge = PaperTradingBridge(db)

    async def _run():
        # 现金 100k，30% 预算 = 30k；股价 1500 → 30k/1500=20 股 → 不足整手 → 跳过
        high = await bridge._place_paper_order(
            account_id="paper-1",
            code="601138",
            signal=1,
            strategy_id="strategy-1",
            signal_date="2026-06-12",
            latest_price=1500.0,
        )
        # 股价 50 → 30k/50=600 股 → 整手 600 股，下单成功
        mid = await bridge._place_paper_order(
            account_id="paper-1",
            code="300308",
            signal=1,
            strategy_id="strategy-1",
            signal_date="2026-06-12",
            latest_price=50.0,
        )
        # 缺价格 → 不下单，明确 no_price 而非超额下单
        nop = await bridge._place_paper_order(
            account_id="paper-1",
            code="000001",
            signal=1,
            strategy_id="strategy-1",
            signal_date="2026-06-12",
            latest_price=0.0,
        )
        return high, mid, nop

    high, mid, nop = asyncio.run(_run())

    assert high["placed"] is False
    assert high["reason"] == "insufficient_cash_for_one_lot"
    assert mid["placed"] is True
    assert mid["shares"] == 600
    assert nop["placed"] is False
    assert nop["reason"] == "no_price"


def test_bridge_signal_metadata_uses_latest_bar_date(monkeypatch) -> None:
    from aiask_quant_core.backtest import StrategyRegistry
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    class _RuntimeStrategy:
        def generate_signals(self, closes, volumes):
            return [0, 0, 1]

    monkeypatch.setattr(
        StrategyRegistry,
        "create_runtime_strategy",
        lambda *_args, **_kwargs: (_RuntimeStrategy(), {}),
    )

    db = _FakePaperDb()
    bridge = PaperTradingBridge(db)

    async def _run():
        return await bridge.run_daily_signals(
            _strategy(),
            [
                {"date": "2026-06-03", "close": 10.0, "volume": 1000},
                {"date": "2026-06-04", "close": 10.5, "volume": 1200},
                {"date": "2026-06-05", "close": 12.0, "volume": 1500},
            ],
            signal_date="2026-06-18",
        )

    result = asyncio.run(_run())

    assert result["success"] is True
    assert db.signals
    metadata = json.loads(db.signals[0]["signal_metadata"])
    assert db.signals[0]["signal_date"] == "2026-06-18"
    assert metadata["latest_bar_date"] == "2026-06-05"
    assert metadata["latest_nonzero_signal_date"] == "2026-06-05"
    assert metadata["action_source"] == "paper_trading_bridge"
    assert metadata["candidate_contract_hash"] == "candidate-1"
    assert metadata["tested_object_hash"] == "tested-1"
    order_metadata = json.loads(db.orders[0]["reason"])
    assert order_metadata["idempotency_key"] == metadata["idempotency_key"]
    assert db.orders[0]["signal_id"] == str(db.signals[0]["id"])


def test_default_signal_day_is_latest_closed_bar_not_wall_clock(monkeypatch):
    import strategy_factory.application.research.paper_trading_bridge as module

    _buy_signals(monkeypatch)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 8, 10, tzinfo=ZoneInfo("Asia/Shanghai"))

    monkeypatch.setattr(module, "datetime", Clock)
    db = _FakePaperDb()
    # Unsorted, timestamp-shaped bars; Monday is still open, Sunday is not a session.
    bars = [
        {"time": "2026-06-08T00:00:00+08:00", "close": 900, "volume": 100},
        {"time": "2026-06-04T00:00:00+08:00", "close": 10, "volume": 100},
        {"time": "2026-06-05T00:00:00+08:00", "close": 20, "volume": 100},
        {"time": "2026-06-07T00:00:00+08:00", "close": 100, "volume": 100},
    ]
    result = asyncio.run(module.PaperTradingBridge(db).run_daily_signals(_strategy(), bars))
    assert result["signal_date"] == "2026-06-05"
    assert db.orders[0]["signal_date"] == "2026-06-05"
    assert db.orders[0]["shares"] == 1500  # 30,000 / Friday's 20, not the open Monday's 900.


def test_incomplete_final_bar_is_not_used_even_after_close(monkeypatch):
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    _buy_signals(monkeypatch)
    db = _FakePaperDb()
    result = asyncio.run(PaperTradingBridge(db).run_daily_signals(_strategy(), [
        {"date": "2026-06-04", "close": 10, "volume": 100},
        {"date": "2026-06-05", "close": 20, "volume": 100, "is_closed": False},
    ]))
    assert result["signal_date"] == "2026-06-04"


def test_multisymbol_bridge_uses_separate_series_and_idempotency(monkeypatch):
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    _buy_signals(monkeypatch)
    db = _FakePaperDb()
    bridge = PaperTradingBridge(db)
    strategy = _strategy(["600519", "000001"])
    histories = {
        "600519": [{"date": "2026-06-05", "close": 20, "volume": 100}],
        "000001": [{"date": "2026-06-04", "close": 50, "volume": 100}],
    }

    async def run():
        first = await bridge.run_daily_signals(strategy, histories)
        second = await bridge.run_daily_signals(strategy, histories)
        ambiguous = await bridge.run_daily_signals(strategy, histories["600519"])
        return first, second, ambiguous

    first, second, ambiguous = asyncio.run(run())
    assert first["success"] is True
    assert [(row["code"], row["signal_date"], row["shares"]) for row in db.orders] == [
        ("600519", "2026-06-05", 1500), ("000001", "2026-06-04", 600),
    ]
    assert len(db.signals) == 2
    assert all(result["order_result"]["reused"] for result in second["results"])
    assert ambiguous["error"] == "symbol_required_for_multi_symbol"


@pytest.mark.parametrize("hash_name", ["candidate_contract_hash", "tested_object_hash"])
def test_changed_hash_is_not_reused_or_allowed_to_overwrite_signal(monkeypatch, hash_name):
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    _buy_signals(monkeypatch)
    db = _FakePaperDb()
    bridge = PaperTradingBridge(db)
    bars = [{"date": "2026-06-05", "close": 20, "volume": 100}]

    async def run():
        first = await bridge.run_daily_signals(_strategy(), bars)
        changed = _strategy()
        changed["params"][hash_name] = "different-hash"
        second = await bridge.run_daily_signals(changed, bars)
        return first, second

    first, second = asyncio.run(run())
    assert first["order_result"]["placed"] is True
    assert second["success"] is False
    assert second["error"] == "signal_identity_conflict"
    assert len(db.orders) == len(db.signals) == 1
    assert json.loads(db.signals[0]["signal_metadata"])[hash_name] != "different-hash"


def test_missing_tested_identity_never_places_an_order(monkeypatch):
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    _buy_signals(monkeypatch)
    db = _FakePaperDb()
    strategy = _strategy()
    del strategy["params"]["tested_object_hash"]
    result = asyncio.run(PaperTradingBridge(db).run_daily_signals(strategy, [
        {"date": "2026-06-05", "close": 20, "volume": 100},
    ]))
    assert result["error"] == "missing_tested_object_hash"
    assert db.orders == []


def test_sell_links_exit_to_actual_open_position(monkeypatch):
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge
    from aiask_quant_core.backtest import StrategyRegistry

    class Runtime:
        def generate_signals(self, closes, volumes):
            return [-1] * len(closes)

    monkeypatch.setattr(StrategyRegistry, "create_runtime_strategy", lambda *_a, **_kw: (Runtime(), {}))
    db = _FakePaperDb()
    db.conn.execute("INSERT INTO paper_positions VALUES ('paper-1', '600519', 200, 20)")
    db.conn.execute("INSERT INTO strategy_trade_positions VALUES ('entry-position', 'paper-1', 'strategy-1', '600519', 'open', 200)")
    result = asyncio.run(PaperTradingBridge(db).run_daily_signals(_strategy(), [
        {"date": "2026-06-05", "close": 20, "volume": 100},
    ]))
    assert result["order_result"]["placed"] is True
    assert db.orders[0]["position_id"] == "entry-position"
    assert db.orders[0]["direction"] == "sell"


def test_scheduler_processes_each_symbol_and_preserves_latest_closed_dates(monkeypatch):
    from strategy_factory.application.research.paper_trading_scheduler import PaperTradingScheduler

    _buy_signals(monkeypatch)
    db = _FakePaperDb()
    db.strategies = [_strategy(["600519", "000001", "missing"])]
    days = [date(2026, 4, 1) + timedelta(days=n) for n in range(66)]
    days = [day for day in days if day.weekday() < 5]
    for code, price, offset in [("600519", 20, 0), ("000001", 50, 1)]:
        series = days[:len(days) - offset]
        db.histories[code] = [{"time": day.isoformat(), "close": price, "volume": 100} for day in reversed(series)]
    scheduler = PaperTradingScheduler(db)
    first = asyncio.run(scheduler.run_daily_paper_trading_cycle())
    second = asyncio.run(scheduler.run_daily_paper_trading_cycle())
    assert db.loaded_symbols[:3] == ["600519", "000001", "missing"]
    assert first["signal_count"] == first["order_count"] == 2
    assert first["processed_symbol_count"] == 3
    assert first["signal_dates"] == sorted([days[-1].isoformat(), days[-2].isoformat()])
    assert {row["code"]: row["signal_date"] for row in db.orders} == {
        "600519": days[-1].isoformat(), "000001": days[-2].isoformat(),
    }
    assert second["order_count"] == 0
    assert len(db.orders) == 2
    assert any("missing: insufficient_closed_bars" in error for error in first["errors"])


def test_order_idempotency_rejects_changed_hash_even_without_signal_writer():
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    db = _FakePaperDb()
    bridge = PaperTradingBridge(db)

    async def run():
        kwargs = dict(account_id="paper-1", code="600519", signal=1, strategy_id="strategy-1",
                      signal_date="2026-06-05", latest_price=20, candidate_contract_hash="candidate-1")
        first = await bridge._place_paper_order(**kwargs, tested_object_hash="tested-1")
        conflict = await bridge._place_paper_order(**kwargs, tested_object_hash="tested-2")
        return first, conflict

    first, conflict = asyncio.run(run())
    assert first["placed"] is True
    assert conflict == {"placed": False, "reason": "order_identity_conflict"}
    assert len(db.orders) == 1


def test_nav_snapshot_uses_that_days_closed_price_and_never_backdates_cash(monkeypatch):
    import strategy_factory.application.research.paper_trading_bridge as bridge_module
    import strategy_factory.application.research.paper_trading_scheduler as scheduler_module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 5, 16, tzinfo=ZoneInfo("Asia/Shanghai"))

    monkeypatch.setattr(bridge_module, "datetime", Clock)
    monkeypatch.setattr(scheduler_module, "datetime", Clock)
    db = _FakePaperDb()
    db.conn.execute("INSERT INTO paper_positions VALUES ('paper-1', '600519', 100, 999)")
    db.histories["600519"] = [{"date": "2026-06-05", "close": 20, "volume": 100}]
    scheduler = scheduler_module.PaperTradingScheduler(db)
    asyncio.run(scheduler._snapshot_all_navs("2026-06-05"))
    nav = dict(db.conn.execute("SELECT * FROM paper_nav").fetchone())
    assert nav["total_value"] == 102_000
    assert nav["market_value"] == 2000
    assert nav["daily_return"] is None  # No prior observation is not a measured zero return.
    asyncio.run(scheduler._snapshot_all_navs("2026-06-04"))
    assert db.conn.execute("SELECT COUNT(*) FROM paper_nav").fetchone()[0] == 1


def test_missing_closed_valuation_does_not_create_a_fake_nav_snapshot(monkeypatch):
    import strategy_factory.application.research.paper_trading_scheduler as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 5, 16, tzinfo=ZoneInfo("Asia/Shanghai"))

    monkeypatch.setattr(module, "datetime", Clock)
    db = _FakePaperDb()
    db.conn.execute("INSERT INTO paper_positions VALUES ('paper-1', '600519', 100, 999)")
    asyncio.run(module.PaperTradingScheduler(db)._snapshot_all_navs("2026-06-05"))
    assert db.conn.execute("SELECT COUNT(*) FROM paper_nav").fetchone()[0] == 0


def test_order_fill_position_link_and_promotion_metrics_share_the_same_identity(monkeypatch):
    from aiask_quant_core.backtest import StrategyRegistry
    from strategy_factory.application.research.paper_trading_bridge import PaperTradingBridge

    class Runtime:
        def generate_signals(self, closes, volumes):
            return [1 if close >= 10 else -1 for close in closes]

    monkeypatch.setattr(StrategyRegistry, "create_runtime_strategy", lambda *_a, **_kw: (Runtime(), {}))
    db = _FakePaperDb()
    bridge = PaperTradingBridge(db)

    async def run():
        await bridge.run_daily_signals(_strategy(), [{"date": "2026-06-04", "close": 10, "volume": 100}])
        entry = db.orders[0]
        db.conn.execute("UPDATE paper_orders SET status = 'filled' WHERE id = ?", (entry["id"],))
        db.conn.execute("INSERT INTO paper_positions VALUES ('paper-1', '600519', ?, 10)", (entry["shares"],))
        db.conn.execute("INSERT INTO strategy_trade_positions VALUES (?, 'paper-1', 'strategy-1', '600519', 'open', ?)",
                        (entry["position_id"], entry["shares"]))
        db.conn.execute("INSERT INTO paper_trades VALUES ('fill-buy', 'paper-1', ?, '600519', 'buy', ?, 10, 5, 30000, '2026-06-04T14:00:00+08:00', ?)",
                        (entry["position_id"], entry["shares"], str(entry["id"])))
        await bridge.run_daily_signals(_strategy(), [{"date": "2026-06-05", "close": 9, "volume": 100}])
        exit_order = db.orders[1]
        db.conn.execute("INSERT INTO paper_trades VALUES ('fill-sell', 'paper-1', ?, '600519', 'sell', ?, 9, 5, 27000, '2026-06-05T14:00:00+08:00', ?)",
                        (exit_order["position_id"], exit_order["shares"], str(exit_order["id"])))
        return await bridge.evaluate_promotion(_strategy())

    result = asyncio.run(run())
    assert db.orders[0]["position_id"] == db.orders[1]["position_id"]
    assert result["unbound_or_mismatched_trade_count"] == 0
    assert result["trade_count"] == 1
    assert result["net_pnl"] == -3010
    assert result["actual_win_rate"] == 0
    assert result["total_return"] == -0.0301
    assert result["eligible"] is False  # Real fill evidence still does not waive sample/risk gates.
