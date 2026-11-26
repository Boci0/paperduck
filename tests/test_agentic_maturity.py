from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QObject, Qt
from PyQt6.QtWidgets import QApplication

from src.tools import ai_tools, checkpoints, login_vault, skill_crystallize
from src.tools.ambient import AmbientRing
from src.tools.errands import ErrandsPlugin, Probes, run_routine_script
from src.ui.ai_tool_host import AiToolHost
from src.ui.quick_bar import QuickCommandBar
from src.ui.quick_tools import HOTKEY_ACTIONS, HOTKEY_MODES


def test_ambient_ring_privacy_scrubbing_dedup_and_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    ring = AmbientRing(max_events=5, ttl_s=60.0)
    monkeypatch.setattr(login_vault, "typed_password_shown", lambda: False)

    assert not ring.record_window("explorer.exe", "Program Manager", now=100.0)
    assert not ring.record_window("pythonw.exe", "Paperduck", now=100.0)
    assert not ring.record_window("chrome.exe", "Sign in - Bitwarden", now=100.0)
    assert ring.record_window("Code.exe", "main.py - desktop_pet", now=100.0)
    assert ring.record_window("Code.exe", "main.py - desktop_pet", now=105.0)
    assert len(ring.recent(now=105.0)) == 1
    assert ring.recent(now=105.0)[0].count == 2

    assert not ring.record_clipboard("secret", private=True, now=106.0)
    assert ring.record_clipboard("use sk-123456789012345678901234567890", app="Code.exe", now=107.0)
    prompt = ring.snapshot_prompt(now=110.0)
    assert "sk-1234567890" not in prompt and "[key removed]" in prompt
    assert "Code.exe: main.py - desktop_pet" in prompt

    monkeypatch.setattr(login_vault, "typed_password_shown", lambda: True)
    assert not ring.record_window("notepad.exe", "notes.txt", now=111.0)
    assert not ring.record_clipboard("hello", now=111.0)
    monkeypatch.setattr(login_vault, "typed_password_shown", lambda: False)

    # TTL expiration
    assert ring.recent(now=200.0) == []


def test_quick_bar_and_hotkeys() -> None:
    from src.tools.ai_assist import AiAssistPlugin

    _app = QApplication.instance() or QApplication([])
    assert "quick" in HOTKEY_ACTIONS
    modes = {m for _k, m in HOTKEY_MODES.values()}
    assert "quick" in modes
    menu_ids = {item.callback_id for item in AiAssistPlugin().get_menu_items()}
    assert "snip:quick" in menu_ids

    bar = QuickCommandBar()
    got: list[str] = []
    bar.submitted.connect(got.append)
    bar.open_at_cursor("Code")
    assert bar.isVisible() and bar._ctx.text() == "Code"
    assert bar._drag_offset is None
    bar._close.click()
    assert not bar.isVisible()

    bar.open_at_cursor("Code")
    bar._input.setText("explain this")
    bar._on_submit()
    assert got == ["explain this"] and not bar.isVisible()
    bar.close()


