# -*- coding: utf-8 -*-
"""
지금타 V13.2.2 — 1~9호선 다중 환승 ETA
핵심:
  1호선: 서울시 realtimePosition + 사용자가 제공한 코레일 공식 평/휴일 시간표
  2~9호선: 서울시 realtimePosition + 서울교통공사 공식 열차운행시각표(250930)
  모든 노선: 현재열차 trainNo -> 시간표 trainNo 매칭 -> 현재 지연 -> 향후 역 ETA
"""
import json, os, re, urllib.request, urllib.parse, time, statistics, heapq
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
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
TRANSFER_DATA = load_json("transfer_data.json")
EXTRA_LINES = ("경의중앙선", "수인분당선", "경춘선", "경강선", "서해선", "공항철도")

LINE_NAMES = [f"{i}호선" for i in range(1, 10)] + list(EXTRA_LINES)
LINE_NUM = {f"{i}호선": str(i) for i in range(1, 10)}
LINE_IDS = {f"{i}호선": f"100{i}" for i in range(1, 10)}
LINE_IDS.update({"경의중앙선": "1063", "수인분당선": "1075", "경춘선": "1067", "경강선": "1081", "서해선": "1093", "공항철도": "1065"})

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
    "세종릉": "세종대왕릉",
    "도예촌": "신둔도예촌",
    "경광주": "경기광주",
    "신이매": "이매",
    "신판교": "판교",
    "신초지": "초지",
    "시흥능": "시흥능곡",
    "시흥청": "시흥시청",
    "신신현": "신현",
    "신신천": "신천",
    "시흥대": "시흥대야",
    "신소사": "소사",
    "부천종": "부천종합운동장",
    "신김포": "김포공항",
    "평내호": "평내호평",
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
    raw = str(v or "").strip()
    # 서울시 공식 realtimePosition 명세:
    # 0 진입 / 1 도착 / 2 출발 / 3 전역출발
    known = {"0": "진입", "1": "도착", "2": "출발", "3": "전역출발"}
    if raw in known:
        return known[raw]
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", raw):
        return ""
    return raw or ""

def scheduled_reference(stop, status):
    """단일 역 상태의 기본 기준시각. 상태 3은 scheduled_reference_at에서 처리."""
    arr, dep = stop["arr"], stop["dep"]
    s = str(status or "")
    if s == "2" and dep is not None:
        return dep
    if s == "1" and arr is not None:
        return arr
    if s == "0" and arr is not None:
        return arr - 30
    return dep if dep is not None else arr

def scheduled_reference_at(stops, index, status):
    """
    trainSttus=3(전역출발)은 statnNm 기준 현재 역의 출발시각이 아니라
    바로 이전 역의 출발시각을 관측 기준으로 사용한다.

    목적역에서 3을 현재역 dep로 해석하면
    target arr < current dep가 되어 24시간 wrap이 발생할 수 있다.
    """
    if index is None or not (0 <= index < len(stops)):
        return None
    s = str(status or "").strip()
    if s == "3":
        for j in range(index - 1, -1, -1):
            prev = stops[j]
            ref = prev.get("dep") if prev.get("dep") is not None else prev.get("arr")
            if ref is not None:
                return ref
        arr = stops[index].get("arr")
        return (arr - 60) if arr is not None else scheduled_reference(stops[index], status)
    return scheduled_reference(stops[index], status)

def nearest_schedule_dt(sched_sec, ref_dt, delay=0):
    """24/25시 시간표까지 포함해 ref_dt와 가장 가까운 운행 occurrence를 찾는다."""
    base = ref_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    vals = [
        base + timedelta(days=d, seconds=sched_sec + delay)
        for d in (-2, -1, 0, 1, 2)
    ]
    return min(vals, key=lambda x: abs((x - ref_dt).total_seconds()))

def clock_to_sec(v):
    p = str(v or "").split(":")
    if len(p) < 2:
        raise ValueError("시각은 HH:MM 형식이어야 합니다.")
    return int(p[0]) * 3600 + int(p[1]) * 60 + (int(p[2]) if len(p) > 2 else 0)

