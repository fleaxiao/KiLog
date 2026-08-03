from __future__ import annotations

from pathlib import Path

import wx

from .recorder import LogFileExistsError, Recorder, RecorderConfig


BG = "#14231E"
PANEL = "#20352D"
FIELD = "#0E1C17"
ORANGE = "#F2A33A"
CREAM = "#F3F0E8"
MUTED = "#A9BCB4"
DIM = "#72877E"
RED = "#F07167"
WINDOW_ALPHA = 242


class KiLogWindow(wx.Frame):
    """Native wxPython control panel using the GUI runtime bundled with KiCad."""

    POLL_MS = 160

    def __init__(self, recorder: Recorder, asset_directory: Path):
        super().__init__(
            parent=None,
            title="KiLog",
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )
        self.recorder = recorder
        self.asset_directory = asset_directory
        self.SetBackgroundColour(BG)
        self.SetIcon(self._load_icon())
        self.SetDoubleBuffered(True)

        self._build()
        self._set_controls(False)
        self.Fit()
        self.SetMinSize(self.GetSize())
        self.Centre(wx.BOTH)
        if self.CanSetTransparent():
            self.SetTransparent(WINDOW_ALPHA)

        self.timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_poll, self.timer)
        self.Bind(wx.EVT_CLOSE, self._on_close)
        self.timer.Start(self.POLL_MS)

    @staticmethod
    def _font(size: int, bold: bool = False, mono: bool = False) -> wx.Font:
        family = wx.FONTFAMILY_TELETYPE if mono else wx.FONTFAMILY_SWISS
        weight = wx.FONTWEIGHT_SEMIBOLD if bold else wx.FONTWEIGHT_NORMAL
        return wx.Font(size, family, wx.FONTSTYLE_NORMAL, weight)

    def _load_icon(self) -> wx.Icon:
        path = self.asset_directory / "icon-ui-64.png"
        if path.exists():
            return wx.Icon(str(path), wx.BITMAP_TYPE_PNG)
        return wx.NullIcon

    def _build(self) -> None:
        root = wx.Panel(self)
        root.SetBackgroundColour(BG)
        outer = wx.BoxSizer(wx.VERTICAL)

        header = wx.BoxSizer(wx.HORIZONTAL)
        title = wx.StaticText(root, label="KiLog")
        title.SetForegroundColour(CREAM)
        title.SetFont(self._font(13, bold=True))
        self.counter_text = wx.StaticText(root, label="0 changes")
        self.counter_text.SetForegroundColour(ORANGE)
        self.counter_text.SetFont(self._font(8, mono=True))
        header.Add(title, 1, wx.ALIGN_CENTER_VERTICAL)
        header.Add(self.counter_text, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(header, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)

        form_sizer = wx.FlexGridSizer(rows=1, cols=2, vgap=6, hgap=10)
        pcb_label = wx.StaticText(root, label="File prefix")
        pcb_label.SetForegroundColour(MUTED)
        pcb_label.SetFont(self._font(8))
        self.pcb_entry = self._text_field(root, "ref")
        form_sizer.Add(pcb_label, 0, wx.ALIGN_CENTER_VERTICAL)
        form_sizer.Add(self.pcb_entry, 1, wx.EXPAND)
        form_sizer.AddGrowableCol(1, 1)
        outer.Add(form_sizer, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = self._button(root, "Start", self._on_start, primary=True)
        self.note_button = self._button(root, "Note", self._on_note)
        self.undo_button = self._button(root, "Undo", self._on_undo)
        self.end_button = self._button(root, "End", self._on_end)
        for index, button in enumerate(
            (self.start_button, self.note_button, self.undo_button, self.end_button)
        ):
            buttons.Add(button, 1, wx.LEFT if index else 0, 6)
        outer.Add(buttons, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)

        status_row = wx.BoxSizer(wx.HORIZONTAL)
        self.status_text = wx.StaticText(root, label="Ready — click Start to record", size=(286, -1))
        self.status_text.SetForegroundColour(MUTED)
        self.status_text.SetFont(self._font(8))
        status_row.Add(self.status_text, 1, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(status_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 14)

        output_directory = str(self.recorder.adapter.output_directory)
        output = wx.StaticText(
            root,
            label=output_directory,
            size=(360, -1),
            style=wx.ST_ELLIPSIZE_MIDDLE,
        )
        output.SetForegroundColour(DIM)
        output.SetFont(self._font(7, mono=True))
        output.SetToolTip(f"Output directory: {output_directory}")
        outer.Add(output, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 14)
        root.SetSizer(outer)

    def _text_field(self, parent: wx.Window, value: str, subdued: bool = False) -> wx.TextCtrl:
        field = wx.TextCtrl(parent, value=value, size=(280, 25), style=wx.BORDER_SIMPLE)
        field.SetBackgroundColour(BG if subdued else FIELD)
        field.SetForegroundColour(DIM if subdued else CREAM)
        field.SetFont(self._font(7 if subdued else 8, mono=True))
        return field

    def _button(self, parent: wx.Window, label: str, handler, primary: bool = False) -> wx.Button:
        button = wx.Button(parent, label=label, size=(-1, 29), style=wx.BORDER_NONE)
        button.SetBackgroundColour(ORANGE if primary else PANEL)
        button.SetForegroundColour(BG if primary else CREAM)
        button.SetFont(self._font(8, bold=True))
        button.Bind(wx.EVT_BUTTON, handler)
        return button

    def _set_controls(self, running: bool) -> None:
        self.start_button.Enable(not running)
        self.note_button.Enable(running)
        self.undo_button.Enable(running)
        self.end_button.Enable(running)
        self.pcb_entry.Enable(not running)

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
                    pcb_stem=self.pcb_entry.GetValue(),
                )
            )
            self._set_controls(True)
            self._status("Recording live PCB changes", ORANGE)

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

    def _on_close(self, event: wx.CloseEvent) -> None:
        self.timer.Stop()
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
