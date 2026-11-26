"""Perching: the duck sits on the top edge of another app's window or the taskbar, follows it, falls off when it goes.

Geometry is pure (rects as (left, top, right, bottom) tuples in Qt logical pixels) so it is tested without windows.
The Win32 half lists top-level windows with ctypes and converts their physical-pixel DWM frames to logical pixels.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Sequence
from ctypes import wintypes

Rect = tuple[int, int, int, int]  # left, top, right, bottom

PERCH_REACH_PX = 40  # feet within this of a window's top edge snap onto it on release
TASKBAR_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _inside(r: Rect, x: int, y: int) -> bool:
    return r[0] <= x < r[2] and r[1] <= y < r[3]


def pick_perch(rects: Sequence[Rect], feet_x: int, feet_y: int, reach: int = PERCH_REACH_PX) -> int | None:
    """Index of the window to sit on: rects are in z-order (topmost first); the first whose top edge is within
    `reach` of the feet, runs under feet_x, and is not covered there by a window above it."""
    for i, r in enumerate(rects):
        if r[0] <= feet_x < r[2] and abs(feet_y - r[1]) <= reach:
            if not any(_inside(above, feet_x, r[1]) for above in rects[:i]):
                return i
    return None


def perch_covered(rects: Sequence[Rect], index: int | None, feet_x: int) -> bool:
    """True when the perch (rects[index], z-order topmost first) is no longer where the duck can be seen sitting:
    gone from the list (index None) or a window above it covers its top edge under the feet."""
    if index is None:
        return True
    top = rects[index][1]
    return any(_inside(above, feet_x, top) for above in rects[:index])


def perch_position(edge: Rect, center_x: int, sprite_cx: int, feet_y: int, margin: int,
                   right_reserve: int = 0) -> tuple[int, int]:
    """Pet window top-left that stands the feet (local y `feet_y`) on the edge's top, with the sprite centre
    (local x `sprite_cx`) kept at least `margin` inside the edge's ends (and clear of `right_reserve` on the
    right when the window has room, e.g. Win32 caption buttons)."""
    right = edge[2] - max(0, right_reserve)
    if right - margin < edge[0] + margin:
        right = edge[2]
    lo, hi = edge[0] + margin, right - margin
    cx = (edge[0] + edge[2]) // 2 if lo > hi else max(lo, min(hi, center_x))
    return cx - sprite_cx, edge[1] - feet_y


def perch_lost(rect: Rect | None, monitors: Sequence[Rect], feet_x: int) -> bool:
    """True when there is nothing left to sit on: the window is gone (None: closed, minimized, cloaked), fills a
    whole monitor (fullscreen), or its top edge under the feet is on no monitor (moved off screen)."""
    if rect is None or any(rect[0] <= m[0] and rect[1] <= m[1] and rect[2] >= m[2] and rect[3] >= m[3]
                           for m in monitors):
        return True
    x = max(rect[0], min(rect[2] - 1, feet_x))
    return not any(_inside(m, x, rect[1]) for m in monitors)


# ---------------------------------------------------------------------------- Win32


_USER32: Any = None
_DWMAPI: Any = None


def _user32():
    global _USER32
    if _USER32 is None:
        u = ctypes.windll.user32
        u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        for fn in (u.IsWindow, u.IsWindowVisible, u.IsIconic):
            fn.argtypes = [wintypes.HWND]
        _USER32 = u
    return _USER32


def _dwmapi():
    global _DWMAPI
    if _DWMAPI is None:
        _DWMAPI = ctypes.windll.dwmapi
    return _DWMAPI


def _class(u, hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(hwnd, buf, 64)
    return buf.value


def _physical_rect(u, hwnd: int) -> Rect | None:
    """DWM frame bounds (no invisible resize border) of a visible, not minimized, not cloaked window."""
    if not (u.IsWindow(hwnd) and u.IsWindowVisible(hwnd)) or u.IsIconic(hwnd):
        return None
    dwm = ctypes.windll.dwmapi
    cloaked = wintypes.DWORD()
    dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
    if cloaked.value:
        return None
    r = wintypes.RECT()
    if dwm.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(r), ctypes.sizeof(r)) != 0:  # EXTENDED_FRAME
        return None
    return (r.left, r.top, r.right, r.bottom) if r.right - r.left > 0 and r.bottom - r.top > 0 else None


def top_level_windows(stop_at_hwnd: int | None = None) -> list[tuple[int, Rect]]:
    """(hwnd, physical rect) of the windows the duck can sit on, topmost first: visible, not minimized, not
    cloaked, not tool windows (the taskbar is one, and is always kept), not this process's. When `stop_at_hwnd`
    is set and still visible, enumeration stops once it is added."""
    u = _user32()
    me = os.getpid()
    found: list[tuple[int, Rect]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd, _lp):
        if not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
            return True
        pid = wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != me:
            ex = u.GetWindowLongW(hwnd, -20)
            skip = ex & 0xA0  # WS_EX_TOOLWINDOW, WS_EX_TRANSPARENT (click-through overlays show nothing to sit on)
            cls = _class(u, hwnd)
            if (not skip and cls not in ("Progman", "WorkerW")) or cls in TASKBAR_CLASSES:  # not the desktop
                rect = _physical_rect(u, hwnd)
                if rect is not None:
                    found.append((int(hwnd), rect))
                    if stop_at_hwnd is not None and int(hwnd) == stop_at_hwnd:
                        return False
        return True

    u.EnumWindows(visit, 0)
    return found


def is_taskbar(hwnd: int) -> bool:
    """True when `hwnd` is the Windows taskbar (where caption-button right reserve does not apply)."""
    return _class(_user32(), hwnd) in TASKBAR_CLASSES


def window_rect(hwnd: int) -> Rect | None:
    """Physical frame of a perch window, or None once it is closed, minimized or cloaked."""
    return _physical_rect(_user32(), hwnd)


def to_logical(rect: Rect, screens: Sequence[tuple[Rect, float]]) -> Rect:
    """Physical px to Qt logical px. `screens` is (logical geometry, device pixel ratio) per monitor; Qt keeps
    each screen's top-left at its physical position and scales from there, so map through the screen holding the
    rect's top-left (nearest by origin when none does)."""
    def native(s: tuple[Rect, float]) -> Rect:
        (l, t, r, b), dpr = s
        return l, t, l + round((r - l) * dpr), t + round((b - t) * dpr)
    if not screens:
        return rect
    hit = [s for s in screens if _inside(native(s), rect[0], rect[1])]
    (l, t, _, _), dpr = hit[0] if hit else min(screens, key=lambda s: abs(s[0][0] - rect[0]) + abs(s[0][1] - rect[1]))
    f = lambda v, o: o + round((v - o) / dpr)  # noqa: E731
    return f(rect[0], l), f(rect[1], t), f(rect[2], l), f(rect[3], t)
