# -*- coding: utf-8 -*-
"""
지금타 V10 — 1~9호선 다중 환승 ETA
핵심:
  1호선: 서울시 realtimePosition + 사용자가 제공한 코레일 공식 평/휴일 시간표
  2~9호선: 서울시 realtimePosition + 서울교통공사 공식 열차운행시각표(250930)
  모든 노선: 현재열차 trainNo -> 시간표 trainNo 매칭 -> 현재 지연 -> 향후 역 ETA
"""
import json, os, re, urllib.request, urllib.parse, time, statistics, heapq
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

BASE = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")

def now_kst():
    """
    앱 내부 계산은 기존 시간표/서울시 API처럼 timezone-naive 한국 현지시각을 사용한다.
    Vercel 서버는 UTC이므로 now_kst()를 직접 쓰면 9시간 차이가 난다.
    """
    return datetime.now(KST).replace(tzinfo=None)

API_KEY = os.environ.get("SEOUL_API_KEY", "").strip()

def require_api_key():
    if not API_KEY:
        raise RuntimeError("SEOUL_API_KEY 환경변수가 설정되지 않았습니다.")

def load_json(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))

S1 = {
    "weekday": load_json("schedule_weekday.json")["trains"],
    "holiday": load_json("schedule_holiday.json")["trains"],
}
S1_STATIONS = load_json("stations.json")
OFF = load_json("official_2to9_schedule.json")
EXTRA = load_json("korail_extra_lines_schedule.json")
KR_HOLIDAYS = load_json("kr_holidays_2026_2035.json")
ROUTE_GRAPH = load_json("route_graph.json")
EXTRA_LINES = ("경의중앙선", "수인분당선")

LINE_NAMES = [f"{i}호선" for i in range(1, 10)] + list(EXTRA_LINES)
LINE_NUM = {f"{i}호선": str(i) for i in range(1, 10)}
LINE_IDS = {f"{i}호선": f"100{i}" for i in range(1, 10)}
LINE_IDS.update({"경의중앙선": "1063", "수인분당선": "1075"})

STATION_ALIASES = {
    "서울": "서울역", "지하서울": "서울역",
    "성균관": "성균관대",
    "가산디": "가산디지털단지",
    "금천구": "금천구청",
    "동두중": "동두천중앙",
    "백마고": "백마고지",
    "쌍용나": "쌍용(나사렛대)",
    "온양온": "온양온천",
    "평지제": "평택지제",
    "종로5": "종로5가",
    "1종로": "종로3가",
    "1동대": "동대문",
    "1지청": "청량리",
    "총신대입구": "총신대입구(이수)",
    "이수": "총신대입구(이수)",
    # 경의중앙선 DIA 약칭
    "1양원": "양원",
    "1양정": "양정",
    "디엠시": "디지털미디어시티",
    "홍대입": "홍대입구",
    "효창공": "효창공원앞",
    "항공대": "한국항공대",
    # 수인분당선 DIA 약칭/구역명
    "강남구": "강남구청",
    "로데오": "압구정로데오",
    "남동인": "남동인더스파크",
    "소래포": "소래포구",
    "수원시": "수원시청",
    "매탄권": "매탄권선",
    "신길온": "능길",
    "신길온천": "능길",
    "인천논": "인천논현",
    "신인천": "인천",
    "신수원": "수원",
}

def canon_station(v):
    s = str(v or "").strip().replace(" ", "")
    if s.endswith("역") and s != "서울역":
        s = s[:-1]
    return STATION_ALIASES.get(s, s)

def norm_train(v):
    return re.sub(r"\s+", "", str(v or "")).upper()

def train_digits(v):
    return re.sub(r"\D", "", norm_train(v)).lstrip("0") or "0"

def parse_dt(v):
    if not v:
        return now_kst()
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y%m%d%H%M%S", "%H:%M:%S", "%H%M%S"):
        try:
            d = datetime.strptime(s, fmt)
            if fmt.startswith("%H"):
                n = now_kst()
                d = d.replace(year=n.year, month=n.month, day=n.day)
            return d
        except Exception:
            pass
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 14:
        try:
            return datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        except Exception:
            pass
    return now_kst()

def align_clock(actual_dt, sched_sec):
    actual = actual_dt.hour * 3600 + actual_dt.minute * 60 + actual_dt.second
    candidates = [actual - 86400, actual, actual + 86400, actual + 172800]
    return min(candidates, key=lambda x: abs(x - sched_sec))

def status_name(v):
    return {"0": "진입", "1": "도착", "2": "출발"}.get(str(v or ""), str(v or "") or "위치")

def scheduled_reference(stop, status):
    arr, dep = stop["arr"], stop["dep"]
    s = str(status or "")
    if s == "2" and dep is not None:
        return dep
    if s == "1" and arr is not None:
        return arr
    if s == "0" and arr is not None:
        return arr - 30
    return dep if dep is not None else arr

def clock_to_sec(v):
    p = str(v or "").split(":")
    if len(p) < 2:
        raise ValueError("시각은 HH:MM 형식이어야 합니다.")
    return int(p[0]) * 3600 + int(p[1]) * 60 + (int(p[2]) if len(p) > 2 else 0)

def clock_dt_near(v, ref=None):
    ref = ref or now_kst()
    sec = clock_to_sec(v)
    base = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(
        [base + timedelta(days=d, seconds=sec) for d in (-1, 0, 1)],
        key=lambda x: abs((x - ref).total_seconds())
    )

