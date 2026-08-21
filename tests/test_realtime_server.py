from datetime import datetime, timedelta

import engine
import realtime_store


def test_line2_api_train_number_normalization():
    assert engine.realtime_train_identity("2호선", "2451") == {
        "api_train_no": "2451", "service_train_no": "2451",
        "run_type": "scheduled_mainline", "normalized": False,
    }
    assert engine.realtime_train_identity("2호선", "8451")["service_train_no"] == "2451"
    assert engine.realtime_train_identity("2호선", "3451")["service_train_no"] == "2451"
    assert engine.realtime_train_identity("2호선", "1636")["run_type"] == "branch"
    assert engine.realtime_train_identity("2호선", "1636")["service_train_no"] == "1636"


def test_line2_special_and_test_are_not_forced_to_timetable():
    special = engine.realtime_train_identity("2호선", "9001")
    test = engine.realtime_train_identity("2호선", "9951")
    assert special["run_type"] == "special" and special["service_train_no"] == ""
    assert test["run_type"] == "test" and test["service_train_no"] == ""

    rows = [
        {"trainNo": "9001", "statnNm": "건대입구", "trainSttus": "2", "updnLine": "내선", "recptnDt": "2026-08-21 20:00:00"},
        {"trainNo": "9951", "statnNm": "성수", "trainSttus": "1", "updnLine": "내선", "recptnDt": "2026-08-21 20:00:00"},
    ]
    observations, diag = engine.observe_delays("2호선", "DAY", rows, client_cache=[])
    assert observations == []
    assert diag["special_trains"][0]["api_train_no"] == "9001"
    assert diag["test_trains"][0]["api_train_no"] == "9951"


def test_line2_normalized_api_number_keeps_large_early_operation_as_exact_live():
    # 2451은 평일 시간표상 뚝섬 20:14:30 출발. API 8451이 1시간 조발해
    # 19:14:30에 관측돼도 다른 운행편으로 재배정하지 않는다.
    row = {
        "trainNo": "8451",
        "statnNm": "뚝섬",
        "trainSttus": "2",
        "updnLine": "내선",
        "recptnDt": "2026-08-21 19:14:30",
    }
    observations, diag = engine.observe_delays("2호선", "DAY", [row], client_cache=[])
    assert len(observations) == 1
    obs = observations[0]
    assert obs["train_no"] == "2451"
    assert obs["raw_train_no"] == "8451"
    assert obs["source_kind"] == "live"
    assert obs["match_kind"] == "line2_train_number_normalized"
    assert round(obs["delay"]) == -3600
    assert diag["matched_line2_normalized"] == 1


def test_line2_cached_exact_allows_large_delay_magnitude(monkeypatch):
    fixed = datetime(2026, 8, 21, 19, 15, 0)
    monkeypatch.setattr(engine, "now_kst", lambda: fixed)
    rows = [{
        "line": "2호선",
        "train_no": "2451",
        "observed_at": "2026-08-21 19:14:30",
        "delay_seconds": -3600,
        "current_station": "뚝섬",
        "status": "출발",
    }]
    observations, diag = engine.observe_delays("2호선", "DAY", [], client_cache=rows)
    cached = [o for o in observations if o.get("source_kind") == "cache"]
    assert len(cached) == 1
    assert cached[0]["train_no"] == "2451"
    assert cached[0]["delay"] == -3600
    assert diag["cached_exact"] == 1


class _FakeRedis:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        return True


def test_train_state_keeps_missing_train_after_successful_snapshot(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(realtime_store, "client", lambda: fake)
    t0 = datetime(2026, 8, 21, 20, 0, 0)
    state_rows = [{
        "api_train_no": "8451",
        "service_train_no": "2451",
        "run_type": "scheduled_mainline",
        "station": "뚝섬",
        "status": "출발",
        "direction": "IN",
        "source_observed_at": "2026-08-21 20:00:00",
    }]
    states = realtime_store.update_train_states(
        line_id="1002", line_name="2호선", observations=state_rows, observed_at=t0,
    )
    assert states["8451"]["presence_state"] == "live"

    states = realtime_store.update_train_states(
        line_id="1002", line_name="2호선", observations=[], observed_at=t0 + timedelta(seconds=10),
    )
    assert states["8451"]["presence_state"] == "missing_recent"
    assert states["8451"]["service_train_no"] == "2451"
    assert states["8451"]["missing_since"] == "2026-08-21 20:00:10"


def test_snapshot_signature_ignores_api_row_order():
    rows_a = [
        {"trainNo": "8451", "statnNm": "뚝섬", "trainSttus": "2"},
        {"trainNo": "2453", "statnNm": "한양대", "trainSttus": "1"},
    ]
    rows_b = list(reversed(rows_a))
    source = datetime(2026, 8, 21, 20, 0, 0)
    assert realtime_store._snapshot_signature(rows_a, source) == realtime_store._snapshot_signature(rows_b, source)


class _FakePipeline:
    def __init__(self, parent):
        self.parent = parent
        self.ops = []

    def set(self, key, value, ex=None):
        self.ops.append((key, value))
        return self

    def execute(self):
        for key, value in self.ops:
            self.parent.data[key] = value
        return [True] * len(self.ops)


class _FakeRedisPipeline(_FakeRedis):
    def pipeline(self, transaction=False):
        return _FakePipeline(self)


def test_put_snapshot_writes_payload_only_when_source_changes(monkeypatch):
    fake = _FakeRedisPipeline()
    monkeypatch.setattr(realtime_store, "client", lambda: fake)
    t0 = datetime(2026, 8, 21, 20, 0, 0)
    rows = [{"trainNo": "8451", "statnNm": "뚝섬", "recptnDt": "2026-08-21 20:00:00"}]

    changed1 = realtime_store.put_snapshot(
        line_id="1002", line_name="2호선", rows=rows,
        fetched_at=t0, source_observed_at=t0,
    )
    first_snapshot = fake.data[realtime_store._key("snapshot", "1002")]
    changed2 = realtime_store.put_snapshot(
        line_id="1002", line_name="2호선", rows=list(reversed(rows)),
        fetched_at=t0 + timedelta(seconds=5), source_observed_at=t0,
    )
    assert changed1 is True
    assert changed2 is False
    assert fake.data[realtime_store._key("snapshot", "1002")] == first_snapshot
    health = realtime_store._load(fake.data[realtime_store._key("health", "1002")])
    assert health["last_success_at"] == "2026-08-21 20:00:05"
