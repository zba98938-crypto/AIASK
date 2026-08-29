from __future__ import annotations

import asyncio

from aiask_quant_core.storage.sqlite import SQLiteAdapter

from akshare_mcp.storage.sqlite.strategy_incubation import StrategyIncubationMixin


class _SignalEvidenceBackfillDb(StrategyIncubationMixin):
    def __init__(self, *, strategy: dict, orders: list[dict], trades: list[dict], existing=None):
        self._strategy = dict(strategy)
        self._orders = [dict(item) for item in list(orders or [])]
        self._trades = [dict(item) for item in list(trades or [])]
        self._existing = [dict(item) for item in list(existing or [])]
        self.saved_rows: list[dict] = []

    async def list_strategy_paper_orders(self, strategy_id: str, signal_date=None, status=None, limit: int = 200):
        assert strategy_id == self._strategy["id"]
        return list(self._orders)[:limit]

    async def list_strategy_paper_trades(self, strategy_id: str, account_id=None, limit: int = 500):
        assert strategy_id == self._strategy["id"]
        return list(self._trades)[:limit]

    async def list_strategy_signal_evidence(self, *, signal_id=None, strategy_id=None, limit: int = 200):
        assert strategy_id in {None, self._strategy["id"]}
        rows = list(self._existing)
        if signal_id is not None:
            rows = [row for row in rows if str(row.get("signal_id")) == str(signal_id)]
        return rows[:limit]

    async def get_strategy(self, strategy_id: str):
        assert strategy_id == self._strategy["id"]
        return dict(self._strategy)

    async def save_strategy_signal_evidence(self, evidence: dict):
        payload = dict(evidence)
        self.saved_rows.append(payload)
        self._existing.append({"signal_id": payload.get("signal_id")})
        return payload


class _AcceptanceHarness(StrategyIncubationMixin):
    def __init__(self, *, verification: dict, backfill_result: dict):
        self._verification = dict(verification)
        self._backfill_result = dict(backfill_result)
        self.snapshot_payload = None

    async def backfill_trade_position_links(self, strategy_id=None):
        return {"strategy_id": strategy_id, "position_count": 1, "fill_count": 2}

    async def backfill_strategy_signal_evidence_native(self, strategy_id=None, limit: int = 5000):
        return dict(self._backfill_result)

    async def get_execution_audit_verification(self, strategy_id=None):
        return dict(self._verification)

    async def upsert_execution_audit_snapshot(self, payload: dict):
        self.snapshot_payload = dict(payload or {})
        return dict(payload or {})


def test_backfill_strategy_signal_evidence_native_prefers_compile_stable_records():
    strategy = {
        "id": "strategy-compile-stable",
        "params": {
            "evidence_chain": {
                "evidences": [
                    {
                        "evidence_id": "ev-entry",
                        "source_type": "research_note",
                        "direction": "up",
                        "target_symbols": ["688336"],
                        "raw_confidence": 0.82,
                    }
                ]
            },
            "prediction_contract": {
                "claims": [
                    {
                        "claim_id": "claim-entry",
                        "evidence_ids": ["ev-entry"],
                    }
                ]
            },
            "claim_to_trade_plan_map": {
                "claim_to_trade_step_ids": {
                    "claim-entry": ["entry_step_1"],
                }
            },
        },
    }
    db = _SignalEvidenceBackfillDb(
        strategy=strategy,
        orders=[
            {
                "id": 101,
                "strategy_id": strategy["id"],
                "account_id": "acc-1",
                "signal_id": "sig-compile",
                "position_id": "pos-compile",
                "code": "688336",
                "direction": "buy",
                "signal_date": "2026-04-17",
            }
        ],
        trades=[],
    )

    result = asyncio.run(
        db.backfill_strategy_signal_evidence_native(strategy_id=strategy["id"])
    )

    assert result["saved_signal_count"] == 1
    assert result["compile_stable_signal_count"] == 1
    assert result["proxy_backfilled_signal_count"] == 0
    assert len(db.saved_rows) == 1
    saved = db.saved_rows[0]
    assert saved["signal_id"] == "sig-compile"
    assert saved["applied_claim_id"] == "claim-entry"
    assert saved["applied_trade_step_id"] == "entry_step_1"
    assert saved["payload"]["build_mode"] == "compile_stable_native"
    assert saved["payload"]["backfill_mode"] == "paper_execution_native_backfill_v1"


