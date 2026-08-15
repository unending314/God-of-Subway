# -*- coding: utf-8 -*-
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

import engine

BASE = Path(__file__).resolve().parent

app = FastAPI(
    title="지금타",
    docs_url=None,
    redoc_url=None,
)

@app.get("/")
def home():
    return FileResponse(BASE / "index.html", media_type="text/html; charset=utf-8")

@app.get("/api/health")
def health():
    today_now = engine.now_kst()
    today_mode, today_reason = engine.resolve_service_mode("AUTO", today_now)
    today_holiday = engine.holiday_info(today_now)
    return {
        "ok": True,
        "version": "V9.3-vercel",
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
        "api_key_configured": bool(engine.API_KEY),
    }

@app.get("/api/stations")
def stations():
    return {"ok": True, "stations": engine.STATIONS_BY_LINE}

@app.post("/api/route")
async def route(request: Request):
    try:
        payload = await request.json()
        result = engine.calculate_route(payload)
        status = 200 if result.get("ok") else 422
        return JSONResponse(result, status_code=status)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )

@app.post("/api/trip_update")
async def trip_update(request: Request):
    try:
        payload = await request.json()
        result = engine.calculate_live_trip(payload)
        status = 200 if result.get("ok") else 422
        return JSONResponse(result, status_code=status)
    except Exception as e:
        return JSONResponse(
            {"ok": False, "error": f"{type(e).__name__}: {e}"},
            status_code=500,
        )
