"""Errands: standing orders the chat model sets ("when bg2 finishes, fix what failed").

The pet checks each trigger locally for free on its 3s hint tick; the model runs only when one fires, in the chat
that set it. Every probe is injected through `Probes`, so the checks are testable without Windows.
"""

from __future__ import annotations

import ctypes
import fnmatch
import os
import re
import sys
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem

MAX_ERRANDS = 8  # waiting at once
MAX_ERRAND_CHARS = 1000  # one instruction
MAX_SCRIPT_CHARS = 2000  # compiled routine script
MAX_ERRAND_RUNS = 20  # fires per day
ERRAND_HOURS = 24  # how long an errand waits
STARTUP_DAYS = 7  # a next_startup errand waits longer: the PC may stay on for days
EVIDENCE_LINES = 40  # output lines handed to the model when a background errand fires
AWAY_S = 300  # user_returns needs this long without input first (same as the pet's sleep)
PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".tmp", ".download")
ERRAND_MARK = "[Paperduck: an errand you set fired"
_REPEATABLE = frozenset({"every", "at_time", "file_appears", "window_appears", "port_opens", "user_returns"})
KINDS: dict[str, str] = {
    "background_done": "id: a background command (bgN) exits",
    "background_prints": "id, text: a background command's new output contains text",
    "app_closes": "exe: no process with that exe name runs any more",
    "file_appears": "pattern: a new file matching a glob in one folder, e.g. ~/Downloads/*.zip, finished writing",
    "port_opens": "port: a local TCP port starts listening",
    "window_appears": "title: the front window's title contains text",
    "at_time": "time: HH:MM local time, or minutes: from now",
    "every": "minutes: repeat every N minutes (1 to 10080)",
    "user_returns": "(no fields): the user comes back after 5 minutes away",
    "next_startup": "(no fields): the next time Paperduck starts (after a reboot or crash)",
}
_EXE = re.compile(r"^[\w .()+-]{1,80}\.exe$", re.I)
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass
class Probes:
    """What an errand can look at. The defaults read Windows; tests pass fakes."""

    bg: Callable[[str, int], tuple[int | None, bool, str, int] | None] = lambda _n, _s: None  # BackgroundProcs.peek
    exes: Callable[[], set[str]] = lambda: running_exes()
    listening: Callable[[int], bool] = lambda port: port_listening(port)
    title: Callable[[], str] = lambda: _foreground_title()
    idle: Callable[[], float] = lambda: _idle_seconds()


@dataclass
class Errand:
    id: str
    kind: str
    arg: dict[str, Any]
    instruction: str
    sid: str = ""  # the chat that set it; it runs there
    created: float = 0.0
    expires: float = 0.0
    base: dict[str, Any] = field(default_factory=dict)  # state at creation (output offset, files already there)
    repeat: bool = False
    script: str = ""
    last_run: float = 0.0
    run_count: int = 0


def parse_when(when: Any) -> tuple[str, dict[str, Any]]:
    """{kind, ...fields} -> (kind, clean fields), or ValueError naming what is wrong."""
    if not isinstance(when, dict) or when.get("kind") not in KINDS:
        raise ValueError("when needs kind: one of " + ", ".join(KINDS))
    kind = when["kind"]

    def text(key: str, limit: int = 200) -> str:
        value = when.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{kind} needs {key} (text, up to {limit} characters)")
        return value.strip()

    if kind == "background_done":
        return kind, {"id": text("id", 20)}
    if kind == "background_prints":
        return kind, {"id": text("id", 20), "text": text("text")}
    if kind == "app_closes":
        exe = text("exe", 84)
        if not _EXE.match(exe):
            raise ValueError("exe is a program file name such as setup.exe")
        return kind, {"exe": exe.lower()}
    if kind == "file_appears":
        pattern = text("pattern", 400)
        folder, glob = os.path.split(os.path.expanduser(pattern))
        if not glob or not any(c in glob for c in "*?[") or not os.path.isdir(folder):
            raise ValueError("pattern is an existing folder plus a file glob, e.g. ~/Downloads/*.zip")
        return kind, {"pattern": pattern, "folder": folder, "glob": glob}
    if kind == "port_opens":
        port = when.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("port is a whole number from 1 to 65535")
        return kind, {"port": port}
    if kind == "window_appears":
        return kind, {"title": text("title", 120)}
    if kind == "at_time":
        minutes = when.get("minutes")
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= ERRAND_HOURS * 60:
                raise ValueError(f"minutes is a whole number from 1 to {ERRAND_HOURS * 60}")
            return kind, {"minutes": minutes}
        if not _HHMM.match(text("time", 5)):
            raise ValueError("time is HH:MM in 24-hour local time, or give minutes")
        return kind, {"time": when["time"].strip()}
    if kind == "every":
        minutes = when.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 10080:
            raise ValueError("every needs minutes (whole number from 1 to 10080)")
        return kind, {"minutes": minutes}
    return kind, {}


