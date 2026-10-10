import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from kipy.board_types import FootprintInstance

from kilog.kicad_adapter import KiCadBoardAdapter


class SelectionBoard:
    def __init__(self):
        self.footprints = []
        for uuid in ("a", "b", "c"):
            item = FootprintInstance()
            item.proto.id.value = uuid
            self.footprints.append(item)
        self.selected = {"a", "track"}
        self.calls = []

    def get_items(self, **kwargs):
        return self.footprints

    def get_selection(self, **kwargs):
        return [item for item in self.footprints if item.proto.id.value in self.selected]

    def add_to_selection(self, items):
        ids = {item.proto.id.value for item in items}
        self.calls.append(("add", ids))
        self.selected.update(ids)

    def remove_from_selection(self, items):
        ids = {item.proto.id.value for item in items}
        self.calls.append(("remove", ids))
        self.selected.difference_update(ids)


def test_highlight_updates_footprints_without_clearing_other_selection():
    board = SelectionBoard()
    adapter = KiCadBoardAdapter(None, board)
    assert adapter.select_components({"b", "c"}) == {"b", "c"}
    assert board.selected == {"b", "c", "track"}
    assert board.calls == [("remove", {"a"}), ("add", {"b", "c"})]
    board.calls.clear()
    adapter.select_components({"b", "c"})
    assert not board.calls
    assert adapter.select_components(set()) == set()
    assert board.selected == {"track"}


def test_missing_component_fails_before_changing_selection():
    board = SelectionBoard()
    with pytest.raises(ValueError, match="components changed"):
        KiCadBoardAdapter(None, board).select_components({"missing"})
    assert board.selected == {"a", "track"}
    assert not board.calls


def test_locked_components_are_hidden_and_not_modified_by_selection():
    board = SelectionBoard()
    board.footprints[0].locked = True
    adapter = KiCadBoardAdapter(None, board)
    assert {uuid for uuid, _reference in adapter.selection_components()} == {"b", "c"}
    assert adapter.select_components({"b"}) == {"b"}
    assert board.selected == {"a", "b", "track"}
    with pytest.raises(ValueError, match="components changed"):
        adapter.select_components({"a"})



def handlers():
    path = Path(__file__).parents[1] / "kilog" / "ui.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {"_selected_component_ids", "_on_select_check", "_sync_select_to_board", "_set_select_all",
             "_sync_select_from_board", "_sync_select_selection", "_poll_select"}
    methods = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
    namespace = {"BG": "bg", "DIM": "dim", "PANEL": "panel", "ORANGE": "orange"}
    exec(compile(ast.Module(body=methods, type_ignores=[]), str(path), "exec"), namespace)
    return {name: namespace[name] for name in names}


class Button:
    def __init__(self, selected=False):
        self.selected = selected

    def GetValue(self):
        return self.selected

    def SetValue(self, value):
        self.selected = value

    def SetBackgroundColour(self, value):
        pass

    def SetForegroundColour(self, value):
        pass

    def Refresh(self):
        pass


def frame(select):
    view = SimpleNamespace(
        select_buttons={"a": Button(), "b": Button()}, select_board_path="board",
        select_components=[("a", "R1"), ("b", "R2")],
        select_locked_ids=set(),
        _select_metadata_tick=0, SELECT_METADATA_POLL_TICKS=12,
        recorder=SimpleNamespace(adapter=SimpleNamespace(board_path="board", select_components=select,
                                                         locked_component_ids=lambda: set())),
        select_note_button=SimpleNamespace(Enable=lambda value: None),
        select_all_button=SimpleNamespace(Enable=lambda value: None, SetIcon=lambda *args: None),
        _run_action=lambda action: action(),
    )
    for name, method in handlers().items():
        setattr(view, name, method.__get__(view))
    return view


def test_single_click_and_bulk_toggle_send_selected_ids():
    requests = []
    view = frame(lambda ids: requests.append(ids) or ids)
    view.select_buttons["a"].SetValue(True)
    view._on_select_check(SimpleNamespace(GetEventObject=lambda: view.select_buttons["a"]))
    assert requests == [{"a"}]
    assert view._selected_component_ids() == {"a"}
    view._set_select_all(True)
    assert requests[-1] == {"a", "b"}
    view._set_select_all(False)
    assert requests[-1] == set()


