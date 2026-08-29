from __future__ import annotations

import asyncio

from akshare_mcp.tools.managers import _paper_trading_manager_support as support_module


class _FakeAcquire:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakePaperConn:
    def __init__(self, *, account: dict, positions: list[dict], trades: list[dict]):
        self.account = dict(account)
        self.positions = [dict(item) for item in positions]
        self.trades = [dict(item) for item in trades]

    def _find_position(self, account_id: str, code: str):
        for row in self.positions:
            if str(row.get("account_id")) == str(account_id) and str(row.get("stock_code")) == str(code):
                return row
        return None

    async def fetchrow(self, query: str, *args):
        if "SELECT * FROM paper_accounts WHERE id" in query:
            account_id = args[0]
            return dict(self.account) if str(self.account.get("id")) == str(account_id) else None
        if "SELECT * FROM paper_positions WHERE account_id = $1 AND stock_code = $2" in query:
            account_id, code = args
            row = self._find_position(account_id, code)
            return dict(row) if row else None
        return None

    async def fetch(self, query: str, *args):
        if "SELECT * FROM paper_positions WHERE account_id=$1" in query:
            account_id = args[0]
            return [
                dict(row)
                for row in self.positions
                if str(row.get("account_id")) == str(account_id)
            ]
        if "SELECT * FROM paper_trades WHERE account_id=$1" in query:
            account_id = args[0]
            rows = [
                dict(row)
                for row in self.trades
                if str(row.get("account_id")) == str(account_id)
            ]
            return rows
        return []

    async def fetchval(self, query: str, *args):
        if "FROM paper_trades" in query and "CASE" in query:
            account_id = args[0]
            cash_delta = 0.0
            for trade in self.trades:
                if str(trade.get("account_id")) != str(account_id):
                    continue
                amount = float(trade.get("amount") or 0.0)
                commission = float(trade.get("commission") or 0.0)
                if str(trade.get("trade_type")) == "buy":
                    cash_delta -= amount + commission
                elif str(trade.get("trade_type")) == "sell":
                    cash_delta += amount - commission
            return cash_delta
        if "SUM(market_value)" in query:
            account_id = args[0]
            return sum(
                float(row.get("market_value") or 0.0)
                for row in self.positions
                if str(row.get("account_id")) == str(account_id)
            )
        return None

    async def execute(self, query: str, *args):
        if query.startswith("DELETE FROM paper_positions"):
            account_id = args[0]
            self.positions = [
                row
                for row in self.positions
                if str(row.get("account_id")) != str(account_id)
            ]
            return
        if "INSERT INTO paper_positions" in query:
            (
                account_id,
                code,
                stock_name,
                quantity,
                cost_price,
                current_price,
                market_value,
                profit_rate,
            ) = args[:8]
            existing = self._find_position(account_id, code)
            payload = {
                "account_id": account_id,
                "stock_code": code,
                "stock_name": stock_name,
                "quantity": int(quantity),
                "cost_price": float(cost_price),
                "current_price": float(current_price),
                "market_value": float(market_value),
                "profit_rate": float(profit_rate),
            }
            if existing:
                existing.update(payload)
            else:
                self.positions.append(payload)
            return
        if "INSERT INTO paper_trades" in query:
            (
                trade_id,
                account_id,
                stock_code,
                stock_name,
                trade_type,
                price,
                quantity,
                amount,
                commission,
                strategy_id,
                source_order_id,
                signal_id,
                position_id,
            ) = args[:13]
            self.trades.append(
                {
                    "id": trade_id,
                    "account_id": account_id,
                    "stock_code": stock_code,
                    "stock_name": stock_name,
                    "trade_type": trade_type,
                    "price": float(price),
                    "quantity": int(quantity),
                    "amount": float(amount),
                    "commission": float(commission),
                    "strategy_id": strategy_id,
                    "source_order_id": source_order_id,
                    "signal_id": signal_id,
                    "position_id": position_id,
                }
            )
            return
        if query.startswith("UPDATE paper_accounts SET current_capital"):
            current_capital, total_value, account_id = args
            if str(self.account.get("id")) == str(account_id):
                self.account["current_capital"] = float(current_capital)
                self.account["total_value"] = float(total_value)
            return
        return


class _FakePaperDb:
    def __init__(self, conn: _FakePaperConn):
        self._conn = conn

    def acquire(self):
        return _FakeAcquire(self._conn)


