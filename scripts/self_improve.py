"""Self-improvement loop for the AI chat harness: score it on a fixed task set, let the model rewrite its own
lessons, keep a rewrite only when the score goes up.

    python scripts/self_improve.py                      baseline plus 3 improve rounds on DeepSeek
    python scripts/self_improve.py --rounds 5 --max-cost 0.50 --model deepseek-chat
    python scripts/self_improve.py --apply              same, then offer to save the best lessons to memory
    python scripts/self_improve.py --save-baseline      one pass; remember which tasks pass for this model
    python scripts/self_improve.py --check              one pass; exit 1 if a task that used to pass fails

Each task runs the real `ask` loop and system prompt with file tools only, inside a throwaway folder, and is
checked by code. The lessons are AI memory notes tagged "[harness]". The run uses a sandboxed memory with only
those notes, so your own notes and skills do not skew the score. Only those notes change: never the code,
the system prompt or the approval rules. --apply shows them and asks before replacing the old [harness] notes
in your real memory file. The key comes from Credential Manager (set it in the chat's AI settings first).
A pass costs one chat per task per repeat (23 tasks x --repeats 2); --max-cost stops the run once that many USD
are spent. A weak improver model cannot make things worse: a rewrite is kept only if more tasks pass and no
task that passed fails. Notes tuned on one model go to every chat, so score with the model you chat with.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.tools.ai_assist import AiReply, ask, chat_messages, load_key  # noqa: E402
from src.tools.ai_tools import (FILE_TOOLS, MAX_NOTE_CHARS, MEMORY_HEADER, TOOL_SPECS, apply_edits,  # noqa: E402
                                calculate, contrast, load_skill,
                                memory_path, plan_edit, plan_edits, plan_skill_update, read_memory, resolve,
                                run_file_tool, skills_dir)

TAG = "[harness] "
MAX_LESSONS = 6
BENCH_TOOLS = FILE_TOOLS | {"edit_file", "edit_files", "calculate"}


class BenchHost:
    """File tools confined to one folder; edits are allowed without asking (nobody is there to ask)."""

    specs = [s for s in TOOL_SPECS if s["function"]["name"] in BENCH_TOOLS]

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _outside(self, args: dict[str, Any]) -> bool:
        paths = [args.get("path")] + [e.get("path") for e in args.get("edits") or [] if isinstance(e, dict)]
        return any(not resolve(self.root, p).is_relative_to(self.root) for p in paths)

    def run(self, name: str, args: dict[str, Any]) -> str:
        if name not in BENCH_TOOLS:
            return f"error: there is no tool named {name}"
        if name == "calculate":
            return calculate(args.get("expression"))
        if self._outside(args):
            return "error: stay inside the Project Folder"
        if name in FILE_TOOLS:
            return run_file_tool(self.root, name, args)
        try:
            edits = plan_edits(self.root, args) if name == "edit_files" else [plan_edit(self.root, args)]
        except ValueError as exc:
            return f"error: {exc}"
        return apply_edits(edits)


def _read(root: Path, rel: str) -> str:
    try:
        return (root / rel).read_text("utf-8")
    except OSError:
        return ""


@dataclass
class Task:
    name: str
    prompt: str
    files: dict[str, str]
    check: Callable[[Path, str], str]  # (folder, reply text) -> "" on pass, else why it failed


_LONG = "".join(f"filler line {i}\n" for i in range(4000)) + "secret_code = 7391\n"

TASKS = [
    Task("typo", "Fix the spelling mistakes in notes.txt.",
         {"notes.txt": "Teh meeting is on Monday.\nBring teh slides.\n"},
         lambda r, _t: "" if _read(r, "notes.txt") == "The meeting is on Monday.\nBring the slides.\n"
         else "notes.txt still wrong: " + repr(_read(r, "notes.txt")[:120])),
    Task("rename", "Rename the function calc_total to compute_total everywhere in this project.",
         {"lib.py": "def calc_total(xs):\n    return sum(xs)\n",
          "app.py": "from lib import calc_total\n\nprint(calc_total([1, 2]))\n"},
         lambda r, _t: "calc_total left behind" if "calc_total" in _read(r, "lib.py") + _read(r, "app.py")
         else "" if _read(r, "app.py").count("compute_total") == 2 else "app.py not updated"),
    Task("csv_row", "Add a row for Dana, age 41, to people.csv. Keep the existing rows.",
         {"people.csv": "name,age\nAmir,30\nBea,25\n"},
         lambda r, _t: "" if _read(r, "people.csv").splitlines() == ["name,age", "Amir,30", "Bea,25", "Dana,41"]
         else "people.csv is " + repr(_read(r, "people.csv"))),
    Task("count_todo", "How many TODO comments are in this project? Answer with just the number.",
         {"a.py": "# TODO one\nx = 1  # TODO two\n", "b.py": "# TODO three\n", "c.txt": "nothing here\n"},
         lambda _r, t: "" if re.findall(r"\d+", t) == ["3"] else f"answered {t[:80]!r}"),
    Task("new_json", "Create config.json with {\"debug\": false, \"port\": 8080}.", {},
         lambda r, _t: "" if _json(_read(r, "config.json")) == {"debug": False, "port": 8080}
         else "config.json is " + repr(_read(r, "config.json")[:120])),
    Task("which_file", "Which file in this project defines the class Invoice? Answer with just the file name.",
         {"models/user.py": "class User: ...\n", "models/billing.py": "class Invoice:\n    pass\n",
          "main.py": "from models.billing import Invoice\n"},
         lambda _r, t: "" if "billing.py" in t and "main.py" not in t else f"answered {t[:80]!r}"),
    Task("second_match", "In settings.ini, change the timeout in the [db] section to 30. Leave [web] alone.",
         {"settings.ini": "[web]\ntimeout = 10\n\n[db]\ntimeout = 10\n"},
         lambda r, _t: "" if _read(r, "settings.ini") == "[web]\ntimeout = 10\n\n[db]\ntimeout = 30\n"
         else "settings.ini is " + repr(_read(r, "settings.ini"))),
    Task("long_file", "What is the value of secret_code in big.txt? Answer with just the number.",
         {"big.txt": _LONG},
         lambda _r, t: "" if "7391" in t else f"answered {t[:80]!r}"),
    Task("read_only", "Read todo.md and tell me how many items are not done. Do not change any file.",
         {"todo.md": "- [x] buy milk\n- [ ] call Ali\n- [ ] pay rent\n- [x] walk\n"},
         lambda r, t: "todo.md was changed" if _read(r, "todo.md") != "- [x] buy milk\n- [ ] call Ali\n- [ ] pay rent\n- [x] walk\n"
         else "" if "2" in t or "two" in t.lower() else f"answered {t[:80]!r}"),
    Task("version_bump", "Bump the version from 1.4.2 to 1.5.0 in every file that has it.",
         {"pyproject.toml": "[project]\nversion = \"1.4.2\"\n", "src/__init__.py": "__version__ = \"1.4.2\"\n",
          "CHANGELOG.md": "# Changes\n"},
         lambda r, _t: "" if "1.5.0" in _read(r, "pyproject.toml") and "1.5.0" in _read(r, "src/__init__.py")
         and "1.4.2" not in _read(r, "pyproject.toml") + _read(r, "src/__init__.py") else "version not bumped everywhere"),
]

# Harder: big edits, near-identical matches, paging, many files, exact formatting
_CLASSES = ("class Cat:\n    def speak(self):\n        return 'meow'\n\n    def size(self):\n        return 1\n\n\n"
            "class Lion:\n    def speak(self):\n        return 'meow'\n\n    def size(self):\n        return 1\n")
_BIG = "".join(f"step_{i} = {i}\n" for i in range(5000))
_SETTINGS = {f"svc{i}.py": f"import config\n\nURL = config.api_url\nprint('svc{i}', config.api_url)\n" for i in range(6)}
_MOVE = {"utils.py": "def slugify(s):\n    return s.lower().replace(' ', '-')\n\n\ndef keep():\n    return 1\n",
         "helpers.py": "",
         "a.py": "from utils import slugify\n\nprint(slugify('A B'))\n",
         "b.py": "from utils import keep, slugify\n\nprint(keep(), slugify('C'))\n",
         "c.py": "import utils\n\nprint(utils.keep())\n"}
_DEFS = {f"pkg/m{i}/mod.py": "".join(f"def f{i}_{j}():\n    pass\n" for j in range(i % 4)) + "# def not_real():\n"
         for i in range(12)}
_PKG = ('{\n  "name": "app",\n  "version": "2.0.0",\n  "dependencies": {\n    "left-pad": {\n      "version": "2.0.0"\n'
        '    },\n    "chalk": {\n      "version": "2.0.0"\n    }\n  }\n}\n')


def _move_ok(r: Path) -> str:
    utils, helpers = _read(r, "utils.py"), _read(r, "helpers.py")
    if "def slugify" in utils or "def slugify" not in helpers or "def keep" not in utils:
        return "slugify not moved cleanly"
    if "from helpers import slugify" not in _read(r, "a.py"):
        return "a.py import not updated"
    b = _read(r, "b.py")
    if "slugify" not in b or "from utils import keep, slugify" in b or "helpers" not in b:
        return "b.py imports not updated"
    return "" if _read(r, "c.py") == _MOVE["c.py"] else "c.py changed but did not need to"


TASKS += [
    Task("twin_methods", "In shapes.py, make Lion.speak return 'roar'. Cat must keep 'meow'.", {"shapes.py": _CLASSES},
         lambda r, _t: "" if _read(r, "shapes.py") == _CLASSES.replace("return 'meow'", "return 'roar'", 2).replace(
             "return 'roar'", "return 'meow'", 1) else "shapes.py is " + repr(_read(r, "shapes.py")[-160:])),
    Task("deep_edit", "In steps.py, change step_4321 to 9999. Change nothing else.", {"steps.py": _BIG},
         lambda r, _t: "" if _read(r, "steps.py") == _BIG.replace("step_4321 = 4321\n", "step_4321 = 9999\n")
         else "steps.py wrong (other lines changed or edit missing)"),
    Task("six_files", "Rename config.api_url to config.base_url in every Python file here.", _SETTINGS,
         lambda r, _t: "" if all(_read(r, f) == t.replace("api_url", "base_url") for f, t in _SETTINGS.items())
         else "some svc files not renamed: " + ", ".join(f for f, t in _SETTINGS.items()
                                                       if _read(r, f) != t.replace("api_url", "base_url"))),
    Task("move_func", "Move the slugify function from utils.py into helpers.py and fix every import that uses it.",
         _MOVE, lambda r, _t: _move_ok(r)),
    Task("count_defs", "How many functions are defined in this project, counting only real def lines (not "
         "comments)? Answer with just the number.", _DEFS,
         lambda _r, t: "" if re.findall(r"\d+", t)[-1:] == [str(sum(i % 4 for i in range(12)))]  # a shown sum ends in it
         else f"answered {t[-80:]!r}"),
    Task("crlf_keep", "In win.txt, change 'beta' to 'gamma'.", {"win.txt": "alpha\r\nbeta\r\ndelta\r\n"},
         lambda r, _t: "" if (r / "win.txt").read_bytes() == b"alpha\r\ngamma\r\ndelta\r\n"
         else "win.txt bytes are " + repr((r / "win.txt").read_bytes())),
    Task("nested_json", "In package.json, bump only the chalk dependency's version to 5.3.0. Keep the formatting.",
         {"package.json": _PKG},
         lambda r, _t: "" if _read(r, "package.json") == '"5.3.0"'.join(_PKG.rsplit('"2.0.0"', 1))  # chalk is last
         else "package.json is " + repr(_read(r, "package.json"))),
]


# Agentic: several hops of reading before the one right edit. Checked statically: no model-written code is run.
_CHAIN = {"main.py": "from settings import load\n\nport = load()['port']\n",
          "settings.py": "import json\n\n\ndef load():\n    base = json.load(open('env/defaults.json'))\n"
                         "    base.update(json.load(open('env/overrides.json')))\n    return base\n",
          "env/defaults.json": '{"port": 8000, "debug": false}\n', "env/overrides.json": '{"port": 8500}\n',
          "README.md": "The server listens on port 8000.\n"}
_STATS = "def mean(xs):\n    return sum(xs) / len(xs)\n\n\ndef spread(xs):\n    return max(xs) - min(xs)\n"
_LOG = ('Traceback (most recent call last):\n  File "report.py", line 3, in <module>\n    print(mean(rows))\n'
        '  File "stats.py", line 2, in mean\n    return sum(xs) / len(xs)\nZeroDivisionError: division by zero\n')
_MODELS = ("class User:\n    name = ''\n\n\nclass Order:\n    total = 0\n\n\nclass Invoice:\n    paid = False\n")


def _chain_ok(r: Path, t: str) -> str:
    if _json(_read(r, "env/overrides.json")) != {"port": 9000}:
        return "overrides.json not set to 9000 (it is what wins)"
    if any(_read(r, f) != _CHAIN[f] for f in ("main.py", "settings.py", "env/defaults.json")):
        return "changed a file that did not need it"
    return "" if "8500" in t else f"did not report the old port 8500: {t[:80]!r}"


def _stats_ok(r: Path) -> str:
    try:
        tree = ast.parse(_read(r, "stats.py"))
    except SyntaxError:
        return "stats.py no longer parses"
    funcs = {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
    if set(funcs) != {"mean", "spread"} or ast.unparse(funcs["spread"]) != ast.unparse(ast.parse(_STATS).body[1]):
        return "spread changed or functions missing"
    guarded = any(isinstance(n, ast.If) and any(isinstance(b, ast.Return) and isinstance(b.value, ast.Constant)
                                                and b.value.value == 0 for b in n.body) for n in ast.walk(funcs["mean"]))
    return "" if guarded else "mean has no empty-list guard returning 0"


def _split_ok(r: Path) -> str:
    for cls, attr in (("User", "name"), ("Order", "total"), ("Invoice", "paid")):
        try:
            body = ast.parse(_read(r, f"models/{cls.lower()}.py")).body
        except SyntaxError:
            return f"models/{cls.lower()}.py does not parse"
        if [type(n).__name__ for n in body if isinstance(n, ast.ClassDef)] != ["ClassDef"] or \
                f"{attr} =" not in _read(r, f"models/{cls.lower()}.py"):
            return f"models/{cls.lower()}.py missing or wrong"
    app = _read(r, "app.py")
    if "class " in app:
        return "app.py still defines classes"
    names = {a.name for n in ast.parse(app).body if isinstance(n, ast.ImportFrom) for a in n.names}
    return "" if {"User", "Order", "Invoice"} <= names else "app.py does not re-export all three"


# Design: spec-driven pages and styles with objective checks (structure, tokens, WCAG contrast)
_CSS_OLD = ("body { background: #ffffff; color: #1a1a1a; }\nh1 { color: #1a1a1a; border-bottom: 2px solid #3366ff; }\n"
            "a { color: #3366ff; }\n.card { background: #f4f4f4; border: 1px solid #dddddd; }\n"
            ".card:hover { border-color: #3366ff; }\n")
_THEME = ".btn {\n  background: #4a90e2;\n  color: #a0c4f0;\n  padding: 8px 16px;\n}\n"


def _hexes(css: str) -> list[str]:
    return [h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", css)]


def _page_ok(r: Path) -> str:
    html = _read(r, "index.html")
    low = html.lower()
    problems = [why for ok, why in (
        ('name="viewport"' in low, "no viewport meta"),
        (len(re.findall(r'class="[^"]*\bplan\b', low)) == 3, "not exactly three .plan elements"),
        (len(re.findall(r"<button\b", low)) >= 3, "fewer than three buttons"),
        (re.search(r":root\s*{[^}]*--bg\s*:", low) and re.search(r":root\s*{[^}]*--fg\s*:", low), "no --bg/--fg on :root"),
        ("prefers-color-scheme: dark" in low.replace("prefers-color-scheme:dark", "prefers-color-scheme: dark"),
         "no dark mode"),
        (re.search(r"@media[^{]*max-width|auto-fit|flex-wrap", low), "nothing makes it fit a phone"),
        (not re.search(r'(src|href)="https?://', low), "loads something from the web"),
        (all("alt=" in tag for tag in re.findall(r"<img\b[^>]*>", low)), "an image has no alt"),
    ) if not ok]
    return "; ".join(problems)


def _tokens_ok(r: Path) -> str:
    css = _read(r, "style.css")
    root = re.search(r":root\s*{([^}]*)}", css)
    if not root:
        return "no :root block"
    outside = css[:root.start()] + css[root.end():]
    if _hexes(outside):
        return f"colors still hard-coded outside :root: {sorted(set(_hexes(outside)))}"
    if sorted(set(_hexes(root.group(1)))) != sorted(set(_hexes(_CSS_OLD))):
        return "the set of colors changed"
    defined = set(re.findall(r"(--[\w-]+)\s*:", root.group(1)))
    used = set(re.findall(r"var\((--[\w-]+)", outside))
    return "" if used and used <= defined else f"var() names not defined: {sorted(used - defined)}"


def _contrast_ok(r: Path) -> str:
    css = _read(r, "theme.css")
    bg, fg = re.search(r"background:\s*(#[0-9a-fA-F]{6})", css), re.search(r"[^-]color:\s*(#[0-9a-fA-F]{6})", css)
    if not bg or not fg or bg.group(1).lower() != "#4a90e2":
        return "background changed or colors not found"
    if css.replace(fg.group(1), "#a0c4f0") != _THEME:
        return "changed more than the text color"
    ratio = contrast(bg.group(1), fg.group(1))
    return "" if ratio >= 4.5 else f"contrast {ratio:.2f}:1 is under 4.5:1"


TASKS += [
    Task("trace_config", "Which port does main.py end up using, and where does it come from? Then change that port "
         "to 9000 in the file that actually decides it.", _CHAIN, _chain_ok),
    Task("fix_from_log", "report.py crashes; error.log has the traceback. Fix the cause in the library so an empty "
         "list gives a mean of 0. Change nothing else.", {"stats.py": _STATS, "error.log": _LOG,
                                                          "report.py": "from stats import mean\n\nrows = []\nprint(mean(rows))\n"},
         lambda r, _t: _stats_ok(r)),
    Task("split_module", "Split app.py so each class lives in its own file under models/ (models/user.py and so "
         "on), and make app.py import and re-export all of them.", {"app.py": _MODELS}, lambda r, _t: _split_ok(r)),
    Task("design_page", "Design index.html: a pricing page with three plans, each an element with class \"plan\" "
         "holding a button. All CSS inline in the file, colors as custom properties --bg and --fg on :root, a dark "
         "mode, a layout that fits a 375px phone, nothing loaded from the web, alt text on any image.", {},
         lambda r, _t: _page_ok(r)),
    Task("design_tokens", "Refactor style.css so every color is a CSS custom property defined once in :root and "
         "used through var(). Keep exactly the same colors.", {"style.css": _CSS_OLD}, lambda r, _t: _tokens_ok(r)),
    Task("design_contrast", "The .btn text in theme.css is hard to read. Change only its text color so it meets "
         "WCAG AA (4.5:1) against the button background, staying close to the brand blue look.",
         {"theme.css": _THEME}, lambda r, _t: _contrast_ok(r)),
]


def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return None


@dataclass
class Result:
    passed: dict[str, str] = field(default_factory=dict)  # task -> "" (pass) or failure reason
    tokens: int = 0
    cost: float = 0.0
    traces: dict[str, str] = field(default_factory=dict)

    @property
    def score(self) -> int:
        return sum(1 for why in self.passed.values() if not why)


AskFn = Callable[..., AiReply]


def _use_lessons(sandbox: Path, lessons: list[str]) -> None:
    os.environ["LOCALAPPDATA"] = str(sandbox)  # skills, rules and memory for the run come from here
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MEMORY_HEADER + "".join(f"- {TAG}{n}\n" for n in lessons), encoding="utf-8")


def run_suite(lessons: list[str], ask_fn: AskFn, base_url: str, key: str, model: str, sandbox: Path,
              tasks: list[Task] = TASKS, repeats: int = 1) -> Result:
    """A task passes only if it passes on every repeat, so one lucky run cannot get a bad note kept."""
    _use_lessons(sandbox, lessons)
    result = Result()
    for task in [t for t in tasks for _ in range(repeats)]:
        if result.passed.get(task.name):
            continue  # already failed on an earlier repeat
        folder = Path(tempfile.mkdtemp(prefix=f"bench_{task.name}_", dir=sandbox))
        for rel, text in task.files.items():
            (folder / rel).parent.mkdir(parents=True, exist_ok=True)
            (folder / rel).write_bytes(text.encode("utf-8"))  # exact bytes: write_text would make every file CRLF
        prompt = f"The Project Folder is {folder}. {task.prompt}"
        try:
            reply = ask_fn(base_url, key, model, chat_messages([{"role": "user", "content": prompt}]),
                           host=BenchHost(folder), web=False)
        except ValueError as exc:
            result.passed[task.name] = f"request failed ({exc})"
            continue
        result.passed[task.name] = task.check(folder, reply.text)
        result.tokens += reply.total_tokens
        result.cost += reply.cost or reply.cost_estimate
        calls = "; ".join(note for note, _out in reply.tools)[:1500]
        result.traces[task.name] = f"tools: {calls or 'none'}\nreply: {reply.text[:600]}"
    return result  # task folders go with the sandbox


def parse_lessons(text: str) -> list[str]:
    """The "- " lines of the model's answer, as notes that fit memory (tag included)."""
    out = []
    for line in text.splitlines():
        note = " ".join(line.strip()[2:].split()) if line.strip().startswith("- ") else ""
        if note and len(TAG) + len(note) <= MAX_NOTE_CHARS and note not in out:
            out.append(note)
    return out[:MAX_LESSONS]


