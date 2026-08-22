export type ServiceMode = 'AUTO' | 'DAY' | 'SAT' | 'END';

export type TransferInfo = {
  seconds?: number;
  distance_m?: number | null;
  alight_position?: string;
  matched?: string;
};

export type RouteTopologySegment = {
  line: string;
  from: string;
  to: string;
  transfer_walk?: number;
  transfer_info?: TransferInfo | null;
};

export type AutoRouteAlternative = {
  segments: RouteTopologySegment[];
  interchanges: string[];
  transfer_count: number;
  route_seconds: number;
  total_seconds: number;
  arrival_time: string;
  confidence: string;
};

export type AutoRouteResponse = {
  ok: boolean;
  error?: string;
  request_id?: string;
  from: string;
  to: string;
  service_mode: string;
  service_mode_reason?: string;
  route_seconds: number;
  estimated_total_seconds?: number;
  estimated_arrival_time?: string;
  estimated_confidence?: string;
  transfer_count: number;
  interchanges: string[];
  segments: RouteTopologySegment[];
  alternatives?: AutoRouteAlternative[];
};

export type DetailedSegment = {
  line: string;
  from: string;
  to: string;
  train_no?: string;
  display_train_no?: string;
  train_no_visible?: boolean;
  service?: string;
  board_dt: string;
  alight_dt: string;
  ride_seconds?: number;
  wait_seconds?: number;
  delay?: number;
  delay_seconds?: number;
  confidence?: string;
  location_kind?: string;
  current_station?: string;
  current_status?: string;
  material_text?: string;
  method?: string;
  transfer_seconds?: number;
  transfer_info?: TransferInfo | null;
  realtime_supported?: boolean;
};

export type RouteResponse = {
  ok: boolean;
  error?: string;
  request_id?: string;
  service_mode?: string;
  service_mode_reason?: string;
  start_time?: string;
  arrival_time?: string;
  total_seconds?: number;
  segments?: DetailedSegment[];
  warnings?: string[];
};

export type StationsResponse = {
  ok: boolean;
  stations: Record<string, string[]>;
};

export type HealthResponse = {
  ok: boolean;
  version: string;
  realtime_store?: { mode?: string; redis_configured?: boolean };
};
