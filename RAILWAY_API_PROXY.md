# Railway API proxy

V14.11.1: Vercel의 `/api/:path*` 요청을 Railway persistent FastAPI 서버로 프록시합니다.

Backend: `https://jigeumta-api-production.up.railway.app`

목적:
- Vercel serverless Python cold start 제거
- Railway 프로세스의 in-memory route cache 재사용
- 브라우저 CORS 변경 없이 기존 상대경로 `/api/*` 유지

롤백: `vercel.json`의 `rewrites` 항목을 제거하면 기존 Vercel `server.py` 함수로 돌아갑니다.
