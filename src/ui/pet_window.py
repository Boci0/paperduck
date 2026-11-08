"""Frameless translucent floating desktop window for Paperduck."""

from __future__ import annotations

import ctypes
import random
import sys
import time
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QElapsedTimer, QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import (
    QCloseEvent,
    QColor,
    QContextMenuEvent,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QEnterEvent,
    QGuiApplication,
    QImage,
    QMouseEvent,
    QPalette,
)
from PyQt6.QtWidgets import QApplication, QInputDialog, QToolTip, QWidget

from src.engine.models import PetState, PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.tools.file_forge import ForgeMode, get_wheel_options_for_paths
from src.tools.ai_assist import AiJob, AiReply
from src.tools.macros import MacroBook
from src.tools.hints import Suggestion
from src.tools import computer
from src.ui.ai_chat import AiChat
from src.ui.ai_tool_host import show_quietly
from src.ui.screen_glow import ScreenGlow
from src.storage.recent_files import RecentFiles
from src.tools.web_drop import WebImageJob, web_image_stem
from src.ui.context_menu import PetContextMenu
from src.ui.drop_wheel import RadialDropWheel
from src.ui.macro_runner import MacroRunner
from src.ui import perch
from src.ui.quick_tools import HOTKEY_ACTIONS, OcrJob, SnipSession, default_hotkey, hotkey_label, parse_hotkey
from src.ui.sfx import preload_tick_sounds, prewarm as prewarm_ticks
from src.ui.tool_wheel import ToolWheel, WheelLayout
from src.updater import UpdateJob
from src.ui.theme import saved_theme


from src.ui.pet_helpers import ScreenRect, yield_topmost, _SHELL_CLASSES, fullscreen_in_front, READING_TOOLS, WORKING_TOOLS, LOOKING_TOOLS, activity_clip, clamp_position_to_screens, build_app_icon, export_windows_ico, VK_VOLUME_MUTE, KEYEVENTF_KEYUP  # noqa: F401 (re-exported)
from src.ui.pet_ai import AiMixin
from src.ui.pet_paint import PaintMixin
from src.ui.pet_tools import ToolsMixin
from src.ui.pet_watch import WatchMixin


