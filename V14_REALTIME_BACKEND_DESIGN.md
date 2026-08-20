# 지금타 V14 Realtime Backend 설계

## 목표

1. 사용자 요청 시 서울시 API를 호출하지 않는다.
2. Collector가 실시간 위치를 중앙 수집해 Redis에 저장한다.
3. 서울시 API가 잠깐 실패해도 최근 실제 관측 + 공식 시간표로 예상 소재를 이어간다.
4. 신분당선은 외부 `trainNo`를 정체성으로 쓰지 않고 기존 `SBV-*` 가상 운행편을 내부 운행편으로 사용한다.
5. Web과 Mobile App은 같은 FastAPI를 사용한다.

## 런타임 구조

```text
Seoul realtimePosition
        |
        v
realtime_worker.py
        |
        +---- Redis ------------------------------+
        |     latest fresh snapshot              |
        |     exact delay memory                 |
        |     fetch health                       |
        |                                        v
        +---- PostgreSQL (2단계)            FastAPI / engine.py
              run registry                       |
              state events                 +-----+-----+
              vehicle-run links            |           |
              prediction validation       Web         App
```

## Redis Key

기본 prefix: `jigeumta:v1`

- `jigeumta:v1:rt:snapshot:{line_code}`: 마지막 정상 realtimePosition snapshot
- `jigeumta:v1:rt:delay:{line_code}`: 열차별 최근 exact delay 관측
- `jigeumta:v1:rt:health:{line_code}`: 마지막 fetch 성공/실패 상태

예: 신분당선은 `line_code=1077`.

### Freshness

- 기본 fresh: 90초
- snapshot 보존: 2시간
- delay memory 보존: 45분
- stale snapshot의 raw 위치는 live로 재사용하지 않는다.
- 일반 노선은 `delay`를 공식 시간표에 적용해 예상 소재를 계산한다.
- 신분당선은 `SBV-*` 가상 운행편의 공개 시간표 위치로 강등한다.

이 정책은 오래된 역 위치를 그대로 '실시간'으로 표시하는 오류를 막으면서도 연속성을 유지한다.

## Store mode

`REALTIME_STORE_MODE`:

- `direct`: V13.5.4 기존 방식. 요청이 서울시 API 직접 조회.
- `hybrid`: Redis fresh snapshot 우선. miss/stale이면 서울시 API 직접 fallback. 이행기 권장.
- `cache_only`: Redis만 조회. miss/stale이면 공식 시간표/최근 delay 기반 fallback. 앱 출시 운영 목표.

## Server-side delay cache

V13.5.4에는 이미 브라우저 `train_delay_cache`를 최대 35분 활용하는 로직이 있다.
V14 worker는 같은 포맷의 exact delay를 Redis에 저장한다.

따라서 API 장애 시:

```text
Redis fresh snapshot 없음
 -> Redis exact delay memory
 -> estimated_train_location(공식 시간표 + 최근 지연)
 -> 예상 소재 표시
```

브라우저 localStorage는 보조 신호로만 남기고 서버 Redis가 우선적인 공용 기억장치가 된다.

## 신분당선

현재 엔진의 장점을 그대로 사용한다.

```text
realtime vehicle(trainNo)
 -> 출발역 ETA
 -> ±3분의 SBV-* virtual run 결합
 -> client/server tracking ID = SBV-*
```

차량이 API에서 사라지면 `tracked_sinbundang_virtual_segment()`가 SBV 운행편의 공개 시간표를 이용해 `신사 출발 전 예상`, `A → B 이동 예상` 등을 계속 표시한다.

2단계에서는 worker가 `vehicle_run_links`에 결합을 지속 저장하여 HTTP 요청을 넘어 같은 vehicle-run 관계를 기억한다.

## 내부 run_id

DB에서는 서비스 날짜까지 포함한다.

- 표준 노선: `20260819:1001:K1234`
- 신분당선: `20260819:1077:SBV-WD-D-001`

API 화면 표시용 열차번호와 내부 식별자는 분리한다.

## 배포 단계

### Phase A - Foundation (이번 패치)

- `realtime_store.py`
- `realtime_worker.py`
- engine Redis read path
- server-side delay cache
- `/api/v1/*` alias
- `/api/v1/realtime/status`

### Phase B - Persistent identity

- PostgreSQL `train_runs`
- `train_current_state`
- `vehicle_run_links`
- 신분당선 worker-side vehicle ↔ SBV matching
- 상태 변화 event만 PostgreSQL 기록

### Phase C - Predictive state

- 마지막 실제 상태 이후 station-to-station progression 계산
- `LIVE -> PREDICTED_FROM_LIVE -> STALE -> SCHEDULE` 상태 전이
- prediction validation 자동 축적
- 노선별 stale threshold 튜닝

### Phase D - App backend

- 기존 endpoint 유지 + `/api/v1` 고정 contract
- 인증/즐겨찾기/사용자 trip session 추가
- Web과 App 모두 동일 API 사용
- 서울시 API key, DB/Redis credential은 서버에만 존재

## 환경변수

```text
SEOUL_API_KEY=...
REDIS_URL=rediss://...
REALTIME_STORE_MODE=hybrid
REALTIME_POLL_SECONDS=60
REALTIME_FRESH_SECONDS=90
REALTIME_SNAPSHOT_TTL_SECONDS=7200
REALTIME_DELAY_TTL_SECONDS=2700
REALTIME_WORKER_CONCURRENCY=4
```

운영 안정화 후 API 서버는 `REALTIME_STORE_MODE=cache_only`로 전환한다.
