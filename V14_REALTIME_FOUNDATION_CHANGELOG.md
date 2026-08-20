# V14 Realtime Foundation Alpha

기준: v13.5.4 transfer patch

## 추가

- `realtime_store.py`: Redis 기반 노선 snapshot / exact delay / fetch health 저장소
- `realtime_worker.py`: 사용자 요청과 분리된 서울시 realtimePosition collector
- `V14_REALTIME_DB_SCHEMA.sql`: Supabase/PostgreSQL 영속 스키마
- `V14_REALTIME_BACKEND_DESIGN.md`: V14 아키텍처/이행 설계
- `/api/v1/stations`
- `/api/v1/auto-route`
- `/api/v1/route`
- `/api/v1/trip-update`
- `/api/v1/realtime/status`

## 엔진 변경

- `REALTIME_STORE_MODE=direct|hybrid|cache_only`
- Redis fresh snapshot은 기존 `position_cache` 형식으로 변환하여 ETA 엔진 재사용
- stale raw snapshot은 실시간 위치로 재사용하지 않음
- Redis exact delay cache와 브라우저 `train_delay_cache`를 열차별 최신 관측으로 병합
- `cache_only`에서는 사용자 요청이 서울시 API를 호출하지 않음

## 운영 전 남은 작업

- Redis 실제 인스턴스 연결 후 collector soak test
- source `recptnDt` 실제 갱신주기 측정
- PostgreSQL 상태 이벤트 writer 구현
- 신분당선 `vehicle_run_links`의 worker-side 지속 매칭
- Cloud Run worker/API 분리 배포
- 모바일 앱용 API 응답 contract 고정
