"""Mouse and keyboard for the AI chat's computer-use tools: Win32 SendInput on the primary monitor, in physical pixels
(the same pixels `screen_map` reports, because the app is per-monitor DPI aware). Windows only, no dependency."""

from __future__ import annotations

import ctypes
import math
import os
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from src.tools.ports import process_name
from src.tools.watch import is_private_title

MAX_TYPE_CHARS = 2000
MAX_SCROLL = 20
MAX_PATH_POINTS = 200
MAX_PATH_INTERP = 600
MAX_HOVER_MS = 2000
_DRAG_MODS = {"shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12}

_VK = {"enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08,
       "delete": 0x2E, "del": 0x2E, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
       "pageup": 0x21, "pagedown": 0x22, "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B}
_VK.update({f"f{n}": 0x6F + n for n in range(1, 13)})
# OEM punctuation keys (zoom is ctrl+plus / ctrl+minus); the models also write them as the symbol itself
_VK.update({"plus": 0xBB, "=": 0xBB, "equals": 0xBB, "minus": 0xBD, "-": 0xBD, "comma": 0xBC, ",": 0xBC,
            "period": 0xBE, ".": 0xBE, "[": 0xDB, "lbracket": 0xDB, "]": 0xDD, "rbracket": 0xDD,
            "\\": 0xDC, "backslash": 0xDC, "/": 0xBF, "slash": 0xBF, ";": 0xBA, "semicolon": 0xBA,
            "'": 0xDE, "quote": 0xDE, "`": 0xC0, "backtick": 0xC0, "insert": 0x2D, "ins": 0x2D,
            "printscreen": 0x2C, "prtsc": 0x2C, "capslock": 0x14})
_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}
_MODIFIERS = {0x10, 0x11, 0x12, 0x5B}

_KEYUP, _EXTENDEDKEY, _UNICODE = 0x2, 0x1, 0x4
_MOUSE_FLAGS = {"left": (0x2, 0x4), "right": (0x8, 0x10), "middle": (0x20, 0x40)}  # (down, up)
_WHEEL = 0x800
_HWHEEL = 0x1000
USER_ABORT_PX = 120


def parse_keys(keys: object) -> list[int]:
    """'ctrl+shift+s' -> virtual-key codes in press order; ValueError names what is not allowed."""
    if not isinstance(keys, str) or not keys.strip():
        raise ValueError("give keys like enter, ctrl+s or alt+tab")
    codes: list[int] = []
    combo = keys.lower().replace(" ", "")
    combo = combo[:-2] + "+plus" if combo.endswith("++") else combo  # "ctrl++" means ctrl and the plus key
    for part in combo.split("+"):
        if part in _VK:
            codes.append(_VK[part])
        elif len(part) == 1 and part.isascii() and part.isalnum():
            codes.append(ord(part.upper()))
        else:
            raise ValueError(f"unknown key {part!r}")
    if len(codes) > 4 or codes[-1] in _MODIFIERS and len(codes) > 1:
        raise ValueError("use up to three modifiers and one key, e.g. ctrl+shift+s")
    return codes


def parse_modifiers(modifiers: object) -> list[int]:
    """['shift', 'ctrl'] or 'ctrl+shift' -> deduplicated virtual-key codes; ValueError on invalid entries."""
    if modifiers is None or modifiers == [] or modifiers == "":
        return []
    if isinstance(modifiers, str):
        parts = [p.strip().lower() for p in modifiers.split("+") if p.strip()]
    elif isinstance(modifiers, (list, tuple)):
        if not all(isinstance(p, str) for p in modifiers):
            raise ValueError("modifiers may only be shift, ctrl or alt")
        parts = [p.strip().lower() for p in modifiers if p.strip()]
    else:
        raise ValueError("modifiers may only be shift, ctrl or alt")
    codes: list[int] = []
    for part in parts:
        vk = _DRAG_MODS.get(part)
        if vk is None:
            raise ValueError("modifiers may only be shift, ctrl or alt")
        if vk not in codes:
            codes.append(vk)
    return codes


class _Mouse(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("data", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("extra", wintypes.WPARAM)]


class _Key(ctypes.Structure):
    _fields_ = [("vk", wintypes.WORD), ("scan", wintypes.WORD), ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("extra", wintypes.WPARAM)]


class _Input(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _Mouse), ("ki", _Key)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


_USER32: Any = None


def _user32():
    global _USER32
    if _USER32 is None:
        u = ctypes.WinDLL("user32", use_last_error=True)
        u.GetForegroundWindow.restype = wintypes.HWND
        u.IsWindowVisible.argtypes = [wintypes.HWND]
        u.IsWindowVisible.restype = wintypes.BOOL
        u.IsIconic.argtypes = [wintypes.HWND]
        u.IsIconic.restype = wintypes.BOOL
        u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u.GetWindowRect.restype = wintypes.BOOL
        u.SetForegroundWindow.argtypes = [wintypes.HWND]
        u.SetForegroundWindow.restype = wintypes.BOOL
        _USER32 = u
    return _USER32


def _send(*inputs: _Input) -> None:
    array = (_Input * len(inputs))(*inputs)
    if _user32().SendInput(len(inputs), array, ctypes.sizeof(_Input)) != len(inputs):
        raise OSError("Windows refused the input (a higher-privilege window may be in front)")


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> _Input:
    return _Input(type=1, ki=_Key(vk, scan, flags, 0, 0))


def _mouse(flags: int, data: int = 0) -> _Input:
    return _Input(type=0, mi=_Mouse(0, 0, data & 0xFFFFFFFF, flags, 0, 0))


def screen_size() -> tuple[int, int]:
    user32 = _user32()
    return user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)


_DEFAULT_SCREEN_SIZE = screen_size


def virtual_screen_bounds() -> tuple[int, int, int, int]:
    """(left, top, width, height) spanning all monitors in physical pixels (primary-only if screen_size is mocked)."""
    if screen_size is not _DEFAULT_SCREEN_SIZE:
        w, h = screen_size()
        return 0, 0, w, h
    user32 = _user32()
    vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    if vw <= 0 or vh <= 0:
        w, h = screen_size()
        return 0, 0, w, h
    return user32.GetSystemMetrics(76), user32.GetSystemMetrics(77), vw, vh


def _check_point(x: int, y: int) -> tuple[int, int]:
    vx, vy, width, height = virtual_screen_bounds()
    if not (vx - 2 <= x < vx + width + 2 and vy - 2 <= y < vy + height + 2):
        raise ValueError(f"({x}, {y}) is off the screen, which is {width}x{height}")
    # a hair past the edge (the taskbar's corner, a centre rounded up) lands on the last pixel instead of failing
    return max(vx, min(x, vx + width - 1)), max(vy, min(y, vy + height - 1))


def cursor_pos() -> tuple[int, int] | None:
    """Current cursor position in physical screen pixels, or None if unavailable."""
    u32 = _user32()
    get_pos = getattr(u32, "GetCursorPos", None)
    if get_pos is None:
        return None
    pt = wintypes.POINT()
    if not get_pos(ctypes.byref(pt)):
        return None
    return int(pt.x), int(pt.y)


def _user_yanked(expected_x: int, expected_y: int) -> bool:
    """True when the user physically moved the mouse far away from our last target."""
    pos = cursor_pos()
    if pos is None:
        return False
    return math.hypot(pos[0] - expected_x, pos[1] - expected_y) > USER_ABORT_PX


def _move(x: int, y: int) -> None:
    x, y = _check_point(x, y)
    _user32().SetCursorPos(x, y)
    time.sleep(0.05)


def click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    if button not in _MOUSE_FLAGS:
        raise ValueError("button is left, right or middle")
    _move(x, y)
    down, up = _MOUSE_FLAGS[button]
    for _ in range(2 if double else 1):
        _send(_mouse(down))
        time.sleep(0.03)
        _send(_mouse(up))
        time.sleep(0.06)


def drag(x: int, y: int, to_x: int, to_y: int, steps: int = 12, button: str = "left",
         modifiers: list[int] | None = None, stop: threading.Event | None = None) -> bool:
    """Button drag in small moves (web canvases such as Canva ignore a jump); button and modifiers always come back up.
    Returns True if completed, False if aborted by `stop` or user mouse-yank."""
    if button not in _MOUSE_FLAGS:
        raise ValueError("button is left, right or middle")
    _check_point(to_x, to_y)  # checks the end is on screen before anything is pressed without warping there
    _move(x, y)
    mods = list(modifiers or ())
    down, up = _MOUSE_FLAGS[button]
    completed = True

    def _run_steps() -> bool:
        prev_x, prev_y = x, y
        time.sleep(0.08)
        for i in range(1, steps + 1):
            if stop is not None:
                if stop.is_set():
                    return False
                if i > 1 and _user_yanked(prev_x, prev_y):
                    stop.set()
                    return False
            nx, ny = round(x + (to_x - x) * i / steps), round(y + (to_y - y) * i / steps)
            _move(nx, ny)
            prev_x, prev_y = nx, ny
        time.sleep(0.08)
        return True

    if mods:
        _send(*[_key(vk) for vk in mods])
        try:
            _send(_mouse(down))
            try:
                completed = _run_steps()
            finally:
                _send(_mouse(up))
        finally:
            _send(*[_key(vk, flags=_KEYUP) for vk in reversed(mods)])
    else:
        _send(_mouse(down))
        try:
            completed = _run_steps()
        finally:
            _send(_mouse(up))
    return completed


def path(points: list[tuple[int, int]], button: str = "left", modifiers: list[int] | None = None,
         step_px: int = 8, delay: float = 0.016, stop: threading.Event | None = None) -> int:
    """Trace a smooth multi-point stroke while holding `button` and optional `modifiers`. Returns vertices reached."""
    if button not in _MOUSE_FLAGS:
        raise ValueError("button is left, right or middle")
    if not (2 <= len(points) <= MAX_PATH_POINTS):
        raise ValueError(f"give 2 to {MAX_PATH_POINTS} points")
    if not (2 <= step_px <= 40):
        raise ValueError("step_px must be between 2 and 40")
    clamped = [_check_point(int(px), int(py)) for px, py in points]

    raw_segs: list[list[tuple[int, int]]] = []
    for (x0, y0), (x1, y1) in zip(clamped, clamped[1:]):
        segs = max(1, min(60, round(math.hypot(x1 - x0, y1 - y0) / step_px)))
        raw_segs.append([(round(x0 + (x1 - x0) * i / segs), round(y0 + (y1 - y0) * i / segs))
                         for i in range(1, segs + 1)])
    total_steps = sum(len(s) for s in raw_segs)
    if total_steps > MAX_PATH_INTERP:
        ratio = MAX_PATH_INTERP / total_steps
        trimmed: list[list[tuple[int, int]]] = []
        for s in raw_segs:
            keep_n = max(1, round(len(s) * ratio))
            if keep_n >= len(s):
                trimmed.append(s)
            else:
                step_pts = [s[min(len(s) - 1, round(k * (len(s) - 1) / max(1, keep_n - 1)))]
                            for k in range(keep_n)]
                step_pts[-1] = s[-1]
                trimmed.append(step_pts)
        raw_segs = trimmed

    _move(*clamped[0])
    mods = list(modifiers or ())
    down, up = _MOUSE_FLAGS[button]
    user32 = _user32()
    vx, vy, vw, vh = virtual_screen_bounds()
    if mods:
        _send(*[_key(vk) for vk in mods])
    reached = 1
    prev_x, prev_y = clamped[0]
    try:
        _send(_mouse(down))
        try:
            time.sleep(0.06)
            for seg in raw_segs:
                for cx, cy in seg:
                    if stop is not None:
                        if stop.is_set():
                            return reached
                        if _user_yanked(prev_x, prev_y):
                            stop.set()
                            return reached
                    ax = round((cx - vx) * 65535 / max(1, vw - 1))
                    ay = round((cy - vy) * 65535 / max(1, vh - 1))
                    _send(_Input(type=0, mi=_Mouse(ax, ay, 0, 0x8001 | 0x4000, 0, 0)))  # MOVE|ABSOLUTE|VIRTUALDESK
                    prev_x, prev_y = cx, cy
                    time.sleep(delay)
                reached += 1
            time.sleep(0.06)
        finally:
            _send(_mouse(up))
    finally:
        if mods:
            _send(*[_key(vk, flags=_KEYUP) for vk in reversed(mods)])
    return reached


def hover(x: int, y: int, wait_ms: int = 500) -> None:
    """Move the cursor to (x, y) without clicking and pause for tooltips/hover state."""
    _move(x, y)
    time.sleep(max(0, min(wait_ms, MAX_HOVER_MS)) / 1000.0)


def scroll(amount: int, x: int | None = None, y: int | None = None,
           horizontal: bool = False, modifiers: list[int] | None = None) -> None:
    """Positive scrolls down (or right when `horizontal`), negative up/left; the wheel acts where the cursor is."""
    if x is not None and y is not None:
        _move(x, y)
    clamped = max(-MAX_SCROLL, min(MAX_SCROLL, amount))
    flag = _HWHEEL if horizontal else _WHEEL
    delta = (clamped * 120) if horizontal else (-clamped * 120)
    mods = list(modifiers or ())
    if mods:
        _send(*[_key(vk) for vk in mods])
        try:
            _send(_mouse(flag, delta))
        finally:
            _send(*[_key(vk, flags=_KEYUP) for vk in reversed(mods)])
    else:
        _send(_mouse(flag, delta))


def type_text(text: str, delay: float = 0.008, stop: threading.Event | None = None,
              guard: Callable[[int], bool] | None = None) -> int:
    """Types `text`; returns how many characters went out (fewer once `stop` is set: Stop ends it mid-text, or
    once `guard(n)`, asked before character n, says False)."""
    chars = text.replace("\r\n", "\n")[:MAX_TYPE_CHARS]  # one Enter per line break, not two
    for n, char in enumerate(chars):
        if (stop is not None and stop.is_set()) or (guard is not None and not guard(n)):
            return n
        if char in "\r\n":
            _send(_key(0x0D), _key(0x0D, flags=_KEYUP))
        elif char == "\t":
            _send(_key(0x09), _key(0x09, flags=_KEYUP))
        else:
            data = char.encode("utf-16-le")
            for i in range(0, len(data), 2):  # a surrogate pair is two units
                unit = int.from_bytes(data[i:i + 2], "little")
                _send(_key(0, unit, _UNICODE), _key(0, unit, _UNICODE | _KEYUP))
        time.sleep(delay)
    return len(chars)


def press(codes: list[int]) -> None:
    downs = [_key(vk, flags=_EXTENDEDKEY if vk in _EXTENDED else 0) for vk in codes]
    ups = [_key(vk, flags=_KEYUP | (_EXTENDEDKEY if vk in _EXTENDED else 0)) for vk in reversed(codes)]
    _send(*downs, *ups)


# The "skeleton" of the front window: its clickable controls from Windows UI Automation, each with an exact rectangle
# (icons and unlabeled buttons that OCR cannot see). Windows PowerShell 5.1 ships the .NET UIAutomationClient.
_UIA_PS = r"""
param([long]$Hwnd, [string]$Popups = '', [string]$Query = '', [string]$ControlTypes = '')
$ErrorActionPreference = 'Stop'
if (-not ('BS.Dpi' -as [type])) {  # a warm worker runs this again in the same process
    Add-Type -Namespace BS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr c);'
}
[void][BS.Dpi]::SetThreadDpiAwarenessContext([IntPtr]-4)
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
if (-not $script:paperduckUiaCond2) {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
    $A = [System.Windows.Automation.AutomationElement]
    $CT = [System.Windows.Automation.ControlType]
    $types = @($CT::Button, $CT::CheckBox, $CT::ComboBox, $CT::Edit, $CT::Hyperlink, $CT::ListItem, $CT::MenuItem,
        $CT::RadioButton, $CT::TabItem, $CT::TreeItem, $CT::SplitButton, $CT::Slider, $CT::DataItem, $CT::Document,
        $CT::Custom, $CT::Image)
    $any = [System.Windows.Automation.OrCondition]::new([System.Windows.Automation.Condition[]]@(
        $types | ForEach-Object { [System.Windows.Automation.PropertyCondition]::new($A::ControlTypeProperty, $_) }))
    $visible = [System.Windows.Automation.PropertyCondition]::new($A::IsOffscreenProperty, $false)
    $script:paperduckUiaCond2 = [System.Windows.Automation.AndCondition]::new($any, $visible)
    $req = [System.Windows.Automation.CacheRequest]::new()
    $req.TreeScope = [System.Windows.Automation.TreeScope]::Element
    $req.AutomationElementMode = [System.Windows.Automation.AutomationElementMode]::Full
    foreach ($prop in @($A::BoundingRectangleProperty, $A::NameProperty, $A::AutomationIdProperty, $A::HelpTextProperty,
        $A::ControlTypeProperty, $A::IsEnabledProperty, $A::HasKeyboardFocusProperty, $A::AcceleratorKeyProperty,
        [System.Windows.Automation.TogglePattern]::ToggleStateProperty,
        [System.Windows.Automation.SelectionItemPattern]::IsSelectedProperty,
        [System.Windows.Automation.ExpandCollapsePattern]::ExpandCollapseStateProperty,
        [System.Windows.Automation.RangeValuePattern]::ValueProperty,
        [System.Windows.Automation.RangeValuePattern]::MaximumProperty,
        [System.Windows.Automation.ValuePattern]::ValueProperty)) {
        $req.Add($prop)
    }
    foreach ($pat in @([System.Windows.Automation.TogglePattern]::Pattern,
        [System.Windows.Automation.SelectionItemPattern]::Pattern,
        [System.Windows.Automation.ExpandCollapsePattern]::Pattern,
        [System.Windows.Automation.RangeValuePattern]::Pattern,
        [System.Windows.Automation.ValuePattern]::Pattern,
        [System.Windows.Automation.InvokePattern]::Pattern)) {
        $req.Add($pat)
    }
    $script:paperduckUiaReq = $req
}
$A = [System.Windows.Automation.AutomationElement]
$roots = New-Object System.Collections.Generic.List[long]
foreach ($tok in ($Popups -split ',')) {
    [long]$pv = 0
    if ([long]::TryParse($tok.Trim(), [ref]$pv) -and $pv -and $pv -ne $Hwnd -and -not $roots.Contains($pv)) { $roots.Add($pv) }
}
if ($Hwnd -and -not $roots.Contains($Hwnd)) { $roots.Add($Hwnd) }
$count = 0
$typeFilter = New-Object System.Collections.Generic.HashSet[string]
if ($ControlTypes) {
    foreach ($t in ($ControlTypes -split ',')) {
        $tt = $t.Trim().ToLowerInvariant()
        if ($tt) { [void]$typeFilter.Add($tt) }
    }
}
$hasQuery = [bool]($Query -and $Query.Trim())
$needle = if ($hasQuery) { $Query.Trim() } else { '' }
foreach ($rh in $roots) {
    if ($count -ge 400) { break }
    $root = $null
    try { $root = $A::FromHandle([IntPtr]$rh) } catch { continue }
    if (-not $root) { continue }
    $useCache = $false
    $found = $null
    $scope = $null
    try {
        $scope = $script:paperduckUiaReq.Activate()
        $found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $script:paperduckUiaCond2)
        $useCache = $true
    } catch {
        $useCache = $false
    } finally {
        if ($scope) { $scope.Dispose() }
    }
    if (-not $useCache) {
        try { $found = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $script:paperduckUiaCond2) } catch { continue }
    }
    foreach ($e in $found) {
        try {
            $cur = if ($useCache) { $e.Cached } else { $e.Current }
            $r = $cur.BoundingRectangle
            if ($r.IsEmpty -or $r.Width -lt 4 -or $r.Height -lt 4) { continue }
            $name = $cur.Name
            if (-not $name) { $name = $cur.AutomationId }
            if (-not $name) { $name = $cur.HelpText }
            if ($name) { $name = ($name -replace '[\r\n\t]+', ' ').Trim() } else { $name = '' }
            $kind = $cur.ControlType.ProgrammaticName -replace '^ControlType\.', ''
            if ($typeFilter.Count -gt 0 -and -not $typeFilter.Contains($kind.ToLowerInvariant())) { continue }
            if ($hasQuery -and $name.IndexOf($needle, [System.StringComparison]::OrdinalIgnoreCase) -lt 0) { continue }
            $tags = @()
            if (-not $cur.IsEnabled) { $tags += 'disabled' }
            if ($cur.HasKeyboardFocus) { $tags += 'focus' }
            $acc = $cur.AcceleratorKey
            if ($acc) {
                $accClean = ($acc -replace '[\r\n\t]+', ' ').Trim()
                if ($accClean.Length -gt 20) { $accClean = $accClean.Substring(0, 20) }
                if ($accClean) { $tags += "key=$accClean" }
            }
            $tp = $null
            if ($useCache) {
                if ($e.TryGetCachedPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tp)) {
                    $ts = $tp.Cached.ToggleState.ToString()
                    if ($ts -eq 'On') { $tags += 'on' }
                    elseif ($ts -eq 'Off') { $tags += 'off' }
                    elseif ($ts -eq 'Indeterminate') { $tags += 'mixed' }
                }
            } elseif ($e.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$tp)) {
                $ts = $tp.Current.ToggleState.ToString()
                if ($ts -eq 'On') { $tags += 'on' }
                elseif ($ts -eq 'Off') { $tags += 'off' }
                elseif ($ts -eq 'Indeterminate') { $tags += 'mixed' }
            }
            $sp = $null
            if ($useCache) {
                if ($e.TryGetCachedPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$sp) -and $sp.Cached.IsSelected) { $tags += 'selected' }
            } elseif ($e.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$sp) -and $sp.Current.IsSelected) {
                $tags += 'selected'
            }
            $ep = $null
            if ($useCache) {
                if ($e.TryGetCachedPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$ep) -and $ep.Cached.ExpandCollapseState.ToString() -eq 'Expanded') { $tags += 'expanded' }
            } elseif ($e.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$ep) -and $ep.Current.ExpandCollapseState.ToString() -eq 'Expanded') {
                $tags += 'expanded'
            }
            $rv = $null
            $vp = $null
            if ($useCache) {
                if ($e.TryGetCachedPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$rv)) {
                    $tags += "val=$([math]::Round($rv.Cached.Value, 2))/$([math]::Round($rv.Cached.Maximum, 2))"
                } elseif ($kind -ne 'Document' -and $e.TryGetCachedPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
                    $v = $vp.Cached.Value
                    if ($v) {
                        $vClean = ($v -replace '[\r\n\t]+', ' ').Trim()
                        if ($vClean.Length -gt 30) { $vClean = $vClean.Substring(0, 30) }
                        if ($vClean) { $tags += "val=$vClean" }
                    }
                }
            } else {
                if ($e.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$rv)) {
                    $tags += "val=$([math]::Round($rv.Current.Value, 2))/$([math]::Round($rv.Current.Maximum, 2))"
                } elseif ($kind -ne 'Document' -and $e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
                    $v = $vp.Current.Value
                    if ($v) {
                        $vClean = ($v -replace '[\r\n\t]+', ' ').Trim()
                        if ($vClean.Length -gt 30) { $vClean = $vClean.Substring(0, 30) }
                        if ($vClean) { $tags += "val=$vClean" }
                    }
                }
            }
            if ($kind -in @('Custom', 'Image') -and -not $name -and $tags.Count -eq 0) {
                $ip = $null
                $hasInv = if ($useCache) { $e.TryGetCachedPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip) } else { $e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$ip) }
                if (-not $hasInv) { continue }
            }
            if ($tags.Count -gt 0) {
                "$([int]$r.X),$([int]$r.Y),$([int]$r.Width),$([int]$r.Height)`t$kind`t$name`t$($tags -join ', ')"
            } else {
                "$([int]$r.X),$([int]$r.Y),$([int]$r.Width),$([int]$r.Height)`t$kind`t$name"
            }
            $count++
            if ($count -ge 400) { break }
        } catch { }
    }
}
"""


def foreground_window() -> int:
    return int(_user32().GetForegroundWindow() or 0)


def front_state() -> tuple[str, bool]:
    """(title, is a dialog) of the front window."""
    user32 = _user32()
    user32.GetForegroundWindow.restype = wintypes.HWND
    return window_state(user32.GetForegroundWindow())


def window_state(hwnd: int | None) -> tuple[str, bool]:
    """Modal dialogs are owned windows, so an owner means one is open."""
    if not hwnd:
        return "", False
    user32 = _user32()
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    return buf.value, bool(user32.GetWindow(hwnd, 4))  # GW_OWNER


def front_note(before: tuple[str, bool], after: tuple[str, bool]) -> str:
    """What an action did to the front window, in a line the model can act on without another screen_map."""
    (was, _), (now, dialog) = before, after
    show = lambda t: repr("a private window" if is_private_title(t) else t[:80])  # noqa: E731
    note = f" Front window changed: {show(was)} -> {show(now)}." if now != was else f" Front window unchanged: {show(now)}."
    if dialog:
        note += " A dialog is open in front: answer it or press esc before anything else."
    return note


def _ours(user32, hwnd: int) -> bool:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return pid.value == os.getpid()


def ours_at(x: int, y: int) -> bool:
    """Whether the pet or its chat is the window at a screen point (computer actions click through them)."""
    user32 = _user32()
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    hwnd = user32.WindowFromPoint(wintypes.POINT(x, y))
    return bool(hwnd) and _ours(user32, int(hwnd))


def front_app() -> int:
    """The window the user is working in: the foreground one, or when that is ours (the pet, its chat or menu),
    the first real app window under ours in the z-order (visible, not minimized, not cloaked, not a tool window,
    titled). 0 when there is none."""
    user32 = _user32()
    fg = foreground_window()
    if fg and not _ours(user32, fg):
        return fg
    user32.GetTopWindow.restype = user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    hwnd = user32.GetTopWindow(None)
    for _ in range(1000):  # a z-order walk; the bound guards against a window list that changes under us
        if not hwnd:
            return 0
        cloaked = wintypes.DWORD()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        if (user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and not cloaked.value
                and not user32.GetWindowLongW(hwnd, -20) & 0x80  # WS_EX_TOOLWINDOW
                and user32.GetWindowTextLengthW(hwnd) > 0 and not _ours(user32, hwnd)):
            return int(hwnd)
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    return 0


def apps_at_points(windows: list[tuple[tuple[int, int, int, int], str]], points: list[tuple[int, int]]) -> set[str]:
    """Exe names of the topmost window at each point. `windows` is ((left, top, right, bottom), exe), topmost first."""
    found = set()
    for x, y in points:
        hit = next((exe for (left, top, right, bottom), exe in windows if left <= x < right and top <= y < bottom), "")
        if hit:
            found.add(hit.lower())
    return found


def visible_apps(cols: int = 8, rows: int = 5) -> set[str]:
    """Apps that show on screen: the topmost real window (not ours, minimized, cloaked or a tool window) at a grid
    of points over every monitor. shortcut: a window smaller than one grid cell can be missed; denser if needed."""
    user32 = _user32()
    user32.GetTopWindow.restype = user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)  # the virtual screen: every monitor
    width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    windows: list[tuple[tuple[int, int, int, int], str]] = []
    names: dict[int, str] = {}
    hwnd = user32.GetTopWindow(None)
    for _ in range(1000):  # bounded z-order walk, as in front_app
        if not hwnd:
            break
        cloaked = wintypes.DWORD()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        if (user32.IsWindowVisible(hwnd) and not user32.IsIconic(hwnd) and not cloaked.value
                and not user32.GetWindowLongW(hwnd, -20) & 0x80 and not _ours(user32, hwnd)):  # WS_EX_TOOLWINDOW
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in names:
                names[pid.value] = process_name(pid.value)
            windows.append(((rect.left, rect.top, rect.right, rect.bottom), names[pid.value]))
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    points = [(left + width * (2 * c + 1) // (2 * cols), top + height * (2 * r + 1) // (2 * rows))
              for c in range(cols) for r in range(rows)]
    return apps_at_points(windows, points)


def focus_front_app() -> int:
    """Give the keyboard back to the app under the pet and chat when one of ours has it (the chat does after the
    user presses Enter), so typed keys and the described 'front window' are that app. Returns its handle."""
    hwnd = front_app()
    if hwnd and hwnd != foreground_window():
        _user32().SetForegroundWindow(wintypes.HWND(hwnd))  # allowed: we are the foreground process
        time.sleep(0.05)
    return hwnd


def window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(64)
    _user32().GetClassNameW(wintypes.HWND(hwnd), buf, 64)
    return buf.value


def is_topmost(hwnd: int) -> bool:
    user32 = _user32()
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    return bool(user32.GetWindowLongW(hwnd, -20) & 0x8)  # WS_EX_TOPMOST


def under_foreign_topmost(hwnd: int) -> bool:
    """True when a visible topmost window of another process sits above `hwnd` in the z-order (so re-raising it
    would change something); False means it is already on top and a SetWindowPos would be churn."""
    user32 = _user32()
    user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    above = user32.GetWindow(wintypes.HWND(hwnd), 3)  # GW_HWNDPREV
    for _ in range(1000):
        if not above:
            return False
        if user32.IsWindowVisible(above) and user32.GetWindowLongW(above, -20) & 0x8 and not _ours(user32, above):
            return True
        above = user32.GetWindow(above, 3)
    return True


def exclude_from_capture(hwnd: int, on: bool) -> bool:
    """WDA_EXCLUDEFROMCAPTURE: the window stays on screen but is left out of screen grabs (Windows 10 2004+).
    False when Windows refused, so the caller hides the window instead."""
    return bool(_user32().SetWindowDisplayAffinity(wintypes.HWND(hwnd), 0x11 if on else 0))


def click_through(hwnd: int, on: bool) -> bool:
    """Toggle WS_EX_TRANSPARENT on a layered window: clicks and the wheel fall to the window below while it stays
    drawn. False when the window is not layered (the style would not change hit-testing), so the caller hides it."""
    user32 = _user32()
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    style = user32.GetWindowLongW(hwnd, -20)
    if not style & 0x80000:  # WS_EX_LAYERED
        return False
    user32.SetWindowLongW(hwnd, -20, style | 0x20 if on else style & ~0x20)
    return True


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD), ("szDevice", ctypes.c_wchar * 32)]