def train(tasks: list[Task]) -> list[Task]:
    """The tasks the improver may see; every third one is held out, so notes tuned to one task show up as no gain."""
    return [t for i, t in enumerate(tasks) if i % 3 != 2]


def propose(lessons: list[str], result: Result, tasks: list[Task], ask_fn: AskFn, base_url: str, key: str,
            model: str) -> tuple[list[str], float]:
    failed = [t for t in train(tasks) if result.passed.get(t.name)]
    report = "\n\n".join(f"TASK: {t.prompt}\nWHY IT FAILED: {result.passed[t.name]}\n{result.traces.get(t.name, '')}"
                         for t in failed)
    current = "\n".join(f"- {n}" for n in lessons) or "(none)"
    prompt = (
        "You are improving the standing notes of an AI assistant that edits files with tools (list_files, "
        "read_file, find_files, search_text, edit_file, edit_files, calculate). Below are its current notes and the tasks "
        "it failed, with the tools it called and its answer.\n\n"
        f"CURRENT NOTES:\n{current}\n\nFAILURES:\n{report}\n\n"
        f"Write the full new set of notes: at most {MAX_LESSONS}, one per line starting with \"- \", each under "
        f"{MAX_NOTE_CHARS - len(TAG)} characters. Each must be a general working habit that would help on similar "
        "tasks, never an answer to these tasks or a file name from them. Every note is sent with every chat, so "
        "spend at most 2 notes on one kind of mistake and merge notes that overlap. Keep notes that still help. "
        "Reply with the notes only.")
    reply = ask_fn(base_url, key, model, [{"role": "user", "content": prompt}], web=False)
    return parse_lessons(reply.text), reply.cost or reply.cost_estimate