def _due(arg: dict[str, Any], now: float) -> float:
    if "minutes" in arg:
        return now + arg["minutes"] * 60
    hour, minute = (int(p) for p in arg["time"].split(":"))
    at = datetime.fromtimestamp(now).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at.timestamp() <= now:
        at += timedelta(days=1)
    return at.timestamp()


def describe(kind: str, arg: dict[str, Any]) -> str:
    """Plain English, for the approval card, the menu and the model."""
    return {
        "background_done": lambda: f"when {arg['id']} finishes",
        "background_prints": lambda: f"when {arg['id']} prints {arg['text']!r}",
        "app_closes": lambda: f"when {arg['exe']} closes",
        "file_appears": lambda: f"when a file matching {arg['pattern']} appears",
        "port_opens": lambda: f"when port {arg['port']} opens",
        "window_appears": lambda: f"when a window titled {arg['title']!r} comes to the front",
        "at_time": lambda: (f"in {arg['minutes']} minutes" if "minutes" in arg else f"at {arg['time']}"),
        "every": lambda: f"every {arg['minutes']} minutes",
        "user_returns": lambda: "when you come back to the PC",
        "next_startup": lambda: "the next time Paperduck starts",
    }[kind]()


def _matches(folder: str, glob: str) -> dict[str, int]:
    """name -> size of the files in `folder` matching `glob`, half-written downloads left out."""
    found: dict[str, int] = {}
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                if (fnmatch.fnmatch(entry.name.lower(), glob.lower()) and entry.is_file()
                        and not entry.name.lower().endswith(PARTIAL_SUFFIXES)):
                    found[entry.name] = entry.stat().st_size
    except OSError:
        pass
    return found


def baseline(kind: str, arg: dict[str, Any], probes: Probes, now: float) -> dict[str, Any]:
    """What is true when the errand is set, so it fires on a change, not on what was already there.
    ValueError when the trigger cannot happen (no such background command, the app is not running)."""
    if kind.startswith("background_"):
        got = probes.bg(arg["id"], 0)
        if got is None:
            raise ValueError(f"there is no background command {arg['id']}")
        if kind == "background_done" and got[0] is not None:
            raise ValueError(f"{arg['id']} has already exited; read_background it instead")
        return {"offset": got[3]}
    if kind == "app_closes":
        if arg["exe"] not in probes.exes():
            raise ValueError(f"{arg['exe']} is not running now")
    if kind == "file_appears":
        return {"names": sorted(_matches(arg["folder"], arg["glob"]))}
    if kind in ("at_time", "every"):
        return {"due": _due(arg, now)}
    return {}


