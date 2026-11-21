"""Tailscale mobile web bridge: auth, CSRF/CSP/Fetch-Metadata security, Tailscale fallback, and Qt integration."""

from __future__ import annotations

import http.cookiejar
import json
import re
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace
from typing import Any

import pytest

from src.engine.models import PetStats
from src.engine.pet_state import PetStateMachine
from src.graphics.animation_controller import AnimationController
from src.tools import ai_assist, phone_bridge
from src.tools.phone_bridge import (
    DEFAULT_PORT,
    MAX_BODY_BYTES,
    RATE_MAX_FAILS,
    BridgeHandlers,
    PhoneBridge,
    TailscaleInfo,
)


def _dummy_handlers(state_box: dict[str, Any], actions: list[tuple[str, Any]], screen_bytes: bytes | None = b"PNGDATA") -> BridgeHandlers:
    return BridgeHandlers(
        get_state=lambda: dict(state_box),
        send_message=lambda text: actions.append(("send", text)) or (True, "sent"),
        stop_chat=lambda: actions.append(("stop", None)) or True,
        new_chat=lambda: actions.append(("new", None)) or (True, "ok"),
        resolve_approval=lambda aid, choice: actions.append(("approve", (aid, choice))) or True,
        grab_screen=lambda max_w: actions.append(("screen", max_w)) or (screen_bytes, "" if screen_bytes else "locked"),
    )


def _req(url: str, *, method: str = "GET", body: bytes | None = None, headers: dict[str, str] | None = None,
         opener: urllib.request.OpenerDirector | None = None) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    open_fn = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_fn(req, timeout=5) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read()


