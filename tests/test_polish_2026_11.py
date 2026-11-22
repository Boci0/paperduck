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
    assert events == [(100, 100), 0x2, (125, 110), (150, 120), (175, 130), (200, 140), 0x4]

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


# --- screen vision upgrades: pixel diff, coordinate ruler, grid, SoM overlay ------------------------------

def test_changed_region_and_diff_note_detect_boxes_and_zero_changes() -> None:
    from PyQt6.QtGui import QColor, QImage
    from src.ui.ai_tool_host import changed_region, diff_note

    a = QImage(200, 120, QImage.Format.Format_RGB32)
    a.fill(QColor("#ffffff"))
    b = a.copy()
    assert changed_region(a, b) == (0, 0, 0, 0, 0)
    assert "0 pixels changed" in diff_note(a, b)

    # 1px flicker (< 4 sampled hits) is ignored
    b.setPixel(10, 10, 0xFF000000)
    assert changed_region(a, b) == (0, 0, 0, 0, 0)

    # Localized rectangle change (fresh copy without the 1px dot at 10, 10)
    b = a.copy()
    for x in range(40, 80):
        for y in range(20, 50):
            b.setPixel(x, y, 0xFF112233)
    res = changed_region(a, b)
    assert res is not None and res[:2] == (40, 20) and 38 <= res[2] <= 42 and 28 <= res[3] <= 32
    note = diff_note(a, b)
    assert "Screen changed in box 40,20," in note and "of screen)." in note

    # Mismatched sizes return None / ""
    assert changed_region(a, a.scaled(100, 60)) is None
    assert diff_note(a, a.scaled(100, 60)) == ""


def test_render_screen_overlay_rulers_grid_som_and_scale_cap(qapp: QApplication) -> None:
    from PyQt6.QtGui import QColor, QImage
    from src.ui.ai_tool_host import RULER_LEFT, RULER_TOP, render_screen_overlay
    from src.ui.chat_widgets import MAX_IMAGE_SIDE

    # Large 2560x1440 input stays within MAX_IMAGE_SIDE including rulers
    big = QImage(2560, 1440, QImage.Format.Format_RGB32)
    big.fill(QColor("#203040"))
    out_big = render_screen_overlay(big)
    assert max(out_big.width(), out_big.height()) <= MAX_IMAGE_SIDE
    assert out_big.width() > RULER_LEFT and out_big.height() > RULER_TOP

    # Small input keeps exact 1:1 image pixels at (RULER_LEFT, RULER_TOP)
    small = QImage(240, 160, QImage.Format.Format_RGB32)
    small.fill(QColor("#ffffff"))
    plain = render_screen_overlay(small, origin=(100, 50))
    assert (plain.width(), plain.height()) == (240 + RULER_LEFT, 160 + RULER_TOP)
    assert plain.pixelColor(RULER_LEFT + 10, RULER_TOP + 10) == QColor("#ffffff")

    # Grid lines are drawn inside the viewport at major ruler ticks (100, 200...)
    gridded = render_screen_overlay(small, origin=(0, 0), grid=True)
    assert gridded.pixelColor(RULER_LEFT + 100, RULER_TOP + 10) != QColor("#ffffff")

    # SoM draws teal control boxes, amber OCR boxes, and skips giant full-screen containers
    items = {
        1: (10, 10, 220, 140, "Giant Canvas"),  # > 65% area -> skipped
        2: (20, 30, 60, 24, "Save"),
        3: (100, 80, 70, 20, "OCR Text"),
    }
    som = render_screen_overlay(small, origin=(0, 0), items=items, ocr_ids={3})
    assert som.pixelColor(RULER_LEFT + 20, RULER_TOP + 30) != QColor("#ffffff")
    assert som.pixelColor(RULER_LEFT + 100, RULER_TOP + 80) != QColor("#ffffff")


