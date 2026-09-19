"""Exercise the timer handler without requiring a live wx application."""
import ast
from pathlib import Path
from types import SimpleNamespace

from kilog.recorder import Recorder, RecorderConfig
from tests.helpers import FakeAdapter, item, snapshot


def test_fill_button_records_outline_then_actual_fill(tmp_path):
    source = Path(__file__).parents[1] / "kilog" / "ui.py"
    handler = next(node for node in ast.walk(ast.parse(source.read_text(encoding="utf-8")))
                   if isinstance(node, ast.FunctionDef) and node.name == "_on_fill_board")
    for arg in handler.args.args:
        arg.annotation = None
    handler.returns = None
    namespace = {}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"), namespace)

    class FillAdapter(FakeAdapter):
        def fill_board_copper(self, net, layers):
            self.current = snapshot(item("zone", "zone", outline=[1, 2], filled=False))

        def refill_board_copper(self):
            self.current = snapshot(item("zone", "zone", outline=[1, 2], filled=True))

    recorder = Recorder(FillAdapter(tmp_path, [snapshot()]))
    recorder.start(RecorderConfig())
    frame = SimpleNamespace(
        recorder=recorder,
        front_copper_check=SimpleNamespace(GetValue=lambda: True),
        back_copper_check=SimpleNamespace(GetValue=lambda: True),
        copper_net_entry=SimpleNamespace(GetValue=lambda: "GND"),
        _refresh_record=lambda: None,
        _run_action=lambda action: action(),
    )
    namespace["_on_fill_board"](frame, None)
    steps = recorder._log_document(recorder.baseline)["steps"]
    assert len(steps) == 2
    assert steps[0]["changes"][0]["operation"] == "zone.add"
    assert steps[1]["changes"][0]["operation"] == "zone.refill"
    assert steps[1]["changes"][0]["value"] is True


def test_poll_refreshes_counter_when_undo_returns_no_new_event(tmp_path):
    source = Path(__file__).parents[1] / "kilog" / "ui.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    handler = next(node for node in ast.walk(tree)
                   if isinstance(node, ast.FunctionDef) and node.name == "_on_poll")
    # Only annotations require wx; execute the real handler body with fake UI.
    for arg in handler.args.args:
        arg.annotation = None
    handler.returns = None
    namespace = {}
    exec(compile(ast.Module(body=[handler], type_ignores=[]), str(source), "exec"), namespace)

    initial = snapshot()
    recorder = Recorder(FakeAdapter(tmp_path, [
        initial, snapshot(item("track", "track", width=100)), initial,
    ]))
    recorder.start(RecorderConfig())
    recorder.poll()
    refreshed_counts = []
    frame = SimpleNamespace(
        recorder=recorder,
        _pcb_window=SimpleNamespace(is_open=lambda: True),
        _sync_pcb_hotkeys=lambda: None,
        _position_poll_tick=0,
        POSITION_POLL_TICKS=10,
        _refresh_record=lambda: refreshed_counts.append(recorder.event_count),
        replay=SimpleNamespace(playing=False),
    )
    namespace["_on_poll"](frame, None)
    assert refreshed_counts == [0]
    assert not recorder.log_path.exists()
