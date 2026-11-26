from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.tools import ai_assist, ai_tools, computer
from src.tools.agents import Agents, ReadOnlyHost
from src.ui.ai_tool_host import AiToolHost


def test_pillar1_offload_and_structured_compact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai_assist, "scratch_dir", lambda: tmp_path)
    huge = "HEAD\n" + ("row data line\n" * 2000) + "TAIL\n"
    offloaded = ai_assist._offload_output("run_command", huge)
    assert len(offloaded) < len(huge)
    assert "HEAD" in offloaded and "TAIL" in offloaded and "saved to" in offloaded
    obs_files = list((tmp_path / "obs").glob("*.txt"))
    assert len(obs_files) == 1 and obs_files[0].read_text("utf-8") == huge
    assert ai_assist._offload_output("read_file", huge) == huge
    assert "Goal:" in ai_assist.COMPACT_PROMPT and "Failed Attempts" in ai_assist.COMPACT_PROMPT


def test_pillar2_deferred_tools_batch_read_and_query_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("name,dept,score\nAda,eng,95\nBob,sales,70\nCara,eng,88\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=123\n", encoding="utf-8")

    out = ai_tools.batch_read(tmp_path, {"items": [
        {"path": "a.py", "outline": True},
        {"path": ".env"},
        {"path": "b.csv", "start_line": 1, "end_line": 3},
    ]})
    assert "def alpha()" in out
    assert "secret file refused" in out and "SECRET=123" not in out
    assert "Ada,eng,95" in out

    q = json.loads(ai_tools.query_files(tmp_path, {
        "paths": ["b.csv"],
        "where": "dept == 'eng' and int(score) >= 85",
        "group_by": "dept",
        "agg": "sum",
        "agg_field": "score",
    }))
    assert q["matched"] == 1 and q["rows"][0]["sum_score"] == 183.0

    bad = ai_tools.query_files(tmp_path, {"paths": [".env"]})
    assert bad.startswith("error: secret file refused")

    groups, msg = ai_tools.search_tools_catalog("mouse click")
    assert "computer_control" in groups and "mouse_click" in msg
    legacy_groups, _ = ai_tools.search_tools_catalog(group="computer")
    assert legacy_groups == {"screen_inspect", "computer_control"}
    assert ai_tools.infer_tool_groups("please click the save button") == {"computer_control"}


