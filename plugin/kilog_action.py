from __future__ import annotations

from pathlib import Path
import sys

import wx


PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


def main() -> int:
    try:
        from kipy import KiCad

        from kilog.kicad_adapter import KiCadBoardAdapter
        from kilog.recorder import Recorder
        from kilog.ui import KiLogWindow

        kicad = KiCad(client_name="com.kilog.recorder", timeout_ms=2500)
        board = kicad.get_board()
        adapter = KiCadBoardAdapter(kicad, board)
        recorder = Recorder(adapter)
        app = wx.App(False)
        window = KiLogWindow(recorder, PLUGIN_DIR / "assets")
        window.Show()
        app.MainLoop()
        return 0
    except Exception as exc:
        app = wx.GetApp() or wx.App(False)
        wx.MessageBox(
            f"{exc}\n\n请确认 PCB Editor 已打开，并在 Preferences > Plugins 中启用 IPC API。",
            "KiLog 启动失败",
            wx.OK | wx.ICON_ERROR,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
