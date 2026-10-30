from __future__ import annotations

import sqlite3

from aiask_quant_core.strategy_lifecycle_ledger import StrategyLifecycleLedger


def test_lifecycle_ledger_counts_only_non_excluded_execution_evidence() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """
            CREATE TABLE strategy_signals (id TEXT, strategy_id TEXT, signal REAL);
            CREATE TABLE paper_orders (id TEXT, strategy_id TEXT, account_id TEXT);
            CREATE TABLE paper_trades (id TEXT, source_order_id TEXT, account_id TEXT);
            CREATE TABLE strategy_trade_positions (strategy_id TEXT, account_id TEXT, status TEXT);
            CREATE TABLE signal_forward_returns (signal_id TEXT);
            CREATE TABLE strategy_execution_audit_snapshots (
                strategy_id TEXT, verdict_status TEXT, updated_at TEXT, metadata TEXT
            );
            CREATE TABLE paper_accounts (id TEXT, strategy_id TEXT);
            CREATE TABLE strategy_incubation_accounts (strategy_id TEXT, account_id TEXT);
            CREATE TABLE paper_evidence_exclusions (account_id TEXT PRIMARY KEY, excluded_at TEXT);

            INSERT INTO strategy_signals VALUES ('sig-1', 'strategy-1', 1);
            INSERT INTO paper_accounts VALUES ('excluded', 'strategy-1');
            INSERT INTO paper_accounts VALUES ('usable', 'strategy-1');
            INSERT INTO strategy_incubation_accounts VALUES ('strategy-1', 'excluded');
            INSERT INTO strategy_incubation_accounts VALUES ('strategy-1', 'usable');
            INSERT INTO paper_orders VALUES ('order-excluded', 'strategy-1', 'excluded');
            INSERT INTO paper_orders VALUES ('order-usable', 'strategy-1', 'usable');
            INSERT INTO paper_trades VALUES ('trade-excluded', 'order-excluded', 'excluded');
            INSERT INTO paper_trades VALUES ('trade-usable', 'order-usable', 'usable');
            INSERT INTO strategy_trade_positions VALUES ('strategy-1', 'excluded', 'open');
            INSERT INTO strategy_trade_positions VALUES ('strategy-1', 'usable', 'open');
            INSERT INTO strategy_trade_positions VALUES ('strategy-1', 'excluded', 'closed');
            INSERT INTO strategy_trade_positions VALUES ('strategy-1', 'usable', 'closed');
            INSERT INTO paper_evidence_exclusions VALUES ('excluded', '2026-10-30 12:00:00');
            INSERT INTO strategy_execution_audit_snapshots
                VALUES ('strategy-1', 'passed', '2026-10-30 11:00:00', '{}');
            """
        )

        ledger = StrategyLifecycleLedger(conn)
        evidence = ledger._query_evidence_counts("strategy-1")
        assert evidence["signal_count"] == 1
        assert evidence["order_count"] == 1
        assert evidence["trade_count"] == 1
        assert evidence["open_position_count"] == 1
        assert evidence["closed_position_count"] == 1
        assert evidence["audit_snapshot_count"] == 0
        assert evidence["hard_gate_passed"] is False

        conn.execute(
            "UPDATE strategy_execution_audit_snapshots "
            "SET metadata = ? WHERE strategy_id = ?",
            ('{"evidence_exclusion_ids":["excluded"]}', "strategy-1"),
        )
        current_evidence = ledger._query_evidence_counts("strategy-1")
        assert current_evidence["audit_snapshot_count"] == 1
        assert current_evidence["hard_gate_passed"] is True

        conn.execute("DROP TABLE paper_evidence_exclusions")
        legacy_schema_evidence = ledger._query_evidence_counts("strategy-1")
        assert legacy_schema_evidence["order_count"] == 2
        assert legacy_schema_evidence["trade_count"] == 2
        assert legacy_schema_evidence["open_position_count"] == 2
        assert legacy_schema_evidence["closed_position_count"] == 2
        assert legacy_schema_evidence["audit_snapshot_count"] == 0
    finally:
        conn.close()