def test_editor_selection_is_read_without_writing_back():
    board = SelectionBoard()
    board.footprints[2].locked = True
    adapter = KiCadBoardAdapter(None, board)
    assert adapter.selected_component_ids() == {"a"}
    board.selected = {"b", "c", "track"}
    assert adapter.selected_component_ids() == {"b"}
    assert board.calls == []
    assert adapter.locked_component_ids() == {"c"}


def test_locked_defaults_keep_mark_enabled_after_deselect_all():
    view = frame(lambda ids: ids)
    view.select_locked_ids = {"locked"}
    enabled = []
    view.select_note_button.Enable = enabled.append
    view._set_select_all(False)
    assert view._selected_component_ids() == set()
    assert view.select_locked_ids == {"locked"}
    assert enabled[-1] is True


def test_native_selection_and_deselection_update_buttons_without_echo():
    writes = []
    view = frame(lambda ids: writes.append(ids) or ids)
    adapter = view.recorder.adapter
    adapter.selection_components = lambda: view.select_components
    adapter.selected_component_ids = lambda: {"b", "hidden"}
    view._sync_select_from_board()
    assert view._selected_component_ids() == {"b"}
    adapter.selected_component_ids = lambda: set()
    view._sync_select_from_board()
    assert not view._selected_component_ids()
    assert writes == []


def test_poll_only_reads_on_select_page_and_keeps_state_on_ipc_failure():
    view = frame(lambda ids: ids)
    view.select_buttons["a"].SetValue(True)
    reads = []
    view.recorder.adapter.selection_components = lambda: reads.append(True) or view.select_components
    def unavailable():
        raise RuntimeError("IPC busy")
    view.recorder.adapter.selected_component_ids = unavailable
    view.mode_tabs = SimpleNamespace(GetSelection=lambda: 0)
    view._poll_select()
    assert not reads
    view.mode_tabs.GetSelection = lambda: 2
    view._poll_select()
    assert reads == []
    assert view._selected_component_ids() == {"a"}


def test_selection_polled_each_tick_but_metadata_only_every_twelve_ticks():
    view = frame(lambda ids: ids)
    view.mode_tabs = SimpleNamespace(GetSelection=lambda: 2)
    reads = {"selection": 0, "components": 0, "locked": 0}
    def selection():
        reads["selection"] += 1
        return {"b"}
    def components():
        reads["components"] += 1
        return view.select_components
    def locked():
        reads["locked"] += 1
        return set()
    adapter = view.recorder.adapter
    adapter.selected_component_ids = selection
    adapter.selection_components = components
    adapter.locked_component_ids = locked
    for _ in range(11):
        view._poll_select()
    assert view._selected_component_ids() == {"b"}
    assert reads == {"selection": 11, "components": 0, "locked": 0}
    view._poll_select()
    assert reads == {"selection": 12, "components": 1, "locked": 1}
    view._sync_select_from_board()  # Entry/save still force a fresh full read.
    assert reads == {"selection": 13, "components": 2, "locked": 2}
    assert view._select_metadata_tick == 0


def test_metadata_failure_does_not_delay_selection_or_retry_full_reads_each_tick():
    view = frame(lambda ids: ids)
    view.mode_tabs = SimpleNamespace(GetSelection=lambda: 2)
    view._select_metadata_tick = 11
    view.recorder.adapter.selected_component_ids = lambda: {"b"}
    attempts = []
    def fail():
        attempts.append(True)
        raise RuntimeError("metadata unavailable")
    view.recorder.adapter.selection_components = fail
    view._poll_select()
    assert view._selected_component_ids() == {"b"}
    view.recorder.adapter.selected_component_ids = lambda: {"a"}
    view._poll_select()
    assert view._selected_component_ids() == {"a"}
    assert attempts == [True]


def test_failed_click_restores_previous_ui_selection():
    def fail(_ids):
        raise RuntimeError("IPC unavailable")
    view = frame(fail)
    view.select_buttons["a"].SetValue(True)
    with pytest.raises(RuntimeError, match="IPC unavailable"):
        view._on_select_check(SimpleNamespace(GetEventObject=lambda: view.select_buttons["a"]))
    assert not view._selected_component_ids()


def test_ui_uses_actual_selection_and_blocks_changed_board():
    view = frame(lambda ids: {"a"})
    view._set_select_all(True)
    assert view._selected_component_ids() == {"a"}
    view.recorder.adapter.board_path = "other"
    with pytest.raises(ValueError, match="active PCB changed"):
        view._set_select_all(False)
    assert view._selected_component_ids() == {"a"}
