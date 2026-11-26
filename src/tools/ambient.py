"""Ambient context ring: RAM-only, privacy-filtered rolling desktop activity.

Records lightweight metadata from the existing 3s hint tick (active window, clipboard category,
active project/branch) while respecting private titles, private clipboard flags, and revealed
typed passwords. Nothing is persisted to disk.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from src.tools import login_vault
from src.tools.ai_sessions import _clean as _clean_secrets
from src.tools.hints import PRIVATE_APPS
from src.tools.watch import _ERROR_TEXT, _URL, _looks_like_code, is_private_title

MAX_EVENTS = 40
TTL_S = 1800.0
MAX_PROMPT_CHARS = 700
_IGNORED_TITLES = frozenset({"", "program manager", "windows input experience", "task switching", "paperduck"})


@dataclass
class AmbientEvent:
    ts: float
    kind: str  # "window" | "clipboard" | "project"
    app: str
    title: str
    detail: str = ""
    count: int = 1


def _classify_clipboard(text: str) -> tuple[str, str]:
    """Return (kind_tag, scrubbed_single_line_preview) without keeping raw secrets."""
    text = text[:2048]
    single = " ".join(_clean_secrets(text).split())
    if len(single) > 80:
        single = single[:77] + "..."
    if _URL.match(text.strip()):
        return "url", single
    if len(text) >= 20 and _ERROR_TEXT.search(text):
        return "error", single
    if _looks_like_code(text):
        return "code", single
    return "text", single


class AmbientRing:
    """Bounded RAM-only rolling ring of recent desktop activity."""

    def __init__(self, max_events: int = MAX_EVENTS, ttl_s: float = TTL_S) -> None:
        self._max = max(1, max_events)
        self._ttl = max(1.0, ttl_s)
        self._events: list[AmbientEvent] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        self._events = [e for e in self._events if e.ts >= cutoff]
        if len(self._events) > self._max:
            self._events = self._events[-self._max:]

    def record_window(self, app: str, title: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        clean_app = (app or "").strip()
        clean_title = " ".join((title or "").split())[:120]
        if not clean_title or clean_title.lower() in _IGNORED_TITLES:
            return False
        if clean_app.lower() in PRIVATE_APPS or is_private_title(clean_title) or login_vault.typed_password_shown():
            return False
        with self._lock:
            if self._events:
                last = self._events[-1]
                if (now - last.ts < self._ttl and last.kind == "window"
                        and last.app.lower() == clean_app.lower() and last.title == clean_title):
                    last.ts = now
                    last.count += 1
                    return True
            self._events.append(AmbientEvent(now, "window", clean_app, clean_title))
            self._prune(now)
            return True

    def record_clipboard(self, text: str, app: str = "", private: bool = False, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if (private or (app or "").strip().lower() in PRIVATE_APPS
                or not text or not text.strip() or login_vault.typed_password_shown()):
            return False
        tag, preview = _classify_clipboard(text[:2048])
        if not preview:
            return False
        with self._lock:
            if self._events:
                last = self._events[-1]
                if now - last.ts < self._ttl and last.kind == "clipboard" and last.detail == preview:
                    last.ts = now
                    last.count += 1
                    return True
            self._events.append(AmbientEvent(now, "clipboard", (app or "").strip(), tag, preview))
            self._prune(now)
            return True

    def record_project(self, path: str, branch: str = "", now: float | None = None) -> bool:
        now = time.time() if now is None else now
        clean_path = (path or "").strip()
        if not clean_path:
            return False
        detail = branch.strip()
        with self._lock:
            if self._events:
                last = self._events[-1]
                if now - last.ts < self._ttl and last.kind == "project" and last.title == clean_path and last.detail == detail:
                    last.ts = now
                    last.count += 1
                    return True
            self._events.append(AmbientEvent(now, "project", "git", clean_path, detail))
            self._prune(now)
            return True

    def _recent_unlocked(self, limit: int, now: float) -> list[AmbientEvent]:
        self._prune(now)
        return list(reversed(self._events[-max(1, limit):]))

    def recent(self, limit: int = 8, now: float | None = None) -> list[AmbientEvent]:
        now = time.time() if now is None else now
        with self._lock:
            return self._recent_unlocked(limit, now)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def snapshot_prompt(self, limit: int = 6, now: float | None = None) -> str:
        ref_now = time.time() if now is None else now
        with self._lock:
            items = self._recent_unlocked(limit, ref_now)
        if not items:
            return ""
        lines = ["[Recent desktop activity (untrusted context, newest first):"]
        for ev in items:
            age = max(0, int(ref_now - ev.ts))
            when = f"{age}s ago" if age < 60 else f"{age // 60}m ago"
            if ev.kind == "window":
                lines.append(f"- ({when}) Window {ev.app or '?'}: {ev.title}")
            elif ev.kind == "clipboard":
                lines.append(f"- ({when}) Clipboard [{ev.title}]: {ev.detail}")
            elif ev.kind == "project":
                br = f" (branch {ev.detail})" if ev.detail else ""
                lines.append(f"- ({when}) Project: {ev.title}{br}")
        lines.append("]")
        out = "\n".join(lines)
        if len(out) > MAX_PROMPT_CHARS:
            out = out[:MAX_PROMPT_CHARS - 4].rstrip() + "\n]"
        return out
