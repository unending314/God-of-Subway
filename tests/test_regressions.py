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


if __name__ == "__main__":
    unittest.main()
