import csv
import json
from pathlib import Path

import engine

ROOT = Path(__file__).resolve().parents[1]


def _pos(rec):
    car=str(rec.get("alight_car") or "").strip()
    door=str(rec.get("alight_door") or "").strip()
    return f"{car}-{door}" if car and door else car or door


def test_default_transfer_fallback_is_180_seconds():
    assert engine.DEFAULT_TRANSFER_SECONDS == 180
    data=json.loads((ROOT/"transfer_data.json").read_text(encoding="utf-8"))
    assert data["meta"]["fallback_seconds"] == 180
    # Missing-time pairs use 180, but measured 240-second records remain intact.
    for pair in data["pairs"].values():
        recs=pair.get("records") or []
        if pair.get("distance_seconds") is None and (not recs or all(r.get("seconds") is None for r in recs)):
            if pair.get("default_seconds") in (180, 240):
                assert pair.get("default_seconds") == 180
    measured_240=[
        pair for pair in data["pairs"].values()
        if pair.get("default_seconds")==240 and any(r.get("seconds") is not None for r in (pair.get("records") or []))
    ]
    assert measured_240


def test_user_sinbundang_position_patch_all_applied():
    audit=json.loads((ROOT/"TRANSFER_UPDATE_V13_5_4_AUDIT.json").read_text(encoding="utf-8"))
    assert audit["user_position_patch_count"] == 22
    pairs=json.loads((ROOT/"transfer_data.json").read_text(encoding="utf-8"))["pairs"]
    for row in audit["user_position_patches"]:
        pair=pairs[f'{row["station"]}|{row["from_line"]}|{row["to_line"]}']
        rec=[r for r in pair["records"] if str(r.get("from_direction") or "")==row["from_direction_key"] and str(r.get("to_direction") or "")==row["to_direction_key"]]
        assert len(rec)==1
        assert _pos(rec[0]) == row["position_primary"]
        assert rec[0].get("position_status") == "user_confirmed"


def test_user_patched_rows_removed_from_needs_verification():
    audit=json.loads((ROOT/"TRANSFER_UPDATE_V13_5_4_AUDIT.json").read_text(encoding="utf-8"))
    patched={(r["station"],r["from_line"],r["from_direction_key"],r["to_line"],r["to_direction_key"]) for r in audit["user_position_patches"]}
    with (ROOT/"transfer_position_needs_verification.csv").open(encoding="utf-8-sig",newline="") as f:
        remaining={(r["station"],r["from_line"],r["from_direction_key"],r["to_line"],r["to_direction_key"]) for r in csv.DictReader(f)}
    assert patched.isdisjoint(remaining)
