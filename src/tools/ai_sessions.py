"""Saved AI chat sessions: one JSON file per conversation under %LOCALAPPDATA%\\Paperduck\\sessions.

Only text is stored: turns (role, content, tool notes) and the task list. Attachment image bytes are never
written, text that looks like an API key is blanked, and keys themselves live in Credential Manager, not here.
The folder keeps at most MAX_SESSIONS files (oldest dropped) of at most MAX_SESSION_BYTES each (oldest turns
dropped). A file that is corrupt or not shaped like a session is skipped, never raised.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.tools.ai_tools import MAX_TASK_CHARS, MAX_TASKS, TASK_STATUSES, skills_dir

MAX_SESSIONS = 30
MAX_SESSION_BYTES = 2 * 1024 * 1024
MAX_TITLE_CHARS = 60
VERSION = 1
_ID = re.compile(r"^[0-9a-f]{12}$")
# shortcut: common key shapes only (sk-..., ghp_..., AKIA..., Bearer ...), not a secret scanner
_KEY_LIKE = re.compile(r"\b(?:sk-[\w-]{20,}|gh[pousr]_\w{30,}|xox[abprs]-[\w-]{20,}|AKIA[0-9A-Z]{16}|AIza[\w-]{30,})"
                       r"|(?i:bearer)\s+[\w.~+/=-]{24,}")


@dataclass
class Session:
    id: str
    title: str
    created: float
    updated: float
    history: list[dict[str, Any]] = field(default_factory=list)
    tasks: list[tuple[str, str]] = field(default_factory=list)


def sessions_dir() -> Path:
    return skills_dir().parent / "sessions"


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _clean(text: str) -> str:
    return _KEY_LIKE.sub("[key removed]", text)


def make_title(history: list[dict[str, Any]]) -> str:
    """The first user message's first line (its attachment blocks start at a `[File:` or `[Image` line)."""
    for turn in history:
        if turn.get("role") == "user":
            first = next((ln.strip() for ln in _clean(turn["content"]).splitlines() if ln.strip()), "")
            if first and not first.startswith("[Earlier conversation"):
                return first[:MAX_TITLE_CHARS]
    return "Chat"


def _body(sid: str, history: list[dict[str, Any]], tasks: list[tuple[str, str]], created: float, now: float) -> dict:
    return {"version": VERSION, "id": sid, "title": make_title(history), "created": created, "updated": now,
            "history": [{"role": t["role"], "content": _clean(t["content"]),
                         **({"said": _clean(t["said"])} if t.get("said") else {}),
                         **({"tools": [[_clean(n), _clean(o)] for n, o in t["tools"]]} if t.get("tools") else {})}
                        for t in history],  # no "images": attachment bytes are not kept
            "tasks": [list(t) for t in tasks]}


def save_session(sid: str, history: list[dict[str, Any]], tasks: list[tuple[str, str]] | None = None,
                 created: float | None = None, root: Path | None = None) -> bool:
    """Write the session atomically, dropping its oldest turns while it is over MAX_SESSION_BYTES, then trim the
    folder to MAX_SESSIONS. False when there is nothing to save or the disk refuses."""
    if not _ID.match(sid) or not history:
        return False
    root = root or sessions_dir()
    now = time.time()
    turns = list(history)
    # a compacted chat starts with its summary pair: it holds everything older, so it is never the part dropped
    head = 2 if len(turns) > 2 and str(turns[0]["content"]).startswith("[Earlier conversation") else 0

    def dump() -> bytes:
        return json.dumps(_body(sid, turns, list(tasks or []), created or now, now), ensure_ascii=False).encode("utf-8")

    data = dump()
    while len(data) > MAX_SESSION_BYTES and len(turns) > head + 2:
        # drop about the excess in one go (sizes measured once), cut before a user turn, then measure again
        over, cut = len(data) - MAX_SESSION_BYTES, head
        while cut < len(turns) - 2 and over > 0:
            t = turns[cut]
            over -= len(t["content"].encode("utf-8")) + sum(len(n) + len(o) for n, o in t.get("tools") or [])
            cut += 1
        while cut < len(turns) - 1 and turns[cut]["role"] != "user":
            cut += 1
        turns = turns[:head] + turns[cut:]
        data = dump()
    tmp_name = ""
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=root, suffix=".tmp", delete=False) as tmp:
            tmp_name = tmp.name
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, root / f"{sid}.json")
    except OSError:
        if tmp_name:
            Path(tmp_name).unlink(missing_ok=True)
        return False
    _trim(root)
    return True