def better(new: Result, old: Result) -> bool:
    """More tasks pass and none that passed now fails; a tie on tasks wins only on fewer tokens."""
    regressed = any(not why and new.passed.get(name) for name, why in old.passed.items())
    if regressed:
        return False
    return new.score > old.score or (new.score == old.score and new.tokens < old.tokens * 0.9)


def improve(lessons: list[str], rounds: int, max_cost: float, ask_fn: AskFn, base_url: str, key: str, model: str,
            sandbox: Path, tasks: list[Task] = TASKS, log: Callable[[str], None] = print,
            repeats: int = 1, improver: str = "", history: list[dict[str, Any]] | None = None,
            confirm: bool = False) -> tuple[list[str], Result]:
    """`history` gets one entry per run (baseline, each round, the confirm reruns) for the report."""
    history = [] if history is None else history
    start = list(lessons)
    best = run_suite(lessons, ask_fn, base_url, key, model, sandbox, tasks, repeats)
    spent = best.cost
    history.append({"label": "baseline", "notes": list(lessons), "result": best, "kept": True, "spent": spent})
    log(f"baseline: {best.score}/{len(tasks)}  tokens {best.tokens}  ${spent:.4f}  "
        f"(held out: {', '.join(t.name for t in tasks if t not in train(tasks))})")
    for n in range(1, rounds + 1):
        if not any(best.passed.get(t.name) for t in train(tasks)):
            log("every task the improver may see passes; stopping")
            break
        if spent >= max_cost:
            log(f"spent ${spent:.4f} of ${max_cost:.2f}; stopping")
            break
        candidate, cost = propose(lessons, best, tasks, ask_fn, base_url, key, improver or model)
        spent += cost
        if not candidate or candidate == lessons:
            log(f"round {n}: no new notes proposed")
            continue
        trial = run_suite(candidate, ask_fn, base_url, key, model, sandbox, tasks, repeats)
        spent += trial.cost
        kept = better(trial, best)
        log(f"round {n}: {trial.score}/{len(tasks)}  tokens {trial.tokens}  {'KEPT' if kept else 'dropped'}  "
            f"(spent ${spent:.4f})")
        history.append({"label": f"round {n}", "notes": candidate, "result": trial, "kept": kept, "spent": spent})
        if kept:
            lessons, best = candidate, trial
    if confirm and lessons != start and spent < max_cost:  # fresh A/B: does the gain survive a rerun?
        scores = []
        for label, notes in (("recheck: old notes", start), ("recheck: new notes", lessons)):
            again = run_suite(notes, ask_fn, base_url, key, model, sandbox, tasks, repeats)
            spent += again.cost
            scores.append(again.score)
            log(f"{label}: {again.score}/{len(tasks)}  tokens {again.tokens}")
            history.append({"label": label, "notes": list(notes), "result": again, "kept": None, "spent": spent})
        if scores[1] <= scores[0]:  # the gain did not survive a fresh run: it was luck, so keep what we had
            log("recheck shows no gain; keeping the old notes")
            history[-1]["kept"] = False
            lessons = start
    for name, why in best.passed.items():
        if why:
            log(f"  still failing {name}: {why}")
    return lessons, best


