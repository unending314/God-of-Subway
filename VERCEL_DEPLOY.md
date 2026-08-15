# 지금타 V10 — Vercel 배포

## GitHub 저장소 구조
저장소 루트에 이 폴더의 파일을 그대로 올립니다.

핵심 파일:
- server.py : Vercel FastAPI entrypoint
- engine.py : 지하철 계산 엔진
- index.html : 프론트엔드
- *.json : 공식 시간표 / 역 / 공휴일 데이터
- pyproject.toml
- .python-version
- vercel.json

## Vercel Dashboard
1. https://vercel.com/new
2. GitHub 저장소 Import
3. Framework Preset은 FastAPI 자동 감지값 사용
   - 자동 감지가 안 되면 Other를 선택해도 됨
4. Root Directory: 저장소 루트
5. Build Command / Output Directory는 직접 지정하지 않음
6. Environment Variables:
   SEOUL_API_KEY = 서울 열린데이터광장 인증키
7. Deploy

## 배포 후 확인
- /
- /api/health
- /api/stations

/api/health에서:
- ok: true
- api_key_configured: true
인지 확인.

## 중요
기존 Render용 app.py처럼 별도 서버를 띄우지 않습니다.
Vercel이 server.py의 FastAPI `app`을 Python Function으로 실행합니다.

API 키는 GitHub에 절대 커밋하지 않습니다.
이미 외부에 공개된 키라면 새 키로 교체하는 것을 권장합니다.


## V10 Vercel KST 수정
Vercel 런타임은 기본 UTC이므로 Python의 `datetime.now()`를 직접 사용하면
서울시 시간표/실시간 API(KST)와 9시간 차이가 발생합니다.

V10부터 모든 운행 계산의 현재시각을 `Asia/Seoul`로 고정합니다.
- 실시간 열차 후보 판정
- API 데이터 freshness
- AUTO 공휴일 판정
- 라이브 추적
- 승차 가능시간 계산

프론트엔드의 사용자 입력시각도 한국시간 기준으로 동일한 시간축에서 비교됩니다.


## V10 UI 개선
- 열차 후보/선택 열차에 실제 운행 시발역 → 종착역 표시
- 일반열차 / 급행열차 구분을 텍스트로 명확히 표시
- 급행 배지는 기존처럼 유지
- 역 입력창 클릭만으로 전체 역 목록을 표시하지 않음
- 한 글자 이상 입력했을 때만 검색 후보 표시
- 초성 검색 지원
  · `ㅅ` → 초성이 ㅅ으로 시작하는 역
  · `ㅅㄱ` → 초성이 ㅅㄱ으로 시작하는 역
  · `성` → '성'으로 시작하는 역
- 최대 8개 후보 표시, ↑/↓/Enter/Esc 키보드 조작 지원


## V10 — 자동 지하철 길찾기
- 출발역/도착역만 입력하면 지원 노선 전체에서 빠른 경로 자동 탐색
- 공식 열차별 시간표에서 생성한 station-line 그래프를 Dijkstra로 탐색
- 비용 = 시간표 기반 차내 주행시간 + 환승 기본 4분
- 자동 생성 경로를 기존 구간 편집기에 바로 채움
- 이후 기존 realtimePosition ETA 엔진으로 즉시 재계산
- 자동 경로 생성 후에도 노선/역/환승시간을 사용자가 직접 수정 가능
- 초성 검색 지원
- 현재 지원: 1~9호선, 경의중앙선, 수인분당선

예:
마포구청 → 성균관대
6호선 마포구청→합정
2호선 합정→신도림
1호선 신도림→성균관대

주의:
- V10 자동 경로 탐색 자체는 번들된 공식 시간표 그래프 기준
- 이후 실제 도착시간은 실시간 열차 위치/지연으로 별도 재계산
- 같은 이름이지만 서로 다른 역인 5호선 양평 / 경의중앙선 양평은 환승 연결에서 제외
