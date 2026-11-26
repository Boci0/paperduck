"""Regression tests for v3.0.0 pre-release bugfixes and optimizations."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog


_QT_APP: QApplication | None = None


def _app() -> QApplication:
    global _QT_APP
    _QT_APP = QApplication.instance() or _QT_APP or QApplication([])
    return _QT_APP


def test_customize_dialog_toggle_wheel_toast_no_typeerror() -> None:
    _app()
    from src.ui.customize_dialog import CustomizeDialog
    from src.ui.tool_wheel import ToolWheelOption, WheelLayout

    toasts: list[tuple[str, object]] = []
    wheel = WheelLayout()
    base = [ToolWheelOption(f"Opt{i}", "open", "#C9D1D9", f"opt:{i}") for i in range(10)]
    fake_win = SimpleNamespace(
        wheel=wheel,
        all_tool_wheel_options=lambda: base,
        _show_toast=lambda text, color: toasts.append((text, color)),
        trigger_tool_action=lambda _cid: None,
        menu_layout=SimpleNamespace(pinned=[], hidden_sections=set()),
        hotkeys={},
        macros=SimpleNamespace(macros=[], double_click=""),
    )
    dlg = SimpleNamespace(
        _win=fake_win,
        _wheel_count=SimpleNamespace(setText=lambda _t: None),
        _refresh_wheel_count=lambda: None,
    )
    box = QCheckBox()
    box.setChecked(True)
    # Toggling a non-existent slice produces "GONE" toast without TypeError
    CustomizeDialog._toggle_wheel(dlg, "missing:id", box)  # type: ignore[arg-type]
    assert toasts and toasts[0][0] == "GONE"


def test_checkpoints_ls_files_failure_and_head_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import checkpoints

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(tmp_path), check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "test@example.com")
    git("config", "user.name", "Test")
    tracked = tmp_path / "a.txt"
    tracked.write_text("v1\n", encoding="utf-8")
    git("add", "a.txt")
    git("commit", "-m", "init")

    keep_nonascii = tmp_path / "café_keep.txt"
    keep_nonascii.write_text("keep\n", encoding="utf-8")

    # If ls-files fails during create_checkpoint, it must return None (never empty untracked_before)
    real_git = checkpoints._git

    def fail_ls(cwd: Path, *args: str, timeout: float = 10.0):
        if "ls-files" in args:
            return 1, "", "timeout"
        return real_git(cwd, *args, timeout=timeout)

    monkeypatch.setattr(checkpoints, "_git", fail_ls)
    assert checkpoints.create_checkpoint(tmp_path) is None
    monkeypatch.undo()

    cp = checkpoints.create_checkpoint(tmp_path)
    assert cp is not None
    # Simulate staged edit + new untracked file
    tracked.write_text("v2\n", encoding="utf-8")
    git("add", "a.txt")
    new_file = tmp_path / "new.txt"
    new_file.write_text("temp\n", encoding="utf-8")

    res = checkpoints.revert_checkpoint(cp)
    assert res.startswith("reverted checkpoint")
    assert tracked.read_text(encoding="utf-8") == "v1\n"
    assert keep_nonascii.exists()
    assert not new_file.exists()


def test_python_file_runs_with_spaces_in_path(tmp_path: Path) -> None:
    from src.tools import ai_tools

    spaced = tmp_path / "dir with spaces"
    spaced.mkdir()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ai_tools, "scratch_dir", lambda: spaced)
    try:
        path, cmd = ai_tools.python_file("print('hello spaces')", exe=sys.executable)
        out = ai_tools.run_command(spaced, cmd)
        assert "exit code 0" in out
        assert "hello spaces" in out
        path.unlink(missing_ok=True)
    finally:
        monkeypatch.undo()


def test_compact_split_skips_leading_summary_pair() -> None:
    from src.tools import ai_assist

    turns = [
        {"role": "user", "content": f"{ai_assist.COMPACT_PREFIX}\nold summary"},
        {"role": "assistant", "content": "Noted. Continuing from that summary."},
        *[
            {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg {i}"}
            for i in range(ai_assist.KEEP_TURNS)
        ],
    ]
    assert ai_assist.compact_split(turns) == 0


def test_worker_sequential_edits_chain(tmp_path: Path) -> None:
    from src.tools.agents import ReadOnlyHost

    target = tmp_path / "demo.py"
    target.write_text("a = 1\nb = 2\n", encoding="utf-8")
    host = ReadOnlyHost(tmp_path, role="worker", allowed_paths=[str(target)])
    r1 = host.run("edit_file", {"path": "demo.py", "old_text": "a = 1", "new_text": "a = 10"})
    assert r1.startswith("staged")
    r2 = host.run("edit_file", {"path": "demo.py", "old_text": "b = 2", "new_text": "b = 20"})
    assert r2.startswith("staged")
    assert len(host.staged_edits) == 1
    assert host.staged_edits[0].after == "a = 10\nb = 20\n"


def test_login_vault_empty_password_does_not_match() -> None:
    from src.tools import login_vault

    assert login_vault._shows("some text", 0, login_vault.hashlib.sha256(b"").digest()) is False
    login_vault.remember_typed(999999, "")
    assert not any(h == 999999 for h, _n, _d, _t in login_vault._typed)


def test_clip_vault_surrogate_b64_and_sha256() -> None:
    from src.tools.clip_vault import ClipVaultPlugin

    bad = "abc\ud800def"
    vault = ClipVaultPlugin(read_text=lambda: bad, write_text=lambda _t: None)
    ok_b64, _toast, _col = vault.execute_action("clipvault:b64_toggle")
    assert ok_b64
    ok_sha, _toast2, _col2 = vault.execute_action("clipvault:sha256")
    assert ok_sha


def test_file_forge_pre_1980_zip_and_truncated_pdf(tmp_path: Path) -> None:
    from src.tools import file_forge

    old_file = tmp_path / "old.txt"
    old_file.write_text("hello", encoding="utf-8")
    # 1970-01-02 timestamp
    os.utime(old_file, (86400, 86400))
    res = file_forge.compress_to_zip([old_file], output_path=tmp_path / "out.zip")
    assert res.ok

    bad_pdf = b"%PDF-1.4\n1 0 obj\n<< /Author (Alice) >>\n/Info 1 0 R\n"
    assert file_forge._scrub_pdf(bad_pdf) is None


def test_ambient_ignores_private_apps_and_slices_clipboard() -> None:
    from src.tools.ambient import AmbientRing

    ring = AmbientRing()
    assert ring.record_window("1password.exe", "Vault") is False
    assert ring.record_clipboard("x" * 20_000, app="notepad.exe") is True
    assert len(ring.recent(1)[0].detail) <= 80


def test_ai_chat_apply_scroll_handles_deleted_target() -> None:
    _app()
    from src.ui.ai_chat import AiChat

    class DeletedTarget:
        def y(self) -> int:
            raise RuntimeError("wrapped C/C++ object of type _HoverRow has been deleted")

    values: list[int] = []
    bar = SimpleNamespace(maximum=lambda: 100, setValue=lambda v: values.append(v))
    fake = SimpleNamespace(
        _follow=True,
        _scroll_target=DeletedTarget(),
        _scroll=SimpleNamespace(verticalScrollBar=lambda: bar),
    )
    AiChat._apply_scroll(fake)  # type: ignore[arg-type]
    assert fake._scroll_target is None
    assert values == [100]


def test_ocr_job_emits_finished_on_rmdir_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _app()
    from src.ui import quick_tools

    d = tmp_path / "ocr_dir"
    d.mkdir()
    png = d / "snip.png"
    png.write_bytes(b"png")
    (d / "keep.txt").write_text("lock", encoding="utf-8")  # causes rmdir() OSError

    emitted: list[tuple[object, str, object]] = []
    monkeypatch.setattr(quick_tools, "ocr_image_file", lambda _p: "hello ocr")
    monkeypatch.setattr(quick_tools, "emit_later", lambda obj, sig, val: emitted.append((obj, sig, val)))
    job = quick_tools.OcrJob()
    job._run(png)
    assert emitted == [(job, "finished", "hello ocr")]


def test_destructive_mcp_still_reviewed_when_server_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _app()
    from PyQt6.QtCore import QObject
    import src.ui.ai_tool_host as host_mod
    from src.ui.ai_tool_host import AiToolHost

    parent = QObject()
    host = AiToolHost(parent)
    reviewed: list[str] = []
    confirm_reviews: list[object] = []

    class FakeMcp:
        image = b""

        @staticmethod
        def server_of(name: str) -> str | None:
            return "fs" if name.startswith("fs__") else None

        @staticmethod
        def is_destructive(name: str) -> bool:
            return name == "fs__delete"

        @staticmethod
        def is_readonly(_name: str) -> bool:
            return False

        @staticmethod
        def call(name: str, _args: dict, _cancel: object) -> str:
            return f"ran {name}"

    class FakeChat:
        _allowed = {"mcp:fs"}

        @staticmethod
        def confirm_mcp(_server: str, _tool: str, done, _args: str = "", _danger: str = "", review=None) -> None:
            confirm_reviews.append(review)
            done(True)

    host.mcp = FakeMcp()  # type: ignore[assignment]
    monkeypatch.setattr(host_mod, "_host_chat", lambda _h: FakeChat)
    monkeypatch.setattr(host_mod, "_host_auto_mode", lambda _h: True)
    monkeypatch.setattr(host, "_review", lambda what: (reviewed.append(what) or (True, "ok")))
    monkeypatch.setattr(host, "_waiting", lambda fn: (res := [], fn(res.append), res[0])[2])

    out = host._run("fs__delete", {"path": "x"})
    assert out == "ran fs__delete"
    assert len(reviewed) == 1
    assert confirm_reviews == [(True, "ok")]
