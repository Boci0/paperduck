"""Atomic JSON state persistence and corruption recovery for Paperduck."""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.engine.models import PetState, PetStats

logger = logging.getLogger(__name__)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "Paperduck"
_FROZEN = bool(getattr(sys, "frozen", False))
_PROJECT_ROOT = Path(sys.executable).parent if _FROZEN else Path(__file__).resolve().parents[2]


def log_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Paperduck" / "paperduck.log"


def log_text(max_chars: int = 100_000) -> str:
    """The newest `max_chars` of the log (the rotated `.1` file first when the current one is short); "" if none."""
    path = log_path()
    parts = []
    for p in (path.with_name(path.name + ".1"), path):
        try:
            parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "".join(parts)[-max_chars:]


def autostart_command() -> str:
    """Console-free launch command; a Run key has no working dir, so run a launcher file that finds the app root.

    Run values are capped at 260 characters, so no inline -c code here. The launcher sits in src/ because in-place
    updates replace src/ only: a root-level file never reached updated copies, so the Run value pointed at nothing.
    """
    if _FROZEN:
        return f'"{sys.executable}"'  # Paperduck.exe moves to its install root itself
    exe = Path(sys.executable)
    gui_exe = exe.with_name("pythonw.exe")
    launcher = gui_exe if gui_exe.exists() else exe
    return f'"{launcher}" "{_PROJECT_ROOT / "src" / "autostart.pyw"}"'


def autostart_enabled() -> bool:
    """The HKCU Run value is the source of truth for Start with Windows (not state.json)."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            winreg.QueryValueEx(key, _RUN_VALUE)
        return True
    except OSError:
        return False


def stale_autostart(value: str, root: Path, command: str) -> bool:
    """A Run value for this copy (it names `root`) that differs from today's command: the old inline -c form, or
    the root paperduck.pyw that updated copies never received. Another copy's value is never this copy's to fix."""
    root_low = str(root).lower().rstrip("\\/")
    val_low = value.lower()
    idx = val_low.find(root_low)
    if idx < 0:
        return False
    after = val_low[idx + len(root_low):idx + len(root_low) + 1]
    return after in ("", "\\", "/", '"', "'", " ") and value != command


def repair_autostart() -> bool:
    """Rewrite this copy's Run value when stale; True if it was rewritten."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, _RUN_VALUE)
    except OSError:
        return False
    if not isinstance(value, str) or not stale_autostart(value, _PROJECT_ROOT, autostart_command()):
        return False
    return set_autostart(True)


def set_autostart(enabled: bool) -> bool:
    """Add or remove the HKCU Run value; returns False if the registry refused."""
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, _RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        logger.warning("Could not update Start with Windows registry value", exc_info=True)
        return False


@dataclass
class PetSaveData:
    """Deserialized snapshot of utility state, plugin data, and UI preferences."""

    stats: PetStats = field(default_factory=PetStats)
    state: PetState = PetState.IDLE
    position: tuple[int, int] | None = None
    always_on_top: bool = True
    auto_wander: bool = True
    active_tool: str = "sysmon"
    tools_data: dict[str, dict[str, Any]] = field(default_factory=dict)


def _parse_position(window_dict: Any) -> tuple[int, int] | None:
    if not isinstance(window_dict, dict):
        return None
    raw_x = window_dict.get("x")
    raw_y = window_dict.get("y")
    if isinstance(raw_x, bool) or isinstance(raw_y, bool):
        return None
    if raw_x is None or raw_y is None:
        return None
    try:
        fx = float(raw_x)
        fy = float(raw_y)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(fx) and math.isfinite(fy)):
        return None
    return (int(fx), int(fy))


