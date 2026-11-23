"""Saved logins the AI agent can type but never read.

Values are encrypted with Windows DPAPI (current user, plus a Paperduck-only entropy), so another Windows account or
a copied file cannot open them. Each login belongs to one exact https host. The agent never names a login: the pet
reads the host of the page that owns the focused box straight from the browser (UI Automation), so a lookalike
domain or an injected instruction cannot route a password to another site. Nothing here logs or returns a value.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from src.tools import warm_ps
from src.tools.ports import process_name

# Chromium browsers expose the page URL on the focused box's Document element; Firefox's UIA does not
CHROMIUM_BROWSERS = frozenset({"chrome.exe", "msedge.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe"})
FIELDS = ("username", "password")
MAX_VALUE_CHARS = 512
_ENTROPY = b"paperduck-logins-v1"
_UI_FORBIDDEN = 0x1

# The focused element, and the URL of the nearest Document above it (the page or iframe that owns the box)
_FOCUS_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$A = [System.Windows.Automation.AutomationElement]
$CT = [System.Windows.Automation.ControlType]
$f = $A::FocusedElement
if (-not $f) { return }
$w = [System.Windows.Automation.TreeWalker]::RawViewWalker
$e = $f; $url = ''; $doc = 0
for ($i = 0; $i -lt 80 -and $e; $i++) {
    if ($e.Current.ControlType -eq $CT::Document) {
        $vp = $null
        if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$vp)) { $url = $vp.Current.Value }
        $doc = 1
        break
    }
    $e = $w.GetParent($e)
}
$c = $f.Current
"$($c.ProcessId)`t$($c.ControlType.ProgrammaticName)`t$($c.IsPassword)`t$($c.IsEnabled)`t$doc`t$($url -replace '[\t\r\n]', '')"
"""


@dataclass(frozen=True)
class Field:
    site: str  # exact https host of the page that owns the box
    url: str
    password: bool
    hwnd: int


def vault_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Paperduck" / "logins.bin"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("saved logins need Windows")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    ent = ctypes.create_string_buffer(_ENTROPY, len(_ENTROPY))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_ent = _Blob(len(_ENTROPY), ctypes.cast(ent, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    try:
        if not fn(ctypes.byref(blob_in), None, ctypes.byref(blob_ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
            raise OSError("Windows could not " + ("encrypt" if protect else "decrypt") + " the saved logins")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.memset(out.pbData, 0, out.cbData)
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
    finally:
        ctypes.memset(buf, 0, len(data))  # shortcut: Python str copies of a value cannot be wiped; this wipes ours


def load(path: Path | None = None) -> dict[str, dict[str, str]]:
    """{host: {"username", "password"}}. Raises OSError on a file it cannot open, so a save never overwrites it."""
    path = path or vault_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(_dpapi(path.read_bytes(), protect=False).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OSError("the saved logins file is damaged") from exc
    if not isinstance(data, dict):
        raise OSError("the saved logins file is damaged")
    return {h: {f: v.get(f, "") for f in FIELDS} for h, v in data.items()
            if isinstance(h, str) and isinstance(v, dict) and all(isinstance(v.get(f, ""), str) for f in FIELDS)}


def save(logins: dict[str, dict[str, str]], path: Path | None = None) -> None:
    path = path or vault_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(_dpapi(json.dumps(logins).encode("utf-8"), protect=True))
    os.replace(tmp, path)


def store(site: str, username: str, password: str, path: Path | None = None) -> None:
    """Save (or replace) the login for one host."""
    if not site or len(username) > MAX_VALUE_CHARS or len(password) > MAX_VALUE_CHARS:
        raise ValueError("a login needs a site, and each value at most 512 characters")
    logins = load(path)
    logins[site] = {"username": username, "password": password}
    save(logins, path)


def forget(site: str, path: Path | None = None) -> bool:
    logins = load(path)
    if logins.pop(site, None) is None:
        return False
    save(logins, path)
    return True


def site_of(url: str) -> str:
    """Exact host of an https URL, '' for anything else (http, data:, file:, about:, embedded credentials)."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not host or parts.username or parts.password:
        return ""
    return host.lower().rstrip(".")


def parse_focus(out: str, front_pid: int, hwnd: int) -> Field | str:
    """The probe's line as a Field, or an error for the model (it never contains a value)."""
    cols = (out or "").strip("\r\n").split("\t")  # not strip(): an empty URL leaves a trailing tab
    if len(cols) != 6:
        return "error: could not read the focused box (UI Automation gave no answer)"
    pid, kind, pw, enabled, doc, url = cols
    if not pid.isdigit() or int(pid) != front_pid:
        return "error: the keyboard focus is not in the front browser window"
    if kind != "ControlType.Edit" or enabled != "True":
        return "error: click the username or password box first, so it has the keyboard focus"
    if doc != "1":
        return "error: the focused box is not inside a web page (the address bar, say)"
    site = site_of(url)
    if not site:
        return "error: the page is not https, so no saved login is typed into it"
    return Field(site, url, pw == "True", hwnd)


def focused_field() -> Field | str:
    """What has the keyboard focus right now, checked to be an enabled box inside an https page of a Chromium
    browser that is the foreground window."""
    if sys.platform != "win32":
        return "error: saved logins need Windows"
    user32 = ctypes.windll.user32
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = int(user32.GetForegroundWindow() or 0)
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    if not hwnd or process_name(pid.value).lower() not in CHROMIUM_BROWSERS:
        return "error: saved logins are typed only into Chrome, Edge or another Chromium browser in front"
    return parse_focus(warm_ps.run("login", _FOCUS_PS, {}, 5.0) or "", pid.value, hwnd)
