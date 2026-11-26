"""Zero-polling Clipboard Vault and Instant Developer Text/Hash Transformer plugin."""

from __future__ import annotations

import base64
import binascii
import collections
import hashlib
import json
import re
import urllib.parse
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem

_PRIVACY_MIME_MARKERS: frozenset[str] = frozenset(
    {
        "excludeclipboardcontentfrommonitorprocessing",
        "org.nspasteboard.concealedtype",
        "org.nspasteboard.transienttype",
        "clipboard viewer ignore",
    }
)

_QT_WIN_MIME = re.compile(r'^application/x-qt-windows-mime;value="(.*)"$', re.I)


def _format_name(fmt: str) -> str:
    """A clipboard format's own name: Qt on Windows reports registered ones as application/x-qt-windows-mime;value="..."."""
    found = _QT_WIN_MIME.match(fmt)
    return (found[1] if found else fmt).lower()


def clipboard_is_private(mime: Any) -> bool:
    """The copy carries a password manager's privacy mark (ExcludeClipboardContentFromMonitorProcessing, or
    CanIncludeInClipboardHistory set to 0): no clipboard reader in the app may keep, inspect or send it."""
    if mime is None:
        return False
    for fmt in mime.formats():
        name = _format_name(fmt)
        if name in _PRIVACY_MIME_MARKERS:
            return True
        if name == "canincludeinclipboardhistory" and bytes(mime.data(fmt)) == b"\x00\x00\x00\x00":
            return True
    return False


_BLANK_LINES_RE = re.compile(r"\n{3,}")
_URL_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def decode_jwt(token: str) -> str:
    """Return header and payload of a JWT as pretty JSON (signature is not verified)."""
    parts = token.strip().split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    decoded = []
    for part in parts[:2]:
        raw = base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))
        decoded.append(json.loads(raw))
    return json.dumps({"header": decoded[0], "payload": decoded[1]}, indent=2, ensure_ascii=False)


def toggle_url_encoding(text: str) -> tuple[str, str]:
    if _URL_ESCAPE_RE.search(text):
        return urllib.parse.unquote(text), "URL DEC"
    return urllib.parse.quote(text, safe=""), "URL ENC"


def toggle_timestamp(text: str) -> tuple[str, str]:
    """Epoch seconds (10 digits) or ms (13 digits) to ISO 8601 UTC, or ISO back to epoch."""
    raw = text.strip()
    if raw.isdigit() and len(raw) in (10, 13):
        seconds = int(raw) / (1000 if len(raw) == 13 else 1)
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(), "ISO DATE"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return str(int(parsed.timestamp())), "EPOCH"


def convert_case(text: str, style: str) -> str:
    words = [w.lower() for w in _WORD_RE.findall(text.strip())]
    if not words:
        raise ValueError("no words")
    if style == "camel":
        return words[0] + "".join(w.capitalize() for w in words[1:])
    return ("_" if style == "snake" else "-").join(words)


def toggle_string_escape(text: str) -> tuple[str, str]:
    raw = text.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            value = json.loads(raw)
            if isinstance(value, str):
                return value, "UNESCAPED"
        except json.JSONDecodeError:
            pass
    return json.dumps(text, ensure_ascii=False), "ESCAPED"


@dataclass(frozen=True)
class ClipEntry:
    """Bounded deduplicated in-memory clipboard entry."""

    text: str
    byte_size: int
    digest: bytes

    @property
    def preview(self) -> str:
        collapsed = " ".join(self.text.split())
        if len(collapsed) > 26:
            return f"{collapsed[:23]}..."
        return collapsed


def _format_byte_badge(num_bytes: int) -> str:
    """Format byte count into a <= 4 char HUD bar string."""
    if num_bytes <= 0:
        return "  0B"
    if num_bytes < 1000:
        return f"{num_bytes:3d}B"
    kb = max(1, int(round(num_bytes / 1024.0)))
    return f"{min(99, kb):2d}KB"


