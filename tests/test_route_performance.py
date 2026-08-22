# -*- coding: utf-8 -*-
import engine


def _offline_cache(lines, timeout=5):
    return {
        line: {"rows": [], "error": "test schedule fallback", "available": False}
        for line in set(lines) if line
    }


def test_auto_candidate_topology_cache_reuses_same_od():
    engine._auto_candidate_routes_cached.cache_clear()
    first = engine.auto_candidate_routes("성균관대", "마포", "SAT")
    info1 = engine._auto_candidate_routes_cached.cache_info()
    second = engine.auto_candidate_routes("성균관대", "마포", "SAT")
    info2 = engine._auto_candidate_routes_cached.cache_info()
    assert first == second
    assert info1.misses == 1
    assert info2.hits >= 1
    # caller mutation must not poison cached topology
    second[0]["segments"][0]["from"] = "변조"
    third = engine.auto_candidate_routes("성균관대", "마포", "SAT")
    assert third[0]["segments"][0]["from"] != "변조"


def test_auto_route_exposes_phase_timing_diagnostics(monkeypatch):
    monkeypatch.setattr(engine, "prefetch_position_cache", _offline_cache)
    result = engine.calculate_auto_route({
        "from": "성균관대",
        "to": "마포",
        "start_time": "10:18",
        "day": "SAT",
    })
    assert result["ok"] is True
    perf = result["diagnostics"]["performance_ms"]
    assert set(perf) == {"candidate_routes", "realtime_prefetch", "candidate_scoring", "total"}
    assert perf["total"] >= 0
    assert result["live_scored_count"] == result["candidate_count"]


def test_auto_candidates_include_transfer_diverse_route_for_unjeong_dongtan():
    engine._auto_candidate_routes_cached.cache_clear()
    candidates = engine.auto_candidate_routes("운정", "동탄", "SAT")
    signatures = [
        tuple((s["line"], s["from"], s["to"]) for s in c["segments"])
        for c in candidates
    ]
    # 정적 최단경로군이 경의중앙↔서해선 조합으로 포화되어도,
    # 실제 시간표에서 유리한 저환승 GTX-A 경로가 반드시 평가 후보에 들어와야 한다.
    assert any(
        sig == (
            ("경의중앙선", "운정", "옥수"),
            ("3호선", "옥수", "수서"),
            ("GTX-A", "수서", "동탄"),
        )
        for sig in signatures
    ), signatures


def test_unjeong_dongtan_schedule_scoring_beats_old_five_transfer_route(monkeypatch):
    from datetime import datetime

    engine._auto_candidate_routes_cached.cache_clear()
    monkeypatch.setattr(engine, "prefetch_position_cache", _offline_cache)
    monkeypatch.setattr(engine, "now_kst", lambda: datetime(2026, 8, 22, 14, 41, 0))
    result = engine.calculate_auto_route({
        "from": "운정",
        "to": "동탄",
        "start_time": "2026-08-22 14:41:00",
        "day": "SAT",
    })
    assert result["ok"] is True
    # 기존 5환승 추천은 17:20 도착이었다. 저환승 후보를 함께 채점하면 17:04가 나온다.
    assert result["estimated_arrival_time"] == "2026-08-22 17:04:00"
    assert result["transfer_count"] <= 4
    assert result["interchanges"] in (["옥수", "수서"], ["대곡", "서울역", "충무로", "수서"])
