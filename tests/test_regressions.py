# -*- coding: utf-8 -*-
import unittest
from datetime import datetime

import engine


class ExpressPassStationRegressionTest(unittest.TestCase):
    def test_dangjeong_never_served_by_express(self):
        for mode in ("DAY", "END"):
            for start, end in (("당정", "안양"), ("안양", "당정"), ("당정", "수원"), ("수원", "당정")):
                express = [
                    tr["train_no"]
                    for tr in engine.route_trains("1호선", mode, start, end)
                    if tr.get("service") == "express"
                ]
                self.assertEqual(express, [], f"{mode} {start}->{end}: {express[:5]}")

    def test_all_line1_express_pass_times_are_non_callable(self):
        for mode in ("DAY", "END"):
            bad = []
            for tr in engine.all_trains("1호선", mode):
                if tr.get("service") != "express":
                    continue
                stops = tr.get("stops", [])
                for i, stop in enumerate(stops):
                    if 0 < i < len(stops) - 1 and stop.get("arr") is None and stop.get("dep") is not None and stop.get("call"):
                        bad.append((tr.get("train_no"), stop.get("station")))
            self.assertEqual(bad, [])


    def test_line1_route_graph_matches_callable_timetable(self):
        for mode in ("DAY", "SAT", "END"):
            expected = {}
            for tr in engine.all_trains("1호선", mode):
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
                for line, a, b, sec in engine.ROUTE_GRAPH["modes"][mode]
                if line == "1호선"
            }
            self.assertEqual(actual, expected)


    def test_route_api_logic_dangjeong_does_not_choose_express(self):
        original = engine.prefetch_position_cache
        engine.prefetch_position_cache = lambda lines, timeout=5: {
            line: {"rows": [], "error": "test schedule only", "available": False}
            for line in set(lines) if line
        }
        try:
            for start, end in (("당정", "안양"), ("안양", "당정")):
                result = engine.calculate_route({
                    "start_time": "2026-08-18 08:35:00",
                    "day": "DAY",
                    "segments": [{"line": "1호선", "from": start, "to": end, "transfer_walk": 0}],
                })
                self.assertTrue(result.get("ok"), result)
                self.assertNotEqual(result["segments"][0].get("service"), "express", result)
        finally:
            engine.prefetch_position_cache = original

    def test_schedule_only_recommendation_at_dangjeong_is_local(self):
        cache = {"1호선": {"rows": [], "error": "test schedule only", "available": False}}
        cases = (
            ("당정", "안양", datetime(2026, 8, 18, 8, 35)),
            ("안양", "당정", datetime(2026, 8, 18, 8, 30)),
            ("당정", "수원", datetime(2026, 8, 18, 8, 35)),
        )
        for start, end, ready in cases:
            result = engine.calculate_segment("1호선", "DAY", start, end, ready, cache)
            self.assertTrue(result.get("ok"), result)
            self.assertNotEqual(result["chosen"].get("service"), "express", result["chosen"])

    def test_structural_skip_stop_inference_across_korail_lines(self):
        known = (
            ("1호선", "K1602", {"월계", "녹천", "방학"}),
            ("경의중앙선", "K5702", {"효창공원앞", "서강대", "수색"}),
            ("수인분당선", "K6402", {"매교", "매탄권선", "영통"}),
        )
        for line, train_no, expected_pass in known:
            tr = engine.get_train(line, "DAY", train_no)
            self.assertIsNotNone(tr, (line, train_no))
            self.assertEqual(tr.get("service"), "express", (line, train_no, tr))
            actual_pass = {s["station"] for s in tr["stops"] if not s.get("call", True)}
            self.assertTrue(expected_pass <= actual_pass, (line, train_no, actual_pass))

    def test_skipped_passenger_station_never_boards_that_express(self):
        cases = (
            ("1호선", "K1602", "월계", "의정부"),
            ("경의중앙선", "K5702", "효창공원앞", "공덕"),
            ("수인분당선", "K6402", "매교", "수원"),
        )
        for line, train_no, start, end in cases:
            trains = list(engine.route_trains(line, "DAY", start, end))
            self.assertNotIn(train_no, {tr["train_no"] for tr in trains}, (line, start, end))

    def test_schedule_only_skipped_station_recommendations_are_local(self):
        cases = (
            ("경의중앙선", "효창공원앞", "공덕", datetime(2026, 8, 18, 7, 40)),
            ("수인분당선", "매교", "수원", datetime(2026, 8, 18, 6, 35)),
            ("1호선", "월계", "의정부", datetime(2026, 8, 18, 6, 40)),
        )
        for line, start, end, ready in cases:
            cache = {line: {"rows": [], "error": "test schedule only", "available": False}}
            result = engine.calculate_segment(line, "DAY", start, end, ready, cache)
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["chosen"].get("service"), "local", result["chosen"])

    def test_all_korail_passenger_pass_times_are_non_callable(self):
        lines = ("1호선",) + engine.EXTRA_LINES
        for mode in ("DAY", "END"):
            for line in lines:
                passenger = (
                    engine.S1_PASSENGER_STATIONS
                    if line == "1호선"
                    else {engine.canon_station(x) for x in engine.EXTRA[line].get("stations", [])}
                )
                bad = []
                for tr in engine.all_trains(line, mode):
                    stops = tr.get("stops", [])
                    for i, stop in enumerate(stops):
                        if (
                            0 < i < len(stops) - 1
                            and stop.get("station") in passenger
                            and stop.get("arr") is None
                            and stop.get("dep") is not None
                            and stop.get("call", True)
                        ):
                            bad.append((tr.get("train_no"), stop.get("station")))
                self.assertEqual(bad, [], f"{line} {mode}: {bad[:10]}")

    def test_route_graph_matches_all_korail_normalized_timetables(self):
        for mode in ("DAY", "SAT", "END"):
            expected = {}
            korail_lines = ("1호선",) + engine.EXTRA_LINES
            for line in korail_lines:
                for tr in engine.all_trains(line, mode):
                    calls = [s for s in tr.get("stops", []) if s.get("call", True)]
                    for a, b in zip(calls, calls[1:]):
                        dep = engine.stop_board_sec(a)
                        arr = engine.stop_alight_sec(b)
                        if dep is None or arr is None:
                            continue
                        while arr < dep:
                            arr += 86400
                        key = (line, engine.canon_station(a["station"]), engine.canon_station(b["station"]))
                        sec = arr - dep
                        if key not in expected or sec < expected[key]:
                            expected[key] = sec
            actual = {
                (line, a, b): sec
                for line, a, b, sec in engine.ROUTE_GRAPH["modes"][mode]
                if line in korail_lines
            }
            self.assertEqual(actual, expected)

    def test_raw_timetable_integrity_is_clean(self):
        report = engine.timetable_integrity_report()
        self.assertTrue(report.get("ok"), report)
        self.assertEqual(report.get("issue_count"), 0, report)

    def test_operational_points_are_not_user_station_options(self):
        self.assertNotIn("마전", engine.STATIONS_BY_LINE["1호선"])
        self.assertNotIn("수색직결선", engine.STATIONS_BY_LINE["공항철도"])


