"""Unit tests for multi-monitor geometry clamping, PetWindow interactions, and ContextMenu."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QImage, QMouseEvent
from PyQt6.QtWidgets import QApplication

from src.engine.models import PetState, PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.ui.context_menu import PetContextMenu
from src.ui.pet_window import (
    PetWindow,
    ScreenRect,
    clamp_position_to_screens,
    export_windows_ico,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


class TestScreenGeometryClamping:
    def test_clamps_within_single_monitor_bounds(self) -> None:
        screens = [ScreenRect(0, 0, 1920, 1040)]
        assert clamp_position_to_screens(-100, -50, 168, 204, screens) == (0, 0)
        assert clamp_position_to_screens(2500, 1400, 168, 204, screens) == (
            1920 - 168,
            1040 - 204,
        )
        assert clamp_position_to_screens(400, 300, 168, 204, screens) == (400, 300)

    def test_supports_negative_coordinate_secondary_monitor(self) -> None:
        screens = [
            ScreenRect(-1920, 0, 1920, 1080),  # Left monitor
            ScreenRect(0, 0, 1920, 1040),      # Primary right monitor
        ]
        # Valid position on left monitor stays on left monitor
        assert clamp_position_to_screens(-1200, 250, 168, 204, screens) == (-1200, 250)
        # Dragged past far-left edge clamps to left monitor's left edge (-1920)
        assert clamp_position_to_screens(-2400, 250, 168, 204, screens) == (-1920, 250)
        # Dragged past far-right edge clamps to right monitor's right edge
        assert clamp_position_to_screens(2400, 250, 168, 204, screens) == (1920 - 168, 250)

    def test_empty_screens_returns_input_unchanged(self) -> None:
        assert clamp_position_to_screens(123, 456, 168, 204, []) == (123, 456)

    def test_straddling_seam_between_adjacent_monitors_does_not_jump(self) -> None:
        screens = [
            ScreenRect(-1920, 0, 1920, 1080),
            ScreenRect(0, 0, 1920, 1040),
        ]
        # Window straddling x=0 seam (-80..96) is fully visible across both monitors
        assert clamp_position_to_screens(-80, 300, 176, 222, screens) == (-80, 300)
        # Straddling seam while dragged above top edge clamps y to 0 without snapping x
        assert clamp_position_to_screens(-80, -40, 176, 222, screens) == (-80, 0)


class TestPetWindowAndContextMenu:
    def test_window_flags_and_translucent_attributes(self, qapp: QApplication) -> None:
        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(state_machine=sm, animation_controller=anim, always_on_top=True)
        try:
            flags = win.windowFlags()
            assert bool(flags & Qt.WindowType.FramelessWindowHint)
            assert bool(flags & Qt.WindowType.WindowStaysOnTopHint)
            assert win.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

            win.set_always_on_top(False)
            assert not bool(win.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)
        finally:
            win.shutdown()
            win.close()

    def test_mouse_drag_moves_window_within_clamped_bounds(self, qapp: QApplication) -> None:
        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(
            state_machine=sm,
            animation_controller=anim,
            auto_wander=False,
        )
        try:
            win.move(200, 200)
            win.show()
            qapp.processEvents()

            # Click on center of the sprite area
            local_pt = win.sprite_center_point()
            global_start = win.mapToGlobal(local_pt)

            press_event = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(local_pt),
                QPointF(global_start),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mousePressEvent(press_event)

            global_target = global_start + QPoint(80, 60)
            move_event = QMouseEvent(
                QEvent.Type.MouseMove,
                QPointF(local_pt),
                QPointF(global_target),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mouseMoveEvent(move_event)

            release_event = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(local_pt),
                QPointF(global_target),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mouseReleaseEvent(release_event)

            # Position moved from (200, 200) toward (280, 260) within screen bounds
            assert win.pos() != QPoint(200, 200)
        finally:
            win.shutdown()
            win.close()

    def test_click_without_drag_triggers_pet_interaction(self, qapp: QApplication) -> None:
        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(state_machine=sm, animation_controller=anim, auto_wander=False)
        try:
            assert sm.active_tool_id == "sysmon"
            local_pt = win.sprite_center_point()
            global_pt = win.mapToGlobal(local_pt)

            press = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(local_pt),
                QPointF(global_pt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(local_pt),
                QPointF(global_pt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mousePressEvent(press)
            win.mouseReleaseEvent(release)

            # Single click is deferred so it cannot pair with a double-click
            assert win._click_timer.isActive()
            assert sm.active_tool_id == "sysmon"
            win._click_timer.timeout.emit()
            assert sm.active_tool_id == "pomodoro"

            snips: list[str] = []
            win.trigger_snip = snips.append  # type: ignore[method-assign]
            dbl = QMouseEvent(
                QEvent.Type.MouseButtonDblClick,
                QPointF(local_pt),
                QPointF(global_pt),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.mousePressEvent(press)
            win.mouseReleaseEvent(release)
            win.mouseDoubleClickEvent(dbl)
            assert snips == ["image"]
            assert not win._click_timer.isActive()
        finally:
            win.shutdown()
            win.close()

    def test_context_menu_actions_trigger_state_changes(self, qapp: QApplication) -> None:
        sm = PetStateMachine(PetStats())
        quit_called: list[bool] = []
        reset_called: list[bool] = []

        menu = PetContextMenu(
            state_machine=sm,
            always_on_top=True,
            auto_wander=True,
            on_feed=sm.refresh_metrics,
            on_play=sm.toggle_pomodoro,
            on_toggle_sleep=lambda: sm.wake() if sm.state == PetState.SLEEPING else sm.rest(),
            on_reset_position=lambda: reset_called.append(True),
            on_toggle_on_top=lambda _val: None,
            on_toggle_wander=lambda _val: None,
            on_quit=lambda: quit_called.append(True),
        )

        def all_actions(m):
            for a in m.actions():
                yield a
                if a.menu() is not None:
                    yield from all_actions(a.menu())

        action_texts = [a.text() for a in all_actions(menu) if not a.isSeparator()]
        assert any("Refresh System Metrics" in t for t in action_texts)
        assert any("Pomodoro" in t for t in action_texts)
        assert any("Do Not Disturb" in t for t in action_texts)
        assert any("Reset Position" in t for t in action_texts)
        assert any("Quit" in t for t in action_texts)

        menu.play_action.trigger()
        assert sm.state == PetState.PLAYING

        menu.sleep_action.trigger()
        assert sm.state == PetState.SLEEPING

        menu.reset_position_action.trigger()
        assert reset_called == [True]

        menu.quit_action.trigger()
        assert quit_called == [True]

    def test_paint_dispatch_renders_crisp_sprite_and_transparent_corners(
        self, qapp: QApplication
    ) -> None:
        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(state_machine=sm, animation_controller=anim, auto_wander=False)
        try:
            canvas = QImage(win.width(), win.height(), QImage.Format.Format_ARGB32_Premultiplied)
            for _ in range(15):
                anim.advance(160)
                canvas.fill(0)
                win.render(canvas)

            # Top-left corner must remain fully transparent when HUD is hidden
            assert canvas.pixelColor(0, 0).alpha() == 0
            # Center of sprite region must have visible (frosted, translucent) pixel art rendered
            sprite_center = win.sprite_center_point()
            assert canvas.pixelColor(sprite_center.x(), sprite_center.y()).alpha() >= 100
        finally:
            win.shutdown()
            assert not win.has_active_timers()
            win.close()

    def test_transparent_region_returns_httransparent_and_ignores_hit(
        self, qapp: QApplication
    ) -> None:
        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(state_machine=sm, animation_controller=anim, auto_wander=False)
        try:
            # Upper transparent HUD zone when not hovered must pass through
            assert win.hit_test_point(QPoint(5, 5)) is False
            # Sprite body center must hit
            assert win.hit_test_point(win.sprite_center_point()) is True
        finally:
            win.shutdown()
            win.close()

    def test_right_click_opens_context_menu_only_once(self, qapp: QApplication) -> None:
        from PyQt6.QtGui import QContextMenuEvent

        sm = PetStateMachine(PetStats())
        anim = AnimationController(size=32)
        win = PetWindow(state_machine=sm, animation_controller=anim, auto_wander=False)
        invocations: list[QPoint] = []
        win._open_context_menu = lambda pos: invocations.append(pos)  # type: ignore[method-assign]
        try:
            local_pt = win.sprite_center_point()
            global_pt = win.mapToGlobal(local_pt)

            press = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                QPointF(local_pt),
                QPointF(global_pt),
                Qt.MouseButton.RightButton,
                Qt.MouseButton.RightButton,
                Qt.KeyboardModifier.NoModifier,
            )
            release = QMouseEvent(
                QEvent.Type.MouseButtonRelease,
                QPointF(local_pt),
                QPointF(global_pt),
                Qt.MouseButton.RightButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            )
            ctx = QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                local_pt,
                global_pt,
            )

            win.mousePressEvent(press)
            win.mouseReleaseEvent(release)
            win.contextMenuEvent(ctx)

            assert len(invocations) == 1
        finally:
            win.shutdown()
            win.close()

    def test_export_windows_ico_writes_valid_multires_ico_header(
        self, qapp: QApplication, tmp_path
    ) -> None:
        ico_file = export_windows_ico(tmp_path / "paperduck.ico")
        assert ico_file.exists()
        raw = ico_file.read_bytes()
        # ICO magic header: 00 00 01 00 + 6 images (06 00)
        assert raw[:6] == b"\x00\x00\x01\x00\x06\x00"
        assert len(raw) > 512


def _mouse(kind: QEvent.Type, win: PetWindow, global_pt: QPoint, buttons: Qt.MouseButton) -> QMouseEvent:
    return QMouseEvent(
        kind,
        QPointF(win.mapFromGlobal(global_pt)),
        QPointF(global_pt),
        Qt.MouseButton.LeftButton,
        buttons,
        Qt.KeyboardModifier.NoModifier,
    )


def test_long_press_wheel_hold_drag_release_runs_slice(qapp: QApplication) -> None:
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    fired: list[str] = []
    win.trigger_tool_action = fired.append  # type: ignore[method-assign]
    try:
        start = win.mapToGlobal(win.sprite_center_point())
        win.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, win, start, Qt.MouseButton.LeftButton))
        win._on_hold_timeout()  # stands in for the 450ms hold
        wheel = win.tool_wheel
        assert wheel is not None and wheel.isVisible()

        labels = [o.label for o in wheel.options]
        target_idx = labels.index("OCR")
        target = wheel.mapToGlobal(wheel.slice_center_point(target_idx))
        win.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, win, target, Qt.MouseButton.LeftButton))
        assert wheel.hovered_index == target_idx
        win.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, win, target, Qt.MouseButton.NoButton))
        assert fired == [] and win._wheel_release_timer.isActive()  # waits out a possible bounce
        win._wheel_release_timer.stop()
        win._finish_wheel_release()
        assert fired == ["snip:text"]
        assert not wheel.isVisible()
        assert not win._click_timer.isActive()  # a hold never also cycles the HUD
    finally:
        win.shutdown()
        win.close()


def test_long_press_wheel_release_on_hub_closes(qapp: QApplication) -> None:
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    fired: list[str] = []
    win.trigger_tool_action = fired.append  # type: ignore[method-assign]
    try:
        start = win.mapToGlobal(win.sprite_center_point())
        win.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, win, start, Qt.MouseButton.LeftButton))
        win._on_hold_timeout()
        wheel = win.tool_wheel
        assert wheel is not None and wheel.isVisible()
        hub = wheel.mapToGlobal(wheel.hub_center_point())
        win.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, win, hub, Qt.MouseButton.LeftButton))
        win._wheel_release_timer.stop()
        win._finish_wheel_release()
        assert not wheel.isVisible() and fired == []
    finally:
        win.shutdown()
        win.close()


def test_a_quick_repress_during_a_wheel_hold_keeps_one_wheel_and_no_double_click(qapp: QApplication) -> None:
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    fired: list[str] = []
    win.trigger_tool_action = fired.append  # type: ignore[method-assign]
    win.trigger_snip = fired.append  # type: ignore[method-assign]  # the default double-click action
    try:
        start = win.mapToGlobal(win.sprite_center_point())
        win.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, win, start, Qt.MouseButton.LeftButton))
        win._on_hold_timeout()
        wheel = win.tool_wheel
        win.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, win, start, Qt.MouseButton.NoButton))
        win.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, win, start, Qt.MouseButton.LeftButton))  # bounce
        win.mouseDoubleClickEvent(_mouse(QEvent.Type.MouseButtonDblClick, win, start, Qt.MouseButton.LeftButton))
        assert wheel.isVisible() and not win._wheel_release_timer.isActive() and not win._hold_timer.isActive()
        assert win._wheel_holding and fired == []
        target = wheel.mapToGlobal(wheel.slice_center_point([o.label for o in wheel.options].index("OCR")))
        win.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, win, target, Qt.MouseButton.LeftButton))
        win.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, win, target, Qt.MouseButton.NoButton))
        win._wheel_release_timer.stop()
        win._finish_wheel_release()
        assert fired == ["snip:text"] and not wheel.isVisible()
    finally:
        win.shutdown()
        win.close()


def test_wheel_open_animation_staggers_and_paints(qapp: QApplication) -> None:
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    try:
        wheel = win.open_tool_wheel()
        canvas = QImage(wheel.width(), wheel.height(), QImage.Format.Format_ARGB32_Premultiplied)
        for t in (0.0, 0.3, 1.0):
            wheel._open_t = t
            canvas.fill(0)
            wheel.render(canvas)
        wheel._open_t = 0.3
        last = len(wheel.options) - 1
        assert wheel._slice_opacity(0) > wheel._slice_opacity(last)
        wheel._open_t = 1.0
        assert wheel._slice_opacity(last) == 1.0
        center = wheel.hub_center_point()
        assert canvas.pixelColor(center.x(), center.y() + 30).alpha() > 0
    finally:
        win.shutdown()
        win.close()


def test_tool_wheel_slices_and_web_search_dispatch(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtGui import QDesktopServices, QGuiApplication

    from src.ui.tool_wheel import google_search_url

    opened: list[str] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    try:
        labels = [o.label for o in win.tool_wheel_options()]
        assert labels[:4] == ["OCR", "Clips", "Search", "Focus"]
        assert labels[-4:] == ["YouTube", "Gemini", "GitHub", "Snap Ask"]  # macros fill free room last
        assert len(labels) <= 10 and "Ask AI" in labels

        win.trigger_tool_action("web:https://www.youtube.com")
        QGuiApplication.clipboard().setText("TypeError: x is not a function")
        win.trigger_tool_action("pet:search")
        assert opened[0] == "https://www.youtube.com"
        assert opened[1].startswith("https://www.google.com/search?q=TypeError")
        assert google_search_url("a b&c") == "https://www.google.com/search?q=a+b%26c"

        sm.registry.get_plugin("clipvault")._ring.clear()
        win.trigger_tool_action("pet:clips")
        assert win._toast_text == "NO CLIPS"
    finally:
        win.shutdown()
        win.close()


def test_topmost_yields_to_held_buttons_and_foreign_topmost_windows() -> None:
    from src.ui.pet_window import yield_topmost

    class User32:
        def __init__(self, buttons=(), fg=10, under=20, topmost=()):
            self.buttons, self.fg, self.under, self.topmost = set(buttons), fg, under, set(topmost)

        def GetAsyncKeyState(self, vk):
            return 0x8000 if vk in self.buttons else 0

        def GetCursorPos(self, _pt):
            return 1

        def WindowFromPoint(self, _pt):
            return self.under + 1  # a child; GetAncestor gives its top-level window

        def GetAncestor(self, hwnd, _flag):
            return hwnd - 1

        def GetForegroundWindow(self):
            return self.fg

        def GetWindowLongW(self, hwnd, _index):
            return 0x8 if hwnd in self.topmost else 0

    assert not yield_topmost({99}, User32())  # a normal app in front: keep the pet on top
    assert yield_topmost({99}, User32(buttons={0x01}))  # dragging a window toward a snap zone
    assert yield_topmost({99}, User32(buttons={0x04}))
    assert yield_topmost({99}, User32(topmost={10}))  # Snap Assist / Alt+Tab / Start has focus
    assert yield_topmost({99}, User32(topmost={20}))  # a topmost flyout under the cursor
    assert not yield_topmost({10, 20}, User32(topmost={10, 20}))  # our own chat or wheel never blocks it


def test_away_naps_and_wakes_with_a_yawn(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    import src.tools.system_monitor as system_monitor
    import src.ui.pet_watch as pw

    # real CPU or RAM over 90% (a busy suite) would make it hungry from the first sample, taken while it is built
    monkeypatch.setattr(system_monitor, "get_system_metrics", lambda: system_monitor.SystemMetrics(5.0, 40.0, 50.0))
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    try:
        monkeypatch.setattr(sm, "refresh_metrics", lambda: False)
        monkeypatch.setattr(pw, "idle_seconds", lambda: win.AWAY_AFTER_S + 1)
        monkeypatch.setattr(pw, "display_kept_on", lambda: True)  # a video is playing
        win._check_away()
        assert win._anim.base_state == "idle"

        monkeypatch.setattr(pw, "display_kept_on", lambda: False)
        win._check_away()
        assert win._anim.base_state == "sleep"
        assert sm.state == PetState.IDLE  # a nap is visual only, never DND

        monkeypatch.setattr(pw, "idle_seconds", lambda: 0.5)
        win._check_away()
        assert win._anim.base_state == "idle"
        assert win._anim.active_clip_name == "yawn"
    finally:
        win.shutdown()
        win.close()


def test_drag_dangles_then_lands(qapp: QApplication) -> None:
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    try:
        start = win.mapToGlobal(win.sprite_center_point())
        win.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, win, start, Qt.MouseButton.LeftButton))
        win.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, win, start + QPoint(40, 0), Qt.MouseButton.LeftButton))
        assert win._anim.base_state == "dangle"
        win.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, win, start + QPoint(40, 0), Qt.MouseButton.NoButton))
        # Back on the state's own clip (idle, or hungry if a busy test run tipped CPU over 90%), not "dangle"
        assert win._anim.base_state == win._base_clip(sm.state) != "dangle"
        assert win._anim.active_clip_name == "land"
        assert not win._click_timer.isActive()  # a drag never also cycles the HUD
    finally:
        win.shutdown()
        win.close()


def test_first_run_tips_show_in_order_and_right_click_stops_them(qapp: QApplication) -> None:
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    try:
        win._tip_due = 0.0
        win._poll_hint()
        assert win._hint is not None and win._hint.text == win.TIPS[0][0] and win.tips_seen == 1
        win._run_hint()  # a click only closes a tip
        assert win._hint is None
        win._tip_due = 0.0
        win._poll_hint()
        assert win._hint is not None and win._hint.text == win.TIPS[1][0]
        win._dismiss_hint()  # right-click: no more tips
        assert win.tips_seen == len(win.TIPS)
        win._tip_due = 0.0
        win._poll_hint()
        assert win._hint is None or win._hint.rule != "tip"
    finally:
        win.shutdown()
        win.close()


def test_tips_are_skipped_for_installs_from_before_tips(qapp: QApplication, tmp_path) -> None:
    from src.main import PaperduckApp
    from src.storage.storage_manager import StorageManager

    old = tmp_path / "old.json"
    StorageManager(old).save(stats=PetStats(), state=PetState.IDLE, position=(100, 100), always_on_top=False,
                             auto_wander=False, active_tool="sysmon", tools_data={"macros": {}})
    for path, seen in ((old, len(PetWindow.TIPS)), (tmp_path / "new.json", 0)):
        app = PaperduckApp(app=qapp, storage=StorageManager(path), enable_tray=False)
        try:
            assert app.window.tips_seen == seen
        finally:
            app.shutdown()


def test_pet_size_keeps_feet_in_place_and_persists(qapp: QApplication, tmp_path) -> None:
    from src.main import PaperduckApp
    from src.storage.storage_manager import StorageManager

    path = tmp_path / "s.json"
    app = PaperduckApp(app=qapp, storage=StorageManager(path), enable_tray=False)
    try:
        win = app.window
        win.move_clamped(300, 300)
        feet = win.mapToGlobal(win._sprite_rect.bottomLeft()).y()
        win.trigger_tool_action("size:2")
        assert win.pet_scale == 2 and win._sprite_rect.width() == 64
        assert win.height() == PetWindow.WINDOW_HEIGHT - 64
        assert win.mapToGlobal(win._sprite_rect.bottomLeft()).y() == feet
        on_body = win._sprite_rect.topLeft() + QPoint(70 // 2, 84 // 2)  # duck hull, 128-unit box at half size
        assert win.hit_test_point(on_body) and not win.hit_test_point(win._sprite_rect.topLeft())
        win.trigger_tool_action("size:9")  # unknown sizes are ignored
        assert win.pet_scale == 2
        menu = win.build_context_menu()
        assert [a.text() for a in menu.size_menu.actions() if a.isChecked()] == ["Small"]
        menu.deleteLater()
        app.save_state()
    finally:
        app.shutdown()
    app = PaperduckApp(app=qapp, storage=StorageManager(path), enable_tray=False)
    try:
        assert app.window.pet_scale == 2
    finally:
        app.shutdown()


def test_notices_react_by_colour_and_quiet_ones_skip_the_tray(qapp: QApplication) -> None:
    sm = PetStateMachine(PetStats())
    shown: list[str] = []
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False,
                    on_notification=shown.append)
    try:
        for color, clip in (("#38B764", "cheer"), ("#EF7D57", "flinch"), ("#E05A47", "flinch")):
            win._react(color)
            assert win._anim.active_clip_name == clip, color
        sm.pop_notifications = lambda: [("PUSHED", "#38B764")]  # type: ignore[method-assign]
        win._handle_stats_change(sm.stats)
        assert win._anim.active_clip_name == "cheer" and shown == []  # toast and cheer, no tray balloon

        win._on_ai_thinking(True)
        win._on_ai_thinking(False)
        win._anim.play_oneshot("blink")
        win._on_ai_answered(True)  # a quick chat answer: no fuss
        assert win._anim.active_clip_name == "blink"
        win._ai_since -= win.LONG_TASK_S
        win._on_ai_answered(True)
        assert win._anim.active_clip_name == "cheer"
        win._on_ai_answered(False)
        assert win._anim.active_clip_name == "flinch"
    finally:
        win.shutdown()
        win.close()


def test_press_then_delete_leaves_no_pending_call_on_a_dead_pet(qapp: QApplication, monkeypatch) -> None:
    """The audio prewarm after a press was a lambda on the pet's hold timer: deleting the pet within
    PREWARM_DELAY_MS made it raise on a deleted QTimer, which PyQt turns into an abort (CI exit at shutdown)."""
    import sys
    import time

    errors: list[BaseException] = []
    monkeypatch.setattr(sys, "excepthook", lambda kind, value, tb: errors.append(value))
    win = PetWindow(state_machine=PetStateMachine(PetStats()), animation_controller=AnimationController(size=32),
                    auto_wander=False)
    local_pt = win.sprite_center_point()
    win.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(local_pt), QPointF(win.mapToGlobal(local_pt)),
                                    Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    win.shutdown()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    end = time.monotonic() + (PetWindow.PREWARM_DELAY_MS + 150) / 1000
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.01)
    assert errors == []