def test_http_bridge_auth_csrf_headers_and_actions() -> None:
    state = {"busy": False, "model": "m1", "auto_mode": False, "progress": "", "stream_text": "",
             "tasks": [["t1", "in_progress"]], "procs": [], "history": [], "approvals": [], "error": ""}
    actions: list[tuple[str, Any]] = []
    bridge = PhoneBridge(_dummy_handlers(state, actions), port=0, token="sec_token_1234567890", manage_tailscale=False)
    ok, base = bridge.start()
    assert ok and base.startswith("http://127.0.0.1:")
    try:
        # Unauthenticated API is 401 and HTML login carries strict CSP + security headers
        code, hdrs, html = _req(base + "/")
        assert code == 200 and hdrs.get("x-frame-options") == "DENY"
        assert hdrs.get("x-content-type-options") == "nosniff"
        assert hdrs.get("referrer-policy") == "no-referrer"
        assert "script-src 'nonce-" in hdrs.get("content-security-policy", "")
        assert "Pair Paperduck" in html.decode("utf-8")

        api_code, _, _ = _req(base + "/api/state")
        assert api_code == 401

        # Cross-site fetch metadata blocked before auth
        cs_code, _, _ = _req(base + "/api/state", headers={"Sec-Fetch-Site": "cross-site", "Authorization": "Bearer sec_token_1234567890"})
        assert cs_code == 403

        # Cross-site subresource/iframe /pair stays blocked, while top-level QR app navigation succeeds
        cs_img, _, _ = _req(base + "/pair?t=sec_token_1234567890",
                            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"})
        assert cs_img == 403
        cs_iframe, _, _ = _req(base + "/pair?t=sec_token_1234567890",
                               headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"})
        assert cs_iframe == 403
        qr_jar = http.cookiejar.CookieJar()
        qr_op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(qr_jar))
        cs_nav, _, _ = _req(base + "/pair?t=sec_token_1234567890",
                            headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"},
                            opener=qr_op)
        assert cs_nav == 200

        # Wrong token fails; Bearer token accesses state, screen, send, approve, stop, new
        bad_code, _, _ = _req(base + "/api/state", headers={"Authorization": "Bearer wrong"})
        assert bad_code == 401

        auth_h = {"Authorization": "Bearer sec_token_1234567890"}
        ok_code, _, raw = _req(base + "/api/state", headers=auth_h)
        assert ok_code == 200 and json.loads(raw)["model"] == "m1"

        sc_code, sc_hdrs, sc_body = _req(base + "/api/screen.png?max_w=1024", headers=auth_h)
        assert sc_code == 200 and sc_hdrs.get("content-type") == "image/png" and sc_body == b"PNGDATA"
        assert ("screen", 1024) in actions

        post_h = {**auth_h, "Content-Type": "application/json"}
        s_code, _, _ = _req(base + "/api/send", method="POST", body=json.dumps({"text": "run checks"}).encode(), headers=post_h)
        assert s_code == 200 and ("send", "run checks") in actions

        a_code, _, _ = _req(base + "/api/approve", method="POST", body=json.dumps({"id": "a1", "choice": "once"}).encode(), headers=post_h)
        assert a_code == 200 and ("approve", ("a1", "once")) in actions

        _req(base + "/api/stop", method="POST", body=b"{}", headers=post_h)
        _req(base + "/api/new", method="POST", body=b"{}", headers=post_h)
        assert ("stop", None) in actions and ("new", None) in actions

        # Cookie pairing flow sets HttpOnly cookie and enforces X-CSRF-Token on POSTs
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        p_code, _, app_html = _req(base + "/pair?t=sec_token_1234567890", opener=opener)
        assert p_code == 200
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', app_html.decode("utf-8"))
        assert m is not None
        csrf = m.group(1)

        no_csrf, _, _ = _req(base + "/api/send", method="POST", body=b'{"text":"x"}',
                             headers={"Content-Type": "application/json"}, opener=opener)
        assert no_csrf == 401

        with_csrf, _, _ = _req(base + "/api/send", method="POST", body=b'{"text":"from cookie"}',
                               headers={"Content-Type": "application/json", "X-CSRF-Token": csrf}, opener=opener)
        assert with_csrf == 200 and ("send", "from cookie") in actions

        # Bad Origin rejected on POST
        bad_orig, _, _ = _req(base + "/api/stop", method="POST", body=b"{}",
                              headers={"Content-Type": "application/json", "X-CSRF-Token": csrf, "Origin": "https://evil.invalid"},
                              opener=opener)
        assert bad_orig == 403

        # Token rotation invalidates old cookie sessions
        new_tok = bridge.rotate_token()
        assert new_tok != "sec_token_1234567890"
        stale, _, _ = _req(base + "/api/state", opener=opener)
        assert stale == 401

        # Oversized body rejected
        big, _, _ = _req(base + "/api/send", method="POST", body=b"{" + b"x" * MAX_BODY_BYTES,
                         headers={"Authorization": f"Bearer {new_tok}", "Content-Type": "application/json"})
        assert big == 413
    finally:
        bridge.stop()


def test_bridge_rate_limit_and_locked_screen_503() -> None:
    actions: list[tuple[str, Any]] = []
    bridge = PhoneBridge(_dummy_handlers({}, actions, screen_bytes=None), port=0, token="good_tok", manage_tailscale=False)
    ok, base = bridge.start()
    assert ok
    try:
        code, _, body = _req(base + "/api/screen.png", headers={"Authorization": "Bearer good_tok"})
        assert code == 503 and "locked" in json.loads(body)["error"]

        for _ in range(RATE_MAX_FAILS):
            rc, _, _ = _req(base + "/pair?t=wrong")
            assert rc == 401
        limited, _, _ = _req(base + "/pair?t=good_tok")
        assert limited == 429
    finally:
        bridge.stop()


def test_tailscale_serve_and_fallback_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    info = TailscaleInfo(dns_name="duckbox.tail123.ts.net", ip="127.0.0.1", backend_state="Running", host_name="duckbox")
    monkeypatch.setattr(phone_bridge, "tailscale_info", lambda: info)
    monkeypatch.setattr(phone_bridge, "serve_status_empty", lambda: True)
    monkeypatch.setattr(phone_bridge, "start_tailscale_serve", lambda port, _i=None: calls.append(f"serve:{port}") or (True, f"https://{info.dns_name}"))
    monkeypatch.setattr(phone_bridge, "stop_tailscale_serve", lambda: calls.append("reset"))

    bridge = PhoneBridge(_dummy_handlers({}, []), port=0, token="tok_ts", manage_tailscale=True)
    ok, url = bridge.start()
    assert ok and url == "https://duckbox.tail123.ts.net" and bridge.mode == "tailscale_serve"
    assert bridge.bound_host == "127.0.0.1"
    bridge.stop()
    assert "reset" in calls

    # When HTTPS Serve fails, fall back cleanly to Tailscale MagicDNS (still bound strictly to Tailscale IP)
    calls.clear()
    monkeypatch.setattr(phone_bridge, "start_tailscale_serve", lambda _p, _i=None: (False, "https disabled"))
    ok2, url2 = bridge.start()
    assert ok2 and bridge.mode == "tailscale_ip" and url2.startswith("http://duckbox.tail123.ts.net:")
    assert bridge.bound_host == "127.0.0.1"
    bridge.stop()
    assert "reset" not in calls

    # Without MagicDNS, fallback uses the bound Tailscale IP
    info_no_dns = TailscaleInfo(dns_name="", ip="127.0.0.1", backend_state="Running", host_name="duckbox")
    monkeypatch.setattr(phone_bridge, "tailscale_info", lambda: info_no_dns)
    ok3, url3 = bridge.start()
    assert ok3 and bridge.mode == "tailscale_ip" and url3.startswith("http://127.0.0.1:")
    bridge.stop()


def test_qt_chat_remote_send_steer_approve_and_menu() -> None:
    from PyQt6.QtWidgets import QApplication
    from src.ui.ai_chat import AiChat
    from src.ui.pet_window import PetWindow

    app = QApplication.instance() or QApplication([])

    plugin = ai_assist.AiAssistPlugin()
    plugin.base_url, plugin.model = "https://example.invalid/v1", "m1"
    chat = AiChat(plugin)
    try:
        # remote send does not clobber desktop draft or require showing desktop window
        chat._draft.setPlainText("half typed on PC")
        ok, err = chat.send_remote("check repo")
        assert not ok and "API key" in err
        assert chat._draft.toPlainText() == "half typed on PC"

        # remote steer while job is running queues into _steer_q without touching _draft
        chat._job = SimpleNamespace()
        ok_s, mode = chat.send_remote("skip docs")
        assert ok_s and mode == "steered"
        assert chat._steer_q == ["skip docs"] and chat._steer_typed == ["skip docs"]
        assert chat._draft.toPlainText() == "half typed on PC"

        # remote approval opens & resolves cleanly via resolve_approval
        answers: list[bool] = []
        changed: list[int] = []
        chat.remote_changed.connect(lambda: changed.append(1))
        chat.confirm("run_command", "Run check:\n\npytest -q", "commands", answers.append)
        snap = chat.remote_snapshot()
        assert len(snap["approvals"]) == 1
        aid = snap["approvals"][0]["id"]
        assert chat.resolve_approval(aid, "once") is True
        assert answers == [True] and chat.remote_snapshot()["approvals"] == []
        assert not chat.resolve_approval(aid, "once")  # idempotent single-shot
        assert len(changed) >= 2
    finally:
        chat.close()

    # Window menu includes Phone Control submenu and persists disabled default cleanly
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    try:
        menu = win.build_context_menu()
        try:
            assert "Phone Control" in menu.plugin_submenus
            assert "remote:toggle" in menu.plugin_actions
            assert "remote:pair" in menu.plugin_actions
            assert "remote:rotate" in menu.plugin_actions
        finally:
            menu.deleteLater()
        assert win.remote.to_dict() == {"enabled": False, "port": DEFAULT_PORT}
        win.remote.load_dict({"enabled": True, "port": 51234}, auto_start=False)
        assert win.remote.enabled is True and win.remote.port == 51234 and not win.remote.running
    finally:
        win.shutdown()
        win.close()


def test_sse_events_push_state_updates_and_close_cleanly() -> None:
    state = {"busy": False, "model": "m1", "progress": "", "history": [], "approvals": []}
    bridge = PhoneBridge(_dummy_handlers(state, []), port=0, token="sse_tok", manage_tailscale=False)
    ok, base = bridge.start()
    assert ok
    seen: list[str] = []
    ready = threading.Event()

    def reader() -> None:
        req = urllib.request.Request(base + "/api/events", headers={"Authorization": "Bearer sse_tok"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            while len(seen) < 2:
                line = resp.readline().decode("utf-8", "replace")
                if not line:
                    break
                if line.startswith("data: "):
                    seen.append(line[6:].strip())
                    if len(seen) == 1:
                        ready.set()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        assert ready.wait(timeout=3.0)
        state["busy"] = True
        state["progress"] = "Running tests"
        bridge.notify_change()
        t.join(timeout=3.0)
        assert len(seen) == 2
        assert json.loads(seen[1])["progress"] == "Running tests"
    finally:
        bridge.stop()


def test_encode_png_and_remote_controller_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtCore import QCoreApplication, QTimer
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication
    from src.ui.pet_remote import RemoteController, _encode_png
    from src.ui.pet_window import PetWindow

    app = QApplication.instance() or QApplication([])
    img = QImage(640, 360, QImage.Format.Format_ARGB32)
    img.fill(QColor("#112233"))
    png = _encode_png(img, 320)
    assert png is not None and png.startswith(b"\x89PNG")
    scaled = QImage.fromData(png, "PNG")
    assert scaled.width() == 320 and scaled.height() == 180

    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    ctrl = RemoteController(win, manage_tailscale=False, port=0)
    ctrl.bridge._explicit_token = "ctrl_tok"
    ok, base = ctrl.start()
    assert ok
    try:
        box: dict[str, Any] = {}

        def hit_api() -> None:
            box["state"] = _req(base + "/api/state", headers={"Authorization": "Bearer ctrl_tok"})
            QTimer.singleShot(0, app.quit)

        t = threading.Thread(target=hit_api, daemon=True)
        t.start()
        app.exec()
        t.join(timeout=3.0)
        code, _hdrs, raw = box["state"]
        assert code == 200 and json.loads(raw)["pet_state"] == "IDLE"
    finally:
        ctrl.close()
        win.shutdown()
        win.close()


def _qr_roundtrip_decode(grid: list[list[bool]]) -> str:
    """Independent ISO-18004 level-L byte-mode decoder + GF(256) RS syndrome check over rendered modules."""
    from src.tools import qr

    n = len(grid)
    ver = (n - 17) // 4
    spec = next(s for s in qr._VERSIONS if s[0] == ver)
    _v, tot, ec_len, g1b, g1d, g2b, g2d, align = spec
    _base, func = qr._build_base_matrix(ver, align)

    # Read 15-bit format info (coords1) -> ECC L (01) and 3-bit mask index
    coords1 = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
               (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    fmt = 0
    for r, c in coords1:
        fmt = (fmt << 1) | int(grid[r][c])
    unmasked_fmt = fmt ^ 0x5412
    fmt_bits = (unmasked_fmt >> 10) & 0x1F
    assert qr._bch_format(fmt_bits) == fmt
    assert (fmt_bits >> 3) == 0b01
    mask = fmt_bits & 0x7

    bits: list[int] = []
    col, up = n - 1, True
    while col > 0:
        if col == 6:
            col -= 1
        for r in (range(n - 1, -1, -1) if up else range(n)):
            for dc in (0, -1):
                c = col + dc
                if not func[r][c]:
                    b = int(grid[r][c]) ^ (1 if qr._mask_fn(mask, r, c) else 0)
                    bits.append(b)
        col -= 2
        up = not up

    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, tot * 8, 8)]
    blocks: list[list[int]] = [[] for _ in range(g1b + g2b)]
    ecs: list[list[int]] = [[] for _ in range(g1b + g2b)]
    idx = 0
    for i in range(max(g1d, g2d)):
        for b_i, cap in enumerate([g1d] * g1b + [g2d] * g2b):
            if i < cap:
                blocks[b_i].append(codewords[idx])
                idx += 1
    for _ in range(ec_len):
        for b_i in range(len(blocks)):
            ecs[b_i].append(codewords[idx])
            idx += 1
    for blk, ec in zip(blocks, ecs):
        assert qr._rs_remainder(blk, ec_len) == ec  # valid Reed-Solomon parity

    stream = [bit for blk in blocks for cw in blk for bit in [(cw >> s) & 1 for s in range(7, -1, -1)]]
    assert stream[:4] == [0, 1, 0, 0]  # Byte mode
    cnt_w = 8 if ver < 10 else 16
    length = int("".join(str(b) for b in stream[4:4 + cnt_w]), 2)
    raw = bytes(int("".join(str(b) for b in stream[4 + cnt_w + 8 * k:4 + cnt_w + 8 * (k + 1)]), 2)
                for k in range(length))
    return raw.decode("utf-8")


def test_qr_encoder_roundtrip_and_pairing_dialog_contrast(monkeypatch: pytest.MonkeyPatch) -> None:
    from PyQt6.QtGui import QImage
    from PyQt6.QtWidgets import QApplication
    from src.tools.qr import qr_matrix
    from src.ui.pet_remote import RemoteController, _PairingDialog
    from src.ui.pet_window import PetWindow
    from src.ui.theme import GLASS_RGBA, TEXT_HEX

    for sample in (
        "http://100.99.60.49:48791/pair?t=AT_DMwYDKXR5aYKbkpi-cL1hPw4wUaKX",
        "https://boci.tail6ace9c.ts.net/pair?t=sample_token_value_123456",
        "https://example.invalid/" + "x" * 160,  # exercises multi-block versions >= 6
    ):
        assert _qr_roundtrip_decode(qr_matrix(sample)) == sample

    # CertDomains missing -> skips slow `tailscale serve` CLI completely
    info = TailscaleInfo(dns_name="boci.tail.ts.net", ip="127.0.0.1", backend_state="Running",
                         host_name="boci", serve_enabled=False)
    monkeypatch.setattr(phone_bridge, "tailscale_info", lambda: info)
    monkeypatch.setattr(phone_bridge, "start_tailscale_serve", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not call serve")))
    fast_bridge = PhoneBridge(_dummy_handlers({}, []), port=0, token="fast_tok", manage_tailscale=True)
    ok, _ = fast_bridge.start()
    try:
        assert ok and fast_bridge.mode == "tailscale_ip" and fast_bridge.last_error == ""
    finally:
        fast_bridge.stop()

    app = QApplication.instance() or QApplication([])
    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    ctrl = RemoteController(win, manage_tailscale=False, port=0)
    ctrl.bridge._explicit_token = "dlg_tok_123456"
    ctrl.start()
    try:
        dlg = _PairingDialog(ctrl, win)
        try:
            dlg.show()
            app.processEvents()
            assert dlg._url_edit.text().startswith("http://127.0.0.1:")
            assert dlg._url_edit.echoMode() == dlg._url_edit.EchoMode.Password
            assert "127.0.0.1" not in dlg._status.text()
            assert len(dlg._qr._matrix) >= 21
            # Buttons explicitly style text & paper colors (never white-on-white OS fallback)
            ss = dlg.styleSheet()
            assert f"color: {TEXT_HEX}" in ss
            assert GLASS_RGBA[:3] != (255, 255, 255)
            shot = QImage(dlg.size(), QImage.Format.Format_ARGB32)
            dlg.render(shot)
            assert not shot.isNull()
        finally:
            dlg.close()
            dlg.deleteLater()
    finally:
        ctrl.close()
        win.shutdown()
        win.close()


def test_mobile_html_controls_csp_and_upload_endpoint() -> None:
    import base64
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication
    from src.tools.phone_bridge import (
        MAX_REMOTE_ATTACHMENTS,
        MAX_REMOTE_FILE_BYTES,
        RemoteAttachment,
        _app_html,
        _login_html,
        sanitize_attachment_name,
    )

    assert sanitize_attachment_name("../../etc/passwd") == "passwd"
    assert sanitize_attachment_name("C:\\temp\\a:b*.txt") == "a_b_.txt"
    assert sanitize_attachment_name("") == "upload.bin"

    for doc in (_app_html("nonce_abc", "csrf_xyz"), _login_html("nonce_abc", "bad")):
        assert "innerHTML" not in doc
        assert "\u2014" not in doc
        assert not re.search(r"<[a-zA-Z][^>]*\sstyle\s*=", doc)
    app_doc = _app_html("nonce_abc", "csrf_xyz")
    for required_id in (
        'id="minScreenBtn"',
        'id="expandScreenBtn"',
        'id="closeModalBtn"',
        'id="attachBtn"',
        'id="fileInput"',
        'id="attachStrip"',
        'id="chatPane"',
        'class="top-dock"',
        'enterkeyhint="enter"',
        'closedby="any"',
    ):
        assert required_id in app_doc
    assert "empty-card" not in app_doc
    assert "scroll-padding-bottom" not in app_doc

    _app = QApplication.instance() or QApplication([])
    img = QImage(16, 12, QImage.Format.Format_RGBA8888)
    img.fill(QColor("#a86f00"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "PNG")
    png_bytes = bytes(ba.data())

    received: list[tuple[str, list[RemoteAttachment]]] = []
    handlers = BridgeHandlers(
        get_state=lambda: {"busy": False, "history": [], "approvals": []},
        send_message=lambda _t: (True, "sent"),
        stop_chat=lambda: True,
        new_chat=lambda: (True, "ok"),
        resolve_approval=lambda _a, _c: True,
        grab_screen=lambda _w: (png_bytes, ""),
        send_attachments=lambda text, atts: received.append((text, list(atts))) or (True, "sent"),
    )
    bridge = PhoneBridge(handlers, port=0, token="up_tok_1234", manage_tailscale=False)
    ok, base = bridge.start()
    assert ok
    try:
        headers = {"Authorization": "Bearer up_tok_1234", "Content-Type": "application/json"}
        # Unauthenticated upload denied
        unauth, _, _ = _req(base + "/api/upload", method="POST", body=b"{}", headers={"Content-Type": "application/json"})
        assert unauth == 401

        # Attachment-only upload (empty text) with unsafe path filename is sanitized cleanly
        payload = {
            "text": "",
            "attachments": [
                {"name": "..\\..\\photo.png", "mime": "image/png", "data_b64": base64.b64encode(png_bytes).decode("ascii")},
                {"name": "notes.md", "mime": "text/markdown", "data_b64": base64.b64encode(b"# Heading\nhello").decode("ascii")},
            ],
        }
        code, _, raw = _req(base + "/api/upload", method="POST", body=json.dumps(payload).encode("utf-8"), headers=headers)
        assert code == 200 and json.loads(raw)["ok"] is True
        assert len(received) == 1
        assert received[0][0] == ""
        assert [a.name for a in received[0][1]] == ["photo.png", "notes.md"]
        assert received[0][1][0].data == png_bytes

        # Bad base64 -> 400
        bad_b64, _, _ = _req(
            base + "/api/upload",
            method="POST",
            body=json.dumps({"text": "hi", "attachments": [{"name": "a.txt", "mime": "text/plain", "data_b64": "!!!notb64"}]}).encode(),
            headers=headers,
        )
        assert bad_b64 == 400

        # Too many files -> 400
        too_many, _, _ = _req(
            base + "/api/upload",
            method="POST",
            body=json.dumps({
                "text": "hi",
                "attachments": [{"name": f"{i}.txt", "mime": "text/plain", "data_b64": "YQ=="} for i in range(MAX_REMOTE_ATTACHMENTS + 1)],
            }).encode(),
            headers=headers,
        )
        assert too_many == 400

        # Oversized single file -> 413
        huge_b64 = base64.b64encode(b"a" * (MAX_REMOTE_FILE_BYTES + 1)).decode("ascii")
        too_big, _, _ = _req(
            base + "/api/upload",
            method="POST",
            body=json.dumps({"text": "", "attachments": [{"name": "big.bin", "mime": "application/octet-stream", "data_b64": huge_b64}]}).encode(),
            headers=headers,
        )
        assert too_big == 413
    finally:
        bridge.stop()


def test_remote_prepare_attachment_and_chat_send_remote(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication
    from src.tools.phone_bridge import RemoteAttachment
    from src.ui import pet_remote
    from src.ui.ai_chat import AiChat

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pet_remote, "scratch_dir", lambda: tmp_path)
    monkeypatch.setattr(pet_remote, "ocr_image_file", lambda _p: "OCR line")

    img = QImage(24, 18, QImage.Format.Format_RGBA8888)
    img.fill(QColor("#112233"))
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    assert img.save(buf, "PNG")
    png_bytes = bytes(ba.data())

    from pathlib import Path

    att_img, err1 = pet_remote._prepare_attachment(RemoteAttachment("screen.png", "image/png", png_bytes))
    assert att_img is not None and err1 == ""
    assert att_img.kind == "image" and att_img.image_url.startswith("data:image/png;base64,") and att_img.text == "OCR line"
    assert att_img.saved_path and Path(att_img.saved_path).is_file()
    assert (tmp_path / "remote_uploads") in Path(att_img.saved_path).parents
    assert Path(att_img.saved_path).read_bytes().startswith(b"\x89PNG")
    assert f"[Saved file path: {att_img.saved_path}]" in att_img.as_prompt()

    att_txt, err2 = pet_remote._prepare_attachment(RemoteAttachment("app.py", "text/x-python", b"print('ok')\n"))
    assert att_txt is not None and err2 == ""
    assert att_txt.kind == "file" and att_txt.text == "print('ok')\n" and att_txt.path == ""
    assert att_txt.saved_path and Path(att_txt.saved_path).is_file()
    assert f"[Saved file path: {att_txt.saved_path}]" in att_txt.as_prompt()

    att_bin, err3 = pet_remote._prepare_attachment(RemoteAttachment("data.bin", "application/octet-stream", b"\x00\x01\x02\x03"))
    assert att_bin is not None and err3 == ""
    assert att_bin.kind == "file" and att_bin.path and tmp_path in __import__("pathlib").Path(att_bin.path).parents

    plugin = ai_assist.AiAssistPlugin()
    plugin.base_url, plugin.model = "https://example.invalid/v1", "m1"
    chat = AiChat(plugin)
    try:
        # Steering with attachment while job active leaves _steer_q and _steer_typed aligned and records attachment
        chat._job = SimpleNamespace()
        ok_s, mode = chat.send_remote("", [att_img, att_txt])
        assert ok_s and mode == "steered"
        assert len(chat._steer_q) == 1 and "screen.png" in chat._steer_q[0] and "print('ok')" in chat._steer_q[0]
        assert chat._steer_typed == [""]
        assert att_img.thumb is not None and not att_img.thumb.isNull()

        # Pending steering appears immediately in remote_snapshot while job is busy
        busy_snap = chat.remote_snapshot()
        assert busy_snap["history"][-1]["steering"] is True
        assert busy_snap["history"][-1]["attachments"] == ["screen.png", "app.py"]

        # Simulate idle history containing an image-only user turn and check remote_snapshot extraction
        chat._job = None
        chat._remote_steers.clear()
        chat._history.append({"role": "user", "content": att_img.as_prompt(), "images": [att_img.image_url]})
        snap = chat.remote_snapshot()
        assert snap["history"][-1]["attachments"] == ["screen.png"]
    finally:
        chat.close()


def test_session_survives_restart_with_same_token_until_logout_or_rotate() -> None:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    b1 = PhoneBridge(_dummy_handlers({"busy": False}, []), port=0, token="persist_tok_1234", manage_tailscale=False)
    ok1, url1 = b1.start()
    assert ok1
    try:
        p_code, _, _ = _req(url1 + "/pair?t=persist_tok_1234", opener=opener)
        assert p_code == 200
    finally:
        b1.stop()

    # Restart a brand-new PhoneBridge instance with the same token: cookie session stays paired
    b2 = PhoneBridge(_dummy_handlers({"busy": False, "model": "after_restart"}, []), port=0,
                     token="persist_tok_1234", manage_tailscale=False)
    ok2, url2 = b2.start()
    assert ok2
    try:
        # Copy cookie across ephemeral ports by reading csrf from `/` and polling `/api/state`
        c_list = list(jar)
        assert len(c_list) == 1
        cookie_hdr = f"{c_list[0].name}={c_list[0].value}"
        s_code, _, raw = _req(url2 + "/api/state", headers={"Cookie": cookie_hdr})
        assert s_code == 200 and json.loads(raw)["model"] == "after_restart"

        root_code, _, html_b = _req(url2 + "/", headers={"Cookie": cookie_hdr})
        assert root_code == 200
        csrf_m = re.search(r'<meta name="csrf-token" content="([^"]+)"', html_b.decode("utf-8"))
        assert csrf_m is not None

        # Logout revokes that specific session immediately
        out_code, _, _ = _req(url2 + "/api/logout", method="POST", body=b"{}",
                              headers={"Cookie": cookie_hdr, "X-CSRF-Token": csrf_m.group(1)})
        assert out_code == 200
        after_out, _, _ = _req(url2 + "/api/state", headers={"Cookie": cookie_hdr})
        assert after_out == 401
    finally:
        b2.stop()


def test_post_pair_persistent_cookie_settings_sessions_procs_and_screen_opts() -> None:
    import urllib.parse

    calls: list[tuple[str, Any]] = []
    handlers = BridgeHandlers(
        get_state=lambda: calls.append(("get_state", None)) or {"busy": False, "model": "m1", "history": [], "approvals": []},
        send_message=lambda _t: (True, "sent"),
        stop_chat=lambda: True,
        new_chat=lambda: (True, "ok"),
        resolve_approval=lambda _a, _c: True,
        grab_screen=lambda max_w: (b"PNG", ""),
        set_settings=lambda payload: calls.append(("settings", payload)) or (True, "updated"),
        set_target=lambda target: calls.append(("target", target)) or (True, target),
        refresh_models=lambda: calls.append(("refresh_models", None)) or (True, "refreshing"),
        list_sessions=lambda: [{"id": "0123456789ab", "title": "First", "turns": 4, "age": "1m ago", "active": True}],
        resume_session=lambda sid: calls.append(("resume", sid)) or (True, "resumed"),
        delete_session=lambda sid: calls.append(("delete", sid)) or (True, "deleted"),
        stop_proc=lambda name: calls.append(("stop_proc", name)) or (True, "stopping"),
        grab_screen_opts=lambda max_w, mon, fmt, q: (
            calls.append(("screen_opts", (max_w, mon, fmt, q)))
            or (b"JPGDATA" if fmt == "jpeg" else b"PNGDATA", "image/jpeg" if fmt == "jpeg" else "image/png", "")
        ),
    )
    bridge = PhoneBridge(handlers, port=0, token="post_pair_tok_123", manage_tailscale=False)
    ok, base = bridge.start()
    assert ok
    try:
        # Login page uses POST /pair so tokens stay out of browser history URLs
        _c, _h, login_b = _req(base + "/")
        assert 'method="POST" action="/pair"' in login_b.decode("utf-8")

        # POST /pair (form-urlencoded) sets persistent Max-Age cookie and redirects to /
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        form_body = urllib.parse.urlencode({"t": "post_pair_tok_123"}).encode("utf-8")
        p_code, _ph, app_b = _req(
            base + "/pair",
            method="POST",
            body=form_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            opener=opener,
        )
        assert p_code == 200
        c_list = list(jar)
        assert len(c_list) == 1 and c_list[0].expires is not None
        csrf_m = re.search(r'<meta name="csrf-token" content="([^"]+)"', app_b.decode("utf-8"))
        assert csrf_m is not None
        csrf = csrf_m.group(1)

        # POST /pair (JSON) also works without CSRF (pre-auth) and rejects bad token
        bad_pair, _, _ = _req(
            base + "/pair",
            method="POST",
            body=json.dumps({"t": "wrong"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert bad_pair == 401
        ok_json_pair, json_hdrs, json_body = _req(
            base + "/pair",
            method="POST",
            body=json.dumps({"t": "post_pair_tok_123"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert ok_json_pair == 200 and json.loads(json_body)["ok"] is True
        assert "Max-Age=" in json_hdrs.get("set-cookie", "")

        post_h = {"Content-Type": "application/json", "X-CSRF-Token": csrf}

        # State snapshot caching shares one get_state() call across /api/state calls at the same version
        _req(base + "/api/state", opener=opener)
        _req(base + "/api/state", opener=opener)
        assert sum(1 for k, _ in calls if k == "get_state") == 1
        bridge.notify_change()
        _req(base + "/api/state", opener=opener)
        assert sum(1 for k, _ in calls if k == "get_state") == 2

        # Screen endpoint supports JPEG, monitor selection, and preserves /api/screen.png
        sc_code, sc_h, sc_b = _req(base + "/api/screen?max_w=900&monitor=1&fmt=jpeg&q=70", opener=opener)
        assert sc_code == 200 and sc_h.get("content-type") == "image/jpeg" and sc_b == b"JPGDATA"
        assert ("screen_opts", (900, 1, "jpeg", 70)) in calls

        png_code, png_h, png_b = _req(base + "/api/screen.png?max_w=800&monitor=2", opener=opener)
        assert png_code == 200 and png_h.get("content-type") == "image/png" and png_b == b"PNGDATA"
        assert ("screen_opts", (800, 2, "png", 80)) in calls

        # Settings, target switching, model refresh, sessions list/resume/delete, and background proc stop
        s_code, _, _ = _req(
            base + "/api/settings",
            method="POST",
            body=json.dumps({
                "model": "m2",
                "thinking": "high",
                "screen_thinking": "max",
                "auto": True,
            }).encode(),
            headers=post_h,
            opener=opener,
        )
        assert s_code == 200 and (
            "settings",
            {"model": "m2", "thinking": "high", "screen_thinking": "max", "auto": True},
        ) in calls

        t_code, _, t_body = _req(
            base + "/api/target",
            method="POST",
            body=json.dumps({"target": "laptop"}).encode(),
            headers=post_h,
            opener=opener,
        )
        t_json = json.loads(t_body)
        assert t_code == 200 and ("target", "laptop") in calls and isinstance(t_json.get("state"), dict)
        bad_t, _, _ = _req(
            base + "/api/target",
            method="POST",
            body=json.dumps({"target": "tablet"}).encode(),
            headers=post_h,
            opener=opener,
        )
        assert bad_t == 400

        bad_set, _, _ = _req(
            base + "/api/settings",
            method="POST",
            body=json.dumps({"auto": "yes"}).encode(),
            headers=post_h,
            opener=opener,
        )
        assert bad_set == 400

        r_code, _, _ = _req(base + "/api/models/refresh", method="POST", body=b"{}", headers=post_h, opener=opener)
        assert r_code == 200 and ("refresh_models", None) in calls

        ls_code, _, ls_b = _req(base + "/api/sessions", opener=opener)
        assert ls_code == 200 and json.loads(ls_b)["sessions"][0]["id"] == "0123456789ab"

        res_code, _, _ = _req(
            base + "/api/sessions/resume",
            method="POST",
            body=json.dumps({"id": "0123456789ab"}).encode(),
            headers=post_h,
            opener=opener,
        )
        assert res_code == 200 and ("resume", "0123456789ab") in calls

        bad_sid, _, _ = _req(
            base + "/api/sessions/delete",
            method="POST",
            body=json.dumps({"id": "../bad"}).encode(),
            headers=post_h,
            opener=opener,
        )
        assert bad_sid == 400

        del_code, _, _ = _req(
            base + "/api/sessions/delete",
            method="POST",
            body=json.dumps({"id": "0123456789ab"}).encode(),
            headers=post_h,
            opener=opener,
        )
        assert del_code == 200 and ("delete", "0123456789ab") in calls

        proc_code, _, _ = _req(
            base + "/api/procs/stop",
            method="POST",
            body=json.dumps({"name": "bg1"}).encode(),
            headers=post_h,
            opener=opener,
        )
        assert proc_code == 200 and ("stop_proc", "bg1") in calls
    finally:
        bridge.stop()


def test_mobile_html_new_controls_and_js_invariants() -> None:
    from src.tools.phone_bridge import _app_html

    doc = _app_html("nonce_123", "csrf_456")
    for req_id in (
        'id="chatsBtn"',
        'id="settingsBtn"',
        'id="settingsDrawer"',
        'id="targetPhoneBtn"',
        'id="targetLaptopBtn"',
        'id="modelSelect"',
        'id="thinkingSelect"',
        'id="screenThinkingSelect"',
        'id="autoModeChk"',
        'id="logoutBtn"',
        'id="monitorSelect"',
        'id="screenFmtSelect"',
        'id="screenInterval"',
        'id="agentsBox"',
        'id="chatsModal"',
        'id="modalMonitorSelect"',
        'class="top-actions"',
        'class="settings-stack"',
        'class="screen-opts"',
        'name="mobile-web-app-capable"',
        'flex-wrap: wrap;',
        'id="streamStatus"',
        'id="alertsChk"',
        'id="repairBanner"',
        'id="repairBtn"',
        'id="queueBar"',
        'id="modalIntervalSelect"',
        'id="modalStage"',
        'id="fsExitFab"',
        '[hidden] { display: none !important; }',
        'fs-live',
        'probeSession',
        'approval-detail-bar',
        'diff-add',
        'flex-shrink: 0;',
        'visualViewport',
        "addEventListener('pageshow'",
        'font-size: max(16px',
    ):
        assert req_id in doc
    assert 'id="progressText"' not in doc
    assert "innerHTML" not in doc
    assert "\u2014" not in doc
    assert not re.search(r"<[a-zA-Z][^>]*\sstyle\s*=", doc)


def test_desktop_stream_label_sizes_to_card_width_on_first_chunk() -> None:
    from PyQt6.QtWidgets import QApplication
    from src.ui.ai_chat import AiChat

    _app = QApplication.instance() or QApplication([])
    plugin = ai_assist.AiAssistPlugin()
    plugin.base_url, plugin.model = "https://example.invalid/v1", "m1"
    chat = AiChat(plugin)
    try:
        chat.show()
        _app.processEvents()
        chat._stream_text = "word " * 40
        assert chat._sync_stream() is True
        _app.processEvents()
        assert chat._stream_label is not None
        assert chat._stream_label.width() < 400
        assert chat._stream_label.height() >= chat._stream_label.heightForWidth(chat._stream_label.width())
    finally:
        chat.close()


def test_ai_chat_sync_signals_tool_details_agents_and_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from PyQt6.QtWidgets import QApplication
    from src.tools import ai_sessions
    from src.ui.ai_chat import AiChat

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ai_sessions, "sessions_dir", lambda: tmp_path)

    plugin = ai_assist.AiAssistPlugin()
    plugin.base_url, plugin.model = "https://example.invalid/v1", "m1"
    chat = AiChat(plugin)
    try:
        changed: list[int] = []
        chat.remote_changed.connect(lambda: changed.append(1))

        # Model / thinking / screen_thinking / auto changes emit remote_changed and validate inputs
        ok_set, _ = chat.remote_set_settings(
            model="m2", thinking="high", screen_thinking="max", auto=True
        )
        assert ok_set and plugin.model == "m2" and plugin.thinking == "high"
        assert plugin.screen_thinking == "max" and plugin.auto is True
        assert ", screen max" in chat._title.text()
        assert len(changed) >= 1
        bad_set, _ = chat.remote_set_settings(model="bad model with spaces")
        assert not bad_set

        # reply_details displays screen_thinking_used when activated
        from src.ui.chat_widgets import reply_details

        r_used = ai_assist.AiReply(text="ok", model="m2", screen_thinking_used="max")
        assert "thinking high > screen max" in reply_details(r_used, "m2", "high")
        assert "screen thinking max" in reply_details(r_used, "m2", "off")

        # add_note and sub-agent events emit remote_changed and populate agents snapshot
        prev_n = len(changed)
        chat.add_note("Heads up")
        assert len(changed) > prev_n

        prev_n = len(changed)
        chat._on_agent_event(1, "start", "high\nInspect module")
        chat._on_agent_event(1, "tool", "read_file src/main.py")
        chat._on_agent_event(1, "done", "All good")
        assert len(changed) > prev_n

        # Tool output summaries scrub secrets and collapse whitespace
        chat._history.append({"role": "user", "content": "Hello [File: notes, v1.txt, 1.2 KB, read on demand]"})
        chat._history.append({
            "role": "assistant",
            "content": "Done.",
            "tools": [("run_command", "line1\n  sk-1234567890abcdef1234567890  line2")],
        })
        snap = chat.remote_snapshot()
        assert snap["history"][0]["attachments"] == ["notes, v1.txt"]
        td = snap["history"][1]["tool_details"][0]
        assert td["name"] == "run_command" and "sk-123456" not in td["summary"] and "\n" not in td["summary"]
        assert len(snap["agents"]) == 1 and snap["agents"][0]["status"] == "done" and snap["agents"][0]["report"] == "All good"
        assert snap["thinking_levels"]
    finally:
        chat.close()


def test_remote_controller_focus_safe_capture_jpeg_pruning_and_dialog(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    import os
    from PyQt6.QtGui import QColor, QImage
    from PyQt6.QtWidgets import QApplication
    from src.tools import computer
    from src.ui import pet_remote
    from src.ui.pet_remote import MAX_STAGED_UPLOADS, RemoteController, _PairingDialog, _encode_image, _stage_upload_file
    from src.ui.pet_window import PetWindow

    app = QApplication.instance() or QApplication([])
    img = QImage(320, 180, QImage.Format.Format_ARGB32)
    img.fill(QColor("#a86f00"))
    jpg, mime = _encode_image(img, 160, fmt="jpeg", quality=75)
    assert jpg is not None and jpg.startswith(b"\xff\xd8\xff") and mime == "image/jpeg"

    # Staging prunes excess uploads
    monkeypatch.setattr(pet_remote, "scratch_dir", lambda: tmp_path)
    for i in range(MAX_STAGED_UPLOADS + 5):
        p = _stage_upload_file(f"f{i}.bin", b"abc")
        os.utime(p, (1000 + i, 1000 + i))
    staged_dir = tmp_path / "remote_uploads"
    assert len(list(staged_dir.iterdir())) <= MAX_STAGED_UPLOADS

    # Focus-safe capture never invokes focus_front_app()
    monkeypatch.setattr(computer, "focus_front_app", lambda: (_ for _ in ()).throw(AssertionError("must not steal focus")))
    fake_screen = SimpleNamespace(
        name=lambda: "DISPLAY1",
        geometry=lambda: SimpleNamespace(width=lambda: 1920, height=lambda: 1080),
        grabWindow=lambda _id: SimpleNamespace(toImage=lambda: img),
    )
    monkeypatch.setattr(pet_remote, "_ordered_screens", lambda: [fake_screen])

    sm = PetStateMachine(PetStats())
    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    ctrl = RemoteController(win, manage_tailscale=False, port=0)
    ctrl.bridge._explicit_token = "tok_ctrl_safe"
    ok, base = ctrl.start()
    assert ok
    try:
        from PyQt6.QtCore import QTimer

        box: dict[str, Any] = {}

        def hit_screen() -> None:
            box["resp"] = _req(
                base + "/api/screen?max_w=160&monitor=0&fmt=jpeg&q=75",
                headers={"Authorization": "Bearer tok_ctrl_safe"},
            )
            QTimer.singleShot(0, app.quit)

        t = threading.Thread(target=hit_screen, daemon=True)
        t.start()
        app.exec()
        t.join(timeout=3.0)
        sc_code, sc_hdrs, sc_data = box["resp"]
        assert sc_code == 200 and sc_hdrs.get("content-type") == "image/jpeg" and sc_data.startswith(b"\xff\xd8\xff")

        dlg = _PairingDialog(ctrl, win)
        try:
            assert dlg._tok_edit.echoMode() == dlg._tok_edit.EchoMode.Password
            dlg._show_tok_btn.click()
            assert dlg._tok_edit.echoMode() == dlg._tok_edit.EchoMode.Normal
            assert dlg._show_tok_btn.text() == "Hide"
        finally:
            dlg.close()
            dlg.deleteLater()
    finally:
        ctrl.close()
        win.shutdown()
        win.close()


def test_dedicated_phone_chat_isolation_and_deepseek_screen_guard(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from PyQt6.QtWidgets import QApplication
    from src.tools import ai_sessions
    from src.ui.pet_remote import RemoteController
    from src.ui.pet_window import PetWindow

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ai_sessions, "sessions_dir", lambda: tmp_path)

    sm = PetStateMachine(PetStats())
    ai_plugin = ai_assist.AiAssistPlugin()
    ai_plugin.base_url = "https://example.invalid/v1"
    ai_plugin.model, ai_plugin.thinking, ai_plugin.auto = "deepseek-flash", "high", False
    sm.registry.register(ai_plugin)

    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    ctrl = RemoteController(win, manage_tailscale=False, port=0)
    try:
        # Untouched phone settings preserve exact minimal to_dict()
        assert ctrl.to_dict() == {"enabled": False, "port": 0}
        assert win.remote.to_dict() == {"enabled": False, "port": DEFAULT_PORT}

        # Phone chat settings are isolated from Laptop AiAssistPlugin and Laptop AiChat
        ok_s, _ = ctrl._set_settings({"thinking": "low", "screen_thinking": "high", "auto": True})
        assert ok_s
        assert ai_plugin.thinking == "high" and ai_plugin.auto is False
        assert ctrl._phone_plugin.thinking == "low" and ctrl._phone_plugin.screen_thinking == "high" and ctrl._phone_plugin.auto is True

        # Phone overrides persist in to_dict() / load_dict()
        saved = ctrl.to_dict()
        assert saved["chat"] == {"thinking": "low", "screen_thinking": "high", "auto": True}
        ctrl2 = RemoteController(win, manage_tailscale=False, port=0)
        ctrl2.load_dict(saved, auto_start=False)
        assert ctrl2._phone_plugin.thinking == "low" and ctrl2._phone_plugin.auto is True
        ctrl2.close()

        # Phone chat history & approvals never pop up a desktop window and never touch laptop chat history
        laptop = ctrl._ensure_laptop_chat()
        assert laptop is not None
        laptop._history.append({"role": "user", "content": "laptop msg"})
        phone = ctrl._ensure_phone_chat()
        phone._history.append({"role": "user", "content": "phone msg"})
        assert phone._remote_only is True

        answers: list[bool] = []
        phone.confirm("run_command", "Run:\n\necho hi", "commands", answers.append, review=(False, "check it"))
        assert not phone.isVisible()
        assert phone.resolve_approval(phone.remote_snapshot()["approvals"][0]["id"], "once") is True
        assert answers == [True]

        phone_snap = ctrl._get_state()
        assert phone_snap["target"] == "phone" and phone_snap["history"][0]["text"] == "phone msg"
        ok_sw, _ = ctrl._set_target("laptop")
        assert ok_sw
        lap_snap = ctrl._get_state()
        assert lap_snap["target"] == "laptop" and lap_snap["history"][0]["text"] == "laptop msg"
        assert lap_snap["thinking"] == "high" and lap_snap["auto_mode"] is False

        # Changing laptop thinking never overwrites an active phone chat, even if un-overridden,
        # and explicit target routing keeps phone vs laptop settings strictly isolated.
        ctrl3 = RemoteController(win, manage_tailscale=False, port=0)
        try:
            ph3 = ctrl3._ensure_phone_chat()
            assert ph3._plugin.thinking == "high"
            laptop._pick(thinking="off")
            assert ctrl3._ensure_phone_chat()._plugin.thinking == "high"
            ctrl3._set_target("laptop")
            ok_t, _ = ctrl3._set_settings({"target": "phone", "thinking": "low"})
            assert ok_t and ph3._plugin.thinking == "low" and ai_plugin.thinking == "off"
        finally:
            ctrl3.close()
            laptop._pick(thinking="high")

        # DeepSeek off -> enabled screen_thinking guard prevents mid-answer 400
        seen_bodies: list[dict[str, Any]] = []
        responses = iter([
            {
                "model": "deepseek-flash",
                "choices": [{"message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "screen_map", "arguments": "{}"}}],
                }, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
            {
                "model": "deepseek-flash",
                "choices": [{"message": {"role": "assistant", "content": "Done."}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            },
        ])
        monkeypatch.setattr(ai_assist, "_request", lambda _u, _k, _p, body, *_a, **_kw: seen_bodies.append(body) or next(responses))
        host = SimpleNamespace(specs=[], run=lambda _n, _a: "ok")
        reply = ai_assist.ask(
            "https://api.deepseek.com/v1",
            "sk-test-1234",
            "deepseek-flash",
            [{"role": "user", "content": "click it"}],
            thinking="off",
            screen_thinking="high",
            host=host,
            web=False,
        )
        assert reply.text == "Done."
        assert reply.screen_thinking_used == ""
        assert all(b["thinking"]["type"] == "disabled" for b in seen_bodies)
    finally:
        ctrl.close()
        win.shutdown()
        win.close()


def test_phone_context_laptop_chat_tool_and_capture_safety(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from PyQt6.QtWidgets import QApplication
    from src.tools import ai_sessions
    from src.ui.ai_chat import REMOTE_SYSTEM_NOTE
    from src.ui.pet_remote import RemoteController
    from src.ui.pet_window import PetWindow

    _app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(ai_sessions, "sessions_dir", lambda: tmp_path)

    msgs = ai_assist.chat_messages([{"role": "user", "content": "hi"}], extra_system=REMOTE_SYSTEM_NOTE)
    assert "already connected" in msgs[0]["content"] and "laptop_chat" in msgs[0]["content"]
    plain_msgs = ai_assist.chat_messages([{"role": "user", "content": "hi"}])
    assert "laptop_chat tool" not in plain_msgs[0]["content"]

    sm = PetStateMachine(PetStats())
    ai_plugin = ai_assist.AiAssistPlugin()
    ai_plugin.base_url = "https://example.invalid/v1"
    ai_plugin.model, ai_plugin.thinking, ai_plugin.auto = "deepseek-flash", "high", False
    sm.registry.register(ai_plugin)

    win = PetWindow(state_machine=sm, animation_controller=AnimationController(size=32), auto_wander=False)
    ctrl = RemoteController(win, manage_tailscale=False, port=0)
    try:
        laptop = ctrl._ensure_laptop_chat()
        phone = ctrl._ensure_phone_chat()
        assert laptop is not None

        # Only phone chat exposes laptop_chat tool
        assert any(s["function"]["name"] == "laptop_chat" for s in phone._tool_host.specs)
        assert any(s["function"]["name"] == "laptop_chat" for s in phone._tool_host.all_specs)
        assert not any(s["function"]["name"] == "laptop_chat" for s in laptop._tool_host.specs)
        assert laptop._tool_host.run("laptop_chat", {"action": "read"}).startswith("error:")

        laptop._history.append({"role": "user", "content": "secret-free check"})
        laptop._history.append({"role": "assistant", "content": "all done here"})
        summary = phone._tool_host.run("laptop_chat", {"action": "read"})
        assert "Laptop chat: idle" in summary and "secret-free check" in summary and "all done here" in summary

        # send action asks user approval and delivers via send_remote
        sent: list[str] = []
        monkeypatch.setattr(laptop, "send_remote", lambda msg: sent.append(msg) or (True, "sent"))
        monkeypatch.setattr(phone._tool_host, "_allowed", lambda _k, _t, _w: True)
        res = phone._tool_host.run("laptop_chat", {"action": "send", "message": "hi laptop"})
        assert res == "Laptop chat: sent" and sent == ["hi laptop"]
    finally:
        ctrl.close()
        win.shutdown()
        win.close()
