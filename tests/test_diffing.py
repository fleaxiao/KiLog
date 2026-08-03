from __future__ import annotations

from uuid import UUID

from kilog.diffing import build_event
from tests.helpers import item, snapshot


def test_move_and_rotate_have_semantic_operation_and_unique_uuids():
    before = snapshot(item("fp-1", "footprint", position={"x": 1}, orientation=0, value="U1"))
    after = snapshot(item("fp-1", "footprint", position={"x": 2}, orientation=90, value="U1"))

    event = build_event(before, after, 1, "11111111-1111-4111-8111-111111111111")

    assert event is not None
    assert event["summary"]["operations"] == {"footprint.move": 1}
    assert len(event["changes"]) == 1
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
    assert {change["op"] for change in event["changes"]} == {"add", "remove"}


def test_equal_snapshots_do_not_create_event():
    state = snapshot(item("fp-1", "footprint", position={"x": 1}))
    assert build_event(state, state, 1, "session") is None


def test_footprint_move_omits_derived_fields_and_large_definition_arrays():
    before = snapshot(
        item(
            "fp-1",
            "footprint",
            position={"x_nm": "100", "y_nm": "200"},
            reference_field={"position": {"x_nm": "110"}},
            definition={"items": [{"large": [1, 2, 3]}]},
        )
    )
    after = snapshot(
        item(
            "fp-1",
            "footprint",
            position={"x_nm": "150", "y_nm": "250"},
            reference_field={"position": {"x_nm": "160"}},
            definition={"items": [{"large": [4, 5, 6]}]},
        )
    )

    event = build_event(before, after, 1, "session")

    assert event is not None
    assert [change["path"] for change in event["changes"]] == [
        "/items/fp-1/data/transform",
    ]
    assert event["changes"][0]["before"] == {
        "position": {"x_nm": "100", "y_nm": "200"},
        "orientation": None,
    }
    assert event["changes"][0]["after"] == {
        "position": {"x_nm": "150", "y_nm": "250"},
        "orientation": None,
    }


def test_equal_length_lists_are_diffed_at_leaf_values():
    before = snapshot(item("zone-1", "zone", outline=[{"x": 1}, {"x": 2}]))
    after = snapshot(item("zone-1", "zone", outline=[{"x": 1}, {"x": 3}]))

    event = build_event(before, after, 1, "session")

    assert event is not None
    assert event["changes"][0]["path"] == "/items/zone-1/data/outline/1/x"
    assert event["changes"][0]["before"] == 2
    assert event["changes"][0]["after"] == 3


def board_edge():
    return item(
        "edge-1",
        "shape",
        layer="BL_Edge_Cuts",
        shape={
            "rectangle": {
                "top_left": {"x": 0, "y": 0},
                "bottom_right": {"x": 100, "y": 100},
            }
        },
    )


def test_footprint_changes_outside_edge_cuts_are_ignored():
    before = snapshot(
        board_edge(),
        item("fp-1", "footprint", position={"x": 150, "y": 20}, orientation=0),
    )
    after = snapshot(
        board_edge(),
        item("fp-1", "footprint", position={"x": 170, "y": 30}, orientation=90),
    )

    assert build_event(before, after, 1, "session") is None


def test_footprint_entering_edge_cuts_is_recorded_as_move():
    before = snapshot(
        board_edge(),
        item("fp-1", "footprint", position={"x": 150, "y": 20}, orientation=90),
    )
    after = snapshot(
        board_edge(),
        item("fp-1", "footprint", position={"x": 50, "y": 30}, orientation=270),
    )

    event = build_event(before, after, 1, "session")

    assert event is not None
    assert len(event["changes"]) == 1
    change = event["changes"][0]
    assert change["op"] == "add"
    assert change["operation"] == "footprint.move"
    assert "before" not in change
    assert change["after"] == {
        "position": {"x": 50, "y": 30},
        "orientation": 270,
    }


def test_footprint_leaving_edge_cuts_is_not_recorded():
    before = snapshot(
        board_edge(),
        item("fp-1", "footprint", position={"x": 50, "y": 30}, orientation=0),
    )
    after = snapshot(
        board_edge(),
        item("fp-1", "footprint", position={"x": 150, "y": 30}, orientation=0),
    )

    event = build_event(before, after, 1, "session")

    assert event is None
