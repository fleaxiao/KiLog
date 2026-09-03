from __future__ import annotations

import json

import pytest

from kilog.recorder import LogFileExistsError, Recorder, RecorderConfig, RecorderError
from kilog.replay import ReplayController
from tests.test_replay import ReplayAdapter, write_log
from tests.helpers import FakeAdapter, item, snapshot


def test_resume_from_replay_truncates_future_and_records_from_current_step(tmp_path):
    path = tmp_path / "ref.json"
    write_log(path, 3)
    adapter = ReplayAdapter()
    replay = ReplayController(adapter)
    replay.load(path)
    replay.seek(2)
    recorder = Recorder(adapter)

    recorder.resume(replay.branch())

    truncated = json.loads(path.read_text(encoding="utf-8"))
    assert recorder.recording
    assert recorder.event_count == 2
    assert recorder.baseline == adapter.current
    assert [step["step"] for step in truncated["steps"]] == [1, 2]
    assert [step["step_uuid"] for step in truncated["steps"]] == [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000002",
    ]

    adapter.current = snapshot(
        item(
            "fp-1",
            "footprint",
            position={"x_nm": "9"},
            orientation={"value_degrees": 0},
        )
    )
    recorder.flush()

    continued = json.loads(path.read_text(encoding="utf-8"))
    assert [step["step"] for step in continued["steps"]] == [1, 2, 3]
    assert continued["steps"][2]["changes"][0]["position"] == {"x_nm": "9"}


@pytest.mark.parametrize("position", [0, 3])
def test_resume_from_replay_supports_log_boundaries(tmp_path, position):
    path = tmp_path / "ref.json"
    write_log(path, 3)
    adapter = ReplayAdapter()
    replay = ReplayController(adapter)
    replay.load(path)
    replay.seek(position)
    recorder = Recorder(adapter)

    recorder.resume(replay.branch())

    document = json.loads(path.read_text(encoding="utf-8"))
    assert recorder.event_count == position
    assert len(document["steps"]) == position


def test_poll_debounces_and_appends_event_to_log_json(tmp_path):
    initial = snapshot(item("fp-1", "footprint", position={"x": 1}))
    moved = snapshot(item("fp-1", "footprint", position={"x": 2}))
    adapter = FakeAdapter(tmp_path, [initial, moved, moved])
    recorder = Recorder(adapter)

    recorder.start(RecorderConfig(settle_seconds=0.4))
    assert recorder.poll(now=10.0) is None
    event = recorder.poll(now=10.5)

    assert event is not None
    path = tmp_path / "ref.json"
    assert path.exists()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert set(persisted) == {"initial_pcb_path", "steps"}
    assert persisted["initial_pcb_path"] == str(tmp_path / "demo.kicad_pcb")
    assert persisted["steps"][0]["step"] == 1
    assert persisted["steps"][0]["step_uuid"] == event["event_uuid"]
    change = persisted["steps"][0]["changes"][0]
    assert change["id"] == "fp-1"
    assert "operation" in change
    assert "op" not in change


def test_simultaneous_track_add_remove_and_modify_use_separate_steps(tmp_path):
    initial = snapshot(
        item("track-delete", "track", width=100_000),
        item("track-modify", "track", width=100_000),
    )
    changed = snapshot(
        item("track-add", "track", width=200_000),
        item("track-modify", "track", width=300_000),
    )
    recorder = Recorder(FakeAdapter(tmp_path, [initial, changed]))
    recorder.start(RecorderConfig())

    recorder.end()

    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert recorder.event_count == 3
    assert [step["step"] for step in persisted["steps"]] == [1, 2, 3]
    assert len({step["step_uuid"] for step in persisted["steps"]}) == 3

    step_items = []
    operations = set()
    for step in persisted["steps"]:
        item_ids = {
            change.get("id")
            or change["item"]["data"]["id"]["value"]
            for change in step["changes"]
        }
        assert len(item_ids) == 1
        step_items.extend(item_ids)
        operations.update(change["operation"] for change in step["changes"])

    assert set(step_items) == {"track-add", "track-delete", "track-modify"}
    assert operations == {"routing.add", "routing.remove", "routing.modify"}