class ShinbundangTimetableRegressionTest(unittest.TestCase):
    def test_imported_train_counts_and_station_options(self):
        self.assertEqual(len(engine.SINBUNDANG["trains"]["weekday"]), 326)
        self.assertEqual(len(engine.SINBUNDANG["trains"]["holiday"]), 272)
        self.assertEqual(len(engine.STATIONS_BY_LINE["신분당선"]), 16)
        for operational in ("광교기지", "분당연결선분기", "판교주박기지"):
            self.assertNotIn(operational, engine.STATIONS_BY_LINE["신분당선"])

    def test_weekday_first_down_train_matches_uploaded_timetable(self):
        tr = engine.get_train("신분당선", "DAY", "DX9003")
        self.assertIsNotNone(tr)
        self.assertEqual(tr["start"], "신사")
        self.assertEqual(tr["dest"], "광교")
        by_station = {s["station"]: s for s in tr["stops"]}
        self.assertEqual(by_station["신사"]["dep"], 5 * 3600 + 30 * 60)
        self.assertEqual(by_station["강남"]["arr"], 20066)
        self.assertTrue(by_station["정자"]["call"])

    def test_weekend_sat_and_end_use_same_uploaded_profile(self):
        sat = engine.get_train("신분당선", "SAT", "DX0601")
        end = engine.get_train("신분당선", "END", "DX0601")
        self.assertEqual(sat, end)
        self.assertEqual(sat["stops"][0]["station"], "신사")

    def test_sinbundang_realtime_line_id_and_prefetch(self):
        self.assertEqual(engine.LINE_IDS.get("신분당선"), "1077")
        self.assertNotIn("신분당선", engine.SCHEDULE_ONLY_LINES)

        sample_row = {
            "subwayId": "1077",
            "subwayNm": "신분당선",
            "statnNm": "강남",
            # API가 DX 접두사 없이 숫자 열번을 주더라도 digit fallback으로 매칭되어야 한다.
            "trainNo": "9003",
            "recptnDt": "2026-08-19 05:34:26",
            "trainSttus": "1",
            "updnLine": "1",
            "statnTnm": "광교",
        }
        original = engine.fetch_position
        calls = []
        def fake_fetch(line, timeout=5):
            calls.append((line, timeout))
            return True, None, {"realtimePositionList": [sample_row]}
        engine.fetch_position = fake_fetch
        try:
            cache = engine.prefetch_position_cache(["신분당선"], timeout=1)
            self.assertEqual(calls, [("신분당선", 1)])
            self.assertTrue(cache["신분당선"]["available"])
            self.assertEqual(cache["신분당선"]["rows"], [sample_row])
            observations, diag = engine.observe_delays("신분당선", "DAY", cache["신분당선"]["rows"], [])
            self.assertEqual(diag["matched"], 1, diag)
            self.assertEqual(observations[0]["train_no"], "DX9003")
            self.assertEqual(observations[0]["source_kind"], "live")
        finally:
            engine.fetch_position = original


    def test_sinbundang_realtime_query_uses_composite_identifier_first(self):
        self.assertEqual(
            engine.realtime_query_candidates("신분당선"),
            ("1077:신분당선", "신분당선"),
        )

        sample = {
            "realtimePositionList": [{
                "subwayId": "1077", "subwayNm": "신분당선",
                "statnNm": "강남", "trainNo": "16",
            }]
        }
        class FakeResponse:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                import json
                return json.dumps(sample, ensure_ascii=False).encode("utf-8")

        original_urlopen = engine.urllib.request.urlopen
        original_key = engine.API_KEY
        seen = []
        def fake_urlopen(req, timeout=5):
            seen.append(req.full_url)
            return FakeResponse()
        engine.urllib.request.urlopen = fake_urlopen
        engine.API_KEY = "test-key"
        try:
            ok, err, data = engine.fetch_position("신분당선", timeout=1)
            self.assertTrue(ok, err)
            self.assertEqual(data.get("_jigeumta_query"), "1077:신분당선")
            self.assertEqual(len(seen), 1)
            self.assertIn("1077%3A%EC%8B%A0%EB%B6%84%EB%8B%B9%EC%84%A0", seen[0])
        finally:
            engine.urllib.request.urlopen = original_urlopen
            engine.API_KEY = original_key

    def test_sinbundang_realtime_query_falls_back_to_plain_name_on_empty_result(self):
        import json
        row = {"subwayId": "1077", "subwayNm": "신분당선", "statnNm": "강남", "trainNo": "16"}
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")

        original_urlopen = engine.urllib.request.urlopen
        original_key = engine.API_KEY
        seen = []
        def fake_urlopen(req, timeout=5):
            seen.append(req.full_url)
            if "1077%3A" in req.full_url:
                return FakeResponse({"realtimePositionList": []})
            return FakeResponse({"realtimePositionList": [row]})
        engine.urllib.request.urlopen = fake_urlopen
        engine.API_KEY = "test-key"
        try:
            ok, err, data = engine.fetch_position("신분당선", timeout=1)
            self.assertTrue(ok, err)
            self.assertEqual(len(seen), 2)
            self.assertEqual(data.get("_jigeumta_query"), "신분당선")
            self.assertEqual(data["realtimePositionList"], [row])
        finally:
            engine.urllib.request.urlopen = original_urlopen
            engine.API_KEY = original_key

    def test_sinbundang_context_matching_when_api_train_number_differs(self):
        row = {
            "subwayId": "1077",
            "subwayNm": "신분당선",
            "statnNm": "강남",
            # 서울시 API 열차번호가 Rail.Blue DX 운행열번과 전혀 다른 체계여도 동작해야 한다.
            "trainNo": "16",
            "recptnDt": "2026-08-19 05:34:26",
            "trainSttus": "1",
            "updnLine": "1",
            "statnTnm": "광교(경기대)",
        }
        observations, diag = engine.observe_delays("신분당선", "DAY", [row], [])
        self.assertEqual(diag["matched"], 0, diag)
        self.assertEqual(diag["matched_context"], 1, diag)
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["train_no"], "DX9003")
        self.assertEqual(observations[0]["raw_train_no"], "16")
        self.assertEqual(observations[0]["source_kind"], "live_context")
        self.assertEqual(round(observations[0]["delay"]), 0)

    def test_sinbundang_context_live_data_promotes_confidence_medium(self):
        original_now = engine.now_kst
        engine.now_kst = lambda: datetime(2026, 8, 19, 5, 34, 30)
        try:
            row = {
                "subwayId": "1077", "subwayNm": "신분당선",
                "statnNm": "강남", "trainNo": "16",
                "recptnDt": "2026-08-19 05:34:26", "trainSttus": "1",
                "updnLine": "1", "statnTnm": "광교(경기대)",
            }
            cache = {
                "신분당선": {
                    "rows": [row], "error": "", "available": True,
                    "query": "1077:신분당선",
                }
            }
            result = engine.calculate_segment(
                "신분당선", "DAY", "신사", "강남",
                datetime(2026, 8, 19, 5, 29, 0), cache
            )
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["chosen"]["train_no"], "DX9003")
            self.assertEqual(result["chosen"]["delay_source"], "schedule_only")
            self.assertEqual(result["chosen"]["confidence"], "낮음")
            self.assertEqual(result["diagnostics"]["matched_context"], 1)  # 진단에는 남음
            self.assertEqual(result["diagnostics"]["position_rows_diagnostic_only"], 1)
            self.assertEqual(result["diagnostics"]["realtime_query"], "1077:신분당선")
        finally:
            engine.now_kst = original_now

    def test_sinbundang_wrong_subway_id_is_filtered(self):
        data = {"realtimePositionList": [
            {"subwayId": "1077", "trainNo": "9003"},
            {"subwayId": "1002", "trainNo": "2001"},
            {"trainNo": "legacy-without-id"},
        ]}
        rows = engine.position_rows(data, "신분당선")
        self.assertEqual([x.get("trainNo") for x in rows], ["9003", "legacy-without-id"])

    def test_sinbundang_live_exact_promotes_confidence_high(self):
        original_now = engine.now_kst
        engine.now_kst = lambda: datetime(2026, 8, 19, 5, 34, 30)
        try:
            row = {
                "subwayId": "1077", "subwayNm": "신분당선",
                "statnNm": "강남", "trainNo": "9003",
                "recptnDt": "2026-08-19 05:34:26", "trainSttus": "1",
                "updnLine": "1", "statnTnm": "광교",
            }
            cache = {"신분당선": {"rows": [row], "error": "", "available": True}}
            result = engine.calculate_segment(
                "신분당선", "DAY", "신사", "강남",
                datetime(2026, 8, 19, 5, 29, 0), cache
            )
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["chosen"]["train_no"], "DX9003")
            self.assertEqual(result["chosen"]["delay_source"], "schedule_only")
            self.assertEqual(result["chosen"]["confidence"], "낮음")
            self.assertEqual(result["diagnostics"]["matched"], 1)  # 번호 매칭은 진단용일 뿐 ETA 근거가 아님
            self.assertEqual(result["diagnostics"]["position_rows_diagnostic_only"], 1)
        finally:
            engine.now_kst = original_now



    def test_sinbundang_station_arrival_is_primary_even_when_train_number_is_unrelated(self):
        original_now = engine.now_kst
        original_fetch = engine.fetch_station_arrival
        engine.now_kst = lambda: datetime(2026, 8, 19, 5, 29, 30)
        row = {
            "subwayId": "1077", "updnLine": "하행",
            "btrainNo": "16", "bstatnNm": "광교(경기대)",
            "barvlDt": "60", "recptnDt": "2026-08-19 05:29:00",
            "arvlMsg2": "1분 후", "arvlMsg3": "신사", "arvlCd": "99",
        }
        engine.fetch_station_arrival = lambda station, timeout=5: (
            True, None, {"realtimeArrivalList": [row]}
        )
        try:
            cache = {
                "신분당선": {
                    "rows": [{
                        "subwayId": "1077", "statnNm": "강남",
                        "trainNo": "999", "recptnDt": "2026-08-19 05:29:20",
                        "updnLine": "1", "statnTnm": "광교", "trainSttus": "1",
                    }],
                    "error": "", "available": True, "query": "1077:신분당선",
                }
            }
            result = engine.calculate_segment(
                "신분당선", "DAY", "신사", "강남",
                datetime(2026, 8, 19, 5, 29, 0), cache
            )
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["chosen"]["train_no"], "DX9003")
            self.assertEqual(result["chosen"].get("external_train_no"), "16")
            self.assertEqual(result["chosen"]["delay_source"], "station_arrival")
            self.assertEqual(result["chosen"]["confidence"], "중간")
            self.assertEqual(result["chosen"]["board_dt"], datetime(2026, 8, 19, 5, 30, 0))
            self.assertEqual(result["chosen"]["alight_dt"], datetime(2026, 8, 19, 5, 34, 26))
            self.assertEqual(result["diagnostics"]["station_arrival_matched"], 1)
            self.assertEqual(result["diagnostics"]["position_rows_diagnostic_only"], 1)
        finally:
            engine.now_kst = original_now
            engine.fetch_station_arrival = original_fetch

    def test_sinbundang_station_arrival_filters_other_lines(self):
        data = {"realtimeArrivalList": [
            {"subwayId": "1077", "btrainNo": "16"},
            {"subwayId": "1002", "btrainNo": "2010"},
        ]}
        rows = engine.station_arrival_rows(data, "신분당선")
        self.assertEqual([r["btrainNo"] for r in rows], ["16"])

    def test_sinbundang_position_train_number_no_longer_promotes_route_confidence(self):
        original_now = engine.now_kst
        original_fetch = engine.fetch_station_arrival
        engine.now_kst = lambda: datetime(2026, 8, 19, 5, 34, 30)
        engine.fetch_station_arrival = lambda station, timeout=5: (True, None, {"realtimeArrivalList": []})
        try:
            row = {
                "subwayId": "1077", "subwayNm": "신분당선",
                "statnNm": "강남", "trainNo": "9003",
                "recptnDt": "2026-08-19 05:34:26", "trainSttus": "1",
                "updnLine": "1", "statnTnm": "광교",
            }
            cache = {"신분당선": {"rows": [row], "error": "", "available": True}}
            result = engine.calculate_segment(
                "신분당선", "DAY", "신사", "강남",
                datetime(2026, 8, 19, 5, 29, 0), cache
            )
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result["chosen"]["delay_source"], "schedule_only")
            self.assertEqual(result["chosen"]["confidence"], "낮음")
            self.assertEqual(result["diagnostics"]["position_rows_diagnostic_only"], 1)
        finally:
            engine.now_kst = original_now
            engine.fetch_station_arrival = original_fetch

    def test_after_midnight_auto_uses_previous_weekday_service_day(self):
        result = engine.calculate_route({
            "start_time": "2026-08-20 00:10:00",
            "day": "AUTO",
            "segments": [{"line": "신분당선", "from": "신사", "to": "강남", "transfer_walk": 0}],
        }, position_cache={"신분당선": {"rows": [], "error": "schedule only", "available": False}})
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("service_mode"), "DAY")
        self.assertEqual(result["segments"][0]["train_no"], "DX9325")
        self.assertEqual(result["segments"][0]["board_dt"], "2026-08-20 00:10:00")

    def test_route_graph_contains_shinbundang_and_auto_transfer(self):
        edges = [row for row in engine.ROUTE_GRAPH["modes"]["DAY"] if row[0] == "신분당선"]
        self.assertTrue(edges)
        self.assertIn(("신분당선", "신사", "논현"), {(l, a, b) for l, a, b, _ in edges})
        path = engine.auto_find_path("광교", "역삼", "DAY")
        self.assertTrue(path)
        lines_in_path = {edge[0][0] for edge in path["edges"]} | {edge[1][0] for edge in path["edges"]}
        self.assertIn("신분당선", lines_in_path)
        self.assertIn("2호선", lines_in_path)



