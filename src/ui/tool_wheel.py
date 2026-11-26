"""Long-press radial tool wheel: hold the pet, drag onto a slice, release to run it."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import ctypes
import sys
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPoint
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QWidget

from src.ui.drop_wheel import RadialDropWheel
from src.ui.theme import TEXT_DIM_HEX, TEXT_HEX


# (label, url, accent): edit this tuple to change the web shortcut slices
WEB_SHORTCUTS: tuple[tuple[str, str, str], ...] = (
    ("YouTube", "https://www.youtube.com", "#EF7D57"),
    ("Gemini", "https://gemini.google.com", "#41A6F6"),
    ("GitHub", "https://github.com", "#C9D1D9"),
)


def google_search_url(query: str) -> str:
    return "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)


@dataclass(frozen=True)
class ToolWheelOption:
    """One tool slice; `callback_id` goes through PetWindow's normal tool dispatch."""

    label: str
    subtitle: str
    color_hex: str
    callback_id: str

    @property
    def preview_suffix(self) -> str:
        return self.subtitle


MAX_SLICES = 10  # more than this and the slice labels overlap
MAX_CUSTOM = 8
# Off until switched on in Settings > Customize Wheel; ids go through PetWindow's normal tool dispatch
EXTRA_SLICES: tuple[ToolWheelOption, ...] = (
    ToolWheelOption("Snip", "region", "#73EFF7", "snip:image"),
    ToolWheelOption("Color", "pick", "#FFCD75", "snip:color"),
    ToolWheelOption("Quick", "prompt", "#F2BE3A", "snip:quick"),
    ToolWheelOption("Watch", "window", "#73EFF7", "snip:watch"),
    ToolWheelOption("Phone", "bridge", "#38B764", "remote:toggle"),
    ToolWheelOption("JSON", "format", "#38B764", "clipvault:json_toggle"),
    ToolWheelOption("UUID", "new", "#8A9AF2", "clipvault:dev:uuid"),
    ToolWheelOption("VS Code", "project", "#41A6F6", "project:code"),
    ToolWheelOption("Terminal", "project", "#C9D1D9", "project:terminal"),
)


def custom_callback(target: str) -> str:
    return ("web:" if target.startswith(("http://", "https://")) else "open:") + target


