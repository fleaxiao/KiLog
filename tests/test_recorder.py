from __future__ import annotations

import json

import pytest

from kilog.recorder import LogFileExistsError, Recorder, RecorderConfig, RecorderError
from kilog.replay import ReplayController
from tests.test_replay import ReplayAdapter, write_log
from tests.helpers import FakeAdapter, item, snapshot


def _recorded_document(recorder):
    if recorder.recording:
        return recorder._log_document(recorder.baseline)
    return json.loads(recorder.log_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("existing", [False, True])
def test_recording_only_writes_log_on_stop(tmp_path, monkeypatch, existing):
    import kilog.recorder as recorder_module

    path = tmp_path / "ref.json"
    original = b'{"keep": true}\n'
    if existing:
        path.write_bytes(original)
    initial = snapshot()
    first = snapshot(item("a", "track", width=100))
    second = snapshot(item("a", "track", width=200))
    adapter = FakeAdapter(tmp_path, [initial, first, second])
    recorder = Recorder(adapter)
    writes = []
    writer_name = "write_json_atomic" if existing else "write_json_new"
    real_writer = getattr(recorder_module, writer_name)

    def writer(*args):
        writes.append(args[0])
        return real_writer(*args)

    monkeypatch.setattr(recorder_module, writer_name, writer)
    recorder.start(RecorderConfig(overwrite_existing=existing))
    recorder.poll()
    recorder.flush()
    recorder.undo()
    recorder.preview(0)
    recorder.confirm_preview()
    assert writes == []
    if existing:
        assert path.read_bytes() == original
    else:
        assert not path.exists()

    recorder.end()
    assert writes == [path]
    assert json.loads(path.read_text())["steps"] == []
    assert not recorder.recording


def test_resume_keeps_original_file_until_stop(tmp_path):
    path = tmp_path / "ref.json"
    write_log(path, 3)
    original = path.read_bytes()
    adapter = ReplayAdapter()
    replay = ReplayController(adapter)
    replay.load(path)
    replay.seek(1)
    recorder = Recorder(adapter)
    recorder.resume(replay.branch())
    assert path.read_bytes() == original
    assert recorder.event_count == 1
    recorder.end()
    assert len(json.loads(path.read_text())["steps"]) == 1


def test_failed_stop_keeps_recording_for_retry(tmp_path, monkeypatch):
    import kilog.recorder as recorder_module

    path = tmp_path / "ref.json"
    original = b'{"keep": true}\n'
    path.write_bytes(original)
    recorder = Recorder(FakeAdapter(tmp_path, [
        snapshot(), snapshot(item("a", "track", width=100)),
    ]))
    recorder.start(RecorderConfig(overwrite_existing=True))
    real_writer = recorder_module.write_json_atomic

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(recorder_module, "write_json_atomic", fail)
    with pytest.raises(OSError, match="disk unavailable"):
        recorder.end()
    assert recorder.recording
    assert recorder.event_count == 1
    assert path.read_bytes() == original
    monkeypatch.setattr(recorder_module, "write_json_atomic", real_writer)
    recorder.end()
    assert not recorder.recording
    assert len(json.loads(path.read_text())["steps"]) == 1


def test_stop_does_not_overwrite_file_created_during_recording(tmp_path):
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot()]))
    recorder.start(RecorderConfig())
    path = tmp_path / "ref.json"
    path.write_text("external log", encoding="utf-8")
    with pytest.raises(LogFileExistsError, match="appeared during recording"):
        recorder.end()
    assert path.read_text() == "external log"
    assert recorder.recording


def test_resume_from_replay_truncates_future_and_records_from_current_step(tmp_path):
    path = tmp_path / "ref.json"
    write_log(path, 3)
    adapter = ReplayAdapter()
    replay = ReplayController(adapter)
    replay.load(path)
    replay.seek(2)
    recorder = Recorder(adapter)

    recorder.resume(replay.branch())

    truncated = _recorded_document(recorder)
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

    continued = _recorded_document(recorder)
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

    document = _recorded_document(recorder)
    assert recorder.event_count == position
    assert len(document["steps"]) == position


