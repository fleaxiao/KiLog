from __future__ import annotations

import json

import pytest

from kilog.recorder import LogFileExistsError, Recorder, RecorderConfig, RecorderError
from tests.helpers import FakeAdapter, item, snapshot


def test_poll_debounces_and_appends_event_to_log_json(tmp_path):
    initial = snapshot(item("fp-1", "footprint", position={"x": 1}))
    moved = snapshot(item("fp-1", "footprint", position={"x": 2}))
    adapter = FakeAdapter(tmp_path, [initial, moved, moved])
    recorder = Recorder(adapter)

    recorder.start(RecorderConfig(settle_seconds=0.4))
    assert recorder.poll(now=10.0) is None
    event = recorder.poll(now=10.5)

    assert event is not None
    path = tmp_path / "ref_log.json"
    assert path.exists()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert set(persisted) == {"initial_pcb_path", "changes"}
    assert persisted["initial_pcb_path"] == str(tmp_path / "demo.kicad_pcb")
    assert persisted["changes"][0]["item_uuid"] == "fp-1"
    assert "operation" in persisted["changes"][0]
    assert "op" not in persisted["changes"][0]


def test_note_flushes_change_and_saves_live_copy(tmp_path):
    initial = snapshot()
    routed = snapshot(item("track-1", "track", width=250000))
    adapter = FakeAdapter(tmp_path, [initial, routed])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(pcb_stem="ref.kicad_pcb"))

    note_path = recorder.note()

    assert (tmp_path / "ref_log.json").exists()
    assert note_path == tmp_path / "ref_01.kicad_pcb"
    assert note_path.read_text(encoding="utf-8") == "(kicad_pcb)"


def test_undo_restores_board_and_removes_event_from_log(tmp_path):
    initial = snapshot()
    with_via = snapshot(item("via-1", "via", diameter=600000))
    adapter = FakeAdapter(tmp_path, [initial, with_via, with_via])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))
    recorder.poll(now=1)
    recorder.poll(now=2)
    log_path = tmp_path / "ref_log.json"
    assert log_path.exists()

    removed, strategy = recorder.undo()

    assert removed == log_path
    assert strategy == "native"
    assert json.loads(log_path.read_text(encoding="utf-8"))["changes"] == []
    assert recorder.baseline == initial
    assert recorder.event_count == 0


def test_end_flushes_and_stops(tmp_path):
    initial = snapshot()
    changed = snapshot(item("zone-1", "zone", outline=[1]))
    adapter = FakeAdapter(tmp_path, [initial, changed])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig())

    event = recorder.end()

    assert event is not None
    assert not recorder.recording
    persisted = json.loads((tmp_path / "ref_log.json").read_text(encoding="utf-8"))
    assert len(persisted["changes"]) == 1


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

    assert removed.name == "ref_log.json"
    assert json.loads(removed.read_text(encoding="utf-8"))["changes"] == []
    assert recorder.baseline == initial


def test_start_warns_and_does_not_overwrite_existing_log(tmp_path):
    path = tmp_path / "ref_log.json"
    path.write_text('{"keep": true}\n', encoding="utf-8")
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot()]))

    with pytest.raises(LogFileExistsError, match="already exists"):
        recorder.start(RecorderConfig())

    assert path.read_text(encoding="utf-8") == '{"keep": true}\n'
    assert not recorder.recording


def test_alternate_log_name_can_be_used_after_conflict(tmp_path):
    (tmp_path / "ref_log.json").write_text("{}\n", encoding="utf-8")
    recorder = Recorder(FakeAdapter(tmp_path, [snapshot()]))

    recorder.start(RecorderConfig(pcb_stem="next"))

    assert (tmp_path / "next_log.json").exists()
    assert (tmp_path / "ref_log.json").read_text(encoding="utf-8") == "{}\n"


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

    persisted = json.loads((tmp_path / "ref_log.json").read_text(encoding="utf-8"))
    assert len(persisted["changes"]) == 1
    transform = persisted["changes"][0]
    assert set(transform) == {
        "change_uuid",
        "item_uuid",
        "operation",
        "position",
        "orientation",
    }
    assert transform["operation"] == "footprint.move"
    assert transform["position"] == {"x": 15, "y": 25}
    assert transform["orientation"] == 270
    assert recorder.event_count == 1
