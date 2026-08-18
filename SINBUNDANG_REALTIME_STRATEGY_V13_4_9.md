# V13.4.9 신분당선 실시간 전략

## 결론

신분당선에서는 서울시 `realtimePosition.trainNo`를 Rail.Blue `DX9xxx` 운행열번과 동일한 값으로 간주하지 않는다.
`realtimePosition`은 위치/번호 체계를 조사하는 **진단 데이터**로만 사용한다.

생산 ETA는 다음 순서다.

1. 승차역 `realtimeStationArrival`에서 `subwayId=1077` 행 수집
2. `barvlDt`, `recptnDt`로 실제 승차 예정시각 산출
3. 방향(`updnLine`)과 종착역(`bstatnNm`)이 맞는 Rail.Blue DIA 중 해당 시각과 가장 가까운 편성 선택
4. 그 DIA의 승차역→하차역 실제 구간소요시간을 실시간 승차 예정시각에 더해 ETA 계산
5. 역 도착정보가 없거나 DIA 매칭이 불가능하면 기존 공식 시간표로 fallback

`btrainNo` 역시 DX 열번 식별자로 사용하지 않는다. 응답/로그에는 외부 번호로만 보존한다.

## 신뢰도

- `station_arrival`: **중간** — 승차역 도착예정은 실시간이지만 물리 열차와 DX DIA를 ID로 직접 동일시하지 않음
- `schedule_only`: **낮음** — 실시간 역 도착정보가 없거나 매칭 실패
- 신분당선 `realtimePosition.trainNo` 일치만으로 **높음**으로 승격하지 않음

## 실제 API 진단

배포 환경에 다음을 설정한다.

```text
SEOUL_API_KEY=...
JIGEUMTA_DEBUG_TOKEN=<긴 임의 문자열>
```

그 뒤 다음 엔드포인트를 호출하면 한 시점의 `realtimePosition`과 강남/판교/정자 도착정보를 동시에 저장한다.

```text
GET /api/debug/sinbundang_probe?token=<JIGEUMTA_DEBUG_TOKEN>
```

선택 역 변경:

```text
GET /api/debug/sinbundang_probe?token=...&stations=강남,양재,판교,정자
```

응답은 API 키를 포함하지 않으며 Vercel 구조화 로그에도 `sinbundang_realtime_probe`로 기록된다. `DATABASE_URL`이 설정되어 있으면 기존 `app_error_logs`에도 영구 저장된다.

## 10분 연속 진단

로컬에서 서울시 API 키를 환경변수로 설정한 뒤:

```bash
python tools/probe_sinbundang_api.py --duration 600 --interval 20
```

생성 파일:

- `probe_output/<timestamp>/snapshots.jsonl`
- `probe_output/<timestamp>/analysis.json`

자동 판정 항목:

- 같은 `trainNo`가 여러 snapshot에서 반복되는지
- 같은 번호가 실제로 역을 이동하는지
- 한 snapshot에서 같은 번호가 여러 위치에 중복되는지
- `realtimePosition.trainNo`와 `realtimeStationArrival.btrainNo`가 겹치는지

번호가 안정적으로 이동하더라도 그것은 **단기 추적 ID일 가능성**만 의미하며 Rail.Blue DX 열번과 동일하다는 뜻은 아니다.