class StorageManager:
    """Persists and restores Paperduck state at ~/.paperduck/state.json."""

    SCHEMA_VERSION: int = 2

    def __init__(self, state_path: Path | str | None = None) -> None:
        if state_path is None:
            self._state_path = Path.home() / ".paperduck" / "state.json"
        else:
            self._state_path = Path(state_path)

    @property
    def state_path(self) -> Path:
        return self._state_path

    def load(self) -> PetSaveData:
        """Load saved state, recovering gracefully to defaults on missing or corrupt files."""
        if not self._state_path.exists():
            return PetSaveData()

        try:
            raw_text = self._state_path.read_text(encoding="utf-8")
            payload = json.loads(raw_text)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning(
                "Corrupt state file at %s (%s); falling back to defaults.",
                self._state_path,
                exc,
            )
            self._backup_corrupt_file()
            return PetSaveData()

        if not isinstance(payload, dict):
            logger.warning(
                "Invalid root JSON type in %s; falling back to defaults.",
                self._state_path,
            )
            self._backup_corrupt_file()  # the next save would overwrite it
            return PetSaveData()

        stats_raw = payload.get("stats")
        stats = PetStats.from_dict(stats_raw) if isinstance(stats_raw, dict) else PetStats()

        raw_state = payload.get("state")
        try:
            state = PetState(raw_state)
        except (ValueError, TypeError):
            state = PetState.IDLE

        window_raw = payload.get("window")
        position = _parse_position(window_raw)
        always_on_top = True
        auto_wander = True
        if isinstance(window_raw, dict):
            if isinstance(window_raw.get("always_on_top"), bool):
                always_on_top = window_raw["always_on_top"]
            if isinstance(window_raw.get("auto_wander"), bool):
                auto_wander = window_raw["auto_wander"]

        raw_active_tool = payload.get("active_tool")
        active_tool = (
            raw_active_tool
            if isinstance(raw_active_tool, str) and raw_active_tool.strip()
            else "sysmon"
        )

        raw_tools = payload.get("tools")
        tools_data: dict[str, dict[str, Any]] = {}
        if isinstance(raw_tools, dict):
            for k, v in raw_tools.items():
                if isinstance(k, str) and isinstance(v, dict):
                    tools_data[k] = v

        return PetSaveData(
            stats=stats,
            state=state,
            position=position,
            always_on_top=always_on_top,
            auto_wander=auto_wander,
            active_tool=active_tool,
            tools_data=tools_data,
        )

    def save(
        self,
        stats: PetStats,
        state: PetState,
        *,
        position: tuple[int, int] | None = None,
        always_on_top: bool = True,
        auto_wander: bool = True,
        active_tool: str = "sysmon",
        tools_data: dict[str, dict[str, Any]] | None = None,
    ) -> bool:
        """Atomically serialize state to disk using temp-file + os.replace."""
        window_payload: dict[str, Any] = {
            "always_on_top": bool(always_on_top),
            "auto_wander": bool(auto_wander),
        }
        if position is not None:
            window_payload["x"] = int(position[0])
            window_payload["y"] = int(position[1])

        document: dict[str, Any] = {
            "version": self.SCHEMA_VERSION,
            "state": state.value if isinstance(state, PetState) else str(state),
            "stats": stats.to_dict(),
            "window": window_payload,
            "active_tool": str(active_tool) if active_tool else "sysmon",
            "tools": tools_data if isinstance(tools_data, dict) else {},
        }

        temp_path: Path | None = None
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            serialized = json.dumps(document, indent=2, sort_keys=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self._state_path.parent),
                prefix=".state_",
                suffix=".tmp",
                delete=False,
            ) as tmp_file:
                temp_path = Path(tmp_file.name)
                tmp_file.write(serialized)
                tmp_file.flush()
                os.fsync(tmp_file.fileno())

            os.replace(temp_path, self._state_path)
            temp_path = None
            return True
        except OSError as exc:
            logger.error("Failed to save state to %s: %s", self._state_path, exc)
            return False
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass

    def _backup_corrupt_file(self) -> None:
        backup_path = self._state_path.with_name(f"{self._state_path.name}.corrupt")
        try:
            shutil.copy2(self._state_path, backup_path)
        except OSError:
            pass