def window_on_monitor(hwnd: int) -> tuple[str, tuple[int, int, int, int], tuple[int, int]] | None:
    """(monitor device name, window rect inside that monitor, monitor size), all in physical pixels, or None.
    Uses the DWM frame bounds, which leave out the invisible resize border and shadow."""
    if not hwnd:
        return None
    user32 = _user32()
    rect = wintypes.RECT()
    if ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect)) != 0:
        if not user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    mon = user32.MonitorFromWindow(wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
    info = _MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if not mon or not user32.GetMonitorInfoW(mon, ctypes.byref(info)):
        return None
    m = info.rcMonitor
    left, top = max(rect.left, m.left) - m.left, max(rect.top, m.top) - m.top
    right, bottom = min(rect.right, m.right) - m.left, min(rect.bottom, m.bottom) - m.top
    if right - left < 40 or bottom - top < 40:
        return None
    return info.szDevice, (left, top, right, bottom), (m.right - m.left, m.bottom - m.top)


_UIA_HEAD = r"""
if (-not ('BS.Dpi' -as [type])) {
    Add-Type -Namespace BS -Name Dpi -MemberDefinition '[DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr c);'
}
[void][BS.Dpi]::SetThreadDpiAwarenessContext([IntPtr]-4)
if (-not $script:paperduckUiaBase) {
    Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, WindowsBase
    $script:paperduckUiaBase = $true
}
$A = [System.Windows.Automation.AutomationElement]
"""

# The element under a point and its ancestors, one `localized type<TAB>name` line each (deepest first)
_UIA_AT_PS = "param([int]$X, [int]$Y)\n" + _UIA_HEAD + r"""
$e = $A::FromPoint([System.Windows.Point]::new($X, $Y))
$walk = [System.Windows.Automation.TreeWalker]::ControlViewWalker
for ($i = 0; $e -and $i -lt 8; $i++) {
    "$($e.Current.LocalizedControlType)`t$($e.Current.Name -replace '\s+', ' ')"
    $e = $walk.GetParent($e)
}
"""

# Press the control named $Name whose centre is (X, Y) in window $Hwnd through its Invoke pattern; prints 'invoked'
_UIA_INVOKE_PS = "param([long]$Hwnd, [int]$X, [int]$Y, [string]$Name)\n" + _UIA_HEAD + r"""
$cond = [System.Windows.Automation.PropertyCondition]::new($A::NameProperty, $Name)
foreach ($e in $A::FromHandle([IntPtr]$Hwnd).FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
    $r = $e.Current.BoundingRectangle
    if ([math]::Abs($r.X + $r.Width / 2 - $X) -le 3 -and [math]::Abs($r.Y + $r.Height / 2 - $Y) -le 3) {
        $p = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); 'invoked' }
        break
    }
}
"""


def uia_at(x: int, y: int) -> list[tuple[str, str]]:
    """(localized type, name) of the element at a screen point and its ancestors; [] when unknown."""
    from src.tools import warm_ps

    out = warm_ps.run("uia", _UIA_AT_PS, {"X": x, "Y": y}, 10.0) or ""
    return [(kind.casefold(), name) for kind, _tab, name in (line.partition("\t") for line in out.splitlines())]


def uia_invoke(hwnd: int, x: int, y: int, name: str) -> bool:
    """Press the control `name` centred at (x, y) without the mouse (for a target something else covers)."""
    from src.tools import warm_ps

    return (warm_ps.run("uia", _UIA_INVOKE_PS, {"Hwnd": hwnd, "X": x, "Y": y, "Name": name}, 10.0) or "").strip() == "invoked"


_UIA_FOCUS_PS = _UIA_HEAD + r"""
$e = $null
try { $e = $A::FocusedElement } catch { }
if (-not $e) { return }
$c = $e.Current
$kind = $c.ControlType.ProgrammaticName -replace '^ControlType\.', ''
$name = ($c.Name -replace '[\r\n\t]+', ' ').Trim()
if ($name.Length -gt 50) { $name = $name.Substring(0, 50) }
$state = @()
if (-not $c.IsEnabled) { $state += 'disabled' }
$vp = $null
if (-not $c.IsPassword -and $kind -ne 'Document' -and $e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) {
    $v = ($vp.Current.Value -replace '[\r\n\t]+', ' ').Trim()
    if ($v.Length -gt 40) { $v = $v.Substring(0, 40) }
    if ($v) { $state += "val=$v" }
}
$suffix = if ($state.Count -gt 0) { " ($($state -join ', '))" } else { '' }
"$kind`t$name$suffix"
"""


_UIA_STATE_PS = "param([int]$X, [int]$Y)\n" + _UIA_HEAD + r"""
$e = $null
try { $e = $A::FromPoint([System.Windows.Point]::new($X, $Y)) } catch { }
if (-not $e) { return }
$c = $e.Current
$kind = $c.ControlType.ProgrammaticName -replace '^ControlType\.', ''
$name = ($c.Name -replace '[\r\n\t]+', ' ').Trim()
if ($name.Length -gt 50) { $name = $name.Substring(0, 50) }
$flags = @()
if (-not $c.IsEnabled) { $flags += 'disabled' }
if ($c.IsOffscreen) { $flags += 'offscreen' }
"$kind`t$name`t$($flags -join ',')"
"""


def uia_focus_note() -> str:
    from src.tools import warm_ps

    out = (warm_ps.run("uia", _UIA_FOCUS_PS, {}, 4.0) or "").strip()
    if not out or "\t" not in out:
        return ""
    kind, _t, rest = out.partition("\t")
    return f" Focus: [{kind}] {rest}." if rest else f" Focus: [{kind}]."


def uia_zero_change_hint(x: int, y: int) -> str:
    from src.tools import warm_ps

    out = (warm_ps.run("uia", _UIA_STATE_PS, {"X": x, "Y": y}, 4.0) or "").strip()
    if not out:
        return ""
    parts = out.split("\t")
    kind = parts[0] if len(parts) > 0 else ""
    label = parts[1] if len(parts) > 1 else ""
    flags = parts[2] if len(parts) > 2 else ""
    if "disabled" in flags:
        return f' UIA: [{kind}] "{label}" is disabled.'
    if "offscreen" in flags:
        return f' UIA: [{kind}] "{label}" is offscreen.'
    return ""


def restore_cursor_and_window(hwnd: int, pos: tuple[int, int] | None) -> None:
    if pos is not None:
        try:
            _move(pos[0], pos[1])
        except (ValueError, OSError):
            pass
    if hwnd and not _ours(_user32(), hwnd):
        _bring_to_front(_user32(), hwnd)


WINDOW_ACTIONS = ("focus", "maximize", "minimize", "restore", "snap_left", "snap_right")
UIA_ACTIONS = ("invoke", "set_value", "toggle", "select", "expand", "collapse", "range", "focus")


def list_windows(max_windows: int = 40) -> list[dict[str, Any]]:
    """Visible (and minimized) top-level app windows in z-order, excluding our own."""
    user32 = _user32()
    user32.GetTopWindow.restype = user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    fg = foreground_window()
    out: list[dict[str, Any]] = []
    names: dict[int, str] = {}
    hwnd = user32.GetTopWindow(None)
    for _ in range(1000):
        if not hwnd or len(out) >= max_windows:
            break
        cloaked = wintypes.DWORD()
        ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(hwnd), 14, ctypes.byref(cloaked), 4)
        if (user32.IsWindowVisible(hwnd) and not cloaked.value
                and not (user32.GetWindowLongW(hwnd, -20) & 0x80)
                and user32.GetWindowTextLengthW(hwnd) > 0 and not _ours(user32, hwnd)):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            raw_title = buf.value.strip()
            title = "a private window" if is_private_title(raw_title) else raw_title[:120]
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in names:
                names[pid.value] = process_name(pid.value)
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            state = "minimized" if user32.IsIconic(hwnd) else ("maximized" if user32.IsZoomed(hwnd) else "normal")
            out.append({
                "hwnd": int(hwnd),
                "title": title,
                "exe": names[pid.value],
                "state": state,
                "rect": [rect.left, rect.top, max(0, rect.right - rect.left), max(0, rect.bottom - rect.top)],
                "foreground": int(hwnd) == fg,
            })
        hwnd = user32.GetWindow(hwnd, 2)  # GW_HWNDNEXT
    return out


