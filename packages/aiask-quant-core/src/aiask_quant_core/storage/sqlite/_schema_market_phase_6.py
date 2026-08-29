from ._schema_market_common import _run_market_migration_once, logger


async def init_market_tables_phase_6(conn) -> None:
    await conn.execute(
        """
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS candidate_artifact_id TEXT;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS experiment_id TEXT;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS source_type TEXT;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS event_type TEXT;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS target_symbols TEXT DEFAULT '[]';
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS direction TEXT;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS horizon_days INTEGER;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS raw_confidence REAL;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS calibrated_confidence REAL;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS freshness_ts TEXT;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS proxy_only INTEGER DEFAULT 0;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS support_metric TEXT DEFAULT '{}';
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS doc_uid TEXT;
        ALTER TABLE strategy_candidate_evidence ADD COLUMN IF NOT EXISTS headline_label_id TEXT;

        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS candidate_artifact_id TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS experiment_id TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS applied_claim_id TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS applied_trade_step_id TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS source_type TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS direction TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS horizon_days INTEGER;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS signal_ts TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS raw_confidence REAL;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS calibrated_confidence REAL;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS proxy_only INTEGER DEFAULT 0;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS doc_uid TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS headline_label_id TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS runtime_action_reason TEXT;
        ALTER TABLE strategy_signal_evidence ADD COLUMN IF NOT EXISTS runtime_action_source TEXT;

        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS entry_ts TEXT;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS exit_ts TEXT;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS entry_avg_price REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS exit_avg_price REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS gross_qty INTEGER;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS gross_return REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS net_return REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS gross_pnl REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS net_pnl REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS hold_days REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS exit_reason TEXT;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS mfe REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS mae REAL;
        ALTER TABLE strategy_trade_positions ADD COLUMN IF NOT EXISTS price_path_audit_status TEXT;

        CREATE TABLE IF NOT EXISTS market_headline_labels (
            label_id TEXT PRIMARY KEY,
            doc_id INTEGER REFERENCES market_documents(id) ON DELETE CASCADE,
            doc_uid TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            doc_type TEXT NOT NULL,
            published_at TEXT,
            headline TEXT,
            label TEXT NOT NULL,
            event_type TEXT,
            direction TEXT,
            horizon_days INTEGER,
            intensity TEXT,
            confidence REAL,
            keywords TEXT NOT NULL DEFAULT '[]',
            payload TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_market_headline_labels_stock_time
            ON market_headline_labels(stock_code, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_headline_labels_doc_uid
            ON market_headline_labels(doc_uid, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_market_headline_labels_label
            ON market_headline_labels(label, published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_candidate_evidence_artifact
            ON strategy_candidate_evidence(candidate_artifact_id, experiment_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_signal_evidence_claim
            ON strategy_signal_evidence(signal_id, applied_claim_id, signal_date DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_signal_evidence_trade_step
            ON strategy_signal_evidence(signal_id, applied_trade_step_id, signal_date DESC);
        CREATE INDEX IF NOT EXISTS idx_strategy_trade_positions_roundtrip
            ON strategy_trade_positions(strategy_id, status, exit_ts DESC, updated_at DESC);
        """
    )

    await conn.execute(
        """
        ALTER TABLE strategy_signal_evidence
        DROP CONSTRAINT IF EXISTS strategy_signal_evidence_signal_id_evidence_id_key;
        """
    )
    await conn.execute(
        """
        DROP INDEX IF EXISTS ux_strategy_signal_evidence_signal_evidence_claim;
        """
    )
    await conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_strategy_signal_evidence_signal_evidence_claim
            ON strategy_signal_evidence(
                signal_id,
                evidence_id,
                COALESCE(applied_claim_id, ''),
                COALESCE(applied_trade_step_id, '')
            );
        """
    )

    await _run_market_migration_once(
        conn,
        "strategy_candidate_evidence_native_backfill_v1",
        """
        UPDATE strategy_candidate_evidence
        SET candidate_artifact_id = COALESCE(
                candidate_artifact_id,
                NULLIF(json_extract(payload, '$.candidate_artifact_id'), ''),
                NULLIF(json_extract(payload, '$.source_candidate_artifact_id'), ''),
                NULLIF(json_extract(payload, '$.hypothesis_artifact_id'), '')
            ),
            experiment_id = COALESCE(experiment_id, NULLIF(json_extract(payload, '$.experiment_id'), '')),
            source_type = COALESCE(source_type, NULLIF(json_extract(payload, '$.source_type'), ''), NULLIF(json_extract(payload, '$.evidence_type'), '')),
            event_type = COALESCE(event_type, NULLIF(json_extract(payload, '$.event_type'), '')),
            target_symbols = COALESCE(
                target_symbols,
                CASE
                    WHEN json_type(payload, '$.target_symbols') = 'array' THEN json_extract(payload, '$.target_symbols')
                    WHEN json_type(payload, '$.target_symbols') IS NOT NULL AND COALESCE(json_extract(payload, '$.target_symbols'), '') <> '' THEN json_array(json_extract(payload, '$.target_symbols'))
                    ELSE NULL
                END
            ),
            direction = COALESCE(direction, NULLIF(json_extract(payload, '$.direction'), '')),
            horizon_days = COALESCE(horizon_days, CAST(NULLIF(json_extract(payload, '$.horizon_days'), '') AS INTEGER)),
            raw_confidence = COALESCE(raw_confidence, CAST(NULLIF(json_extract(payload, '$.raw_confidence'), '') AS REAL)),
            calibrated_confidence = COALESCE(calibrated_confidence, CAST(NULLIF(json_extract(payload, '$.calibrated_confidence'), '') AS REAL)),
            freshness_ts = COALESCE(freshness_ts, NULLIF(json_extract(payload, '$.freshness_ts'), '')),
            proxy_only = COALESCE(proxy_only, CAST(COALESCE(json_extract(payload, '$.proxy_only'), 0) AS INTEGER), 0),
            support_metric = COALESCE(
                support_metric,
                CASE WHEN json_type(payload, '$.support_metric') IS NOT NULL THEN json_extract(payload, '$.support_metric') ELSE NULL END
            ),
            doc_uid = COALESCE(doc_uid, NULLIF(json_extract(payload, '$.doc_uid'), '')),
            headline_label_id = COALESCE(headline_label_id, NULLIF(json_extract(payload, '$.headline_label_id'), ''))
        WHERE payload IS NOT NULL;
        """,
    )

    await _run_market_migration_once(
        conn,
        "strategy_signal_evidence_native_backfill_v1",
        """
        UPDATE strategy_signal_evidence
        SET candidate_artifact_id = COALESCE(
                candidate_artifact_id,
                NULLIF(json_extract(payload, '$.candidate_artifact_id'), ''),
                NULLIF(json_extract(payload, '$.source_candidate_artifact_id'), ''),
                NULLIF(json_extract(payload, '$.hypothesis_artifact_id'), '')
            ),
            experiment_id = COALESCE(experiment_id, NULLIF(json_extract(payload, '$.experiment_id'), '')),
            applied_claim_id = COALESCE(applied_claim_id, NULLIF(json_extract(payload, '$.applied_claim_id'), '')),
            applied_trade_step_id = COALESCE(applied_trade_step_id, NULLIF(json_extract(payload, '$.applied_trade_step_id'), '')),
            source_type = COALESCE(source_type, NULLIF(json_extract(payload, '$.source_type'), ''), NULLIF(json_extract(payload, '$.evidence_type'), '')),
            direction = COALESCE(direction, NULLIF(json_extract(payload, '$.direction'), '')),
            horizon_days = COALESCE(horizon_days, CAST(NULLIF(json_extract(payload, '$.horizon_days'), '') AS INTEGER)),
            signal_ts = COALESCE(signal_ts, NULLIF(json_extract(payload, '$.signal_ts'), ''), signal_date),
            raw_confidence = COALESCE(raw_confidence, CAST(NULLIF(json_extract(payload, '$.raw_confidence'), '') AS REAL)),
            calibrated_confidence = COALESCE(calibrated_confidence, CAST(NULLIF(json_extract(payload, '$.calibrated_confidence'), '') AS REAL)),
            proxy_only = COALESCE(proxy_only, CAST(COALESCE(json_extract(payload, '$.proxy_only'), 0) AS INTEGER), 0),
            doc_uid = COALESCE(doc_uid, NULLIF(json_extract(payload, '$.doc_uid'), '')),
            headline_label_id = COALESCE(headline_label_id, NULLIF(json_extract(payload, '$.headline_label_id'), '')),
            runtime_action_reason = COALESCE(runtime_action_reason, NULLIF(json_extract(payload, '$.runtime_action_reason'), '')),
            runtime_action_source = COALESCE(runtime_action_source, NULLIF(json_extract(payload, '$.runtime_action_source'), ''))
        WHERE payload IS NOT NULL;
        """,
    )

    await _run_market_migration_once(
        conn,
        "strategy_trade_positions_roundtrip_backfill_v1",
        """
        UPDATE strategy_trade_positions
        SET entry_ts = COALESCE(entry_ts, opened_at),
            exit_ts = COALESCE(exit_ts, closed_at),
            entry_avg_price = COALESCE(entry_avg_price, entry_amount / NULLIF(entry_shares, 0)),
            exit_avg_price = COALESCE(exit_avg_price, exit_amount / NULLIF(exit_shares, 0)),
            gross_qty = COALESCE(gross_qty, max(COALESCE(entry_shares, 0), COALESCE(exit_shares, 0))),
            gross_pnl = COALESCE(gross_pnl, exit_amount - entry_amount),
            net_pnl = COALESCE(net_pnl, realized_pnl),
            gross_return = COALESCE(gross_return, (exit_amount - entry_amount) / NULLIF(entry_amount, 0)),
            net_return = COALESCE(net_return, realized_return),
            hold_days = COALESCE(
                hold_days,
                julianday(COALESCE(closed_at, CURRENT_TIMESTAMP)) - julianday(COALESCE(opened_at, created_at))
            ),
            exit_reason = COALESCE(
                exit_reason,
                CASE
                    WHEN status = 'closed' THEN 'filled_round_trip'
                    WHEN status = 'open' THEN 'position_open'
                    ELSE exit_reason
                END
            ),
            price_path_audit_status = COALESCE(
                price_path_audit_status,
                CASE
                    WHEN status = 'closed' THEN 'pending_refresh'
                    WHEN status = 'open' THEN 'open_position'
                    ELSE 'unknown'
                END
            );
        """,
    )
    logger.info("Market tables phase 6 initialized")
