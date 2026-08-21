# -*- coding: utf-8 -*-
"""지금타 V14.10 realtime collector.

사용자 요청과 분리해 서울시 realtimePosition을 노선 단위로 수집하고 Redis를 갱신한다.
2026-08-21 2호선 실측에서 원천 recptnDt가 대체로 약 20초 간격으로 갱신되었지만,
새 상태를 가능한 빨리 잡기 위해 기본 poll은 5초로 둔다.

한 번의 노선 호출로 해당 노선의 전체 열차가 반환되므로 사용자 수와 서울시 API 호출량은 분리된다.
"""
from __future__ import annotations

import argparse
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import engine
import realtime_store

POLL_SECONDS = max(1, int(os.environ.get("REALTIME_POLL_SECONDS", "5")))
CONCURRENCY = max(1, min(24, int(os.environ.get("REALTIME_WORKER_CONCURRENCY", "12"))))
FETCH_TIMEOUT = max(1, int(os.environ.get("REALTIME_FETCH_TIMEOUT", "4")))


def _source_observed_at(rows: list[dict]) -> datetime | None:
    values = []
    for row in rows:
        dt = engine.parse_dt(row.get("recptnDt") or row.get("lastRecptnDt"))
        if dt:
            values.append(dt)
    return max(values) if values else None


def _train_state_rows(line: str, rows: list[dict], fallback_observed_at: datetime) -> list[dict]:
    out = []
    for row in rows:
        raw_no = row.get("trainNo") or row.get("btrainNo")
        identity = engine.realtime_train_identity(line, raw_no)
        observed = engine.parse_dt(row.get("recptnDt") or row.get("lastRecptnDt")) or fallback_observed_at
        out.append({
            "api_train_no": str(raw_no or ""),
            "service_train_no": str(identity.get("service_train_no") or ""),
            "run_type": str(identity.get("run_type") or "unknown"),
            "station": engine.canon_station(row.get("statnNm")),
            "status": engine.status_name(row.get("trainSttus")),
            "direction": engine._api_direction_to_schedule(line, row.get("updnLine")) or str(row.get("updnLine") or ""),
            "source_observed_at": observed.strftime("%Y-%m-%d %H:%M:%S"),
        })
    return out


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
            "api_train_no": str(item.get("api_train_no") or item.get("raw_train_no") or ""),
            "run_type": str(item.get("run_type") or "scheduled"),
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
        source_at = _source_observed_at(rows)

        changed = realtime_store.put_snapshot(
            line_id=line_id,
            line_name=line,
            rows=rows,
            query=query,
            fetched_at=attempted,
            source_observed_at=source_at,
        )

        # 서울시 원천 snapshot이 실제로 바뀐 경우에만 train-state/delay를 다시 계산하고 저장한다.
        # 5초 polling 자체는 유지하되 Redis write와 CPU 사용을 줄인다.
        if changed:
            states = realtime_store.update_train_states(
                line_id=line_id,
                line_name=line,
                observations=_train_state_rows(line, rows, attempted),
                observed_at=attempted,
            )
            delays = _delay_rows(line, mode, rows)
            if delays:
                realtime_store.merge_delay_rows(line_id, delays)
        else:
            states = realtime_store.get_train_states(line_id)
            delays = []

        live_states = sum(1 for x in states.values() if x.get("presence_state") == "live")
        missing_states = sum(1 for x in states.values() if x.get("presence_state") == "missing_recent")
        special = sum(1 for x in states.values() if x.get("run_type") == "special" and x.get("presence_state") == "live")
        test = sum(1 for x in states.values() if x.get("run_type") == "test" and x.get("presence_state") == "live")
        return {
            "line": line, "ok": True, "rows": len(rows), "delay_rows": len(delays), "query": query,
            "changed": changed,
            "source_observed_at": source_at.strftime("%H:%M:%S") if source_at else "",
            "live_states": live_states, "missing_recent": missing_states,
            "special": special, "test": test,
        }
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
    if not engine.API_KEY:
        raise RuntimeError("realtime worker에는 SEOUL_API_KEY 환경변수가 필요합니다.")
    lines = selected_lines()
    mode, _ = engine.resolve_service_mode("AUTO", engine.now_kst())
    results = []
    with ThreadPoolExecutor(max_workers=min(CONCURRENCY, len(lines) or 1)) as pool:
        futures = {pool.submit(collect_line, line, mode): line for line in lines}
        for future in as_completed(futures):
            results.append(future.result())

    results.sort(key=lambda x: engine.LINE_NAMES.index(x["line"]) if x.get("line") in engine.LINE_NAMES else 999)
    ok_count = sum(1 for x in results if x.get("ok"))
    failed = [x for x in results if not x.get("ok")]
    row_count = sum(int(x.get("rows") or 0) for x in results if x.get("ok"))
    changed_count = sum(1 for x in results if x.get("ok") and x.get("changed"))
    missing = sum(int(x.get("missing_recent") or 0) for x in results if x.get("ok"))
    special = sum(int(x.get("special") or 0) for x in results if x.get("ok"))
    stamp = engine.now_kst().strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"[{stamp}] cycle ok={ok_count}/{len(results)} rows={row_count} changed={changed_count} "
        f"missing_recent={missing} special={special} failures={len(failed)}",
        flush=True,
    )
    for result in failed:
        print(f"  FAIL {result.get('line')}: {result.get('error')}", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="한 사이클만 수집하고 종료")
    parser.add_argument("--poll", type=int, default=None, help="환경변수 대신 polling 초를 임시 지정")
    args = parser.parse_args()
    poll_seconds = max(1, args.poll) if args.poll else POLL_SECONDS
    if args.once:
        collect_once()
        return
    print(
        f"JigeumTa realtime worker {engine.APP_VERSION} start: "
        f"lines={len(selected_lines())} poll={poll_seconds}s concurrency={CONCURRENCY}",
        flush=True,
    )
    while True:
        started = time.monotonic()
        collect_once()
        elapsed = time.monotonic() - started
        time.sleep(max(0.2, poll_seconds - elapsed))


if __name__ == "__main__":
    main()