def test_poll_records_each_observed_change(tmp_path):
    initial = snapshot(item("fp-1", "footprint", position={"x": 1}))
    moved = snapshot(item("fp-1", "footprint", position={"x": 2}))
    adapter = FakeAdapter(tmp_path, [initial, moved, moved])
    recorder = Recorder(adapter)

    recorder.start(RecorderConfig(settle_seconds=0.4))
    event = recorder.poll(now=10.0)
    assert recorder.poll(now=10.5) is None

    assert event is not None
    path = tmp_path / "ref.json"
    assert not path.exists()
    persisted = _recorded_document(recorder)
    assert set(persisted) == {"initial_pcb_path", "steps"}
    assert persisted["initial_pcb_path"] == str(tmp_path / "demo.kicad_pcb")
    assert persisted["steps"][0]["step"] == 1
    assert persisted["steps"][0]["step_uuid"] == event["event_uuid"]
    change = persisted["steps"][0]["changes"][0]
    assert change["id"] == "fp-1"
    assert "operation" in change
    assert "op" not in change


def test_simultaneous_track_changes_stay_in_one_observed_step(tmp_path):
    initial = snapshot(_route("old", 0, 100), _route("changed", 1000, 1100))
    final = snapshot(_route("new", 2000, 2100), _route("changed", 1000, 1100, width=200))
    recorder = Recorder(FakeAdapter(tmp_path, [initial, final]))
    recorder.start(RecorderConfig())
    recorder.end()
    assert recorder.event_count == 1
    assert {c["operation"] for c in recorder.events[0]["changes"]} == {
        "routing.add", "routing.remove", "routing.modify"}


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

    persisted = _recorded_document(recorder)
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


def test_single_step_flush_keeps_disconnected_fanout_items_together(tmp_path):
    net = {"name": "GND"}
    initial = snapshot()
    fanned_out = snapshot(
        item(
            "track-1",
            "track",
            start={"x_nm": "0", "y_nm": "0"},
            end={"x_nm": "100", "y_nm": "0"},
            net=net,
        ),
        item("via-1", "via", position={"x_nm": "100", "y_nm": "0"}, net=net),
        item(
            "track-2",
            "track",
            start={"x_nm": "1000", "y_nm": "0"},
            end={"x_nm": "1100", "y_nm": "0"},
            net=net,
        ),
        item("via-2", "via", position={"x_nm": "1100", "y_nm": "0"}, net=net),
    )
    recorder = Recorder(FakeAdapter(tmp_path, [initial, fanned_out]))
    recorder.start(RecorderConfig())

    recorder.flush(single_step=True)

    persisted = _recorded_document(recorder)
    assert recorder.event_count == 1
    assert len(persisted["steps"]) == 1
    assert {
        change.get("id") or change["item"]["data"]["id"]["value"]
        for change in persisted["steps"][0]["changes"]
    } == {"track-1", "via-1", "track-2", "via-2"}


