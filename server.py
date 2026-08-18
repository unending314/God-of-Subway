# -*- coding: utf-8 -*-
from pathlib import Path
import os
import uuid
import hmac

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

import engine
import observability

BASE = Path(__file__).resolve().parent
VERSION = "V13.4.9.0-vercel"

app = FastAPI(
    title="지금타",
    docs_url=None,
    redoc_url=None,
)

TIMETABLE_INTEGRITY = engine.timetable_integrity_report()
if not TIMETABLE_INTEGRITY.get("ok"):
    observability.record_event(
        event_type="timetable_integrity_error",
        level="error",
        endpoint="startup",
        error_type="TimetableIntegrityError",
        message=f"시간표 무결성 오류 {TIMETABLE_INTEGRITY.get('issue_count', 0)}건",
        diagnostics=TIMETABLE_INTEGRITY,
    )


def _request_id(request: Request) -> str:
    return request.headers.get("x-request-id") or str(uuid.uuid4())


def _request_context(request: Request) -> dict:
    return {
        "user_agent": request.headers.get("user-agent", "")[:500],
        "referer": request.headers.get("referer", "")[:1000],
    }


def _response(data: dict, status_code: int, request_id: str) -> JSONResponse:
    return JSONResponse(data, status_code=status_code, headers={"X-Request-ID": request_id})



def _record_realtime_match_issues(*, endpoint: str, request_id: str, payload, result, context):
    """실시간 행은 왔는데 시간표에 하나도 붙지 않은 경우를 항상 구조화 로그로 남긴다."""
    segments = result.get("segments") if isinstance(result, dict) else None
    if not isinstance(segments, list):
        return
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        diag = segment.get("diagnostics")
        if not isinstance(diag, dict):
            continue
        line = segment.get("line", "")
        if line == "신분당선":
            arrival_rows = int(diag.get("station_arrival_rows") or 0)
            arrival_matched = int(diag.get("station_arrival_matched") or 0)
            if arrival_rows > 0 and arrival_matched == 0:
                observability.record_event(
                    event_type="sinbundang_station_arrival_match_failure",
                    level="warning",
                    endpoint=endpoint, request_id=request_id, status_code=200,
                    message=f"신분당선 {arrival_rows}건 도착정보 수신 후 DX 시간표 매칭 0건",
                    payload=payload,
                    diagnostics={
                        "segment_index": index, "from": segment.get("from", ""), "to": segment.get("to", ""),
                        "station_arrival_rows": arrival_rows,
                        "station_arrival_train_numbers": diag.get("station_arrival_train_numbers", [])[:20],
                        "station_arrival_error": diag.get("station_arrival_error", ""),
                    },
                    context=context,
                )
            # realtimePosition은 신분당선 생산 ETA에서 진단용이므로 번호 매칭 실패를 오류로 보지 않는다.
            continue

        positions = int(diag.get("positions") or 0)
        matched = int(diag.get("matched") or 0)
        matched_context = int(diag.get("matched_context") or 0)
        if positions <= 0 or matched + matched_context > 0:
            continue
        observability.record_event(
            event_type="realtime_train_match_failure",
            level="warning",
            endpoint=endpoint,
            request_id=request_id,
            status_code=200,
            message=f"{line} 실시간 {positions}건 수신 후 시간표 매칭 0건",
            payload=payload,
            diagnostics={
                "segment_index": index,
                "line": line,
                "from": segment.get("from", ""),
                "to": segment.get("to", ""),
                "realtime_query": diag.get("realtime_query", ""),
                "positions": positions,
                "unmatched_train": diag.get("unmatched_train", [])[:10],
                "unmatched_station": diag.get("unmatched_station", [])[:10],
            },
            context=context,
        )

