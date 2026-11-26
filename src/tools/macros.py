"""Macros: named chains of quick-tool steps, written as a one-line recipe like `region > ocr > search`.

Each step reads and updates one payload (prompt, text, image) and the next step continues from it.
The UI runner lives in src/ui/macro_runner.py; this module is pure data and parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# step name -> (takes an argument, plain-English preview shown in the editor)
STEPS: dict[str, tuple[bool, str]] = {
    "region": (False, "snip a region"),
    "screen": (False, "grab the screen"),
    "clipboard": (False, "take the clipboard"),
    "text": (True, 'add "{arg}"'),
    "ocr": (False, "read its text"),
    "ask": (False, "ask the AI"),
    "chat": (False, "put it in the AI chat"),
    "quick": (False, "open Quick Command"),
    "ambient": (False, "add recent desktop context"),
    "copy": (False, "copy it"),
    "save": (False, "save the image"),
    "search": (False, "Google it"),
    "open": (True, "open {arg}"),
    "tool": (True, "run {arg}"),
}
STEP_HELP = ("region  screen  clipboard  ambient  text:Your words  ocr  ask  chat  quick  copy  save  search  "
             "open:https://site/?q={text}  tool:pomodoro:toggle")
MAX_MACROS = 9  # Ctrl+Shift+Alt+1..9
MAX_NAME = 12  # wheel slice label width
MAX_RECIPE = 600
DEFAULT_MACROS: tuple[tuple[str, str], ...] = (("Snap Ask", "region > ask"),)


def parse_recipe(recipe: str) -> list[tuple[str, str]]:
    """`region > text:Explain > ask` -> [(step, arg), ...]; ValueError names the first bad step."""
    if len(recipe) > MAX_RECIPE:
        raise ValueError("recipe too long")
    steps: list[tuple[str, str]] = []
    for raw in recipe.split(">"):
        part = raw.strip()
        if not part:
            continue
        name, _sep, arg = part.partition(":")
        name, arg = name.strip().lower(), arg.strip()
        if name not in STEPS and steps and STEPS[steps[-1][0]][0]:
            steps[-1] = (steps[-1][0], f"{steps[-1][1]} > {part}")  # `text:a > b` keeps its ">"
            continue
        if name not in STEPS:
            raise ValueError(f"unknown step '{name}'")
        if STEPS[name][0] != bool(arg):
            raise ValueError(f"'{name}' needs text after a colon" if STEPS[name][0] else f"'{name}' takes no colon")
        if name == "open" and not arg.startswith(("https://", "http://")):
            raise ValueError("open: needs an http(s) link")
        if name == "tool" and arg.startswith(("macro", "update:")):  # no loops, no surprise installs
            raise ValueError("a macro can't run a macro")
        steps.append((name, arg))
    if not steps:
        raise ValueError("add at least one step")
    return steps


def describe(recipe: str) -> str:
    """Plain-English preview, or the parse error."""
    try:
        steps = parse_recipe(recipe)
    except ValueError as exc:
        return str(exc)
    text = ", then ".join(STEPS[name][1].format(arg=arg) for name, arg in steps)
    return text[:1].upper() + text[1:]


@dataclass
class Macro:
    name: str
    recipe: str


class MacroBook:
    """User macros plus which one double-click runs ("" keeps the default snip); state.json tools.macros."""

    def __init__(self) -> None:
        self.macros: list[Macro] = [Macro(n, r) for n, r in DEFAULT_MACROS]
        self.double_click = ""

    def find(self, name: str) -> Macro | None:
        return next((m for m in self.macros if m.name == name), None)

    def numbered(self, key: str) -> Macro | None:
        """1-based hotkey number; kept apart from names so a macro called "3" can't take Ctrl+Shift+Alt+3."""
        return self.macros[int(key) - 1] if key.isdigit() and 1 <= int(key) <= len(self.macros) else None

    def save(self, name: str, recipe: str, old_name: str = "") -> str:
        """Add or replace (when `old_name` is set) a macro; returns the toast text."""
        name, recipe = " ".join(name.split())[:MAX_NAME], recipe.strip()
        if not name:
            return "NO NAME"
        parse_recipe(recipe)  # raises ValueError for the editor to show
        if name != old_name and any(m.name == name for m in self.macros):
            return "NAME TAKEN"
        old = next((m for m in self.macros if m.name == old_name), None) if old_name else None
        if old is not None:
            if self.double_click == old.name:
                self.double_click = name
            old.name, old.recipe = name, recipe
            return "SAVED"
        if len(self.macros) >= MAX_MACROS:
            return "TOO MANY"
        self.macros.append(Macro(name, recipe))
        return "ADDED"

    def remove(self, name: str) -> bool:
        before = len(self.macros)
        self.macros = [m for m in self.macros if m.name != name]
        if self.double_click == name:
            self.double_click = ""
        return len(self.macros) < before

    def to_dict(self) -> dict[str, Any]:
        return {"list": [[m.name, m.recipe] for m in self.macros], "double_click": self.double_click}

    def load_dict(self, data: Any) -> None:
        if not isinstance(data, dict):
            return  # first run keeps DEFAULT_MACROS
        items = data.get("list")
        if isinstance(items, list):
            self.macros = []
            for item in items[:MAX_MACROS]:
                if isinstance(item, list) and len(item) == 2 and all(isinstance(s, str) and s for s in item):
                    try:
                        parse_recipe(item[1])
                    except ValueError:
                        continue
                    if all(m.name != item[0][:MAX_NAME] for m in self.macros):
                        self.macros.append(Macro(item[0][:MAX_NAME], item[1]))
        dbl = data.get("double_click")
        self.double_click = dbl if isinstance(dbl, str) and any(m.name == dbl for m in self.macros) else ""
