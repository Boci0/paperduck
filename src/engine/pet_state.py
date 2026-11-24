"""Headless utility state coordinator driving Paperduck multi-tools and operational states."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from pathlib import Path

from src.engine.models import CompanionState, HudData, PetState, PetStats, ToolMenuItem
from src.tools.base import ToolPlugin, ToolRegistry
from src.tools.ai_assist import AiAssistPlugin
from src.tools.clip_vault import ClipVaultPlugin
from src.tools.file_forge import FileForgePlugin, ForgeMode
from src.tools.errands import ErrandsPlugin
from src.tools.hints import HintsPlugin
from src.tools.launcher import LauncherPlugin
from src.tools.pomodoro import PomodoroPhase, PomodoroPlugin
from src.tools.project import ProjectPlugin
from src.tools.system_monitor import SystemMonitorPlugin

StateChangeCallback = Callable[[PetState, PetState, PetStats], None]
StatsChangeCallback = Callable[[PetStats], None]


class PetStateMachine:
    """Discrete-tick multi-tool state machine for Paperduck."""

    ALERT_THRESHOLD: float = 90.0

    def __init__(
        self,
        stats: PetStats | None = None,
        initial_state: PetState | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        self._stats: PetStats = stats if stats is not None else PetStats()
        self._stats.clamp()
        self._dnd_enabled: bool = initial_state == PetState.SLEEPING
        self._state_callbacks: list[StateChangeCallback] = []
        self._stats_callbacks: list[StatsChangeCallback] = []

        if registry is not None:
            self._registry = registry
        else:
            sysmon = SystemMonitorPlugin()
            pomodoro = PomodoroPlugin()
            pomodoro.completed_sessions = self._stats.completed_sessions
            forge = FileForgePlugin()
            clipvault = ClipVaultPlugin()
            project = ProjectPlugin()
            launcher = LauncherPlugin(project_dir=lambda: project.repo)
            self._registry = ToolRegistry([sysmon, pomodoro, forge, clipvault, project,
                                           launcher, HintsPlugin(), AiAssistPlugin(), ErrandsPlugin()])

        self._sync_stats_from_tools()

        if initial_state is not None and isinstance(initial_state, PetState):
            self._state = initial_state
            if initial_state == PetState.PLAYING:
                pomo = self.pomodoro_plugin
                if pomo is not None and not pomo.running:
                    pomo.start_work()
        else:
            self._state = self._derive_state()

    @property
    def stats(self) -> PetStats:
        return self._stats

    @property
    def state(self) -> PetState:
        return self._state

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def active_tool_id(self) -> str:
        return self._registry.active_tool_id

    @property
    def active_plugin(self) -> ToolPlugin | None:
        return self._registry.active_plugin

    @property
    def sysmon_plugin(self) -> SystemMonitorPlugin | None:
        plugin = self._registry.get_plugin("sysmon")
        return plugin if isinstance(plugin, SystemMonitorPlugin) else None

    @property
    def pomodoro_plugin(self) -> PomodoroPlugin | None:
        plugin = self._registry.get_plugin("pomodoro")
        return plugin if isinstance(plugin, PomodoroPlugin) else None

    @property
    def forge_plugin(self) -> FileForgePlugin | None:
        plugin = self._registry.get_plugin("forge")
        return plugin if isinstance(plugin, FileForgePlugin) else None

    @property
    def clip_vault_plugin(self) -> ClipVaultPlugin | None:
        plugin = self._registry.get_plugin("clipvault")
        return plugin if isinstance(plugin, ClipVaultPlugin) else None

    @property
    def launcher_plugin(self) -> LauncherPlugin | None:
        plugin = self._registry.get_plugin("launcher")
        return plugin if isinstance(plugin, LauncherPlugin) else None

    def on_state_change(self, callback: StateChangeCallback) -> Callable[[], None]:
        """Register a listener for state transitions; returns an unsubscribe callable."""
        self._state_callbacks.append(callback)

        def _unsubscribe() -> None:
            if callback in self._state_callbacks:
                self._state_callbacks.remove(callback)

        return _unsubscribe

    def on_stats_change(self, callback: StatsChangeCallback) -> Callable[[], None]:
        """Register a listener for metric/tool updates; returns an unsubscribe callable."""
        self._stats_callbacks.append(callback)

        def _unsubscribe() -> None:
            if callback in self._stats_callbacks:
                self._stats_callbacks.remove(callback)

        return _unsubscribe

    def tick(self, elapsed_ticks: int = 1) -> None:
        """Advance registered tools by one or more discrete ticks."""
        if elapsed_ticks <= 0:
            return

        for _ in range(elapsed_ticks):
            self._step_single_tick()

        self._stats.last_updated = time.time()
        self._emit_stats_changed()

    def _step_single_tick(self) -> None:
        self._registry.tick_all()
        self._sync_stats_from_tools()
        self._transition_to(self._derive_state())

    def _sync_stats_from_tools(self) -> None:
        sysmon = self.sysmon_plugin
        if sysmon is not None:
            m = sysmon.metrics
            self._stats.cpu_usage = m.cpu_percent
            self._stats.ram_usage = m.ram_percent
            self._stats.disk_usage = m.disk_percent

        pomo = self.pomodoro_plugin
        if pomo is not None:
            self._stats.completed_sessions = pomo.completed_sessions
        self._stats.clamp()

    def get_hud_data(self) -> HudData:
        """Return the HudData card for the currently active tool plugin."""
        plugin = self._registry.active_plugin
        if plugin is not None:
            return plugin.get_hud_data()
        return HudData(badge_left="BYTE", badge_right="IDLE")

    def get_active_menu_items(self) -> list[ToolMenuItem]:
        """Return context menu items for all registered tools."""
        items: list[ToolMenuItem] = []
        for plugin in self._registry.plugins:
            items.extend(plugin.get_menu_items())
        return items

    def cycle_tool(self) -> ToolPlugin | None:
        """Cycle the active HUD view to the next registered tool plugin."""
        nxt = self._registry.cycle_active_tool()
        self._stats.last_updated = time.time()
        self._emit_stats_changed()
        return nxt

    def select_tool(self, tool_id: str) -> bool:
        """Select a specific registered tool plugin by ID."""
        if self._registry.set_active_tool(tool_id):
            self._stats.last_updated = time.time()
            self._emit_stats_changed()
            return True
        return False

    def execute_tool_action(self, callback_id: str) -> tuple[bool, str, str]:
        """Dispatch a tool action callback_id to registered plugins."""
        for plugin in self._registry.plugins:
            handled, toast_text, color_hex = plugin.execute_action(callback_id)
            if handled:
                if callback_id.startswith("pomodoro:") and self._dnd_enabled:
                    self._dnd_enabled = False
                self._sync_stats_from_tools()
                self._stats.last_updated = time.time()
                self._transition_to(self._derive_state())
                self._emit_stats_changed()
                return (True, toast_text, color_hex)
        return (False, "", "")

    def submit_files(
        self,
        paths: Sequence[Path | str],
        *,
        mode: ForgeMode | None = None,
    ) -> bool:
        """Route dropped or selected local files/folders to FileForgePlugin."""
        forge = self.forge_plugin
        if forge is None:
            return False
        if not forge.submit_paths(paths, mode_override=mode):
            return False
        self._dnd_enabled = False
        self._registry.set_active_tool("forge")
        self._stats.last_updated = time.time()
        self._transition_to(self._derive_state())
        self._emit_stats_changed()
        return True

    def pop_notifications(self) -> list[tuple[str, str]]:
        """Drain queued async completion toasts from registered plugins."""
        return self._registry.pop_notifications()

    def notify_plugin_activity(self) -> None:
        """Emit stats update when an event-driven plugin (e.g. ClipVault) mutates."""
        self._stats.last_updated = time.time()
        self._emit_stats_changed()

    def shutdown(self) -> None:
        """Stop background workers and detach OS listeners across all plugins."""
        self._registry.close_all()

    def refresh_metrics(self) -> bool:
        """Trigger an immediate system monitor refresh."""
        sysmon = self.sysmon_plugin
        if sysmon is not None:
            sysmon.tick()
        self._sync_stats_from_tools()
        self._stats.last_updated = time.time()
        self._transition_to(self._derive_state())
        self._emit_stats_changed()
        return True

    def toggle_pomodoro(self) -> bool:
        """Start, pause, or resume the Pomodoro timer and switch HUD to Pomodoro."""
        pomo = self.pomodoro_plugin
        if pomo is None:
            return False
        self._dnd_enabled = False
        running = pomo.toggle_pause()
        self._registry.set_active_tool("pomodoro")
        self._sync_stats_from_tools()
        self._stats.last_updated = time.time()
        self._transition_to(self._derive_state())
        self._emit_stats_changed()
        return running

    def rest(self) -> bool:
        """Enter Do Not Disturb / Standby mode."""
        self._dnd_enabled = True
        pomo = self.pomodoro_plugin
        if pomo is not None and pomo.running:
            pomo.toggle_pause()
        self._stats.last_updated = time.time()
        self._transition_to(PetState.SLEEPING)
        self._emit_stats_changed()
        return True

    def wake(self) -> bool:
        """Exit Do Not Disturb / Standby mode."""
        if self._state != PetState.SLEEPING and not self._dnd_enabled:
            return False
        self._dnd_enabled = False
        pomo = self.pomodoro_plugin
        if pomo is not None and pomo.phase == PomodoroPhase.BREAK and pomo.running:
            pomo.reset()
        self._stats.last_updated = time.time()
        self._transition_to(self._derive_state())
        self._emit_stats_changed()
        return True

    # Legacy action aliases mapped onto utility operations
    def feed(self, amount: float = 25.0) -> bool:
        if amount <= 0.0:
            return False
        return self.refresh_metrics()

    def pet(self, amount: float = 15.0) -> bool:
        if amount <= 0.0:
            return False
        self.cycle_tool()
        return True

    def play(self) -> bool:
        return self.toggle_pomodoro()

    def _derive_state(self) -> PetState:
        if self._dnd_enabled:
            return PetState.SLEEPING

        # Priority 1: Active Pomodoro Focus or Break state
        pomo = self.pomodoro_plugin
        if pomo is not None:
            pomo_desired = pomo.desired_state()
            if pomo_desired is not None:
                return pomo_desired

        # Priority 2: System Monitor high resource alert
        sysmon = self.sysmon_plugin
        if sysmon is not None:
            sys_desired = sysmon.desired_state()
            if sys_desired is not None:
                return sys_desired

        # Priority 3: Any other registered plugin requesting an override state
        for plugin in self._registry.plugins:
            if plugin.tool_id in {"pomodoro", "sysmon"}:
                continue
            desired = plugin.desired_state()
            if desired is not None:
                return desired

        return CompanionState.IDLE

    def _transition_to(self, next_state: PetState) -> None:
        if next_state == self._state:
            return
        previous = self._state
        self._state = next_state
        for callback in tuple(self._state_callbacks):
            callback(previous, next_state, self._stats)

    def _emit_stats_changed(self) -> None:
        for callback in tuple(self._stats_callbacks):
            callback(self._stats)