async def _run_engine_endpoint(request: Request, endpoint: str, fn):
    request_id = _request_id(request)
    payload = None
    context = _request_context(request)
    try:
        payload = await request.json()
        result = await run_in_threadpool(fn, payload)
        if not result.get("ok"):
            error_id = await run_in_threadpool(
                observability.record_failed_result,
                endpoint=endpoint,
                request_id=request_id,
                payload=payload,
                result=result,
                status_code=422,
                context=context,
            )
            result = dict(result)
            result["error_id"] = error_id
            result["request_id"] = request_id
            return _response(result, 422, request_id)

        await run_in_threadpool(
            observability.record_low_confidence,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
            result=result,
            context=context,
        )
        await run_in_threadpool(
            _record_realtime_match_issues,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
            result=result,
            context=context,
        )
        result = dict(result)
        result["request_id"] = request_id
        return _response(result, 200, request_id)
    except ValueError as e:
        error_id = await run_in_threadpool(
            observability.record_exception,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
            exc=e,
            status_code=422,
            context=context,
        )
        return _response(
            {"ok": False, "error": f"{type(e).__name__}: {e}", "error_id": error_id, "request_id": request_id},
            422,
            request_id,
        )
    except Exception as e:
        error_id = await run_in_threadpool(
            observability.record_exception,
            endpoint=endpoint,
            request_id=request_id,
            payload=payload,
            exc=e,
            status_code=500,
            context=context,
        )
        return _response(
            {"ok": False, "error": f"{type(e).__name__}: {e}", "error_id": error_id, "request_id": request_id},
            500,
            request_id,
        )


@app.get("/")
def home():
    return FileResponse(BASE / "index.html", media_type="text/html; charset=utf-8")


@app.get("/jigeumta_logo_140.png")
def site_logo():
    return FileResponse(BASE / "jigeumta_logo_140.png", media_type="image/png")


@app.get("/api/health")
def health():
    today_now = engine.now_kst()
    today_mode, today_reason = engine.resolve_service_mode("AUTO", today_now)
    today_holiday = engine.holiday_info(today_now)
    return {
        "ok": True,
        "version": VERSION,
        "today_service_mode": today_mode,
        "today_service_reason": today_reason,
        "today_is_holiday": bool(today_holiday),
        "line1_weekday_trains": len(engine.S1["weekday"]),
        "line1_holiday_trains": len(engine.S1["holiday"]),
        "metro_source": engine.OFF["meta"]["version"],
        "gyeongui_weekday_trains": len(engine.EXTRA["경의중앙선"]["trains"]["weekday"]),
        "gyeongui_holiday_trains": len(engine.EXTRA["경의중앙선"]["trains"]["holiday"]),
        "suin_weekday_trains": len(engine.EXTRA["수인분당선"]["trains"]["weekday"]),
        "suin_holiday_trains": len(engine.EXTRA["수인분당선"]["trains"]["holiday"]),
        "sinbundang_weekday_trains": len(engine.SINBUNDANG["trains"]["weekday"]),
        "sinbundang_weekend_trains": len(engine.SINBUNDANG["trains"]["holiday"]),
        "sinbundang_source_sha256": engine.SINBUNDANG.get("meta", {}).get("source_sha256", ""),
        "schedule_only_lines": sorted(engine.SCHEDULE_ONLY_LINES),
        "realtime_line_ids": dict(engine.LINE_IDS),
        "realtime_query_aliases": {k: list(v) for k, v in engine.REALTIME_QUERY_ALIASES.items()},
        "sinbundang_realtime_strategy": "realtimeStationArrival + Rail.Blue DIA 구간소요시간",
        "sinbundang_position_role": "diagnostic_only",
        "extra_lines": {
            line: {
                "weekday_trains": len(engine.EXTRA[line]["trains"]["weekday"]),
                "holiday_trains": len(engine.EXTRA[line]["trains"]["holiday"]),
            }
            for line in engine.EXTRA_LINES
        },
        "api_key_configured": bool(engine.API_KEY),
        "persistent_error_log_configured": bool(os.environ.get("DATABASE_URL", "").strip()),
        "timetable_integrity": TIMETABLE_INTEGRITY,
        "route_graph_version": engine.ROUTE_GRAPH.get("meta", {}).get("version"),
    }


@app.get("/api/stations")
def stations():
    return {"ok": True, "stations": engine.STATIONS_BY_LINE}


