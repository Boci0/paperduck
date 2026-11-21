"""Tailscale-first mobile web bridge for Paperduck AI Assist.

Binds strictly to loopback (127.0.0.1) by default and exposes HTTPS over `tailscale serve`
(falling back to the device's Tailscale IPv4 address if Serve is not enabled on the tailnet).
Handles pairing tokens in Windows Credential Manager, HttpOnly cookie sessions + CSRF +
Fetch Metadata / CSP headers, Server-Sent Events for live updates, and PNG screen peeks.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import ctypes.wintypes as wintypes
import hmac
import http.cookies
import json
import logging
import re
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger(__name__)

DEFAULT_PORT = 48791
MAX_BODY_BYTES = 64 * 1024
MAX_UPLOAD_BODY_BYTES = 24 * 1024 * 1024
MAX_REMOTE_ATTACHMENTS = 4
MAX_REMOTE_FILE_BYTES = 4 * 1024 * 1024
MAX_PROMPT_CHARS = 32_000
MAX_SESSIONS = 16
RATE_WINDOW_S = 60.0
RATE_MAX_FAILS = 10
COOKIE_MAX_AGE_S = 30 * 24 * 3600
SSE_MIN_INTERVAL_S = 0.15
REMOTE_CRED_TARGET = "Paperduck Remote/pairing-token"
COOKIE_NAME = "pd_remote"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
_SID_RE = re.compile(r"^[0-9a-f]{12}$")


def sanitize_attachment_name(raw: str) -> str:
    """Strip directory traversal and Windows-unsafe characters while preserving a short file extension."""
    leaf = PurePosixPath(str(raw or "").replace("\\", "/")).name.lstrip(".")
    clean = _SAFE_NAME_RE.sub("_", leaf).strip(" .")
    if not clean or clean in (".", ".."):
        return "upload.bin"
    if len(clean) <= 100:
        return clean
    stem, dot, ext = clean.rpartition(".")
    if dot and len(ext) <= 12:
        return f"{stem[:99 - len(ext)]}.{ext}"
    return clean[:100]


@dataclass(frozen=True)
class RemoteAttachment:
    """Validated binary payload uploaded from the phone UI."""

    name: str
    mime: str
    data: bytes


@dataclass(frozen=True)
class TailscaleInfo:
    dns_name: str
    ip: str
    backend_state: str
    host_name: str
    serve_enabled: bool = True


@dataclass(frozen=True)
class BridgeHandlers:
    """Thread-safe callbacks supplied by the Qt-thread controller."""

    get_state: Callable[[], dict[str, Any]]
    send_message: Callable[[str], tuple[bool, str]]
    stop_chat: Callable[[], bool]
    new_chat: Callable[[], tuple[bool, str]]
    resolve_approval: Callable[[str, str | None], bool]
    grab_screen: Callable[[int], tuple[bytes | None, str]]
    send_attachments: Callable[[str, list[RemoteAttachment]], tuple[bool, str]] | None = None
    set_settings: Callable[[dict[str, Any]], tuple[bool, str]] | None = None
    set_target: Callable[[str], tuple[bool, str]] | None = None
    refresh_models: Callable[[], tuple[bool, str]] | None = None
    list_sessions: Callable[[], list[dict[str, Any]]] | None = None
    resume_session: Callable[[str], tuple[bool, str]] | None = None
    delete_session: Callable[[str], tuple[bool, str]] | None = None
    stop_proc: Callable[[str], tuple[bool, str]] | None = None
    grab_screen_opts: Callable[[int, int, str, int], tuple[bytes | None, str, str]] | None = None


# ---------------------------------------------------------------------------
# Windows Credential Manager (pairing token, DPAPI-encrypted per user)
# ---------------------------------------------------------------------------

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def save_remote_token(token: str) -> bool:
    """Persist the phone pairing token in Windows Credential Manager."""
    if sys.platform != "win32" or not token or any(ch.isspace() for ch in token):
        return False
    blob = token.encode("utf-16-le")
    buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
    cred = _CREDENTIAL(
        Type=_CRED_TYPE_GENERIC,
        TargetName=REMOTE_CRED_TARGET,
        CredentialBlobSize=len(blob),
        CredentialBlob=buf,
        Persist=_CRED_PERSIST_LOCAL_MACHINE,
        UserName="Paperduck",
    )
    return bool(ctypes.windll.advapi32.CredWriteW(ctypes.byref(cred), 0))


def load_remote_token() -> str:
    """Read the saved phone pairing token from Windows Credential Manager, or ''."""
    if sys.platform != "win32":
        return ""
    advapi32 = ctypes.windll.advapi32
    ptr = ctypes.POINTER(_CREDENTIAL)()
    if not advapi32.CredReadW(REMOTE_CRED_TARGET, _CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
        return ""
    try:
        cred = ptr.contents
        return ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize).decode("utf-16-le", "replace")
    finally:
        advapi32.CredFree(ptr)


def delete_remote_token() -> bool:
    if sys.platform != "win32":
        return False
    return bool(ctypes.windll.advapi32.CredDeleteW(REMOTE_CRED_TARGET, _CRED_TYPE_GENERIC, 0))


def generate_token() -> str:
    return secrets.token_urlsafe(24)


def ensure_remote_token() -> str:
    tok = load_remote_token()
    if tok:
        return tok
    tok = generate_token()
    save_remote_token(tok)
    return tok


def rotate_remote_token() -> str:
    tok = generate_token()
    save_remote_token(tok)
    return tok


# ---------------------------------------------------------------------------
# Tailscale discovery and Serve helpers
# ---------------------------------------------------------------------------

def _run_tailscale(args: list[str], timeout_s: float = 5.0) -> tuple[int, str, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(
            ["tailscale", *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            creationflags=flags,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def tailscale_info() -> TailscaleInfo | None:
    """Inspect active Tailscale node metadata (`dns_name`, IPv4 `ip`), or None when unavailable."""
    rc, out, _err = _run_tailscale(["status", "--json"], timeout_s=4.0)
    if rc != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    backend = str(data.get("BackendState") or "")
    self_node = data.get("Self") or {}
    dns_name = str(self_node.get("DNSName") or "").rstrip(".")
    host_name = str(self_node.get("HostName") or "")
    ips = [str(ip) for ip in (data.get("TailscaleIPs") or self_node.get("TailscaleIPs") or [])]
    ipv4 = next((ip for ip in ips if "." in ip and ":" not in ip), "")
    serve_enabled = bool(data.get("CertDomains"))
    if backend != "Running" or (not dns_name and not ipv4):
        return None
    return TailscaleInfo(dns_name=dns_name, ip=ipv4, backend_state=backend, host_name=host_name,
                         serve_enabled=serve_enabled)


def serve_status_empty() -> bool:
    rc, out, _err = _run_tailscale(["serve", "status", "--json"], timeout_s=4.0)
    if rc != 0:
        return False
    try:
        parsed = json.loads(out or "{}")
    except ValueError:
        return False
    return isinstance(parsed, dict) and not parsed


def start_tailscale_serve(local_port: int, info: TailscaleInfo | None = None) -> tuple[bool, str]:
    """Expose `http://127.0.0.1:<local_port>` over HTTPS via `tailscale serve --bg`."""
    info = info or tailscale_info()
    if info is None or not info.dns_name:
        return False, "Tailscale is not running with MagicDNS"
    rc, _out, err = _run_tailscale(["serve", "--bg", f"http://127.0.0.1:{local_port}"], timeout_s=8.0)
    if rc != 0:
        return False, err or f"tailscale serve exited {rc}"
    return True, f"https://{info.dns_name}"


def stop_tailscale_serve() -> None:
    _run_tailscale(["serve", "reset"], timeout_s=4.0)


# ---------------------------------------------------------------------------
# Mobile HTML UI (Paper-and-Ink palette, CSP nonces, zero innerHTML)
# ---------------------------------------------------------------------------

# Home Screen install: iOS reads apple-touch-icon, Android/Chrome read the manifest
_INSTALL_LINKS = ('<link rel="apple-touch-icon" href="/apple-touch-icon.png">\n'
                  '<link rel="manifest" href="/manifest.webmanifest">')
_MANIFEST = json.dumps({
    "name": "Paperduck", "short_name": "Paperduck", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#f4f0e7", "theme_color": "#f4f0e7",
    "icons": [{"src": "/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"}],
}).encode("utf-8")


def _app_html(nonce: str, csrf_token: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="color-scheme" content="light dark">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Paperduck">
{_INSTALL_LINKS}
<meta name="csrf-token" content="{csrf_token}">
<title>Paperduck</title>
<style nonce="{nonce}">
:root {{
  color-scheme: light dark;
  --target: 38px;
  --bg-light: #f4f0e7;
  --bg-dark: #151419;
  --paper-light: #faf8f3;
  --paper-dark: #1c1b20;
  --raised-light: #f0ece3;
  --raised-dark: #26252c;
  --bubble-light: #f0e9d6;
  --bubble-dark: #34312c;
  --ink-light: #1d1c22;
  --ink-dark: #efece5;
  --dim-light: #77736b;
  --dim-dark: #9b978e;
  --line-light: #ded7c9;
  --line-dark: #36343d;
  --accent-light: #a86f00;
  --accent-dark: #f2be3a;
  --on-accent-light: #ffffff;
  --on-accent-dark: #1d1c22;
  --danger-light: #c9472c;
  --danger-dark: #ee7a5c;
  --ok-light: #1f8a4c;
  --ok-dark: #38b764;
  --bg: var(--bg-light);
  --paper: var(--paper-light);
  --raised: var(--raised-light);
  --bubble: var(--bubble-light);
  --ink: var(--ink-light);
  --dim: var(--dim-light);
  --line: var(--line-light);
  --accent: var(--accent-light);
  --on-accent: var(--on-accent-light);
  --danger: var(--danger-light);
  --ok: var(--ok-light);
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: var(--bg-dark);
    --paper: var(--paper-dark);
    --raised: var(--raised-dark);
    --bubble: var(--bubble-dark);
    --ink: var(--ink-dark);
    --dim: var(--dim-dark);
    --line: var(--line-dark);
    --accent: var(--accent-dark);
    --on-accent: var(--on-accent-dark);
    --danger: var(--danger-dark);
    --ok: var(--ok-dark);
  }}
}}
@supports (color: light-dark(white, black)) {{
  :root {{
    --bg: light-dark(var(--bg-light), var(--bg-dark));
    --paper: light-dark(var(--paper-light), var(--paper-dark));
    --raised: light-dark(var(--raised-light), var(--raised-dark));
    --bubble: light-dark(var(--bubble-light), var(--bubble-dark));
    --ink: light-dark(var(--ink-light), var(--ink-dark));
    --dim: light-dark(var(--dim-light), var(--dim-dark));
    --line: light-dark(var(--line-light), var(--line-dark));
    --accent: light-dark(var(--accent-light), var(--accent-dark));
    --on-accent: light-dark(var(--on-accent-light), var(--on-accent-dark));
    --danger: light-dark(var(--danger-light), var(--danger-dark));
    --ok: light-dark(var(--ok-light), var(--ok-dark));
  }}
}}
@media (pointer: coarse) {{
  :root {{ --target: 44px; }}
  .select-sm {{ font-size: 16px; }}
}}
* {{ box-sizing: border-box; }}
[hidden] {{ display: none !important; }}
.visually-hidden {{
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}}
html {{
  background: var(--bg);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  block-size: var(--app-h, 100dvh);
  overflow: hidden;
  -webkit-tap-highlight-color: transparent;
}}
body {{
  margin: 0;
  block-size: var(--app-h, 100dvh);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  overflow-wrap: break-word;
}}
.top-dock {{
  flex: none;
  z-index: 20;
}}
:where(button, input:not([type="checkbox"]), select, textarea, label:has(input[type="checkbox"])) {{
  min-block-size: var(--target);
}}
button:focus-visible, textarea:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible, a:focus-visible {{
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}}
.mono {{
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
  font-variant-numeric: tabular-nums;
}}
header {{
  background: var(--paper);
  border-bottom: 1px solid var(--line);
  padding: max(0.42rem, env(safe-area-inset-top, 0px)) max(0.75rem, env(safe-area-inset-right, 0px)) 0.38rem max(0.75rem, env(safe-area-inset-left, 0px));
}}
.sheet-w {{
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
}}
.topbar {{
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 0.32rem;
}}
.top-actions {{
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 0.28rem;
  flex: none;
}}
.brand {{
  font-weight: 700;
  font-size: 0.94rem;
  letter-spacing: -0.01em;
  margin-right: auto;
  display: flex;
  align-items: center;
  gap: 0.36rem;
  min-width: 0;
}}
.duck-mark {{
  width: 19px;
  height: 19px;
  flex: none;
}}
.meta-line {{
  margin-top: 0.18rem;
  font-size: 0.72rem;
  color: var(--dim);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.4rem;
  white-space: nowrap;
  overflow: hidden;
}}
.target-bar {{
  display: inline-flex;
  align-items: center;
  background: var(--raised);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 2px;
  gap: 2px;
  min-width: 0;
}}
.btn.target-btn {{
  min-block-size: 28px;
  padding: 0 0.46rem;
  font-size: 0.73rem;
  border: none;
  background: transparent;
  border-radius: 4px;
}}
@media (max-width: 420px) {{
  .topbar {{ gap: 0.24rem; }}
  .top-actions {{ gap: 0.22rem; }}
  .top-actions .btn.sm {{
    padding: 0 0.42rem;
    font-size: 0.72rem;
    min-block-size: 32px;
  }}
  .btn.target-btn {{
    padding: 0 0.38rem;
    font-size: 0.7rem;
  }}
}}
@media (max-width: 340px) {{
  .topbar {{ flex-wrap: wrap; }}
}}
.btn.target-btn.active {{
  background: var(--paper);
  color: var(--ink);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
}}
.target-badge {{
  font-size: 0.68rem;
  color: var(--accent);
}}
.target-badge[hidden] {{ display: none; }}
.scope-pill {{
  font-size: 0.72rem;
  color: var(--accent);
  background: var(--raised);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 0.14rem 0.45rem;
  margin-right: auto;
}}
.status-unit {{
  display: inline-flex;
  align-items: center;
  gap: 0.36rem;
  min-width: 0;
  flex: 1;
}}
#modelText {{
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.pip {{
  width: 6px;
  height: 6px;
  background: var(--ok);
  flex: none;
}}
.pip.busy {{ background: var(--accent); }}
.pip.off {{ background: var(--danger); }}
.btn {{
  appearance: none;
  border: 1px solid var(--line);
  background: var(--paper);
  color: var(--ink);
  border-radius: 6px;
  padding: 0 0.68rem;
  font: inherit;
  font-size: 0.82rem;
  font-weight: 600;
  cursor: pointer;
  touch-action: manipulation;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 0.3rem;
}}
.btn:hover:not(:disabled) {{ background: var(--raised); }}
.btn:active:not(:disabled) {{ opacity: 0.72; }}
.btn:disabled {{ opacity: 0.4; cursor: not-allowed; }}
.btn.primary {{
  background: var(--accent);
  color: var(--on-accent);
  border-color: var(--accent);
}}
.btn.primary:hover:not(:disabled) {{ opacity: 0.9; background: var(--accent); }}
.btn.danger {{
  background: var(--danger);
  color: #ffffff;
  border-color: var(--danger);
}}
.btn.active {{
  border-color: var(--accent);
  color: var(--accent);
  background: var(--raised);
}}
.btn.sm {{
  min-block-size: 32px;
  padding: 0 0.52rem;
  font-size: 0.76rem;
}}
.btn.xs {{
  min-block-size: 26px;
  padding: 0 0.42rem;
  font-size: 0.72rem;
}}
.select-sm {{
  border: 1px solid var(--line);
  background: var(--raised);
  color: var(--ink);
  border-radius: 6px;
  padding: 0 0.45rem;
  font: inherit;
  font-size: 0.76rem;
  min-block-size: 32px;
  max-width: 100%;
}}
.icon-btn {{
  padding: 0;
  min-inline-size: var(--target);
  background: var(--raised);
}}
.drawer {{
  background: var(--paper);
  border-bottom: 1px solid var(--line);
  padding: 0.58rem max(0.85rem, env(safe-area-inset-right, 0px)) 0.65rem max(0.85rem, env(safe-area-inset-left, 0px));
}}
.drawer[hidden] {{ display: none; }}
.settings-stack {{
  display: flex;
  flex-direction: column;
  gap: 0.52rem;
}}
.settings-top, .settings-row, .settings-foot {{
  display: flex;
  align-items: center;
  gap: 0.42rem;
  flex-wrap: wrap;
}}
.settings-top-btns {{
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}}
.settings-pair {{
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.48rem;
}}
@media (max-width: 340px) {{
  .settings-pair {{ grid-template-columns: 1fr; }}
}}
.field-stack {{
  display: flex;
  flex-direction: column;
  gap: 0.22rem;
  font-size: 0.74rem;
  color: var(--dim);
  min-width: 0;
}}
.field-stack[hidden] {{ display: none; }}
.field-pair {{
  display: flex;
  align-items: center;
  gap: 0.38rem;
  font-size: 0.76rem;
  color: var(--dim);
  min-width: 0;
}}
.field-pair.grow {{
  flex: 1 1 200px;
}}
.field-pair.grow select {{
  flex: 1 1 auto;
  min-width: 0;
}}
.screen-drawer {{
  background: var(--paper);
  border-bottom: 1px solid var(--line);
  padding: 0.42rem max(0.75rem, env(safe-area-inset-right, 0px)) 0.48rem max(0.75rem, env(safe-area-inset-left, 0px));
}}
.screen-drawer[hidden] {{ display: none; }}
.screen-bar {{
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.3rem;
}}
.screen-opts {{
  margin-top: 0.28rem;
}}
.screen-opts summary {{
  font-size: 0.72rem;
  color: var(--dim);
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.4rem;
  list-style: none;
}}
.screen-opts summary::-webkit-details-marker {{ display: none; }}
.screen-opts-row {{
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.34rem;
  margin-top: 0.32rem;
}}
.toggle-label {{
  display: inline-flex;
  align-items: center;
  gap: 0.32rem;
  font-size: 0.76rem;
  color: var(--ink);
  padding: 0 0.48rem;
  min-block-size: 32px;
  border-radius: 6px;
  background: var(--raised);
  border: 1px solid var(--line);
  cursor: pointer;
  touch-action: manipulation;
}}
.toggle-label input {{ accent-color: var(--accent); width: 15px; height: 15px; margin: 0; }}
.screen-meta {{
  font-size: 0.71rem;
  color: var(--dim);
  margin-left: auto;
}}
.screen-body {{
  margin-top: 0.38rem;
}}
.screen-body[hidden] {{ display: none; }}
.screen-frame {{
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--raised);
  overflow: hidden;
  display: flex;
  justify-content: center;
}}
.screen-img {{
  display: block;
  max-inline-size: 100%;
  max-block-size: min(20dvh, 170px);
  width: auto;
  height: auto;
  object-fit: contain;
  cursor: zoom-in;
}}
.pane-wrap {{
  position: relative;
  flex: 1 1 0;
  min-block-size: 0;
  display: flex;
  flex-direction: column;
}}
main {{
  flex: 1 1 0;
  min-block-size: 0;
  overflow-y: auto;
  overscroll-behavior-y: contain;
  -webkit-overflow-scrolling: touch;
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
  padding: 0.85rem max(0.85rem, env(safe-area-inset-right, 0px)) 2.2rem max(0.85rem, env(safe-area-inset-left, 0px));
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}}
main > * {{
  flex-shrink: 0;
}}
.empty-note {{
  margin: 2.2rem 0 0.5rem;
  text-align: center;
  color: var(--dim);
  font-size: 0.84rem;
}}
.empty-note[hidden] {{ display: none; }}
.strip {{
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0.55rem 0.72rem;
  font-size: 0.83rem;
}}
.strip[hidden] {{ display: none; }}
.strip-title {{
  font-size: 0.72rem;
  font-weight: 700;
  color: var(--dim);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  margin-bottom: 0.35rem;
}}
.task-item {{
  display: flex;
  gap: 0.45rem;
  padding: 0.14rem 0;
  align-items: baseline;
}}
.task-mark {{ font-size: 0.78rem; color: var(--accent); flex: none; }}
.task-done {{ color: var(--dim); text-decoration: line-through; }}
.task-done .task-mark {{ color: var(--dim); }}
#thread {{
  display: flex;
  flex-direction: column;
  gap: 0.82rem;
}}
.msg-wrap {{
  display: flex;
  flex-direction: column;
  gap: 0.28rem;
  min-width: 0;
  max-width: 100%;
}}
.msg-wrap.user {{ align-items: flex-end; }}
.msg-wrap.assistant {{ align-items: flex-start; }}
.msg-wrap.note {{ align-items: center; }}
.msg-wrap.error {{ align-items: stretch; }}
.user-atts {{
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 0.3rem;
  max-width: 86%;
}}
.att-badge {{
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  background: var(--raised);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0.18rem 0.45rem;
  font-size: 0.74rem;
  color: var(--dim);
}}
.msg {{
  line-height: 1.52;
  font-size: 0.94rem;
  color: var(--ink);
  min-width: 0;
  max-width: 100%;
}}
.msg-wrap.user .msg {{
  max-width: 86%;
  padding: 0.54rem 0.78rem;
  border-radius: 8px;
  background: var(--bubble);
  border: 1px solid var(--line);
}}
.msg-wrap.assistant .msg {{
  width: 100%;
  padding: 0.1rem 0;
}}
.msg-wrap.note .msg {{
  color: var(--dim);
  font-size: 0.78rem;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  padding: 0.22rem 0.65rem;
}}
.msg-wrap.error .msg {{
  background: var(--paper);
  border: 1px solid var(--danger);
  border-left: 3px solid var(--danger);
  border-radius: 6px;
  padding: 0.55rem 0.72rem;
}}
.msg h1, .msg h2, .msg h3 {{
  margin: 0.55rem 0 0.22rem;
  line-height: 1.3;
  text-wrap: balance;
}}
.msg h1 {{ font-size: 1.06rem; }}
.msg h2 {{ font-size: 0.98rem; }}
.msg h3 {{ font-size: 0.93rem; }}
.msg p, .msg li {{ margin: 0.22rem 0; text-wrap: pretty; }}
.msg ul, .msg ol {{ margin: 0.25rem 0 0.35rem; padding-left: 1.25rem; }}
.msg blockquote {{
  margin: 0.35rem 0;
  padding: 0.12rem 0.68rem;
  border-left: 2px solid var(--accent);
  color: var(--dim);
}}
.spacer {{ height: 0.4rem; }}
.code-wrap {{
  margin: 0.45rem 0;
  border-radius: 6px;
  border: 1px solid var(--line);
  background: var(--paper);
  overflow: hidden;
}}
.code-bar {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0.22rem 0.52rem;
  background: var(--raised);
  border-bottom: 1px solid var(--line);
  font-size: 0.73rem;
  color: var(--dim);
}}
.msg pre {{
  margin: 0;
  padding: 0.58rem 0.7rem;
  overflow-x: auto;
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
  font-size: 0.82rem;
  line-height: 1.42;
}}
.msg code {{
  background: var(--raised);
  padding: 0.08rem 0.3rem;
  border-radius: 4px;
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
  font-size: 0.86em;
  overflow-wrap: anywhere;
}}
.msg pre code {{ background: transparent; padding: 0; }}
.msg a {{ color: var(--accent); overflow-wrap: anywhere; }}
.msg hr {{ border: none; border-top: 1px solid var(--line); margin: 0.6rem 0; }}
.table-wrap {{
  overflow-x: auto;
  margin: 0.45rem 0;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--paper);
}}
.msg table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.84rem;
}}
.msg th, .msg td {{
  padding: 0.34rem 0.55rem;
  border-bottom: 1px solid var(--line);
  border-right: 1px solid var(--line);
  text-align: left;
  vertical-align: top;
}}
.msg th {{
  background: var(--raised);
  font-weight: 700;
}}
.msg tr:last-child td {{ border-bottom: none; }}
.msg th:last-child, .msg td:last-child {{ border-right: none; }}
.steer-tag {{
  font-size: 0.68rem;
  color: var(--accent);
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 0.08rem 0.36rem;
}}
.trunc-note {{
  font-size: 0.75rem;
  color: var(--dim);
  text-align: center;
  padding: 0.2rem 0;
}}
.trunc-note[hidden] {{ display: none; }}
.proc-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.25rem 0;
}}
.proc-cmd {{
  font-size: 0.75rem;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
}}
.agent-card {{
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--raised);
  padding: 0.38rem 0.55rem;
  margin-top: 0.35rem;
}}
.agent-card summary {{
  cursor: pointer;
  font-size: 0.78rem;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 0.45rem;
  flex-wrap: wrap;
}}
.agent-badge {{
  font-size: 0.7rem;
  padding: 0.06rem 0.34rem;
  border-radius: 4px;
  border: 1px solid var(--line);
  background: var(--paper);
  color: var(--dim);
}}
.agent-badge.running {{ color: var(--accent); border-color: var(--accent); }}
.agent-badge.failed {{ color: var(--danger); border-color: var(--danger); }}
.agent-task {{
  font-size: 0.78rem;
  color: var(--dim);
  margin: 0.32rem 0;
}}
.agent-log {{
  margin: 0.32rem 0 0;
  padding: 0.42rem 0.52rem;
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 4px;
  font-size: 0.74rem;
  max-height: 150px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}}
.agent-report {{
  margin-top: 0.4rem;
  padding-top: 0.35rem;
  border-top: 1px solid var(--line);
  font-size: 0.84rem;
}}
.msg-foot {{
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 0.4rem 0.75rem;
}}
.tools-details {{
  font-size: 0.75rem;
  color: var(--dim);
  width: 100%;
}}
.tools-details summary {{
  cursor: pointer;
  padding: 0.16rem 0;
  user-select: none;
}}
.tools-list {{
  margin-top: 0.28rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}}
.tool-row {{
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  flex-wrap: wrap;
}}
.tool-sum {{
  font-size: 0.72rem;
  color: var(--dim);
  overflow-wrap: anywhere;
}}
.session-list {{
  display: flex;
  flex-direction: column;
  gap: 0.42rem;
  max-height: 62dvh;
  overflow-y: auto;
}}
.session-row {{
  display: flex;
  align-items: center;
  gap: 0.45rem;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--raised);
  padding: 0.42rem 0.55rem;
}}
.session-row.active {{
  border-color: var(--accent);
}}
.session-pick {{
  appearance: none;
  border: none;
  background: transparent;
  color: var(--ink);
  font: inherit;
  text-align: left;
  flex: 1;
  min-width: 0;
  padding: 0;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.12rem;
}}
.session-title {{
  font-size: 0.85rem;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.session-meta {{
  font-size: 0.72rem;
  color: var(--dim);
}}
.tool-pill {{
  font-size: 0.73rem;
  color: var(--dim);
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 0.12rem 0.42rem;
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
}}
.copy-link {{
  appearance: none;
  background: transparent;
  border: none;
  color: var(--dim);
  font: inherit;
  font-size: 0.75rem;
  padding: 0.16rem 0.2rem;
  cursor: pointer;
  text-decoration: underline;
  min-block-size: 28px;
}}
.copy-link:hover {{ color: var(--ink); }}
.stream-status {{
  display: flex;
  align-items: center;
  gap: 0.45rem;
  color: var(--dim);
  font-size: 0.78rem;
  padding: 0.25rem 0;
  white-space: normal;
  overflow-wrap: anywhere;
}}
.stream-status::before {{
  content: "";
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  flex: none;
  animation: pulseDot 1.2s ease-in-out infinite;
}}
@keyframes pulseDot {{
  0%, 100% {{ opacity: 0.35; }}
  50% {{ opacity: 1; }}
}}
.stream-status[hidden] {{ display: none; }}
.approval {{
  background: var(--paper);
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  border-radius: 6px;
  clip-path: polygon(0 0, calc(100% - 10px) 0, 100% 10px, 100% 100%, 0 100%);
  padding: 0.72rem 0.8rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}}
.approval.danger {{ border-left-color: var(--danger); }}
.approval-head {{ font-weight: 700; font-size: 0.88rem; }}
.approval pre {{
  margin: 0;
  background: var(--raised);
  border: 1px solid var(--line);
  padding: 0.5rem;
  border-radius: 4px;
  overflow: auto;
  max-block-size: 38dvh;
  font-size: 0.8rem;
  font-family: ui-monospace, "Cascadia Mono", Consolas, monospace;
  white-space: pre-wrap;
  word-break: break-word;
}}
.approval pre.collapsed {{ max-block-size: 8.5rem; }}
.approval pre.nowrap {{ white-space: pre; word-break: normal; }}
.approval-detail-bar {{
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.35rem;
}}
.diff-line {{ display: block; }}
.diff-add {{ color: var(--ok); }}
.diff-del {{ color: var(--danger); }}
.diff-hdr {{ color: var(--accent); font-weight: 600; }}
.repair-banner {{
  background: var(--paper);
  border-bottom: 1px solid var(--danger);
  padding: 0.45rem max(0.85rem, env(safe-area-inset-right, 0px)) 0.45rem max(0.85rem, env(safe-area-inset-left, 0px));
}}
.repair-banner[hidden] {{ display: none; }}
.repair-row {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.82rem;
  color: var(--danger);
  font-weight: 600;
}}
.queue-bar {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  background: var(--raised);
  border: 1px solid var(--line);
  border-left: 3px solid var(--accent);
  border-radius: 6px;
  padding: 0.34rem 0.55rem;
  margin-bottom: 0.42rem;
  font-size: 0.76rem;
  color: var(--ink);
}}
.queue-bar[hidden] {{ display: none; }}
.queue-actions {{
  display: inline-flex;
  align-items: center;
  gap: 0.32rem;
  flex: none;
}}
.approval-actions {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}}
.jump-btn {{
  position: absolute;
  right: max(0.85rem, env(safe-area-inset-right, 0px));
  bottom: 0.65rem;
  z-index: 15;
}}
.jump-btn[hidden] {{ display: none; }}
.toast {{
  position: fixed;
  left: 50%;
  transform: translateX(-50%);
  bottom: calc(4.9rem + env(safe-area-inset-bottom, 0px));
  background: var(--ink);
  color: var(--paper);
  padding: 0.36rem 0.8rem;
  border-radius: 6px;
  font-size: 0.8rem;
  font-weight: 600;
  z-index: 30;
  pointer-events: none;
}}
.toast[hidden] {{ display: none; }}
.composer-wrap {{
  flex: none;
  z-index: 20;
  background: var(--paper);
  border-top: 1px solid var(--line);
  padding: 0.5rem max(0.85rem, env(safe-area-inset-right, 0px)) max(0.58rem, env(safe-area-inset-bottom, 0px)) max(0.85rem, env(safe-area-inset-left, 0px));
}}
.attach-strip {{
  display: flex;
  flex-wrap: wrap;
  gap: 0.38rem;
  margin-bottom: 0.45rem;
}}
.attach-strip[hidden] {{ display: none; }}
.chip {{
  display: inline-flex;
  align-items: center;
  gap: 0.38rem;
  background: var(--raised);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 0.22rem 0.3rem 0.22rem 0.35rem;
  max-width: 100%;
}}
.chip-thumb {{
  width: 26px;
  height: 26px;
  object-fit: cover;
  border-radius: 4px;
  border: 1px solid var(--line);
  flex: none;
}}
.chip-icon {{
  width: 18px;
  height: 18px;
  flex: none;
  color: var(--dim);
}}
.chip-info {{
  display: flex;
  flex-direction: column;
  min-width: 0;
}}
.chip-name {{
  font-size: 0.76rem;
  font-weight: 600;
  max-inline-size: 16ch;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}}
.chip-size {{
  font-size: 0.68rem;
  color: var(--dim);
}}
.chip-x {{
  appearance: none;
  border: none;
  background: transparent;
  color: var(--dim);
  font-weight: 700;
  font-size: 0.9rem;
  min-inline-size: 28px;
  min-block-size: 28px;
  cursor: pointer;
  border-radius: 4px;
}}
.chip-x:hover {{ color: var(--danger); background: var(--paper); }}
.composer {{
  display: flex;
  gap: 0.42rem;
  align-items: flex-end;
}}
textarea {{
  flex: 1;
  min-width: 0;
  resize: none;
  border: 1px solid var(--line);
  background: var(--raised);
  color: var(--ink);
  border-radius: 6px;
  padding: 0.52rem 0.72rem;
  font: inherit;
  font-size: max(16px, 0.96rem);
  line-height: 1.38;
  max-height: 8.5rem;
}}
dialog.screen-modal {{
  width: min(98vw, 1180px);
  max-height: 94dvh;
  padding: 0.55rem;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--paper);
  color: var(--ink);
}}
dialog.screen-modal.fs-live[open] {{
  width: 100vw;
  height: 100dvh;
  max-width: none;
  max-height: none;
  margin: 0;
  padding: 0;
  border: none;
  border-radius: 0;
  background: #09090c;
  color: #efece5;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}
dialog.screen-modal::backdrop {{
  background: rgba(0, 0, 0, 0.88);
}}
.modal-head {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.34rem;
  margin-bottom: 0.42rem;
}}
dialog.screen-modal.fs-live .modal-head {{
  margin: 0;
  padding: max(0.38rem, env(safe-area-inset-top, 0px)) max(0.6rem, env(safe-area-inset-right, 0px)) 0.38rem max(0.6rem, env(safe-area-inset-left, 0px));
  background: rgba(24, 23, 28, 0.94);
  border-bottom: 1px solid #302f39;
  flex: none;
  z-index: 2;
}}
dialog.screen-modal.fs-live.hud-hidden .modal-head {{
  display: none;
}}
dialog.screen-modal.fs-live .btn,
dialog.screen-modal.fs-live .select-sm,
dialog.screen-modal.fs-live .toggle-label {{
  background: #232229;
  color: #efece5;
  border-color: #3a3844;
  min-block-size: 30px;
  font-size: 0.72rem;
}}
.modal-tools {{
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.28rem;
  flex: 1;
  justify-content: flex-end;
}}
.modal-title {{ font-weight: 700; font-size: 0.86rem; margin: 0; }}
@media (max-width: 420px) {{
  dialog.screen-modal.fs-live .modal-title {{ display: none; }}
}}
.modal-stage {{
  flex: 1 1 0;
  min-block-size: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: auto;
  touch-action: pinch-zoom pan-x pan-y;
  padding-bottom: env(safe-area-inset-bottom, 0px);
}}
.modal-img {{
  display: block;
  width: 100%;
  height: 100%;
  object-fit: contain;
  cursor: pointer;
}}
.fs-exit-fab {{
  position: fixed;
  top: max(0.55rem, env(safe-area-inset-top, 0px));
  right: max(0.55rem, env(safe-area-inset-right, 0px));
  z-index: 5;
  opacity: 0.82;
}}
dialog.screen-modal.fs-live:not(.hud-hidden) .fs-exit-fab {{
  display: none;
}}
</style>
</head>
<body>
<div class="top-dock">
  <header>
    <div class="sheet-w">
      <div class="topbar">
        <div class="brand">
          <svg class="duck-mark" viewBox="0 0 24 24" aria-hidden="true">
            <polygon points="3,14 9,6 15,8 19,6 22,9 18,11 19,17 8,19" fill="#F2BE3A" stroke="#1D1C22" stroke-width="1.3" stroke-linejoin="round"/>
            <polygon points="18,9 23,10 19,11.5" fill="#EE7A5C"/>
            <circle cx="15.5" cy="9" r="1" fill="#1D1C22"/>
          </svg>
          <div id="targetBar" class="target-bar" role="group" aria-label="Chat target">
            <button type="button" class="btn target-btn active" id="targetPhoneBtn" aria-pressed="true">Phone <span id="phoneBadge" class="target-badge" hidden></span></button>
            <button type="button" class="btn target-btn" id="targetLaptopBtn" aria-pressed="false">Laptop <span id="laptopBadge" class="target-badge" hidden></span></button>
          </div>
        </div>
        <div class="top-actions">
          <button type="button" class="btn sm" id="screenBtn" aria-expanded="false">Screen</button>
          <button type="button" class="btn sm" id="newBtn">New</button>
          <button type="button" class="btn sm danger" id="stopBtn" hidden>Stop</button>
          <button type="button" class="btn sm" id="settingsBtn" aria-expanded="false">Menu</button>
        </div>
      </div>
      <div class="meta-line mono">
        <span class="status-unit"><span id="dot" class="pip"></span><span id="modelText">connecting...</span></span>
      </div>
    </div>
  </header>

  <section id="settingsDrawer" class="drawer" hidden>
    <div class="sheet-w">
      <div class="settings-stack">
        <div class="settings-top">
          <span id="settingsScope" class="scope-pill mono">Phone chat</span>
          <div class="settings-top-btns">
            <button type="button" class="btn sm" id="chatsBtn">Saved Chats</button>
            <button type="button" class="btn sm" id="logoutBtn">Sign Out</button>
          </div>
        </div>
        <div class="settings-row">
          <label class="field-pair grow">Model
            <select id="modelSelect" class="select-sm"></select>
          </label>
          <button type="button" class="btn sm" id="refreshModelsBtn">Refresh</button>
        </div>
        <div class="settings-pair">
          <label class="field-stack">Thinking
            <select id="thinkingSelect" class="select-sm"></select>
          </label>
          <label id="screenThinkingField" class="field-stack">Screen work
            <select id="screenThinkingSelect" class="select-sm"></select>
          </label>
        </div>
        <div class="settings-foot">
          <label class="toggle-label"><input type="checkbox" id="autoModeChk"> Auto mode (hands-free, reviewed)</label>
          <label class="toggle-label"><input type="checkbox" id="alertsChk"> Alerts</label>
        </div>
      </div>
    </div>
  </section>

  <section id="screenDrawer" class="screen-drawer" hidden>
    <div class="sheet-w">
      <div class="screen-bar">
        <button type="button" class="btn sm" id="refreshScreenBtn">Refresh</button>
        <label class="toggle-label"><input type="checkbox" id="autoScreenChk"> Live</label>
        <button type="button" class="btn sm" id="minScreenBtn" aria-expanded="true">Hide</button>
        <button type="button" class="btn sm primary" id="expandScreenBtn">Fullscreen</button>
      </div>
      <details class="screen-opts">
        <summary class="mono"><span>Display options</span><span id="screenMeta" class="screen-meta mono">Not loaded</span></summary>
        <div class="screen-opts-row">
          <select id="monitorSelect" class="select-sm" aria-label="Display" hidden></select>
          <select id="screenFmtSelect" class="select-sm" aria-label="Image format">
            <option value="jpeg">Fast JPG</option>
            <option value="png">Sharp PNG</option>
          </select>
          <select id="screenInterval" class="select-sm" aria-label="Auto refresh interval">
            <option value="500">0.5s</option>
            <option value="1000" selected>1s</option>
            <option value="2000">2s</option>
            <option value="3000">3s</option>
            <option value="5000">5s</option>
          </select>
        </div>
      </details>
      <div id="screenBody" class="screen-body">
        <div class="screen-frame">
          <img id="screenImg" class="screen-img" alt="PC Screen" hidden>
        </div>
      </div>
    </div>
  </section>

  <div id="repairBanner" class="repair-banner" hidden>
    <div class="sheet-w repair-row">
      <span>Session expired (token rotated or signed out).</span>
      <button type="button" class="btn sm primary" id="repairBtn">Pair again</button>
    </div>
  </div>
</div>

<div class="pane-wrap">
  <main id="chatPane">
    <p id="emptyNote" class="empty-note" hidden>Type a request, paste a screenshot, or use + to attach images and files.</p>

    <section id="agentsBox" class="strip" hidden>
      <div class="strip-title">Sub-Agents</div>
      <div id="agentsList"></div>
    </section>
    <section id="tasksBox" class="strip" hidden>
      <div class="strip-title">Tasks</div>
      <div id="tasksList"></div>
    </section>
    <section id="procsBox" class="strip" hidden>
      <div class="strip-title">Background Processes</div>
      <div id="procsList"></div>
    </section>

    <div id="truncNote" class="trunc-note mono" hidden></div>
    <div id="thread"></div>
    <div id="streamWrap" class="msg-wrap assistant" hidden><div id="streamRow" class="msg"></div></div>
    <div id="streamStatus" class="stream-status mono" hidden><span id="streamLabel">Working...</span></div>
    <div id="approvals"></div>
  </main>

  <button type="button" class="btn sm jump-btn" id="jumpBtn" hidden>Bottom</button>
</div>
<div id="toast" class="toast" role="status" hidden></div>

<dialog id="screenModal" class="screen-modal fs-live" closedby="any" aria-labelledby="screenModalTitle">
  <div class="modal-head">
    <h2 id="screenModalTitle" class="modal-title">Live Screen</h2>
    <div class="modal-tools">
      <select id="modalMonitorSelect" class="select-sm" aria-label="Fullscreen display" hidden></select>
      <select id="modalIntervalSelect" class="select-sm" aria-label="Live refresh interval">
        <option value="500">0.5s</option>
        <option value="1000" selected>1s</option>
        <option value="2000">2s</option>
        <option value="3000">3s</option>
        <option value="5000">5s</option>
      </select>
      <button type="button" class="btn sm" id="modalRefreshBtn">Refresh</button>
      <label class="toggle-label"><input type="checkbox" id="modalAutoChk"> Live</label>
      <span id="modalMeta" class="screen-meta mono"></span>
      <button type="button" class="btn sm primary" id="closeModalBtn">Exit</button>
    </div>
  </div>
  <button type="button" class="btn sm fs-exit-fab" id="fsExitFab">Exit</button>
  <div class="modal-stage" id="modalStage">
    <img id="modalImg" class="modal-img" alt="Expanded PC Screen">
  </div>
</dialog>

<dialog id="chatsModal" class="screen-modal" closedby="any" aria-labelledby="chatsModalTitle">
  <div class="modal-head">
    <h2 id="chatsModalTitle" class="modal-title">Saved Chats</h2>
    <button type="button" class="btn sm" id="closeChatsBtn">Close</button>
  </div>
  <div id="chatsList" class="session-list"></div>
</dialog>

<footer class="composer-wrap">
  <div class="sheet-w">
    <div id="queueBar" class="queue-bar" hidden>
      <span id="queueLabel" class="mono"></span>
      <div class="queue-actions">
        <button type="button" class="btn xs" id="queueRetryBtn">Retry</button>
        <button type="button" class="btn xs" id="queueCancelBtn">Cancel</button>
      </div>
    </div>
    <div id="attachStrip" class="attach-strip" hidden></div>
    <form id="sendForm" class="composer">
      <input id="fileInput" type="file" class="visually-hidden" multiple accept="image/*,.txt,.md,.py,.js,.ts,.tsx,.jsx,.json,.csv,.tsv,.yaml,.yml,.toml,.ini,.cfg,.log,.html,.css,.xml,.svg,.sql,.sh,.ps1,.bat,.pdf,.docx,.xlsx,.pptx">
      <button type="button" class="btn icon-btn" id="attachBtn" aria-label="Attach photo or file" title="Attach photo or file">
        <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
          <path d="M9 3v12M3 9h12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
        </svg>
      </button>
      <label for="draft" class="visually-hidden">Instruction for Paperduck</label>
      <textarea id="draft" rows="1" placeholder="Message Paperduck..." enterkeyhint="enter"></textarea>
      <button type="submit" class="btn primary" id="sendBtn">Send</button>
    </form>
  </div>
</footer>

<script nonce="{nonce}">
(() => {{
  const MAX_FILES = {MAX_REMOTE_ATTACHMENTS};
  const MAX_FILE_BYTES = {MAX_REMOTE_FILE_BYTES};
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const dot = document.getElementById('dot');
  const modelText = document.getElementById('modelText');
  const targetPhoneBtn = document.getElementById('targetPhoneBtn');
  const targetLaptopBtn = document.getElementById('targetLaptopBtn');
  const phoneBadge = document.getElementById('phoneBadge');
  const laptopBadge = document.getElementById('laptopBadge');
  const screenBtn = document.getElementById('screenBtn');
  const settingsBtn = document.getElementById('settingsBtn');
  const settingsDrawer = document.getElementById('settingsDrawer');
  const settingsScope = document.getElementById('settingsScope');
  const modelSelect = document.getElementById('modelSelect');
  const refreshModelsBtn = document.getElementById('refreshModelsBtn');
  const thinkingSelect = document.getElementById('thinkingSelect');
  const screenThinkingField = document.getElementById('screenThinkingField');
  const screenThinkingSelect = document.getElementById('screenThinkingSelect');
  const autoModeChk = document.getElementById('autoModeChk');
  const logoutBtn = document.getElementById('logoutBtn');
  const chatsBtn = document.getElementById('chatsBtn');
  const chatsModal = document.getElementById('chatsModal');
  const chatsList = document.getElementById('chatsList');
  const closeChatsBtn = document.getElementById('closeChatsBtn');
  const screenDrawer = document.getElementById('screenDrawer');
  const screenBody = document.getElementById('screenBody');
  const monitorSelect = document.getElementById('monitorSelect');
  const screenFmtSelect = document.getElementById('screenFmtSelect');
  const refreshScreenBtn = document.getElementById('refreshScreenBtn');
  const autoScreenChk = document.getElementById('autoScreenChk');
  const screenInterval = document.getElementById('screenInterval');
  const minScreenBtn = document.getElementById('minScreenBtn');
  const expandScreenBtn = document.getElementById('expandScreenBtn');
  const screenMeta = document.getElementById('screenMeta');
  const screenImg = document.getElementById('screenImg');
  const screenModal = document.getElementById('screenModal');
  const modalMonitorSelect = document.getElementById('modalMonitorSelect');
  const modalIntervalSelect = document.getElementById('modalIntervalSelect');
  const modalRefreshBtn = document.getElementById('modalRefreshBtn');
  const modalAutoChk = document.getElementById('modalAutoChk');
  const modalMeta = document.getElementById('modalMeta');
  const modalStage = document.getElementById('modalStage');
  const modalImg = document.getElementById('modalImg');
  const closeModalBtn = document.getElementById('closeModalBtn');
  const fsExitFab = document.getElementById('fsExitFab');
  const newBtn = document.getElementById('newBtn');
  const stopBtn = document.getElementById('stopBtn');
  const chatPane = document.getElementById('chatPane');
  const emptyNote = document.getElementById('emptyNote');
  const agentsBox = document.getElementById('agentsBox');
  const agentsList = document.getElementById('agentsList');
  const tasksBox = document.getElementById('tasksBox');
  const tasksList = document.getElementById('tasksList');
  const procsBox = document.getElementById('procsBox');
  const procsList = document.getElementById('procsList');
  const truncNote = document.getElementById('truncNote');
  const threadEl = document.getElementById('thread');
  const streamWrap = document.getElementById('streamWrap');
  const streamRow = document.getElementById('streamRow');
  const streamStatus = document.getElementById('streamStatus');
  const streamLabel = document.getElementById('streamLabel');
  const approvalsEl = document.getElementById('approvals');
  const jumpBtn = document.getElementById('jumpBtn');
  const toastEl = document.getElementById('toast');
  const attachStrip = document.getElementById('attachStrip');
  const fileInput = document.getElementById('fileInput');
  const attachBtn = document.getElementById('attachBtn');
  const sendForm = document.getElementById('sendForm');
  const draft = document.getElementById('draft');
  const sendBtn = document.getElementById('sendBtn');
  const alertsChk = document.getElementById('alertsChk');
  const repairBanner = document.getElementById('repairBanner');
  const repairBtn = document.getElementById('repairBtn');
  const queueBar = document.getElementById('queueBar');
  const queueLabel = document.getElementById('queueLabel');
  const queueRetryBtn = document.getElementById('queueRetryBtn');
  const queueCancelBtn = document.getElementById('queueCancelBtn');

  let lastHeaderKey = '';
  let lastTarget = '';
  let lastSettingsKey = '';
  let lastMonitorsKey = '';
  let lastAgentsKey = '';
  let lastTasksKey = '';
  let lastProcsKey = '';
  let lastHistoryKey = '';
  let lastStreamKey = '';
  let lastApprovalsKey = '';
  let screenTimer = null;
  let screenOpen = false;
  let settingsOpen = false;
  let screenMinimized = false;
  let loadingScreen = false;
  let selectedMonitor = 0;
  let toastTimer = null;
  let newConfirmTimer = null;
  let newConfirmArmed = false;
  let hasHistory = false;
  let isBusy = false;
  let staged = [];
  let sending = false;
  let pendingTarget = null;
  let userTouching = false;
  let disconnectTimer = null;
  let sessionExpired = false;
  let unreadDone = false;
  let wasBusy = null;
  let prevApprovalsCount = 0;
  let pendingQueue = [];
  let flushingQueue = false;
  let alertsEnabled = false;
  try {{ alertsEnabled = localStorage.getItem('pd_alerts') === '1'; }} catch (_e) {{}}
  alertsChk.checked = alertsEnabled;
  const openAgents = new Set();

  function saveDraft() {{
    try {{
      if (draft.value) sessionStorage.setItem('pd_draft', draft.value);
      else sessionStorage.removeItem('pd_draft');
    }} catch (_e) {{}}
  }}

  function handleUnauthorized() {{
    if (sessionExpired) return;
    sessionExpired = true;
    saveDraft();
    clearTimeout(sseTimer);
    if (disconnectTimer) {{ clearTimeout(disconnectTimer); disconnectTimer = null; }}
    if (es) {{
      try {{ es.close(); }} catch (_e) {{}}
      es = null;
    }}
    dot.className = 'pip off';
    modelText.textContent = 'session expired';
    repairBanner.hidden = false;
  }}

  function syncTitle(busy, approvalCount) {{
    if (approvalCount > 0) {{
      document.title = '(' + approvalCount + '!) Paperduck';
    }} else if (unreadDone) {{
      document.title = '(Done) Paperduck';
    }} else if (busy) {{
      document.title = '[*] Paperduck';
    }} else {{
      document.title = 'Paperduck';
    }}
  }}

  function notifyUser(title, body) {{
    try {{
      if (navigator.vibrate) navigator.vibrate([120, 60, 120]);
    }} catch (_e) {{}}
    if (!alertsEnabled || typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
    if (!document.hidden) return;
    try {{
      const n = new Notification(title, {{ body: body || '', tag: 'paperduck-remote' }});
      setTimeout(() => {{ try {{ n.close(); }} catch (_e) {{}} }}, 8000);
    }} catch (_e) {{}}
  }}

  function showToast(text) {{
    toastEl.textContent = text;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {{ toastEl.hidden = true; }}, 1900);
  }}

  function legacyCopy(text) {{
    const field = Object.assign(document.createElement('textarea'), {{ value: text }});
    field.setAttribute('readonly', '');
    field.style.cssText = 'position:fixed;inset-block-start:0;opacity:0';
    document.body.append(field);
    field.select();
    try {{ return document.execCommand('copy'); }} finally {{ field.remove(); }}
  }}

  async function copyText(text, btn) {{
    try {{
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        await navigator.clipboard.writeText(text);
      }} else if (!legacyCopy(text)) {{
        throw new Error('copy unavailable');
      }}
      if (btn) {{
        const orig = btn.textContent;
        btn.textContent = 'Copied';
        setTimeout(() => {{ btn.textContent = orig; }}, 1300);
      }}
    }} catch (_e) {{
      if (legacyCopy(text)) {{
        if (btn) {{
          const orig = btn.textContent;
          btn.textContent = 'Copied';
          setTimeout(() => {{ btn.textContent = orig; }}, 1300);
        }}
        return;
      }}
      showToast('Copy unavailable here');
    }}
  }}

  function sizeLabel(n) {{
    return n < 1024 ? n + ' B' : n < 1048576 ? (n / 1024).toFixed(1) + ' KB' : (n / 1048576).toFixed(1) + ' MB';
  }}

  function renderChips() {{
    attachStrip.textContent = '';
    attachStrip.hidden = staged.length === 0;
    staged.forEach((item, idx) => {{
      const chip = document.createElement('div');
      chip.className = 'chip';
      if (item.thumbUrl) {{
        const im = document.createElement('img');
        im.className = 'chip-thumb';
        im.src = item.thumbUrl;
        im.alt = item.name;
        chip.appendChild(im);
      }} else {{
        const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('viewBox', '0 0 20 20');
        svg.setAttribute('aria-hidden', 'true');
        svg.classList.add('chip-icon');
        const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        p.setAttribute('d', 'M5 2h7l4 4v12H5V2zm7 0v4h4');
        p.setAttribute('fill', 'none');
        p.setAttribute('stroke', 'currentColor');
        p.setAttribute('stroke-width', '1.4');
        svg.appendChild(p);
        chip.appendChild(svg);
      }}
      const info = document.createElement('div');
      info.className = 'chip-info';
      const nm = document.createElement('span');
      nm.className = 'chip-name';
      nm.textContent = item.name;
      const sz = document.createElement('span');
      sz.className = 'chip-size mono';
      sz.textContent = sizeLabel(item.size);
      info.append(nm, sz);
      const rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'chip-x';
      rm.setAttribute('aria-label', 'Remove ' + item.name);
      rm.textContent = 'x';
      rm.addEventListener('click', () => {{
        const dropped = staged.splice(idx, 1)[0];
        if (dropped && dropped.thumbUrl) URL.revokeObjectURL(dropped.thumbUrl);
        renderChips();
      }});
      chip.append(info, rm);
      attachStrip.appendChild(chip);
    }});
  }}

  function addFiles(fileList) {{
    for (const file of fileList) {{
      if (staged.length >= MAX_FILES) {{
        showToast('Up to ' + MAX_FILES + ' files per message');
        break;
      }}
      if (file.size > MAX_FILE_BYTES) {{
        showToast(file.name + ' exceeds 4 MB');
        continue;
      }}
      const isImg = (file.type || '').startsWith('image/');
      staged.push({{
        file,
        name: file.name || (isImg ? 'pasted.png' : 'file'),
        mime: file.type || '',
        size: file.size,
        thumbUrl: isImg ? URL.createObjectURL(file) : '',
      }});
    }}
    renderChips();
  }}

  function clearStaged({{ revoke = true }} = {{}}) {{
    if (revoke) {{
      for (const item of staged) {{
        if (item.thumbUrl) URL.revokeObjectURL(item.thumbUrl);
      }}
    }}
    staged = [];
    renderChips();
  }}

  function readFileBase64(file) {{
    return new Promise((resolve, reject) => {{
      const reader = new FileReader();
      reader.onload = () => {{
        const res = String(reader.result || '');
        const comma = res.indexOf(',');
        resolve(comma >= 0 ? res.slice(comma + 1) : res);
      }};
      reader.onerror = () => reject(reader.error || new Error('read failed'));
      reader.readAsDataURL(file);
    }});
  }}

  async function apiPost(path, payload) {{
    const res = await fetch(path, {{
      method: 'POST',
      credentials: 'same-origin',
      headers: {{ 'Content-Type': 'application/json', 'X-CSRF-Token': csrf }},
      body: JSON.stringify(payload || {{}}),
    }});
    if (res.status === 401) {{
      handleUnauthorized();
      return {{ ok: false, status: 401, error: 'Session expired' }};
    }}
    try {{
      const data = await res.json();
      if (data && typeof data === 'object' && data.status === undefined) data.status = res.status;
      return data;
    }} catch (_e) {{
      return {{ ok: false, status: res.status, error: 'HTTP ' + res.status }};
    }}
  }}

  function appendFormattedLine(parent, line) {{
    const re = /(`[^`]+`|\\[[^\\]]+\\]\\(https?:\\/\\/[^\\s)]+\\)|\\*\\*[^*]+\\*\\*|~~[^~]+~~|\\*(?!\\s)[^*]+?(?<!\\s)\\*|https?:\\/\\/[^\\s<>()]+)/g;
    let idx = 0;
    let m;
    while ((m = re.exec(line)) !== null) {{
      if (m.index > idx) parent.appendChild(document.createTextNode(line.slice(idx, m.index)));
      const tok = m[0];
      if (tok.startsWith('`') && tok.endsWith('`')) {{
        const c = document.createElement('code');
        c.textContent = tok.slice(1, -1);
        parent.appendChild(c);
      }} else if (tok.startsWith('[')) {{
        const lm = /^\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)$/.exec(tok);
        if (lm) {{
          const a = document.createElement('a');
          a.href = lm[2];
          a.target = '_blank';
          a.rel = 'noopener noreferrer';
          a.textContent = lm[1];
          parent.appendChild(a);
        }} else {{
          parent.appendChild(document.createTextNode(tok));
        }}
      }} else if (tok.startsWith('**') && tok.endsWith('**')) {{
        const b = document.createElement('strong');
        b.textContent = tok.slice(2, -2);
        parent.appendChild(b);
      }} else if (tok.startsWith('~~') && tok.endsWith('~~')) {{
        const del = document.createElement('del');
        del.textContent = tok.slice(2, -2);
        parent.appendChild(del);
      }} else if (tok.startsWith('*') && tok.endsWith('*')) {{
        const em = document.createElement('em');
        em.textContent = tok.slice(1, -1);
        parent.appendChild(em);
      }} else {{
        const trail = /[.,;:!?)]+$/.exec(tok);
        const cleanUrl = trail ? tok.slice(0, -trail[0].length) : tok;
        const a = document.createElement('a');
        a.href = cleanUrl;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        a.textContent = cleanUrl;
        parent.appendChild(a);
        if (trail) parent.appendChild(document.createTextNode(trail[0]));
      }}
      idx = m.index + tok.length;
    }}
    if (idx < line.length) parent.appendChild(document.createTextNode(line.slice(idx)));
  }}

  function splitTableCells(line) {{
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|')) s = s.slice(0, -1);
    return s.split('|').map((c) => c.trim());
  }}

  function isTableSep(line) {{
    return /^\\|?\\s*:?-{3,}:?\\s*(\\|\\s*:?-{3,}:?\\s*)+\\|?$/.test(line.trim());
  }}

  function renderSafeBody(container, text) {{
    container.textContent = '';
    const lines = String(text || '').split(/\\r?\\n/);
    let inFence = false;
    let codeEl = null;
    let listEl = null;
    let listTag = '';

    function flushList() {{ listEl = null; listTag = ''; }}

    for (let i = 0; i < lines.length; i++) {{
      const raw = lines[i];
      const trimmed = raw.trim();
      if (trimmed.startsWith('```')) {{
        flushList();
        if (!inFence) {{
          inFence = true;
          const lang = trimmed.slice(3).trim() || 'code';
          const wrap = document.createElement('div');
          wrap.className = 'code-wrap';
          const bar = document.createElement('div');
          bar.className = 'code-bar mono';
          const lbl = document.createElement('span');
          lbl.textContent = lang;
          const cp = document.createElement('button');
          cp.type = 'button';
          cp.className = 'btn sm';
          cp.textContent = 'Copy';
          bar.append(lbl, cp);
          const pre = document.createElement('pre');
          codeEl = document.createElement('code');
          pre.appendChild(codeEl);
          wrap.append(bar, pre);
          container.appendChild(wrap);
          cp.addEventListener('click', ((el, btn) => () => copyText(el.textContent || '', btn))(codeEl, cp));
        }} else {{
          inFence = false;
          codeEl = null;
        }}
        continue;
      }}
      if (inFence && codeEl) {{
        codeEl.textContent += (codeEl.textContent ? '\\n' : '') + raw;
        continue;
      }}
      if (trimmed === '') {{
        flushList();
        const sp = document.createElement('div');
        sp.className = 'spacer';
        container.appendChild(sp);
        continue;
      }}
      if (trimmed.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {{
        flushList();
        const wrap = document.createElement('div');
        wrap.className = 'table-wrap';
        const tbl = document.createElement('table');
        const thead = document.createElement('thead');
        const htr = document.createElement('tr');
        for (const h of splitTableCells(raw)) {{
          const th = document.createElement('th');
          appendFormattedLine(th, h);
          htr.appendChild(th);
        }}
        thead.appendChild(htr);
        tbl.appendChild(thead);
        const tbody = document.createElement('tbody');
        i += 2;
        while (i < lines.length && lines[i].trim().includes('|') && lines[i].trim() !== '') {{
          const rtr = document.createElement('tr');
          for (const cell of splitTableCells(lines[i])) {{
            const td = document.createElement('td');
            appendFormattedLine(td, cell);
            rtr.appendChild(td);
          }}
          tbody.appendChild(rtr);
          i++;
        }}
        i--;
        tbl.appendChild(tbody);
        wrap.appendChild(tbl);
        container.appendChild(wrap);
        continue;
      }}
      if (/^(?:---+|\\*\\*\\*+|___+)$/.test(trimmed)) {{
        flushList();
        container.appendChild(document.createElement('hr'));
        continue;
      }}
      const hMatch = /^(#{{1,3}})\\s+(.*)$/.exec(raw);
      if (hMatch) {{
        flushList();
        const h = document.createElement('h' + hMatch[1].length);
        appendFormattedLine(h, hMatch[2]);
        container.appendChild(h);
        continue;
      }}
      const ulMatch = /^\\s*[-*]\\s+(.*)$/.exec(raw);
      if (ulMatch) {{
        if (listTag !== 'ul') {{
          listTag = 'ul';
          listEl = document.createElement('ul');
          container.appendChild(listEl);
        }}
        const li = document.createElement('li');
        appendFormattedLine(li, ulMatch[1]);
        listEl.appendChild(li);
        continue;
      }}
      const olMatch = /^\\s*\\d+\\.\\s+(.*)$/.exec(raw);
      if (olMatch) {{
        if (listTag !== 'ol') {{
          listTag = 'ol';
          listEl = document.createElement('ol');
          container.appendChild(listEl);
        }}
        const li = document.createElement('li');
        appendFormattedLine(li, olMatch[1]);
        listEl.appendChild(li);
        continue;
      }}
      flushList();
      if (/^>\\s?/.test(raw)) {{
        const bq = document.createElement('blockquote');
        appendFormattedLine(bq, raw.replace(/^>\\s?/, ''));
        container.appendChild(bq);
        continue;
      }}
      const p = document.createElement('p');
      appendFormattedLine(p, raw);
      container.appendChild(p);
    }}
  }}

  function isNearBottom() {{
    return (chatPane.scrollTop + chatPane.clientHeight) >= (chatPane.scrollHeight - 44);
  }}

  function syncJumpButton() {{
    const scrollable = chatPane.scrollHeight > chatPane.clientHeight + 64;
    jumpBtn.hidden = !(scrollable && !isNearBottom());
  }}

  function buildTurnNode(item, turnKey) {{
    const role = item.role || 'assistant';
    const wrap = document.createElement('div');
    wrap.className = 'msg-wrap ' + role;
    wrap.dataset.turnKey = turnKey;
    if (role === 'user' && ((item.attachments && item.attachments.length) || item.steering)) {{
      const atts = document.createElement('div');
      atts.className = 'user-atts';
      if (item.steering) {{
        const st = document.createElement('span');
        st.className = 'steer-tag mono';
        st.textContent = 'steering';
        atts.appendChild(st);
      }}
      for (const fname of (item.attachments || [])) {{
        const b = document.createElement('span');
        b.className = 'att-badge mono';
        b.textContent = fname;
        atts.appendChild(b);
      }}
      wrap.appendChild(atts);
    }}
    if (item.text) {{
      const box = document.createElement('div');
      box.className = 'msg';
      if (role === 'error') {{
        box.textContent = item.text;
      }} else {{
        renderSafeBody(box, item.text);
      }}
      wrap.appendChild(box);
    }}
    const detailsList = (item.tool_details && item.tool_details.length)
      ? item.tool_details
      : (item.tools || []).map((name) => ({{ name, summary: '' }}));
    if (detailsList.length || (role === 'assistant' && item.text)) {{
      const foot = document.createElement('div');
      foot.className = 'msg-foot';
      if (detailsList.length) {{
        const det = document.createElement('details');
        det.className = 'tools-details';
        const sum = document.createElement('summary');
        sum.className = 'mono';
        sum.textContent = detailsList.length + (detailsList.length === 1 ? ' tool' : ' tools');
        const tl = document.createElement('div');
        tl.className = 'tools-list';
        for (const td of detailsList) {{
          const row = document.createElement('div');
          row.className = 'tool-row';
          const pill = document.createElement('span');
          pill.className = 'tool-pill';
          pill.textContent = td.name;
          row.appendChild(pill);
          if (td.summary) {{
            const sm = document.createElement('span');
            sm.className = 'tool-sum';
            sm.textContent = td.summary;
            row.appendChild(sm);
          }}
          tl.appendChild(row);
        }}
        det.append(sum, tl);
        foot.appendChild(det);
      }}
      if (role === 'assistant' && item.text) {{
        const cp = document.createElement('button');
        cp.type = 'button';
        cp.className = 'copy-link';
        cp.textContent = 'Copy';
        cp.addEventListener('click', () => copyText(item.text, cp));
        foot.appendChild(cp);
      }}
      wrap.appendChild(foot);
    }}
    return wrap;
  }}

  function renderState(s) {{
    if (!s) return;
    const curTarget = s.target === 'laptop' ? 'laptop' : 'phone';
    if (pendingTarget && curTarget !== pendingTarget) return;
    const atBottom = isNearBottom();
    let shouldScroll = false;
    isBusy = !!s.busy;
    if (disconnectTimer) {{ clearTimeout(disconnectTimer); disconnectTimer = null; }}

    if (lastTarget && curTarget !== lastTarget) {{
      lastHeaderKey = '';
      lastSettingsKey = '';
      lastAgentsKey = '';
      lastTasksKey = '';
      lastProcsKey = '';
      lastHistoryKey = '';
      lastStreamKey = '';
      lastApprovalsKey = '';
      wasBusy = null;
      prevApprovalsCount = (s.approvals || []).length;
      resetNewConfirm();
    }}
    lastTarget = curTarget;

    const targets = s.targets || {{}};
    for (const [key, btn, badge] of [['phone', targetPhoneBtn, phoneBadge], ['laptop', targetLaptopBtn, laptopBadge]]) {{
      const active = curTarget === key;
      btn.classList.toggle('active', active);
      btn.setAttribute('aria-pressed', String(active));
      const info = targets[key] || {{}};
      if (info.approvals > 0) {{
        badge.textContent = info.approvals + '!';
        badge.hidden = false;
      }} else if (info.busy) {{
        badge.textContent = '*';
        badge.hidden = false;
      }} else {{
        badge.hidden = true;
      }}
    }}
    settingsScope.textContent = curTarget === 'laptop' ? 'Laptop chat' : 'Phone chat';

    const headerKey = JSON.stringify([curTarget, s.busy, s.model_name || s.model, s.thinking, s.screen_thinking || '', s.auto_mode, s.progress]);
    if (headerKey !== lastHeaderKey) {{
      lastHeaderKey = headerKey;
      if (s.busy) shouldScroll = true;
      dot.className = 'pip' + (s.busy ? ' busy' : '');
      const shownModel = s.model_name || s.model || 'no model';
      const thinkTag = (s.thinking && s.thinking !== 'off') ? ' · ' + s.thinking : '';
      const screenTag = s.screen_thinking ? ' · scr ' + s.screen_thinking : '';
      modelText.textContent = shownModel + thinkTag + screenTag + (s.auto_mode ? ' · auto' : '');
      newBtn.hidden = !!s.busy;
      stopBtn.hidden = !s.busy;
      newBtn.disabled = !!s.busy;
      if (s.busy && newConfirmArmed) resetNewConfirm();
      if (!sending) sendBtn.textContent = s.busy ? 'Steer' : 'Send';
      streamStatus.hidden = !s.busy;
      streamLabel.textContent = s.progress || 'Thinking...';
    }}

    const settingsKey = JSON.stringify([curTarget, s.models || [], s.model, s.thinking_levels || [], s.thinking, s.screen_thinking || '', s.supports_screen_thinking !== false, !!s.auto_mode, !!s.ready]);
    if (settingsKey !== lastSettingsKey) {{
      lastSettingsKey = settingsKey;
      modelSelect.textContent = '';
      const models = s.models || [];
      if (!models.length) {{
        const opt = document.createElement('option');
        opt.value = s.model || '';
        opt.textContent = s.model || (s.ready ? 'Loading models...' : 'Setup needed on PC');
        modelSelect.appendChild(opt);
      }} else {{
        for (const m of models) {{
          const opt = document.createElement('option');
          opt.value = m.id;
          opt.textContent = (m.name && m.name !== m.id) ? (m.name + ' (' + m.id + ')') : m.id;
          modelSelect.appendChild(opt);
        }}
      }}
      if (s.model) modelSelect.value = s.model;
      modelSelect.disabled = !s.ready || !models.length;
      refreshModelsBtn.disabled = !s.ready;

      const levels = s.thinking_levels || ['off', 'low', 'medium', 'high'];
      thinkingSelect.textContent = '';
      for (const lv of levels) {{
        const opt = document.createElement('option');
        opt.value = lv;
        opt.textContent = lv.charAt(0).toUpperCase() + lv.slice(1);
        thinkingSelect.appendChild(opt);
      }}
      if (s.thinking) thinkingSelect.value = s.thinking;

      screenThinkingField.hidden = s.supports_screen_thinking === false;
      screenThinkingSelect.textContent = '';
      const sSame = document.createElement('option');
      sSame.value = '';
      sSame.textContent = 'Same as chat';
      screenThinkingSelect.appendChild(sSame);
      for (const lv of levels) {{
        const opt = document.createElement('option');
        opt.value = lv;
        opt.textContent = lv.charAt(0).toUpperCase() + lv.slice(1);
        screenThinkingSelect.appendChild(opt);
      }}
      screenThinkingSelect.value = s.screen_thinking || '';
      autoModeChk.checked = !!s.auto_mode;
    }}

    const monitors = s.monitors || [];
    const monitorsKey = JSON.stringify(monitors);
    if (monitorsKey !== lastMonitorsKey) {{
      lastMonitorsKey = monitorsKey;
      if (selectedMonitor >= monitors.length) selectedMonitor = 0;
      for (const sel of [monitorSelect, modalMonitorSelect]) {{
        sel.textContent = '';
        for (const m of monitors) {{
          const opt = document.createElement('option');
          opt.value = String(m.index);
          opt.textContent = 'Display ' + (m.index + 1) + ' (' + m.width + 'x' + m.height + ')' + (m.primary ? ' *' : '');
          sel.appendChild(opt);
        }}
        sel.value = String(selectedMonitor);
        sel.hidden = monitors.length <= 1;
      }}
    }}

    const agents = s.agents || [];
    const agentsKey = JSON.stringify(agents);
    if (agentsKey !== lastAgentsKey) {{
      lastAgentsKey = agentsKey;
      shouldScroll = true;
      agentsBox.hidden = agents.length === 0;
      agentsList.textContent = '';
      for (const ag of agents) {{
        const det = document.createElement('details');
        det.className = 'agent-card';
        if (openAgents.has(ag.n) || ag.status === 'running') det.open = true;
        det.addEventListener('toggle', () => {{
          if (det.open) openAgents.add(ag.n);
          else openAgents.delete(ag.n);
        }});
        const sum = document.createElement('summary');
        const badge = document.createElement('span');
        badge.className = 'agent-badge mono ' + (ag.status || '');
        badge.textContent = ag.status || 'running';
        const ttl = document.createElement('span');
        ttl.textContent = 'Agent ' + ag.n + ' (' + (ag.effort || 'medium') + ', ' + (ag.calls || 0) + ' calls, ' + (ag.elapsed || 0) + 's)';
        sum.append(badge, ttl);
        det.appendChild(sum);
        if (ag.task) {{
          const tk = document.createElement('div');
          tk.className = 'agent-task';
          tk.textContent = ag.task;
          det.appendChild(tk);
        }}
        if (ag.log && ag.log.length) {{
          const pre = document.createElement('pre');
          pre.className = 'agent-log mono';
          pre.textContent = ag.log.join('\\n');
          det.appendChild(pre);
        }}
        if (ag.report) {{
          const rep = document.createElement('div');
          rep.className = 'agent-report msg';
          renderSafeBody(rep, ag.report);
          det.appendChild(rep);
        }}
        agentsList.appendChild(det);
      }}
    }}

    const tasks = s.tasks || [];
    const tasksKey = JSON.stringify(tasks);
    if (tasksKey !== lastTasksKey) {{
      lastTasksKey = tasksKey;
      shouldScroll = true;
      tasksBox.hidden = tasks.length === 0;
      tasksList.textContent = '';
      for (const [label, status] of tasks) {{
        const row = document.createElement('div');
        row.className = 'task-item' + (status === 'done' ? ' task-done' : '');
        const marker = document.createElement('span');
        marker.className = 'task-mark mono';
        marker.textContent = status === 'done' ? '[x]' : status === 'in_progress' ? '[>]' : '[ ]';
        const txt = document.createElement('span');
        txt.textContent = label;
        row.append(marker, txt);
        tasksList.appendChild(row);
      }}
    }}

    const procs = s.procs || [];
    const procsKey = JSON.stringify(procs);
    if (procsKey !== lastProcsKey) {{
      lastProcsKey = procsKey;
      shouldScroll = true;
      procsBox.hidden = procs.length === 0;
      procsList.textContent = '';
      for (const [name, cmd] of procs) {{
        const row = document.createElement('div');
        row.className = 'proc-row';
        const lbl = document.createElement('span');
        lbl.className = 'proc-cmd mono';
        lbl.textContent = name + ': ' + cmd;
        const stopP = document.createElement('button');
        stopP.type = 'button';
        stopP.className = 'btn xs danger';
        stopP.textContent = 'Stop';
        stopP.addEventListener('click', async () => {{
          stopP.disabled = true;
          stopP.textContent = '...';
          const out = await apiPost('/api/procs/stop', {{ name }});
          if (!out || !out.ok) {{
            showToast((out && (out.message || out.error)) || 'Stop failed');
            stopP.disabled = false;
            stopP.textContent = 'Stop';
          }} else {{
            showToast('Stopping ' + name);
          }}
        }});
        row.append(lbl, stopP);
        procsList.appendChild(row);
      }}
    }}

    const history = s.history || [];
    hasHistory = history.length > 0;
    const trunc = Number(s.history_truncated || 0);
    truncNote.hidden = trunc <= 0;
    if (trunc > 0) truncNote.textContent = trunc + ' earlier messages hidden';

    const historyKey = JSON.stringify([history, s.error]);
    if (historyKey !== lastHistoryKey) {{
      lastHistoryKey = historyKey;
      shouldScroll = true;
      const entries = history.slice();
      if (s.error) entries.push({{ role: 'error', text: s.error }});
      const existing = new Map();
      for (const ch of Array.from(threadEl.children)) {{
        if (ch.dataset && ch.dataset.turnKey) existing.set(ch.dataset.turnKey, ch);
      }}
      const seenCounts = new Map();
      const desired = [];
      for (const item of entries) {{
        const sig = JSON.stringify(item);
        const count = seenCounts.get(sig) || 0;
        seenCounts.set(sig, count + 1);
        const tkey = sig + '#' + count;
        const node = existing.get(tkey) || buildTurnNode(item, tkey);
        desired.push(node);
      }}
      for (let i = 0; i < desired.length; i++) {{
        const cur = threadEl.children[i];
        if (cur !== desired[i]) threadEl.insertBefore(desired[i], cur || null);
      }}
      while (threadEl.children.length > desired.length) {{
        threadEl.lastElementChild.remove();
      }}
    }}

    const stream = s.stream_text || '';
    if (stream !== lastStreamKey) {{
      lastStreamKey = stream;
      shouldScroll = true;
      if (stream) {{
        streamWrap.hidden = false;
        renderSafeBody(streamRow, stream);
      }} else {{
        streamWrap.hidden = true;
        streamRow.textContent = '';
      }}
    }}

    emptyNote.hidden = history.length > 0 || !!stream || !!s.busy || !!s.error;

    const approvals = s.approvals || [];
    if (wasBusy === true && !s.busy && document.hidden) {{
      unreadDone = true;
      notifyUser('Paperduck finished', 'Reply is ready.');
    }}
    if (approvals.length > prevApprovalsCount) {{
      notifyUser('Paperduck needs approval', (approvals[0] && approvals[0].title) || 'Tap Allow or Deny.');
    }}
    wasBusy = !!s.busy;
    prevApprovalsCount = approvals.length;
    syncTitle(!!s.busy, approvals.length);

    const approvalsKey = JSON.stringify(approvals);
    if (approvalsKey !== lastApprovalsKey) {{
      lastApprovalsKey = approvalsKey;
      shouldScroll = true;
      approvalsEl.textContent = '';
      for (const ap of approvals) {{
        const card = document.createElement('section');
        card.className = 'approval' + (ap.danger ? ' danger' : '');
        const head = document.createElement('div');
        head.className = 'approval-head';
        head.textContent = ap.title || 'Permission needed';
        card.appendChild(head);
        if (ap.review) {{
          const rev = document.createElement('div');
          rev.textContent = 'Reviewer: ' + ap.review;
          card.appendChild(rev);
        }}
        if (ap.intro) {{
          const intro = document.createElement('div');
          intro.textContent = ap.intro;
          card.appendChild(intro);
        }}
        if (ap.detail) {{
          const lines = String(ap.detail).split(/\\r?\\n/);
          const isDiff = lines.some((ln) => ln.startsWith('@@ ') || ln.startsWith('diff --git '))
            || (lines.some((ln) => ln.startsWith('+') && !ln.startsWith('+++')) && lines.some((ln) => ln.startsWith('-') && !ln.startsWith('---')));
          const isLong = lines.length > 10 || ap.detail.length > 400;
          const isWide = isDiff || lines.some((ln) => ln.length > 52);
          const pre = document.createElement('pre');
          if (isLong) pre.classList.add('collapsed');
          if (isDiff) pre.classList.add('nowrap');
          if (isDiff) {{
            for (const ln of lines) {{
              const span = document.createElement('span');
              span.className = 'diff-line';
              if (ln.startsWith('@@') || ln.startsWith('diff ') || ln.startsWith('+++') || ln.startsWith('---')) span.classList.add('diff-hdr');
              else if (ln.startsWith('+')) span.classList.add('diff-add');
              else if (ln.startsWith('-')) span.classList.add('diff-del');
              span.textContent = ln;
              pre.appendChild(span);
            }}
          }} else {{
            pre.textContent = ap.detail;
          }}
          if (isLong || isWide) {{
            const bar = document.createElement('div');
            bar.className = 'approval-detail-bar';
            if (isWide) {{
              const wrapBtn = document.createElement('button');
              wrapBtn.type = 'button';
              wrapBtn.className = 'btn xs';
              wrapBtn.textContent = pre.classList.contains('nowrap') ? 'Wrap' : 'Scroll';
              wrapBtn.addEventListener('click', () => {{
                const nowrap = pre.classList.toggle('nowrap');
                wrapBtn.textContent = nowrap ? 'Wrap' : 'Scroll';
              }});
              bar.appendChild(wrapBtn);
            }}
            if (isLong) {{
              const expBtn = document.createElement('button');
              expBtn.type = 'button';
              expBtn.className = 'btn xs';
              expBtn.textContent = 'Expand (' + lines.length + ' lines)';
              expBtn.addEventListener('click', () => {{
                const col = pre.classList.toggle('collapsed');
                expBtn.textContent = col ? ('Expand (' + lines.length + ' lines)') : 'Collapse';
              }});
              bar.appendChild(expBtn);
            }}
            card.appendChild(bar);
          }}
          card.appendChild(pre);
        }}
        const acts = document.createElement('div');
        acts.className = 'approval-actions';
        const apBtns = [];
        async function submitApproval(choice) {{
          for (const btn of apBtns) btn.disabled = true;
          try {{
            const out = await apiPost('/api/approve', {{ id: ap.id, choice }});
            if (!out || !out.ok) {{
              showToast((out && (out.error || out.message)) || 'Approval expired or failed');
              for (const btn of apBtns) btn.disabled = false;
            }}
          }} catch (_e) {{
            showToast('Network error');
            for (const btn of apBtns) btn.disabled = false;
          }}
        }}
        for (const c of (ap.choices || ['once'])) {{
          const b = document.createElement('button');
          b.type = 'button';
          b.className = 'btn sm ' + (c === 'once' ? 'primary' : '');
          b.textContent = c === 'chat' ? 'Allow for this chat' : 'Allow once';
          b.addEventListener('click', () => submitApproval(c));
          acts.appendChild(b);
          apBtns.push(b);
        }}
        const deny = document.createElement('button');
        deny.type = 'button';
        deny.className = 'btn sm danger';
        deny.textContent = 'Deny';
        deny.addEventListener('click', () => submitApproval(null));
        acts.appendChild(deny);
        apBtns.push(deny);
        card.appendChild(acts);
        approvalsEl.appendChild(card);
      }}
    }}

    if (!userTouching && atBottom && shouldScroll) {{
      chatPane.scrollTop = chatPane.scrollHeight;
      requestAnimationFrame(() => {{
        if (!userTouching) chatPane.scrollTop = chatPane.scrollHeight;
        syncJumpButton();
      }});
    }} else {{
      syncJumpButton();
    }}
  }}

  async function refreshState() {{
    try {{
      const res = await fetch('/api/state', {{ credentials: 'same-origin' }});
      if (res.status === 401) {{ handleUnauthorized(); return; }}
      if (res.ok) {{
        sessionExpired = false;
        repairBanner.hidden = true;
        renderState(await res.json());
      }}
    }} catch (_e) {{}}
  }}

  function setScreenStatus(text) {{
    screenMeta.textContent = text;
    modalMeta.textContent = text;
  }}

  let prevAutoBeforeFs = null;
  let pendingScreenReload = false;

  function screenVisible() {{
    return !document.hidden && ((screenOpen && !screenMinimized) || screenModal.open);
  }}

  function scheduleNextScreen() {{
    if (screenTimer) {{ clearTimeout(screenTimer); screenTimer = null; }}
    const liveOn = screenModal.open ? modalAutoChk.checked : autoScreenChk.checked;
    if (screenVisible() && liveOn && !sessionExpired) {{
      const ms = Math.max(400, Number(screenInterval.value) || 1000);
      screenTimer = setTimeout(loadScreen, ms);
    }}
  }}

  async function loadScreen() {{
    if (sessionExpired) return;
    if (loadingScreen) {{
      pendingScreenReload = true;
      return;
    }}
    loadingScreen = true;
    pendingScreenReload = false;
    if (!screenImg.src) setScreenStatus('Capturing...');
    const fmt = screenFmtSelect.value || 'jpeg';
    const maxW = screenModal.open ? 1600 : 1280;
    const q = screenModal.open ? 82 : 76;
    try {{
      const urlPath = '/api/screen?max_w=' + maxW + '&monitor=' + encodeURIComponent(selectedMonitor)
        + '&fmt=' + encodeURIComponent(fmt) + '&q=' + q + '&t=' + Date.now();
      const res = await fetch(urlPath, {{ credentials: 'same-origin' }});
      if (res.status === 401) {{ handleUnauthorized(); return; }}
      if (!res.ok) {{
        let msg = 'Unavailable';
        try {{ msg = (await res.json()).error || msg; }} catch (_e) {{}}
        setScreenStatus(msg);
        screenImg.hidden = true;
        return;
      }}
      const blob = await res.blob();
      const prev = screenImg.dataset.url;
      const url = URL.createObjectURL(blob);
      screenImg.dataset.url = url;
      screenImg.src = url;
      screenImg.hidden = false;
      if (screenModal.open) modalImg.src = url;
      if (prev) URL.revokeObjectURL(prev);
      setScreenStatus(screenMinimized && !screenModal.open ? 'Hidden' : new Date().toLocaleTimeString());
    }} catch (_e) {{
      setScreenStatus('Capture failed');
    }} finally {{
      loadingScreen = false;
      if (pendingScreenReload && screenVisible()) {{
        pendingScreenReload = false;
        loadScreen();
      }} else {{
        pendingScreenReload = false;
        scheduleNextScreen();
      }}
    }}
  }}

  function syncScreenTimer() {{
    if (!screenModal.open) modalAutoChk.checked = autoScreenChk.checked;
    modalIntervalSelect.value = screenInterval.value;
    scheduleNextScreen();
  }}

  function syncScreenView() {{
    screenBody.hidden = screenMinimized;
    minScreenBtn.textContent = screenMinimized ? 'Show' : 'Hide';
    minScreenBtn.setAttribute('aria-expanded', String(!screenMinimized));
    setScreenStatus(screenMinimized && !screenModal.open ? 'Hidden' : (screenImg.src ? new Date().toLocaleTimeString() : 'Not loaded'));
    syncScreenTimer();
  }}

  async function openFullscreenScreen() {{
    if (prevAutoBeforeFs === null) prevAutoBeforeFs = autoScreenChk.checked;
    modalAutoChk.checked = true;
    autoScreenChk.checked = true;
    modalIntervalSelect.value = screenInterval.value;
    screenModal.classList.remove('hud-hidden');
    if (screenImg.src) modalImg.src = screenImg.src;
    if (!screenModal.open) screenModal.showModal();
    if (screenModal.requestFullscreen) {{
      try {{ await screenModal.requestFullscreen(); }} catch (_e) {{}}
    }}
    loadScreen();
    syncScreenTimer();
  }}

  function closeFullscreenScreen() {{
    if (document.fullscreenElement === screenModal && document.exitFullscreen) {{
      try {{ document.exitFullscreen(); }} catch (_e) {{}}
    }}
    if (screenModal.open) screenModal.close();
  }}

  function setMonitor(idx) {{
    selectedMonitor = Math.max(0, Number(idx) || 0);
    monitorSelect.value = String(selectedMonitor);
    modalMonitorSelect.value = String(selectedMonitor);
    loadScreen();
  }}

  function resetNewConfirm() {{
    clearTimeout(newConfirmTimer);
    newConfirmArmed = false;
    newBtn.textContent = 'New';
    newBtn.classList.remove('danger');
  }}

  async function applySettings(patch) {{
    const body = Object.assign({{ target: lastTarget || 'phone' }}, patch);
    const out = await apiPost('/api/settings', body);
    if (!out || !out.ok) {{
      showToast((out && (out.message || out.error)) || 'Update failed');
      lastSettingsKey = '';
      refreshState();
      return;
    }}
    showToast('Updated');
    refreshState();
  }}

  async function loadSessionsList() {{
    chatsList.textContent = 'Loading...';
    try {{
      const res = await fetch('/api/sessions', {{ credentials: 'same-origin' }});
      if (res.status === 401) {{ window.location.reload(); return; }}
      const data = await res.json();
      const sessions = (data && data.sessions) || [];
      chatsList.textContent = '';
      if (!sessions.length) {{
        const p = document.createElement('p');
        p.className = 'empty-note';
        p.textContent = 'No saved chats yet.';
        chatsList.appendChild(p);
        return;
      }}
      for (const sess of sessions) {{
        const row = document.createElement('div');
        row.className = 'session-row' + (sess.active ? ' active' : '');
        const pick = document.createElement('button');
        pick.type = 'button';
        pick.className = 'session-pick';
        const ttl = document.createElement('span');
        ttl.className = 'session-title';
        ttl.textContent = sess.title || 'Chat';
        const meta = document.createElement('span');
        meta.className = 'session-meta mono';
        meta.textContent = (sess.active ? 'Current · ' : '') + sess.turns + ' msgs · ' + sess.age;
        pick.append(ttl, meta);
        pick.addEventListener('click', async () => {{
          if (sess.active) {{ chatsModal.close(); return; }}
          pick.disabled = true;
          const out = await apiPost('/api/sessions/resume', {{ id: sess.id }});
          if (out && out.ok) {{
            chatsModal.close();
            showToast('Chat loaded');
            refreshState();
          }} else {{
            pick.disabled = false;
            showToast((out && (out.message || out.error)) || 'Could not load chat');
          }}
        }});
        const del = document.createElement('button');
        del.type = 'button';
        del.className = 'btn xs';
        del.textContent = 'Del';
        let armed = false;
        let armTimer = null;
        del.addEventListener('click', async () => {{
          if (!armed) {{
            armed = true;
            del.textContent = 'Sure?';
            del.classList.add('danger');
            armTimer = setTimeout(() => {{
              armed = false;
              del.textContent = 'Del';
              del.classList.remove('danger');
            }}, 3000);
            return;
          }}
          clearTimeout(armTimer);
          del.disabled = true;
          const out = await apiPost('/api/sessions/delete', {{ id: sess.id }});
          if (out && out.ok) {{
            showToast('Deleted');
            loadSessionsList();
            refreshState();
          }} else {{
            del.disabled = false;
            showToast((out && (out.message || out.error)) || 'Delete failed');
          }}
        }});
        row.append(pick, del);
        chatsList.appendChild(row);
      }}
    }} catch (_e) {{
      chatsList.textContent = 'Failed to load chats.';
    }}
  }}

  function setSettingsOpen(open) {{
    settingsOpen = !!open;
    settingsDrawer.hidden = !settingsOpen;
    settingsBtn.classList.toggle('active', settingsOpen);
    settingsBtn.setAttribute('aria-expanded', String(settingsOpen));
    if (settingsOpen && screenOpen) setScreenOpen(false);
  }}

  function setScreenOpen(open) {{
    screenOpen = !!open;
    screenDrawer.hidden = !screenOpen;
    screenBtn.classList.toggle('active', screenOpen);
    screenBtn.setAttribute('aria-expanded', String(screenOpen));
    if (screenOpen) {{
      if (settingsOpen) setSettingsOpen(false);
      if (!screenMinimized) loadScreen();
    }}
    syncScreenTimer();
  }}

  async function switchTarget(target) {{
    if (target === lastTarget || pendingTarget === target) return;
    pendingTarget = target;
    targetPhoneBtn.disabled = true;
    targetLaptopBtn.disabled = true;
    try {{
      const out = await apiPost('/api/target', {{ target }});
      if (!out || !out.ok) {{
        pendingTarget = null;
        showToast((out && (out.message || out.error)) || 'Switch failed');
        return;
      }}
      pendingTarget = null;
      if (out.state) {{
        renderState(out.state);
      }} else {{
        refreshState();
      }}
    }} catch (_e) {{
      pendingTarget = null;
      showToast('Switch failed');
    }} finally {{
      targetPhoneBtn.disabled = false;
      targetLaptopBtn.disabled = false;
    }}
  }}

  targetPhoneBtn.addEventListener('click', () => switchTarget('phone'));
  targetLaptopBtn.addEventListener('click', () => switchTarget('laptop'));

  settingsBtn.addEventListener('click', () => setSettingsOpen(!settingsOpen));
  modelSelect.addEventListener('change', () => applySettings({{ model: modelSelect.value }}));
  thinkingSelect.addEventListener('change', () => applySettings({{ thinking: thinkingSelect.value }}));
  screenThinkingSelect.addEventListener('change', () => applySettings({{ screen_thinking: screenThinkingSelect.value }}));
  autoModeChk.addEventListener('change', () => applySettings({{ auto: autoModeChk.checked }}));
  alertsChk.addEventListener('change', async () => {{
    alertsEnabled = alertsChk.checked;
    try {{ localStorage.setItem('pd_alerts', alertsEnabled ? '1' : '0'); }} catch (_e) {{}}
    if (alertsEnabled && typeof Notification !== 'undefined' && Notification.permission === 'default') {{
      try {{
        const perm = await Notification.requestPermission();
        if (perm === 'denied') showToast('Notifications blocked in browser settings');
      }} catch (_e) {{}}
    }}
  }});
  repairBtn.addEventListener('click', () => {{
    saveDraft();
    window.location.reload();
  }});
  refreshModelsBtn.addEventListener('click', async () => {{
    refreshModelsBtn.disabled = true;
    const out = await apiPost('/api/models/refresh', {{}});
    showToast((out && out.ok) ? 'Refreshing models...' : ((out && (out.message || out.error)) || 'Refresh failed'));
    setTimeout(() => {{ refreshModelsBtn.disabled = false; }}, 1200);
  }});
  logoutBtn.addEventListener('click', async () => {{
    await apiPost('/api/logout', {{}});
    window.location.reload();
  }});

  chatsBtn.addEventListener('click', () => {{
    setSettingsOpen(false);
    chatsModal.showModal();
    loadSessionsList();
  }});
  closeChatsBtn.addEventListener('click', () => chatsModal.close());

  screenBtn.addEventListener('click', () => setScreenOpen(!screenOpen));
  monitorSelect.addEventListener('change', () => setMonitor(monitorSelect.value));
  modalMonitorSelect.addEventListener('change', () => setMonitor(modalMonitorSelect.value));
  screenFmtSelect.addEventListener('change', loadScreen);
  screenInterval.addEventListener('change', () => {{
    modalIntervalSelect.value = screenInterval.value;
    syncScreenTimer();
  }});
  modalIntervalSelect.addEventListener('change', () => {{
    screenInterval.value = modalIntervalSelect.value;
    syncScreenTimer();
  }});
  refreshScreenBtn.addEventListener('click', () => {{
    if (screenMinimized) {{
      screenMinimized = false;
      syncScreenView();
    }}
    loadScreen();
  }});
  modalRefreshBtn.addEventListener('click', loadScreen);
  autoScreenChk.addEventListener('change', () => {{
    modalAutoChk.checked = autoScreenChk.checked;
    syncScreenTimer();
  }});
  modalAutoChk.addEventListener('change', () => {{
    autoScreenChk.checked = modalAutoChk.checked;
    syncScreenTimer();
  }});
  minScreenBtn.addEventListener('click', () => {{
    screenMinimized = !screenMinimized;
    syncScreenView();
    if (!screenMinimized && !screenImg.src) loadScreen();
  }});
  expandScreenBtn.addEventListener('click', openFullscreenScreen);
  screenImg.addEventListener('click', openFullscreenScreen);
  modalImg.addEventListener('click', () => screenModal.classList.toggle('hud-hidden'));
  closeModalBtn.addEventListener('click', closeFullscreenScreen);
  fsExitFab.addEventListener('click', closeFullscreenScreen);
  screenModal.addEventListener('close', () => {{
    if (document.fullscreenElement === screenModal && document.exitFullscreen) {{
      try {{ document.exitFullscreen(); }} catch (_e) {{}}
    }}
    if (prevAutoBeforeFs !== null) {{
      autoScreenChk.checked = prevAutoBeforeFs;
      modalAutoChk.checked = prevAutoBeforeFs;
      prevAutoBeforeFs = null;
    }}
    screenModal.classList.remove('hud-hidden');
    syncScreenTimer();
  }});
  for (const dlg of [screenModal, chatsModal]) {{
    if (!('closedBy' in HTMLDialogElement.prototype)) {{
      dlg.addEventListener('click', (event) => {{
        if (event.target !== dlg) return;
        const rect = dlg.getBoundingClientRect();
        if (rect.top <= event.clientY && event.clientY <= rect.top + rect.height
            && rect.left <= event.clientX && event.clientX <= rect.left + rect.width) return;
        dlg.close();
      }});
    }}
  }}

  jumpBtn.addEventListener('click', () => chatPane.scrollTo({{ top: chatPane.scrollHeight, behavior: 'smooth' }}));
  chatPane.addEventListener('scroll', syncJumpButton, {{ passive: true }});
  chatPane.addEventListener('touchstart', () => {{ userTouching = true; }}, {{ passive: true }});
  chatPane.addEventListener('pointerdown', () => {{ userTouching = true; }}, {{ passive: true }});
  for (const ev of ['touchend', 'touchcancel', 'pointerup', 'pointercancel']) {{
    chatPane.addEventListener(ev, () => {{ userTouching = false; }}, {{ passive: true }});
  }}

  stopBtn.addEventListener('click', async () => {{
    const out = await apiPost('/api/stop', {{}});
    showToast((out && out.ok) ? 'Stopped' : ((out && (out.message || out.error)) || 'Stop failed'));
  }});
  newBtn.addEventListener('click', async () => {{
    if (hasHistory && !newConfirmArmed) {{
      newConfirmArmed = true;
      newBtn.textContent = 'Sure?';
      newBtn.classList.add('danger');
      clearTimeout(newConfirmTimer);
      newConfirmTimer = setTimeout(resetNewConfirm, 3000);
      return;
    }}
    resetNewConfirm();
    const out = await apiPost('/api/new', {{}});
    if (out && !out.ok) {{
      showToast(out.message || out.error || 'Cannot start new chat');
    }}
  }});

  attachBtn.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {{
    if (fileInput.files && fileInput.files.length) addFiles(fileInput.files);
    fileInput.value = '';
  }});

  draft.addEventListener('paste', (e) => {{
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    const files = [];
    for (const it of items) {{
      if (it.kind === 'file') {{
        const f = it.getAsFile();
        if (f) files.push(f);
      }}
    }}
    if (files.length) {{
      e.preventDefault();
      addFiles(files);
    }}
  }});

  let lastDraftLen = 0;
  let lastDraftH = 0;
  function fitDraft() {{
    const len = draft.value.length;
    if (!lastDraftH) lastDraftH = draft.offsetHeight || 44;
    if (len >= lastDraftLen && draft.scrollHeight <= draft.clientHeight + 2) {{
      lastDraftLen = len;
      return;
    }}
    lastDraftLen = len;
    const savedTop = chatPane.scrollTop;
    draft.style.height = 'auto';
    const target = Math.max(44, Math.min(136, draft.scrollHeight));
    draft.style.height = target + 'px';
    if (target !== lastDraftH) {{
      lastDraftH = target;
      chatPane.scrollTop = savedTop;
    }}
  }}
  draft.addEventListener('input', () => {{
    fitDraft();
    saveDraft();
  }});
  try {{
    const savedDraft = sessionStorage.getItem('pd_draft');
    if (savedDraft && !draft.value) {{
      draft.value = savedDraft;
      fitDraft();
    }}
  }} catch (_e) {{}}
  draft.addEventListener('keydown', (e) => {{
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {{
      e.preventDefault();
      if (e.isComposing || e.keyCode === 229) return;
      sendForm.requestSubmit();
    }}
  }});

  function syncQueueBar() {{
    queueBar.hidden = pendingQueue.length === 0;
    if (pendingQueue.length > 0) {{
      const n = pendingQueue.length;
      queueLabel.textContent = n + (n === 1 ? ' message queued' : ' messages queued')
        + (navigator.onLine === false ? ' (offline)' : ' (waiting to retry)');
    }}
  }}

  async function postMessageItem(item) {{
    if (item.files && item.files.length) {{
      const encoded = await Promise.all(item.files.map(async (f) => ({{
        name: f.name,
        mime: f.mime,
        data_b64: await readFileBase64(f.file),
      }})));
      return await apiPost('/api/upload', {{ text: item.text, attachments: encoded }});
    }}
    return await apiPost('/api/send', {{ text: item.text }});
  }}

  async function flushQueue() {{
    if (flushingQueue || !pendingQueue.length || sessionExpired || navigator.onLine === false) return;
    flushingQueue = true;
    queueRetryBtn.disabled = true;
    try {{
      while (pendingQueue.length && !sessionExpired) {{
        const item = pendingQueue[0];
        let out = null;
        try {{
          out = await postMessageItem(item);
        }} catch (_e) {{
          break;
        }}
        if (out && out.ok) {{
          pendingQueue.shift();
          for (const f of (item.files || [])) if (f.thumbUrl) URL.revokeObjectURL(f.thumbUrl);
          syncQueueBar();
          showToast(out.message === 'steered' ? 'Queued steering sent' : 'Queued message sent');
          await refreshState();
          chatPane.scrollTop = chatPane.scrollHeight;
          syncJumpButton();
        }} else if (out && out.status && out.status >= 400 && out.status < 500) {{
          pendingQueue.shift();
          if (out.status !== 401 && !draft.value && item.text) {{
            draft.value = item.text;
            fitDraft();
            saveDraft();
          }}
          if (item.files && item.files.length && staged.length === 0) {{
            staged = item.files;
            renderChips();
          }}
          syncQueueBar();
          showToast(out.message || out.error || 'Queued send rejected');
          break;
        }} else {{
          break;
        }}
      }}
    }} finally {{
      flushingQueue = false;
      queueRetryBtn.disabled = false;
      syncQueueBar();
    }}
  }}

  queueRetryBtn.addEventListener('click', () => flushQueue());
  queueCancelBtn.addEventListener('click', () => {{
    if (!pendingQueue.length) return;
    const item = pendingQueue.shift();
    if (item && item.text) {{
      draft.value = draft.value ? (draft.value + '\\n' + item.text) : item.text;
      fitDraft();
      saveDraft();
    }}
    if (item && item.files && item.files.length) {{
      for (const f of item.files) {{
        if (staged.length < MAX_FILES) staged.push(f);
        else if (f.thumbUrl) URL.revokeObjectURL(f.thumbUrl);
      }}
      renderChips();
    }}
    syncQueueBar();
  }});

  sendForm.addEventListener('submit', async (e) => {{
    e.preventDefault();
    if (sending) return;
    const text = draft.value.trim();
    const files = staged.slice();
    if (!text && files.length === 0) return;
    if (navigator.onLine === false) {{
      draft.value = '';
      fitDraft();
      saveDraft();
      clearStaged({{ revoke: false }});
      pendingQueue.push({{ text, files }});
      syncQueueBar();
      showToast('Offline: queued to send on reconnect');
      return;
    }}
    sending = true;
    sendBtn.disabled = true;
    sendBtn.textContent = files.length ? 'Uploading...' : '...';
    draft.value = '';
    fitDraft();
    saveDraft();
    clearStaged({{ revoke: false }});
    let out = null;
    let netErr = false;
    try {{
      out = await postMessageItem({{ text, files }});
    }} catch (_e) {{
      netErr = true;
      out = {{ ok: false, error: 'Network error' }};
    }} finally {{
      sending = false;
      sendBtn.disabled = false;
      sendBtn.textContent = isBusy ? 'Steer' : 'Send';
    }}
    if (out && out.ok) {{
      for (const f of files) if (f.thumbUrl) URL.revokeObjectURL(f.thumbUrl);
      showToast(out.message === 'steered' ? 'Steering sent' : 'Sent');
      await refreshState();
      chatPane.scrollTop = chatPane.scrollHeight;
      syncJumpButton();
      return;
    }}
    if ((netErr || (out && out.status >= 500)) && !sessionExpired) {{
      pendingQueue.push({{ text, files }});
      syncQueueBar();
      showToast('Connection lost: queued for retry');
      return;
    }}
    if (!draft.value && text) {{
      draft.value = text;
      fitDraft();
      saveDraft();
    }}
    if (files.length && staged.length === 0) {{
      staged = files;
      renderChips();
    }}
    showToast((out && (out.message || out.error)) || 'Send failed');
    refreshState();
  }});

  let es = null;
  let sseTimer = null;
  let retryMs = 1500;
  let probingSession = false;

  async function probeSession() {{
    if (probingSession || sessionExpired) return;
    probingSession = true;
    try {{
      const res = await fetch('/api/state', {{ credentials: 'same-origin' }});
      if (res.status === 401) {{
        handleUnauthorized();
        return;
      }}
      if (res.ok) {{
        sessionExpired = false;
        repairBanner.hidden = true;
        renderState(await res.json());
      }}
    }} catch (_e) {{}} finally {{
      probingSession = false;
    }}
  }}

  function scheduleReconnect() {{
    if (sessionExpired) return;
    clearTimeout(sseTimer);
    sseTimer = setTimeout(connectEvents, retryMs);
    retryMs = Math.min(6000, Math.round(retryMs * 1.5));
  }}

  function connectEvents() {{
    if (sessionExpired) return;
    clearTimeout(sseTimer);
    if (es) {{
      try {{ es.close(); }} catch (_e) {{}}
    }}
    es = new EventSource('/api/events', {{ withCredentials: true }});
    es.addEventListener('state', (ev) => {{
      try {{
        retryMs = 1500;
        sessionExpired = false;
        repairBanner.hidden = true;
        if (disconnectTimer) {{ clearTimeout(disconnectTimer); disconnectTimer = null; }}
        renderState(JSON.parse(ev.data));
      }} catch (_e) {{}}
    }});
    es.onopen = () => {{
      retryMs = 1500;
      sessionExpired = false;
      repairBanner.hidden = true;
      if (disconnectTimer) {{ clearTimeout(disconnectTimer); disconnectTimer = null; }}
      flushQueue();
    }};
    es.onerror = () => {{
      if (!disconnectTimer && !sessionExpired) {{
        disconnectTimer = setTimeout(() => {{
          disconnectTimer = null;
          if (sessionExpired) return;
          lastHeaderKey = '';
          dot.className = 'pip off';
          modelText.textContent = 'reconnecting...';
          probeSession();
        }}, 1800);
      }}
      if (es && es.readyState === EventSource.CLOSED) {{
        es.close();
        es = null;
        probeSession();
        scheduleReconnect();
      }}
    }};
  }}

  function syncViewportHeight() {{
    const vv = window.visualViewport;
    if (!vv) return;
    const atBottom = isNearBottom();
    document.documentElement.style.setProperty('--app-h', Math.round(vv.height) + 'px');
    if (window.scrollY) window.scrollTo(0, 0);
    if (atBottom && !userTouching) {{
      chatPane.scrollTop = chatPane.scrollHeight;
    }}
    syncJumpButton();
  }}
  if (window.visualViewport) {{
    window.visualViewport.addEventListener('resize', syncViewportHeight, {{ passive: true }});
    window.visualViewport.addEventListener('scroll', syncViewportHeight, {{ passive: true }});
    syncViewportHeight();
  }}

  function recoverConnection() {{
    if (unreadDone) {{
      unreadDone = false;
      syncTitle(isBusy, prevApprovalsCount);
    }}
    lastHeaderKey = '';
    refreshState();
    if (!sessionExpired && (!es || es.readyState === EventSource.CLOSED)) connectEvents();
    flushQueue();
    if (screenVisible() && (screenModal.open ? modalAutoChk.checked : autoScreenChk.checked)) {{
      loadScreen();
    }}
  }}
  document.addEventListener('visibilitychange', () => {{
    if (document.visibilityState === 'visible') recoverConnection();
    else if (screenTimer) {{ clearTimeout(screenTimer); screenTimer = null; }}
  }});
  window.addEventListener('focus', () => {{
    if (unreadDone) {{
      unreadDone = false;
      syncTitle(isBusy, prevApprovalsCount);
    }}
  }});
  window.addEventListener('pageshow', recoverConnection);
  window.addEventListener('online', recoverConnection);

  refreshState();
  connectEvents();
}})();
</script>
</body>
</html>"""


