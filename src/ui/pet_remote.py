"""Qt-thread controller and pairing dialog for the Tailscale mobile web bridge."""

from __future__ import annotations

import base64
import re
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.tools import computer
from src.tools.ai_assist import THINKING_LEVELS, AiAssistPlugin, valid_model
from src.tools.ai_tools import OFFICE_EXTS, _office_text, _pdf_text, scratch_dir
from src.tools.phone_bridge import (
    DEFAULT_PORT,
    BridgeHandlers,
    PhoneBridge,
    RemoteAttachment,
    sanitize_attachment_name,
)
from src.tools.qr import qr_matrix
from src.tools.web_drop import sniff_image_format
from src.ui.ai_chat import AiChat
from src.ui.ai_tool_host import AiToolHost, _ours, show_quietly
from src.ui.chat_widgets import INLINE_FILE_CHARS, Attachment, _size_text, image_data_url
from src.ui.quick_tools import ocr_image_file
from src.ui.theme import (
    ACCENT_HEX,
    APP_STYLESHEET,
    DANGER_HEX,
    FONT_FAMILY,
    GLASS_RAISED_RGBA,
    GLASS_RGBA,
    HOVER_RGBA,
    OUTLINE_RGBA,
    TEXT_DIM_HEX,
    TEXT_HEX,
    rgba_css,
    ui_font,
)

if TYPE_CHECKING:
    from src.ui.pet_window import PetWindow

SCREEN_CACHE_S = 0.75
MAX_STAGED_UPLOADS = 40
STAGED_UPLOAD_MAX_AGE_S = 24 * 3600


def _encode_image(image: Any, max_w: int, fmt: str = "png", quality: int = 78) -> tuple[bytes | None, str]:
    if image is None or image.isNull():
        return None, ""
    shot = (
        image.scaledToWidth(max_w, Qt.TransformationMode.SmoothTransformation)
        if image.width() > max_w > 0
        else image
    )
    is_jpeg = fmt.lower() in ("jpg", "jpeg")
    qt_fmt = "JPG" if is_jpeg else "PNG"
    mime = "image/jpeg" if is_jpeg else "image/png"
    ba = QByteArray()
    buf = QBuffer(ba)
    if not buf.open(QIODevice.OpenModeFlag.WriteOnly):
        return None, ""
    try:
        saved = shot.save(buf, qt_fmt, max(40, min(95, quality))) if is_jpeg else shot.save(buf, qt_fmt)
        if not saved:
            return None, ""
        return bytes(ba.data()), mime
    finally:
        buf.close()


def _encode_png(image: Any, max_w: int) -> bytes | None:
    data, _mime = _encode_image(image, max_w, fmt="png")
    return data


def _ordered_screens() -> list[Any]:
    screens = list(QGuiApplication.screens() or [])
    primary = QGuiApplication.primaryScreen()
    if primary is not None and primary in screens:
        screens.remove(primary)
        screens.insert(0, primary)
    return screens


def _list_monitors() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for idx, scr in enumerate(_ordered_screens()):
        geo = scr.geometry()
        out.append({
            "index": idx,
            "name": scr.name() or f"Display {idx + 1}",
            "width": int(geo.width()),
            "height": int(geo.height()),
            "primary": idx == 0,
        })
    return out


_CAPTURE_IN_FLIGHT: bool = False
_CAPTURE_WAITERS: list[tuple[Any, int, Callable[[Any], None]]] = []


