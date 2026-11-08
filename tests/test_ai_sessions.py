"""Saved AI chat sessions: atomic JSON files, caps, no secrets or image bytes, corrupt files skipped, resume."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools import ai_assist, ai_sessions
from src.tools.ai_sessions import (MAX_SESSION_BYTES, MAX_SESSIONS, delete_sessions, list_sessions, load_session,
                                   new_id, save_session, sessions_dir)

HISTORY = [{"role": "user", "content": "fix the bug\n\n[File: a.py]\nprint(1)", "images": ["data:image/png;base64,AAAA"]},
           {"role": "assistant", "content": "done", "tools": [("run_command(pytest)", "exit code 0")]}]


def test_sessions_live_under_localappdata_and_roundtrip(tmp_path: Path) -> None:
    assert sessions_dir().parts[-2:] == ("Paperduck", "sessions")
    sid = new_id()
    assert save_session(sid, HISTORY, [("a", "done"), ("b", "pending")], created=5.0)
    session = load_session(sid)
    assert session is not None and session.title == "fix the bug" and session.created == 5.0
    assert session.history[1]["tools"] == [("run_command(pytest)", "exit code 0")]
    assert session.tasks == [("a", "done"), ("b", "pending")]
    assert [s.id for s in list_sessions()] == [sid]
    assert list(sessions_dir().glob("*.tmp")) == []


def test_images_and_key_like_text_are_never_written() -> None:
    sid = new_id()
    key = "sk-" + "a1B2c3D4" * 4
    history = [{"role": "user", "content": f"my key is {key} and Bearer abcdefghijklmnopqrstuvwxyz0123", "images": ["data:AAAA"]},
               {"role": "assistant", "content": "ok", "tools": [("read_file(x)", f"token ghp_{'x' * 36}")]}]
    assert save_session(sid, history)
    raw = (sessions_dir() / f"{sid}.json").read_text("utf-8")
    assert "images" not in raw and "AAAA" not in raw and key not in raw and "ghp_" not in raw
    assert "abcdefghijklmnopqrstuvwxyz0123" not in raw and "Bearer abc" not in raw and "[key removed]" in raw


def test_empty_and_bad_ids_are_not_saved_or_loaded(tmp_path: Path) -> None:
    assert not save_session(new_id(), []) and not save_session("../evil", HISTORY)
    assert load_session("../evil") is None and load_session("0" * 12) is None
    assert list_sessions() == []


def test_corrupt_or_odd_files_are_skipped() -> None:
    good = new_id()
    save_session(good, HISTORY)
    root = sessions_dir()
    (root / ("a" * 12 + ".json")).write_text("{not json", encoding="utf-8")
    (root / ("b" * 12 + ".json")).write_text(json.dumps({"id": "b" * 12, "history": [{"role": "robot", "content": "x"}]}),
                                             encoding="utf-8")
    (root / ("c" * 12 + ".json")).write_text(json.dumps({"id": "d" * 12, "history": []}), encoding="utf-8")  # id mismatch
    (root / ("e" * 12 + ".json")).write_text(json.dumps({"id": "e" * 12, "history": [
        {"role": "assistant", "content": "x", "tools": [["only one"]]}]}), encoding="utf-8")
    (root / ("f" * 12 + ".json")).write_bytes(b"\xff\xfe\x00")
    (root / "notes.json").write_text("[]", encoding="utf-8")
    assert [s.id for s in list_sessions()] == [good]
    assert load_session("a" * 12) is None


def test_folder_keeps_the_newest_sessions_only(monkeypatch: pytest.MonkeyPatch) -> None:
    ids = []
    for n in range(MAX_SESSIONS + 3):
        sid = new_id()
        ids.append(sid)
        save_session(sid, [{"role": "user", "content": f"chat {n}"}])
        import os
        path = sessions_dir() / f"{sid}.json"
        os.utime(path, (1000 + n, 1000 + n))  # distinct, increasing ages even on a coarse clock
    assert len(list(sessions_dir().glob("*.json"))) == MAX_SESSIONS
    assert load_session(ids[0]) is None and load_session(ids[-1]) is not None


def test_a_session_over_the_byte_cap_loses_its_oldest_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_sessions, "MAX_SESSION_BYTES", 5000)
    history = []
    for n in range(20):
        history += [{"role": "user", "content": f"q{n} " + "x" * 400}, {"role": "assistant", "content": f"a{n} " + "y" * 400}]
    sid = new_id()
    assert save_session(sid, history)
    session = load_session(sid)
    assert session is not None and (sessions_dir() / f"{sid}.json").stat().st_size <= 5000
    assert session.history[0]["role"] == "user" and session.history[-1]["content"].startswith("a19")
    assert len(session.history) < 40 and MAX_SESSION_BYTES == 2 * 1024 * 1024


def test_delete_sessions_removes_everything() -> None:
    save_session(new_id(), HISTORY)
    delete_sessions()
    assert list_sessions() == []


_APP = None


def _chat():
    from PyQt6.QtWidgets import QApplication

    from src.ui.ai_chat import AiChat

    global _APP
    _APP = QApplication.instance() or QApplication([])  # kept: a collected QApplication crashes Qt
    plugin = ai_assist.AiAssistPlugin()
    plugin.base_url, plugin.model = "https://example.invalid/v1", "m1"
    return AiChat(plugin)


def test_chat_saves_after_answers_and_resumes_with_thread_tasks_and_compaction(monkeypatch: pytest.MonkeyPatch) -> None:
    chat = _chat()
    try:
        long_text = "z" * 60000
        for n in range(12):  # a long chat: past the auto-compact threshold once resumed
            chat._history += [{"role": "user", "content": f"q{n}\n\n[File: big.txt]\n{long_text}"},
                              {"role": "assistant", "content": f"a{n}", "tools": [("read_file(x)", "out")]}]
        chat._tasks = [("step", "in_progress")]
        chat._save_session()
        sid = chat._sid
        assert sid is not None and len(list_sessions()) == 1
        chat.new_chat()
        assert chat._sid is None and chat._history == [] and not list_sessions()[0].history == []
        assert chat.resume(sid)
        assert chat._sid == sid and len(chat._history) == 24 and chat._tasks == [("step", "in_progress")]
        assert not chat._task_list.isHidden() and chat._thread_layout.count() == 24
        assert chat._history[1]["tools"] == [("read_file(x)", "out")]

        asked = []

        class Reply:
            text = "SUMMARY"

        monkeypatch.setattr(ai_assist, "ask", lambda *a, **k: asked.append(a) or Reply())
        sent, cut, summary = ai_assist.compact_history("u", "k", "m", chat._history)
        assert ai_assist.history_chars(chat._history) > ai_assist.COMPACT_AT and cut > 0 and summary == "SUMMARY"
        assert sent[0]["content"].startswith(ai_assist.COMPACT_MARK)

        chat._history.append({"role": "user", "content": "more"})
        chat._history.append({"role": "assistant", "content": "ok"})
        chat._save_session()
        assert len(load_session(sid).history) == 26  # same session file, updated
        assert chat.resume("0" * 12) is False  # an unreadable saved chat leaves the current one alone
        assert len(chat._history) == 26
    finally:
        chat.close()


def test_resume_shows_compaction_and_attachments_cleanly() -> None:
    chat = _chat()
    try:
        sid = new_id()
        save_session(sid, [*ai_assist.summary_turns("what happened"),
                           {"role": "user", "content": "[File: notes.txt]\nbody"},
                           {"role": "assistant", "content": "read it"}])
        assert chat.resume(sid)
        assert len(chat._history) == 4
        assert chat._thread_layout.count() == 3  # note, attachment-only bubble, reply (the ack is not shown)
    finally:
        chat.close()


def test_chat_saved_before_the_rename_still_shows_its_summary() -> None:
    from src.tools.ai_sessions import new_id, save_session
    sid = new_id()
    save_session(sid, [{"role": "user", "content": "[Earlier conversation, compacted by Paperduck into this summary:]\nold"},
                       {"role": "assistant", "content": "Noted. Continuing from that summary."},
                       {"role": "user", "content": "next"}, {"role": "assistant", "content": "ok"}])
    chat = _chat()
    try:
        assert chat.resume(sid)
        from src.ui.chat_widgets import ChatText
        rows = [chat._thread_layout.itemAt(i).widget() for i in range(chat._thread_layout.count())]
        shown = " | ".join(w.toPlainText() for row in rows for w in row.findChildren(ChatText))
        assert "compacted into a summary" in shown and "compacted by Paperduck" not in shown
    finally:
        chat.close()


def test_copy_links_put_the_exact_prompt_and_reply_on_the_clipboard() -> None:
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtTest import QTest

    from src.ui.chat_widgets import _ClickLabel

    chat = _chat()
    clip = QGuiApplication.clipboard()
    saved = clip.text()
    try:
        prompt = "fix  this\n\ttabbed line ü 🦆"
        reply = "# Title\n\n**bold** and `code`\n\n```py\nprint('x')  \n```\n- item\n\ntrailing  "
        chat._add_row("user", prompt)
        chat._add_row("assistant", reply, markdown=True)
        chat._add_row("note", "a note has no copy link")
        links = [w for w in chat.findChildren(_ClickLabel) if w.text() == "Copy"]
        assert len(links) == 2
        for link, expected in zip(links, (prompt, reply)):
            QTest.mouseClick(link, Qt.MouseButton.LeftButton)
            assert clip.text() == expected and link.text() == "Copied"
    finally:
        clip.setText(saved)
        chat.deleteLater()


def test_delete_this_chat_removes_only_the_open_chat() -> None:
    keep = new_id()
    save_session(keep, HISTORY)
    chat = _chat()
    try:
        chat._history += [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a", "tools": []}]
        chat._save_session()
        sid = chat._sid
        assert sid is not None and {s.id for s in list_sessions()} == {sid, keep}
        chat._delete_current()
        assert [s.id for s in list_sessions()] == [keep] and load_session(sid) is None
        assert chat._sid is None and chat._history == [] and chat._thread_layout.count() == 0
        ai_sessions.delete_session("../../evil")  # not an id: ignored, never a path
        assert [s.id for s in list_sessions()] == [keep]
    finally:
        chat.deleteLater()


def test_compacted_turns_are_archived_as_searchable_text(tmp_path: Path) -> None:
    from src.tools.ai_sessions import archive_turns, delete_session

    turns = [{"role": "user", "content": "[Earlier conversation, compacted by Paperduck into this summary:]\nold"},
             {"role": "user", "content": "the build key is sk-" + "a" * 30},
             {"role": "assistant", "content": "ran it", "tools": [("run_command", "exit 0")]}]
    path = archive_turns("abc123abc123", turns, tmp_path)
    text = path.read_text("utf-8")
    assert "## user\n[key removed]" in text.replace("the build key is ", "") or "[key removed]" in text
    assert "old" not in text and "[tool run_command] exit 0" in text  # an earlier summary is not archived again
    assert archive_turns("abc123abc123", [], tmp_path) is None and archive_turns("bad id", turns, tmp_path) is None
    delete_session("abc123abc123", tmp_path)
    assert not path.exists()


def test_summary_points_at_the_archive_and_keeps_tasks_verbatim() -> None:
    first = ai_assist.summary_turns("did things", "C:/x/abc.archive.txt", [("ship 2.0.5", "doing")])[0]["content"]
    assert "- [doing] ship 2.0.5" in first and "C:/x/abc.archive.txt" in first
    assert ai_assist.summary_turns("plain")[0]["content"].endswith("plain")
