"""Find where the AI chat actually goes wrong, from your own saved chats and log. Local only: no API calls.

    python scripts/mine_failures.py                 writes dist/failure_report.html, prints a summary
    python scripts/mine_failures.py --top 25

Signals per answer: failed tool calls (the same test the agent loop uses), retry chains (one call repeated
with only numbers or a few words changed, e.g. _fix.ps1 to _fix6.ps1), answers you stopped, and your next
message pushing back ("still wrong", "that's not it", "undo"). The worst answers come first, each with your
request, the tool chain and what you said next, so it can be turned into a benchmark task in
scripts/self_improve.py. Also lists stray files the AI left in the Paperduck data folder.
The report quotes your chats, so it stays on this PC (dist/ is gitignored).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools.ai_assist import _failed  # noqa: E402
from src.tools.ai_sessions import Session, _clean, list_sessions, sessions_dir  # noqa: E402

STOPPED = "(stopped by the user"
PUSHBACK = re.compile(r"\b(still|again|wrong|not what|that'?s not|isn'?t|doesn'?t|didn'?t|don'?t|won'?t|undo|revert|"
                      r"broke|broken|why did|stop|no,|nope|instead|you forgot|missing|try again|not working)\b", re.I)
RETRY_RUN = 3  # the same call this many times in one answer is a retry chain
_KNOWN = {"app", "venv", "sessions", "skills", "paperduck.log", "crash.log", "memory.md", "rules.md", "mcp.json",
          "state.json", "state.json.bak", ".installed", "update-cache"}


def _shape(note: str) -> str:
    """A call with its numbers and quoted bits blanked, so poem_fix3.ps1 and poem_fix5.ps1 match."""
    return re.sub(r"\d+", "#", re.sub(r"(['\"]).*?\1", "''", note.lower()))


@dataclass
class Case:
    session: str
    request: str
    tools: list[tuple[str, str]]
    reply: str
    next_user: str = ""
    failed: list[tuple[str, str]] = field(default_factory=list)
    retries: list[tuple[str, int]] = field(default_factory=list)
    stopped: bool = False

    @property
    def pushback(self) -> bool:
        return bool(self.next_user) and bool(PUSHBACK.search(self.next_user[:300]))

    @property
    def score(self) -> int:
        return (len(self.failed) + 2 * sum(n for _s, n in self.retries) // RETRY_RUN + 4 * self.pushback
                + 3 * self.stopped)


def cases(sessions: list[Session]) -> list[Case]:
    out = []
    for s in sessions:
        turns = s.history
        for i, turn in enumerate(turns):
            if turn["role"] != "assistant" or i == 0:
                continue
            request = next((t["content"] for t in reversed(turns[:i]) if t["role"] == "user"), "")
            nxt = next((t["content"] for t in turns[i + 1:] if t["role"] == "user"), "")
            tools = list(turn.get("tools") or [])
            shapes = Counter(_shape(n) for n, _o in tools)
            case = Case(s.title, request, tools, turn["content"], nxt,
                        failed=[(n, o) for n, o in tools if _failed(o)],
                        retries=[(sh, k) for sh, k in shapes.most_common() if k >= RETRY_RUN],
                        stopped=turn["content"].startswith(STOPPED))
            if case.score:
                out.append(case)
    return sorted(out, key=lambda c: c.score, reverse=True)


def _error_kind(output: str) -> str:
    head = output.strip().splitlines()[0] if output.strip() else ""
    return re.sub(r"\d+", "#", re.sub(r"[A-Za-z]:\\\S+|/\S+", "<path>", head))[:80]


def tool_failures(all_cases: list[Case]) -> tuple[Counter, Counter]:
    by_tool, by_kind = Counter(), Counter()
    for c in all_cases:
        for note, out in c.failed:
            name = note.split("(", 1)[0]
            by_tool[name] += 1
            by_kind[f"{name}: {_error_kind(out)}"] += 1
    return by_tool, by_kind


def log_failures(log: Path) -> Counter:
    try:
        text = log.read_text("utf-8", "replace")
    except OSError:
        return Counter()
    return Counter(re.findall(r"tool ([\w-]+): failed", text))


def stray_files(data_dir: Path) -> list[str]:
    try:
        return sorted(p.name for p in data_dir.iterdir() if p.name not in _KNOWN)
    except OSError:
        return []


def report_html(top: list[Case], total: int, sessions: int, by_tool: Counter, by_kind: Counter, logged: Counter,
                strays: list[str]) -> str:
    from scripts.self_improve import _REPORT_CSS

    def table(rows: list[tuple[str, int]], head: str) -> str:
        body = "".join(f"<tr><td>{escape(k)}</td><td class=num>{v}</td></tr>" for k, v in rows)
        return f"<div class=scroll><table><tr><th>{head}</th><th>count</th></tr>{body}</table></div>"

    items = []
    for c in top:
        flags = ", ".join(x for x, on in (("you pushed back", c.pushback), ("stopped", c.stopped),
                                          (f"{len(c.failed)} failed calls", c.failed),
                                          ("retry chain", c.retries)) if on)
        chain = "\n".join(f"{'FAIL ' if _failed(o) else '     '}{n}" + (f"\n        {o.strip()[:160]}" if _failed(o) else "")
                          for n, o in c.tools)
        retry = "".join(f"<li>{escape(sh)} x{k}</li>" for sh, k in c.retries)
        items.append(
            f"<div class=card><b>score {c.score}</b> <span class=muted>{escape(c.session)} | {escape(flags)}</span>"
            f"<p><b>You asked:</b> {escape(c.request[:400])}</p>"
            + (f"<p><b>You said next:</b> {escape(c.next_user[:300])}</p>" if c.next_user else "")
            + (f"<p><b>Repeated:</b></p><ul>{retry}</ul>" if retry else "")
            + f"<details><summary>{len(c.tools)} tool calls</summary><pre>{escape(chain)}</pre></details>"
            f"<details><summary>its answer</summary><pre>{escape(c.reply[:1500])}</pre></details></div>")
    stray = (f"<p>{len(strays)} files the AI left in the data folder (scripts, images, drafts). They belong in a "
             f"temp folder or the Project Folder:</p><pre>{escape(', '.join(strays))}</pre>") if strays else "<p>none</p>"
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width, "
            f"initial-scale=1'><title>Chat Failure Report</title><style>{_REPORT_CSS}</style><main>"
            f"<h1>Where the AI chat goes wrong</h1><p class=muted>{sessions} saved chats, {total} answers with a "
            f"problem; the worst {len(top)} below. Quotes your chats: keep this file on this PC.</p>"
            f"<h2>Failed calls by tool (saved chats)</h2>{table(by_tool.most_common(15), 'tool')}"
            f"<h2>Failed calls by tool (whole log)</h2>{table(logged.most_common(15), 'tool')}"
            f"<h2>Most common errors</h2>{table(by_kind.most_common(20), 'error')}"
            f"<h2>Stray files</h2>{stray}<h2>Worst answers</h2>{''.join(items)}</main></html>")


def export_eval_cases(all_cases: list[Case], limit: int = 20) -> list[dict[str, Any]]:
    exported: list[dict[str, Any]] = []
    for idx, c in enumerate(all_cases[:limit], 1):
        exported.append({
            "name": f"mined_{idx}",
            "score": c.score,
            "prompt": _clean(c.request[:1200]),
            "next_user": _clean(c.next_user[:400]),
            "failed_tools": [{"call": _clean(n[:120]), "error": _clean(o[:240])} for n, o in c.failed[:6]],
            "retries": [{"shape": sh, "count": k} for sh, k in c.retries],
            "stopped": c.stopped,
        })
    return exported


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--report", default=str(ROOT / "dist" / "failure_report.html"))
    parser.add_argument("--export-eval", default="", help="Optional JSON path for sanitized failure trajectories")
    args = parser.parse_args()
    data_dir = sessions_dir().parent
    sessions = list_sessions()
    found = cases(sessions)
    by_tool, by_kind = tool_failures(found)
    logged = log_failures(data_dir / "paperduck.log")
    strays = stray_files(data_dir)
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report_html(found[:args.top], len(found), len(sessions), by_tool, by_kind, logged, strays), "utf-8")
    if args.export_eval:
        eval_path = Path(args.export_eval)
        eval_path.parent.mkdir(parents=True, exist_ok=True)
        eval_path.write_text(json.dumps(export_eval_cases(found, args.top), indent=2) + "\n", encoding="utf-8")
        print(f"exported eval cases: {eval_path}")
    print(f"{len(sessions)} chats, {len(found)} answers with a problem "
          f"({sum(c.pushback for c in found)} pushed back, {sum(c.stopped for c in found)} stopped, "
          f"{sum(bool(c.retries) for c in found)} retry chains, {sum(len(c.failed) for c in found)} failed calls)")
    print("failed calls in the log:", ", ".join(f"{k} {v}" for k, v in logged.most_common(6)) or "none")
    print("top errors:", *(f"  {v:3}  {k}" for k, v in by_kind.most_common(6)), sep="\n")
    print(f"stray files in {data_dir}: {len(strays)}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
