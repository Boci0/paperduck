"""Warm Windows PowerShell workers: starting powershell.exe and loading .NET costs about 0.5s, which screen_map paid
twice (UI Automation and OCR) on every look. One worker per kind runs scripts sent over stdin and exits by itself
after IDLE_S without a request, so an idle pet holds no PowerShell. Any failure returns None and the caller runs its
script the old one-shot way."""

from __future__ import annotations

import base64
import json
import subprocess
import threading
import uuid
from typing import Any

IDLE_S = 90

# Reads one JSON request per line ({"script", "args", "end"}), runs the script with its named args, prints the output
# and then the end marker. ReadLineAsync with a wait lets it quit when idle (or when the pet's end of the pipe closes).
_LOOP = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$in = [Console]::In
while ($true) {
    $read = $in.ReadLineAsync()
    if (-not $read.Wait(%d000)) { break }
    $line = $read.Result
    if ($null -eq $line) { break }
    $req = $line | ConvertFrom-Json
    $named = @{}
    foreach ($p in $req.args.psobject.Properties) { $named[$p.Name] = $p.Value }
    $ok = 1
    try { $out = @(& ([scriptblock]::Create($req.script)) @named) -join "`n" } catch { $out = ''; $ok = 0 }
    [Console]::Out.Write($out)
    [Console]::Out.WriteLine("`n" + $req.end + " " + $ok)
    [Console]::Out.Flush()
}
""" % IDLE_S
_ENCODED = base64.b64encode(_LOOP.encode("utf-16-le")).decode()  # -Command would mangle the quotes on this line


class _Worker:
    def __init__(self) -> None:
        self.proc: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()

    def run(self, script: str, args: dict[str, Any], timeout_s: float) -> str | None:
        with self.lock:
            for _attempt in range(2):  # the worker may have quit while idle: start it once more
                if self.proc is None or self.proc.poll() is not None:
                    try:
                        self.proc = subprocess.Popen(
                            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                             "-EncodedCommand", _ENCODED], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    except OSError:
                        return None
                out = self._ask(script, args, timeout_s)
                if out is not None:
                    return out
            return None

    def _cleanup_proc(self, proc: subprocess.Popen[bytes]) -> None:
        try:
            proc.kill()
        except OSError:
            pass
        for stream in (proc.stdin, proc.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        try:
            proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if self.proc is proc:
            self.proc = None

    def _ask(self, script: str, args: dict[str, Any], timeout_s: float) -> str | None:
        proc, end = self.proc, uuid.uuid4().hex
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write((json.dumps({"script": script, "args": args, "end": end}) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError:
            self._cleanup_proc(proc)
            return None
        lines: list[bytes] = []
        status = [""]

        def read() -> None:
            marker = end.encode()
            for raw in iter(proc.stdout.readline, b""):
                if raw.startswith(marker):
                    status[0] = raw.decode(errors="replace").split()[-1]
                    return
                lines.append(raw)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader.join(timeout_s)
        if reader.is_alive() or not status[0]:  # hung or died mid-script: never reuse it
            timed_out = reader.is_alive()
            self._cleanup_proc(proc)
            return "" if timed_out else None
        if status[0] != "1":
            return ""  # the script failed, as a non-zero exit did before
        return b"".join(lines).decode("utf-8", errors="replace").strip()


_workers: dict[str, _Worker] = {}
_workers_lock = threading.Lock()


def run(kind: str, script: str, args: dict[str, Any], timeout_s: float = 25.0) -> str | None:
    """Output of `script` run with named `args` on the warm worker for `kind` ('' when the script failed or timed
    out), or None when no worker could run it."""
    with _workers_lock:
        worker = _workers.setdefault(kind, _Worker())
    return worker.run(script, args, timeout_s)


def close_all() -> None:
    """Stop all warm PowerShell workers cleanly on shutdown."""
    with _workers_lock:
        workers = list(_workers.values())
        _workers.clear()
    for w in workers:
        with w.lock:
            if w.proc is not None:
                w._cleanup_proc(w.proc)
