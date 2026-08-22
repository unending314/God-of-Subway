import engine
import realtime_store


def test_cache_only_reads_redis_without_source_api(monkeypatch):
    monkeypatch.setattr(realtime_store, "store_mode", lambda: "cache_only")
    monkeypatch.setattr(realtime_store, "is_configured", lambda: True)
    monkeypatch.setattr(realtime_store, "get_delay_rows", lambda line_id: [
        {
            "line": "1호선", "train_no": "K123", "observed_at": "2026-08-19 18:00:00",
            "delay_seconds": 120, "current_station": "서울역", "status": "출발",
        }
    ] if line_id == engine.LINE_IDS["1호선"] else [])
    monkeypatch.setattr(realtime_store, "position_cache_entry", lambda **kwargs: {
        "rows": [{"trainNo": "K123", "statnNm": "서울역"}],
        "available": True, "error": "", "query": "1호선",
        "cache_state": "fresh", "cache_age_seconds": 10, "realtime_source": "redis",
    })
    monkeypatch.setattr(engine, "fetch_position", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("source API called")))

    cache = engine.prefetch_position_cache(["1호선"])
    assert cache["1호선"]["available"] is True
    assert cache["1호선"]["realtime_source"] == "redis"
    assert len(cache["1호선"]["rows"]) == 1
    assert cache["__train_delay_cache__"][0]["train_no"] == "K123"


def test_cache_only_stale_snapshot_does_not_reuse_raw_position(monkeypatch):
    monkeypatch.setattr(realtime_store, "store_mode", lambda: "cache_only")
    monkeypatch.setattr(realtime_store, "is_configured", lambda: True)
    monkeypatch.setattr(realtime_store, "get_delay_rows", lambda line_id: [])
    monkeypatch.setattr(realtime_store, "position_cache_entry", lambda **kwargs: {
        "rows": [], "available": False, "error": "Redis snapshot stale (130s)",
        "query": "1호선", "cache_state": "stale", "cache_age_seconds": 130,
        "realtime_source": "redis", "stale_row_count": 42,
    })

    cache = engine.prefetch_position_cache(["1호선"])
    assert cache["1호선"]["rows"] == []
    assert cache["1호선"]["available"] is False
    assert cache["1호선"]["stale_row_count"] == 42


def test_client_delay_cache_merges_with_server_cache_using_newest():
    cache = {
        "__train_delay_cache__": [{
            "line": "1호선", "train_no": "K123", "observed_at": "2026-08-19 18:00:00",
            "delay_seconds": 60,
        }]
    }
    engine.attach_client_delay_cache(cache, {
        "train_delay_cache": [{
            "line": "1호선", "train_no": "K123", "observed_at": "2026-08-19 18:01:00",
            "delay_seconds": 90,
        }]
    })
    rows = cache["__train_delay_cache__"]
    assert len(rows) == 1
    assert rows[0]["delay_seconds"] == 90


def test_realtime_cache_diagnostics_summarizes_redis_source(monkeypatch):
    monkeypatch.setattr(realtime_store, "store_mode", lambda: "hybrid")
    cache = {
        "2호선": {
            "rows": [], "available": True, "error": "",
            "cache_state": "fresh", "cache_age_seconds": 3,
            "source_age_seconds": 18, "realtime_source": "redis",
        }
    }
    diag = engine.realtime_cache_diagnostics(cache, ["2호선"])
    assert diag["store_mode"] == "hybrid"
    assert diag["realtime_source"] == "redis"
    assert diag["cache_state"] == "fresh"
    assert diag["cache_age_seconds"] == 3
    assert diag["source_age_seconds"] == 18
    assert diag["lines"]["2호선"]["realtime_source"] == "redis"


def test_calculate_route_exposes_realtime_source_diagnostics(monkeypatch):
    monkeypatch.setattr(realtime_store, "store_mode", lambda: "hybrid")
    monkeypatch.setattr(engine, "prefetch_position_cache", lambda lines, timeout=5: {
        "1호선": {
            "rows": [], "available": True, "error": "",
            "cache_state": "fresh", "cache_age_seconds": 2,
            "source_age_seconds": 12, "realtime_source": "redis",
        }
    })
    result = engine.calculate_route({
        "start_time": "2026-08-18 08:35:00",
        "day": "DAY",
        "segments": [{"line": "1호선", "from": "당정", "to": "안양", "transfer_walk": 0}],
    })
    assert result["ok"] is True
    assert result["diagnostics"]["realtime_source"] == "redis"
    assert result["diagnostics"]["cache_state"] == "fresh"
    assert result["diagnostics"]["cache_age_seconds"] == 2
    assert result["diagnostics"]["lines"]["1호선"]["source_age_seconds"] == 12


def test_auto_route_exposes_realtime_source_diagnostics(monkeypatch):
    monkeypatch.setattr(realtime_store, "store_mode", lambda: "hybrid")
    monkeypatch.setattr(engine, "prefetch_position_cache", lambda lines, timeout=5: {
        line: {
            "rows": [], "available": True, "error": "",
            "cache_state": "fresh", "cache_age_seconds": 4,
            "source_age_seconds": 20, "realtime_source": "redis",
        }
        for line in set(lines) if line
    })
    result = engine.calculate_auto_route({
        "from": "강남", "to": "역삼",
        "start_time": "2026-08-18 08:00:00", "day": "DAY",
    })
    assert result["ok"] is True
    assert result["diagnostics"]["realtime_source"] == "redis"
    assert result["diagnostics"]["cache_state"] == "fresh"
    assert result["diagnostics"]["lines"]["2호선"]["cache_age_seconds"] == 4
