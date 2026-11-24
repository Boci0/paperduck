"""Tests for single-monitor computer use and agentic core upgrades."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_computer_inputs_keys_scroll_and_yank_abort(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import computer

    assert computer.parse_keys("ctrl+[") == [0x11, 0xDB]
    assert computer.parse_keys("insert") == [0x2D]
    assert computer.parse_keys("printscreen") == [0x2C]

    sent: list[tuple[int, int, int]] = []
    monkeypatch.setattr(computer, "_send", lambda *ins: sent.extend((i.type, i.mi.flags, i.mi.data) for i in ins))
    monkeypatch.setattr(computer, "_move", lambda x, y: None)
    computer.scroll(3, horizontal=True, modifiers=[0x11])
    assert any(t == 0 and f == computer._HWHEEL and d == 360 for t, f, d in sent)
    assert any(t == 1 for t, _f, _d in sent)

    monkeypatch.setattr(computer, "_check_point", lambda x, y: (x, y))
    monkeypatch.setattr(computer.time, "sleep", lambda _s: None)
    monkeypatch.setattr(computer, "cursor_pos", lambda: (500, 500))
    stop = threading.Event()
    assert computer.drag(0, 0, 100, 100, steps=5, button="middle", stop=stop) is False
    assert stop.is_set()

    stop2 = threading.Event()
    assert computer.path([(0, 0), (100, 100)], button="middle", stop=stop2) == 1
    assert stop2.is_set()


def test_computer_window_list_focus_and_uia_action(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import computer

    monkeypatch.setattr(computer, "list_windows", lambda: [
        {"hwnd": 101, "title": "Editor", "exe": "code.exe", "state": "normal", "rect": [0, 0, 800, 600], "foreground": True},
    ])
    class _FakeU32:
        def ShowWindow(self, *_a): return 1
        def IsIconic(self, *_a): return 0
        def SetWindowPos(self, *_a): return 1
        def SystemParametersInfoW(self, *_a): return 0
        def GetForegroundWindow(self): return 101

    monkeypatch.setattr(computer, "_user32", lambda: _FakeU32())
    monkeypatch.setattr(computer, "_ours", lambda *_a: False)
    assert computer.focus_window(exe="code.exe", action="maximize") == 'maximize "Editor" (code.exe)'

    from src.tools import warm_ps
    monkeypatch.setattr(computer, "popup_windows", lambda _h: [])
    monkeypatch.setattr(warm_ps, "run", lambda *_a: "OK:toggled to On")
    assert computer.uia_action(101, 10, 20, "Bold", "toggle") == (True, "toggled to On")


def test_ai_tools_outline_edit_range_syntax_readonly_and_bg(tmp_path: Path) -> None:
    from src.tools import ai_tools

    py = tmp_path / "sample.py"
    py.write_text("class Foo:\n    def bar(self, x):\n        return x\n\nasync def baz():\n    return 1\n", encoding="utf-8")
    out = ai_tools.file_outline(tmp_path, "sample.py")
    assert "class Foo" in out and "def bar(self, x)" in out and "async def baz()" in out

    dup = tmp_path / "dup.py"
    dup.write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
    e1 = ai_tools.plan_edit(tmp_path, {"path": "dup.py", "old_text": "x = 1", "new_text": "x = 9", "start_line": 2, "end_line": 3})
    assert e1.after == "x = 1\ny = 2\nx = 9\n"
    e2 = ai_tools.plan_edit(tmp_path, {"path": "dup.py", "old_text": "x = 1", "new_text": "x = 0", "replace_all": True})
    assert e2.after == "x = 0\ny = 2\nx = 0\n"

    bad_py = ai_tools.Edit(tmp_path / "bad.py", "bad.py", "", "def broken(:\n", False, "")
    assert "syntax check found issues" in ai_tools.syntax_check_edits([bad_py])

    assert ai_tools.is_readonly_command("git status") is True
    assert ai_tools.is_readonly_command("pytest -q") is True
    assert ai_tools.is_readonly_command("git diff --output=x.txt") is False
    assert ai_tools.is_readonly_command("git status && del a.py") is False
    assert ai_tools.is_readonly_command("rm -rf /") is False

    procs = ai_tools.BackgroundProcs()
    try:
        started = procs.start(tmp_path, f'"{sys.executable}" -c "import sys; print(sys.stdin.readline().strip(), flush=True)"')
        assert started.startswith("started bg1")
        assert procs.send("bg1", "hello") == "sent input to bg1"
    finally:
        procs.stop_all()


def test_search_sessions_and_mcp_stderr(tmp_path: Path) -> None:
    from src.tools import ai_sessions, mcp_client

    ai_sessions.save_session("abcdef012345", [{"role": "user", "content": "fix my webpack config sk-1234567890123456789012345"}, {"role": "assistant", "content": "done"}], root=tmp_path)
    found = ai_sessions.search_sessions("webpack", root=tmp_path)
    assert "abcdef012345" in found and "[key removed]" in found
    assert ai_sessions.search_sessions("nonexistent_xyz", root=tmp_path) == "no matching past chats"

    hub = mcp_client.McpHub(tmp_path / "mcp.json")
    hub._servers["pw"] = SimpleNamespace(readonly={"browser_snapshot"}, destructive=set())
    assert hub.is_readonly("pw__browser_snapshot") is True
    assert hub.is_readonly("pw__browser_click") is False


def test_ai_assist_helper_model_trimmed_and_parallel_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import ai_assist

    p = ai_assist.AiAssistPlugin()
    p.load_dict({"helper_model": "deepseek-flash"})
    assert p.helper_model == "deepseek-flash" and p.to_dict()["helper_model"] == "deepseek-flash"

    long_txt = "HEAD" + "m" * 600 + "TAIL"
    trimmed = ai_assist._trimmed(long_txt)
    assert trimmed is not None and trimmed.startswith("HEAD") and "TAIL" in trimmed and trimmed.endswith(ai_assist.TRIMMED)

    threads: list[int] = []
    class ParallelHost:
        specs = [{"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
                 for n in ("read_file", "edit_file")]
        def can_parallel(self, name: str, args: dict) -> bool:
            return name == "read_file"
        def run(self, name: str, args: dict) -> str:
            threads.append(threading.get_ident())
            return f"ok:{args['path']}"

    res = ai_assist._run_calls([("read_file", '{"path":"a"}'), ("read_file", '{"path":"b"}'), ("edit_file", "{}")], ParallelHost(), None)
    assert res[:2] == ["ok:a", "ok:b"] and res[2] is None


def test_host_paste_restores_clipboard_and_sticky_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtCore import QObject
    from PyQt6.QtWidgets import QApplication
    from src.ui.ai_tool_host import AiToolHost

    app = QApplication.instance() or QApplication([])
    cb = app.clipboard()
    cb.setText("original-clip")

    class _Win(QObject):
        _ai_chat = None
        _state_machine = SimpleNamespace(registry=SimpleNamespace(get_plugin=lambda _n: None))

    host = AiToolHost(_Win())
    try:
        host.mcp_loaded.add("pw")
        assert "pw" in host.mcp_loaded
    finally:
        host._idle_timer.stop()


def test_host_wait_for_ui_batch_verify_and_helper_routing(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtGui import QImage
    from src.tools import ai_assist, computer
    from src.ui import ai_tool_host
    from src.ui.ai_tool_host import AiToolHost

    monkeypatch.setattr(computer, "front_state", lambda: ("My Editor", False))
    fake = SimpleNamespace(cancel=threading.Event())
    assert AiToolHost._wait_for_ui(fake, {"window_title": "Editor", "timeout_s": 1}).startswith('window_title "Editor" matched')
    fake.cancel.set()
    assert AiToolHost._wait_for_ui(fake, {"window_title": "Missing", "timeout_s": 1}) == "error: stopped by the user"

    img = QImage(20, 20, QImage.Format.Format_RGB32)
    batch_fake = SimpleNamespace(
        cancel=threading.Event(),
        _allowed=lambda *_a, **_k: True,
        _in_batch=False,
        _batch_acted=False,
        _computer=lambda _n, _a, approved=False: "ok",
        _on_gui=lambda fn: (img, 1),
        _grab_screen=lambda done, primary=True: None,
        _peek_region=lambda image, hwnd, diff=False: f"Peeked diff={diff}",
    )
    out = AiToolHost._batch(batch_fake, [{"tool": "press_key", "keys": "enter"}], verify="peek")
    assert out == "1. ok\nPeeked diff=False"

    asked_models: list[str] = []
    monkeypatch.setattr(ai_tool_host, "review_action", lambda _u, _k, model, *_a: asked_models.append(model) or (True, "ok"))
    monkeypatch.setattr(ai_tool_host, "load_key", lambda _u: "k")
    monkeypatch.setattr(ai_tool_host, "ask", lambda _u, _k, model, *_a, **_kw: asked_models.append(model) or ai_assist.AiReply("done"))
    monkeypatch.setattr(ai_tool_host, "fit_thinking", lambda _u, e, _m: e)
    plugin = SimpleNamespace(base_url="https://api.example/v1", model="main-model", helper_model="fast-model", auto=True)
    chat = SimpleNamespace(_plugin=plugin, _allowed=set(), user_requests=lambda: ["hi"], confirm=lambda *_a, **kw: kw["review"])
    r_fake = SimpleNamespace(_win=SimpleNamespace(_ai_chat=chat), cancel=threading.Event())
    assert AiToolHost._review(r_fake, "act") == (True, "ok")
    AiToolHost._run_agent(r_fake, "task", "low", SimpleNamespace(root=None, images=[]), threading.Event())
    AiToolHost._run_agent(r_fake, "task", "high", SimpleNamespace(root=None, images=[]), threading.Event())
    assert asked_models == ["fast-model", "fast-model", "main-model"]

    seen_reviews: list[object] = []
    chat2 = SimpleNamespace(_plugin=plugin, _allowed=set(), confirm=lambda _k, _t, _w, done, **kw: seen_reviews.append(kw.get("review")) or done(True))
    a_fake = SimpleNamespace(outside=False, _win=SimpleNamespace(_ai_chat=chat2), cancel=threading.Event(),
                           _review=lambda _a: (_ for _ in ()).throw(AssertionError("should not call _review")),
                           _waiting=lambda ask: (box := []) or ask(box.append) or box[0])
    assert AiToolHost._allowed(a_fake, "run_command", "run: git status", "commands", fast_review=(True, "read-only command")) is True
    assert seen_reviews == [(True, "read-only command")]
