# 지금타 Mobile v0.1.0

Railway의 지금타 API를 직접 소비하는 Expo / React Native 앱의 첫 실행형 골격입니다.

## 기술 스택

- Expo SDK 57
- React Native 0.86
- React 19.2
- TypeScript
- 백엔드: `https://jigeumta-api-production.up.railway.app/api/v1/*`

## 현재 구현

- API health/version 확인
- 전체 역 목록 로딩
- 출발/도착역 검색 및 선택
- 출발/도착역 교환
- `POST /api/v1/auto-route` 자동 경로 탐색
- 선택된 topology에 대해 `POST /api/v1/route` 상세 열차/ETA 계산
- 총 소요시간/도착 예정시각/신뢰도/환승역 표시
- 구간별 열차, 승하차 시각, 현재 소재, 지연, 환승시간 표시
- 대안 경로 최대 3개 요약
- API 오류/로딩 상태 처리

## 실행

```bash
npm install
cp .env.example .env
npx expo start
```

SDK 57은 development build를 기준으로 개발하는 것을 권장합니다. Android 실제 기기/에뮬레이터에서는 다음 흐름을 권장합니다.

```bash
npx expo prebuild
npx expo run:android
```

EAS 내부 테스트 빌드:

```bash
npm install -g eas-cli
eas login
eas build --profile preview --platform android
```

## API

기본 API 주소는 `EXPO_PUBLIC_API_BASE_URL`로 변경할 수 있습니다. 미설정 시 production Railway API를 사용합니다.

## 다음 구현 순서

1. 대안 경로 선택 후 상세 재계산
2. `trip-update` 기반 탑승 열차 고정/20초 주기 라이브 추적
3. 최근 검색 및 즐겨찾기
4. 푸시 기반 장애/지연 알림
5. 앱스토어용 아이콘/스플래시 및 EAS production signing