def test_deleting_then_redrawing_connected_path_stays_in_observed_steps(tmp_path):
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

    persisted = _recorded_document(recorder)
    assert recorder.event_count == 2
    assert len(persisted["steps"]) == 2
    assert {change["operation"] for step in persisted["steps"] for change in step["changes"]} == {
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

    persisted = _recorded_document(recorder)
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


def _route(item_uuid, start, end, layer="BL_F_Cu", width=100):
    return item(item_uuid, "track", start={"x_nm": str(start), "y_nm": "0"},
                end={"x_nm": str(end), "y_nm": "0"},
                layer=layer, net={"name": "GND"}, width=width)


@pytest.mark.parametrize("end,layer", [(300, "BL_F_Cu"), (100, "BL_B_Cu")])
def test_touching_branch_or_other_layer_is_not_a_reroute(tmp_path, end, layer):
    initial = snapshot(_route("old", 0, 100))
    final = snapshot(_route("new", 0, end, layer))
    recorder = Recorder(FakeAdapter(tmp_path, [initial, snapshot(), final]))
    recorder.start(RecorderConfig())
    recorder.flush()
    recorder.flush()
    assert recorder.event_count == 2
    assert [e["changes"][0]["operation"] for e in recorder.events] == [
        "routing.remove", "routing.add",
    ]


def test_same_segment_edits_keep_observed_order(tmp_path):
    states = [snapshot(), snapshot(_route("z-first", 0, 100)),
              snapshot(_route("z-first", 0, 100, width=200)),
              snapshot(_route("z-first", 0, 100, width=200), _route("a-second", 500, 600))]
    recorder = Recorder(FakeAdapter(tmp_path, states))
    recorder.start(RecorderConfig())
    for _ in range(3):
        recorder.flush()
    assert recorder.event_count == 3
    assert recorder.history == states[:-1]
    assert [e["changes"][0]["item_uuid"] for e in recorder.events] == ["z-first", "z-first", "a-second"]


def test_fast_observed_changes_are_not_debounced_together(tmp_path):
    states = [snapshot(), snapshot(_route("z-first", 0, 100)),
              snapshot(_route("z-first", 0, 100), _route("a-second", 500, 600))]
    recorder = Recorder(FakeAdapter(tmp_path, states))
    recorder.start(RecorderConfig(settle_seconds=60))
    recorder.poll(now=1)
    recorder.poll(now=1.01)
    assert recorder.event_count == 2
    assert recorder.history == states[:-1]


def _dragged_path(middle, bend):
    points = [(0, 0), (30, bend), (70, bend), (100, 0)]
    return snapshot(*[
        item(uuid, "track", start={"x_nm": str(start[0]), "y_nm": str(start[1])},
             end={"x_nm": str(end[0]), "y_nm": str(end[1])},
             net={"name": "GND"}, layer="BL_F_Cu", width=100)
        for uuid, start, end in zip(["left", middle, "right"], points, points[1:])
    ])


@pytest.mark.parametrize("replace_uuid", [False, True])
def test_dragging_path_does_not_merge_distinct_observations(tmp_path, replace_uuid):
    initial = _dragged_path("middle", 0)
    moved = _dragged_path("middle-1" if replace_uuid else "middle", 20)
    final = _dragged_path("middle-2" if replace_uuid else "middle", 40)
    recorder = Recorder(FakeAdapter(tmp_path, [initial, moved, moved, final, final]))
    recorder.start(RecorderConfig(settle_seconds=0))
    recorder.poll(now=1)
    recorder.poll(now=2)
    assert recorder.event_count == 1
    step_uuid = recorder.events[0]["event_uuid"]
    recorder.poll(now=3)
    recorder.poll(now=4)
    assert recorder.event_count == 2
    assert recorder.events[0]["event_uuid"] == step_uuid
    assert recorder.baseline == final
    document = _recorded_document(recorder)
    assert len(document["steps"]) == 2
    recorder.undo()
    assert recorder.baseline == moved
    assert recorder.event_count == 1


@pytest.mark.parametrize("kind", ["track", "via", "zone"])
def test_native_undo_rewinds_despite_field_ids_and_fill_cache(tmp_path, kind):
    def board(child_id, filled, added=False):
        values = [
            item("fp", "footprint", reference_field={"text": {
                "id": {"value": child_id}, "text": "U1", "position": {"x_nm": "10"}}}),
            item("old-zone", "zone", outline=[1, 2], filled=filled),
        ]
        if added:
            values.append(item("created", kind, width=200000))
        return snapshot(*values)
    initial = board("old", False)
    created = board("old", False, True)
    regenerated = board("new", False, True)
    undone = board("newer", True)
    recorder = Recorder(FakeAdapter(tmp_path, [initial, created, regenerated, undone]))
    recorder.start(RecorderConfig())
    recorder.poll()
    assert recorder.event_count == 1
    recorder.poll()
    assert recorder.event_count == 1
    recorder.poll()
    assert recorder.event_count == 0
    recorder.end()
    assert json.loads(recorder.log_path.read_text())["steps"] == []


def test_zone_fill_is_recorded_separately_from_outline_and_survives_save(tmp_path):
    outline = snapshot(item("zone", "zone", outline=[1, 2], filled=False))
    filled = snapshot(item("zone", "zone", outline=[1, 2], filled=True))
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot(), outline, filled]))
    recorder.start(RecorderConfig())
    recorder.poll()
    recorder.poll()
    assert recorder.event_count == 2
    recorder.end()
    steps = json.loads(recorder.log_path.read_text())["steps"]
    assert steps[0]["changes"][0]["operation"] == "zone.add"
    assert steps[1]["changes"] == [{
        "operation": "zone.refill", "id": "zone",
        "path": "/items/zone/data/filled", "value": True,
    }]


