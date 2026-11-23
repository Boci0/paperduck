"""Saved logins: encrypted at rest, typed only into the exact https host that owns the focused box, never shown."""

from __future__ import annotations

import json
import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest

from src.tools import login_vault as lv

win = pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows only")


@win
def test_vault_is_encrypted_round_trips_and_refuses_to_overwrite_a_damaged_file(tmp_path):
    path = tmp_path / "logins.bin"
    lv.store("identity.unimas.my", "100360", "hunter2-Secret", path)
    raw = path.read_bytes()
    assert b"hunter2" not in raw and b"100360" not in raw and b"unimas" not in raw
    assert lv.load(path) == {"identity.unimas.my": {"username": "100360", "password": "hunter2-Secret"}}
    assert lv.forget("identity.unimas.my", path) and lv.load(path) == {}
    path.write_bytes(b"not a dpapi blob")
    with pytest.raises(OSError):
        lv.store("x.example", "", "pw", path)  # never replaces logins it could not read
    assert path.read_bytes() == b"not a dpapi blob"


def test_site_of_takes_only_the_exact_https_host():
    assert lv.site_of("https://Identity.UNIMAS.my./oauth/authorize?x=1") == "identity.unimas.my"
    for bad in ("http://identity.unimas.my/", "data:text/html,<input type=password>", "file:///C:/login.html",
                "https://user:pw@identity.unimas.my/", "about:blank", "", "https://"):
        assert lv.site_of(bad) == "", bad


@pytest.mark.parametrize("line, want", [
    ("42\tControlType.Edit\tTrue\tTrue\t1\thttps://identity.unimas.my/login", ("identity.unimas.my", True)),
    ("42\tControlType.Edit\tFalse\tTrue\t1\thttps://identity.unimas.my/login", ("identity.unimas.my", False)),
    ("41\tControlType.Edit\tTrue\tTrue\t1\thttps://identity.unimas.my/login", "not in the front browser"),
    ("42\tControlType.Button\tFalse\tTrue\t1\thttps://identity.unimas.my/login", "click the username"),
    ("42\tControlType.Edit\tTrue\tFalse\t1\thttps://identity.unimas.my/login", "click the username"),
    ("42\tControlType.Edit\tFalse\tTrue\t0\t", "not inside a web page"),  # the address bar
    ("42\tControlType.Edit\tTrue\tTrue\t1\thttp://identity.unimas.my/login", "not https"),
    ("", "no answer"),
])
def test_parse_focus_checks_the_box_before_any_value_is_touched(line, want):
    got = lv.parse_focus(line, 42, 7)
    if isinstance(want, tuple):
        assert isinstance(got, lv.Field) and (got.site, got.password, got.hwnd) == (*want, 7)
    else:
        assert isinstance(got, str) and got.startswith("error") and want in got


class _Chat:
    def __init__(self, login: tuple[str, str] | None = None, confirm: bool = True) -> None:
        self.login, self.confirm_answer = login, confirm
        self.asked: list[str] = []
        self.confirms: list[tuple[str, Any]] = []
        self._allowed: set[str] = set()
        self._plugin = SimpleNamespace(auto=False)

    def ask_login(self, site: str, done) -> None:
        self.asked.append(site)
        done(self.login)

    def confirm(self, key, text, what, done, review=None, **_kw) -> None:
        self.confirms.append((text, review))
        done(self.confirm_answer)


def _host(monkeypatch, tmp_path, chat: _Chat, fields: list[Any], allowed: bool = True):
    from src.ui import ai_tool_host

    typed: list[str] = []
    monkeypatch.setattr(lv, "vault_path", lambda: tmp_path / "logins.bin")
    monkeypatch.setattr(lv, "focused_field", lambda: fields.pop(0) if len(fields) > 1 else fields[0])
    monkeypatch.setattr(ai_tool_host.computer, "focus_front_app", lambda: 0)
    monkeypatch.setattr(ai_tool_host.computer, "type_text", lambda text, stop=None: typed.append(text) or len(text))
    reviews: list[str] = []

    def review(key, text, what, risk=""):  # stands in for the reviewer / Allow card
        reviews.append(text)
        return allowed

    host = SimpleNamespace(chat=chat, cancel=threading.Event(), _waiting=_wait, _allowed=review)
    return host, typed, reviews


def _wait(ask):
    box: list[Any] = []
    ask(box.append)
    return box[0] if box else None


def _run(host, args):
    from src.ui.ai_tool_host import AiToolHost

    return AiToolHost._type_login(host, args)


PW = lv.Field("identity.unimas.my", "https://identity.unimas.my/login", True, 7)
USER = lv.Field("identity.unimas.my", "https://identity.unimas.my/login", False, 7)


@win
def test_first_use_asks_on_a_card_saves_encrypted_and_types_without_a_second_approval(monkeypatch, tmp_path):
    chat = _Chat(login=("100360", "hunter2-Secret"))
    host, typed, reviews = _host(monkeypatch, tmp_path, chat, [PW])
    out = _run(host, {"field": "password"})
    assert out == "typed the saved password for identity.unimas.my"
    assert "hunter2" not in out and typed == ["hunter2-Secret"] and chat.asked == ["identity.unimas.my"]
    assert reviews == []  # the user just typed it on the card for this site: that is the approval
    assert lv.load(tmp_path / "logins.bin")["identity.unimas.my"]["password"] == "hunter2-Secret"

    # later uses go through the reviewer / approval, and the review text never holds the value
    host2, typed2, reviews2 = _host(monkeypatch, tmp_path, _Chat(), [USER])
    assert _run(host2, {"field": "username"}) == "typed the saved username for identity.unimas.my"
    assert typed2 == ["100360"] and len(reviews2) == 1 and "100360" not in reviews2[0] and "hunter2" not in reviews2[0]


