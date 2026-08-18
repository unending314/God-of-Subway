# -*- coding: utf-8 -*-
"""신분당선 서울시 API 연속 진단기.

사용 예:
  SEOUL_API_KEY=... python tools/probe_sinbundang_api.py --duration 600 --interval 20

realtimePosition과 강남/판교/정자 realtimeStationArrival을 같은 시각대에 반복 수집해
trainNo/btrainNo가 실제로 안정적인 추적 ID인지, 두 API 사이 번호가 일치하는지 분석한다.
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
    pos_hist = defaultdict(list)
    arr_hist = defaultdict(list)
    simultaneous_duplicate = []
    overlap_samples = 0
    for snap in snapshots:
        captured = snap.get("captured_at")
        seen = defaultdict(set)
        for row in snap.get("position", {}).get("rows", []):
            no = str(row.get("trainNo") or "")
            if not no:
                continue
            station = engine.canon_station(row.get("statnNm"))
            pos_hist[no].append((captured, station, str(row.get("trainSttus") or ""), str(row.get("updnLine") or "")))
            seen[no].add(station)
        for no, stations in seen.items():
            if len(stations) > 1:
                simultaneous_duplicate.append({"captured_at": captured, "train_no": no, "stations": sorted(stations)})
        for station, rows in snap.get("arrivals", {}).items():
            for row in rows:
                no = str(row.get("btrainNo") or "")
                if no:
                    arr_hist[no].append((captured, station, str(row.get("barvlDt") or ""), engine.canon_station(row.get("bstatnNm"))))
        if snap.get("number_overlap"):
            overlap_samples += 1

    moving = {}
    for no, rows in pos_hist.items():
        distinct_stations = [x[1] for x in rows if x[1]]
        if len(rows) >= 2:
            moving[no] = {
                "samples": len(rows),
                "distinct_stations": list(dict.fromkeys(distinct_stations)),
                "moved": len(set(distinct_stations)) >= 2,
                "first": rows[0],
                "last": rows[-1],
            }
    stable_moving = {k: v for k, v in moving.items() if v["moved"]}
    repeated_static = {k: v for k, v in moving.items() if not v["moved"]}

    if simultaneous_duplicate:
        verdict = "trainNo가 한 스냅샷에서 여러 열차 위치에 중복되어 물리 열차 추적 ID로 사용하기 위험함"
    elif stable_moving:
        verdict = "같은 trainNo가 시간에 따라 역을 이동하므로 최소한 단기 추적 ID로는 사용 가능성이 높음"
    elif moving:
        verdict = "trainNo가 반복되지만 관측시간 동안 역 이동이 없어 추가 샘플 필요"
    else:
        verdict = "동일 trainNo의 반복 관측이 부족해 의미를 판정할 수 없음"

    return {
        "snapshot_count": len(snapshots),
        "position_unique_train_numbers": sorted(pos_hist),
        "arrival_unique_train_numbers": sorted(arr_hist),
        "position_repeated_ids": moving,
        "position_stable_moving_ids": stable_moving,
        "position_repeated_static_ids": repeated_static,
        "simultaneous_duplicate_ids": simultaneous_duplicate,
        "samples_with_position_arrival_number_overlap": overlap_samples,
        "verdict": verdict,
        "important_note": "번호가 안정적이어도 Rail.Blue DX 운행열번과 동일하다는 뜻은 아니다.",
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=600, help="수집 시간(초), 기본 600")
    ap.add_argument("--interval", type=int, default=20, help="수집 간격(초), 기본 20")
    ap.add_argument("--stations", nargs="*", default=["강남", "판교", "정자"])
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
        snap = engine.sinbundang_probe_snapshot(args.stations, timeout=5)
        snapshots.append(snap)
        with jsonl.open("a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
        print(f"[{seq}] {snap['captured_at']} position={len(snap['position']['rows'])} arrivals={sum(len(v) for v in snap['arrivals'].values())} overlap={snap['number_overlap']}", flush=True)
        if time.monotonic() - started >= duration:
            break
        time.sleep(interval)
    report = analyze(snapshots)
    (out_dir / "analysis.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"saved: {out_dir}")


if __name__ == "__main__":
    main()
