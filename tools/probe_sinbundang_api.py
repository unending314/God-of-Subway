# -*- coding: utf-8 -*-
"""신분당선 realtimePosition 차량 식별값 연속 진단기.

사용 예:
  SEOUL_API_KEY=... python tools/probe_sinbundang_api.py --duration 600 --interval 20

신분당선에서는 서울시 API의 trainNo를 공개 운행열번으로 해석하지 않고
'실시간 차량 식별값(vehicle_id)'으로만 추적한다. 같은 값이 시간에 따라 역을
이동하는지, 동시에 여러 위치에 중복되는지 확인한다.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import engine  # noqa: E402


def analyze(snapshots):
    hist = defaultdict(list)
    simultaneous_duplicate = []
    max_concurrent = 0
    for snap in snapshots:
        captured = snap.get("captured_at")
        rows = snap.get("position", {}).get("rows", [])
        max_concurrent = max(max_concurrent, len(rows))
        seen = defaultdict(set)
        for row in rows:
            vid = str(row.get("vehicle_id") or row.get("trainNo") or "")
            if not vid:
                continue
            station = engine.canon_station(row.get("statnNm"))
            hist[vid].append((captured, station, str(row.get("trainSttus") or ""), str(row.get("updnLine") or "")))
            seen[vid].add(station)
        for vid, stations in seen.items():
            if len(stations) > 1:
                simultaneous_duplicate.append({"captured_at": captured, "vehicle_id": vid, "stations": sorted(stations)})

    repeated = {}
    for vid, rows in hist.items():
        stations = [x[1] for x in rows if x[1]]
        if len(rows) >= 2:
            repeated[vid] = {
                "samples": len(rows),
                "distinct_stations": list(dict.fromkeys(stations)),
                "moved": len(set(stations)) >= 2,
                "first": rows[0],
                "last": rows[-1],
            }
    moving = {k: v for k, v in repeated.items() if v["moved"]}

    if simultaneous_duplicate:
        verdict = "동일 vehicle_id가 한 스냅샷에서 여러 위치에 중복됨: 고정 차량 추적키로 사용 금지"
    elif moving:
        verdict = "동일 vehicle_id가 시간에 따라 역을 이동함: 단기 차량 추적키로 사용 가능"
    elif repeated:
        verdict = "vehicle_id는 반복 관측됐지만 관측시간 동안 역 이동이 없어 추가 수집 필요"
    else:
        verdict = "반복 관측 표본이 부족함"

    return {
        "snapshot_count": len(snapshots),
        "unique_vehicle_ids": sorted(hist),
        "unique_vehicle_id_count": len(hist),
        "max_concurrent_rows": max_concurrent,
        "repeated_vehicle_ids": repeated,
        "moving_vehicle_ids": moving,
        "simultaneous_duplicate_vehicle_ids": simultaneous_duplicate,
        "verdict": verdict,
        "important_note": "vehicle_id는 공개 열차번호가 아니며 앱 화면에서도 차량번호로만 표시한다.",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=600, help="수집 시간(초), 기본 600")
    ap.add_argument("--interval", type=int, default=20, help="수집 간격(초), 기본 20")
    ap.add_argument("--output", default="")
    args = ap.parse_args()
    engine.require_api_key()
    duration = max(1, min(args.duration, 3600))
    interval = max(5, args.interval)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output) if args.output else ROOT / "probe_output" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = out_dir / "snapshots.jsonl"
    snapshots = []
    started = time.monotonic()
    seq = 0
    while True:
        seq += 1
        snap = engine.sinbundang_probe_snapshot(timeout=5)
        snapshots.append(snap)
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        print(f"[{seq}] {snap['captured_at']} vehicles={len(snap['position']['rows'])} ids={snap.get('vehicle_ids', [])}", flush=True)
        if time.monotonic() - started >= duration:
            break
        time.sleep(interval)
    report = analyze(snapshots)
    (out_dir / "analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"saved: {out_dir}")


if __name__ == "__main__":
    main()