def test_zone_fill_undo_and_redo_preserve_outline_step(tmp_path):
    outline = snapshot(item("zone", "zone", outline=[1, 2], filled=False))
    filled = snapshot(item("zone", "zone", outline=[1, 2], filled=True))
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot(), outline, filled, outline, filled]))
    recorder.start(RecorderConfig())
    for count in (1, 2, 1, 2):
        recorder.poll()
        assert recorder.event_count == count


def test_observed_return_to_original_rewinds_steps(tmp_path):
    initial = _dragged_path("middle", 0)
    recorder = Recorder(FakeAdapter(tmp_path, [
        initial, _dragged_path("middle", 20), initial,
    ]))
    recorder.start(RecorderConfig())
    recorder.flush()
    recorder.flush()
    assert recorder.event_count == 0
    assert recorder.baseline == initial
    assert _recorded_document(recorder)["steps"] == []


def test_poll_follows_native_undo_and_redo_without_issuing_board_commands(tmp_path):
    initial = snapshot()
    first = snapshot(_route("a", 0, 100))
    second = snapshot(_route("a", 0, 100), _route("b", 100, 200))

    class ObservedAdapter(FakeAdapter):
        def undo_to(self, target):
            pytest.fail("Polling must not issue another undo")

        def restore_snapshot(self, *args, **kwargs):
            pytest.fail("Polling must not modify the board")

    recorder = Recorder(ObservedAdapter(tmp_path, [
        initial, first, second, first, initial, first, second,
    ]))
    recorder.start(RecorderConfig())
    for expected_count in [1, 2, 1, 0, 1, 2]:
        recorder.poll()
        assert recorder.event_count == expected_count
        assert len(recorder.events) == expected_count
        assert not recorder.log_path.exists()
    recorder.end()
    document = json.loads(recorder.log_path.read_text())
    assert [step["step"] for step in document["steps"]] == [1, 2]
    assert [step["changes"][0]["operation"] for step in document["steps"]] == [
        "routing.add", "routing.add",
    ]


def test_multiple_undos_then_new_edit_discards_undone_steps(tmp_path):
    initial = snapshot()
    first = snapshot(_route("a", 0, 100))
    second = snapshot(_route("a", 0, 100), _route("b", 100, 200))
    third = snapshot(*second.items.values(), _route("c", 200, 300))
    replacement = snapshot(*first.items.values(), _route("d", 400, 500))
    recorder = Recorder(FakeAdapter(tmp_path, [
        initial, first, second, third, first, replacement,
    ]))
    recorder.start(RecorderConfig())
    for _ in range(3):
        recorder.poll()
    first_uuid = recorder.events[0]["event_uuid"]
    assert recorder.event_count == 3
    recorder.poll()
    assert recorder.event_count == 1
    assert recorder.events[0]["event_uuid"] == first_uuid
    recorder.poll()
    assert recorder.event_count == 2
    assert recorder.history == [initial, first]
    recorder.end()
    document = json.loads(recorder.log_path.read_text())
    assert [s["changes"][0]["item"]["data"]["id"]["value"]
            for s in document["steps"]] == ["a", "d"]


def test_stop_catches_undo_before_next_poll(tmp_path):
    initial = snapshot()
    first = snapshot(_route("a", 0, 100))
    recorder = Recorder(FakeAdapter(tmp_path, [initial, first, initial]))
    recorder.start(RecorderConfig())
    recorder.poll()
    assert recorder.event_count == 1
    recorder.end()
    assert recorder.event_count == 0
    assert json.loads(recorder.log_path.read_text())["steps"] == []


def test_intervening_operation_separates_drags_of_same_path(tmp_path):
    initial = _dragged_path("middle", 0)
    moved = _dragged_path("middle-1", 20)
    via = item("via", "via", diameter=600000)
    with_via = snapshot(*moved.items.values(), via)
    final = snapshot(*_dragged_path("middle-2", 40).items.values(), via)
    recorder = Recorder(FakeAdapter(tmp_path, [initial, moved, with_via, final]))
    recorder.start(RecorderConfig())
    for _ in range(3):
        recorder.flush()
    assert recorder.event_count == 3
    assert recorder.events[1]["changes"][0]["operation"] == "via.add"
    assert recorder.history == [initial, moved, with_via]


