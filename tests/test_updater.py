"""Unit tests for the GitHub release updater."""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import pytest

from src.updater import install_source_zip, is_installed, is_newer, parse_version


def _zipball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(f"Boci0-paperduck-abc123/{name}", text)
    return buf.getvalue()


def _app(tmp_path: Path) -> Path:
    """`<tmp>/app` like a real install, so the update cache beside it stays inside this test's folder."""
    (tmp_path / "app" / "src").mkdir(parents=True)
    (tmp_path / "app" / "src" / "main.py").write_text("old")
    return tmp_path / "app"


def test_version_compare() -> None:
    assert parse_version("v1.10.2") == (1, 10, 2)
    assert is_newer("v0.10.0", "0.9.0")
    assert not is_newer("v0.9.0", "0.9.0")
    assert not is_newer("0.8.5", "0.9.0")


def test_install_swaps_src_only(tmp_path: Path) -> None:
    app = _app(tmp_path)
    install_source_zip(_zipball({"src/main.py": "new", "src/ui/x.py": "x", "README.md": "skip"}), app)
    assert (app / "src" / "main.py").read_text() == "new"
    assert (app / "src" / "ui" / "x.py").read_text() == "x"
    assert not (app / "README.md").exists()
    assert not (app / "src.old").exists() and not (app / "src.new").exists()


def test_bad_update_keeps_running_install(tmp_path: Path) -> None:
    app = _app(tmp_path)
    with pytest.raises(ValueError):
        install_source_zip(_zipball({"src/other.py": "no main"}), app)
    with pytest.raises(ValueError):
        install_source_zip(_zipball({"src/../../evil.py": "x", "src/main.py": "new"}), app)
    # On Windows 'src.new' / 'Q:evil.py' is just 'Q:evil.py': another drive's working folder
    other_drive = "Q" if not str(tmp_path).upper().startswith("Q") else "R"
    with pytest.raises(ValueError):
        install_source_zip(_zipball({f"src/{other_drive}:evil.py": "x", "src/main.py": "new"}), app)
    assert (app / "src" / "main.py").read_text() == "old"
    assert not (tmp_path.parent / "evil.py").exists()


def test_locked_new_src_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = _app(tmp_path)
    real_rename = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == "src.new":
            raise PermissionError("in use by antivirus")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    with pytest.raises(PermissionError):
        install_source_zip(_zipball({"src/main.py": "new"}), app)
    assert (app / "src" / "main.py").read_text() == "old"


def test_git_checkout_is_not_an_install(tmp_path: Path) -> None:
    assert not is_installed(tmp_path)
    (tmp_path / ".installed").write_text("")
    assert is_installed(tmp_path)


# --- Signed releases -------------------------------------------------------

import json  # noqa: E402
import threading  # noqa: E402
import urllib.error  # noqa: E402
from http.server import BaseHTTPRequestHandler, HTTPServer  # noqa: E402

from src import signing, updater  # noqa: E402

_TEST_SECRET = bytes(range(32))
_TEST_PUBLIC = signing.public_key(_TEST_SECRET)


