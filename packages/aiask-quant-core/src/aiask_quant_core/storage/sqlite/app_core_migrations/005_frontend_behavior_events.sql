CREATE TABLE IF NOT EXISTS frontend_behavior_events (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES app_users(id),
    session_id TEXT NOT NULL,
    page_key TEXT NOT NULL,
    route TEXT NOT NULL,
    event_type TEXT NOT NULL,
    target_type TEXT,
    target_label TEXT,
    target_id TEXT,
    target_testid TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'web',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_frontend_behavior_events_user_created_at
    ON frontend_behavior_events(user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_frontend_behavior_events_session_created_at
    ON frontend_behavior_events(session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_frontend_behavior_events_page_key_created_at
    ON frontend_behavior_events(page_key, created_at DESC);