def test_new_connected_branch_does_not_merge_into_previous_drag(tmp_path):
    initial = _dragged_path("middle", 0)
    moved = _dragged_path("middle-1", 20)
    final = snapshot(*moved.items.values(), _route("branch", 100, 200))
    recorder = Recorder(FakeAdapter(tmp_path, [initial, moved, final]))
    recorder.start(RecorderConfig())
    recorder.flush()
    recorder.flush()
    assert recorder.event_count == 2
    assert recorder.events[-1]["changes"][0]["item_uuid"] == "branch"


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

    assert not (tmp_path / "ref.json").exists()
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

    persisted = _recorded_document(recorder)
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
    assert not log_path.exists()

    removed, strategy = recorder.undo()

    assert removed == log_path
    assert strategy == "native"
    assert _recorded_document(recorder)["steps"] == []
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
    persisted = _recorded_document(recorder)
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
    persisted = _recorded_document(recorder)
    assert len(persisted["steps"]) == 2
    assert recorder.poll(now=10) is None

    path, strategy = recorder.undo()

    assert strategy == "preview"
    assert recorder.preview_position is None
    assert recorder.event_count == 1
    assert recorder.baseline == first
    assert len(_recorded_document(recorder)["steps"]) == 1


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
    persisted = _recorded_document(recorder)
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
    persisted = _recorded_document(recorder)
    assert len(persisted["steps"]) == 1


def test_end_collects_silkscreen_in_final_step(tmp_path):
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

    persisted = _recorded_document(recorder)
    assert [step["step"] for step in persisted["steps"]] == [1, 2]
    assert [change["operation"] for change in persisted["steps"][0]["changes"]] == [
        "via.add"
    ]
    final_changes = persisted["steps"][-1]["changes"]
    assert [change["operation"] for change in final_changes] == [
        "footprint.field.modify"
    ]
    assert final_changes[0]["value"] == "190"


def test_end_extracts_silkscreen_from_mixed_step(tmp_path):
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

    persisted = _recorded_document(recorder)
    assert len(persisted["steps"]) == 2
    assert {c["operation"] for c in persisted["steps"][0]["changes"]} == {
        "zone.modify"}
    assert {c["operation"] for c in persisted["steps"][1]["changes"]} == {
        "footprint.field.modify"}


def test_undo_without_history_is_rejected(tmp_path):
    initial = snapshot()
    recorder = Recorder(FakeAdapter(tmp_path, [initial]))
    recorder.start(RecorderConfig())
    with pytest.raises(RecorderError, match="no recorded operations"):
        recorder.undo()


def test_native_undo_can_cross_multiple_observations(tmp_path):
    initial = snapshot()
    first = snapshot(_route("a", 0, 100))
    second = snapshot(_route("a", 0, 100, width=200))

    class NativeAdapter(FakeAdapter):
        def undo_to(self, target):
            self.current = initial
            return initial, "native"

        def restore_snapshot(self, *args, **kwargs):
            pytest.fail("Native undo must not be followed by a snapshot restoration")

    recorder = Recorder(NativeAdapter(tmp_path, [initial, first, second]))
    recorder.start(RecorderConfig())
    recorder.flush()
    recorder.flush()
    recorder.undo()
    assert recorder.event_count == 0
    assert recorder.baseline == initial
    assert _recorded_document(recorder)["steps"] == []


def test_native_undo_to_unobserved_state_keeps_actual_result(tmp_path):
    initial = snapshot()
    intermediate = snapshot(_route("a", 0, 100))
    final = snapshot(_route("a", 0, 100, width=200))

    class NativeAdapter(FakeAdapter):
        def undo_to(self, target):
            self.current = intermediate
            return intermediate, "native"

        def restore_snapshot(self, *args, **kwargs):
            pytest.fail("Native undo must not be followed by a snapshot restoration")

    recorder = Recorder(NativeAdapter(tmp_path, [initial, final]))
    recorder.start(RecorderConfig())
    recorder.flush()
    with pytest.raises(RecorderError, match="between recorded positions"):
        recorder.undo()
    assert recorder.adapter.current == intermediate
    assert recorder.baseline == intermediate
    assert recorder.events[-1]["changes"][0]["after"] == 100