def schedule_dt_after(sched_sec, ready_dt, delay=0):
    """
    A timetable second may be 24:xx / 25:xx. Try nearby service-day midnights
    and return the earliest delayed occurrence that is still catchable.
    """
    midnight = ready_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    vals = []
    for d in (-2, -1, 0, 1):
        dt = midnight + timedelta(days=d, seconds=sched_sec + delay)
        if dt >= ready_dt - timedelta(seconds=5):
            vals.append(dt)
    return min(vals) if vals else None

def holiday_info(d):
    """번들된 대한민국 공휴일/대체공휴일 정보."""
    if isinstance(d, datetime):
        d = d.date()
    return KR_HOLIDAYS.get("dates", {}).get(d.isoformat())


def auto_service_mode(d):
    """
    운행일 AUTO:
    - 법정 공휴일/대체공휴일 -> END(일요일·공휴일)
    - 일반 토요일 -> SAT
    - 일반 일요일 -> END
    - 그 외 -> DAY
    """
    if isinstance(d, datetime):
        d = d.date()
    info = holiday_info(d)
    if info:
        return "END", info.get("name", "공휴일")
    wd = d.weekday()
    if wd == 5:
        return "SAT", "토요일"
    if wd == 6:
        return "END", "일요일"
    return "DAY", "평일"


def resolve_service_mode(mode, ref_dt=None):
    ref_dt = ref_dt or now_kst()
    if mode == "AUTO":
        resolved, reason = auto_service_mode(ref_dt)
        return resolved, reason
    labels = {"DAY": "평일 수동 선택", "SAT": "토요일 수동 선택", "END": "일요일·공휴일 수동 선택"}
    return mode, labels.get(mode, mode)


def choose_modes(mode):
    """
    mode은 calculate_route/calculate_live_trip 진입 시 AUTO가 실제 DAY/SAT/END로
    이미 해석되는 것이 원칙이다. 방어적으로 AUTO가 남아 있으면 오늘 기준 처리.
    """
    if mode == "AUTO":
        mode, _ = resolve_service_mode("AUTO", now_kst())
    if mode == "DAY":
        return "weekday", "DAY"
    if mode == "SAT":
        return "holiday", "SAT"
    return "holiday", "END"


# ---------- Normalize timetable structures into one common model ----------
def normalize_s1_train(tn, tr):
    return {
        "train_no": tn,
        "direction": tr.get("direction", ""),
        "service": tr.get("service", "local"),
        "start": canon_station(tr.get("start", "")),
        "dest": canon_station(tr.get("dest", "")),
        "stops": [{
            "station": canon_station(s.get("station")),
            "arr": s.get("arr"),
            "dep": s.get("dep"),
            "call": bool(s.get("call", True)),
        } for s in tr.get("stops", [])],
    }

def normalize_extra_train(tn, tr):
    return {
        "train_no": tn,
        "direction": tr.get("direction", ""),
        "service": tr.get("service", "local"),
        "start": canon_station(tr.get("start", "")),
        "dest": canon_station(tr.get("dest", "")),
        "linked_train_no": norm_train(tr.get("linked_train_no", "")),
        "stops": [{
            "station": canon_station(s.get("station")),
            "arr": s.get("arr"),
            "dep": s.get("dep"),
            "call": bool(s.get("call", True)),
        } for s in tr.get("stops", [])],
    }


def normalize_metro_train(tn, raw):
    direction, gub, start, dest, compact_stops = raw
    return {
        "train_no": tn,
        "direction": direction,
        "service": "express" if str(gub) == "1" else "local",
        "start": start,
        "dest": dest,
        "stops": [{
            "station": canon_station(s[0]),
            "arr": s[1],
            "dep": s[2],
            "call": True,
        } for s in compact_stops],
    }

# Train indexes: exact + digits fallback.
S1_NUM = {}
for day, trains in S1.items():
    x = defaultdict(list)
    for tn in trains:
        x[train_digits(tn)].append(tn)
    S1_NUM[day] = x

METRO_NUM = {}
for week, lines in OFF["days"].items():
    for line, trains in lines.items():
        x = defaultdict(list)
        for tn in trains:
            x[train_digits(tn)].append(tn)
        METRO_NUM[(week, line)] = x

EXTRA_NUM = {}
EXTRA_ALIAS = {}
EXTRA_ALIAS_NUM = {}
for line, source in EXTRA.items():
    for day, trains in source.get("trains", {}).items():
        nums = defaultdict(list)
        aliases = defaultdict(list)
        alias_nums = defaultdict(list)
        for tn, tr in trains.items():
            nums[train_digits(tn)].append(tn)
            linked = norm_train(tr.get("linked_train_no", ""))
            if linked:
                aliases[linked].append(tn)
                alias_nums[train_digits(linked)].append(tn)
        EXTRA_NUM[(line, day)] = nums
        EXTRA_ALIAS[(line, day)] = aliases
        EXTRA_ALIAS_NUM[(line, day)] = alias_nums


