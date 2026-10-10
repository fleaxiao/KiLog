from kilog import window_position
from kilog.window_position import PcbEditorWindow, WindowRect, bottom_left_position


def test_places_compact_panel_at_pcb_canvas_bottom_left():
    bounds = WindowRect(35, 112, 1482, 1141)

    assert bottom_left_position(bounds, (275, 200), (4, 4)) == (39, 937)


def test_keeps_oversized_window_inside_left_edge():
    bounds = WindowRect(100, 50, 400, 250)

    assert bottom_left_position(bounds, (500, 100), (12, 16)) == (100, 134)


def test_keeps_window_inside_bottom_edge():
    bounds = WindowRect(100, 50, 800, 200)

    assert bottom_left_position(bounds, (300, 140), (12, 30)) == (112, 50)


def test_pcb_window_reports_closed_without_adopting_another_window(monkeypatch):
    editor = PcbEditorWindow()
    editor._hwnd = 123
    monkeypatch.setattr(window_position, "_window_handle_exists", lambda _hwnd: False)
    monkeypatch.setattr(editor, "_find_window", lambda: 456)

    assert not editor.is_open()
    assert editor._hwnd == 123


def test_pcb_window_finds_initial_window_once(monkeypatch):
    editor = PcbEditorWindow()
    monkeypatch.setattr(editor, "_find_window", lambda: 123)

    assert editor.is_open()
    assert editor._hwnd == 123
