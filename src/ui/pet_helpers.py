"""Module-level helpers of the pet window: screen geometry, topmost and fullscreen checks, activity clips, icons."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtGui import QIcon, QPixmap

from src.graphics.duck import render_icon
from src.tools import computer


@dataclass(frozen=True)
class ScreenRect:
    """Pure value representation of an available desktop screen rectangle."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def contains_point(self, px: int, py: int) -> bool:
        return self.x <= px < self.right and self.y <= py < self.bottom

    def distance_sq_to_point(self, px: int, py: int) -> int:
        dx = 0
        if px < self.x:
            dx = self.x - px
        elif px > self.right:
            dx = px - self.right

        dy = 0
        if py < self.y:
            dy = self.y - py
        elif py > self.bottom:
            dy = py - self.bottom
        return dx * dx + dy * dy


def yield_topmost(own: set[int], u32: object | None = None) -> bool:
    """True while re-raising the pet would cover something the user is working with.

    That is: a mouse button is held (dragging a window into a snap zone), or the foreground window or the
    top-level window under the cursor is someone else's topmost window (Snap Assist, Alt+Tab, Task View,
    Start, the snap layouts flyout, other apps' always-on-top windows).
    """
    if u32 is None:
        u32 = ctypes.windll.user32
        u32.WindowFromPoint.argtypes = [wintypes.POINT]
        for fn in (u32.WindowFromPoint, u32.GetForegroundWindow, u32.GetAncestor):
            fn.restype = ctypes.c_void_p
        u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        u32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    if any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02, 0x04)):  # left, right, middle button
        return True
    pt = wintypes.POINT()
    under = u32.GetAncestor(u32.WindowFromPoint(pt), 2) if u32.GetCursorPos(ctypes.byref(pt)) else None  # GA_ROOT
    return any(hwnd and hwnd not in own and u32.GetWindowLongW(hwnd, -20) & 0x8  # GWL_EXSTYLE, WS_EX_TOPMOST
               for hwnd in (u32.GetForegroundWindow(), under))


_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def fullscreen_in_front(own: set[int], pet_hwnd: int) -> bool:
    """True while someone else's window fills the pet's whole monitor and has focus (a borderless game, a video,
    a slideshow). Those are rarely topmost, so yield_topmost misses them and the pet popped over them every tick."""
    fg = computer.foreground_window()
    if not fg or fg in own or computer.window_class(fg) in _SHELL_CLASSES:  # the desktop is monitor-sized too
        return False
    front, pet = computer.window_on_monitor(fg), computer.window_on_monitor(pet_hwnd)
    return front is not None and pet is not None and front[0] == pet[0] and front[1] == (0, 0, *front[2])


# What the duck acts out while the chat model runs a tool (body language); anything else reads as thinking
READING_TOOLS = frozenset({"list_files", "read_file", "find_files", "search_text", "read_clipboard", "read_screen",
                           "screen_map", "preview_document", "load_skill", "read_background", "pet_status"})
WORKING_TOOLS = frozenset({"run_command", "run_powershell", "start_background", "edit_file", "edit_files", "mouse_click", "type_text",
                           "press_key", "scroll", "computer_batch", "save_file"})
LOOKING_TOOLS = frozenset({"web_search", "fetch_page", "open_link", "find_mcp_servers"})


def activity_clip(tool: str) -> str:
    """The base clip for what the chat model does now: reading, typing, looking around, or waiting on the user."""
    if tool == "approval":
        return "wait"
    if tool in WORKING_TOOLS:
        return "type"
    if tool in LOOKING_TOOLS or "__" in tool:  # web tools and MCP servers (server__tool)
        return "scan"
    return "focus"  # reading tools and plain thinking: head over its work


def clamp_position_to_screens(
    x: int,
    y: int,
    width: int,
    height: int,
    screens: Sequence[ScreenRect],
) -> tuple[int, int]:
    """Clamp window top-left (x, y) across adjacent monitors or to the nearest screen."""
    if not screens:
        return (x, y)

    right_edge = x + width - 1
    bottom_edge = y + height - 1

    # Allow smooth straddling across horizontally adjacent monitors
    for left_scr in screens:
        if left_scr.x <= x < left_scr.right and right_edge >= left_scr.right:
            for right_scr in screens:
                if (
                    right_scr is not left_scr
                    and left_scr.right == right_scr.x
                    and right_scr.x <= right_edge < right_scr.right
                ):
                    shared_top = max(left_scr.y, right_scr.y)
                    shared_bot = min(left_scr.bottom, right_scr.bottom)
                    if shared_bot - shared_top >= height:
                        return (x, max(shared_top, min(shared_bot - height, y)))

    # Allow smooth straddling across vertically adjacent monitors
    for top_scr in screens:
        if top_scr.y <= y < top_scr.bottom and bottom_edge >= top_scr.bottom:
            for bot_scr in screens:
                if (
                    bot_scr is not top_scr
                    and top_scr.bottom == bot_scr.y
                    and bot_scr.y <= bottom_edge < bot_scr.bottom
                ):
                    shared_left = max(top_scr.x, bot_scr.x)
                    shared_right = min(top_scr.right, bot_scr.right)
                    if shared_right - shared_left >= width:
                        return (max(shared_left, min(shared_right - width, x)), y)

    cx = x + width // 2
    cy = y + height // 2

    target: ScreenRect | None = None
    for scr in screens:
        if scr.contains_point(cx, cy):
            target = scr
            break

    if target is None:
        target = min(screens, key=lambda s: s.distance_sq_to_point(cx, cy))

    max_x = max(target.x, target.right - width)
    max_y = max(target.y, target.bottom - height)
    clamped_x = max(target.x, min(max_x, x))
    clamped_y = max(target.y, min(max_y, y))
    return (clamped_x, clamped_y)


def build_app_icon(size: int = 64) -> QIcon:
    """App and tray icon: the resting duck filling the icon square."""
    return QIcon(QPixmap.fromImage(render_icon(size)))


def export_windows_ico(
    output_path: Path | str,
    sizes: Sequence[int] = (16, 32, 48, 64, 128, 256),
) -> Path:
    """Write a multi-resolution Windows .ico file, each size rendered sharp from the vector duck."""
    from PyQt6.QtCore import QBuffer, QIODevice

    dest = Path(output_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    png_blobs: list[tuple[int, bytes]] = []
    for sz in sizes:
        scaled = render_icon(sz)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled.save(buf, "PNG")
        png_blobs.append((sz, bytes(buf.data())))

    count = len(png_blobs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + (16 * count)
    entries = bytearray()
    payload = bytearray()

    for sz, blob in png_blobs:
        dim_byte = 0 if sz >= 256 else sz
        entries.extend(
            struct.pack(
                "<BBBBHHII",
                dim_byte,
                dim_byte,
                0,
                0,
                1,
                32,
                len(blob),
                offset,
            )
        )
        payload.extend(blob)
        offset += len(blob)

    dest.write_bytes(header + bytes(entries) + bytes(payload))
    return dest


VK_VOLUME_MUTE = 0xAD
KEYEVENTF_KEYUP = 0x0002