_REPORT_CSS = """
:root { --bg: #f7f7f8; --fg: #1d1d22; --muted: #6b6b76; --card: #ffffff; --line: #e2e2e8;
        --pass: #1f7a3d; --pass-bg: #e3f4e8; --fail: #b3261e; --fail-bg: #fbe5e3; --hold: #6a4fb3; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #16161a; --fg: #e8e8ee; --muted: #9a9aa6; --card: #1f1f25; --line: #33333d;
          --pass: #6fd28f; --pass-bg: #183323; --fail: #ff8a80; --fail-bg: #3a1c1a; --hold: #b9a4f5; } }
body { background: var(--bg); color: var(--fg); font: 14px/1.5 system-ui, sans-serif; margin: 0; padding: 16px; }
main { max-width: 1200px; margin: 0 auto; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 16px; margin: 28px 0 8px; }
.muted { color: var(--muted); }
.scroll { overflow-x: auto; background: var(--card); border: 1px solid var(--line); border-radius: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
th { font-weight: 600; white-space: nowrap; }
td.num { font-variant-numeric: tabular-nums; text-align: right; }
.pass { color: var(--pass); background: var(--pass-bg); } .fail { color: var(--fail); background: var(--fail-bg); }
.hold { color: var(--hold); font-size: 12px; }
details summary { cursor: pointer; font-weight: 600; }
pre { white-space: pre-wrap; word-break: break-word; font-size: 12px; margin: 4px 0 0; max-width: 360px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px; margin: 8px 0; }
.added { color: var(--pass); } .removed { color: var(--fail); text-decoration: line-through; }
"""


