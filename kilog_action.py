from __future__ import annotations

import ctypes
from pathlib import Path
import sys


def _enable_high_dpi() -> None:
    """Prevent Windows from bitmap-scaling the wx client area."""
    if sys.platform != "win32":
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2.  This must run before
        # importing wx or creating any HWND; non-client areas are already
        # rendered at native DPI by Windows, which otherwise makes only the
        # plugin client text look blurred.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        # Windows 8.1 fallback: PROCESS_PER_MONITOR_DPI_AWARE.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


_enable_high_dpi()

import wx  # noqa: E402  (DPI awareness must be configured first.)


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
            f"{exc}\n\nMake sure PCB Editor is open and the IPC API is enabled "
            "under Preferences > Plugins.",
            "KiLog startup failed",
            wx.OK | wx.ICON_ERROR,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
