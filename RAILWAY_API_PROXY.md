# V14.11.1 Railway API proxy

Vercel의 `/api/:path*` 요청을 상시 실행 중인 Railway FastAPI 서비스로 rewrite합니다.

- Railway origin: `https://jigeumta-api-production.up.railway.app`
- 기존 프론트엔드 API 경로(`/api/auto_route`, `/api/route`, `/api/trip_update`, `/api/stations`, `/api/health`, `/api/client_log`)는 변경하지 않습니다.
- `server.py`는 로컬 실행 및 롤백/직접 API 실행을 위해 유지합니다.
- Railway API가 기존 legacy `/api/*` 및 `/api/v1/*` 경로를 모두 제공하는 것을 V14.11.1 패키징 시 확인했습니다.