def report_html(history: list[dict[str, Any]], tasks: list[Task], model: str) -> str:
    """One page: score per run (training and held-out apart), task-by-run grid with what the model did, note diffs."""
    from html import escape
    held = {t.name for t in tasks if t not in train(tasks)}

    def split(res: Result) -> tuple[int, int]:
        return (sum(1 for t in tasks if t.name not in held and not res.passed.get(t.name, "x")),
                sum(1 for t in tasks if t.name in held and not res.passed.get(t.name, "x")))

    rows = []
    for h in history:
        tr, ho = split(h["result"])
        verdict = {True: "kept", False: "dropped", None: "rerun"}[h["kept"]]
        rows.append(f"<tr><th>{escape(h['label'])}</th><td>{verdict}</td><td class=num>{tr}/{len(tasks) - len(held)}</td>"
                    f"<td class=num>{ho}/{len(held)}</td><td class=num>{h['result'].tokens:,}</td>"
                    f"<td class=num>${h['spent']:.4f}</td><td class=num>{len(h['notes'])}</td></tr>")
    head = "".join(f"<th>{escape(h['label'])}</th>" for h in history)
    grid = []
    for t in tasks:
        cells = []
        for h in history:
            why = h["result"].passed.get(t.name, "not run")
            trace = escape(h["result"].traces.get(t.name, ""))
            label = "pass" if not why else "FAIL"
            body = (f"<details><summary>{label}</summary><pre>{escape(why) + chr(10) if why else ''}{trace}</pre></details>")
            cells.append(f"<td class={'pass' if not why else 'fail'}>{body}</td>")
        tag = " <span class=hold>held out</span>" if t.name in held else ""
        grid.append(f"<tr><th title=\"{escape(t.prompt)}\">{escape(t.name)}{tag}</th>{''.join(cells)}</tr>")
    notes, prev = [], set(history[0]["notes"]) if history else set()
    for h in history:
        now = set(h["notes"])
        items = "".join(f"<li class={'added' if n not in prev else ''}>{escape(n)}</li>" for n in h["notes"])
        items += "".join(f"<li class=removed>{escape(n)}</li>" for n in sorted(prev - now))
        verdict = {True: "kept", False: "dropped", None: "rerun"}[h["kept"]]
        notes.append(f"<div class=card><b>{escape(h['label'])}</b> <span class=muted>({verdict})</span>"
                     f"<ul>{items or '<li class=muted>no notes</li>'}</ul></div>")
        if h["kept"]:
            prev = now
    return (f"<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width, "
            f"initial-scale=1'><title>Harness Learning Report</title><style>{_REPORT_CSS}</style><main>"
            f"<h1>Harness learning report</h1><p class=muted>{escape(model)}, {len(tasks)} tasks, {len(held)} held "
            "out (the improver never sees them, so a gain there is a real one). Green notes are new in that run, "
            "struck-out ones were removed. Open a cell to see the tools the model called and its answer.</p>"
            f"<h2>Score per run</h2><div class=scroll><table><tr><th>run</th><th>verdict</th><th>training</th>"
            f"<th>held out</th><th>tokens</th><th>spent</th><th>notes</th></tr>{''.join(rows)}</table></div>"
            f"<h2>Every task in every run</h2><div class=scroll><table><tr><th>task</th>{head}</tr>{''.join(grid)}"
            f"</table></div><h2>Notes tried</h2>{''.join(notes)}</main></html>")