def test_backfill_strategy_signal_evidence_native_synthesizes_proxy_lineage_for_legacy_runtime():
    strategy = {
        "id": "strategy-legacy",
        "params": {
            "runtime_playbook": {
                "exit_policy": {"initial_stop_loss_pct": 0.08},
            }
        },
    }
    db = _SignalEvidenceBackfillDb(
        strategy=strategy,
        orders=[
            {
                "id": 201,
                "strategy_id": strategy["id"],
                "account_id": "acc-legacy",
                "signal_id": "sig-buy",
                "position_id": "pos-buy",
                "code": "688303",
                "direction": "buy",
                "signal_date": "2026-04-10",
            },
            {
                "id": 202,
                "strategy_id": strategy["id"],
                "account_id": "acc-legacy",
                "signal_id": "sig-sell",
                "position_id": "pos-buy",
                "code": "688303",
                "direction": "sell",
                "signal_date": "2026-04-11",
            },
        ],
        trades=[
            {
                "id": "trade-sell",
                "strategy_id": strategy["id"],
                "account_id": "acc-legacy",
                "signal_id": "sig-sell",
                "position_id": "pos-buy",
                "stock_code": "688303",
                "trade_type": "sell",
                "source_order_id": "202",
            }
        ],
    )

    result = asyncio.run(
        db.backfill_strategy_signal_evidence_native(strategy_id=strategy["id"])
    )

    assert result["saved_signal_count"] == 2
    assert result["proxy_backfilled_signal_count"] == 2
    assert result["compile_stable_signal_count"] == 0
    assert result["semantic_contract_gap_strategy_ids"] == [strategy["id"]]
    buy_row = next(row for row in db.saved_rows if row["signal_id"] == "sig-buy")
    sell_row = next(row for row in db.saved_rows if row["signal_id"] == "sig-sell")
    assert buy_row["applied_trade_step_id"] == "entry_step_backfill"
    assert buy_row["applied_claim_id"] == "legacy_claim_entry"
    assert buy_row["runtime_action_reason"] == "entry_execution_backfill"
    assert sell_row["applied_trade_step_id"] == "exit_step_backfill"
    assert sell_row["applied_claim_id"] == "legacy_claim_exit"
    assert sell_row["runtime_action_reason"] == "exit_execution_backfill"
    assert sell_row["payload"]["semantic_contract_status"] == "legacy_contract_gap"
    assert sell_row["payload"]["backfill_mode"] == "paper_execution_native_backfill_v1"


def test_run_execution_audit_acceptance_emits_gap_categories_and_actionable_todos():
    verification = {
        "coverage": {
            "paper_orders": {"total": 6, "position_id_ratio": 1.0},
            "paper_trades": {"total": 6, "position_id_ratio": 1.0},
        },
        "lineage_source": {
            "status": "native_backfilled",
            "semantic_contract": {
                "status": "legacy_contract_gap",
                "missing_fields": [
                    "claim_to_trade_plan_map",
                    "trade_plan_to_dsl_map",
                    "evidence_alignment_audit",
                ],
            },
        },
        "trade_round_trip": {
            "position_count": 3,
            "fill_count": 6,
            "audit_summary": {
                "realized_trade_count": 3,
                "incomplete_position_count": 0,
                "audit_ready_for_hard_gate": False,
                "execution_audit_gate_status": "insufficient_samples",
                "execution_audit_gate_reasons": ["realized_trade_count<20"],
            },
        },
        "recommendations": [],
    }
    db = _AcceptanceHarness(
        verification=verification,
        backfill_result={"saved_signal_count": 3, "proxy_backfilled_signal_count": 3},
    )

    result = asyncio.run(
        db.run_execution_audit_acceptance(strategy_id="strategy-gap", backfill=True)
    )

    assert result["acceptance_matrix"]["native_lineage_ready"] is True
    assert result["acceptance_matrix"]["trade_evidence_ready"] is True
    assert result["acceptance_matrix"]["hard_gate_ready"] is False
    assert result["execution_audit_snapshot_id"] == result["snapshot"]["snapshot_id"]
    assert result["execution_audit_gate_status"] == "insufficient_samples"
    assert "sample_gap" in result["gap_categories"]
    assert any(
        detail["blocker"] == "insufficient_samples"
        and detail["category"] == "sample_gap"
        for detail in result["blocker_details"]
    )
    assert any("production hard gate" in todo for todo in result["actionable_todos"])
    assert result["backfill_result"]["native_signal_evidence"]["saved_signal_count"] == 3


