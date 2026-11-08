"""Chat drag, fullscreen yield, edge-safe computer use and file edit edge cases."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from src.engine.models import PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.tools import ai_tools, computer
from src.ui import pet_window
from src.ui.pet_window import PetWindow


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


# --- chat header drag --------------------------------------------------------------------------------------

def _mouse(kind: QEvent.Type, local: QPoint, glob: QPoint) -> QMouseEvent:
    return QMouseEvent(kind, QPointF(local), QPointF(glob), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                       Qt.KeyboardModifier.NoModifier)


def test_dragging_the_chat_header_moves_it_and_keeps_it_there(
        qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools.ai_assist import AiAssistPlugin
    from src.ui.ai_chat import AiChat

    for mod in ("ai_chat", "chat_setup"):  # the chat and its Setup page both read the key

        monkeypatch.setattr(f"src.ui.{mod}.load_key", lambda _url: "k")
    monkeypatch.setattr(AiAssistPlugin, "has_key", lambda self: True)
    plugin = AiAssistPlugin()
    plugin.base_url, plugin.model = "https://api.example/v1", "m"
    chat = AiChat(plugin)
    try:
        area = qapp.primaryScreen().availableGeometry()
        chat.open_near(QRect(area.center().x() - 64, area.bottom() - 200, 128, 128))
        qapp.processEvents()
        start = chat.pos()
        grip = QPoint(150, 8)  # in the header, left of the buttons
        chat.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, grip, start + grip))
        chat.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, grip, start + grip + QPoint(-120, -60)))
        chat.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, grip, start + grip + QPoint(-120, -60)))
        assert chat.pos() == start + QPoint(-120, -60) and not hasattr(chat, "moved")  # the pet stays put
        assert chat._anchor.top() == chat.geometry().bottom() + 1 + chat.GAP
        chat._relayout()  # content changes keep the dropped spot instead of snapping back to the pet
        assert chat.pos() == start + QPoint(-120, -60)
        below = QPoint(150, chat._header_sep.mapTo(chat, QPoint(0, 0)).y() + 20)  # the thread: no drag
        chat.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, below, chat.pos() + below))
        assert chat._drag_from is None
    finally:
        chat.close()


# --- fullscreen yield --------------------------------------------------------------------------------------

def test_a_fullscreen_app_on_the_pets_monitor_is_not_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    rects = {10: ("D1", (0, 0, 1920, 1080), (1920, 1080)), 11: ("D1", (0, 0, 1920, 1040), (1920, 1080)),
             12: ("D2", (0, 0, 1920, 1080), (1920, 1080)), 99: ("D1", (1500, 800, 1676, 1022), (1920, 1080))}
    front = [10]
    monkeypatch.setattr(computer, "foreground_window", lambda: front[0])
    monkeypatch.setattr(computer, "window_on_monitor", rects.get)
    monkeypatch.setattr(computer, "window_class", lambda h: "WorkerW" if h == 13 else "Game")
    rects[13] = rects[10]
    assert pet_window.fullscreen_in_front({99}, 99)  # a borderless game or video fills the pet's monitor
    for hwnd in (11, 12, 13, 99):  # maximized only, on another monitor, the desktop, our own window
        front[0] = hwnd
        assert not pet_window.fullscreen_in_front({99}, 99), hwnd


# --- computer use near the edges ---------------------------------------------------------------------------

def test_clicks_a_hair_past_the_edge_land_on_the_last_pixel(monkeypatch: pytest.MonkeyPatch) -> None:
    moved = []

    class U32:
        def SetCursorPos(self, x, y):  # noqa: N802
            moved.append((x, y))

    monkeypatch.setattr(computer, "screen_size", lambda: (1920, 1080))
    monkeypatch.setattr(computer, "_user32", U32)
    monkeypatch.setattr(computer.time, "sleep", lambda _s: None)
    computer._move(1920, 1081)
    computer._move(-1, 5)
    assert moved == [(1919, 1079), (0, 5)]
    with pytest.raises(ValueError, match="off the screen"):
        computer._move(1930, 10)


def test_screen_map_drops_controls_off_the_primary_monitor_and_scroll_needs_both_coordinates() -> None:
    mapped = ai_tools.parse_boxes("", 1920, 1080, "10,10,30,30\tButton\tSave\n2000,10,30,30\tButton\tFar\n")
    assert '"Save"' in mapped and '"Far"' not in mapped
    with pytest.raises(ValueError, match="both x and y"):
        ai_tools.computer_action("scroll", {"amount": 2, "x": 5})
    with pytest.raises(ValueError):
        ai_tools.computer_action("scroll", {"amount": 2, "x": "5", "y": 1})
    assert ai_tools.computer_action("scroll", {"amount": 2, "x": 5, "y": 6}) == ("scroll down 2 at (5, 6)", "")


# --- file edits at the edges of a file ---------------------------------------------------------------------

def test_edits_handle_bom_empty_files_overlaps_and_no_ops(tmp_path: Path) -> None:
    bom = tmp_path / "bom.txt"
    bom.write_bytes(b"\xef\xbb\xbffirst\nsecond\n")
    edit = ai_tools.plan_edit(tmp_path, {"path": "bom.txt", "old_text": "first", "new_text": "1st"})
    assert ai_tools.apply_edit(edit) == "edited bom.txt (+1 -1 lines)"
    assert bom.read_bytes() == b"\xef\xbb\xbf1st\nsecond\n"  # matched line 1 and kept the BOM

    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    edit = ai_tools.plan_edit(tmp_path, {"path": "empty.txt", "old_text": "", "new_text": "hi\n"})
    assert ai_tools.apply_edit(edit) == "edited empty.txt (+1 -0 lines)" and empty.read_text() == "hi\n"

    (tmp_path / "a.txt").write_text("aaa", encoding="utf-8")
    with pytest.raises(ValueError, match="appears 2 times"):  # str.count says 1: the matches overlap
        ai_tools.plan_edit(tmp_path, {"path": "a.txt", "old_text": "aa", "new_text": "b"})
    with pytest.raises(ValueError, match="nothing would change"):
        ai_tools.plan_edit(tmp_path, {"path": "a.txt", "old_text": "aaa", "new_text": "aaa"})


def test_diff_marks_a_missing_final_newline_and_counts_the_whole_diff(tmp_path: Path) -> None:
    (tmp_path / "n.txt").write_text("a\nb", encoding="utf-8")
    edit = ai_tools.plan_edit(tmp_path, {"path": "n.txt", "old_text": "b", "new_text": "c"})
    assert "-b\n\\ No newline at end of file\n+c\n\\ No newline at end of file\n" in edit.diff
    big = "\n".join(f"line {i}" for i in range(3000)) + "\n"
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    edit = ai_tools.plan_edit(tmp_path, {"path": "big.txt", "old_text": big, "new_text": "x\n"})
    assert "diff cut" in edit.diff and (edit.added, edit.removed) == (1, 3000)


def test_typed_crlf_is_one_enter(monkeypatch: pytest.MonkeyPatch) -> None:
    sent = []
    monkeypatch.setattr(computer, "_send", lambda *inputs: sent.append(inputs[0].ki.vk))
    monkeypatch.setattr(computer.time, "sleep", lambda _s: None)
    computer.type_text("a\r\nb")
    assert sent.count(0x0D) == 1
