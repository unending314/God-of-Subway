# 예측 신뢰도와 오류 로그 — V13.4.4

## 1. 신뢰도 판정 기준

지금타의 신뢰도는 **경로 전체의 정확도 점수**가 아니라, 각 추천 열차 ETA를 만들 때 사용한 운행정보의 강도를 나타낸다.
최종 화면의 신뢰도는 여러 구간 중 **가장 낮은 신뢰도**를 표시한다.

### 높음

해당 열차번호가 서울시 `realtimePosition`에서 직접 확인되고, 그 열차의 공식 시간표와 현재 위치를 결합해 ETA를 계산한 경우다.
엔진 내부 `delay_source=live_exact`가 대표적이다.

### 중간

해당 추천 열차가 현재 API에 직접 잡히지는 않았지만 다음 중 하나가 있는 경우다.

- 같은 열차의 최근 직접 관측값을 브라우저 캐시에서 재사용 (`cached_exact`)
- 현재 운행 중인 같은 방향/등급 열차들의 지연 중앙값을 공식 시간표에 적용 (`live_median`)
- 탑승 열차 추적 중 API에서 일시적으로 사라졌지만, 다른 실시간 관측 또는 최근 캐시가 남아 있어 잠금 추적을 유지

즉 열차 자체를 지금 직접 보고 있지는 않지만, 현재 운행상황을 반영할 근거가 남아 있는 상태다.

### 낮음

추천 열차 ETA를 계산할 때 **사용 가능한 실시간 관측이 하나도 없어 공식 시간표만 사용한 경우**다.
주요 발생 조건은 다음과 같다.

1. 서울시 실시간 위치 API가 실패/타임아웃/오류를 반환함
2. API 응답은 왔지만 해당 노선에서 시간표와 매칭되는 열차가 0대임
3. 특정 시간대에 아직 실시간 API에 잡힌 열차가 없고, 최근 exact-train 캐시도 없음
4. 탑승 열차가 API에서 사라졌고 동일 노선의 다른 유효 관측/최근 캐시도 없어 시간표만으로 임시 추적함

`낮음` 자체가 계산 오류를 뜻하지는 않는다. **열차 운행 여부·정차역·공식 시각표는 사용하지만 현재 지연을 검증할 실시간 근거가 없는 상태**다.

## 2. 급행 통과역 회귀 방지

V13.4.2에서 1호선 K19xx가 엔진에서 급행으로 재분류되었지만, 원본 DIA의 통과역에 `call=true`가 남아 있었다.
예를 들어 당정은 `arr=null, dep=통과시각, call=true`였기 때문에 승차 가능 역으로 오판되었다.

V13.4.3에서는 다음 두 방어를 모두 적용한다.

1. `schedule_weekday.json`, `schedule_holiday.json`의 K19xx 중간 통과역을 `call=false`로 교정
2. `engine.py` 정규화 단계에서 급행의 중간역이 `arr=null + dep 존재`이면 원본 `call` 값과 무관하게 승하차 불가로 강제
3. 같은 잘못된 플래그에서 파생된 `route_graph.json`의 1호선 최소 주행시간도 정차 가능 역 기준으로 재생성

`route_pair()`는 `call=true`인 역만 승차/하차점으로 사용한다. 자동 경로 후보 그래프도 같은 정차 가능 기준과 일치한다.

## 3. 오류 로그 구조

오류는 `observability.py`를 통해 구조화 JSON으로 기록된다.

기록 대상:

- 서버 Python 예외 (`server_exception`)
- 엔진이 `ok=false`를 반환한 계산 실패 (`engine_failure`)
- 브라우저 JavaScript 오류 (`js_error`)
- 처리되지 않은 Promise 오류 (`unhandled_promise_rejection`)
- API 요청 실패/타임아웃 (`api_request_failed`)
- 낮은 신뢰도 결과 (`low_confidence_route`, warning)

각 오류에는 UUID `error_id`와 요청 단위 `request_id`가 붙는다. 서버 오류는 UI에도 `오류 ID`가 표시되므로, 사용자가 제보한 ID로 서버 로그를 바로 역추적할 수 있다.

로그에는 재현에 필요한 출발/도착역, 노선, 시간, 실패 구간, diagnostics, traceback을 넣고, `SEOUL_API_KEY`와 전체 실시간 캐시 내용은 저장하지 않는다.

## 4. 저장 위치

### Vercel 기본

항상 stdout에 `JIGEUMTA_LOG {...}` 형태로 출력된다. 별도 DB 설정 없이 배포 즉시 Vercel Function Logs에서 검색할 수 있다.

### 로컬 실행

기본적으로 `logs/error_events.jsonl`에도 누적된다. `.gitignore` 대상이다.

### PostgreSQL 영구 저장

Vercel 로그 보존기간과 무관하게 장기 분석하려면 환경변수 `DATABASE_URL`에 PostgreSQL 연결 문자열을 설정한다.
Neon, Supabase 등 PostgreSQL 호환 DB를 사용할 수 있다.

앱은 최초 기록 시 `app_error_logs` 테이블과 인덱스를 자동 생성한다. 수동 생성이 필요하면 `LOGGING_DB_SCHEMA.sql`을 실행한다.

필요 환경변수:

```text
DATABASE_URL=postgresql://...
```

낮은 신뢰도는 정상적인 운영 상태에서도 자주 발생할 수 있으므로 기본적으로 별도 warning 로그를 저장하지 않는다. 원인 빈도를 분석할 때만 다음으로 켠다.

```text
LOG_LOW_CONFIDENCE=1
```

## 5. 조회 예시

최근 오류 50건:

```sql
SELECT created_at, event_type, endpoint, error_type, message, request_id, id
FROM app_error_logs
WHERE level = 'error'
ORDER BY created_at DESC
LIMIT 50;
```

특정 오류 ID 추적:

```sql
SELECT *
FROM app_error_logs
WHERE id = '사용자가 제보한 UUID';
```

낮은 신뢰도 발생 빈도:

```sql
SELECT date_trunc('hour', created_at) AS hour, count(*)
FROM app_error_logs
WHERE event_type = 'low_confidence_route'
GROUP BY 1
ORDER BY 1 DESC;
```


## 시간표 무결성 로그

V13.4.4부터 서버 시작 시 원본 시간표의 `call/service` 값이 구조 기반 통과역 규칙과 일치하는지 검사합니다. 불일치가 있으면 `timetable_integrity_error` 이벤트로 구조화 로그를 기록하며 `/api/health`의 `timetable_integrity`에서도 확인할 수 있습니다.
