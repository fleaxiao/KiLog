from __future__ import annotations

from pathlib import Path

import wx

from .path_display import default_log_name
from .recorder import LogFileExistsError, Recorder, RecorderConfig
from .replay import ReplayController
from .window_position import PcbEditorWindow, WindowRect, bottom_left_position


BG = "#14231E"
PANEL = "#20352D"
FIELD = "#0E1C17"
ORANGE = "#F2A33A"
CREAM = "#F3F0E8"
MUTED = "#A9BCB4"
DIM = "#72877E"
SLIDER_TRACK = "#28513F"
TAB_FONT_SIZE = 8
UI_FONT_SIZE = 8
META_FONT_SIZE = 8
POSITION_COUNTER_PLACEHOLDER = "999/999"


class UnderlinedTextField(wx.Panel):
    """A borderless single-line text field with only a bottom rule."""

    def __init__(
        self,
        parent: wx.Window,
        value: str,
        size: tuple[int, int],
        centered: bool = False,
    ):
        super().__init__(parent, size=size)
        self.SetBackgroundColour(BG)
        self.editor = wx.TextCtrl(
            self,
            value=value,
            style=wx.BORDER_NONE | (wx.TE_CENTER if centered else wx.TE_LEFT),
        )
        self.editor.SetMargins(0, 0)
        self.Bind(wx.EVT_SIZE, self._on_size)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self._layout_editor()

    def _on_size(self, event: wx.SizeEvent) -> None:
        self._layout_editor()
        event.Skip()

    def _layout_editor(self) -> None:
        width, height = self.GetClientSize()
        available_height = max(1, height - 1)
        preferred_height = self.editor.GetBestSize().GetHeight() + 4
        editor_height = max(1, min(preferred_height, available_height - 3))
        editor_y = max(0, (available_height - editor_height) // 2)
        self.editor.SetSize(0, editor_y, width, editor_height)

    def FitToFont(self) -> None:
        width = self.GetSize().GetWidth()
        required_height = self.editor.GetBestSize().GetHeight() + 10
        self.SetMinSize(wx.Size(width, required_height))
        self.SetSize(wx.Size(width, required_height))
        self._layout_editor()

    def _on_paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.PaintDC(self)
        width, height = self.GetClientSize()
        dc.SetPen(wx.Pen(DIM, 1))
        dc.DrawLine(0, height - 1, width, height - 1)

    def GetValue(self) -> str:
        return self.editor.GetValue()

    def SetEditable(self, editable: bool) -> None:
        self.editor.SetEditable(editable)
        self.editor.SetBackgroundColour(BG)
        self.editor.Refresh()

    def ClearSelection(self) -> None:
        self.editor.SetInsertionPoint(0)
        self.editor.SetSelection(0, 0)


class UnderlinedPathDisplay(wx.Panel):
    """Read-only ellipsized path with the same full-width rule as Log fields."""

    def __init__(
        self,
        parent: wx.Window,
        value: str,
        size: tuple[int, int],
        font: wx.Font,
        button_handler=None,
    ):
        super().__init__(parent, size=size, style=wx.BORDER_NONE)
        self.SetMinSize(size)
        self.SetBackgroundColour(BG)
        self.text = wx.StaticText(self, label=value, style=wx.ST_ELLIPSIZE_START)
        self.text.SetForegroundColour(DIM)
        self.text.SetFont(font)
        layout = wx.BoxSizer(wx.HORIZONTAL)
        layout.Add(self.text, 1, wx.ALIGN_CENTER_VERTICAL | wx.BOTTOM, 2)
        self.button = None
        if button_handler is not None:
            self.button = wx.Button(
                self,
                label="Load",
                size=(80, 30),
                style=wx.BORDER_NONE,
            )
            self.button.SetMinSize(wx.Size(80, 30))
            self.button.SetBackgroundColour(PANEL)
            self.button.SetForegroundColour(CREAM)
            self.button.SetFont(font)
            self.button.Bind(wx.EVT_BUTTON, button_handler)
            layout.Add(self.button, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.BOTTOM, 3)
        self.SetSizer(layout)
        self.Bind(wx.EVT_PAINT, self._on_paint)

    def _on_paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.PaintDC(self)
        width, height = self.GetClientSize()
        dc.SetPen(wx.Pen(DIM, 1))
        dc.DrawLine(0, height - 1, width, height - 1)

    def SetLabel(self, value: str) -> None:
        self.text.SetLabel(value)
        self.Layout()

    def SetToolTip(self, value: str) -> None:
        super().SetToolTip(value)
        self.text.SetToolTip(value)


class FlatTab(wx.Panel):
    """Theme-independent tab that keeps its colour when hovered."""

    def __init__(self, parent, label: str, size: wx.Size, font: wx.Font, handler):
        super().__init__(parent, size=size, style=wx.BORDER_NONE)
        self.SetMinSize(size)
        self.label = wx.StaticText(self, label=label)
        self.label.SetFont(font)
        layout = wx.BoxSizer(wx.HORIZONTAL)
        layout.AddStretchSpacer()
        layout.Add(self.label, 0, wx.ALIGN_CENTER_VERTICAL)
        layout.AddStretchSpacer()
        self.SetSizer(layout)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self.Bind(wx.EVT_LEFT_UP, handler)
        self.label.Bind(wx.EVT_LEFT_UP, handler)

    def set_selected(self, selected: bool) -> None:
        self.SetBackgroundColour(BG if selected else FIELD)
        self.label.SetForegroundColour(CREAM if selected else DIM)
        self.Refresh()
        self.label.Refresh()


class SpeedSelector(wx.Button):
    """Compact speed selector backed by the native popup menu."""

    def __init__(
        self,
        parent: wx.Window,
        choices: list[str],
        selection: int,
        size: wx.Size,
        font: wx.Font,
        handler,
    ):
        super().__init__(parent, size=size, style=wx.BORDER_NONE)
        self._choices = choices
        self._selection = selection
        self._handler = handler
        self.popup_open = False
        self.SetMinSize(size)
        self.SetBackgroundColour(PANEL)
        self.SetForegroundColour(CREAM)
        self.SetFont(font)
        self._update_label()
        self.Bind(wx.EVT_BUTTON, self._show_menu)

    def _update_label(self) -> None:
        self.SetLabel(f"{self.GetStringSelection()} ▾")

    def _show_menu(self, _event: wx.CommandEvent) -> None:
        menu = wx.Menu()
        for index, choice in enumerate(self._choices):
            # Plain native items omit the radio/check gutter, keeping the
            # platform menu as narrow as its labels allow.
            item = menu.Append(wx.ID_ANY, choice)
            menu.Bind(
                wx.EVT_MENU,
                lambda _menu_event, selected=index: self._select(selected),
                item,
            )

        self.popup_open = True
        try:
            self.PopupMenu(menu, wx.Point(0, self.GetClientSize().GetHeight()))
        finally:
            self.popup_open = False
            menu.Destroy()

    def _select(self, selection: int) -> None:
        self._selection = selection
        self._update_label()
        self._handler()

    def GetSelection(self) -> int:
        return self._selection

    def GetStringSelection(self) -> str:
        return self._choices[self._selection]


class ReplaySlider(wx.Panel):
    """Smooth replay slider that commits an expensive seek after dragging."""

    def __init__(self, parent: wx.Window, handler, live: bool = False):
        super().__init__(parent, style=wx.BORDER_NONE)
        self.SetMinSize(wx.Size(-1, self.FromDIP(20)))
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._handler = handler
        self._live = live
        self._minimum = 0
        self._maximum = 1
        self._value = 0
        self._visual_value = 0.0
        self._dragging = False
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_MOTION, self._on_motion)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

    def SetRange(self, minimum: int, maximum: int) -> None:
        self._minimum = int(minimum)
        self._maximum = max(self._minimum + 1, int(maximum))
        self.SetValue(self._value)

    def SetValue(self, value: int) -> None:
        self._value = max(self._minimum, min(int(value), self._maximum))
        if not self._dragging:
            self._visual_value = float(self._value)
            self.Refresh(False)

    def GetValue(self) -> int:
        return self._value

    def Enable(self, enable: bool = True) -> bool:
        changed = super().Enable(enable)
        self.Refresh(False)
        return changed

    def _track_geometry(self) -> tuple[float, float, float]:
        width, height = self.GetClientSize()
        radius = self._thumb_radius()
        edge_padding = radius + self.FromDIP(1)
        return edge_padding, max(edge_padding, width - edge_padding), height / 2.0

    def _thumb_radius(self) -> float:
        height = self.GetClientSize().GetHeight()
        return max(2.0, min(float(self.FromDIP(5)), (height - 2.0) / 2.0))

    def _value_from_x(self, x: int) -> float:
        left, right, _center_y = self._track_geometry()
        if right <= left:
            return float(self._minimum)
        fraction = max(0.0, min(1.0, (x - left) / (right - left)))
        return self._minimum + fraction * (self._maximum - self._minimum)

    def _thumb_x(self) -> float:
        left, right, _center_y = self._track_geometry()
        span = self._maximum - self._minimum
        fraction = 0.0 if span <= 0 else (self._visual_value - self._minimum) / span
        return left + max(0.0, min(1.0, fraction)) * (right - left)

    def _on_paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        dc.SetBackground(wx.Brush(BG))
        dc.Clear()
        graphics = wx.GraphicsContext.Create(dc)
        if graphics is None:
            return
        left, right, center_y = self._track_geometry()
        track_colour = wx.Colour(SLIDER_TRACK if self.IsEnabled() else FIELD)
        thumb_colour = wx.Colour(ORANGE if self.IsEnabled() else DIM)
        graphics.SetPen(wx.Pen(track_colour, self.FromDIP(4)))
        graphics.StrokeLine(left, center_y, right, center_y)
        radius = self._thumb_radius()
        graphics.SetPen(wx.TRANSPARENT_PEN)
        graphics.SetBrush(wx.Brush(thumb_colour))
        graphics.DrawEllipse(
            self._thumb_x() - radius,
            center_y - radius,
            radius * 2,
            radius * 2,
        )

    def _on_left_down(self, event: wx.MouseEvent) -> None:
        if not self.IsEnabled():
            return
        self.SetFocus()
        self._dragging = True
        self._visual_value = self._value_from_x(event.GetX())
        self._commit_live_value()
        if not self.HasCapture():
            self.CaptureMouse()
        self.Refresh(False)

    def _on_motion(self, event: wx.MouseEvent) -> None:
        if not self._dragging or not event.LeftIsDown():
            return
        self._visual_value = self._value_from_x(event.GetX())
        self._commit_live_value()
        self.Refresh(False)

    def _on_left_up(self, event: wx.MouseEvent) -> None:
        if not self._dragging:
            return
        self._visual_value = self._value_from_x(event.GetX())
        self._commit_drag()

    def _on_capture_lost(self, _event: wx.MouseCaptureLostEvent) -> None:
        if self._dragging:
            self._commit_drag(release_capture=False)

    def _commit_drag(self, release_capture: bool = True) -> None:
        self._dragging = False
        if release_capture and self.HasCapture():
            self.ReleaseMouse()
        previous = self._value
        self._value = int(round(self._visual_value))
        self._visual_value = float(self._value)
        self.Refresh(False)
        if not self._live or self._value != previous:
            self._handler()

    def _commit_live_value(self) -> None:
        if not self._live:
            return
        value = int(round(self._visual_value))
        if value == self._value:
            return
        self._value = value
        self._handler()

    def _on_key_down(self, event: wx.KeyEvent) -> None:
        if not self.IsEnabled() or event.GetKeyCode() not in (wx.WXK_LEFT, wx.WXK_RIGHT):
            event.Skip()
            return
        direction = -1 if event.GetKeyCode() == wx.WXK_LEFT else 1
        self.SetValue(self._value + direction)
        self._handler()