def focus_window(hwnd: int | None = None, title: str | None = None, exe: str | None = None, action: str = "focus") -> str:
    """Focus, restore, minimize, maximize, or snap a window on screen."""
    if action not in WINDOW_ACTIONS:
        raise ValueError(f"action must be one of {', '.join(WINDOW_ACTIONS)}")
    windows = list_windows()
    if not windows:
        return "error: no open application windows found"
    target: dict[str, Any] | None = None
    if hwnd is not None:
        target = next((w for w in windows if w["hwnd"] == hwnd), None)
        if target is None:
            return f"error: no open window with hwnd {hwnd}"
    else:
        t_q = (title or "").strip().casefold()
        e_q = (exe or "").strip().casefold()
        if not t_q and not e_q:
            raise ValueError("give hwnd, title, or exe")
        hits = [
            w for w in windows
            if (not t_q or t_q in w["title"].casefold()) and (not e_q or e_q in w["exe"].casefold())
        ]
        if not hits:
            sample = "; ".join(f'{w["exe"]}: "{w["title"][:40]}"' for w in windows[:6])
            return f"error: no matching window found (open: {sample})"
        target = hits[0]
    thwnd = int(target["hwnd"])
    user32 = _user32()
    if _ours(user32, thwnd):
        return "error: cannot target Paperduck's own window"
    if action == "minimize":
        user32.ShowWindow(wintypes.HWND(thwnd), 6)  # SW_MINIMIZE
    elif action == "maximize":
        user32.ShowWindow(wintypes.HWND(thwnd), 3)  # SW_MAXIMIZE
        _bring_to_front(user32, thwnd)
    elif action == "restore":
        user32.ShowWindow(wintypes.HWND(thwnd), 9)  # SW_RESTORE
        _bring_to_front(user32, thwnd)
    elif action in ("snap_left", "snap_right"):
        user32.ShowWindow(wintypes.HWND(thwnd), 9)
        work = wintypes.RECT()
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0):  # SPI_GETWORKAREA
            ww = max(200, work.right - work.left)
            wh = max(200, work.bottom - work.top)
            half = ww // 2
            sx = work.left if action == "snap_left" else work.left + half
            sw = half if action == "snap_left" else ww - half
            user32.SetWindowPos(wintypes.HWND(thwnd), 0, sx, work.top, sw, wh, 0x0040)  # SWP_SHOWWINDOW
        _bring_to_front(user32, thwnd)
    else:  # focus
        if user32.IsIconic(wintypes.HWND(thwnd)):
            user32.ShowWindow(wintypes.HWND(thwnd), 9)
        _bring_to_front(user32, thwnd)
    return f'{action} "{target["title"]}" ({target["exe"]})'


