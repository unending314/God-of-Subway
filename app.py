# -*- coding: utf-8 -*-
"""지금타 V14.11.6 로컬 실행 서버.

비즈니스 로직은 engine.py 하나만 사용한다. 배포(server.py)와 로컬(app.py)이
서로 다른 ETA/급행 판정 코드를 갖지 않게 하여 회귀를 방지한다.
"""
import json
import os
import socket
import threading
import time
import urllib.parse
import uuid
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import engine
import observability
import realtime_store

BASE = Path(__file__).resolve().parent
VERSION = engine.APP_VERSION


def ensure_api_key():
    if engine.API_KEY:
        return
    print("=" * 70)
    print(f"지금타 {VERSION} — 로컬 실행")
    print("서울 열린데이터광장 인증키를 입력하세요.")
    print("키는 파일에 저장되지 않고 현재 프로세스 메모리에만 유지됩니다.")
    print("=" * 70)
    engine.API_KEY = input("인증키: ").strip()
    if not engine.API_KEY:
        raise SystemExit("SEOUL_API_KEY가 없어 실행을 종료합니다.")


def free_port(start=8765):
    for p in range(start, start + 50):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                pass
    raise RuntimeError("사용 가능한 로컬 포트를 찾지 못했습니다.")


def health_payload():
    now = engine.now_kst()
    mode, reason = engine.resolve_service_mode("AUTO", now)
    return {
        "ok": True,
        "version": VERSION,
        "today_service_mode": mode,
        "today_service_reason": reason,
        "today_is_holiday": bool(engine.holiday_info(now)),
        "line1_weekday_trains": len(engine.S1["weekday"]),
        "line1_holiday_trains": len(engine.S1["holiday"]),
        "metro_source": engine.OFF["meta"]["version"],
        "api_key_configured": bool(engine.API_KEY),
        "persistent_error_log_configured": bool(os.environ.get("DATABASE_URL", "").strip()),
    }


class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()

    def translate_path(self, path):
        rel = urllib.parse.urlparse(path).path.lstrip("/") or "index.html"
        return str(BASE / rel)

    def log_message(self, fmt, *args):
        print("[web]", fmt % args)

    def send_json(self, obj, status=200, request_id=""):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if request_id:
            self.send_header("X-Request-ID", request_id)
        self.end_headers()
        self.wfile.write(body)

    def _context(self):
        return {
            "user_agent": self.headers.get("User-Agent", "")[:500],
            "referer": self.headers.get("Referer", "")[:1000],
        }

    def _read_payload(self):
        n = int(self.headers.get("Content-Length", "0") or 0)
        return json.loads(self.rfile.read(n).decode("utf-8") or "{}")

    def _run_engine(self, endpoint, fn, payload, request_id):
        try:
            result = fn(payload)
            if not result.get("ok"):
                error_id = observability.record_failed_result(
                    endpoint=endpoint, request_id=request_id, payload=payload,
                    result=result, status_code=422, context=self._context(),
                )
                result = dict(result)
                result.update({"error_id": error_id, "request_id": request_id})
                return self.send_json(result, 422, request_id)
            observability.record_low_confidence(
                endpoint=endpoint, request_id=request_id, payload=payload,
                result=result, context=self._context(),
            )
            result = dict(result)
            result["request_id"] = request_id
            return self.send_json(result, 200, request_id)
        except ValueError as e:
            error_id = observability.record_exception(
                endpoint=endpoint, request_id=request_id, payload=payload,
                exc=e, status_code=422, context=self._context(),
            )
            return self.send_json({
                "ok": False, "error": f"{type(e).__name__}: {e}",
                "error_id": error_id, "request_id": request_id,
            }, 422, request_id)
        except Exception as e:
            error_id = observability.record_exception(
                endpoint=endpoint, request_id=request_id, payload=payload,
                exc=e, status_code=500, context=self._context(),
            )
            return self.send_json({
                "ok": False, "error": f"{type(e).__name__}: {e}",
                "error_id": error_id, "request_id": request_id,
            }, 500, request_id)

    def do_POST(self):
        endpoint = urllib.parse.urlparse(self.path).path
        request_id = self.headers.get("X-Request-ID") or str(uuid.uuid4())
        try:
            payload = self._read_payload()
        except Exception as e:
            return self.send_json({"ok": False, "error": f"잘못된 JSON: {e}"}, 400, request_id)

        if endpoint == "/api/auto_route":
            return self._run_engine(endpoint, engine.calculate_auto_route, payload, request_id)
        if endpoint == "/api/route":
            return self._run_engine(endpoint, engine.calculate_route, payload, request_id)
        if endpoint == "/api/trip_update":
            return self._run_engine(endpoint, engine.calculate_live_trip, payload, request_id)
        if endpoint == "/api/v1/auto-route":
            return self._run_engine(endpoint, engine.calculate_auto_route, payload, request_id)
        if endpoint == "/api/v1/route":
            return self._run_engine(endpoint, engine.calculate_route, payload, request_id)
        if endpoint == "/api/v1/trip-update":
            return self._run_engine(endpoint, engine.calculate_live_trip, payload, request_id)
        if endpoint == "/api/client_log":
            event_id = observability.record_event(
                event_type=str(payload.get("event_type") or "client_error")[:100],
                level=str(payload.get("level") or "error")[:20],
                endpoint=endpoint,
                request_id=request_id,
                status_code=200,
                error_type=str(payload.get("error_type") or "ClientError")[:200],
                message=str(payload.get("message") or "")[:4000],
                diagnostics=payload.get("details") if isinstance(payload.get("details"), dict) else {},
                context={
                    **self._context(),
                    "page": str(payload.get("page") or "")[:1000],
                    "session_id": str(payload.get("session_id") or "")[:100],
                    "related_error_id": str(payload.get("related_error_id") or "")[:100],
                },
            )
            return self.send_json({"ok": True, "event_id": event_id}, 200, request_id)
        return self.send_json({"ok": False, "error": f"알 수 없는 API 경로: {endpoint}"}, 404, request_id)

    def do_GET(self):
        endpoint = urllib.parse.urlparse(self.path).path
        if endpoint == "/api/health":
            return self.send_json(health_payload())
        if endpoint == "/api/stations":
            return self.send_json({"ok": True, "stations": engine.STATIONS_BY_LINE})
        if endpoint == "/api/v1/stations":
            return self.send_json({"ok": True, "stations": engine.STATIONS_BY_LINE})
        if endpoint == "/api/v1/realtime/status":
            return self.send_json({
                "ok": True,
                "store": realtime_store.status_for_lines(engine.REALTIME_STORE_IDS, now=engine.now_kst()),
            })
        if endpoint.startswith("/api/"):
            return self.send_json({"ok": False, "error": f"알 수 없는 API 경로: {endpoint}"}, 404)
        return super().do_GET()


if __name__ == "__main__":
    ensure_api_key()
    deploy_port = os.environ.get("PORT")
    if deploy_port:
        port = int(deploy_port)
        host = "0.0.0.0"
        browser_url = None
        shown_url = f"http://0.0.0.0:{port}/"
    else:
        port = free_port()
        host = "127.0.0.1"
        browser_url = f"http://127.0.0.1:{port}/?v=13.4.7-{int(time.time())}"
        shown_url = browser_url

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"\n실행 완료: {shown_url}")
    print(f"로그 파일: {BASE / 'logs' / 'error_events.jsonl'}")
    print("종료: Ctrl+C")
    if browser_url:
        threading.Timer(0.7, lambda: webbrowser.open(browser_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