class VectorIconButton(wx.Control):
    """DPI-safe vector button shared by record and replay actions."""

    def __init__(
        self,
        parent: wx.Window,
        icon: str,
        handler,
        tooltip: str,
        primary: bool = False,
    ):
        size = parent.FromDIP((52, 25))
        super().__init__(parent, size=size, style=wx.BORDER_NONE | wx.WANTS_CHARS)
        self.SetMinSize(size)
        self.SetBackgroundStyle(wx.BG_STYLE_PAINT)
        self.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        self._icon = icon
        self._handler = handler
        self._primary = primary
        self._pressed = False
        self.SetToolTip(tooltip)
        self.Bind(wx.EVT_PAINT, self._on_paint)
        self.Bind(wx.EVT_LEFT_DOWN, self._on_left_down)
        self.Bind(wx.EVT_LEFT_UP, self._on_left_up)
        self.Bind(wx.EVT_MOUSE_CAPTURE_LOST, self._on_capture_lost)
        self.Bind(wx.EVT_KEY_DOWN, self._on_key_down)

    def SetIcon(self, icon: str, tooltip: str | None = None) -> None:
        if icon == self._icon:
            if tooltip is not None:
                self.SetToolTip(tooltip)
            return
        self._icon = icon
        if tooltip is not None:
            self.SetToolTip(tooltip)
        elif icon in {"play", "pause"}:
            self.SetToolTip("Pause replay" if icon == "pause" else "Play replay")
        self.Refresh(False)

    def Enable(self, enable: bool = True) -> bool:
        changed = super().Enable(enable)
        self.Refresh(False)
        return changed

    def _on_paint(self, _event: wx.PaintEvent) -> None:
        dc = wx.AutoBufferedPaintDC(self)
        width, height = self.GetClientSize()
        background = ORANGE if self._primary else PANEL
        dc.SetBackground(wx.Brush(background))
        dc.Clear()
        graphics = wx.GraphicsContext.Create(dc)
        if graphics is None:
            return
        foreground = BG if self._primary else CREAM
        if not self.IsEnabled():
            foreground = DIM
        graphics.SetPen(wx.Pen(foreground, self.FromDIP(2)))
        graphics.SetBrush(wx.Brush(foreground))
        offset = self.FromDIP(1) if self._pressed else 0
        self._draw_icon(graphics, width / 2.0, height / 2.0 + offset)

    def _draw_icon(self, graphics: wx.GraphicsContext, center_x: float, center_y: float) -> None:
        icon = self._icon
        unit = float(self.FromDIP(4))
        bar_width = max(1.0, float(self.FromDIP(2)))
        icon_height = float(self.FromDIP(11))
        if icon == "play":
            self._draw_triangle(graphics, center_x, center_y, unit + 1, 1)
        elif icon == "pause":
            gap = float(self.FromDIP(2))
            graphics.DrawRectangle(
                center_x - gap - bar_width,
                center_y - icon_height / 2,
                bar_width,
                icon_height,
            )
            graphics.DrawRectangle(
                center_x + gap,
                center_y - icon_height / 2,
                bar_width,
                icon_height,
            )
        elif icon == "rewind":
            self._draw_triangle(graphics, center_x - unit / 1.5, center_y, unit, -1)
            self._draw_triangle(graphics, center_x + unit / 1.5, center_y, unit, -1)
        elif icon == "fast_forward":
            self._draw_triangle(graphics, center_x - unit / 1.5, center_y, unit, 1)
            self._draw_triangle(graphics, center_x + unit / 1.5, center_y, unit, 1)
        elif icon == "previous":
            graphics.DrawRectangle(
                center_x - unit - bar_width,
                center_y - icon_height / 2,
                bar_width,
                icon_height,
            )
            self._draw_triangle(graphics, center_x + bar_width / 2, center_y, unit, -1)
        elif icon == "next":
            self._draw_triangle(graphics, center_x - bar_width / 2, center_y, unit, 1)
            graphics.DrawRectangle(
                center_x + unit,
                center_y - icon_height / 2,
                bar_width,
                icon_height,
            )
        elif icon == "record":
            radius = float(self.FromDIP(5))
            graphics.DrawEllipse(
                center_x - radius,
                center_y - radius,
                radius * 2,
                radius * 2,
            )
        elif icon == "note":
            half_width = float(self.FromDIP(5))
            half_height = float(self.FromDIP(7))
            notch = float(self.FromDIP(3))
            path = graphics.CreatePath()
            path.MoveToPoint(center_x - half_width, center_y - half_height)
            path.AddLineToPoint(center_x + half_width, center_y - half_height)
            path.AddLineToPoint(center_x + half_width, center_y + half_height)
            path.AddLineToPoint(center_x, center_y + half_height - notch)
            path.AddLineToPoint(center_x - half_width, center_y + half_height)
            path.CloseSubpath()
            graphics.FillPath(path)
        elif icon == "undo":
            arrow_x = center_x - float(self.FromDIP(5))
            self._draw_triangle(
                graphics,
                arrow_x,
                center_y - float(self.FromDIP(1)),
                float(self.FromDIP(3)),
                -1,
            )
            path = graphics.CreatePath()
            path.MoveToPoint(arrow_x, center_y - float(self.FromDIP(1)))
            path.AddCurveToPoint(
                center_x + float(self.FromDIP(7)),
                center_y - float(self.FromDIP(7)),
                center_x + float(self.FromDIP(8)),
                center_y + float(self.FromDIP(5)),
                center_x + float(self.FromDIP(3)),
                center_y + float(self.FromDIP(6)),
            )
            graphics.StrokePath(path)
        elif icon == "stop":
            side = float(self.FromDIP(10))
            graphics.DrawRectangle(
                center_x - side / 2,
                center_y - side / 2,
                side,
                side,
            )

    @staticmethod
    def _draw_triangle(
        graphics: wx.GraphicsContext,
        center_x: float,
        center_y: float,
        half_width: float,
        direction: int,
    ) -> None:
        half_height = half_width * 1.35
        path = graphics.CreatePath()
        path.MoveToPoint(center_x + direction * half_width, center_y)
        path.AddLineToPoint(center_x - direction * half_width, center_y - half_height)
        path.AddLineToPoint(center_x - direction * half_width, center_y + half_height)
        path.CloseSubpath()
        graphics.FillPath(path)

    def _on_left_down(self, _event: wx.MouseEvent) -> None:
        if not self.IsEnabled():
            return
        self.SetFocus()
        self._pressed = True
        if not self.HasCapture():
            self.CaptureMouse()
        self.Refresh(False)

    def _on_left_up(self, event: wx.MouseEvent) -> None:
        if not self._pressed:
            return
        self._pressed = False
        if self.HasCapture():
            self.ReleaseMouse()
        self.Refresh(False)
        if self.GetClientRect().Contains(event.GetPosition()):
            self._handler(event)

    def _on_capture_lost(self, _event: wx.MouseCaptureLostEvent) -> None:
        self._pressed = False
        self.Refresh(False)

    def _on_key_down(self, event: wx.KeyEvent) -> None:
        if self.IsEnabled() and event.GetKeyCode() in (wx.WXK_SPACE, wx.WXK_RETURN):
            self._handler(event)
            return
        event.Skip()