def clock_dt_near(v, ref=None):
    """
    HH:MM[:SS]뿐 아니라 이전 API 응답의 YYYY-MM-DD HH:MM:SS도 허용한다.
    V12.1 새로고침은 고정 플랫폼 시각을 full datetime으로 다시 보내므로
    이를 HH:MM 파서에 넣으면 `invalid literal for int()`가 발생했다.
    """
    ref = ref or now_kst()
    text = str(v or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d%H%M%S"):
        try:
            return datetime.strptime(text, fmt)
        except Exception:
            pass
    sec = clock_to_sec(text)
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

def schedule_dt_before(sched_sec, ready_dt, delay=0, window_seconds=600):
    """
    사용자가 예상보다 일찍 플랫폼/환승역에 도착했을 때 탈 수 있었을
    직전 열차 한 대를 찾는다. 기본 탐색범위는 ready_dt 이전 10분.
    """
    midnight = ready_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    vals = []
    for d in (-2, -1, 0, 1):
        dt = midnight + timedelta(days=d, seconds=sched_sec + delay)
        gap = (ready_dt - dt).total_seconds()
        if 5 < gap <= window_seconds:
            vals.append(dt)
    return max(vals) if vals else None

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


def service_day_reference(ref_dt=None):
    """02:00 이전은 철도 운행일 기준으로 전날에 속한다."""
    ref_dt = ref_dt or now_kst()
    cutoff = ref_dt.replace(hour=2, minute=0, second=0, microsecond=0)
    if ref_dt < cutoff:
        return ref_dt - timedelta(days=1), True
    return ref_dt, False

def resolve_service_mode(mode, ref_dt=None):
    ref_dt = ref_dt or now_kst()
    if mode == "AUTO":
        service_ref, rolled = service_day_reference(ref_dt)
        resolved, reason = auto_service_mode(service_ref)
        if rolled:
            reason = f"전일 운행일 · {reason} (02시 기준)"
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
    normalized_tn = norm_train(tn)
    # 사용자 제공 규칙: 1호선 K19xx 열번은 전부 급행.
    service = "express" if re.fullmatch(r"K19\d{2}", normalized_tn) else tr.get("service", "local")
    return {
        "train_no": tn,
        "direction": tr.get("direction", ""),
        "service": service,
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


@lru_cache(maxsize=None)
def all_trains(line, mode):
    """정규화된 노선별 전체 시간표는 프로세스 생명주기 동안 재사용한다."""
    korail_day, metro_week = choose_modes(mode)
    if line == "1호선":
        return tuple(normalize_s1_train(tn, tr) for tn, tr in S1[korail_day].items())
    if line in EXTRA_LINES:
        return tuple(
            normalize_extra_train(tn, tr)
            for tn, tr in EXTRA[line]["trains"][korail_day].items()
        )
    num = LINE_NUM[line]
    return tuple(
        normalize_metro_train(tn, raw)
        for tn, raw in OFF["days"].get(metro_week, {}).get(num, {}).items()
    )



# ---------- Same-vehicle train-number continuation (2호선 / 6호선) ----------
# 서울교통공사 시간표는 운행단위(열번) 기준이라 물리적으로 같은 차량이
# 짧은 정차 후 열번을 바꾸는 경우가 별도 열차로 끊겨 있다.
# - 2호선 본선: 성수에서 같은 방향의 다음 열번으로 이어짐
# - 6호선: 봉화산/신내 쪽에서 온 열차가 응암에서 열번을 바꿔 응암순환 구간으로 이어짐
# 실제 승객에게는 환승이 아니므로, 아래에서 두 시간표를 '연속운행 가상열차'로 묶는다.
_CONTINUATION_CACHE = {}
_VIRTUAL_TRAIN_CACHE = {}
MAX_CONTINUATION_GAP_SECONDS = 180
LINE2_MAX_CONTINUATION_GAP_SECONDS = 120  # 성수에서 3분(180초) 연결은 차량기지 입고 등 오판 가능성이 높아 배제


def _first_train_sec(tr):
    for st in tr.get("stops", []):
        sec = st.get("dep") if st.get("dep") is not None else st.get("arr")
        if sec is not None:
            return sec
    return None


def _last_train_sec(tr):
    for st in reversed(tr.get("stops", [])):
        sec = st.get("arr") if st.get("arr") is not None else st.get("dep")
        if sec is not None:
            return sec
    return None


def _line2_mainline(tr):
    names = {canon_station(x.get("station")) for x in tr.get("stops", [])}
    # 성수지선(신설동 방면)을 순환 본선 연속열번으로 잘못 묶지 않기 위한 방어조건.
    return len(tr.get("stops", [])) > 10 and "성수" in names and ("뚝섬" in names or "건대입구" in names)


def continuation_links(line, mode):
    """시간표의 종착/시발 시각으로 같은 차량의 다음 열번을 추론한다."""
    key = (line, mode)
    if key in _CONTINUATION_CACHE:
        return _CONTINUATION_CACHE[key]
    if line not in ("2호선", "6호선"):
        _CONTINUATION_CACHE[key] = {}
        return {}

    trains = {norm_train(t["train_no"]): t for t in all_trains(line, mode)}
    preds, succs = [], []
    for tn, tr in trains.items():
        first = _first_train_sec(tr)
        last = _last_train_sec(tr)
        if first is None or last is None:
            continue
        if line == "2호선":
            if not _line2_mainline(tr):
                continue
            if canon_station(tr.get("dest")) == "성수":
                preds.append((tn, tr, last))
            if canon_station(tr.get("start")) == "성수":
                succs.append((tn, tr, first))
        else:  # 6호선
            if canon_station(tr.get("dest")) == "응암" and tr.get("direction") == "UP":
                preds.append((tn, tr, last))
            if canon_station(tr.get("start")) == "응암" and tr.get("direction") == "DOWN":
                succs.append((tn, tr, first))

    preds.sort(key=lambda x: x[2])
    succs.sort(key=lambda x: x[2])
    links = {}

    if line == "2호선":
        # 2호선은 성수 종착열차를 시간순으로 먼저 처리하면,
        # 더 이른 종착열차가 후속 열번을 '선점'하는 문제가 생길 수 있다.
        # 예: 평일 2087(09:02 종착)이 2151(09:05 시발)을 180초로 먼저 잡으면
        # 실제 30초 연결인 2089(09:04:30 종착) → 2151이 사라진다.
        #
        # 따라서 모든 유효 후보쌍을 만든 뒤 '간격이 가장 짧은 쌍'부터
        # 1:1로 확정한다. 180초(3분)는 아예 후보에서 제외한다.
        edges = []
        for ptn, pred, pend in preds:
            for stn, succ, sstart in succs:
                if succ.get("direction") != pred.get("direction"):
                    continue
                gap = sstart - pend
                while gap < 0:
                    gap += 86400
                if 0 <= gap <= LINE2_MAX_CONTINUATION_GAP_SECONDS:
                    edges.append((gap, pend, sstart, ptn, stn))

        # 짧은 연결을 최우선. 동일 gap이면 실제 시각 순서로 안정적으로 결정.
        edges.sort(key=lambda x: (x[0], x[1], x[2], x[3], x[4]))
        used_preds = set()
        used_successors = set()
        for gap, pend, sstart, ptn, stn in edges:
            if ptn in used_preds or stn in used_successors:
                continue
            used_preds.add(ptn)
            used_successors.add(stn)
            links[ptn] = {
                "next_train_no": stn,
                "gap_seconds": int(gap),
                "station": "성수",
                "next_start_sec": int(sstart),
            }
    else:
        # 6호선은 사용자 수동 검증에서 30초 초과 연결까지 모두 정상 확인됨.
        # 기존 180초 + 선행열번 기준 최근접 매칭을 유지한다.
        used_successors = set()
        for ptn, pred, pend in preds:
            candidates = []
            for stn, succ, sstart in succs:
                if stn in used_successors:
                    continue
                gap = sstart - pend
                while gap < 0:
                    gap += 86400
                if 0 <= gap <= MAX_CONTINUATION_GAP_SECONDS:
                    candidates.append((gap, stn, succ, sstart))
            if not candidates:
                continue
            candidates.sort(key=lambda x: (x[0], x[1]))
            min_gap = candidates[0][0]
            nearest = [x for x in candidates if x[0] == min_gap]
            if len(nearest) != 1:
                continue
            gap, stn, succ, sstart = nearest[0]
            used_successors.add(stn)
            links[ptn] = {
                "next_train_no": stn,
                "gap_seconds": int(gap),
                "station": "응암",
                "next_start_sec": int(sstart),
            }
    _CONTINUATION_CACHE[key] = links
    return links


def _shifted_stop(stop, offset=0):
    return {
        "station": canon_station(stop.get("station")),
        "arr": None if stop.get("arr") is None else int(stop.get("arr")) + offset,
        "dep": None if stop.get("dep") is None else int(stop.get("dep")) + offset,
        "call": bool(stop.get("call", True)),
    }


def merged_continuation_train(line, mode, train_no):
    """train_no와 바로 이어지는 다음 열번을 한 번만 붙인 가상 열차를 반환한다."""
    key = (line, mode, norm_train(train_no))
    if key in _VIRTUAL_TRAIN_CACHE:
        return _VIRTUAL_TRAIN_CACHE[key]
    link = continuation_links(line, mode).get(norm_train(train_no))
    if not link:
        _VIRTUAL_TRAIN_CACHE[key] = None
        return None
    pred = get_train(line, mode, train_no)
    succ = get_train(line, mode, link["next_train_no"])
    if not pred or not succ:
        _VIRTUAL_TRAIN_CACHE[key] = None
        return None

    pend = _last_train_sec(pred)
    sstart = _first_train_sec(succ)
    offset = 0
    while sstart is not None and pend is not None and sstart + offset < pend:
        offset += 86400

    merged = [_shifted_stop(x) for x in pred.get("stops", [])]
    next_stops = [_shifted_stop(x, offset) for x in succ.get("stops", [])]
    boundary = link["station"]
    # 종착 응암/성수 + 다음 시발 응암/성수는 한 번의 정차로 합친다.
    if merged and next_stops and canon_station(merged[-1]["station"]) == boundary and canon_station(next_stops[0]["station"]) == boundary:
        first_next = next_stops.pop(0)
        if merged[-1].get("arr") is None:
            merged[-1]["arr"] = first_next.get("arr")
        if first_next.get("dep") is not None:
            merged[-1]["dep"] = first_next.get("dep")
        merged[-1]["call"] = bool(merged[-1].get("call", True) or first_next.get("call", True))
    merged.extend(next_stops)

    virtual = {
        "train_no": pred["train_no"],
        "continuation_train_no": succ["train_no"],
        "train_numbers": [pred["train_no"], succ["train_no"]],
        "continuation_station": boundary,
        "continuation_gap_seconds": link["gap_seconds"],
        "continuation_start_sec": link["next_start_sec"] + offset,
        "physical_continuation": True,
        "direction": pred.get("direction", ""),
        "continuation_direction": succ.get("direction", ""),
        "service": pred.get("service", "local"),
        "start": pred.get("start", ""),
        "dest": succ.get("dest", ""),
        "stops": merged,
    }
    _VIRTUAL_TRAIN_CACHE[key] = virtual
    return virtual


@lru_cache(maxsize=1024)
def _route_trains_cached(line, mode, start, end):
    start = canon_station(start); end = canon_station(end)
    result = []
    originals = all_trains(line, mode)
    for tr in originals:
        if route_pair(tr.get("stops", []), start, end):
            result.append(tr)
    if line in ("2호선", "6호선"):
        links = continuation_links(line, mode)
        for ptn, info in links.items():
            pred = get_train(line, mode, ptn)
            succ = get_train(line, mode, info["next_train_no"])
            if not pred or not succ:
                continue
            if route_pair(pred.get("stops", []), start, end) or route_pair(succ.get("stops", []), start, end):
                continue
            virtual = merged_continuation_train(line, mode, ptn)
            if virtual and route_pair(virtual.get("stops", []), start, end):
                result.append(virtual)
    return tuple(result)

def route_trains(line, mode, start, end):
    """start→end 운행 가능 열차 목록을 캐시하여 다중 경로 채점의 반복 스캔을 제거."""
    yield from _route_trains_cached(line, mode, canon_station(start), canon_station(end))


def active_train_no_for_virtual(tr, ref_dt, delay=0):
    if not tr.get("physical_continuation") or not tr.get("continuation_train_no"):
        return tr.get("train_no", "")
    boundary_sec = tr.get("continuation_start_sec")
    if boundary_sec is None:
        return tr.get("train_no", "")
    midnight = _service_occurrence_midnight(tr, ref_dt, delay)
    switch_dt = midnight + timedelta(seconds=boundary_sec + delay)
    return tr.get("continuation_train_no") if ref_dt >= switch_dt else tr.get("train_no", "")

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


# ---------- Transfer data ----------
def _transfer_key(station, from_line, to_line):
    return f"{canon_station(station)}|{from_line}|{to_line}"

def transfer_pair_info(station, from_line, to_line):
    # JSON 역명은 현재 canon_station 별칭과 대부분 동일하므로 direct/canonical 둘 다 시도.
    direct = TRANSFER_DATA.get("pairs", {}).get(f"{station}|{from_line}|{to_line}")
    if direct:
        return direct
    c = canon_station(station)
    for p in TRANSFER_DATA.get("pairs", {}).values():
        if canon_station(p.get("station")) == c and p.get("from_line") == from_line and p.get("to_line") == to_line:
            return p
    return None

def transfer_seconds(station, from_line, to_line):
    p = transfer_pair_info(station, from_line, to_line)
    if p:
        return int(p.get("default_seconds") or p.get("distance_seconds") or DEFAULT_TRANSFER_SECONDS)
    return DEFAULT_TRANSFER_SECONDS

def _direction_station(line, mode, start, end):
    """start→end를 운행하는 열차에서 end 다음 물리 역명을 방향 힌트로 구한다."""
    start = canon_station(start); end = canon_station(end)
    for tr in route_trains(line, mode, start, end):
        pair = route_pair(tr.get("stops", []), start, end)
        if not pair:
            continue
        _, ei = pair
        stops = tr.get("stops", [])
        for j in range(ei + 1, len(stops)):
            st = canon_station(stops[j].get("station"))
            if st and st != end:
                return st
    return ""

def _outgoing_direction_station(line, mode, start, end):
    """start에서 end 쪽으로 출발할 때 start 다음 물리 역명을 구한다."""
    start = canon_station(start); end = canon_station(end)
    for tr in route_trains(line, mode, start, end):
        pair = route_pair(tr.get("stops", []), start, end)
        if not pair:
            continue
        si, _ = pair
        stops = tr.get("stops", [])
        for j in range(si + 1, len(stops)):
            st = canon_station(stops[j].get("station"))
            if st and st != start:
                return st
    return ""

def best_transfer_detail(station, from_seg, to_seg, mode):
    p = transfer_pair_info(station, from_seg.get("line"), to_seg.get("line"))
    if not p:
        return {
            "station": canon_station(station),
            "seconds": DEFAULT_TRANSFER_SECONDS,
            "distance_m": None,
            "alight_position": "",
            "board_position": "",
            "from_direction": "",
            "to_direction": "",
            "matched": "fallback",
        }
    in_dir = _direction_station(from_seg["line"], mode, from_seg["from"], from_seg["to"])
    out_dir = _outgoing_direction_station(to_seg["line"], mode, to_seg["from"], to_seg["to"])
    records = p.get("records") or []
    def canon_dir(x): return canon_station(str(x or "").replace(" 방면", "").strip())
    both = [r for r in records if canon_dir(r.get("from_direction")) == canon_station(in_dir) and canon_dir(r.get("to_direction")) == canon_station(out_dir)]
    one_out = [r for r in records if canon_dir(r.get("to_direction")) == canon_station(out_dir)]
    one_in = [r for r in records if canon_dir(r.get("from_direction")) == canon_station(in_dir)]
    chosen = (both or one_out or one_in or records or [None])[0]
    matched = "direction" if both else "outgoing" if one_out else "incoming" if one_in else "pair"
    sec = int((chosen or {}).get("seconds") or p.get("default_seconds") or DEFAULT_TRANSFER_SECONDS)
    def pos(car, door):
        car=str(car or "").strip(); door=str(door or "").strip()
        return f"{car}-{door}" if car and door else car or door
    return {
        "station": canon_station(station),
        "seconds": sec,
        "distance_m": p.get("distance_m"),
        "alight_position": pos((chosen or {}).get("alight_car"), (chosen or {}).get("alight_door")),
        "board_position": pos((chosen or {}).get("board_car"), (chosen or {}).get("board_door")),
        "from_direction": (chosen or {}).get("from_direction") or in_dir,
        "to_direction": (chosen or {}).get("to_direction") or out_dir,
        "matched": matched,
    }

def enrich_transfer_segments(segments, mode):
    for i in range(len(segments)-1):
        a, b = segments[i], segments[i+1]
        if canon_station(a.get("to")) != canon_station(b.get("from")):
            continue
        info = best_transfer_detail(a["to"], a, b, mode)
        a["transfer_info"] = info
        a["transfer_seconds"] = int(info["seconds"])
        a["transfer_walk"] = round(info["seconds"] / 60, 3)
    if segments:
        segments[-1]["transfer_seconds"] = 0
        segments[-1]["transfer_walk"] = 0
        segments[-1]["transfer_info"] = None
    return segments

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
                    adj[(a, st)].append(((b, st), transfer_seconds(st, a, b), "transfer"))

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
    ok = any(True for _ in route_trains(line, mode, start, end))
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
            w = base_w
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


AUTO_ROUTE_MAX_UNIQUE_CANDIDATES = 8
AUTO_ROUTE_MAX_RAW_PATHS = 36

def _route_graph_shortest_with_bans(start, end, mode, source_node=None, banned_edges=None, banned_nodes=None):
    """
    Yen K-shortest-path용 Dijkstra.
    synthetic source/target를 사용해 출발/도착역의 여러 노선을 하나의 그래프로 취급한다.
    """
    start = canon_station(start)
    end = canon_station(end)
    banned_edges = set(banned_edges or ())
    banned_nodes = set(banned_nodes or ())

    source = ("__SOURCE__", start)
    target = ("__TARGET__", end)
    start_nodes = [(_line, start) for _line in _station_lines(start)]
    target_nodes = {(_line, end) for _line in _station_lines(end)}
    adj = _route_adjacency(mode)

    src = source if source_node is None else source_node
    pq = [(0, 0, src)]
    dist = {src: 0}
    prev = {}
    seq = 1

    while pq:
        d, _, u = heapq.heappop(pq)
        if d != dist.get(u):
            continue

        if u == target:
            nodes = []
            edges = []
            cur = target
            while cur != src:
                pu, kind, w = prev[cur]
                nodes.append(cur)
                edges.append((pu, cur, kind, w))
                cur = pu
            nodes.append(src)
            nodes.reverse()
            edges.reverse()
            return {"cost": int(d), "nodes": nodes, "edges": edges}

        if u in banned_nodes and u != src:
            continue

        if u == source:
            options = [(n, 0, "start") for n in start_nodes]
        elif u in target_nodes:
            options = list(adj.get(u, [])) + [(target, 0, "end")]
        else:
            options = adj.get(u, [])

        for v, w, kind in options:
            if (u, v) in banned_edges:
                continue
            if v in banned_nodes and v != target:
                continue
            nd = d + int(w)
            if nd < dist.get(v, 10**18):
                dist[v] = nd
                prev[v] = (u, kind, int(w))
                heapq.heappush(pq, (nd, seq, v))
                seq += 1
    return None

def _yen_route_paths(start, end, mode, max_raw=AUTO_ROUTE_MAX_RAW_PATHS):
    """
    정적 route graph에서 비용순 simple path를 생성한다.
    후보 생성만 정적 그래프를 사용하고, 최종 선택은 아래에서 실제 시간표/실시간 ETA로 다시 한다.
    """
    first = _route_graph_shortest_with_bans(start, end, mode)
    if not first:
        return []

    accepted = [first]
    candidate_heap = []
    candidate_seen = set()
    seq = 0

    while len(accepted) < max_raw:
        previous = accepted[-1]
        nodes = previous["nodes"]
        edges = previous["edges"]

        for i in range(len(nodes) - 1):
            spur_node = nodes[i]
            root_nodes = nodes[:i + 1]
            root_edges = edges[:i]
            root_cost = sum(int(e[3]) for e in root_edges)

            removed_edges = set()
            for p in accepted:
                if len(p["nodes"]) > i and p["nodes"][:i + 1] == root_nodes:
                    if i < len(p["nodes"]) - 1:
                        removed_edges.add((p["nodes"][i], p["nodes"][i + 1]))

            removed_nodes = set(root_nodes[:-1])
            spur = _route_graph_shortest_with_bans(
                start, end, mode,
                source_node=spur_node,
                banned_edges=removed_edges,
                banned_nodes=removed_nodes,
            )
            if not spur:
                continue

            total_nodes = root_nodes[:-1] + spur["nodes"]
            signature = tuple(total_nodes)
            if signature in candidate_seen or any(tuple(x["nodes"]) == signature for x in accepted):
                continue

            total_edges = root_edges + spur["edges"]
            total_cost = root_cost + spur["cost"]
            candidate_seen.add(signature)
            heapq.heappush(candidate_heap, (
                total_cost, seq,
                {"cost": int(total_cost), "nodes": total_nodes, "edges": total_edges},
            ))
            seq += 1

        if not candidate_heap:
            break
        _, _, nxt = heapq.heappop(candidate_heap)
        accepted.append(nxt)

    return accepted

def auto_candidate_routes(start, end, mode, max_unique=AUTO_ROUTE_MAX_UNIQUE_CANDIDATES):
    """
    정적 최단경로 하나만 쓰지 않고 여러 topology 후보를 만든다.
    동일한 segment 구성이 반복되는 path는 제거한다.
    """
    out = []
    seen = set()

    for raw in _yen_route_paths(start, end, mode):
        usable_edges = [
            e for e in raw["edges"]
            if e[2] not in ("start", "end")
        ]
        path = {
            "start": canon_station(start),
            "end": canon_station(end),
            "seconds": int(raw["cost"]),
            "edges": usable_edges,
        }
        try:
            segments = auto_path_to_segments(path, mode)
            segments = enrich_transfer_segments(segments, mode)
        except Exception:
            continue

        sig = tuple(
            (s.get("line"), canon_station(s.get("from")), canon_station(s.get("to")))
            for s in segments
        )
        if sig in seen:
            continue
        seen.add(sig)
        out.append({
            "path": path,
            "segments": segments,
            "signature": sig,
        })
        if len(out) >= max_unique:
            break

    if not out:
        # 기존 단일 경로를 마지막 fallback으로 유지.
        path = auto_find_path(start, end, mode)
        segments = enrich_transfer_segments(auto_path_to_segments(path, mode), mode)
        out.append({
            "path": path,
            "segments": segments,
            "signature": tuple((s["line"], s["from"], s["to"]) for s in segments),
        })
    return out

def _candidate_interchanges(segments):
    return [
        segments[i]["to"]
        for i in range(len(segments) - 1)
        if canon_station(segments[i]["to"]) == canon_station(segments[i+1]["from"])
    ]

def _route_confidence(result):
    rank = {"높음": 3, "중간": 2, "낮음": 1}
    if not result or not result.get("segments"):
        return "낮음"
    return min(
        (s.get("confidence", "낮음") for s in result["segments"]),
        key=lambda x: rank.get(x, 0),
    )


def auto_path_to_segments(path, mode):
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
            finish_current(w / 60)
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
            finish_current(DEFAULT_TRANSFER_SECONDS / 60)
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
    """
    V13.2.2:
    1) 정적 route graph에서 상위 topology 후보 여러 개 생성
    2) 각 후보를 실제 출발시각의 열차 시간표로 1차 채점
    3) realtimePosition을 후보 간 공유하면서 모든 후보를 실시간 ETA로 재채점
    4) 실제 도착예정시각이 가장 빠른 후보를 선택
       - ETA 동률이면 환승이 적은 경로 우선
    """
    now = now_kst()
    start_text = str(payload.get("start_time") or now.strftime("%H:%M"))
    start_dt = clock_dt_near(start_text, now)
    if start_dt < now - timedelta(hours=8):
        start_dt += timedelta(days=1)

    requested_mode = str(payload.get("day") or "AUTO")
    mode, mode_reason = resolve_service_mode(requested_mode, start_dt)

    start = canon_station(payload.get("from"))
    end = canon_station(payload.get("to"))
    candidates = auto_candidate_routes(start, end, mode)

    # 1차: 실시간 API 호출 없이 실제 시간표 배차/대기/환승을 반영.
    schedule_only_cache = {
        line: {
            "rows": [],
            "error": "자동경로 시간표 사전채점",
            "available": False,
        }
        for line in LINE_NAMES
    }

    schedule_scored = []
    for c in candidates:
        score = calculate_route({
            "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "day": mode,
            "segments": c["segments"],
            "refresh_only": False,
        }, position_cache=schedule_only_cache)
        if not score.get("ok"):
            continue
        schedule_scored.append({
            **c,
            "schedule_result": score,
        })

    if not schedule_scored:
        # 최악의 경우 기존 정적 경로 선택을 유지.
        path = auto_find_path(start, end, mode)
        segments = enrich_transfer_segments(auto_path_to_segments(path, mode), mode)
        interchanges = _candidate_interchanges(segments)
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
            "selection_method": "정적 그래프 fallback",
            "candidate_count": 1,
            "live_scored_count": 0,
            "alternatives": [],
        }

    schedule_scored.sort(key=lambda x: (
        x["schedule_result"]["arrival_time"],
        max(0, len(x["segments"]) - 1),
        x["path"]["seconds"],
    ))

    # 2차: 후보 전체의 노선 실시간 API를 병렬로 1회씩만 조회한 뒤 공유한다.
    # 서울 API 일부 노선이 느려도 노선 수 × timeout으로 직렬 누적되지 않는다.
    live_lines = {
        s.get("line")
        for c in schedule_scored
        for s in c.get("segments", [])
    }
    live_cache = prefetch_position_cache(live_lines, timeout=5)
    live_scored = []
    for c in schedule_scored:
        live = calculate_route({
            "start_time": start_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "day": mode,
            "segments": c["segments"],
            "refresh_only": False,
        }, position_cache=live_cache)

        # 실시간 API 장애여도 calculate_route 내부 schedule fallback으로 보통 ok가 유지된다.
        if not live.get("ok"):
            live = c["schedule_result"]

        live_scored.append({
            **c,
            "live_result": live,
        })

    live_scored.sort(key=lambda x: (
        x["live_result"]["arrival_time"],
        max(0, len(x["segments"]) - 1),
        x["schedule_result"]["arrival_time"],
        x["path"]["seconds"],
    ))

    selected = live_scored[0]
    selected_segments = selected["segments"]
    selected_result = selected["live_result"]
    interchanges = _candidate_interchanges(selected_segments)

    alternatives = []
    for item in live_scored[1:4]:
        result = item["live_result"]
        alternatives.append({
            "segments": item["segments"],
            "interchanges": _candidate_interchanges(item["segments"]),
            "transfer_count": max(0, len(item["segments"]) - 1),
            "route_seconds": int(item["path"]["seconds"]),
            "total_seconds": int(result["total_seconds"]),
            "arrival_time": result["arrival_time"],
            "confidence": _route_confidence(result),
        })

    return {
        "ok": True,
        "service_mode": mode,
        "service_mode_reason": mode_reason,
        "from": start,
        "to": end,
        "route_seconds": int(selected["path"]["seconds"]),
        "estimated_total_seconds": int(selected_result["total_seconds"]),
        "estimated_arrival_time": selected_result["arrival_time"],
        "estimated_confidence": _route_confidence(selected_result),
        "transfer_count": max(0, len(selected_segments) - 1),
        "interchanges": interchanges,
        "segments": selected_segments,
        "selection_method": "다중 후보 시간표 + 실시간 ETA 비교",
        "candidate_count": len(candidates),
        "live_scored_count": len(live_scored),
        "alternatives": alternatives,
    }


# ---------- Live API ----------
def fetch_position(line, timeout=5):
    require_api_key()
    q = urllib.parse.quote(line, safe="")
    url = f"http://swopenAPI.seoul.go.kr/api/subway/{API_KEY}/json/realtimePosition/0/300/{q}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "JigeumTa-V13.2.2/1.0",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("RESULT"), dict):
            return False, data["RESULT"], data
        return True, None, data
    except Exception as e:
        return False, {"message": f"{type(e).__name__}: {e}"}, None

def position_rows(data):
    return data.get("realtimePositionList", []) if isinstance(data, dict) else []

def prefetch_position_cache(lines, timeout=5):
    """여러 노선 realtimePosition을 병렬 조회. 한 노선 장애가 전체 요청을 직렬로 지연시키지 않는다."""
    unique = sorted({str(x) for x in lines if x in LINE_NAMES})
    if not unique:
        return {}
    cache = {}
    workers = min(6, len(unique))
    def one(line):
        try:
            ok, err, data = fetch_position(line, timeout=timeout)
            if ok:
                return line, {"rows": position_rows(data), "error": "", "available": True}
            message = (err or {}).get("message", err) if isinstance(err, dict) else err
            return line, {"rows": [], "error": str(message or "실시간 위치 조회 실패"), "available": False}
        except Exception as e:
            return line, {"rows": [], "error": f"{type(e).__name__}: {e}", "available": False}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, line) for line in unique]
        for fut in as_completed(futures):
            line, value = fut.result()
            cache[line] = value
    return cache

