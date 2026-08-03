from __future__ import annotations

from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class WindowRect:
    left: int
    top: int
    right: int
    bottom: int


def bottom_left_position(
    bounds: WindowRect,
    window_size: tuple[int, int],
    margin: tuple[int, int],
) -> tuple[int, int]:
    """Return a bottom-left position that remains inside the supplied bounds."""
    width, height = window_size
    margin_x, margin_y = margin
    x = bounds.left + margin_x
    y = bounds.bottom - height - margin_y
    if width <= bounds.right - bounds.left:
        x = min(x, bounds.right - width)
    return max(bounds.left, x), max(bounds.top, y)


class PcbEditorWindow:
    """Locate the Windows PCB Editor window and report its canvas bounds."""

    def __init__(self) -> None:
        self._hwnd: int | None = None

    def client_bounds(self) -> WindowRect | None:
        if sys.platform != "win32":
            return None

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL
        hwnd = self._hwnd
        if not hwnd or not user32.IsWindow(hwnd):
            hwnd = self._find_window()
            self._hwnd = hwnd
        if not hwnd or user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
            return None

        client = wintypes.RECT()
        top_left = wintypes.POINT(0, 0)
        bottom_right = wintypes.POINT()
        if not user32.GetClientRect(hwnd, ctypes.byref(client)):
            return None
        bottom_right.x = client.right
        bottom_right.y = client.bottom
        if not user32.ClientToScreen(hwnd, ctypes.byref(top_left)):
            return None
        if not user32.ClientToScreen(hwnd, ctypes.byref(bottom_right)):
            return None
        client_bounds = WindowRect(
            top_left.x,
            top_left.y,
            bottom_right.x,
            bottom_right.y,
        )
        return self._find_canvas_bounds(hwnd, client_bounds) or client_bounds

    def activate(self) -> bool:
        """Return keyboard focus to the PCB Editor without changing its geometry."""
        if sys.platform != "win32":
            return False

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        hwnd = self._hwnd
        if not hwnd or not user32.IsWindow(hwnd):
            hwnd = self._find_window()
            self._hwnd = hwnd
        return bool(hwnd and user32.SetForegroundWindow(hwnd))

    @staticmethod
    def _find_canvas_bounds(hwnd: int, client_bounds: WindowRect) -> WindowRect | None:
        """Find the deepest large child window, which is KiCad's PCB canvas."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumChildWindows.argtypes = [wintypes.HWND, enum_callback, wintypes.LPARAM]
        user32.EnumChildWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND

        client_width = client_bounds.right - client_bounds.left
        client_height = client_bounds.bottom - client_bounds.top
        minimum_width = client_width * 45 // 100
        minimum_height = client_height * 45 // 100
        minimum_top = client_bounds.top + max(24, client_height // 50)
        candidates: list[tuple[int, int, WindowRect]] = []

        def visit(child: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(child):
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(child, ctypes.byref(rect)):
                return True
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if (
                width < minimum_width
                or height < minimum_height
                or rect.top < minimum_top
                or rect.left < client_bounds.left
                or rect.right > client_bounds.right
                or rect.bottom > client_bounds.bottom
            ):
                return True

            depth = 0
            parent = user32.GetParent(child)
            while parent and parent != hwnd:
                depth += 1
                parent = user32.GetParent(parent)
            candidates.append(
                (
                    depth,
                    width * height,
                    WindowRect(rect.left, rect.top, rect.right, rect.bottom),
                )
            )
            return True

        callback = enum_callback(visit)
        user32.EnumChildWindows(hwnd, callback, 0)
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[0], item[1]))[2]

    @staticmethod
    def place_bottom_left(
        window_handle: int,
        bounds: WindowRect,
        margin: tuple[int, int],
    ) -> bool:
        """Position a native window exactly and keep it above all other windows."""
        if sys.platform != "win32" or not window_handle:
            return False

        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL

        window_rect = wintypes.RECT()
        if not user32.GetWindowRect(window_handle, ctypes.byref(window_rect)):
            return False
        position = bottom_left_position(
            bounds,
            (
                window_rect.right - window_rect.left,
                window_rect.bottom - window_rect.top,
            ),
            margin,
        )
        hwnd_topmost = wintypes.HWND(-1)
        swp_nosize = 0x0001
        swp_noactivate = 0x0010
        return bool(
            user32.SetWindowPos(
                window_handle,
                hwnd_topmost,
                position[0],
                position[1],
                0,
                0,
                swp_nosize | swp_noactivate,
            )
        )

    @staticmethod
    def _find_window() -> int | None:
        import ctypes
        from ctypes import wintypes
        from pathlib import Path

        kernel32 = ctypes.windll.kernel32
        user32 = ctypes.windll.user32
        process_query_limited_information = 0x1000
        candidates: list[tuple[int, int]] = []

        enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        def visit(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True

            process_id = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            process = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                process_id.value,
            )
            if not process:
                return True
            try:
                length = wintypes.DWORD(32768)
                image_path = ctypes.create_unicode_buffer(length.value)
                if not kernel32.QueryFullProcessImageNameW(
                    process,
                    0,
                    image_path,
                    ctypes.byref(length),
                ):
                    return True
                process_name = Path(image_path.value).name.casefold()
                title_length = user32.GetWindowTextLengthW(hwnd)
                title = ctypes.create_unicode_buffer(title_length + 1)
                user32.GetWindowTextW(hwnd, title, len(title))
                is_pcb_editor = process_name == "pcbnew.exe" or (
                    process_name == "kicad.exe" and "pcb" in title.value.casefold()
                )
                if not is_pcb_editor:
                    return True

                rect = wintypes.RECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
                    candidates.append((area, int(hwnd)))
            finally:
                kernel32.CloseHandle(process)
            return True

        callback = enum_callback(visit)
        user32.EnumWindows(callback, 0)
        return max(candidates, default=(0, 0))[1] or None