def test_connected_old_and_new_segments_in_one_path_edit_share_a_step(tmp_path):
    net = {"name": "GND"}
    initial = snapshot(
        item(
            "old-a",
            "track",
            start={"x_nm": "0", "y_nm": "0"},
            end={"x_nm": "100", "y_nm": "0"},
            net=net,
        ),
        item(
            "old-b",
            "track",
            start={"x_nm": "100", "y_nm": "0"},
            end={"x_nm": "200", "y_nm": "0"},
            net=net,
        ),
    )
    rerouted = snapshot(
        item(
            "new-a",
            "track",
            start={"x_nm": "0", "y_nm": "0"},
            end={"x_nm": "120", "y_nm": "50"},
            net=net,
        ),
        item(
            "new-b",
            "track",
            start={"x_nm": "120", "y_nm": "50"},
            end={"x_nm": "200", "y_nm": "0"},
            net=net,
        ),
    )
    recorder = Recorder(FakeAdapter(tmp_path, [initial, rerouted]))
    recorder.start(RecorderConfig())

    recorder.end()

    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert recorder.event_count == 1
    assert len(persisted["steps"]) == 1
    changes = persisted["steps"][0]["changes"]
    assert {change["operation"] for change in changes} == {
        "routing.add",
        "routing.remove",
    }
    assert {
        change.get("id") or change["item"]["data"]["id"]["value"]
        for change in changes
    } == {"old-a", "old-b", "new-a", "new-b"}


def test_deleting_then_redrawing_connected_path_is_coalesced_across_polls(tmp_path):
    net = {"name": "GND"}
    initial = snapshot(
        item(
            "old-a",
            "track",
            start={"x_nm": "0", "y_nm": "0"},
            end={"x_nm": "100", "y_nm": "0"},
            net=net,
        ),
        item(
            "old-b",
            "track",
            start={"x_nm": "100", "y_nm": "0"},
            end={"x_nm": "200", "y_nm": "0"},
            net=net,
        ),
    )
    deleted = snapshot()
    redrawn = snapshot(
        item(
            "new-a",
            "track",
            start={"x_nm": "0", "y_nm": "0"},
            end={"x_nm": "120", "y_nm": "50"},
            net=net,
        ),
        item(
            "new-b",
            "track",
            start={"x_nm": "120", "y_nm": "50"},
            end={"x_nm": "200", "y_nm": "0"},
            net=net,
        ),
    )
    adapter = FakeAdapter(
        tmp_path,
        [initial, deleted, deleted, redrawn, redrawn],
    )
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))

    recorder.poll(now=1)
    recorder.poll(now=2)
    assert recorder.event_count == 1
    recorder.poll(now=3)
    recorder.poll(now=4)

    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert recorder.event_count == 1
    assert len(persisted["steps"]) == 1
    assert {change["operation"] for change in persisted["steps"][0]["changes"]} == {
        "routing.add",
        "routing.remove",
    }


def test_deleting_then_drawing_an_unconnected_track_keeps_separate_steps(tmp_path):
    net = {"name": "GND"}
    initial = snapshot(
        item(
            "old",
            "track",
            start={"x_nm": "0", "y_nm": "0"},
            end={"x_nm": "100", "y_nm": "0"},
            net=net,
        )
    )
    deleted = snapshot()
    unrelated = snapshot(
        item(
            "new",
            "track",
            start={"x_nm": "1000", "y_nm": "0"},
            end={"x_nm": "1100", "y_nm": "0"},
            net=net,
        )
    )
    adapter = FakeAdapter(
        tmp_path,
        [initial, deleted, deleted, unrelated, unrelated],
    )
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))

    for now in range(1, 5):
        recorder.poll(now=now)

    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert recorder.event_count == 2
    assert [
        step["changes"][0]["operation"] for step in persisted["steps"]
    ] == ["routing.remove", "routing.add"]


def test_persisted_field_changes_use_target_only_format():
    replaced = Recorder._persisted_change(
        {
            "change_uuid": "change-1",
            "item_uuid": "zone-1",
            "item_kind": "zone",
            "operation": "zone.refill",
            "op": "replace",
            "path": "/items/zone-1/data/priority",
            "before": 1,
            "after": 2,
        }
    )
    removed = Recorder._persisted_change(
        {
            "change_uuid": "change-2",
            "item_uuid": "zone-1",
            "item_kind": "zone",
            "operation": "zone.refill",
            "op": "remove",
            "path": "/items/zone-1/data/priority",
            "before": 2,
        }
    )

    assert replaced == {
        "id": "zone-1",
        "operation": "zone.refill",
        "path": "/items/zone-1/data/priority",
        "value": 2,
    }
    assert removed == {
        "id": "zone-1",
        "operation": "zone.refill",
        "path": "/items/zone-1/data/priority",
        "delete": True,
    }