def cached_position_rows(line, position_cache):
    """
    realtimePosition 장애가 전체 경로 계산 실패로 이어지지 않도록 한다.
    실패 시 빈 관측값을 반환하여 공식 시간표 기반 계산으로 자동 강등한다.
    """
    if line not in position_cache:
        ok, err, data = fetch_position(line)
        if ok:
            position_cache[line] = {
                "rows": position_rows(data),
                "error": "",
                "available": True,
            }
        else:
            message = (err or {}).get("message", err) if isinstance(err, dict) else err
            position_cache[line] = {
                "rows": [],
                "error": str(message or "실시간 위치 조회 실패"),
                "available": False,
            }

    cached = position_cache[line]
    # 이전 코드/테스트에서 list를 직접 넣는 경우도 허용.
    if isinstance(cached, list):
        return cached, "", True
    return (
        cached.get("rows", []),
        cached.get("error", ""),
        bool(cached.get("available")),
    )

# ---------- Schedule path helpers ----------
def indices(stops, station):
    c = canon_station(station)
    return [i for i, s in enumerate(stops) if canon_station(s["station"]) == c]

def route_pair(stops, start, end, min_start_idx=0):
    # 급행/직통의 통과시각은 위치·지연 계산에는 쓰되,
    # call=false 역에서는 승하차할 수 없다.
    starts = [
        i for i in indices(stops, start)
        if i >= min_start_idx and bool(stops[i].get("call", True))
    ]
    ends = [
        i for i in indices(stops, end)
        if bool(stops[i].get("call", True))
    ]
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

