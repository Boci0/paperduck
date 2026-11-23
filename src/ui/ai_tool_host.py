"""Runs the AI chat model's PC tools (src/tools/ai_tools.py) for one PetWindow.

`run` is called on the chat's worker thread. File tools and OCR run right there; anything touching Qt
(clipboard, screen grab, Save dialog, pet actions) is queued to the Qt thread and the worker waits for its result.
Tools of the user's MCP servers (src/tools/mcp_client.py) join the specs; each server's first call in a chat
waits for the user to allow it in the chat.
edit_file, edit_files, run_command, start_background, add_mcp_server, add_skill, update_skill, remember and forget
also wait for the user's Allow in the chat (once, or for the whole chat). Background processes (`procs`) are
killed when the app quits.
"""

from __future__ import annotations

import ctypes
import json
import shutil
import tempfile
import threading
import time
from datetime import date
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QCoreApplication, QMimeData, QObject, QRect, QStandardPaths, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QCursor, QFont, QGuiApplication, QImage, QPainter, QPen, QTextDocument
from PyQt6.QtWidgets import QFileDialog

from src.tools import computer, login_vault
from src.tools.ai_assist import ask, fit_thinking, load_key, review_action, tool_note
from src.tools.ai_sessions import _clean as _clean_secrets
from src.tools.agents import AGENT_PROMPT, AGENT_SPECS, MAX_AGENT_CALLS, Agents, ReadOnlyHost
from src.tools.ai_tools import (calculate, check_layout, find_control, COMPUTER_TOOLS, MAX_BATCH_STEPS, MAX_SAVE_CHARS, MAX_WAIT_MS, resolve_target, FILE_TOOLS, PET_ACTIONS, TOOL_SPECS, BackgroundProcs, apply_edits,
                                computer_action, destructive_reason, scratch_dir, find_skills, is_secret, load_skill, memory_path, parse_boxes, plan_edit,
                                plan_edits, plan_memory, plan_skill_update, print_to_pdf_paths, resolve,
                                PDF_WAIT_S, changed_media, file_stamp, media_stamps, wait_for_new_file, run_command, command_timeout, powershell_file, powershell_error_note, run_file_tool, save_args,
                                skill_text, skills_dir, task_list, write_memory, write_skill)
from src.tools.errands import describe as describe_errand
from src.tools.clip_vault import clipboard_is_private
from src.tools.doc_preview import preview_document
from src.tools.file_forge import scrub_metadata
from src.tools.macros import parse_recipe
from src.tools.mcp_client import SEP, McpHub, add_server, env_summary, find_servers, safe_name
from src.tools.watch import MESSAGE_APPS, READERS
from src.tools.web_drop import decode_image_bytes, load_image_file
from src.ui.quick_tools import ocr_image_file

GUI_TIMEOUT_S = 15.0
SAVE_TIMEOUT_S = 600.0  # the Save dialog waits on the user
APPROVE_TIMEOUT_S = 300.0  # the user reads the MCP confirm, a diff or a command in the chat
# Allowed-for-this-chat keys that ask again once the chat holds web, MCP or agent content: what an injected
# instruction could use to act on the PC. MCP calls stay allowed (a browser server would ask on every click).
# Apps whose window on screen shows content from outside the PC: a screen read with one visible arms the guard too
OUTSIDE_APPS = READERS | MESSAGE_APPS
OUTSIDE_GUARDED = frozenset({"run_command", "start_background", "computer", "edit_file", "edit_outside", "edit_scratch", "memory", "add_skill",
                             "update_skill", "add_mcp_server"})
# Auto mode approves these at once (edits inside the Project Folder, memory notes) unless they look destructive or the
# chat holds outside content; every other approval goes to the reviewer (`review_action`) first.
AUTO_INSTANT = frozenset({"edit_file", "memory"})

LAPTOP_CHAT_SPEC: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "laptop_chat",
        "description": "Read the live desktop Laptop chat's status and recent messages (action='read'), send or steer a message to it (action='send'), or stop its active reply (action='stop').",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["read", "send", "stop"], "description": "Operation to perform on the desktop Laptop chat."},
                "message": {"type": "string", "description": "Message text to send or steer when action='send'."},
            },
            "required": ["action"],
        },
    },
}


def auto_mode(win: Any) -> bool:
    return bool(getattr(getattr(getattr(win, "_ai_chat", None), "_plugin", None), "auto", False))


_GUI_CONTROL_LOCK = threading.Lock()


def _given(args: dict[str, Any]) -> dict[str, Any]:
    """Arguments the model really gave: weaker models send null or "" for an optional one they mean to omit."""
    return {k: v for k, v in args.items() if v is not None and v != ""}


ERRAND_APPROVE_S = 1800.0  # an errand runs while the user may be away, so its approvals wait longer
MAX_CLIP_CHARS = 12000
MAX_SCREEN_CHARS = 8000
MAX_SHARED_NOTE_CHARS = 1500  # OCR text of shared pictures added to a sub-agent's task (the task caps at 4000)
MAX_OCR_SIDE = 4096  # longer sides are scaled down for OCR (Windows OCR refuses very large bitmaps)


def _rel_name(path: Path, base: Path) -> str:
    try:
        return str(path.relative_to(base))
    except ValueError:
        return str(path)


def _strings(value: Any) -> list[str]:
    """Every string value inside nested JSON (keys left out); a loop, so deep nesting cannot overflow the stack."""
    found, todo = [], [value]
    while todo:
        item = todo.pop()
        if isinstance(item, str):
            found.append(item)
        elif isinstance(item, dict):
            todo.extend(item.values())
        elif isinstance(item, list):
            todo.extend(item)
    return found


def _front_window_shot(hwnd: int) -> QImage | None:
    """Only window `hwnd`, cut from a grab of the monitor it is on (None when that fails)."""
    found = computer.window_on_monitor(hwnd)
    if found is None:
        return None
    device, (left, top, right, bottom), (mon_w, mon_h) = found
    screen = next((s for s in QGuiApplication.screens() if s.name() == device), None)
    image = screen.grabWindow(0).toImage() if screen is not None else None
    if image is None or image.isNull() or not mon_w or not mon_h:
        return None
    sx, sy = image.width() / mon_w, image.height() / mon_h  # the grab may be in logical, not physical, pixels
    return image.copy(round(left * sx), round(top * sy), round((right - left) * sx), round((bottom - top) * sy))


def _ours(win: Any) -> list[Any]:
    """The pet's own windows on screen (pet, chat card, its agent sheets): left out of shots and clicked through."""
    chat = win._ai_chat
    sheets = getattr(getattr(chat, "_sheets", None), "sheets", [])
    return [w for w in (win, chat, *sheets) if w is not None and w.isVisible()]


def show_quietly(w: Any) -> None:
    """Show without taking the keyboard from the app the user (or the model) is in."""
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    w.show()
    w.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, False)


def grab_screen(win: Any, done: Callable[[Any], None], primary: bool = False, window: bool = False) -> None:
    """Grab the screen under the cursor (or the primary one, with the front app's handle; or with `window`,
    just the front app's window) on the Qt thread, with the pet `win` and its chat left out of the shot.
    They stay on screen (excluded from capture); only where Windows cannot exclude them are they hidden."""
    if login_vault.typed_password_shown():  # a typed password shown in plain text never leaves in a shot
        done((None, 0) if primary else None)
        return
    shown = _ours(win)
    hidden = [w for w in shown if not computer.exclude_from_capture(int(w.winId()), True)]
    for w in hidden:
        w.hide()

    def grab() -> None:
        hwnd = computer.focus_front_app() if primary else computer.front_app()  # the app under the pet and chat
        image = _front_window_shot(hwnd) if window else None
        if image is None:
            screen = (None if primary else QGuiApplication.screenAt(QCursor.pos())) or QGuiApplication.primaryScreen()
            image = screen.grabWindow(0).toImage() if screen is not None else None
        for w in shown:
            if w in hidden:
                show_quietly(w)
            else:
                computer.exclude_from_capture(int(w.winId()), False)
        done((image, hwnd) if primary else image)

    QTimer.singleShot(180 if hidden else 30, grab)  # the compositor applies the affinity on its next frame


SPOT_R = 16  # half the side of the square compared around a click
ROW_LEFT, ROW_RIGHT, ROW_R = 700, 300, 10  # the strip across the click's row: a list row's label sits to the left


def _rgb32_bytes(image: QImage, rect: QRect) -> tuple[bytes, int, int, int]:
    """(RGB32 bytes, stride, width, height) of `rect` inside `image` (B, G, R, A per pixel)."""
    part = image.copy(rect).convertToFormat(QImage.Format.Format_RGB32)
    bits = part.constBits()
    bits.setsize(part.sizeInBytes())
    return bytes(bits), part.bytesPerLine(), part.width(), part.height()


def _luma(r: int, g: int, b: int) -> float:
    def ch(v: int) -> float:
        x = v / 255.0
        return x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def page_color(image: QImage, rect: QRect) -> int:
    """The commonest colour (0xRRGGBB) on a coarse grid inside `rect`: a design page's background."""
    box = rect.intersected(image.rect())
    if box.isEmpty():
        return 0xFFFFFF
    data, stride, w, h = _rgb32_bytes(image, box)
    counts: dict[int, int] = {}
    best_bucket, best_count, sample = 0xF0F0F0, 0, 0xFFFFFF
    first_in: dict[int, int] = {}
    for y in range(0, h, 6):
        row = y * stride
        for x in range(0, w, 6):
            j = row + 4 * x
            r, g, b = data[j + 2], data[j + 1], data[j]
            bucket = ((r & 0xF0) << 16) | ((g & 0xF0) << 8) | (b & 0xF0)
            if bucket not in first_in:
                first_in[bucket] = (r << 16) | (g << 8) | b
            cnt = counts.get(bucket, 0) + 1
            counts[bucket] = cnt
            if cnt > best_count:
                best_bucket, best_count, sample = bucket, cnt, first_in[bucket]
    return sample


