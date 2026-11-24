"""User-configurable launcher command plugin for Paperduck."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


@dataclass(frozen=True)
class LauncherCommand:
    """One user-configured launcher command."""

    label: str
    command: str
    cwd: str = ""


class LauncherPlugin:
    """Manages and runs user-configured desktop/CLI launcher commands."""

    SUBMENU_NAME: str = "Launcher"
    MAX_COMMANDS: int = 12
    MAX_LABEL_CHARS: int = 32
    MAX_COMMAND_CHARS: int = 1000

    def __init__(self, project_dir: Callable[[], Path | None] | None = None) -> None:
        self._project_dir = project_dir or (lambda: None)
        self._commands: list[LauncherCommand] = []
        self._runs: int = 0
        self._last_label: str = ""
        self._last_error: str = ""

    @property
    def tool_id(self) -> str:
        return "launcher"

    @property
    def display_name(self) -> str:
        return "Launcher"

    @property
    def tick_interval_ms(self) -> int:
        return 10_000

    @property
    def commands(self) -> list[LauncherCommand]:
        return list(self._commands)

    def add_command(self, label: str, command: str, cwd: str = "") -> str:
        clean_label = " ".join(str(label).split())[: self.MAX_LABEL_CHARS]
        clean_cmd = str(command).strip()[: self.MAX_COMMAND_CHARS]
        clean_cwd = str(cwd).strip()
        if not clean_label or not clean_cmd:
            return "EMPTY"
        if len(self._commands) >= self.MAX_COMMANDS:
            return "FULL"
        if clean_cwd and not Path(clean_cwd).expanduser().is_dir():
            return "BAD DIR"
        if any(c.label.casefold() == clean_label.casefold() for c in self._commands):
            return "EXISTS"
        self._commands.append(LauncherCommand(clean_label, clean_cmd, clean_cwd))
        return "ADDED"

    def remove_command(self, index: int) -> bool:
        if not 0 <= index < len(self._commands):
            return False
        self._commands.pop(index)
        return True

    def _resolve_cwd(self, entry: LauncherCommand) -> Path | None:
        if entry.cwd:
            p = Path(entry.cwd).expanduser()
            return p if p.is_dir() else None
        proj = self._project_dir()
        if proj is not None and proj.is_dir():
            return proj
        return Path.home()

    def run_command(self, index: int) -> tuple[bool, str, str]:
        if not 0 <= index < len(self._commands):
            return (True, "GONE", "#EF7D57")
        entry = self._commands[index]
        cwd = self._resolve_cwd(entry)
        if cwd is None:
            self._last_error = "Bad working folder"
            return (True, "BAD DIR", "#EF7D57")
        try:
            flags = (_NO_WINDOW | _NEW_GROUP) if sys.platform == "win32" else 0
            subprocess.Popen(
                entry.command,
                shell=True,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            self._runs += 1
            self._last_label = entry.label
            self._last_error = ""
            return (True, entry.label[:10].upper(), "#38B764")
        except OSError as exc:
            self._last_error = exc.strerror or "Launch failed"
            return (True, "RUN ERR", "#EF7D57")

    def tick(self) -> bool:
        return False

    def desired_state(self) -> CompanionState | None:
        return None

    def get_hud_data(self) -> HudData:
        count = len(self._commands)
        if self._last_error:
            detail = self._last_error
        elif self._last_label:
            detail = f"Last: {self._last_label}"
        elif count:
            detail = f"{count} command(s) ready"
        else:
            detail = "Launcher > Add Command..."
        return HudData(
            badge_left="LAUNCH",
            badge_right="READY" if count else "EMPTY",
            badge_right_color="#38B764" if count else "#8A9AF2",
            bars=(
                HudBar(
                    label="Cmds",
                    value=min(100.0, count * (100.0 / self.MAX_COMMANDS)),
                    color_hex="#73EFF7",
                    display_text=f"{count:2d}/{self.MAX_COMMANDS}",
                ),
                HudBar(
                    label="Runs",
                    value=min(100.0, self._runs * 10.0),
                    color_hex="#41A6F6",
                    display_text=f"{min(999, self._runs):3d}",
                ),
            ),
            detail=detail,
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        sub = self.SUBMENU_NAME
        items = [
            ToolMenuItem(
                label=cmd.label.replace("&", "&&"),
                callback_id=f"launcher:run:{idx}",
                submenu=sub,
            )
            for idx, cmd in enumerate(self._commands)
        ]
        if self._commands:
            items.append(ToolMenuItem(label="Remove Command...", callback_id="launcher:remove", submenu=sub))
        items.append(ToolMenuItem(label="Add Command...", callback_id="launcher:add", submenu=sub))
        return items

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id.startswith("launcher:run:"):
            idx_txt = callback_id.removeprefix("launcher:run:")
            if idx_txt.isdigit():
                return self.run_command(int(idx_txt))
            return (True, "GONE", "#EF7D57")
        if callback_id.startswith("launcher:remove:"):
            idx_txt = callback_id.removeprefix("launcher:remove:")
            if idx_txt.isdigit() and self.remove_command(int(idx_txt)):
                return (True, "REMOVED", "#8A9AF2")
            return (True, "GONE", "#EF7D57")
        return (False, "", "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "commands": [{"label": c.label, "command": c.command, "cwd": c.cwd} for c in self._commands],
            "runs": self._runs,
            "last_label": self._last_label,
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        self._commands = []
        raw_cmds = data.get("commands")
        if isinstance(raw_cmds, list):
            for entry in raw_cmds[: self.MAX_COMMANDS]:
                if not isinstance(entry, dict):
                    continue
                label = entry.get("label")
                cmd = entry.get("command")
                cwd = entry.get("cwd", "")
                if isinstance(label, str) and isinstance(cmd, str) and isinstance(cwd, str):
                    clean_label = " ".join(label.split())[: self.MAX_LABEL_CHARS]
                    clean_cmd = cmd.strip()[: self.MAX_COMMAND_CHARS]
                    if clean_label and clean_cmd and not any(
                        c.label.casefold() == clean_label.casefold() for c in self._commands
                    ):
                        self._commands.append(LauncherCommand(clean_label, clean_cmd, cwd.strip()))
        runs = data.get("runs")
        if isinstance(runs, int) and not isinstance(runs, bool) and runs >= 0:
            self._runs = runs
        last_label = data.get("last_label")
        if isinstance(last_label, str):
            self._last_label = last_label[: self.MAX_LABEL_CHARS]
