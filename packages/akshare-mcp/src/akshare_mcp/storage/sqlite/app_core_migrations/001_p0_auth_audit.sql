CREATE TABLE IF NOT EXISTS app_users (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS permissions (
    id TEXT PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_roles (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES app_users(id),
    role_id TEXT NOT NULL REFERENCES roles(id),
    active INTEGER NOT NULL DEFAULT TRUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, role_id)
);

CREATE TABLE IF NOT EXISTS app_sessions (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES app_users(id),
    access_jti TEXT NOT NULL UNIQUE,
    refresh_token_hash TEXT NOT NULL UNIQUE,
    access_expires_at TEXT NOT NULL,
    refresh_expires_at TEXT NOT NULL,
    revoked_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY,
    trace_id TEXT NOT NULL,
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    status INT NOT NULL,
    duration_ms INT NOT NULL,
    user_id TEXT,
    username TEXT,
    user_role TEXT,
    ts TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_app_sessions_user_id ON app_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_app_sessions_access_jti ON app_sessions(access_jti);
CREATE INDEX IF NOT EXISTS idx_app_sessions_refresh_hash ON app_sessions(refresh_token_hash);
CREATE INDEX IF NOT EXISTS idx_audit_logs_ts ON audit_logs(ts DESC);

INSERT INTO roles (id, code, name)
VALUES
    ('role_admin', 'admin', '管理员'),
    ('role_user', 'user', '普通用户')
ON CONFLICT (code) DO NOTHING;

INSERT INTO app_users (id, username, password_hash)
VALUES
    ('u_admin', 'admin', '240be518fabd2724ddb6f04eebf2ef9f20f3f854de3ec7d00ed284aa9f0c45c0'),
    ('u_demo', 'demo', 'd3ad9315a6d2ce241e7ef31f85a048760a8aa2f96f5c85ca9f1f295f82bc4ab8')
ON CONFLICT (username) DO NOTHING;

INSERT INTO user_roles (user_id, role_id)
VALUES
    ('u_admin', 'role_admin'),
    ('u_demo', 'role_user')
ON CONFLICT (user_id, role_id) DO NOTHING;
