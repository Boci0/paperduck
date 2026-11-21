"""Chat drag, fullscreen yield, edge-safe computer use and file edit edge cases."""

from __future__ import annotations

import os
import threading

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


def test_drag_moves_in_steps_and_always_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list = []

    class U32:
        def SetCursorPos(self, x, y):  # noqa: N802
            events.append((x, y))

    monkeypatch.setattr(computer, "screen_size", lambda: (1920, 1080))
    monkeypatch.setattr(computer, "_user32", U32)
    monkeypatch.setattr(computer.time, "sleep", lambda _s: None)
    monkeypatch.setattr(computer, "_send", lambda *inputs: events.append(inputs[0].mi.flags))
    computer.drag(100, 100, 200, 140, steps=4)
    assert events == [(200, 140), (100, 100), 0x2, (125, 110), (150, 120), (175, 130), (200, 140), 0x4]

    events.clear()  # an end off the screen fails before the button goes down
    with pytest.raises(ValueError, match="off the screen"):
        computer.drag(100, 100, 5000, 100)
    assert 0x2 not in events
    assert ai_tools.computer_action("mouse_drag", {"x": 1, "y": 2, "to_x": 3, "to_y": 4}) == (
        "drag from (1, 2) to (3, 4)", "")
    with pytest.raises(ValueError):
        ai_tools.computer_action("mouse_drag", {"x": 1, "y": 2, "to_x": 3})
    assert "mouse_drag" in ai_tools.COMPUTER_TOOLS


def test_ids_from_screen_map_resolve_to_points_handles_and_offsets() -> None:
    items = {3: (100, 200, 101, 51, "Card"), 7: (400, 400, 11, 11, "Box")}
    resolve = ai_tools.resolve_target
    assert resolve("mouse_click", {"id": 3, "name": "x", "target": "fake"}, items) == {
        "x": 150, "y": 225, "target": '#3 "Card"'}  # the model's own `target` never reaches the approval text
    assert resolve("mouse_drag", {"id": 3, "handle": "se", "dx": 20, "dy": -10}, items) == {
        "x": 200, "y": 250, "to_x": 220, "to_y": 240, "target": '#3 "Card"'}
    assert resolve("mouse_drag", {"id": 7, "to_id": 3}, items)["to_x"] == 150
    assert resolve("mouse_drag", {"x": 1, "y": 2, "dx": 5}, items) == {"x": 1, "y": 2, "to_x": 6, "to_y": 2}
    assert resolve("mouse_click", {"id": 9}, items).startswith("error: no #9")
    assert resolve("mouse_drag", {"id": 3, "handle": "top"}, items).startswith("error: handle")
    assert resolve("type_text", {"text": "hi", "target": "x"}, items) == {"text": "hi"}
    what, _risk = ai_tools.computer_action("mouse_drag", resolve("mouse_drag", {"id": 3, "dx": 1}, items))
    assert what == 'drag #3 "Card" from (150, 225) to (151, 225)'
    assert ai_tools.computer_action("paste_text", {"text": "rm -rf /"})[1]  # pasting a destructive command asks too


def test_batch_runs_wait_and_paste_steps_and_resolves_ids_before_the_approval() -> None:
    from types import SimpleNamespace

    from src.ui.ai_tool_host import AiToolHost

    asked, done = [], []
    fake = SimpleNamespace(cancel=threading.Event(), _items={2: (10, 10, 20, 20, "OK")},
                           _allowed=lambda key, text, what, risk="": asked.append(text) or True,
                           _computer=lambda n, a, approved=False: done.append((n, a)) or "ok")
    out = AiToolHost._batch(fake, [{"tool": "mouse_click", "id": 2}, {"tool": "wait", "ms": 1},
                                   {"tool": "paste_text", "text": "a\nb"}])
    assert out == "1. ok\n2. waited 1 ms\n3. ok"
    assert '1. left-click on #2 "OK" at (20, 20)\n2. wait 1 ms\n3. paste: a\nb' in asked[0]
    assert done == [("mouse_click", {"x": 20, "y": 20, "target": '#2 "OK"'}), ("paste_text", {"text": "a\nb"})]
    assert AiToolHost._batch(fake, [{"tool": "wait", "ms": 99999}]).startswith("error: step 1: wait takes")
    assert AiToolHost._batch(fake, [{"tool": "mouse_click", "id": 5}]).startswith("error: step 1: no #5")


