# -*- coding: utf-8 -*-
"""
지금타 V14.9.4 — 1호선 급행-완행 추월 운행 보정
핵심:
  1호선: 서울시 realtimePosition + 사용자가 제공한 코레일 공식 평/휴일 시간표
  2~9호선: 서울시 realtimePosition + 서울교통공사 공식 열차운행시각표(250930)
  신분당선: 서울시 realtimePosition(subwayId 1077)의 차량 식별값 + 운영사 공식 역간 소요시간 + 공개 역별 시각표
  신분당선 API trainNo 필드는 공개 열차번호가 아닌 차량 식별자로 취급하며, DXxxxx 형태의 과거 가상 열번은 사용하지 않는다.
  기타 실시간 지원 노선: 현재열차 trainNo -> 시간표 trainNo 매칭 -> 현재 지연 -> 향후 역 ETA
"""
import json, os, re, urllib.request, urllib.parse, time, statistics, heapq
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict

import realtime_store

BASE = Path(__file__).resolve().parent
APP_VERSION = "V14.9.4"
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
SINBUNDANG = load_json("sinbundang_public_timetable.json")
SINBUNDANG_RUNTIME = load_json("sinbundang_runtime.json")
SINBUNDANG_VIRTUAL = load_json("sinbundang_virtual_runs.json")
ADDITIONAL = load_json("additional_lines_schedule.json")
KR_HOLIDAYS = load_json("kr_holidays_2026_2035.json")
ROUTE_GRAPH = load_json("route_graph.json")
TRANSFER_DATA = load_json("transfer_data.json")

KORAIL_EXTRA_LINES = ("경의중앙선", "수인분당선", "경춘선", "경강선", "서해선", "공항철도")
TIMETABLE_ADDITIONAL_LINES = tuple(ADDITIONAL.get("lines", {}).keys())
REALTIME_ADDITIONAL_LINES = tuple(
    line for line in TIMETABLE_ADDITIONAL_LINES
    if bool(ADDITIONAL["lines"][line].get("capabilities", {}).get("realtime"))
)
SCHEDULE_ONLY_LINES = {
    line for line in TIMETABLE_ADDITIONAL_LINES
    if not bool(ADDITIONAL["lines"][line].get("capabilities", {}).get("realtime"))
}
EXTRA_LINES = KORAIL_EXTRA_LINES + ("신분당선",) + TIMETABLE_ADDITIONAL_LINES

LINE_ALIASES = {
    "김포도시철도": "김포골드라인",
    "김포경전철": "김포골드라인",
    "용인에버라인": "용인경전철",
    "에버라인": "용인경전철",
}
def canon_line(v):
    raw = str(v or "").strip()
    return LINE_ALIASES.get(raw, raw)

LINE_NAMES = [f"{i}호선" for i in range(1, 10)] + list(dict.fromkeys(EXTRA_LINES))
LINE_NUM = {f"{i}호선": str(i) for i in range(1, 10)}
LINE_IDS = {f"{i}호선": f"100{i}" for i in range(1, 10)}
LINE_IDS.update({
    "경의중앙선": "1063", "수인분당선": "1075", "경춘선": "1067",
    "경강선": "1081", "서해선": "1093", "공항철도": "1065", "신분당선": "1077",
    "우이신설선": "1092", "GTX-A": "1032",
})
# 서울시 문서에는 신림선 subwayId가 아직 명시되지 않은 경우가 있으므로
# direct 조회는 노선명으로 하고, Redis key는 별도의 내부 stable id를 사용한다.
REALTIME_STORE_IDS = dict(LINE_IDS)
REALTIME_STORE_IDS["신림선"] = "SILLIM"

def line_capabilities(line):
    line = canon_line(line)
    if line == "신분당선":
        return {"realtime": True, "public_train_no": False, "data_status": "complete"}
    if line in ADDITIONAL.get("lines", {}):
        return dict(ADDITIONAL["lines"][line].get("capabilities", {}))
    return {"realtime": line not in SCHEDULE_ONLY_LINES, "public_train_no": True, "data_status": "complete"}

def public_train_no_supported(line):
    return bool(line_capabilities(line).get("public_train_no", True))

def realtime_supported(line):
    return bool(line_capabilities(line).get("realtime", line not in SCHEDULE_ONLY_LINES))

# 서울시 realtimePosition의 노선 요청 문자열.
# 신분당선은 운영 중인 API에서 `1077:신분당선` 식별 문자열로 조회되는 경우가 있어
# 이를 1순위로 사용하고, 호선명 단독 호출을 호환 fallback으로 남긴다.
REALTIME_QUERY_ALIASES = {
    "신분당선": ("1077:신분당선", "신분당선"),
}

# API 수신 간격이 긴 코레일 계열에서, 한 번 확인된 열차 지연이
# 다음 조회에서 열차 미포착만으로 0분으로 리셋되는 것을 막는다.
# 브라우저가 최근 exact-train 관측을 전달하면 최대 35분까지만 보조 신호로 사용한다.
STALE_DELAY_CACHE_LINES = {
    "1호선", "경의중앙선", "수인분당선", "경춘선", "경강선", "서해선",
    *REALTIME_ADDITIONAL_LINES,
}
STALE_DELAY_HOLD_SECONDS = 20 * 60
STALE_DELAY_MAX_SECONDS = 35 * 60

def _client_delay_rows(payload):
    rows = payload.get("train_delay_cache") if isinstance(payload, dict) else None
    return rows if isinstance(rows, list) else []

def _merge_delay_cache_rows(*groups):
    """서버 Redis/클라이언트 캐시를 열차별 최신 관측 하나로 병합한다."""
    merged = {}
    for rows in groups:
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            key = (str(row.get("line") or ""), norm_train(row.get("train_no")))
            if not all(key):
                continue
            old = merged.get(key)
            if old is None or str(row.get("observed_at") or "") >= str(old.get("observed_at") or ""):
                merged[key] = row
    return sorted(merged.values(), key=lambda x: str(x.get("observed_at") or ""), reverse=True)[:160]

def attach_client_delay_cache(position_cache, payload):
    if not isinstance(position_cache, dict):
        return position_cache
    client_rows = _client_delay_rows(payload)
    if client_rows:
        position_cache["__train_delay_cache__"] = _merge_delay_cache_rows(
            position_cache.get("__train_delay_cache__", []), client_rows
        )
    return position_cache

def cached_delay_rows(position_cache):
    if not isinstance(position_cache, dict):
        return []
    rows = position_cache.get("__train_delay_cache__")
    return rows if isinstance(rows, list) else []

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
    # 신분당선 API/운영사 병기 역명 호환
    "양재(서초구청)": "양재",
    "양재시민의숲(매헌)": "양재시민의숲",
    "판교(판교테크노밸리)": "판교",
    "미금(분당서울대병원)": "미금",
    "광교중앙(아주대)": "광교중앙",
    "광교(경기대)": "광교",
    # 추가 노선 Rail.Blue/API 표기 호환
    "SR동탄": "동탄",
    "SR구성": "구성",
    "SR성남": "성남",
    # 2026 인천 행정구역/역명 변경 전 호환
    "서구청": "서해구청",
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


def _derive_s1_passenger_stations():
    """
    1호선 원본 역목록에는 여객역이 아닌 운전상 지점(현재 데이터의 '마전')도 섞여 있다.
    전체 평/휴일 DIA에서 한 번도 도착시각이 없고 시종착역으로도 쓰이지 않는 지점은
    승하차 대상에서 제외한다. 나머지 역만 통과/정차 판정에 사용한다.
    """
    arrived = set()
    terminal = set()
    seen = set()
    for trains in S1.values():
        for tr in trains.values():
            stops = tr.get("stops", [])
            for index, stop in enumerate(stops):
                station = canon_station(stop.get("station"))
                if not station:
                    continue
                seen.add(station)
                if stop.get("arr") is not None:
                    arrived.add(station)
                if index in (0, len(stops) - 1):
                    terminal.add(station)
    operational_only = seen - arrived - terminal
    return {canon_station(station) for station in S1_STATIONS} - operational_only


S1_PASSENGER_STATIONS = _derive_s1_passenger_stations()

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

def schedule_dt_before(sched_sec, ready_dt, delay=0, window_seconds=None):
    """
    ready_dt보다 먼저 출발한 해당 시간표 시각의 가장 최근 발생시각을 반환한다.

    과거에는 기본 10분(window_seconds=600) 제한이 있어 경의중앙선처럼
    배차간격이 긴 노선에서 실제 직전 열차가 누락됐다. '직전 열차'는
    배차간격과 무관하게 바로 앞선 운행이어야 하므로 기본값은 제한 없음이다.
    필요할 때만 호출자가 명시적으로 window_seconds를 줄 수 있다.
    """
    midnight = ready_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    vals = []
    for d in (-2, -1, 0, 1):
        dt = midnight + timedelta(days=d, seconds=sched_sec + delay)
        gap = (ready_dt - dt).total_seconds()
        if gap <= 5:
            continue
        if window_seconds is not None and gap > window_seconds:
            continue
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
def _is_internal_pass_time(stop, index, total_stops):
    """중간역에서 도착시각 없이 통과시각만 기록된 DIA 행인지 판정한다."""
    return (
        0 < index < total_stops - 1
        and stop.get("arr") is None
        and stop.get("dep") is not None
    )


def _normalized_stop_call(stop, index, total_stops, passenger_stations=None):
    """
    승하차 가능 여부를 시간표 구조 자체로 정규화한다.

    - 원본 call=false는 항상 유지한다.
    - passenger_stations가 주어진 노선에서 여객역 목록에 없는 운전상 지점은 승하차 불가다.
    - 중간 여객역의 arr=None + dep=통과시각은 service/열차번호와 무관하게 통과로 본다.
      시발역은 arr=None이 정상일 수 있으므로 첫 역에는 이 보정을 적용하지 않는다.

    이 규칙으로 특정 열차번호(K19xx/K57xx/K64xx...) 하드코딩에 의존하지 않는다.
    """
    if not bool(stop.get("call", True)):
        return False
    station = canon_station(stop.get("station"))
    if passenger_stations is not None and station not in passenger_stations:
        return False
    if _is_internal_pass_time(stop, index, total_stops):
        return False
    return True


def _normalized_service(raw_service, stops, passenger_stations=None, force_express=False):
    """여객역 스킵이 존재하면 원본 표기가 local이어도 급행으로 분류한다."""
    if force_express or str(raw_service or "local").lower() == "express":
        return "express"
    total = len(stops)
    for index, stop in enumerate(stops):
        station = canon_station(stop.get("station"))
        if passenger_stations is not None and station not in passenger_stations:
            continue
        if _is_internal_pass_time(stop, index, total):
            return "express"
    return "local"


def normalize_s1_train(tn, tr):
    normalized_tn = norm_train(tn)
    raw_stops = tr.get("stops", [])
    # K19xx는 기존 사용자 제공 규칙을 fallback으로 보존하되,
    # 실제 승하차 판정은 아래의 구조 기반 규칙으로 수행한다.
    service = _normalized_service(
        tr.get("service", "local"),
        raw_stops,
        passenger_stations=S1_PASSENGER_STATIONS,
        force_express=bool(re.fullmatch(r"K19\d{2}", normalized_tn)),
    )
    return {
        "train_no": tn,
        "direction": tr.get("direction", ""),
        "service": service,
        "start": canon_station(tr.get("start", "")),
        "dest": canon_station(tr.get("dest", "")),
        "stops": [{
            "station": canon_station(stop.get("station")),
            "arr": stop.get("arr"),
            "dep": stop.get("dep"),
            "call": _normalized_stop_call(
                stop,
                index,
                len(raw_stops),
                passenger_stations=S1_PASSENGER_STATIONS,
            ),
        } for index, stop in enumerate(raw_stops)],
    }


