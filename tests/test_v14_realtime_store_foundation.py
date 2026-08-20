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