class ClipVaultPlugin:
    """Event-driven O(1) Clipboard Ring and instant text/JSON/Base64/SHA-256 utility."""

    MAX_SLOTS: int = 9
    MAX_PINS: int = 3
    MAX_CLIP_BYTES: int = 32_768  # 32 KiB per slot (< 300 KiB worst-case heap)
    SUBMENU_NAME: str = "Clip Vault & Quick Transforms"
    DEV_SUBMENU: str = "Dev Transforms"
    DEV_ITEMS: tuple[tuple[str, str], ...] = (
        ("Decode JWT (no signature check)", "jwt"),
        ("URL Encode / Decode", "url"),
        ("Timestamp <-> ISO Date", "time"),
        ("To camelCase", "camel"),
        ("To snake_case", "snake"),
        ("To kebab-case", "kebab"),
        ("Escape / Unescape String", "escape"),
        ("Copy New UUID", "uuid"),
    )

    def __init__(
        self,
        *,
        read_text: Callable[[], str] | None = None,
        write_text: Callable[[str], None] | None = None,
        read_paths: Callable[[], list[Path]] | None = None,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        self._ring: collections.deque[ClipEntry] = collections.deque(maxlen=self.MAX_SLOTS)
        # Pinned entries survive ring rotation and Clear Vault; RAM-only like the ring
        self._pins: list[ClipEntry] = []
        self._transform_count: int = 0
        self._ignore_digest: bytes | None = None
        self._attached_clipboard: Any = None
        self._pending_clip_change: bool = False
        self._last_clip_seq: int | None = None

        self._read_text_fn = read_text
        self._write_text_fn = write_text
        self._read_paths_fn = read_paths
        self._on_change = on_change

    @property
    def tool_id(self) -> str:
        return "clipvault"

    @property
    def display_name(self) -> str:
        return "Clip Vault"

    @property
    def tick_interval_ms(self) -> int:
        return 5_000

    @property
    def entries(self) -> tuple[ClipEntry, ...]:
        return tuple(self._ring)

    @property
    def pins(self) -> tuple[ClipEntry, ...]:
        return tuple(self._pins)

    @property
    def transform_count(self) -> int:
        return self._transform_count

    def set_on_change(self, callback: Callable[[], None] | None) -> None:
        self._on_change = callback

    def attach_qt_clipboard(self, clipboard: Any | None = None) -> None:
        """Bind to QClipboard.dataChanged for 0% idle CPU event-driven capture."""
        self.close()
        if clipboard is None:
            from PyQt6.QtWidgets import QApplication

            app = QApplication.instance()
            if app is None:
                return
            clipboard = app.clipboard()
        if clipboard is None:
            return
        self._attached_clipboard = clipboard
        clipboard.dataChanged.connect(self._schedule_qt_clipboard_changed)
        # Seed initial item if safe plain text is currently on clipboard
        self._on_qt_clipboard_changed(notify=False)

    def _schedule_qt_clipboard_changed(self) -> None:
        if self._pending_clip_change or self._attached_clipboard is None:
            return
        self._pending_clip_change = True
        try:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, self._flush_qt_clipboard_changed)
        except Exception:
            self._pending_clip_change = False
            self._on_qt_clipboard_changed()

    def _flush_qt_clipboard_changed(self) -> None:
        self._pending_clip_change = False
        if sys.platform == "win32":
            try:
                import ctypes
                seq = ctypes.windll.user32.GetClipboardSequenceNumber()
                if seq and seq == self._last_clip_seq:
                    return
                self._last_clip_seq = seq
            except Exception:
                pass
        self._on_qt_clipboard_changed()

    def _on_qt_clipboard_changed(self, *, notify: bool = True) -> None:
        cb = self._attached_clipboard
        if cb is None:
            return
        try:
            mime = cb.mimeData()
            if mime is None or clipboard_is_private(mime):  # password manager and Windows privacy exclusion formats
                return

            if not mime.hasText():
                return
            text = mime.text()
        except (RuntimeError, OSError):
            return
        if self.capture_text(text) and notify and self._on_change is not None:
            self._on_change()

    def capture_text(
        self,
        text: str,
        *,
        mime_formats: Sequence[str] = (),
    ) -> bool:
        """Validate, deduplicate in O(1), and insert text into the bounded ring."""
        for fmt in mime_formats:
            if _format_name(fmt) in _PRIVACY_MIME_MARKERS:
                return False

        if not isinstance(text, str) or not text or not text.strip():
            return False
        if len(text) > self.MAX_CLIP_BYTES:
            return False

        encoded = text.encode("utf-8", errors="surrogatepass")
        if len(encoded) > self.MAX_CLIP_BYTES:
            return False

        digest = hashlib.blake2b(encoded, digest_size=8).digest()
        if self._ring and self._ring[0].digest == digest:
            return False

        # Deduplicate older matching slot in-place (max 9 items -> O(1))
        for existing in list(self._ring):
            if existing.digest == digest:
                self._ring.remove(existing)
                break

        self._ring.appendleft(
            ClipEntry(text=text, byte_size=len(encoded), digest=digest)
        )
        return True

    def _get_current_text(self) -> str:
        if self._read_text_fn is not None:
            return self._read_text_fn()
        if self._attached_clipboard is not None:
            return str(self._attached_clipboard.text() or "")
        if self._ring:
            return self._ring[0].text
        return ""

    def _set_current_text(self, text: str) -> None:
        encoded = text.encode("utf-8", errors="replace")
        if len(encoded) <= self.MAX_CLIP_BYTES:
            self.capture_text(text)
        if self._write_text_fn is not None:
            self._write_text_fn(text)
            return
        if self._attached_clipboard is not None:
            self._attached_clipboard.setText(text)

    def _get_current_paths(self) -> list[Path]:
        if self._read_paths_fn is not None:
            return list(self._read_paths_fn())
        if self._attached_clipboard is not None:
            mime = self._attached_clipboard.mimeData()
            if mime is not None and mime.hasUrls():
                return [
                    Path(u.toLocalFile())
                    for u in mime.urls()
                    if u.isLocalFile() and u.toLocalFile()
                ]
        return []

    def tick(self) -> bool:
        return False

    def desired_state(self) -> CompanionState | None:
        return None

    def get_hud_data(self) -> HudData:
        count = len(self._ring)
        latest_bytes = self._ring[0].byte_size if self._ring else 0
        slot_pct = (count / self.MAX_SLOTS) * 100.0
        size_pct = min(100.0, (latest_bytes / self.MAX_CLIP_BYTES) * 100.0) if latest_bytes > 0 else 0.0
        tx_pct = min(100.0, float(self._transform_count) * 10.0)

        return HudData(
            badge_left="CLIP.VLT",
            badge_right="LIVE" if count > 0 else "IDLE",
            badge_right_color="#73EFF7" if count > 0 else "#38B764",
            bars=(
                HudBar(
                    label="Slots",
                    value=slot_pct,
                    color_hex="#73EFF7",
                    display_text=f"{count}/{self.MAX_SLOTS}",
                ),
                HudBar(
                    label="Size",
                    value=size_pct,
                    color_hex="#41A6F6",
                    display_text=_format_byte_badge(latest_bytes),
                ),
                HudBar(
                    label="Edits",
                    value=tx_pct,
                    color_hex="#FFCD75",
                    display_text=f"{min(999, self._transform_count):3d}",
                ),
            ),
            detail=f"Latest: {self._ring[0].preview}" if self._ring else "Copy text to fill the vault",
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        sub = self.SUBMENU_NAME
        items: list[ToolMenuItem] = [
            ToolMenuItem(
                label="Format / Minify JSON in Clipboard",
                callback_id="clipvault:json_toggle",
                submenu=sub,
            ),
            ToolMenuItem(
                label="Clean Plain Text (Strip & Trim)",
                callback_id="clipvault:clean_text",
                submenu=sub,
            ),
            ToolMenuItem(
                label="Base64 Encode / Decode",
                callback_id="clipvault:b64_toggle",
                submenu=sub,
            ),
            ToolMenuItem(
                label="Copy SHA-256 Digest (Text or File)",
                callback_id="clipvault:sha256",
                submenu=sub,
            ),
        ]
        items.extend(
            ToolMenuItem(label=label, callback_id=f"clipvault:dev:{key}", submenu=self.DEV_SUBMENU)
            for label, key in self.DEV_ITEMS
        )
        for idx, entry in enumerate(self._ring):
            items.append(
                ToolMenuItem(
                    label=f"Recall #{idx + 1}: {entry.preview}",
                    callback_id=f"clipvault:restore:{idx}",
                    submenu=sub,
                )
            )
        for idx, entry in enumerate(self._pins):
            items.append(
                ToolMenuItem(
                    label=f"Pinned #{idx + 1}: {entry.preview}",
                    callback_id=f"clipvault:pinned:{idx}",
                    submenu=sub,
                )
            )
        items.append(
            ToolMenuItem(
                label=f"Pin Latest Clip ({len(self._pins)}/{self.MAX_PINS})",
                callback_id="clipvault:pin",
                enabled=bool(self._ring) and len(self._pins) < self.MAX_PINS,
                submenu=sub,
            )
        )
        if self._pins:
            items.append(ToolMenuItem(label="Clear Pins", callback_id="clipvault:clear_pins", submenu=sub))
        items.append(
            ToolMenuItem(
                label=f"Clear Vault ({len(self._ring)}/{self.MAX_SLOTS})",
                callback_id="clipvault:clear",
                enabled=bool(self._ring),
                submenu=sub,
            )
        )
        return items

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id == "clipvault:clear":
            self._ring.clear()
            return (True, "CLEARED", "#8A9AF2")

        if callback_id == "clipvault:pin":
            if not self._ring:
                return (True, "EMPTY", "#EF7D57")
            if any(p.digest == self._ring[0].digest for p in self._pins):
                return (True, "PINNED", "#8A9AF2")
            if len(self._pins) >= self.MAX_PINS:
                return (True, "PINS FULL", "#EF7D57")
            self._pins.append(self._ring[0])
            return (True, "PINNED", "#73EFF7")

        if callback_id == "clipvault:clear_pins":
            self._pins.clear()
            return (True, "UNPINNED", "#8A9AF2")

        if callback_id.startswith("clipvault:pinned:"):
            idx_txt = callback_id.removeprefix("clipvault:pinned:")
            if idx_txt.isdigit() and int(idx_txt) < len(self._pins):
                self._set_current_text(self._pins[int(idx_txt)].text)
                return (True, f"PIN #{int(idx_txt) + 1}", "#73EFF7")
            return (True, "EMPTY", "#EF7D57")

        if callback_id.startswith("clipvault:restore:"):
            try:
                idx = int(callback_id.split("clipvault:restore:", 1)[1])
            except ValueError:
                return (False, "", "")
            if 0 <= idx < len(self._ring):
                chosen = self._ring[idx]
                self._set_current_text(chosen.text)
                return (True, f"CLIP #{idx + 1}", "#73EFF7")
            return (True, "EMPTY", "#EF7D57")

        if callback_id.startswith("clipvault:dev:"):
            return self._run_dev_transform(callback_id.removeprefix("clipvault:dev:"))

        if callback_id == "clipvault:json_toggle":
            raw = self._get_current_text().strip()
            if not raw:
                return (True, "NO TEXT", "#EF7D57")
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, RecursionError):  # deeply nested arrays raise RecursionError
                return (True, "NOT JSON", "#EF7D57")
            if "\n" in raw:
                transformed = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
                badge = "JSON MIN"
            else:
                transformed = json.dumps(parsed, indent=2, ensure_ascii=False)
                badge = "JSON FMT"
            self._set_current_text(transformed)
            self._transform_count += 1
            return (True, badge, "#38B764")

        if callback_id == "clipvault:clean_text":
            raw = self._get_current_text()
            if not raw:
                return (True, "NO TEXT", "#EF7D57")
            normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
            lines = [ln.rstrip(" \t") for ln in normalized.split("\n")]
            cleaned = _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()
            self._set_current_text(cleaned)
            self._transform_count += 1
            return (True, "CLEANED", "#38B764")

        if callback_id == "clipvault:b64_toggle":
            raw = self._get_current_text()
            if not raw:
                return (True, "NO TEXT", "#EF7D57")
            compact = "".join(raw.split())
            if len(compact) >= 4 and len(compact) % 4 == 0:
                try:
                    decoded_bytes = base64.b64decode(compact, validate=True)
                    decoded_str = decoded_bytes.decode("utf-8")
                    if all(ch.isprintable() or ch in "\r\n\t" for ch in decoded_str):
                        self._set_current_text(decoded_str)
                        self._transform_count += 1
                        return (True, "B64 DEC", "#73EFF7")
                except (binascii.Error, UnicodeDecodeError, ValueError):
                    pass
            encoded_str = base64.b64encode(raw.encode("utf-8", errors="replace")).decode("ascii")
            self._set_current_text(encoded_str)
            self._transform_count += 1
            return (True, "B64 ENC", "#73EFF7")

        if callback_id == "clipvault:sha256":
            paths = self._get_current_paths()
            target_file: Path | None = None
            try:
                if paths and paths[0].is_file():
                    target_file = paths[0]
            except OSError:
                target_file = None
            if target_file is None:
                raw_txt = self._get_current_text()
                candidate_str = raw_txt.strip().strip('"')
                if candidate_str and len(candidate_str) <= 1024:
                    try:
                        p = Path(candidate_str)
                        if p.is_file():
                            target_file = p
                    except OSError:
                        pass

            if target_file is not None:
                try:
                    with target_file.open("rb") as fin:
                        digest_hex = hashlib.file_digest(fin, "sha256").hexdigest()
                    self._set_current_text(digest_hex)
                    self._transform_count += 1
                    return (True, "FILE SHA", "#FFCD75")
                except OSError:
                    return (True, "HASH ERR", "#EF7D57")

            raw = self._get_current_text()
            if not raw:
                return (True, "EMPTY", "#EF7D57")
            digest_hex = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()
            self._set_current_text(digest_hex)
            self._transform_count += 1
            return (True, "SHA256!", "#FFCD75")

        return (False, "", "")

    def _run_dev_transform(self, key: str) -> tuple[bool, str, str]:
        if key == "uuid":
            self._set_current_text(str(uuid.uuid4()))
            self._transform_count += 1
            return (True, "UUID", "#73EFF7")
        raw = self._get_current_text()
        if not raw.strip():
            return (True, "NO TEXT", "#EF7D57")
        try:
            if key == "jwt":
                result, badge = decode_jwt(raw), "JWT"
            elif key == "url":
                result, badge = toggle_url_encoding(raw)
            elif key == "time":
                result, badge = toggle_timestamp(raw)
            elif key in ("camel", "snake", "kebab"):
                result, badge = convert_case(raw, key), key.upper()
            elif key == "escape":
                result, badge = toggle_string_escape(raw)
            else:
                return (False, "", "")
        except (ValueError, binascii.Error, OverflowError, OSError, RecursionError):
            return (True, "NO MATCH", "#EF7D57")
        self._set_current_text(result)
        self._transform_count += 1
        return (True, badge, "#38B764")

    def close(self) -> None:
        if self._attached_clipboard is not None:
            try:
                self._attached_clipboard.dataChanged.disconnect(self._on_qt_clipboard_changed)
            except (TypeError, RuntimeError):
                pass
            self._attached_clipboard = None

    def to_dict(self) -> dict[str, Any]:
        # Raw clipboard snippets are intentionally kept in RAM only to protect sensitive data
        return {
            "transform_count": self._transform_count,
        }

    def load_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        try:
            self._transform_count = max(0, int(data.get("transform_count", 0)))
        except (TypeError, ValueError):
            pass