def check(errand: Errand, probes: Probes, now: float) -> str | None:
    """Evidence text when the trigger fired, else None. Updates `errand.base` for two-step triggers."""
    kind, arg, base = errand.kind, errand.arg, errand.base
    if kind.startswith("background_"):
        # a finished build's whole tail matters; a "prints" match counts only output written after it was set
        got = probes.bg(arg["id"], 0 if kind == "background_done" else base.get("offset", 0))
        if got is None:
            return f"{arg['id']} is gone (the chat was cleared or the app restarted)"
        code, stopped, text, _end = got
        tail = "\n".join(text.strip().splitlines()[-EVIDENCE_LINES:]) or "(no output)"
        if kind == "background_done":
            if code is None:
                return None
            how = "was stopped" if stopped else f"exited with code {code}"
            return f"{arg['id']} {how}. Its last output:\n{tail}"
        if arg["text"].lower() in text.lower():
            return f"{arg['id']} printed {arg['text']!r}. Its newest output:\n{tail}"
        if code is not None:
            return f"{arg['id']} exited with code {code} without printing {arg['text']!r}. Its last output:\n{tail}"
        return None
    if kind == "app_closes":
        return None if arg["exe"] in probes.exes() else f"{arg['exe']} is no longer running"
    if kind == "file_appears":
        now_files = _matches(arg["folder"], arg["glob"])
        new = {n: s for n, s in now_files.items() if n not in base.get("names", [])}
        settled = [n for n, s in new.items() if base.get("sizes", {}).get(n) == s]
        base["sizes"] = new  # a size must hold over two checks: a download or a copy may still be writing
        if settled:
            names = ", ".join(f"{n} ({new[n]} bytes)" for n in sorted(settled)[:10])
            return f"new in {arg['folder']}: {names}"
        return None
    if kind == "port_opens":
        is_open = probes.listening(arg["port"])
        if base.get("active"):
            if not is_open:
                base["active"] = False
            return None
        return f"port {arg['port']} is listening" if is_open else None
    if kind == "window_appears":
        from src.tools.watch import is_private_title

        title = probes.title()
        matched = bool(title and not is_private_title(title) and arg["title"].lower() in title.lower())
        if base.get("active"):
            if not matched:
                base["active"] = False
            return None
        return f"the front window is {title[:120]!r}" if matched else None
    if kind in ("at_time", "every"):
        return "it is time" if now >= base.get("due", 0) else None
    if kind == "user_returns":
        idle = probes.idle()
        if idle >= AWAY_S:
            base["away"] = True
        elif base.get("away"):
            return "the user is back at the PC"
        return None
    return None  # next_startup fires in ErrandsPlugin.load_dict