def test_ed25519_matches_rfc8032_vectors() -> None:
    sk = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
    pk = signing.public_key(sk)
    assert pk.hex() == "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
    sig = signing.sign(sk, b"\x72")
    assert sig.hex() == ("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da"
                         "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
    assert signing.verify(pk, b"\x72", sig)
    assert not signing.verify(pk, b"\x73", sig)
    assert not signing.verify(pk, b"\x72", sig[:63] + bytes([sig[63] ^ 1]))
    assert not signing.verify(pk, b"\x72", sig[:32] + (2**253).to_bytes(32, "little"))  # s >= L
    assert not signing.verify(b"\x00" * 31, b"", sig) and not signing.verify(pk, b"", b"short")


def _bundle(version: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("paperduck/src/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr("paperduck/src/main.py", f"v{version}")
    return buf.getvalue()


def _sig(data: bytes, secret: bytes = _TEST_SECRET) -> bytes:
    return signing.sign(secret, data).hex().encode() + b"\n"


def test_verified_bundle_rejects_tampering_wrong_key_and_replays() -> None:
    data = _bundle("1.0.0")
    assert updater.verified_bundle(data, _sig(data), "v1.0.0", _TEST_PUBLIC, "0.9.2") == data
    with pytest.raises(ValueError, match="bad signature"):
        updater.verified_bundle(data + b"x", _sig(data), "v1.0.0", _TEST_PUBLIC, "0.9.2")
    with pytest.raises(ValueError, match="bad signature"):
        updater.verified_bundle(data, _sig(data, bytes(32)), "v1.0.0", _TEST_PUBLIC, "0.9.2")
    with pytest.raises(ValueError, match="bad signature"):
        updater.verified_bundle(data, b"not hex", "v1.0.0", _TEST_PUBLIC, "0.9.2")
    old = _bundle("0.9.0")  # genuinely signed but older: replayed under a new tag to downgrade
    with pytest.raises(ValueError, match="version mismatch"):
        updater.verified_bundle(old, _sig(old), "v1.0.0", _TEST_PUBLIC, "0.9.2")
    with pytest.raises(ValueError, match="version mismatch"):
        updater.verified_bundle(old, _sig(old), "v0.9.0", _TEST_PUBLIC, "0.9.2")
    with pytest.raises(ValueError, match="bad signature"):  # the real key never signed test data
        updater.verified_bundle(data, _sig(data), "v1.0.0", None, "0.9.2")


class _FakeGitHub(BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}

    def do_GET(self) -> None:  # noqa: N802
        body = self.files.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        if body is not None:
            self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


@pytest.mark.parametrize("case", ["signed", "unsigned", "tampered"])
def test_update_job_installs_only_signed_releases(case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    data = _bundle("99.0.0")
    assets = [] if case == "unsigned" else [
        {"name": "paperduck.zip", "browser_download_url": f"{base}/zip"},
        {"name": "paperduck.zip.sig", "browser_download_url": f"{base}/sig"},
    ]
    _FakeGitHub.files = {
        "/latest": json.dumps({"tag_name": "v99.0.0", "zipball_url": f"{base}/zip", "assets": assets}).encode(),
        "/zip": data + (b"evil" if case == "tampered" else b""),
        "/sig": _sig(data),
    }
    app = _app(tmp_path)
    (app / ".installed").write_text("")
    monkeypatch.setattr(updater, "LATEST_API", f"{base}/latest")
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEY", _TEST_PUBLIC)
    monkeypatch.setattr(updater, "APP_DIR", app)
    monkeypatch.setattr(updater, "is_installed", lambda: True)
    real_install = updater.install_source_zip
    monkeypatch.setattr(updater, "install_source_zip", lambda d: real_install(d, app))
    results: list[tuple[str, str]] = []
    job = updater.UpdateJob()
    job.finished.connect(lambda status, tag: results.append((status, tag)))
    try:
        job._run(install=True)
    finally:
        server.shutdown()
    expected = {"signed": ("installed", "v99.0.0"), "unsigned": ("error", "unsigned release"),
                "tampered": ("error", "bad signature")}[case]
    assert results == [expected]
    assert (app / "src" / "main.py").read_text() == ("v99.0.0" if case == "signed" else "old")


def test_startup_check_prefetches_so_install_needs_no_download(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    server = HTTPServer(("127.0.0.1", 0), _FakeGitHub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    data = _bundle("99.0.0")
    assets = [{"name": "paperduck.zip", "browser_download_url": f"{base}/zip"},
              {"name": "paperduck.zip.sig", "browser_download_url": f"{base}/sig"}]
    _FakeGitHub.files = {"/latest": json.dumps({"tag_name": "v99.0.0", "assets": assets}).encode(),
                         "/zip": data, "/sig": _sig(data)}
    app = _app(tmp_path)
    monkeypatch.setattr(updater, "LATEST_API", f"{base}/latest")
    monkeypatch.setattr(updater, "RELEASE_PUBLIC_KEY", _TEST_PUBLIC)
    monkeypatch.setattr(updater, "APP_DIR", app)
    monkeypatch.setattr(updater, "is_installed", lambda: True)
    real_install = updater.install_source_zip
    monkeypatch.setattr(updater, "install_source_zip", lambda d: real_install(d, app))
    results: list[tuple[str, str]] = []
    job = updater.UpdateJob()
    job.finished.connect(lambda status, tag: results.append((status, tag)))
    cache = tmp_path / updater.CACHE_NAME
    try:
        job._run(install=False)
        assert results == [("ready", "v99.0.0")] and (app / "src" / "main.py").read_text() == "old"
        del _FakeGitHub.files["/zip"]  # installing now must come from the cache
        job._run(install=True)
        assert results[-1] == ("installed", "v99.0.0") and (app / "src" / "main.py").read_text() == "v99.0.0"
        (app / "src" / "main.py").write_text("old")
        (cache / "v99.0.0-paperduck.zip").write_bytes(data + b"evil")  # a tampered cache is not trusted
        job._run(install=True)
        assert results[-1] == ("error", "HTTP Error 404: Not Found")
        assert (app / "src" / "main.py").read_text() == "old"
        _FakeGitHub.files["/latest"] = json.dumps({"tag_name": "v0.0.1", "assets": []}).encode()
        job._run(install=False)  # up to date: the leftover download goes
        assert results[-1][0] == "current" and not cache.exists()
    finally:
        server.shutdown()


def test_update_job_reports_a_broken_http_reply(monkeypatch: pytest.MonkeyPatch,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    """http.client.HTTPException killed the worker before `finished`: CHECKING, then silence."""
    import socket

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)

    def serve() -> None:
        conn, _addr = server.accept()
        conn.recv(65536)
        conn.sendall(b"NOT-HTTP\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    monkeypatch.setattr(updater, "RETRY_WAITS_S", ())  # this server answers once
    monkeypatch.setattr(updater, "LATEST_API", f"http://127.0.0.1:{server.getsockname()[1]}/latest")
    results: list[tuple[str, str]] = []
    job = updater.UpdateJob()
    job.finished.connect(lambda status, tag: results.append((status, tag)))
    try:
        job._run(install=True)
    finally:
        server.close()
    assert [status for status, _tag in results] == ["error"]
    assert "update check failed" in caplog.text  # the reason reaches the log, not just "UPDATE ERR"


def test_install_ps1_pins_release_key_and_verifier() -> None:
    import hashlib
    import re

    from src.updater import RELEASE_PUBLIC_KEY

    root = Path(__file__).resolve().parents[1]
    script = (root / "install.ps1").read_text(encoding="utf-8")
    key = re.search(r'\$ReleasePublicKey = "([0-9a-f]+)"', script).group(1)
    pinned = re.search(r'\$SigningPySha256 = "([0-9A-F]+)"', script).group(1)
    # raw.githubusercontent serves the LF bytes git stores, not a CRLF working copy
    served = (root / "src" / "signing.py").read_bytes().replace(b"\r\n", b"\n")
    assert bytes.fromhex(key) == RELEASE_PUBLIC_KEY
    assert hashlib.sha256(served).hexdigest().upper() == pinned


def test_changed_library_pins_need_the_installer(tmp_path: Path) -> None:
    app = _app(tmp_path)
    (app / "requirements.txt").write_text("PyQt6==6.12.0 \\n    --hash=sha256:aa\nPyQt6_sip==13.13.0\n")
    same_new_hashes = "pyqt6==6.12.0 \\n    --hash=sha256:bb\nPyQt6-sip==13.13.0\n"  # case, _ vs - and hashes do not count
    install_source_zip(_zipball({"src/main.py": "new", "requirements.txt": same_new_hashes}), app)
    assert (app / "src" / "main.py").read_text() == "new" and "bb" in (app / "requirements.txt").read_text()
    with pytest.raises(ValueError, match="needs installer"):
        install_source_zip(_zipball({"src/main.py": "newer", "requirements.txt": "PyQt6==6.13.0\n"}), app)
    assert (app / "src" / "main.py").read_text() == "new" and not (app / "src.new").exists()
    install_source_zip(_zipball({"src/main.py": "no reqs file in bundle"}), app)  # nothing to compare: allowed


def _frozen_zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            zf.writestr(f"Paperduck/{name}", text)
    return buf.getvalue()


def _frozen_app(tmp_path: Path) -> Path:
    app = tmp_path / "app"
    (app / "_internal").mkdir(parents=True)
    (app / "Paperduck.exe").write_text("old")
    (app / ".installed").write_text("marker")
    return app


def test_frozen_update_stages_then_swaps_after_exit(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    from src.updater import finish_frozen_update, install_frozen_zip, remove_old_build, zip_version

    app = _frozen_app(tmp_path)
    data = _frozen_zip({"Paperduck.exe": "new", "_internal/x.dll": "x", "VERSION": "2.0.0\n"})
    assert zip_version(data) == "2.0.0"
    install_frozen_zip(data, app)
    assert (app / "Paperduck.exe").read_text() == "old"  # the running copy is never touched
    staged = tmp_path / "app.new"
    assert (staged / "Paperduck.exe").read_text() == "new" and is_installed(staged)
    with pytest.raises(ValueError):
        finish_frozen_update(os.getpid(), app)  # only a staged folder may swap itself in
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        from src import updater

        assert updater._wait_for_exit(old.pid, timeout_ms=100) is False  # still running: no swap, no relaunch
    finally:
        old.kill()
        old.wait()
    assert finish_frozen_update(old.pid, staged, starts=lambda _d: True) == app
    assert (app / "Paperduck.exe").read_text() == "new" and (app / "_internal" / "x.dll").exists()
    assert is_installed(app)  # the marker is not in the signed bundle; it follows the install
    assert (tmp_path / "app.old" / "Paperduck.exe").read_text() == "old"
    remove_old_build(app)
    assert not (tmp_path / "app.old").exists() and not staged.exists()


def test_bad_frozen_update_keeps_running_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.updater import finish_frozen_update, install_frozen_zip

    app = _frozen_app(tmp_path)
    with pytest.raises(ValueError):
        install_frozen_zip(_frozen_zip({"_internal/x.dll": "no exe"}), app)
    with pytest.raises(ValueError):
        install_frozen_zip(_frozen_zip({"../evil.dll": "x", "Paperduck.exe": "new"}), app)
    assert not (tmp_path / "evil.dll").exists()
    install_frozen_zip(_frozen_zip({"Paperduck.exe": "new"}), app)

    def locked(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("antivirus")

    with pytest.raises(OSError, match="does not start"):
        finish_frozen_update(0, tmp_path / "app.new", starts=lambda _d: False)  # a copy that cannot load
    assert (app / "Paperduck.exe").read_text() == "old" and not (tmp_path / "app.old").exists()
    monkeypatch.setattr(shutil, "copytree", locked)
    with pytest.raises(PermissionError):
        finish_frozen_update(0, tmp_path / "app.new", starts=lambda _d: True)
    assert (app / "Paperduck.exe").read_text() == "old" and not (tmp_path / "app.old").exists()


@pytest.mark.skipif(shutil.which("powershell.exe") is None, reason="needs Windows PowerShell")
def test_install_ps1_verifier_matches_python_signer(tmp_path: Path) -> None:
    """The standalone install checks signatures in PowerShell; it must agree with src/signing.py."""
    import re
    import subprocess

    from src.signing import public_key, sign

    script = (Path(__file__).resolve().parents[1] / "install.ps1").read_text(encoding="utf-8")
    verifier = re.search(r"Add-Type -AssemblyName System\.Numerics\n.*?\n}\n", script, re.S).group(0)
    secret = bytes(range(32))
    message = tmp_path / "bundle.zip"
    message.write_bytes(b"release bytes" * 1000)
    good = sign(secret, message.read_bytes())
    forged = good[:40] + bytes([good[40] ^ 1]) + good[41:]
    other_key = public_key(bytes(32))
    cases = [(public_key(secret), good), (public_key(secret), forged), (other_key, good)]
    lines = [verifier] + [f"Test-Ed25519Signature ([byte[]]@({','.join(map(str, key))})) '{message}' "
                          f"([byte[]]@({','.join(map(str, sig))}))" for key, sig in cases]
    (tmp_path / "check.ps1").write_text("\n".join(lines), encoding="utf-8")
    out = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(tmp_path / "check.ps1")],
                         capture_output=True, text=True, timeout=120)
    assert out.stdout.split() == ["True", "False", "False"], out.stderr


@pytest.mark.parametrize("fails", [False, True])
def test_update_helper_tells_the_old_build_a_swap_failed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                         fails: bool) -> None:
    from src import main as main_mod

    def finish(_pid: int) -> Path:
        if fails:
            raise PermissionError("folder in use")
        return tmp_path / "app"

    started: list[list[str]] = []
    monkeypatch.setattr(main_mod, "finish_frozen_update", finish)
    monkeypatch.setattr(main_mod, "setup_logging", lambda: None)
    monkeypatch.setattr(main_mod.os, "chdir", lambda _p: None)
    import subprocess

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **_kw: started.append(argv))
    assert main_mod._finish_update(["Paperduck.exe", "--finish-update", "1"]) == 0
    assert (main_mod.UPDATE_FAILED_FLAG in started[0][1:]) is fails


def test_download_reports_progress_and_enforces_the_limit() -> None:
    body = bytes(range(256)) * 4096  # 1 MiB: four 256 KiB chunks

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/bundle.zip"
    try:
        seen: list[int] = []
        assert updater._get(url, len(body), seen.append) == body
        assert seen == sorted(set(seen)) and seen[-1] == 100  # rising, no repeats, ends at 100
        with pytest.raises(ValueError, match="too large"):
            updater._get(url, len(body) - 1)
    finally:
        server.shutdown()


def test_download_retries_github_outages_and_says_whose_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    replies: list[int] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            code = replies.pop(0)
            self.send_response(code)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *_args: object) -> None:
            pass

    monkeypatch.setattr(updater, "RETRY_WAITS_S", (0.0, 0.0))
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/bundle.zip"
    try:
        replies[:] = [504, 503, 200]
        assert updater._get(url, 10) == b"ok" and not replies  # two outages, then it works
        replies[:] = [504, 504, 504]
        with pytest.raises(OSError, match=r"GitHub is not responding \(HTTP 504\); try again in a few minutes"):
            updater._get(url, 10)
        assert not replies  # three tries in all
        replies[:] = [404, 200]
        with pytest.raises(urllib.error.HTTPError):
            updater._get(url, 10)
        assert replies == [200]  # a 404 is not retried
    finally:
        server.shutdown()


def test_launch_env_drops_the_parent_qt_dll_folder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inherited, it let a copy with no Qt DLLs pass the self-check by loading the helper's own."""
    import os

    monkeypatch.setenv("PATH", os.pathsep.join([r"C:\app.new\_internal\PyQt6\Qt6\bin", r"C:\Windows"]))
    assert updater.launch_env()["PATH"] == r"C:\Windows"


def _sandboxed_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake %LOCALAPPDATA% whose Paperduck writes "land" in a package's private copy (the probe is pre-made)."""
    local = tmp_path / "Local"
    (local / "Paperduck" / "app" / "src").mkdir(parents=True)
    (local / "Paperduck" / "app" / "src" / "main.py").write_text("old")
    copy = local / "Packages" / "Claude_abc" / "LocalCache" / "Local" / "Paperduck"
    (copy / "app").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr("src.updater.os.getpid", lambda: 4242)
    for folder in (copy, copy / "app"):
        (folder / ".write-probe-4242").write_text("")
    return local


def test_writes_redirected_only_when_the_probe_lands_in_a_package_copy(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    from src.updater import writes_redirected

    local = _sandboxed_local(tmp_path, monkeypatch)
    assert writes_redirected(local / "Paperduck")
    assert not (local / "Paperduck" / ".write-probe-4242").exists()  # the probe is cleaned up
    assert not writes_redirected(tmp_path)  # outside %LOCALAPPDATA%
    shutil.rmtree(local / "Packages")
    assert not writes_redirected(local / "Paperduck")


def test_sandboxed_updates_refuse_before_touching_the_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.updater import SANDBOXED, finish_frozen_update, install_frozen_zip

    app = _sandboxed_local(tmp_path, monkeypatch) / "Paperduck" / "app"
    for run in (lambda: install_source_zip(_zipball({"src/main.py": "new"}), app),
                lambda: install_frozen_zip(_zipball({"Paperduck.exe": "x"}), app),
                lambda: finish_frozen_update(0, app.with_name("app.new"), starts=lambda _: True)):
        with pytest.raises(OSError, match="sandbox") as err:
            run()
        assert str(err.value) == SANDBOXED
    assert (app / "src" / "main.py").read_text() == "old"
    assert sorted(p.name for p in app.parent.iterdir()) == ["app"]
