export function hhmm(date = new Date()): string {
  const h = String(date.getHours()).padStart(2, '0');
  const m = String(date.getMinutes()).padStart(2, '0');
  return `${h}:${m}`;
}

export function formatClock(value?: string): string {
  if (!value) return '-';
  const match = value.match(/(?:T|\s)(\d{2}):(\d{2})/);
  return match ? `${match[1]}:${match[2]}` : value;
}

export function formatDuration(seconds?: number): string {
  if (seconds == null || !Number.isFinite(seconds)) return '-';
  const total = Math.max(0, Math.round(seconds));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h) return `${h}시간 ${m}분`;
  return `${m}분`;
}

export function formatTransfer(seconds?: number): string {
  if (seconds == null) return '-';
  const total = Math.max(0, Math.round(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return s ? `${m}분 ${s}초` : `${m}분`;
}
