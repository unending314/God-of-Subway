# -*- coding: utf-8 -*-
"""지금타 V14 realtime collector.

사용자 요청과 분리해 서울시 realtimePosition을 주기적으로 수집하고 Redis를 갱신한다.
기본 주기는 60초. 서울 열린데이터광장 FAQ가 실시간 데이터를 분단위로 제공한다고 안내하므로
더 짧은 주기는 실제 recptnDt 갱신주기/API 정책을 관측한 뒤 조정한다.
"""
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import engine
import realtime_store

POLL_SECONDS = max(10, int(os.environ.get("REALTIME_POLL_SECONDS", "60")))
CONCURRENCY = max(1, min(8, int(os.environ.get("REALTIME_WORKER_CONCURRENCY", "4"))))
FETCH_TIMEOUT = max(1, int(os.environ.get("REALTIME_FETCH_TIMEOUT", "5")))


def _source_observed_at(rows: list[dict]) -> datetime | None:
    values = []
    for row in rows:
        dt = engine.parse_dt(row.get("recptnDt") or row.get("lastRecptnDt"))
        if dt:
            values.append(dt)
    return max(values) if values else None


def _delay_rows(line: str, mode: str, rows: list[dict]) -> list[dict]:
    if line == "신분당선":
        return []
    observations, _ = engine.observe_delays(line, mode, rows, client_cache=[])
    out = []
    for item in observations:
        if item.get("source_kind") != "live":
            continue
        observed = item.get("observed")
        out.append({
            "line": line,
            "train_no": str(item.get("train_no") or ""),
            "observed_at": observed.strftime("%Y-%m-%d %H:%M:%S") if observed else "",
            "delay_seconds": round(float(item.get("delay") or 0)),
            "current_station": str(item.get("current_station") or ""),
            "status": str(item.get("status") or ""),
        })
    return out


def collect_line(line: str, mode: str) -> dict:
    line_id = engine.REALTIME_STORE_IDS[line]
    attempted = engine.now_kst()
    try:
        ok, err, data = engine.fetch_position(line, timeout=FETCH_TIMEOUT)
        if not ok:
            message = (err or {}).get("message", err) if isinstance(err, dict) else err
            realtime_store.mark_fetch_failure(
                line_id=line_id, line_name=line, error=str(message or "실시간 위치 조회 실패"), attempted_at=attempted,
            )
            return {"line": line, "ok": False, "error": str(message or "실시간 위치 조회 실패")}

        rows = engine.position_rows(data or {}, line)
        query = data.get("_jigeumta_query", "") if isinstance(data, dict) else ""
        realtime_store.put_snapshot(
            line_id=line_id,
            line_name=line,
            rows=rows,
            query=query,
            fetched_at=attempted,
            source_observed_at=_source_observed_at(rows),
        )
        delays = _delay_rows(line, mode, rows)
        if delays:
            realtime_store.merge_delay_rows(line_id, delays)
        return {"line": line, "ok": True, "rows": len(rows), "delay_rows": len(delays), "query": query}
    except Exception as exc:
        try:
            realtime_store.mark_fetch_failure(
                line_id=line_id, line_name=line, error=f"{type(exc).__name__}: {exc}", attempted_at=attempted,
            )
        except Exception:
            pass
        return {"line": line, "ok": False, "error": f"{type(exc).__name__}: {exc}"}


def selected_lines() -> list[str]:
    raw = os.environ.get("REALTIME_LINES", "").strip()
    if raw:
        requested = [x.strip() for x in raw.split(",") if x.strip()]
        return [x for x in requested if x in engine.REALTIME_STORE_IDS and x not in engine.SCHEDULE_ONLY_LINES]
    return [x for x in engine.LINE_NAMES if x in engine.REALTIME_STORE_IDS and x not in engine.SCHEDULE_ONLY_LINES]


def collect_once() -> list[dict]:
    if not realtime_store.is_configured():
        raise RuntimeError("realtime worker에는 REDIS_URL과 redis 패키지가 필요합니다.")
    lines = selected_lines()
    mode, _ = engine.resolve_service_mode("AUTO", engine.now_kst())
    results = []
    with ThreadPoolExecutor(max_workers=min(CONCURRENCY, len(lines) or 1)) as pool:
        futures = {pool.submit(collect_line, line, mode): line for line in lines}
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            state = "OK" if result.get("ok") else "FAIL"
            print(f"[{engine.now_kst():%Y-%m-%d %H:%M:%S}] {state} {result}", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="한 사이클만 수집하고 종료")
    args = parser.parse_args()
    if args.once:
        collect_once()
        return
    while True:
        started = time.monotonic()
        collect_once()
        elapsed = time.monotonic() - started
        time.sleep(max(1, POLL_SECONDS - elapsed))


if __name__ == "__main__":
    main()