def get_train(line, mode, raw_train_no):
    korail_day, metro_week = choose_modes(mode)
    n = norm_train(raw_train_no)
    digits = train_digits(raw_train_no)

    if line == "1호선":
        trains = S1[korail_day]
        if n in trains:
            return normalize_s1_train(n, trains[n])
        c = S1_NUM[korail_day].get(digits, [])
        if len(c) == 1:
            return normalize_s1_train(c[0], trains[c[0]])
        return None

    if line in EXTRA_LINES:
        trains = EXTRA[line]["trains"][korail_day]

        # 1) actual train number in the timetable always wins.
        if n in trains:
            return normalize_extra_train(n, trains[n])

        # 2) digit-only fallback for API formatting differences.
        c = EXTRA_NUM.get((line, korail_day), {}).get(digits, [])
        if len(c) == 1:
            return normalize_extra_train(c[0], trains[c[0]])

        # 3) DIA '연계열번' fallback. Only used if no actual train number matched.
        c = EXTRA_ALIAS.get((line, korail_day), {}).get(n, [])
        if len(c) == 1:
            return normalize_extra_train(c[0], trains[c[0]])
        c = EXTRA_ALIAS_NUM.get((line, korail_day), {}).get(digits, [])
        if len(c) == 1:
            return normalize_extra_train(c[0], trains[c[0]])
        return None

    num = LINE_NUM[line]
    trains = OFF["days"].get(metro_week, {}).get(num, {})
    if n in trains:
        return normalize_metro_train(n, trains[n])
    c = METRO_NUM.get((metro_week, num), {}).get(digits, [])
    if len(c) == 1:
        return normalize_metro_train(c[0], trains[c[0]])
    return None


def all_trains(line, mode):
    korail_day, metro_week = choose_modes(mode)
    if line == "1호선":
        return [normalize_s1_train(tn, tr) for tn, tr in S1[korail_day].items()]
    if line in EXTRA_LINES:
        return [
            normalize_extra_train(tn, tr)
            for tn, tr in EXTRA[line]["trains"][korail_day].items()
        ]
    num = LINE_NUM[line]
    return [
        normalize_metro_train(tn, raw)
        for tn, raw in OFF["days"].get(metro_week, {}).get(num, {}).items()
    ]


def station_options():
    s1 = sorted({canon_station(x) for x in S1_STATIONS})
    out = {"1호선": s1}
    for i in range(2, 10):
        num = str(i)
        out[f"{i}호선"] = sorted({canon_station(x) for x in OFF["stations"].get(num, [])})
    for line in EXTRA_LINES:
        out[line] = sorted({canon_station(x) for x in EXTRA[line].get("stations", [])})
    return out


STATIONS_BY_LINE = station_options()


# ---------- Automatic route search ----------
TRANSFER_EXCLUDE = set(ROUTE_GRAPH.get("meta", {}).get("excluded_same_name_transfer_stations", []))
DEFAULT_TRANSFER_SECONDS = int(ROUTE_GRAPH.get("meta", {}).get("default_transfer_seconds", 240))
_ROUTE_ADJ_CACHE = {}
_SEGMENT_SERVE_CACHE = {}

def _route_node(line, station):
    return (line, canon_station(station))

def _route_adjacency(mode):
    if mode in _ROUTE_ADJ_CACHE:
        return _ROUTE_ADJ_CACHE[mode]

    adj = defaultdict(list)

    # Timetable-derived directional ride edges.
    for line, a, b, sec in ROUTE_GRAPH.get("modes", {}).get(mode, []):
        u = _route_node(line, a)
        v = _route_node(line, b)
        adj[u].append((v, int(sec), "ride"))

    # A same-named station on multiple supported lines is normally a transfer.
    # 양평(5호선 / 경의중앙선) is a known different-location duplicate.
    station_lines = defaultdict(list)
    for line, names in STATIONS_BY_LINE.items():
        for st in names:
            station_lines[canon_station(st)].append(line)

    for st, lines in station_lines.items():
        if st in TRANSFER_EXCLUDE or len(lines) < 2:
            continue
        uniq = sorted(set(lines))
        for a in uniq:
            for b in uniq:
                if a != b:
                    adj[(a, st)].append(((b, st), DEFAULT_TRANSFER_SECONDS, "transfer"))

    _ROUTE_ADJ_CACHE[mode] = adj
    return adj

def _station_lines(station):
    c = canon_station(station)
    return [
        line for line, names in STATIONS_BY_LINE.items()
        if c in {canon_station(x) for x in names}
    ]

def _segment_has_train(line, mode, start, end):
    key = (line, mode, canon_station(start), canon_station(end))
    if key in _SEGMENT_SERVE_CACHE:
        return _SEGMENT_SERVE_CACHE[key]
    ok = False
    for tr in all_trains(line, mode):
        if route_pair(tr["stops"], start, end):
            ok = True
            break
    _SEGMENT_SERVE_CACHE[key] = ok
    return ok

def auto_find_path(start, end, mode, transfer_seconds=None):
    """
    Supported timetable network에서 Dijkstra 최소시간 경로 탐색.
    비용 = 공식 시간표 기반 차내 최소 주행시간 + 환승 기본시간.
    """
    start = canon_station(start)
    end = canon_station(end)
    if not start or not end:
        raise ValueError("출발역과 도착역을 입력하세요.")
    if start == end:
        raise ValueError("출발역과 도착역이 같습니다.")

    start_lines = _station_lines(start)
    end_lines = set(_station_lines(end))
    if not start_lines:
        raise ValueError(f"지원 노선에서 출발역 '{start}'을 찾지 못했습니다.")
    if not end_lines:
        raise ValueError(f"지원 노선에서 도착역 '{end}'을 찾지 못했습니다.")

    adj = _route_adjacency(mode)
    transfer_seconds = int(transfer_seconds or DEFAULT_TRANSFER_SECONDS)

    # If user changes default transfer penalty, adjust transfer edges at traversal time.
    dist = {}
    prev = {}
    pq = []
    starts = set()
    for line in start_lines:
        node = (line, start)
        starts.add(node)
        dist[node] = 0
        heapq.heappush(pq, (0, node))

    target = None
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist.get(u):
            continue
        if u[1] == end and u[0] in end_lines:
            target = u
            break
        for v, base_w, kind in adj.get(u, []):
            w = transfer_seconds if kind == "transfer" else base_w
            nd = d + w
            if nd < dist.get(v, 10**18):
                dist[v] = nd
                prev[v] = (u, kind, w)
                heapq.heappush(pq, (nd, v))

    if target is None:
        raise ValueError(f"{start} → {end} 경로를 찾지 못했습니다.")

    edges = []
    cur = target
    while cur not in starts:
        pu, kind, w = prev[cur]
        edges.append((pu, cur, kind, w))
        cur = pu
    edges.reverse()

    return {
        "start": start,
        "end": end,
        "seconds": int(dist[target]),
        "edges": edges,
    }

