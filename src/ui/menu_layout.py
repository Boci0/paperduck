"""Customizable right-click menu layout (pinned top actions and collapsed sections)."""

from __future__ import annotations

from typing import Any

PINNABLE_ACTIONS: tuple[tuple[str, str], ...] = (
    ("Open AI Chat", "ai:chat"),
    ("Quick Command", "snip:quick"),
    ("Watch Now", "snip:watch"),
    ("Snip Region -> Image", "snip:image"),
    ("Snip Region -> OCR", "snip:text"),
    ("Pick Screen Color", "snip:color"),
    ("Start / Pause Pomodoro", "pomodoro:toggle"),
    ("Open Project in VS Code", "project:code"),
    ("Toggle Phone Bridge", "remote:toggle"),
)
_PINNABLE_MAP: dict[str, str] = {cid: label for label, cid in PINNABLE_ACTIONS}

HIDEABLE_SECTIONS: tuple[str, ...] = (
    "Screenshot",
    "Macros",
    "Recent Files",
    "Ports",
    "File Forge (Drop / Convert)",
    "Clip Vault & Quick Transforms",
    "Dev Transforms",
    "Project",
    "Launcher",
    "Hints",
    "Errands",
    "Phone Control",
    "Pomodoro",
)
_HIDEABLE_SET = frozenset(HIDEABLE_SECTIONS)


class MenuLayout:
    """User preferences for pinned root actions and hidden sections (persisted in state.json)."""

    def __init__(self) -> None:
        self.pinned: list[str] = []
        self.hidden: set[str] = set()

    def is_hidden(self, section: str) -> bool:
        return section in self.hidden

    def set_hidden(self, section: str, hide: bool) -> None:
        if section not in _HIDEABLE_SET:
            return
        if hide:
            self.hidden.add(section)
        else:
            self.hidden.discard(section)

    def is_pinned(self, cid: str) -> bool:
        return cid in self.pinned

    def set_pinned(self, cid: str, pinned: bool) -> None:
        if cid not in _PINNABLE_MAP:
            return
        if pinned:
            if cid not in self.pinned:
                self.pinned.append(cid)
        else:
            self.pinned = [c for c in self.pinned if c != cid]

    @staticmethod
    def label_for(cid: str) -> str:
        return _PINNABLE_MAP.get(cid, "")

    def to_dict(self) -> dict[str, Any]:
        return {"pinned": list(self.pinned), "hidden": sorted(self.hidden)}

    def load_dict(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        pinned = data.get("pinned")
        if isinstance(pinned, list):
            seen: set[str] = set()
            self.pinned = [
                c for c in pinned
                if isinstance(c, str) and c in _PINNABLE_MAP and not (c in seen or seen.add(c))
            ]
        hidden = data.get("hidden")
        if isinstance(hidden, list):
            self.hidden = {s for s in hidden if isinstance(s, str) and s in _HIDEABLE_SET}