class WheelLayout:
    """Which slices the wheel shows (tools.wheel in state.json, outside app/src so updates keep it).

    Only the user's changes are stored (`overrides` by callback id), so slices added by an update
    keep their default; `custom` holds the user's own (label, URL or path) slices.
    """

    def __init__(self) -> None:
        self.overrides: dict[str, bool] = {}
        self.custom: list[tuple[str, str]] = []
        self.macros: list[str] = []  # macro names, kept in sync by PetWindow; slices go last so they only fill free room

    def catalog(self, base: list[ToolWheelOption]) -> list[tuple[ToolWheelOption, bool]]:
        """Every choosable slice with whether it is on: built-ins, opt-in extras, custom, then macros."""
        custom = [ToolWheelOption(label, "open", "#C9D1D9", custom_callback(t)) for label, t in self.custom]
        custom += [ToolWheelOption(name, "macro", "#F2A6E0", f"macro:{name}") for name in self.macros]
        pairs = [(o, True) for o in base] + [(o, False) for o in EXTRA_SLICES] + [(o, True) for o in custom]
        return [(o, self.overrides.get(o.callback_id, default)) for o, default in pairs]

    def visible(self, base: list[ToolWheelOption]) -> list[ToolWheelOption]:
        shown = [o for o, on in self.catalog(base) if on][:MAX_SLICES]
        return shown or base[:1]

    def toggle(self, callback_id: str, base: list[ToolWheelOption]) -> str:
        """Flip one slice; returns the toast text."""
        catalog = self.catalog(base)
        on = next((state for o, state in catalog if o.callback_id == callback_id), None)
        if on is None:
            return "GONE"
        visible = sum(state for _o, state in catalog)
        if on and visible <= 1:
            return "KEEP ONE"
        if not on and visible >= MAX_SLICES:
            return "WHEEL FULL"
        self.overrides[callback_id] = not on
        return "HIDDEN" if on else "SHOWN"

    def add_custom(self, label: str, target: str) -> str:
        """Validate and add a user slice; returns the toast text."""
        label, target = " ".join(label.split())[:12], target.strip()
        if not label or not target:
            return "CANCEL"
        if len(self.custom) >= MAX_CUSTOM:
            return "TOO MANY"
        if not target.startswith(("http://", "https://")) and not Path(target).exists():
            return "BAD LINK"
        if any(t == target for _l, t in self.custom):
            return "EXISTS"
        self.custom.append((label, target))
        return "ADDED"

    def remove_custom(self, index: int) -> bool:
        if not 0 <= index < len(self.custom):
            return False
        _label, target = self.custom.pop(index)
        self.overrides.pop(custom_callback(target), None)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"slices": dict(self.overrides), "custom": [list(pair) for pair in self.custom]}

    def load_dict(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        slices = data.get("slices")
        if isinstance(slices, dict):
            self.overrides = {k: v for k, v in slices.items() if isinstance(k, str) and isinstance(v, bool)}
        if isinstance(data.get("hidden"), list):  # first format: only a hidden list
            self.overrides.update({h: False for h in data["hidden"] if isinstance(h, str)})
        custom = data.get("custom")
        if isinstance(custom, list):
            self.custom = [(c[0][:12], c[1]) for c in custom
                           if isinstance(c, list) and len(c) == 2 and all(isinstance(s, str) and s for s in c)
                           ][:MAX_CUSTOM]


class ToolWheel(RadialDropWheel):
    """Tool launcher wheel sharing the drop wheel's look and open animation.

    Hold-drag-release picks a slice; releasing over the hub or off the ring closes it.
    """

    HUB_LABEL: str = "Close"

    def __init__(
        self,
        *,
        on_selected: Callable[[ToolWheelOption], None],
        always_on_top: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            on_option_dropped=lambda _paths, _opt: None,
            always_on_top=always_on_top,
            parent=parent,
        )
        self._on_selected = on_selected
        self._button_up_ticks = 0
        self.setAcceptDrops(False)
        self.setWindowTitle("Paperduck Tool Wheel")

    def open_tools(
        self,
        options: Sequence[ToolWheelOption],
        *,
        window_top_left: QPoint,
        initial_global_cursor: QPoint | None = None,
    ) -> None:
        self._button_up_ticks = 0
        self.open_for_paths(
            [], options, window_top_left=window_top_left, initial_global_cursor=initial_global_cursor
        )

    def _on_watchdog_tick(self) -> None:
        """Close if the hold ended without a release reaching the pet (e.g. Win+Shift+S grabbed it)."""
        if not self.isVisible():
            self._watchdog_timer.stop()
            return
        if sys.platform != "win32":
            return
        self.restack_topmost(only_if_changed=True)
        l_down = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        self._button_up_ticks = 0 if l_down else self._button_up_ticks + 1
        # Two ticks (160ms): a normal release reaches the pet first and picks the slice
        if self._button_up_ticks >= 2:
            self.dismiss()

    def release_at(self, global_pos: QPoint) -> ToolWheelOption | None:
        """End a hold-drag: run the slice under the cursor, or just close."""
        chosen = self.update_hover_from_global(global_pos)
        if chosen is None:
            self.dismiss()
            return None
        self._fire(chosen)  # type: ignore[arg-type]
        return chosen  # type: ignore[return-value]

    def _fire(self, option: ToolWheelOption) -> None:
        self.dismiss()
        self._on_selected(option)

    def _header_preview_text(self) -> tuple[str, QColor]:
        chosen = self.selected_option()
        if chosen is not None:
            return (f"{chosen.label}  ->  {chosen.preview_suffix}", QColor(TEXT_HEX))
        return ("Drag onto a tool, release", QColor(TEXT_DIM_HEX))