def test_persisted_footprint_field_move_uses_field_target_format():
    persisted = Recorder._persisted_change(
        {
            "change_uuid": "change-1",
            "item_uuid": "fp-1",
            "item_kind": "footprint",
            "operation": "footprint.field.modify",
            "op": "replace",
            "path": "/items/fp-1/data/reference_field/text/text/position/x_nm",
            "before": "110",
            "after": "160",
        }
    )

    assert persisted == {
        "id": "fp-1",
        "operation": "footprint.field.modify",
        "path": "/items/fp-1/data/reference_field/text/text/position/x_nm",
        "value": "160",
    }


def test_note_flushes_change_and_saves_live_copy(tmp_path):
    initial = snapshot()
    routed = snapshot(item("track-1", "track", width=250000))
    adapter = FakeAdapter(tmp_path, [initial, routed])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(pcb_stem="ref.json"))

    note_path = recorder.note()

    assert (tmp_path / "ref.json").exists()
    assert note_path == tmp_path / "ref_001.kicad_pcb"
    assert note_path.read_text(encoding="utf-8") == "(kicad_pcb)"


def test_note_name_uses_step_when_one_step_has_multiple_changes(tmp_path):
    initial = snapshot()
    changed = snapshot(
        item("track-1", "track", width=250000),
        item("via-1", "via", diameter=600000),
    )
    recorder = Recorder(FakeAdapter(tmp_path, [initial, changed]))
    recorder.start(RecorderConfig(pcb_stem="ref"))

    note_path = recorder.note()

    assert recorder.event_count == 1
    assert recorder.recorded_position == 1
    assert note_path.name == "ref_001.kicad_pcb"


def test_note_at_same_recorded_position_does_not_overwrite(tmp_path):
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot()]))
    recorder.start(RecorderConfig(pcb_stem="ref"))
    first = recorder.note()

    with pytest.raises(RecorderError, match="position 0 is already marked"):
        recorder.note()

    assert first.name == "ref_000.kicad_pcb"


def test_full_board_copper_zones_are_recorded_for_replay(tmp_path):
    initial = snapshot()
    filled = snapshot(
        item("zone-front", "zone", layers=["BL_F_Cu"], net={"name": "GND"}),
        item("zone-back", "zone", layers=["BL_B_Cu"], net={"name": "GND"}),
    )
    recorder = Recorder(FakeAdapter(tmp_path, [initial, filled]))
    recorder.start(RecorderConfig())

    recorder.end()

    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert len(persisted["steps"]) == 1
    changes = persisted["steps"][0]["changes"]
    assert [change["operation"] for change in changes] == [
        "zone.add",
        "zone.add",
    ]
    assert all("item" in change for change in changes)
    assert all("before" not in change and "after" not in change for change in changes)
    assert all(set(change["item"]) == {"type", "data"} for change in changes)
    assert persisted["steps"][0]["step"] == 1
    assert isinstance(persisted["steps"][0]["step_uuid"], str)


def test_persisted_complete_item_keeps_type_and_native_id_without_kind():
    persisted = Recorder._persisted_change(
        {
            "change_uuid": "change-1",
            "item_uuid": "track-1",
            "item_kind": "track",
            "operation": "routing.add",
            "op": "add",
            "path": "/items/track-1",
            "after": {
                "kind": "track",
                "type": "kiapi.board.types.Track",
                "data": {"id": {"value": "track-1"}, "width": "250000"},
            },
        }
    )

    assert persisted["item"] == {
        "type": "kiapi.board.types.Track",
        "data": {"id": {"value": "track-1"}, "width": "250000"},
    }
    assert "item_uuid" not in persisted
    assert "id" not in persisted


def test_undo_restores_board_and_removes_event_from_log(tmp_path):
    initial = snapshot()
    with_via = snapshot(item("via-1", "via", diameter=600000))
    adapter = FakeAdapter(tmp_path, [initial, with_via, with_via])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))
    recorder.poll(now=1)
    recorder.poll(now=2)
    log_path = tmp_path / "ref.json"
    assert log_path.exists()

    removed, strategy = recorder.undo()

    assert removed == log_path
    assert strategy == "native"
    assert json.loads(log_path.read_text(encoding="utf-8"))["steps"] == []
    assert recorder.baseline == initial
    assert recorder.event_count == 0


def test_undo_to_returns_to_selected_record_position(tmp_path):
    initial = snapshot()
    first = snapshot(item("via-1", "via", diameter=600000))
    second = snapshot(
        item("via-1", "via", diameter=600000),
        item("via-2", "via", diameter=600000),
    )
    third = snapshot(
        item("via-1", "via", diameter=600000),
        item("via-2", "via", diameter=600000),
        item("via-3", "via", diameter=600000),
    )
    adapter = FakeAdapter(
        tmp_path,
        [initial, first, first, second, second, third, third, third],
    )
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))
    for now in range(1, 7):
        recorder.poll(now=now)
    assert recorder.event_count == 3

    path, strategies = recorder.undo_to(1)

    assert path.name == "ref.json"
    assert strategies == ("native", "native")
    assert recorder.event_count == 1
    assert recorder.baseline == first
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert len(persisted["steps"]) == 1


