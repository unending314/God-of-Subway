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


if __name__ == "__main__":
    unittest.main()