def _login_html(nonce: str, error: str = "") -> str:
    err_html = '<p class="err" id="errMsg"></p>' if error else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Paperduck">
{_INSTALL_LINKS}
<title>Pair Paperduck</title>
<style nonce="{nonce}">
:root {{
  color-scheme: light dark;
  --bg: #f4f0e7; --paper: #faf8f3; --raised: #f0ece3; --ink: #1d1c22; --dim: #77736b;
  --line: #ded7c9; --accent: #a86f00; --on-accent: #ffffff; --danger: #c9472c;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #151419; --paper: #1c1b20; --raised: #26252c; --ink: #efece5; --dim: #9b978e;
    --line: #36343d; --accent: #f2be3a; --on-accent: #1d1c22; --danger: #ee7a5c;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  min-block-size: 100dvh;
  display: grid;
  place-items: center;
  padding: 1rem;
  background: var(--bg);
  color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}}
.card {{
  width: min(380px, 100%);
  padding: 1.25rem;
  border-radius: 6px;
  clip-path: polygon(0 0, calc(100% - 12px) 0, 100% 12px, 100% 100%, 0 100%);
  background: var(--paper);
  border: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  gap: 0.78rem;
}}
.head {{ display: flex; align-items: center; gap: 0.5rem; }}
.head h1 {{ margin: 0; font-size: 1.05rem; }}
.sub {{ margin: 0; color: var(--dim); font-size: 0.86rem; line-height: 1.45; }}
.duck {{ width: 24px; height: 24px; flex: none; }}
.visually-hidden {{ position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }}
input, button {{ min-block-size: 44px; border-radius: 6px; font: inherit; padding: 0 0.8rem; }}
input {{ border: 1px solid var(--line); background: var(--raised); color: var(--ink); }}
input:focus {{ outline: 2px solid var(--accent); outline-offset: -1px; }}
button {{ border: none; background: var(--accent); color: var(--on-accent); font-weight: 700; cursor: pointer; }}
.err {{ color: var(--danger); margin: 0; font-size: 0.86rem; font-weight: 600; }}
.hint {{ margin: 0; padding: 0.55rem 0.7rem; border-radius: 6px; background: var(--raised); font-size: 0.84rem; line-height: 1.45; }}
</style>
</head>
<body>
<form class="card" method="POST" action="/pair">
  <div class="head">
    <svg class="duck" viewBox="0 0 24 24" aria-hidden="true">
      <polygon points="3,14 9,6 15,8 19,6 22,9 18,11 19,17 8,19" fill="#F2BE3A" stroke="#1D1C22" stroke-width="1.3" stroke-linejoin="round"/>
      <polygon points="18,9 23,10 19,11.5" fill="#EE7A5C"/>
      <circle cx="15.5" cy="9" r="1" fill="#1D1C22"/>
    </svg>
    <h1>Paperduck Remote</h1>
  </div>
  <p class="sub">Scan the QR code in Paperduck &gt; Phone Control, or paste your pairing token below.</p>
  <p class="hint" id="homeHint" hidden>This Home Screen app keeps its own sign-in, separate from the browser. Scanning the QR code opens the browser instead, so copy the pairing token from Paperduck &gt; Phone Control and paste it here.</p>
  {err_html}
  <label for="tokenInput" class="visually-hidden">Pairing token</label>
  <input id="tokenInput" name="t" type="password" placeholder="Pairing token" autocomplete="current-password" required>
  <button type="submit">Pair This Device</button>