def auto_path_to_segments(path, mode, transfer_minutes=4):
    """
    station-line path를 기존 지금타 segment 포맷으로 압축.
    같은 노선이라도 하나의 실제 열차가 start→end를 운행하지 못하면
    같은 역에서 별도 구간으로 분리한다.
    """
    edges = path["edges"]
    segments = []
    current = None

    def finish_current(walk=None):
        nonlocal current
        if current:
            if walk is not None:
                current["transfer_walk"] = float(walk)
            segments.append(current)
            current = None

    for u, v, kind, w in edges:
        if kind == "transfer":
            finish_current(transfer_minutes)
            continue

        line = u[0]
        fr = u[1]
        to = v[1]

        if current is None:
            current = {
                "line": line,
                "from": fr,
                "to": to,
                "transfer_walk": 0,
            }
            continue

        if current["line"] == line:
            # Only merge if at least one actual timetable train can cover
            # the full accumulated segment.
            if _segment_has_train(line, mode, current["from"], to):
                current["to"] = to
            else:
                # Same-line train change/branch junction.
                finish_current(1)
                current = {
                    "line": line,
                    "from": fr,
                    "to": to,
                    "transfer_walk": 0,
                }
        else:
            finish_current(transfer_minutes)
            current = {
                "line": line,
                "from": fr,
                "to": to,
                "transfer_walk": 0,
            }

    finish_current(0)

    if not segments:
        raise ValueError("자동 경로를 구간으로 변환하지 못했습니다.")
    if len(segments) > 8:
        raise ValueError(f"자동 경로가 {len(segments)}개 구간이라 현재 최대 8구간 제한을 초과합니다.")

    return segments

def calculate_auto_route(payload):
    now = now_kst()
    start_text = str(payload.get("start_time") or now.strftime("%H:%M"))
    start_dt = clock_dt_near(start_text, now)
    if start_dt < now - timedelta(hours=8):
        start_dt += timedelta(days=1)

    requested_mode = str(payload.get("day") or "AUTO")
    mode, mode_reason = resolve_service_mode(requested_mode, start_dt)

    start = canon_station(payload.get("from"))
    end = canon_station(payload.get("to"))
    transfer_minutes = float(payload.get("transfer_minutes") or 4)
    transfer_seconds = max(0, round(transfer_minutes * 60))

    path = auto_find_path(start, end, mode, transfer_seconds)
    segments = auto_path_to_segments(path, mode, transfer_minutes)

    # Human-readable interchange list.
    interchanges = []
    for i in range(len(segments) - 1):
        if segments[i]["to"] == segments[i+1]["from"]:
            interchanges.append(segments[i]["to"])

    return {
        "ok": True,
        "service_mode": mode,
        "service_mode_reason": mode_reason,
        "from": start,
        "to": end,
        "route_seconds": path["seconds"],
        "transfer_count": max(0, len(segments) - 1),
        "interchanges": interchanges,
        "segments": segments,
    }


# ---------- Live API ----------
def fetch_position(line):
    require_api_key()
    q = urllib.parse.quote(line, safe="")
    url = f"http://swopenAPI.seoul.go.kr/api/subway/{API_KEY}/json/realtimePosition/0/300/{q}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "JigeumTa-V10/1.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            raw = r.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("RESULT"), dict):
            return False, data["RESULT"], data
        return True, None, data
    except Exception as e:
        return False, {"message": f"{type(e).__name__}: {e}"}, None

def position_rows(data):
    return data.get("realtimePositionList", []) if isinstance(data, dict) else []

# ---------- Schedule path helpers ----------
def indices(stops, station):
    c = canon_station(station)
    return [i for i, s in enumerate(stops) if canon_station(s["station"]) == c]

def route_pair(stops, start, end, min_start_idx=0):
    starts = [i for i in indices(stops, start) if i >= min_start_idx]
    ends = indices(stops, end)
    pairs = [(i, j) for i in starts for j in ends if j > i]
    return min(pairs, key=lambda p: p[1] - p[0]) if pairs else None

def first_current_index(stops, current, before_or_at=None):
    inds = indices(stops, current)
    if before_or_at is not None:
        valid = [i for i in inds if i <= before_or_at]
        if valid:
            return max(valid)
    return inds[0] if inds else None

def stop_board_sec(stop):
    return stop["dep"] if stop["dep"] is not None else stop["arr"]

def stop_alight_sec(stop):
    return stop["arr"] if stop["arr"] is not None else stop["dep"]