def normalize_extra_train(line, tn, tr):
    raw_stops = tr.get("stops", [])
    passenger_stations = {
        canon_station(station)
        for station in EXTRA.get(line, {}).get("stations", [])
    }
    service = _normalized_service(
        tr.get("service", "local"),
        raw_stops,
        passenger_stations=passenger_stations,
    )
    return {
        "train_no": tn,
        "direction": tr.get("direction", ""),
        "service": service,
        "start": canon_station(tr.get("start", "")),
        "dest": canon_station(tr.get("dest", "")),
        "linked_train_no": norm_train(tr.get("linked_train_no", "")),
        "stops": [{
            "station": canon_station(stop.get("station")),
            "arr": stop.get("arr"),
            "dep": stop.get("dep"),
            "call": _normalized_stop_call(
                stop,
                index,
                len(raw_stops),
                passenger_stations=passenger_stations,
            ),
        } for index, stop in enumerate(raw_stops)],
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


def normalize_additional_train(line, tn, tr):
    """Rail.Blue 추가 노선을 기존 공통 열차 모델로 변환한다."""
    source_no = str(tr.get("source_train_no") or tn)
    stops = [{
        "station": canon_station(stop.get("station")),
        "arr": stop.get("arr"),
        "dep": stop.get("dep"),
        "call": bool(stop.get("call", True)),
        **({"estimated": True} if stop.get("estimated") else {}),
        **({"source_resolution_seconds": stop.get("source_resolution_seconds")} if stop.get("source_resolution_seconds") else {}),
    } for stop in tr.get("stops", [])]
    passenger_stations = {
        canon_station(x)
        for x in ADDITIONAL.get("lines", {}).get(line, {}).get("stations", [])
    }
    service = str(tr.get("service") or "local")
    # 추가 노선 자료의 pass 행은 arr/dep에 통과시각을 같이 넣어 보존하므로
    # call=false인 실제 여객역이 중간에 있으면 급행/통과 운행으로 분류한다.
    if service != "express":
        for stop in stops[1:-1]:
            if stop.get("station") in passenger_stations and not stop.get("call", True):
                service = "express"
                break
    return {
        "train_no": source_no,
        "internal_train_no": str(tn),
        "direction": str(tr.get("direction") or ""),
        "service": service,
        "start": canon_station(tr.get("start", "")),
        "dest": canon_station(tr.get("dest", "")),
        "segment": str(tr.get("segment") or ""),
        "railblue_train_id": str(tr.get("railblue_train_id") or ""),
        "stops": stops,
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

ADDITIONAL_EXACT = {}
ADDITIONAL_NUM = {}
for line, source in ADDITIONAL.get("lines", {}).items():
    for day, trains in source.get("trains", {}).items():
        exact = defaultdict(list)
        nums = defaultdict(list)
        for tn, tr in trains.items():
            identifiers = {
                norm_train(tn),
                norm_train(tr.get("source_train_no", "")),
                norm_train(tr.get("railblue_train_id", "")),
            }
            identifiers.discard("")
            for identifier in identifiers:
                exact[identifier].append(tn)
                nums[train_digits(identifier)].append(tn)
        ADDITIONAL_EXACT[(line, day)] = exact
        ADDITIONAL_NUM[(line, day)] = nums


def get_train(line, mode, raw_train_no):
    line = canon_line(line)
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

    if line == "신분당선":
        # 공개 열차번호가 없으므로 get_train은 내부 시간표 slot_id에 대해서만 동작한다.
        # 서울시 API의 trainNo(차량 식별자)는 여기에서 시간표 열번으로 매칭하지 않는다.
        for tr in all_trains(line, mode):
            if norm_train(tr.get("train_no")) == n:
                return tr
        return None

    if line in KORAIL_EXTRA_LINES:
        trains = EXTRA[line]["trains"][korail_day]

        # 1) actual train number in the timetable always wins.
        if n in trains:
            return normalize_extra_train(line, n, trains[n])

        # 2) digit-only fallback for API formatting differences.
        c = EXTRA_NUM.get((line, korail_day), {}).get(digits, [])
        if len(c) == 1:
            return normalize_extra_train(line, c[0], trains[c[0]])

        # 3) DIA '연계열번' fallback. Only used if no actual train number matched.
        c = EXTRA_ALIAS.get((line, korail_day), {}).get(n, [])
        if len(c) == 1:
            return normalize_extra_train(line, c[0], trains[c[0]])
        c = EXTRA_ALIAS_NUM.get((line, korail_day), {}).get(digits, [])
        if len(c) == 1:
            return normalize_extra_train(line, c[0], trains[c[0]])
        return None

    if line in TIMETABLE_ADDITIONAL_LINES:
        trains = ADDITIONAL["lines"][line]["trains"][korail_day]
        exact = ADDITIONAL_EXACT.get((line, korail_day), {}).get(n, [])
        if len(set(exact)) == 1:
            key = exact[0]
            return normalize_additional_train(line, key, trains[key])
        c = ADDITIONAL_NUM.get((line, korail_day), {}).get(digits, [])
        unique = list(dict.fromkeys(c))
        if len(unique) == 1:
            key = unique[0]
            return normalize_additional_train(line, key, trains[key])
        return None

    num = LINE_NUM[line]
    trains = OFF["days"].get(metro_week, {}).get(num, {})
    if n in trains:
        return normalize_metro_train(n, trains[n])
    c = METRO_NUM.get((metro_week, num), {}).get(digits, [])
    if len(c) == 1:
        return normalize_metro_train(c[0], trains[c[0]])
    return None


def _sinbundang_route_templates():
    """시간표 열번과 무관한 노선 토폴로지용 내부 템플릿 2개."""
    stations=[canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations", [])]
    out=[]
    for direction, seq in (("DOWN", stations), ("UP", list(reversed(stations)))):
        stops=[]; t=0
        for i, st in enumerate(seq):
            if i==0:
                stops.append({"station":st,"arr":None,"dep":0,"call":True})
                continue
            prev=seq[i-1]
            edge=int(SINBUNDANG_RUNTIME.get("edge_seconds",{}).get(direction,{}).get(f"{prev}>{st}",0))
            t += edge
            if i==len(seq)-1:
                stops.append({"station":st,"arr":t,"dep":None,"call":True})
            else:
                stops.append({"station":st,"arr":t,"dep":t+30,"call":True})
                t += 30
        out.append({
            "train_no":f"@SBW-{direction}","direction":direction,"service":"local",
            "start":seq[0],"dest":seq[-1],"stops":stops,"internal_route_template":True,
        })
    return tuple(out)


@lru_cache(maxsize=None)
def all_trains(line, mode):
    """정규화된 노선별 전체 시간표는 프로세스 생명주기 동안 재사용한다."""
    line = canon_line(line)
    korail_day, metro_week = choose_modes(mode)
    if line == "1호선":
        return tuple(normalize_s1_train(tn, tr) for tn, tr in S1[korail_day].items())
    if line == "신분당선":
        return _sinbundang_route_templates()
    if line in KORAIL_EXTRA_LINES:
        return tuple(
            normalize_extra_train(line, tn, tr)
            for tn, tr in EXTRA[line]["trains"][korail_day].items()
        )
    if line in TIMETABLE_ADDITIONAL_LINES:
        return tuple(
            normalize_additional_train(line, tn, tr)
            for tn, tr in ADDITIONAL["lines"][line]["trains"][korail_day].items()
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
    # 운전상 지점은 UI 출발/도착역 후보에서도 제외한다.
    s1 = sorted(S1_PASSENGER_STATIONS)
    out = {"1호선": s1}
    for i in range(2, 10):
        num = str(i)
        out[f"{i}호선"] = sorted({canon_station(x) for x in OFF["stations"].get(num, [])})
    for line in KORAIL_EXTRA_LINES:
        out[line] = sorted({canon_station(x) for x in EXTRA[line].get("stations", [])})
    out["신분당선"] = list(SINBUNDANG.get("stations", []))
    for line in TIMETABLE_ADDITIONAL_LINES:
        out[line] = sorted({
            canon_station(x)
            for x in ADDITIONAL["lines"][line].get("stations", [])
        })
    return out


STATIONS_BY_LINE = station_options()


def timetable_integrity_report():
    """로드된 원본 시간표가 구조 기반 승하차 규칙과 일치하는지 요약한다."""
    issues = []

    def inspect(line, day, trains, passenger_stations, force_express_pattern=None):
        for train_no, tr in trains.items():
            raw_service = str(tr.get("service", "local")).lower()
            stops = tr.get("stops", [])
            skipped_passenger = []
            for index, stop in enumerate(stops):
                station = canon_station(stop.get("station"))
                raw_call = bool(stop.get("call", True))
                if station not in passenger_stations:
                    if raw_call:
                        issues.append({
                            "line": line, "day": day, "train_no": train_no,
                            "kind": "operational_point_callable", "station": station,
                        })
                    continue
                if _is_internal_pass_time(stop, index, len(stops)):
                    skipped_passenger.append(station)
                    if raw_call:
                        issues.append({
                            "line": line, "day": day, "train_no": train_no,
                            "kind": "passenger_pass_callable", "station": station,
                        })
            force_express = bool(force_express_pattern and re.fullmatch(force_express_pattern, norm_train(train_no)))
            if (skipped_passenger or force_express) and raw_service != "express":
                issues.append({
                    "line": line, "day": day, "train_no": train_no,
                    "kind": "express_mislabeled_local",
                    "skipped_station_count": len(skipped_passenger),
                })

    for day, trains in S1.items():
        inspect("1호선", day, trains, S1_PASSENGER_STATIONS, r"K19\d{2}")
    for line in KORAIL_EXTRA_LINES:
        passenger = {canon_station(x) for x in EXTRA[line].get("stations", [])}
        for day, trains in EXTRA[line].get("trains", {}).items():
            inspect(line, day, trains, passenger)

    # 신분당선 생산 데이터에는 legacy DX 열번이 어떤 형태로도 남아 있으면 안 된다.
    if "DX" in json.dumps(SINBUNDANG.get("departures", {}), ensure_ascii=False).upper():
        issues.append({"line":"신분당선","day":"all","train_no":"","kind":"legacy_dx_identifier_present"})
    if "DX" in json.dumps(SINBUNDANG_VIRTUAL.get("runs", {}), ensure_ascii=False).upper():
        issues.append({"line":"신분당선","day":"all","train_no":"","kind":"legacy_dx_identifier_in_virtual_runs"})
    for day, expected_count in (("weekday", 326), ("holiday", 272)):
        actual_count = len(SINBUNDANG_VIRTUAL.get("runs", {}).get(day, {}))
        if actual_count != expected_count:
            issues.append({"line":"신분당선","day":day,"train_no":"","kind":"virtual_run_count_mismatch","actual":actual_count,"expected":expected_count})
    stations = SINBUNDANG_RUNTIME.get("stations", [])
    dwell = 30
    for direction, expected in (("DOWN", 2522), ("UP", 2514)):
        total = 0
        if direction == "DOWN":
            pairs = zip(stations, stations[1:])
        else:
            pairs = zip(reversed(stations), reversed(stations[:-1]))
        for a, b in pairs:
            total += int(SINBUNDANG_RUNTIME.get("edge_seconds", {}).get(direction, {}).get(f"{a}>{b}", 0))
        total += max(0, len(stations)-2) * dwell
        if total != expected:
            issues.append({"line":"신분당선","day":"all","train_no":"","kind":"runtime_matrix_total_mismatch","direction":direction,"total":total,"expected":expected})

    by_kind = defaultdict(int)
    by_line = defaultdict(int)
    for issue in issues:
        by_kind[issue["kind"]] += 1
        by_line[issue["line"]] += 1
    return {
        "ok": not issues,
        "issue_count": len(issues),
        "by_kind": dict(sorted(by_kind.items())),
        "by_line": dict(sorted(by_line.items())),
        "examples": issues[:20],
    }


# ---------- Transfer data ----------
# 서로 다른 노선명이지만 동일 선로/승강장을 공유하는 구간.
# 같은 진행방향으로 갈아타는 경우에는 별도의 환승 보행시간이 없다고 본다.
# (다음 열차 대기시간은 각 노선 시간표 계산에서 별도로 반영된다.)
SHARED_TRACK_CORRIDORS = {
    frozenset(("서해선", "경의중앙선")): ("일산", "풍산", "백마", "곡산", "대곡", "능곡"),
    frozenset(("4호선", "수인분당선")): ("한대앞", "중앙", "고잔", "초지", "안산", "능길", "정왕", "오이도"),
}

def _shared_track_corridor(station, from_line, to_line):
    corridor = SHARED_TRACK_CORRIDORS.get(frozenset((from_line, to_line)))
    if not corridor:
        return None
    canonical = tuple(canon_station(x) for x in corridor)
    return canonical if canon_station(station) in canonical else None

def _shared_track_pair(station, from_line, to_line):
    return _shared_track_corridor(station, from_line, to_line) is not None

def _corridor_motion(corridor, station, other_station, incoming):
    """공유선로의 물리적 진행방향을 -1/+1로 반환한다.

    incoming=True이면 other_station→station, False이면 station→other_station 이동이다.
    공유구간 끝역에서 타 노선 가지선으로 이어지는 경우도 같은 축의 연장으로 취급한다.
    """
    st = canon_station(station); other = canon_station(other_station)
    try:
        i = corridor.index(st)
    except ValueError:
        return 0
    if other in corridor:
        j = corridor.index(other)
        delta = (i - j) if incoming else (j - i)
        return 1 if delta > 0 else -1 if delta < 0 else 0
    # 공유구간 바깥 역은 끝역에서 이어지는 가지선으로만 해석한다.
    if i == 0:
        return 1 if incoming else -1
    if i == len(corridor) - 1:
        return -1 if incoming else 1
    return 0

def _shared_track_same_direction(station, from_seg, to_seg):
    corridor = _shared_track_corridor(station, from_seg.get("line"), to_seg.get("line"))
    if not corridor:
        return False
    incoming = _corridor_motion(corridor, station, from_seg.get("from"), incoming=True)
    outgoing = _corridor_motion(corridor, station, to_seg.get("to"), incoming=False)
    return bool(incoming and outgoing and incoming == outgoing)

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

def _first_not_none(*values):
    for value in values:
        if value is not None:
            return value
    return None

def _segment_transfer_seconds(seg):
    raw = seg.get("transfer_seconds")
    if raw is not None:
        return max(0, int(raw))
    return max(0, int(round(float(seg.get("transfer_walk") or 0) * 60)))

def transfer_seconds(station, from_line, to_line):
    # 자동 경로 후보 생성 단계에는 진행방향이 아직 확정되지 않는다.
    # 공유선로 구간은 같은 방향 환승이 0초일 수 있으므로 대표 penalty를 0으로 둬
    # 후보 자체가 과도한 환승비용 때문에 탈락하지 않게 한다. 실제 ETA 재계산 때
    # best_transfer_detail()이 방향을 보고 정확한 값을 적용한다.
    if _shared_track_pair(station, from_line, to_line):
        return 0
    p = transfer_pair_info(station, from_line, to_line)
    if p:
        raw = _first_not_none(
            p.get("default_seconds"),
            p.get("distance_seconds"),
            DEFAULT_TRANSFER_SECONDS,
        )
        return max(0, int(raw))
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
            # 운전상 지점/비여객 통과점(call=false)은 환승 방향 힌트가 아니다.
            # 예: 신분당선 판교주박기지. 실제 다음 여객 정차역까지 진행한다.
            if not bool(stops[j].get("call", True)):
                continue
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
            # 운전상 지점/비여객 통과점은 승객이 인식하는 운행방향이 아니므로 건너뛴다.
            if not bool(stops[j].get("call", True)):
                continue
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
    # direction_strict records come from public fast-transfer data that only covers
    # the explicitly published direction combination. Do not leak that position to
    # an unlisted opposite direction through the historical one-side fallback.
    fallback_records = [r for r in records if not bool(r.get("direction_strict"))]
    one_out = [r for r in fallback_records if canon_dir(r.get("to_direction")) == canon_station(out_dir)]
    one_in = [r for r in fallback_records if canon_dir(r.get("from_direction")) == canon_station(in_dir)]
    chosen = (both or one_out or one_in or fallback_records or [None])[0]
    matched = "direction" if both else "outgoing" if one_out else "incoming" if one_in else "pair" if chosen else "fallback"
    sec_raw = _first_not_none(
        (chosen or {}).get("seconds"),
        p.get("default_seconds"),
        DEFAULT_TRANSFER_SECONDS,
    )
    sec = max(0, int(sec_raw))
    shared_same_direction = _shared_track_same_direction(station, from_seg, to_seg)
    if shared_same_direction:
        sec = 0
        matched = "shared_track_same_direction"
    def pos(car, door):
        car=str(car or "").strip(); door=str(door or "").strip()
        return f"{car}-{door}" if car and door else car or door
    return {
        "station": canon_station(station),
        "seconds": sec,
        "distance_m": p.get("distance_m"),
        "alight_position": "" if shared_same_direction else pos((chosen or {}).get("alight_car"), (chosen or {}).get("alight_door")),
        "board_position": "" if shared_same_direction else pos((chosen or {}).get("board_car"), (chosen or {}).get("board_door")),
        "from_direction": (chosen or {}).get("from_direction") or in_dir,
        "to_direction": (chosen or {}).get("to_direction") or out_dir,
        "matched": matched,
        "shared_track": bool(shared_same_direction),
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
DEFAULT_TRANSFER_SECONDS = int(ROUTE_GRAPH.get("meta", {}).get("default_transfer_seconds", 180))
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
    attach_client_delay_cache(schedule_only_cache, payload)

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
    attach_client_delay_cache(live_cache, payload)
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
def realtime_query_candidates(line):
    values = REALTIME_QUERY_ALIASES.get(line) or (line,)
    # 중복 제거하면서 선언 순서 유지.
    return tuple(dict.fromkeys(str(x).strip() for x in values if str(x).strip()))

def fetch_position(line, timeout=5):
    require_api_key()
    last_error = None
    last_data = None
    candidates = realtime_query_candidates(line)
    for query_value in candidates:
        q = urllib.parse.quote(query_value, safe="")
        url = f"http://swopenAPI.seoul.go.kr/api/subway/{API_KEY}/json/realtimePosition/0/300/{q}"
        req = urllib.request.Request(url, headers={
            "User-Agent": f"JigeumTa-{APP_VERSION}/1.0",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
            last_data = data
            if isinstance(data, dict) and isinstance(data.get("RESULT"), dict):
                last_error = data["RESULT"]
                continue
            rows = data.get("realtimePositionList", []) if isinstance(data, dict) else []
            if rows or len(candidates) == 1:
                if isinstance(data, dict):
                    data = dict(data)
                    data["_jigeumta_query"] = query_value
                return True, None, data
            # 정상 응답이지만 0건이면 다음 호환 query를 시도한다.
            last_error = {"message": f"{query_value} 실시간 위치 0건"}
        except Exception as e:
            last_error = {"message": f"{type(e).__name__}: {e}"}
    return False, last_error or {"message": "실시간 위치 조회 실패"}, last_data



def _sinbundang_day_key(mode):
    korail_day, _ = choose_modes(mode)
    return korail_day


def sinbundang_direction(start, end):
    stations = [canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations", [])]
    a, b = canon_station(start), canon_station(end)
    if a not in stations or b not in stations or a == b:
        return ""
    return "DOWN" if stations.index(a) < stations.index(b) else "UP"


def sinbundang_runtime_seconds(start, end):
    """운영사 공식 역간 소요시간 + 중간역 30초 정차를 합산한다."""
    stations = [canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations", [])]
    a, b = canon_station(start), canon_station(end)
    if a not in stations or b not in stations or a == b:
        return None
    ai, bi = stations.index(a), stations.index(b)
    direction = "DOWN" if ai < bi else "UP"
    step = 1 if direction == "DOWN" else -1
    edge_map = SINBUNDANG_RUNTIME.get("edge_seconds", {}).get(direction, {})
    total = 0
    i = ai
    while i != bi:
        j = i + step
        key = f"{stations[i]}>{stations[j]}"
        sec = edge_map.get(key)
        if sec is None:
            return None
        total += int(sec)
        i = j
    # 운영사 전체 소요시간표는 중간 정차역마다 30초 정차가 누적된다.
    intermediate = max(0, abs(bi - ai) - 1)
    total += intermediate * 30
    return total


def _sinbundang_destination_reaches(destination, end, direction):
    stations = [canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations", [])]
    dest, target = canon_station(destination), canon_station(end)
    if target not in stations:
        return False
    if not dest or dest not in stations:
        # 종착역이 비어 있으면 전구간 운행으로 단정하지 않고, 실시간 차량의 경우만
        # 방향 정보가 정확하다는 전제에서 허용한다.
        return True
    di, ti = stations.index(dest), stations.index(target)
    return di >= ti if direction == "DOWN" else di <= ti


def _sinbundang_previous_station(station, direction):
    stations = [canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations", [])]
    st = canon_station(station)
    if st not in stations:
        return ""
    i = stations.index(st)
    # trainSttus=3(전역출발)에서 statnNm은 다음 도착 대상역으로 해석한다.
    j = i - 1 if direction == "DOWN" else i + 1
    return stations[j] if 0 <= j < len(stations) else ""


def _sinbundang_vehicle_anchor(row):
    """
    realtimePosition 한 행을 '어느 역 출발시각을 기준으로 볼 것인가'로 정규화한다.
    반환: (anchor_station, anchor_departure_dt, current_station, status_text, observed_dt)
    """
    current = canon_station(row.get("statnNm"))
    direction = _api_direction_to_schedule("신분당선", row.get("updnLine"))
    observed = parse_dt(row.get("recptnDt") or row.get("lastRecptnDt"))
    raw_status = str(row.get("trainSttus") or "").strip()
    status = status_name(raw_status)
    assumptions = SINBUNDANG_RUNTIME.get("status_assumptions_seconds", {})

    if raw_status == "3":
        previous = _sinbundang_previous_station(current, direction)
        if previous:
            return previous, observed, current, status or "전역출발", observed
    if raw_status == "0":
        return current, observed + timedelta(seconds=int(assumptions.get("진입_to_departure", 45))), current, status or "진입", observed
    if raw_status == "1":
        return current, observed + timedelta(seconds=int(assumptions.get("도착_to_departure", 25))), current, status or "도착", observed
    # 출발(2) 또는 알 수 없는 상태는 수신시각을 현재역 출발 기준으로 둔다.
    return current, observed, current, status or "위치확인", observed


def _sinbundang_vehicle_eta_to(row, target):
    direction = _api_direction_to_schedule("신분당선", row.get("updnLine"))
    anchor, anchor_dt, current, status, observed = _sinbundang_vehicle_anchor(row)
    if not direction or not anchor:
        return None
    runtime = sinbundang_runtime_seconds(anchor, target)
    if runtime is None:
        return None
    return {
        "eta": anchor_dt + timedelta(seconds=runtime),
        "anchor_station": anchor,
        "current_station": current,
        "status": status,
        "observed": observed,
        "direction": direction,
    }


def sinbundang_vehicle_candidates(mode, start, end, ready_dt, rows):
    """
    공개 열차번호 없이 realtimePosition의 차량 식별값(trainNo)을 그 순간의 차량번호로 사용한다.
    실시간 소재에서 출발역까지의 ETA를 계산하고, 출발역→도착역은 운영사 공식 소요시간을 적용한다.
    지연시간은 별도로 추론하지 않는다.
    """
    now = now_kst()
    stations = [canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations", [])]
    start, end = canon_station(start), canon_station(end)
    if start not in stations or end not in stations:
        return []
    direction = sinbundang_direction(start, end)
    si, ei = stations.index(start), stations.index(end)
    ride_seconds = sinbundang_runtime_seconds(start, end)
    if ride_seconds is None:
        return []

    out = []
    for row in rows:
        if str(row.get("subwayId") or "1077").strip() not in {"", "1077"}:
            continue
        vehicle_id = str(row.get("trainNo") or "").strip()
        if not vehicle_id:
            continue
        row_direction = _api_direction_to_schedule("신분당선", row.get("updnLine"))
        if row_direction != direction:
            continue
        current = canon_station(row.get("statnNm"))
        if current not in stations:
            continue
        ci = stations.index(current)
        if (direction == "DOWN" and ci > si) or (direction == "UP" and ci < si):
            continue
        destination = canon_station(row.get("statnTnm"))
        if not _sinbundang_destination_reaches(destination, end, direction):
            continue

        # 현재 승차역에서 이미 '출발' 상태면 해당 차량은 놓친 것으로 본다.
        raw_status = str(row.get("trainSttus") or "").strip()
        if current == start and raw_status == "2":
            continue

        eta_info = _sinbundang_vehicle_eta_to(row, start)
        if not eta_info:
            continue
        board_dt = eta_info["eta"]
        # anchor가 승차역 이전 역이면 runtime은 승차역 '도착'까지이므로 평균 정차 30초 뒤 출발로 본다.
        if canon_station(eta_info.get("anchor_station")) != start:
            board_dt += timedelta(seconds=30)
        age = max(0.0, (now - eta_info["observed"]).total_seconds())
        # 3분 넘게 오래된 소재는 실시간 후보로 쓰지 않는다.
        if age > 180:
            continue
        if board_dt < ready_dt - timedelta(seconds=10):
            continue
        if board_dt > ready_dt + timedelta(hours=1):
            continue
        alight_dt = board_dt + timedelta(seconds=ride_seconds)
        out.append({
            "line": "신분당선", "from": start, "to": end,
            "train_no": vehicle_id, "vehicle_id": vehicle_id,
            "external_train_no": vehicle_id,
            "service": "local", "direction": direction,
            "origin": "", "destination": destination or ("광교" if direction == "DOWN" else "신사"),
            "board_dt": board_dt, "alight_dt": alight_dt,
            "wait_seconds": round((board_dt - ready_dt).total_seconds()),
            "ride_seconds": int(ride_seconds),
            "delay_seconds": None, "delay_source": "vehicle_position",
            "observed_at": eta_info["observed"].strftime("%Y-%m-%d %H:%M:%S"),
            "data_age_seconds": max(0, round(age)),
            "current_station": current, "status": eta_info["status"],
            "location_kind": "live",
            "location_label": f"{current} {eta_info['status']}".strip(),
            "confidence": "중간",
            "method": "실시간 차량 소재 + 운영사 공식 역간 소요시간",
            "projected": False,
            "delay_available": False,
        })
    out.sort(key=lambda x: (x["alight_dt"], x["board_dt"], str(x["train_no"])))
    return out


def _sinbundang_virtual_runs(mode):
    day = _sinbundang_day_key(mode)
    return SINBUNDANG_VIRTUAL.get("runs", {}).get(day, {})


def _sinbundang_run_pair(run, start, end):
    """익명 가상 운행편에서 승차/하차 여객역의 시각표 초를 찾는다."""
    start, end = canon_station(start), canon_station(end)
    stops = run.get("stops", [])
    si = ei = None
    for i, stop in enumerate(stops):
        if not bool(stop.get("call", True)):
            continue
        st = canon_station(stop.get("station"))
        if si is None and st == start:
            si = i
            continue
        if si is not None and st == end:
            ei = i
            break
    if si is None or ei is None or ei <= si:
        return None
    bs = stops[si].get("dep")
    if bs is None:
        bs = stops[si].get("arr")
    es = stops[ei].get("arr")
    if es is None:
        es = stops[ei].get("dep")
    if bs is None or es is None:
        return None
    return si, ei, int(bs), int(es)


def _sinbundang_run_base_dt(run, reference_station, occurrence_dt):
    """reference_station의 시간표 시각과 실제 occurrence를 이용해 서비스데이 자정을 복원한다."""
    ref = canon_station(reference_station)
    for stop in run.get("stops", []):
        if not bool(stop.get("call", True)) or canon_station(stop.get("station")) != ref:
            continue
        sec = stop.get("dep") if stop.get("dep") is not None else stop.get("arr")
        if sec is not None:
            return occurrence_dt - timedelta(seconds=int(sec))
    return occurrence_dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _sinbundang_virtual_location(run, base_dt, at_dt=None):
    """
    익명 가상 운행편의 공개 시각표를 연결해 현재 예상 소재를 계산한다.
    공개되지 않은 열차번호/차량번호를 추정하지 않고 시각표상의 위치만 표현한다.
    """
    at_dt = at_dt or now_kst()
    passenger = []
    valid_stations = {canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations", [])}
    for stop in run.get("stops", []):
        st = canon_station(stop.get("station"))
        if not bool(stop.get("call", True)) or st not in valid_stations:
            continue
        arr = base_dt + timedelta(seconds=int(stop["arr"])) if stop.get("arr") is not None else None
        dep = base_dt + timedelta(seconds=int(stop["dep"])) if stop.get("dep") is not None else None
        passenger.append((st, arr, dep))
    if not passenger:
        return {"station":"", "status":"", "label":"예상 소재 계산 중"}

    first_st, first_arr, first_dep = passenger[0]
    first_time = first_dep or first_arr
    if first_time and at_dt < first_time:
        return {"station": first_st, "status":"출발 전 예상", "label":f"{first_st} 출발 전 예상"}

    for i, (st, arr, dep) in enumerate(passenger):
        # 역 정차 구간
        if arr is not None and dep is not None and arr <= at_dt <= dep:
            return {"station":st, "status":"정차 예상", "label":f"{st} 정차 예상"}
        # 종착역 도착 이후
        if i == len(passenger)-1 and arr is not None and at_dt >= arr:
            return {"station":st, "status":"도착 예상", "label":f"{st} 도착 예상"}
        # 현 역 출발 ~ 다음 역 도착 사이
        if i < len(passenger)-1:
            nst, narr, ndep = passenger[i+1]
            left = dep or arr
            right = narr or ndep
            if left is not None and right is not None and left <= at_dt < right:
                return {"station":st, "status":"이동 예상", "label":f"{st} → {nst} 이동 예상"}

    last_st = passenger[-1][0]
    return {"station":last_st, "status":"운행 종료 예상", "label":f"{last_st} 운행 종료 예상"}


def _sinbundang_virtual_candidate(run_id, run, start, end, ready_dt, previous=False):
    pair = _sinbundang_run_pair(run, start, end)
    if not pair:
        return None
    _, _, board_sec, alight_sec = pair
    if previous:
        board_dt = schedule_dt_before(board_sec, ready_dt, 0)
    else:
        board_dt = schedule_dt_after(board_sec, ready_dt, 0)
    if not board_dt:
        return None
    base_dt = board_dt - timedelta(seconds=board_sec)
    alight_dt = base_dt + timedelta(seconds=alight_sec)
    if alight_dt < board_dt:
        return None
    loc = _sinbundang_virtual_location(run, base_dt, now_kst())
    return {
        "line":"신분당선", "from":canon_station(start), "to":canon_station(end),
        # 내부 가상 운행편 ID. 화면에서는 신분당선 식별번호를 렌더링하지 않는다.
        "train_no":run_id, "virtual_run_id":run_id, "vehicle_id":"", "external_train_no":"",
        "service":"local", "direction":run.get("direction", sinbundang_direction(start,end)),
        "origin":canon_station(run.get("start")), "destination":canon_station(run.get("dest")),
        "board_dt":board_dt, "alight_dt":alight_dt,
        "wait_seconds":round((board_dt-ready_dt).total_seconds()),
        "ride_seconds":max(0, round((alight_dt-board_dt).total_seconds())),
        "delay_seconds":None, "delay_source":"schedule_virtual_run",
        "observed_at":"", "data_age_seconds":0,
        "current_station":loc.get("station", ""), "status":loc.get("status", ""),
        "location_kind":"expected", "location_label":loc.get("label", "예상 소재 계산 중"),
        "confidence":"낮음", "method":"공개 역별 시각표 가상 운행편",
        "projected":True, "delay_available":False,
        "is_previous":bool(previous),
    }


def _sinbundang_event_reaches(event, end, direction):
    return _sinbundang_destination_reaches(event.get("dest"), end, direction)


def sinbundang_schedule_candidates(mode, start, end, ready_dt, limit=8):
    """공개 역별 시각표의 열(column)을 익명 가상 운행편으로 연결해 후보를 구성한다."""
    direction = sinbundang_direction(start, end)
    if not direction:
        return []
    rows = []
    for run_id, run in _sinbundang_virtual_runs(mode).items():
        if run.get("direction") != direction:
            continue
        c = _sinbundang_virtual_candidate(run_id, run, start, end, ready_dt, previous=False)
        if not c:
            continue
        if c["board_dt"] > ready_dt + timedelta(hours=4):
            continue
        rows.append(c)
    rows.sort(key=lambda x:(x["board_dt"], x["alight_dt"], x["train_no"]))
    # 같은 시각/종착의 중복 익명 운행편은 하나만 노출한다.
    unique=[]; seen=set()
    for row in rows:
        key=(row["board_dt"], row.get("destination"))
        if key in seen:
            continue
        seen.add(key); unique.append(row)
        if len(unique)>=limit:
            break
    return unique


def sinbundang_previous_schedule_candidate(mode, start, end, ready_dt):
    direction = sinbundang_direction(start, end)
    if not direction:
        return None
    best = None
    for run_id, run in _sinbundang_virtual_runs(mode).items():
        if run.get("direction") != direction:
            continue
        c = _sinbundang_virtual_candidate(run_id, run, start, end, ready_dt, previous=True)
        if not c:
            continue
        if best is None or c["board_dt"] > best["board_dt"]:
            best = c
    return best


def calculate_sinbundang_segment(mode, start, end, ready_dt, position_cache):
    positions, realtime_error, realtime_available = cached_position_rows("신분당선", position_cache)
    direct = sinbundang_vehicle_candidates(mode, start, end, ready_dt, positions)
    projected = sinbundang_schedule_candidates(mode, start, end, ready_dt, limit=10)

    # 실시간 차량도 가장 가까운 익명 가상 운행편(±3분)에 결합한다.
    # 이렇게 하면 차량 소재가 순간 사라져도 같은 운행편의 시간표 예상 소재로 연속 추적할 수 있다.
    merged = []
    paired_virtual_ids = set()
    for d in direct:
        matches = [p for p in projected if abs((p["board_dt"]-d["board_dt"]).total_seconds()) <= 180]
        if matches:
            p = min(matches, key=lambda x:abs((x["board_dt"]-d["board_dt"]).total_seconds()))
            m = dict(d)
            m["vehicle_id"] = str(d.get("vehicle_id") or d.get("train_no") or "")
            m["train_no"] = p["train_no"]
            m["virtual_run_id"] = p["train_no"]
            m["origin"] = p.get("origin") or m.get("origin", "")
            m["destination"] = p.get("destination") or m.get("destination", "")
            m["method"] = "실시간 차량 소재 + 공개 시각표 가상 운행편 + 공식 역간 소요시간"
            merged.append(m)
            paired_virtual_ids.add(p["train_no"])
        else:
            merged.append(d)
    for p in projected:
        if p["train_no"] in paired_virtual_ids:
            continue
        merged.append(p)
    merged.sort(key=lambda c:(c["alight_dt"], 1 if c.get("projected") else 0, c["board_dt"]))
    near=[c for c in merged if -5 <= (c["board_dt"]-ready_dt).total_seconds() <= 3600] or merged
    if not near:
        return {"ok":False,"error":f"신분당선 {start}→{end} 운행 정보를 찾지 못했습니다.","diagnostics":{}}
    chosen=near[0]
    previous=sinbundang_previous_schedule_candidate(mode,start,end,ready_dt)
    diag={
        "positions":len(positions),
        "vehicle_ids":sorted({str(x.get("trainNo") or "") for x in positions if str(x.get("trainNo") or "")})[:30],
        "live_vehicle_candidates":len(direct),
        "schedule_candidates":len(projected),
        "realtime_available":realtime_available,
        "realtime_error":realtime_error or "",
        "strategy":"vehicle_position_plus_official_runtime",
        "delay_inference":False,
    }
    cached_meta=position_cache.get("신분당선",{}) if isinstance(position_cache,dict) else {}
    if isinstance(cached_meta,dict) and cached_meta.get("query"):
        diag["realtime_query"]=cached_meta.get("query")
    # 신분당선도 다른 노선과 동일하게 근처 후보 6개 + 직전 후보 1개를 공개한다.
    # 공개 운행 열차번호가 없으므로 식별자는 UI에서 숨기고, 시간/방향/소재/신뢰도만 표시한다.
    public = [
        public_candidate(c, selected=(c is chosen))
        for c in near[:6]
    ]
    if previous:
        previous = dict(previous)
        previous["is_previous"] = True
        prev_key = (previous.get("board_dt"), previous.get("destination"))
        existing = {(x.get("board_dt"), x.get("destination")) for x in public}
        if prev_key not in existing:
            public.insert(0, public_candidate(previous))
    return {
        "ok":True,"chosen":chosen,"candidates":near[:10],"public_candidates":public,
        "previous_candidate":public_candidate(previous) if previous else None,
        "diagnostics":diag,
    }


def sinbundang_probe_snapshot(stations=None, timeout=5):
    """신분당선 realtimePosition 차량 소재 단일 스냅샷. trainNo는 차량 식별자로 기록한다."""
    ok_pos, err_pos, pos_data = fetch_position("신분당선", timeout=timeout)
    pos_rows = position_rows(pos_data or {}, "신분당선") if ok_pos else []
    keep = ("subwayId","subwayNm","statnId","statnNm","trainNo","lastRecptnDt","recptnDt","updnLine","statnTid","statnTnm","trainSttus","directAt","lstcarAt")
    rows=[]
    for row in pos_rows:
        clean={k:row.get(k) for k in keep}
        clean["vehicle_id"]=str(row.get("trainNo") or "")
        rows.append(clean)
    return {
        "captured_at":now_kst().strftime("%Y-%m-%d %H:%M:%S"),
        "position":{"ok":ok_pos,"error":err_pos,"query":(pos_data or {}).get("_jigeumta_query","") if isinstance(pos_data,dict) else "","rows":rows},
        "vehicle_ids":sorted({r["vehicle_id"] for r in rows if r.get("vehicle_id")}),
        "note":"서울시 trainNo 필드는 신분당선에서 공개 운행열번이 아니라 실시간 차량 식별자로 취급한다.",
    }


def position_rows(data, line=None):
    """
    realtimePosition 응답 행을 반환한다.

    서울시 API 요청 인자는 subwayNm(호선명)이지만 응답에는 subwayId가 함께 온다.
    신분당선은 subwayId=1077이며, LINE_IDS에 등록된 노선은 응답 ID가 명시된 경우
    해당 ID와 일치하는 행만 사용해 잘못된 노선 응답이 ETA에 섞이지 않게 한다.
    테스트/과거 캐시처럼 subwayId가 없는 행은 호환성을 위해 유지한다.
    """
    rows = data.get("realtimePositionList", []) if isinstance(data, dict) else []
    if not line:
        return rows
    expected = LINE_IDS.get(line)
    if not expected:
        return rows
    return [
        row for row in rows
        if not str(row.get("subwayId") or "").strip()
        or str(row.get("subwayId") or "").strip() == expected
    ]

def prefetch_position_cache(lines, timeout=5):
    """여러 노선의 실시간 상태를 공유 cache로 구성한다.

    V14 migration mode:
      direct     - 기존처럼 사용자 요청에서 서울시 API 직접 조회
      hybrid     - Redis fresh snapshot 우선, miss/stale이면 직접 조회
      cache_only - Redis만 사용; miss/stale이면 rows=[] + 최근 exact delay로 시간표 예측
    """
    unique = sorted({canon_line(x) for x in lines if canon_line(x) in LINE_NAMES})
    if not unique:
        return {}

    cache = {}
    mode = realtime_store.store_mode()
    redis_enabled = mode in {"hybrid", "cache_only"} and realtime_store.is_configured()

    # 서버 공용 exact-delay memory. stale raw 위치 자체는 live로 재사용하지 않는다.
    server_delay_rows = []
    if redis_enabled:
        for line in unique:
            line_id = REALTIME_STORE_IDS.get(line)
            if not line_id:
                continue
            try:
                server_delay_rows.extend(realtime_store.get_delay_rows(line_id))
            except Exception:
                pass
    if server_delay_rows:
        cache["__train_delay_cache__"] = _merge_delay_cache_rows(server_delay_rows)

    pending = []
    for line in unique:
        if line in SCHEDULE_ONLY_LINES:
            cache[line] = {
                "rows": [],
                "error": "실시간 위치 API 미연동 · 공식 시간표만 사용",
                "available": False,
                "cache_state": "schedule_only",
                "realtime_source": "schedule",
            }
            continue

        redis_entry = None
        if redis_enabled and REALTIME_STORE_IDS.get(line):
            try:
                redis_entry = realtime_store.position_cache_entry(
                    line_id=REALTIME_STORE_IDS[line], line_name=line, now=now_kst()
                )
                cache[line] = redis_entry
            except Exception as e:
                redis_entry = {
                    "rows": [], "available": False,
                    "error": f"Redis {type(e).__name__}: {e}",
                    "cache_state": "error", "realtime_source": "redis",
                }
                cache[line] = redis_entry

        if mode == "cache_only":
            if not redis_enabled and line not in cache:
                cache[line] = {
                    "rows": [], "available": False,
                    "error": "REALTIME_STORE_MODE=cache_only 이지만 REDIS_URL/redis 설정이 없습니다.",
                    "cache_state": "miss", "realtime_source": "redis",
                }
            continue

        # direct 또는 hybrid의 Redis miss/stale만 원본 API로 보충한다.
        if mode == "direct" or not redis_entry or not redis_entry.get("available"):
            pending.append(line)

    if not pending:
        return cache

    workers = min(6, len(pending))
    def one(line):
        try:
            ok, err, data = fetch_position(line, timeout=timeout)
            if ok:
                return line, {
                    "rows": position_rows(data, line),
                    "error": "",
                    "available": True,
                    "query": data.get("_jigeumta_query", "") if isinstance(data, dict) else "",
                    "cache_state": "fresh",
                    "cache_age_seconds": 0,
                    "realtime_source": "direct",
                }
            message = (err or {}).get("message", err) if isinstance(err, dict) else err
            return line, {
                "rows": [], "error": str(message or "실시간 위치 조회 실패"),
                "available": False, "cache_state": "miss", "realtime_source": "direct",
            }
        except Exception as e:
            return line, {
                "rows": [], "error": f"{type(e).__name__}: {e}",
                "available": False, "cache_state": "error", "realtime_source": "direct",
            }

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(one, line) for line in pending]
        for fut in as_completed(futures):
            line, value = fut.result()
            # hybrid에서 direct 실패 시 Redis stale metadata를 보존하되 raw rows는 계속 비운다.
            previous = cache.get(line)
            if not value.get("available") and isinstance(previous, dict) and previous.get("realtime_source") == "redis":
                previous = dict(previous)
                previous["direct_error"] = value.get("error", "")
                cache[line] = previous
            else:
                cache[line] = value
    return cache

def cached_position_rows(line, position_cache):
    """
    realtimePosition 장애가 전체 경로 계산 실패로 이어지지 않도록 한다.
    실패 시 빈 관측값을 반환하여 공식 시간표 기반 계산으로 자동 강등한다.
    SCHEDULE_ONLY_LINES로 명시된 노선만 네트워크 호출 없이 schedule-only로 처리한다.
    """
    if line in SCHEDULE_ONLY_LINES:
        if line not in position_cache:
            position_cache[line] = {
                "rows": [],
                "error": "실시간 위치 API 미연동 · 공식 시간표만 사용",
                "available": False,
            }
        cached = position_cache[line]
        return cached.get("rows", []), cached.get("error", ""), False

    if line not in position_cache:
        fetched = prefetch_position_cache([line], timeout=5)
        # 서버 delay cache도 함께 전달한다.
        if isinstance(fetched, dict) and fetched.get("__train_delay_cache__"):
            position_cache["__train_delay_cache__"] = _merge_delay_cache_rows(
                position_cache.get("__train_delay_cache__", []),
                fetched.get("__train_delay_cache__", []),
            )
        position_cache[line] = fetched.get(line, {
            "rows": [], "error": "실시간 위치 조회 실패", "available": False
        })

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

def _api_direction_to_schedule(line, raw):
    text = str(raw or "").strip().upper()
    if text in {"0", "UP", "상행", "상행/내선", "상행선"}:
        return "UP"
    if text in {"1", "DN", "DOWN", "하행", "하행/외선", "하행선"}:
        return "DOWN"
    return ""

def infer_live_train_by_context(line, mode, position):
    """폐기된 호환 함수. 신분당선 차량번호를 공개 시간표 열번으로 매칭하지 않는다."""
    return None


def _is_live_source(kind):
    return kind in {"live", "live_context"}

# ---------- Delay observations ----------
def observe_delays(line, mode, positions, client_cache=None):
    now = now_kst()
    obs = []
    unmatched_train = []
    unmatched_station = []
    live_by_train = {}

    context_matched = 0
    for p in positions:
        raw_tn = p.get("trainNo") or p.get("btrainNo")
        tr = get_train(line, mode, raw_tn)
        inferred = None
        source_kind = "live"
        if not tr:
            inferred = infer_live_train_by_context(line, mode, p)
            if inferred:
                tr = inferred["train"]
                source_kind = "live_context"
                context_matched += 1
            else:
                if raw_tn and len(unmatched_train) < 10:
                    unmatched_train.append(str(raw_tn))
                continue
        cur = canon_station(p.get("statnNm"))
        ci = inferred["current_index"] if inferred else first_current_index(tr["stops"], cur)
        if ci is None:
            if cur and len(unmatched_station) < 10:
                unmatched_station.append(cur)
            continue
        ref = inferred["ref"] if inferred else scheduled_reference_at(tr["stops"], ci, p.get("trainSttus"))
        if ref is None:
            continue
        observed = inferred["observed"] if inferred else parse_dt(p.get("recptnDt") or p.get("lastRecptnDt"))
        delay = inferred["delay"] if inferred else align_clock(observed, ref) - ref
        item = {
            "train_no": tr["train_no"],
            "raw_train_no": str(raw_tn or ""),
            "direction": tr["direction"],
            "service": tr["service"],
            "delay": delay,
            "current_station": cur,
            "status": status_name(p.get("trainSttus")),
            "observed": observed,
            "ref": ref,
            "train": tr,
            "raw": p,
            "source_kind": source_kind,
            "match_kind": "train_number" if source_kind == "live" else "schedule_context",
            "match_margin_seconds": inferred.get("match_margin_seconds") if inferred else None,
            "cache_age_seconds": 0,
        }
        key = norm_train(tr["train_no"])
        previous = live_by_train.get(key)
        if previous is None or observed > previous["observed"]:
            live_by_train[key] = item

    obs.extend(live_by_train.values())

    # 최근 exact-train 관측 캐시. 현재 API에 같은 열차의 더 최신 관측이 있으면 사용하지 않는다.
    cache_used = 0
    if line in STALE_DELAY_CACHE_LINES:
        for row in client_cache or []:
            if str(row.get("line") or "") != line:
                continue
            raw_tn = row.get("train_no")
            tr = get_train(line, mode, raw_tn)
            if not tr:
                continue
            try:
                observed = datetime.strptime(str(row.get("observed_at") or ""), "%Y-%m-%d %H:%M:%S")
                raw_delay = float(row.get("delay_seconds"))
            except Exception:
                continue
            age = (now - observed).total_seconds()
            if age < -120 or age > STALE_DELAY_MAX_SECONDS:
                continue
            if abs(raw_delay) > 2700:
                continue
            key = norm_train(tr["train_no"])
            current = live_by_train.get(key)
            if current is not None and current.get("observed") >= observed:
                continue

            # 20분까지는 마지막 확인 지연을 그대로 유지. 이후 35분까지 선형으로 0에 수렴.
            if age <= STALE_DELAY_HOLD_SECONDS:
                factor = 1.0
            else:
                factor = max(0.0, (STALE_DELAY_MAX_SECONDS - age) / (STALE_DELAY_MAX_SECONDS - STALE_DELAY_HOLD_SECONDS))
            delay = raw_delay * factor
            obs = [o for o in obs if norm_train(o.get("train_no")) != key]
            obs.append({
                "train_no": tr["train_no"],
                "direction": tr["direction"],
                "service": tr["service"],
                "delay": delay,
                "current_station": canon_station(row.get("current_station")),
                "status": str(row.get("status") or "최근 관측"),
                "observed": observed,
                "ref": None,
                "train": tr,
                "raw": {},
                "source_kind": "cache",
                "cache_age_seconds": max(0, round(age)),
                "raw_cached_delay": raw_delay,
            })
            cache_used += 1

    obs.sort(key=lambda o: o.get("observed") or now, reverse=True)
    return obs, {
        "positions": len(positions),
        "matched": sum(1 for o in live_by_train.values() if o.get("source_kind") == "live"),
        "matched_context": sum(1 for o in live_by_train.values() if o.get("source_kind") == "live_context"),
        "cached_exact": cache_used,
        "unmatched_train": unmatched_train,
        "unmatched_station": unmatched_station,
    }

def median_delay(observations, direction=None, service=None):
    vals = []
    for o in observations:
        # 브라우저 캐시는 해당 열차 exact fallback에만 사용하고 다른 열차 지연으로 전파하지 않는다.
        if o.get("source_kind") == "cache":
            continue
        if direction and o["direction"] != direction:
            continue
        if service and o["service"] != service:
            continue
        if abs(o["delay"]) <= 2700:
            vals.append(o["delay"])
    if not vals and (direction or service):
        return median_delay(observations)
    return statistics.median(vals) if vals else 0

def delay_for_train(tr, observations):
    """exact train 관측 > 동일 방향/등급 중앙값 > 0 순으로 지연값을 선택."""
    key = norm_train(tr.get("train_no"))
    exact = [o for o in observations if norm_train(o.get("train_no")) == key]
    if exact:
        o = max(exact, key=lambda x: x.get("observed") or datetime.min)
        kind = o.get("source_kind")
        if kind == "live":
            source = "live_exact"
        elif kind == "live_context":
            source = "live_context"
        else:
            source = "cached_exact"
        return o.get("delay", 0), source, o
    delay = median_delay(observations, tr.get("direction"), tr.get("service"))
    return delay, ("live_median" if any(_is_live_source(o.get("source_kind")) for o in observations) else "schedule_only"), None


# ---------- 1호선 급행-완행 계획 추월 ----------
# 코레일 1호선 시간표에는 완행이 대피역에 수분간 정차하고 그 사이 급행이 통과하는
# 계획 추월이 명시돼 있다. 두 열차의 실시간 지연을 독립적으로 더하면 급행이 조금
# 지연됐을 때 이 운행 순서를 뒤집는 오류가 생기므로, 대피역 이후 ETA에 운행 순서
# 제약을 적용한다.
LINE1_OVERTAKE_CLEARANCE_SECONDS = 0
LINE1_OVERTAKE_MAX_EXTRA_HOLD_SECONDS = 10 * 60

def _direction_key(tr):
    return str(tr.get("direction") or "").strip().lower()

def _timed_stop_map(tr):
    return {
        canon_station(stop.get("station")): (i, stop)
        for i, stop in enumerate(tr.get("stops", []))
        if stop_time_sec(stop) is not None
    }

def _detect_line1_overtake_events(mode):
    """
    공식 시간표에서 고신뢰 계획 추월만 자동 검출한다.

    조건:
      1) 완행과 급행이 같은 방향으로 운행
      2) 완행이 한 역에 90초 이상 정차
      3) 그 정차시간 안에 급행이 해당 역을 통과/정차
      4) 직전 공통 지점에서는 완행이 앞서고, 이후 공통 지점에서는 급행이 앞섬

    이렇게 하면 분기 합류처럼 단순히 시간 순서가 바뀌는 경우를 추월로 오인하지 않는다.
    """
    trains = all_trains("1호선", mode)
    locals_ = [tr for tr in trains if tr.get("service") == "local"]
    expresses = [tr for tr in trains if tr.get("service") == "express"]
    events = []

    express_maps = [(ex, _timed_stop_map(ex)) for ex in expresses]
    for local in locals_:
        lmap = _timed_stop_map(local)
        for express, emap in express_maps:
            if _direction_key(local) != _direction_key(express):
                continue
            common = set(lmap).intersection(emap)
            if len(common) < 3:
                continue
            for station in common:
                li, ls = lmap[station]
                ei, es = emap[station]
                larr = ls.get("arr")
                ldep = ls.get("dep")
                epass = stop_time_sec(es)
                if larr is None or ldep is None or epass is None:
                    continue
                if ldep - larr < 90 or not (larr <= epass <= ldep):
                    continue

                upstream = []
                downstream = []
                for other in common:
                    oli, ols = lmap[other]
                    oei, oes = emap[other]
                    lt = stop_time_sec(ols)
                    et = stop_time_sec(oes)
                    if lt is None or et is None:
                        continue
                    if oli < li and oei < ei:
                        upstream.append((oli, other, lt, et))
                    elif oli > li and oei > ei:
                        downstream.append((oli, other, lt, et))
                if not upstream or not downstream:
                    continue
                upstream.sort(reverse=True)
                downstream.sort()
                # 추월 직전에는 완행이 먼저, 이후에는 급행이 먼저여야 한다.
                _, upstream_station, local_before, express_before = upstream[0]
                if not (local_before < express_before):
                    continue
                first_downstream = next(
                    ((idx, st, lt, et) for idx, st, lt, et in downstream if et < lt),
                    None,
                )
                if first_downstream is None:
                    continue
                _, downstream_station, _, _ = first_downstream
                events.append({
                    "local_train_no": local.get("train_no"),
                    "express_train_no": express.get("train_no"),
                    "direction": local.get("direction", ""),
                    "station": station,
                    "local_index": li,
                    "express_index": ei,
                    "local_arrival_sec": int(larr),
                    "local_departure_sec": int(ldep),
                    "express_pass_sec": int(epass),
                    "upstream_station": upstream_station,
                    "downstream_station": downstream_station,
                })

    events.sort(key=lambda x: (norm_train(x["local_train_no"]), x["local_index"], x["express_pass_sec"]))
    return tuple(events)

@lru_cache(maxsize=None)
def line1_overtake_events(mode):
    korail_day, _ = choose_modes(mode)
    precomputed = ROUTE_GRAPH.get("line1_overtakes", {}).get(korail_day)
    if isinstance(precomputed, list):
        return tuple(precomputed)
    return _detect_line1_overtake_events(mode)

def _line1_overtake_events_for_local(mode, train_no):
    key = norm_train(train_no)
    return [e for e in line1_overtake_events(mode) if norm_train(e.get("local_train_no")) == key]

def _line1_overtake_events_for_express(mode, train_no):
    key = norm_train(train_no)
    return [e for e in line1_overtake_events(mode) if norm_train(e.get("express_train_no")) == key]

def _line1_observation_baseline(tr, exact_obs):
    if not exact_obs:
        return None, ""
    station = canon_station(exact_obs.get("current_station"))
    idx = first_current_index(tr.get("stops", []), station) if station else None
    return idx, str(exact_obs.get("status") or "").strip()

def line1_operational_overtake_adjustment(tr, mode, start_idx, end_idx, base_delay, observations, exact_obs=None):
    """
    완행의 구간별 지연을 계획 추월 순서에 맞게 보정한다.

    반환값의 board_delay/end_delay는 각각 승차역 출발과 하차역 도착에 적용할 지연이다.
    현재 열차가 이미 대피역을 출발한 실시간 관측이 있으면 해당 추월은 다시 적용하지 않는다.
    """
    base_delay = float(base_delay or 0)
    result = {
        "board_delay": base_delay,
        "end_delay": base_delay,
        "extra_hold_seconds": 0,
        "notices": [],
    }
    if tr.get("service") != "local":
        return result

    baseline_idx, baseline_status = _line1_observation_baseline(tr, exact_obs)
    current_delay = base_delay
    board_delay = base_delay

    for event in _line1_overtake_events_for_local(mode, tr.get("train_no")):
        idx = int(event["local_index"])
        # 목적지에 도착한 뒤의 추월, 또는 목적지에서 하차한 뒤 발생하는 대피는 무관하다.
        if idx >= end_idx:
            continue
        if baseline_idx is not None:
            if idx < baseline_idx:
                continue
            if idx == baseline_idx and baseline_status == "출발":
                continue

        express = get_train("1호선", mode, event["express_train_no"])
        if not express:
            continue
        express_delay, express_delay_source, _ = delay_for_train(express, observations)
        naive_departure = event["local_departure_sec"] + current_delay
        required_departure = (
            event["express_pass_sec"]
            + float(express_delay or 0)
            + LINE1_OVERTAKE_CLEARANCE_SECONDS
        )
        extra = max(0.0, required_departure - naive_departure)
        applied = 0.0
        # 급행이 매우 크게 무너진 경우 실제 운전정리가 바뀔 수 있으므로 무한정 완행을 묶지 않는다.
        if 0 < extra <= LINE1_OVERTAKE_MAX_EXTRA_HOLD_SECONDS:
            current_delay += extra
            applied = extra

        if idx <= start_idx:
            board_delay = current_delay

        if extra > LINE1_OVERTAKE_MAX_EXTRA_HOLD_SECONDS:
            notice_text = (
                f"{event['station']}에서 {event['express_train_no']} 급행 추월 계획"
                " · 급행 지연이 커 실제 운전정리 변동 가능"
            )
            role = "yield_uncertain"
        else:
            if applied >= 60:
                mins, secs = divmod(round(applied), 60)
                hold_text = f"{mins}분" + (f" {secs}초" if secs else "")
            elif applied > 0:
                hold_text = f"{round(applied)}초"
            else:
                hold_text = ""
            notice_text = (
                f"{event['station']}에서 {event['express_train_no']} 급행 추월 대기 예상"
                + (f" · {hold_text} 추가 대기 반영" if hold_text else "")
            )
            role = "yield"
        notice = {
            "type": "planned_overtake",
            "role": role,
            "station": event["station"],
            "counterpart_train_no": event["express_train_no"],
            "counterpart_service": "express",
            "extra_hold_seconds": round(applied),
            "counterpart_delay_seconds": round(float(express_delay or 0)),
            "counterpart_delay_source": express_delay_source,
            "text": notice_text,
        }
        result["notices"].append(notice)

    result["board_delay"] = board_delay
    result["end_delay"] = current_delay
    result["extra_hold_seconds"] = max(0, round(current_delay - base_delay))
    return result

def line1_express_overtake_notices(tr, mode, start_idx, end_idx):
    if tr.get("service") != "express":
        return []
    notices = []
    for event in _line1_overtake_events_for_express(mode, tr.get("train_no")):
        # 추월 지점이 사용자의 승차~하차 구간 안에 있을 때만 노출한다.
        ex_idx = int(event["express_index"])
        if not (start_idx <= ex_idx < end_idx):
            continue
        notices.append({
            "type": "planned_overtake",
            "role": "overtake",
            "station": event["station"],
            "counterpart_train_no": event["local_train_no"],
            "counterpart_service": "local",
            "extra_hold_seconds": 0,
            "text": f"{event['station']}에서 {event['local_train_no']} 일반열차 추월 예정",
        })
    return notices

# ---------- Segment ETA ----------
def direct_live_candidates(line, mode, start, end, ready_dt, observations):
    now = now_kst()
    out = []
    for o in observations:
        if o.get("source_kind") != "live":
            continue
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

        operation_notices = []
        operational_hold_seconds = 0
        projected_delay_seconds = round(o["delay"])
        if line == "1호선":
            if tr.get("service") == "local":
                adj = line1_operational_overtake_adjustment(
                    tr, mode, si, ei, o["delay"], observations, exact_obs=o
                )
                board_extra = max(0, adj["board_delay"] - float(o["delay"] or 0))
                end_extra = max(0, adj["end_delay"] - float(o["delay"] or 0))
                board_dt += timedelta(seconds=board_extra)
                alight_dt += timedelta(seconds=end_extra)
                operation_notices = adj["notices"]
                operational_hold_seconds = adj["extra_hold_seconds"]
                projected_delay_seconds = round(adj["end_delay"])
            elif tr.get("service") == "express":
                operation_notices = line1_express_overtake_notices(tr, mode, si, ei)

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
            "projected_delay_seconds": projected_delay_seconds,
            "operational_hold_seconds": operational_hold_seconds,
            "operation_notices": operation_notices,
            "delay_source": "live_exact",
            "observed_at": o["observed"].strftime("%Y-%m-%d %H:%M:%S"),
            "data_age_seconds": max(0, round((now - o["observed"]).total_seconds())),
            "current_station": o["current_station"],
            "status": o["status"],
            "location_kind": "live",
            "location_label": f"{o['current_station']} {o['status']}",
            "confidence": "높음",
            "method": "실시간 열차 위치 + 열차별 공식 시간표" + (" + 계획 추월 운행 반영" if operation_notices else ""),
            "projected": False,
        })
    out.sort(key=lambda x: (x["alight_dt"], x["board_dt"]))
    return out

def static_projected_candidates(line, mode, start, end, ready_dt, observations):
    # V14.8: 1호선은 급행-완행 계획 추월까지 반영해 승차/하차 지연을 구간별로 계산한다.
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
        delay, delay_source, exact_obs = delay_for_train(tr, observations)

        board_delay = float(delay or 0)
        end_delay = float(delay or 0)
        operation_notices = []
        operational_hold_seconds = 0
        if line == "1호선":
            if tr.get("service") == "local":
                adj = line1_operational_overtake_adjustment(
                    tr, mode, si, ei, delay, observations, exact_obs=exact_obs
                )
                board_delay = adj["board_delay"]
                end_delay = adj["end_delay"]
                operation_notices = adj["notices"]
                operational_hold_seconds = adj["extra_hold_seconds"]
            elif tr.get("service") == "express":
                operation_notices = line1_express_overtake_notices(tr, mode, si, ei)

        board_dt = schedule_dt_after(bsec, ready_dt, board_delay)
        if not board_dt:
            continue
        if (board_dt - ready_dt).total_seconds() > 4 * 3600:
            continue
        # 승차역 이전 추월은 board_delay에, 승차 후 추월은 end_delay에만 반영한다.
        alight_dt = board_dt + timedelta(seconds=(asec - bsec) + (end_delay - board_delay))
        raw.append((
            alight_dt, board_dt, tr, delay, delay_source, exact_obs,
            round(end_delay), operational_hold_seconds, operation_notices,
        ))

    raw.sort(key=lambda x: (x[0], x[1]))
    out = []
    for (
        alight_dt, board_dt, tr, delay, delay_source, exact_obs,
        projected_delay_seconds, operational_hold_seconds, operation_notices,
    ) in raw[:30]:
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
            "projected_delay_seconds": projected_delay_seconds,
            "operational_hold_seconds": operational_hold_seconds,
            "operation_notices": operation_notices,
            "delay_source": delay_source,
            "observed_at": exact_obs["observed"].strftime("%Y-%m-%d %H:%M:%S") if exact_obs else "",
            "data_age_seconds": exact_obs.get("cache_age_seconds", 0) if exact_obs else 0,
            "current_station": expected_location.get("station", ""),
            "status": expected_location.get("status", ""),
            "location_kind": "expected",
            "location_label": expected_location.get("label", "예상 소재 계산 중"),
            "confidence": "중간" if delay_source in ("cached_exact", "live_median", "live_context") else ("높음" if delay_source == "live_exact" else "낮음"),
            "method": (
                "최근 실시간 지연 캐시 + 공식 시간표" if delay_source == "cached_exact" else
                "실시간 위치 + 시간표 문맥 매칭" if delay_source == "live_context" else
                "공식 시간표 + 현재 동일방향 지연 중앙값" if delay_source == "live_median" else
                "공식 시간표만 사용"
            ) + (" + 계획 추월 운행 반영" if operation_notices else ""),
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

        delay, delay_source, exact_obs = delay_for_train(tr, observations)
        board_delay = float(delay or 0)
        end_delay = float(delay or 0)
        operation_notices = []
        operational_hold_seconds = 0
        if line == "1호선":
            if tr.get("service") == "local":
                adj = line1_operational_overtake_adjustment(
                    tr, mode, si, ei, delay, observations, exact_obs=exact_obs
                )
                board_delay = adj["board_delay"]
                end_delay = adj["end_delay"]
                operation_notices = adj["notices"]
                operational_hold_seconds = adj["extra_hold_seconds"]
            elif tr.get("service") == "express":
                operation_notices = line1_express_overtake_notices(tr, mode, si, ei)
        board_dt = schedule_dt_before(bsec, ready_dt, board_delay)
        if not board_dt:
            continue
        alight_dt = board_dt + timedelta(seconds=(asec - bsec) + (end_delay - board_delay))

        # 직전 열차는 '현재도 운행 중인가'가 아니라 ready_dt 직전에 실제로
        # 출발했던 열차인가가 기준이다. 짧은 구간에서는 이미 목적지에 도착한
        # 열차도 정상적인 직전 열차이므로 도착 후 2분 필터를 적용하지 않는다.
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
            "projected_delay_seconds": round(end_delay),
            "operational_hold_seconds": operational_hold_seconds,
            "operation_notices": operation_notices,
            "delay_source": delay_source,
            "observed_at": exact_obs["observed"].strftime("%Y-%m-%d %H:%M:%S") if exact_obs else "",
            "data_age_seconds": exact_obs.get("cache_age_seconds", 0) if exact_obs else 0,
            "current_station": expected_location.get("station", ""),
            "status": expected_location.get("status", ""),
            "location_kind": "expected",
            "location_label": expected_location.get("label", "예상 소재 계산 중"),
            "confidence": "중간" if observations else "낮음",
            "method": ("직전 열차 추정 · 공식 시간표 + 현재 지연" if observations else "직전 열차 추정 · 공식 시간표") + (" + 계획 추월 운행 반영" if operation_notices else ""),
            "projected": True,
        }
        if best is None or c["board_dt"] > best["board_dt"]:
            best = c
    return best


def public_candidate(c, selected=False):
    line = canon_line(c.get("line", ""))
    train_no = c.get("train_no", "")
    visible = public_train_no_supported(line)
    realtime_ok = realtime_supported(line)
    return {
        "line": line,
        "train_no": train_no,
        "display_train_no": train_no if visible else "",
        "train_no_visible": bool(visible),
        "realtime_supported": bool(realtime_ok),
        "data_status": str(line_capabilities(line).get("data_status") or "complete"),
        "vehicle_id": c.get("vehicle_id", ""),
        "external_train_no": c.get("external_train_no", ""),
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
        "delay_seconds": c.get("delay_seconds"),
        "projected_delay_seconds": c.get("projected_delay_seconds", c.get("delay_seconds")),
        "operational_hold_seconds": c.get("operational_hold_seconds", 0),
        "operation_notices": c.get("operation_notices", []),
        "delay_source": c.get("delay_source", ""),
        "delay_available": c.get("delay_available", line not in SCHEDULE_ONLY_LINES),
        "observed_at": c.get("observed_at", ""),
        "data_age_seconds": c.get("data_age_seconds", 0),
        "is_previous": bool(c.get("is_previous")),
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
    line = canon_line(line)
    if line not in LINE_NAMES:
        return {"ok": False, "error": f"지원하지 않는 노선: {line}"}
    if canon_station(start) not in {canon_station(x) for x in STATIONS_BY_LINE[line]}:
        return {"ok": False, "error": f"{line} 시간표에서 승차역 '{start}'을 찾지 못했습니다."}
    if canon_station(end) not in {canon_station(x) for x in STATIONS_BY_LINE[line]}:
        return {"ok": False, "error": f"{line} 시간표에서 하차역 '{end}'을 찾지 못했습니다."}

    if line == "신분당선":
        return calculate_sinbundang_segment(mode, start, end, ready_dt, position_cache)

    positions, realtime_error, realtime_available = cached_position_rows(line, position_cache)
    raw_observations, diag = observe_delays(line, mode, positions, cached_delay_rows(position_cache))
    diag["realtime_available"] = realtime_available
    cached_meta = position_cache.get(line, {}) if isinstance(position_cache, dict) else {}
    if isinstance(cached_meta, dict) and cached_meta.get("query"):
        diag["realtime_query"] = cached_meta.get("query")
    if realtime_error:
        diag["realtime_error"] = realtime_error

    observations = raw_observations
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
    # 후보 열차군 맨 앞에 추천 열차의 직전 열차 1대를 항상 추가.
    # 기본 추천 선정에는 참여하지 않고, 사용자가 예상보다 빨리 도착했을 때 선택할 수 있게만 한다.
    if previous and str(previous.get("train_no")) not in {str(x.get("train_no")) for x in public}:
        previous = dict(previous)
        previous["is_previous"] = True
        public.insert(0, public_candidate(previous))
    return {
        "ok": True,
        "chosen": chosen,
        "candidates": near[:10],
        "public_candidates": public,
        "previous_candidate": public_candidate(previous) if previous else None,
        "diagnostics": diag,
    }

def _sinbundang_virtual_run_by_id(mode, run_id):
    return _sinbundang_virtual_runs(mode).get(str(run_id or ""))


def _sinbundang_scheduled_event_dt(run, base_dt, station, raw_status=""):
    st = canon_station(station)
    for stop in run.get("stops", []):
        if not bool(stop.get("call", True)) or canon_station(stop.get("station")) != st:
            continue
        # 출발 상태면 dep, 진입/도착/전역출발이면 arr 우선. 없는 쪽으로 fallback.
        if str(raw_status) == "2":
            sec = stop.get("dep") if stop.get("dep") is not None else stop.get("arr")
        else:
            sec = stop.get("arr") if stop.get("arr") is not None else stop.get("dep")
        return base_dt + timedelta(seconds=int(sec)) if sec is not None else None
    return None


def _sinbundang_match_live_vehicle_to_virtual_run(run, base_dt, positions):
    """
    익명 가상 운행편과 실시간 차량을 방향+현재역+시각으로 보조 연결한다.
    공개 열차번호 매칭이 아니므로 충분히 가까운 단일 후보일 때만 채택한다.
    """
    direction = run.get("direction", "")
    scored = []
    for row in positions:
        if str(row.get("subwayId") or "1077").strip() not in {"", "1077"}:
            continue
        if _api_direction_to_schedule("신분당선", row.get("updnLine")) != direction:
            continue
        current = canon_station(row.get("statnNm"))
        if not current:
            continue
        observed = parse_dt(row.get("recptnDt") or row.get("lastRecptnDt"))
        if not observed:
            continue
        sched = _sinbundang_scheduled_event_dt(run, base_dt, current, row.get("trainSttus"))
        if not sched:
            continue
        delta = abs((observed - sched).total_seconds())
        # 배차 간격보다 넓게 잡으면 인접 운행편과 뒤섞인다. 4분 이내만 보조 연결.
        if delta <= 240:
            scored.append((delta, row))
    if not scored:
        return None
    scored.sort(key=lambda x:x[0])
    # 2개 차량이 거의 같은 점수면 어떤 물리 차량인지 모호하므로 연결하지 않는다.
    if len(scored) > 1 and scored[1][0] - scored[0][0] < 75:
        return None
    return scored[0][1]


def tracked_sinbundang_virtual_segment(mode, start, end, run_id, position_cache, boarded_at=None):
    """시간표 기반 익명 운행편을 고정 추적하고, 가능할 때만 실시간 차량 소재를 보조 결합한다."""
    now = now_kst()
    start, end = canon_station(start), canon_station(end)
    run = _sinbundang_virtual_run_by_id(mode, run_id)
    if not run:
        return {"ok":False,"error":"선택한 신분당선 가상 운행편을 시간표에서 찾지 못했습니다."}
    pair = _sinbundang_run_pair(run, start, end)
    if not pair:
        return {"ok":False,"error":f"선택한 신분당선 운행편이 {start}→{end} 구간을 운행하지 않습니다."}
    _, _, board_sec, alight_sec = pair
    board_dt = None
    if boarded_at:
        try:
            board_dt = datetime.strptime(boarded_at, "%Y-%m-%d %H:%M:%S")
        except Exception:
            board_dt = None
    if board_dt is None:
        # 현재와 가장 가까운 해당 운행편 발생시각을 선택한다.
        before = schedule_dt_before(board_sec, now + timedelta(seconds=5), 0)
        after = schedule_dt_after(board_sec, now, 0)
        opts = [x for x in (before, after) if x]
        board_dt = min(opts, key=lambda x:abs((x-now).total_seconds())) if opts else now
    base_dt = board_dt - timedelta(seconds=board_sec)
    scheduled_alight = base_dt + timedelta(seconds=alight_sec)
    expected = _sinbundang_virtual_location(run, base_dt, now)

    positions, realtime_error, realtime_available = cached_position_rows("신분당선", position_cache)
    live_row = _sinbundang_match_live_vehicle_to_virtual_run(run, base_dt, positions)
    diag = {
        "positions":len(positions), "tracked_virtual_run_id":str(run_id),
        "matched_vehicle_id":str((live_row or {}).get("trainNo") or ""),
        "realtime_available":realtime_available, "realtime_error":realtime_error or "",
        "strategy":"virtual_public_timetable_plus_optional_vehicle_position", "delay_inference":False,
    }

    if live_row:
        current = canon_station(live_row.get("statnNm"))
        status = status_name(live_row.get("trainSttus")) or "위치확인"
        observed = parse_dt(live_row.get("recptnDt") or live_row.get("lastRecptnDt"))
        direction = _api_direction_to_schedule("신분당선", live_row.get("updnLine")) or run.get("direction", "")
        eta_info = _sinbundang_vehicle_eta_to(live_row, end)
        if eta_info:
            alight_dt = max(now, eta_info["eta"])
            stations=[canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations",[])]
            passed=False
            if current in stations and end in stations:
                ci,ei=stations.index(current),stations.index(end)
                raw=str(live_row.get("trainSttus") or "").strip()
                passed=(direction=="DOWN" and ci>ei) or (direction=="UP" and ci<ei) or (ci==ei and raw in {"1","2"})
            if passed:
                alight_dt=now
            return {"ok":True,"arrived":bool(passed),"chosen":{
                "line":"신분당선","from":start,"to":end,"train_no":str(run_id),"virtual_run_id":str(run_id),
                "vehicle_id":str(live_row.get("trainNo") or ""),"external_train_no":"",
                "service":"local","direction":direction,"origin":canon_station(run.get("start")),"destination":canon_station(run.get("dest")),
                "board_dt":board_dt,"alight_dt":alight_dt,"wait_seconds":0,
                "ride_seconds":max(0,round((alight_dt-board_dt).total_seconds())),
                "remaining_seconds":max(0,round((alight_dt-now).total_seconds())),
                "delay_seconds":None,"delay_source":"vehicle_tracking","delay_available":False,
                "observed_at":observed.strftime("%Y-%m-%d %H:%M:%S"),"data_age_seconds":max(0,round((now-observed).total_seconds())),
                "current_station":current,"status":status,"location_kind":"live","location_label":f"{current} {status}".strip(),
                "confidence":"중간","method":"가상 운행편 + 실시간 차량 소재 + 공식 역간 소요시간",
                "projected":False,"tracking":True,"arrived":bool(passed),
            },"diagnostics":diag}

    arrived = now >= scheduled_alight
    shown_alight = scheduled_alight if not arrived else now
    return {"ok":True,"arrived":arrived,"chosen":{
        "line":"신분당선","from":start,"to":end,"train_no":str(run_id),"virtual_run_id":str(run_id),
        "vehicle_id":"","external_train_no":"","service":"local","direction":run.get("direction", sinbundang_direction(start,end)),
        "origin":canon_station(run.get("start")),"destination":canon_station(run.get("dest")),
        "board_dt":board_dt,"alight_dt":shown_alight,"wait_seconds":0,
        "ride_seconds":max(0,round((scheduled_alight-board_dt).total_seconds())),
        "remaining_seconds":max(0,round((scheduled_alight-now).total_seconds())),
        "delay_seconds":None,"delay_source":"schedule_virtual_tracking","delay_available":False,
        "observed_at":"","data_age_seconds":0,
        "current_station":expected.get("station", ""),"status":expected.get("status", ""),
        "location_kind":"expected","location_label":expected.get("label", "예상 소재 계산 중"),
        "confidence":"낮음","method":"공개 역별 시각표 가상 운행편 고정 추적",
        "projected":True,"tracking":True,"arrived":arrived,
    },"diagnostics":diag}


def tracked_sinbundang_vehicle_segment(mode, start, end, vehicle_id, position_cache, boarded_at=None):
    """신분당선은 실시간 차량 ID 또는 익명 가상 운행편 ID를 고정 추적한다."""
    if str(vehicle_id or "").startswith("SBV-"):
        return tracked_sinbundang_virtual_segment(mode, start, end, vehicle_id, position_cache, boarded_at)
    now = now_kst()
    start, end = canon_station(start), canon_station(end)
    ride_seconds = sinbundang_runtime_seconds(start, end)
    if ride_seconds is None:
        return {"ok":False,"error":f"신분당선 {start}→{end} 소요시간을 계산하지 못했습니다."}
    board_dt = None
    if boarded_at:
        try: board_dt = datetime.strptime(boarded_at, "%Y-%m-%d %H:%M:%S")
        except Exception: board_dt = None
    positions, realtime_error, realtime_available = cached_position_rows("신분당선", position_cache)
    rows=[r for r in positions if norm_train(r.get("trainNo")) == norm_train(vehicle_id)]
    row=max(rows, key=lambda r: parse_dt(r.get("recptnDt") or r.get("lastRecptnDt")), default=None)
    diag={
        "positions":len(positions),"tracked_vehicle_id":str(vehicle_id),"vehicle_found":bool(row),
        "realtime_available":realtime_available,"realtime_error":realtime_error or "",
        "strategy":"tracked_vehicle_position_plus_official_runtime","delay_inference":False,
    }
    cached_meta=position_cache.get("신분당선",{}) if isinstance(position_cache,dict) else {}
    if isinstance(cached_meta,dict) and cached_meta.get("query"):
        diag["realtime_query"]=cached_meta.get("query")

    if row:
        current=canon_station(row.get("statnNm"))
        direction=_api_direction_to_schedule("신분당선", row.get("updnLine")) or sinbundang_direction(start,end)
        status=status_name(row.get("trainSttus")) or "위치확인"
        observed=parse_dt(row.get("recptnDt") or row.get("lastRecptnDt"))
        stations=[canon_station(x) for x in SINBUNDANG_RUNTIME.get("stations",[])]
        if current in stations and end in stations:
            ci,ei=stations.index(current),stations.index(end)
            raw=str(row.get("trainSttus") or "").strip()
            passed=(direction=="DOWN" and ci>ei) or (direction=="UP" and ci<ei)
            at_done=(ci==ei and raw in {"1","2"})
            if passed or at_done:
                return {"ok":True,"arrived":True,"chosen":{
                    "line":"신분당선","from":start,"to":end,"train_no":str(vehicle_id),"vehicle_id":str(vehicle_id),
                    "service":"local","direction":direction,"origin":"","destination":canon_station(row.get("statnTnm")),
                    "board_dt":board_dt or now,"alight_dt":now,"wait_seconds":0,"ride_seconds":0,"remaining_seconds":0,
                    "delay_seconds":None,"delay_source":"vehicle_tracking","delay_available":False,
                    "observed_at":observed.strftime("%Y-%m-%d %H:%M:%S"),"data_age_seconds":max(0,round((now-observed).total_seconds())),
                    "current_station":current,"status":status,"location_kind":"live","location_label":f"{current} {status}".strip(),
                    "confidence":"중간","method":"탑승 차량 실시간 소재 + 운영사 공식 역간 소요시간","projected":False,
                    "tracking":True,"arrived":True,
                },"diagnostics":diag}

        eta_info=_sinbundang_vehicle_eta_to(row,end)
        if eta_info:
            alight_dt=max(now,eta_info["eta"])
            shown_board=board_dt or now
            if shown_board>alight_dt: shown_board=alight_dt
            return {"ok":True,"arrived":False,"chosen":{
                "line":"신분당선","from":start,"to":end,"train_no":str(vehicle_id),"vehicle_id":str(vehicle_id),
                "service":"local","direction":direction,"origin":"","destination":canon_station(row.get("statnTnm")),
                "board_dt":shown_board,"alight_dt":alight_dt,"wait_seconds":0,
                "ride_seconds":max(0,round((alight_dt-shown_board).total_seconds())),
                "remaining_seconds":max(0,round((alight_dt-now).total_seconds())),
                "delay_seconds":None,"delay_source":"vehicle_tracking","delay_available":False,
                "observed_at":eta_info["observed"].strftime("%Y-%m-%d %H:%M:%S"),
                "data_age_seconds":max(0,round((now-eta_info["observed"]).total_seconds())),
                "current_station":eta_info["current_station"],"status":eta_info["status"],
                "location_kind":"live","location_label":f"{eta_info['current_station']} {eta_info['status']}".strip(),
                "confidence":"중간","method":"탑승 차량 실시간 소재 + 운영사 공식 역간 소요시간",
                "projected":False,"tracking":True,"arrived":False,
            },"diagnostics":diag}

    # 차량이 순간 미포착되면 탑승시각 + 운영사 공식 소요시간으로 임시 유지한다.
    shown_board=board_dt or now
    target_dt=shown_board+timedelta(seconds=ride_seconds)
    if target_dt<now: target_dt=now
    return {"ok":True,"arrived":False,"chosen":{
        "line":"신분당선","from":start,"to":end,"train_no":str(vehicle_id),"vehicle_id":str(vehicle_id),
        "service":"local","direction":sinbundang_direction(start,end),"origin":"","destination":"",
        "board_dt":shown_board,"alight_dt":target_dt,"wait_seconds":0,
        "ride_seconds":max(0,round((target_dt-shown_board).total_seconds())),
        "remaining_seconds":max(0,round((target_dt-now).total_seconds())),
        "delay_seconds":None,"delay_source":"vehicle_tracking_fallback","delay_available":False,
        "observed_at":"","data_age_seconds":0,"current_station":"","status":"위치 재포착 대기",
        "location_kind":"expected","location_label":"차량 위치 재포착 대기","confidence":"낮음",
        "method":"탑승 차량 잠금 + 운영사 공식 소요시간 fallback","projected":True,"tracking":True,"arrived":False,
    },"diagnostics":diag}


def tracked_train_segment(line, mode, start, end, train_no, position_cache, boarded_at=None):
    """
    사용자가 실제 탑승했다고 표시한 열차를 잠금 추적한다.
    다른 후보 열차로 절대 교체하지 않고 지정 train_no만 따라간다.
    """
    line = canon_line(line)
    now = now_kst()
    if line == "신분당선":
        return tracked_sinbundang_vehicle_segment(mode, start, end, train_no, position_cache, boarded_at)
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
    observations, diag = observe_delays(line, mode, positions, cached_delay_rows(position_cache))
    diag["realtime_available"] = realtime_available
    cached_meta = position_cache.get(line, {}) if isinstance(position_cache, dict) else {}
    if isinstance(cached_meta, dict) and cached_meta.get("query"):
        diag["realtime_query"] = cached_meta.get("query")
    if realtime_error:
        diag["realtime_error"] = realtime_error
    wanted_numbers = {norm_train(tr.get("train_no"))}
    if tr.get("continuation_train_no"):
        wanted_numbers.add(norm_train(tr.get("continuation_train_no")))

    # 열번이 응암/성수에서 바뀌어도 같은 물리 차량의 후속 열번까지 추적한다.
    live_options = [o for o in observations if o.get("source_kind") == "live" and norm_train(o.get("train_no")) in wanted_numbers]
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
                    "delay_source": "live_exact",
                    "observed_at": live["observed"].strftime("%Y-%m-%d %H:%M:%S"),
                    "data_age_seconds": max(0, round((now - live["observed"]).total_seconds())),
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
                operation_notices = []
                operational_hold_seconds = 0
                projected_delay_seconds = round(live["delay"])
                if line == "1호선":
                    if tr.get("service") == "local":
                        adj = line1_operational_overtake_adjustment(
                            tr, mode, si, ei, live["delay"], observations, exact_obs=live
                        )
                        extra_after_observation = max(0, adj["end_delay"] - float(live["delay"] or 0))
                        remaining += extra_after_observation
                        operation_notices = adj["notices"]
                        operational_hold_seconds = round(extra_after_observation)
                        projected_delay_seconds = round(adj["end_delay"])
                    elif tr.get("service") == "express":
                        operation_notices = line1_express_overtake_notices(tr, mode, si, ei)

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
                        "projected_delay_seconds": projected_delay_seconds,
                        "operational_hold_seconds": operational_hold_seconds,
                        "operation_notices": operation_notices,
                        "delay_source": "live_exact",
                        "observed_at": live["observed"].strftime("%Y-%m-%d %H:%M:%S"),
                        "current_station": live["current_station"],
                        "location_kind": "live",
                        "location_label": f"{live['current_station']} {live['status']}",
                        "confidence": "높음",
                        "method": ("탑승 열차 연속운행 추적" if tr.get("physical_continuation") else "탑승 열차 실시간 고정 추적") + (" + 계획 추월 운행 반영" if operation_notices else ""),
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
    delay, delay_source, exact_obs = delay_for_train(tr, observations)
    # 연속운행 가상열차에서는 fallback 방향을 유지하되 exact 열번 캐시는 우선한다.
    if delay_source not in ("cached_exact", "live_exact"):
        delay = median_delay(observations, fallback_direction, tr["service"])
    operation_notices = []
    operational_hold_seconds = 0
    projected_delay = float(delay or 0)
    if line == "1호선":
        if tr.get("service") == "local":
            adj = line1_operational_overtake_adjustment(
                tr, mode, si, ei, delay, observations, exact_obs=exact_obs
            )
            projected_delay = adj["end_delay"]
            operational_hold_seconds = adj["extra_hold_seconds"]
            operation_notices = adj["notices"]
        elif tr.get("service") == "express":
            operation_notices = line1_express_overtake_notices(tr, mode, si, ei)
    expected_location = estimated_train_location(tr, now, delay)
    target_dt = nearest_schedule_dt(target_sec, now, projected_delay)
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
            "projected_delay_seconds": round(projected_delay),
            "operational_hold_seconds": operational_hold_seconds,
            "operation_notices": operation_notices,
            "delay_source": delay_source,
            "observed_at": exact_obs["observed"].strftime("%Y-%m-%d %H:%M:%S") if exact_obs else "",
            "data_age_seconds": exact_obs.get("cache_age_seconds", 0) if exact_obs else 0,
            "current_station": expected_location.get("station", ""),
            "location_kind": "expected",
            "location_label": expected_location.get("label", "예상 소재 계산 중"),
            "confidence": "중간" if delay_source in ("cached_exact", "live_median", "live_context") or observations else "낮음",
            "method": (
                "최근 실시간 지연 캐시로 추적 유지" if delay_source == "cached_exact" else
                (("연속운행 열차 잠금 · 실시간 위치 재포착 대기" if tr.get("physical_continuation") else "탑승 열차 잠금 · 실시간 위치 재포착 대기")
                 if realtime_available else ("공식 시간표 기반 추적 · 실시간 위치 API 미연동" if line in SCHEDULE_ONLY_LINES else "공식 시간표 기반 임시 추적 · 실시간 위치 조회 실패"))
            ) + (" + 계획 추월 운행 반영" if operation_notices else ""),
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
    attach_client_delay_cache(position_cache, payload)
    results = []
    warnings = []

    # 1) 현재 실제 탑승 중인 열차는 무조건 이 번호로 고정.
    active = segments[active_index]
    line = canon_line(active.get("line"))
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
    current["transfer_seconds"] = _segment_transfer_seconds(active)
    results.append(current)

    # 2) 현재 열차의 최신 도착 ETA + 환승시간을 다음 구간 ready 시각으로 사용.
    ready_dt = current["alight_dt"]
    if active_index < len(segments) - 1:
        ready_dt += timedelta(seconds=max(0, _segment_transfer_seconds(active)))

    # 3) 이후 모든 구간을 지금 시점에서 다시 탐색.
    for idx in range(active_index + 1, len(segments)):
        s = segments[idx]
        line = canon_line(s.get("line"))
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
        chosen["transfer_seconds"] = _segment_transfer_seconds(s)
        results.append(chosen)

        if chosen["confidence"] != "높음":
            warnings.append(
                f"{idx+1}구간 {line} {fr}→{to}: {chosen['method']} ({chosen['confidence']} 신뢰도)"
            )

        if idx < len(segments) - 1:
            ready_dt = chosen["alight_dt"] + timedelta(
                seconds=max(0, _segment_transfer_seconds(s))
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
    line = canon_line(d.get("line", ""))
    if line:
        visible = public_train_no_supported(line)
        d["line"] = line
        d["display_train_no"] = d.get("train_no", "") if visible else ""
        d["train_no_visible"] = bool(visible)
        d["realtime_supported"] = bool(realtime_supported(line))
        d["data_status"] = str(line_capabilities(line).get("data_status") or "complete")
        if line in SCHEDULE_ONLY_LINES:
            d["delay_available"] = False
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
    attach_client_delay_cache(position_cache, payload)

    results = []
    warnings = []

    for idx, s in enumerate(segments):
        line = canon_line(s.get("line"))
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
        chosen["transfer_seconds"] = _segment_transfer_seconds(s)
        results.append(chosen)

        if chosen["confidence"] != "높음":
            warnings.append(
                f"{idx+1}구간 {line} {fr}→{to}: {chosen['method']} ({chosen['confidence']} 신뢰도)"
            )

        transfer_seconds_value = max(0, _segment_transfer_seconds(s))
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