@app.post("/api/auto_route")
async def auto_route(request: Request):
    return await _run_engine_endpoint(request, "/api/auto_route", engine.calculate_auto_route)


@app.post("/api/route")
async def route(request: Request):
    return await _run_engine_endpoint(request, "/api/route", engine.calculate_route)


@app.post("/api/trip_update")
async def trip_update(request: Request):
    return await _run_engine_endpoint(request, "/api/trip_update", engine.calculate_live_trip)



def _debug_authorized(request: Request) -> bool:
    configured = os.environ.get("JIGEUMTA_DEBUG_TOKEN", "").strip()
    if not configured:
        return False
    provided = (request.headers.get("x-debug-token") or request.query_params.get("token") or "").strip()
    return bool(provided) and hmac.compare_digest(configured, provided)


@app.get("/api/debug/sinbundang_probe")
async def sinbundang_probe(request: Request):
    """
    신분당선 realtimePosition + realtimeStationArrival 단일 스냅샷.
    API quota 남용 방지를 위해 JIGEUMTA_DEBUG_TOKEN 설정 시에만 사용한다.
    """
    request_id = _request_id(request)
    if not os.environ.get("JIGEUMTA_DEBUG_TOKEN", "").strip():
        return _response({
            "ok": False,
            "error": "JIGEUMTA_DEBUG_TOKEN 환경변수를 먼저 설정하세요.",
            "request_id": request_id,
        }, 503, request_id)
    if not _debug_authorized(request):
        return _response({"ok": False, "error": "unauthorized", "request_id": request_id}, 401, request_id)
    raw = str(request.query_params.get("stations") or "강남,판교,정자")
    stations = [x.strip() for x in raw.split(",") if x.strip()][:8]
    try:
        snapshot = await run_in_threadpool(engine.sinbundang_probe_snapshot, stations, 5)
        await run_in_threadpool(
            observability.record_event,
            event_type="sinbundang_realtime_probe",
            level="info",
            endpoint="/api/debug/sinbundang_probe",
            request_id=request_id,
            status_code=200,
            message=f"신분당선 실시간 진단 snapshot: position {len(snapshot.get('position', {}).get('rows', []))}건",
            diagnostics=snapshot,
            context=_request_context(request),
        )
        snapshot = dict(snapshot)
        snapshot["request_id"] = request_id
        return _response(snapshot, 200, request_id)
    except Exception as e:
        error_id = await run_in_threadpool(
            observability.record_exception,
            endpoint="/api/debug/sinbundang_probe",
            request_id=request_id,
            payload={"stations": stations},
            exc=e,
            status_code=500,
            context=_request_context(request),
        )
        return _response({
            "ok": False, "error": f"{type(e).__name__}: {e}",
            "error_id": error_id, "request_id": request_id,
        }, 500, request_id)

@app.post("/api/client_log")
async def client_log(request: Request):
    request_id = _request_id(request)
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("client log payload must be an object")
        event_type = str(payload.get("event_type") or "client_error")[:100]
        level = str(payload.get("level") or "error").lower()
        if level not in {"error", "warning", "info"}:
            level = "error"
        message = str(payload.get("message") or "")[:4000]
        details = payload.get("details") if isinstance(payload.get("details"), dict) else {}
        event_id = await run_in_threadpool(
            observability.record_event,
            event_type=event_type,
            level=level,
            endpoint="/api/client_log",
            request_id=request_id,
            status_code=200,
            error_type=str(payload.get("error_type") or "ClientError")[:200],
            message=message,
            diagnostics=details,
            context={
                **_request_context(request),
                "page": str(payload.get("page") or "")[:1000],
                "session_id": str(payload.get("session_id") or "")[:100],
                "related_error_id": str(payload.get("related_error_id") or "")[:100],
            },
        )
        return _response({"ok": True, "event_id": event_id}, 200, request_id)
    except Exception as e:
        # 로그 수집 API 자체가 실패해도 프론트 동작에는 영향이 없어야 한다.
        return _response({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400, request_id)