def _capture_monitor(win: Any, monitor_index: int, done: Callable[[Any], None]) -> None:
    """Capture a monitor on the Qt thread without stealing foreground window focus on the PC."""
    global _CAPTURE_IN_FLIGHT
    if _CAPTURE_IN_FLIGHT:
        _CAPTURE_WAITERS.append((win, monitor_index, done))
        return
    _CAPTURE_IN_FLIGHT = True

    shown = _ours(win)
    hidden: list[Any] = []
    for w in shown:
        try:
            wid = int(w.winId())
            if not computer.exclude_from_capture(wid, True):
                w.hide()
                hidden.append(w)
        except RuntimeError:
            pass

    def grab() -> None:
        global _CAPTURE_IN_FLIGHT
        image = None
        try:
            screens = _ordered_screens()
            if screens:
                idx = max(0, min(monitor_index, len(screens) - 1))
                image = screens[idx].grabWindow(0).toImage()
        finally:
            for w in shown:
                try:
                    if w in hidden:
                        show_quietly(w)
                    else:
                        computer.exclude_from_capture(int(w.winId()), False)
                except RuntimeError:
                    pass
            _CAPTURE_IN_FLIGHT = False
            try:
                done(image)
            finally:
                if _CAPTURE_WAITERS:
                    next_win, next_idx, next_done = _CAPTURE_WAITERS.pop(0)
                    QTimer.singleShot(0, lambda nw=next_win, ni=next_idx, nd=next_done: _capture_monitor(nw, ni, nd))

    if not shown:
        grab()
    else:
        QTimer.singleShot(180 if hidden else 30, grab)