def test_record_preview_changes_board_before_undo_confirms_log_truncation(tmp_path):
    initial = snapshot()
    first = snapshot(item("via-1", "via", diameter=600000))
    second = snapshot(
        item("via-1", "via", diameter=600000),
        item("via-2", "via", diameter=600000),
    )
    adapter = FakeAdapter(tmp_path, [initial, first, first, second, second, second])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))
    for now in range(1, 5):
        recorder.poll(now=now)
    assert recorder.event_count == 2

    position = recorder.preview(1)

    assert position == 1
    assert recorder.preview_position == 1
    assert adapter.current == first
    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert len(persisted["steps"]) == 2
    assert recorder.poll(now=10) is None

    path, strategy = recorder.undo()

    assert strategy == "preview"
    assert recorder.preview_position is None
    assert recorder.event_count == 1
    assert recorder.baseline == first
    assert len(json.loads(path.read_text(encoding="utf-8"))["steps"]) == 1


def test_confirmed_preview_continues_recording_from_reset_state(tmp_path):
    initial = snapshot()
    first = snapshot(item("via-1", "via", diameter=600000))
    second = snapshot(
        item("via-1", "via", diameter=600000),
        item("via-2", "via", diameter=600000),
    )
    replacement = snapshot(
        item("via-1", "via", diameter=600000),
        item("via-3", "via", diameter=600000),
    )
    adapter = FakeAdapter(tmp_path, [initial, first, first, second, second, second])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))
    for now in range(1, 5):
        recorder.poll(now=now)
    recorder.preview(1)

    recorder.confirm_preview()
    adapter.snapshots.extend([replacement, replacement])
    recorder.poll(now=10)
    recorder.poll(now=11)

    assert recorder.recording
    assert recorder.event_count == 2
    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert [step["step"] for step in persisted["steps"]] == [1, 2]
    assert len({step["step_uuid"] for step in persisted["steps"]}) == 2
    assert (
        persisted["steps"][-1]["changes"][0]["item"]["data"]["id"]["value"]
        == "via-3"
    )


def test_end_flushes_and_stops(tmp_path):
    initial = snapshot()
    changed = snapshot(item("zone-1", "zone", outline=[1]))
    adapter = FakeAdapter(tmp_path, [initial, changed])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig())

    event = recorder.end()

    assert event is not None
    assert not recorder.recording
    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert len(persisted["steps"]) == 1


def test_end_moves_all_silkscreen_adjustments_to_one_final_step(tmp_path):
    initial = snapshot(
        item(
            "fp-1",
            "footprint",
            position={"x_nm": "100", "y_nm": "200"},
            reference_field={"text": {"text": {"position": {"x_nm": "110"}}}},
        )
    )
    silk_once = snapshot(
        item(
            "fp-1",
            "footprint",
            position={"x_nm": "100", "y_nm": "200"},
            reference_field={"text": {"text": {"position": {"x_nm": "160"}}}},
        )
    )
    with_via = snapshot(
        item(
            "fp-1",
            "footprint",
            position={"x_nm": "100", "y_nm": "200"},
            reference_field={"text": {"text": {"position": {"x_nm": "160"}}}},
        ),
        item("via-1", "via", diameter=600000),
    )
    silk_final = snapshot(
        item(
            "fp-1",
            "footprint",
            position={"x_nm": "100", "y_nm": "200"},
            reference_field={"text": {"text": {"position": {"x_nm": "190"}}}},
        ),
        item("via-1", "via", diameter=600000),
    )
    adapter = FakeAdapter(
        tmp_path,
        [initial, silk_once, silk_once, with_via, with_via, silk_final, silk_final],
    )
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))
    for now in range(1, 7):
        recorder.poll(now=now)

    recorder.end()

    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert [step["step"] for step in persisted["steps"]] == [1, 2]
    assert [change["operation"] for change in persisted["steps"][0]["changes"]] == [
        "via.add"
    ]
    final_changes = persisted["steps"][-1]["changes"]
    assert [change["operation"] for change in final_changes] == [
        "footprint.field.modify"
    ]
    assert final_changes[0]["value"] == "190"