def test_map_supports_annotate_and_grid_and_exposes_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    import base64
    from types import SimpleNamespace
    from PyQt6.QtGui import QColor, QImage
    from src.ui.ai_tool_host import RULER_LEFT, RULER_TOP, AiToolHost

    spec = next(s for s in ai_tools.TOOL_SPECS if s["function"]["name"] == "screen_map")
    props = spec["function"]["parameters"]["properties"]
    assert props["annotate"]["type"] == "boolean" and props["grid"]["type"] == "boolean"

    image = QImage(400, 300, QImage.Format.Format_ARGB32)
    image.fill(QColor("#ffffff"))
    monkeypatch.setattr(computer, "ui_elements", lambda _hwnd: "110,60,80,26\tButton\tApply\n")
    fake = SimpleNamespace(
        _ocr=lambda _img, boxes=False: "120,100,70,18\tCanvas",
        _offset_ocr_boxes=AiToolHost._offset_ocr_boxes,
        _items={},
        _ocr_ids=set(),
        _shot="",
        _ref=None,
        _maps=0,
    )
    out = AiToolHost._map(fake, image, 0, region=[100, 50, 200, 150], annotate=True, grid=True)
    assert "Top and left rulers mark true screen-pixel coordinates." in out
    assert "#id boxes are drawn on the screenshot" in out
    assert "Faint grid lines match the ruler ticks." in out
    assert "#1 [Button]" in out and "#2 (255, 159)" in out
    assert fake._shot.startswith("data:image/png;base64,")
    decoded = QImage.fromData(base64.b64decode(fake._shot.split(",", 1)[1]))
    assert (decoded.width(), decoded.height()) == (200 + RULER_LEFT, 150 + RULER_TOP)


