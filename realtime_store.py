# -*- coding: utf-8 -*-
"""지금타 V14.11.4 realtime state store.

Redis 역할:
- 노선별 최신 realtimePosition snapshot
- 최근 열차별 exact delay 관측
- API 원본 열차의 last-seen / missing 상태
- collector fetch health

서울시 realtimePosition은 ground truth DB가 아니라 오류 가능한 센서로 취급한다.
최신 snapshot에서 특정 열차가 빠졌다고 그 열차 상태를 즉시 삭제하지 않으며,
마지막 관측은 train-state/delay cache에 남겨 시간표 기반 예측에 사용한다.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from functools import lru_cache
from typing import Any

try:
    import redis
except Exception:  # pragma: no cover - 미설치 환경에서 기존 direct 모드 유지
    redis = None

PREFIX = os.environ.get("REALTIME_REDIS_PREFIX", "jigeumta:v2").strip() or "jigeumta:v2"
DEFAULT_FRESH_SECONDS = int(os.environ.get("REALTIME_FRESH_SECONDS", "60"))
SNAPSHOT_TTL_SECONDS = int(os.environ.get("REALTIME_SNAPSHOT_TTL_SECONDS", "7200"))
DELAY_TTL_SECONDS = int(os.environ.get("REALTIME_DELAY_TTL_SECONDS", "14400"))
TRAIN_STATE_TTL_SECONDS = int(os.environ.get("REALTIME_TRAIN_STATE_TTL_SECONDS", "14400"))
HEALTH_TTL_SECONDS = int(os.environ.get("REALTIME_HEALTH_TTL_SECONDS", "172800"))
MISSING_RECENT_SECONDS = int(os.environ.get("REALTIME_MISSING_RECENT_SECONDS", "600"))


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


def _fmt(dt: datetime | None) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _snapshot_signature(rows: list[dict], source_observed_at: datetime | None = None) -> str:
    """API row 순서가 바뀌어도 같은 원천 snapshot이면 같은 서명을 만든다."""
    canonical = []
    for row in rows:
        if isinstance(row, dict):
            canonical.append(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str))
        else:
            canonical.append(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str))
    canonical.sort()
    material = _fmt(source_observed_at) + "\n" + "\n".join(canonical)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()


def put_snapshot(*, line_id: str, line_name: str, rows: list[dict], query: str = "",
                 fetched_at: datetime | None = None, source_observed_at: datetime | None = None) -> bool:
    """최신 snapshot을 저장한다.

    5초 polling에서 서울시 원천 상태가 대체로 약 20초마다 갱신되므로,
    동일한 snapshot이면 payload/trains/delay를 다시 쓰지 않고 health만 갱신한다.
    반환값은 snapshot 내용이 실제로 바뀌었는지 여부다.
    """
    fetched_at = fetched_at or datetime.now()
    signature = _snapshot_signature(rows, source_observed_at)
    sig_key = _key("snapshot_sig", line_id)
    old_signature = client().get(sig_key)
    health_payload = {
        "line_id": str(line_id), "line": str(line_name), "ok": True,
        "attempted_at": _fmt(fetched_at), "last_success_at": _fmt(fetched_at),
        "source_observed_at": _fmt(source_observed_at),
        "row_count": len(rows), "error": "",
    }
    if old_signature == signature and client().get(_key("snapshot", line_id)):
        client().set(_key("health", line_id), _dump(health_payload), ex=HEALTH_TTL_SECONDS)
        return False

    payload = {
        "schema": 2,
        "line_id": str(line_id),
        "line": str(line_name),
        "fetched_at": _fmt(fetched_at),
        "source_observed_at": _fmt(source_observed_at),
        "query": str(query or ""),
        "rows": rows,
    }
    pipe = client().pipeline(transaction=False)
    pipe.set(_key("snapshot", line_id), _dump(payload), ex=SNAPSHOT_TTL_SECONDS)
    pipe.set(sig_key, signature, ex=SNAPSHOT_TTL_SECONDS)
    pipe.set(_key("health", line_id), _dump(health_payload), ex=HEALTH_TTL_SECONDS)
    pipe.execute()
    return True


def mark_fetch_success(*, line_id: str, line_name: str, row_count: int,
                       attempted_at: datetime | None = None,
                       source_observed_at: datetime | None = None) -> None:
    attempted_at = attempted_at or datetime.now()
    old = get_health(line_id) or {}
    payload = {
        "line_id": str(line_id), "line": str(line_name), "ok": True,
        "attempted_at": _fmt(attempted_at), "last_success_at": _fmt(attempted_at),
        "source_observed_at": _fmt(source_observed_at) or str(old.get("source_observed_at") or ""),
        "row_count": int(row_count), "error": "",
    }
    client().set(_key("health", line_id), _dump(payload), ex=HEALTH_TTL_SECONDS)


def mark_fetch_failure(*, line_id: str, line_name: str, error: str, attempted_at: datetime | None = None) -> None:
    attempted_at = attempted_at or datetime.now()
    health = get_health(line_id) or {}
    payload = {
        "line_id": str(line_id), "line": str(line_name), "ok": False,
        "attempted_at": _fmt(attempted_at),
        "last_success_at": str(health.get("last_success_at") or ""),
        "source_observed_at": str(health.get("source_observed_at") or ""),
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
    health = get_health(line_id) or {}
    # snapshot 내용이 같은 동안 collector는 snapshot을 다시 쓸 수도 있고 health만 갱신할 수도 있다.
    # transport freshness는 마지막 성공 poll을 기준으로 판정한다.
    last_success = _parse_dt(health.get("last_success_at")) or _parse_dt(payload.get("fetched_at"))
    age = max(0, int((now - last_success).total_seconds())) if last_success else None
    source_dt = _parse_dt(health.get("source_observed_at") or payload.get("source_observed_at"))
    source_age = max(0, int((now - source_dt).total_seconds())) if source_dt else None
    limit = DEFAULT_FRESH_SECONDS if fresh_seconds is None else int(fresh_seconds)
    payload["cache_age_seconds"] = age
    payload["source_age_seconds"] = source_age
    payload["cache_state"] = "fresh" if age is not None and age <= limit else "stale"
    return payload


def get_health(line_id: str) -> dict | None:
    value = _load(client().get(_key("health", line_id)))
    return value if isinstance(value, dict) else None


def get_delay_rows(line_id: str) -> list[dict]:
    value = _load(client().get(_key("delay", line_id)), [])
    return value if isinstance(value, list) else []


def put_delay_rows(line_id: str, rows: list[dict]) -> None:
    client().set(_key("delay", line_id), _dump(rows[:240]), ex=DELAY_TTL_SECONDS)


def merge_delay_rows(line_id: str, new_rows: list[dict], *, max_rows: int = 220) -> list[dict]:
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


def get_train_states(line_id: str) -> dict[str, dict]:
    value = _load(client().get(_key("trains", line_id)), {})
    return value if isinstance(value, dict) else {}


def update_train_states(*, line_id: str, line_name: str, observations: list[dict],
                        observed_at: datetime | None = None) -> dict[str, dict]:
    """API raw trainNo별 last-seen 상태를 유지한다.

    성공 snapshot에서 빠진 열차도 즉시 삭제하지 않고 missing_since를 기록한다.
    이 저장소는 서비스 시간표 매칭과 별개인 raw trajectory/장애 복원 레이어다.
    """
    observed_at = observed_at or datetime.now()
    now_text = _fmt(observed_at)
    current = get_train_states(line_id)
    seen: set[str] = set()

    for item in observations:
        if not isinstance(item, dict):
            continue
        api_no = str(item.get("api_train_no") or "").strip()
        if not api_no:
            continue
        seen.add(api_no)
        old = current.get(api_no) if isinstance(current.get(api_no), dict) else {}
        first_seen = str(old.get("first_seen_at") or item.get("source_observed_at") or now_text)
        last_seen = str(item.get("source_observed_at") or now_text)
        current[api_no] = {
            "line": line_name,
            "api_train_no": api_no,
            "service_train_no": str(item.get("service_train_no") or ""),
            "run_type": str(item.get("run_type") or "unknown"),
            "station": str(item.get("station") or ""),
            "status": str(item.get("status") or ""),
            "direction": str(item.get("direction") or ""),
            "first_seen_at": first_seen,
            "last_seen_at": last_seen,
            "missing_since": "",
            "presence_state": "live",
        }

    # 같은 성공 snapshot에 없어진 raw trainNo는 recent missing으로 보존한다.
    # 오래된 state는 일정 시간이 지나면 제거한다.
    pruned: dict[str, dict] = {}
    for api_no, state in current.items():
        if api_no in seen:
            pruned[api_no] = state
            continue
        if not isinstance(state, dict):
            continue
        last_seen = _parse_dt(state.get("last_seen_at"))
        age = (observed_at - last_seen).total_seconds() if last_seen else TRAIN_STATE_TTL_SECONDS + 1
        if age > TRAIN_STATE_TTL_SECONDS:
            continue
        state = dict(state)
        if not state.get("missing_since"):
            state["missing_since"] = now_text
        state["presence_state"] = "missing_recent" if age <= MISSING_RECENT_SECONDS else "stale"
        pruned[api_no] = state

    client().set(_key("trains", line_id), _dump(pruned), ex=TRAIN_STATE_TTL_SECONDS)
    return pruned


def train_state_summary(line_id: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    states = get_train_states(line_id)
    counts = {"live": 0, "missing_recent": 0, "stale": 0, "special": 0, "test": 0}
    missing = []
    for state in states.values():
        presence = str(state.get("presence_state") or "")
        if presence in counts:
            counts[presence] += 1
        run_type = str(state.get("run_type") or "")
        if run_type in {"special", "test"}:
            counts[run_type] += 1
        if presence != "live" and len(missing) < 20:
            last = _parse_dt(state.get("last_seen_at"))
            missing.append({
                "api_train_no": state.get("api_train_no", ""),
                "service_train_no": state.get("service_train_no", ""),
                "run_type": run_type,
                "station": state.get("station", ""),
                "last_seen_at": state.get("last_seen_at", ""),
                "age_seconds": max(0, int((now - last).total_seconds())) if last else None,
            })
    return {"counts": counts, "missing": missing, "total_states": len(states)}


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
        "source_age_seconds": snap.get("source_age_seconds"),
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
            state = train_state_summary(line_id, now=now)
            out[line_name] = {k: entry.get(k) for k in (
                "available", "cache_state", "cache_age_seconds", "source_age_seconds", "fetched_at", "source_observed_at",
                "last_success_at", "stale_row_count", "error",
            )}
            out[line_name]["train_states"] = state.get("counts", {})
        except Exception as exc:
            out[line_name] = {"available": False, "cache_state": "error", "error": f"{type(exc).__name__}: {exc}"}
    return {"configured": True, "mode": store_mode(), "lines": out}


def config_summary() -> dict:
    return {
        "mode": store_mode(),
        "redis_configured": is_configured(),
        "prefix": PREFIX,
        "fresh_seconds": DEFAULT_FRESH_SECONDS,
        "snapshot_ttl_seconds": SNAPSHOT_TTL_SECONDS,
        "delay_ttl_seconds": DELAY_TTL_SECONDS,
        "train_state_ttl_seconds": TRAIN_STATE_TTL_SECONDS,
        "missing_recent_seconds": MISSING_RECENT_SECONDS,
    }
