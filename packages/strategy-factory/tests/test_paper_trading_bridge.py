from __future__ import annotations

import asyncio
import json


class _FakePaperDb:
    def __init__(self):
        self.accounts = {
            "paper-1": {
                "id": "paper-1",
                "strategy_id": "strategy-1",
                "status": "active",
                "current_capital": 100_000.0,
            }
        }
        self.orders: list[dict] = []
        self.signals: list[dict] = []
        self._next_order_id = 1

    async def fetchrow(self, query: str, *args):
        if "FROM paper_orders" in query:
            strategy_id, signal_date, code, direction = [str(item) for item in args[:4]]
            for order in self.orders:
                if (
                    order.get("source") == "paper_trading_bridge"
                    and str(order.get("strategy_id")) == strategy_id
                    and str(order.get("signal_date")) == signal_date
                    and str(order.get("code")) == code
                    and str(order.get("direction")) == direction
                ):
                    return dict(order)
            return None
        if "FROM paper_positions" in query:
            return None
        if "FROM paper_accounts WHERE id" in query:
            account_id = str(args[0])
            return dict(self.accounts.get(account_id) or {})
        if "FROM paper_accounts WHERE strategy_id" in query:
            strategy_id = str(args[0])
            for account in self.accounts.values():
                if account.get("strategy_id") == strategy_id and account.get("status") == "active":
                    return dict(account)
            return None
        if "INSERT INTO paper_orders" in query:
            account_id, code, direction, shares, strategy_id, signal_date = args[:6]
            for order in self.orders:
                if (
                    order.get("source") == "paper_trading_bridge"
                    and str(order.get("strategy_id")) == str(strategy_id)
                    and str(order.get("signal_date")) == str(signal_date)
                    and str(order.get("code")) == str(code)
                    and str(order.get("direction")) == str(direction)
                ):
                    raise RuntimeError("UNIQUE constraint failed: paper_orders")
            row = {
                "id": self._next_order_id,
                "account_id": account_id,
                "code": code,
                "direction": direction,
                "shares": int(shares),
                "status": "pending",
                "order_type": "market",
                "strategy_id": strategy_id,
                "signal_date": signal_date,
                "source": "paper_trading_bridge",
            }
            self._next_order_id += 1
            self.orders.append(row)
            return dict(row)
        return None

    async def execute(self, query: str, *args):
        if "INSERT OR REPLACE INTO strategy_signals" in query:
            strategy_id, signal_date, code, signal, score, signal_metadata = args[:6]
            self.signals.append(
                {
                    "strategy_id": strategy_id,
                    "signal_date": signal_date,
                    "code": code,
                    "signal": signal,
                    "score": score,
                    "signal_metadata": signal_metadata,
                }
            )
            return None
        return None


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
            {
                "id": "strategy-1",
                "strategy_type": "momentum",
                "params": {"target_symbols": ["600519"]},
            },
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
