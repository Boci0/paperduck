"""Multi-file edits, the task list and background commands of the AI chat tools."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tools import ai_tools
from src.tools.ai_tools import TOOL_SPECS


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("import os\nprint('Hello World')\nimport os\n", encoding="utf-8")
    (root / ".env").write_text("TOKEN=abc", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "app.py").write_text("hello", encoding="utf-8")
    return root


def test_edit_files_plans_every_file_and_writes_all_or_none(repo: Path) -> None:
    (repo / "src" / "b.py").write_bytes(b"one\r\ntwo\r\n")
    args = {"edits": [{"path": "src/app.py", "old_text": "import os\nprint('Hello World')\nimport os", "new_text": "X"},
                      {"path": "src/b.py", "old_text": "two", "new_text": "2"},
                      {"path": "new/c.txt", "old_text": "", "new_text": "hi\n"}]}
    edits = ai_tools.plan_edits(repo, args)
    assert [e.rel for e in edits] == ["src/app.py", "src/b.py", "new/c.txt"]
    assert ai_tools.apply_edits(edits).splitlines() == [
        "edited src/app.py (+1 -3 lines)", "edited src/b.py (+1 -1 lines)", "created new/c.txt (+1 -0 lines)"]
    assert (repo / "src" / "b.py").read_bytes() == b"one\r\n2\r\n"  # CRLF kept
    assert (repo / "new" / "c.txt").read_text() == "hi\n"


def test_edit_files_refuses_bad_plans_and_changed_files(repo: Path) -> None:
    ok = {"path": "src/app.py", "old_text": "print", "new_text": "pass"}
    for bad in ({"edits": []}, {"edits": "x"}, {"edits": [{"path": "a"}]},
                {"edits": [{**ok, "path": ".env"}]}, {"edits": [{**ok, "path": ".git/app.py"}]},
                {"edits": [{**ok, "path": "../outside.txt"}]},
                {"edits": [ok, {**ok, "old_text": ""}]},  # creating a file that already has an edit
                {"edits": [ok] * (ai_tools.MAX_EDITS + 1)}):
        with pytest.raises(ValueError):
            ai_tools.plan_edits(repo, bad)
    with pytest.raises(ValueError, match="edit 2"):
        ai_tools.plan_edits(repo, {"edits": [ok, {"path": "src/x.py", "old_text": "nothing", "new_text": "x"}]})
    (repo / "b.txt").write_text("b", encoding="utf-8")
    edits = ai_tools.plan_edits(repo, {"edits": [{"path": "b.txt", "old_text": "b", "new_text": "B"},
                                                 {"path": "n.txt", "old_text": "", "new_text": "n"}]})
    (repo / "b.txt").write_text("changed", encoding="utf-8")  # changed while the user decided
    assert ai_tools.apply_edits(edits).startswith("error: the file changed") and not (repo / "n.txt").exists()


def test_edit_files_rolls_back_files_already_written(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / "a.txt").write_text("a", encoding="utf-8")
    (repo / "b.txt").write_text("b", encoding="utf-8")
    edits = ai_tools.plan_edits(repo, {"edits": [{"path": "a.txt", "old_text": "a", "new_text": "A"},
                                                 {"path": "new.txt", "old_text": "", "new_text": "n"},
                                                 {"path": "b.txt", "old_text": "b", "new_text": "B"}]})
    real = Path.write_text

    def flaky(self: Path, *a, **k):
        if self.name == "b.txt":
            raise PermissionError(13, "denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", flaky)
    out = ai_tools.apply_edits(edits)
    monkeypatch.undo()
    assert out.startswith("error: could not write b.txt") and "put back" in out
    assert (repo / "a.txt").read_text() == "a" and (repo / "b.txt").read_text() == "b"
    assert not (repo / "new.txt").exists()


def test_task_list_validates_items() -> None:
    items = [{"text": " read   the\nfile ", "status": "done"}, {"text": "fix", "status": "in_progress"}]
    assert ai_tools.task_list({"tasks": items}) == [("read the file", "done"), ("fix", "in_progress")]
    for bad in ({}, {"tasks": []}, {"tasks": [{"text": "x", "status": "maybe"}]}, {"tasks": [{"status": "done"}]},
                {"tasks": ["x"]}, {"tasks": [{"text": "x", "status": "done"}] * (ai_tools.MAX_TASKS + 1)}):
        with pytest.raises(ValueError):
            ai_tools.task_list(bad)


def _host(repo: Path, chat):
    from PyQt6.QtCore import QObject
    from PyQt6.QtWidgets import QApplication

    from src.ui.ai_tool_host import AiToolHost

    class Window(QObject):
        _ai_chat = chat
        _state_machine = SimpleNamespace(registry=SimpleNamespace(get_plugin=lambda _n: SimpleNamespace(repo=repo)))

    app = QApplication.instance() or QApplication([])
    return app, AiToolHost(Window(), mcp=SimpleNamespace(server_of=lambda n: None, specs=lambda: [], close=lambda: None))


def _run_on_thread(app, work) -> None:
    thread = threading.Thread(target=work)
    thread.start()
    while thread.is_alive():
        app.processEvents()


def test_host_edit_files_asks_once_with_every_diff_and_task_list_reaches_the_chat(repo: Path) -> None:
    asked: list[str] = []
    shown: list = []

    class Chat:
        def confirm(self, key, text, what, done, danger=""):
            asked.append(text)
            done(True)

        def set_tasks(self, tasks):
            shown.append(tasks)

    app, host = _host(repo, Chat())
    results: list[str] = []

    def work() -> None:
        results.append(host.run("edit_files", {"edits": [{"path": "x.txt", "old_text": "", "new_text": "x"},
                                                         {"path": "y.txt", "old_text": "", "new_text": "y"}]}))
        results.append(host.run("plan_tasks", {"tasks": [{"text": "a", "status": "done"},
                                                         {"text": "b", "status": "pending"}]}))
        results.append(host.run("plan_tasks", {"tasks": []}))

    _run_on_thread(app, work)
    assert len(asked) == 1 and "2 files" in asked[0] and "x.txt" in asked[0] and "y.txt" in asked[0]
    assert (repo / "x.txt").exists() and (repo / "y.txt").exists()
    assert results[1] == "task list shown (1 of 2 done)" and shown == [[("a", "done"), ("b", "pending")]]
    assert results[2].startswith("error:")


def test_edits_outside_the_project_folder_ask_under_their_own_key(repo: Path, tmp_path: Path) -> None:
    from src.tools.ai_tools import skills_dir

    keys: list[str] = []

    class Chat:
        def confirm(self, key, text, what, done, **extra):
            keys.append(key)
            done(True)

    app, host = _host(repo, Chat())
    outside = tmp_path / "elsewhere.txt"
    skill = skills_dir() / "mine" / "SKILL.md"

    def work() -> None:
        host.run("edit_file", {"path": "inside.txt", "old_text": "", "new_text": "x"})
        host.run("edit_file", {"path": str(outside), "old_text": "", "new_text": "x"})
        host.run("edit_file", {"path": str(skill), "old_text": "", "new_text": "x"})
        host.run("edit_files", {"edits": [{"path": "a2.txt", "old_text": "", "new_text": "x"},
                                          {"path": str(tmp_path / "b2.txt"), "old_text": "", "new_text": "x"}]})

    skill.parent.mkdir(parents=True, exist_ok=True)
    _run_on_thread(app, work)
    assert keys == ["edit_file", "edit_outside", "edit_outside", "edit_outside"]  # one file outside: the whole batch
    from src.ui.ai_tool_host import OUTSIDE_GUARDED
    assert "edit_outside" in OUTSIDE_GUARDED


def _py(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def _until(cond, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.05)
    return cond()


def test_background_process_output_is_read_in_pieces_and_stop_kills_it(tmp_path: Path) -> None:
    procs = ai_tools.BackgroundProcs()
    try:
        assert procs.start(tmp_path, _py("import time;print('ready',flush=True);time.sleep(60)")).startswith("started bg1")
        assert _until(lambda: "ready" in procs._procs["bg1"].text)
        first = procs.read("bg1")
        assert first.startswith("running") and "ready" in first
        assert procs.read("bg1").endswith("(no new output)")
        assert "bg1: running" in procs.read("") and procs.snapshot()[0][0] == "bg1"
        assert procs.stop("bg1") == "stopped bg1"
        assert procs.snapshot() == [] and procs.read("bg1").startswith("stopped as asked")
        assert procs.stop("bg1").endswith("already exited")
        assert procs.stop("nope").startswith("error") and procs.read("zzz").startswith("error")
    finally:
        procs.stop_all()


def test_background_processes_are_capped_and_output_is_trimmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_tools, "MAX_BACKGROUND_CHARS", 1000)
    procs = ai_tools.BackgroundProcs()
    try:
        assert procs.start(tmp_path, "  ").startswith("error")
        assert procs.start(tmp_path, _py("print('x'*5000)")).startswith("started")
        assert _until(lambda: not procs._procs["bg1"].running and len(procs._procs["bg1"].text) == 1000)
        out = procs.read("bg1")
        assert out.startswith("exited with code 0") and "characters dropped" in out and out.count("x") == 1000
        assert procs.read("bg1").endswith("(no new output)")
        for _ in range(ai_tools.MAX_BACKGROUND):
            assert procs.start(tmp_path, _py("import time;time.sleep(60)")).startswith("started")
        assert "already running" in procs.start(tmp_path, _py("pass"))
        assert len(procs.snapshot()) == ai_tools.MAX_BACKGROUND
        procs.stop_all()
        assert procs.snapshot() == []
    finally:
        procs.stop_all()


def test_host_background_start_needs_approval_and_guards_destructive(repo: Path) -> None:
    asked: list[tuple[str, str]] = []
    answers = iter([False, True, True])

    class Chat:
        def confirm(self, key, text, what, done, danger=""):
            asked.append((key, danger))
            done(next(answers))

        def watch_procs(self):
            pass

    app, host = _host(repo, Chat())
    results: list[str] = []

    def work() -> None:
        results.append(host.run("start_background", {"command": "echo no"}))
        results.append(host.run("start_background", {"command": _py("import time;time.sleep(60)")}))
        results.append(host.run("start_background", {"command": "del /q x.txt & ping -n 30 127.0.0.1"}))
        results.append(host.run("stop_background", {"id": "bg1"}))
        host.procs.stop_all()

    _run_on_thread(app, work)
    assert results[0] == "error: the user declined this command"
    assert results[1].startswith("started bg1") and results[2].startswith("started bg2") and results[3] == "stopped bg1"
    assert asked == [("start_background", ""), ("start_background", ""), ("start_background", "del")]
    names = {s["function"]["name"] for s in TOOL_SPECS}
    assert {"edit_files", "plan_tasks", "start_background", "read_background", "stop_background"} <= names
    assert {"edit_files", "start_background"} <= ai_tools.APPROVED_TOOLS
    assert "read_background" in ai_tools.LOCAL_DATA_TOOLS


def test_chat_shows_the_task_list_and_running_processes_and_new_chat_clears_them() -> None:
    from PyQt6.QtWidgets import QApplication

    from src.tools.ai_assist import AiAssistPlugin
    from src.ui.ai_chat import AiChat

    app = QApplication.instance() or QApplication([])
    procs = ai_tools.BackgroundProcs()
    chat = AiChat(AiAssistPlugin(), tool_host=SimpleNamespace(specs=[], run=lambda n, a: "", procs=procs))
    try:
        chat.set_tasks([("read", "done"), ("fix", "in_progress"), ("test", "pending")])
        assert not chat._task_list.isHidden() and not chat._panel.isHidden() and chat._task_list.height() == 4 * 17 + 6
        chat._task_list.resize(340, chat._task_list.height())
        chat._task_list.grab()  # paints without error
        from PyQt6.QtCore import QEvent, QPointF, Qt
        from PyQt6.QtGui import QMouseEvent

        def click(y: float) -> None:
            pos = QPointF(20, y)
            chat._task_list.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, pos, Qt.MouseButton.LeftButton,
                                                        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))

        click(60)  # a task row does nothing
        assert chat._task_list.height() == 4 * 17 + 6
        click(8)  # the header folds the list to one row, again unfolds it
        assert chat._task_list.height() == 17 + 6
        chat._task_list.grab()
        chat.set_tasks([("read", "done"), ("fix", "done")])  # new tasks keep the fold
        assert chat._task_list.height() == 17 + 6
        click(8)
        assert chat._task_list.height() == 3 * 17 + 6
        started = procs.start(Path.home(), _py("import time;time.sleep(60)"))
        assert started.startswith("started")
        chat.watch_procs()
        assert not chat._proc_list.isHidden() and chat._proc_list._rows[0][0] == "bg1" and chat._procs_timer.isActive()
        chat._stop_proc("bg1")
        assert _until(lambda: (app.processEvents() or True) and procs.snapshot() == [])
        chat._refresh_procs()
        assert chat._proc_list.isHidden() and not chat._procs_timer.isActive()
        procs.start(Path.home(), _py("import time;time.sleep(60)"))
        chat.new_chat()  # kills everything and clears the checklist
        assert _until(lambda: procs.snapshot() == []) and chat._task_list.isHidden() and chat._tasks == []
    finally:
        procs.stop_all()
        chat.close()


def test_stopped_background_process_reads_as_stopped_not_failed(tmp_path) -> None:
    import sys
    import time

    from src.tools.ai_tools import BackgroundProcs

    procs = BackgroundProcs()
    procs.start(tmp_path, f'"{sys.executable}" -c "import time; time.sleep(60)"')
    time.sleep(0.5)
    assert procs.stop("bg1") == "stopped bg1"
    assert procs.read("bg1").startswith("stopped as asked") and "bg1: stopped" in procs.read(None)
    procs.stop_all()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_stop_kills_children_that_left_the_tree(tmp_path: Path) -> None:
    import subprocess

    pid_file = tmp_path / "pid"
    child = f'"{sys.executable}" -c "import os,time;open(r\'{pid_file}\',\'w\').write(str(os.getpid()));time.sleep(60)"'
    procs = ai_tools.BackgroundProcs()
    try:
        assert procs.start(tmp_path, f'start "" /b {child}').startswith("started bg1")
        assert procs._procs["bg1"].job is not None  # started suspended, joined, resumed
        assert _until(lambda: pid_file.exists() and pid_file.read_text() != "")
        assert _until(lambda: not procs._procs["bg1"].running)  # cmd.exe exited; the child lives on, orphaned

        def alive() -> bool:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid_file.read_text()}", "/NH"],
                                 capture_output=True, text=True).stdout
            return pid_file.read_text() in out

        assert alive()
        assert procs.stop("bg1").endswith("already exited")
        assert _until(lambda: not alive())
    finally:
        procs.stop_all()