def test_normalize_trade_audit_summary_counts_excludes_open_positions_from_incomplete_gate():
    normalized = StrategyIncubationMixin()._normalize_trade_audit_summary_counts(
        {
            "mapped_position_count": 3,
            "realized_trade_count": 1,
            "incomplete_position_count": 3,
            "open_position_count": 2,
        }
    )

    assert normalized["raw_incomplete_position_count"] == 3
    assert normalized["open_position_count"] == 2
    assert normalized["incomplete_position_count"] == 1


def test_backfill_trade_position_links_expires_unfilled_order_only_intent(initialized_db):
    async def _run():
        adapter = SQLiteAdapter(path=initialized_db)
        await adapter.initialize()
        try:
            order = await adapter.save_paper_order(
                {
                    "strategy_id": "strategy-order-only",
                    "account_id": "paper-account",
                    "signal_date": "2026-06-12",
                    "source": "paper_trading_bridge",
                    "code": "002475",
                    "direction": "buy",
                    "shares": 100,
                    "price": 10.0,
                    "order_type": "marketable_limit",
                    "status": "pending",
                }
            )
            async with adapter.acquire() as conn:
                await conn.execute(
                    "UPDATE paper_orders SET created_at=datetime('now', '-3 day') WHERE id=$1",
                    order["id"],
                )

            result = await adapter.backfill_trade_position_links("strategy-order-only")
            async with adapter.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT id, status, reason, position_id FROM paper_orders WHERE id=$1",
                    order["id"],
                )
                trade_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM paper_trades WHERE source_order_id=$1",
                    str(order["id"]),
                )
        finally:
            await adapter.close()
        return result, dict(row), int(trade_count or 0)

    result, row, trade_count = asyncio.run(_run())

    assert result["unfilled_order_links"]["linked_order_count"] == 1
    assert row["status"] == "expired"
    assert row["reason"] == "stale_unfilled_order_position_backfill"
    assert row["position_id"] == f"ordpos_{row['id']}"
    assert trade_count == 0