def test_undo_flushes_a_change_that_is_still_inside_debounce_window(tmp_path):
    initial = snapshot()
    moved = snapshot(item("fp-1", "footprint", position={"x": 2}))
    adapter = FakeAdapter(tmp_path, [initial, moved])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=60))

    removed, _ = recorder.undo()

    assert removed.name == "ref.json"
    assert _recorded_document(recorder)["steps"] == []
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

    persisted = _recorded_document(recorder)
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

    assert not (tmp_path / "next.json").exists()
    assert (tmp_path / "ref.json").read_text(encoding="utf-8") == "{}\n"


def test_consecutive_footprint_transforms_keep_distinct_observations(tmp_path):
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

    persisted = _recorded_document(recorder)
    assert len(persisted["steps"]) == 2
    assert persisted["steps"][0]["step"] == 1
    transform = persisted["steps"][1]["changes"][0]
    assert set(transform) == {
        "id",
        "operation",
        "position",
        "orientation",
    }
    assert transform["operation"] == "footprint.move"
    assert transform["position"] == {"x": 15, "y": 25}
    assert transform["orientation"] == 270
    assert recorder.event_count == 2
    recorder.end()
    saved = _recorded_document(recorder)
    assert len(saved["steps"]) == 1
    assert saved["steps"][0]["changes"][0] == transform
    assert recorder.event_count == 2  # Export compaction does not rewrite live history.


@pytest.mark.parametrize("barrier", ["footprint", "track", "silk"])
def test_stop_does_not_merge_moves_across_other_operations(barrier):
    import copy

    def move(x):
        return {"operation": "footprint.move", "id": "fp", "position": {"x": x},
                "orientation": x * 90}

    intervening = {
        "footprint": {**move(9), "id": "other"},
        "track": {"operation": "routing.remove", "id": "track"},
        "silk": {"operation": "footprint.field.modify", "id": "fp",
                 "path": "/items/fp/data/reference_field/text", "value": "R1"},
    }[barrier]
    document = {"initial_pcb_path": "demo.kicad_pcb", "steps": [
        {"step": i, "step_uuid": str(i), "changes": [change]}
        for i, change in enumerate([move(1), move(2), intervening, move(3), move(4)], 1)
    ]}
    original = copy.deepcopy(document)
    final = snapshot(item("fp", "footprint", reference_field={"text": "R1"}))
    result = Recorder._finalize_document(document, final, final)
    moves = [c for s in result["steps"] for c in s["changes"]
             if c["operation"] == "footprint.move" and c["id"] == "fp"]
    assert moves == [move(2), move(4)]
    assert document == original
    assert [s["step"] for s in result["steps"]] == [1, 2, 3]
    assert len({s["step_uuid"] for s in result["steps"]}) == 3


def test_stop_uses_final_silk_position_after_footprint_move(tmp_path):
    def footprint(x, silk_x):
        return item("fp", "footprint", position={"x": x}, orientation=0,
                    reference_field={"text": {"position": {"x": silk_x}}})

    states = [snapshot(footprint(0, 0)), snapshot(footprint(0, 5)),
              snapshot(footprint(10, 15))]
    recorder = Recorder(FakeAdapter(tmp_path, states))
    recorder.start(RecorderConfig())
    recorder.poll()
    recorder.poll()
    recorder.end()
    steps = json.loads(recorder.log_path.read_text())["steps"]
    assert [s["changes"][0]["operation"] for s in steps] == [
        "footprint.move", "footprint.field.modify"]
    assert steps[-1]["changes"][0]["value"] == 15


def test_stop_collects_standalone_silk_text_and_graphics(tmp_path):
    text = item("silk", "text", layer="BL_F_SilkS", position={"x": 0})
    moved = item("silk", "text", layer="BL_F_SilkS", position={"x": 5})
    line = item("outline", "shape", layer="BL_B_SilkS", width=100)
    route = _route("track", 0, 100)
    states = [snapshot(text), snapshot(moved, line), snapshot(moved, line, route)]
    recorder = Recorder(FakeAdapter(tmp_path, states))
    recorder.start(RecorderConfig())
    recorder.poll()
    recorder.poll()
    recorder.end()
    steps = json.loads(recorder.log_path.read_text())["steps"]
    assert len(steps) == 2
    assert steps[0]["changes"][0]["operation"] == "routing.add"
    assert {c["operation"] for c in steps[-1]["changes"]} == {"text.modify", "shape.add"}