class PetWindow(WatchMixin, ToolsMixin, AiMixin, PaintMixin, QWidget):
    """Borderless translucent desktop widget rendering Paperduck and hover HUD."""

    WINDOW_WIDTH: int = 176
    WINDOW_HEIGHT: int = 222
    SPRITE_SCALE: int = 4  # the largest size, 32x32 * 4 = 128x128; WINDOW_HEIGHT is laid out for it
    PET_SCALES: dict[int, str] = {2: "Small", 3: "Medium", 4: "Large"}  # whole numbers keep the pixels crisp
    DEFAULT_PET_SCALE: int = 2  # Small until the user picks another size (a saved size wins)
    DRAG_THRESHOLD_PX: int = 5
    HOLD_TO_WHEEL_MS: int = 450
    WHEEL_RELEASE_GRACE_MS: int = 90
    # First-run tips on the hint card: (title, how-to line), one at a time, TIP_GAP_S apart
    TIPS: tuple[tuple[str, str], ...] = (
        ("Hold me for tools", "Press, wait, slide, release"),
        ("Drop files on me", "Convert, zip or extract them"),
        ("Right-click for more", "Settings, macros, AI chat"),
    )
    TIP_FIRST_S: float = 8.0
    TIP_GAP_S: float = 30.0
    SUCCESS_HEX: str = "#38B764"  # the green every tool uses for a finished job
    QUIET_NOTICES: frozenset[str] = frozenset({"COMMITTED", "PUSHED"})  # toast and react, no tray balloon
    BIG_WINS: frozenset[str] = frozenset({"FOCUS DONE", "PUSHED"})  # cheer plus the pixel-star burst
    LONG_TASK_S: float = 30.0  # an AI answer that took this long cheers when it lands
    AWAY_AFTER_S: float = 300.0  # no keyboard or mouse input this long: the idle clip becomes a nap
    PERCH_TICK_MS: int = 16  # perch follow and fall frame time (about 60 fps)
    PERCH_COVER_EVERY: int = 6  # perch ticks between checks that no other window covers the duck's footing
    FALL_ACCEL: float = 2400.0  # px/s^2: a flutter, a bit gentler than a drop
    PREWARM_DELAY_MS: int = 150  # a drag has usually passed DRAG_THRESHOLD_PX by now; still before the wheel opens

    def __init__(
        self,
        *,
        state_machine: PetStateMachine,
        animation_controller: AnimationController | None = None,
        always_on_top: bool = True,
        auto_wander: bool = True,
        on_quit_requested: Callable[[], None] | None = None,
        on_notification: Callable[[str], None] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._state_machine = state_machine
        self._anim = animation_controller or AnimationController(size=32)
        self._always_on_top = always_on_top
        self._auto_wander = auto_wander
        self._on_quit_requested = on_quit_requested
        self._on_notification = on_notification

        self.pet_scale = self.DEFAULT_PET_SCALE  # Settings > Pet Size; persisted by PaperduckApp
        self._layout_sprite()
        self._hud_rect = QRect(8, 4, self.WINDOW_WIDTH - 16, 72)

        # Rendered frames keyed by (clip_name, frame_index, facing_left, device px)
        self._image_cache: dict[tuple[str, int, bool, int], QImage] = {}

        # Mouse drag and hover state
        self._press_global_pos: QPoint | None = None
        self._last_drag_global_pos: QPoint | None = None
        self._drag_offset: QPoint | None = None
        self._did_drag: bool = False
        self._hovered: bool = False
        self._hud_pinned_until_ticks: int = 0

        # Floating micro-toast feedback
        self._toast_text: str = ""
        self._toast_color: QColor = QColor("#73EFF7")
        self._toast_steps: int = 0
        self._toast_max_steps: int = 12

        # Gentle autonomous walk steps
        self._walk_dx: int = 0
        self._walk_steps_remaining: int = 0
        self._perch: tuple[int, int] | None = None  # (hwnd, sprite centre x minus the edge's left) while sitting
        self._fall_to: int | None = None  # window y a perch fall ends at
        self._perch_ticks = 0
        self._fall_x, self._fall_y, self._fall_v = 0, 0.0, 0.0  # x, exact y, speed in px/s
        # Perch follow and falls run at display rate (the 90ms overlay tick looked choppy); only while needed
        self._perch_timer = QTimer(self)
        self._perch_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._perch_timer.setInterval(self.PERCH_TICK_MS)
        self._perch_timer.timeout.connect(self._on_perch_tick)
        self._perch_clock = QElapsedTimer()
        self._is_shutting_down: bool = False
        self._drop_wheel: RadialDropWheel | None = None
        self._snip_session: SnipSession | None = None
        # Single click waits out the double-click interval so a double-click snips without also cycling tools
        self._click_timer = QTimer(self)
        self._click_timer.setSingleShot(True)
        self._click_timer.setInterval(QApplication.styleHints().mouseDoubleClickInterval())
        self._click_timer.timeout.connect(self.trigger_pet)
        # Holding still on the pet opens the tool wheel; any drag past the threshold cancels it
        self._tool_wheel: ToolWheel | None = None
        self._wheel_holding: bool = False
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.setInterval(self.HOLD_TO_WHEEL_MS)
        self._hold_timer.timeout.connect(self._on_hold_timeout)
        # A release that ends a wheel hold waits this long: a press inside it (a quick re-press, or a worn switch
        # bouncing) keeps the same hold, instead of closing the wheel and opening a second one plus a double-click
        self._wheel_release_timer = QTimer(self)
        self._wheel_release_timer.setSingleShot(True)
        self._wheel_release_timer.setInterval(self.WHEEL_RELEASE_GRACE_MS)
        self._wheel_release_timer.timeout.connect(self._finish_wheel_release)
        self._wheel_release_pos = QPoint()
        self._ocr_jobs: list[OcrJob] = []
        self._ai_chat: AiChat | None = None  # built hidden soon after launch (_prebuild_ai_chat) or on first open
        QTimer.singleShot(self.PREBUILD_CHAT_MS, self._prebuild_ai_chat)
        self._ai_thinking: bool = False
        self._ai_since: float = 0.0  # when the pending AI answer started
        self._ai_activity = ""  # the tool the chat model runs now (body language), "approval" while one waits
        self._errand_running = False  # the chat's pending answer is a fired errand
        self._away: bool = False  # no input for AWAY_AFTER_S: idle naps (visual only, never DND)
        self._glows: list[ScreenGlow] = []
        # Image dragged out of a browser (URL or raw image), fetched only once a slice is chosen
        self._pending_web_source: str | QImage | None = None
        # Raw path strings cached at drag-enter: re-reading or parsing thousands per event lags big drops
        self._drag_paths: list[str] = []
        self._web_jobs: list[WebImageJob] = []
        self._update_jobs: list[UpdateJob] = []
        # Clean PNG from a web drop, deleted once File Forge converts it to the chosen format
        self._web_intermediate: Path | None = None
        self.recent = RecentFiles()
        self.wheel = WheelLayout()  # Settings > Customize Wheel, persisted as tools.wheel
        self.macros = MacroBook()  # right-click > Macros, persisted as tools.macros
        self._macro_runner = MacroRunner(self)
        QTimer.singleShot(0, preload_tick_sounds)  # wheels are built lazily; sounds must be ready first

        self._configure_window()
        self._sync_animation_to_pet_state(self._state_machine.state)

        self._unsub_state = self._state_machine.on_state_change(self._handle_state_change)
        self._unsub_stats = self._state_machine.on_stats_change(self._handle_stats_change)

        # Frame timer uses exact frame durations when idle and steps smoothly during toasts/walks
        self._anim_timer = QTimer(self)
        self._anim_clock = QElapsedTimer()  # real time since the last tick: 45ms shine frames ran on a 60ms floor
        self._anim_timer.timeout.connect(self._on_animation_tick)
        self._schedule_next_anim_tick()

        # Ambient behavior timer (occasional blinks and tiny hops while idle)
        self._ambient_timer = QTimer(self)
        self._ambient_timer.setInterval(4200)
        self._ambient_timer.timeout.connect(self._on_ambient_tick)
        self._ambient_timer.start()

        # Other topmost windows can push the pet down the z-order; Qt sets the flag only once
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(2000)
        self._topmost_timer.timeout.connect(self._reassert_topmost)
        self._topmost_timer.start()

        # HUD auto-hide countdown timer (250ms resolution only while HUD/toast active)
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setInterval(90)
        self._overlay_timer.timeout.connect(self._on_overlay_tick)

        # Offline context hints (Hints tool): a clickable card over the sprite, polled every 3s
        self._hint: Suggestion | None = None
        self._hint_until: float = 0.0
        self.hotkeys: dict[str, str] = {}  # mode -> combo text overriding a default; persisted by PaperduckApp
        self.on_hotkeys_changed: Callable[[], None] | None = None  # set by PaperduckApp: re-register them
        self.tips_seen: int = 0  # persisted by PaperduckApp; a tip counts as seen once shown
        self._tip_due: float = time.monotonic() + self.TIP_FIRST_S
        self._hint_rect = QRect(8, 4, self.WINDOW_WIDTH - 16, 40)
        self._hint_timer = QTimer(self)
        self._hint_timer.setInterval(3000)
        self._hint_timer.timeout.connect(self._poll_hint)
        self._hint_timer.start()
        # Watch mode: one lens answer at a time, for the hint on the card
        self._watch_job: AiJob | None = None
        self._watch_reply: AiReply | None = None
        self._watch_open = False  # the user clicked: open the chat when the answer lands
        self._folder_job: AiJob | None = None
        self._tidy_asks = 0  # a fresh approval key per plan, so "Allow" never carries over to the next one

    def _configure_window(self) -> None:
        self.setWindowTitle("Paperduck")
        self.setWindowIcon(build_app_icon(64))

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAutoFillBackground(False)
        self.setAcceptDrops(True)

        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(0, 0, 0, 0))
        self.setPalette(palette)

        self._apply_window_flags(re_show=False)

    def _apply_window_flags(self, *, re_show: bool) -> None:
        was_visible = self.isVisible()
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAcceptDrops(True)
        if re_show and was_visible:
            show_quietly(self)

    @property
    def always_on_top(self) -> bool:
        return self._always_on_top

    @property
    def auto_wander(self) -> bool:
        return self._auto_wander

    def _open_wheels(self) -> list:
        return [w for w in (self._tool_wheel, self._drop_wheel) if w is not None and w.isVisible()]

    def _reassert_topmost(self) -> None:
        if sys.platform != "win32" or not self._always_on_top or not self.isVisible():
            return
        if QApplication.activePopupWidget() is not None or QToolTip.isVisible():  # raising would cover it
            return
        wheels = self._open_wheels()
        own = {int(w.winId()) for w in (self, *wheels, self._ai_chat) if w is not None and w.isVisible()}
        set_pos = ctypes.windll.user32.SetWindowPos
        set_pos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                            ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        if fullscreen_in_front(own, int(self.winId())):
            # Skipping the re-raise is not enough: a topmost pet stays above a normal fullscreen window. Placing it
            # right after that window drops its topmost bit, so it sits behind until the fullscreen app goes.
            fg = computer.foreground_window()
            for w in (self, self._ai_chat):
                if w is not None and w.isVisible() and computer.is_topmost(int(w.winId())):
                    set_pos(int(w.winId()), fg, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            return
        if yield_topmost(own):  # the next tick tries again
            return
        # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE: re-raise without stealing focus
        raised = False
        pet = int(self.winId())
        if not wheels and (not computer.is_topmost(pet) or computer.under_foreign_topmost(pet)):  # else no churn
            set_pos(int(self.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            raised = True
        for wheel in wheels:
            wheel.restack_topmost()  # wheels stay above the pet but below an in-flight drag image
        chat = self._ai_chat
        if chat is not None and chat.isVisible() and (raised or not computer.is_topmost(int(chat.winId()))
                                                      or computer.under_foreign_topmost(int(chat.winId()))):
            set_pos(int(chat.winId()), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)  # the pet never covers its chat

    def set_always_on_top(self, enabled: bool) -> None:
        if self._always_on_top == bool(enabled):
            return
        self._always_on_top = bool(enabled)
        self._apply_window_flags(re_show=True)
        for wheel in (self._drop_wheel, self._tool_wheel):
            if wheel is not None:
                was_open = wheel.isVisible()
                wheel._always_on_top = self._always_on_top
                wheel._configure_window()  # setWindowFlags hides an open wheel
                if was_open:
                    show_quietly(wheel)

    def set_auto_wander(self, enabled: bool) -> None:
        self._auto_wander = bool(enabled)
        if not self._auto_wander and self._walk_steps_remaining > 0:
            self._walk_steps_remaining = 0
            self._sync_animation_to_pet_state(self._state_machine.state)
            self._schedule_next_anim_tick()
            self.update()

    def _layout_sprite(self) -> None:
        """Size the window to the sprite: same width (HUD and cards keep their size), shorter for smaller pets."""
        size = 32 * self.pet_scale
        height = self.WINDOW_HEIGHT - 32 * self.SPRITE_SCALE + size
        self._sprite_size_px = size
        self._sprite_rect = QRect((self.WINDOW_WIDTH - size) // 2, height - size - 6, size, size)
        self.setFixedSize(self.WINDOW_WIDTH, height)

    def _edit_hotkey(self, mode: str) -> None:
        if mode not in HOTKEY_ACTIONS:
            return
        current = self.hotkeys.get(mode) or default_hotkey(mode)
        text, ok = QInputDialog.getText(
            self, f"Paperduck - {HOTKEY_ACTIONS[mode]} Hotkey",
            f"Type a combo like Ctrl+Alt+K (A-Z, 0-9, F1-F24).\nEmpty resets to {default_hotkey(mode)}.", text=current)
        if not ok:
            return
        if not text.strip():
            self.hotkeys.pop(mode, None)
        else:
            try:
                label = hotkey_label(*parse_hotkey(text))
            except ValueError as exc:
                self._show_toast("BAD KEYS", QColor("#E05A47"))
                if self._on_notification is not None:
                    self._on_notification(f"Hotkey not changed: {exc}")
                return
            if label in (self.hotkeys.get(m) or default_hotkey(m) for m in HOTKEY_ACTIONS if m != mode):
                self._show_toast("IN USE", QColor("#E05A47"))
                return
            self.hotkeys[mode] = label
        self._show_toast("HOTKEY SET", QColor("#38B764"))
        if self.on_hotkeys_changed is not None:
            self.on_hotkeys_changed()

    def set_pet_scale(self, scale: int) -> None:
        if scale not in self.PET_SCALES or scale == self.pet_scale:
            return
        old_height = self.height()
        self.pet_scale = scale
        self._layout_sprite()
        # Keep the pet standing on the same spot: the window grows or shrinks upward
        self.move_clamped(self.x(), self.y() + old_height - self.height())
        self.update()

    def sprite_center_point(self) -> QPoint:
        return self._sprite_rect.center()

    def available_screen_rects(self) -> list[ScreenRect]:
        screens = QGuiApplication.screens()
        result: list[ScreenRect] = []
        for scr in screens:
            geom = scr.availableGeometry()
            result.append(ScreenRect(geom.x(), geom.y(), geom.width(), geom.height()))
        return result

    def move_clamped(self, x: int, y: int) -> None:
        cx, cy = clamp_position_to_screens(
            x,
            y,
            self.width(),
            self.height(),
            self.available_screen_rects(),
        )
        self.move(cx, cy)

    def reset_to_default_position(self) -> None:
        self._perch = self._fall_to = None
        primary = QGuiApplication.primaryScreen()
        if primary is None:
            self.move(120, 120)
            return
        avail = primary.availableGeometry()
        target_x = avail.right() - self.width() - 48
        target_y = avail.bottom() - self.height() - 18
        self.move_clamped(target_x, target_y)
        self._show_toast("HOME", QColor("#73EFF7"))

    # ------------------------------------------------------------------
    # Simulation & Animation Synchronization
    # ------------------------------------------------------------------

    def _base_clip(self, state: PetState) -> str:
        mapping = {
            PetState.IDLE: "idle",
            PetState.SLEEPING: "sleep",
            PetState.PLAYING: "focus",
            PetState.HUNGRY: "hungry",
        }
        if self._did_drag and self._press_global_pos is not None:
            return "dangle"  # held in the air, whatever the state
        clip = mapping.get(state, "idle")
        if self._ai_thinking and clip == "idle":
            return activity_clip(self._ai_activity)
        return "sleep" if clip == "idle" and self._away else clip

    def _sync_animation_to_pet_state(self, state: PetState) -> None:
        self._anim.set_state(self._base_clip(state))

    def _handle_state_change(
        self, _old_state: PetState, new_state: PetState, _stats: PetStats
    ) -> None:
        self._walk_steps_remaining = 0
        self._sync_animation_to_pet_state(new_state)
        # No HUD pin here: RAM/CPU hovering near the 90% alert line flips state on its own,
        # which popped the card up out of nowhere; the clip change already shows the state
        self._schedule_next_anim_tick()
        self.update()

    def _handle_stats_change(self, _stats: PetStats) -> None:
        self._collect_forge_outputs()
        clip = self._base_clip(self._state_machine.state)
        if self._walk_steps_remaining == 0 and self._anim.base_state != clip:
            self._anim.set_state(clip)  # a song started or stopped
            self._schedule_next_anim_tick()
            self.update()
        notifications = self._state_machine.pop_notifications()
        if notifications:
            toast_text, color_hex = notifications[-1]
            # FOCUS DONE is ice-blue, but a finished focus session is the biggest win there is
            self._react(self.SUCCESS_HEX if toast_text == "FOCUS DONE" else color_hex or self.SUCCESS_HEX,
                        big=toast_text in self.BIG_WINS)
            self._show_toast(toast_text, QColor(color_hex or "#38B764"))
            if self._on_notification is not None and toast_text not in self.QUIET_NOTICES:
                self._on_notification(toast_text)
            return
        if self._is_hud_visible():
            self.update()

    def _schedule_next_anim_tick(self) -> None:
        duration = max(60, self._anim.current_frame_duration_ms())
        self._anim_timer.start(duration)
        self._anim_clock.start()

    def _on_animation_tick(self) -> None:
        # measured, capped so a stall (sleep, a busy Qt thread) skips at most a second of frames
        elapsed = min(1000, self._anim_clock.elapsed()) if self._anim_clock.isValid() else self._anim_timer.interval()
        changed = self._anim.advance(elapsed)
        self._schedule_next_anim_tick()
        if changed:
            self.update()

    def _on_ambient_tick(self) -> None:
        if self._press_global_pos is not None:
            return
        if self._open_wheels() or self._walk_steps_remaining > 0 or self._fall_to is not None:
            return  # a hop or blink must not move the pet out from under an open wheel, or interrupt a walk
        if self._state_machine.state != PetState.IDLE or self._away:
            return

        roll = random.random()
        if roll < 0.08 and self._anim.base_state == "idle":  # no yawning mid-song
            self._anim.play_oneshot("yawn")
            self._schedule_next_anim_tick()
            self.update()
        elif roll < 0.18:
            self._anim.play_oneshot("shine")  # a glint runs along the wire
            self._schedule_next_anim_tick()
            self.update()
        elif roll < 0.48:
            # Subtle blink
            self._anim.play_oneshot("blink")
            self._schedule_next_anim_tick()
            self.update()
        elif roll < 0.72 and self._auto_wander and not self._hovered:
            # Gentle short hop left or right
            direction = random.choice((-1, 1))
            self._walk_dx = direction * 4
            self._walk_steps_remaining = random.randint(6, 10)
            self._anim.facing_left = direction < 0
            self._anim.set_state("walk")
            self._schedule_next_anim_tick()
            if not self._overlay_timer.isActive():
                self._overlay_timer.start()
            self.update()

    def _on_overlay_tick(self) -> None:
        needs_repaint = False

        if self._hud_pinned_until_ticks > 0:
            self._hud_pinned_until_ticks -= 1
            if self._hud_pinned_until_ticks == 0:
                needs_repaint = True

        if self._toast_steps > 0:
            self._toast_steps -= 1
            needs_repaint = True

        if self._walk_steps_remaining > 0:
            self._walk_steps_remaining -= 1
            before = self.pos()
            if self._perch is not None:
                moved_on = self._follow_perch(self._walk_dx)  # a hop along the edge; False: a fall replaced it
            else:
                moved_on = True
                self.move_clamped(before.x() + self._walk_dx, before.y())
            if moved_on and (self.pos() == before or self._walk_steps_remaining == 0):
                self._walk_steps_remaining = 0
                self._sync_animation_to_pet_state(self._state_machine.state)  # a walk may run in any state
                self._schedule_next_anim_tick()
            needs_repaint = True

        if (
            self._hud_pinned_until_ticks <= 0
            and self._toast_steps <= 0
            and self._walk_steps_remaining <= 0
        ):
            self._overlay_timer.stop()

        if needs_repaint:
            self.update()

    # ------------------------------------------------------------------
    # Perching on other windows (src/ui/perch.py)
    # ------------------------------------------------------------------

    def _feet_local(self) -> tuple[int, int]:
        """(sprite centre x, feet y) in window coordinates."""
        r = self._sprite_rect
        return r.center().x(), r.top() + r.height() * 53 // 64  # lowest opaque row of the standing poses

    def _clamp_feet(self, x: int, y: int) -> tuple[int, int]:
        """clamp_position_to_screens, but the window may hang below the work area by the gap under the feet, so
        they touch the taskbar instead of hovering over it."""
        gap = self.height() - self._feet_local()[1]
        screens = [ScreenRect(s.x, s.y, s.width, s.height + gap) for s in self.available_screen_rects()]
        return clamp_position_to_screens(x, y, self.width(), self.height(), screens)

    @staticmethod
    def _qt_screens() -> list[tuple[perch.Rect, float]]:
        out = []
        for scr in QGuiApplication.screens():
            g = scr.geometry()
            out.append(((g.x(), g.y(), g.x() + g.width(), g.y() + g.height()), scr.devicePixelRatio()))
        return out

    def _try_perch(self) -> None:
        """On release: sit on the window (or taskbar) whose top edge is within reach of the feet."""
        if QGuiApplication.platformName() != "windows":
            return  # offscreen tests: never perch on the real desktop's windows
        hwnds, rects = self._perch_candidates()
        sx, fy = self._feet_local()
        i = perch.pick_perch(rects, self.x() + sx, self.y() + fy)
        if i is None:
            return
        self._perch = (hwnds[i], self.x() + sx - rects[i][0])
        self._perch_ticks = 0
        if self._follow_perch(fall=False):
            self._start_perch_timer()

    def _perch_candidates(self) -> tuple[list[int], list[perch.Rect]]:
        """Windows the duck could sit on, topmost first, in logical px."""
        screens = self._qt_screens()
        wins = perch.top_level_windows()
        return [h for h, _ in wins], [perch.to_logical(r, screens) for _, r in wins]

    def _check_perch_cover(self) -> None:
        """Another window came up over the spot the duck sits on (alt-tab, a click on another app): hop onto that
        window's top edge when it is right at the feet, else fall to the floor."""
        assert self._perch is not None
        hwnds, rects = self._perch_candidates()
        sx, fy = self._feet_local()
        fx = self.x() + sx
        index = hwnds.index(self._perch[0]) if self._perch[0] in hwnds else None
        if not perch.perch_covered(rects, index, fx):
            return
        j = perch.pick_perch(rects, fx, self.y() + fy)
        if j is not None:
            self._perch = (hwnds[j], fx - rects[j][0])
            self._follow_perch()  # falls itself when there is no room up there
            return
        self._perch = None
        self._start_fall()

    def _start_perch_timer(self) -> None:
        if not self._perch_timer.isActive():
            self._perch_clock.start()
            self._perch_timer.start()

    def _on_perch_tick(self) -> None:
        dt = min(0.1, self._perch_clock.restart() / 1000.0)  # measured; a stall never teleports the fall
        if self._fall_to is not None:
            self._fall_v += self.FALL_ACCEL * dt
            self._fall_y = min(float(self._fall_to), self._fall_y + self._fall_v * dt)
            self.move(self._fall_x, round(self._fall_y))
            if self.y() >= self._fall_to:
                self._land_fall()
        elif self._perch is not None:
            self._perch_ticks += 1
            if self._perch_ticks % self.PERCH_COVER_EVERY == 0:
                self._check_perch_cover()  # a full window scan (~0.7ms), so not every frame
            if self._perch is not None and self._walk_steps_remaining <= 0:
                self._follow_perch()  # a hop steps along the edge on the overlay tick instead
        if self._perch is None and self._fall_to is None:
            self._perch_timer.stop()

    def _follow_perch(self, dx: int = 0, *, fall: bool = True) -> bool:
        """Stand on the perch's top edge (dx: a wander step along it). False when it is gone, then fall
        (unless `fall` is off: a drop where there is no room to sit stays where it was dropped)."""
        assert self._perch is not None
        hwnd, offset = self._perch
        screens = self._qt_screens()
        phys = perch.window_rect(hwnd)
        rect = perch.to_logical(phys, screens) if phys is not None else None
        sx, fy = self._feet_local()
        lost = rect is None or perch.perch_lost(rect, [g for g, _ in screens], rect[0] + offset)
        if rect is not None and not lost:
            x, y = perch.perch_position(rect, rect[0] + offset + dx, sx, fy, self._sprite_rect.width() // 4)
            cx, cy = self._clamp_feet(x, y)
            lost = cy != y  # pushed off the edge (window dragged to the screen top, maximized)
        if rect is None or lost:
            self._perch = None
            if fall:
                self._start_fall()
            return False
        if (cx, cy) != (self.x(), self.y()):
            self.move(cx, cy)
        self._perch = (hwnd, cx + sx - rect[0])
        return True

    def _start_fall(self) -> None:
        """Flutter down (dangle clip, accelerating) to the work-area floor under the duck."""
        sx, fy = self._feet_local()
        scr = QGuiApplication.screenAt(QPoint(self.x() + sx, self.y() + fy)) or self.screen()
        floor = scr.availableGeometry().bottom() + 1
        self._fall_x, fall_to = self._clamp_feet(self.x(), floor - fy)
        self._fall_y, self._fall_v = float(self.y()), 0.0
        self._walk_steps_remaining = 0
        if fall_to <= self.y():
            self.move(self._fall_x, fall_to)
            self._land_fall()
            return
        self._fall_to = fall_to
        self._anim.set_state("dangle")
        self._schedule_next_anim_tick()
        self._start_perch_timer()

    def _land_fall(self) -> None:
        self._fall_to = None
        self._sync_animation_to_pet_state(self._state_machine.state)
        self._anim.play_oneshot("land")
        self._schedule_next_anim_tick()
        self.update()

    def _pin_hud(self, steps: int = 35) -> None:
        self._hud_pinned_until_ticks = max(self._hud_pinned_until_ticks, steps)
        if not self._overlay_timer.isActive():
            self._overlay_timer.start()
        self.update()

    def _show_toast(self, text: str, color: QColor) -> None:
        self._toast_text = text
        self._toast_color = color
        self._toast_steps = self._toast_max_steps
        # Toasts alone never open the HUD card (background notices would pop it); view switches pin it
        if not self._overlay_timer.isActive():
            self._overlay_timer.start()
        self.update()

    def _is_hud_visible(self) -> bool:
        if self._open_wheels() or self._hint is not None:
            return False  # the HUD would cover wheel slices or the hint card being clicked
        return self._hovered or self._hud_pinned_until_ticks > 0

    # ------------------------------------------------------------------
    # Mouse & Context Menu Events
    # ------------------------------------------------------------------

    def hit_test_point(self, local_pos: QPoint) -> bool:
        """Return True if local_pos hits opaque sprite pixels or an active HUD card."""
        if self._is_hud_visible() and self._hud_rect.contains(local_pos):
            return True
        if self._hint is not None and self._hint_rect.contains(local_pos):
            return True
        if not self._sprite_rect.contains(local_pos):
            return False

        # Rendered alpha decides; a 3x3 probe a few px apart forgives thin parts like the beak tip
        image = self._current_qimage()
        k = image.width() / max(1, self._sprite_rect.width())
        lx, ly = local_pos.x() - self._sprite_rect.left(), local_pos.y() - self._sprite_rect.top()
        for oy in (-3, 0, 3):
            for ox in (-3, 0, 3):
                ix, iy = int((lx + ox) * k), int((ly + oy) * k)
                if 0 <= ix < image.width() and 0 <= iy < image.height() and image.pixelColor(ix, iy).alpha() > 0:
                    return True
        return False

    def _is_interactive_hit(self, local_pos: QPoint) -> bool:
        return self.hit_test_point(local_pos)

    def enterEvent(self, event: QEnterEvent | None) -> None:
        self._hovered = True
        self.update()
        if event is not None:
            super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._hovered = False
        self.update()
        if event is not None:
            super().leaveEvent(event)

    def _prewarm_if_holding(self) -> None:
        if self._hold_timer.isActive():
            prewarm_ticks()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.MouseButton.LeftButton and self._wheel_release_timer.isActive():
            self._wheel_release_timer.stop()  # pressed again right away: the same hold goes on
            event.accept()
            return
        local_pos = event.position().toPoint()
        if not self._is_interactive_hit(local_pos):
            event.ignore()
            return
        if (self._hint is not None and event.button() == Qt.MouseButton.LeftButton
                and self._hint_rect.contains(local_pos)):
            self._run_hint()
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            global_pos = event.globalPosition().toPoint()
            self._press_global_pos = global_pos
            self._last_drag_global_pos = global_pos
            self._drag_offset = global_pos - self.frameGeometry().topLeft()
            self._did_drag = False
            self._perch = self._fall_to = None  # picked up
            self._wheel_holding = False  # a hold whose release never arrived (wheel watchdog closed it)
            self._walk_steps_remaining = 0
            self._hold_timer.start()
            # Resume audio during the hold delay, not as the tool wheel appears. Waits PREWARM_DELAY_MS and skips
            # drags (they stop the hold timer): the blocking ~175ms resume froze the start of every drag.
            QTimer.singleShot(self.PREWARM_DELAY_MS, self._prewarm_if_holding)  # bound: dropped if the pet is deleted
            event.accept()
            return

        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if (
            self._press_global_pos is not None
            and self._drag_offset is not None
            and (event.buttons() & Qt.MouseButton.LeftButton)
        ):
            global_pos = event.globalPosition().toPoint()
            if self._wheel_holding:
                if self._tool_wheel is not None:
                    self._tool_wheel.update_hover_from_global(global_pos)
                event.accept()
                return
            delta = global_pos - self._press_global_pos
            if not self._did_drag and delta.manhattanLength() >= self.DRAG_THRESHOLD_PX:
                self._did_drag = True
                self._hold_timer.stop()
                self._sync_animation_to_pet_state(self._state_machine.state)  # picks up "dangle"
                self._schedule_next_anim_tick()
            if self._did_drag:
                step_dx = (
                    (global_pos.x() - self._last_drag_global_pos.x())
                    if self._last_drag_global_pos is not None
                    else delta.x()
                )
                if step_dx != 0:
                    self._anim.facing_left = step_dx < 0
                self._last_drag_global_pos = global_pos
                candidate = global_pos - self._drag_offset
                self.move_clamped(candidate.x(), candidate.y())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if event.button() == Qt.MouseButton.LeftButton and self._press_global_pos is not None:
            self._hold_timer.stop()
            if self._wheel_holding:
                self._wheel_release_pos = event.globalPosition().toPoint()
                self._wheel_release_timer.start()
                event.accept()
                return
            was_drag = self._did_drag
            self._press_global_pos = None
            self._last_drag_global_pos = None
            self._drag_offset = None
            self._did_drag = False
            if not was_drag:
                self._click_timer.start()
            else:
                self._sync_animation_to_pet_state(self._state_machine.state)
                self._anim.play_oneshot("land")
                self._schedule_next_anim_tick()
                self._try_perch()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _finish_wheel_release(self) -> None:
        self._wheel_holding = False
        self._press_global_pos = None
        self._last_drag_global_pos = None
        self._drag_offset = None
        self._did_drag = False
        if self._tool_wheel is not None and self._tool_wheel.isVisible():
            self._tool_wheel.release_at(self._wheel_release_pos)

    def mouseDoubleClickEvent(self, event: QMouseEvent | None) -> None:
        if event is None:
            return
        if self._wheel_holding:  # the second press of a bounce inside a wheel hold, not a double-click
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._is_interactive_hit(
            event.position().toPoint()
        ):
            self._click_timer.stop()
            macro = self.macros.find(self.macros.double_click) if self.macros.double_click else None
            if macro is not None:
                self._macro_runner.run(macro)
            else:
                self.trigger_snip("image")
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def contextMenuEvent(self, event: QContextMenuEvent | None) -> None:
        if event is None:
            return
        if not self._is_interactive_hit(event.pos()):
            event.ignore()
            return
        if self._hint is not None and self._hint_rect.contains(event.pos()):
            self._dismiss_hint()
            event.accept()
            return
        self._open_context_menu(event.globalPos())
        event.accept()

    @staticmethod
    def _local_drop_files(mime) -> list[str]:
        if mime is None or not mime.hasUrls():
            return []
        # Raw strings, no exists(): per-file Path/stat work stalls big drops, and File Forge
        # converts and filters missing paths on its worker thread. Non-file URLs give "".
        return [f for u in mime.urls() if (f := u.toLocalFile())]

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        if event is None:
            return
        if self._drop_wheel is not None and self._drop_wheel.isVisible():
            # Cursor slid back off the wheel mid-drag: keep its paths and open animation as they are
            event.acceptProposedAction()
            return
        mime = event.mimeData()
        paths = self._drag_paths = self._local_drop_files(mime)
        self._pending_web_source = None if paths else self._web_drop_source(mime)
        if paths or self._pending_web_source is not None:
            forge = self._state_machine.forge_plugin
            if forge is not None and forge.busy:
                self._pending_web_source = None
                self._show_toast("BUSY!", QColor("#EF7D57"))
                event.ignore()
                return
            self._hovered = True
            self._walk_steps_remaining = 0
            if paths:
                self._spawn_drop_wheel(paths, event.position().toPoint())
            else:
                source = self._pending_web_source
                stem = web_image_stem(source) if isinstance(source, str) else "web_image"
                # Re-encoding slices only: archiving would pass the downloaded image through untouched
                options = [
                    o for o in get_wheel_options_for_paths([Path("web.png")])
                    if o.mode not in (ForgeMode.ZIP, ForgeMode.TAR_XZ)
                ]
                self._spawn_drop_wheel([Path(f"{stem}.png")], event.position().toPoint(), options)
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent | None) -> None:
        if event is None:
            return
        if self._pending_web_source is not None or self._drag_paths:
            if self._drop_wheel is not None and self._drop_wheel.isVisible():
                self._drop_wheel.update_hover_from_global(
                    self.mapToGlobal(event.position().toPoint())
                )
            event.acceptProposedAction()
            return
        event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent | None) -> None:
        self._hovered = False
        self.update()
        if event is not None:
            super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent | None) -> None:
        if event is None:
            return
        # Same drag as the enter: reuse its paths instead of re-reading thousands of URLs
        paths, self._drag_paths = self._drag_paths or self._local_drop_files(event.mimeData()), []
        if self._drop_wheel is not None and self._drop_wheel.isVisible():
            chosen = self._drop_wheel.update_hover_from_global(
                self.mapToGlobal(event.position().toPoint())
            )
            self._drop_wheel.dismiss()
            if chosen is not None and (paths or self._pending_web_source is not None):
                self._dispatch_drop(paths, chosen.mode)
            else:
                self._on_wheel_cancelled()
            event.acceptProposedAction()
            return
        if paths or self._pending_web_source is not None:
            self._dispatch_drop(paths, None)
            event.acceptProposedAction()
            return
        event.ignore()

    def build_context_menu(self) -> PetContextMenu:
        return PetContextMenu(
            state_machine=self._state_machine,
            always_on_top=self._always_on_top,
            auto_wander=self._auto_wander,
            on_feed=self.trigger_feed,
            on_play=self.trigger_play,
            on_pet=self.trigger_pet,
            on_select_tool=self.trigger_select_tool,
            on_tool_action=self.trigger_tool_action,
            on_toggle_sleep=self.trigger_toggle_sleep,
            on_reset_position=self.reset_to_default_position,
            on_toggle_on_top=self.set_always_on_top,
            on_toggle_wander=self.set_auto_wander,
            on_quit=self.trigger_quit,
            recent_files=self.recent.entries(),
            wheel_slices=[(o.label, o.callback_id, on) for o, on in self.wheel.catalog(self.all_tool_wheel_options())],
            wheel_custom=[label for label, _target in self.wheel.custom],
            macros=[m.name for m in self.macros.macros],
            double_click_macro=self.macros.double_click,
            pet_scale=self.pet_scale,
            hotkeys=[(label, self.hotkeys.get(mode) or default_hotkey(mode), mode)
                     for mode, label in HOTKEY_ACTIONS.items()],
            pet_scales=self.PET_SCALES,
            theme=saved_theme(),
            parent=self,
        )

    def _open_context_menu(self, global_pos: QPoint) -> None:
        menu = self.build_context_menu()
        try:
            menu.exec(global_pos)
        finally:
            menu.deleteLater()

    # ------------------------------------------------------------------
    # Lifecycle & Cleanup
    # ------------------------------------------------------------------

    def has_active_timers(self) -> bool:
        return (
            self._anim_timer.isActive()
            or self._ambient_timer.isActive()
            or self._overlay_timer.isActive()
            or self._perch_timer.isActive()
        )

    def shutdown(self) -> None:
        """Stop all internal timers and detach state listeners cleanly."""
        self._is_shutting_down = True
        self._anim_timer.stop()
        self._ambient_timer.stop()
        self._topmost_timer.stop()
        self._overlay_timer.stop()
        self._perch_timer.stop()
        self._hint_timer.stop()
        if self._ai_chat is not None:
            self._ai_chat.close()
            self._ai_chat = None
        if self._snip_session is not None:
            self._snip_session.close()
            self._snip_session = None
        if self._drop_wheel is not None:
            self._drop_wheel.dismiss()
            self._drop_wheel.close()
            self._drop_wheel.deleteLater()
            self._drop_wheel = None
        self._hold_timer.stop()
        if self._tool_wheel is not None:
            self._tool_wheel.dismiss()
            self._tool_wheel.close()
            self._tool_wheel.deleteLater()
            self._tool_wheel = None
        if self._unsub_state is not None:
            self._unsub_state()
            self._unsub_state = None  # type: ignore[assignment]
        if self._unsub_stats is not None:
            self._unsub_stats()
            self._unsub_stats = None  # type: ignore[assignment]
        self._state_machine.shutdown()

    def closeEvent(self, event: QCloseEvent | None) -> None:
        if not self._is_shutting_down:
            self.trigger_quit()
        else:
            self.shutdown()
        if event is not None:
            super().closeEvent(event)
