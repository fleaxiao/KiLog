from __future__ import annotations

from pathlib import Path

import wx

from .path_display import default_log_name, trailing_directories
from .recorder import LogFileExistsError, Recorder, RecorderConfig
from .window_position import PcbEditorWindow, WindowRect, bottom_left_position


BG = "#14231E"
PANEL = "#20352D"
FIELD = "#0E1C17"
ORANGE = "#F2A33A"
CREAM = "#F3F0E8"
MUTED = "#A9BCB4"
DIM = "#72877E"
RED = "#F07167"
WINDOW_ALPHA = 242
UI_FONT_FACE = "Segoe UI"


class UnderlinedTextField(wx.Panel):
    """A borderless single-line text field with only a bottom rule."""

    def __init__(self, parent: wx.Window, value: str, size: tuple[int, int]):
        super().__init__(parent, size=size)
        self.SetBackgroundColour(BG)
        self.editor = wx.TextCtrl(
            self,
            value=value,
            style=wx.BORDER_NONE | wx.TE_LEFT,
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
        self.editor.SetSize(0, 0, width, max(1, height - 2))

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
        end = self.editor.GetLastPosition()
        self.editor.SetInsertionPoint(end)
        self.editor.SetSelection(end, end)


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
        self.Fit()
        self.SetMinSize(self.GetSize())
        self._move_to_pcb_bottom_left()
        if self.CanSetTransparent():
            self.SetTransparent(WINDOW_ALPHA)

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_poll, self.timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.timer.Start(self.POLL_MS)

    @staticmethod
    def _font(size: int, bold: bool = False, mono: bool = False) -> wx.Font:
        weight = wx.FONTWEIGHT_SEMIBOLD if bold else wx.FONTWEIGHT_NORMAL
        return wx.Font(
            size,
            wx.FONTFAMILY_SWISS,
            wx.FONTSTYLE_NORMAL,
            weight,
            False,
            UI_FONT_FACE,
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

        output_path = Path(self.recorder.adapter.output_directory)
        output_directory = str(output_path)

        details_row = wx.BoxSizer(wx.HORIZONTAL)
        path_label = wx.StaticText(root, label="Path:")
        path_label.SetForegroundColour(MUTED)
        path_label.SetFont(self._font(9))
        output = wx.StaticText(
            root,
            label=trailing_directories(output_path, count=2),
            size=(115, -1),
            style=wx.ST_ELLIPSIZE_START,
        )
        output.SetForegroundColour(DIM)
        output.SetFont(self._font(8, mono=True))
        output.SetToolTip(f"Output directory: {output_directory}")
        log_label = wx.StaticText(root, label="Log:")
        log_label.SetForegroundColour(MUTED)
        log_label.SetFont(self._font(9))
        board_path = getattr(self.recorder.adapter, "board_path", None)
        self.pcb_entry = self._text_field(
            root,
            default_log_name(board_path),
            size=(70, 18),
        )

        details_row.Add(path_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 5)
        details_row.Add(output, 1, wx.ALIGN_CENTER_VERTICAL)
        details_row.Add(log_label, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT | wx.RIGHT, 8)
        details_row.Add(self.pcb_entry, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(details_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 12)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = self._button(root, "Start", self._on_start, primary=True)
        self.note_button = self._button(root, "Note", self._on_note)
        self.undo_button = self._button(root, "Undo", self._on_undo)
        self.end_button = self._button(root, "End", self._on_end)
        for index, button in enumerate(
            (self.start_button, self.note_button, self.undo_button, self.end_button)
        ):
            buttons.Add(button, 1, wx.LEFT if index else 0, 6)
        outer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)

        status_row = wx.BoxSizer(wx.HORIZONTAL)
        self.status_text = wx.StaticText(root, label="Ready — click Start to record", size=(225, -1))
        self.status_text.SetForegroundColour(MUTED)
        self.status_text.SetFont(self._font(9))
        self.counter_text = wx.StaticText(root, label="0 changes")
        self.counter_text.SetForegroundColour(ORANGE)
        self.counter_text.SetFont(self._font(8, mono=True))
        status_row.Add(self.status_text, 1, wx.ALIGN_CENTER_VERTICAL)
        status_row.Add(self.counter_text, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        outer.Add(
            status_row,
            0,
            wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM,
            10,
        )
        root.SetSizer(outer)

    def _text_field(
        self,
        parent: wx.Window,
        value: str,
        subdued: bool = False,
        size: tuple[int, int] = (150, 18),
    ) -> UnderlinedTextField:
        field = UnderlinedTextField(parent, value=value, size=size)
        field.editor.SetBackgroundColour(BG)
        field.editor.SetForegroundColour(DIM if subdued else CREAM)
        field.editor.SetFont(self._font(8 if subdued else 9, mono=True))
        return field

    def _button(self, parent: wx.Window, label: str, handler, primary: bool = False) -> wx.Button:
        button = wx.Button(parent, label=label, size=(58, 27), style=wx.BORDER_NONE)
        button.SetMinSize(self.FromDIP((58, 27)))
        button.SetBackgroundColour(ORANGE if primary else PANEL)
        button.SetForegroundColour(BG if primary else CREAM)
        button.SetFont(self._font(9, bold=True))
        button.Bind(wx.EVT_BUTTON, handler)
        return button

    def _configure_shortcuts(self) -> None:
        self._note_hotkey_id = int(wx.NewIdRef())
        self._undo_hotkey_id = int(wx.NewIdRef())
        self.SetAcceleratorTable(
            wx.AcceleratorTable(
                [
                    (wx.ACCEL_CTRL, ord("D"), self.note_button.GetId()),
                    (wx.ACCEL_CTRL, ord("Z"), self.undo_button.GetId()),
                ]
            )
        )
        self.Bind(wx.EVT_MENU, self._on_note_shortcut, id=self.note_button.GetId())
        self.Bind(wx.EVT_MENU, self._on_undo_shortcut, id=self.undo_button.GetId())
        self.Bind(wx.EVT_HOTKEY, self._on_note_shortcut, id=self._note_hotkey_id)
        self.Bind(wx.EVT_HOTKEY, self._on_undo_shortcut, id=self._undo_hotkey_id)
        self.note_button.SetToolTip("Note (Ctrl+D)")
        self.undo_button.SetToolTip("Undo (Ctrl+Z)")

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
        if self.undo_button.IsEnabled():
            self._on_undo(event)

    def _set_controls(self, running: bool) -> None:
        self.start_button.Enable(not running)
        self.note_button.Enable(running)
        self.undo_button.Enable(running)
        self.end_button.Enable(running)
        self.pcb_entry.SetEditable(not running)
        if running:
            self.pcb_entry.ClearSelection()
        else:
            self._unregister_pcb_hotkeys()

    def _status(self, text: str, colour: str = MUTED) -> None:
        self.status_text.SetLabel(text)
        self.status_text.SetForegroundColour(colour)
        self.counter_text.SetLabel(f"{self.recorder.event_count} changes")
        self.Layout()

    def _run_action(self, action) -> None:
        try:
            action()
        except LogFileExistsError as exc:
            self._status("Recording not started — log file already exists", ORANGE)
            wx.MessageBox(str(exc), "Existing log file", wx.OK | wx.ICON_WARNING, self)
        except Exception as exc:
            self._status(str(exc), RED)
            wx.MessageBox(str(exc), "KiLog", wx.OK | wx.ICON_ERROR, self)

    def _on_start(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            self.recorder.start(
                RecorderConfig(
                    pcb_stem=self.pcb_entry.GetValue().strip(),
                )
            )
            self._set_controls(True)
            self._status("Recording live PCB changes", ORANGE)
            wx.CallAfter(self._pcb_window.activate)

        self._run_action(action)

    def _on_note(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            path = self.recorder.note()
            self._status(f"Saved reference {path.name}", ORANGE)

        self._run_action(action)

    def _on_undo(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            path, strategy = self.recorder.undo()
            label = "native KiCad undo" if strategy == "native" else "snapshot restore"
            self._status(f"Undone · {label} · updated {path.name}", ORANGE)

        self._run_action(action)

    def _on_end(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            event = self.recorder.end()
            self._set_controls(False)
            suffix = " · final change saved" if event else ""
            self._status(f"Recording ended{suffix}", MUTED)

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
                    log_name = self.recorder.log_path.name if self.recorder.log_path else "log"
                    self._status(f"Saved event #{event['sequence']:02d} to {log_name}", ORANGE)
        except Exception as exc:
            lowered = str(exc).lower()
            if "busy" in lowered or "timeout" in lowered:
                self._status("Waiting for KiCad to finish the active tool…", ORANGE)
            else:
                self._status(f"Temporary recording error: {exc}", RED)

    def _move_to_pcb_bottom_left(self) -> None:
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