def test_mcp_schemas_join_the_tool_list_only_once_their_server_is_loaded(qapp: QApplication) -> None:
    from types import SimpleNamespace

    from PyQt6.QtCore import QObject

    from src.tools.ai_assist import _all_specs
    from src.ui.ai_tool_host import AiToolHost

    def spec(name: str) -> dict:
        return {"type": "function", "function": {"name": name, "description": "d", "parameters": {}}}

    mcp = [spec("playwright__click"), spec("playwright__goto"), spec("context7__docs")]
    hub = SimpleNamespace(specs=lambda: mcp, server_of=lambda n: n.partition("__")[0] if "__" in n else None,
                          close=lambda: None)
    host = AiToolHost(QObject(), mcp=hub)
    names = [s["function"]["name"] for s in host.specs]
    assert "playwright__click" not in names and names[-1] == "load_mcp_tools"
    assert "playwright (click, goto)" in host.specs[-1]["function"]["description"]
    assert host.run("load_mcp_tools", {"server": "nope"}).startswith("error:")
    assert "playwright__goto" in host.run("load_mcp_tools", {"server": "playwright"})
    names = [s["function"]["name"] for s in host.specs]
    assert "playwright__goto" in names and "context7__docs" not in names
    assert host.specs[-1]["function"]["parameters"]["properties"]["server"]["enum"] == ["context7"]
    assert "context7__docs" in [s["function"]["name"] for s in _all_specs(host)]  # Claude's fixed list has all


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
    assert computer.type_text("a\r\nb") == 3
    assert sent.count(0x0D) == 1


def test_stop_ends_typing_mid_text(monkeypatch: pytest.MonkeyPatch) -> None:
    stop, sent = threading.Event(), []
    monkeypatch.setattr(computer, "_send", lambda *inputs: sent.append(1) if len(sent) < 4 else stop.set())
    monkeypatch.setattr(computer.time, "sleep", lambda _s: None)
    assert computer.type_text("abcdefgh", stop=stop) == 5  # the 5th key went out as Stop landed

def test_layout_check_measures_margins_gaps_text_and_placeholders() -> None:
    page = (100, 100, 800, 1000)
    ink = [(y, 160, 840) for y in range(160, 1040, 3)]  # 60 px margins all round
    items = {1: (160, 160, 500, 48, "WEEK 8 CHEAT SHEET")}
    items |= {n: (160, 200 + n * 60, 400, 20, f"body line {n}") for n in range(2, 8)}
    assert ai_tools.check_layout(page, items, ink) == []
    worse = [(y, 102, 840) for y in range(160, 700, 3)] + [(y, 160, 840) for y in range(820, 830, 3)]
    items |= {8: (160, 640, 300, 6, "Add a heading"), 9: (170, 641, 300, 6, "overlapping"),
              10: (820, 300, 120, 20, "past the edge")}
    got = " | ".join(ai_tools.check_layout(page, items, worse))
    for want in ("2 px from the left edge", "uneven side margins", "empty band at the bottom", "empty gap of",
                 "tiny", 'leftover template text #8 "Add a heading"', "#8 and #9 overlap", "#10 runs past"):
        assert want in got, want
    flat = {n: (160, 200 + n * 60, 400, 20, f"line {n}") for n in range(1, 6)}
    assert any("weak hierarchy" in p for p in ai_tools.check_layout(page, flat, ink))
    assert ai_tools.check_layout(page, {}, []) == ["the page looks empty: nothing but background was found inside it"]


def test_layout_check_scans_the_grab_and_keeps_open_problems(qapp: QApplication) -> None:
    from types import SimpleNamespace

    from PyQt6.QtGui import QColor, QImage

    from src.ui.ai_tool_host import AiToolHost

    image = QImage(1000, 1000, QImage.Format.Format_RGB32)
    image.fill(QColor("#e9ebef"))
    # the page: white, with one dark block against its left edge
    for x in range(100, 900):
        for y in range(100, 900):
            image.setPixel(x, y, 0xFFFFFFFF if not (102 <= x < 500 and 200 <= y < 820) else 0xFF222222)

    def fake_map(img, hwnd, region=None):
        fake._items, fake._ocr_ids = {}, set()
        return "Screen 1000x1000. listing"

    fake = SimpleNamespace(_map=fake_map, _items={}, _ocr_ids=set(), layout_open="")
    out = AiToolHost._layout_check(fake, image, 0, [100, 100, 800, 800])
    assert out.startswith("Layout check: NOT up to standard") and "from the left edge" in out
    assert "listing" in out and "left edge" in fake.layout_open
    # a rough box around a selected page: the page's own edges are found and its outline is no content
    image.fill(QColor("#e9ebef"))
    for x in range(200, 800):
        for y in range(100, 900):
            edge = x < 202 or x > 797 or y < 102 or y > 897
            block = 260 <= x < 740 and 160 <= y < 840 and y % 12 < 4  # text-like lines, mostly page
            image.setPixel(x, y, 0xFF8B3DFF if edge else 0xFF222222 if block else 0xFFFFFFFF)
    out = AiToolHost._layout_check(fake, image, 0, [170, 80, 660, 840])
    assert out.startswith("Layout check: no problems measured"), out[:300]
    assert "(The page measures 20" in out and fake.layout_open == ""