def test_reconcile_account_state_repairs_position_and_cash_drift():
    conn = _FakePaperConn(
        account={
            "id": "acc-1",
            "initial_capital": 100000.0,
            "current_capital": 100000.0,
            "total_value": 100480.0,
        },
        positions=[
            {
                "account_id": "acc-1",
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "quantity": 40,
                "cost_price": 12.0,
                "current_price": 12.0,
                "market_value": 480.0,
                "profit_rate": 0.0,
            }
        ],
        trades=[
            {
                "id": "t-1",
                "account_id": "acc-1",
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "trade_type": "buy",
                "price": 10.0,
                "quantity": 100,
                "amount": 1000.0,
                "commission": 0.0,
            }
        ],
    )
    db = _FakePaperDb(conn)

    result = asyncio.run(
        support_module._reconcile_account_state(
            db,
            "acc-1",
            refresh_prices=False,
            force=False,
        )
    )

    assert result["drift_detected"] is True
    assert result["reconciled"] is True
    assert "position_quantity_mismatch:600519" in result["reasons"]
    assert conn.account["current_capital"] == 99000.0
    assert conn.account["total_value"] == 100200.0
    assert len(conn.positions) == 1
    assert conn.positions[0]["quantity"] == 100
    assert conn.positions[0]["cost_price"] == 10.0
    assert conn.positions[0]["current_price"] == 12.0


def test_reconcile_account_state_keeps_consistent_snapshot_without_repair():
    conn = _FakePaperConn(
        account={
            "id": "acc-1",
            "initial_capital": 100000.0,
            "current_capital": 99000.0,
            "total_value": 100200.0,
        },
        positions=[
            {
                "account_id": "acc-1",
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "quantity": 100,
                "cost_price": 10.0,
                "current_price": 12.0,
                "market_value": 1200.0,
                "profit_rate": 0.2,
            }
        ],
        trades=[
            {
                "id": "t-1",
                "account_id": "acc-1",
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "trade_type": "buy",
                "price": 10.0,
                "quantity": 100,
                "amount": 1000.0,
                "commission": 0.0,
            }
        ],
    )
    db = _FakePaperDb(conn)

    result = asyncio.run(
        support_module._reconcile_account_state(
            db,
            "acc-1",
            refresh_prices=False,
            force=False,
        )
    )

    assert result["drift_detected"] is False
    assert result["reconciled"] is False
    assert result["reasons"] == []
    assert conn.account["current_capital"] == 99000.0
    assert conn.account["total_value"] == 100200.0
    assert len(conn.positions) == 1
    assert conn.positions[0]["quantity"] == 100


def test_reconcile_account_state_refresh_prices_detects_valuation_drift(monkeypatch):
    async def _fixed_price(_code: str, _db):
        return 11.0

    monkeypatch.setattr(support_module, "_get_price", _fixed_price)

    conn = _FakePaperConn(
        account={
            "id": "acc-1",
            "initial_capital": 100000.0,
            "current_capital": 99000.0,
            "total_value": 100000.0,
        },
        positions=[
            {
                "account_id": "acc-1",
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "quantity": 100,
                "cost_price": 10.0,
                "current_price": 10.0,
                "market_value": 1000.0,
                "profit_rate": 0.0,
            }
        ],
        trades=[
            {
                "id": "t-1",
                "account_id": "acc-1",
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "trade_type": "buy",
                "price": 10.0,
                "quantity": 100,
                "amount": 1000.0,
                "commission": 0.0,
            }
        ],
    )
    db = _FakePaperDb(conn)

    result = asyncio.run(
        support_module._reconcile_account_state(
            db,
            "acc-1",
            refresh_prices=True,
            force=False,
        )
    )

    assert result["drift_detected"] is True
    assert result["reconciled"] is True
    assert "position_valuation_mismatch:600519" in result["reasons"]
    assert conn.account["total_value"] == 100100.0
    assert len(conn.positions) == 1
    assert conn.positions[0]["current_price"] == 11.0
    assert conn.positions[0]["market_value"] == 1100.0


def test_fill_order_recomputes_cash_from_trade_ledger(monkeypatch):
    async def _noop_record_trade_position_fill(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        support_module,
        "build_cost_model",
        lambda *_args, **_kwargs: {"estimated": {"commission": 0.0}},
    )
    monkeypatch.setattr(support_module, "_record_trade_position_fill", _noop_record_trade_position_fill)

    conn = _FakePaperConn(
        account={
            "id": "acc-1",
            "initial_capital": 100000.0,
            "current_capital": 50000.0,
            "total_value": 50000.0,
        },
        positions=[],
        trades=[],
    )

    trade_id, commission = asyncio.run(
        support_module._fill_order(
            conn,
            "acc-1",
            "600519",
            "buy",
            100,
            10.0,
        )
    )

    assert trade_id
    assert commission == 0.0
    assert conn.account["current_capital"] == 99000.0
    assert conn.account["total_value"] == 100000.0
    assert len(conn.positions) == 1
    assert conn.positions[0]["stock_code"] == "600519"
    assert conn.positions[0]["quantity"] == 100
    assert conn.positions[0]["cost_price"] == 10.0