def stop_time_sec(stop):
    # 1호선 급행 통과역은 arr=None, dep=통과시각 형태가 있으므로 dep 우선.
    return stop.get("dep") if stop.get("dep") is not None else stop.get("arr")

def _service_occurrence_midnight(tr, ref_dt, delay=0):
    points = [stop_time_sec(x) for x in tr.get("stops", [])]
    points = [x for x in points if x is not None]
    base = ref_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if not points:
        return base
    first, last = min(points) + delay, max(points) + delay
    choices = []
    for d in (-2, -1, 0, 1):
        midnight = base + timedelta(days=d)
        st = midnight + timedelta(seconds=first)
        en = midnight + timedelta(seconds=last)
        if st <= ref_dt <= en:
            score = 0
        elif ref_dt < st:
            score = (st-ref_dt).total_seconds()
        else:
            score = (ref_dt-en).total_seconds()
        choices.append((score, midnight))
    return min(choices, key=lambda x:x[0])[1]

def estimated_train_location(tr, ref_dt=None, delay=0):
    """API 미포착 시 공식 시간표+추정 지연으로 현재 소재를 계산."""
    ref_dt = ref_dt or now_kst()
    stops = tr.get("stops", [])
    timed = [(i, stop_time_sec(x)) for i,x in enumerate(stops) if stop_time_sec(x) is not None]
    if not timed:
        return {"kind":"expected","label":"예상 소재 계산 불가","station":"","status":""}
    midnight = _service_occurrence_midnight(tr, ref_dt, delay)
    timeline = [(i, midnight + timedelta(seconds=sec+delay)) for i,sec in timed]
    first_i, first_dt = timeline[0]
    last_i, last_dt = timeline[-1]

    if ref_dt < first_dt:
        st = canon_station(stops[first_i]["station"])
        if (first_dt-ref_dt).total_seconds() <= 3*3600:
            return {"kind":"expected","label":f"{st} 출발 전 예상","station":st,"status":"출발 전 예상"}
        return {"kind":"expected","label":"운행 전","station":"","status":"운행 전"}

    if ref_dt > last_dt:
        st = canon_station(stops[last_i]["station"])
        if (ref_dt-last_dt).total_seconds() <= 30*60:
            return {"kind":"expected","label":f"{st} 도착 추정","station":st,"status":"도착 추정"}
        return {"kind":"expected","label":"운행 종료 추정","station":st,"status":"운행 종료 추정"}

    ni, nd = min(timeline, key=lambda x:abs((x[1]-ref_dt).total_seconds()))
    if abs((nd-ref_dt).total_seconds()) <= 45:
        stop = stops[ni]
        st = canon_station(stop["station"])
        pass_like = ni not in (0, len(stops)-1) and stop.get("arr") is None
        status = "통과 예상" if pass_like else "부근 예상"
        return {"kind":"expected","label":f"{st}역 {status}","station":st,"status":status}

    for (i,t1),(j,t2) in zip(timeline,timeline[1:]):
        if t1 <= ref_dt <= t2:
            a = canon_station(stops[i]["station"]); b = canon_station(stops[j]["station"])
            return {"kind":"expected","label":f"{a} → {b} 사이 예상","station":"","status":"구간 운행 예상"}
    return {"kind":"expected","label":"예상 소재 계산 중","station":"","status":""}

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
        ref = scheduled_reference_at(tr["stops"], ci, p.get("trainSttus"))
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
        pair = route_pair(stops, start, end, min_start_idx=ci or 0) if ci is not None else None
        if not pair and line in ("2호선", "6호선"):
            virtual = merged_continuation_train(line, mode, tr.get("train_no"))
            if virtual:
                tr = virtual
                stops = tr["stops"]
                ci = first_current_index(stops, o["current_station"])
                pair = route_pair(stops, start, end, min_start_idx=ci or 0) if ci is not None else None
        if ci is None or not pair:
            continue
        si, ei = pair
        # If the current train is already beyond the boarding station, reject it.
        if ci > si:
            continue

        ref = scheduled_reference_at(stops, ci, o["raw"].get("trainSttus"))
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
            "continuation_train_no": tr.get("continuation_train_no", ""),
            "physical_continuation": bool(tr.get("physical_continuation")),
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
            "status": o["status"],
            "location_kind": "live",
            "location_label": f"{o['current_station']} {o['status']}",
            "confidence": "높음",
            "method": "실시간 열차 위치 + 열차별 공식 시간표",
            "projected": False,
        })
    out.sort(key=lambda x: (x["alight_dt"], x["board_dt"]))
    return out

