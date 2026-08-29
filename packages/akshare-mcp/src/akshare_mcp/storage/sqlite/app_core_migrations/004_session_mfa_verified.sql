ALTER TABLE app_sessions
    ADD COLUMN IF NOT EXISTS mfa_verified_at TEXT;
