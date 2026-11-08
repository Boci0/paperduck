"""GitHub release check and in-place source update for an installed Paperduck.

Updates must be signed: a release carries `paperduck.zip` plus `paperduck.zip.sig`, an Ed25519
signature made with the maintainer's offline key (scripts/release.py). HTTPS only proves the file
came from GitHub; the signature proves it came from the key holder, so a hijacked GitHub account
or an intercepted download cannot ship code. The version inside the zip must match the tag and be
newer, so an old signed release cannot be replayed under a new tag to downgrade an install.

A frozen (PyInstaller) build updates from `paperduck-win64.zip` instead: the whole app folder, runtime
included. Windows will not rename a folder whose exe or DLLs are loaded, so the new build is staged as
`app.new` and the swap runs from there (`--finish-update`) once the old process has exited.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from src import __version__
from src.signing import verify

logger = logging.getLogger(__name__)

REPO = "Boci0/paperduck"
LATEST_API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_URL = f"https://github.com/{REPO}/releases"
FROZEN = bool(getattr(sys, "frozen", False))
# Frozen: the folder holding Paperduck.exe (`<root>/app`); source: the folder holding src/
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parents[1]
# install.ps1 writes this marker; a git checkout has none, so the updater never overwrites dev work
INSTALL_MARKER = ".installed"
SOURCE_ASSET = "paperduck.zip"
FROZEN_ASSET = "paperduck-win64.zip"
FROZEN_EXE = "Paperduck.exe"
_MAX_ZIP_BYTES = (80 if FROZEN else 20) * 1024 * 1024
ASSET_NAME = FROZEN_ASSET if FROZEN else SOURCE_ASSET
SIG_NAME = ASSET_NAME + ".sig"
# Public half of the release signing key; the private half never leaves the maintainer's PC
RELEASE_PUBLIC_KEY = bytes.fromhex("f0cc212a80d53fe877827253c020e2c01d023c4280eebf47423c6032a0259316")
_TIMEOUT_S = 15
CACHE_NAME = "update-cache"  # beside app/: the startup check downloads a newer bundle here, so installing is instant


def parse_version(tag: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3); non-numeric parts are ignored."""
    return tuple(int(n) for n in re.findall(r"\d+", tag)[:3])


def is_newer(tag: str, current: str = __version__) -> bool:
    return parse_version(tag) > parse_version(current)


def is_installed(app_dir: Path = APP_DIR) -> bool:
    return (app_dir / INSTALL_MARKER).is_file()


RETRY_WAITS_S = (2.0, 8.0)  # GitHub's download servers time out (502/503/504) for minutes at a time


def _get(url: str, limit: int, progress: Callable[[int], None] | None = None) -> bytes:
    """`_get_once`, retried after RETRY_WAITS_S when GitHub or the network fails; a final failure is an OSError
    that says plainly whose side it is on (it reaches the user as "Update failed: ...")."""
    for wait in (*RETRY_WAITS_S, None):
        try:
            return _get_once(url, limit, progress)
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:  # 404 and friends: retrying will not help
                raise
            if wait is None:
                raise OSError(f"GitHub is not responding (HTTP {exc.code}); try again in a few minutes") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            if wait is None:
                reason = getattr(exc, "reason", None) or exc
                raise OSError(f"could not reach GitHub ({reason}); check the connection and try again") from exc
        logger.info("download of %s failed, retrying in %.0f s", url, wait)
        time.sleep(wait)
    raise AssertionError("unreachable")


