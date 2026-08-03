from __future__ import annotations

from uuid import UUID

from kilog.diffing import build_event
from tests.helpers import item, snapshot


def test_move_and_rotate_have_semantic_operation_and_unique_uuids():
    before = snapshot(item("fp-1", "footprint", position={"x": 1}, orientation=0, value="U1"))
    after = snapshot(item("fp-1", "footprint", position={"x": 2}, orientation=90, value="U1"))

    event = build_event(before, after, 1, "11111111-1111-4111-8111-111111111111")

    assert event is not None
    assert event["summary"]["operations"] == {"footprint.move_rotate": 2}
    assert len({change["change_uuid"] for change in event["changes"]}) == 2
    UUID(event["event_uuid"])
    for change in event["changes"]:
        UUID(change["change_uuid"])
        assert change["item_uuid"] == "fp-1"


def test_add_track_remove_via_and_modify_zone_are_json_patch_changes():
    before = snapshot(
        item("via-1", "via", position={"x": 5}),
        item("zone-1", "zone", outline=[1, 2], filled_polygons=[1]),
    )
    after = snapshot(
        item("track-1", "track", start={"x": 1}, end={"x": 2}),
        item("zone-1", "zone", outline=[1, 2], filled_polygons=[1, 2]),
    )

    event = build_event(before, after, 7, "11111111-1111-4111-8111-111111111111")

    assert event is not None
    operations = {change["operation"] for change in event["changes"]}
    assert operations == {"routing.add", "via.remove", "zone.refill"}
    assert {change["op"] for change in event["changes"]} == {"add", "remove", "replace"}


def test_equal_snapshots_do_not_create_event():
    state = snapshot(item("fp-1", "footprint", position={"x": 1}))
    assert build_event(state, state, 1, "session") is None
