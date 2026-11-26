"""Transactional checkpoints and per-task action ledger for safe autonomous runs."""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class LedgerEntry:
    ts: float
    tool: str
    summary: str
    ok: bool
    mutating: bool


@dataclass
class TaskCheckpoint:
    id: str
    root: Path
    created: float
    head: str
    stash_ref: str
    untracked_before: tuple[str, ...]


def _git(cwd: Path, *args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def create_checkpoint(root: Path | None) -> TaskCheckpoint | None:
    """Create a non-destructive git checkpoint before mutating work. Returns None outside a git repo."""
    if root is None or not root.exists():
        return None
    rc, top, _ = _git(root, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return None
    repo = Path(top)
    rc, head, _ = _git(repo, "rev-parse", "HEAD")
    if rc != 0 or not head:
        return None
    rc, untracked_raw, _ = _git(repo, "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z")
    if rc != 0:
        return None
    untracked = tuple(p for p in untracked_raw.split("\0") if p)
    rc, stash_ref, _ = _git(repo, "stash", "create")
    return TaskCheckpoint(
        id=uuid.uuid4().hex[:8],
        root=repo,
        created=time.time(),
        head=head,
        stash_ref=stash_ref if rc == 0 else "",
        untracked_before=untracked,
    )


def revert_checkpoint(cp: TaskCheckpoint) -> str:
    """Restore tracked files to pre-task state and remove only untracked files created after the checkpoint."""
    rc, _, err = _git(cp.root, "reset", "--hard", cp.head)
    if rc != 0:
        return f"error: git reset failed: {err or 'unknown error'}"
    rc, now_untracked, _ = _git(cp.root, "-c", "core.quotePath=false", "ls-files", "--others", "--exclude-standard", "-z")
    before = set(cp.untracked_before)
    removed = 0
    if rc == 0:
        root_resolved = cp.root.resolve()
        for rel in now_untracked.split("\0"):
            if not rel or rel in before:
                continue
            target = (cp.root / rel).resolve()
            try:
                if target.is_relative_to(root_resolved) and target.is_file():
                    target.unlink()
                    removed += 1
            except OSError:
                pass
    if cp.stash_ref:
        rc, _, err = _git(cp.root, "stash", "apply", "--quiet", cp.stash_ref)
        if rc != 0:
            return f"error: restored HEAD and removed {removed} new file(s), but stash apply failed: {err}"
    return f"reverted checkpoint {cp.id} (removed {removed} new file(s))"
