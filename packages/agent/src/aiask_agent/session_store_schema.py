from __future__ import annotations

import sqlite3


def ensure_session_store_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT,
            title TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            name TEXT,
            tool_call_id TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            deleted_reason TEXT,
            deleted_by TEXT
        );
        CREATE TABLE IF NOT EXISTS responses (
            response_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            accessed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS run_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS session_handoffs (
            handoff_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            user_id TEXT,
            target TEXT,
            status TEXT NOT NULL,
            reason TEXT,
            summary TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS subgoals (
            subgoal_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            user_id TEXT,
            title TEXT NOT NULL,
            criteria_json TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_activity_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id TEXT,
            session_id TEXT,
            run_id TEXT,
            trace_id TEXT,
            page_key TEXT,
            route TEXT,
            event_type TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            target_label TEXT,
            target_testid TEXT,
            payload_json TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT 'desktop',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tool_invocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invocation_id TEXT NOT NULL UNIQUE,
            user_id TEXT,
            session_id TEXT,
            run_id TEXT,
            trace_id TEXT,
            tool_name TEXT NOT NULL,
            capability TEXT,
            category TEXT,
            side_effect TEXT,
            status TEXT NOT NULL,
            input_summary_json TEXT NOT NULL DEFAULT '{}',
            output_summary_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT,
            error_summary TEXT,
            duration_ms INTEGER,
            approval_id TEXT,
            action_intent_id TEXT,
            source_chain_json TEXT NOT NULL DEFAULT '[]',
            secrets_redacted INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL UNIQUE,
            user_id TEXT,
            session_id TEXT,
            run_id TEXT,
            trace_id TEXT,
            tool_call_id TEXT,
            tool_name TEXT,
            provider TEXT,
            source_type TEXT NOT NULL,
            title TEXT,
            url TEXT,
            published_at TEXT,
            fetched_at TEXT NOT NULL,
            data_timestamp TEXT,
            excerpt TEXT,
            source_tier TEXT,
            credibility_score REAL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS agent_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artifact_id TEXT NOT NULL UNIQUE,
            user_id TEXT,
            session_id TEXT,
            run_id TEXT,
            trace_id TEXT,
            tool_call_id TEXT,
            tool_name TEXT,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            path TEXT,
            uri TEXT,
            mime_type TEXT,
            size_bytes INTEGER,
            sha256 TEXT,
            preview_text TEXT,
            preview_json TEXT NOT NULL DEFAULT '{}',
            source_id TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS context_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            user_id TEXT,
            session_id TEXT NOT NULL,
            run_id TEXT,
            trace_id TEXT,
            context_summary_id TEXT,
            policy TEXT NOT NULL DEFAULT 'runtime_prepare',
            compacted INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            source_message_ids_json TEXT NOT NULL DEFAULT '[]',
            source_ids_json TEXT NOT NULL DEFAULT '[]',
            artifact_ids_json TEXT NOT NULL DEFAULT '[]',
            token_estimate_before INTEGER,
            token_estimate_after INTEGER,
            summary TEXT,
            summary_model TEXT,
            risk_flags_json TEXT NOT NULL DEFAULT '[]',
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS feedback_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feedback_id TEXT NOT NULL UNIQUE,
            user_id TEXT,
            session_id TEXT,
            run_id TEXT,
            target_type TEXT NOT NULL,
            target_id TEXT,
            feedback_type TEXT NOT NULL,
            rating INTEGER,
            comment TEXT,
            allow_learning INTEGER NOT NULL DEFAULT 0,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_data_policies (
            user_id TEXT PRIMARY KEY,
            event_ttl_days INTEGER NOT NULL DEFAULT 90,
            audit_ttl_days INTEGER NOT NULL DEFAULT 180,
            run_event_ttl_days INTEGER NOT NULL DEFAULT 180,
            tool_payload_ttl_days INTEGER NOT NULL DEFAULT 90,
            conversation_retention TEXT NOT NULL DEFAULT 'keep_until_user_deletes',
            allow_product_analytics INTEGER NOT NULL DEFAULT 1,
            allow_learning INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_profiles (
            broker_profile_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            display_name TEXT,
            account_ref_hash TEXT,
            market TEXT,
            read_only_enabled INTEGER NOT NULL DEFAULT 1,
            write_enabled INTEGER NOT NULL DEFAULT 0,
            consent_status TEXT NOT NULL DEFAULT 'unknown',
            consent_version TEXT,
            status TEXT NOT NULL DEFAULT 'unconfigured',
            error_code TEXT,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            last_sync_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_account_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            broker_profile_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            account_ref_hash TEXT,
            currency TEXT,
            total_asset REAL,
            cash_available REAL,
            market_value REAL,
            frozen_cash REAL,
            buying_power REAL,
            source_tool TEXT,
            source_run_id TEXT,
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_position_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            broker_profile_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            symbol TEXT,
            exchange TEXT,
            name TEXT,
            quantity REAL,
            available_quantity REAL,
            cost_basis REAL,
            last_price REAL,
            market_value REAL,
            unrealized_pnl REAL,
            unrealized_pnl_pct REAL,
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_order_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            broker_profile_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            order_ref_hash TEXT,
            symbol TEXT,
            side TEXT,
            order_type TEXT,
            price REAL,
            quantity REAL,
            filled_quantity REAL,
            status TEXT,
            submitted_at TEXT,
            updated_at TEXT,
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_deal_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            broker_profile_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            deal_ref_hash TEXT,
            order_ref_hash TEXT,
            symbol TEXT,
            side TEXT,
            price REAL,
            quantity REAL,
            amount REAL,
            fee REAL,
            occurred_at TEXT,
            observed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS broker_behavior_analytics (
            analytics_id TEXT PRIMARY KEY,
            broker_profile_id TEXT,
            user_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            period_start TEXT,
            period_end TEXT,
            metrics_json TEXT NOT NULL DEFAULT '{}',
            signals_json TEXT NOT NULL DEFAULT '{}',
            risk_flags_json TEXT NOT NULL DEFAULT '[]',
            source_snapshot_ids_json TEXT NOT NULL DEFAULT '{}',
            model_version TEXT NOT NULL DEFAULT 'deterministic-p0',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
        CREATE INDEX IF NOT EXISTS idx_responses_session ON responses(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events(run_id, event_id);
        CREATE INDEX IF NOT EXISTS idx_session_handoffs_session ON session_handoffs(session_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_subgoals_session ON subgoals(session_id, updated_at);
        CREATE INDEX IF NOT EXISTS idx_user_activity_events_user_created ON user_activity_events(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_activity_events_session_created ON user_activity_events(session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_user_activity_events_page_created ON user_activity_events(page_key, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tool_invocations_user_created ON tool_invocations(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_tool_invocations_run_created ON tool_invocations(run_id, created_at ASC);
        CREATE INDEX IF NOT EXISTS idx_tool_invocations_tool_created ON tool_invocations(tool_name, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_sources_run_created ON agent_sources(run_id, created_at ASC);
        CREATE INDEX IF NOT EXISTS idx_agent_sources_session_created ON agent_sources(session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_sources_tool_call ON agent_sources(tool_call_id, created_at ASC);
        CREATE INDEX IF NOT EXISTS idx_agent_sources_url ON agent_sources(url);
        CREATE INDEX IF NOT EXISTS idx_agent_artifacts_run_created ON agent_artifacts(run_id, created_at ASC);
        CREATE INDEX IF NOT EXISTS idx_agent_artifacts_session_created ON agent_artifacts(session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_agent_artifacts_tool_call ON agent_artifacts(tool_call_id, created_at ASC);
        CREATE INDEX IF NOT EXISTS idx_agent_artifacts_path ON agent_artifacts(path);
        CREATE INDEX IF NOT EXISTS idx_context_snapshots_run_created ON context_snapshots(run_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_context_snapshots_session_created ON context_snapshots(session_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_context_snapshots_user_created ON context_snapshots(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_feedback_events_user_created ON feedback_events(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_feedback_events_target ON feedback_events(target_type, target_id);
        CREATE INDEX IF NOT EXISTS idx_broker_profiles_user_provider ON broker_profiles(user_id, provider, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_broker_account_user_provider ON broker_account_snapshots(user_id, provider, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_broker_position_user_provider ON broker_position_snapshots(user_id, provider, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_broker_order_user_provider ON broker_order_snapshots(user_id, provider, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_broker_deal_user_provider ON broker_deal_snapshots(user_id, provider, observed_at DESC);
        CREATE INDEX IF NOT EXISTS idx_broker_analytics_user_provider ON broker_behavior_analytics(user_id, provider, created_at DESC);
        """
    )
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    for column, ddl in {
        "deleted_at": "ALTER TABLE messages ADD COLUMN deleted_at TEXT",
        "deleted_reason": "ALTER TABLE messages ADD COLUMN deleted_reason TEXT",
        "deleted_by": "ALTER TABLE messages ADD COLUMN deleted_by TEXT",
    }.items():
        if column not in existing_columns:
            conn.execute(ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_session_active ON messages(session_id, deleted_at, id)")
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS aiask_search_fts
            USING fts5(kind, object_id, session_id, user_id, content, payload_json)
            """
        )
    except sqlite3.OperationalError:
        pass
    conn.commit()
