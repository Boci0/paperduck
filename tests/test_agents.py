import threading
import time
from pathlib import Path

import pytest

from src.tools.agents import MAX_RUNNING, Agents, ReadOnlyHost


def _agents(run):
    posts: list[tuple[str, str]] = []
    return Agents(run, lambda text, source: posts.append((source, text))), posts


def test_read_only_host_offers_and_runs_only_read_tools(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    host = ReadOnlyHost(tmp_path)
    assert {s["function"]["name"] for s in host.specs} == {"list_files", "read_file", "find_files", "search_text", "calculate"}
    assert "x = 1" in host.run("read_file", {"path": "a.py"})
    assert host.run("calculate", {"expression": "6*7"}).endswith("42")
    assert host.run("edit_file", {"path": "a.py"}).startswith("error")
    assert host.run("run_command", {"command": "del a.py"}).startswith("error") and (tmp_path / "a.py").exists()


def test_spawned_agents_report_through_the_inbox_and_wait_joins_them() -> None:
    agents, posts = _agents(lambda task, effort, host, cancel: f"found {task} at {effort}")
    cancel = threading.Event()
    assert agents.spawn("the parser", "high", None, cancel).startswith("started sub-agent 1 (high effort)")
    assert agents.spawn("the tests", "weird", None, cancel).startswith("started sub-agent 2 (low effort)")
    said = agents.wait(None, cancel)  # an agent may finish before wait starts: both answers are right
    assert said in ("all 2 sub-agents finished; their reports follow as branch notes",
                    "all 1 sub-agents finished; their reports follow as branch notes") or said.startswith("no sub-agents")
    assert not agents.running()
    assert sorted(posts) == [("agent 1", "task: the parser\nreport: found the parser at high"),
                             ("agent 2", "task: the tests\nreport: found the tests at low")]
    assert agents.wait(None, cancel).startswith("no sub-agents are running")
    assert agents.spawn("  ", "low", None, cancel).startswith("error")


def test_running_agents_are_capped_and_stop_ends_a_wait() -> None:
    release = threading.Event()

    def slow(task, effort, host, cancel):
        while not release.is_set() and not cancel.is_set():
            time.sleep(0.01)
        if cancel.is_set():
            raise ValueError("stopped")
        return "late"

    agents, posts = _agents(slow)
    cancel = threading.Event()
    for i in range(MAX_RUNNING):
        assert agents.spawn(f"t{i}", "low", None, cancel).startswith("started")
    assert agents.spawn("one more", "low", None, cancel).startswith(f"error: {MAX_RUNNING} sub-agents")
    assert agents.wait(0.2, cancel).endswith("still running after the wait")
    threading.Timer(0.1, cancel.set).start()
    t = time.monotonic()
    assert agents.wait(None, cancel) == "stopped" and time.monotonic() - t < 3
    for _ in range(200):
        if len(posts) == MAX_RUNNING:
            break
        time.sleep(0.01)
    assert all(text.endswith("report: (failed: stopped)") for _src, text in posts)  # never silent


def test_an_agent_crash_still_reports() -> None:
    def broken(*_a):
        raise KeyError("x")

    agents, posts = _agents(broken)
    agents.spawn("t", "low", None, threading.Event())
    agents.wait(5, threading.Event())
    assert posts == [("agent 1", "task: t\nreport: (failed: KeyError)")]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")  # the raise is the point
def test_a_failing_notify_still_ends_the_agent() -> None:
    def notify(_n, kind, _text):
        if kind == "done":
            raise RuntimeError("card closed")

    agents = Agents(lambda *_a: "ok", lambda *_a: None, notify)
    agents.spawn("t", "low", None, threading.Event())
    t = time.monotonic()
    agents.wait(5, threading.Event())
    assert not agents.running() and time.monotonic() - t < 3
    for thread in threading.enumerate():  # let its raise land inside this test, where the warning is filtered
        if "_work" in thread.name:
            thread.join(2)


def test_an_agent_gets_a_tool_budget_and_reports_progress(tmp_path: Path) -> None:
    from src.tools.agents import BUDGET_NOTE, MAX_AGENT_CALLS

    seen: list[str] = []
    host = ReadOnlyHost(tmp_path, seen.append)
    outs = [host.run("list_files", {"path": ""}) for _ in range(MAX_AGENT_CALLS + 2)]
    assert BUDGET_NOTE not in outs[MAX_AGENT_CALLS - 1] and outs[-1] == outs[-2] == BUDGET_NOTE
    assert seen[0] == "list_files()" and len(seen) == MAX_AGENT_CALLS + 2

    events: list[tuple[int, str, str]] = []

    def run(task, effort, h, cancel):
        h.run("calculate", {"expression": "1+1"})
        return "two"

    agents = Agents(run, lambda *_a: None, lambda n, kind, text: events.append((n, kind, text)))
    agents.spawn("add", "max", None, threading.Event())
    agents.wait(5, threading.Event())
    assert events == [(1, "start", "max\nadd"), (1, "tool", "calculate(1+1)"), (1, "done", "two")]


def test_research_tools_wait_while_agents_run() -> None:
    """Live, the main answer redid its agents' reading instead of waiting: research is refused until they finish."""
    release = threading.Event()

    def slow(task, effort, host, cancel):
        release.wait(5)
        return "found it"

    agents, _posts = _agents(slow)
    assert agents.blocked("read_file") is None  # no agents, nothing refused
    agents.spawn("find the parser", "low", None, threading.Event())
    for name in ("read_file", "search_text", "list_files", "web_search", "fetch_page", "run_powershell", "run_command"):
        refused = agents.blocked(name)
        assert refused.startswith("error: 1 sub-agents are still researching") and "find the parser" in refused
        assert "wait_agents" in refused
    for name in ("calculate", "mouse_click", "spawn_agent", "wait_agents", "edit_file"):
        assert agents.blocked(name) is None
    release.set()
    agents.wait(5, threading.Event())
    assert agents.blocked("read_file") is None


def test_after_its_agents_report_an_answer_gets_a_few_check_reads() -> None:
    """Live, after the reports came in the main answer re-read everything (25 calls): it keeps MAX_CHECK_CALLS."""
    from src.tools.agents import MAX_CHECK_CALLS

    agents, _posts = _agents(lambda *_a: "found it")
    mine, other = threading.Event(), threading.Event()
    agents.spawn("find the parser", "low", None, mine)
    agents.wait(5, mine)
    assert all(agents.blocked("read_file", mine) is None for _ in range(MAX_CHECK_CALLS))
    assert "check reads" in agents.blocked("search_text", mine) and "check reads" in agents.blocked("web_search", mine)
    assert agents.blocked("run_command", mine) is None  # tests after an edit still run
    assert agents.blocked("read_file", other) is None and agents.blocked("read_file") is None  # an answer without agents
    agents.spawn("one more part", "low", None, mine)  # more research: a fresh budget
    agents.wait(5, mine)
    assert agents.blocked("read_file", mine) is None


def test_ask_refuses_a_blocked_tool_before_running_it(monkeypatch) -> None:
    from src.tools import ai_assist

    monkeypatch.setattr(ai_assist, "web_search", lambda q: (_ for _ in ()).throw(AssertionError("ran")))

    class Host:
        specs: list = []

        def blocked(self, name):
            return "error: wait" if name == "web_search" else None

        def run(self, name, args):
            raise AssertionError("ran")

    assert ai_assist._run_tool("web_search", '{"query": "x"}', Host()) == "error: wait"


def test_agent_sheets_deal_like_a_deck_and_move_together() -> None:
    from PyQt6.QtCore import QPoint, QRect, Qt
    from PyQt6.QtWidgets import QApplication, QWidget

    from src.ui.agent_sheets import FRONT_MIN_H, PEEK, STEP_X, AgentSheets

    app = QApplication.instance() or QApplication([])

    class Card(QWidget):
        def moveEvent(self, _event) -> None:  # what AiChat.moveEvent does
            stack.card_moved()

    card = Card(None, Qt.WindowType.FramelessWindowHint)  # like the chat card: pos() and geometry() agree
    stack = AgentSheets(card)
    card.setGeometry(QRect(400, 400, 380, 300))
    card.show()
    slot = lambda k: QPoint(400 + STEP_X * k, 400 - PEEK * k)  # noqa: E731
    try:
        for n in (1, 2, 3):
            stack.on_event(n, "start", f"low\ntask {n}")
        stack.on_event(1, "tool", "search_text(parse)")
        stack.on_event(1, "done", "src/parse.py:12")
        one, two, three = stack.sheets
        assert [w.pos() for w in stack.order] == [slot(0), slot(1), slot(2), slot(3)] and stack.order[0] is card
        assert one.calls == 1 and one.status == "done" and "src/parse.py:12" in one.report_text.toPlainText()
        stack.clicked(two)  # the middle sheet: dealt to the front, the rest keep their order behind it
        app.processEvents()
        assert stack.order == [two, card, one, three] and stack.front is two
        assert [w.pos() for w in stack.order] == [slot(0), slot(1), slot(2), slot(3)]
        assert two.height() == FRONT_MIN_H == one.height()  # sheets are sized like the front one
        stack.drag_by(QPoint(30, 20), two)  # dragging the front sheet moves the whole deck
        app.processEvents()
        assert card.pos() == slot(1) + QPoint(30, 20) and three.pos() == slot(3) + QPoint(30, 20)
        stack.clicked(two)  # a click on the front sheet brings the card back
        app.processEvents()
        assert stack.order[0] is card and card.pos() == slot(0) + QPoint(30, 20)
        card.move(card.pos() + QPoint(-30, -20))  # the card dragged by itself: the deck follows
        app.processEvents()
        assert two.pos() == slot(1)
        stack.close_sheet(one)  # the rest close up
        assert stack.order == [card, two, three] and three.pos() == slot(2)
        from PyQt6.QtTest import QTest  # a click on a back sheet's Close closes it rather than dealing it
        QTest.mouseClick(three, Qt.MouseButton.LeftButton, pos=three.close_btn.geometry().center())
        app.processEvents()
        assert stack.order == [card, two] and stack.sheets == [two]
    finally:
        stack.close_all()
        card.close()


def test_screen_tools_leave_out_and_click_through_the_agent_sheets() -> None:
    from types import SimpleNamespace

    from src.ui.ai_tool_host import _ours

    shown = lambda: SimpleNamespace(isVisible=lambda: True)  # noqa: E731
    pet, card, sheet = shown(), shown(), shown()
    hidden_sheet = SimpleNamespace(isVisible=lambda: False)
    card._sheets = SimpleNamespace(sheets=[sheet, hidden_sheet])
    pet._ai_chat = card
    assert _ours(pet) == [pet, card, sheet]


def test_agent_host_views_images_and_hands_the_picture_over_once(tmp_path: Path) -> None:
    seen: list[tuple[Path | None, dict]] = []

    def view(root, args):
        seen.append((root, args))
        return "a.png: 2x2 px", "data:image/png;base64,AAAA"

    assert "view_image" not in {s["function"]["name"] for s in ReadOnlyHost(tmp_path).specs}
    assert ReadOnlyHost(tmp_path).run("view_image", {"path": "a.png"}).startswith("error")
    host = ReadOnlyHost(tmp_path, images=["data:image/png;base64,BBBB"], view=view)
    assert "view_image" in {s["function"]["name"] for s in host.specs} and host.images == ["data:image/png;base64,BBBB"]
    assert host.take_image() == ""
    assert host.run("view_image", {"path": "a.png"}) == "a.png: 2x2 px"
    assert seen == [(tmp_path, {"path": "a.png"})]
    assert host.take_image() == "data:image/png;base64,AAAA" and host.take_image() == ""


def test_spawn_hands_shared_pictures_and_the_viewer_to_the_agent_host() -> None:
    got = []
    agents, _posts = _agents(lambda task, effort, host, cancel: got.append(host) or "ok")
    view = lambda root, args: ("", "")  # noqa: E731
    assert agents.spawn("look", "low", None, threading.Event(), False, ["data:image/png;base64,AA"], view).startswith("started")
    agents.wait(None, threading.Event())
    for _ in range(200):
        if got:
            break
        time.sleep(0.01)
    assert got[0].images == ["data:image/png;base64,AA"] and got[0]._view is view


def test_spawn_spec_offers_share_images_and_the_prompt_names_view_image() -> None:
    from src.tools.agents import AGENT_PROMPT, SPAWN_SPEC
    props = SPAWN_SPEC["function"]["parameters"]["properties"]
    assert props["share_images"]["type"] == "boolean" and "share_images" not in SPAWN_SPEC["function"]["parameters"]["required"]
    assert "view_image" in AGENT_PROMPT and "share_images" in SPAWN_SPEC["function"]["description"]


def test_web_calls_count_against_the_agent_budget_and_show_on_its_sheet(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.tools import ai_assist
    from src.tools.agents import BUDGET_NOTE, MAX_AGENT_CALLS

    monkeypatch.setattr(ai_assist, "web_search", lambda q: f"results for {q}")
    seen: list[str] = []
    host = ReadOnlyHost(None, seen.append)
    outs = [ai_assist._run_tool_raw("web_search", {"query": "ducks"}, host, None) for _ in range(MAX_AGENT_CALLS + 1)]
    assert outs[0] == outs[MAX_AGENT_CALLS - 1] == "results for ducks" and outs[-1] == BUDGET_NOTE
    assert len(seen) == host.calls == MAX_AGENT_CALLS + 1 and "ducks" in seen[0]


def test_stop_all_ends_running_agents_drops_their_reports_and_restarts_the_count() -> None:
    def slow(task, effort, host, cancel):
        if not cancel.wait(5):
            return "late"
        raise ValueError("stopped")

    agents, posts = _agents(slow)
    old = threading.Event()
    assert agents.spawn("old chat", "low", None, old).startswith("started sub-agent 1")
    agents.stop_all()
    assert old.is_set() and not agents.running() and agents.blocked("read_file") is None
    time.sleep(0.2)
    assert posts == []  # nothing of the old chat reaches the next one
    assert agents.spawn("new chat", "low", None, threading.Event()).startswith("started sub-agent 1")
    agents.stop_all()


def test_new_chat_stops_agents_and_empties_the_branch_inbox() -> None:
    from types import SimpleNamespace

    from PyQt6.QtWidgets import QApplication

    from src.tools import ai_assist
    from src.ui import ai_chat

    app = QApplication.instance() or QApplication([])  # noqa: F841
    stopped: list[bool] = []
    host = SimpleNamespace(specs=[], run=lambda n, a: "", procs=None, agents=SimpleNamespace(stop_all=lambda: stopped.append(True)))
    chat = ai_chat.AiChat(ai_assist.AiAssistPlugin(), tool_host=host)
    try:
        chat.add_branch_note("task: x, report: old", "agent 1")
        assert chat._branch_q
        chat.new_chat()
        assert stopped == [True] and chat._branch_q == []
    finally:
        chat.deleteLater()


def test_an_edit_refills_the_check_reads_but_only_for_an_answer_with_agents() -> None:
    from src.tools.agents import MAX_CHECK_CALLS

    agents, _posts = _agents(lambda *_a: "found it")
    mine, other = threading.Event(), threading.Event()
    agents.spawn("find the parser", "low", None, mine)
    agents.wait(5, mine)
    assert all(agents.blocked("read_file", mine) is None for _ in range(MAX_CHECK_CALLS))
    assert "check reads" in agents.blocked("read_file", mine)
    assert agents.blocked("edit_file", mine) is None  # reads after an edit are implementation work
    assert all(agents.blocked("read_file", mine) is None for _ in range(MAX_CHECK_CALLS))
    assert "check reads" in agents.blocked("read_file", mine)
    assert agents.blocked("edit_files", mine) is None and agents.blocked("read_file", mine) is None
    assert agents.blocked("edit_file", other) is None and other not in agents._checks  # no entry is made


def test_a_long_report_keeps_its_head_and_its_tail() -> None:
    from src.tools.agents import MAX_REPORT_CHARS

    agents, posts = _agents(lambda *_a: "H" * 6000 + "M" * 6000 + "OPEN: the tail")
    cancel = threading.Event()
    agents.spawn("t", "low", None, cancel)
    agents.wait(5, cancel)
    text = posts[0][1].split("report: ", 1)[1]
    assert len(text) <= MAX_REPORT_CHARS and text.startswith("H" * 5500) and text.endswith("OPEN: the tail")
    assert "[...cut...]" in text