def _bring_to_front(user32: Any, hwnd: int) -> None:
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return
    if user32.SetForegroundWindow(wintypes.HWND(hwnd)):
        time.sleep(0.05)
        return
    fg_tid = user32.GetWindowThreadProcessId(wintypes.HWND(fg), None) if fg else 0
    cur_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    attached = bool(fg_tid and fg_tid != cur_tid and user32.AttachThreadInput(cur_tid, fg_tid, True))
    try:
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)
    time.sleep(0.05)


def popup_windows(hwnd: int) -> list[int]:
    """Up to 4 visible popup/owned windows belonging to `hwnd`'s process (context menus, dropdowns)."""
    if not hwnd:
        return []
    user32 = _user32()
    if not hasattr(user32, "GetTopWindow"):
        return []
    if hasattr(user32, "GetDesktopWindow") and hwnd == user32.GetDesktopWindow():
        return []
    user32.GetTopWindow.restype = user32.GetWindow.restype = wintypes.HWND
    user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    target_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(target_pid))
    out: list[int] = []
    cur = user32.GetTopWindow(None)
    for _ in range(500):
        if not cur or len(out) >= 4:
            break
        if int(cur) != hwnd and user32.IsWindowVisible(cur) and not user32.IsIconic(cur):
            cloaked = wintypes.DWORD()
            ctypes.windll.dwmapi.DwmGetWindowAttribute(wintypes.HWND(cur), 14, ctypes.byref(cloaked), 4)
            if not cloaked.value:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(cur, ctypes.byref(pid))
                cls = window_class(int(cur))
                if pid.value == target_pid.value or cls == "#32768":
                    rect = wintypes.RECT()
                    if user32.GetWindowRect(cur, ctypes.byref(rect)) and (rect.right - rect.left) >= 16 and (rect.bottom - rect.top) >= 16:
                        out.append(int(cur))
        cur = user32.GetWindow(cur, 2)
    return out