class KiLogWindow(wx.Frame):
    """Native wxPython control panel using the GUI runtime bundled with KiCad."""

    POLL_MS = 160
    POSITION_POLL_TICKS = 4

    def __init__(self, recorder: Recorder, asset_directory: Path):
        super().__init__(
            parent=None,
            title="KiLog",
            style=(wx.DEFAULT_FRAME_STYLE | wx.STAY_ON_TOP)
            & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )
        self.recorder = recorder
        self.replay = ReplayController(recorder.adapter)
        self.asset_directory = asset_directory
        self._pcb_window = PcbEditorWindow()
        self._position_poll_tick = 0
        self._pcb_hotkeys_registered = False
        self.SetBackgroundColour(BG)
        self.SetIcon(self._load_icon())
        self.SetDoubleBuffered(True)

        self._build()
        self._configure_shortcuts()
        self._set_controls(False)
        self.pcb_entry.ClearSelection()
        wx.CallAfter(self.start_button.SetFocus)
        self.Fit()
        self.SetMinSize(self.GetSize())
        self._move_to_pcb_bottom_left()

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_poll, self.timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.timer.Start(self.POLL_MS)

    @staticmethod
    def _font(size: int, bold: bool = False, mono: bool = False) -> wx.Font:
        weight = wx.FONTWEIGHT_SEMIBOLD if bold else wx.FONTWEIGHT_NORMAL
        system_font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        return wx.Font(
            size,
            wx.FONTFAMILY_DEFAULT,
            wx.FONTSTYLE_NORMAL,
            weight,
            False,
            system_font.GetFaceName(),
        )

    def _load_icon(self) -> wx.Icon:
        path = self.asset_directory / "icon-ui-64.png"
        if path.exists():
            return wx.Icon(str(path), wx.BITMAP_TYPE_PNG)
        return wx.NullIcon

    def _build(self) -> None:
        root = wx.Panel(self)
        root.SetBackgroundColour(BG)
        outer = wx.BoxSizer(wx.VERTICAL)

        tab_bar = wx.BoxSizer(wx.HORIZONTAL)
        self.record_tab_button = self._mode_tab(root, "Record", 0)
        self.replay_tab_button = self._mode_tab(root, "Replay", 1)
        self.skill_tab_button = self._mode_tab(root, "Skill", 2)
        tab_bar.Add(self.record_tab_button, 0, wx.RIGHT, 3)
        tab_bar.Add(self.replay_tab_button, 0, wx.RIGHT, 3)
        tab_bar.Add(self.skill_tab_button, 0)
        outer.Add(tab_bar, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 6)

        self.mode_tabs = wx.Simplebook(root)
        self.mode_tabs.SetBackgroundColour(BG)

        record_page = wx.Panel(self.mode_tabs)
        record_page.SetBackgroundColour(BG)
        record_sizer = wx.BoxSizer(wx.VERTICAL)

        output_path = Path(self.recorder.adapter.output_directory)
        output_directory = str(output_path)
        details_row = wx.BoxSizer(wx.HORIZONTAL)
        path_label = wx.StaticText(record_page, label="Path:")
        path_label.SetForegroundColour(MUTED)
        path_label.SetFont(self._font(UI_FONT_SIZE))
        output = UnderlinedPathDisplay(
            record_page,
            output_directory,
            size=(130, 22),
            font=self._font(META_FONT_SIZE, mono=True),
        )
        output.SetToolTip(f"Output directory: {output_directory}")
        log_label = wx.StaticText(
            record_page,
            label="Log:",
            style=wx.ALIGN_LEFT,
        )
        log_label.SetForegroundColour(MUTED)
        log_label.SetFont(self._font(UI_FONT_SIZE))
        board_path = getattr(self.recorder.adapter, "board_path", None)
        default_name = default_log_name(board_path)
        self.pcb_entry = self._text_field(
            record_page,
            default_name,
            size=(80, 22),
            centered=True,
        )
        path_size = output.GetMinSize()
        log_height = self.pcb_entry.GetMinSize().GetHeight()
        output.SetMinSize(wx.Size(path_size.GetWidth(), log_height))
        output.SetSize(wx.Size(path_size.GetWidth(), log_height))
        text_width, _ = self.pcb_entry.editor.GetTextExtent(default_name)
        if text_width > self.pcb_entry.editor.GetClientSize().GetWidth() - 4:
            self.pcb_entry.editor.SetToolTip(default_name)
        details_row.Add(path_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        details_row.Add(output, 1, wx.ALIGN_CENTER_VERTICAL)
        details_row.AddSpacer(14)
        details_row.Add(log_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        details_row.Add(self.pcb_entry, 0, wx.ALIGN_CENTER_VERTICAL)
        record_sizer.Add(
            details_row,
            0,
            wx.EXPAND | wx.ALL,
            8,
        )
        record_sizer.AddStretchSpacer()

        record_progress_row = wx.BoxSizer(wx.HORIZONTAL)
        self.record_slider = ReplaySlider(record_page, self._on_record_seek, live=True)
        self.counter_text = wx.StaticText(
            record_page,
            label="0/0",
            style=wx.ALIGN_RIGHT,
        )
        self.counter_text.SetForegroundColour(ORANGE)
        self.counter_text.SetFont(self._font(META_FONT_SIZE, mono=True))
        counter_width, _ = self.counter_text.GetTextExtent(POSITION_COUNTER_PLACEHOLDER)
        counter_height = self.counter_text.GetBestSize().GetHeight()
        self.counter_text.SetMinSize(wx.Size(counter_width, counter_height))
        self.counter_text.SetToolTip("Record position 0 of 0")
        record_progress_row.Add(self.record_slider, 1, wx.ALIGN_CENTER_VERTICAL)
        record_progress_row.Add(
            self.counter_text,
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        record_sizer.Add(
            record_progress_row,
            0,
            wx.EXPAND | wx.LEFT | wx.RIGHT,
            8,
        )

        buttons = wx.FlexGridSizer(rows=1, cols=5, vgap=0, hgap=4)
        for column in range(5):
            buttons.AddGrowableCol(column, 1)
        self.record_reset_button = self._icon_button(
            record_page,
            "undo",
            self._on_record_reset,
            "Reset recording to previewed position",
        )
        self.record_back_button = self._icon_button(
            record_page,
            "previous",
            self._on_record_back,
            "Previous recorded position",
        )
        self.start_button = self._icon_button(
            record_page,
            "record",
            self._on_record_toggle,
            "Start recording",
            primary=True,
        )
        self.record_forward_button = self._icon_button(
            record_page,
            "next",
            self._on_record_forward,
            "Next recorded position",
        )
        self.note_button = self._icon_button(
            record_page,
            "note",
            self._on_note,
            "Mark current recorded step",
        )
        record_sizer.AddStretchSpacer()
        for button in (
            self.record_reset_button,
            self.record_back_button,
            self.start_button,
            self.record_forward_button,
            self.note_button,
        ):
            buttons.Add(button, 0, wx.EXPAND)
        record_sizer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)
        record_sizer.AddSpacer(8)
        record_page.SetSizer(record_sizer)

        replay_page = wx.Panel(self.mode_tabs)
        replay_page.SetBackgroundColour(BG)
        replay_sizer = wx.BoxSizer(wx.VERTICAL)

        replay_header = wx.BoxSizer(wx.HORIZONTAL)
        replay_path_label = wx.StaticText(replay_page, label="Path:")
        replay_path_label.SetForegroundColour(MUTED)
        replay_path_label.SetFont(self._font(UI_FONT_SIZE))
        self.replay_file_text = UnderlinedPathDisplay(
            replay_page,
            "No log selected",
            size=(300, log_height),
            font=self._font(META_FONT_SIZE, mono=True),
            button_handler=self._on_load_replay,
        )
        assert self.replay_file_text.button is not None
        self.load_button = self.replay_file_text.button
        speed_label = wx.StaticText(
            replay_page,
            label="Speed:",
            style=wx.ALIGN_LEFT,
        )
        speed_label.SetForegroundColour(MUTED)
        speed_label.SetFont(self._font(UI_FONT_SIZE))
        # Keep the two setting groups aligned at both edges while allowing the
        # shorter "Log:" label to sit directly beside its text field.
        speed_label_width = speed_label.GetBestSize().GetWidth()
        log_label_width = log_label.GetBestSize().GetWidth()
        ref_width = 80 + max(0, speed_label_width - log_label_width)
        self.pcb_entry.SetMinSize(wx.Size(ref_width, log_height))
        self.pcb_entry.SetSize(wx.Size(ref_width, log_height))
        self.speed_choice = SpeedSelector(
            replay_page,
            ["0.25×", "0.5×", "1×", "2×", "4×"],
            2,
            wx.Size(80, 30),
            self._font(UI_FONT_SIZE),
            self._on_speed,
        )
        shared_header_height = max(
            details_row.GetMinSize().GetHeight(),
            replay_header.GetMinSize().GetHeight(),
            self.replay_file_text.GetBestSize().GetHeight(),
            self.speed_choice.GetBestSize().GetHeight(),
        )
        details_row.SetMinSize(0, shared_header_height)
        replay_header.SetMinSize(0, shared_header_height)
        replay_header.Add(replay_path_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        replay_header.Add(self.replay_file_text, 0, wx.ALIGN_CENTER_VERTICAL)
        replay_header.AddSpacer(16)
        replay_header.AddStretchSpacer()
        replay_header.Add(speed_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        replay_header.Add(self.speed_choice, 0, wx.ALIGN_CENTER_VERTICAL)
        replay_sizer.Add(
            replay_header,
            0,
            wx.EXPAND | wx.ALL,
            8,
        )
        replay_sizer.AddStretchSpacer()

        replay_progress_row = wx.BoxSizer(wx.HORIZONTAL)
        self.replay_slider = ReplaySlider(replay_page, self._on_replay_seek)
        self.replay_position_text = wx.StaticText(
            replay_page,
            label="0/0",
            style=wx.ALIGN_RIGHT,
        )
        self.replay_position_text.SetForegroundColour(ORANGE)
        self.replay_position_text.SetFont(self._font(META_FONT_SIZE, mono=True))
        counter_width, _ = self.replay_position_text.GetTextExtent(
            POSITION_COUNTER_PLACEHOLDER
        )
        counter_height = self.replay_position_text.GetBestSize().GetHeight()
        self.replay_position_text.SetMinSize(wx.Size(counter_width, counter_height))
        replay_progress_row.Add(self.replay_slider, 1, wx.ALIGN_CENTER_VERTICAL)
        replay_progress_row.Add(
            self.replay_position_text,
            0,
            wx.ALIGN_CENTER_VERTICAL,
        )
        replay_sizer.Add(
            replay_progress_row,
            0,
            wx.EXPAND | wx.LEFT | wx.RIGHT,
            8,
        )

        replay_sizer.AddStretchSpacer()
        replay_controls = wx.FlexGridSizer(rows=1, cols=5, vgap=0, hgap=4)
        for column in range(5):
            replay_controls.AddGrowableCol(column, 1)
        self.replay_start_button = self._icon_button(
            replay_page,
            "rewind",
            self._on_replay_start,
            "Return to the beginning",
        )
        self.back_button = self._icon_button(
            replay_page,
            "previous",
            self._on_replay_back,
            "Previous step",
        )
        self.play_button = self._icon_button(
            replay_page,
            "play",
            self._on_replay_toggle,
            "Play replay",
            primary=True,
        )
        self.forward_button = self._icon_button(
            replay_page,
            "next",
            self._on_replay_forward,
            "Next step",
        )
        self.replay_note_button = self._icon_button(
            replay_page,
            "note",
            self._on_replay_note,
            "Mark current replay step",
        )
        for button in (
            self.replay_start_button,
            self.back_button,
            self.play_button,
            self.forward_button,
            self.replay_note_button,
        ):
            replay_controls.Add(button, 0, wx.EXPAND)
        replay_sizer.Add(
            replay_controls,
            0,
            wx.EXPAND | wx.LEFT | wx.RIGHT,
            8,
        )
        replay_sizer.AddSpacer(8)
        replay_page.SetSizer(replay_sizer)

        skill_page = wx.Panel(self.mode_tabs)
        skill_page.SetBackgroundColour(BG)
        skill_sizer = wx.BoxSizer(wx.VERTICAL)
        copper_row = wx.BoxSizer(wx.HORIZONTAL)
        self.fill_board_button = self._button(
            skill_page,
            "Fill",
            self._on_fill_board,
        )
        self.fill_board_button.SetMinSize(self.FromDIP((72, 25)))
        net_label = wx.StaticText(skill_page, label="Net:")
        net_label.SetForegroundColour(MUTED)
        net_label.SetFont(self._font(UI_FONT_SIZE))
        self.copper_net_entry = self._text_field(
            skill_page, "GND", size=(68, 22), centered=True
        )
        layer_label = wx.StaticText(skill_page, label="Layers:")
        layer_label.SetForegroundColour(MUTED)
        layer_label.SetFont(self._font(UI_FONT_SIZE))
        self.front_copper_check = wx.CheckBox(skill_page, label="F.Cu")
        self.back_copper_check = wx.CheckBox(skill_page, label="B.Cu")
        for checkbox in (self.front_copper_check, self.back_copper_check):
            checkbox.SetBackgroundColour(BG)
            checkbox.SetForegroundColour(CREAM)
            checkbox.SetFont(self._font(UI_FONT_SIZE))
            checkbox.SetValue(True)
        copper_row.Add(self.fill_board_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        copper_row.Add(net_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        copper_row.Add(self.copper_net_entry, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        copper_row.Add(layer_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        copper_row.Add(self.front_copper_check, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        copper_row.Add(self.back_copper_check, 0, wx.ALIGN_CENTER_VERTICAL)
        skill_sizer.Add(copper_row, 0, wx.EXPAND | wx.ALL, 8)
        fanout_row = wx.BoxSizer(wx.HORIZONTAL)
        self.fanout_button = self._button(
            skill_page,
            "Fanout",
            self._on_fanout,
        )
        self.fanout_button.SetMinSize(self.FromDIP((72, 25)))
        fanout_net_label = wx.StaticText(skill_page, label="Net:")
        fanout_net_label.SetForegroundColour(MUTED)
        fanout_net_label.SetFont(self._font(UI_FONT_SIZE))
        self.fanout_net_entry = self._text_field(
            skill_page, "GND", size=(68, 22), centered=True
        )
        fanout_width_label = wx.StaticText(skill_page, label="Width:")
        fanout_width_label.SetForegroundColour(MUTED)
        fanout_width_label.SetFont(self._font(UI_FONT_SIZE))
        self.fanout_width_entry = self._text_field(
            skill_page, "0.5", size=(52, 22), centered=True
        )
        fanout_row.Add(self.fanout_button, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        fanout_row.Add(fanout_net_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        fanout_row.Add(self.fanout_net_entry, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 8)
        fanout_row.Add(fanout_width_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 4)
        fanout_row.Add(self.fanout_width_entry, 0, wx.ALIGN_CENTER_VERTICAL)
        skill_sizer.Add(fanout_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)
        skill_sizer.AddStretchSpacer()
        skill_sizer.AddSpacer(8)
        skill_page.SetSizer(skill_sizer)

        self.mode_tabs.AddPage(record_page, "", select=True)
        self.mode_tabs.AddPage(replay_page, "")
        self.mode_tabs.AddPage(skill_page, "")
        record_size = record_page.GetBestSize()
        replay_size = replay_page.GetBestSize()
        skill_size = skill_page.GetBestSize()
        page_width = max(
            record_size.GetWidth(),
            replay_size.GetWidth(),
            skill_size.GetWidth(),
        )
        page_height = max(
            record_size.GetHeight(),
            replay_size.GetHeight(),
            skill_size.GetHeight(),
        )
        page_height += self.FromDIP(10)
        self.mode_tabs.SetMinSize(wx.Size(page_width, page_height))
        outer.Add(self.mode_tabs, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        self._select_mode(0)
        root.SetSizer(outer)

    def _text_field(
        self,
        parent: wx.Window,
        value: str,
        subdued: bool = False,
        size: tuple[int, int] = (150, 18),
        centered: bool = False,
    ) -> UnderlinedTextField:
        field = UnderlinedTextField(
            parent,
            value=value,
            size=size,
            centered=centered,
        )
        field.editor.SetBackgroundColour(BG)
        field.editor.SetForegroundColour(DIM if subdued else CREAM)
        field.editor.SetFont(
            self._font(META_FONT_SIZE if subdued else UI_FONT_SIZE, mono=True)
        )
        field.FitToFont()
        return field

    def _button(
        self,
        parent: wx.Window,
        label: str,
        handler,
        primary: bool = False,
        bold: bool = True,
    ) -> wx.Button:
        button = wx.Button(parent, label=label, size=(52, 25), style=wx.BORDER_NONE)
        button.SetMinSize(self.FromDIP((52, 25)))
        button.SetBackgroundColour(ORANGE if primary else PANEL)
        button.SetForegroundColour(BG if primary else CREAM)
        button.SetFont(self._font(UI_FONT_SIZE, bold=bold))
        button.Bind(wx.EVT_BUTTON, handler)
        return button

    @staticmethod
    def _icon_button(
        parent: wx.Window,
        icon: str,
        handler,
        tooltip: str,
        primary: bool = False,
    ) -> VectorIconButton:
        return VectorIconButton(parent, icon, handler, tooltip, primary)

    def _mode_tab(self, parent: wx.Window, label: str, index: int) -> FlatTab:
        return FlatTab(
            parent,
            label,
            self.FromDIP((60, 28)),
            self._font(TAB_FONT_SIZE),
            lambda _event: self._select_mode(index),
        )

    def _select_mode(self, index: int) -> None:
        self.mode_tabs.ChangeSelection(index)
        for tab_index, button in enumerate(
            (self.record_tab_button, self.replay_tab_button, self.skill_tab_button)
        ):
            button.set_selected(tab_index == index)
        self.Layout()

    def _configure_shortcuts(self) -> None:
        self._note_hotkey_id = int(wx.NewIdRef())
        self._undo_hotkey_id = int(wx.NewIdRef())
        self._undo_command_id = int(wx.NewIdRef())
        self.SetAcceleratorTable(
            wx.AcceleratorTable(
                [
                    (wx.ACCEL_CTRL, ord("D"), self.note_button.GetId()),
                    (wx.ACCEL_CTRL, ord("Z"), self._undo_command_id),
                ]
            )
        )
        self.Bind(wx.EVT_MENU, self._on_note_shortcut, id=self.note_button.GetId())
        self.Bind(wx.EVT_MENU, self._on_undo_shortcut, id=self._undo_command_id)
        self.Bind(wx.EVT_HOTKEY, self._on_note_shortcut, id=self._note_hotkey_id)
        self.Bind(wx.EVT_HOTKEY, self._on_undo_shortcut, id=self._undo_hotkey_id)
        self.note_button.SetToolTip("Mark current recorded step (Ctrl+D)")

    def _sync_pcb_hotkeys(self) -> None:
        should_register = self.recorder.recording and self._pcb_window.is_foreground()
        if should_register == self._pcb_hotkeys_registered:
            return
        if should_register:
            note_registered = self.RegisterHotKey(
                self._note_hotkey_id,
                wx.MOD_CONTROL,
                ord("D"),
            )
            undo_registered = self.RegisterHotKey(
                self._undo_hotkey_id,
                wx.MOD_CONTROL,
                ord("Z"),
            )
            if note_registered and undo_registered:
                self._pcb_hotkeys_registered = True
                return
            if note_registered:
                self.UnregisterHotKey(self._note_hotkey_id)
            if undo_registered:
                self.UnregisterHotKey(self._undo_hotkey_id)
            return
        self._unregister_pcb_hotkeys()

    def _unregister_pcb_hotkeys(self) -> None:
        if not self._pcb_hotkeys_registered:
            return
        self.UnregisterHotKey(self._note_hotkey_id)
        self.UnregisterHotKey(self._undo_hotkey_id)
        self._pcb_hotkeys_registered = False

    def _on_note_shortcut(self, event: wx.CommandEvent) -> None:
        if self.note_button.IsEnabled():
            self._on_note(event)

    def _on_undo_shortcut(self, event: wx.CommandEvent) -> None:
        if self.recorder.recording:
            self._on_undo(event)

    def _set_controls(self, running: bool) -> None:
        self.start_button.Enable(True)
        self.start_button.SetIcon(
            "stop" if running else "record",
            "End recording" if running else "Start recording",
        )
        self.note_button.Enable(running)
        self.pcb_entry.SetEditable(not running)
        self.load_button.Enable(not running)
        if running:
            self.pcb_entry.ClearSelection()
        else:
            self._unregister_pcb_hotkeys()
        self._sync_record_controls()
        self._sync_replay_controls()

    def _sync_record_controls(self) -> None:
        count = self.recorder.event_count
        position = (
            self.recorder.preview_position
            if self.recorder.preview_position is not None
            else count
        )
        self.record_slider.Enable(self.recorder.recording and count > 0)
        self.record_slider.SetRange(0, max(1, count))
        self.record_slider.SetValue(position)
        navigation_enabled = self.recorder.recording and count > 0
        self.record_back_button.Enable(navigation_enabled and position > 0)
        self.record_forward_button.Enable(navigation_enabled and position < count)
        self.record_reset_button.Enable(
            self.recorder.recording and self.recorder.preview_position is not None
        )
        self.counter_text.SetLabel(f"{position}/{count}")
        self.counter_text.SetToolTip(f"Record position {position} of {count}")
        self.counter_text.GetParent().Layout()
        self.mode_tabs.Layout()

    def _sync_replay_controls(self) -> None:
        loaded = self.replay.log is not None
        enabled = loaded and not self.recorder.recording
        self.replay_slider.Enable(enabled)
        self.replay_slider.SetRange(0, max(1, self.replay.total))
        self.replay_slider.SetValue(self.replay.position)
        position_label = f"{self.replay.position}/{self.replay.total}"
        self.replay_position_text.SetLabel(position_label)
        self.replay_position_text.SetToolTip(
            f"Replay step {self.replay.position} of {self.replay.total}"
        )
        self.play_button.SetIcon("pause" if self.replay.playing else "play")
        self.play_button.Enable(enabled and self.replay.total > 0)
        self.replay_start_button.Enable(
            enabled and (self.replay.position > 0 or self.replay.playing)
        )
        self.back_button.Enable(enabled and self.replay.position > 0)
        self.forward_button.Enable(enabled and self.replay.position < self.replay.total)
        self.replay_note_button.Enable(enabled)
        # Playback speed is a preference, so it remains selectable before a
        # log is loaded.  Disabling it here made the native choice control
        # silently ignore clicks, which looked like a broken popup.
        self.speed_choice.Enable(not self.recorder.recording)
        self.replay_position_text.GetParent().Layout()
        self.mode_tabs.Layout()

    def _refresh_record(self) -> None:
        self._sync_record_controls()
        self.Layout()

    def _run_action(self, action) -> None:
        try:
            action()
        except LogFileExistsError as exc:
            wx.MessageBox(str(exc), "Existing log file", wx.OK | wx.ICON_WARNING, self)
        except Exception as exc:
            wx.MessageBox(str(exc), "KiLog", wx.OK | wx.ICON_ERROR, self)

    def _on_start(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            log_name = self.pcb_entry.GetValue().strip()
            try:
                self.recorder.start(RecorderConfig(pcb_stem=log_name))
            except LogFileExistsError as exc:
                result = wx.MessageBox(
                    f"{exc}\n\nOverwrite this log and start a new recording?",
                    "Existing log file",
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                    self,
                )
                if result != wx.YES:
                    return
                self.recorder.start(
                    RecorderConfig(
                        pcb_stem=log_name,
                        overwrite_existing=True,
                    )
                )
            self._set_controls(True)
            self.Layout()
            wx.CallAfter(self._pcb_window.activate)

        self._run_action(action)

    def _on_record_toggle(self, event: wx.CommandEvent) -> None:
        if self.recorder.recording:
            self._on_end(event)
        else:
            self._on_start(event)

    def _on_note(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            self.recorder.note()
            self._refresh_record()

        self._run_action(action)

    def _on_undo(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            current = self.recorder.event_count
            target = self.record_slider.GetValue()
            if self.recorder.preview_position is not None:
                self.recorder.undo()
            elif current == 0 or target >= current:
                self.recorder.undo()
            else:
                self.recorder.undo_to(target)
            self._refresh_record()

        self._run_action(action)

    def _on_end(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            if self.recorder.preview_position is not None:
                self.recorder.confirm_preview()
            self.recorder.end()
            self._set_controls(False)
            self.Layout()

        self._run_action(action)

    def _on_load_replay(self, _event: wx.CommandEvent) -> None:
        wildcard = "KiLog JSON (*.json)|*.json|All files (*.*)|*.*"
        dialog = wx.FileDialog(
            self,
            "Choose a KiLog JSON file",
            defaultDir=str(self.recorder.adapter.output_directory),
            wildcard=wildcard,
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        )
        if dialog.ShowModal() != wx.ID_OK:
            dialog.Destroy()
            return
        path = Path(dialog.GetPath())
        dialog.Destroy()

        def action() -> None:
            log = self.replay.load(path)
            self.replay_file_text.SetLabel(str(log.path))
            self.replay_file_text.SetToolTip(str(log.path))
            self._sync_replay_controls()
            self.Layout()

        self._run_action(action)

    def _run_replay_action(self, action) -> None:
        def wrapped() -> None:
            action()
            self._sync_replay_controls()
            self.Layout()

        self._run_action(wrapped)

    def _on_replay_toggle(self, _event: wx.CommandEvent) -> None:
        self._run_replay_action(self.replay.toggle)

    def _on_replay_start(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            self.replay.pause()
            self.replay.seek(0)

        self._run_replay_action(action)

    def _on_replay_back(self, _event: wx.CommandEvent) -> None:
        self._run_replay_action(self.replay.step_back)

    def _on_replay_forward(self, _event: wx.CommandEvent) -> None:
        self._run_replay_action(self.replay.step_forward)

    def _on_replay_note(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            self.replay.pause()
            self.replay.note()
            self._sync_replay_controls()
            self.Layout()

        self._run_action(action)

    def _on_replay_seek(self) -> None:
        target = self.replay_slider.GetValue()
        if target != self.replay.position:
            self._run_replay_action(lambda: self.replay.seek(target))

    def _on_record_seek(self) -> None:
        target = min(self.record_slider.GetValue(), self.recorder.event_count)

        def action() -> None:
            self.recorder.preview(target)
            self._refresh_record()

        self._run_action(action)

    def _preview_record_position(self, target: int) -> None:
        def action() -> None:
            self.recorder.preview(target)
            self._refresh_record()

        self._run_action(action)

    def _on_record_back(self, _event: wx.CommandEvent) -> None:
        self._preview_record_position(self.record_slider.GetValue() - 1)

    def _on_record_forward(self, _event: wx.CommandEvent) -> None:
        self._preview_record_position(self.record_slider.GetValue() + 1)

    def _on_record_reset(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            if self.recorder.preview_position is None:
                return
            self.recorder.confirm_preview()
            self._refresh_record()
            wx.CallAfter(self._pcb_window.activate)

        self._run_action(action)

    def _on_speed(self) -> None:
        speeds = (0.25, 0.5, 1.0, 2.0, 4.0)
        self.replay.set_speed(speeds[self.speed_choice.GetSelection()])

    def _on_fill_board(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            layers = tuple(
                name
                for name, checkbox in (
                    ("F.Cu", self.front_copper_check),
                    ("B.Cu", self.back_copper_check),
                )
                if checkbox.GetValue()
            )
            self.recorder.adapter.fill_board_copper(
                self.copper_net_entry.GetValue(),
                layers,
            )

        self._run_action(action)

    def _on_fanout(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            self.recorder.adapter.fanout_net(
                self.fanout_net_entry.GetValue().strip(),
                self.fanout_width_entry.GetValue(),
            )

        self._run_action(action)

    def _on_poll(self, _event: wx.TimerEvent) -> None:
        self._sync_pcb_hotkeys()
        self._position_poll_tick += 1
        if self._position_poll_tick >= self.POSITION_POLL_TICKS:
            self._position_poll_tick = 0
            self._move_to_pcb_bottom_left()
        try:
            if self.recorder.recording:
                event = self.recorder.poll()
                if event:
                    self._refresh_record()
            if self.replay.playing and self.replay.tick():
                self._sync_replay_controls()
                self.Layout()
        except Exception as exc:
            lowered = str(exc).lower()
            if "busy" in lowered or "timeout" in lowered:
                return
            elif self.replay.log is not None and not self.recorder.recording:
                self.replay.pause()
                self._sync_replay_controls()
                self.Layout()
            else:
                self._refresh_record()

    def _move_to_pcb_bottom_left(self) -> None:
        if self.speed_choice.popup_open:
            return

        bounds = self._pcb_window.client_bounds()
        if bounds is None:
            display_index = wx.Display.GetFromWindow(self)
            if display_index == wx.NOT_FOUND:
                display_index = 0
            area = wx.Display(display_index).GetClientArea()
            bounds = WindowRect(area.x, area.y, area.GetRight() + 1, area.GetBottom() + 1)

        margin_x, margin_y = self.FromDIP((4, 4))
        if self._pcb_window.place_bottom_left(
            self.GetHandle(),
            bounds,
            (margin_x, margin_y),
        ):
            return

        size = self.GetSize()
        position = bottom_left_position(
            bounds,
            (size.GetWidth(), size.GetHeight()),
            (margin_x, margin_y),
        )
        if self.GetPosition() != wx.Point(*position):
            self.Move(position)

    def _on_close(self, event: wx.CloseEvent) -> None:
        self.timer.Stop()
        self.replay.pause()
        self._unregister_pcb_hotkeys()
        if self.recorder.recording:
            try:
                self.recorder.end()
            except Exception as exc:
                result = wx.MessageBox(
                    f"The final change could not be saved: {exc}\nClose anyway?",
                    "KiLog",
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                    self,
                )
                if result != wx.YES:
                    self.timer.Start(self.POLL_MS)
                    event.Veto()
                    return
        event.Skip()
