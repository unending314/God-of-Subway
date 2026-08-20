-- 지금타 V14 realtime/prediction 영속 저장 스키마 (PostgreSQL / Supabase)
-- Redis = hot current state, PostgreSQL = 이력/매핑/운영 분석.

CREATE TABLE IF NOT EXISTS realtime_fetch_events (
    id BIGSERIAL PRIMARY KEY,
    line_code TEXT NOT NULL,
    line_name TEXT NOT NULL,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    success BOOLEAN NOT NULL,
    latency_ms INTEGER,
    row_count INTEGER,
    source_observed_at TIMESTAMPTZ,
    query_value TEXT,
    error_type TEXT,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_realtime_fetch_events_line_time
    ON realtime_fetch_events (line_code, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_realtime_fetch_events_failed
    ON realtime_fetch_events (requested_at DESC) WHERE success = FALSE;

-- 하루의 물리적/논리적 운행편. 표준 노선은 공식 train_no, 신분당선은 SBV-* 가상 운행편을 사용한다.
CREATE TABLE IF NOT EXISTS train_runs (
    run_id TEXT PRIMARY KEY,
    service_date DATE NOT NULL,
    line_code TEXT NOT NULL,
    line_name TEXT NOT NULL,
    schedule_run_id TEXT NOT NULL,
    public_train_no TEXT,
    direction TEXT,
    service_type TEXT,
    origin_station TEXT,
    destination_station TEXT,
    scheduled_start_at TIMESTAMPTZ,
    scheduled_end_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (service_date, line_code, schedule_run_id)
);
CREATE INDEX IF NOT EXISTS idx_train_runs_service_line
    ON train_runs (service_date, line_code, scheduled_start_at);

-- 최신 상태 한 건. API/worker가 UPSERT한다.
CREATE TABLE IF NOT EXISTS train_current_state (
    run_id TEXT PRIMARY KEY REFERENCES train_runs(run_id) ON DELETE CASCADE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_observed_at TIMESTAMPTZ,
    state_source TEXT NOT NULL, -- live | predicted_from_live | schedule
    station_name TEXT,
    next_station_name TEXT,
    train_status TEXT,
    progress DOUBLE PRECISION,
    delay_seconds INTEGER,
    external_vehicle_id TEXT,
    confidence DOUBLE PRECISION,
    state_hash TEXT,
    raw_context JSONB
);
CREATE INDEX IF NOT EXISTS idx_train_current_state_updated
    ON train_current_state (updated_at DESC);

-- 상태가 실제로 바뀌었을 때만 기록하는 event history.
CREATE TABLE IF NOT EXISTS train_state_events (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES train_runs(run_id) ON DELETE CASCADE,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_observed_at TIMESTAMPTZ,
    state_source TEXT NOT NULL,
    station_name TEXT,
    next_station_name TEXT,
    train_status TEXT,
    progress DOUBLE PRECISION,
    delay_seconds INTEGER,
    external_vehicle_id TEXT,
    confidence DOUBLE PRECISION,
    state_hash TEXT NOT NULL,
    raw_context JSONB
);
CREATE INDEX IF NOT EXISTS idx_train_state_events_run_time
    ON train_state_events (run_id, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_train_state_events_captured
    ON train_state_events (captured_at DESC);

-- 외부 API의 vehicle/trainNo와 지금타 내부 운행편 ID 연결 이력.
CREATE TABLE IF NOT EXISTS vehicle_run_links (
    id BIGSERIAL PRIMARY KEY,
    service_date DATE NOT NULL,
    line_code TEXT NOT NULL,
    external_vehicle_id TEXT NOT NULL,
    run_id TEXT NOT NULL REFERENCES train_runs(run_id) ON DELETE CASCADE,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL,
    match_method TEXT NOT NULL,
    match_confidence DOUBLE PRECISION NOT NULL,
    evidence JSONB,
    UNIQUE (service_date, line_code, external_vehicle_id, run_id)
);
CREATE INDEX IF NOT EXISTS idx_vehicle_run_links_vehicle
    ON vehicle_run_links (service_date, line_code, external_vehicle_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_vehicle_run_links_run
    ON vehicle_run_links (run_id, last_seen_at DESC);

-- 예측이 다음 실제 관측과 얼마나 달랐는지 평가하는 데이터.
CREATE TABLE IF NOT EXISTS prediction_validation_events (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES train_runs(run_id) ON DELETE CASCADE,
    predicted_at TIMESTAMPTZ NOT NULL,
    predicted_station TEXT,
    predicted_next_station TEXT,
    predicted_progress DOUBLE PRECISION,
    predicted_delay_seconds INTEGER,
    prediction_source TEXT NOT NULL,
    confidence DOUBLE PRECISION,
    next_observed_at TIMESTAMPTZ,
    next_observed_station TEXT,
    station_error_count INTEGER,
    time_error_seconds INTEGER,
    details JSONB
);
CREATE INDEX IF NOT EXISTS idx_prediction_validation_run
    ON prediction_validation_events (run_id, predicted_at DESC);