def test_layout_check_flags_low_contrast_text() -> None:
    from types import SimpleNamespace
    from PyQt6.QtGui import QColor, QImage
    from src.ui.ai_tool_host import AiToolHost

    image = QImage(1000, 1000, QImage.Format.Format_ARGB32)
    image.fill(QColor("#e9ebef"))
    for x in range(200, 800):
        for y in range(100, 900):
            in_box = 255 <= x < 445 and 205 <= y < 225 and y % 4 < 2
            image.setPixel(x, y, 0xFFE6E6E6 if in_box else 0xFFFFFFFF)  # ~1.25:1 against white (below 2.5:1)
    fake = SimpleNamespace(_map=lambda _i, _h, region=None: "",
                           _items={1: (250, 200, 200, 30, "Faint heading")}, _ocr_ids={1}, layout_open="")
    out = AiToolHost._layout_check(fake, image, 0, [200, 100, 600, 800])
    assert "NOT up to standard" in out and "low contrast" in out and "Faint heading" in out


def test_map_crops_region_and_offsets_ocr_boxes(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from PyQt6.QtGui import QColor, QImage
    from src.ui.ai_tool_host import AiToolHost

    seen_sizes: list[tuple[int, int]] = []
    image = QImage(500, 400, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    monkeypatch.setattr(computer, "ui_elements", lambda _hwnd: "")

    def fake_ocr(img: QImage, boxes: bool = False) -> str:
        seen_sizes.append((img.width(), img.height()))
        return "10,20,60,18\tSubmit"

    fake = SimpleNamespace(_ocr=fake_ocr, _offset_ocr_boxes=AiToolHost._offset_ocr_boxes,
                           _items={}, _ocr_ids=set(), _shot="", _ref=None, _maps=0)
    out = AiToolHost._map(fake, image, 0, [100, 50, 200, 120])
    assert seen_sizes == [(200, 120)]
    assert "(140, 79)" in out and "box 110,70,60x18" in out  # offset by (+100, +50)


def test_stale_click_falls_back_to_local_uia(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from PyQt6.QtGui import QColor, QImage
    from src.ui import ai_tool_host
    from src.ui.ai_tool_host import AiToolHost

    before = QImage(900, 800, QImage.Format.Format_ARGB32)
    before.fill(QColor("#ffffff"))
    after = QImage(900, 800, QImage.Format.Format_ARGB32)
    after.fill(QColor("#111111"))
    fake = SimpleNamespace(_ref=before, _maps=1, _refused=None, _on_gui=lambda fn: fn(lambda v: v),
                           _grab_screen=lambda done, primary=True: done((after, 42)), _map=lambda *a: "map")
    monkeypatch.setattr(ai_tool_host, "follow", lambda *a: None)
    monkeypatch.setattr(computer, "ui_elements", lambda _hwnd: "110,120,60,24\tButton\tSave")
    note, nx, ny = AiToolHost._stale_click(fake, 100, 100, "Save")
    assert (nx, ny) == (140, 132) and "followed it" in note
    monkeypatch.setattr(computer, "ui_elements", lambda _hwnd: "700,600,40,20\tButton\tSave")
    err, _, _ = AiToolHost._stale_click(fake, 100, 100, "Save")
    assert err.startswith("error: not clicked")


def test_lru_sprite_cache_evicts_oldest_not_all(qapp: QApplication) -> None:
    from collections import OrderedDict
    from PyQt6.QtGui import QImage
    from src.ui.pet_paint import PaintMixin

    class FakePet(PaintMixin):
        def __init__(self) -> None:
            from types import SimpleNamespace
            self._sprite_rect = type("R", (), {"width": lambda _s: 128})()
            self._image_cache: OrderedDict[tuple[str, int, bool, int], QImage] = OrderedDict()
            for i in range(96):
                self._image_cache[("k", i, False, 128)] = QImage(8, 8, QImage.Format.Format_ARGB32)
            self._anim = SimpleNamespace(active_clip_name="k", frame_index=0, facing_left=False,
                                         current_frame=lambda: None)
            self.devicePixelRatioF = lambda: 1.0

    pet = FakePet()
    pet._current_qimage()  # touch ("k", 0, ...) -> promoted to MRU
    pet._anim.active_clip_name = "new"
    from src.graphics.duck import get_poses
    pet._anim.current_frame = lambda: get_poses("idle")[0]
    pet._current_qimage()
    assert len(pet._image_cache) == 96
    assert ("k", 0, False, 128) in pet._image_cache
    assert ("k", 1, False, 128) not in pet._image_cache  # oldest untouched entry evicted

