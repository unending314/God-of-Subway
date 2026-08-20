# -*- coding: utf-8 -*-
import engine


EXPECTED_IMPORTED = {
    "신림선", "우이신설선", "GTX-A", "김포골드라인",
    "의정부경전철", "용인경전철", "인천1호선", "인천2호선",
}
EXPECTED_REALTIME = {"신림선", "우이신설선", "GTX-A"}
EXPECTED_SCHEDULE_ONLY = {"김포골드라인", "의정부경전철", "용인경전철", "인천1호선", "인천2호선"}


def test_uploaded_additional_lines_are_loaded():
    assert EXPECTED_IMPORTED <= set(engine.TIMETABLE_ADDITIONAL_LINES)
    assert EXPECTED_REALTIME <= set(engine.REALTIME_ADDITIONAL_LINES)
    assert EXPECTED_SCHEDULE_ONLY <= set(engine.SCHEDULE_ONLY_LINES)
    assert "인천1호선" in engine.TIMETABLE_ADDITIONAL_LINES


def test_capability_and_public_train_number_policy():
    for line in EXPECTED_REALTIME:
        assert engine.realtime_supported(line) is True
        assert engine.public_train_no_supported(line) is True
    for line in EXPECTED_SCHEDULE_ONLY:
        assert engine.realtime_supported(line) is False
        assert engine.public_train_no_supported(line) is False
        tr = engine.all_trains(line, "DAY")[0]
        public = engine.public_candidate({
            "line": line,
            "train_no": tr["train_no"],
            "board_dt": "2026-08-20 12:00:00",
            "alight_dt": "2026-08-20 12:10:00",
        })
        assert public["train_no_visible"] is False
        assert public["display_train_no"] == ""
        assert public["delay_available"] is False


def test_schedule_only_lines_never_call_realtime_api(monkeypatch):
    monkeypatch.setattr(
        engine,
        "fetch_position",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("source API called")),
    )
    cache = engine.prefetch_position_cache(list(EXPECTED_SCHEDULE_ONLY))
    for line in EXPECTED_SCHEDULE_ONLY:
        assert cache[line]["available"] is False
        assert cache[line]["realtime_source"] == "schedule"
        assert cache[line]["rows"] == []


def test_realtime_additional_train_ids_match_timetable_variants():
    for line in EXPECTED_REALTIME:
        tr = engine.all_trains(line, "DAY")[0]
        assert engine.get_train(line, "DAY", tr["train_no"]) is not None
        digits = "".join(ch for ch in str(tr["train_no"]) if ch.isdigit())
        if digits:
            assert engine.get_train(line, "DAY", digits) is not None


def test_gtx_a_north_south_are_disconnected():
    assert list(engine.route_trains("GTX-A", "DAY", "서울역", "운정중앙"))
    assert list(engine.route_trains("GTX-A", "DAY", "수서", "동탄"))
    assert list(engine.route_trains("GTX-A", "DAY", "서울역", "수서")) == []
    assert list(engine.route_trains("GTX-A", "DAY", "동탄", "운정중앙")) == []

    graph_edges = [row for row in engine.ROUTE_GRAPH["modes"]["DAY"] if row[0] == "GTX-A"]
    north = {"서울역", "연신내", "대곡", "킨텍스", "운정중앙"}
    south = {"수서", "성남", "구성", "동탄"}
    assert not [row for row in graph_edges if (row[1] in north and row[2] in south) or (row[1] in south and row[2] in north)]


def test_gtx_explicit_pass_service_is_preserved():
    tr = engine.get_train("GTX-A", "DAY", "X0812")
    assert tr is not None
    assert tr["start"] == "서울역"
    assert tr["dest"] == "운정중앙"
    passed = {s["station"] for s in tr["stops"] if not s.get("call", True)}
    assert {"연신내", "대곡", "킨텍스"} <= passed
    assert tr["service"] == "express"
    assert "X0812" not in {x["train_no"] for x in engine.route_trains("GTX-A", "DAY", "연신내", "운정중앙")}


def test_station_sets_and_aliases():
    assert engine.canon_line("김포도시철도") == "김포골드라인"
    assert engine.canon_line("용인에버라인") == "용인경전철"
    assert {"서울역", "연신내", "대곡", "킨텍스", "운정중앙", "수서", "성남", "구성", "동탄"} <= set(engine.STATIONS_BY_LINE["GTX-A"])


def test_incheon_lines_are_complete_for_service():
    caps1 = engine.line_capabilities("인천1호선")
    caps2 = engine.line_capabilities("인천2호선")
    assert caps1["data_status"] == "complete"
    assert caps2["data_status"] == "complete_with_interpolation"
    assert {"송도달빛축제공원", "검단호수공원"} <= set(engine.STATIONS_BY_LINE["인천1호선"])
    assert {"운연", "검단오류", "서해구청"} <= set(engine.STATIONS_BY_LINE["인천2호선"])
    assert "서구청" not in engine.STATIONS_BY_LINE["인천2호선"]
    assert engine.canon_station("서구청") == "서해구청"


def test_incheon2_interpolated_seohaegu_office_is_callable():
    trains = list(engine.route_trains("인천2호선", "DAY", "서해구청", "아시아드경기장"))
    assert trains
    assert any(any(s["station"] == "서해구청" and s.get("call", True) for s in tr["stops"]) for tr in trains)


def test_imported_line_route_graph_matches_callable_timetable():
    for line in EXPECTED_IMPORTED:
        for mode in ("DAY", "SAT", "END"):
            expected = {}
            for tr in engine.all_trains(line, mode):
                calls = [s for s in tr.get("stops", []) if s.get("call", True)]
                for a, b in zip(calls, calls[1:]):
                    dep = engine.stop_board_sec(a)
                    arr = engine.stop_alight_sec(b)
                    if dep is None or arr is None:
                        continue
                    while arr < dep:
                        arr += 86400
                    key = (engine.canon_station(a["station"]), engine.canon_station(b["station"]))
                    sec = arr - dep
                    if key not in expected or sec < expected[key]:
                        expected[key] = sec
            actual = {
                (a, b): sec
                for graph_line, a, b, sec in engine.ROUTE_GRAPH["modes"][mode]
                if graph_line == line
            }
            assert actual == expected, (line, mode)


def test_auto_path_can_use_each_imported_line():
    cases = (
        ("관악산", "강남", "신림선"),
        ("북한산우이", "종로3가", "우이신설선"),
        ("운정중앙", "강남", "GTX-A"),
        ("동탄", "강남", "GTX-A"),
        ("양촌", "서울역", "김포골드라인"),
        ("탑석", "서울역", "의정부경전철"),
        ("전대·에버랜드", "강남", "용인경전철"),
        ("검단호수공원", "서울역", "인천1호선"),
        ("검단오류", "서울역", "인천2호선"),
    )
    for start, end, expected_line in cases:
        path = engine.auto_find_path(start, end, "DAY")
        assert path, (start, end)
        used = {node[0] for edge in path["edges"] for node in edge[:2]}
        assert expected_line in used, (start, end, used)
