"""Main application entrypoint assembling multi-tool plugins, UI, system tray, and persistence."""

from __future__ import annotations

import ctypes
import faulthandler
import logging
import logging.handlers
import os
import re
import signal
import sys
from pathlib import Path
from collections.abc import Callable
from typing import Any

from PyQt6.QtCore import QPoint, Qt, QTimer
from PyQt6.QtGui import QColor, QCursor
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.storage.storage_manager import StorageManager, log_path, repair_autostart
from src.ui.context_menu import style_text_box_menus
from src.ui.pet_window import PetWindow, build_app_icon
from src.ui.quick_tools import GlobalHotkeys
from src.ui.theme import APP_STYLESHEET
from src.updater import (APP_DIR, FROZEN, FROZEN_EXE, launch_env, finish_frozen_update, is_installed,
                         remove_old_build)

# Stored beside plugin state in state.json "tools"; not a ToolPlugin, so it never joins the HUD cycle
RECENT_FILES_KEY = "recent"
WHEEL_KEY = "wheel"
MACROS_KEY = "macros"
TIPS_KEY = "tips"
SIZE_KEY = "size"
HOTKEYS_KEY = "hotkeys"

UPDATE_FAILED_FLAG = "--update-failed"
SIMULATION_TICK_INTERVAL_MS: int = 2_000
AUTO_SAVE_INTERVAL_MS: int = 60_000


