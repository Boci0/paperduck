"""Modular multi-tool plugin package for Paperduck."""

from src.tools.base import ToolPlugin, ToolRegistry
from src.tools.clip_vault import ClipEntry, ClipVaultPlugin
from src.tools.file_forge import (
    FileForgePlugin,
    ForgeMode,
    ForgeResult,
    ForgeWheelOption,
    get_wheel_options_for_paths,
)
from src.tools.launcher import LauncherCommand, LauncherPlugin
from src.tools.pomodoro import PomodoroPlugin
from src.tools.system_monitor import SystemMetrics, SystemMonitorPlugin, get_system_metrics

__all__ = [
    "ClipEntry",
    "ClipVaultPlugin",
    "FileForgePlugin",
    "ForgeMode",
    "ForgeResult",
    "ForgeWheelOption",
    "LauncherCommand",
    "LauncherPlugin",
    "PomodoroPlugin",
    "SystemMetrics",
    "SystemMonitorPlugin",
    "ToolPlugin",
    "ToolRegistry",
    "get_system_metrics",
    "get_wheel_options_for_paths",
]
