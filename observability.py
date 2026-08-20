# -*- coding: utf-8 -*-
"""지금타 오류/진단 로그 수집기.

- 항상 stdout에 구조화 JSON을 남겨 Vercel Function Logs에서 즉시 확인할 수 있다.
- 로컬 실행에서는 logs/error_events.jsonl에도 누적한다.
- DATABASE_URL이 있으면 PostgreSQL에도 best-effort로 영구 저장한다.

로그 저장 실패가 실제 경로 계산을 실패시키면 안 되므로 모든 persistence 오류는 삼킨다.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback as tb_module
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
APP_VERSION = "V14.8.0"
_DB_LOCK = threading.Lock()
_DB_SCHEMA_READY = False
_LOCAL_LOCK = threading.Lock()


def new_event_id() -> str:
    return str(uuid.uuid4())


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "<max-depth>"
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in list(value)[:100]]
    return str(value)


def summarize_payload(payload: Any) -> dict[str, Any]:
    """API 키나 대형 실시간 캐시를 제외하고 재현에 필요한 입력만 남긴다."""
    if not isinstance(payload, dict):
        return {"payload_type": type(payload).__name__}
    out: dict[str, Any] = {}
    allowed = (
        "from", "to", "start_time", "day", "refresh_only", "baseline_minutes",
        "active_index", "boarded_train_no", "boarded_at",
    )
    for key in allowed:
        if key in payload:
            out[key] = _json_safe(payload.get(key))
    segments = payload.get("segments")
    if isinstance(segments, list):
        out["segments"] = [
            {
                "line": s.get("line"),
                "from": s.get("from"),
                "to": s.get("to"),
                "transfer_seconds": s.get("transfer_seconds"),
                "transfer_walk": s.get("transfer_walk"),
            }
            for s in segments[:8] if isinstance(s, dict)
        ]
    cache = payload.get("train_delay_cache")
    if isinstance(cache, list):
        out["train_delay_cache_count"] = len(cache)
    return out


def summarize_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    out: dict[str, Any] = {}
    for key in (
        "ok", "error", "failed_segment", "service_mode", "selection_method",
        "candidate_count", "arrival_time", "remaining_seconds",
    ):
        if key in result:
            out[key] = _json_safe(result.get(key))
    segments = result.get("segments") or result.get("partial_segments")
    if isinstance(segments, list):
        out["segments"] = []
        for s in segments[:8]:
            if not isinstance(s, dict):
                continue
            out["segments"].append({
                "line": s.get("line"), "from": s.get("from"), "to": s.get("to"),
                "train_no": s.get("train_no"), "service": s.get("service"),
                "confidence": s.get("confidence"), "method": s.get("method"),
                "delay_source": s.get("delay_source"), "delay_seconds": s.get("delay_seconds"),
                "diagnostics": _json_safe(s.get("diagnostics", {})),
            })
    if "diagnostics" in result:
        out["diagnostics"] = _json_safe(result.get("diagnostics"))
    return out


def _local_log_path() -> Path | None:
    configured = os.environ.get("LOG_FILE_PATH", "").strip()
    if configured:
        return Path(configured)
    if os.environ.get("VERCEL"):
        return None
    return BASE / "logs" / "error_events.jsonl"


def _write_local(event: dict[str, Any]) -> None:
    path = _local_log_path()
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _LOCAL_LOCK:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass


def _write_postgres(event: dict[str, Any]) -> None:
    global _DB_SCHEMA_READY
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        return
    try:
        import psycopg  # type: ignore
    except Exception:
        return
    try:
        with _DB_LOCK:
            with psycopg.connect(database_url, connect_timeout=3, autocommit=True) as conn:
                if not _DB_SCHEMA_READY:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS app_error_logs (
                            id UUID PRIMARY KEY,
                            created_at TIMESTAMPTZ NOT NULL,
                            level TEXT NOT NULL,
                            event_type TEXT NOT NULL,
                            endpoint TEXT,
                            request_id TEXT,
                            status_code INTEGER,
                            error_type TEXT,
                            message TEXT,
                            payload JSONB,
                            diagnostics JSONB,
                            context JSONB,
                            traceback TEXT,
                            app_version TEXT NOT NULL
                        )
                    """)
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_error_logs_created_at ON app_error_logs (created_at DESC)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_error_logs_event_type ON app_error_logs (event_type, created_at DESC)")
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_error_logs_request_id ON app_error_logs (request_id)")
                    _DB_SCHEMA_READY = True
                conn.execute(
                    """
                    INSERT INTO app_error_logs
                    (id, created_at, level, event_type, endpoint, request_id, status_code,
                     error_type, message, payload, diagnostics, context, traceback, app_version)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
                    """,
                    (
                        event["id"], event["created_at"], event["level"], event["event_type"],
                        event.get("endpoint"), event.get("request_id"), event.get("status_code"),
                        event.get("error_type"), event.get("message"),
                        json.dumps(event.get("payload"), ensure_ascii=False),
                        json.dumps(event.get("diagnostics"), ensure_ascii=False),
                        json.dumps(event.get("context"), ensure_ascii=False),
                        event.get("traceback"), event["app_version"],
                    ),
                )
    except Exception:
        # 관측 시스템 장애가 본 서비스 장애로 전파되지 않게 한다.
        pass