class _QrWidget(QWidget):
    """Paints a crisp black-on-white scannable QR code with a 4-module quiet zone."""

    def __init__(self, size: int = 168) -> None:
        super().__init__()
        self._matrix: list[list[bool]] = []
        self.setFixedSize(size, size)

    def set_text(self, text: str) -> None:
        try:
            self._matrix = qr_matrix(text) if text else []
        except ValueError:
            self._matrix = []
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#FFFFFF"))
        if not self._matrix:
            painter.setPen(QColor("#666666"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Bridge\nStopped")
            painter.end()
            return
        n = len(self._matrix)
        pad = 4
        cells = n + 2 * pad
        step = min(self.width(), self.height()) // cells
        if step < 1:
            painter.end()
            return
        total = step * cells
        ox = (self.width() - total) // 2
        oy = (self.height() - total) // 2
        dark = QColor("#000000")
        for r, row in enumerate(self._matrix):
            for c, on in enumerate(row):
                if on:
                    painter.fillRect(ox + (c + pad) * step, oy + (r + pad) * step, step, step, dark)
        painter.end()


class _PairingDialog(QDialog):
    """Shows QR code, pairing URL, step-by-step phone instructions, and bridge controls."""

    def __init__(self, controller: RemoteController, parent: PetWindow | None = None) -> None:
        super().__init__(parent)
        self._ctrl = controller
        self.setWindowTitle("Paperduck - Phone Control (Tailscale)")
        self.setMinimumWidth(560)
        self.setStyleSheet(
            APP_STYLESHEET
            + f"""
            QDialog {{
                background-color: {rgba_css(GLASS_RGBA)};
                color: {TEXT_HEX};
                font-family: "{FONT_FAMILY}";
            }}
            QLabel {{ color: {TEXT_HEX}; background: transparent; }}
            QLineEdit {{
                background: {rgba_css(GLASS_RAISED_RGBA)};
                color: {TEXT_HEX};
                border: 1px solid {rgba_css(OUTLINE_RGBA)};
                border-radius: 6px;
                padding: 6px 8px;
                font-family: Consolas, monospace;
            }}
            QPushButton {{
                background: {rgba_css(GLASS_RAISED_RGBA)};
                color: {TEXT_HEX};
                border: 1px solid {rgba_css(OUTLINE_RGBA)};
                border-radius: 6px;
                padding: 6px 14px;
                font-weight: 600;
            }}
            QPushButton:hover {{
                background: {rgba_css(HOVER_RGBA)};
                color: {ACCENT_HEX};
                border-color: {ACCENT_HEX};
            }}
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        self._status = QLabel()
        self._status.setWordWrap(True)
        self._status.setFont(ui_font(10.0, bold=True))
        root.addWidget(self._status)

        hero = QHBoxLayout()
        hero.setSpacing(14)
        self._qr = _QrWidget(176)
        hero.addWidget(self._qr, 0, Qt.AlignmentFlag.AlignTop)

        guide_col = QVBoxLayout()
        guide_col.setSpacing(6)
        steps_head = QLabel("How to connect your phone:")
        steps_head.setFont(ui_font(9.5, bold=True))
        steps = QLabel(
            "1. Turn on Tailscale on your phone.\n"
            "2. Point your phone camera at the QR code (or tap Copy Link and open it in your phone browser).\n"
            "3. Send a task, steer mid-run, tap Allow/Deny on prompts, or open Screen to watch your PC.\n\n"
            "Keep Windows unlocked if Paperduck needs to see or click GUI windows."
        )
        steps.setWordWrap(True)
        steps.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        guide_col.addWidget(steps_head)
        guide_col.addWidget(steps)
        guide_col.addStretch(1)
        hero.addLayout(guide_col, 1)
        root.addLayout(hero)

        url_row = QHBoxLayout()
        self._url_edit = QLineEdit()
        self._url_edit.setReadOnly(True)
        self._url_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._show_url_btn = QPushButton("Show")
        self._show_url_btn.clicked.connect(self._toggle_url_visibility)
        self._copy_url = QPushButton("Copy Link")
        self._copy_url.setStyleSheet(
            f"QPushButton {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX}; "
            f"border: 1.5px solid {ACCENT_HEX}; border-radius: 6px; padding: 6px 14px; font-weight: 600; }}"
            f"QPushButton:hover {{ background: {rgba_css(HOVER_RGBA)}; color: {ACCENT_HEX}; }}"
        )
        self._copy_url.clicked.connect(lambda _c=False: self._copy(self._url_edit.text(), "Pairing link copied."))
        url_row.addWidget(self._url_edit, 1)
        url_row.addWidget(self._show_url_btn)
        url_row.addWidget(self._copy_url)
        root.addLayout(url_row)

        tok_row = QHBoxLayout()
        tok_label = QLabel("Token:")
        tok_label.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        self._tok_edit = QLineEdit()
        self._tok_edit.setReadOnly(True)
        self._tok_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._show_tok_btn = QPushButton("Show")
        self._show_tok_btn.clicked.connect(self._toggle_token_visibility)
        self._copy_tok = QPushButton("Copy Token")
        self._copy_tok.clicked.connect(lambda _c=False: self._copy(self._ctrl.bridge.token, "Token copied."))
        tok_row.addWidget(tok_label)
        tok_row.addWidget(self._tok_edit, 1)
        tok_row.addWidget(self._show_tok_btn)
        tok_row.addWidget(self._copy_tok)
        root.addLayout(tok_row)

        self._note = QLabel()
        self._note.setWordWrap(True)
        self._note.setStyleSheet(f"color: {TEXT_DIM_HEX}; font-size: 8.5pt;")
        root.addWidget(self._note)

        actions = QHBoxLayout()
        self._toggle_btn = QPushButton()
        self._toggle_btn.clicked.connect(self._on_toggle)
        self._rotate_btn = QPushButton("Rotate Token")
        self._rotate_btn.clicked.connect(self._on_rotate)
        self._close_btn = QPushButton("Close")
        self._close_btn.clicked.connect(self.accept)
        actions.addWidget(self._toggle_btn)
        actions.addWidget(self._rotate_btn)
        actions.addStretch(1)
        actions.addWidget(self._close_btn)
        root.addLayout(actions)

        self._flash_note = ""
        self._last_running: bool | None = None
        self._ctrl.bridge_state_changed.connect(self._refresh)
        self._refresh()

    def _toggle_url_visibility(self, _checked: bool = False) -> None:
        hidden = self._url_edit.echoMode() == QLineEdit.EchoMode.Password
        self._url_edit.setEchoMode(QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        self._show_url_btn.setText("Hide" if hidden else "Show")

    def _toggle_token_visibility(self, _checked: bool = False) -> None:
        hidden = self._tok_edit.echoMode() == QLineEdit.EchoMode.Password
        self._tok_edit.setEchoMode(QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password)
        self._show_tok_btn.setText("Hide" if hidden else "Show")

    def _copy(self, text: str, note: str) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None and text:
            cb.setText(text)
            self._flash_note = note
            self._note.setText(note)

    def _on_toggle(self, _checked: bool = False) -> None:
        self._flash_note = ""
        self._ctrl.toggle_async()
        self._refresh()

    def _on_rotate(self, _checked: bool = False) -> None:
        self._ctrl.rotate_token()
        self._flash_note = "Pairing token rotated; older phone sessions were signed out."
        self._refresh()

    def _refresh(self) -> None:
        b = self._ctrl.bridge
        if self._last_running is not None and b.running != self._last_running:
            self._flash_note = ""
        self._last_running = b.running
        busy = self._ctrl.transitioning
        self._toggle_btn.setEnabled(not busy)
        self._rotate_btn.setEnabled(not busy)
        if busy:
            self._status.setText("Stopping bridge..." if b.running else "Starting bridge...")
            self._status.setStyleSheet(f"color: {TEXT_DIM_HEX};")
            self._toggle_btn.setText("Working...")
            self._copy_url.setEnabled(False)
            return
        if b.running:
            host = urlsplit(b.public_url).hostname or ""
            is_raw_ip = bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host))
            mode_label = {
                "tailscale_serve": "Tailscale HTTPS",
                "tailscale_ip": "Tailscale Private" if is_raw_ip else "Tailscale MagicDNS",
                "loopback": "Localhost Only",
            }.get(b.mode, "Listening")
            shown_dest = "private link ready (use QR or Copy Link)" if is_raw_ip else (host or b.public_url)
            self._status.setText(f"Active ({mode_label}): {shown_dest}")
            self._status.setStyleSheet(f"color: {ACCENT_HEX};")
            self._url_edit.setText(b.pair_url)
            self._url_edit.setCursorPosition(0)
            self._tok_edit.setText(b.token)
            self._qr.set_text(b.pair_url)
            self._copy_url.setEnabled(True)
            self._toggle_btn.setText("Stop Bridge")
            if self._flash_note:
                self._note.setText(self._flash_note)
            elif b.last_error:
                self._note.setText(b.last_error)
            else:
                self._note.setText(b.status_note or "")
        else:
            msg = b.last_error or "Phone Control is stopped."
            self._status.setText(msg)
            self._status.setStyleSheet(f"color: {DANGER_HEX if b.last_error else TEXT_DIM_HEX};")
            self._url_edit.setText("")
            self._tok_edit.setText(b.token)
            self._qr.set_text("")
            self._copy_url.setEnabled(False)
            self._toggle_btn.setText("Start Bridge")
            self._note.setText(self._flash_note)


def _prune_staged_uploads(root: Path) -> None:
    now = time.time()
    try:
        files = sorted((p for p in root.iterdir() if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for idx, path in enumerate(files):
        try:
            if idx >= MAX_STAGED_UPLOADS or (now - path.stat().st_mtime) > STAGED_UPLOAD_MAX_AGE_S:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _stage_upload_file(name: str, data: bytes) -> Path:
    """Persist an oversized or binary phone upload into a bounded scratch subdirectory without overwriting."""
    root = scratch_dir() / "remote_uploads"
    root.mkdir(parents=True, exist_ok=True)
    safe = sanitize_attachment_name(name)
    stem, dot, ext = safe.rpartition(".")
    suffix = f".{ext}" if dot else ""
    base = stem if dot else safe
    dest = root / f"{base[:60]}_{uuid.uuid4().hex[:8]}{suffix}"
    dest.write_bytes(data)
    _prune_staged_uploads(root)
    return dest


def _prepare_attachment(item: RemoteAttachment) -> tuple[Attachment | None, str]:
    """Decode, strip metadata / OCR images or extract text from Office/PDF/text uploads off the GUI thread."""
    safe_name = sanitize_attachment_name(item.name)
    if sniff_image_format(item.data[:16]) is not None:
        qimg = QImage.fromData(item.data)
        if qimg.isNull():
            return None, f"Unreadable image: {safe_name}"
        data_url = image_data_url(qimg)
        png_bytes = base64.b64decode(data_url.split(",", 1)[1])
        img_name = f"{Path(safe_name).stem or 'image'}.png"
        dest = _stage_upload_file(img_name, png_bytes)
        ocr_text = ocr_image_file(dest).strip()
        words = len(ocr_text.split())
        detail = f"{words} words read" if words else "Image, no text found"
        return Attachment("image", safe_name, text=ocr_text, detail=detail,
                          image_url=data_url, saved_path=str(dest)), ""

    ext = Path(safe_name).suffix.lower()
    dest = _stage_upload_file(safe_name, item.data)
    extracted: str | None = None
    if ext in OFFICE_EXTS or ext == ".pdf" or item.data[:5] == b"%PDF-":
        try:
            if ext in OFFICE_EXTS:
                extracted = _office_text(dest)
            else:
                extracted = _pdf_text(dest)
        except (ValueError, zipfile.BadZipFile, ET.ParseError, OSError, UnicodeDecodeError):
            extracted = None
    elif b"\x00" not in item.data[:8192]:
        for enc in ("utf-8-sig", "utf-16"):
            try:
                extracted = item.data.decode(enc)
                break
            except UnicodeDecodeError:
                continue

    if extracted is not None:
        lines = extracted.count("\n") + 1
        detail = f"{_size_text(len(item.data))}, {lines} lines"
        if len(extracted) <= INLINE_FILE_CHARS:
            return Attachment("file", safe_name, text=extracted, detail=detail, saved_path=str(dest)), ""
        return Attachment("file", safe_name, text=extracted, detail=detail + ", read on demand", path=str(dest)), ""

    return Attachment("file", safe_name, detail=f"{_size_text(len(item.data))}, uploaded file", path=str(dest)), ""


class RemoteController(QObject):
    """Owns PhoneBridge lifecycle and marshals HTTP callbacks safely onto the Qt GUI thread."""

    _call = pyqtSignal(object)
    bridge_state_changed = pyqtSignal()

    def __init__(self, win: PetWindow, *, manage_tailscale: bool = True, port: int = DEFAULT_PORT) -> None:
        super().__init__(win)
        self._win = win
        self.enabled: bool = False
        self.port: int = port
        self._dialog: _PairingDialog | None = None
        self._screen_lock = threading.Lock()
        self._last_screen: tuple[float, int, int, str, int, bytes, str] | None = None
        self._op_lock = threading.Lock()
        self._transitioning = False
        self._active_target: str = "phone"
        self._phone_plugin = AiAssistPlugin()
        self._phone_chat: AiChat | None = None
        self._phone_overrides: dict[str, Any] = {}
        self._call.connect(self._on_call)
        self.bridge = PhoneBridge(
            BridgeHandlers(
                get_state=self._get_state,
                send_message=self._send_message,
                stop_chat=self._stop_chat,
                new_chat=self._new_chat,
                resolve_approval=self._resolve_approval,
                grab_screen=self._grab_screen,
                send_attachments=self._send_attachments,
                set_settings=self._set_settings,
                set_target=self._set_target,
                refresh_models=self._refresh_models,
                list_sessions=self._list_sessions,
                resume_session=self._resume_session,
                delete_session=self._delete_session,
                stop_proc=self._stop_proc,
                grab_screen_opts=self._grab_screen_opts,
            ),
            port=port,
            manage_tailscale=manage_tailscale,
        )

    @property
    def running(self) -> bool:
        return self.bridge.running

    @property
    def transitioning(self) -> bool:
        return self._transitioning

    def _on_call(self, payload: tuple[Callable[[Callable[[Any], None]], None], dict[str, Any]]) -> None:
        fn, box = payload

        def done(value: Any) -> None:
            box["result"] = value
            box["event"].set()

        if self._win._is_shutting_down:
            done(None)
            return
        try:
            fn(done)
        except Exception as exc:
            box["error"] = exc
            done(None)

    def _on_gui(self, fn: Callable[[Callable[[Any], None]], None], timeout_s: float = 5.0) -> Any:
        if self._win._is_shutting_down:
            return None
        if QThread.currentThread() == self.thread():
            out: list[Any] = []
            fn(out.append)
            return out[0] if out else None
        box: dict[str, Any] = {"event": threading.Event(), "result": None}
        self._call.emit((fn, box))
        if not box["event"].wait(timeout=timeout_s):
            return None
        return box["result"]

    def _sync_phone_plugin(self) -> None:
        laptop = self._win._state_machine.registry.get_plugin("ai")
        if isinstance(laptop, AiAssistPlugin):
            self._phone_plugin.provider = laptop.provider
            self._phone_plugin.base_url = laptop.base_url
            for key in ("model", "thinking", "screen_thinking", "auto"):
                val = self._phone_overrides[key] if key in self._phone_overrides else getattr(laptop, key)
                setattr(self._phone_plugin, key, val)

    def _ensure_laptop_chat(self):
        chat = self._win._ai_chat
        if chat is None:
            plugin = self._win._state_machine.registry.get_plugin("ai")
            if not isinstance(plugin, AiAssistPlugin):
                return None
            self._win._make_ai_chat(plugin)
            chat = self._win._ai_chat
        return chat

    def _ensure_phone_chat(self) -> AiChat:
        self._sync_phone_plugin()
        if self._phone_chat is None:
            host = AiToolHost(self._win)
            chat = AiChat(self._phone_plugin, tool_host=host, remote_only=True)
            chat.remote_changed.connect(self.notify_change)
            self._phone_chat = chat
        return self._phone_chat

    def _active_chat(self):
        return self._ensure_phone_chat() if self._active_target == "phone" else self._ensure_laptop_chat()

    def _existing_active_chat(self):
        return self._phone_chat if self._active_target == "phone" else self._win._ai_chat

    @staticmethod
    def _target_summary(chat: Any) -> dict[str, Any]:
        if chat is None:
            return {"busy": False, "approvals": 0}
        return {
            "busy": bool(getattr(chat, "_job", None) is not None and not getattr(chat, "_testing", False)),
            "approvals": len(getattr(chat, "_remote_approvals", ()) or ()),
        }

    def notify_change(self, *_args: object) -> None:
        self.bridge.notify_change()

    def _get_state(self) -> dict[str, Any]:
        def read(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            targets = {
                "phone": self._target_summary(self._phone_chat),
                "laptop": self._target_summary(self._win._ai_chat),
            }
            if chat is None:
                done({"busy": False, "ready": False, "model": "", "model_name": "", "thinking": "off",
                      "screen_thinking": "", "supports_screen_thinking": True,
                      "thinking_levels": ["off"], "models": [], "auto_mode": False, "sid": "",
                      "progress": "", "stream_text": "", "tasks": [], "procs": [], "agents": [],
                      "history": [], "history_total": 0, "history_truncated": 0, "approvals": [],
                      "error": "AI plugin unavailable", "monitors": _list_monitors(),
                      "target": self._active_target, "targets": targets,
                      "pet_state": self._win._state_machine.state.name})
                return
            snap = chat.remote_snapshot()
            snap["pet_state"] = self._win._state_machine.state.name
            snap["monitors"] = _list_monitors()
            snap["target"] = self._active_target
            snap["targets"] = targets
            done(snap)

        res = self._on_gui(read)
        return res if isinstance(res, dict) else {"error": "unavailable"}

    def _send_message(self, text: str) -> tuple[bool, str]:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            done(chat.send_remote(text) if chat is not None else (False, "AI chat unavailable"))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _send_attachments(self, text: str, raw_items: list[RemoteAttachment]) -> tuple[bool, str]:
        if not raw_items:
            return self._send_message(text)
        prepared: list[Attachment] = []
        errors: list[str] = []
        for item in raw_items:
            att, err = _prepare_attachment(item)
            if att is not None:
                prepared.append(att)
            elif err:
                errors.append(err)
        if not prepared and not text.strip():
            return False, errors[0] if errors else "unreadable attachment"

        def run(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            done(chat.send_remote(text, prepared) if chat is not None else (False, "AI chat unavailable"))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _stop_chat(self) -> bool:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._existing_active_chat()
            if chat is not None:
                chat.stop()
            done(True)

        return bool(self._on_gui(run))

    def _new_chat(self) -> tuple[bool, str]:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            if chat is None:
                done((False, "AI chat unavailable"))
                return
            if chat._job is not None:
                done((False, "chat is busy; stop first"))
                return
            chat.new_chat()
            done((True, "ok"))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _resolve_approval(self, approval_id: str, choice: str | None) -> bool:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._existing_active_chat()
            done(chat.resolve_approval(approval_id, choice) if chat is not None else False)

        return bool(self._on_gui(run))

    def _set_target(self, target: str) -> tuple[bool, str]:
        if target not in ("phone", "laptop"):
            return False, "invalid target"

        def run(done: Callable[[Any], None]) -> None:
            self._active_target = target
            self._active_chat()
            self.notify_change()
            done((True, target))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _set_settings(self, payload: dict[str, Any]) -> tuple[bool, str]:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            if chat is None:
                done((False, "AI chat unavailable"))
                return
            ok, msg = chat.remote_set_settings(
                model=payload.get("model"),
                thinking=payload.get("thinking"),
                screen_thinking=payload.get("screen_thinking"),
                auto=payload.get("auto"),
            )
            if ok and self._active_target == "phone":
                for key in ("model", "thinking", "screen_thinking", "auto"):
                    if key in payload or key in self._phone_overrides:
                        self._phone_overrides[key] = getattr(self._phone_plugin, key)
            done((ok, msg))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _refresh_models(self) -> tuple[bool, str]:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            done(chat.remote_refresh_models() if chat is not None else (False, "AI chat unavailable"))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _list_sessions(self) -> list[dict[str, Any]]:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            done(chat.remote_list_sessions() if chat is not None else [])

        res = self._on_gui(run)
        return res if isinstance(res, list) else []

    def _resume_session(self, sid: str) -> tuple[bool, str]:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            done(chat.remote_resume_session(sid) if chat is not None else (False, "AI chat unavailable"))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _delete_session(self, sid: str) -> tuple[bool, str]:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._active_chat()
            done(chat.remote_delete_session(sid) if chat is not None else (False, "AI chat unavailable"))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _stop_proc(self, name: str) -> tuple[bool, str]:
        def run(done: Callable[[Any], None]) -> None:
            chat = self._existing_active_chat()
            done(chat.remote_stop_proc(name) if chat is not None else (False, "AI chat unavailable"))

        res = self._on_gui(run)
        return res if isinstance(res, tuple) else (False, "GUI unavailable")

    def _grab_screen_opts(self, max_w: int, monitor: int = 0, fmt: str = "png", quality: int = 78) -> tuple[bytes | None, str, str]:
        fmt_norm = "jpeg" if fmt.lower() in ("jpg", "jpeg") else "png"
        q_norm = max(40, min(95, quality)) if fmt_norm == "jpeg" else 0
        now = time.monotonic()
        with self._screen_lock:
            if self._last_screen is not None:
                ts, c_mon, c_w, c_fmt, c_q, c_bytes, c_mime = self._last_screen
                if now - ts < SCREEN_CACHE_S and c_mon == monitor and c_w == max_w and c_fmt == fmt_norm and c_q == q_norm:
                    return c_bytes, c_mime, ""

        def trigger(done: Callable[[Any], None]) -> None:
            def on_captured(image: Any) -> None:
                data, mime = _encode_image(image, max_w, fmt=fmt_norm, quality=q_norm or 78)
                if data is None:
                    done((None, "", "Screen unavailable (Windows locked or headless)"))
                else:
                    done((data, mime, ""))

            _capture_monitor(self._win, monitor, on_captured)

        res = self._on_gui(trigger, timeout_s=5.0)
        if not isinstance(res, tuple):
            return None, "", "Screen capture timed out"
        data, mime, err = res
        if data is not None:
            with self._screen_lock:
                self._last_screen = (time.monotonic(), monitor, max_w, fmt_norm, q_norm, data, mime)
        return data, mime, err

    def _grab_screen(self, max_w: int) -> tuple[bytes | None, str]:
        data, _mime, err = self._grab_screen_opts(max_w, 0, "png", 80)
        return data, err

    def start(self) -> tuple[bool, str]:
        self.bridge.preferred_port = self.port
        ok, info = self.bridge.start()
        self.enabled = ok
        self.bridge_state_changed.emit()
        return ok, info

    def stop(self) -> None:
        self.enabled = False
        self.bridge.stop()
        self.bridge_state_changed.emit()

    def toggle(self) -> tuple[bool, str]:
        if self.running:
            self.stop()
            return False, "Stopped"
        return self.start()

    def _run_async(self, op: Callable[[], tuple[bool, str]], on_done: Callable[[bool, str], None] | None = None) -> None:
        if self._transitioning:
            return
        self._transitioning = True
        self.bridge_state_changed.emit()

        def worker() -> None:
            ok, info = False, ""
            try:
                with self._op_lock:
                    ok, info = op()
            finally:
                self._transitioning = False
                self.bridge_state_changed.emit()
                if on_done is not None and not self._win._is_shutting_down:
                    self._call.emit((lambda done, o=ok, i=info: (on_done(o, i), done(None)),
                                     {"event": threading.Event(), "result": None}))

        threading.Thread(target=worker, daemon=True).start()

    def start_async(self, on_done: Callable[[bool, str], None] | None = None) -> None:
        self._run_async(self.start, on_done)

    def stop_async(self, on_done: Callable[[bool, str], None] | None = None) -> None:
        def op() -> tuple[bool, str]:
            self.stop()
            return False, "Stopped"

        self._run_async(op, on_done)

    def toggle_async(self, on_done: Callable[[bool, str], None] | None = None) -> None:
        if self.running:
            self.stop_async(on_done)
        else:
            self.start_async(on_done)

    def rotate_token(self) -> str:
        return self.bridge.rotate_token()

    def show_pairing_dialog(self) -> None:
        if self._dialog is None:
            self._dialog = _PairingDialog(self, self._win)
            self._dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
            self._dialog.destroyed.connect(self._on_dialog_closed)
        else:
            self._dialog._refresh()
        self._dialog.show()
        self._dialog.raise_()
        self._dialog.activateWindow()
        if not self.running and not self._transitioning:
            self.start_async()

    def _on_dialog_closed(self) -> None:
        self._dialog = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"enabled": self.enabled, "port": self.port}
        if self._phone_overrides:
            payload["chat"] = dict(self._phone_overrides)
        return payload

    def load_dict(self, data: Any, *, auto_start: bool = True) -> None:
        if not isinstance(data, dict):
            return
        port = data.get("port")
        if isinstance(port, int) and not isinstance(port, bool) and (port == 0 or 1024 <= port <= 65535):
            self.port = port
            self.bridge.preferred_port = port
        chat_cfg = data.get("chat")
        if isinstance(chat_cfg, dict):
            overrides: dict[str, Any] = {}
            if isinstance(chat_cfg.get("model"), str) and valid_model(chat_cfg["model"]):
                overrides["model"] = chat_cfg["model"]
            if chat_cfg.get("thinking") in THINKING_LEVELS:
                overrides["thinking"] = chat_cfg["thinking"]
            scr = chat_cfg.get("screen_thinking")
            if scr == "" or scr in THINKING_LEVELS:
                overrides["screen_thinking"] = scr
            if isinstance(chat_cfg.get("auto"), bool):
                overrides["auto"] = chat_cfg["auto"]
            self._phone_overrides = overrides
            self._sync_phone_plugin()
        self.enabled = bool(data.get("enabled", False))
        if self.enabled and auto_start and not self.running:
            self.start()

    def close(self) -> None:
        if self._dialog is not None:
            self._dialog.close()
            self._dialog = None
        if self._phone_chat is not None:
            self._phone_chat.stop()
            self._phone_chat._stop_all_procs()
            self._phone_chat.close()
            self._phone_chat = None
        self.bridge.stop()