def test_backfill_trade_position_links_pairs_legacy_buy_with_orphan_exit(initialized_db):
    async def _run():
        adapter = SQLiteAdapter(path=initialized_db)
        await adapter.initialize()
        try:
            strategy_id = "strategy-legacy-orphan-entry"
            account_id = "paper-legacy"
            code = "002475"
            buy_orders = []
            for index, quantity in enumerate((1200, 800, 400), start=1):
                order = await adapter.save_paper_order(
                    {
                        "strategy_id": strategy_id,
                        "account_id": account_id,
                        "signal_date": f"2026-06-1{index}",
                        "source": "paper_trading_bridge",
                        "code": code,
                        "direction": "buy",
                        "shares": quantity,
                        "price": 10.0 + index,
                        "order_type": "marketable_limit",
                        "status": "filled",
                        "filled_at": f"2026-06-1{index}T00:00:00+00:00",
                    }
                )
                buy_orders.append(order)
                await adapter.save_paper_trade(
                    {
                        "id": f"buy-{quantity}",
                        "strategy_id": strategy_id,
                        "account_id": account_id,
                        "stock_code": code,
                        "trade_type": "buy",
                        "price": 10.0 + index,
                        "quantity": quantity,
                        "amount": (10.0 + index) * quantity,
                        "commission": 1.0,
                        "trade_time": f"2026-06-1{index}T00:00:00+00:00",
                        "source_order_id": str(order["id"]),
                    }
                )
            sell_order = await adapter.save_paper_order(
                {
                    "strategy_id": strategy_id,
                    "account_id": account_id,
                    "signal_id": "sig-exit",
                    "position_id": "pos-exit-800",
                    "signal_date": "2026-06-18",
                    "source": "strategy_signal",
                    "code": code,
                    "direction": "sell",
                    "shares": 800,
                    "price": 14.0,
                    "order_type": "marketable_limit",
                    "status": "filled",
                    "filled_at": "2026-06-18T00:00:00+00:00",
                }
            )
            await adapter.save_paper_trade(
                {
                    "id": "sell-800",
                    "strategy_id": strategy_id,
                    "account_id": account_id,
                    "signal_id": "sig-exit",
                    "position_id": "pos-exit-800",
                    "stock_code": code,
                    "trade_type": "sell",
                    "price": 14.0,
                    "quantity": 800,
                    "amount": 11200.0,
                    "commission": 1.0,
                    "trade_time": "2026-06-18T00:00:00+00:00",
                    "source_order_id": str(sell_order["id"]),
                }
            )

            result = await adapter.backfill_trade_position_links(strategy_id)
            summary = await adapter.get_strategy_trade_audit_summary(strategy_id)
            async with adapter.acquire() as conn:
                exit_position = dict(
                    await conn.fetchrow(
                        "SELECT * FROM strategy_trade_positions WHERE position_id=$1",
                        "pos-exit-800",
                    )
                )
                coverage = dict(
                    await conn.fetchrow(
                        """
                        SELECT
                            COUNT(*) AS order_count,
                            COUNT(*) FILTER (
                                WHERE NULLIF(TRIM(COALESCE(position_id, '')), '') IS NOT NULL
                            ) AS linked_order_count
                        FROM paper_orders
                        WHERE strategy_id=$1
                        """,
                        strategy_id,
                    )
                )
                trade_coverage = dict(
                    await conn.fetchrow(
                        """
                        SELECT
                            COUNT(*) AS trade_count,
                            COUNT(*) FILTER (
                                WHERE NULLIF(TRIM(COALESCE(position_id, '')), '') IS NOT NULL
                            ) AS linked_trade_count
                        FROM paper_trades
                        WHERE strategy_id=$1
                        """,
                        strategy_id,
                    )
                )
                exact_buy = dict(
                    await conn.fetchrow(
                        "SELECT position_id FROM paper_trades WHERE id=$1",
                        "buy-800",
                    )
                )
                open_positions = [
                    dict(row)
                    for row in await conn.fetch(
                        """
                        SELECT position_id, status
                        FROM strategy_trade_positions
                        WHERE strategy_id=$1 AND status='open'
                        ORDER BY position_id
                        """,
                        strategy_id,
                    )
                ]
        finally:
            await adapter.close()
        return result, summary, exit_position, coverage, trade_coverage, exact_buy, open_positions

    result, summary, exit_position, coverage, trade_coverage, exact_buy, open_positions = asyncio.run(_run())

    assert result["legacy_orphan_entry_links"]["linked_entry_trade_count"] == 1
    assert result["open_entry_links"]["linked_open_entry_trade_count"] == 2
    assert exact_buy["position_id"] == "pos-exit-800"
    assert exit_position["status"] == "closed"
    assert exit_position["audit_eligible"] == 1
    assert exit_position["entry_shares"] == 800
    assert exit_position["exit_shares"] == 800
    assert coverage["order_count"] == coverage["linked_order_count"] == 4
    assert trade_coverage["trade_count"] == trade_coverage["linked_trade_count"] == 4
    assert summary["realized_trade_count"] == 1
    assert summary["incomplete_position_count"] == 0
    assert summary["open_position_count"] == 2
    assert {row["status"] for row in open_positions} == {"open"}