@win
def test_declines_wrong_boxes_moved_focus_and_quirky_password_boxes(monkeypatch, tmp_path):
    lv.store("identity.unimas.my", "100360", "hunter2-Secret", tmp_path / "logins.bin")

    host, typed, _ = _host(monkeypatch, tmp_path, _Chat(), [PW])
    assert "password box" in _run(host, {"field": "username"}) and typed == []

    host, typed, _ = _host(monkeypatch, tmp_path, _Chat(), [PW], allowed=False)
    assert "declined" in _run(host, {"field": "password"}) and typed == []

    moved = lv.Field("evil.example", "https://evil.example/", True, 7)
    host, typed, _ = _host(monkeypatch, tmp_path, _Chat(), [PW, moved])
    assert "focus moved" in _run(host, {"field": "password"}) and typed == []

    # a page that styles a text box as a password box: the user always decides, even in auto mode
    chat = _Chat(confirm=False)
    chat._plugin.auto = True
    host, typed, _ = _host(monkeypatch, tmp_path, chat, [USER])
    assert "declined" in _run(host, {"field": "password"}) and typed == []
    assert chat.confirms and chat.confirms[0][1][0] is False

    other = lv.Field("other.example", "https://other.example/", True, 7)
    chat = _Chat(login=None)
    host, typed, _ = _host(monkeypatch, tmp_path, chat, [other])
    assert "did not save" in _run(host, {"field": "password"}) and typed == [] and chat.asked == ["other.example"]


def test_saved_login_approval_is_never_remembered_for_the_chat():
    from src.ui.ai_chat import NEVER_REMEMBERED

    assert "login" in NEVER_REMEMBERED


def test_phone_login_endpoint_needs_auth_validates_and_never_echoes_the_value() -> None:
    import dataclasses
    import json

    from src.tools.phone_bridge import PhoneBridge
    from tests.test_phone_bridge import _dummy_handlers, _req

    got: list[Any] = []
    handlers = dataclasses.replace(_dummy_handlers({"approvals": []}, []),
                                   resolve_login=lambda lid, u, p: got.append((lid, u, p)) or lid == "l1")
    bridge = PhoneBridge(handlers, port=0, token="sec_token_1234567890", manage_tailscale=False)
    ok, base = bridge.start()
    assert ok
    try:
        post = {"Authorization": "Bearer sec_token_1234567890", "Content-Type": "application/json"}
        body = json.dumps({"id": "l1", "username": "100360", "password": "hunter2-Secret"}).encode()
        assert _req(base + "/api/login", method="POST", body=body, headers={"Content-Type": "application/json"})[0] == 401
        code, _, raw = _req(base + "/api/login", method="POST", body=body, headers=post)
        assert code == 200 and b"hunter2" not in raw and got == [("l1", "100360", "hunter2-Secret")]
        assert _req(base + "/api/login", method="POST", body=json.dumps({"id": "l1", "password": None}).encode(),
                    headers=post)[0] == 200 and got[-1] == ("l1", None, None)
        for bad in ({"id": "l1", "password": ""}, {"id": "l1", "password": "x" * 513}, {"id": 5, "password": "x"},
                    {"id": "l1", "username": 3, "password": "x"}):
            assert _req(base + "/api/login", method="POST", body=json.dumps(bad).encode(), headers=post)[0] == 400
        assert _req(base + "/api/login", method="POST", body=json.dumps({"id": "nope", "password": "x"}).encode(),
                    headers=post)[0] == 404
    finally:
        bridge.stop()


def test_chat_login_card_keeps_values_out_of_the_snapshot_and_clears_its_boxes() -> None:
    from PyQt6.QtWidgets import QApplication, QLineEdit

    from src.tools.ai_assist import AiAssistPlugin
    from src.ui.ai_chat import AiChat

    app = QApplication.instance() or QApplication([])
    chat = AiChat(AiAssistPlugin())
    got: list[Any] = []
    chat.ask_login("identity.unimas.my", got.append)
    app.processEvents()
    entry = chat._remote_approvals[-1]
    assert entry["kind"] == "login" and entry["site"] == "identity.unimas.my" and entry["choices"] == []
    boxes = [b for b in chat.findChildren(QLineEdit) if b.placeholderText() in ("Username (optional)", "Password")]
    user, pw = sorted(boxes, key=lambda b: b.placeholderText() != "Username (optional)")
    assert pw.echoMode() == QLineEdit.EchoMode.Password
    user.setText("100360")
    pw.setText("hunter2-Secret")
    assert "hunter2" not in json.dumps(chat._remote_approvals)
    assert chat.resolve_approval(entry["id"], "once") is False  # an Allow cannot stand in for the login
    assert chat.resolve_login(entry["id"], "100360", "hunter2-Secret") is True
    assert got == [("100360", "hunter2-Secret")] and pw.text() == "" and user.text() == ""
    assert all(a["id"] != entry["id"] for a in chat._remote_approvals)
    assert chat.resolve_login(entry["id"], "x", "y") is False  # answered once only

    # the saved-login approval card offers Allow once and Deny, never Allow for this chat
    answers: list[Any] = []
    chat.confirm("login", "Type saved password", "saved password", answers.append)
    app.processEvents()
    assert chat._remote_approvals[-1]["choices"] == ["once"]
    chat.resolve_approval(chat._remote_approvals[-1]["id"], "once")
    assert answers == [True] and "login" not in chat._allowed