# ---------- Delay observations ----------
def observe_delays(line, mode, positions):
    now = now_kst()
    obs = []
    unmatched_train = []
    unmatched_station = []
    for p in positions:
        raw_tn = p.get("trainNo") or p.get("btrainNo")
        tr = get_train(line, mode, raw_tn)
        if not tr:
            if raw_tn and len(unmatched_train) < 10:
                unmatched_train.append(str(raw_tn))
            continue
        cur = canon_station(p.get("statnNm"))
        ci = first_current_index(tr["stops"], cur)
        if ci is None:
            if cur and len(unmatched_station) < 10:
                unmatched_station.append(cur)
            continue
        ref = scheduled_reference(tr["stops"][ci], p.get("trainSttus"))
        if ref is None:
            continue
        observed = parse_dt(p.get("recptnDt") or p.get("lastRecptnDt"))
        delay = align_clock(observed, ref) - ref
        obs.append({
            "train_no": tr["train_no"],
            "direction": tr["direction"],
            "service": tr["service"],
            "delay": delay,
            "current_station": cur,
            "status": status_name(p.get("trainSttus")),
            "observed": observed,
            "ref": ref,
            "train": tr,
            "raw": p,
        })
    return obs, {
        "positions": len(positions),
        "matched": len(obs),
        "unmatched_train": unmatched_train,
        "unmatched_station": unmatched_station,
    }

def median_delay(observations, direction=None, service=None):
    vals = []
    for o in observations:
        if direction and o["direction"] != direction:
            continue
        if service and o["service"] != service:
            continue
        # Ignore clearly broken outliers (> 45 min) in MVP smoothing.
        if abs(o["delay"]) <= 2700:
            vals.append(o["delay"])
    if not vals and (direction or service):
        return median_delay(observations)
    return statistics.median(vals) if vals else 0

# ---------- Segment ETA ----------
def direct_live_candidates(line, mode, start, end, ready_dt, observations):
    now = now_kst()
    out = []
    for o in observations:
        tr = o["train"]
        stops = tr["stops"]
        # Find a route start after the currently observed position.
        ci = first_current_index(stops, o["current_station"])
        if ci is None:
            continue
        pair = route_pair(stops, start, end, min_start_idx=ci)
        if not pair:
            continue
        si, ei = pair
        # If the current train is already beyond the boarding station, reject it.
        if ci > si:
            continue

        ref = scheduled_reference(stops[ci], o["raw"].get("trainSttus"))
        bsec = stop_board_sec(stops[si])
        asec = stop_alight_sec(stops[ei])
        if None in (ref, bsec, asec):
            continue
        while bsec < ref:
            bsec += 86400
        while asec < bsec:
            asec += 86400

        observed = o["observed"]
        age = max(0, (now - observed).total_seconds())
        board_dt = now + timedelta(seconds=(bsec - ref - age))
        alight_dt = now + timedelta(seconds=(asec - ref - age))

        if board_dt < ready_dt - timedelta(seconds=5):
            continue

        out.append({
            "line": line,
            "from": canon_station(start),
            "to": canon_station(end),
            "train_no": tr["train_no"],
            "service": tr["service"],
            "direction": tr["direction"],
            "origin": tr["start"],
            "destination": tr["dest"],
            "board_dt": board_dt,
            "alight_dt": alight_dt,
            "wait_seconds": round((board_dt - ready_dt).total_seconds()),
            "ride_seconds": round((alight_dt - board_dt).total_seconds()),
            "delay_seconds": round(o["delay"]),
            "current_station": o["current_station"],
            "confidence": "높음",
            "method": "실시간 열차 위치 + 열차별 공식 시간표",
            "projected": False,
        })
    out.sort(key=lambda x: (x["alight_dt"], x["board_dt"]))
    return out

def static_projected_candidates(line, mode, start, end, ready_dt, observations):
    out = []
    for tr in all_trains(line, mode):
        pair = route_pair(tr["stops"], start, end)
        if not pair:
            continue
        si, ei = pair
        bsec = stop_board_sec(tr["stops"][si])
        asec = stop_alight_sec(tr["stops"][ei])
        if bsec is None or asec is None:
            continue
        while asec < bsec:
            asec += 86400

        delay = median_delay(observations, tr["direction"], tr["service"])
        board_dt = schedule_dt_after(bsec, ready_dt, delay)
        if not board_dt:
            continue
        alight_dt = board_dt + timedelta(seconds=asec - bsec)
        if (board_dt - ready_dt).total_seconds() > 4 * 3600:
            continue

        out.append({
            "line": line,
            "from": canon_station(start),
            "to": canon_station(end),
            "train_no": tr["train_no"],
            "service": tr["service"],
            "direction": tr["direction"],
            "origin": tr["start"],
            "destination": tr["dest"],
            "board_dt": board_dt,
            "alight_dt": alight_dt,
            "wait_seconds": round((board_dt - ready_dt).total_seconds()),
            "ride_seconds": round((alight_dt - board_dt).total_seconds()),
            "delay_seconds": round(delay),
            "current_station": "",
            "confidence": "중간" if observations else "낮음",
            "method": "공식 시간표 + 현재 동일방향 지연 중앙값" if observations else "공식 시간표만 사용",
            "projected": True,
        })
    out.sort(key=lambda x: (x["alight_dt"], x["board_dt"]))
    return out[:30]