def save_to_memory(path: Path, lessons: list[str]) -> None:
    """Replace every [harness] note in the real memory file; the user's other lines stay as they are."""
    try:
        lines = path.read_text("utf-8").splitlines()
    except OSError:
        lines = MEMORY_HEADER.splitlines()
    kept = [line for line in lines if not line.startswith(f"- {TAG}")]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([*kept, *(f"- {TAG}{n}" for n in lessons)]) + "\n", encoding="utf-8")


BASELINE = ROOT / "scripts" / "harness_baseline.json"  # {model: [tasks that passed]}; task names only, safe to commit


def regressions(result: Result, saved: list[str]) -> list[str]:
    """Tasks that passed in the saved baseline and fail now (a task no longer in the run is not a regression)."""
    return [name for name in saved if result.passed.get(name)]


def load_baseline(path: Path = BASELINE) -> dict[str, list[str]]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {m: [n for n in names if isinstance(n, str)] for m, names in data.items() if isinstance(names, list)} \
        if isinstance(data, dict) else {}


def save_baseline(model: str, result: Result, path: Path = BASELINE) -> None:
    data = load_baseline(path)
    data[model] = sorted(name for name, why in result.passed.items() if not why)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def check(model: str, lessons: list[str], args: argparse.Namespace, key: str, sandbox: Path) -> int:
    """One pass with the current notes against the saved baseline: 1 when a task that used to pass fails."""
    result = run_suite(lessons, ask, args.base_url, key, model, sandbox, repeats=args.repeats)
    print(f"{result.score}/{len(result.passed)} tasks passed (about ${result.cost:.3f})")
    saved = load_baseline().get(model)
    if args.save_baseline:
        save_baseline(model, result)
        print(f"baseline for {model} saved to {BASELINE}")
        return 0
    if saved is None:
        print(f"no baseline for {model}; run once with --save-baseline")
        return 2
    bad = regressions(result, saved)
    for name in bad:
        print(f"REGRESSION {name}: {result.passed[name]}")
    print("no regressions" if not bad else f"{len(bad)} regression(s)")
    return 1 if bad else 0