def snap_page(image: QImage, given: QRect) -> QRect | None:
    """The page itself around a rough box from the model: the rows and columns, holding the box's middle, where the
    page colour fills at least 40% of the box (gaps under 15% of it, such as a coloured band, are bridged). The box as
    given when that finds less than half of it (a full-bleed picture); None for a box under 16 px."""
    if given.width() < 16 or given.height() < 16:
        return None
    bg = page_color(image, given)
    br, bg_g, bb = (bg >> 16) & 255, (bg >> 8) & 255, bg & 255
    pad_x, pad_y = max(24, given.width() // 10), max(24, given.height() // 10)
    win = given.adjusted(-pad_x, -pad_y, pad_x, pad_y).intersected(image.rect())
    if win.isEmpty():
        return None
    data, stride, _w, _h = _rgb32_bytes(image, win)
    gx0, gx1 = max(0, given.left() - win.left()), min(win.width() - 1, given.right() - win.left())
    gy0, gy1 = max(0, given.top() - win.top()), min(win.height() - 1, given.bottom() - win.top())
    xs = range(gx0, gx1 + 1, 4)
    ys = range(gy0, gy1 + 1, 4)
    need_x, need_y = 0.4 * len(xs), 0.4 * len(ys)

    def near(j: int) -> bool:
        return abs(data[j + 2] - br) <= 12 and abs(data[j + 1] - bg_g) <= 12 and abs(data[j] - bb) <= 12

    def row_ok(y: int) -> bool:
        base = (y - win.top()) * stride
        return sum(near(base + 4 * x) for x in xs) >= need_x

    def col_ok(x: int) -> bool:
        col = 4 * (x - win.left())
        return sum(near(y * stride + col) for y in ys) >= need_y

    def span(ok: Callable[[int], bool], lo: int, hi: int, size: int) -> tuple[int, int]:
        good = [v for v in range(lo, hi + 1, 2) if ok(v)]
        mid = (lo + hi) // 2
        if not good:
            return lo, hi
        start = min(good, key=lambda v: abs(v - mid))
        a = b = start
        for v in sorted(g for g in good if g < start)[::-1]:  # walk out from the middle, bridging short gaps
            if a - v > 0.15 * size:
                break
            a = v
        for v in sorted(g for g in good if g > start):
            if v - b > 0.15 * size:
                break
            b = v
        return a, b

    top, bottom = span(row_ok, win.top(), win.bottom(), given.height())
    left, right = span(col_ok, win.left(), win.right(), given.width())
    found = QRect(left, top, right - left + 1, bottom - top + 1)
    return found if found.width() * found.height() >= 0.5 * given.width() * given.height() else given


def _diff(old: QImage, new: QImage, rect: QRect) -> tuple[int, int]:
    """(pixels that differ by more than 48 in a channel, columns holding any of them) inside `rect`."""
    a, b = (img.copy(rect).convertToFormat(QImage.Format.Format_RGB32) for img in (old, new))
    pa, pb = a.constBits(), b.constBits()
    pa.setsize(a.sizeInBytes())
    pb.setsize(b.sizeInBytes())
    da, db, stride = bytes(pa), bytes(pb), a.bytesPerLine()
    changed, cols = 0, set()
    for row in range(a.height()):
        i = row * stride
        if da[i:i + stride] == db[i:i + stride]:
            continue
        for x in range(a.width()):
            j = i + 4 * x
            if max(abs(da[j] - db[j]), abs(da[j + 1] - db[j + 1]), abs(da[j + 2] - db[j + 2])) > 48:
                changed += 1
                cols.add(x)
    return changed, len(cols)


def spot_changed(old: QImage, new: QImage, x: int, y: int) -> bool:
    """True when the click at (x, y) could hit something else than the model saw: a fifth of the square around it
    changed (moved, scrolled, re-rendered, covered), or text along its row did, so an identical button there now
    belongs to another row. A blinking caret (under 6 columns) or a hover tint does not count."""
    if old.size() != new.size():
        return True
    square = QRect(x - SPOT_R, y - SPOT_R, 2 * SPOT_R + 1, 2 * SPOT_R + 1).intersected(old.rect())
    if square.isEmpty():
        return False
    changed, _cols = _diff(old, new, square)
    if changed * 5 > square.width() * square.height():
        return True
    row = QRect(x - ROW_LEFT, y - ROW_R, ROW_LEFT + ROW_RIGHT + 1, 2 * ROW_R + 1).intersected(old.rect())
    return _diff(old, new, row)[1] >= 6


FOLLOW_PX = 300  # how far up or down a moved target is looked for
RULER_LEFT = 36
RULER_TOP = 20
MAX_SOM_BADGES = 120
SOM_MAX_AREA_RATIO = 0.65
MAX_SAMPLE_POINTS = 24
MAX_PALETTE_SWATCHES = 12


def changed_region(old: QImage, new: QImage, step: int = 2) -> tuple[int, int, int, int, int] | None:
    """(x, y, w, h, pct) of pixels that changed between `old` and `new`, or (0, 0, 0, 0, 0) when unchanged (< 4 hits).
    Returns None when either image is null or their sizes differ."""
    if old.isNull() or new.isNull() or old.size() != new.size():
        return None
    a, b = old.convertToFormat(QImage.Format.Format_RGB32), new.convertToFormat(QImage.Format.Format_RGB32)
    pa, pb = a.constBits(), b.constBits()
    pa.setsize(a.sizeInBytes())
    pb.setsize(b.sizeInBytes())
    da, db, stride = bytes(pa), bytes(pb), a.bytesPerLine()
    w, h = a.width(), a.height()
    row_bytes = w * 4
    min_x, min_y, max_x, max_y, hits = w, h, -1, -1, 0
    for y in range(0, h, step):
        i = y * stride
        if da[i:i + row_bytes] == db[i:i + row_bytes]:
            continue
        for x in range(0, w, step):
            j = i + 4 * x
            if max(abs(da[j] - db[j]), abs(da[j + 1] - db[j + 1]), abs(da[j + 2] - db[j + 2])) > 24:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
                hits += 1
    if hits < 4:
        return (0, 0, 0, 0, 0)
    bw = min(w, max_x + step) - min_x
    bh = min(h, max_y + step) - min_y
    total = max(1, ((w + step - 1) // step) * ((h + step - 1) // step))
    return (min_x, min_y, bw, bh, round(hits * 100 / total))


def diff_note(old: QImage, new: QImage) -> str:
    """One-sentence post-action pixel-change summary for the model."""
    res = changed_region(old, new)
    if res is None:
        return ""
    x, y, w, h, pct = res
    if w == 0:
        return " Screen unchanged (0 pixels changed: target may have missed, lacked focus, or stayed in the same state)."
    share = "<1%" if pct == 0 else f"~{pct}%"
    return f" Screen changed in box {x},{y},{w}x{h} ({share} of screen)."


def _ruler_step(span_px: int, out_px: int, min_gap: int) -> int:
    scale = out_px / max(1, span_px)
    for s in (25, 50, 100, 200, 250, 400, 500, 1000):
        if s * scale >= min_gap:
            return s
    return 1000


def render_screen_overlay(
    image: QImage,
    origin: tuple[int, int] = (0, 0),
    items: dict[int, tuple[int, int, int, int, str]] | None = None,
    ocr_ids: set[int] | None = None,
    grid: bool = False,
) -> QImage:
    """Draw outer top/left true-screen-pixel coordinate rulers, optional coordinate grid lines, and optional
    Set-of-Mark #id boxes/badges onto a copy of `image`, pre-scaled so total dimensions fit MAX_IMAGE_SIDE."""
    from src.ui.chat_widgets import MAX_IMAGE_SIDE

    max_w, max_h = MAX_IMAGE_SIDE - RULER_LEFT, MAX_IMAGE_SIDE - RULER_TOP
    scaled = (image.scaled(max_w, max_h, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
              if image.width() > max_w or image.height() > max_h else image)
    sx = scaled.width() / max(1, image.width())
    sy = scaled.height() / max(1, image.height())
    ox, oy = origin

    canvas = QImage(scaled.width() + RULER_LEFT, scaled.height() + RULER_TOP, QImage.Format.Format_ARGB32)
    canvas.fill(QColor("#1c212b"))
    has_app = QGuiApplication.instance() is not None
    painter = QPainter(canvas)
    try:
        painter.drawImage(RULER_LEFT, RULER_TOP, scaled)
        sep_pen = QPen(QColor("#3c4657"), 1)
        painter.setPen(sep_pen)
        painter.drawLine(RULER_LEFT - 1, 0, RULER_LEFT - 1, canvas.height() - 1)
        painter.drawLine(0, RULER_TOP - 1, canvas.width() - 1, RULER_TOP - 1)

        font = None
        if has_app:
            font = QFont("Consolas", 8)
            font.setStyleHint(QFont.StyleHint.Monospace)
            painter.setFont(font)
            painter.setPen(QColor("#94a3b8"))
            painter.drawText(QRect(2, 2, RULER_LEFT - 6, RULER_TOP - 4),
                             Qt.AlignmentFlag.AlignCenter, "px")

        minor_pen = QPen(QColor("#64748b"), 1)
        major_pen = QPen(QColor("#cbd5e1"), 1)
        grid_pen = QPen(QColor(0, 175, 230, 75), 1)
        text_color = QColor("#f1f5f9")

        # X ruler (top)
        step_x = _ruler_step(image.width(), scaled.width(), 72)
        minor_x = max(1, step_x // 2)
        start_vx = (ox // minor_x) * minor_x
        if start_vx < ox:
            start_vx += minor_x
        first_major_px: int | None = None
        vx = start_vx
        while vx < ox + image.width():
            px = RULER_LEFT + round((vx - ox) * sx)
            if RULER_LEFT <= px < canvas.width():
                if vx % step_x == 0:
                    if first_major_px is None:
                        first_major_px = px
                    painter.setPen(major_pen)
                    painter.drawLine(px, RULER_TOP - 8, px, RULER_TOP - 1)
                    if grid:
                        painter.setPen(grid_pen)
                        painter.drawLine(px, RULER_TOP, px, canvas.height() - 1)
                    if has_app:
                        painter.setPen(text_color)
                        tx = min(px + 2, max(RULER_LEFT, canvas.width() - 34))
                        painter.drawText(QRect(tx, 1, 34, RULER_TOP - 8),
                                         Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(vx))
                else:
                    painter.setPen(minor_pen)
                    painter.drawLine(px, RULER_TOP - 4, px, RULER_TOP - 1)
            vx += minor_x
        if has_app and (first_major_px is None or first_major_px > RULER_LEFT + 34):
            painter.setPen(text_color)
            painter.drawText(QRect(RULER_LEFT + 2, 1, 34, RULER_TOP - 8),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(ox))

        # Y ruler (left)
        step_y = _ruler_step(image.height(), scaled.height(), 56)
        minor_y = max(1, step_y // 2)
        start_vy = (oy // minor_y) * minor_y
        if start_vy < oy:
            start_vy += minor_y
        first_major_py: int | None = None
        vy = start_vy
        while vy < oy + image.height():
            py = RULER_TOP + round((vy - oy) * sy)
            if RULER_TOP <= py < canvas.height():
                if vy % step_y == 0:
                    if first_major_py is None:
                        first_major_py = py
                    painter.setPen(major_pen)
                    painter.drawLine(RULER_LEFT - 8, py, RULER_LEFT - 1, py)
                    if grid:
                        painter.setPen(grid_pen)
                        painter.drawLine(RULER_LEFT, py, canvas.width() - 1, py)
                    if has_app:
                        painter.setPen(text_color)
                        ty = max(RULER_TOP, min(py - 6, canvas.height() - 13))
                        painter.drawText(QRect(0, ty, RULER_LEFT - 9, 12),
                                         Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(vy))
                else:
                    painter.setPen(minor_pen)
                    painter.drawLine(RULER_LEFT - 4, py, RULER_LEFT - 1, py)
            vy += minor_y
        if has_app and (first_major_py is None or first_major_py > RULER_TOP + 24):
            painter.setPen(text_color)
            painter.drawText(QRect(0, RULER_TOP + 1, RULER_LEFT - 9, 12),
                             Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(oy))

        # Optional Set-of-Mark #id boxes and badges
        if items:
            vp = QRect(RULER_LEFT, RULER_TOP, scaled.width(), scaled.height())
            painter.setClipRect(vp)
            ocr_set = ocr_ids or set()
            ordered = sorted(items.items(), key=lambda kv: (kv[0] in ocr_set, kv[0]))
            placed: list[QRect] = []
            badges = 0
            vp_area = max(1, vp.width() * vp.height())
            if has_app and font is not None:
                badge_font = QFont(font)
                badge_font.setPointSize(7)
                badge_font.setBold(True)
                painter.setFont(badge_font)

            for n, (ix, iy, iw, ih, label) in ordered:
                px = RULER_LEFT + round((ix - ox) * sx)
                py = RULER_TOP + round((iy - oy) * sy)
                pw = max(2, round(iw * sx))
                ph = max(2, round(ih * sy))
                box = QRect(px, py, pw, ph)
                if box.intersected(vp).isEmpty() or pw * ph > SOM_MAX_AREA_RATIO * vp_area:
                    continue
                is_ocr = n in ocr_set
                is_icon = not is_ocr and label == "(unlabeled)"
                border_col = (QColor(245, 158, 11, 200) if is_ocr
                              else QColor(192, 132, 252, 220) if is_icon
                              else QColor(0, 180, 216, 215))
                fill_col = (QColor(92, 45, 12, 235) if is_ocr
                            else QColor(74, 29, 106, 235) if is_icon
                            else QColor(11, 57, 84, 235))
                painter.setPen(QPen(border_col, 1))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(px, py, pw - 1, ph - 1)

                if badges < MAX_SOM_BADGES:
                    tag = f"#{n}"
                    bw = max(16, len(tag) * 5 + 5)
                    bh = 11
                    candidates = [
                        QRect(px, py - bh, bw, bh),
                        QRect(px, py, bw, bh),
                        QRect(px + pw - bw, py - bh, bw, bh),
                        QRect(px, py + ph, bw, bh),
                    ]
                    clamped = [
                        QRect(
                            max(vp.left(), min(c.x(), vp.right() - bw + 1)),
                            max(vp.top(), min(c.y(), vp.bottom() - bh + 1)),
                            bw,
                            bh,
                        )
                        for c in candidates
                    ]
                    chosen = next((c for c in clamped if not any(c.intersects(p) for p in placed)), None)
                    if chosen is not None:
                        painter.setPen(QPen(border_col, 1))
                        painter.setBrush(fill_col)
                        painter.drawRoundedRect(chosen, 3, 3)
                        if has_app:
                            painter.setPen(QColor("#ffffff"))
                            painter.drawText(chosen, Qt.AlignmentFlag.AlignCenter, tag)
                        placed.append(chosen)
                        badges += 1
    finally:
        painter.end()
    return canvas


def compose_side_by_side(before: QImage, after: QImage) -> QImage:
    """Render `before` and `after` side-by-side with BEFORE / AFTER headers, scaled to fit MAX_IMAGE_SIDE."""
    from src.ui.chat_widgets import MAX_IMAGE_SIDE

    header_h, gap = 22, 4
    max_panel_w = (MAX_IMAGE_SIDE - gap) // 2
    max_panel_h = MAX_IMAGE_SIDE - header_h
    before_s = (before.scaled(max_panel_w, max_panel_h, Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                if before.width() > max_panel_w or before.height() > max_panel_h else before)
    after_s = (after.scaled(before_s.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
               if after.size() != before_s.size() else after)
    pw, ph = max(1, before_s.width()), max(1, before_s.height())
    canvas = QImage(pw * 2 + gap, ph + header_h, QImage.Format.Format_ARGB32)
    canvas.fill(QColor("#1c212b"))
    has_app = QGuiApplication.instance() is not None
    painter = QPainter(canvas)
    try:
        painter.drawImage(0, header_h, before_s)
        painter.drawImage(pw + gap, header_h, after_s)
        painter.setPen(QPen(QColor("#475569"), 1))
        painter.drawLine(pw + gap // 2, 0, pw + gap // 2, canvas.height() - 1)
        if has_app:
            font = QFont("Consolas", 9)
            font.setBold(True)
            painter.setFont(font)
            painter.setPen(QColor("#f1f5f9"))
            painter.drawText(QRect(0, 0, pw, header_h), Qt.AlignmentFlag.AlignCenter, "BEFORE")
            painter.drawText(QRect(pw + gap, 0, pw, header_h), Qt.AlignmentFlag.AlignCenter, "AFTER")
    finally:
        painter.end()
    return canvas


def analyze_colors(image: QImage, points: Any = None, region: Any = None, top: Any = 6) -> str:
    """Sample exact #RRGGBB colors at `points` and/or dominant palette swatches inside `region`."""
    if points is None and region is None:
        return "error: give points, region, or both"
    if isinstance(top, bool) or not isinstance(top, int) or not (1 <= top <= MAX_PALETTE_SWATCHES):
        return f"error: top must be an integer from 1 to {MAX_PALETTE_SWATCHES}"

    sections: list[str] = []
    if points is not None:
        if (not isinstance(points, list) or not (1 <= len(points) <= MAX_SAMPLE_POINTS)
                or not all(isinstance(p, (list, tuple)) and len(p) == 2
                           and not isinstance(p[0], bool) and not isinstance(p[1], bool)
                           and isinstance(p[0], int) and isinstance(p[1], int) for p in points)):
            return f"error: points must be 1 to {MAX_SAMPLE_POINTS} [x, y] pairs"
        rgb_img = image.convertToFormat(QImage.Format.Format_RGB32)
        bits = rgb_img.constBits()
        bits.setsize(rgb_img.sizeInBytes())
        data, stride = bytes(bits), rgb_img.bytesPerLine()
        iw, ih = rgb_img.width(), rgb_img.height()
        pt_lines = ["Point samples:"]
        for x, y in points:
            if not (0 <= x < iw and 0 <= y < ih):
                pt_lines.append(f"- ({x}, {y}): off screen")
                continue
            j = y * stride + 4 * x
            r, g, b = data[j + 2], data[j + 1], data[j]
            sr = sg = sb = cnt = 0
            for ny in range(max(0, y - 1), min(ih, y + 2)):
                row = ny * stride
                for nx in range(max(0, x - 1), min(iw, x + 2)):
                    k = row + 4 * nx
                    sr += data[k + 2]
                    sg += data[k + 1]
                    sb += data[k]
                    cnt += 1
            ar, ag, ab = round(sr / cnt), round(sg / cnt), round(sb / cnt)
            luma = _luma(r, g, b)
            tone = "dark" if luma < 0.18 else "mid" if luma < 0.65 else "light"
            exact = f"#{r:02x}{g:02x}{b:02x}"
            avg = f"#{ar:02x}{ag:02x}{ab:02x}"
            extra = f"3x3 avg {avg}, " if avg != exact else ""
            pt_lines.append(f"- ({x}, {y}): {exact} ({extra}luma {luma:.2f} {tone})")
        sections.append("\n".join(pt_lines))

    if region is not None:
        if (not isinstance(region, list) or len(region) != 4
                or any(isinstance(v, bool) or not isinstance(v, int) for v in region)):
            return "error: region is [left, top, width, height] in screen pixels"
        rect = QRect(*region).intersected(image.rect())
        if rect.width() < 4 or rect.height() < 4:
            return "error: the region is too small or off the screen"
        data, stride, rw, rh = _rgb32_bytes(image, rect)
        step = max(1, int(((rw * rh) / 4000) ** 0.5))
        buckets: dict[tuple[int, int, int], list[int]] = {}
        total = 0
        for y in range(0, rh, step):
            row = y * stride
            for x in range(0, rw, step):
                j = row + 4 * x
                r, g, b = data[j + 2], data[j + 1], data[j]
                key = (r >> 4, g >> 4, b >> 4)
                acc = buckets.get(key)
                if acc is None:
                    buckets[key] = [1, r, g, b]
                else:
                    acc[0] += 1
                    acc[1] += r
                    acc[2] += g
                    acc[3] += b
                total += 1
        ranked = sorted(buckets.values(), key=lambda v: v[0], reverse=True)[:top]
        swatches = []
        for cnt, sr, sg, sb in ranked:
            mr, mg, mb = round(sr / cnt), round(sg / cnt), round(sb / cnt)
            luma = _luma(mr, mg, mb)
            swatches.append((f"#{mr:02x}{mg:02x}{mb:02x}", cnt * 100.0 / max(1, total), luma))
        lumas = [s[2] for s in swatches]
        min_l, max_l = min(lumas), max(lumas)
        contrast = (max_l + 0.05) / (min_l + 0.05)
        reg_lines = [
            f"Region {rect.x()},{rect.y()},{rect.width()}x{rect.height()} palette "
            f"({len(buckets)} tones, value range {min_l:.2f}-{max_l:.2f}, contrast {contrast:.1f}:1):"
        ]
        for hx, pct, luma in swatches:
            reg_lines.append(f"- {hx} ({pct:.1f}%, luma {luma:.2f})")
        sections.append("\n".join(reg_lines))

    return "\n\n".join(sections)


def detect_toolbar_icons(
    image: QImage,
    existing: list[tuple[int, int, int, int]],
    max_icons: int = 40,
) -> list[tuple[int, int, int, int]]:
    """Fast edge/connected-component detector for unlabeled icons in toolbar strips (top 180px, left 120px,
    or full image when <= 600x400). Returns (x, y, w, h) boxes not already covered by `existing`."""
    iw, ih = image.width(), image.height()
    if iw < 16 or ih < 16:
        return []
    strips = [(0, 0, iw, ih)] if iw * ih <= 600 * 400 else [
        (0, 0, iw, min(ih, 180)),
        (0, min(ih, 180), min(iw, 120), max(0, ih - 180)),
    ]
    found: list[tuple[int, int, int, int]] = []
    step = 2

    for sx, sy, sw, sh in strips:
        if sw < 16 or sh < 16:
            continue
        rect = QRect(sx, sy, sw, sh)
        bg = page_color(image, rect)
        br, bg_g, bb = (bg >> 16) & 255, (bg >> 8) & 255, bg & 255
        data, stride, rw, rh = _rgb32_bytes(image, rect)
        gw, gh = (rw + step - 1) // step, (rh + step - 1) // step
        fg = bytearray(gw * gh)
        for gy in range(gh):
            row = (gy * step) * stride
            off = gy * gw
            for gx in range(gw):
                j = row + 4 * (gx * step)
                if max(abs(data[j + 2] - br), abs(data[j + 1] - bg_g), abs(data[j] - bb)) > 28:
                    fg[off + gx] = 1

        visited = bytearray(gw * gh)
        for gy in range(gh):
            row_off = gy * gw
            for gx in range(gw):
                idx = row_off + gx
                if not fg[idx] or visited[idx]:
                    continue
                visited[idx] = 1
                stack = [(gx, gy)]
                min_gx = max_gx = gx
                min_gy = max_gy = gy
                cells = 0
                while stack:
                    cx, cy = stack.pop()
                    cells += 1
                    if cx < min_gx:
                        min_gx = cx
                    elif cx > max_gx:
                        max_gx = cx
                    if cy < min_gy:
                        min_gy = cy
                    elif cy > max_gy:
                        max_gy = cy
                    for dy in (-1, 0, 1):
                        ny = cy + dy
                        if not (0 <= ny < gh):
                            continue
                        n_off = ny * gw
                        for dx in (-1, 0, 1):
                            if dx == 0 and dy == 0:
                                continue
                            nx = cx + dx
                            if 0 <= nx < gw:
                                n_idx = n_off + nx
                                if fg[n_idx] and not visited[n_idx]:
                                    visited[n_idx] = 1
                                    stack.append((nx, ny))
                bw = (max_gx - min_gx + 1) * step
                bh = (max_gy - min_gy + 1) * step
                if not (12 <= bw <= 64 and 12 <= bh <= 64):
                    continue
                aspect = bw / max(1, bh)
                if not (0.4 <= aspect <= 2.5):
                    continue
                box_cells = (max_gx - min_gx + 1) * (max_gy - min_gy + 1)
                fill = cells / max(1, box_cells)
                if not (0.08 <= fill <= 0.92):
                    continue
                bx, by = sx + min_gx * step, sy + min_gy * step
                bcx, bcy = bx + bw // 2, by + bh // 2
                if any(ex <= bcx <= ex + ew and ey <= bcy <= ey + eh for ex, ey, ew, eh in existing):
                    continue
                if any(not (bx + bw <= fx or fx + fw <= bx or by + bh <= fy or fy + fh <= by)
                       for fx, fy, fw, fh in found):
                    continue
                found.append((bx, by, bw, bh))
    found.sort(key=lambda b: (b[1] // 16, b[0]))
    return found[:max_icons]


def _rows(img: QImage, rect: QRect) -> list[bytes]:
    part = img.copy(rect).convertToFormat(QImage.Format.Format_RGB32)
    bits = part.constBits()
    bits.setsize(part.sizeInBytes())
    data, stride = bytes(bits), part.bytesPerLine()
    return [data[i * stride:(i + 1) * stride] for i in range(part.height())]


def follow(old: QImage, new: QImage, x: int, y: int) -> tuple[int, int] | None:
    """Where the target at (x, y) went: the strip along its row (label and button) found again exactly once,
    shifted up or down by up to FOLLOW_PX (a banner pushed the page, a row was inserted, the list re-sorted, it
    scrolled). None when it is gone, found more than once, or the strip is blank.
    shortcut: vertical moves with pixel-identical rendering only; a sideways move or a re-styled row is refused."""
    if old.size() != new.size():
        return None
    strip = QRect(x - ROW_LEFT, y - SPOT_R, ROW_LEFT + ROW_RIGHT + 1, 2 * SPOT_R + 1).intersected(old.rect())
    want = _rows(old, strip)
    if len(set(want)) < 2:
        return None
    have = _rows(new, QRect(strip.x(), 0, strip.width(), new.height()))
    top, n, hits = strip.y(), len(want), []
    for start in range(max(0, top - FOLLOW_PX), min(len(have) - n, top + FOLLOW_PX) + 1):
        if start != top and have[start] == want[0] and have[start:start + n] == want:
            hits.append(start - top)
            if len(hits) > 1:
                return None
    return (x, y + hits[0]) if hits else None


def _host_chat(host: Any) -> Any:
    """Resolve the owning chat from either an AiToolHost or a test SimpleNamespace mock."""
    chat = getattr(host, "chat", None)
    return chat if chat is not None else getattr(getattr(host, "_win", None), "_ai_chat", None)


def _host_auto_mode(host: Any) -> bool:
    """Read auto-mode flag from the resolved chat's plugin cleanly (False when unconfigured)."""
    chat = _host_chat(host)
    return bool(getattr(getattr(chat, "_plugin", None), "auto", False))


class AiToolHost(QObject):
    """The ToolHost the chat hands to ask(); `window` is the PetWindow."""

    _call = pyqtSignal(object)
    activity = pyqtSignal(str)  # the tool that runs now, "approval" while one waits on the user, "" when idle
    agent_event = pyqtSignal(int, str, str)  # (agent n, kind, text) from a sub-agent's thread, for its sheet

    def __init__(self, window: Any, mcp: McpHub | None = None) -> None:
        super().__init__(window)  # lives on the Qt thread, so the queued signal lands there
        self._win = window
        self.chat: Any = None
        self.saved: list[Path] = []
        self._shot = ""
        self._ref: QImage | None = None  # the screen as the model last saw it: screen_map, then after each action
        self._prev_ref: QImage | None = None  # screen before the latest action (used by peek_region(diff=True))
        self._maps = 0
        self._refused: tuple[int, int, int] | None = None  # (x, y, map) of the last click refused as stale
        self._in_batch = False
        self._batch_acted = False
        self._items: dict[int, tuple[int, int, int, int, str]] = {}  # the last screen_map's #id: (x, y, w, h, label)
        self._ocr_ids: set[int] = set()  # which of those ids are OCR text lines
        self.mcp_loaded: set[str] = set()  # MCP servers whose tools this answer offers (the chat clears it per answer)
        self.layout_open = ""  # the last layout_check's problems, "" once one passes (the chat clears it per answer)
        self.mcp = mcp or McpHub()
        self.procs = BackgroundProcs()
        self.cancel = threading.Event()  # the chat swaps in a fresh one per answer and sets it on Stop
        self.ran: list[tuple[str, str]] = []  # (tool note, output) this answer ran; a fresh list per answer, kept on Stop
        self.approve_s = APPROVE_TIMEOUT_S  # the chat raises it for an errand's answer
        self.outside = False  # ask() sets it while the chat holds web, MCP or agent content (see _allowed)
        self.agents = Agents(self._run_agent, lambda text, source: _host_chat(self).add_branch_note(text, source),
                             self.agent_event.emit)
        self._call.connect(self._on_call)
        self._idle_timer = QTimer(self)
        self._idle_timer.timeout.connect(self._close_idle_mcp)
        self._idle_timer.start(60_000)
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.mcp.close)
            app.aboutToQuit.connect(self.procs.stop_all)

    def _chat(self) -> Any:
        return _host_chat(self)

    def _auto_mode(self) -> bool:
        return _host_auto_mode(self)

    def _close_idle_mcp(self) -> None:
        if getattr(_host_chat(self), "_job", None) is None:  # never mid-answer
            threading.Thread(target=self.mcp.close_idle, daemon=True).start()  # taskkill blocks briefly

    @property
    def specs(self) -> list[dict[str, Any]]:
        """Built-in tools plus the MCP tools of servers loaded in this answer, and `load_mcp_tools` naming the rest:
        every round resends the tool list, and one browser server's schemas alone are ~20k characters."""
        mcp = self.mcp.specs()  # started on first use, on the worker thread
        loaded = getattr(self, "mcp_loaded", set())
        waiting: dict[str, list[str]] = {}
        for s in mcp:
            server, _sep, tool = s["function"]["name"].partition(SEP)
            if server not in loaded:
                waiting.setdefault(server, []).append(tool)
        shown = [s for s in mcp if s["function"]["name"].partition(SEP)[0] in loaded]
        base = TOOL_SPECS + AGENT_SPECS + ([LAPTOP_CHAT_SPEC] if getattr(_host_chat(self), "_remote_only", False) else [])
        if not waiting:
            return base + shown
        listed = "; ".join(f"{server} ({', '.join(tools)})" for server, tools in waiting.items())
        loader = {"type": "function", "function": {
            "name": "load_mcp_tools", "description": "Add an MCP server's tools to this answer, then call them as "
            f"server{SEP}tool. Servers and their tools: {listed}.",
            "parameters": {"type": "object", "properties": {"server": {"type": "string", "enum": list(waiting)}},
                           "required": ["server"]}}}
        return base + shown + [loader]

    @property
    def all_specs(self) -> list[dict[str, Any]]:
        """Every tool with every MCP schema: Claude's tool list is fixed for a whole answer."""
        base = TOOL_SPECS + AGENT_SPECS + ([LAPTOP_CHAT_SPEC] if getattr(_host_chat(self), "_remote_only", False) else [])
        return base + self.mcp.specs()

    def blocked(self, name: str) -> str | None:
        """ask() refuses a research tool while this chat's sub-agents still run (see Agents.blocked)."""
        return self.agents.blocked(name, self.cancel)

    def run(self, name: str, args: dict[str, Any]) -> str:
        """One tool call; the pet acts it out meanwhile (`activity`)."""
        self._tool = name  # what the pet goes back to acting out after an approval
        self.activity.emit(name)
        ran, out = self.ran, "error: did not finish"  # this answer's list: a stopped worker's late tool stays in it
        try:
            out = self._run(name, args)
            if out.startswith("error: no screen grab") and time.monotonic() - login_vault.blocked_at < 10:
                out = login_vault.BLOCK_NOTE
            return out
        finally:
            ran.append((tool_note(name, args), out))
            self.activity.emit("")

    def _run(self, name: str, args: dict[str, Any]) -> str:
        if name == "load_mcp_tools":
            want = args.get("server")
            tools = [s["function"]["name"] for s in self.mcp.specs() if self.mcp.server_of(s["function"]["name"]) == want]
            if not tools:
                return f"error: no MCP server named {want!r}"
            self.mcp_loaded.add(want)
            return f"loaded {len(tools)} tools: {', '.join(tools)}. Call them from the next step."
        server = self.mcp.server_of(name)
        if server is not None:
            chat = _host_chat(self)
            shown = json.dumps(args, ensure_ascii=False)
            # Values only: an argument named "format" or "truncate" is not a destructive command
            risk = "destructive tool" if self.mcp.is_destructive(name) else destructive_reason("\n".join(_strings(args)))
            review = (self._review(f'MCP tool "{name}" on server "{server}" with arguments:\n{shown}')
                      if _host_auto_mode(self) and f"mcp:{server}" not in chat._allowed else None)
            if self.cancel.is_set():
                return "error: stopped"
            extra = {"review": review} if review else {}
            allowed = self._waiting(lambda done: chat.confirm_mcp(server, name, done, shown[:600], risk, **extra))
            if allowed is not True:
                return "error: the user did not allow this MCP server"
            out = self.mcp.call(name, args, self.cancel)
            if self.mcp.image:  # a browser screenshot rides along like screen_map's (dropped for blind models)
                from src.ui.chat_widgets import image_data_url
                try:
                    self._shot = image_data_url(decode_image_bytes(self.mcp.image))
                except ValueError:
                    pass
            return out
        if name in FILE_TOOLS:
            secrets = False
            if name == "read_file":
                try:
                    path = resolve(self._project(), args.get("path"))
                except (OSError, ValueError):
                    path = None
                if path is not None and is_secret(path) and path.is_file():
                    secrets = self._allowed("read_secret", f"The model wants to read {path}, which looks like a "
                                            "secrets file (keys, passwords or tokens).", "secret files",
                                            "secrets file")
            return run_file_tool(self._project(), name, args, secrets=secrets)
        if name in ("edit_file", "edit_files"):
            try:
                edits = plan_edits(self._project(), args) if name == "edit_files" else [plan_edit(self._project(), args)]
            except ValueError as exc:
                return f"error: {exc}"
            if len(edits) == 1:
                verb = "create" if edits[0].before is None else "edit"
                ask = f"The model wants to {verb} {edits[0].rel}:\n\n{edits[0].diff}"
            else:
                diffs = "\n\n".join(f"{'new file' if e.before is None else 'edit'} {e.rel}\n{e.diff}" for e in edits)
                ask = f"The model wants to change {len(edits)} files, all or none:\n\n{diffs}"
            # Only edits wholly inside the Project Folder share the "edit_file" approval:
            # elsewhere (Startup, secrets, Paperduck's own skills and MCP config) asks under its own key.
            root, app_data = self._project(), skills_dir().parent.resolve()
            inside = root is not None and all(
                e.path.resolve().is_relative_to(root.resolve()) and not e.path.resolve().is_relative_to(app_data)
                and not is_secret(e.path) for e in edits)
            # Helper scripts in the scratch folder get a key of their own, so allowing them never allows Startup or
            # other folders; still reviewed, since a later `run_command` card shows only the script's name
            scratch = not inside and all(e.path.resolve().is_relative_to(scratch_dir().resolve())
                                         and not is_secret(e.path) for e in edits)
            key, what = (("edit_file", "file edits") if inside else ("edit_scratch", "helper scripts in the scratch folder")
                         if scratch else ("edit_outside", "edits outside the Project Folder"))
            if not self._allowed(key, ask, what):
                return "error: the user declined this edit"
            return apply_edits(edits)
        if name in ("set_errand", "cancel_errand"):
            return self._errand(name, args)
        if name == "spawn_agent":  # read-only tools only, so nothing to allow; Stop on this answer stops it
            share, task = args.get("share_images") is True, args.get("task")
            if isinstance(task, str) and (notes := self._shared_notes(share)):
                task += notes
            return self.agents.spawn(task, args.get("effort"), self._project(), self.cancel,
                                     args.get("_local") is True, self._shared_images(share), self.see_image,
                                     self._review if _host_auto_mode(self) else None)
        if name == "wait_agents":
            return self.agents.wait(args.get("timeout_s"), self.cancel)
        if name == "calculate":  # pure arithmetic, nothing to allow
            return calculate(args.get("expression"))
        if name == "plan_tasks":
            try:
                tasks = task_list(args)
            except ValueError as exc:
                return f"error: {exc}"
            self._on_gui(lambda done: done(_host_chat(self).set_tasks(tasks) or "ok"))
            return f"task list shown ({sum(1 for _t, s in tasks if s == 'done')} of {len(tasks)} done)"
        if name in ("run_command", "run_powershell"):
            ps = name == "run_powershell"
            command = args.get("script" if ps else "command")
            ps_path = args.get("path") if ps else None
            cwd = self._project() or Path.home()
            user_script: Path | None = None
            if ps and (not isinstance(command, str) or not command.strip()):
                if not isinstance(ps_path, str) or not ps_path.strip():
                    return "error: give a script or path"
                try:
                    resolved = resolve(self._project(), ps_path)
                except (OSError, ValueError):
                    return "error: bad script path"
                allowed_roots = [scratch_dir().resolve()] + ([self._project().resolve()] if self._project() else [])
                if resolved.suffix.lower() != ".ps1" or not any(resolved.is_relative_to(r) for r in allowed_roots):
                    return "error: path must be a .ps1 file inside the scratch or Project Folder"
                if is_secret(resolved) or not resolved.is_file():
                    return "error: script file not found"
                if resolved.stat().st_size > MAX_SAVE_CHARS:
                    return f"error: script is longer than {MAX_SAVE_CHARS} characters"
                try:
                    command = resolved.read_text(encoding="utf-8-sig")
                except (OSError, UnicodeDecodeError):
                    return "error: could not read script as UTF-8"
                user_script = resolved
            if not isinstance(command, str) or not command.strip():
                return "error: give a script" if ps else "error: give a command"
            where = "the Project Folder" if self._project() else f"your home folder ({cwd})"
            risk = destructive_reason(command)
            seconds = command_timeout(args.get("timeout_s"))
            limit = f" (allowed to run up to {seconds:g} s)" if args.get("timeout_s") is not None else ""
            what = " this PowerShell script" if ps else ""
            if not self._allowed("run_command", f"The model wants to run{what} in {where}{limit}:\n\n{command.strip()}",
                                 "commands", risk):
                return "error: the user declined this command"
            script, source = None, command
            if user_script is not None:
                command = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{user_script}"'
            elif ps:  # the user saw the script; it runs from a file so no cmd.exe quoting touches it
                try:
                    script, command = powershell_file(command)
                except (ValueError, OSError) as exc:
                    return f"error: {exc}"
            pdfs = {pdf: file_stamp(pdf) for pdf in print_to_pdf_paths(command, cwd)}
            media = media_stamps(command, cwd)
            out = run_command(cwd, command, self.cancel, seconds)
            failed = out.startswith(("error", "stopped after")) or (
                out.startswith("exit code") and not out.startswith("exit code 0"))
            if script is not None and not failed:
                script.unlink(missing_ok=True)
            active = user_script or script
            if active is not None and failed:
                out += powershell_error_note(out, source, active.name, saved_path=active)
            for pdf, before in pdfs.items():  # headless Edge stamps HeadlessChrome, Skia and dates
                # msedge.exe hands off to a child and returns before the PDF is written: wait for the new file
                if wait_for_new_file(pdf, before, PDF_WAIT_S, self.cancel):
                    out += f"\n[Paperduck: {pdf.name}: metadata {scrub_metadata(pdf)}]"
                else:
                    out += f"\n[Paperduck: {pdf.name} was not written within {PDF_WAIT_S:g} s; call clean_metadata on it]"
            # Other PDFs and images the command made may carry metadata too; they are the user's to keep as they
            # are (a build's own fixtures, say), so the model is told rather than the files changed
            unclean = [p for p in changed_media(media, cwd, command)
                       if p not in pdfs and scrub_metadata(p, dry_run=True) == "would clean"]
            if unclean:
                names = ", ".join(_rel_name(p, cwd) for p in unclean[:8])
                out += (f"\n[Paperduck: this made or changed {names}, which still carry metadata (author, creating app, "
                        "dates). Call clean_metadata on each one you hand to the user.]")
            return out
        if name == "start_background":
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return "error: give a command"
            cwd = self._project() or Path.home()
            where = "the Project Folder" if self._project() else f"your home folder ({cwd})"
            # its own key: allowing background starts for this chat must not also allow every foreground command
            if not self._allowed("start_background", f"The model wants to start this in the background in {where}. It keeps "
                                 f"running until you stop it:\n\n{command.strip()}", "background commands",
                                 destructive_reason(command)):
                return "error: the user declined this command"
            out = self.procs.start(cwd, command)
            self._on_gui(lambda done: done(_host_chat(self).watch_procs() or "ok"))
            return out
        if name == "read_background":
            return self.procs.read(args.get("id"))
        if name == "stop_background":
            return self.procs.stop(args.get("id"))
        if name == "load_skill":
            return load_skill(skills_dir(), args.get("name"))
        if name == "find_skill":
            return find_skills(skills_dir(), args.get("query"))
        if name == "find_mcp_servers":
            query = args.get("query")
            return find_servers(query.strip()) if isinstance(query, str) and query.strip() else "error: give a query"
        if name == "add_mcp_server":
            return self._add_mcp_server(args)
        if name == "add_skill":
            fields = [args.get(k) for k in ("name", "description", "instructions")]
            if not all(isinstance(f, str) and f.strip() for f in fields):
                return "error: give a name, a description and instructions"
            skill, description, instructions = (f.strip() for f in fields)
            text = skill_text(description, instructions)
            if not self._allowed("add_skill", f'The model wants to save a skill "{skill}" that later chats will '
                                 f"follow as your own instructions:\n\n{text}", "saving skills"):
                return "error: the user declined this skill"
            return write_skill(skills_dir(), skill, text)
        if name == "update_skill":
            plan = lambda: plan_skill_update(skills_dir(), args.get("name"), args.get("description"),  # noqa: E731
                                             args.get("instructions"))
            try:
                path, text, diff = plan()
                if not self._allowed("update_skill", f'The model wants to change your skill "{path.parent.name}", '
                                     f"which later chats follow as your own instructions:\n\n{diff}", "skill updates"):
                    return "error: the user declined this skill change"
                if plan()[2] != diff:
                    return "error: the skill changed while the user was deciding; load_skill it again"
                path.write_text(text, encoding="utf-8")
            except ValueError as exc:
                return f"error: {exc}"
            except OSError as exc:
                return f"error: could not save the skill ({exc.strerror or 'write failed'})"
            return f'updated skill "{path.parent.name}"'
        if name in ("remember", "forget"):
            try:
                _text, note = plan_memory(memory_path(), name, args.get("note"))
            except ValueError as exc:
                return f"error: {exc}"
            verb = "remember this in every later chat" if name == "remember" else "forget this note"
            if not self._allowed("memory", f"The model wants to {verb}:\n\n{note}", "memory changes"):
                return "error: the user declined this memory change"
            try:
                text, again = plan_memory(memory_path(), name, args.get("note"))  # the file may have changed meanwhile
            except ValueError as exc:
                return f"error: {exc}"
            if again != note:
                return "error: memory changed while the user was deciding; try again"
            return write_memory(memory_path(), text)
        if name == "make_sheet":
            return self._make_sheet(args)
        if name == "read_screen":
            image = self._on_gui(self._grab_screen)
            return self._ocr(image) if isinstance(image, QImage) and not image.isNull() else "error: no screen grab"
        if name == "screen_map":
            got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
            if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
                return "error: no screen grab"
            return self._map(*got, region=args.get("region"), annotate=args.get("annotate") is True,
                             grid=args.get("grid") is True)
        if name == "peek_region":
            got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
            if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
                return "error: no screen grab"
            return self._peek_region(*got, region=args.get("region"), grid=args.get("grid") is True,
                                     grayscale=args.get("grayscale") is True, diff=args.get("diff") is True)
        if name == "sample_colors":
            got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
            if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
                return "error: no screen grab"
            return self._sample_colors(got[0], args.get("points"), args.get("region"), args.get("top"))
        if name == "layout_check":
            got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
            if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
                return "error: no screen grab"
            return self._layout_check(*got, args.get("page"))
        if name == "type_login":
            if not _GUI_CONTROL_LOCK.acquire(blocking=False):
                return "error: another chat is currently controlling the screen; try again once it finishes"
            try:
                return self._type_login(args)
            finally:
                _GUI_CONTROL_LOCK.release()
        if name in COMPUTER_TOOLS or name == "computer_batch":
            if not _GUI_CONTROL_LOCK.acquire(blocking=False):
                return "error: another chat is currently controlling the screen; try again once it finishes"
            try:
                return self._computer(name, args) if name in COMPUTER_TOOLS else self._batch(args.get("steps"))
            finally:
                _GUI_CONTROL_LOCK.release()
        if name == "preview_document":  # reads a private copy in a hidden Office instance, so it needs no approval
            try:
                path = resolve(self._project(), args.get("path"))
            except (OSError, ValueError):
                return "error: bad path"
            text, sheet = preview_document(path, args.get("pages"))
            if sheet is not None:
                from src.ui.chat_widgets import image_data_url

                self._shot = image_data_url(sheet)  # sent like screen_map's screenshot (dropped for blind models)
            return text
        if name == "view_image":  # a read, so no approval; the picture rides along like screen_map's screenshot
            text, self._shot = self.see_image(self._project(), args)
            return text
        if name == "clean_metadata":  # only blanks metadata in a PDF/PNG/JPEG, so it needs no approval
            try:
                path = resolve(self._project(), args.get("path"))
            except (OSError, ValueError):
                return "error: bad path"
            return scrub_metadata(path) if path.is_file() else "error: not a file"
        if name == "save_file":
            try:
                file_name, content = save_args(args)
            except ValueError as exc:
                return f"error: {exc}"
            out = self._on_gui(lambda done: done(self._save_file(file_name, content)), SAVE_TIMEOUT_S)
            return out if isinstance(out, str) else "error: the user did not answer the Save dialog in time"
        if name == "laptop_chat":
            return self._laptop_chat_tool(args)
        out = self._on_gui(lambda done: done(self._pet_tool(name, args)))
        return out if isinstance(out, str) else "error: the pet did not answer in time"

    def _laptop_chat_tool(self, args: dict[str, Any]) -> str:
        """Allow the phone chat to inspect, message/steer, or stop the live desktop Laptop chat."""
        if not getattr(_host_chat(self), "_remote_only", False):
            return "error: laptop_chat is only available from the phone chat"
        action = str(args.get("action") or "read").strip().lower()
        if action == "read":
            def read(done: Callable[[Any], None]) -> None:
                chat = getattr(self._win, "_ai_chat", None)
                if chat is None:
                    done("Laptop chat: not opened yet (idle, 0 messages).")
                    return
                plugin = getattr(chat, "_plugin", None)
                hist = list(getattr(chat, "_history", []) or [])
                busy = getattr(chat, "_job", None) is not None and not getattr(chat, "_testing", False)
                progress = getattr(chat, "_progress", "") or ""
                approvals = len(getattr(chat, "_remote_approvals", ()) or ())
                status = f"busy ({progress})" if (busy and progress) else ("busy" if busy else "idle")
                model = getattr(plugin, "model", "") or "unset"
                thinking = getattr(plugin, "thinking", "off")
                auto = bool(getattr(plugin, "auto", False))
                lines = [
                    f"Laptop chat: {status}, model={model}, thinking={thinking}, auto={auto}, "
                    f"pending_approvals={approvals}, messages={len(hist)}."
                ]
                for turn in hist[-10:]:
                    role = turn.get("role", "assistant")
                    raw_txt = _clean_secrets(str(turn.get("content") or "")).strip()
                    snippet = raw_txt[:350] + ("..." if len(raw_txt) > 350 else "")
                    lines.append(f"[{role}] {snippet}" if snippet else f"[{role}] (empty)")
                done("\n".join(lines)[:4000])

            out = self._on_gui(read)
            return out if isinstance(out, str) else "error: could not read laptop chat"

        if action == "send":
            msg = str(args.get("message") or "").strip()
            if not msg:
                return "error: give a non-empty message"
            if not self._allowed("laptop_chat", f"Send this message to the desktop Laptop chat:\n\n{msg[:1500]}",
                                 "laptop chat"):
                return "error: the user declined sending to the laptop chat"

            def deliver(done: Callable[[Any], None]) -> None:
                remote = getattr(self._win, "remote", None)
                chat = remote._ensure_laptop_chat() if remote is not None else getattr(self._win, "_ai_chat", None)
                if chat is None:
                    done("error: laptop chat is unavailable")
                    return
                ok, status = chat.send_remote(msg)
                done(f"Laptop chat: {status}" if ok else f"error: {status}")

            out = self._on_gui(deliver)
            return out if isinstance(out, str) else "error: laptop chat did not respond"

        if action == "stop":
            if not self._allowed("laptop_chat", "Stop the active reply in the desktop Laptop chat.", "laptop chat"):
                return "error: the user declined stopping the laptop chat"

            def halt(done: Callable[[Any], None]) -> None:
                chat = getattr(self._win, "_ai_chat", None)
                if chat is None or getattr(chat, "_job", None) is None or getattr(chat, "_testing", False):
                    done("Laptop chat is already idle.")
                    return
                chat.stop()
                done("Laptop chat stopped.")

            out = self._on_gui(halt)
            return out if isinstance(out, str) else "error: laptop chat did not respond"

        return "error: action must be 'read', 'send', or 'stop'"

    def _errand(self, name: str, args: dict[str, Any]) -> str:
        """set_errand asks every time (it is an instruction that runs later, maybe while the user is away);
        cancel_errand only removes one. The plugin is touched on the Qt thread, where the pet polls it."""
        errands = self._win._state_machine.registry.get_plugin("errands")
        if errands is None:
            return "error: errands are not available"
        if name == "cancel_errand":
            out = self._on_gui(lambda done: done(errands.cancel(args.get("id"))))
            return out if isinstance(out, str) else "error: the pet did not answer in time"
        try:
            from src.tools.errands import parse_when

            kind, arg = parse_when(args.get("when"))
        except ValueError as exc:
            return f"error: {exc}"
        instruction = args.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            return "error: give the instruction to follow when it fires"
        if not self._allowed(f"errand:{time.monotonic()}", f"The model wants to set an errand: {describe_errand(kind, arg)}, "
                             "it will run this in this chat (edits and commands still ask you):\n\n"
                             f"{instruction.strip()}", "errand"):
            return "error: the user declined this errand"

        def add(done: Callable[[Any], None]) -> None:
            try:
                errand = errands.add(args.get("when"), instruction, _host_chat(self).ensure_sid())
            except ValueError as exc:
                done(f"error: {exc}")
                return
            self._win.update()  # the errand tag
            done(f"errand {errand.id} set: {describe_errand(errand.kind, errand.arg)}. It runs in this chat when "
                 "that happens; tell the user they can leave it to you.")

        out = self._on_gui(add)
        return out if isinstance(out, str) else "error: the pet did not answer in time"

    def _computer(self, name: str, args: dict[str, Any], approved: bool = False) -> str:
        """Mouse and keyboard on the primary monitor. Every action needs the user's Allow (once or for the chat); a
        delete key or a destructive typed command asks every time. `approved`: the batch it is in was allowed whole."""
        raw_id = args.get("id")
        items = getattr(self, "_items", {})
        target_label = str(args.get("name") or (items[raw_id][4] if isinstance(raw_id, int) and raw_id in items else ""))
        args = resolve_target(name, _given(args), items)
        if isinstance(args, str):
            return args
        try:
            what, risk = computer_action(name, args)
        except ValueError as exc:
            return f"error: {exc}"
        if not approved and not self._allowed("computer", f"The model wants to control your computer:\n\n{what}",
                                              "computer control", risk):
            return "error: the user declined this action"
        note = ""
        if name == "mouse_click":
            if args.get("name") is not None:
                found = self._find_click(args)
                if isinstance(found, str):  # an error, or pressed without the mouse
                    return found
                args = {**args, "x": found[0], "y": found[1]}
            if not getattr(self, "_batch_acted", False):
                note, x, y = self._stale_click(args["x"], args["y"], target_label)
                if note.startswith("error"):
                    return note
                args = {**args, "x": x, "y": y}
        elif name == "mouse_drag" and not getattr(self, "_batch_acted", False):
            note, x, y = self._stale_click(args["x"], args["y"], target_label)  # the grab point only: drop spot may be anywhere
            if note.startswith("error"):
                return note
            args = {**args, "x": x, "y": y}
        elif name == "mouse_path" and not getattr(self, "_batch_acted", False):
            ox, oy = args["points"][0]
            note, nx, ny = self._stale_click(ox, oy, target_label)
            if note.startswith("error"):
                return note
            if (nx, ny) != (ox, oy):
                dx, dy = nx - ox, ny - oy
                args = {**args, "points": [[p[0] + dx, p[1] + dy] for p in args["points"]]}
        out = self._on_gui(lambda done: self._act(done, name, args), 90.0)  # up to 2000 chars at 20 ms
        return note + out if isinstance(out, str) else "error: the action did not finish in time"

    @staticmethod
    def _offset_ocr_boxes(raw: str, dx: int, dy: int) -> str:
        if not (dx or dy):
            return raw
        out = []
        for line in raw.splitlines():
            box, tab, rest = line.partition("\t")
            try:
                x, y, w, h = (int(n) for n in box.split(","))
            except ValueError:
                out.append(line)
                continue
            out.append(f"{x + dx},{y + dy},{w},{h}{tab}{rest}")
        return "\n".join(out)

    def _map(self, image: QImage, hwnd: int, region: Any = None, annotate: bool = False, grid: bool = False) -> str:
        """screen_map's text for a grab; the grab becomes what the next click is checked against. `region`
        ([left, top, width, height]) lists only what is inside it and sends just that part as the screenshot: the
        provider charges about the same per image whatever its size, so a crop shows more for the same tokens."""
        clip = None
        if region is not None:
            if (not isinstance(region, list) or len(region) != 4
                    or any(isinstance(v, bool) or not isinstance(v, int) for v in region)):
                return "error: region is [left, top, width, height] in screen pixels"
            rect = QRect(*region).intersected(image.rect())
            if rect.width() < 8 or rect.height() < 8:
                return "error: the region is (almost) off the screen"
            clip = (rect.x(), rect.y(), rect.width(), rect.height())
        shown = image.copy(QRect(*clip)) if clip else image
        with ThreadPoolExecutor(max_workers=2) as pool:  # OCR (cropped when zoomed) and the control tree overlap
            controls = pool.submit(computer.ui_elements, hwnd)
            raw_ocr = pool.submit(self._ocr, shown, True).result()
            elements = controls.result()
        lines = self._offset_ocr_boxes(raw_ocr, clip[0], clip[1]) if clip else raw_ocr
        ox, oy = (clip[0], clip[1]) if clip else (0, 0)
        if annotate or elements.count("\n") < 12:
            existing: list[tuple[int, int, int, int]] = []
            for raw_line in (elements.splitlines() + lines.splitlines()):
                box_part = raw_line.partition("\t")[0]
                try:
                    bx, by, bw, bh = (int(v) for v in box_part.split(","))
                except ValueError:
                    continue
                existing.append((bx - ox, by - oy, bw, bh))
            icons = detect_toolbar_icons(shown, existing)
            if icons:
                extra = "\n".join(f"{ix + ox},{iy + oy},{iw},{ih}\tIcon\t(unlabeled)" for ix, iy, iw, ih in icons)
                elements = (elements.rstrip("\n") + "\n" + extra) if elements.strip() else extra
        from src.ui.chat_widgets import MAX_IMAGE_SIDE, image_data_url

        max_side = MAX_IMAGE_SIDE - RULER_LEFT
        side = max(shown.width(), shown.height())
        note = (f"The screenshot you see is scaled down {side / max_side:.2f}x: do not read pixels from "
                "it, use the (x, y) listed here." if side > max_side else "The screenshot is at full size.")
        note += " Top and left rulers mark true screen-pixel coordinates."
        if annotate:
            note += " #id boxes are drawn on the screenshot (teal = controls, violet = unlabeled icon, amber = OCR text)."
        if grid:
            note += " Faint grid lines match the ruler ticks."
        if clip:
            note = (f"Zoomed: only region {clip[0]},{clip[1]},{clip[2]}x{clip[3]} is listed and shown; (x, y) are "
                    "still screen pixels. " + note)
        self._items, self._ocr_ids = {}, set()  # the ids this map hands out; clicks and drags resolve `id` with them
        out = parse_boxes(lines, image.width(), image.height(), elements, note, self._items, clip, self._ocr_ids)
        composed = render_screen_overlay(
            shown,
            origin=(clip[0], clip[1]) if clip else (0, 0),
            items=self._items if annotate else None,
            ocr_ids=self._ocr_ids if annotate else None,
            grid=grid,
        )
        self._shot = image_data_url(composed)
        if self._ref is not None:
            self._prev_ref = self._ref
        self._ref, self._maps = image, self._maps + 1
        return out

    def _peek_region(self, image: QImage, _hwnd: int, region: Any = None, grid: bool = False,
                     grayscale: bool = False, diff: bool = False) -> str:
        """Fast image-only look at `image` or a `region` crop, skipping UIA and OCR."""
        clip = None
        if region is not None:
            if (not isinstance(region, list) or len(region) != 4
                    or any(isinstance(v, bool) or not isinstance(v, int) for v in region)):
                return "error: region is [left, top, width, height] in screen pixels"
            rect = QRect(*region).intersected(image.rect())
            if rect.width() < 8 or rect.height() < 8:
                return "error: the region is (almost) off the screen"
            clip = (rect.x(), rect.y(), rect.width(), rect.height())
        shown = image.copy(QRect(*clip)) if clip else image
        origin = (clip[0], clip[1]) if clip else (0, 0)
        from src.ui.chat_widgets import image_data_url

        modes: list[str] = []
        dnote = ""
        if diff and self._ref is not None and self._ref.size() == image.size():
            prev_shown = self._ref.copy(QRect(*clip)) if clip else self._ref
            if (changed_region(prev_shown, shown) == (0, 0, 0, 0, 0)
                    and getattr(self, "_prev_ref", None) is not None and self._prev_ref.size() == image.size()):
                prev_shown = self._prev_ref.copy(QRect(*clip)) if clip else self._prev_ref
            dnote = diff_note(prev_shown, shown)
            if grayscale:
                prev_shown = prev_shown.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32)
                shown_view = shown.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32)
                modes.append("grayscale")
            else:
                shown_view = shown
            composed = compose_side_by_side(prev_shown, shown_view)
            modes.append("side-by-side BEFORE | AFTER")
        else:
            target_img = (shown.convertToFormat(QImage.Format.Format_Grayscale8).convertToFormat(QImage.Format.Format_ARGB32)
                          if grayscale else shown)
            if grayscale:
                modes.append("grayscale")
            if grid:
                modes.append("grid")
            composed = render_screen_overlay(target_img, origin=origin, grid=grid)
            if diff:
                dnote = " No prior screen grab of matching size to diff against; showing current view."

        self._shot = image_data_url(composed)
        if self._ref is not None:
            self._prev_ref = self._ref
        self._ref = image
        where = (f"region {clip[0]},{clip[1]},{clip[2]}x{clip[3]} of {image.width()}x{image.height()}"
                 if clip else f"full screen {image.width()}x{image.height()}")
        mode_note = f" ({', '.join(modes)})" if modes else ""
        return f"Peeked {where}{mode_note}.{dnote}"

    @staticmethod
    def _sample_colors(image: QImage, points: Any = None, region: Any = None, top: Any = None) -> str:
        return analyze_colors(image, points=points, region=region, top=6 if top is None else top)

    def _make_sheet(self, args: dict[str, Any]) -> str:
        """make_sheet: render in the scratch folder, then copy to Documents\\Paperduck under a name nothing has."""
        from src.tools.sheet import check_spec, page_count, render_sheet
        from src.ui.chat_widgets import image_data_url

        spec = check_spec(args)
        if isinstance(spec, str):
            return spec
        got = render_sheet(spec, scratch_dir() / "sheets", self.cancel)
        if isinstance(got, str):
            return got
        pdf, png = got
        target, image = pdf, png  # kept in the scratch folder when no user folder takes it
        loc = QStandardPaths.StandardLocation
        # Controlled Folder Access can block Documents (it shows as "file not found"); Downloads is not protected
        for base in (QStandardPaths.writableLocation(loc.DocumentsLocation),
                     QStandardPaths.writableLocation(loc.DownloadLocation)):
            if not base:
                continue
            folder = Path(base) / "Paperduck"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                stem, n = pdf.stem, 2
                while (folder / f"{stem}.pdf").exists() or (folder / f"{stem}.png").exists():
                    stem, n = f"{pdf.stem} ({n})", n + 1  # never over a file the user may have kept or edited
                shutil.copyfile(pdf, folder / f"{stem}.pdf")
                shutil.copyfile(png, folder / f"{stem}.png")
            except OSError:
                continue
            target, image = folder / f"{stem}.pdf", folder / f"{stem}.png"
            break
        preview = QImage(str(png))
        if not preview.isNull():
            self._shot = image_data_url(preview)
        pages = page_count(target)
        fit = ("fits on one page" if pages == 1 else f"needs {pages} pages even at the smallest size: shorten the "
               "points or drop the least useful ones, then call make_sheet again")
        return (f"saved {target} and the same page as an image, {image} ({fit}; {spec['columns']} columns, "
                f"{spec['chars']} characters). Look at the preview: if it reads well, tell the user where it is. For "
                "Canva, upload the PNG (a PDF upload makes Canva swap the fonts and sizes), add it on a blank page "
                "and right-click it, Set image as background, so it fills the page; then check with screen_map.")

    def _layout_check(self, image: QImage, hwnd: int, page: Any) -> str:
        """layout_check: screen_map zoomed on `page`, headed by the problems `check_layout` measures there."""
        if (not isinstance(page, list) or len(page) != 4
                or any(isinstance(v, bool) or not isinstance(v, int) for v in page)):
            return "error: page is [left, top, width, height] in screen pixels"
        rect = snap_page(image, QRect(*page).intersected(image.rect()))
        if rect is None:
            return "error: no page found there; give the page's box from a screen_map"
        listing = self._map(image, hwnd, region=[rect.x(), rect.y(), rect.width(), rect.height()])
        if listing.startswith("error:"):
            return listing.replace("region", "page")
        bg = page_color(image, rect)
        br, bg_g, bb = (bg >> 16) & 255, (bg >> 8) & 255, bg & 255
        data, stride, rw, rh = _rgb32_bytes(image, rect)
        ink: list[tuple[int, int, int]] = []
        inset = 5  # a selection outline or the page's own border sits on the edge; it is not content
        x_end, y_end = max(inset, rw - inset), max(inset, rh - inset)
        for y in range(inset, y_end, 3):  # every third pixel over the single copied RGB32 buffer
            row = y * stride
            first_x = last_x = -1
            for x in range(inset, x_end, 3):
                j = row + 4 * x
                if max(abs(data[j + 2] - br), abs(data[j + 1] - bg_g), abs(data[j] - bb)) > 24:
                    if first_x < 0:
                        first_x = x
                    last_x = x
            if first_x >= 0:
                ink.append((rect.top() + y, rect.left() + first_x, rect.left() + last_x))
        texts = {n: it for n, it in self._items.items() if n in self._ocr_ids}  # controls (the canvas) are no text
        low_contrast: list[tuple[int, float, str]] = []
        for n, (bx, by, bw, bh, lbl) in texts.items():
            if bw < 4 or bh < 4 or not lbl.strip():
                continue
            lx0, ly0 = max(0, bx - rect.x()), max(0, by - rect.y())
            lx1, ly1 = min(rw - 1, bx + bw - 1 - rect.x()), min(rh - 1, by + bh - 1 - rect.y())
            if lx1 - lx0 < 3 or ly1 - ly0 < 3:
                continue
            # Sample local background along the outer box edge, then find peak foreground contrast inside
            edge_lumas = []
            for sx in range(lx0, lx1 + 1, max(1, (lx1 - lx0) // 6)):
                for sy in (ly0, ly1):
                    j = sy * stride + 4 * sx
                    edge_lumas.append(_luma(data[j + 2], data[j + 1], data[j]))
            bg_l = sorted(edge_lumas)[len(edge_lumas) // 2]
            best_ratio = 1.0
            for sy in range(ly0 + 1, ly1, max(1, (ly1 - ly0) // 6)):
                row = sy * stride
                for sx in range(lx0 + 1, lx1, max(1, (lx1 - lx0) // 8)):
                    j = row + 4 * sx
                    fg_l = _luma(data[j + 2], data[j + 1], data[j])
                    hi, lo = max(bg_l, fg_l), min(bg_l, fg_l)
                    ratio = (hi + 0.05) / (lo + 0.05)
                    if ratio > best_ratio:
                        best_ratio = ratio
            if best_ratio < 2.5:
                low_contrast.append((n, best_ratio, lbl))
        problems = check_layout((rect.x(), rect.y(), rect.width(), rect.height()), texts, ink, low_contrast)
        if (rect.x(), rect.y(), rect.width(), rect.height()) != tuple(page):
            listing = f"(The page measures {rect.x()},{rect.y()},{rect.width()}x{rect.height()}.)\n" + listing
        self.layout_open = "; ".join(problems)
        if not problems:
            return ("Layout check: no problems measured. Still look at the screenshot: is it clear, balanced and "
                    "well styled? Fix what is not.\n\n" + listing)
        return ("Layout check: NOT up to standard yet. Fix each problem, then run layout_check again (a full-bleed "
                "background or picture may touch the edge on purpose):\n- " + "\n- ".join(problems)
                + "\n\n" + listing)

    def _find_click(self, args: dict[str, Any]) -> tuple[int, int] | str:
        """Where the control `name` (on the row of `near`) is right now, from a fresh look. When something else covers
        its spot (a cookie bar, a toolbar) it is pressed through UI Automation instead, never under a dialog."""
        got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
        if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
            return "error: no screen grab"
        image, hwnd = got
        near = args.get("near") or ""
        with ThreadPoolExecutor(max_workers=2) as pool:  # OCR (only needed to find `near`) and the controls overlap
            controls = pool.submit(computer.ui_elements, hwnd)
            ocr = self._ocr(image, True) if near.strip() else ""
            elements = controls.result()
        found = find_control(elements, ocr, args["name"], near, image.width(), image.height())
        if isinstance(found, str):
            return found
        x, y, label = found
        self._ref = image  # the check right before the click compares against this look
        fold = lambda t: " ".join(t.split()).casefold()  # noqa: E731
        chain = [] if computer.ours_at(x, y) else computer.uia_at(x, y)  # clicks pass through the pet and chat
        if not chain or any(fold(n) == fold(label) for _kind, n in chain):
            return x, y
        cover = next((n for _kind, n in chain if n.strip()), "something else")[:60]
        if computer.front_state()[1] or any(kind in ("dialog", "alert", "alert dialog") for kind, _n in chain):
            return f'error: not clicked: a dialog ("{cover}") covers "{label}"; answer or close it first'
        if computer.uia_invoke(hwnd, x, y, label):
            return (f'pressed "{label}" at ({x}, {y}) through Windows UI Automation, because "{cover}" covers its '
                    "spot. Call screen_map for details.")
        return f'error: not clicked: "{cover}" covers "{label}" at ({x}, {y}); close it or scroll it out of the way first'

    def _stale_click(self, x: int, y: int, label: str = "") -> tuple[str, int, int]:
        """(note, where to click). The note is an error when the screen at (x, y) changed since the model last saw
        it and the target cannot be found again, so the click could hit the wrong thing; a target that only moved
        is followed (by row pixels, else local UIA label). A spot that changes again right after a fresh screen_map is
        animated and gets clicked."""
        if self._ref is None:
            return "", x, y
        got = self._on_gui(lambda done: self._grab_screen(done, primary=True))
        if not isinstance(got, tuple) or not isinstance(got[0], QImage) or got[0].isNull():
            return "", x, y  # no grab to compare: click as before
        if not spot_changed(self._ref, got[0], x, y):
            return "", x, y
        if (moved := follow(self._ref, got[0], x, y)) is not None:
            return f"The target moved from ({x}, {y}) to {moved} since you looked; followed it and ", *moved
        if label and got[1]:
            found = find_control(computer.ui_elements(got[1]), "", label, "", got[0].width(), got[0].height())
            if isinstance(found, tuple) and abs(found[0] - x) <= FOLLOW_PX and abs(found[1] - y) <= FOLLOW_PX:
                return f'The control "{label[:40]}" moved from ({x}, {y}) to ({found[0]}, {found[1]}); followed it and ', found[0], found[1]
        if self._refused is not None and self._refused[:2] == (x, y) and self._refused[2] != self._maps:
            return "", x, y
        self._refused = (x, y, self._maps)  # against this map; the one below is a fresh look
        return (f"error: not clicked: the screen at ({x}, {y}) changed since you last looked (the page moved, a list "
                "row shifted, it scrolled, re-rendered or something covers it), so the click could land on the wrong "
                "thing. The screen now (a fresh screen_map, no need to call it): find your target again and click its "
                "new position.\n\n" + self._map(*got), x, y)

    def _batch(self, steps: Any) -> str:
        """computer_batch: one approval listing every step (asked as risky when any step is), then each step through
        `_computer`, stopping at the first error or Stop."""
        if not isinstance(steps, list) or not steps or len(steps) > MAX_BATCH_STEPS:
            return f"error: give 1 to {MAX_BATCH_STEPS} steps"
        plan, risks, todo = [], [], []
        for i, step in enumerate(steps, 1):
            tool = step.get("tool") if isinstance(step, dict) else None
            if tool == "wait":  # batch-only: lets an app catch up (a toolbar, a save) without a round of its own
                ms = step.get("ms")
                if isinstance(ms, bool) or not isinstance(ms, int) or not 0 < ms <= MAX_WAIT_MS:
                    return f"error: step {i}: wait takes ms from 1 to {MAX_WAIT_MS}"
                plan.append(f"{i}. wait {ms} ms")
                todo.append((tool, {"ms": ms}))
                continue
            if tool not in COMPUTER_TOOLS:
                return f"error: step {i} needs \"tool\" set to one of {', '.join(sorted(COMPUTER_TOOLS | {'wait'}))}"
            args = resolve_target(tool, _given({k: v for k, v in step.items() if k != "tool"}),
                                  getattr(self, "_items", {}))
            if isinstance(args, str):
                return f"error: step {i}: {args.removeprefix('error: ')}"
            try:
                what, risk = computer_action(tool, args)
            except ValueError as exc:
                return f"error: step {i}: {exc}"
            plan.append(f"{i}. {what}")
            risks += [risk] if risk else []
            todo.append((tool, args))
        if not self._allowed("computer", "The model wants to control your computer in these steps:\n\n" + "\n".join(plan),
                             "computer control", ", ".join(risks)):
            return "error: the user declined these actions"
        cancel, lines = self.cancel, []
        self._batch_acted = False
        try:
            for i, (tool, args) in enumerate(todo, 1):
                if cancel.is_set():
                    return "\n".join([*lines, "error: stopped by the user"])
                if tool == "wait":
                    cancel.wait(args["ms"] / 1000)
                    lines.append(f"{i}. waited {args['ms']} ms")
                    continue
                self._in_batch = i < len(todo)
                out = self._computer(tool, args, approved=True)
                self._batch_acted = True
                lines.append(f"{i}. {out}")
                if out.startswith("error"):
                    return "\n".join([*lines, f"error: stopped at step {i} of {len(steps)}"])
            return "\n".join(lines)
        finally:
            self._in_batch = False
            self._batch_acted = False

    def _act(self, done: Callable[[Any], None], name: str, args: dict[str, Any]) -> None:
        """Make the pet and the chat click-through (hidden only where Windows cannot), give the keyboard to the
        app under them, act, then restore them."""
        in_batch = getattr(self, "_in_batch", False)
        shown = _ours(self._win)
        hidden = [w for w in shown if not computer.click_through(int(w.winId()), True)]
        for w in hidden:
            w.hide()
        before = [("", False)]
        if name == "paste_text" and (cb := QGuiApplication.clipboard()) is not None:
            if args.get("html") is True:  # both formats: rich-paste apps take the HTML, the rest the plain text
                mime, doc = QMimeData(), QTextDocument()
                doc.setHtml(args["text"])
                mime.setHtml(args["text"])
                mime.setText(doc.toPlainText())
                cb.setMimeData(mime)
            else:
                cb.setText(args["text"])  # here on the Qt thread; the worker only presses ctrl+v

        def act() -> str:  # on a worker thread: typing takes 8 ms a character and must not freeze the pet
            computer.focus_front_app()  # the chat has the keyboard after the user pressed Enter
            before[0] = computer.front_state()
            try:
                if name == "mouse_click":
                    computer.click(args["x"], args["y"], args.get("button") or "left", bool(args.get("double")))
                    result = "clicked"
                elif name == "mouse_hover":
                    wait_ms = int(args["wait_ms"]) if args.get("wait_ms") is not None else 500
                    computer.hover(args["x"], args["y"], wait_ms)
                    chain = [] if login_vault.typed_password_shown() else computer.uia_at(args["x"], args["y"])
                    picks = [f'[{k}] "{lbl[:50]}"' if k else f'"{lbl[:50]}"'
                             for k, lbl in chain if lbl.strip()][:2]
                    under = f"; under cursor: {', '.join(picks)}" if picks else ""
                    result = f"hovered at ({args['x']}, {args['y']}){under}"
                elif name == "paste_text":
                    time.sleep(0.05)  # the app reads the clipboard on ctrl+v; give the copy a moment to land
                    computer.press(computer.parse_keys("ctrl+v"))
                    result = f"pasted {len(args['text'])} characters"
                elif name == "mouse_drag":
                    computer.drag(
                        args["x"], args["y"], args["to_x"], args["to_y"],
                        button=args.get("button") or "left",
                        modifiers=computer.parse_modifiers(args.get("modifiers")),
                    )
                    result = "dragged"
                elif name == "mouse_path":
                    pts = [(p[0], p[1]) for p in args["points"]]
                    done_pts = computer.path(
                        pts,
                        button=args.get("button") or "left",
                        modifiers=computer.parse_modifiers(args.get("modifiers")),
                        step_px=int(args["step_px"]) if args.get("step_px") is not None else 8,
                        stop=self.cancel,
                    )
                    result = (f"error: stopped by the user after {done_pts} points" if self.cancel.is_set()
                              else f"traced {done_pts}-point path")
                elif name == "type_text":
                    typed = computer.type_text(args["text"], stop=self.cancel)
                    result = (f"error: stopped by the user after {typed} characters" if self.cancel.is_set()
                              else f"typed {typed} characters")
                elif name == "press_key":
                    computer.press(computer.parse_keys(args["keys"]))
                    result = f"pressed {args['keys']}"
                else:
                    computer.scroll(args["amount"], args.get("x"), args.get("y"))
                    result = "scrolled"
            except (ValueError, OSError, TypeError, ctypes.ArgumentError) as exc:  # a bad arg must not hang the call
                result = f"error: {exc}"
            return result

        def finish(result: str) -> None:
            for w in shown:  # back without taking the keyboard from the app the model is driving
                if w in hidden:
                    show_quietly(w)
                else:
                    computer.click_through(int(w.winId()), False)
            tail = "" if in_batch else " Call screen_map for details."
            base = (result + "." + computer.front_note(before[0], computer.front_state())
                    if not result.startswith("error") else result)
            if self._ref is None or in_batch:
                done(base + tail if not result.startswith("error") else base)
                return

            def keep(got: Any) -> None:  # what this action left is what a planned next click aims at
                dnote = ""
                if isinstance(got, tuple) and isinstance(got[0], QImage) and not got[0].isNull():
                    if not result.startswith("error") and self._ref is not None:
                        dnote = diff_note(self._ref, got[0])
                        if name == "mouse_hover" and callable(getattr(self, "_ocr", None)):
                            res = changed_region(self._ref, got[0])
                            if res is not None and 0 < res[2] * res[3] <= 300_000:
                                crop = got[0].copy(QRect(res[0], res[1], res[2], res[3]))
                                self._prev_ref = self._ref
                                self._ref = got[0]

                                def read_tip() -> None:
                                    tip = " ".join((self._ocr(crop, empty="") or "").split())[:120]
                                    tip_note = f' Hover text: "{tip}".' if tip else ""
                                    done(base + dnote + tip_note + tail)

                                threading.Thread(target=read_tip, daemon=True).start()
                                return
                    if self._ref is not None:
                        self._prev_ref = self._ref
                    self._ref = got[0]
                done(base + dnote + tail if not result.startswith("error") else base)

            self._grab_screen(keep, primary=True)

        def work() -> None:
            result = act()
            # back on the Qt thread; the app needs a moment to react before the next look
            delay = 80 if in_batch else 250
            self._call.emit((lambda _done: QTimer.singleShot(delay, lambda: finish(result)),
                             {"event": threading.Event(), "result": None}))

        QTimer.singleShot(180 if hidden else 0, lambda: threading.Thread(target=work, daemon=True).start())

    def _shared_images(self, share: bool) -> list[str]:
        """The pictures of the newest user turn that has any (data URLs, at most MAX_ATTACHMENTS), when asked for."""
        from src.ui.chat_widgets import MAX_ATTACHMENTS

        turns = list(_host_chat(self)._history) if share else []
        return next((t["images"][:MAX_ATTACHMENTS] for t in reversed(turns) if t["role"] == "user" and t.get("images")), [])

    def _shared_notes(self, share: bool) -> str:
        """What a shared picture's task text adds: that the pictures are in the message, and their OCR text. Live,
        a model that takes an image without an error but cannot read it searched the disk for the file instead."""
        turns = list(_host_chat(self)._history) if share else []
        notes = next((t.get("image_notes") or [] for t in reversed(turns) if t["role"] == "user" and t.get("images")), [])
        if not notes:
            return ""
        return ("\n\n[Paperduck: the user's pictures are attached to this message itself; they are not files, so do "
                "not look for them on disk. Their text, read by OCR:]\n" + "\n".join(notes))[:MAX_SHARED_NOTE_CHARS]

    def _run_agent(self, task: str, effort: str, host: ReadOnlyHost, cancel: threading.Event) -> str:
        """A sub-agent's thread: one ask() with the chat's provider and model at the agent's own thinking level."""
        plugin = _host_chat(self)._plugin
        url, model = plugin.base_url, plugin.model
        where = f"the Project Folder {host.root}" if host.root else f"the home folder {Path.home()}"
        messages = [{"role": "system", "content": AGENT_PROMPT.format(calls=MAX_AGENT_CALLS, where=where, today=date.today())},
                    {"role": "user", "content": [{"type": "text", "text": task}, *(
                        {"type": "image_url", "image_url": {"url": u}} for u in host.images)] if host.images else task}]
        return ask(url, load_key(url), model, messages, thinking=fit_thinking(url, effort, model), host=host,
                   cancel=cancel).text

    def _add_mcp_server(self, args: dict[str, Any]) -> str:
        server, command = args.get("name"), args.get("command")
        cmd_args, env = args.get("args") or [], args.get("env") or {}
        if (not isinstance(server, str) or not server or safe_name(server) != server or SEP in server
                or not isinstance(command, str) or not command.strip()
                or not isinstance(cmd_args, list) or not all(isinstance(a, str) for a in cmd_args)
                or not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values())):
            return "error: give a name (letters, digits, single _ and -), a command, string args and string env values"
        line = " ".join([command.strip(), *cmd_args])
        env_note = env_summary(env)  # values too (NODE_OPTIONS can run code), secret-looking ones hidden
        if not self._allowed("add_mcp_server", f'The model wants to install MCP server "{server}". It will run this '
                             f"on your PC with your permissions whenever the AI chat starts:\n\n{line}{env_note}",
                             "MCP installs"):
            return "error: the user declined this MCP server"
        out = add_server(server, command.strip(), cmd_args, {str(k): v for k, v in env.items()}, self.mcp.path)
        if out.startswith("error"):
            return out
        tools = [s["function"]["name"] for s in self.mcp.specs() if self.mcp.server_of(s["function"]["name"]) == server]
        if not tools:
            return out + ", but it did not start (wrong command, missing npx / uvx, or a missing env value)"
        return out + (f"; it started with these tools: {', '.join(tools)}. If they are not in your tool list yet, "
                      "they are from the user's next message, so tell the user to send one.")

    def _project(self) -> Path | None:
        repo = getattr(self._win._state_machine.registry.get_plugin("project"), "repo", None)
        return repo if repo is not None and Path(repo).is_dir() else None  # a deleted folder would fail every command

    def _type_login(self, args: dict[str, Any]) -> str:
        """type_login: the saved value for the exact https host of the page that owns the focused box, typed by the
        pet. Order: code checks the box (Chromium, focused Edit inside an https page, password box for a password),
        a missing login is asked for on a masked card, then each use is approved one at a time (auto mode: the
        reviewer; a box that is not a real password box always asks the user), the focus is checked again and only
        then typed. The value never reaches the model, the chat history or the log."""
        field = args.get("field")
        if field not in login_vault.FIELDS:
            return "error: field is username or password"
        computer.focus_front_app()
        target = login_vault.focused_field()
        if isinstance(target, str):
            return target
        if field == "username" and target.password:
            return "error: the focused box is a password box; click the username box first"
        try:
            entry = login_vault.load().get(target.site)
        except OSError as exc:
            return f"error: {exc}"
        chat = _host_chat(self)
        fresh = entry is None or args.get("reenter") is True
        if fresh:
            got = self._waiting(lambda done: chat.ask_login(target.site, done))
            if not isinstance(got, tuple):
                return f"error: the user did not save a login for {target.site}"
            try:
                login_vault.store(target.site, *got)
            except (OSError, ValueError) as exc:
                return f"error: {exc}"
            entry = {"username": got[0], "password": got[1]}
        value = entry[field]
        if not value:
            return f"error: the login saved for {target.site} has no {field}; call again with reenter true"
        page = "\n\n" + target.url[:300]
        if field == "password" and not target.password:
            # a quirky page (a text box styled to hide letters): the user decides every time, auto mode or not
            text = (f"The model wants to type your saved password for {target.site} into a box this page does not "
                    "mark as a password box, so it may show the password on screen. Allow only if it is the "
                    f"password box you expect.{page}")
            ok = self._waiting(lambda done: chat.confirm(
                "login", text, f"saved password for {target.site}", done,
                review=(False, "not a real password box, so you decide")))
            if ok is not True:
                return "error: the user declined typing the password into a box that is not a password box"
        elif not fresh and not self._allowed(
                "login", f"The model wants to type your saved {field} for {target.site} into the focused {field} "
                f"box of this page. The pet checked the page is {target.site} itself, where the user saved this "
                f"login; the model never sees the value.{page}", f"saved {field} for {target.site}"):
            return "error: the user declined typing the saved login"
        computer.focus_front_app()  # an answer on the chat card took the keyboard
        now = login_vault.focused_field()
        if not isinstance(now, login_vault.Field) or (now.site, now.password, now.hwnd) != (
                target.site, target.password, target.hwnd):
            return "error: the focus moved before typing, so nothing was typed; click the box and try again"
        def still_there(n: int) -> bool:  # before each character (about 15 ms): same window in front, same box
            if computer.foreground_window() != target.hwnd:
                return False
            if n == 0:  # just checked above
                return True
            got = login_vault.focused_field()
            return isinstance(got, login_vault.Field) and (got.site, got.password, got.hwnd) == (
                target.site, target.password, target.hwnd)

        if field == "password":
            login_vault.remember_typed(target.hwnd, value)  # before typing: a partial reveal is watched too
        typed = computer.type_text(value, stop=self.cancel, guard=still_there)
        if typed < len(value):
            if self.cancel.is_set():
                return "error: stopped by the user part way through typing"
            return ("error: the focus left the box part way through, so typing stopped there; click the box, clear "
                    "it (ctrl+a, backspace) and call type_login again")
        return f"typed the saved {field} for {target.site}"

    def _allowed(self, key: str, text: str, what: str, risk: str = "") -> bool:
        """The user's answer in the chat; no answer within APPROVE_TIMEOUT_S counts as declined.
        A `risk` (destructive command) is asked every time, even when the chat allowed the tool before. So is a
        tool in OUTSIDE_GUARDED once the chat holds outside content, which could have told the model what to do."""
        chat = _host_chat(self)
        extra: dict[str, Any] = {"danger": risk} if risk else {"untrusted": True} if (
            self.outside and key in OUTSIDE_GUARDED) else {}
        if _host_auto_mode(self) and (extra or key not in AUTO_INSTANT | chat._allowed):  # allowed for this chat: no review
            # every action gets its own review: a cached yes would pass later clicks (Send, Buy) unseen
            extra["review"] = self._review(text)
            if self.cancel.is_set():
                return False
        ask = lambda done: chat.confirm(key, text, what, done, **extra)  # noqa: E731
        return self._waiting(ask) is True

    def _review(self, action: str) -> tuple[bool, str]:
        """Auto mode's reviewer on this chat's provider and model (worker thread; Stop ends it)."""
        chat = _host_chat(self)
        p = chat._plugin
        return review_action(p.base_url, load_key(p.base_url), p.model, chat.user_requests(), action, self.cancel)

    def _waiting(self, ask: Callable[[Callable[[Any], None]], None]) -> Any:
        """An approval on the Qt thread: the pet waits with the user (`activity` "approval") until it is answered."""
        self.activity.emit("approval")
        try:
            return self._on_gui(ask, self.approve_s)
        finally:
            self.activity.emit(getattr(self, "_tool", ""))

    def allow_link(self, name: str, url: str) -> bool:
        """A link the model built after reading local data: the user checks the full URL for their data."""
        verb = {"open_link": "open this link in your browser", "fetch_page": "fetch this page",
                "spawn_agent": "give a sub-agent this link"}.get(name, f"pass this link to the MCP tool {name}")
        return self._allowed("link", f"The model wants to {verb} after reading your files, clipboard or screen. "
                             f"Check the address does not carry your data:\n\n{url[:2000]}", "links",
                             "new link after reading your data")

    def see_image(self, root: Path | None, args: dict[str, Any]) -> tuple[str, str]:
        """view_image for this chat and its sub-agents: (text with size and OCR, data URL for a model that sees)."""
        from src.ui.chat_widgets import image_data_url

        path = None
        try:
            path = resolve(root, args.get("path"))
            raw = str(args.get("path") or "")
            if not path.is_file() and raw and not Path(raw).is_absolute():
                # MCP servers run in the scratch folder: Playwright's ".playwright-mcp\page-....png" is there
                alt = resolve(scratch_dir(), raw)
                path = alt if alt.is_file() else path
            if is_secret(path):
                return "error: that looks like a secrets file and view_image does not open those", ""
            image = load_image_file(path)
        except (OSError, ValueError) as exc:
            return f"error: {exc}" + (f" ({path}); give the full path" if path is not None else ""), ""
        small = image if max(image.width(), image.height()) <= MAX_OCR_SIDE else image.scaled(
            MAX_OCR_SIDE, MAX_OCR_SIDE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        text = self._ocr(small, empty="no text found in it")
        return f"{path.name}: {image.width()}x{image.height()} px. Text in it (OCR):\n{text}", image_data_url(image)

    def take_image(self) -> str:
        """The screenshot (data URL) of the last screen_map, once: the request loop sends it to a model that sees."""
        shot, self._shot = self._shot, ""
        return shot

    def take_saved(self) -> list[Path]:
        """Files saved since the last call (the chat shows a row for each)."""
        saved, self.saved = self.saved, []
        return saved

    # ------------------------------------------------------------------ thread bridge

    def _on_gui(self, fn: Callable[[Callable[[Any], None]], None], timeout: float = GUI_TIMEOUT_S) -> Any:
        """Run fn(done) on the Qt thread and wait for done(result)."""
        box: dict[str, Any] = {"event": threading.Event(), "result": None}
        self._call.emit((fn, box))
        box["event"].wait(timeout)
        return box["result"]

    def _on_call(self, item: tuple[Callable, dict[str, Any]]) -> None:
        fn, box = item

        def done(result: Any) -> None:
            box["result"] = result
            box["event"].set()

        try:
            fn(done)
        except Exception as exc:  # a tool bug must not leave the worker waiting
            done(f"error: {type(exc).__name__}")

    # ------------------------------------------------------------------ Qt-thread tools

    def _grab_screen(self, done: Callable[[Any], None], primary: bool = False) -> None:
        """Every screen read of a chat comes here; one that shows a browser, reader or chat app arms `outside`."""
        def got(result: Any) -> None:
            if result is not None and computer.visible_apps() & OUTSIDE_APPS:
                self.outside = True  # the page or message on screen could carry instructions, like a fetched page
            done(result)

        grab_screen(self._win, got, primary)

    def _save_file(self, file_name: str, content: str) -> str:
        """Nothing is written unless the user picks a path; the dialog itself confirms overwrites."""
        downloads = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
        chat = _host_chat(self)
        parent = self._win if getattr(chat, "_remote_only", False) else chat
        path, _filter = QFileDialog.getSaveFileName(parent, "Save file from Paperduck AI",
                                                    str(Path(downloads) / file_name))
        if not path:
            return "the user cancelled the save"
        try:
            Path(path).write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            return f"error: could not save ({exc.strerror or 'write failed'})"
        self.saved.append(Path(path))
        return f"saved to {path}"

    def _pet_tool(self, name: str, args: dict[str, Any]) -> str:
        cb = QGuiApplication.clipboard()
        if name == "read_clipboard":
            if cb is not None and clipboard_is_private(cb.mimeData()):
                return "the clipboard holds a copy a password manager marked private; it is not read"
            text = cb.text() if cb is not None else ""
            return text[:MAX_CLIP_CHARS] if text.strip() else "the clipboard has no text"
        if name == "copy_text":
            text = args.get("text")
            if not isinstance(text, str) or not text or cb is None:
                return "error: nothing to copy"
            cb.setText(text)
            return "copied to the clipboard"
        if name == "pet_status":
            rows = []
            for plugin in self._win._state_machine.registry.plugins:
                hud = plugin.get_hud_data()
                bars = ", ".join(f"{b.label} {(b.display_text or f'{b.value:.0f}%').strip()}" for b in hud.bars)
                rows.append(f"{hud.badge_left} [{hud.badge_right}] {bars}. {hud.detail}".strip())
            project = self._win._state_machine.registry.get_plugin("project")
            repo = getattr(project, "repo", None)
            errands = self._win._state_machine.registry.get_plugin("errands")
            waiting = errands.summary() if errands is not None else ""
            return "\n".join([*rows, f"Project Folder: {repo or 'none chosen'}",
                              "Errands waiting:\n" + waiting if waiting else "No errands waiting"])
        if name == "pet_action":
            action = args.get("action")
            if action not in PET_ACTIONS:
                return "error: not an allowed action"
            self._win.trigger_tool_action(action)
            return f"done: {PET_ACTIONS[action]}"
        if name == "run_macro":
            macro = self._win.macros.find(args.get("name") if isinstance(args.get("name"), str) else "")
            if macro is None:
                return "error: no such macro. Saved: " + ", ".join(m.name for m in self._win.macros.macros)
            if any(step in ("ask", "chat") for step, _arg in parse_recipe(macro.recipe)):  # it would use this busy chat
                return "error: that macro uses the AI chat itself, so the user has to run it"
            self._win._macro_runner.run(macro)
            return f"started macro {macro.name}: {macro.recipe}"
        if name == "open_link":
            url = args.get("url")
            if not isinstance(url, str) or not url.startswith(("https://", "http://")):
                return "error: web links (http or https) only"
            self._win.trigger_tool_action("web:" + url)
            return "opened in the browser"
        return "error: unknown tool"

    # ------------------------------------------------------------------ worker-thread OCR

    @staticmethod
    def _ocr(image: QImage, boxes: bool = False, empty: str = "no text found on screen") -> str:
        tmp = Path(tempfile.mkdtemp(prefix="paperduck_ocr_"))
        bmp = tmp / "screen.bmp"
        try:
            image.save(str(bmp), "BMP")
            text = ocr_image_file(bmp, boxes=boxes)
        finally:
            for leftover in (bmp, bmp.with_suffix(".ps1")):
                leftover.unlink(missing_ok=True)
            tmp.rmdir()
        if boxes:
            return text.strip()[:MAX_SCREEN_CHARS * 2]
        return text.strip()[:MAX_SCREEN_CHARS] or empty
