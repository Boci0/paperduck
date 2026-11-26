"""Project tool plugin: non-blocking Git status HUD plus a terminal / VS Code / Explorer launcher."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)


@dataclass(frozen=True)
class GitStatus:
    branch: str
    changed: int
    ahead: int
    behind: int


def git_notice(old: GitStatus | None, new: GitStatus) -> tuple[str, str] | None:
    """A commit or push seen between two polls of the same branch, as a (toast, colour) notice.
    Only `ahead` moves with `behind` unchanged: a pull, fetch or branch switch says nothing."""
    if old is None or old.branch != new.branch or old.behind != new.behind:
        return None
    if new.ahead > old.ahead:
        return ("COMMITTED", "#38B764")
    if old.ahead > 0 and new.ahead == 0:
        return ("PUSHED", "#38B764")
    return None


def parse_git_status(text: str) -> GitStatus:
    """Parse `git status --porcelain=v2 --branch` output."""
    branch, changed, ahead, behind = "?", 0, 0, 0
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            branch = line.removeprefix("# branch.head ").strip()
        elif line.startswith("# branch.ab "):
            for token in line.removeprefix("# branch.ab ").split():
                if token[1:].isdigit():
                    if token[0] == "+":
                        ahead = int(token[1:])
                    elif token[0] == "-":
                        behind = int(token[1:])
        elif line and not line.startswith("#"):
            changed += 1
    return GitStatus(branch="detached" if branch == "(detached)" else branch,
                     changed=changed, ahead=ahead, behind=behind)


class ProjectPlugin:
    """Tracks one project folder: live Git branch/changes HUD and quick launchers."""

    REFRESH_SECONDS: float = 10.0
    MAX_RECENT: int = 5
    SUBMENU_NAME: str = "Project"

    def __init__(self) -> None:
        self._repo: Path | None = None
        self._recent: list[str] = []
        self._status: GitStatus | None = None
        self._error: str = ""
        self._proc: subprocess.Popen[bytes] | None = None
        self._out: Any = None
        self._code_exe: str | None = shutil.which("code")  # temp file holding the running git call's stdout
        self._next_run: float = 0.0
        self._git = shutil.which("git")
        self._notice: tuple[str, str] | None = None

    @property
    def tool_id(self) -> str:
        return "project"

    @property
    def display_name(self) -> str:
        return "Project"

    @property
    def tick_interval_ms(self) -> int:
        return 1_000

    @property
    def repo(self) -> Path | None:
        return self._repo

    @property
    def recent(self) -> tuple[str, ...]:
        return tuple(self._recent)

    @property
    def status(self) -> GitStatus | None:
        return self._status

    @property
    def has_pending_update(self) -> bool:
        return self._proc is not None

    def set_repo(self, path: str | Path) -> None:
        self._kill_proc()
        self._repo = Path(path)
        key = str(self._repo)
        self._recent = [key] + [r for r in self._recent if r != key][: self.MAX_RECENT - 1]
        self._status = None
        self._error = ""
        self._next_run = 0.0

    def _kill_proc(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._proc = None
            if self._out is not None:
                try:
                    self._out.close()
                except OSError:
                    pass
                self._out = None

    def tick(self) -> bool:
        # Popen + poll keeps the UI thread free; git output is collected on a later tick
        if self._proc is not None:
            if self._proc.poll() is None:
                return False
            code = self._proc.returncode
            self._out.seek(0)
            out = self._out.read().decode("utf-8", "replace")
            self._out.close()
            self._proc = None
            if code == 0:
                old, self._status, self._error = self._status, parse_git_status(out), ""
                self._notice = git_notice(old, self._status) or self._notice
            else:
                self._status, self._error = None, "Not a git repo"
            return True
        if self._repo is None or self._git is None or time.monotonic() < self._next_run:
            return False
        if not self._repo.is_dir() or not (self._repo / ".git").exists():
            self._next_run = time.monotonic() + self.REFRESH_SECONDS
            if self._status is not None or not self._error:
                self._status, self._error = None, "Not a git repo"
                return True
            return False
        self._next_run = time.monotonic() + self.REFRESH_SECONDS
        out = None
        try:
            out = tempfile.TemporaryFile()
            self._proc = subprocess.Popen(
                [self._git, "-C", str(self._repo), "status", "--porcelain=v2", "--branch"],
                # A temp file, not a PIPE: polled-but-unread pipes fill at 4 KB on Windows and git blocks forever
                stdout=out, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
            self._out = out
        except OSError:
            if out is not None:
                try:
                    out.close()
                except OSError:
                    pass
            self._status = None
            self._error = "git failed to start"
            return True
        return False

    def pop_notification(self) -> tuple[str, str] | None:
        notice, self._notice = self._notice, None
        return notice

    def desired_state(self) -> CompanionState | None:
        return None

    def get_hud_data(self) -> HudData:
        st = self._status
        changed, ahead, behind = (st.changed, st.ahead, st.behind) if st else (0, 0, 0)
        if self._repo is None:
            detail = "Pick a project folder"
        elif self._git is None:
            detail = "git not found on PATH"
        elif self._error:
            detail = f"{self._error}  {self._repo.name}"
        elif st is None:
            detail = f"Reading git...  {self._repo.name}"
        else:
            detail = f"{st.branch}  {self._repo.name}"
        dirty = changed > 0
        return HudData(
            badge_left="PROJECT",
            badge_right="DIRTY" if dirty else ("CLEAN" if st else "IDLE"),
            badge_right_color="#FFCD75" if dirty else "#38B764",
            bars=(
                HudBar(label="Changed", value=min(100.0, changed * 5.0), color_hex="#FFCD75",
                       display_text=f"{min(999, changed):3d}"),
                HudBar(label="Ahead", value=min(100.0, ahead * 10.0), color_hex="#38B764",
                       display_text=f"{min(999, ahead):3d}"),
                HudBar(label="Behind", value=min(100.0, behind * 10.0), color_hex="#EF7D57",
                       display_text=f"{min(999, behind):3d}"),
            ),
            detail=detail,
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        sub = self.SUBMENU_NAME
        has_repo = self._repo is not None
        items = [
            ToolMenuItem(label="Open in VS Code", callback_id="project:code",
                         enabled=has_repo and self._code_exe is not None, submenu=sub),
            ToolMenuItem(label="Open Terminal Here", callback_id="project:terminal", enabled=has_repo, submenu=sub),
            ToolMenuItem(label="Open in Explorer", callback_id="project:explorer", enabled=has_repo, submenu=sub),
            ToolMenuItem(label="Copy Branch Name", callback_id="project:branch",
                         enabled=self._status is not None, submenu=sub),
            ToolMenuItem(label="Refresh Git Status", callback_id="project:refresh", enabled=has_repo, submenu=sub),
        ]
        for idx, path in enumerate(self._recent):
            if self._repo is not None and path == str(self._repo):
                continue
            items.append(ToolMenuItem(label=f"Switch to {Path(path).name}",
                                      callback_id=f"project:switch:{idx}", submenu=sub))
        items.append(ToolMenuItem(label="Choose Project Folder...", callback_id="project:pick", submenu=sub))
        return items

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id.startswith("project:switch:"):
            idx_txt = callback_id.removeprefix("project:switch:")
            if idx_txt.isdigit() and int(idx_txt) < len(self._recent):
                self.set_repo(self._recent[int(idx_txt)])
                return (True, self._repo.name[:10].upper() if self._repo else "", "#73EFF7")
            return (True, "GONE", "#EF7D57")
        if callback_id == "project:refresh":
            self._next_run = 0.0
            return (True, "REFRESH", "#73EFF7")
        if callback_id == "project:branch":
            if self._status is None:
                return (True, "NO BRANCH", "#EF7D57")
            from PyQt6.QtGui import QGuiApplication

            cb = QGuiApplication.clipboard()
            if cb is not None:
                cb.setText(self._status.branch)
            return (True, "BRANCH", "#38B764")
        if callback_id not in ("project:code", "project:terminal", "project:explorer"):
            return (False, "", "")
        if self._repo is None or not self._repo.is_dir():
            return (True, "NO FOLDER", "#EF7D57")
        path = str(self._repo)
        try:
            if callback_id == "project:code":
                code = shutil.which("code")
                if code is None:
                    return (True, "NO VSCODE", "#EF7D57")
                subprocess.Popen([code, path], creationflags=_NO_WINDOW)
                return (True, "VS CODE", "#41A6F6")
            if callback_id == "project:terminal":
                wt = shutil.which("wt")
                if wt is not None:
                    subprocess.Popen([wt, "-d", path])
                else:
                    subprocess.Popen(["cmd.exe", "/k"], cwd=path, creationflags=_NEW_CONSOLE)
                return (True, "TERMINAL", "#41A6F6")
            os.startfile(path)
            return (True, "EXPLORER", "#41A6F6")
        except OSError:
            return (True, "OPEN ERR", "#EF7D57")

    def close(self) -> None:
        self._kill_proc()

    def to_dict(self) -> dict[str, Any]:
        return {"repo": str(self._repo) if self._repo else "", "recent": list(self._recent)}

    def load_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        recent = data.get("recent")
        if isinstance(recent, list):
            self._recent = [r for r in recent if isinstance(r, str) and r][: self.MAX_RECENT]
        repo = data.get("repo")
        if isinstance(repo, str) and repo:
            self.set_repo(repo)