def test_act_reports_post_action_pixel_diff(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QColor, QImage
    from src.ui import ai_tool_host
    from src.ui.ai_tool_host import AiToolHost

    before = QImage(200, 150, QImage.Format.Format_RGB32)
    before.fill(QColor("#ffffff"))
    changed = before.copy()
    for x in range(30, 60):
        for y in range(20, 40):
            changed.setPixel(x, y, 0xFF000000)

    monkeypatch.setattr(ai_tool_host, "_ours", lambda _w: [])
    monkeypatch.setattr(computer, "focus_front_app", lambda: 0)
    monkeypatch.setattr(computer, "front_state", lambda: ("Paint", False))
    monkeypatch.setattr(computer, "click", lambda *_a, **_kw: None)
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

    grabs = iter([before.copy(), changed])

    def fake_emit(item):
        fn, box = item
        fn(lambda v: box.__setitem__("result", v))

    results: list[str] = []
    fake = SimpleNamespace(
        _win=None,
        _in_batch=False,
        _ref=before,
        cancel=threading.Event(),
        _call=SimpleNamespace(emit=fake_emit),
        _grab_screen=lambda done, primary=True: done((next(grabs), 1)),
    )

    AiToolHost._act(fake, results.append, "mouse_click", {"x": 45, "y": 30})
    assert "Screen unchanged (0 pixels changed" in results[-1]
    assert results[-1].endswith("Call screen_map for details.")

    AiToolHost._act(fake, results.append, "mouse_click", {"x": 45, "y": 30})
    assert "Screen changed in box 30,20," in results[-1]
    assert results[-1].endswith("Call screen_map for details.")
    assert fake._ref == changed


# --- Track 2: motor primitives + semantic UIA state ------------------------------------------------------

def test_parse_modifiers_modifier_drag_path_and_hover(monkeypatch: pytest.MonkeyPatch) -> None:
    assert computer.parse_modifiers(None) == []
    assert computer.parse_modifiers(["shift", "ctrl", "shift"]) == [0x10, 0x11]
    assert computer.parse_modifiers("ctrl+alt") == [0x11, 0x12]
    with pytest.raises(ValueError, match="modifiers"):
        computer.parse_modifiers(["win"])

    events: list = []

    class U32:
        def SetCursorPos(self, x, y):  # noqa: N802
            events.append((x, y))

    monkeypatch.setattr(computer, "screen_size", lambda: (1920, 1080))
    monkeypatch.setattr(computer, "_user32", U32)
    monkeypatch.setattr(computer.time, "sleep", lambda _s: None)
    monkeypatch.setattr(computer, "_send", lambda *inputs: events.append(
        tuple((i.ki.vk, i.ki.flags) if i.type == 1 else ("M", i.mi.flags) for i in inputs)))

    # Modifier drag presses Shift before mouse-down and releases Shift after mouse-up
    computer.drag(10, 10, 30, 10, steps=2, button="right", modifiers=[0x10])
    assert events == [
        (10, 10),
        ((0x10, 0),), (("M", 0x8),),
        (20, 10), (30, 10),
        (("M", 0x10),), ((0x10, 0x2),),
    ]

    # path validates all points before pressing anything
    events.clear()
    with pytest.raises(ValueError, match="off the screen"):
        computer.path([(10, 10), (5000, 10)])
    assert events == []

    # path traces and stops cleanly on cancel, still releasing button and modifier
    stop = threading.Event()
    moves = 0

    def send_stop(*inputs):
        nonlocal moves
        events.append(tuple((i.ki.vk, i.ki.flags) if i.type == 1 else ("M", i.mi.flags) for i in inputs))
        if any(i.type == 0 and (i.mi.flags & 0x1) for i in inputs):
            moves += 1
            if moves >= 2:
                stop.set()

    monkeypatch.setattr(computer, "_send", send_stop)
    reached = computer.path([(0, 0), (40, 0), (80, 0)], modifiers=[0x11], step_px=20, stop=stop)
    assert reached == 2
    assert (("M", 0x2),) in events and (("M", 0x4),) in events
    assert events[-2:] == [(("M", 0x4),), ((0x11, 0x2),)]

    # hover moves without mouse clicks
    events.clear()
    computer.hover(50, 60, wait_ms=10)
    assert events == [(50, 60)]


def test_parse_boxes_and_find_control_handle_uia_state_and_prioritize_non_items() -> None:
    raw_uia = (
        "10,10,40,24\tButton\tBrush\ton, key=B\n"
        "60,10,40,24\tEdit\tSize\tval=12px, focus\n"
    )
    mapped = ai_tools.parse_boxes("", 1920, 1080, raw_uia)
    assert '#1 [Button] "Brush" [on, key=B] (30, 22) box 10,10,40x24' in mapped
    assert '#2 [Edit] "Size" [val=12px, focus] (80, 22) box 60,10,40x24' in mapped
    assert ai_tools.find_control(raw_uia, "", "Brush", "", 1920, 1080) == (30, 22, "Brush")

    # Past 150 controls, non-DataItem/TreeItem controls are kept ahead of list/tree items
    many = "".join(f"10,{i},20,10\tDataItem\tRow{i}\n" for i in range(160))
    many += "500,500,60,24\tButton\tSave\n"
    prioritized = ai_tools.parse_boxes("", 1920, 1080, many)
    assert '[Button] "Save"' in prioritized


def test_resolve_target_and_computer_action_for_hover_drag_mods_and_path() -> None:
    items = {1: (100, 100, 20, 20, "Pencil")}
    assert ai_tools.resolve_target("mouse_hover", {"id": 1, "wait_ms": 300}, items) == {
        "x": 110, "y": 110, "wait_ms": 300, "target": '#1 "Pencil"'
    }
    assert ai_tools.computer_action("mouse_hover", {"x": 110, "y": 110, "target": '#1 "Pencil"'}) == (
        'hover on #1 "Pencil" at (110, 110)', ""
    )
    assert ai_tools.computer_action(
        "mouse_drag", {"x": 1, "y": 2, "to_x": 30, "to_y": 40, "modifiers": ["shift"], "button": "right"}
    ) == ("shift+right-drag from (1, 2) to (30, 40)", "")
    assert ai_tools.computer_action(
        "mouse_path", {"points": [[10, 20], [30, 40], [50, 20]], "modifiers": ["ctrl"]}
    ) == ("ctrl+trace 3-point path from (10, 20) to (50, 20)", "")
    with pytest.raises(ValueError, match="points"):
        ai_tools.computer_action("mouse_path", {"points": [[10, 20]]})
    with pytest.raises(ValueError, match="modifiers"):
        ai_tools.computer_action("mouse_path", {"points": [[10, 20], [30, 40]], "modifiers": ["bad"]})


def test_act_handles_mouse_hover_tooltip_ocr_and_mouse_path(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from PyQt6.QtCore import QTimer
    from PyQt6.QtGui import QColor, QImage
    from src.ui import ai_tool_host
    from src.ui.ai_tool_host import AiToolHost

    before = QImage(200, 150, QImage.Format.Format_RGB32)
    before.fill(QColor("#ffffff"))
    hovered = before.copy()
    for x in range(50, 110):
        for y in range(40, 60):
            hovered.setPixel(x, y, 0xFF222222)

    monkeypatch.setattr(ai_tool_host, "_ours", lambda _w: [])
    monkeypatch.setattr(computer, "focus_front_app", lambda: 0)
    monkeypatch.setattr(computer, "front_state", lambda: ("Krita", False))
    monkeypatch.setattr(computer, "hover", lambda *_a, **_kw: None)
    monkeypatch.setattr(computer, "uia_at", lambda _x, _y: [("button", "Freehand Brush")])
    monkeypatch.setattr(computer, "path", lambda pts, **_kw: len(pts))
    monkeypatch.setattr(QTimer, "singleShot", staticmethod(lambda _ms, fn: fn()))

    grabs = iter([hovered, hovered])

    def fake_emit(item):
        fn, box = item
        fn(lambda v: box.__setitem__("result", v))

    done_ev = threading.Event()
    results: list[str] = []

    def record(txt: str) -> None:
        results.append(txt)
        done_ev.set()

    fake = SimpleNamespace(
        _win=None,
        _in_batch=False,
        _ref=before,
        cancel=threading.Event(),
        _call=SimpleNamespace(emit=fake_emit),
        _grab_screen=lambda done, primary=True: done((next(grabs), 1)),
        _ocr=lambda _img, empty="": "Freehand Brush Tool (B)",
    )

    AiToolHost._act(fake, record, "mouse_hover", {"x": 40, "y": 30, "wait_ms": 200})
    assert done_ev.wait(3.0)
    assert 'under cursor: [button] "Freehand Brush"' in results[-1]
    assert 'Hover text: "Freehand Brush Tool (B)".' in results[-1]

    done_ev.clear()
    AiToolHost._act(fake, record, "mouse_path", {"points": [[10, 10], [50, 50], [90, 20]]})
    assert done_ev.wait(3.0)
    assert "traced 3-point path." in results[-1]


# --- Track 3: canvas vision instruments + smart screenshot retention -------------------------------------

def test_analyze_colors_points_palette_and_validation() -> None:
    from PyQt6.QtGui import QColor, QImage
    from src.ui.ai_tool_host import analyze_colors

    img = QImage(100, 80, QImage.Format.Format_RGB32)
    img.fill(QColor("#ffffff"))
    for x in range(10, 50):
        for y in range(10, 40):
            img.setPixel(x, y, 0xFF112233)

    out = analyze_colors(img, points=[[20, 20], [80, 60], [500, 500]], region=[0, 0, 100, 80], top=3)
    assert "(20, 20): #112233" in out and "dark)" in out
    assert "(80, 60): #ffffff" in out and "light)" in out
    assert "(500, 500): off screen" in out
    assert "Region 0,0,100x80 palette" in out and "#ffffff" in out and "#112233" in out and "contrast" in out

    assert analyze_colors(img).startswith("error: give points")
    assert analyze_colors(img, points=[[1, 1]], top=0).startswith("error: top must be")
    assert analyze_colors(img, points=[[1, 1]] * 25).startswith("error: points must be")


def test_peek_region_grayscale_diff_and_skips_ocr_uia(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64
    from types import SimpleNamespace
    from PyQt6.QtGui import QColor, QImage
    from src.ui.ai_tool_host import RULER_LEFT, RULER_TOP, AiToolHost
    from src.ui.chat_widgets import MAX_IMAGE_SIDE

    monkeypatch.setattr(computer, "ui_elements", lambda *_a: (_ for _ in ()).throw(AssertionError("no uia")))
    before = QImage(200, 150, QImage.Format.Format_RGB32)
    before.fill(QColor("#2080d0"))
    after = before.copy()
    for x in range(40, 80):
        for y in range(30, 70):
            after.setPixel(x, y, 0xFFE04020)

    fake = SimpleNamespace(_ref=None, _shot="")
    # Grayscale + grid peek without prior ref
    out1 = AiToolHost._peek_region(fake, before, 0, region=[20, 10, 120, 90], grid=True, grayscale=True, diff=True)
    assert "grayscale" in out1 and "No prior screen grab" in out1
    img1 = QImage.fromData(base64.b64decode(fake._shot.split(",", 1)[1]))
    assert (img1.width(), img1.height()) == (120 + RULER_LEFT, 90 + RULER_TOP)
    px = img1.pixelColor(RULER_LEFT + 10, RULER_TOP + 10)
    assert px.red() == px.green() == px.blue()  # grayscale

    # Side-by-side BEFORE | AFTER diff against prior ref
    out2 = AiToolHost._peek_region(fake, after, 0, region=[20, 10, 120, 90], diff=True)
    assert "side-by-side BEFORE | AFTER" in out2 and "Screen changed in box" in out2
    img2 = QImage.fromData(base64.b64decode(fake._shot.split(",", 1)[1]))
    assert max(img2.width(), img2.height()) <= MAX_IMAGE_SIDE
    assert img2.width() > 200  # two panels side by side

    # When _ref already matches current screen (post-action), diff falls back to _prev_ref
    out3 = AiToolHost._peek_region(fake, after, 0, region=[20, 10, 120, 90], diff=True)
    assert "side-by-side BEFORE | AFTER" in out3 and "Screen changed in box" in out3


def test_smart_screenshot_retention_pins_ref_or_overview_while_keeping_homogeneous_trim() -> None:
    from src.tools import ai_assist

    assert ai_assist._shot_role("view_image", {"path": "ref.png"}) == "ref"
    assert ai_assist._shot_role("screen_map", "{}") == "full"
    assert ai_assist._shot_role("peek_region", '{"region": [0, 0, 100, 100]}') == "detail"

    # Homogeneous full-screen shots prune down to just the newest (1)
    assert ai_assist._prune_shot_keys([(1, "full"), (2, "full"), (3, "full"), (4, "full")], 2) == {4}
    # Mixed ref + detail shots pin the ref alongside the newest detail
    assert ai_assist._prune_shot_keys([(1, "ref"), (2, "detail"), (3, "detail"), (4, "detail")], 2) == {1, 4}
    # Mixed full + detail shots pin the newest full view alongside the newest detail
    assert ai_assist._prune_shot_keys([(1, "full"), (2, "detail"), (3, "detail"), (4, "detail")], 2) == {1, 4}

    # Claude _drop_old_shots keeps the pinned view_image reference turn intact
    img_block = [{"type": "text", "text": "ok"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]
    turns = [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "view_image", "input": {"path": "r.png"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": list(img_block)}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t2", "name": "peek_region", "input": {"region": [0, 0, 50, 50]}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t2", "content": list(img_block)}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t3", "name": "peek_region", "input": {"region": [10, 10, 50, 50]}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t3", "content": list(img_block)}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t4", "name": "peek_region", "input": {"region": [20, 20, 50, 50]}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t4", "content": list(img_block)}]},
    ]
    pruned = ai_assist._drop_old_shots(turns)
    assert isinstance(pruned[1]["content"][0]["content"], list)  # pinned ref kept
    assert isinstance(pruned[3]["content"][0]["content"], str)   # intermediate detail dropped
    assert isinstance(pruned[5]["content"][0]["content"], str)   # intermediate detail dropped
    assert isinstance(pruned[7]["content"][0]["content"], list)  # newest turn kept


# --- Track 4 & 5: visual-design skill, check_layout upgrades, unlabeled icon detector ------------------

def test_visual_design_skill_and_geometry_helpers_run_in_powershell(tmp_path: Path) -> None:
    import json
    import subprocess
    from src.tools.builtin_skills import helpers_path

    body = ai_tools.load_skill(tmp_path / "skills", "visual-design")
    hp = helpers_path("visual-design")
    assert f'. "{hp}"' in body and "mouse_hover" in body and "spawn_agent" in body
    assert hp.is_file()

    script = (
        f'. "{hp}"\n'
        '$sun = New-CirclePoints 100 100 20 20 8\n'
        'Write-Output $sun\n'
        'New-BezierPoints 0 0 10 20 30 20 40 0 4\n'
        'New-GridSlots 10 20 220 120 2 1 10 20\n'
    )
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    assert len(lines) == 3
    circle = json.loads(lines[0])
    bezier = json.loads(lines[1])
    slots = json.loads(lines[2])
    assert len(circle) == 9 and circle[0] == [120, 100] and circle[-1] == [120, 100]
    assert len(bezier) == 5 and bezier[0] == [0, 0] and bezier[-1] == [40, 0]
    assert slots == [[20, 30, 90, 100], [130, 30, 90, 100]]


def test_check_layout_detects_alignment_drift_and_visual_imbalance() -> None:
    page = (100, 100, 800, 1000)
    # Balanced page with 20px drift between #2 and #3 (intentional offset > 14px) -> clean
    ink_bal = [(y, 160, 840) for y in range(160, 1040, 3)]
    items_ok = {
        1: (160, 160, 500, 48, "TITLE"),
        2: (160, 240, 400, 20, "First block"),
        3: (160, 300, 400, 20, "Aligned block"),
        4: (160, 360, 400, 20, "Third block"),
    }
    assert ai_tools.check_layout(page, items_ok, ink_bal) == []

    # 8px left-edge drift between #2 and #3 -> flagged
    items_drift = {**items_ok, 3: (168, 300, 400, 20, "Drifted block")}
    assert any("near-aligned left edges" in p for p in ai_tools.check_layout(page, items_drift, ink_bal))

    # Narrow content heavily leaning left -> flagged
    ink_left = [(y, 160, 320) for y in range(160, 1040, 3)]
    assert any("visual weight leans heavily to the left" in p
               for p in ai_tools.check_layout(page, items_ok, ink_left))


def test_detect_toolbar_icons_finds_unlabeled_icons_and_integrates_with_map(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from PyQt6.QtGui import QColor, QImage, QPainter, QPen
    from src.ui.ai_tool_host import AiToolHost, detect_toolbar_icons

    img = QImage(400, 300, QImage.Format.Format_ARGB32)
    img.fill(QColor("#f3f4f6"))
    p = QPainter(img)
    try:
        p.setPen(QPen(QColor("#1f2937"), 2))
        # Icon 1 at (20, 20, 24x24): hollow square + diagonal
        p.drawRect(20, 20, 22, 22)
        p.drawLine(20, 20, 42, 42)
        # Icon 2 at (70, 20, 24x24): hollow circle
        p.drawEllipse(70, 20, 22, 22)
        # Covered by existing UIA control at (120, 20, 24x24) -> should be skipped
        p.drawRect(120, 20, 22, 22)
    finally:
        p.end()

    icons = detect_toolbar_icons(img, existing=[(115, 15, 34, 34)])
    assert len(icons) == 2
    assert any(16 <= x <= 24 and 16 <= y <= 24 for x, y, _w, _h in icons)
    assert any(66 <= x <= 74 and 16 <= y <= 24 for x, y, _w, _h in icons)

    monkeypatch.setattr(computer, "ui_elements", lambda _hwnd: "115,15,34,34\tButton\tBrush\n")
    fake = SimpleNamespace(
        _ocr=lambda _img, boxes=False: "",
        _offset_ocr_boxes=AiToolHost._offset_ocr_boxes,
        _items={},
        _ocr_ids=set(),
        _shot="",
        _ref=None,
        _maps=0,
    )
    out = AiToolHost._map(fake, img, 0, annotate=True)
    assert '[Icon] "(unlabeled)"' in out
    assert "violet = unlabeled icon" in out





