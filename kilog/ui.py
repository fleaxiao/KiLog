from __future__ import annotations

from pathlib import Path

import wx

from .recorder import Recorder, RecorderConfig


BG = "#063D2C"
PANEL = "#0A4A36"
FIELD = "#052F23"
ORANGE = "#F5A11A"
CREAM = "#F7F2E8"
MUTED = "#A8C6B8"
DIM = "#6E9C87"
RED = "#F26B5E"


class KiLogWindow(wx.Frame):
    """Native wxPython control panel using the GUI runtime bundled with KiCad."""

    POLL_MS = 160

    def __init__(self, recorder: Recorder, asset_directory: Path):
        super().__init__(
            parent=None,
            title="KiLog — PCB operation recorder",
            style=wx.DEFAULT_FRAME_STYLE & ~(wx.RESIZE_BORDER | wx.MAXIMIZE_BOX),
        )
        self.recorder = recorder
        self.asset_directory = asset_directory
        self.SetBackgroundColour(BG)
        self.SetIcon(self._load_icon())

        self._build()
        self._set_controls(False)
        self.Fit()
        self.SetMinSize(self.GetSize())
        self.Centre(wx.BOTH)

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
        icon_path = self.asset_directory / "icon-ui-64.png"
        if icon_path.exists():
            bitmap = wx.Bitmap(str(icon_path), wx.BITMAP_TYPE_PNG)
            header.Add(wx.StaticBitmap(root, bitmap=bitmap), 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 12)
        titles = wx.BoxSizer(wx.VERTICAL)
        title = wx.StaticText(root, label="KiLog")
        title.SetForegroundColour(CREAM)
        title.SetFont(self._font(20, bold=True))
        subtitle = wx.StaticText(root, label="LIVE PCB CHANGE JOURNAL")
        subtitle.SetForegroundColour(ORANGE)
        subtitle.SetFont(self._font(8, mono=True))
        titles.Add(title, 0, wx.BOTTOM, 2)
        titles.Add(subtitle)
        header.Add(titles, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(header, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 22)

        form = wx.Panel(root)
        form.SetBackgroundColour(PANEL)
        form_sizer = wx.FlexGridSizer(rows=2, cols=2, vgap=6, hgap=14)
        pcb_label = wx.StaticText(form, label="目标 PCB 文件名")
        log_label = wx.StaticText(form, label="目标 Log 文件名")
        for label in (pcb_label, log_label):
            label.SetForegroundColour(MUTED)
            label.SetFont(self._font(9))
        self.pcb_entry = self._text_field(form, "ref")
        self.log_entry = self._text_field(form, "log")
        form_sizer.Add(pcb_label, 0, wx.EXPAND)
        form_sizer.Add(log_label, 0, wx.EXPAND)
        form_sizer.Add(self.pcb_entry, 1, wx.EXPAND)
        form_sizer.Add(self.log_entry, 1, wx.EXPAND)
        form_sizer.AddGrowableCol(0, 1)
        form_sizer.AddGrowableCol(1, 1)
        form.SetSizer(form_sizer)
        outer.Add(form, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 22)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.start_button = self._button(root, "start", self._on_start, primary=True)
        self.note_button = self._button(root, "note", self._on_note)
        self.undo_button = self._button(root, "undo", self._on_undo)
        self.end_button = self._button(root, "end", self._on_end)
        for index, button in enumerate(
            (self.start_button, self.note_button, self.undo_button, self.end_button)
        ):
            buttons.Add(button, 1, wx.LEFT if index else 0, 8)
        outer.Add(buttons, 0, wx.EXPAND | wx.ALL, 22)

        status_row = wx.BoxSizer(wx.HORIZONTAL)
        self.status_dot = wx.StaticText(root, label="●")
        self.status_dot.SetForegroundColour(MUTED)
        self.status_text = wx.StaticText(root, label="就绪 — 点击 start 开始记录", size=(350, -1))
        self.status_text.SetForegroundColour(MUTED)
        self.status_text.SetFont(self._font(9))
        self.counter_text = wx.StaticText(root, label="0 changes")
        self.counter_text.SetForegroundColour(ORANGE)
        self.counter_text.SetFont(self._font(8, mono=True))
        status_row.Add(self.status_dot, 0, wx.RIGHT | wx.ALIGN_CENTER_VERTICAL, 7)
        status_row.Add(self.status_text, 1, wx.ALIGN_CENTER_VERTICAL)
        status_row.Add(self.counter_text, 0, wx.ALIGN_CENTER_VERTICAL)
        outer.Add(status_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 22)

        output = wx.StaticText(root, label=f"输出目录  {self.recorder.adapter.output_directory}")
        output.SetForegroundColour(DIM)
        output.SetFont(self._font(8, mono=True))
        outer.Add(output, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM, 22)
        root.SetSizer(outer)

    def _text_field(self, parent: wx.Window, value: str) -> wx.TextCtrl:
        field = wx.TextCtrl(parent, value=value, size=(220, 30), style=wx.BORDER_SIMPLE)
        field.SetBackgroundColour(FIELD)
        field.SetForegroundColour(CREAM)
        field.SetFont(self._font(10, mono=True))
        return field

    def _button(self, parent: wx.Window, label: str, handler, primary: bool = False) -> wx.Button:
        button = wx.Button(parent, label=label, size=(104, 38), style=wx.BORDER_NONE)
        button.SetBackgroundColour(ORANGE if primary else PANEL)
        button.SetForegroundColour(BG if primary else CREAM)
        button.SetFont(self._font(10, bold=True))
        button.Bind(wx.EVT_BUTTON, handler)
        return button

    def _set_controls(self, running: bool) -> None:
        self.start_button.Enable(not running)
        self.note_button.Enable(running)
        self.undo_button.Enable(running)
        self.end_button.Enable(running)
        self.pcb_entry.Enable(not running)
        self.log_entry.Enable(not running)

    def _status(self, text: str, colour: str = MUTED) -> None:
        self.status_text.SetLabel(text)
        self.status_dot.SetForegroundColour(colour)
        self.counter_text.SetLabel(f"{self.recorder.event_count} changes")
        self.Layout()

    def _run_action(self, action) -> None:
        try:
            action()
        except Exception as exc:
            self._status(str(exc), RED)
            wx.MessageBox(str(exc), "KiLog", wx.OK | wx.ICON_ERROR, self)

    def _on_start(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            self.recorder.start(
                RecorderConfig(
                    pcb_stem=self.pcb_entry.GetValue(),
                    log_stem=self.log_entry.GetValue(),
                )
            )
            self._set_controls(True)
            self._status("记录中 — 监听未保存 PCB 内存状态", ORANGE)

        self._run_action(action)

    def _on_note(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            path = self.recorder.note()
            self._status(f"已保存快照 {path.name}", ORANGE)

        self._run_action(action)

    def _on_undo(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            path, strategy = self.recorder.undo()
            label = "KiCad 原生撤销" if strategy == "native" else "对象快照恢复"
            self._status(f"已撤销并删除 {path.name} · {label}", ORANGE)

        self._run_action(action)

    def _on_end(self, _event: wx.CommandEvent) -> None:
        def action() -> None:
            event = self.recorder.end()
            self._set_controls(False)
            suffix = "，最后变化已写入" if event else ""
            self._status(f"记录已结束{suffix}", MUTED)

        self._run_action(action)

    def _on_poll(self, _event: wx.TimerEvent) -> None:
        try:
            if self.recorder.recording:
                event = self.recorder.poll()
                if event:
                    self._status(f"已写入 log #{event['sequence']:06d}", ORANGE)
        except Exception as exc:
            lowered = str(exc).lower()
            if "busy" in lowered or "timeout" in lowered:
                self._status("等待 KiCad 完成交互操作…", ORANGE)
            else:
                self._status(f"监听暂时失败：{exc}", RED)

    def _on_close(self, event: wx.CloseEvent) -> None:
        self.timer.Stop()
        if self.recorder.recording:
            try:
                self.recorder.end()
            except Exception as exc:
                result = wx.MessageBox(
                    f"最后变化写入失败：{exc}\n仍要关闭吗？",
                    "KiLog",
                    wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
                    self,
                )
                if result != wx.YES:
                    self.timer.Start(self.POLL_MS)
                    event.Veto()
                    return
        event.Skip()
