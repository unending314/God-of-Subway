import type {
  AutoRouteResponse,
  HealthResponse,
  RouteResponse,
  RouteTopologySegment,
  ServiceMode,
  StationsResponse,
} from '../types/api';

const DEFAULT_BASE = 'https://jigeumta-api-production.up.railway.app';
export const API_BASE = (process.env.EXPO_PUBLIC_API_BASE_URL || DEFAULT_BASE).replace(/\/$/, '');

async function requestJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      Accept: 'application/json',
      'Content-Type': 'application/json',
      ...(init?.headers || {}),
    },
  });
  const body = (await response.json()) as T & { error?: string };
  if (!response.ok) {
    throw new Error(body?.error || `HTTP ${response.status}`);
  }
  return body;
}

export const api = {
  health: () => requestJson<HealthResponse>('/api/health'),
  stations: () => requestJson<StationsResponse>('/api/v1/stations'),
  autoRoute: (from: string, to: string, startTime: string, day: ServiceMode = 'AUTO') =>
    requestJson<AutoRouteResponse>('/api/v1/auto-route', {
      method: 'POST',
      body: JSON.stringify({ from, to, start_time: startTime, day }),
    }),
  route: (segments: RouteTopologySegment[], startTime: string, day: ServiceMode = 'AUTO') =>
    requestJson<RouteResponse>('/api/v1/route', {
      method: 'POST',
      body: JSON.stringify({ segments, start_time: startTime, day, refresh_only: false }),
    }),
  tripUpdate: (payload: unknown) =>
    requestJson<RouteResponse>('/api/v1/trip-update', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
};