_UIA_ACTION_PS = "param([string]$Roots, [int]$X, [int]$Y, [string]$Name, [string]$Action, [string]$Value)\n" + _UIA_HEAD + r"""
$rootIds = New-Object System.Collections.Generic.List[long]
foreach ($tok in ($Roots -split ',')) {
    [long]$rv = 0
    if ([long]::TryParse($tok.Trim(), [ref]$rv) -and $rv -and -not $rootIds.Contains($rv)) { $rootIds.Add($rv) }
}
$target = $null
if ($Name) {
    $cond = [System.Windows.Automation.PropertyCondition]::new($A::NameProperty, $Name)
    foreach ($rh in $rootIds) {
        if (-not $rh) { continue }
        try {
            $root = $A::FromHandle([IntPtr]$rh)
            foreach ($e in $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)) {
                $r = $e.Current.BoundingRectangle
                if ([math]::Abs($r.X + $r.Width / 2 - $X) -le 8 -and [math]::Abs($r.Y + $r.Height / 2 - $Y) -le 8) {
                    $target = $e
                    break
                }
            }
        } catch { }
        if ($target) { break }
    }
}
if (-not $target) {
    try { $target = $A::FromPoint([System.Windows.Point]::new($X, $Y)) } catch { }
}
if (-not $target) { 'ERR:control not found'; return }
$p = $null
switch ($Action) {
    'invoke' {
        if ($target.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); 'OK:invoked' }
        else { 'ERR:invoke not supported' }
    }
    'set_value' {
        if ($target.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$p)) { $p.SetValue($Value); 'OK:value set' }
        else { 'ERR:set_value not supported' }
    }
    'toggle' {
        if ($target.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$p)) {
            $p.Toggle()
            "OK:toggled to $($p.Current.ToggleState)"
        } else { 'ERR:toggle not supported' }
    }
    'select' {
        if ($target.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select(); 'OK:selected' }
        else { 'ERR:select not supported' }
    }
    'expand' {
        if ($target.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Expand(); 'OK:expanded' }
        else { 'ERR:expand not supported' }
    }
    'collapse' {
        if ($target.TryGetCurrentPattern([System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$p)) { $p.Collapse(); 'OK:collapsed' }
        else { 'ERR:collapse not supported' }
    }
    'range' {
        if ($target.TryGetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$p)) {
            $num = 0.0
            if ([double]::TryParse($Value, [ref]$num)) { $p.SetValue($num); "OK:range set to $num" }
            else { 'ERR:range value must be a number' }
        } else { 'ERR:range not supported' }
    }
    'focus' {
        $target.SetFocus()
        'OK:focused'
    }
    default { "ERR:unknown action $Action" }
}
"""


