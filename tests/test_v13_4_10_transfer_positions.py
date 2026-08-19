import json
from pathlib import Path

import engine

ROOT = Path(__file__).resolve().parents[1]


def test_sinnonhyeon_user_override_gwanggyo_to_line9_is_6_3():
    info = engine.best_transfer_detail(
        "신논현",
        {"line": "신분당선", "from": "논현", "to": "신논현"},
        {"line": "9호선", "from": "신논현", "to": "언주"},
        "DAY",
    )
    assert info["matched"] == "direction"
    assert info["from_direction"] == "강남"
    assert info["to_direction"] == "언주"
    assert info["alight_position"] == "6-3"


def test_sinnonhyeon_sinsa_direction_to_line9_is_1_2():
    info = engine.best_transfer_detail(
        "신논현",
        {"line": "신분당선", "from": "강남", "to": "신논현"},
        {"line": "9호선", "from": "신논현", "to": "사평"},
        "DAY",
    )
    assert info["matched"] == "direction"
    assert info["from_direction"] == "논현"
    assert info["alight_position"] == "1-2"


def test_yangjae_line3_direction_specific_position():
    # 매봉→양재는 3호선 대화 방면이며 양재 다음 여객역은 남부터미널.
    info = engine.best_transfer_detail(
        "양재",
        {"line": "3호선", "from": "매봉", "to": "양재"},
        {"line": "신분당선", "from": "양재", "to": "강남"},
        "DAY",
    )
    assert info["matched"] == "direction"
    assert info["from_direction"] == "남부터미널"
    assert info["alight_position"] == "5-4"


def test_pangyo_direction_hint_skips_operational_point():
    # 판교 직후 DIA에 판교주박기지가 있어도 방향 키는 실제 다음 여객역 정자여야 한다.
    assert engine._direction_station("신분당선", "DAY", "청계산입구", "판교") == "정자"
    info = engine.best_transfer_detail(
        "판교",
        {"line": "신분당선", "from": "청계산입구", "to": "판교"},
        {"line": "경강선", "from": "판교", "to": "성남"},
        "DAY",
    )
    assert info["matched"] == "direction"
    assert info["from_direction"] == "정자"
    assert info["alight_position"] == "3-1"


def test_transfer_master_counts_and_critical_override():
    master = json.loads((ROOT / "transfer_position_master.json").read_text(encoding="utf-8"))
    meta = master["meta"]
    assert meta["record_count"] == len(master["records"]) == 1138
    assert meta["current_app_record_count"] == 942
    assert meta["future_network_record_count"] == 196
    assert meta["verified_position_count"] == 1014
    assert meta["needs_verification_count"] == 124

    rows = [
        r for r in master["records"]
        if r["station"] == "신논현"
        and r["from_line"] == "신분당선"
        and r["from_direction_key"] == "강남"
        and r["to_line"] == "9호선"
    ]
    assert rows
    assert {r["position_primary"] for r in rows} == {"6-3"}
    assert {r["source_kind"] for r in rows} == {"user_direct_verification"}


def test_current_app_master_rows_correspond_to_transfer_records():
    transfer = json.loads((ROOT / "transfer_data.json").read_text(encoding="utf-8"))["pairs"]
    master = json.loads((ROOT / "transfer_position_master.json").read_text(encoding="utf-8"))["records"]

    actual = set()
    for pair in transfer.values():
        for rec in pair.get("records") or []:
            actual.add((
                pair["station"], pair["from_line"], str(rec.get("from_direction") or ""),
                pair["to_line"], str(rec.get("to_direction") or ""),
                (f'{rec.get("alight_car")}-{rec.get("alight_door")}' if rec.get("alight_car") and rec.get("alight_door") else str(rec.get("alight_car") or rec.get("alight_door") or "")),
            ))

    current = [r for r in master if r["scope"] == "current_app"]
    assert len(current) == 942
    for r in current:
        key = (
            r["station"], r["from_line"], r["from_direction_key"],
            r["to_line"], r["to_direction_key"], r["position_primary"],
        )
        assert key in actual