def static_projected_candidates(line, mode, start, end, ready_dt, observations):
    # V13.2.2: 모든 열차에 예상 소재를 계산한 뒤 자르는 대신,
    # 먼저 시간표상 탑승 가능한 후보를 추린 뒤 상위 30개만 소재를 계산한다.
    raw = []
    now = now_kst()
    for tr in route_trains(line, mode, start, end):
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
        if (board_dt - ready_dt).total_seconds() > 4 * 3600:
            continue
        alight_dt = board_dt + timedelta(seconds=asec - bsec)
        raw.append((alight_dt, board_dt, tr, delay))

    raw.sort(key=lambda x: (x[0], x[1]))
    out = []
    for alight_dt, board_dt, tr, delay in raw[:30]:
        expected_location = estimated_train_location(tr, now, delay)
        out.append({
            "line": line,
            "from": canon_station(start),
            "to": canon_station(end),
            "train_no": tr["train_no"],
            "continuation_train_no": tr.get("continuation_train_no", ""),
            "physical_continuation": bool(tr.get("physical_continuation")),
            "service": tr["service"],
            "direction": tr["direction"],
            "origin": tr["start"],
            "destination": tr["dest"],
            "board_dt": board_dt,
            "alight_dt": alight_dt,
            "wait_seconds": round((board_dt - ready_dt).total_seconds()),
            "ride_seconds": round((alight_dt - board_dt).total_seconds()),
            "delay_seconds": round(delay),
            "current_station": expected_location.get("station", ""),
            "status": expected_location.get("status", ""),
            "location_kind": "expected",
            "location_label": expected_location.get("label", "예상 소재 계산 중"),
            "confidence": "중간" if observations else "낮음",
            "method": "공식 시간표 + 현재 동일방향 지연 중앙값" if observations else "공식 시간표만 사용",
            "projected": True,
        })
    return out