def _get_once(url: str, limit: int, progress: Callable[[int], None] | None = None) -> bytes:
    """Download up to `limit` bytes; `progress` gets the percent done whenever it changes (needs Content-Length)."""
    req = urllib.request.Request(url, headers={"User-Agent": f"Paperduck/{__version__}"})
    chunks: list[bytes] = []
    size, shown = 0, -1
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        while chunk := resp.read(256 * 1024):
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise ValueError("download too large")
            pct = min(100, size * 100 // total) if total else -1
            if progress is not None and pct != shown:
                shown = pct
                progress(pct)
    return b"".join(chunks)


def latest_release() -> tuple[str, str, str]:
    """(tag, zip url, signature url) of the newest release; urls are '' when it is not signed."""
    info = json.loads(_get(LATEST_API, 1024 * 1024))
    assets = {a.get("name"): str(a.get("browser_download_url") or "") for a in info.get("assets") or []
              if isinstance(a, dict)}
    tag = str(info["tag_name"])
    if not re.fullmatch(r"v?\d+(\.\d+){0,2}", tag):  # the tag names cache files: no `..`, `\` or `:` from the API
        raise ValueError("bad release tag")
    return tag, assets.get(ASSET_NAME, ""), assets.get(SIG_NAME, "")


def _bundle(tag: str, zip_url: str, sig_url: str, progress: Callable[[int], None] | None = None) -> bytes:
    """The verified bundle for `tag`: the prefetched copy when it still verifies, else downloaded and cached."""
    cache = APP_DIR.parent / CACHE_NAME
    zip_path, sig_path = cache / f"{tag}-{ASSET_NAME}", cache / f"{tag}-{SIG_NAME}"
    if zip_path.is_file() and sig_path.is_file():
        try:  # verified again on every use: the cache is just files on disk
            return verified_bundle(zip_path.read_bytes(), sig_path.read_bytes(), tag)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            logger.warning("cached update %s unusable, downloading again: %s", tag, exc)
    if not zip_url or not sig_url:
        raise ValueError("unsigned release")
    sig = _get(sig_url, 1024)
    data = verified_bundle(_get(zip_url, _MAX_ZIP_BYTES, progress), sig, tag)
    try:
        shutil.rmtree(cache, ignore_errors=True)  # only one release is ever kept
        cache.mkdir(parents=True, exist_ok=True)
        zip_path.write_bytes(data)
        sig_path.write_bytes(sig)  # written last: a zip cut off mid-write has no signature beside it
    except OSError as exc:
        logger.warning("could not cache update %s: %s", tag, exc)
    return data


def zip_version(data: bytes) -> str:
    """`__version__` declared in the bundle's src/__init__.py, or a frozen bundle's top-level VERSION ('' if missing)."""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            parts = Path(name).parts
            if len(parts) == 2 and parts[1] == "VERSION":
                return zf.read(name).decode("ascii", "replace").strip()
            if len(parts) == 3 and parts[1:] == ("src", "__init__.py"):
                match = re.search(r"__version__\s*=\s*[\"']([^\"']+)", zf.read(name).decode("utf-8", "replace"))
                return match.group(1) if match else ""
    return ""


def verified_bundle(data: bytes, sig_text: bytes, tag: str, public: bytes | None = None,
                    current: str = __version__) -> bytes:
    """Return `data` only if it is signed by the release key and really is the newer `tag`; else raise."""
    try:
        signature = bytes.fromhex(sig_text.decode("ascii").strip())
    except ValueError:
        raise ValueError("bad signature") from None
    if not verify(public or RELEASE_PUBLIC_KEY, data, signature):
        raise ValueError("bad signature")
    version = zip_version(data)
    if not version or parse_version(version) != parse_version(tag) or not is_newer(version, current):
        raise ValueError("version mismatch")
    return data


def _bundle_requirements(data: bytes) -> str | None:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if len(Path(name).parts) == 2 and Path(name).name == "requirements.txt":
                return zf.read(name).decode("utf-8-sig", "replace")
    return None


def _pins(text: str) -> set[str]:
    """The `name==version` pins of a requirements file: hash lines, comments, extras and markers ignored,
    names normalised the way pip does (case-folded, runs of `-` `_` `.` as one `-`)."""
    found = (re.match(r"\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s\\;]+)", line)
             for line in text.splitlines())
    return {f"{re.sub(r'[-_.]+', '-', m[1]).lower()}=={m[2]}" for m in found if m}


def install_source_zip(data: bytes, app_dir: Path = APP_DIR) -> None:
    """Swap `app_dir/src` for the `src/` folder inside a verified release bundle.

    The new tree is fully extracted next to the old one before a rename swap, so a bad zip
    leaves the running install untouched. A bundle whose pinned libraries differ from the installed
    ones is refused ("needs installer"): new code on old libraries can crash at start, and the running
    app cannot safely replace its own libraries, so the user re-runs install.ps1 for that release.
    """
    new_dir, old_dir = app_dir / "src.new", app_dir / "src.old"
    shutil.rmtree(new_dir, ignore_errors=True)
    shutil.rmtree(old_dir, ignore_errors=True)
    wanted = _bundle_requirements(data)
    have = app_dir / "requirements.txt"
    if wanted is not None and have.is_file():  # checked before extracting, so a refusal leaves nothing behind
        try:
            installed = have.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            raise ValueError("needs installer") from None  # cannot tell what is installed: do not guess
        if _pins(wanted) != _pins(installed):
            raise ValueError("needs installer")
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            # Bundles wrap everything in one top folder ('paperduck/', or '<owner>-<repo>-<sha>/')
            if len(parts) < 3 or parts[1] != "src" or info.is_dir():
                continue
            rel = Path(*parts[2:])
            if rel.is_absolute() or rel.drive or ".." in rel.parts:  # 'D:x' is drive-relative, not absolute
                raise ValueError(f"unsafe path in update: {info.filename}")
            dest = new_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
    if not (new_dir / "main.py").is_file():
        shutil.rmtree(new_dir, ignore_errors=True)
        raise ValueError("update has no src/main.py")
    (app_dir / "src").rename(old_dir)
    try:
        new_dir.rename(app_dir / "src")
    except OSError:
        old_dir.rename(app_dir / "src")  # e.g. antivirus still scanning src.new: keep the old code, not none
        raise
    shutil.rmtree(old_dir, ignore_errors=True)
    if wanted is not None:
        try:
            have.write_text(wanted, encoding="utf-8", newline="")  # same pins, maybe new hashes: keep the file current
        except OSError:
            pass  # the new src is already in place; a stale copy only costs a later comparison


def install_frozen_zip(data: bytes, app_dir: Path = APP_DIR) -> None:
    """Stage the `Paperduck/` folder of a verified bundle as `app.new` for `finish_frozen_update`.

    The running app is not touched. No library check: a frozen bundle carries its own runtime.
    """
    new_dir = app_dir.with_name(app_dir.name + ".new")
    shutil.rmtree(new_dir, ignore_errors=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            parts = Path(info.filename).parts
            if len(parts) < 2 or info.is_dir():
                continue
            rel = Path(*parts[1:])
            if rel.is_absolute() or rel.drive or ".." in rel.parts:
                raise ValueError(f"unsafe path in update: {info.filename}")
            dest = new_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(zf.read(info))
    if not (new_dir / FROZEN_EXE).is_file():
        shutil.rmtree(new_dir, ignore_errors=True)
        raise ValueError(f"update has no {FROZEN_EXE}")
    if is_installed(app_dir):
        (new_dir / INSTALL_MARKER).write_bytes((app_dir / INSTALL_MARKER).read_bytes())


def _wait_for_exit(pid: int, timeout_ms: int = 30_000) -> bool:
    """True once process `pid` has exited (or never existed), False if it is still running at the timeout."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return True
    try:
        return kernel32.WaitForSingleObject(ctypes.c_void_p(handle), timeout_ms) == 0  # WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


def launch_env() -> dict[str, str]:
    """This environment minus the Qt DLL folder PyQt6 put on PATH, so a launched build loads only its own DLLs."""
    path = os.environ.get("PATH", "").split(os.pathsep)
    return {**os.environ, "PATH": os.pathsep.join(p for p in path if "pyqt6" not in p.lower())}


def _starts(app_dir: Path) -> bool:
    """Test-launch a swapped-in frozen build: False if it cannot even load (missing DLLs, broken files)."""
    import subprocess

    try:
        return subprocess.run([str(app_dir / FROZEN_EXE), "--self-check"], cwd=str(app_dir.parent),
                              env=launch_env(), timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def finish_frozen_update(old_pid: int, new_dir: Path = APP_DIR,
                         starts: Callable[[Path], bool] = _starts) -> Path | None:
    """Run from the staged `app.new` copy: once `old_pid` exits, replace `app` with a copy of this folder.

    Copied, not renamed, because this process runs from `app.new`. A failed copy, or a copy that does not
    start (a v1.8.4 swap lost every Qt DLL and bricked the install), puts the old folder back.
    Returns the app folder to launch, or None if the old copy is still running (launching would make two).
    """
    if not new_dir.name.endswith(".new"):
        raise ValueError(f"not a staged update: {new_dir}")
    app_dir = new_dir.with_name(new_dir.name.removesuffix(".new"))
    old_dir = app_dir.with_name(app_dir.name + ".old")
    if not _wait_for_exit(old_pid):
        return None
    shutil.rmtree(old_dir, ignore_errors=True)
    for attempt in range(20):  # antivirus may still hold the old exe for a moment after it exits
        try:
            app_dir.rename(old_dir)
            break
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.25)
    try:
        shutil.copytree(new_dir, app_dir)
        if not starts(app_dir):
            raise OSError("the new build does not start")
    except OSError:
        shutil.rmtree(app_dir, ignore_errors=True)
        old_dir.rename(app_dir)
        raise
    return app_dir


def remove_old_build(app_dir: Path = APP_DIR) -> None:
    """Delete the replaced `app.old` and the staged `app.new` once a frozen update has been swapped in."""
    for suffix in (".old", ".new"):
        shutil.rmtree(app_dir.with_name(app_dir.name + suffix), ignore_errors=True)


class UpdateJob(QObject):
    """Checks GitHub (and, when `install`, applies the update) on a worker thread.

    `finished` delivers (status, tag) on the Qt main thread; status is one of
    'current', 'available', 'ready' (downloaded and verified in the background), 'installed', 'error'.
    """

    finished = pyqtSignal(str, str)
    progress = pyqtSignal(int)  # percent of the bundle downloaded

    def start(self, *, install: bool) -> None:
        threading.Thread(target=self._run, args=(install,), daemon=True).start()

    def _run(self, install: bool) -> None:
        tag = ""
        try:
            tag, zip_url, sig_url = latest_release()
            if not is_newer(tag):
                shutil.rmtree(APP_DIR.parent / CACHE_NAME, ignore_errors=True)  # the cached update is installed
                self.finished.emit("current", tag)
                return
            if not is_installed():
                self.finished.emit("available", tag)
                return
            if not install:  # the quiet startup check: fetch it now, at whatever speed GitHub gives
                try:
                    _bundle(tag, zip_url, sig_url)
                except (OSError, ValueError, zipfile.BadZipFile, http.client.HTTPException) as exc:
                    logger.warning("prefetching update %s failed: %s: %s", tag, type(exc).__name__, exc)
                    self.finished.emit("available", tag)  # still say it exists; installing retries and explains
                    return
                self.finished.emit("ready", tag)
                return
            apply = install_frozen_zip if FROZEN else install_source_zip
            apply(_bundle(tag, zip_url, sig_url, self.progress.emit))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, http.client.HTTPException) as exc:
            logger.warning("update %s failed: %s: %s", tag or "check", type(exc).__name__, exc)
            self.finished.emit("error", str(exc) or tag)
            return
        self.finished.emit("installed", tag)
