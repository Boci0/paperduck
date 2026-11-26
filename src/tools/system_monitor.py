"""Zero-dependency Windows ctypes and cross-platform System Monitor plugin."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import os
import shutil
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem
from src.tools.ports import PROTECTED, can_kill, kill_pid, list_listening_ports


@dataclass(frozen=True)
class SystemMetrics:
    """Snapshot of CPU, RAM, and Disk utilization percentages [0.0..100.0]."""

    cpu_percent: float
    ram_percent: float
    disk_percent: float


class _FILETIME(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _filetime_to_u64(ft: _FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


class _WinCpuSampler:
    """Stateful kernel32.GetSystemTimes delta sampler with zero subprocess overhead."""

    def __init__(self) -> None:
        self._prev_idle: int | None = None
        self._prev_kernel: int | None = None
        self._prev_user: int | None = None
        self._last_cpu: float = 0.0
        self._idle_ft = _FILETIME()
        self._kernel_ft = _FILETIME()
        self._user_ft = _FILETIME()

    def sample(self) -> float:
        if sys.platform != "win32":
            return 0.0
        ok = ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(self._idle_ft),
            ctypes.byref(self._kernel_ft),
            ctypes.byref(self._user_ft),
        )
        if not ok:
            return self._last_cpu

        idle = _filetime_to_u64(self._idle_ft)
        kernel = _filetime_to_u64(self._kernel_ft)
        user = _filetime_to_u64(self._user_ft)

        if self._prev_idle is None or self._prev_kernel is None or self._prev_user is None:
            self._prev_idle = idle
            self._prev_kernel = kernel
            self._prev_user = user
            return self._last_cpu

        d_idle = max(0, idle - self._prev_idle)
        d_kernel = max(0, kernel - self._prev_kernel)
        d_user = max(0, user - self._prev_user)

        self._prev_idle = idle
        self._prev_kernel = kernel
        self._prev_user = user

        total = d_kernel + d_user
        if total <= 0:
            return self._last_cpu

        busy = max(0, total - d_idle)
        self._last_cpu = max(0.0, min(100.0, (busy / total) * 100.0))
        return self._last_cpu


_DEFAULT_CPU_SAMPLER = _WinCpuSampler()


_MEM_STRUCT = _MEMORYSTATUSEX()
_MEM_STRUCT.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
_DISK_CACHE: tuple[float, float] = (0.0, 0.0)


def _sample_ram_percent() -> float:
    if sys.platform == "win32":
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(_MEM_STRUCT))
        if ok and _MEM_STRUCT.ullTotalPhys > 0:
            used = _MEM_STRUCT.ullTotalPhys - _MEM_STRUCT.ullAvailPhys
            return max(0.0, min(100.0, (used / _MEM_STRUCT.ullTotalPhys) * 100.0))
    return 0.0


def _sample_disk_percent() -> float:
    global _DISK_CACHE
    now = time.monotonic()
    cached_at, cached_val = _DISK_CACHE
    if cached_at > 0.0 and now - cached_at < 30.0:
        return cached_val
    target = os.environ.get("SystemDrive", "C:") + os.sep if sys.platform == "win32" else "/"
    try:
        usage = shutil.disk_usage(Path(target))
        if usage.total > 0:
            val = max(0.0, min(100.0, (usage.used / usage.total) * 100.0))
            _DISK_CACHE = (now, val)
            return val
    except OSError:
        pass
    return cached_val


def get_system_metrics() -> SystemMetrics:
    """Collect instantaneous CPU, RAM, and Disk usage with sub-millisecond overhead."""
    return SystemMetrics(
        cpu_percent=round(_DEFAULT_CPU_SAMPLER.sample(), 1),
        ram_percent=round(_sample_ram_percent(), 1),
        disk_percent=round(_sample_disk_percent(), 1),
    )


def _metric_color(val: float, default_hex: str) -> str:
    if val >= 90.0:
        return "#EF7D57"  # Alert coral
    if val >= 75.0:
        return "#FFCD75"  # Warning gold
    return default_hex


class SystemMonitorPlugin:
    """Live CPU, RAM, and Disk telemetry tool plugin for Paperduck."""

    ALERT_THRESHOLD: float = 90.0
    PORTS_SUBMENU: str = "Ports"

    def __init__(
        self,
        sampler: Callable[[], SystemMetrics] | None = None,
    ) -> None:
        self._sampler = sampler or get_system_metrics
        # Prime the delta sampler once at init so the next sample has a baseline
        self._metrics = self._sampler()
        self._last_tick_at = time.monotonic()

    @property
    def tool_id(self) -> str:
        return "sysmon"

    @property
    def display_name(self) -> str:
        return "System Monitor"

    @property
    def tick_interval_ms(self) -> int:
        return 2_000

    @property
    def metrics(self) -> SystemMetrics:
        return self._metrics

    def tick(self) -> bool:
        if self._sampler is get_system_metrics:
            now = time.monotonic()
            if now - self._last_tick_at < 1.5:
                return False
            self._last_tick_at = now
        prev = self._metrics
        self._metrics = self._sampler()
        return self._metrics != prev

    def desired_state(self) -> CompanionState | None:
        if (
            self._metrics.cpu_percent >= self.ALERT_THRESHOLD
            or self._metrics.ram_percent >= self.ALERT_THRESHOLD
        ):
            return CompanionState.ALERT
        return None

    def get_hud_data(self) -> HudData:
        is_alert = self.desired_state() == CompanionState.ALERT
        status_text = "HIGH" if is_alert else "OK"
        status_color = "#EF7D57" if is_alert else "#38B764"
        return HudData(
            badge_left="SYS.MON",
            badge_right=status_text,
            badge_right_color=status_color,
            bars=(
                HudBar(
                    label="CPU",
                    value=self._metrics.cpu_percent,
                    color_hex=_metric_color(self._metrics.cpu_percent, "#38B764"),
                ),
                HudBar(
                    label="RAM",
                    value=self._metrics.ram_percent,
                    color_hex=_metric_color(self._metrics.ram_percent, "#41A6F6"),
                ),
                HudBar(
                    label="Disk",
                    value=self._metrics.disk_percent,
                    color_hex=_metric_color(self._metrics.disk_percent, "#FF77A8"),
                ),
            ),
            detail="CPU or RAM over 90% alerts" if is_alert else "Alerts at 90% CPU or RAM",
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        items = [
            ToolMenuItem(
                label=(
                    f"Refresh Metrics (CPU {int(round(self._metrics.cpu_percent))}% | "
                    f"RAM {int(round(self._metrics.ram_percent))}%)"
                ),
                callback_id="sysmon:refresh",
            ),
        ]
        ports = list_listening_ports()
        items.extend(
            ToolMenuItem(
                label=f":{p.port}  {p.name} (pid {p.pid}) - Kill",
                callback_id=f"sysmon:kill:{p.pid}:{p.port}",
                enabled=can_kill(p.pid) and p.name != PROTECTED,
                submenu=self.PORTS_SUBMENU,
            )
            for p in ports
        )
        if not ports:
            items.append(ToolMenuItem(
                label="No dev ports listening", callback_id="sysmon:noports",
                enabled=False, submenu=self.PORTS_SUBMENU,
            ))
        return items

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id == "sysmon:refresh":
            self.tick()
            return (True, f"CPU {int(round(self._metrics.cpu_percent))}%", "#73EFF7")
        if callback_id.startswith("sysmon:kill:"):
            pid_txt, _, port_txt = callback_id.removeprefix("sysmon:kill:").partition(":")
            if not (pid_txt.isdigit() and port_txt.isdigit()):
                return (True, "KILL ERR", "#EF7D57")
            pid, port = int(pid_txt), int(port_txt)
            # The menu can be stale: only kill if this pid still owns the port
            if not any(p.pid == pid and p.port == port for p in list_listening_ports()):
                return (True, "PORT FREE", "#8A9AF2")
            if kill_pid(pid):
                return (True, f"KILLED :{port}", "#38B764")
            return (True, "KILL ERR", "#EF7D57")
        return (False, "", "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cpu_percent": self._metrics.cpu_percent,
            "ram_percent": self._metrics.ram_percent,
            "disk_percent": self._metrics.disk_percent,
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        try:
            cpu = float(data.get("cpu_percent", self._metrics.cpu_percent))
            ram = float(data.get("ram_percent", self._metrics.ram_percent))
            disk = float(data.get("disk_percent", self._metrics.disk_percent))
            self._metrics = SystemMetrics(
                cpu_percent=max(0.0, min(100.0, cpu)),
                ram_percent=max(0.0, min(100.0, ram)),
                disk_percent=max(0.0, min(100.0, disk)),
            )
        except (TypeError, ValueError):
            pass