def record_event(
    *,
    event_type: str,
    level: str = "error",
    endpoint: str = "",
    request_id: str = "",
    status_code: int | None = None,
    error_type: str = "",
    message: str = "",
    payload: Any = None,
    diagnostics: Any = None,
    context: Any = None,
    traceback_text: str = "",
    event_id: str | None = None,
) -> str:
    event_id = event_id or new_event_id()
    event = {
        "id": event_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "event_type": event_type,
        "endpoint": endpoint or None,
        "request_id": request_id or None,
        "status_code": status_code,
        "error_type": error_type or None,
        "message": str(message or "")[:4000] or None,
        "payload": summarize_payload(payload) if payload is not None else None,
        "diagnostics": _json_safe(diagnostics) if diagnostics is not None else None,
        "context": _json_safe(context) if context is not None else None,
        "traceback": traceback_text[-12000:] if traceback_text else None,
        "app_version": APP_VERSION,
        "deployment": {
            "vercel_env": os.environ.get("VERCEL_ENV"),
            "vercel_git_commit_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA"),
            "python": sys.version.split()[0],
        },
    }
    print("JIGEUMTA_LOG " + json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
    _write_local(event)
    _write_postgres(event)
    return event_id


def record_exception(*, endpoint: str, request_id: str, payload: Any, exc: BaseException, status_code: int = 500, context: Any = None) -> str:
    return record_event(
        event_type="server_exception",
        level="error",
        endpoint=endpoint,
        request_id=request_id,
        status_code=status_code,
        error_type=type(exc).__name__,
        message=str(exc),
        payload=payload,
        context=context,
        traceback_text="".join(tb_module.format_exception(type(exc), exc, exc.__traceback__)),
    )


def record_failed_result(*, endpoint: str, request_id: str, payload: Any, result: dict[str, Any], status_code: int = 422, context: Any = None) -> str:
    return record_event(
        event_type="engine_failure",
        level="error",
        endpoint=endpoint,
        request_id=request_id,
        status_code=status_code,
        error_type="EngineResultError",
        message=str(result.get("error") or "engine returned ok=false"),
        payload=payload,
        diagnostics={"result": summarize_result(result)},
        context=context,
    )


def record_low_confidence(*, endpoint: str, request_id: str, payload: Any, result: dict[str, Any], context: Any = None) -> str | None:
    if os.environ.get("LOG_LOW_CONFIDENCE", "0").strip().lower() in {"0", "false", "off", "no"}:
        return None
    segments = result.get("segments") if isinstance(result, dict) else None
    if not isinstance(segments, list):
        return None
    low = [s for s in segments if isinstance(s, dict) and s.get("confidence") == "낮음"]
    if not low:
        return None
    return record_event(
        event_type="low_confidence_route",
        level="warning",
        endpoint=endpoint,
        request_id=request_id,
        status_code=200,
        message=f"{len(low)}개 구간이 낮음 신뢰도",
        payload=payload,
        diagnostics={"result": summarize_result(result)},
        context=context,
    )