class PaperduckApp:
    """Application controller coordinating multi-tool engine, floating window, tray, and storage."""

    def __init__(
        self,
        *,
        app: QApplication,
        storage: StorageManager | None = None,
        sim_tick_ms: int = SIMULATION_TICK_INTERVAL_MS,
        auto_save_ms: int = AUTO_SAVE_INTERVAL_MS,
        enable_tray: bool = True,
    ) -> None:
        self._app = app
        # Pet/overlay windows are Qt.Tool and don't count as "last window", so closing any
        # dialog (file pickers) would quit the app; quitting goes via quit() only
        self._app.setQuitOnLastWindowClosed(False)
        self._storage = storage or StorageManager()
        self._is_shut_down = False

        saved = self._storage.load()
        self.state_machine = PetStateMachine(
            stats=saved.stats,
            initial_state=saved.state,
        )

        # Restore per-plugin state and active HUD tool
        for plugin in self.state_machine.registry.plugins:
            plugin_data = saved.tools_data.get(plugin.tool_id)
            if isinstance(plugin_data, dict):
                plugin.load_dict(plugin_data)
        self.state_machine.select_tool(saved.active_tool)

        clip_vault = self.state_machine.clip_vault_plugin
        if clip_vault is not None:
            clip_vault.set_on_change(self.state_machine.notify_plugin_activity)
            clip_vault.attach_qt_clipboard(self._app.clipboard())

        self.animation_controller = AnimationController(size=32)
        self.window = PetWindow(
            state_machine=self.state_machine,
            animation_controller=self.animation_controller,
            always_on_top=saved.always_on_top,
            auto_wander=saved.auto_wander,
            on_quit_requested=self.quit,
            on_notification=self._show_tray_message,
        )
        self.window.recent.load_dict(saved.tools_data.get(RECENT_FILES_KEY))
        self.window.wheel.load_dict(saved.tools_data.get(WHEEL_KEY))
        self.window.macros.load_dict(saved.tools_data.get(MACROS_KEY))
        tips = saved.tools_data.get(TIPS_KEY)
        # No tips entry but saved tools: an install from before tips, whose user already knows the pet
        seen = tips.get("seen") if isinstance(tips, dict) else (len(PetWindow.TIPS) if saved.tools_data else 0)
        self.window.tips_seen = seen if isinstance(seen, int) and not isinstance(seen, bool) and seen >= 0 else 0

        keys = saved.tools_data.get(HOTKEYS_KEY)
        if isinstance(keys, dict):  # GlobalHotkeys falls back to the default for anything it cannot parse
            self.window.hotkeys = {m: c for m, c in keys.items() if isinstance(m, str) and isinstance(c, str)}
        size = saved.tools_data.get(SIZE_KEY)
        if isinstance(size, dict) and isinstance(size.get("scale"), int):
            self.window.set_pet_scale(size["scale"])  # unknown scales are ignored

        if saved.position is not None:
            self.window.move_clamped(saved.position[0], saved.position[1])
        else:
            self.window.reset_to_default_position()

        # Utility tick timer (2s base; scales dynamically to 1s/250ms when tools are active)
        self._base_tick_ms = max(100, sim_tick_ms)
        self.sim_timer = QTimer(self.window)
        self.sim_timer.setInterval(self._desired_tick_interval_ms())
        self.sim_timer.timeout.connect(self._on_simulation_tick)
        self.sim_timer.start()
        self._unsub_sim_sync = self.state_machine.on_stats_change(
            lambda _s: self._sync_sim_timer_interval()
        )

        # Periodic 60-second auto-save timer
        self.auto_save_timer = QTimer(self.window)
        self.auto_save_timer.setInterval(max(1_000, auto_save_ms))
        self.auto_save_timer.timeout.connect(self.save_state)
        self.auto_save_timer.start()

        # Optional Windows system tray icon for quick recovery and controls
        self.tray_icon: QSystemTrayIcon | None = None
        if enable_tray and QSystemTrayIcon.isSystemTrayAvailable():
            tray = QSystemTrayIcon(build_app_icon(64), self.window)
            tray.setToolTip("Paperduck")
            tray.activated.connect(self._on_tray_activated)
            tray.show()
            self.tray_icon = tray

        # System-wide snip hotkeys share the desktop-integration switch with the tray, so
        # headless tests never grab real key combos
        self.hotkeys: GlobalHotkeys | None = None
        if enable_tray:
            self._start_hotkeys()
            self.window.on_hotkeys_changed = self._start_hotkeys
            repair_autostart()  # copies updated in place may hold a Run value for a launcher they never received
            if is_installed():
                # One quiet check per launch; installing waits for Settings > Check for Updates
                QTimer.singleShot(20_000, lambda: self.window.check_for_updates(install=False))

        self._app.aboutToQuit.connect(self.shutdown)
        self._app.commitDataRequest.connect(lambda _mgr: self.save_state())

    def _show_tray_message(self, text: str) -> None:
        if self.tray_icon is not None:
            self.tray_icon.showMessage("Paperduck", text, build_app_icon(64), 4_000)

    def _desired_tick_interval_ms(self) -> int:
        return self.state_machine.registry.min_active_tick_interval_ms(self._base_tick_ms)

    def _sync_sim_timer_interval(self) -> None:
        if self._is_shut_down:
            return
        desired = self._desired_tick_interval_ms()
        if self.sim_timer.interval() != desired:
            self.sim_timer.setInterval(desired)

    def show(self) -> None:
        self.window.show()
        self.window.raise_()

    def _on_simulation_tick(self) -> None:
        self.state_machine.tick(1)  # its stats-change event resyncs the timer interval

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Context:
            menu = self.window.build_context_menu()
            try:
                menu.exec(QCursor.pos())
            finally:
                menu.deleteLater()  # one tray menu per right-click: without this they piled up
        elif reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.window.showNormal()
            self.window.raise_()
            self.window.activateWindow()
            self.window._show_toast("READY", QColor("#73EFF7"))

    def _start_hotkeys(self) -> None:
        """(Re)register the system-wide hotkeys with the user's combos; say which ones another app holds."""
        if self.hotkeys is not None:
            self.hotkeys.close()
        self.hotkeys = GlobalHotkeys(self.window.hotkeys)
        self.hotkeys.triggered.connect(
            lambda m: self.window.trigger_tool_action(m if m.startswith("macrokey:") else f"snip:{m}"))
        self.hotkeys.start()
        if self.hotkeys.failed:
            self._show_tray_message(f"Taken by another app: {', '.join(self.hotkeys.failed)}. "
                                    "Change them in Settings > Hotkeys")

    def save_state(self) -> bool:
        pos: QPoint = self.window.pos()
        tools_payload: dict[str, dict[str, Any]] = {}
        for plugin in self.state_machine.registry.plugins:
            tools_payload[plugin.tool_id] = plugin.to_dict()
        tools_payload[RECENT_FILES_KEY] = self.window.recent.to_dict()
        tools_payload[WHEEL_KEY] = self.window.wheel.to_dict()
        tools_payload[MACROS_KEY] = self.window.macros.to_dict()
        tools_payload[TIPS_KEY] = {"seen": self.window.tips_seen}
        tools_payload[SIZE_KEY] = {"scale": self.window.pet_scale}
        tools_payload[HOTKEYS_KEY] = dict(self.window.hotkeys)
        return self._storage.save(
            stats=self.state_machine.stats,
            state=self.state_machine.state,
            position=(pos.x(), pos.y()),
            always_on_top=self.window.always_on_top,
            auto_wander=self.window.auto_wander,
            active_tool=self.state_machine.active_tool_id,
            tools_data=tools_payload,
        )

    def has_active_timers(self) -> bool:
        return (
            self.sim_timer.isActive()
            or self.auto_save_timer.isActive()
            or self.window.has_active_timers()
        )

    def shutdown(self) -> None:
        """Idempotently stop timers, persist state, and release tray resources."""
        if self._is_shut_down:
            return
        self._is_shut_down = True

        if self._unsub_sim_sync is not None:
            self._unsub_sim_sync()
            self._unsub_sim_sync = None  # type: ignore[assignment]
        self.sim_timer.stop()
        self.auto_save_timer.stop()
        if self.hotkeys is not None:
            self.hotkeys.close()
            self.hotkeys = None
        self.save_state()
        self.window.shutdown()
        self.state_machine.shutdown()

        if self.tray_icon is not None:
            self.tray_icon.hide()
            self.tray_icon.deleteLater()
            self.tray_icon = None

    def quit(self) -> None:
        self.shutdown()
        self.window.close()
        self._app.quit()