def load_mined_cases(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return []
    return [c for c in data if isinstance(c, dict) and isinstance(c.get("prompt"), str)] if isinstance(data, list) else []


def propose_skill_patch(skill_name: str, body: str, failures: list[dict[str, Any]], ask_fn: AskFn,
                        base_url: str, key: str, model: str) -> tuple[str, float]:
    diag = "\n\n".join(
        f"REQUEST: {c.get('prompt', '')[:400]}\nNEXT_USER: {c.get('next_user', '')[:250]}\n"
        f"FAILED: {json.dumps(c.get('failed_tools', [])[:4])}\nRETRIES: {json.dumps(c.get('retries', []))}"
        for c in failures[:8]
    )
    prompt = (
        f"You are refining the Paperduck skill '{skill_name}' based on real failed chat traces.\n"
        f"CURRENT SKILL BODY:\n{body[:8000]}\n\nFAILURE TRACES:\n{diag}\n\n"
        "Return only the full updated Markdown instructions body (concise, concrete steps fixing the observed failure modes)."
    )
    reply = ask_fn(base_url, key, model, [{"role": "user", "content": prompt}], web=False)
    return reply.text.strip(), reply.cost or reply.cost_estimate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="https://api.deepseek.com")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--max-cost", type=float, default=0.25, help="USD; checked between passes")
    parser.add_argument("--repeats", type=int, default=2, help="runs per task; a pass needs all of them")
    parser.add_argument("--improver-model", default="", help="model that writes the notes (default: --model)")
    parser.add_argument("--apply", action="store_true", help="offer to save the best notes to your memory")
    parser.add_argument("--no-recheck", action="store_true", help="skip the final old-vs-new rerun")
    parser.add_argument("--report", default=str(ROOT / "dist" / "harness_report.html"), help="HTML report path")
    parser.add_argument("--check", action="store_true", help="one pass, no rewriting; exit 1 on a regression vs the baseline")
    parser.add_argument("--save-baseline", action="store_true", help="one pass, then save which tasks pass as the baseline")
    parser.add_argument("--skill", default="", help="propose an update to a user skill from mined failure traces")
    parser.add_argument("--eval-json", default="", help="JSON file exported by mine_failures.py --export-eval")
    args = parser.parse_args()
    key = load_key(args.base_url)
    if not key:
        print(f"no API key saved for {args.base_url}; set it in the AI chat settings first")
        return 1
    if args.skill:
        sdir = skills_dir()
        body = load_skill(sdir, args.skill)
        if body.startswith("error:"):
            print(body)
            return 1
        eval_file = Path(args.eval_json) if args.eval_json else (ROOT / "dist" / "mined_tasks.json")
        mined = load_mined_cases(eval_file)
        if not mined:
            print(f"no failure cases in {eval_file}; run scripts/mine_failures.py --export-eval {eval_file} first")
            return 1
        updated, spent = propose_skill_patch(args.skill, body, mined, ask, args.base_url, key, args.improver_model or args.model)
        try:
            path, text, diff = plan_skill_update(sdir, args.skill, None, updated)
        except ValueError as exc:
            print(f"skill update skipped ({exc}, spent ${spent:.4f})")
            return 0
        print(diff)
        if args.apply and input(f"\nApply update to {path}? [y/N] ").strip().lower() == "y":
            path.write_text(text, encoding="utf-8")
            print(f"updated {path}")
        return 0
    real_memory = memory_path()  # before LOCALAPPDATA points at the sandbox
    current = [n[len(TAG):] for n in read_memory(real_memory) if n.startswith(TAG)]
    sandbox = Path(tempfile.mkdtemp(prefix="paperduck_rsi_"))
    if args.check or args.save_baseline:
        try:
            return check(args.model, current, args, key, sandbox)
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
    history: list[dict[str, Any]] = []
    try:
        lessons, _best = improve(current, args.rounds, args.max_cost, ask, args.base_url, key, args.model, sandbox,
                                 repeats=args.repeats, improver=args.improver_model, history=history,
                                 confirm=not args.no_recheck)
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
        if history:  # even a run stopped by Ctrl+C shows what it got through
            report = Path(args.report)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(report_html(history, TASKS, args.model), encoding="utf-8")
            print(f"\nreport: {report}")
    print("\nbest notes:" if lessons else "\nno notes")
    print("\n".join(f"- {TAG}{n}" for n in lessons))
    if args.apply and lessons != current:
        if input(f"\nReplace the [harness] notes in {real_memory}? [y/N] ").strip().lower() == "y":
            save_to_memory(real_memory, lessons)
            print("saved; new chats will see them")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
