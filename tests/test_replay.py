from __future__ import annotations

import json

import pytest

from kilog.replay import ReplayController, ReplayError, load_replay_log
from tests.helpers import item, snapshot


class ReplayAdapter:
    def __init__(self):
        self.initial = snapshot(item("fp-1", "footprint", position={"x": 0}))
        self.current = self.initial
        self.applied: list[str] = []
        self.apply_calls = 0
        self.restores = 0
        self.restore_descriptions: list[str] = []
        self.prepared_path = None

    def prepare_replay(self, initial_pcb_path):
        self.prepared_path = initial_pcb_path
        self.current = self.initial
        return self.initial

    def snapshot(self):
        return self.current

    def restore_snapshot(self, target, description=""):
        self.current = target
        state = target.items["fp-1"]
        position = int(state.data["position"].get("x_nm", state.data["position"].get("x", 0)))
        self.applied = [f"change-{index}" for index in range(1, position + 1)]
        self.restores += 1
        self.restore_descriptions.append(description)
        return target

    def apply_change(self, change):
        self.applied.append(change["change_uuid"])
        self.apply_calls += 1
        self.current = snapshot(
            item(
                "fp-1",
                "footprint",
                position=change["position"],
                orientation=change["orientation"],
            )
        )
        return self.current


def write_log(path, count=3):
    path.write_text(
        json.dumps(
            {
                "initial_pcb_path": "C:/project/demo.kicad_pcb",
                "changes": [
                    {
                        "change_uuid": f"change-{index}",
                        "item_uuid": "fp-1",
                        "operation": "footprint.move",
                        "position": {"x_nm": str(index)},
                        "orientation": {"value_degrees": 0},
                    }
                    for index in range(1, count + 1)
                ],
            }
        ),
        encoding="utf-8",
    )


def test_load_validates_log_shape(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text('{"changes": []}', encoding="utf-8")

    with pytest.raises(ReplayError, match="initial_pcb_path"):
        load_replay_log(path)


def test_default_playback_interval_is_point_four_seconds():
    replay = ReplayController(ReplayAdapter())

    assert replay.step_seconds == 0.4
    assert replay.speed == 1.0


def test_step_play_pause_and_completion(tmp_path):
    path = tmp_path / "log.json"
    write_log(path, 2)
    adapter = ReplayAdapter()
    replay = ReplayController(adapter, step_seconds=1)
    replay.load(path)

    assert adapter.prepared_path == "C:/project/demo.kicad_pcb"

    replay.play(now=10)
    assert replay.tick(now=10)
    assert replay.position == 1
    assert replay.playing
    assert not replay.tick(now=10.5)
    assert replay.tick(now=11)
    assert replay.position == 2
    assert not replay.playing


def test_backward_seek_restores_cached_target_without_replaying_from_baseline(tmp_path):
    path = tmp_path / "log.json"
    write_log(path, 4)
    adapter = ReplayAdapter()
    replay = ReplayController(adapter)
    replay.load(path)
    replay.step_forward()
    replay.step_forward()
    assert adapter.apply_calls == 2

    assert replay.step_back()

    assert adapter.restores == 1
    assert adapter.applied == ["change-1"]
    assert adapter.apply_calls == 2
    assert adapter.restore_descriptions == ["KiLog: replay back to step 1"]
    assert replay.position == 1


def test_forward_seek_continues_from_current_position_after_going_back(tmp_path):
    path = tmp_path / "log.json"
    write_log(path, 4)
    adapter = ReplayAdapter()
    replay = ReplayController(adapter)
    replay.load(path)
    replay.seek(3)
    assert adapter.apply_calls == 3

    replay.seek(1)
    replay.seek(3)

    assert adapter.restores == 1
    assert adapter.apply_calls == 5
    assert adapter.applied == ["change-1", "change-2", "change-3"]
    assert replay.position == 3


def test_skip_is_clamped_to_log_bounds(tmp_path):
    path = tmp_path / "log.json"
    write_log(path, 3)
    adapter = ReplayAdapter()
    replay = ReplayController(adapter)
    replay.load(path)

    replay.skip(10)
    assert replay.position == 3
    assert adapter.apply_calls == 3
    replay.skip(-10)
    assert replay.position == 0
    assert adapter.apply_calls == 3
    assert adapter.restores == 1
    assert adapter.restore_descriptions == ["KiLog: replay back to step 0"]


def test_load_rejects_board_that_is_already_at_every_logged_target(tmp_path):
    path = tmp_path / "log.json"
    write_log(path, 1)
    adapter = ReplayAdapter()
    adapter.initial = snapshot(
        item(
            "fp-1",
            "footprint",
            position={"x_nm": "1"},
            orientation={"value_degrees": 0},
        )
    )

    with pytest.raises(ReplayError, match="still looks fully replayed"):
        ReplayController(adapter).load(path)
