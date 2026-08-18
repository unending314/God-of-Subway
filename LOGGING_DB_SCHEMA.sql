-- 지금타 V13.4.3 오류 로그 영구 저장용 PostgreSQL 스키마
-- observability.py가 DATABASE_URL 연결 시 자동 생성하지만, 수동 생성도 가능하다.
CREATE TABLE IF NOT EXISTS app_error_logs (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    endpoint TEXT,
    request_id TEXT,
    status_code INTEGER,
    error_type TEXT,
    message TEXT,
    payload JSONB,
    diagnostics JSONB,
    context JSONB,
    traceback TEXT,
    app_version TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_app_error_logs_created_at
    ON app_error_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_error_logs_event_type
    ON app_error_logs (event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_app_error_logs_request_id
    ON app_error_logs (request_id);
