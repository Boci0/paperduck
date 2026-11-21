"""Offline context hint rules, clipboard change detection, cooldowns and mutes."""

import base64

from src.tools.hints import FOCUS_DWELL_S, HintsPlugin, suggest

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.sig"


def test_rules_match_their_inputs():
    assert suggest("chrome.exe", JWT, 0, True).action == "clipvault:dev:jwt"
    assert suggest("chrome.exe", "1793286902", 0, True).action == "clipvault:dev:time"
    assert suggest("chrome.exe", "1793286902123", 0, True).action == "clipvault:dev:time"
    assert suggest("chrome.exe", '{"a": 1, "b": [1, 2, 3], "c": "some value here"}', 0, True).rule == "json"
    assert suggest("Code.exe", "", FOCUS_DWELL_S, True).action == "pet:focus"


def test_rules_stay_quiet_on_lookalikes():
    assert suggest("chrome.exe", "eyJx.eyJy.z", 0, True) is None  # JWT shape, not decodable
    assert suggest("chrome.exe", "0123456789", 0, True) is None  # 10 digits, not a 2000..2100 epoch
    assert suggest("chrome.exe", '{"a": 1, "b": 2}', 0, True) is None  # short JSON
    assert suggest("chrome.exe", '{\n "a": 1, "b": 2, "c": "already pretty printed"\n}', 0, True) is None
    assert suggest("code.exe", "", FOCUS_DWELL_S - 1, True) is None
    assert suggest("code.exe", "", FOCUS_DWELL_S, False) is None  # Pomodoro already running
    assert suggest("notepad.exe", "", FOCUS_DWELL_S, True) is None


def test_same_clip_offered_once_and_cooldown():
    hints = HintsPlugin()
    assert hints.poll("chrome.exe", JWT, True, now=0).rule == "jwt"
    assert hints.poll("chrome.exe", JWT, True, now=500) is None  # unchanged clipboard
    assert hints.poll("chrome.exe", JWT + "x", True, now=10) is None  # cooldown
    assert hints.poll("chrome.exe", JWT, True, now=200).rule == "jwt"  # changed again, cooldown over
    assert hints.shown == 2


def test_private_app_copy_never_offered():
    hints = HintsPlugin()
    assert hints.poll("KeePassXC.exe", JWT, True, now=0) is None
    assert hints.poll("chrome.exe", JWT, True, now=5) is None


def test_focus_dwell_mute_and_toggle_persist():
    hints = HintsPlugin()
    hints.poll("code.exe", "", True, now=0)
    assert hints.poll("code.exe", "", True, now=FOCUS_DWELL_S).rule == "focus"
    assert hints.poll("code.exe", "", True, now=FOCUS_DWELL_S * 2) is None  # 30 min cooldown
    hints.dismiss("focus")
    assert hints.poll("code.exe", "", True, now=FOCUS_DWELL_S * 10) is None
    assert hints.execute_action("hints:toggle") == (True, "HINTS OFF", "#73EFF7")
    restored = HintsPlugin()
    restored.load_dict(hints.to_dict())
    assert not restored.enabled
    assert restored.poll("chrome.exe", JWT, True, now=0) is None


def test_hostile_clipboard_text_never_raises():
    """The 3s hint poll is a Qt timer slot: an exception there aborts the app."""
    nested = "[" * 3000 + "]" * 3000
    deep = base64.urlsafe_b64encode(b'{"a":' + nested.encode() + b"}").decode().rstrip("=")
    for clip in ("²" * 10, nested, f"eyJhbGciOiJIUzI1NiJ9.{deep}.sig"):
        assert suggest("code.exe", clip, 0.0, True) is None


def test_hints_tracks_bounded_history_and_recall_menu():
    hints = HintsPlugin()
    hints.poll("code.exe", "", True, now=0.0)
    for i in range(6):
        hints._next_ok.clear()
        hints.poll("chrome.exe", JWT if i % 2 == 0 else "1793286902", True, now=float((i + 1) * 100))
    assert len(hints.history) == 5
    items = hints.get_menu_items()
    recall = [it for it in items if it.callback_id == "hints:last"]
    assert len(recall) == 1 and "Recall Last Hint:" in recall[0].label