</form>
<script nonce="{nonce}">
const e = document.getElementById('errMsg');
if (e) e.textContent = {json.dumps(error)};
if (navigator.standalone || window.matchMedia('(display-mode: standalone)').matches) {{
  document.getElementById('homeHint').hidden = false;
}}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Server
# ---------------------------------------------------------------------------

class _BridgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], bridge: PhoneBridge) -> None:
        self.bridge = bridge
        super().__init__(server_address, _BridgeHandler)


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("remote: " + format, *args)

    def _bridge(self) -> PhoneBridge:
        return self.server.bridge  # type: ignore[attr-defined]

    def _is_https(self) -> bool:
        proto = (self.headers.get("X-Forwarded-Proto") or "").lower()
        if proto == "https":
            return True
        bridge = self._bridge()
        if bridge.mode == "tailscale_serve" and bridge.public_url:
            host = (self.headers.get("Host") or "").split(":", 1)[0].lower()
            pub_host = (urlsplit(bridge.public_url).hostname or "").lower()
            if host and pub_host and host == pub_host:
                return True
        return False

    def _send_security_headers(self, nonce: str = "", *, html: bool = False,
                               cache_control: str = "no-store") -> None:
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(), focus-without-user-activation=()")
        self.send_header("Vary", "Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site")
        self.send_header("Cache-Control", cache_control)
        if html:
            self.send_header(
                "Content-Security-Policy",
                f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "img-src 'self' data: blob:; connect-src 'self'; manifest-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
            )

    def _drain_small_body(self, cap: int = 4 * MAX_BODY_BYTES) -> None:
        """Read bounded POST body before closing an error response so Windows does not emit TCP RST."""
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            return
        if 0 < length <= cap:
            try:
                self.rfile.read(length)
            except OSError:
                pass

    def _send_bytes(self, status: int, content_type: str, body: bytes, *, html: bool = False, nonce: str = "",
                    extra_headers: list[tuple[str, str]] | None = None) -> None:
        if status >= 400:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if status >= 400:
            self.send_header("Connection", "close")
        self._send_security_headers(nonce, html=html)
        for k, v in extra_headers or ():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, status: int, data: dict[str, Any]) -> None:
        self._send_bytes(status, "application/json; charset=utf-8", json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _reject_cross_site(self, *, is_post: bool = False, allow_navigate: bool = False) -> bool:
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site == "cross-site":
            if allow_navigate and not is_post:
                mode = (self.headers.get("Sec-Fetch-Mode") or "").lower()
                dest = (self.headers.get("Sec-Fetch-Dest") or "").lower()
                if mode == "navigate" and dest in ("document", ""):
                    return False
            if is_post:
                self._drain_small_body()
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "cross-site request rejected"})
            return True
        if is_post:
            origin = self.headers.get("Origin")
            host = self.headers.get("Host") or ""
            if origin and not self._bridge()._origin_allowed(origin, host):
                self._drain_small_body()
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "origin mismatch"})
                return True
        return False

    def _cookie_session(self) -> str | None:
        raw = self.headers.get("Cookie") or ""
        if not raw:
            return None
        jar: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE_NAME)
        return morsel.value if morsel is not None else None

    def _auth_csrf(self, *, require_csrf: bool = False) -> tuple[bool, str]:
        bridge = self._bridge()
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):
            if bridge._check_token(auth[7:].strip(), self.client_address[0]):
                return True, ""
            return False, ""
        sid = self._cookie_session()
        if sid is None:
            return False, ""
        csrf = bridge._session_csrf(sid)
        if csrf is None:
            return False, ""
        if require_csrf:
            sent = self.headers.get("X-CSRF-Token") or ""
            if not hmac.compare_digest(sent, csrf):
                return False, ""
        return True, csrf

    def _handle_pair(self, token: str, *, as_json: bool = False) -> None:
        bridge = self._bridge()
        ip = self.client_address[0]
        if bridge._is_rate_limited(ip):
            if as_json:
                self._send_json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "too many attempts; wait a minute"})
                return
            nonce = secrets.token_urlsafe(16)
            self._send_bytes(HTTPStatus.TOO_MANY_REQUESTS, "text/html; charset=utf-8",
                             _login_html(nonce, "Too many attempts; wait a minute.").encode("utf-8"),
                             html=True, nonce=nonce)
            return
        if not bridge._check_token(token, ip):
            if as_json:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid pairing token"})
                return
            nonce = secrets.token_urlsafe(16)
            self._send_bytes(HTTPStatus.UNAUTHORIZED, "text/html; charset=utf-8",
                             _login_html(nonce, "Invalid pairing token.").encode("utf-8"),
                             html=True, nonce=nonce)
            return
        sid = bridge._create_session()
        cookie = f"{COOKIE_NAME}={sid}; HttpOnly; SameSite=Lax; Path=/; Max-Age={COOKIE_MAX_AGE_S}"
        if self._is_https():
            cookie += "; Secure"
        if as_json:
            self._send_bytes(
                HTTPStatus.OK,
                "application/json; charset=utf-8",
                b'{"ok": true}',
                extra_headers=[("Set-Cookie", cookie)],
            )
            return
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self._send_security_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        bridge = self._bridge()

        if path == "/pair":
            if self._reject_cross_site(allow_navigate=True):
                return
            token = (parse_qs(parsed.query).get("t") or [""])[0]
            self._handle_pair(token, as_json=False)
            return

        if path == "/":
            ok, csrf = self._auth_csrf()
            nonce = secrets.token_urlsafe(16)
            if not ok:
                self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8",
                                 _login_html(nonce).encode("utf-8"), html=True, nonce=nonce)
                return
            if not csrf:
                csrf = bridge._session_csrf(self._cookie_session() or "") or ""
            self._send_bytes(HTTPStatus.OK, "text/html; charset=utf-8",
                             _app_html(nonce, csrf).encode("utf-8"), html=True, nonce=nonce)
            return

        if self._reject_cross_site():
            return
        # Install assets hold nothing private and iOS may fetch them without the session cookie
        if path == "/manifest.webmanifest":
            self._send_bytes(HTTPStatus.OK, "application/manifest+json", _MANIFEST)
            return
        if path == "/apple-touch-icon.png" and bridge.icon_png:
            self._send_bytes(HTTPStatus.OK, "image/png", bridge.icon_png)
            return
        ok, _csrf = self._auth_csrf()
        if not ok:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        if path == "/api/state":
            _ver, raw = bridge.get_state_json()
            self._send_bytes(HTTPStatus.OK, "application/json; charset=utf-8", raw)
            return

        if path in ("/api/screen.png", "/api/screen"):
            qs = parse_qs(parsed.query)
            try:
                max_w = max(320, min(1920, int((qs.get("max_w") or ["1280"])[0])))
            except ValueError:
                max_w = 1280
            try:
                monitor = max(0, min(15, int((qs.get("monitor") or ["0"])[0])))
            except ValueError:
                monitor = 0
            if path == "/api/screen.png":
                fmt, quality = "png", 80
            else:
                raw_fmt = (qs.get("fmt") or ["jpeg"])[0].lower()
                fmt = "jpeg" if raw_fmt in ("jpg", "jpeg") else ("png" if raw_fmt == "png" else "jpeg")
                try:
                    quality = max(40, min(95, int((qs.get("q") or ["78"])[0])))
                except ValueError:
                    quality = 78
            if bridge.handlers.grab_screen_opts is not None:
                img_bytes, mime, err = bridge.handlers.grab_screen_opts(max_w, monitor, fmt, quality)
            else:
                img_bytes, err = bridge.handlers.grab_screen(max_w)
                mime = "image/png"
            if not img_bytes:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": err or "screen unavailable"})
                return
            self._send_bytes(HTTPStatus.OK, mime or "image/png", img_bytes)
            return

        if path == "/api/sessions":
            sessions = bridge.handlers.list_sessions() if bridge.handlers.list_sessions is not None else []
            self._send_json(HTTPStatus.OK, {"sessions": sessions})
            return

        if path == "/api/events":
            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("X-Accel-Buffering", "no")
            self._send_security_headers(cache_control="no-cache, no-store, no-transform")
            self.end_headers()
            seen_ver = -1
            last_push = 0.0
            try:
                self.wfile.write(b": connected\n\n")
                self.wfile.flush()
                while not bridge._stop_event.is_set():
                    ver, changed = bridge.wait_for_version(seen_ver, timeout_s=15.0)
                    if bridge._stop_event.is_set():
                        break
                    if changed or seen_ver < 0:
                        if seen_ver >= 0:
                            elapsed = time.monotonic() - last_push
                            if elapsed < SSE_MIN_INTERVAL_S:
                                bridge._stop_event.wait(SSE_MIN_INTERVAL_S - elapsed)
                                if bridge._stop_event.is_set():
                                    break
                        ver, raw = bridge.get_state_json()
                        seen_ver = ver
                        last_push = time.monotonic()
                        self.wfile.write(b"event: state\ndata: " + raw + b"\n\n")
                    else:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (OSError, BrokenPipeError, ConnectionResetError):
                pass
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        bridge = self._bridge()

        if self._reject_cross_site(is_post=True):
            return

        if path == "/pair":
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad content-length"})
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._drain_small_body()
                self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
                return
            raw = self.rfile.read(length) if length else b""
            ctype = (self.headers.get("Content-Type") or "").lower()
            is_json = "application/json" in ctype
            token = ""
            if is_json:
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                    if isinstance(payload, dict) and isinstance(payload.get("t"), str):
                        token = payload["t"]
                except (ValueError, UnicodeDecodeError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
                    return
            else:
                form = parse_qs(raw.decode("utf-8", "replace"))
                token = (form.get("t") or [""])[0]
            self._handle_pair(token, as_json=is_json)
            return

        ok, _csrf = self._auth_csrf(require_csrf=True)
        if not ok:
            self._drain_small_body()
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return

        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad content-length"})
            return
        max_bytes = MAX_UPLOAD_BODY_BYTES if path == "/api/upload" else MAX_BODY_BYTES
        if length < 0 or length > max_bytes:
            if 0 < length <= 4 * MAX_BODY_BYTES:
                self.rfile.read(length)
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "body too large"})
            return
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid json"})
            return
        if not isinstance(data, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "expected object"})
            return

        if path == "/api/send":
            text = data.get("text")
            if not isinstance(text, str) or not text.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "empty message"})
                return
            accepted, msg = bridge.handlers.send_message(text.strip()[:MAX_PROMPT_CHARS])
            self._send_json(HTTPStatus.OK if accepted else HTTPStatus.CONFLICT, {"ok": accepted, "message": msg})
            return

        if path == "/api/upload":
            text = data.get("text", "")
            raw_atts = data.get("attachments", [])
            if not isinstance(text, str) or not isinstance(raw_atts, list):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid upload payload"})
                return
            if len(raw_atts) > MAX_REMOTE_ATTACHMENTS:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"too many attachments (max {MAX_REMOTE_ATTACHMENTS})"})
                return
            attachments: list[RemoteAttachment] = []
            for entry in raw_atts:
                if not isinstance(entry, dict):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid attachment item"})
                    return
                name = entry.get("name", "")
                mime = entry.get("mime", "")
                b64 = entry.get("data_b64", "")
                if not isinstance(name, str) or not isinstance(mime, str) or not isinstance(b64, str):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid attachment fields"})
                    return
                if b64.startswith("data:") and ";base64," in b64[:128]:
                    b64 = b64.split(",", 1)[1]
                b64_clean = "".join(b64.split())
                if len(b64_clean) > (MAX_REMOTE_FILE_BYTES * 4 // 3) + 16:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"{sanitize_attachment_name(name)} exceeds 4 MB"})
                    return
                try:
                    raw_file = base64.b64decode(b64_clean, validate=True)
                except (binascii.Error, ValueError):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": f"invalid base64 for {sanitize_attachment_name(name)}"})
                    return
                if len(raw_file) > MAX_REMOTE_FILE_BYTES:
                    self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": f"{sanitize_attachment_name(name)} exceeds 4 MB"})
                    return
                attachments.append(RemoteAttachment(name=sanitize_attachment_name(name), mime=mime[:120], data=raw_file))
            clean_text = text.strip()[:MAX_PROMPT_CHARS]
            if not clean_text and not attachments:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "empty message"})
                return
            if bridge.handlers.send_attachments is None:
                self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "attachments unsupported"})
                return
            accepted, msg = bridge.handlers.send_attachments(clean_text, attachments)
            self._send_json(HTTPStatus.OK if accepted else HTTPStatus.CONFLICT, {"ok": accepted, "message": msg})
            return

        if path == "/api/stop":
            stopped = bridge.handlers.stop_chat()
            self._send_json(HTTPStatus.OK, {"ok": stopped})
            return

        if path == "/api/new":
            started, msg = bridge.handlers.new_chat()
            self._send_json(HTTPStatus.OK if started else HTTPStatus.CONFLICT, {"ok": started, "message": msg})
            return

        if path == "/api/approve":
            aid = data.get("id")
            choice = data.get("choice")
            if not isinstance(aid, str) or choice not in ("once", "chat", None):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid approval payload"})
                return
            resolved = bridge.handlers.resolve_approval(aid, choice)
            self._send_json(HTTPStatus.OK if resolved else HTTPStatus.NOT_FOUND, {"ok": resolved})
            return

        if path == "/api/settings":
            if bridge.handlers.set_settings is None:
                self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "settings unsupported"})
                return
            clean_payload: dict[str, Any] = {}
            if "model" in data:
                if not isinstance(data["model"], str):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid model"})
                    return
                clean_payload["model"] = data["model"]
            if "thinking" in data:
                if not isinstance(data["thinking"], str):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid thinking"})
                    return
                clean_payload["thinking"] = data["thinking"]
            if "screen_thinking" in data:
                if not isinstance(data["screen_thinking"], str):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid screen thinking"})
                    return
                clean_payload["screen_thinking"] = data["screen_thinking"]
            if "auto" in data:
                if not isinstance(data["auto"], bool):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid auto"})
                    return
                clean_payload["auto"] = data["auto"]
            if not clean_payload:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "no settings supplied"})
                return
            if "target" in data:
                if data["target"] not in ("phone", "laptop"):
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid target"})
                    return
                clean_payload["target"] = data["target"]
            ok, msg = bridge.handlers.set_settings(clean_payload)
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.BAD_REQUEST, {"ok": ok, "message": msg})
            return

        if path == "/api/target":
            if bridge.handlers.set_target is None:
                self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "target switching unsupported"})
                return
            target = data.get("target")
            if target not in ("phone", "laptop"):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid target"})
                return
            ok, msg = bridge.handlers.set_target(target)
            if ok:
                _ver, raw = bridge.get_state_json()
                self._send_json(HTTPStatus.OK, {"ok": True, "message": msg, "state": json.loads(raw.decode("utf-8"))})
            else:
                self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": msg})
            return

        if path == "/api/models/refresh":
            if bridge.handlers.refresh_models is None:
                self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "model refresh unsupported"})
                return
            ok, msg = bridge.handlers.refresh_models()
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg})
            return

        if path == "/api/sessions/resume":
            if bridge.handlers.resume_session is None:
                self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "sessions unsupported"})
                return
            sid = data.get("id")
            if not isinstance(sid, str) or not _SID_RE.match(sid):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid session id"})
                return
            ok, msg = bridge.handlers.resume_session(sid)
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg})
            return

        if path == "/api/sessions/delete":
            if bridge.handlers.delete_session is None:
                self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "sessions unsupported"})
                return
            sid = data.get("id")
            if not isinstance(sid, str) or not _SID_RE.match(sid):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid session id"})
                return
            ok, msg = bridge.handlers.delete_session(sid)
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.CONFLICT, {"ok": ok, "message": msg})
            return

        if path == "/api/procs/stop":
            if bridge.handlers.stop_proc is None:
                self._send_json(HTTPStatus.NOT_IMPLEMENTED, {"error": "process control unsupported"})
                return
            name = data.get("name")
            if not isinstance(name, str) or not name.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid process name"})
                return
            ok, msg = bridge.handlers.stop_proc(name.strip())
            self._send_json(HTTPStatus.OK if ok else HTTPStatus.NOT_FOUND, {"ok": ok, "message": msg})
            return

        if path == "/api/logout":
            sid = self._cookie_session()
            if sid:
                bridge._drop_session(sid)
            self._send_bytes(
                HTTPStatus.OK,
                "application/json; charset=utf-8",
                b'{"ok": true}',
                extra_headers=[
                    ("Set-Cookie", f"{COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"),
                    ("Clear-Site-Data", '"cookies"'),
                ],
            )
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})


