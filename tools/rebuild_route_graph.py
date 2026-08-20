# -*- coding: utf-8 -*-
"""시간표 기반 외부노선(1호선·코레일계·신분당선) 정규화 결과를 route_graph.json에 반영한다.

2~9호선 그래프는 기존 값을 보존한다. 신분당선은 운영사 공식 역간 소요시간, 그 외 외부노선은 정규화 시간표로 재생성한다.
"""
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
import engine  # noqa: E402

REBUILD_LINES = {"1호선", *engine.EXTRA_LINES}


def build_mode(mode):
    best = {}
    for line in REBUILD_LINES:
        if line == "신분당선":
            for direction, mapping in engine.SINBUNDANG_RUNTIME.get("edge_seconds", {}).items():
                for pair, sec in mapping.items():
                    a, b = pair.split(">", 1)
                    best[(line, engine.canon_station(a), engine.canon_station(b))] = int(sec)
            continue
        for tr in engine.all_trains(line, mode):
            calls = [s for s in tr.get("stops", []) if s.get("call", True)]
            for a, b in zip(calls, calls[1:]):
                dep = engine.stop_board_sec(a)
                arr = engine.stop_alight_sec(b)
                if dep is None or arr is None:
                    continue
                while arr < dep:
                    arr += 86400
                key = (line, engine.canon_station(a.get("station")), engine.canon_station(b.get("station")))
                sec = int(arr - dep)
                if sec < 0:
                    continue
                if key not in best or sec < best[key]:
                    best[key] = sec
    return [[line, a, b, sec] for (line, a, b), sec in sorted(best.items())]


def main():
    path = BASE / "route_graph.json"
    old = json.loads(path.read_text(encoding="utf-8"))
    meta = dict(old.get("meta", {}))
    meta.update({
        "version": "V14.9.0",
        "weight": "minimum scheduled running time between consecutive callable passenger stops",
        "normalization": "Passenger-stop structural inference; Shinbundang edges use DX LINE official interstation runtime; GTX-A northern/southern segments remain disconnected",
    })
    modes = {}
    for mode in ("DAY", "SAT", "END"):
        preserved = [row for row in old.get("modes", {}).get(mode, []) if row[0] not in REBUILD_LINES]
        rebuilt = build_mode(mode)
        modes[mode] = sorted(preserved + rebuilt, key=lambda row: (row[0], row[1], row[2], row[3]))
    line1_overtakes = {
        "weekday": list(engine._detect_line1_overtake_events("DAY")),
        "holiday": list(engine._detect_line1_overtake_events("END")),
    }
    data = {"meta": meta, "modes": modes, "line1_overtakes": line1_overtakes}
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    for mode, rows in data["modes"].items():
        print(mode, len(rows))


if __name__ == "__main__":
    main()