def test_backfill_trade_position_links_does_not_overclose_open_entry(initialized_db):
    async def _run():
        adapter = SQLiteAdapter(path=initialized_db)
        await adapter.initialize()
        try:
            strategy_id = "strategy-legacy-overexit"
            account_id = "paper-overexit"
            code = "600188"
            order = await adapter.save_paper_order(
                {
                    "strategy_id": strategy_id,
                    "account_id": account_id,
                    "signal_date": "2026-06-13",
                    "source": "paper_trading_bridge",
                    "code": code,
                    "direction": "buy",
                    "shares": 100,
                    "price": 20.0,
                    "status": "filled",
                    "filled_at": "2026-06-13T00:00:00+00:00",
                }
            )
            await adapter.save_paper_trade(
                {
                    "id": "buy-100",
                    "strategy_id": strategy_id,
                    "account_id": account_id,
                    "stock_code": code,
                    "trade_type": "buy",
                    "price": 20.0,
                    "quantity": 100,
                    "amount": 2000.0,
                    "commission": 1.0,
                    "trade_time": "2026-06-13T00:00:00+00:00",
                    "source_order_id": str(order["id"]),
                }
            )
            await adapter.save_paper_trade(
                {
                    "id": "sell-200",
                    "strategy_id": strategy_id,
                    "account_id": account_id,
                    "stock_code": code,
                    "trade_type": "sell",
                    "price": 21.0,
                    "quantity": 200,
                    "amount": 4200.0,
                    "commission": 1.0,
                    "trade_time": "2026-06-14T00:00:00+00:00",
                }
            )

            result = await adapter.backfill_trade_position_links(strategy_id)
            summary = await adapter.get_strategy_trade_audit_summary(strategy_id)
            async with adapter.acquire() as conn:
                sell = dict(
                    await conn.fetchrow(
                        "SELECT position_id FROM paper_trades WHERE id=$1",
                        "sell-200",
                    )
                )
                positions = [
                    dict(row)
                    for row in await conn.fetch(
                        """
                        SELECT position_id, status, entry_shares, exit_shares, audit_eligible
                        FROM strategy_trade_positions
                        WHERE strategy_id=$1
                        ORDER BY position_id
                        """,
                        strategy_id,
                    )
                ]
        finally:
            await adapter.close()
        return result, summary, sell, positions

    result, summary, sell, positions = asyncio.run(_run())

    assert result["open_entry_links"]["linked_open_entry_trade_count"] == 1
    assert result["orphan_sell_links"]["linked_orphan_sell_trade_count"] == 1
    assert result["orphan_exit_links"]["linked_exit_trade_count"] == 0
    assert sell["position_id"] == "orphanexit_sell-200"
    positions_by_id = {row["position_id"]: row for row in positions}
    assert positions_by_id["orphanexit_sell-200"] == {
        "position_id": "orphanexit_sell-200",
        "status": "orphaned_exit",
        "entry_shares": 0,
        "exit_shares": 200,
        "audit_eligible": 0,
    }
    assert positions_by_id["ordpos_1"] == {
        "position_id": "ordpos_1",
        "status": "open",
        "entry_shares": 100,
        "exit_shares": 0,
        "audit_eligible": 0,
    }
    assert summary["realized_trade_count"] == 0
    assert summary["open_position_count"] == 1
    assert summary["incomplete_position_count"] == 1


