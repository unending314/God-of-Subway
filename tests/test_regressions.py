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
        lines = ("1호선",) + engine.KORAIL_EXTRA_LINES
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
            korail_lines = ("1호선",) + engine.KORAIL_EXTRA_LINES
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


class ShinbundangVehicleEtaRegressionTest(unittest.TestCase):
    def test_public_timetable_has_no_legacy_dx_train_numbers(self):
        self.assertEqual(engine.SINBUNDANG["meta"]["source_service_columns"]["weekday"], 326)
        self.assertEqual(engine.SINBUNDANG["meta"]["source_service_columns"]["holiday"], 272)
        self.assertGreater(engine.SINBUNDANG["counts"]["weekday"], 5000)
        self.assertGreater(engine.SINBUNDANG["counts"]["holiday"], 4000)
        self.assertFalse(engine.SINBUNDANG["meta"]["public_train_numbers_available"])
        self.assertTrue(engine.SINBUNDANG["meta"]["legacy_dx_train_numbers_discarded"])
        payload = str(engine.SINBUNDANG)
        self.assertNotIn("DX9003", payload)
        self.assertEqual(len(engine.STATIONS_BY_LINE["신분당선"]), 16)

    def test_official_runtime_full_line_matches_operator(self):
        self.assertEqual(engine.sinbundang_runtime_seconds("신사", "광교"), 42 * 60 + 2)
        self.assertEqual(engine.sinbundang_runtime_seconds("광교", "신사"), 41 * 60 + 54)
        self.assertEqual(engine.sinbundang_runtime_seconds("신사", "논현"), 58)
        self.assertEqual(engine.sinbundang_runtime_seconds("논현", "신사"), 65)

    def test_api_train_no_is_used_as_vehicle_id_not_timetable_train_number(self):
        original_now = engine.now_kst
        engine.now_kst = lambda: datetime(2026, 8, 19, 11, 0, 10)
        try:
            row = {
                "subwayId": "1077", "subwayNm": "신분당선",
                "statnNm": "신논현", "trainNo": "16",
                "recptnDt": "2026-08-19 11:00:00", "trainSttus": "2",
                "updnLine": "1", "statnTnm": "광교(경기대)",
            }
            cache = {"신분당선": {"rows": [row], "error": "", "available": True, "query": "1077:신분당선"}}
            result = engine.calculate_segment("신분당선", "DAY", "강남", "판교", datetime(2026, 8, 19, 11, 0, 0), cache)
            self.assertTrue(result.get("ok"), result)
            chosen = result["chosen"]
            self.assertTrue(chosen["train_no"].startswith("SBV-"))
            self.assertEqual(chosen["vehicle_id"], "16")
            self.assertEqual(chosen["delay_source"], "vehicle_position")
            self.assertIsNone(chosen["delay_seconds"])
            self.assertFalse(chosen["delay_available"])
            self.assertEqual(chosen["confidence"], "중간")
            self.assertEqual(chosen["board_dt"], datetime(2026, 8, 19, 11, 1, 38))
            self.assertEqual(chosen["alight_dt"], datetime(2026, 8, 19, 11, 15, 0))
            self.assertEqual(result["diagnostics"]["vehicle_ids"], ["16"])
            self.assertFalse(result["diagnostics"]["delay_inference"])
        finally:
            engine.now_kst = original_now

    def test_vehicle_already_departed_boarding_station_is_rejected(self):
        original_now = engine.now_kst
        engine.now_kst = lambda: datetime(2026, 8, 19, 11, 0, 10)
        try:
            row = {
                "subwayId":"1077","statnNm":"강남","trainNo":"16",
                "recptnDt":"2026-08-19 11:00:00","trainSttus":"2","updnLine":"1","statnTnm":"광교",
            }
            c = engine.sinbundang_vehicle_candidates("DAY", "강남", "판교", datetime(2026,8,19,11,0,0), [row])
            self.assertEqual(c, [])
        finally:
            engine.now_kst = original_now

    def test_schedule_fallback_uses_anonymous_public_station_timetable(self):
        cache = {"신분당선": {"rows": [], "error": "test", "available": False}}
        result = engine.calculate_segment("신분당선", "DAY", "신사", "강남", datetime(2026,8,19,5,29,0), cache)
        self.assertTrue(result.get("ok"), result)
        chosen = result["chosen"]
        self.assertTrue(chosen["train_no"].startswith("SBV-"))
        self.assertNotIn("DX", chosen["train_no"])
        self.assertEqual(chosen["delay_source"], "schedule_virtual_run")
        self.assertIsNone(chosen["delay_seconds"])
        self.assertEqual(chosen["confidence"], "낮음")

    def test_schedule_fallback_exposes_same_candidate_count_as_other_lines(self):
        cache = {"신분당선": {"rows": [], "error": "test", "available": False}}
        result = engine.calculate_segment("신분당선", "DAY", "신사", "청계산입구", datetime(2026,8,19,15,3,30), cache)
        self.assertTrue(result.get("ok"), result)
        public = result.get("public_candidates", [])
        self.assertEqual(len(public), 7, public)
        self.assertEqual(sum(1 for c in public if c.get("is_previous")), 1)
        self.assertEqual(sum(1 for c in public if c.get("selected")), 1)
        self.assertTrue(all(str(c.get("train_no", "")).startswith("SBV-") for c in public))
        self.assertTrue(all("DX" not in str(c.get("train_no", "")) for c in public))


    def test_virtual_schedule_candidate_has_expected_location(self):
        original_now = engine.now_kst
        try:
            engine.now_kst = lambda: datetime(2026, 8, 19, 15, 3, 30)
            cache = {"신분당선": {"rows": [], "error": "test", "available": False}}
            result = engine.calculate_segment("신분당선", "DAY", "신사", "청계산입구", datetime(2026,8,19,15,3,30), cache)
            self.assertTrue(result.get("ok"), result)
            chosen = result["chosen"]
            self.assertTrue(chosen["train_no"].startswith("SBV-"))
            self.assertEqual(chosen["location_label"], "신사 출발 전 예상")
            self.assertEqual(chosen["location_kind"], "expected")
        finally:
            engine.now_kst = original_now

    def test_virtual_schedule_candidate_can_be_locked_and_tracks_expected_position(self):
        original_now = engine.now_kst
        try:
            engine.now_kst = lambda: datetime(2026, 8, 19, 15, 3, 30)
            cache = {"신분당선": {"rows": [], "error": "test", "available": False}}
            first = engine.calculate_segment("신분당선", "DAY", "신사", "청계산입구", datetime(2026,8,19,15,3,30), cache)
            run_id = first["chosen"]["train_no"]
            board_dt = first["chosen"]["board_dt"].strftime("%Y-%m-%d %H:%M:%S")
            engine.now_kst = lambda: datetime(2026, 8, 19, 15, 8, 0)
            tracked = engine.tracked_train_segment("신분당선", "DAY", "신사", "청계산입구", run_id, cache, board_dt)
            self.assertTrue(tracked.get("ok"), tracked)
            self.assertTrue(tracked["chosen"].get("tracking"))
            self.assertEqual(tracked["chosen"]["train_no"], run_id)
            self.assertEqual(tracked["chosen"]["location_label"], "신논현 → 강남 이동 예상")
            self.assertEqual(tracked["chosen"]["delay_source"], "schedule_virtual_tracking")
        finally:
            engine.now_kst = original_now

    def test_virtual_schedule_tracking_can_attach_matching_live_vehicle(self):
        original_now = engine.now_kst
        try:
            engine.now_kst = lambda: datetime(2026, 8, 19, 15, 3, 30)
            empty = {"신분당선": {"rows": [], "error": "", "available": True}}
            first = engine.calculate_segment("신분당선", "DAY", "신사", "청계산입구", datetime(2026,8,19,15,3,30), empty)
            run_id = first["chosen"]["train_no"]
            board_dt = first["chosen"]["board_dt"].strftime("%Y-%m-%d %H:%M:%S")
            engine.now_kst = lambda: datetime(2026, 8, 19, 15, 8, 0)
            row = {
                "subwayId":"1077","statnNm":"신논현","trainNo":"16",
                "recptnDt":"2026-08-19 15:07:20","trainSttus":"2","updnLine":"1","statnTnm":"광교",
            }
            cache={"신분당선":{"rows":[row],"error":"","available":True}}
            tracked=engine.tracked_train_segment("신분당선","DAY","신사","청계산입구",run_id,cache,board_dt)
            self.assertTrue(tracked.get("ok"), tracked)
            self.assertEqual(tracked["chosen"]["train_no"], run_id)
            self.assertEqual(tracked["chosen"]["vehicle_id"], "16")
            self.assertEqual(tracked["chosen"]["location_kind"], "live")
            self.assertEqual(tracked["chosen"]["current_station"], "신논현")
            self.assertEqual(tracked["chosen"]["confidence"], "중간")
        finally:
            engine.now_kst = original_now

    def test_tracked_vehicle_uses_same_api_vehicle_id(self):
        original_now = engine.now_kst
        engine.now_kst = lambda: datetime(2026, 8, 19, 11, 5, 0)
        try:
            row = {
                "subwayId":"1077","statnNm":"양재","trainNo":"16",
                "recptnDt":"2026-08-19 11:04:50","trainSttus":"2","updnLine":"1","statnTnm":"광교",
            }
            cache={"신분당선":{"rows":[row],"error":"","available":True}}
            result=engine.tracked_train_segment("신분당선","DAY","강남","판교","16",cache,"2026-08-19 11:01:38")
            self.assertTrue(result.get("ok"), result)
            self.assertFalse(result.get("arrived"))
            self.assertEqual(result["chosen"]["train_no"], "16")
            self.assertEqual(result["chosen"]["delay_source"], "vehicle_tracking")
            self.assertFalse(result["chosen"]["delay_available"])
            self.assertEqual(result["chosen"]["current_station"], "양재")
        finally:
            engine.now_kst = original_now

    def test_sinbundang_wrong_subway_id_is_filtered(self):
        data = {"realtimePositionList": [
            {"subwayId": "1077", "trainNo": "16"},
            {"subwayId": "1002", "trainNo": "2010"},
            {"trainNo": "legacy-without-id"},
        ]}
        rows = engine.position_rows(data, "신분당선")
        self.assertEqual([x.get("trainNo") for x in rows], ["16", "legacy-without-id"])

    def test_after_midnight_auto_uses_previous_weekday_station_timetable(self):
        # 기준 시각을 고정해 실제 테스트 실행 날짜/시각에 따라 다음 날로
        # rollover되는 비결정적 실패를 막는다.
        original_now = engine.now_kst
        engine.now_kst = lambda: datetime(2026, 8, 19, 23, 58, 0)
        try:
            result = engine.calculate_route({
                "start_time": "2026-08-20 00:10:00", "day": "AUTO",
                "segments": [{"line": "신분당선", "from": "신사", "to": "강남", "transfer_walk": 0}],
            }, position_cache={"신분당선": {"rows": [], "error": "schedule only", "available": False}})
            self.assertTrue(result.get("ok"), result)
            self.assertEqual(result.get("service_mode"), "DAY")
            self.assertTrue(result["segments"][0]["train_no"].startswith("SBV-"))
            self.assertEqual(result["segments"][0]["board_dt"], "2026-08-20 00:10:00")
        finally:
            engine.now_kst = original_now

    def test_route_graph_contains_shinbundang_and_auto_transfer(self):
        edges = [row for row in engine.ROUTE_GRAPH["modes"]["DAY"] if row[0] == "신분당선"]
        self.assertTrue(edges)
        path = engine.auto_find_path("광교", "역삼", "DAY")
        self.assertTrue(path)
        lines_in_path = {edge[0][0] for edge in path["edges"]} | {edge[1][0] for edge in path["edges"]}
        self.assertIn("신분당선", lines_in_path)
        self.assertIn("2호선", lines_in_path)

    def test_sinbundang_realtime_query_alias(self):
        self.assertEqual(engine.LINE_IDS.get("신분당선"), "1077")
        self.assertEqual(engine.realtime_query_candidates("신분당선"), ("1077:신분당선", "신분당선"))


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
