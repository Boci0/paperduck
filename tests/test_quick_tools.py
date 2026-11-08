"""Tests for screen snip overlays, the Windows OCR bridge, and global snip hotkeys."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, QRect
from PyQt6.QtGui import QColor, QImage, QPixmap
from PyQt6.QtWidgets import QApplication

from src.main import PaperduckApp
from src.storage.storage_manager import StorageManager
from src.ui.context_menu import QUICK_TOOL_ITEMS
from src.ui.quick_tools import (
    GlobalHotkeys,
    SnipOverlay,
    ocr_image_file,
)


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app  # type: ignore[return-value]


def test_snip_overlay_crops_in_device_pixels_and_reads_colors(qapp: QApplication) -> None:
    img = QImage(200, 100, QImage.Format.Format_RGB32)
    img.fill(QColor("#102030"))
    img.setPixelColor(150, 50, QColor("#ABCDEF"))
    shot = QPixmap.fromImage(img)
    shot.setDevicePixelRatio(2.0)  # 100x50 logical, like a 200% scaled monitor
    results: list[object] = []
    overlay = SnipOverlay(shot, QRect(0, 0, 100, 50), "image", lambda _m, r: results.append(r))
    try:
        crop = overlay.crop(QRect(10, 10, 20, 15))
        assert (crop.width(), crop.height()) == (40, 30)
        assert overlay.pixel_color(QPoint(75, 25)).name().upper() == "#ABCDEF"
    finally:
        overlay.deleteLater()


def test_context_menu_exposes_quick_tools(qapp: QApplication, tmp_path: Path) -> None:
    controller = PaperduckApp(
        app=qapp, storage=StorageManager(tmp_path / "s.json"), enable_tray=False
    )
    try:
        menu = controller.window.build_context_menu()
        for _label, cid in QUICK_TOOL_ITEMS:
            assert cid in menu.plugin_actions
        menu.deleteLater()
    finally:
        controller.shutdown()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows OCR only")
def test_windows_ocr_reads_rendered_text(tmp_path: Path) -> None:
    # Render in a child process: the offscreen QPA used by other test modules has no fonts
    png = tmp_path / "ocr.png"
    script = "; ".join([
        "import sys",
        "from PyQt6.QtGui import QColor, QFont, QImage, QPainter",
        "from PyQt6.QtWidgets import QApplication",
        "app = QApplication([])",
        "img = QImage(520, 120, QImage.Format.Format_RGB32)",
        "img.fill(QColor('white'))",
        "p = QPainter(img)",
        "p.setFont(QFont('Arial', 36))",
        "p.setPen(QColor('black'))",
        "p.drawText(20, 80, 'Hello Sprite')",
        "p.end()",
        "img.save(sys.argv[1], 'PNG')",
    ])
    env = {k: v for k, v in os.environ.items() if k != "QT_QPA_PLATFORM"}
    subprocess.run([sys.executable, "-c", script, str(png)], check=True, env=env, timeout=60)
    assert "Hello" in ocr_image_file(png)


def test_snip_save_falls_back_when_pictures_dir_refuses_writes(
    qapp: QApplication, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.ui import pet_tools as pet_window  # _save_snip_png lives on ToolsMixin

    blocker = tmp_path / "pictures_is_a_file"
    blocker.write_text("x")  # mkdir under a file raises OSError, like a refused OneDrive dir
    monkeypatch.setattr(pet_window.QStandardPaths, "writableLocation", lambda _loc: str(blocker))
    monkeypatch.setattr(pet_window.Path, "home", lambda: tmp_path / "home")
    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(QColor("#102030"))
    saved = pet_window.ToolsMixin._save_snip_png(img)
    assert saved is not None and saved.parent == tmp_path / "home" / ".paperduck" / "snips"
    assert saved.exists()


def test_submenu_titles_render_literal_ampersand(qapp: QApplication, tmp_path: Path) -> None:
    controller = PaperduckApp(
        app=qapp, storage=StorageManager(tmp_path / "s.json"), enable_tray=False
    )
    try:
        menu = controller.window.build_context_menu()
        titles = [sub.title() for sub in menu.plugin_submenus.values()] + [menu.quick_menu.title()]
        # "&&" is how Qt shows a literal "&"; a lone "&" would be swallowed as a mnemonic
        assert "Clip Vault && Quick Transforms" in titles
        assert all("&" not in t.replace("&&", "") for t in titles)
        menu.deleteLater()
    finally:
        controller.shutdown()


def test_closing_a_dialog_does_not_quit_the_app(qapp: QApplication, tmp_path: Path) -> None:
    # Pet windows are Qt.Tool, so Qt would treat a closed file dialog as the last window
    controller = PaperduckApp(
        app=qapp, storage=StorageManager(tmp_path / "s.json"), enable_tray=False
    )
    try:
        assert qapp.quitOnLastWindowClosed() is False
    finally:
        controller.shutdown()


def test_color_loupe_paints_without_error(qapp: QApplication) -> None:
    img = QImage(200, 100, QImage.Format.Format_RGB32)
    img.fill(QColor("#102030"))
    overlay = SnipOverlay(QPixmap.fromImage(img), QRect(0, 0, 200, 100), "color", lambda _m, _r: None)
    try:
        for hover in (QPoint(5, 5), QPoint(195, 95)):  # loupe flips near the far edges
            overlay._hover = hover
            overlay.grab()
    finally:
        overlay.deleteLater()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 hotkeys")
def test_global_hotkeys_start_and_close_cleanly(qapp: QApplication) -> None:
    hotkeys = GlobalHotkeys()
    hotkeys.start()
    assert hotkeys._thread_id != 0
    hotkeys.close()
    time.sleep(0.2)  # let the worker leave its loop and unregister
    # Combos are released, so a second instance can register them again
    again = GlobalHotkeys()
    again.start()
    assert set(again.registered) >= set(hotkeys.registered)
    again.close()


def test_parse_hotkey_reads_combos_and_refuses_unsafe_ones() -> None:
    from src.ui.quick_tools import default_hotkey, hotkey_label, parse_hotkey

    for text in ("Ctrl+Alt+K", "Ctrl+Shift+Alt+S", "Win+F12", "Alt+`", "ctrl + alt + 7"):
        assert parse_hotkey(hotkey_label(*parse_hotkey(text))) == parse_hotkey(text)
    assert hotkey_label(*parse_hotkey("alt+ctrl+k")) == "Ctrl+Alt+K"
    assert default_hotkey("image") == "Ctrl+Shift+Alt+S"
    for bad in ("", "K", "Shift+K", "Ctrl+Hyper+K", "Ctrl+Alt+Enter", "Ctrl+Alt+F25", "Ctrl+Alt+é"):
        with pytest.raises(ValueError):
            parse_hotkey(bad)


@pytest.mark.skipif(sys.platform != "win32", reason="RegisterHotKey")
def test_global_hotkeys_use_overrides_and_report_taken_combos(qapp: QApplication) -> None:
    combo = "Ctrl+Alt+Win+F24"  # unlikely to be held by anything, the dev pet included
    first = GlobalHotkeys({"color": combo})
    first.start()
    try:
        assert "color" in first.registered and combo not in first.failed
        second = GlobalHotkeys({"color": combo})
        second.start()
        try:
            assert combo in second.failed and "color" not in second.registered
        finally:
            second.close()
    finally:
        first.close()
    again = GlobalHotkeys({"color": combo})  # close() waited for the unregister, so it is free at once
    again.start()
    try:
        assert "color" in again.registered
    finally:
        again.close()


def test_hotkey_menu_edits_and_resets(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtWidgets import QInputDialog

    from src.engine.models import PetStats
    from src.engine.pet_state import PetStateMachine
    from src.ui.pet_window import PetWindow

    win = PetWindow(state_machine=PetStateMachine(PetStats()), auto_wander=False)
    changed: list[dict[str, str]] = []
    win.on_hotkeys_changed = lambda: changed.append(dict(win.hotkeys))
    answers = iter([("ctrl+alt+k", True), ("Ctrl+Shift+Alt+S", True), ("nope", True), ("", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(answers))
    try:
        win.trigger_tool_action("hotkey:color")
        assert win.hotkeys == {"color": "Ctrl+Alt+K"}
        win.trigger_tool_action("hotkey:ai")  # already Snip Image's combo
        win.trigger_tool_action("hotkey:ai")  # not a combo
        assert win.hotkeys == {"color": "Ctrl+Alt+K"} and len(changed) == 1
        menu = win.build_context_menu()
        assert menu.plugin_actions["snip:color"].text() == "Pick Screen Color -> Copy HEX\tCtrl+Alt+K"
        menu.deleteLater()
        win.trigger_tool_action("hotkey:color")  # empty resets
        assert win.hotkeys == {} and changed[-1] == {}
        menu = win.build_context_menu()
        assert "Pick Color\tCtrl+Shift+Alt+C" in [a.text() for a in menu.hotkey_menu.actions()]
        menu.deleteLater()
    finally:
        win.shutdown()
        win.close()