def test_trade_audit_summary_excludes_backtest_bootstrap_from_production_hard_gate(initialized_db):
    async def _run():
        adapter = SQLiteAdapter(path=initialized_db)
        await adapter.initialize()
        try:
            strategy_id = "strategy-bootstrap-exclusion"
            account_id = "paper-bootstrap-exclusion"
            await adapter.save_strategy(
                {
                    "id": strategy_id,
                    "name": "Bootstrap exclusion",
                    "strategy_type": "momentum",
                    "status": "incubating",
                    "params": {},
                }
            )

            bootstrap_entry = await adapter.save_paper_order(
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "signal_date": "2026-06-01",
                    "source": "backtest_bootstrap_import",
                    "code": "600188",
                    "direction": "buy",
                    "shares": 100,
                    "price": 10.0,
                    "order_type": "marketable_limit",
                    "status": "filled",
                    "reason": "backtest_bootstrap_entry",
                    "filled_at": "2026-06-01T09:30:00+00:00",
                    "signal_id": "bt-entry",
                    "position_id": "pos-bootstrap",
                }
            )
            bootstrap_exit = await adapter.save_paper_order(
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "signal_date": "2026-06-03",
                    "source": "backtest_bootstrap_import",
                    "code": "600188",
                    "direction": "sell",
                    "shares": 100,
                    "price": 12.0,
                    "order_type": "marketable_limit",
                    "status": "filled",
                    "reason": "backtest_bootstrap_exit",
                    "filled_at": "2026-06-03T09:30:00+00:00",
                    "signal_id": "bt-exit",
                    "position_id": "pos-bootstrap",
                }
            )
            real_entry = await adapter.save_paper_order(
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "signal_date": "2026-06-04",
                    "source": "incubation_factory",
                    "code": "600189",
                    "direction": "buy",
                    "shares": 100,
                    "price": 10.0,
                    "order_type": "marketable_limit",
                    "status": "filled",
                    "reason": "paper_signal_entry",
                    "filled_at": "2026-06-04T09:30:00+00:00",
                    "signal_id": "paper-entry",
                    "position_id": "pos-real",
                }
            )
            real_exit = await adapter.save_paper_order(
                {
                    "account_id": account_id,
                    "strategy_id": strategy_id,
                    "signal_date": "2026-06-06",
                    "source": "incubation_factory_stale_close",
                    "code": "600189",
                    "direction": "sell",
                    "shares": 100,
                    "price": 12.0,
                    "order_type": "marketable_limit",
                    "status": "filled",
                    "reason": "stale_paper_position_time_stop",
                    "filled_at": "2026-06-06T09:30:00+00:00",
                    "signal_id": "paper-exit",
                    "position_id": "pos-real",
                }
            )

            for position_id, code, entry_order, exit_order, signal_id in [
                ("pos-bootstrap", "600188", bootstrap_entry, bootstrap_exit, "bt-entry"),
                ("pos-real", "600189", real_entry, real_exit, "paper-entry"),
            ]:
                await adapter.save_strategy_trade_position(
                    {
                        "position_id": position_id,
                        "strategy_id": strategy_id,
                        "account_id": account_id,
                        "signal_id": signal_id,
                        "code": code,
                        "direction": "long",
                        "status": "closed",
                        "entry_order_id": str(entry_order["id"]),
                        "exit_order_id": str(exit_order["id"]),
                        "entry_shares": 100,
                        "exit_shares": 100,
                        "remaining_shares": 0,
                        "entry_amount": 1000.0,
                        "exit_amount": 1200.0,
                        "entry_commission": 1.0,
                        "exit_commission": 1.0,
                        "realized_pnl": 198.0,
                        "realized_return": 0.1978,
                        "pnl_conversion_efficiency": 0.1978,
                        "execution_conversion_efficiency": 0.5,
                        "trade_expectancy": 0.1978,
                        "audit_eligible": 1,
                        "opened_at": "2026-06-01T09:30:00+00:00",
                        "closed_at": "2026-06-06T09:30:00+00:00",
                        "last_trade_time": "2026-06-06T09:30:00+00:00",
                    }
                )

            return await adapter.get_strategy_trade_audit_summary(strategy_id)
        finally:
            await adapter.close()

    summary = asyncio.run(_run())

    assert summary["closed_round_trip_count"] == 2
    assert summary["bootstrap_round_trip_count"] == 1
    assert summary["real_paper_round_trip_count"] == 1
    assert summary["realized_trade_count"] == 1
    assert summary["execution_audit_gate_status"] == "insufficient_samples"
    assert summary["bootstrap_gate_ready"] is False
    assert summary["audit_ready_for_hard_gate"] is False


def test_refresh_trade_position_clears_stale_exit_fields(initialized_db):
    async def _run():
        adapter = SQLiteAdapter(path=initialized_db)
        await adapter.initialize()
        try:
            await adapter.save_strategy_trade_position(
                {
                    "position_id": "pos-stale-exit",
                    "strategy_id": "strategy-stale-exit",
                    "account_id": "paper-stale",
                    "code": "600188",
                    "status": "closed",
                    "entry_trade_id": "old-buy",
                    "exit_trade_id": "old-sell",
                    "entry_shares": 100,
                    "exit_shares": 100,
                    "remaining_shares": 0,
                    "realized_return": 0.12,
                    "audit_eligible": True,
                }
            )
            await adapter.save_strategy_trade_position_fill(
                {
                    "fill_id": "fill-new-buy",
                    "position_id": "pos-stale-exit",
                    "trade_id": "new-buy",
                    "order_id": "order-new-buy",
                    "strategy_id": "strategy-stale-exit",
                    "account_id": "paper-stale",
                    "code": "600188",
                    "fill_side": "buy",
                    "quantity": 100,
                    "price": 20.0,
                    "amount": 2000.0,
                    "commission": 1.0,
                    "trade_time": "2026-06-13T00:00:00+00:00",
                    "payload": {"source": "test"},
                }
            )
            row = await adapter.refresh_strategy_trade_position("pos-stale-exit")
        finally:
            await adapter.close()
        return row

    row = asyncio.run(_run())

    assert row["status"] == "open"
    assert row["entry_trade_id"] == "new-buy"
    assert row["exit_trade_id"] is None
    assert row["exit_order_id"] is None
    assert row["realized_return"] is None
    assert row["audit_eligible"] == 0