def test_checkpoints_and_hard_verification(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    tracked = tmp_path / "app.py"
    tracked.write_text("x = 1\n", encoding="utf-8")
    git("add", "app.py")
    git("commit", "-m", "init")

    # Pre-existing dirty tracked edit + pre-existing untracked user file
    tracked.write_text("x = 2\n", encoding="utf-8")
    pre_untracked = tmp_path / "notes.txt"
    pre_untracked.write_text("keep me\n", encoding="utf-8")

    cp = checkpoints.create_checkpoint(tmp_path)
    assert cp is not None and cp.stash_ref

    # Simulated mutating agent task
    tracked.write_text("x = 999\n", encoding="utf-8")
    new_file = tmp_path / "temp_gen.py"
    new_file.write_text("bad\n", encoding="utf-8")

    res = checkpoints.revert_checkpoint(cp)
    assert res.startswith("reverted checkpoint")
    assert tracked.read_text(encoding="utf-8") == "x = 2\n"
    assert pre_untracked.read_text(encoding="utf-8") == "keep me\n"
    assert not new_file.exists()

    # Hard verification gate includes last_verify_error
    host = SimpleNamespace(edit_gen=1, verified_gen=0, edited_code_files=["app.py"],
                           last_verify_error="exit code 1", open_tasks=0)
    assert "last verification failed (exit code 1)" in AiToolHost.verify_open.fget(host)
    host.verified_gen = 1
    host.last_verify_error = ""
    assert AiToolHost.verify_open.fget(host) == ""


def test_durable_compilable_routines(tmp_path: Path) -> None:
    plugin = ErrandsPlugin()
    plugin.probes = Probes()
    now = 1000.0
    e = plugin.add({"kind": "every", "minutes": 5}, "check status", "sid1", now=now, script="echo ok")
    assert e.repeat is True and e.expires == 0.0 and e.script == "echo ok"

    # Not due yet
    assert not plugin.poll(now=1100.0)
    # Due: fires and stays active, re-baselined for next interval
    assert plugin.poll(now=1305.0)
    assert len(plugin.fired) == 1 and len(plugin.errands) == 1
    assert plugin.errands[0].run_count == 1

    # Persistence round-trip preserves repeat and script
    restored = ErrandsPlugin()
    restored.load_dict(plugin.to_dict(), now=1306.0)
    assert len(restored.errands) == 1
    assert restored.errands[0].repeat is True and restored.errands[0].script == "echo ok"

    handled, out = run_routine_script("echo routine ok", cwd=tmp_path)
    assert handled is True and "routine ok" in out
    handled_ai, _ = run_routine_script("echo NEEDS_AI: check this", cwd=tmp_path)
    assert handled_ai is False


def test_skill_crystallization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    draft = skill_crystallize.draft_skill_from_run(
        "Run pytest and verify build",
        [
            ("search_tools(query='test')", "ok"),
            ("run_command('pytest')", "exit code 1\nfailed"),
            ("run_command('pytest -q sk-123456789012345678901234567890')", "exit code 0\n9 passed"),
        ],
        reply="All tests passed.",
    )
    assert draft.name == "run-pytest-and-verify-build"
    assert "sk-1234567890" not in draft.instructions and "[key removed]" in draft.instructions
    assert "exit code 1" not in draft.instructions and "pytest -q" in draft.instructions

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ai_tools, "skills_dir", lambda: tmp_path)
    import src.ui.ai_tool_host as host_mod
    monkeypatch.setattr(host_mod, "skills_dir", lambda: tmp_path)
    host = AiToolHost(QObject())
    host.ran = [("run_command('pytest -q')", "exit code 0")]
    asked: list[str] = []
    monkeypatch.setattr(host, "_allowed", lambda key, prompt, what="": asked.append(key) or False)
    assert host.run("crystallize_skill", {"goal": "Run tests"}).startswith("error: the user declined")
    assert asked == ["add_skill"]


def test_menu_wheel_macros_and_customize_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.engine.pet_state import PetStateMachine
    from src.tools import macros
    from src.ui.context_menu import PetContextMenu
    from src.ui.customize_dialog import CustomizeDialog
    from src.ui.menu_layout import MenuLayout
    from src.ui.tool_wheel import EXTRA_SLICES, ToolWheelOption, WheelLayout

    _app = QApplication.instance() or QApplication([])

    # Wheel extras include Quick, Watch, Phone
    extra_ids = {o.callback_id for o in EXTRA_SLICES}
    assert {"snip:quick", "snip:watch", "remote:toggle"} <= extra_ids
    wl = WheelLayout()
    base = [ToolWheelOption("OCR", "copy text", "#41A6F6", "snip:text")]
    assert wl.toggle("snip:quick", base) == "SHOWN"
    assert "snip:quick" in {o.callback_id for o in wl.visible(base)}

    # Macros support ambient and quick steps
    steps = macros.parse_recipe("ambient > text:Fix > quick")
    assert steps == [("ambient", ""), ("text", "Fix"), ("quick", "")]
    assert "Quick Command" in macros.describe("ambient > quick")

    # MenuLayout persistence + PetContextMenu pins / More Tools / Customize...
    ml = MenuLayout()
    ml.set_pinned("snip:quick", True)
    ml.set_hidden("Ports", True)
    ml.set_hidden("Screenshot", True)
    ml2 = MenuLayout()
    ml2.load_dict(ml.to_dict())
    assert ml2.is_pinned("snip:quick") and ml2.is_hidden("Ports") and ml2.is_hidden("Screenshot")

    sm = PetStateMachine()
    menu = PetContextMenu(
        state_machine=sm,
        always_on_top=True,
        auto_wander=False,
        on_feed=lambda: None,
        on_play=lambda: None,
        on_toggle_sleep=lambda: None,
        on_reset_position=lambda: None,
        on_toggle_on_top=lambda _b: None,
        on_toggle_wander=lambda _b: None,
        on_quit=lambda: None,
        on_tool_action=lambda _cid: None,
        menu_layout=ml2,
    )
    texts = [a.text() for a in menu.actions()]
    assert "Quick Command" in texts and "Customize..." in texts and "More Tools" in texts
    assert "Screenshot" not in texts
    menu.deleteLater()

    # CustomizeDialog opens and toggles wheel/menu state cleanly
    fake_win = SimpleNamespace(
        wheel=wl,
        menu_layout=ml2,
        macros=macros.MacroBook(),
        hotkeys={},
        on_hotkeys_changed=lambda: None,
        all_tool_wheel_options=lambda: base,
        trigger_tool_action=lambda _c: None,
        _show_toast=lambda _t: None,
    )
    dlg = CustomizeDialog(fake_win)
    assert dlg._tabs.count() == 3
    dlg.close()
    dlg.deleteLater()