class ErrandsPlugin:
    """Waiting errands, persisted in state.json. `PetWindow` calls `poll` on its hint tick and runs what fired."""

    SUBMENU_NAME = "Errands"

    def __init__(self) -> None:
        self.errands: list[Errand] = []
        self.fired: list[tuple[Errand, str]] = []  # (errand, evidence), oldest first; the pet runs them
        self.probes = Probes()
        self.done = 0
        self._day = time.strftime("%Y-%m-%d")
        self.runs = 0
        self._next = 1

    @property
    def tool_id(self) -> str:
        return "errands"

    @property
    def display_name(self) -> str:
        return "ERRANDS"

    @property
    def tick_interval_ms(self) -> int:
        return 2000

    def tick(self) -> bool:
        return False  # polled by the pet's hint tick instead, which also owns the chat's background commands

    def desired_state(self) -> CompanionState | None:
        return None

    def add(self, when: Any, instruction: Any, sid: str, now: float | None = None,
            repeat: bool = False, script: str = "") -> Errand:
        """A new waiting errand, or ValueError (bad trigger, too many, too long)."""
        kind, arg = parse_when(when)
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError("give the instruction to follow when it fires")
        if len(instruction) > MAX_ERRAND_CHARS:
            raise ValueError(f"keep the instruction under {MAX_ERRAND_CHARS} characters")
        clean_script = script.strip() if isinstance(script, str) else ""
        if len(clean_script) > MAX_SCRIPT_CHARS:
            raise ValueError(f"keep the compiled script under {MAX_SCRIPT_CHARS} characters")
        is_repeat = bool(repeat) or kind == "every"
        if is_repeat and kind not in _REPEATABLE:
            raise ValueError(f"{kind} cannot repeat")
        if len(self.errands) >= MAX_ERRANDS:
            raise ValueError(f"{MAX_ERRANDS} errands are already waiting; cancel one first")
        now = time.time() if now is None else now
        ttl = 0.0 if is_repeat else now + (STARTUP_DAYS * 86400 if kind == "next_startup" else ERRAND_HOURS * 3600)
        errand = Errand(f"e{self._next}", kind, arg, instruction.strip(), sid, now,
                        ttl, baseline(kind, arg, self.probes, now),
                        repeat=is_repeat, script=clean_script)
        self._next += 1
        self.errands.append(errand)
        return errand

    def cancel(self, errand_id: Any) -> str:
        for errand in self.errands:
            if errand.id == errand_id:
                self.errands.remove(errand)
                return f"cancelled {errand.id} ({describe(errand.kind, errand.arg)})"
        return "error: no such errand. Waiting: " + (", ".join(e.id for e in self.errands) or "none")

    def poll(self, now: float | None = None) -> bool:
        """Check every waiting errand; fired and expired ones leave the list. True when something changed."""
        now = time.time() if now is None else now
        changed = False
        for errand in list(self.errands):
            if errand.expires > 0 and now >= errand.expires:
                self.errands.remove(errand)
                changed = True
                continue
            try:
                evidence = check(errand, self.probes, now)
            except (OSError, ValueError, KeyError):
                evidence = None  # a probe hiccup never fires an errand
            if evidence is not None:
                if errand.repeat:
                    errand.last_run = now
                    errand.run_count += 1
                    try:
                        errand.base = baseline(errand.kind, errand.arg, self.probes, now)
                    except (OSError, ValueError):
                        pass
                    if errand.kind in ("port_opens", "window_appears"):
                        errand.base["active"] = True
                    self.fired.append((replace(errand, base=dict(errand.base), arg=dict(errand.arg)), evidence))
                else:
                    self.errands.remove(errand)
                    self.fired.append((errand, evidence))
                changed = True
        return changed

    def take_run(self) -> bool:
        """Count one errand run against today's budget; False when it is used up."""
        today = time.strftime("%Y-%m-%d")
        if today != self._day:
            self._day, self.runs = today, 0
        if self.runs >= MAX_ERRAND_RUNS:
            return False
        self.runs += 1
        return True

    def summary(self) -> str:
        return "\n".join(f"{e.id}: {describe(e.kind, e.arg)}: {e.instruction[:80]}" for e in self.errands)

    def get_hud_data(self) -> HudData:
        return HudData("ERRANDS", "WAIT" if self.errands else "IDLE", "#FFCD75" if self.errands else "#38B764", (
            HudBar("Waiting", len(self.errands) / MAX_ERRANDS * 100, "#FFCD75", f"{len(self.errands)}"),
            HudBar("Done", min(100.0, self.done * 10.0), "#38B764", f"{self.done}"),
            HudBar("Today", self.runs / MAX_ERRAND_RUNS * 100, "#73EFF7", f"{self.runs}/{MAX_ERRAND_RUNS}"),
        ), self.errands[0].instruction[:60] if self.errands else "Ask the chat: when X happens, do Y")

    def get_menu_items(self) -> list[ToolMenuItem]:
        sub = self.SUBMENU_NAME
        if not self.errands:
            return [ToolMenuItem("No errands waiting", "errands:none", enabled=False, submenu=sub)]
        items = [ToolMenuItem(f"Cancel {describe(e.kind, e.arg)}: {e.instruction[:40]}", f"errands:cancel:{e.id}",
                              submenu=sub) for e in self.errands]
        return [*items, ToolMenuItem("Cancel All", "errands:cancel_all", submenu=sub)]

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id == "errands:cancel_all":
            self.errands.clear()
            return True, "CANCELLED", "#8A9AF2"
        if callback_id.startswith("errands:cancel:"):
            out = self.cancel(callback_id[15:])
            return True, "CANCELLED" if not out.startswith("error") else "GONE", "#8A9AF2"
        return False, "", ""

    def to_dict(self) -> dict[str, Any]:
        return {"next": self._next, "day": self._day, "runs": self.runs, "done": self.done, "errands": [
            {"id": e.id, "kind": e.kind, "arg": e.arg, "instruction": e.instruction, "sid": e.sid,
             "created": e.created, "expires": e.expires, "base": e.base,
             **({"repeat": True} if e.repeat else {}),
             **({"script": e.script} if e.script else {}),
             **({"last_run": e.last_run, "run_count": e.run_count} if e.run_count else {})}
            for e in self.errands]}

    def load_dict(self, data: dict[str, Any], now: float | None = None) -> None:
        """Background-command errands are dropped (those commands died with the app); next_startup ones fire."""
        if not isinstance(data, dict):
            return
        now = time.time() if now is None else now
        if isinstance(data.get("next"), int) and not isinstance(data.get("next"), bool) and data["next"] > 0:
            self._next = data["next"]
        if data.get("day") == self._day and isinstance(data.get("runs"), int) and not isinstance(data.get("runs"), bool):
            self.runs = max(0, data["runs"])
        if isinstance(data.get("done"), int) and not isinstance(data.get("done"), bool):
            self.done = max(0, data["done"])
        for raw in data.get("errands") or []:
            try:
                kind, arg = parse_when({"kind": raw["kind"], **raw["arg"]}) if raw["kind"] != "file_appears" \
                    else (raw["kind"], {k: str(raw["arg"][k]) for k in ("pattern", "folder", "glob")})
                errand = Errand(
                    str(raw["id"]), kind, arg, str(raw["instruction"])[:MAX_ERRAND_CHARS],
                    str(raw.get("sid") or ""), float(raw["created"]), float(raw["expires"]),
                    dict(raw.get("base") or {}),
                    repeat=bool(raw.get("repeat")) or kind == "every",
                    script=str(raw.get("script") or "")[:MAX_SCRIPT_CHARS],
                    last_run=float(raw.get("last_run") or 0.0),
                    run_count=int(raw.get("run_count") or 0),
                )
            except (KeyError, TypeError, ValueError):
                continue  # a hand-edited or old entry is skipped, never crashes the start
            if kind.startswith("background_") or (errand.expires > 0 and now >= errand.expires) or not errand.instruction:
                continue
            if kind == "next_startup":
                self.fired.append((errand, "Paperduck just started (the PC was restarted, or the app was)"))
            elif len(self.errands) < MAX_ERRANDS:
                self.errands.append(errand)
            if errand.id.startswith("e") and errand.id[1:].isdigit():
                self._next = max(self._next, int(errand.id[1:]) + 1)