def test_pillar3_syntax_discovery_and_verify_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bad_toml = ai_tools.Edit(tmp_path / "pyproject.toml", "pyproject.toml", "", "a = [\n", False, "")
    assert "syntax check found issues" in ai_tools.syntax_check_edits([bad_toml])
    bad_svg = ai_tools.Edit(tmp_path / "icon.svg", "icon.svg", "", "<svg><rect></svg>", False, "")
    assert "syntax check found issues" in ai_tools.syntax_check_edits([bad_svg])

    (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    ws = ai_tools.discover_workspace(tmp_path)
    assert "project=python" in ws and "AGENTS.md" in ws

    host = SimpleNamespace(edit_gen=1, verified_gen=0, edited_code_files=["app.py"], open_tasks=1)
    assert "unverified edits to app.py" in AiToolHost.verify_open.fget(host)
    assert "1 open item(s)" in AiToolHost.verify_open.fget(host)
    host.verified_gen = 1
    host.open_tasks = 0
    assert AiToolHost.verify_open.fget(host) == ""


def test_pillar4_semantic_loop_and_closest_hint_snippet(tmp_path: Path) -> None:
    text = "alpha = 1\nbeta = 2\ngamma = 3\ndelta = 4\n"
    hint = ai_tools._closest_hint(text, "beta = 999\ngamma = 3")
    assert "closest match near lines" in hint and "2: beta = 2" in hint

    state = [0, 0, None, [], None, set(), []]
    n1 = ai_assist._steer("run_powershell", 'run_powershell{"path":"ps_aa11.ps1"}', "error: line 1", state)
    n2 = ai_assist._steer("run_powershell", 'run_powershell{"path":"ps_bb22.ps1"}', "error: line 2", state)
    assert n1 == "" and n2 == ""
    state[0] = 1
    n3 = ai_assist._steer("run_powershell", 'run_powershell{"path":"ps_cc33.ps1"}', "error: line 3", state)
    assert "same failing/ineffective pattern" in n3


def test_pillar5_multi_role_subagents_and_resume(tmp_path: Path) -> None:
    (tmp_path / "mod_a.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "mod_b.py").write_text("y = 2\n", encoding="utf-8")
    (tmp_path / ".env").write_text("K=1\n", encoding="utf-8")

    worker = ReadOnlyHost(tmp_path, role="worker", allowed_paths=[tmp_path / "mod_a.py"])
    assert "file_outline" in {s["function"]["name"] for s in worker.specs}
    assert "edit_file" in {s["function"]["name"] for s in worker.specs}
    assert worker.run("edit_file", {"path": "mod_a.py", "old_text": "x = 1", "new_text": "x = 10"}).startswith("staged")
    assert len(worker.staged_edits) == 1
    assert "outside this worker's allowed_paths" in worker.run("edit_file", {"path": "mod_b.py", "old_text": "y = 2", "new_text": "y = 9"})
    assert "outside this worker's allowed_paths" in worker.run("edit_file", {"path": ".env", "old_text": "K=1", "new_text": "K=2"})

    verifier = ReadOnlyHost(tmp_path, role="verifier", cmd_runner=lambda _r, a: f"ran:{a.get('command')}")
    assert verifier.run("run_command", {"command": "pytest -q"}) == "ran:pytest -q"

    seen_prior: list[list] = []

    def runner(task, effort, host, cancel):
        seen_prior.append(list(host.prior_messages))
        return f"answer:{task}"

    agents = Agents(runner, lambda *_a: None)
    cancel = threading.Event()
    assert agents.spawn("first", "low", tmp_path, cancel, role="explorer").startswith("started")
    agents.wait(5, cancel)
    assert agents.spawn("second", "low", tmp_path, cancel, role="explorer", agent_id=1).startswith("started")
    agents.wait(5, cancel)
    assert seen_prior[1] == [{"role": "user", "content": "first"}, {"role": "assistant", "content": "answer:first"}]


def test_pillar6_uia_notes_scoped_memory_and_eval_export(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.mine_failures import Case, export_eval_cases
    from src.tools import warm_ps

    monkeypatch.setattr(warm_ps, "run", lambda kind, script, params=None, timeout=10.0: (
        "Edit\tSearch (val=abc)" if "FocusedElement" in script else "Button\tSave\tdisabled"
    ))
    assert computer.uia_focus_note() == " Focus: [Edit] Search (val=abc)."
    assert computer.uia_zero_change_hint(10, 20) == ' UIA: [Button] "Save" is disabled.'

    mem = tmp_path / "memory.md"
    mem.write_text(
        "- Always use pytest\n"
        "- [project:alpha] Alpha uses port 9001\n"
        "- [project:beta] Beta uses port 8002\n",
        encoding="utf-8",
    )
    prompt_alpha = ai_tools.memory_prompt(mem, "alpha check")
    assert "Always use pytest" in prompt_alpha and "port 9001" in prompt_alpha and "port 8002" not in prompt_alpha
    assert "1 off-topic scoped notes omitted" in prompt_alpha
    assert "port 8002" in ai_tools.search_memory(mem, "8002")

    c = Case("s1", "fix sk-1234567890123456789012345", [("run_command(x)", "error: bad")], "sorry", "still wrong",
             failed=[("run_command(x)", "error: bad")], retries=[], stopped=False)
    exported = export_eval_cases([c])
    assert exported[0]["name"] == "mined_1" and "[key removed]" in exported[0]["prompt"]


def test_dynamic_toolsets_v2_partition_and_prewarm() -> None:
    all_names = [s["function"]["name"] for s in ai_tools.TOOL_SPECS]
    grouped_lists = list(ai_tools.TOOL_GROUPS.values())
    grouped = set().union(*grouped_lists)
    assert len(all_names) == len(set(all_names))
    assert not (ai_tools.CORE_TOOL_NAMES & grouped)
    assert sum(len(g) for g in grouped_lists) == len(grouped)
    assert ai_tools.CORE_TOOL_NAMES | grouped == set(all_names)
    assert ai_tools.infer_tool_groups("fix the type error in the app UI server on port 8000") == set()


def test_dynamic_toolsets_v2_append_only_and_unified_search(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtCore import QObject
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])

    def spec(name: str, desc: str = "d") -> dict:
        return {"type": "function", "function": {"name": name, "description": desc, "parameters": {}}}

    mcp = [spec("playwright__click", "click browser"), spec("context7__docs", "fetch docs")]
    hub = SimpleNamespace(
        specs=lambda: mcp,
        server_of=lambda n: n.partition("__")[0] if "__" in n else None,
        close=lambda: None,
    )
    host = AiToolHost(QObject(), mcp=hub)
    base_names = [s["function"]["name"] for s in host.specs[:-1]]

    host.prewarm_groups("take a screenshot")
    names_1 = [s["function"]["name"] for s in host.specs[:-1]]
    assert names_1[:len(base_names)] == base_names
    assert "screen_map" in names_1 and "mouse_click" not in names_1

    # Monotonic: a coding follow-up must not drop previously loaded groups or reorder prefix
    host.prewarm_groups("fix the type error in main.py")
    names_2 = [s["function"]["name"] for s in host.specs[:-1]]
    assert names_2 == names_1

    # Unified search_tools activates both built-in groups and MCP servers at the tail
    out = host.run("search_tools", {"query": "docs"})
    assert "Activated MCP server(s): context7" in out
    names_3 = [s["function"]["name"] for s in host.specs[:-1]]
    assert names_3[:len(names_2)] == names_2
    assert "context7__docs" in names_3 and "playwright__click" not in names_3

    host.clear_loaded_tools()
    assert [s["function"]["name"] for s in host.specs[:-1]] == base_names


def test_claude_dynamic_tools_when_not_thinking(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtCore import QObject
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])

    def spec(name: str) -> dict:
        return {"type": "function", "function": {"name": name, "description": "d", "parameters": {}}}

    mcp = [spec("fake__ping")]
    hub = SimpleNamespace(
        specs=lambda: mcp,
        server_of=lambda n: n.partition("__")[0] if "__" in n else None,
        close=lambda: None,
    )
    host = AiToolHost(QObject(), mcp=hub)
    seen_tools: list[list[str]] = []
    calls = iter([
        {
            "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "t1", "name": "search_tools", "input": {"group": "design_docs"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
        {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "done"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        },
    ])

    def fake_request(_url, _key, _path, body, *_a, **_kw):
        seen_tools.append([t["name"] for t in body.get("tools", [])])
        return next(calls)

    monkeypatch.setattr(ai_assist, "_request", fake_request)
    reply = ai_assist._post_anthropic(
        "https://api.anthropic.com/v1", "k", "claude-x",
        [{"role": "user", "content": "hi"}], thinking="off", timeout=10, host=host, web=False,
    )
    assert reply.text == "done"
    assert "make_sheet" not in seen_tools[0] and "fake__ping" not in seen_tools[0]
    assert "make_sheet" in seen_tools[1] and "fake__ping" not in seen_tools[1]

    seen_thinking: list[list[str]] = []
    monkeypatch.setattr(ai_assist, "_request", lambda _u, _k, _p, body, *_a, **_kw: (
        seen_thinking.append([t["name"] for t in body.get("tools", [])]) or
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 5, "output_tokens": 2}}
    ))
    ai_assist._post_anthropic(
        "https://api.anthropic.com/v1", "k", "claude-x",
        [{"role": "user", "content": "hi"}], thinking="medium", timeout=10, host=host, web=False,
    )
    assert "fake__ping" in seen_thinking[0]