def test_repeating_port_and_window_errands_edge_trigger() -> None:
    listening = [True]
    title = ["Build Failed"]
    plugin = ErrandsPlugin()
    plugin.probes = Probes(listening=lambda _p: listening[0], title=lambda: title[0])

    # Set while port is closed / window not front so initial baseline isn't active
    listening[0] = False
    title[0] = "Editor"
    ep = plugin.add({"kind": "port_opens", "port": 8080}, "check port", "s1", now=100.0, repeat=True)
    ew = plugin.add({"kind": "window_appears", "title": "Build Failed"}, "check win", "s1", now=100.0, repeat=True)
    assert ep.repeat and ew.repeat

    # Transition to active -> fires once
    listening[0] = True
    title[0] = "Build Failed - Log"
    assert plugin.poll(now=101.0)
    assert len(plugin.fired) == 2
    plugin.fired.clear()

    # Still active -> must NOT re-fire
    assert not plugin.poll(now=102.0)
    assert plugin.fired == []

    # Clear active state -> still does not fire
    listening[0] = False
    title[0] = "Editor"
    assert not plugin.poll(now=103.0)

    # Transition back to active -> fires again
    listening[0] = True
    title[0] = "Build Failed"
    assert plugin.poll(now=104.0)
    assert len(plugin.fired) == 2


def test_checkpoint_tried_once_per_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _app = QApplication.instance() or QApplication([])
    import src.ui.ai_tool_host as host_mod

    calls = 0

    def fake_cp(_root: Path | None) -> None:
        nonlocal calls
        calls += 1
        return None

    monkeypatch.setattr(host_mod, "create_checkpoint", fake_cp)
    host = AiToolHost(QObject())
    host._project = lambda: tmp_path  # type: ignore[method-assign]
    monkeypatch.setattr(host, "_allowed", lambda *a, **k: False)

    host.run("run_command", {"command": "echo 1"})
    host.run("run_command", {"command": "echo 2"})
    assert calls == 1

    import threading
    host.reset_answer(threading.Event())
    host.run("run_command", {"command": "echo 3"})
    assert calls == 2


def test_send_prompt_steers_when_busy() -> None:
    from src.ui.ai_chat import AiChat

    steered: list[str] = []
    fake = SimpleNamespace(_job=object(), _testing=False, _steer=lambda txt: steered.append(txt))
    assert AiChat.send_prompt(fake, "  follow up  ") is True  # type: ignore[arg-type]
    assert steered == ["follow up"]

    fake_testing = SimpleNamespace(_job=object(), _testing=True, _steer=lambda txt: steered.append(txt))
    assert AiChat.send_prompt(fake_testing, "ignored") is False  # type: ignore[arg-type]


def test_pet_window_shutdown_closes_quick_bar() -> None:
    from src.engine.pet_state import PetStateMachine
    from src.ui.pet_window import PetWindow

    _app = QApplication.instance() or QApplication([])
    sm = PetStateMachine()
    win = PetWindow(state_machine=sm)
    win.open_quick_bar()
    bar = win._quick_bar
    assert bar is not None and bar.isVisible()
    win.shutdown()
    assert win._quick_bar is None
    assert not bar.isVisible()
    win.close()
