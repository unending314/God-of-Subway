# -*- coding: utf-8 -*-
"""지금타 V14 realtime state store.

Redis는 '현재 철도망 상태'를 저장한다.
- 최신 정상 realtimePosition snapshot
- 최근 열차별 exact delay 관측
- 노선별 fetch health

엔진은 fresh snapshot만 live row로 사용하고, 오래된 snapshot은 live로 재사용하지 않는다.
대신 최근 delay 관측을 공식 시간표에 적용해 예상 소재를 계속 계산한다.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from functools import lru_cache
from typing import Any

try:
    import redis
except Exception:  # pragma: no cover - 미설치 환경에서 기존 direct 모드 유지
    redis = None

PREFIX = os.environ.get("REALTIME_REDIS_PREFIX", "jigeumta:v1").strip() or "jigeumta:v1"
DEFAULT_FRESH_SECONDS = int(os.environ.get("REALTIME_FRESH_SECONDS", "90"))
SNAPSHOT_TTL_SECONDS = int(os.environ.get("REALTIME_SNAPSHOT_TTL_SECONDS", "7200"))
DELAY_TTL_SECONDS = int(os.environ.get("REALTIME_DELAY_TTL_SECONDS", "2700"))
HEALTH_TTL_SECONDS = int(os.environ.get("REALTIME_HEALTH_TTL_SECONDS", "172800"))


def store_mode() -> str:
    mode = os.environ.get("REALTIME_STORE_MODE", "direct").strip().lower()
    return mode if mode in {"direct", "hybrid", "cache_only"} else "direct"


def redis_url() -> str:
    return os.environ.get("REDIS_URL", "").strip()


def is_configured() -> bool:
    return bool(redis_url()) and redis is not None


@lru_cache(maxsize=1)
def client():
    if redis is None:
        raise RuntimeError("redis 패키지가 설치되지 않았습니다.")
    url = redis_url()
    if not url:
        raise RuntimeError("REDIS_URL 환경변수가 설정되지 않았습니다.")
    return redis.Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
        health_check_interval=30,
    )


def _key(kind: str, line_id: str) -> str:
    return f"{PREFIX}:rt:{kind}:{line_id}"


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(raw: Any, default=None):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _parse_dt(text: Any) -> datetime | None:
    value = str(text or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
        try:
            return datetime.strptime(value[:26], fmt)
        except Exception:
            pass
    return None


def put_snapshot(*, line_id: str, line_name: str, rows: list[dict], query: str = "",
                 fetched_at: datetime | None = None, source_observed_at: datetime | None = None) -> None:
    fetched_at = fetched_at or datetime.now()
    payload = {
        "schema": 1,
        "line_id": str(line_id),
        "line": str(line_name),
        "fetched_at": fetched_at.strftime("%Y-%m-%d %H:%M:%S"),
        "source_observed_at": source_observed_at.strftime("%Y-%m-%d %H:%M:%S") if source_observed_at else "",
        "query": str(query or ""),
        "rows": rows,
    }
    pipe = client().pipeline(transaction=False)
    pipe.set(_key("snapshot", line_id), _dump(payload), ex=SNAPSHOT_TTL_SECONDS)
    pipe.set(_key("health", line_id), _dump({
        "line_id": str(line_id), "line": str(line_name), "ok": True,
        "attempted_at": payload["fetched_at"], "last_success_at": payload["fetched_at"],
        "row_count": len(rows), "error": "",
    }), ex=HEALTH_TTL_SECONDS)
    pipe.execute()


def mark_fetch_failure(*, line_id: str, line_name: str, error: str, attempted_at: datetime | None = None) -> None:
    attempted_at = attempted_at or datetime.now()
    health = get_health(line_id) or {}
    payload = {
        "line_id": str(line_id), "line": str(line_name), "ok": False,
        "attempted_at": attempted_at.strftime("%Y-%m-%d %H:%M:%S"),
        "last_success_at": str(health.get("last_success_at") or ""),
        "row_count": int(health.get("row_count") or 0),
        "error": str(error or "실시간 위치 조회 실패")[:2000],
    }
    client().set(_key("health", line_id), _dump(payload), ex=HEALTH_TTL_SECONDS)


def get_snapshot(line_id: str, *, now: datetime | None = None, fresh_seconds: int | None = None) -> dict | None:
    raw = client().get(_key("snapshot", line_id))
    payload = _load(raw)
    if not isinstance(payload, dict):
        return None
    now = now or datetime.now()
    fetched = _parse_dt(payload.get("fetched_at"))
    age = max(0, int((now - fetched).total_seconds())) if fetched else None
    limit = DEFAULT_FRESH_SECONDS if fresh_seconds is None else int(fresh_seconds)
    payload["cache_age_seconds"] = age
    payload["cache_state"] = "fresh" if age is not None and age <= limit else "stale"
    return payload


def get_health(line_id: str) -> dict | None:
    value = _load(client().get(_key("health", line_id)))
    return value if isinstance(value, dict) else None


def get_delay_rows(line_id: str) -> list[dict]:
    value = _load(client().get(_key("delay", line_id)), [])
    return value if isinstance(value, list) else []


def put_delay_rows(line_id: str, rows: list[dict]) -> None:
    client().set(_key("delay", line_id), _dump(rows[:160]), ex=DELAY_TTL_SECONDS)


def merge_delay_rows(line_id: str, new_rows: list[dict], *, max_rows: int = 120) -> list[dict]:
    """열차별 최신 exact 관측 하나만 유지한다. 단일 collector를 전제로 한 가벼운 merge."""
    current = get_delay_rows(line_id)
    merged: dict[tuple[str, str], dict] = {}
    for row in [*current, *new_rows]:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("line") or ""), str(row.get("train_no") or ""))
        if not all(key):
            continue
        old = merged.get(key)
        if old is None or str(row.get("observed_at") or "") >= str(old.get("observed_at") or ""):
            merged[key] = row
    rows = sorted(merged.values(), key=lambda x: str(x.get("observed_at") or ""), reverse=True)[:max_rows]
    put_delay_rows(line_id, rows)
    return rows


def position_cache_entry(*, line_id: str, line_name: str, now: datetime | None = None) -> dict:
    """engine.position_cache 호환 entry를 반환한다.

    stale snapshot의 raw row는 live로 재사용하지 않는다. stale이면 rows=[]로 내려 보내고,
    별도 delay cache가 공식 시간표 기반 예상 소재를 유지하도록 한다.
    """
    snap = get_snapshot(line_id, now=now)
    if not snap:
        health = get_health(line_id) or {}
        return {
            "rows": [], "available": False, "error": str(health.get("error") or "Redis 실시간 snapshot 없음"),
            "query": "", "cache_state": "miss", "cache_age_seconds": None,
            "realtime_source": "redis", "last_success_at": str(health.get("last_success_at") or ""),
        }
    fresh = snap.get("cache_state") == "fresh"
    health = get_health(line_id) or {}
    return {
        "rows": list(snap.get("rows") or []) if fresh else [],
        "available": bool(fresh),
        "error": "" if fresh else f"Redis snapshot stale ({snap.get('cache_age_seconds')}s)",
        "query": str(snap.get("query") or ""),
        "cache_state": str(snap.get("cache_state") or "miss"),
        "cache_age_seconds": snap.get("cache_age_seconds"),
        "source_observed_at": str(snap.get("source_observed_at") or ""),
        "fetched_at": str(snap.get("fetched_at") or ""),
        "realtime_source": "redis",
        "last_success_at": str(health.get("last_success_at") or snap.get("fetched_at") or ""),
        "stale_row_count": 0 if fresh else len(snap.get("rows") or []),
    }


def status_for_lines(line_ids: dict[str, str], *, now: datetime | None = None) -> dict:
    if not is_configured():
        return {"configured": False, "mode": store_mode(), "lines": {}}
    out = {}
    for line_name, line_id in line_ids.items():
        try:
            entry = position_cache_entry(line_id=line_id, line_name=line_name, now=now)
            out[line_name] = {k: entry.get(k) for k in (
                "available", "cache_state", "cache_age_seconds", "fetched_at", "source_observed_at",
                "last_success_at", "stale_row_count", "error",
            )}
        except Exception as exc:
            out[line_name] = {"available": False, "cache_state": "error", "error": f"{type(exc).__name__}: {exc}"}
    return {"configured": True, "mode": store_mode(), "lines": out}


def config_summary() -> dict:
    return {
        "mode": store_mode(),
        "redis_configured": is_configured(),
        "fresh_seconds": DEFAULT_FRESH_SECONDS,
        "snapshot_ttl_seconds": SNAPSHOT_TTL_SECONDS,
        "delay_ttl_seconds": DELAY_TTL_SECONDS,
    }