class PreviousTrainRegressionTest(unittest.TestCase):
    def test_gyeongui_previous_train_survives_headway_over_10_minutes(self):
        ready = datetime(2026, 8, 18, 20, 46, 0)
        previous = engine.previous_schedule_candidate(
            "경의중앙선", "DAY", "백마", "문산", ready, []
        )
        self.assertIsNotNone(previous)
        self.assertEqual(previous.get("train_no"), "K5126")
        self.assertEqual(previous.get("board_dt"), datetime(2026, 8, 18, 20, 35, 30))

    def test_gyeongui_previous_train_not_hidden_after_already_reaching_short_destination(self):
        ready = datetime(2026, 8, 18, 20, 46, 0)
        previous = engine.previous_schedule_candidate(
            "경의중앙선", "DAY", "디지털미디어시티", "홍대입구", ready, []
        )
        self.assertIsNotNone(previous)
        self.assertEqual(previous.get("train_no"), "K5141")
        self.assertEqual(previous.get("board_dt"), datetime(2026, 8, 18, 20, 38, 30))
        self.assertLess(previous.get("alight_dt"), ready)

    def test_calculate_segment_exposes_previous_candidate_and_badge(self):
        ready = datetime(2026, 8, 18, 20, 46, 0)
        cache = {
            "경의중앙선": {"rows": [], "error": "test schedule only", "available": False}
        }
        result = engine.calculate_segment(
            "경의중앙선", "DAY", "백마", "문산", ready, cache
        )
        self.assertTrue(result.get("ok"), result)
        previous = result.get("previous_candidate")
        self.assertIsNotNone(previous, result)
        self.assertEqual(previous.get("train_no"), "K5126")
        self.assertTrue(previous.get("is_previous"), previous)
        public_previous = [
            c for c in result.get("public_candidates", []) if c.get("is_previous")
        ]
        self.assertEqual(len(public_previous), 1, result.get("public_candidates"))
        self.assertEqual(public_previous[0].get("train_no"), "K5126")


if __name__ == "__main__":
    unittest.main()
