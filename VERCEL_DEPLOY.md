# 지금타 V9.2 — Vercel 배포

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