def public_candidate(c, selected=False):
    return {
        "train_no": c.get("train_no", ""),
        "service": c.get("service", "local"),
        "direction": c.get("direction", ""),
        "origin": c.get("origin", ""),
        "destination": c.get("destination", ""),
        "board_dt": c["board_dt"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(c.get("board_dt"), datetime) else c.get("board_dt"),
        "alight_dt": c["alight_dt"].strftime("%Y-%m-%d %H:%M:%S") if isinstance(c.get("alight_dt"), datetime) else c.get("alight_dt"),
        "wait_seconds": c.get("wait_seconds", 0),
        "ride_seconds": c.get("ride_seconds", 0),
        "delay_seconds": c.get("delay_seconds", 0),
        "current_station": c.get("current_station", ""),
        "confidence": c.get("confidence", "중간"),
        "method": c.get("method", ""),
        "projected": bool(c.get("projected")),
        "live_detected": not bool(c.get("projected")),
        "selected": bool(selected),
    }


def calculate_segment(line, mode, start, end, ready_dt, position_cache):
    """
    V8 hybrid candidate model:
    - realtimePosition에 잡힌 실제 열차
    - 해당 시간대 공식 시간표 후보 열차
    를 항상 함께 구성한다.
    """
    if line not in LINE_NAMES:
        return {"ok": False, "error": f"지원하지 않는 노선: {line}"}
    if canon_station(start) not in {canon_station(x) for x in STATIONS_BY_LINE[line]}:
        return {"ok": False, "error": f"{line} 시간표에서 승차역 '{start}'을 찾지 못했습니다."}
    if canon_station(end) not in {canon_station(x) for x in STATIONS_BY_LINE[line]}:
        return {"ok": False, "error": f"{line} 시간표에서 하차역 '{end}'을 찾지 못했습니다."}

    if line not in position_cache:
        ok, err, data = fetch_position(line)
        if not ok:
            return {"ok": False, "error": f"{line} 실시간 위치 조회 실패: {(err or {}).get('message', err)}"}
        position_cache[line] = position_rows(data)

    positions = position_cache[line]
    observations, diag = observe_delays(line, mode, positions)

    # 현재 API에 실제로 잡힌 열차.
    direct = direct_live_candidates(line, mode, start, end, ready_dt, observations)

    # 실시간에 아직 안 잡힌 열차까지 포함한 공식 시간표 후보.
    projected = static_projected_candidates(line, mode, start, end, ready_dt, observations)

    # 같은 열차번호가 양쪽에 있으면 실시간 관측값이 우선.
    merged = {}
    for c in projected:
        merged[str(c.get("train_no"))] = c
    for c in direct:
        merged[str(c.get("train_no"))] = c

    candidates = list(merged.values())

    # 승차 준비시각 기준 한 시간 안쪽의 후보를 우선 표시.
    near = [
        c for c in candidates
        if -5 <= (c["board_dt"] - ready_dt).total_seconds() <= 3600
    ]
    if not near:
        near = candidates

    # 목적지 도착이 빠른 후보를 기본 선택.
    near.sort(key=lambda x: (x["alight_dt"], x["board_dt"]))
    if not near:
        return {
            "ok": False,
            "error": f"{line} {start}→{end} 운행 열차를 현재 시간표에서 찾지 못했습니다.",
            "diagnostics": diag,
        }

    chosen = near[0]
    public = [
        public_candidate(
            c,
            selected=(str(c.get("train_no")) == str(chosen.get("train_no")))
        )
        for c in near[:6]
    ]
    return {
        "ok": True,
        "chosen": chosen,
        "candidates": near[:10],
        "public_candidates": public,
        "diagnostics": diag,
    }

def tracked_train_segment(line, mode, start, end, train_no, position_cache, boarded_at=None):
    """
    사용자가 실제 탑승했다고 표시한 열차를 잠금 추적한다.
    다른 후보 열차로 절대 교체하지 않고 지정 train_no만 따라간다.
    """
    now = now_kst()
    tr = get_train(line, mode, train_no)
    if not tr:
        return {
            "ok": False,
            "error": f"{line} 공식 시간표에서 탑승 열차 {train_no}을 찾지 못했습니다."
        }

    pair = route_pair(tr["stops"], start, end)
    if not pair:
        return {
            "ok": False,
            "error": f"{train_no}열차 시간표에서 {start}→{end} 운행 구간을 찾지 못했습니다."
        }
    si, ei = pair
    target_stop = tr["stops"][ei]
    target_sec = stop_alight_sec(target_stop)
    if target_sec is None:
        return {"ok": False, "error": f"{end} 도착시각이 시간표에 없습니다."}

    if line not in position_cache:
        ok, err, data = fetch_position(line)
        if not ok:
            return {"ok": False, "error": f"{line} 실시간 위치 조회 실패: {(err or {}).get('message', err)}"}
        position_cache[line] = position_rows(data)

    positions = position_cache[line]
    observations, diag = observe_delays(line, mode, positions)
    wanted = norm_train(tr["train_no"])

    # get_train()을 거쳐 정규화된 공식 열차번호가 같은 관측값만 고정 추적.
    live = next((o for o in observations if norm_train(o["train_no"]) == wanted), None)

    board_dt = None
    if boarded_at:
        try:
            board_dt = datetime.strptime(boarded_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            board_dt = None

    if live:
        stops = tr["stops"]
        ci = first_current_index(stops, live["current_station"], before_or_at=ei)
        if ci is None:
            ci = first_current_index(stops, live["current_station"])

        # 이미 목적지보다 뒤로 간 것으로 잡히면 구간 완료 처리.
        if ci is not None and ci > ei:
            return {
                "ok": True,
                "arrived": True,
                "chosen": {
                    "line": line, "from": canon_station(start), "to": canon_station(end),
                    "train_no": tr["train_no"], "service": tr["service"],
                    "direction": tr["direction"], "origin": tr["start"], "destination": tr["dest"],
                    "board_dt": board_dt or now, "alight_dt": now,
                    "wait_seconds": 0, "ride_seconds": 0,
                    "remaining_seconds": 0, "delay_seconds": round(live["delay"]),
                    "current_station": live["current_station"],
                    "confidence": "높음", "method": "탑승 열차 실시간 고정 추적",
                    "projected": False, "tracking": True, "arrived": True,
                    "status": live["status"],
                },
                "diagnostics": diag,
            }

        if ci is not None:
            ref = scheduled_reference(stops[ci], live["raw"].get("trainSttus"))
            asec = target_sec
            if ref is not None:
                while asec < ref:
                    asec += 86400
                age = max(0, (now - live["observed"]).total_seconds())
                alight_dt = now + timedelta(seconds=max(0, asec - ref - age))

                # 최초 탑승시각이 없으면, 현재 구간에서는 지금 이전으로만 표시.
                shown_board = board_dt or now
                return {
                    "ok": True,
                    "arrived": False,
                    "chosen": {
                        "line": line, "from": canon_station(start), "to": canon_station(end),
                        "train_no": tr["train_no"], "service": tr["service"],
                        "direction": tr["direction"], "origin": tr["start"], "destination": tr["dest"],
                        "board_dt": shown_board, "alight_dt": alight_dt,
                        "wait_seconds": 0,
                        "ride_seconds": max(0, round((alight_dt - shown_board).total_seconds())),
                        "remaining_seconds": max(0, round((alight_dt - now).total_seconds())),
                        "delay_seconds": round(live["delay"]),
                        "current_station": live["current_station"],
                        "confidence": "높음",
                        "method": "탑승 열차 실시간 고정 추적",
                        "projected": False, "tracking": True, "arrived": False,
                        "status": live["status"],
                        "data_age_seconds": max(0, round((now - live["observed"]).total_seconds())),
                    },
                    "diagnostics": diag,
                }

    # API에서 순간적으로 열차가 사라져도 잠금을 해제하지 않는다.
    # 공식 시간표 + 현재 같은 방향/등급 열차 지연 중앙값으로 임시 유지.
    delay = median_delay(observations, tr["direction"], tr["service"])
    target_dt = schedule_dt_after(target_sec, now, delay)
    if not target_dt:
        # 심야 24/25시 운행축을 고려한 마지막 fallback
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        possibilities = [
            midnight + timedelta(days=d, seconds=target_sec + delay)
            for d in (-1, 0, 1, 2)
        ]
        future = [x for x in possibilities if x >= now - timedelta(minutes=5)]
        target_dt = min(future) if future else now

    return {
        "ok": True,
        "arrived": False,
        "chosen": {
            "line": line, "from": canon_station(start), "to": canon_station(end),
            "train_no": tr["train_no"], "service": tr["service"],
            "direction": tr["direction"], "origin": tr["start"], "destination": tr["dest"],
            "board_dt": board_dt or now, "alight_dt": target_dt,
            "wait_seconds": 0,
            "ride_seconds": max(0, round((target_dt - (board_dt or now)).total_seconds())),
            "remaining_seconds": max(0, round((target_dt - now).total_seconds())),
            "delay_seconds": round(delay),
            "current_station": "",
            "confidence": "중간",
            "method": "탑승 열차 잠금 · 실시간 위치 재포착 대기",
            "projected": True, "tracking": True, "arrived": False,
            "status": "위치 재포착 대기",
        },
        "diagnostics": diag,
    }


def calculate_live_trip(payload):
    """
    active_index 구간의 boarded_train_no를 고정 추적하고,
    그 열차의 최신 환승역 ETA를 seed로 이후 모든 구간을 매번 재계산한다.
    """
    segments = payload.get("segments") or []
    if not 1 <= len(segments) <= 8:
        raise ValueError("구간은 1~8개로 입력하세요.")

    active_index = int(payload.get("active_index", 0))
    if active_index < 0 or active_index >= len(segments):
        raise ValueError("추적 중인 구간 번호가 올바르지 않습니다.")

    boarded_train_no = str(payload.get("boarded_train_no") or "").strip()
    if not boarded_train_no:
        raise ValueError("탑승한 열차번호가 없습니다.")

    requested_mode = str(payload.get("day") or "AUTO")
    boarded_at = payload.get("boarded_at")
    mode_ref = now_kst()
    if boarded_at:
        try:
            mode_ref = datetime.strptime(boarded_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    mode, mode_reason = resolve_service_mode(requested_mode, mode_ref)
    position_cache = {}
    results = []
    warnings = []

    # 1) 현재 실제 탑승 중인 열차는 무조건 이 번호로 고정.
    active = segments[active_index]
    line = str(active.get("line") or "").strip()
    fr = canon_station(active.get("from"))
    to = canon_station(active.get("to"))

    tracked = tracked_train_segment(
        line, mode, fr, to, boarded_train_no, position_cache, boarded_at
    )
    if not tracked.get("ok"):
        return {
            "ok": False, "failed_segment": active_index + 1,
            "error": tracked.get("error", "탑승 열차 추적 실패"),
            "diagnostics": tracked.get("diagnostics", {}),
        }

    current = tracked["chosen"]
    current["segment_index"] = active_index + 1
    current["diagnostics"] = tracked.get("diagnostics", {})
    current["nearby_candidates"] = [public_candidate(current, selected=True)]
    results.append(current)

    # 2) 현재 열차의 최신 도착 ETA + 환승시간을 다음 구간 ready 시각으로 사용.
    ready_dt = current["alight_dt"]
    if active_index < len(segments) - 1:
        ready_dt += timedelta(minutes=max(0, float(active.get("transfer_walk") or 0)))

    # 3) 이후 모든 구간을 지금 시점에서 다시 탐색.
    for idx in range(active_index + 1, len(segments)):
        s = segments[idx]
        line = str(s.get("line") or "").strip()
        fr = canon_station(s.get("from"))
        to = canon_station(s.get("to"))

        r = calculate_segment(line, mode, fr, to, ready_dt, position_cache)
        if not r.get("ok"):
            return {
                "ok": False,
                "failed_segment": idx + 1,
                "error": r.get("error", "후속 구간 계산 실패"),
                "diagnostics": r.get("diagnostics", {}),
                "segments": [serialize_seg(x) for x in results],
            }

        chosen = r["chosen"]
        chosen["segment_index"] = idx + 1
        chosen["ready_dt"] = ready_dt
        chosen["diagnostics"] = r.get("diagnostics", {})
        chosen["nearby_candidates"] = r.get("public_candidates", [])
        results.append(chosen)

        if chosen["confidence"] != "높음":
            warnings.append(
                f"{idx+1}구간 {line} {fr}→{to}: {chosen['method']} ({chosen['confidence']} 신뢰도)"
            )

        if idx < len(segments) - 1:
            ready_dt = chosen["alight_dt"] + timedelta(
                minutes=max(0, float(s.get("transfer_walk") or 0))
            )
        else:
            ready_dt = chosen["alight_dt"]

    now = now_kst()
    final_dt = results[-1]["alight_dt"]
    return {
        "ok": True,
        "live_tracking": True,
        "service_mode": mode,
        "service_mode_reason": mode_reason,
        "active_index": active_index,
        "boarded_train_no": boarded_train_no,
        "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "arrival_time": final_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "remaining_seconds": max(0, round((final_dt - now).total_seconds())),
        "current_segment_remaining_seconds": current.get("remaining_seconds", 0),
        "current_station": current.get("current_station", ""),
        "current_status": current.get("status", ""),
        "segments": [serialize_seg(x) for x in results],
        "warnings": warnings,
    }

def serialize_seg(x):
    d = {}
    for k, v in x.items():
        d[k] = v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v
    return d

def calculate_route(payload):
    segments = payload.get("segments") or []
    if not 1 <= len(segments) <= 8:
        raise ValueError("구간은 1~8개로 입력하세요.")

    now = now_kst()
    start_text = str(payload.get("start_time") or now.strftime("%H:%M"))
    start_dt = clock_dt_near(start_text, now)
    if start_dt < now - timedelta(hours=8):
        start_dt += timedelta(days=1)

    requested_mode = str(payload.get("day") or "AUTO")
    mode, mode_reason = resolve_service_mode(requested_mode, start_dt)
    ready_dt = start_dt
    position_cache = {}
    results = []
    warnings = []

    for idx, s in enumerate(segments):
        line = str(s.get("line") or "").strip()
        fr = canon_station(s.get("from"))
        to = canon_station(s.get("to"))
        if not fr or not to:
            raise ValueError(f"{idx+1}번 구간의 승차역/하차역을 입력하세요.")
        if fr == to:
            raise ValueError(f"{idx+1}번 구간의 승차역과 하차역이 같습니다.")

        r = calculate_segment(line, mode, fr, to, ready_dt, position_cache)
        if not r.get("ok"):
            return {
                "ok": False,
                "failed_segment": idx + 1,
                "error": r.get("error", "구간 계산 실패"),
                "diagnostics": r.get("diagnostics", {}),
                "partial_segments": [serialize_seg(x) for x in results],
            }

        chosen = r["chosen"]
        chosen["segment_index"] = idx + 1
        chosen["ready_dt"] = ready_dt
        chosen["diagnostics"] = r.get("diagnostics", {})
        chosen["nearby_candidates"] = r.get("public_candidates", [])
        results.append(chosen)

        if chosen["confidence"] != "높음":
            warnings.append(
                f"{idx+1}구간 {line} {fr}→{to}: {chosen['method']} ({chosen['confidence']} 신뢰도)"
            )

        transfer_walk = max(0, float(s.get("transfer_walk") or 0))
        if idx < len(segments) - 1:
            ready_dt = chosen["alight_dt"] + timedelta(minutes=transfer_walk)
        else:
            ready_dt = chosen["alight_dt"]

    end_dt = results[-1]["alight_dt"]
    total_seconds = round((end_dt - start_dt).total_seconds())

    baseline = payload.get("baseline_minutes")
    try:
        baseline = float(baseline) if baseline not in ("", None) else None
    except Exception:
        baseline = None

    return {
        "ok": True,
        "service_mode": mode,
        "service_mode_reason": mode_reason,
        "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "arrival_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "total_seconds": total_seconds,
        "baseline_minutes": baseline,
        "difference_seconds": None if baseline is None else round(total_seconds - baseline * 60),
        "segments": [serialize_seg(x) for x in results],
        "warnings": warnings,
    }