def _detach_console_if_present() -> None:
    """Ensure no console window remains attached if launched directly from Explorer."""
    if sys.platform == "win32" and "--console" not in sys.argv:
        try:
            # Detach from our own console buffer without hiding any parent shell window
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass


def setup_logging() -> Path | None:
    """Send the app's log lines to a small rotating file (calls and errors only, never keys or message text)."""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=512 * 1024, backupCount=1, encoding="utf-8")
    except OSError:
        return None  # no log is better than no app
    root = logging.getLogger("src")
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        handler.close()
        return path  # already set up: a second call must not write every line twice
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return path


MAX_LOGGED_ERRORS = 20  # an error in a paint or timer handler repeats every frame; the first ones say enough
_crash_file = None  # faulthandler writes to this file on a native crash, so it must stay open
_CRASH_BLOCK = re.compile(r"(?m)^(?=Windows fatal exception:|Fatal Python error:)")
# COM RPC errors (facility RPC, 0x8001xxxx, e.g. 0x8001010d during a clipboard change): RPC raises and catches them
# itself, but faulthandler's Windows handler sees every first-chance exception and logs them as fatal
_HANDLED_CODE = re.compile(r"Windows fatal exception: code 0x8001[0-9a-f]{4}\b", re.I)


def prune_crash_log(path: Path) -> None:
    """Drop the handled COM RPC entries from crash.log, so a real crash stands out (faulthandler has no filter)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    blocks = _CRASH_BLOCK.split(text)
    kept = [b for b in blocks if not _HANDLED_CODE.match(b)]
    if len(kept) != len(blocks):
        try:
            path.write_text("".join(kept), encoding="utf-8")
        except OSError:
            pass


def install_crash_guard(on_error: Callable[[], None] | None = None) -> Callable[..., None]:
    """Keep running after an unhandled Python error: with the default hook PyQt6 aborts the whole app
    (0xC0000409) for any exception raised in a Qt slot or event handler. Errors go to the log (Settings > Copy
    Log) and `on_error` runs (a toast). Native crashes cannot be survived, but leave a stack in crash.log."""
    global _crash_file
    log, seen = logging.getLogger("src.main"), [0]

    def hook(kind: type[BaseException], value: BaseException, tb: Any) -> None:
        if issubclass(kind, KeyboardInterrupt):
            sys.__excepthook__(kind, value, tb)
            return
        seen[0] += 1
        if seen[0] <= MAX_LOGGED_ERRORS:
            log.error("unhandled error (kept running)", exc_info=(kind, value, tb))
        if on_error is not None:
            try:
                on_error()
            except Exception:  # noqa: BLE001 - the error report must never raise again
                pass

    sys.excepthook = hook
    if _crash_file is None:
        try:
            prune_crash_log(log_path().with_name("crash.log"))  # what the last run logged; this run's waits a launch
            _crash_file = open(log_path().with_name("crash.log"), "a", encoding="utf-8")  # noqa: SIM115
            faulthandler.enable(_crash_file)
        except (OSError, RuntimeError):
            _crash_file = None
    return hook


def _finish_update(args: list[str]) -> int:
    """`app.new/Paperduck.exe --finish-update <pid>`: swap the staged build in, then start it."""
    import subprocess

    os.chdir(APP_DIR.parent)
    setup_logging()
    log = logging.getLogger("src.main")
    app_dir: Path | None = APP_DIR.with_name(APP_DIR.name.removesuffix(".new"))
    flags: list[str] = []
    try:
        app_dir = finish_frozen_update(int(args[args.index("--finish-update") + 1]))
    except (OSError, ValueError, IndexError) as exc:
        log.warning("finishing the update failed, starting the old build: %s: %s", type(exc).__name__, exc)
        flags.append(UPDATE_FAILED_FLAG)  # the old build reports it instead of looking updated
    if app_dir is None:
        log.warning("finishing the update: the old copy did not exit")
        return 1
    subprocess.Popen([str(app_dir / FROZEN_EXE), *flags], cwd=str(app_dir.parent), close_fds=True,
                     env=launch_env(),
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Launch Paperduck on the Windows desktop."""
    if argv is None:
        _detach_console_if_present()
    raw_args = argv if argv is not None else sys.argv
    if FROZEN and "--finish-update" in raw_args:
        return _finish_update(raw_args)
    if FROZEN:
        os.chdir(APP_DIR.parent)  # outside the app folder, so the update helper can rename it
        remove_old_build()
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    setup_logging()
    install_crash_guard()  # startup errors are logged too
    args = argv if argv is not None else sys.argv
    app = QApplication(args)
    app.setApplicationName("Paperduck")
    app.setWindowIcon(build_app_icon(64))
    app.setStyleSheet(APP_STYLESHEET)
    style_text_box_menus(app)

    storage_override: StorageManager | None = None
    if "--state-file" in args:
        idx = args.index("--state-file")
        if idx + 1 < len(args):
            storage_override = StorageManager(Path(args[idx + 1]))

    controller = PaperduckApp(app=app, storage=storage_override)
    install_crash_guard(lambda: (controller.window._react("#E05A47"),
                                 controller.window._show_toast("OOPS", QColor("#E05A47"))))

    # Allow graceful Ctrl+C exit when launched from a terminal
    signal.signal(signal.SIGINT, lambda *_sig: controller.quit())

    controller.show()
    if UPDATE_FAILED_FLAG in args:
        controller.window._on_update_done(None, "error", "could not replace the app folder (Settings > Copy Log)", True)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
