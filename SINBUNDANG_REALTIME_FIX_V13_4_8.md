# 신분당선 실시간 신뢰도 수정 — V13.4.8

## 원인

V13.4.7은 신분당선 실시간 위치 API를 `신분당선` 이름으로 호출하고, API `trainNo`가 Rail.Blue의 `DX####` 열번과 숫자부가 같을 것이라고 가정했다. 실제 환경에서 이 둘 중 하나라도 성립하지 않으면 `observe_delays()`의 실시간 관측값이 0건이 되어 ETA가 `schedule_only`로 강등되고 신뢰도 `낮음`으로 표시될 수 있었다.

## 수정

1. 신분당선 realtimePosition 요청 후보를 `1077:신분당선` → `신분당선` 순서로 시도한다.
2. 응답 `subwayId=1077` 검증은 유지한다.
3. API 열차번호가 시간표와 직접 매칭되지 않으면 현재역, 상/하행, 종착역, 수신시각을 이용해 가장 가까운 신분당선 DIA를 보조 매칭한다.
4. 보조 매칭은 열차 ID를 직접 확인한 것이 아니므로 `live_context`로 표시하고 신뢰도는 `중간`으로 제한한다.
5. exact 열번 매칭이면 기존처럼 `live_exact`/`높음`을 사용한다.
6. 실시간 행을 받았지만 exact/context 어느 쪽도 0건이면 `realtime_train_match_failure` 로그를 남긴다.

## 진단 필드

각 구간의 `diagnostics`에서 다음 값을 확인할 수 있다.

- `positions`: API에서 받은 실시간 행 수
- `matched`: 열차번호 exact 매칭 수
- `matched_context`: 시간표 문맥 보조 매칭 수
- `unmatched_train`: 끝내 매칭하지 못한 API 열차번호 샘플
- `unmatched_station`: 시간표에서 찾지 못한 현재역 샘플
- `realtime_query`: 실제 성공한 요청 문자열

정상 예시는 `positions > 0`이고 `matched + matched_context > 0`인 상태다.
