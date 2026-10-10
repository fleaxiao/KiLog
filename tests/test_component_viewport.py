"""Check viewport geometry and scrolling without starting a wx application."""
import ast
from pathlib import Path
from types import SimpleNamespace


def viewport(count, width=400, height=68):
    path = Path(__file__).parents[1] / "kilog" / "ui.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ComponentViewport")
    node.bases = []
    node.body = [n for n in node.body if not isinstance(n, ast.FunctionDef) or n.name != "__init__"]
    scope = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), scope)
    view = scope["ComponentViewport"]()
    view.first_row = 0
    view._wheel_rotation = 0
    view.FromDIP = lambda value: value
    view.GetClientSize = lambda: (width, height)
    view.Refresh = lambda *args: None

    class Button:
        def GetLabel(self):
            return "R1"

        def Show(self, visible):
            self.visible = visible

        def SetSize(self, *bounds):
            self.bounds = bounds

    view.buttons = [Button() for _ in range(count)]
    return view


def test_three_rows_and_last_partial_row_remain_inside_viewport():
    view = viewport(50)
    view.layout_buttons()
    assert sum(b.visible for b in view.buttons) == 21
    assert {b.bounds[1] for b in view.buttons if b.visible} == {0, 24, 48}
    view._scroll_to(999)
    assert view.first_row == 5
    assert view.buttons[-1].visible
    assert sum(b.visible for b in view.buttons) == 15
    for button in view.buttons:
        if button.visible:
            x, y, width, height = button.bounds
            assert 0 <= x < x + width <= 384
            assert 0 <= y < y + height <= 68
    view._scroll_to(-999)
    assert view.first_row == 0


def test_wheel_accumulates_partial_steps_and_clamps():
    view = viewport(50)
    event = SimpleNamespace(GetWheelRotation=lambda: -60, GetWheelDelta=lambda: 120)
    view._on_wheel(event)
    assert view.first_row == 0
    view._on_wheel(event)
    assert view.first_row == 1
    for _ in range(2 * view._max_row() + 4):
        view._on_wheel(event)
    assert view.first_row == view._max_row()


def test_empty_or_short_lists_do_not_scroll_and_refresh_clamps_position():
    for count in (0, 1, 20, 21):
        view = viewport(count)
        view._scroll_to(99)
        assert view.first_row == 0
        assert all(button.visible for button in view.buttons)
    view = viewport(50)
    view._scroll_to(5)
    view.buttons = view.buttons[:3]
    view.layout_buttons()
    assert view.first_row == 0


def test_thumb_reaches_both_ends_of_track():
    view = viewport(50)
    _x, top, height, travel = view._geometry()
    assert top == 2
    view._scroll_to(999)
    _x, top, height, travel = view._geometry()
    assert top + height == 66


def test_different_prefixes_flow_in_same_row_without_category_gaps():
    view = viewport(6)
    for button, label in zip(view.buttons, ["C1", "R1", "C2", "R2", "C3", "C4"]):
        button.GetLabel = lambda label=label: label
    view.layout_buttons()
    assert all(b.visible for b in view.buttons)
    assert view.buttons[0].bounds[0] < view.buttons[2].bounds[0]
    assert view.buttons[0].bounds[1] == view.buttons[2].bounds[1]
    assert view.buttons[1].bounds[0] > view.buttons[0].bounds[0]
    assert view.buttons[1].bounds[1] == view.buttons[0].bounds[1]
    view._scroll_to(99)
    assert view.first_row == 0
    assert all(b.visible for b in view.buttons)
