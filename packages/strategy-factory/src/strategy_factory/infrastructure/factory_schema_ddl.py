"""Factory-domain table DDL, owned by Strategy Factory.

Phase 2 of the extraction plan, completed: this is the physical home of the
factory-domain schema. It moved here from
``aiask_quant_core.storage.sqlite.schema_strategy`` unchanged — the module was
self-contained (``logging`` plus ``conn.execute``, nothing else), so relocating it
carries no behavioural risk. quant-core keeps a re-export shim for one major
version.

Single entry point, idempotent:

    await init_strategy_tables(conn, sqlite_python_enabled)

Idempotence comes from ``CREATE TABLE IF NOT EXISTS`` / ``ADD COLUMN IF NOT
EXISTS``. Driver-side vector DDL is gated by ``sqlite_python_enabled``.

Scope note: a handful of ``strategy_vector_*`` tables declared here belong to the
host's vector platform, not the factory domain — verified by reference count, and
excluded from the factory inventory by ``VECTOR_PLATFORM_TABLES`` in the
fingerprint module. They stay in this file because splitting live DDL by
consumer would risk the tables themselves; ownership is tracked by the
inventory, not by file boundaries.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

async def init_strategy_tables(conn, sqlite_python_enabled: bool = False) -> None:
    """Create / migrate all strategy-related tables.

    Args:
        conn: an async SQLite-compatible connection.
        sqlite_python_enabled: whether database-side vector search is enabled.
    """

    # 24. 策略超市表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategies (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            author_id TEXT DEFAULT 'default',
            strategy_type TEXT NOT NULL,
            params TEXT DEFAULT '{}',
            factor_weights TEXT DEFAULT '{}',
            status TEXT DEFAULT 'draft',
            tags TEXT DEFAULT '{}',
            backtest_artifact_id TEXT,
            subscriber_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategies_status ON strategies(status);
        CREATE INDEX IF NOT EXISTS idx_strategies_type ON strategies(strategy_type);
        CREATE INDEX IF NOT EXISTS idx_strategies_author ON strategies(author_id);

        CREATE TABLE IF NOT EXISTS strategy_metrics (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            period TEXT DEFAULT 'all',
            total_return REAL,
            annual_return REAL,
            sharpe_ratio REAL,
            max_drawdown REAL,
            win_rate REAL,
            calmar_ratio REAL,
            trade_count INTEGER,
            computed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, period)
        );

        CREATE TABLE IF NOT EXISTS strategy_reviews (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            rating INTEGER CHECK (rating >= 1 AND rating <= 5),
            comment TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_reviews_strategy ON strategy_reviews(strategy_id);

        CREATE TABLE IF NOT EXISTS strategy_subscriptions (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            subscribed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_subs_user ON strategy_subscriptions(user_id);

        CREATE TABLE IF NOT EXISTS strategy_paper_sessions (
            id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL,
            account_id TEXT NOT NULL,
            session_type TEXT NOT NULL DEFAULT 'personal_paper',
            source_strategy_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_used_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(user_id, strategy_id, session_type)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_paper_sessions_user
            ON strategy_paper_sessions(user_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_paper_sessions_strategy
            ON strategy_paper_sessions(strategy_id, updated_at DESC);
    """)

    # 28. 策略前向信号表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_signals (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            signal_date TEXT NOT NULL,
            code TEXT NOT NULL,
            signal SMALLINT NOT NULL,
            score REAL,
            execution_semantic_mode TEXT,
            action_source TEXT,
            event_action TEXT,
            action_reason TEXT,
            signal_metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, signal_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_signals_strategy
            ON strategy_signals(strategy_id, signal_date DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_signals_date
            ON strategy_signals(signal_date DESC);
    """)
    await conn.execute("""
        ALTER TABLE strategy_signals
        ADD COLUMN IF NOT EXISTS execution_semantic_mode TEXT;
    """)
    await conn.execute("""
        ALTER TABLE strategy_signals
        ADD COLUMN IF NOT EXISTS action_source TEXT;
    """)
    await conn.execute("""
        ALTER TABLE strategy_signals
        ADD COLUMN IF NOT EXISTS event_action TEXT;
    """)
    await conn.execute("""
        ALTER TABLE strategy_signals
        ADD COLUMN IF NOT EXISTS action_reason TEXT;
    """)
    await conn.execute("""
        ALTER TABLE strategy_signals
        ADD COLUMN IF NOT EXISTS signal_metadata TEXT DEFAULT '{}';
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_signal_event_snapshots (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            code TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            latest_bar_date TEXT,
            latest_bar_signal SMALLINT NOT NULL DEFAULT 0,
            execution_semantic_mode TEXT,
            latest_event_index INTEGER,
            latest_event_date TEXT,
            latest_event_signal SMALLINT,
            latest_event_action TEXT,
            latest_event_action_source TEXT,
            latest_event_reason TEXT,
            latest_event_units REAL,
            latest_entry_date TEXT,
            latest_exit_date TEXT,
            event_count INTEGER NOT NULL DEFAULT 0,
            recent_events TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, code, as_of_date)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_signal_event_snapshots_lookup
            ON strategy_signal_event_snapshots(strategy_id, code, as_of_date DESC, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_signal_event_snapshots_strategy
            ON strategy_signal_event_snapshots(strategy_id, as_of_date DESC, updated_at DESC);
    """)
    await conn.execute("""
        DROP VIEW IF EXISTS strategy_signal_event_snapshots_latest;
        CREATE VIEW IF NOT EXISTS strategy_signal_event_snapshots_latest AS
        SELECT
            id,
            strategy_id,
            code,
            as_of_date,
            latest_bar_date,
            latest_bar_signal,
            execution_semantic_mode,
            latest_event_index,
            latest_event_date,
            latest_event_signal,
            latest_event_action,
            latest_event_action_source,
            latest_event_reason,
            latest_event_units,
            latest_entry_date,
            latest_exit_date,
            event_count,
            recent_events,
            metadata,
            created_at,
            updated_at
        FROM (
            SELECT *,
                   ROW_NUMBER() OVER (
                       PARTITION BY strategy_id, code
                       ORDER BY as_of_date DESC, updated_at DESC, id DESC
                   ) AS rn
            FROM strategy_signal_event_snapshots
        )
        WHERE rn = 1;
    """)

    # 29. 前向收益验证表
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_forward_returns (
            id INTEGER PRIMARY KEY,
            signal_id INTEGER NOT NULL,
            forward_days INTEGER NOT NULL,
            actual_return REAL,
            calculated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(signal_id, forward_days)
        );
        CREATE INDEX IF NOT EXISTS idx_signal_fwd_signal
            ON signal_forward_returns(signal_id);
    """)

    # 29.2 交易预测合同与结果表（P0: 只做冻结和持久化，不计算评分）
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_trade_predictions (
            prediction_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            prediction_as_of TEXT NOT NULL,
            target_trading_date TEXT NOT NULL,
            direction TEXT NOT NULL,
            confidence REAL,
            horizon TEXT,
            contract_version TEXT NOT NULL,
            contract_source TEXT DEFAULT 'explicit',
            contract_hash TEXT NOT NULL,
            contract_json TEXT DEFAULT '{}',
            prediction_status TEXT DEFAULT 'pending',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_predictions_strategy
            ON strategy_trade_predictions(strategy_id, target_trading_date DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_predictions_stock_date
            ON strategy_trade_predictions(stock_code, target_trading_date DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_predictions_status
            ON strategy_trade_predictions(prediction_status, target_trading_date DESC);

        CREATE TABLE IF NOT EXISTS strategy_trade_prediction_outcomes (
            outcome_id TEXT PRIMARY KEY,
            prediction_id TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            actual_trading_date TEXT,
            score_version TEXT NOT NULL,
            score_status TEXT DEFAULT 'pending',
            trade_prediction_score REAL,
            outcome_json TEXT DEFAULT '{}',
            data_quality_status TEXT DEFAULT 'unknown',
            metadata TEXT DEFAULT '{}',
            calculated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_prediction_outcomes_prediction
            ON strategy_trade_prediction_outcomes(prediction_id, score_version);
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_prediction_outcomes_strategy
            ON strategy_trade_prediction_outcomes(strategy_id, actual_trading_date DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_prediction_outcomes_score
            ON strategy_trade_prediction_outcomes(score_status, trade_prediction_score DESC);
    """)

    # 29.1 向后兼容：published -> listed 迁移
    await conn.execute("""
        UPDATE strategies SET status = 'listed' WHERE status = 'published';
    """)

    # 30. 策略血统表（策略工厂溯源）
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_lineage (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            parent_id TEXT,
            spawn_reason TEXT NOT NULL,
            birth_regime TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_lineage_sid ON strategy_lineage(strategy_id);")

    # 31. 策略淘汰日志
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_elimination_log (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL,
            elimination_date TEXT NOT NULL,
            red_flags TEXT DEFAULT '[]',
            reason TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_elim_sid ON strategy_elimination_log(strategy_id);")

    # 32. 每日快照历史（策略工厂输入数据）
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_snapshot_history (
            id INTEGER PRIMARY KEY,
            snapshot_date TEXT NOT NULL UNIQUE,
            fear_greed_index INTEGER,
            fg_components TEXT DEFAULT '{}',
            factor_ic TEXT DEFAULT '{}',
            factor_ic_trend TEXT DEFAULT '{}',
            factor_research TEXT DEFAULT '{}',
            north_fund_3d_net REAL,
            margin_5d_change_pct REAL,
            hot_sectors TEXT DEFAULT '[]',
            cold_sectors TEXT DEFAULT '[]',
            listed_count INTEGER DEFAULT 0,
            category_counts TEXT DEFAULT '{}',
            summary TEXT DEFAULT '{}',
            completeness TEXT DEFAULT '{}',
            sources TEXT DEFAULT '{}',
            parameter_distribution_samples TEXT DEFAULT '[]',
            parameter_distribution_summary TEXT DEFAULT '{}',
            failure_reasons TEXT DEFAULT '[]',
            missing_fields TEXT DEFAULT '[]',
            degraded INTEGER DEFAULT FALSE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS summary TEXT DEFAULT '{}';
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS completeness TEXT DEFAULT '{}';
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS factor_research TEXT DEFAULT '{}';
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS sources TEXT DEFAULT '{}';
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS parameter_distribution_samples TEXT DEFAULT '[]';
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS parameter_distribution_summary TEXT DEFAULT '{}';
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS failure_reasons TEXT DEFAULT '[]';
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS missing_fields TEXT DEFAULT '[]';
    """)
    await conn.execute("""
        ALTER TABLE daily_snapshot_history
        ADD COLUMN IF NOT EXISTS degraded INTEGER DEFAULT FALSE;
    """)

    # 33. 策略质检报告
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_quality_reports (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            report_type TEXT NOT NULL DEFAULT 'submission',
            passed INTEGER DEFAULT FALSE,
            summary TEXT DEFAULT '{}',
            quality_gate TEXT DEFAULT '{}',
            validation_report TEXT DEFAULT '{}',
            risk_report TEXT DEFAULT '{}',
            dedup_report TEXT DEFAULT '{}',
            backtest_metrics TEXT DEFAULT '{}',
            snapshot TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, report_type)
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_execution_audit_snapshots (
            strategy_id TEXT PRIMARY KEY REFERENCES strategies(id) ON DELETE CASCADE,
            snapshot_id TEXT NOT NULL UNIQUE,
            as_of_date TEXT,
            source_run_id TEXT,
            factory_run_id TEXT,
            correlation_id TEXT,
            trace_id TEXT,
            submission_lane TEXT,
            parent_task_run_id TEXT,
            source_action TEXT,
            verdict_status TEXT NOT NULL DEFAULT 'missing',
            verdict_reasons TEXT DEFAULT '[]',
            execution_hard_gate_passed INTEGER DEFAULT FALSE,
            verification TEXT DEFAULT '{}',
            acceptance TEXT DEFAULT '{}',
            audit_summary TEXT DEFAULT '{}',
            snapshot TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_closure_snapshots (
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            snapshot_type TEXT NOT NULL,
            snapshot_id TEXT NOT NULL UNIQUE,
            as_of_date TEXT,
            source_run_id TEXT,
            factory_run_id TEXT,
            correlation_id TEXT,
            trace_id TEXT,
            submission_lane TEXT,
            parent_task_run_id TEXT,
            source_action TEXT,
            snapshot TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(strategy_id, snapshot_type)
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_topn_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            as_of_date TEXT,
            trace_id TEXT,
            correlation_id TEXT,
            source_action TEXT,
            universe_count INTEGER DEFAULT 0,
            eligible_count INTEGER DEFAULT 0,
            topn_n INTEGER DEFAULT 20,
            selection_rules TEXT DEFAULT '{}',
            constituents TEXT DEFAULT '[]',
            portfolio_candidate_id TEXT,
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_full_market_scores (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            as_of_date TEXT,
            trace_id TEXT,
            correlation_id TEXT,
            code TEXT NOT NULL,
            rank INTEGER NOT NULL,
            composite_score REAL DEFAULT 0,
            industry TEXT,
            market_cap REAL,
            component_scores TEXT DEFAULT '{}',
            family_candidates TEXT DEFAULT '[]',
            eligible INTEGER DEFAULT TRUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, code)
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_quality_reports_sid ON strategy_quality_reports(strategy_id, updated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_execution_audit_snapshots_as_of ON strategy_execution_audit_snapshots(as_of_date DESC, updated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_execution_audit_snapshots_correlation ON strategy_execution_audit_snapshots(correlation_id, updated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_execution_audit_snapshots_factory_run ON strategy_execution_audit_snapshots(factory_run_id, updated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_closure_snapshots_as_of ON strategy_closure_snapshots(snapshot_type, as_of_date DESC, updated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_closure_snapshots_correlation ON strategy_closure_snapshots(correlation_id, updated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_closure_snapshots_factory_run ON strategy_closure_snapshots(factory_run_id, updated_at DESC);"
    )
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_strategy_factory_topn_snapshots_run_id ON strategy_factory_topn_snapshots(run_id);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_topn_snapshots_as_of ON strategy_factory_topn_snapshots(as_of_date DESC, updated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_topn_snapshots_correlation ON strategy_factory_topn_snapshots(correlation_id, updated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_full_market_scores_run_id ON strategy_factory_full_market_scores(run_id, rank ASC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_full_market_scores_snapshot ON strategy_factory_full_market_scores(snapshot_id, rank ASC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_full_market_scores_code ON strategy_factory_full_market_scores(code, as_of_date DESC);"
    )

    # 34. 策略状态事件（轻量 append-only 审计流）
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_status_events (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            from_status TEXT,
            to_status TEXT NOT NULL,
            event_type TEXT NOT NULL DEFAULT 'status_change',
            actor_id TEXT DEFAULT 'system',
            reason TEXT,
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_status_events_sid ON strategy_status_events(strategy_id, created_at DESC);"
    )
    await conn.execute("""
        UPDATE strategy_status_events SET from_status = 'listed' WHERE from_status = 'published';
    """)
    await conn.execute("""
        UPDATE strategy_status_events SET to_status = 'listed' WHERE to_status = 'published';
    """)
    await conn.execute("""
        DELETE FROM strategy_status_events
        WHERE event_type = 'status_change'
          AND from_status = 'listed'
          AND to_status = 'listed';
    """)

    # 34.1 策略领域事件（通用 append-only 事件流 / outbox 基础）
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_domain_events (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
            aggregate_type TEXT NOT NULL DEFAULT 'strategy',
            aggregate_id TEXT,
            event_type TEXT NOT NULL,
            source TEXT DEFAULT 'system',
            severity TEXT DEFAULT 'info',
            correlation_id TEXT,
            payload TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_domain_events_sid ON strategy_domain_events(strategy_id, created_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_domain_events_aggregate ON strategy_domain_events(aggregate_type, aggregate_id, created_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_domain_events_type ON strategy_domain_events(event_type, created_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_domain_events_correlation ON strategy_domain_events(correlation_id, created_at DESC);"
    )

    # 35. 策略工厂运行历史
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_runs (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            elapsed_seconds REAL DEFAULT 0,
            execution_mode TEXT DEFAULT 'legacy_primary',
            engine_version TEXT DEFAULT 'strategy_factory.v2',
            parity_status TEXT,
            summary TEXT DEFAULT '{}',
            stages TEXT DEFAULT '{}',
            snapshot_summary TEXT DEFAULT '{}',
            artifact_refs TEXT DEFAULT '[]',
            parity_result TEXT DEFAULT '{}',
            error TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "ALTER TABLE strategy_factory_runs ADD COLUMN IF NOT EXISTS execution_mode TEXT DEFAULT 'legacy_primary';"
    )
    await conn.execute(
        "ALTER TABLE strategy_factory_runs ADD COLUMN IF NOT EXISTS engine_version TEXT DEFAULT 'strategy_factory.v2';"
    )
    await conn.execute(
        "ALTER TABLE strategy_factory_runs ADD COLUMN IF NOT EXISTS parity_status TEXT;"
    )
    await conn.execute(
        "ALTER TABLE strategy_factory_runs ADD COLUMN IF NOT EXISTS artifact_refs TEXT DEFAULT '[]';"
    )
    await conn.execute(
        "ALTER TABLE strategy_factory_runs ADD COLUMN IF NOT EXISTS parity_result TEXT DEFAULT '{}';"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_runs_started_at ON strategy_factory_runs(started_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_runs_status ON strategy_factory_runs(status, started_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_runs_execution_mode ON strategy_factory_runs(execution_mode, started_at DESC);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_run_artifacts (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            artifact_version TEXT NOT NULL,
            payload_json TEXT DEFAULT '{}',
            payload_hash TEXT,
            storage_mode TEXT DEFAULT 'inline_json',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_run_artifacts_run_id ON strategy_factory_run_artifacts(run_id, created_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_run_artifacts_type ON strategy_factory_run_artifacts(artifact_type, created_at DESC);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_scheduler_state (
            state_key TEXT PRIMARY KEY,
            payload_json TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_scheduler_state_updated_at ON strategy_factory_scheduler_state(updated_at DESC);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_dispatches (
            id INTEGER PRIMARY KEY,
            dispatch_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            execution_mode TEXT DEFAULT 'legacy_primary',
            requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            started_at TEXT,
            completed_at TEXT,
            run_id TEXT,
            message TEXT,
            error TEXT,
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_dispatches_requested_at ON strategy_factory_dispatches(requested_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_strategy_factory_dispatches_status ON strategy_factory_dispatches(status, requested_at DESC);"
    )

    # 35.1 Cross-factory control-plane ledger.  The legacy Strategy Factory
    # run/dispatch tables above remain the domain audit source for old callers.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS factory_run_ledger (
            run_id TEXT PRIMARY KEY,
            factory_name TEXT NOT NULL,
            status TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'once',
            request_id TEXT,
            idempotency_key TEXT NOT NULL,
            execution_owner TEXT NOT NULL DEFAULT 'aiask_factory_worker',
            owner_id TEXT,
            phase TEXT,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            heartbeat_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            pid INTEGER,
            exit_code INTEGER,
            params_json TEXT NOT NULL DEFAULT '{}',
            summary_json TEXT NOT NULL DEFAULT '{}',
            evidence_refs_json TEXT NOT NULL DEFAULT '[]',
            quality_flags_json TEXT NOT NULL DEFAULT '[]',
            blockers_json TEXT NOT NULL DEFAULT '[]',
            error TEXT,
            raw_status TEXT,
            contract_version TEXT NOT NULL DEFAULT 'aiask.factory_run.v1',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(factory_name, idempotency_key)
        );
        CREATE INDEX IF NOT EXISTS idx_factory_run_ledger_factory_requested
            ON factory_run_ledger(factory_name, requested_at DESC);
        CREATE INDEX IF NOT EXISTS idx_factory_run_ledger_status_requested
            ON factory_run_ledger(status, requested_at DESC);

        CREATE TABLE IF NOT EXISTS factory_run_locks (
            factory_name TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at_epoch REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_factory_run_locks_expiry
            ON factory_run_locks(expires_at_epoch);
    """)

    # 35.2 策略工厂事件簇 / 主题 / 暴露 / 信号
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_event_clusters (
            id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            event_type TEXT DEFAULT 'macro',
            event_name TEXT NOT NULL,
            event_scope TEXT DEFAULT 'market',
            summary TEXT,
            direction TEXT DEFAULT 'neutral',
            intensity REAL DEFAULT 0,
            horizon TEXT DEFAULT 'swing_5_20d',
            confidence REAL DEFAULT 0,
            source_count INTEGER DEFAULT 0,
            source_types TEXT DEFAULT '[]',
            entities TEXT DEFAULT '[]',
            commodities TEXT DEFAULT '[]',
            regions TEXT DEFAULT '[]',
            themes TEXT DEFAULT '[]',
            evidence TEXT DEFAULT '{}',
            occurred_at TEXT,
            last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'active',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_event_clusters_status ON strategy_factory_event_clusters(status, last_seen_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_event_clusters_type ON strategy_factory_event_clusters(event_type, last_seen_at DESC);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_theme_definitions (
            id INTEGER PRIMARY KEY,
            theme_code TEXT NOT NULL UNIQUE,
            theme_name TEXT NOT NULL,
            parent_theme_code TEXT,
            description TEXT,
            direction_rule TEXT,
            aliases TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            active INTEGER DEFAULT TRUE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_theme_definitions_active ON strategy_factory_theme_definitions(active, theme_code);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_company_theme_exposures (
            id INTEGER PRIMARY KEY,
            symbol TEXT NOT NULL,
            theme_code TEXT NOT NULL,
            exposure_type TEXT DEFAULT 'revenue',
            direction TEXT DEFAULT 'positive',
            exposure_score REAL DEFAULT 0,
            evidence TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, theme_code, exposure_type)
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_company_theme_exposures_theme ON strategy_factory_company_theme_exposures(theme_code, exposure_score DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_company_theme_exposures_symbol ON strategy_factory_company_theme_exposures(symbol, updated_at DESC);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_event_signals (
            id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            theme_code TEXT NOT NULL DEFAULT '',
            direction TEXT DEFAULT 'positive',
            theme_score REAL DEFAULT 0,
            exposure_score REAL DEFAULT 0,
            price_confirm_score REAL DEFAULT 0,
            flow_confirm_score REAL DEFAULT 0,
            fundamental_confirm_score REAL DEFAULT 0,
            final_score REAL DEFAULT 0,
            rationale TEXT,
            evidence TEXT DEFAULT '{}',
            observed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(event_id, symbol, theme_code)
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_event_signals_event ON strategy_factory_event_signals(event_id, final_score DESC, observed_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_event_signals_symbol ON strategy_factory_event_signals(symbol, observed_at DESC);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_task_evidence (
            id INTEGER PRIMARY KEY,
            task_key TEXT NOT NULL,
            event_id TEXT,
            theme_code TEXT DEFAULT '',
            symbol TEXT,
            evidence_type TEXT NOT NULL,
            weight REAL DEFAULT 0,
            evidence_payload TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_task_evidence_task ON strategy_factory_task_evidence(task_key, created_at DESC);"
    )

    # =========================================================================
    # PR-1: 事件驱动主题联动 · 主题图 schema (2026-05-14)
    # =========================================================================

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_theme_nodes (
            theme_code TEXT PRIMARY KEY,
            theme_name TEXT NOT NULL,
            parent_theme_code TEXT,
            breadth TEXT NOT NULL DEFAULT 'medium',
            default_horizon TEXT NOT NULL DEFAULT 'swing_5_20d',
            aliases TEXT DEFAULT '[]',
            industry_tags TEXT DEFAULT '[]',
            description TEXT,
            shock_detection_profile TEXT NOT NULL DEFAULT 'fast',
            benchmark_index_code TEXT DEFAULT '000300',
            manual_locked INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_by TEXT
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_theme_nodes_active ON strategy_factory_theme_nodes(is_active, theme_code);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_theme_edges (
            edge_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_theme_code TEXT NOT NULL,
            target_theme_code TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            direction_sign INTEGER NOT NULL,
            magnitude_factor REAL NOT NULL DEFAULT 0.5,
            lag_days INTEGER NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0.5,
            confidence_source TEXT NOT NULL DEFAULT 'manual',
            manual_confidence_backup REAL,
            manual_magnitude_backup REAL,
            manual_locked INTEGER NOT NULL DEFAULT 0,
            last_regression_at TEXT,
            evidence TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_by TEXT,
            UNIQUE(source_theme_code, target_theme_code, relation_type)
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_theme_edges_source ON strategy_factory_theme_edges(source_theme_code) WHERE is_active = 1;"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_theme_edges_target ON strategy_factory_theme_edges(target_theme_code) WHERE is_active = 1;"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_event_injections (
            event_id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            event_name TEXT NOT NULL,
            event_type TEXT NOT NULL,
            direction TEXT,
            confidence REAL NOT NULL,
            intensity REAL NOT NULL,
            horizon TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT 'theme',
            primary_themes TEXT NOT NULL DEFAULT '[]',
            rationale TEXT,
            evidence TEXT,
            valid_from TEXT NOT NULL,
            valid_until TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_review',
            operator_id TEXT,
            approver_id TEXT,
            approved_at TEXT,
            actual_outcome TEXT,
            outcome_notes TEXT,
            outcome_recorded_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_injections_status ON strategy_factory_event_injections(status, valid_until);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_injections_source ON strategy_factory_event_injections(source, created_at DESC);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_event_task_lineage (
            lineage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dedupe_key TEXT,
            event_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            theme_code TEXT NOT NULL,
            impact_direction TEXT NOT NULL,
            impact_magnitude REAL NOT NULL,
            target_symbols TEXT NOT NULL DEFAULT '[]',
            target_count INTEGER NOT NULL,
            breadth_resolved TEXT NOT NULL,
            generated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            gate_1_passed INTEGER,
            gate_2_passed INTEGER,
            gate_3_passed INTEGER,
            strategies_submitted INTEGER DEFAULT 0
        );
    """)
    await conn.execute(
        "ALTER TABLE strategy_factory_event_task_lineage ADD COLUMN IF NOT EXISTS dedupe_key TEXT;"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_task_lineage_dedupe ON strategy_factory_event_task_lineage(dedupe_key);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_task_lineage_event ON strategy_factory_event_task_lineage(event_id, generated_at DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_task_lineage_task ON strategy_factory_event_task_lineage(task_id);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_theme_exposure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            theme_code TEXT NOT NULL,
            exposure_score REAL NOT NULL DEFAULT 0,
            industry_match_level INTEGER DEFAULT 0,
            name_match_score REAL DEFAULT 0,
            mainbz_match_score REAL DEFAULT 0,
            historical_beta REAL DEFAULT 0,
            evidence TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(symbol, theme_code)
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_theme_exposure_theme ON strategy_factory_theme_exposure(theme_code, exposure_score DESC);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_theme_exposure_symbol ON strategy_factory_theme_exposure(symbol);"
    )

    # ---------------------------------------------------------------------
    # PR-B1 (2026-05-24): 事件驱动 outbox 消费状态旁路表
    #
    # 方案 §6 Phase 0 + §8 决议：v1 直接复用 strategy_domain_events 作为
    # append-only outbox 基底。但孵化工厂已经在该表上密集写入（intake/
    # hit_rate/accelerator/alert/feedback 五个 writer，生产 ~5,783 行），
    # 不应该给热写表加新列。改为独立旁路表，按 (event_id, theme_code,
    # target_symbols_signature) 维度记录消费状态：
    #
    #   - dedupe_key       : 上层（event_task_generator / publisher）算出的稳
    #                        定消费指纹，主键即去重边界。
    #   - source_event_id  : 关联到 strategy_factory_event_injections.event_id
    #                        或 strategy_domain_events.id（不强制外键，避免
    #                        循环依赖）。
    #   - status           : pending / processing / processed / failed / abandoned
    #   - attempts         : 当前重试次数（publisher 单 worker 串行扫描时累加）
    #   - last_error       : 最近一次失败原因
    #   - claimed_at / processed_at / failed_at : 时间审计三元组
    # 设计原则：
    #   - 不复制业务字段（event_name/intensity 等），始终通过 dedupe_key 反查源表
    #   - 主键 dedupe_key 让幂等约束在 SQLite 层强制
    #   - 不影响孵化工厂对 strategy_domain_events 的现有写入热路径
    # ---------------------------------------------------------------------
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_event_outbox_state (
            dedupe_key TEXT PRIMARY KEY,
            source_event_id TEXT NOT NULL,
            theme_code TEXT,
            event_type TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            claimed_at TEXT,
            processed_at TEXT,
            failed_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_outbox_state_status "
        "ON strategy_factory_event_outbox_state(status, created_at);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_event_outbox_state_source "
        "ON strategy_factory_event_outbox_state(source_event_id, status);"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_task_evidence_event ON strategy_factory_task_evidence(event_id, theme_code, created_at DESC);"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_factory_market_internals (
            id INTEGER PRIMARY KEY,
            snapshot_date TEXT NOT NULL UNIQUE,
            engine TEXT DEFAULT 'local_db_rule_v1',
            symbol_count INTEGER DEFAULT 0,
            trend_up_count INTEGER DEFAULT 0,
            trend_down_count INTEGER DEFAULT 0,
            avg_return_5d REAL DEFAULT 0,
            avg_return_20d REAL DEFAULT 0,
            avg_volume_ratio REAL DEFAULT 1,
            breadth_score REAL DEFAULT 0,
            margin_proxy_5d_change_pct REAL DEFAULT 0,
            hot_sectors TEXT DEFAULT '[]',
            cold_sectors TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_factory_market_internals_date ON strategy_factory_market_internals(snapshot_date DESC);"
    )
    # Incubation accounts & metrics
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_incubation_accounts (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            account_id TEXT NOT NULL,
            stage TEXT DEFAULT 'warmup',
            status TEXT DEFAULT 'active',
            source_run_id TEXT,
            metadata TEXT DEFAULT '{}',
            bound_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, account_id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_incubation_accounts_sid
            ON strategy_incubation_accounts(strategy_id, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_incubation_accounts_acct
            ON strategy_incubation_accounts(account_id, updated_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_incubation_metrics (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            account_id TEXT,
            metric_date TEXT NOT NULL,
            stage TEXT DEFAULT 'warmup',
            total_value REAL,
            cash REAL,
            market_value REAL,
            nav REAL,
            daily_return REAL,
            max_drawdown REAL,
            sharpe_ratio REAL,
            hit_rate_5d REAL,
            hit_rate_lcb_5d REAL,
            skill_lcb_5d REAL,
            effective_n_5d INTEGER,
            recent_hit_rate_5d REAL,
            recent_skill_lcb_5d REAL,
            stability_gap_5d REAL,
            forward_ic_5d REAL,
            forward_sharpe_5d REAL,
            total_signals INTEGER DEFAULT 0,
            total_orders INTEGER DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            turnover_rate REAL,
            exposure_rate REAL,
            alpha_decay REAL,
            drift_score REAL,
            blockers TEXT DEFAULT '[]',
            risk_flags TEXT DEFAULT '[]',
            decision TEXT,
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id, metric_date)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_incubation_metrics_sid
            ON strategy_incubation_metrics(strategy_id, metric_date DESC);

        CREATE TABLE IF NOT EXISTS strategy_incubation_pipeline_snapshots (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            account_id TEXT,
            pipeline_stage TEXT DEFAULT 'warmup',
            pipeline_status TEXT DEFAULT 'collecting',
            observed_days INTEGER DEFAULT 0,
            promote_streak INTEGER DEFAULT 0,
            halt_streak INTEGER DEFAULT 0,
            latest_decision TEXT,
            readiness_score REAL DEFAULT 0,
            next_action TEXT,
            auto_review INTEGER DEFAULT FALSE,
            auto_promoted INTEGER DEFAULT FALSE,
            blockers TEXT DEFAULT '[]',
            risk_flags TEXT DEFAULT '[]',
            summary TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            task_run_id INTEGER,
            source TEXT DEFAULT 'system',
            evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_incubation_pipeline_snapshots_sid
            ON strategy_incubation_pipeline_snapshots(strategy_id, evaluated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_incubation_pipeline_snapshots_status
            ON strategy_incubation_pipeline_snapshots(pipeline_status, evaluated_at DESC);

        CREATE TABLE IF NOT EXISTS governance_report_snapshots (
            id INTEGER PRIMARY KEY,
            scope_type TEXT NOT NULL,
            scope_id TEXT,
            overall_status TEXT NOT NULL,
            issues TEXT DEFAULT '[]',
            payload_jsonb TEXT DEFAULT '{}',
            generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_governance_report_snapshots_scope
            ON governance_report_snapshots(scope_type, scope_id, generated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_governance_report_snapshots_status
            ON governance_report_snapshots(overall_status, generated_at DESC);

        CREATE TABLE IF NOT EXISTS matching_engine_worker_state (
            engine_name TEXT PRIMARY KEY,
            worker_id TEXT NOT NULL,
            lease_until TEXT,
            last_scan_at TEXT,
            last_processed_order_id INTEGER,
            last_heartbeat_at TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS strategy_runtime_risk_events (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
            account_id TEXT,
            severity TEXT NOT NULL,
            event_type TEXT NOT NULL,
            action TEXT,
            status TEXT DEFAULT 'open',
            title TEXT,
            reason TEXT,
            payload TEXT DEFAULT '{}',
            detected_at TEXT DEFAULT CURRENT_TIMESTAMP,
            resolved_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_runtime_risk_events_sid
            ON strategy_runtime_risk_events(strategy_id, detected_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_runtime_risk_events_status
            ON strategy_runtime_risk_events(status, severity, detected_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_runtime_controls (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            account_id TEXT,
            control_mode TEXT NOT NULL DEFAULT 'active',
            status TEXT NOT NULL DEFAULT 'active',
            source TEXT DEFAULT 'system',
            trigger_event_type TEXT,
            reason TEXT,
            action_summary TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            activated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            released_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(strategy_id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_runtime_controls_mode
            ON strategy_runtime_controls(control_mode, updated_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_runtime_risk_snapshots (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            account_id TEXT,
            posture_level TEXT DEFAULT 'safe',
            escalation_level INTEGER DEFAULT 0,
            control_mode TEXT DEFAULT 'active',
            open_event_count INTEGER DEFAULT 0,
            critical_open_count INTEGER DEFAULT 0,
            warning_open_count INTEGER DEFAULT 0,
            recommended_action TEXT,
            recovery_eligible INTEGER DEFAULT FALSE,
            blockers TEXT DEFAULT '[]',
            summary TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            task_run_id INTEGER,
            source TEXT DEFAULT 'system',
            evaluated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_runtime_risk_snapshots_sid
            ON strategy_runtime_risk_snapshots(strategy_id, evaluated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_runtime_risk_snapshots_posture
            ON strategy_runtime_risk_snapshots(posture_level, evaluated_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_runtime_alerts (
            alert_id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            account_id TEXT,
            alert_key TEXT NOT NULL,
            category TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            status TEXT NOT NULL DEFAULT 'open',
            title TEXT,
            message TEXT,
            escalation_level INTEGER DEFAULT 0,
            channels TEXT DEFAULT '[]',
            related_event_ids TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            source TEXT DEFAULT 'system',
            acknowledged_by TEXT,
            acknowledged_at TEXT,
            resolved_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_runtime_alerts_sid
            ON strategy_runtime_alerts(strategy_id, status, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_runtime_alerts_key
            ON strategy_runtime_alerts(alert_key, status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_promotion_reviews (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            account_id TEXT,
            review_source TEXT DEFAULT 'system',
            stage TEXT DEFAULT 'incubating',
            status TEXT NOT NULL DEFAULT 'watch',
            recommendation TEXT,
            score REAL DEFAULT 0,
            blockers TEXT DEFAULT '[]',
            risk_flags TEXT DEFAULT '[]',
            summary TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            reviewed_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_promotion_reviews_sid
            ON strategy_promotion_reviews(strategy_id, reviewed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_promotion_reviews_status
            ON strategy_promotion_reviews(status, reviewed_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_projection_snapshots (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT NOT NULL REFERENCES strategies(id) ON DELETE CASCADE,
            projection_type TEXT NOT NULL DEFAULT 'strategy_state',
            aggregate_version INTEGER DEFAULT 0,
            current_status TEXT,
            runtime_control_mode TEXT,
            timeline_count INTEGER DEFAULT 0,
            projection TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            task_run_id INTEGER,
            source TEXT DEFAULT 'system',
            rebuilt_at TEXT DEFAULT CURRENT_TIMESTAMP,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_projection_snapshots_sid
            ON strategy_projection_snapshots(strategy_id, rebuilt_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_projection_snapshots_type
            ON strategy_projection_snapshots(projection_type, rebuilt_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_vector_profiles (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
            index_name TEXT DEFAULT 'strategy_behavior',
            collection_name TEXT DEFAULT 'strategy_behavior_embeddings',
            profile_type TEXT NOT NULL,
            vector_method TEXT NOT NULL,
            model_id TEXT DEFAULT 'strategy_behavior_v1',
            metric TEXT DEFAULT 'cosine',
            vector_dim INTEGER DEFAULT 0,
            embedding TEXT DEFAULT '[]',
            signature TEXT,
            backend TEXT DEFAULT 'index',
            index_version TEXT,
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_profiles_sid
            ON strategy_vector_profiles(strategy_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_profiles_type
            ON strategy_vector_profiles(profile_type, vector_method, created_at DESC);

        CREATE TABLE IF NOT EXISTS vector_index_registry (
            id INTEGER PRIMARY KEY,
            index_name TEXT NOT NULL,
            backend TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'building',
            profile_type TEXT,
            vector_method TEXT,
            metric TEXT DEFAULT 'cosine',
            sample_count INTEGER DEFAULT 0,
            index_version TEXT NOT NULL,
            metadata TEXT DEFAULT '{}',
            built_at TEXT,
            activated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(index_name, index_version)
        );
        CREATE INDEX IF NOT EXISTS idx_vector_index_registry_name
            ON vector_index_registry(index_name, created_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_vector_index_snapshots (
            id INTEGER PRIMARY KEY,
            index_name TEXT NOT NULL,
            index_version TEXT NOT NULL,
            collection_name TEXT DEFAULT 'strategy_behavior_embeddings',
            status TEXT NOT NULL DEFAULT 'building',
            profile_type TEXT,
            vector_method TEXT,
            model_id TEXT DEFAULT 'strategy_behavior_v1',
            metric TEXT DEFAULT 'cosine',
            backend TEXT DEFAULT 'index',
            profile_count INTEGER DEFAULT 0,
            bucket_count INTEGER DEFAULT 0,
            vector_dim INTEGER DEFAULT 0,
            centroids TEXT DEFAULT '[]',
            index_params TEXT DEFAULT '{}',
            metrics TEXT DEFAULT '{}',
            metadata TEXT DEFAULT '{}',
            task_run_id INTEGER,
            source TEXT DEFAULT 'system',
            built_at TEXT,
            activated_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        ALTER TABLE strategy_vector_index_snapshots ADD COLUMN IF NOT EXISTS index_params TEXT DEFAULT '{}';
        ALTER TABLE strategy_vector_index_snapshots ADD COLUMN IF NOT EXISTS metrics TEXT DEFAULT '{}';
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_snapshots_name
            ON strategy_vector_index_snapshots(index_name, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_snapshots_name_version
            ON strategy_vector_index_snapshots(index_name, index_version, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_snapshots_status
            ON strategy_vector_index_snapshots(status, created_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_vector_index_items (
            id INTEGER PRIMARY KEY,
            index_name TEXT NOT NULL,
            index_version TEXT NOT NULL,
            collection_name TEXT DEFAULT 'strategy_behavior_embeddings',
            profile_id INTEGER,
            strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
            profile_type TEXT,
            vector_method TEXT,
            model_id TEXT DEFAULT 'strategy_behavior_v1',
            metric TEXT DEFAULT 'cosine',
            vector_dim INTEGER DEFAULT 0,
            bucket_id TEXT,
            coarse_score REAL DEFAULT 0,
            embedding TEXT DEFAULT '[]',
            metadata TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(index_name, index_version, profile_id)
        );
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_items_lookup
            ON strategy_vector_index_items(index_name, index_version, bucket_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_items_sid
            ON strategy_vector_index_items(strategy_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_generation_experiments (
            id INTEGER PRIMARY KEY,
            experiment_id TEXT NOT NULL UNIQUE,
            strategy_id TEXT REFERENCES strategies(id) ON DELETE SET NULL,
            parent_strategy_id TEXT REFERENCES strategies(id) ON DELETE SET NULL,
            generated_strategy_id TEXT REFERENCES strategies(id) ON DELETE SET NULL,
            task_run_id INTEGER,
            source TEXT NOT NULL,
            generator_type TEXT NOT NULL,
            optimizer_type TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            hypothesis TEXT,
            prompt TEXT,
            parameters TEXT DEFAULT '{}',
            strategy_spec TEXT DEFAULT '{}',
            evaluation TEXT DEFAULT '{}',
            result TEXT DEFAULT '{}',
            parent_experiment_id TEXT,
            artifact_id TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        ALTER TABLE strategy_generation_experiments
            ADD COLUMN IF NOT EXISTS parent_strategy_id TEXT REFERENCES strategies(id) ON DELETE SET NULL;
        ALTER TABLE strategy_generation_experiments
            ADD COLUMN IF NOT EXISTS generated_strategy_id TEXT REFERENCES strategies(id) ON DELETE SET NULL;
        ALTER TABLE strategy_generation_experiments
            ADD COLUMN IF NOT EXISTS task_run_id INTEGER;
        CREATE INDEX IF NOT EXISTS idx_strategy_generation_experiments_status
            ON strategy_generation_experiments(status, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_generation_experiments_parent_sid
            ON strategy_generation_experiments(parent_strategy_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_generation_experiments_generated_sid
            ON strategy_generation_experiments(generated_strategy_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_generation_experiments_task_run
            ON strategy_generation_experiments(task_run_id, created_at DESC);

        CREATE TABLE IF NOT EXISTS strategy_task_runs (
            id INTEGER PRIMARY KEY,
            strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
            task_name TEXT NOT NULL,
            task_scope TEXT,
            task_key TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            trace_id TEXT,
            payload TEXT DEFAULT '{}',
            result TEXT DEFAULT '{}',
            error TEXT,
            started_at TEXT DEFAULT CURRENT_TIMESTAMP,
            completed_at TEXT
        );
        ALTER TABLE strategy_task_runs
            ADD COLUMN IF NOT EXISTS strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE;
        CREATE INDEX IF NOT EXISTS idx_strategy_task_runs_name
            ON strategy_task_runs(task_name, started_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_task_runs_sid
            ON strategy_task_runs(strategy_id, started_at DESC);
    """)
    await conn.execute("""
        ALTER TABLE strategy_incubation_metrics
        ADD COLUMN IF NOT EXISTS hit_rate_lcb_5d REAL;
        ALTER TABLE strategy_incubation_metrics
        ADD COLUMN IF NOT EXISTS skill_lcb_5d REAL;
        ALTER TABLE strategy_incubation_metrics
        ADD COLUMN IF NOT EXISTS effective_n_5d INTEGER;
        ALTER TABLE strategy_incubation_metrics
        ADD COLUMN IF NOT EXISTS recent_hit_rate_5d REAL;
        ALTER TABLE strategy_incubation_metrics
        ADD COLUMN IF NOT EXISTS recent_skill_lcb_5d REAL;
        ALTER TABLE strategy_incubation_metrics
        ADD COLUMN IF NOT EXISTS stability_gap_5d REAL;
    """)

    # Compat: backfill index_name for vector profiles
    await conn.execute("""
        ALTER TABLE strategy_vector_profiles
        ADD COLUMN IF NOT EXISTS index_name TEXT DEFAULT 'strategy_behavior';
    """)
    await conn.execute("""
        ALTER TABLE strategy_vector_profiles
        ADD COLUMN IF NOT EXISTS model_id TEXT DEFAULT 'strategy_behavior_v1';
    """)
    await conn.execute("""
        ALTER TABLE strategy_vector_profiles
        ADD COLUMN IF NOT EXISTS collection_name TEXT DEFAULT 'strategy_behavior_embeddings';
    """)
    await conn.execute("""
        UPDATE strategy_vector_profiles
        SET index_name = COALESCE(NULLIF(index_name, ''), json_extract(metadata, '$.index_name'), 'strategy_behavior')
        WHERE index_name IS NULL OR index_name = '';
    """)
    await conn.execute("""
        UPDATE strategy_vector_profiles
        SET model_id = COALESCE(NULLIF(model_id, ''), json_extract(metadata, '$.model_id'), 'strategy_behavior_v1')
        WHERE model_id IS NULL OR model_id = '';
    """)
    await conn.execute("""
        UPDATE strategy_vector_profiles
        SET collection_name = COALESCE(NULLIF(collection_name, ''), json_extract(metadata, '$.collection_name'), 'strategy_behavior_embeddings')
        WHERE collection_name IS NULL OR collection_name = '';
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_profiles_index_name
            ON strategy_vector_profiles(index_name, index_version, created_at DESC);
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_profiles_model
            ON strategy_vector_profiles(model_id, collection_name, index_version, created_at DESC);
    """)
    await conn.execute("""
        ALTER TABLE strategy_vector_index_snapshots
        ADD COLUMN IF NOT EXISTS model_id TEXT DEFAULT 'strategy_behavior_v1';
    """)
    await conn.execute("""
        ALTER TABLE strategy_vector_index_snapshots
        ADD COLUMN IF NOT EXISTS collection_name TEXT DEFAULT 'strategy_behavior_embeddings';
    """)
    await conn.execute("""
        UPDATE strategy_vector_index_snapshots
        SET model_id = COALESCE(NULLIF(model_id, ''), json_extract(metadata, '$.model_id'), 'strategy_behavior_v1')
        WHERE model_id IS NULL OR model_id = '';
    """)
    await conn.execute("""
        UPDATE strategy_vector_index_snapshots
        SET collection_name = COALESCE(NULLIF(collection_name, ''), json_extract(metadata, '$.collection_name'), 'strategy_behavior_embeddings')
        WHERE collection_name IS NULL OR collection_name = '';
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_snapshots_model
            ON strategy_vector_index_snapshots(model_id, collection_name, index_version, created_at DESC);
    """)
    await conn.execute("""
        ALTER TABLE strategy_vector_index_items
        ADD COLUMN IF NOT EXISTS model_id TEXT DEFAULT 'strategy_behavior_v1';
    """)
    await conn.execute("""
        ALTER TABLE strategy_vector_index_items
        ADD COLUMN IF NOT EXISTS collection_name TEXT DEFAULT 'strategy_behavior_embeddings';
    """)
    await conn.execute("""
        UPDATE strategy_vector_index_items
        SET model_id = COALESCE(NULLIF(model_id, ''), json_extract(metadata, '$.model_id'), 'strategy_behavior_v1')
        WHERE model_id IS NULL OR model_id = '';
    """)
    await conn.execute("""
        UPDATE strategy_vector_index_items
        SET collection_name = COALESCE(NULLIF(collection_name, ''), json_extract(metadata, '$.collection_name'), 'strategy_behavior_embeddings')
        WHERE collection_name IS NULL OR collection_name = '';
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_items_model
            ON strategy_vector_index_items(model_id, collection_name, index_version, created_at DESC);
    """)
    await conn.execute("""
        ALTER TABLE strategy_vector_index_snapshots
        DROP CONSTRAINT IF EXISTS strategy_vector_index_snapshots_index_name_index_version_key;
    """)

    # sqlite_python-specific tables (gated)
    if sqlite_python_enabled:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_vector_profile_store (
                profile_id INTEGER PRIMARY KEY REFERENCES strategy_vector_profiles(id) ON DELETE CASCADE,
                strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
                index_name TEXT NOT NULL DEFAULT 'strategy_behavior',
                collection_name TEXT NOT NULL DEFAULT 'strategy_behavior_embeddings',
                index_version TEXT,
                profile_type TEXT,
                vector_method TEXT,
                model_id TEXT DEFAULT 'strategy_behavior_v1',
                metric TEXT DEFAULT 'cosine',
                vector_dim INTEGER DEFAULT 0,
                embedding TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_vector_profile_store_lookup
                ON strategy_vector_profile_store(index_name, index_version, profile_type, vector_dim);
            CREATE INDEX IF NOT EXISTS idx_strategy_vector_profile_store_sid
                ON strategy_vector_profile_store(strategy_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS strategy_vector_index_item_store (
                item_id INTEGER PRIMARY KEY REFERENCES strategy_vector_index_items(id) ON DELETE CASCADE,
                index_name TEXT NOT NULL,
                index_version TEXT NOT NULL,
                collection_name TEXT NOT NULL DEFAULT 'strategy_behavior_embeddings',
                strategy_id TEXT REFERENCES strategies(id) ON DELETE CASCADE,
                profile_id INTEGER,
                profile_type TEXT,
                vector_method TEXT,
                model_id TEXT DEFAULT 'strategy_behavior_v1',
                metric TEXT DEFAULT 'cosine',
                vector_dim INTEGER DEFAULT 0,
                embedding TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_item_store_lookup
                ON strategy_vector_index_item_store(index_name, index_version, profile_type, vector_dim);
            CREATE INDEX IF NOT EXISTS idx_strategy_vector_index_item_store_sid
                ON strategy_vector_index_item_store(strategy_id, updated_at DESC);
        """)
        await conn.execute("""
            ALTER TABLE strategy_vector_profile_store
            ADD COLUMN IF NOT EXISTS model_id TEXT DEFAULT 'strategy_behavior_v1';
        """)
        await conn.execute("""
            ALTER TABLE strategy_vector_profile_store
            ADD COLUMN IF NOT EXISTS collection_name TEXT DEFAULT 'strategy_behavior_embeddings';
        """)
        await conn.execute("""
            UPDATE strategy_vector_profile_store
            SET model_id = COALESCE(NULLIF(model_id, ''), json_extract(metadata, '$.model_id'), 'strategy_behavior_v1')
            WHERE model_id IS NULL OR model_id = '';
        """)
        await conn.execute("""
            UPDATE strategy_vector_profile_store
            SET collection_name = COALESCE(NULLIF(collection_name, ''), json_extract(metadata, '$.collection_name'), 'strategy_behavior_embeddings')
            WHERE collection_name IS NULL OR collection_name = '';
        """)
        await conn.execute("""
            ALTER TABLE strategy_vector_index_item_store
            ADD COLUMN IF NOT EXISTS model_id TEXT DEFAULT 'strategy_behavior_v1';
        """)
        await conn.execute("""
            ALTER TABLE strategy_vector_index_item_store
            ADD COLUMN IF NOT EXISTS collection_name TEXT DEFAULT 'strategy_behavior_embeddings';
        """)
        await conn.execute("""
            UPDATE strategy_vector_index_item_store
            SET model_id = COALESCE(NULLIF(model_id, ''), json_extract(metadata, '$.model_id'), 'strategy_behavior_v1')
            WHERE model_id IS NULL OR model_id = '';
        """)
        await conn.execute("""
            UPDATE strategy_vector_index_item_store
            SET collection_name = COALESCE(NULLIF(collection_name, ''), json_extract(metadata, '$.collection_name'), 'strategy_behavior_embeddings')
            WHERE collection_name IS NULL OR collection_name = '';
        """)

    logger.info("Strategy tables initialized (sqlite_python=%s)", sqlite_python_enabled)