def _trim(root: Path) -> None:
    try:
        files = sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[MAX_SESSIONS:]:
            old.unlink(missing_ok=True)
        for arch in root.glob("*.archive.txt"):  # an archive lives only as long as its session
            if not (root / f"{arch.name.split('.')[0]}.json").exists():
                arch.unlink(missing_ok=True)
        for leftover in root.glob("*.tmp"):  # a crash between write and replace
            if time.time() - leftover.stat().st_mtime > 3600:
                leftover.unlink(missing_ok=True)
    except OSError:
        pass


def _parse(path: Path) -> Session | None:
    """A Session from a file, or None when it is unreadable or not shaped like one."""
    try:
        raw = json.loads(path.read_text("utf-8"))
        sid, history = raw["id"], raw["history"]
        if not isinstance(sid, str) or sid != path.stem or not _ID.match(sid) or not isinstance(history, list):
            return None
        turns: list[dict[str, Any]] = []
        for turn in history:
            if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant") or not isinstance(
                    turn.get("content"), str):
                return None
            item: dict[str, Any] = {"role": turn["role"], "content": turn["content"]}
            if turn["role"] == "user" and isinstance(turn.get("said"), str) and turn["said"]:
                item["said"] = turn["said"]  # the user's typed words, which auto mode's reviewer trusts
            tools = turn.get("tools")
            if tools:
                if not isinstance(tools, list) or not all(isinstance(t, list) and len(t) == 2 and all(
                        isinstance(x, str) for x in t) for t in tools):
                    return None
                item["tools"] = [(n, o) for n, o in tools]
            turns.append(item)
        tasks = [(t[0][:MAX_TASK_CHARS], t[1]) for t in raw.get("tasks") or []
                 if isinstance(t, list) and len(t) == 2 and isinstance(t[0], str) and t[1] in TASK_STATUSES][:MAX_TASKS]
        title = raw.get("title")
        return Session(sid, title if isinstance(title, str) and title else make_title(turns),
                       float(raw.get("created") or 0), float(raw.get("updated") or 0), turns, tasks)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def list_sessions(root: Path | None = None) -> list[Session]:
    """Readable sessions, newest first (at most MAX_SESSIONS)."""
    root = root or sessions_dir()
    try:
        files = list(root.glob("*.json"))
    except OSError:
        return []
    found = [s for s in (_parse(p) for p in files) if s is not None and s.history]
    return sorted(found, key=lambda s: s.updated, reverse=True)[:MAX_SESSIONS]


def load_session(sid: str, root: Path | None = None) -> Session | None:
    return _parse((root or sessions_dir()) / f"{sid}.json") if _ID.match(sid) else None


MAX_ARCHIVE_BYTES = 8 * 1024 * 1024  # shortcut: stops appending past this, no rotation; upgrade if chats run longer


def archive_path(sid: str, root: Path | None = None) -> Path:
    return (root or sessions_dir()) / f"{sid}.archive.txt"


def archive_turns(sid: str, turns: list[dict[str, Any]], root: Path | None = None) -> Path | None:
    """Append turns about to be summarized, as plain text the chat's search_text and read_file can search.

    Compaction replaces them in the session file, so this is the only full copy. Summaries of earlier
    summaries are skipped. Returns the file, or None when nothing was written."""
    if not _ID.match(sid):
        return None
    parts = []
    for t in turns:
        if str(t["content"]).startswith("[Earlier conversation"):
            continue
        parts.append(f"## {t['role']}\n{_clean(t['content'])}\n")
        parts.extend(f"[tool {_clean(n)}] {_clean(o)}\n" for n, o in t.get("tools") or [])
    path = archive_path(sid, root)
    try:
        if not parts or (path.exists() and path.stat().st_size > MAX_ARCHIVE_BYTES):
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(parts) + "\n")
    except OSError:
        return None
    return path


def delete_session(sid: str, root: Path | None = None) -> None:
    """Remove one saved session and its archive; a bad id or a disk error just leaves it."""
    if _ID.match(sid):
        try:
            ((root or sessions_dir()) / f"{sid}.json").unlink(missing_ok=True)
            archive_path(sid, root).unlink(missing_ok=True)
        except OSError:
            pass


def delete_sessions(root: Path | None = None) -> None:
    """Remove every saved session."""
    root = root or sessions_dir()
    try:
        for path in [*root.glob("*.json"), *root.glob("*.archive.txt"), *root.glob("*.tmp")]:
            path.unlink(missing_ok=True)
    except OSError:
        pass