# ---------------------------------------------------------------------- Windows probes


def running_exes() -> set[str]:
    """Lower-case exe names of the processes this user can see (EnumProcesses, about 2 ms)."""
    if sys.platform != "win32":
        return set()
    from src.tools.ports import PROTECTED, process_name

    pids = (ctypes.c_ulong * 4096)()
    size = ctypes.c_ulong()
    if not ctypes.windll.psapi.EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(size)):
        return set()
    names = {process_name(pids[i]).lower() for i in range(size.value // ctypes.sizeof(ctypes.c_ulong)) if pids[i]}
    names.discard(PROTECTED.lower())
    return names


def port_listening(port: int) -> bool:
    if sys.platform != "win32":
        return False
    from src.tools.ports import _AF_INET, _AF_INET6, _MIB_TCP6ROW_OWNER_PID, _MIB_TCPROW_OWNER_PID, _listener_rows

    return any(p == port for family, row in ((_AF_INET, _MIB_TCPROW_OWNER_PID), (_AF_INET6, _MIB_TCP6ROW_OWNER_PID))
               for p, _pid in _listener_rows(family, row))


def _foreground_title() -> str:
    from src.tools.hints import foreground_title

    return foreground_title()


def _idle_seconds() -> float:
    from src.tools.hints import idle_seconds

    return idle_seconds()


def run_routine_script(script: str, cwd: Path | None = None, timeout_s: int = 60) -> tuple[bool, str]:
    """Run a compiled routine command locally. Returns (handled_quietly, output)."""
    from src.tools.ai_tools import run_command

    out = run_command(cwd or Path.home(), script, None, timeout_s)
    handled = out.startswith("exit code 0") and "NEEDS_AI:" not in out
    return handled, out

