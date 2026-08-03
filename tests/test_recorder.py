from __future__ import annotations

import json

import pytest

from kilog.recorder import Recorder, RecorderConfig, RecorderError
from tests.helpers import FakeAdapter, item, snapshot


def test_poll_debounces_and_writes_numbered_json(tmp_path):
    initial = snapshot(item("fp-1", "footprint", position={"x": 1}))
    moved = snapshot(item("fp-1", "footprint", position={"x": 2}))
    adapter = FakeAdapter(tmp_path, [initial, moved, moved])
    recorder = Recorder(adapter)

    recorder.start(RecorderConfig(settle_seconds=0.4))
    assert recorder.poll(now=10.0) is None
    event = recorder.poll(now=10.5)

    assert event is not None
    path = tmp_path / "log_000001.json"
    assert path.exists()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["event_uuid"] == event["event_uuid"]
    assert persisted["changes"][0]["item_uuid"] == "fp-1"


def test_note_flushes_change_and_saves_live_copy(tmp_path):
    initial = snapshot()
    routed = snapshot(item("track-1", "track", width=250000))
    adapter = FakeAdapter(tmp_path, [initial, routed])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(pcb_stem="ref.kicad_pcb", log_stem="log.json"))

    note_path = recorder.note()

    assert (tmp_path / "log_000001.json").exists()
    assert note_path == tmp_path / "ref_000001.kicad_pcb"
    assert note_path.read_text(encoding="utf-8") == "(kicad_pcb)"


def test_undo_restores_board_and_removes_matching_log(tmp_path):
    initial = snapshot()
    with_via = snapshot(item("via-1", "via", diameter=600000))
    adapter = FakeAdapter(tmp_path, [initial, with_via, with_via])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=0))
    recorder.poll(now=1)
    recorder.poll(now=2)
    log_path = tmp_path / "log_000001.json"
    assert log_path.exists()

    removed, strategy = recorder.undo()

    assert removed == log_path
    assert strategy == "native"
    assert not log_path.exists()
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
    assert (tmp_path / "log_000001.json").exists()


def test_undo_without_history_is_rejected(tmp_path):
    initial = snapshot()
    recorder = Recorder(FakeAdapter(tmp_path, [initial]))
    recorder.start(RecorderConfig())
    with pytest.raises(RecorderError, match="没有可撤销"):
        recorder.undo()


def test_undo_flushes_a_change_that_is_still_inside_debounce_window(tmp_path):
    initial = snapshot()
    moved = snapshot(item("fp-1", "footprint", position={"x": 2}))
    adapter = FakeAdapter(tmp_path, [initial, moved])
    recorder = Recorder(adapter)
    recorder.start(RecorderConfig(settle_seconds=60))

    removed, _ = recorder.undo()

    assert removed.name == "log_000001.json"
    assert not removed.exists()
    assert recorder.baseline == initial
