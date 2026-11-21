"""Context hints: suggest an existing tool from the foreground app and the clipboard.

The base rules need no network and no model: a few fixed rules over the foreground process name, how long it
has been in front, and newly copied clipboard text. Each hint maps to a callback id the pet already runs.
Watch mode (separate opt-in, off by default) adds AI lenses from `watch.py` behind a daily call budget.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem
from src.tools.clip_vault import decode_jwt
from src.tools.ports import process_name
from src.tools.watch import CLIPBOARD_LENSES, watch_gate

from src.tools.ai_assist import THINKING_LEVELS as WATCH_THINKING  # same levels as the chat; Watch keeps its own choice so it can stay fast

CODE_APPS = frozenset({
    "code.exe", "cursor.exe", "windsurf.exe", "devenv.exe", "pycharm64.exe", "idea64.exe",
    "webstorm64.exe", "sublime_text.exe", "windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe",
})
# Copies made while these are in front are never inspected, even after switching away
PRIVATE_APPS = frozenset({
    "1password.exe", "bitwarden.exe", "keepass.exe", "keepassxc.exe", "lastpass.exe", "dashlane.exe", "enpass.exe", "nordpass.exe", "roboform.exe", "keeper.exe",
})
FOCUS_DWELL_S = 600.0  # a code app in front this long while Pomodoro is idle
COOLDOWN_S = {"focus": 1800.0}
DEFAULT_COOLDOWN_S = 120.0
WATCH_COOLDOWN_S = 15.0  # Watch rules: copies are deliberate, and pages are offered once anyway
MAX_CLIP_CHARS = 8192
DAILY_AI_CALLS = 40  # Watch mode model calls per day, clicked and pre-fetched together
PREFETCH_DWELL_S = 8.0
MAX_IGNORED = 3  # ready answers left unclicked in one app before pre-fetch stops there this session

_JWT_RE = re.compile(r"^eyJ[\w-]+\.eyJ[\w-]+\.[\w-]*$")
_EPOCH_MIN, _EPOCH_MAX = 946_684_800, 4_102_444_800  # 2000..2100, so phone numbers and ids stay quiet


@dataclass(frozen=True)
class Suggestion:
    rule: str
    text: str
    action: str
    data: str = ""  # Watch mode: the clipboard text a lens reads


def suggest(app: str, clip: str, dwell_s: float, pomodoro_idle: bool) -> Suggestion | None:
    """First matching rule for the foreground exe, new clipboard text ('' if unchanged) and dwell time."""
    clip = clip.strip()
    if clip and len(clip) <= MAX_CLIP_CHARS:
        if _JWT_RE.match(clip):
            try:
                decode_jwt(clip)
                return Suggestion("jwt", "Decode this JWT?", "clipvault:dev:jwt")
            except (ValueError, RecursionError):  # deeply nested JSON raises RecursionError
                pass
        if clip.isascii() and clip.isdigit() and len(clip) in (10, 13):  # '²'.isdigit() but int() rejects it
            seconds = int(clip) // (1000 if len(clip) == 13 else 1)
            if _EPOCH_MIN <= seconds <= _EPOCH_MAX:
                return Suggestion("time", "Convert this timestamp?", "clipvault:dev:time")
        if clip[0] in "{[" and "\n" not in clip and len(clip) >= 40:
            try:
                parsed = json.loads(clip)
            except (ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)) and len(parsed) >= 2:
                return Suggestion("json", "Pretty-print this JSON?", "clipvault:json_toggle")
    if pomodoro_idle and app.lower() in CODE_APPS and dwell_s >= FOCUS_DWELL_S:
        return Suggestion("focus", "Start a focus session?", "pet:focus")
    return None


def foreground_app() -> str:
    """Exe name of the foreground window ('' for none, Paperduck itself, or off Windows)."""
    if sys.platform != "win32":
        return ""
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value in (0, os.getpid()):
        return ""
    return process_name(pid.value)


class _LastInputInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input anywhere (0.0 off Windows or on failure)."""
    if sys.platform != "win32":
        return 0.0
    info = _LastInputInfo(ctypes.sizeof(_LastInputInfo), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return 0.0
    # Both are 32-bit millisecond tick counts; the mask keeps the difference right across the 49.7-day wrap
    return ((ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF) / 1000.0


def display_kept_on() -> bool:
    """True while any app asks Windows to keep the display on (video players, presentations, calls)."""
    if sys.platform != "win32":
        return False
    state = ctypes.c_ulong(0)
    # 16 = SystemExecutionState: the system-wide OR of every process's SetThreadExecutionState request
    if ctypes.WinDLL("powrprof").CallNtPowerInformation(16, None, 0, ctypes.byref(state), ctypes.sizeof(state)):
        return False
    return bool(state.value & 0x2)  # ES_DISPLAY_REQUIRED


def foreground_title() -> str:
    """Title of the foreground window ('' off Windows)."""
    if sys.platform != "win32":
        return ""
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


class HintsPlugin:
    """Rule state: dwell tracking, clipboard change detection, per-rule cooldowns and session mutes."""

    SUBMENU_NAME: str = "Hints"

    def __init__(self) -> None:
        self._enabled = True
        self._app = ""
        self._app_since = 0.0
        self._last_clip = ""
        self._next_ok: dict[str, float] = {}
        self._muted: set[str] = set()
        self.shown = self.used = self.skipped = 0
        self.watch = False
        self.watch_thinking = "off"
        self.levels: Callable[[], tuple[str, ...]] = lambda: WATCH_THINKING  # the provider's levels, set by the pet window
        self.prefetch_apps: set[str] = set()
        self._title = ""
        self._title_since = 0.0
        self._offered: set[tuple[str, str]] = set()  # (lens, window title): each page is offered once
        self._ignored: dict[str, int] = {}
        self._day = date.today().isoformat()
        self.ai_calls = 0
        self.ai_tokens = 0
        self.last_tidy: tuple[list[tuple[str, str]], list[str]] | None = None  # (moves done, folders made) to undo
        self.history: deque[Suggestion] = deque(maxlen=5)

    @property
    def tool_id(self) -> str:
        return "hints"

    @property
    def display_name(self) -> str:
        return "Hints"

    @property
    def tick_interval_ms(self) -> int:
        return 2000

    @property
    def enabled(self) -> bool:
        return self._enabled

    def note_app(self, app: str, now: float | None = None) -> None:
        """Track the front app even on ticks that skip `poll` (a card up, asleep, a wheel open), so the menu's
        Pre-fetch item names the app the user is in. '' (Paperduck itself, e.g. its menu) keeps the last one;
        a private app clears it so it is never offered for pre-fetch."""
        if not app:
            return
        app = "" if app.lower() in PRIVATE_APPS else app
        if app != self._app:
            self._app, self._app_since = app, time.monotonic() if now is None else now

    def poll(self, app: str, clip: str, pomodoro_idle: bool, now: float | None = None,
             title: str = "", watch_ok: bool = True) -> Suggestion | None:
        """Feed one sample; returns a hint to show now or None. `watch_ok` False (no key, or the chat is busy)
        holds Watch offers back without using them up, so the page is offered again once it is True."""
        now = time.monotonic() if now is None else now
        new_clip = "" if clip == self._last_clip else clip
        self._last_clip = clip  # updated even when skipping, so a private copy is never offered later
        self.note_app(app, now)
        if not self._enabled or not app or app.lower() in PRIVATE_APPS:
            return None
        if title != self._title:
            self._title, self._title_since = title, now
        hint = (suggest(app, new_clip, now - self._app_since, pomodoro_idle)
                or (self._watch_hint(app, title, new_clip, now) if watch_ok else None))
        if hint is None or hint.rule in self._muted or now < self._next_ok.get(hint.rule, 0.0):
            return None
        self._next_ok[hint.rule] = now + COOLDOWN_S.get(
            hint.rule, WATCH_COOLDOWN_S if hint.rule.startswith("watch_") else DEFAULT_COOLDOWN_S)
        if hint.rule == "focus":
            self._app_since = now  # the next focus hint needs another full dwell
        self.shown += 1
        if not self.history or self.history[-1] != hint:
            self.history.append(hint)
        return hint

    def _watch_hint(self, app: str, title: str, clip: str, now: float) -> Suggestion | None:
        if not self.watch or len(clip) > MAX_CLIP_CHARS:
            return None
        gate = watch_gate(app, title, clip, now - self._title_since)
        if gate is not None and gate.lens == "summary" and app.lower() in CODE_APPS:
            return None  # long copies in an editor or terminal are code, not prose
        if gate is None or (not gate.data and (gate.lens, title) in self._offered):
            return None
        if not gate.data:
            self._offered.add((gate.lens, title))
        return Suggestion(f"watch_{gate.lens}", gate.text, f"watch:{gate.lens}", gate.data)

    def _roll_day(self) -> None:
        today = date.today().isoformat()
        if today != self._day:
            self._day, self.ai_calls, self.ai_tokens = today, 0, 0

    def budget_left(self) -> int:
        self._roll_day()
        return max(0, DAILY_AI_CALLS - self.ai_calls)

    def record_ai(self, tokens: int) -> None:
        self._roll_day()
        self.ai_calls += 1
        self.ai_tokens += max(0, tokens)

    def can_prefetch(self, hint: Suggestion, now: float | None = None) -> bool:
        """Pre-fetch only clipboard lenses, in an allowlisted app, after the dwell, with budget left."""
        now = time.monotonic() if now is None else now
        app = self._app.lower()
        return (hint.action.removeprefix("watch:") in CLIPBOARD_LENSES and app in self.prefetch_apps
                and now - self._app_since >= PREFETCH_DWELL_S
                and self._ignored.get(app, 0) < MAX_IGNORED and self.budget_left() > 0)

    def ignored_ready(self) -> None:
        """A pre-fetched answer expired unclicked."""
        app = self._app.lower()
        self._ignored[app] = self._ignored.get(app, 0) + 1

    def note_own_copy(self, text: str) -> None:
        """The pet put `text` on the clipboard (a reply draft): never offer a hint for it."""
        self._last_clip = text

    def mark_used(self) -> None:
        self.used += 1

    def dismiss(self, rule: str) -> None:
        """Right-click on a hint: never offer that rule again this session."""
        self._muted.add(rule)
        self.skipped += 1

    def tick(self) -> bool:
        return False

    def desired_state(self) -> CompanionState | None:
        return None

    def get_hud_data(self) -> HudData:
        top = max(1, self.shown)
        if self.watch:
            self._roll_day()
            detail = f"Watch: {self.ai_calls}/{DAILY_AI_CALLS} AI calls today, {self.ai_tokens:,} tokens"
        else:
            detail = "Offline: active app + new clipboard text" if self._enabled else "Turn on from the Hints menu"
        return HudData(
            badge_left="HINTS",
            badge_right="ON" if self._enabled else "OFF",
            badge_right_color="#38B764" if self._enabled else "#8A9AF2",
            bars=(
                HudBar("Shown", min(100.0, self.shown * 10.0), "#73EFF7", f"{self.shown:3d}"),
                HudBar("Used", self.used * 100.0 / top, "#38B764", f"{self.used:3d}"),
                HudBar("Skipped", self.skipped * 100.0 / top, "#EF7D57", f"{self.skipped:3d}"),
            ),
            detail=detail,
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        sub = self.SUBMENU_NAME
        items = [ToolMenuItem(label="Smart Suggestions", callback_id="hints:toggle", checkable=True,
                              checked=self._enabled, submenu=sub),
                 ToolMenuItem(label="Watch Mode (uses AI)", callback_id="hints:watch", checkable=True,
                              checked=self.watch, enabled=self._enabled, submenu=sub)]
        from src.ui.quick_tools import WATCH_SHORT_LABEL

        items.append(ToolMenuItem(label=f"Help With This Screen\t{WATCH_SHORT_LABEL}", callback_id="watch:now",
                                  submenu=sub))
        for level in self.levels():
            items.append(ToolMenuItem(label=f"Watch Thinking: {level.title()}", callback_id=f"hints:wthink:{level}",
                                      checkable=True, checked=self.watch_thinking == level, submenu=sub))
        if self.last_tidy is not None:
            items.append(ToolMenuItem(label=f"Undo Last Tidy ({len(self.last_tidy[0])} files)",
                                      callback_id="watch:undo_tidy", submenu=sub))
        if self.history:
            last = self.history[-1]
            items.append(ToolMenuItem(label=f"Recall Last Hint: {last.text[:28]}",
                                      callback_id="hints:last", submenu=sub))
        app = self._app.lower()
        if self.watch and app:
            items.append(ToolMenuItem(label=f"Pre-fetch Answers in {app}", callback_id="hints:prefetch",
                                      checkable=True, checked=app in self.prefetch_apps, submenu=sub))
        return items

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id.startswith("hints:wthink:") and callback_id[13:] in self.levels():
            self.watch_thinking = callback_id[13:]
            return (True, f"THINK {self.watch_thinking.upper()}", "#73EFF7")
        if callback_id == "hints:watch":
            self.watch = not self.watch
            return (True, "WATCH ON" if self.watch else "WATCH OFF", "#73EFF7")
        if callback_id == "hints:prefetch" and self._app:
            self.prefetch_apps ^= {self._app.lower()}
            return (True, "PREFETCH ON" if self._app.lower() in self.prefetch_apps else "PREFETCH OFF", "#73EFF7")
        if callback_id != "hints:toggle":
            return (False, "", "")
        self._enabled = not self._enabled
        return (True, "HINTS ON" if self._enabled else "HINTS OFF", "#73EFF7")

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self._enabled, "watch": self.watch, "watch_thinking": self.watch_thinking,
                "prefetch_apps": sorted(self.prefetch_apps),
                "ai_day": self._day, "ai_calls": self.ai_calls, "ai_tokens": self.ai_tokens}

    def load_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        if data.get("enabled") is False:
            self._enabled = False
        self.watch = data.get("watch") is True
        if data.get("watch_thinking") in WATCH_THINKING:
            self.watch_thinking = data["watch_thinking"]
        apps = data.get("prefetch_apps")
        if isinstance(apps, list):
            self.prefetch_apps = {a.lower() for a in apps if isinstance(a, str) and a.lower().endswith(".exe")}
        if data.get("ai_day") == self._day:  # yesterday's count starts over
            for name in ("ai_calls", "ai_tokens"):
                if isinstance(data.get(name), int) and data[name] >= 0:
                    setattr(self, name, data[name])