class PhoneBridge:
    """Lifecycle and auth manager for the local + Tailscale HTTP bridge."""

    def __init__(self, handlers: BridgeHandlers, *, port: int = DEFAULT_PORT,
                 token: str | None = None, manage_tailscale: bool = True, icon_png: bytes = b"") -> None:
        self.handlers = handlers
        self.icon_png = icon_png
        self.preferred_port = port
        self._explicit_token = token
        self._token = token or ""
        self._manage_tailscale = manage_tailscale
        self._server: _BridgeHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._ver_cond = threading.Condition()
        self.version = 0
        self._snap_lock = threading.Lock()
        self._snap_cache: tuple[int, bytes] | None = None
        self._revoked: deque[str] = deque(maxlen=MAX_SESSIONS * 4)
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self.bound_host = ""
        self.bound_port = 0
        self.public_url = ""
        self.mode = ""  # "tailscale_serve" | "tailscale_ip" | "loopback"
        self.last_error = ""
        self.status_note = ""
        self._serve_owned = False

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def token(self) -> str:
        return self._token

    @property
    def pair_url(self) -> str:
        if not self.public_url or not self._token:
            return ""
        return f"{self.public_url}/pair?t={self._token}"

    def notify_change(self) -> None:
        with self._ver_cond:
            self.version += 1
            with self._snap_lock:
                self._snap_cache = None
            self._ver_cond.notify_all()

    def get_state_json(self) -> tuple[int, bytes]:
        """Return `(version, utf8_json_bytes)`, caching once per version across SSE clients and /api/state."""
        with self._ver_cond:
            ver = self.version
        with self._snap_lock:
            if self._snap_cache is not None and self._snap_cache[0] == ver:
                return self._snap_cache
        state = self.handlers.get_state()
        state["version"] = ver
        raw = json.dumps(state, ensure_ascii=False).encode("utf-8")
        with self._ver_cond:
            cur_ver = self.version
        if cur_ver == ver:
            with self._snap_lock:
                self._snap_cache = (ver, raw)
        return ver, raw

    def wait_for_version(self, seen: int, timeout_s: float = 15.0) -> tuple[int, bool]:
        with self._ver_cond:
            if self.version != seen or self._stop_event.is_set():
                return self.version, True
            self._ver_cond.wait(timeout=timeout_s)
            return self.version, self.version != seen

    def _is_rate_limited(self, ip: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._fails.get(ip)
            if not q:
                return False
            while q and now - q[0] > RATE_WINDOW_S:
                q.popleft()
            return len(q) >= RATE_MAX_FAILS

    def _origin_allowed(self, origin: str, host_header: str) -> bool:
        netloc = urlsplit(origin).netloc.lower()
        if not netloc:
            return False
        allowed = set()
        if host_header:
            allowed.add(host_header.lower())
        if self.public_url:
            allowed.add(urlsplit(self.public_url).netloc.lower())
        if self.bound_host and self.bound_port:
            allowed.add(f"{self.bound_host}:{self.bound_port}".lower())
        return netloc in allowed

    def _check_token(self, presented: str, ip: str) -> bool:
        if self._is_rate_limited(ip):
            return False
        if presented and self._token and hmac.compare_digest(presented, self._token):
            return True
        now = time.monotonic()
        with self._lock:
            q = self._fails.setdefault(ip, deque())
            while q and now - q[0] > RATE_WINDOW_S:
                q.popleft()
            q.append(now)
        return False

    def _create_session(self) -> str:
        nonce = secrets.token_urlsafe(16)
        key = self._token.encode("utf-8")
        sig = hmac.new(key, b"sid:" + nonce.encode("utf-8"), "sha256").hexdigest()[:24]
        return f"{nonce}.{sig}"

    def _session_csrf(self, sid: str) -> str | None:
        if not sid or not self._token:
            return None
        with self._lock:
            if sid in self._revoked:
                return None
        nonce, sep, sig = sid.partition(".")
        if not sep or not nonce:
            return None
        key = self._token.encode("utf-8")
        expected = hmac.new(key, b"sid:" + nonce.encode("utf-8"), "sha256").hexdigest()[:24]
        if not hmac.compare_digest(sig, expected):
            return None
        return hmac.new(key, b"csrf:" + sid.encode("utf-8"), "sha256").hexdigest()[:24]

    def _drop_session(self, sid: str) -> None:
        if not sid:
            return
        with self._lock:
            if sid not in self._revoked:
                self._revoked.append(sid)

    def rotate_token(self) -> str:
        self._token = generate_token() if self._explicit_token is not None else rotate_remote_token()
        with self._lock:
            self._revoked.clear()
            self._fails.clear()
        return self._token

    def start(self) -> tuple[bool, str]:
        if self._server is not None:
            return True, self.public_url
        self._stop_event.clear()
        with self._snap_lock:
            self._snap_cache = None
        self.last_error = ""
        self.status_note = ""
        self._token = self._explicit_token or ensure_remote_token()

        info = tailscale_info() if self._manage_tailscale else None
        was_empty = serve_status_empty() if (info is not None and info.serve_enabled) else False
        try:
            srv = self._bind_server("127.0.0.1", self.preferred_port)
        except OSError as exc:
            self.last_error = f"bind failed ({exc})"
            return False, self.last_error

        local_port = int(srv.server_address[1])
        self._serve_owned = False
        if self._manage_tailscale and info is not None:
            ok, res = ((True, start_tailscale_serve(local_port, info)) if info.serve_enabled
                       else (False, (False, "HTTPS certificates not enabled on tailnet")))
            serve_ok, serve_msg = res if ok else res
            if serve_ok:
                self._server = srv
                self.bound_host, self.bound_port = "127.0.0.1", local_port
                self.public_url = serve_msg
                self.mode = "tailscale_serve"
                self._serve_owned = was_empty
            else:
                # Tailscale Serve may need tailnet HTTPS cert admin toggle; bind strictly on Tailscale IPv4 instead
                srv.server_close()
                if info.ip:
                    try:
                        srv = self._bind_server(info.ip, self.preferred_port)
                        self._server = srv
                        self.bound_host, self.bound_port = info.ip, int(srv.server_address[1])
                        public_host = info.dns_name or info.ip
                        self.public_url = f"http://{public_host}:{self.bound_port}"
                        self.mode = "tailscale_ip"
                        self.status_note = (
                            "Encrypted Tailscale MagicDNS mode (HTTP). Enable HTTPS Certificates in Tailscale DNS "
                            "admin settings if you prefer HTTPS without a port."
                            if info.dns_name else
                            "Encrypted Tailscale direct mode (HTTP). Enable MagicDNS or HTTPS Certificates in Tailscale DNS "
                            "admin settings to use a hostname instead of an IP."
                        )
                    except OSError as exc:
                        self.last_error = f"Tailscale IP bind failed ({exc})"
                        return False, self.last_error
                else:
                    self.last_error = serve_msg
                    return False, self.last_error
        else:
            self._server = srv
            self.bound_host, self.bound_port = "127.0.0.1", local_port
            self.public_url = f"http://127.0.0.1:{local_port}"
            self.mode = "loopback"
            if self._manage_tailscale:
                self.last_error = "Tailscale is offline; listening on localhost only"

        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True)
        self._thread.start()
        return True, self.public_url

    def _bind_server(self, host: str, port: int) -> _BridgeHTTPServer:
        try:
            return _BridgeHTTPServer((host, port), self)
        except OSError:
            if port != 0:
                return _BridgeHTTPServer((host, 0), self)
            raise

    def stop(self) -> None:
        self._stop_event.set()
        with self._snap_lock:
            self._snap_cache = None
        with self._ver_cond:
            self._ver_cond.notify_all()
        srv, self._server = self._server, None
        if srv is not None:
            try:
                srv.shutdown()
            except Exception:
                pass
            try:
                srv.server_close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._serve_owned:
            self._serve_owned = False
            stop_tailscale_serve()
        self.bound_host = self.public_url = self.mode = ""
        self.bound_port = 0