def test_end_splits_silkscreen_changes_from_a_mixed_step(tmp_path):
    initial = snapshot(
        item(
            "fp-1",
            "footprint",
            reference_field={"text": {"text": {"position": {"x_nm": "110"}}}},
        ),
        item("zone-1", "zone", priority=1),
    )
    changed = snapshot(
        item(
            "fp-1",
            "footprint",
            reference_field={"text": {"text": {"position": {"x_nm": "160"}}}},
        ),
        item("zone-1", "zone", priority=2),
    )
    recorder = Recorder(FakeAdapter(tmp_path, [initial, changed]))
    recorder.start(RecorderConfig())

    recorder.end()

    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert [step["step"] for step in persisted["steps"]] == [1, 2]
    assert {change["operation"] for change in persisted["steps"][0]["changes"]} == {
        "zone.modify"
    }
    assert {change["operation"] for change in persisted["steps"][1]["changes"]} == {
        "footprint.field.modify"
    }
    assert persisted["steps"][0]["step_uuid"] != persisted["steps"][1]["step_uuid"]


def test_undo_without_history_is_rejected(tmp_path):
    initial = snapshot()
    recorder = Recorder(FakeAdapter(tmp_path, [initial]))
    recorder.start(RecorderConfig())
    with pytest.raises(RecorderError, match="no recorded operations"):
        recorder.undo()


def test_undo_flushes_a_change_that_is_still_inside_debounce_window(tmp_path):
    initial = snapshot()
    moved = snapshot(item("fp-1", "footprint", position={"x": 2}))
    adapter = FakeAdapter(tmp_path, [initial, moved])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=60))

    removed, _ = recorder.undo()

    assert removed.name == "ref.json"
    assert json.loads(removed.read_text(encoding="utf-8"))["steps"] == []
    assert recorder.baseline == initial


def test_start_warns_and_does_not_overwrite_existing_log(tmp_path):
    path = tmp_path / "ref.json"
    path.write_text('{"keep": true}\n', encoding="utf-8")
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot()]))

    with pytest.raises(LogFileExistsError, match="already exists"):
        recorder.start(RecorderConfig())

    assert path.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert not recorder.recording


def test_start_overwrites_existing_log_only_when_explicitly_enabled(tmp_path):
    path = tmp_path / "ref.json"
    path.write_text('{"keep": true}\n', encoding="utf-8")
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot()]))

    recorder.start(RecorderConfig(overwrite_existing=True))

    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert set(persisted) == {"initial_pcb_path", "steps"}
    assert persisted["steps"] == []
    assert recorder.recording


def test_failed_initial_board_restore_does_not_overwrite_log(tmp_path):
    class RestoreFailureAdapter(FakeAdapter):
        def prepare_recording(self):
            raise RecorderError("restore failed")

    path = tmp_path / "ref.json"
    path.write_text('{"keep": true}\n', encoding="utf-8")
    recorder = Recorder(RestoreFailureAdapter(tmp_path, [snapshot()]))

    with pytest.raises(RecorderError, match="restore failed"):
        recorder.start(RecorderConfig(overwrite_existing=True))

    assert path.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert not recorder.recording


def test_alternate_log_name_can_be_used_after_conflict(tmp_path):
    (tmp_path / "ref.json").write_text("{}\n", encoding="utf-8")
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot()]))

    recorder.start(RecorderConfig(pcb_stem="next"))

    assert (tmp_path / "next.json").exists()
    assert (tmp_path / "ref.json").read_text(encoding="utf-8") == "{}\n"


def test_consecutive_footprint_transforms_keep_only_final_angle(tmp_path):
    initial = snapshot(
        item("fp-1", "footprint", position={"x": 10, "y": 20}, orientation=0)
    )
    intermediate = snapshot(
        item("fp-1", "footprint", position={"x": 12, "y": 22}, orientation=90)
    )
    final = snapshot(
        item("fp-1", "footprint", position={"x": 15, "y": 25}, orientation=270)
    )
    adapter = FakeAdapter(
        tmp_path,
        [initial, intermediate, intermediate, final, final],
    )
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))

    recorder.poll(now=1.0)
    recorder.poll(now=1.1)
    recorder.poll(now=100.0)
    recorder.poll(now=100.1)

    persisted = json.loads((tmp_path / "ref.json").read_text(encoding="utf-8"))
    assert len(persisted["steps"]) == 1
    assert persisted["steps"][0]["step"] == 1
    transform = persisted["steps"][0]["changes"][0]
    assert set(transform) == {
        "id",
        "operation",
        "position",
        "orientation",
    }
    assert transform["operation"] == "footprint.move"
    assert transform["position"] == {"x": 15, "y": 25}
    assert transform["orientation"] == 270
    assert recorder.event_count == 1
