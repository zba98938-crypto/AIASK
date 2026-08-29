CREATE TABLE IF NOT EXISTS unified_decision_diff_audit (
    id INTEGER PRIMARY KEY,
    trace_id TEXT,
    user_id TEXT,
    stock_code TEXT NOT NULL,
    investment_style TEXT NOT NULL,
    unified_action TEXT NOT NULL,
    action_alignment TEXT NOT NULL,
    legacy_actions TEXT NOT NULL DEFAULT '[]',
    disagreements TEXT NOT NULL DEFAULT '[]',
    diff_summary TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_unified_decision_diff_audit_user_created
    ON unified_decision_diff_audit(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_unified_decision_diff_audit_stock_created
    ON unified_decision_diff_audit(stock_code, created_at DESC);