def previous_schedule_candidate(line, mode, start, end, ready_dt, observations):
    """
    기본 추천보다 바로 앞선 시간표 열차 1대를 반환한다.
    '앞 열차를 탄 것 같아요' 기능용이며 기본 추천 선정에는 참여하지 않는다.
    """
    best = None
    for tr in route_trains(line, mode, start, end):
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
        board_dt = schedule_dt_before(bsec, ready_dt, delay, 600)
        if not board_dt:
            continue
        alight_dt = board_dt + timedelta(seconds=asec - bsec)
        # 이미 목적지에 도착한 지 오래된 열차는 제외.
        if alight_dt < now_kst() - timedelta(minutes=2):
            continue

        expected_location = estimated_train_location(tr, now_kst(), delay)
        c = {
            "line": line,
            "from": canon_station(start),
            "to": canon_station(end),
            "train_no": tr["train_no"],
            "continuation_train_no": tr.get("continuation_train_no", ""),
            "physical_continuation": bool(tr.get("physical_continuation")),
            "service": tr["service"],
            "direction": tr["direction"],
            "origin": tr["start"],
            "destination": tr["dest"],
            "board_dt": board_dt,
            "alight_dt": alight_dt,
            "wait_seconds": round((board_dt - ready_dt).total_seconds()),
            "ride_seconds": round((alight_dt - board_dt).total_seconds()),
            "delay_seconds": round(delay),
            "current_station": expected_location.get("station", ""),
            "status": expected_location.get("status", ""),
            "location_kind": "expected",
            "location_label": expected_location.get("label", "예상 소재 계산 중"),
            "confidence": "중간" if observations else "낮음",
            "method": "직전 열차 추정 · 공식 시간표 + 현재 지연" if observations else "직전 열차 추정 · 공식 시간표",
            "projected": True,
        }
        if best is None or c["board_dt"] > best["board_dt"]:
            best = c
    return best