def uia_action(hwnd: int, x: int, y: int, name: str, action: str, value: str = "") -> tuple[bool, str]:
    """Execute a UIA pattern action on the control at (x, y) / `name`."""
    if action not in UIA_ACTIONS:
        return False, f"unknown action {action}"
    from src.tools import warm_ps

    roots = [*popup_windows(hwnd), hwnd] if hwnd else []
    out = (warm_ps.run("uia", _UIA_ACTION_PS,
                       {"Roots": ",".join(str(r) for r in roots), "X": x, "Y": y, "Name": name, "Action": action, "Value": value}, 10.0) or "").strip()
    if out.startswith("OK:"):
        return True, out[3:]
    if out.startswith("ERR:"):
        return False, out[4:]
    return False, "UIA action failed"


def ui_elements(hwnd: int, timeout_s: float = 25.0, query: str = "",
                control_types: list[str] | tuple[str, ...] | None = None) -> str:
    """Raw `x,y,w,h<TAB>Type<TAB>Name` lines for the controls of window `hwnd` (plus popups); '' on any failure."""
    import subprocess
    import tempfile
    from pathlib import Path

    if not hwnd:
        return ""
    from src.tools import warm_ps

    popups = popup_windows(hwnd)
    types_str = ",".join(t.strip() for t in (control_types or ()) if isinstance(t, str) and t.strip())
    q_str = query.strip() if isinstance(query, str) else ""
    warm = warm_ps.run("uia", _UIA_PS, {"Hwnd": hwnd, "Popups": ",".join(str(p) for p in popups),
                                       "Query": q_str, "ControlTypes": types_str}, timeout_s)
    if warm is not None:
        return warm
    with tempfile.TemporaryDirectory(prefix="paperduck_uia_") as tmp:
        script = Path(tmp) / "uia.ps1"
        script.write_text(_UIA_PS, encoding="utf-8")
        try:
            cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-File", str(script), "-Hwnd", str(hwnd), "-Popups", ",".join(str(p) for p in popups)]
            if q_str:
                cmd += ["-Query", q_str]
            if types_str:
                cmd += ["-ControlTypes", types_str]
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout_s,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return proc.stdout.decode("utf-8", errors="replace").strip() if proc.returncode == 0 else ""