def public_candidate(c, selected=False):
    return {
        "train_no": c.get("train_no", ""),
        "continuation_train_no": c.get("continuation_train_no", ""),
        "physical_continuation": bool(c.get("physical_continuation")),
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
        "status": c.get("status", ""),
        "location_kind": c.get("location_kind", "expected" if c.get("projected") else "live"),
        "location_label": c.get("location_label", ""),
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

    positions, realtime_error, realtime_available = cached_position_rows(line, position_cache)
    observations, diag = observe_delays(line, mode, positions)
    diag["realtime_available"] = realtime_available
    if realtime_error:
        diag["realtime_error"] = realtime_error

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
    previous = previous_schedule_candidate(line, mode, start, end, ready_dt, observations)
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
        "previous_candidate": public_candidate(previous) if previous else None,
        "diagnostics": diag,
    }

def tracked_train_segment(line, mode, start, end, train_no, position_cache, boarded_at=None):
    """
    사용자가 실제 탑승했다고 표시한 열차를 잠금 추적한다.
    다른 후보 열차로 절대 교체하지 않고 지정 train_no만 따라간다.
    """
    now = now_kst()
    base_tr = get_train(line, mode, train_no)
    if not base_tr:
        return {
            "ok": False,
            "error": f"{line} 공식 시간표에서 탑승 열차 {train_no}을 찾지 못했습니다."
        }

    tr = base_tr
    pair = route_pair(tr["stops"], start, end)
    if not pair and line in ("2호선", "6호선"):
        virtual = merged_continuation_train(line, mode, base_tr.get("train_no"))
        if virtual and route_pair(virtual.get("stops", []), start, end):
            tr = virtual
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

    positions, realtime_error, realtime_available = cached_position_rows(line, position_cache)
    observations, diag = observe_delays(line, mode, positions)
    diag["realtime_available"] = realtime_available
    if realtime_error:
        diag["realtime_error"] = realtime_error
    wanted_numbers = {norm_train(tr.get("train_no"))}
    if tr.get("continuation_train_no"):
        wanted_numbers.add(norm_train(tr.get("continuation_train_no")))

    # 열번이 응암/성수에서 바뀌어도 같은 물리 차량의 후속 열번까지 추적한다.
    live_options = [o for o in observations if norm_train(o.get("train_no")) in wanted_numbers]
    live = max(live_options, key=lambda o: o.get("observed") or now, default=None)

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

        # 목적지 도착/출발이 실시간 API에서 확인되는 순간 구간 완료 처리.
        # 이전에는 목적지를 지나친 뒤(ci > ei)에만 완료되어 환승 전환이 늦었다.
        live_status_text = str(live.get("status") or "").strip()
        arrived_at_target = (
            ci is not None and (
                ci > ei or
                (ci == ei and live_status_text in ("도착", "출발"))
            )
        )
        if arrived_at_target:
            return {
                "ok": True,
                "arrived": True,
                "chosen": {
                    "line": line, "from": canon_station(start), "to": canon_station(end),
                    "train_no": live.get("train_no"),
                    "continuation_train_no": tr.get("continuation_train_no", ""),
                    "physical_continuation": bool(tr.get("physical_continuation")),
                    "service": tr["service"],
                    "direction": live.get("direction") or tr["direction"],
                    "origin": live.get("train", {}).get("start") or tr["start"],
                    "destination": live.get("train", {}).get("dest") or tr["dest"],
                    "board_dt": board_dt or now, "alight_dt": now,
                    "wait_seconds": 0, "ride_seconds": 0,
                    "remaining_seconds": 0, "delay_seconds": round(live["delay"]),
                    "current_station": live["current_station"],
                    "location_kind": "live",
                    "location_label": f"{live['current_station']} {live['status']}",
                    "confidence": "높음", "method": "탑승 열차 연속운행 추적" if tr.get("physical_continuation") else "탑승 열차 실시간 고정 추적",
                    "projected": False, "tracking": True, "arrived": True,
                    "status": live["status"],
                },
                "diagnostics": diag,
            }

        if ci is not None:
            ref = scheduled_reference_at(stops, ci, live["raw"].get("trainSttus"))
            asec = target_sec
            if ref is not None:
                while asec < ref:
                    asec += 86400
                age = max(0, (now - live["observed"]).total_seconds())
                remaining = asec - ref - age

                # 24시간 wrap/비정상 상태에 대한 마지막 안전장치.
                if remaining > 6 * 3600:
                    fallback_target = nearest_schedule_dt(target_sec, now, live["delay"])
                    alight_dt = max(now, fallback_target)
                else:
                    alight_dt = now + timedelta(seconds=max(0, remaining))

                shown_board = board_dt or now
                if shown_board > alight_dt:
                    shown_board = alight_dt
                return {
                    "ok": True,
                    "arrived": False,
                    "chosen": {
                        "line": line, "from": canon_station(start), "to": canon_station(end),
                        "train_no": live.get("train_no"),
                        "continuation_train_no": tr.get("continuation_train_no", ""),
                        "physical_continuation": bool(tr.get("physical_continuation")),
                        "service": tr["service"],
                        "direction": live.get("direction") or tr["direction"],
                        "origin": live.get("train", {}).get("start") or tr["start"],
                        "destination": live.get("train", {}).get("dest") or tr["dest"],
                        "board_dt": shown_board, "alight_dt": alight_dt,
                        "wait_seconds": 0,
                        "ride_seconds": max(0, round((alight_dt - shown_board).total_seconds())),
                        "remaining_seconds": max(0, round((alight_dt - now).total_seconds())),
                        "delay_seconds": round(live["delay"]),
                        "current_station": live["current_station"],
                        "location_kind": "live",
                        "location_label": f"{live['current_station']} {live['status']}",
                        "confidence": "높음",
                        "method": "탑승 열차 연속운행 추적" if tr.get("physical_continuation") else "탑승 열차 실시간 고정 추적",
                        "projected": False, "tracking": True, "arrived": False,
                        "status": live["status"],
                        "data_age_seconds": max(0, round((now - live["observed"]).total_seconds())),
                    },
                    "diagnostics": diag,
                }

    # API에서 순간적으로 열차가 사라져도 잠금을 해제하지 않는다.
    # 공식 시간표 + 현재 같은 방향/등급 열차 지연 중앙값으로 임시 유지.
    fallback_direction = tr.get("direction", "")
    if tr.get("physical_continuation") and active_train_no_for_virtual(tr, now, 0) == tr.get("continuation_train_no"):
        fallback_direction = tr.get("continuation_direction") or fallback_direction
    delay = median_delay(observations, fallback_direction, tr["service"])
    expected_location = estimated_train_location(tr, now, delay)
    target_dt = nearest_schedule_dt(target_sec, now, delay)
    if target_dt < now - timedelta(minutes=5):
        target_dt = now
    if target_dt > now + timedelta(hours=6):
        target_dt = now

    return {
        "ok": True,
        "arrived": False,
        "chosen": {
            "line": line, "from": canon_station(start), "to": canon_station(end),
            "train_no": active_train_no_for_virtual(tr, now, delay),
            "continuation_train_no": tr.get("continuation_train_no", ""),
            "physical_continuation": bool(tr.get("physical_continuation")),
            "service": tr["service"],
            "direction": fallback_direction, "origin": tr["start"], "destination": tr["dest"],
            "board_dt": board_dt or now, "alight_dt": target_dt,
            "wait_seconds": 0,
            "ride_seconds": max(0, round((target_dt - (board_dt or now)).total_seconds())),
            "remaining_seconds": max(0, round((target_dt - now).total_seconds())),
            "delay_seconds": round(delay),
            "current_station": expected_location.get("station", ""),
            "location_kind": "expected",
            "location_label": expected_location.get("label", "예상 소재 계산 중"),
            "confidence": "중간" if observations else "낮음",
            "method": (
                ("연속운행 열차 잠금 · 실시간 위치 재포착 대기" if tr.get("physical_continuation") else "탑승 열차 잠금 · 실시간 위치 재포착 대기")
                if realtime_available
                else "공식 시간표 기반 임시 추적 · 실시간 위치 조회 실패"
            ),
            "projected": True, "tracking": True, "arrived": False,
            "status": expected_location.get("status", "위치 재포착 대기"),
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

    # V13.2.2 regression fix:
    # calculate_live_trip()에는 position_cache 인자가 없으므로
    # 추적 요청마다 필요한 노선을 한 번 병렬 prefetch하여 공유한다.
    position_cache = prefetch_position_cache(
        [s.get("line") for s in segments],
        timeout=5,
    )
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
    current["transfer_info"] = active.get("transfer_info")
    current["transfer_seconds"] = int(active.get("transfer_seconds") or round(float(active.get("transfer_walk") or 0) * 60))
    results.append(current)

    # 2) 현재 열차의 최신 도착 ETA + 환승시간을 다음 구간 ready 시각으로 사용.
    ready_dt = current["alight_dt"]
    if active_index < len(segments) - 1:
        ready_dt += timedelta(seconds=max(0, int(active.get("transfer_seconds") or round(float(active.get("transfer_walk") or 0) * 60))))

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
        chosen["previous_candidate"] = r.get("previous_candidate")
        chosen["transfer_info"] = s.get("transfer_info")
        chosen["transfer_seconds"] = int(s.get("transfer_seconds") or round(float(s.get("transfer_walk") or 0) * 60))
        results.append(chosen)

        if chosen["confidence"] != "높음":
            warnings.append(
                f"{idx+1}구간 {line} {fr}→{to}: {chosen['method']} ({chosen['confidence']} 신뢰도)"
            )

        if idx < len(segments) - 1:
            ready_dt = chosen["alight_dt"] + timedelta(
                seconds=max(0, int(s.get("transfer_seconds") or round(float(s.get("transfer_walk") or 0) * 60)))
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
        "boarded_train_no": current.get("train_no") or boarded_train_no,
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

def calculate_route(payload, position_cache=None):
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

    # 수동 새로고침에서는 사용자가 처음 정한 플랫폼 도착 예정시각 자체는
    # 고정한다. 다만 그 시각이 이미 지났다면 이미 지나간 열차가 다시
    # 추천되지 않도록 첫 구간의 실제 탑승 가능 기준만 현재 시각으로 올린다.
    refresh_only = bool(payload.get("refresh_only"))
    ready_dt = max(start_dt, now) if refresh_only else start_dt

    # V13.2.2: caller가 넘긴 cache를 절대 초기화하지 않는다.
    # V13/V13.1에서는 여기서 position_cache={}로 덮어써 다중 후보마다
    # realtimePosition을 다시 순차 호출하는 중대 성능 버그가 있었다.
    if position_cache is None:
        position_cache = prefetch_position_cache([s.get("line") for s in segments], timeout=5)

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
        chosen["previous_candidate"] = r.get("previous_candidate")
        chosen["transfer_info"] = s.get("transfer_info")
        chosen["transfer_seconds"] = int(s.get("transfer_seconds") or round(float(s.get("transfer_walk") or 0) * 60))
        results.append(chosen)

        if chosen["confidence"] != "높음":
            warnings.append(
                f"{idx+1}구간 {line} {fr}→{to}: {chosen['method']} ({chosen['confidence']} 신뢰도)"
            )

        transfer_seconds_value = max(0, int(s.get("transfer_seconds") or round(float(s.get("transfer_walk") or 0) * 60)))
        if idx < len(segments) - 1:
            ready_dt = chosen["alight_dt"] + timedelta(seconds=transfer_seconds_value)
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
        "calculated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "refresh_only": refresh_only,
        "arrival_time": end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "total_seconds": total_seconds,
        "baseline_minutes": baseline,
        "difference_seconds": None if baseline is None else round(total_seconds - baseline * 60),
        "segments": [serialize_seg(x) for x in results],
        "warnings": warnings,
    }

