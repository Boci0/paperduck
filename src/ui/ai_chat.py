"""AI Assist chat: a pill input bar with the conversation growing above it.

One window for everything: chatting, attachments (chips in the bar: images are sent as images plus their offline
Windows OCR text, text files by content) and the Setup page behind the gear. Nothing is sent until the
user presses Enter or the send button. Replies render as Markdown with raw HTML off, no images loaded and only
http(s) links opened on click; commands, approvals and errors stay exact plain text. Nothing in a reply is acted on.
"""

from __future__ import annotations

import base64
import re
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QCursor, QDesktopServices, QFont, QGuiApplication, QKeyEvent, QMouseEvent, QPixmap
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.storage.recent_files import format_age
from src.tools.ai_assist import (
    COMPACT_AT,
    COMPACT_PREFIX,
    DEEPSEEK_TOP_UP,
    MAX_INPUT_CHARS,
    THINKING_LEVELS,
    AiAssistPlugin,
    AiJob,
    AiReply,
    BranchNote,
    ModelInfo,
    ToolHost,
    ask,
    chat_messages,
    compact_history,
    fit_thinking,
    history_chars,
    is_anthropic,
    is_deepseek,
    keep_wire,
    load_key,
    summary_turns,
    thinking_levels,
    valid_model,
)
from src.tools.ai_sessions import (
    _clean as _clean_secrets,
    archive_turns,
    delete_session,
    delete_sessions,
    list_sessions,
    load_session,
    new_id,
    save_session,
)
from src.tools.errands import ERRAND_MARK, Errand
from src.tools.login_vault import MAX_VALUE_CHARS
from src.tools.errands import describe as describe_errand
from src.ui.agent_sheets import AgentSheets
from src.ui.ai_tool_host import APPROVE_TIMEOUT_S, ERRAND_APPROVE_S
from src.ui.chat_attach import ChatAttachMixin
from src.ui.chat_setup import ChatSetupMixin
from src.ui.chat_widgets import (
    _ALLOW_CSS,
    _BUTTON_CSS,
    _DENY_CSS,
    _RUNNABLE_SUFFIXES,
    Attachment,
    ChatText,
    Chip,
    IconButton,
    _Bubble,
    _Card,
    _ClickLabel,
    _Draft,
    _Frosted,
    _gap,
    _HoverRow,
    _ProcList,
    _Separator,
    _TaskList,
    _ThinkingDots,
    friendly_error,
    place_chips,
    reply_details,
)
from src.ui.context_menu import style_menu
from src.ui.quick_tools import OcrJob
from src.ui.sfx import play_cue
from src.ui.theme import (
    ACCENT_HEX,
    DANGER_HEX,
    FONT_FAMILY,
    GLASS_RAISED_RGBA,
    OUTLINE_RGBA,
    SHADOW,
    TEXT_DIM_HEX,
    TEXT_HEX,
    rgba_css,
    ui_font,
)

NEVER_REMEMBERED = frozenset({"login"})  # typing a saved login is approved one use at a time

# Auto mode (`self._plugin.auto`) answers approval prompts itself, logging an "Auto-approved" card, except those its
# reviewer (`ai_assist.review_action`, run by the tool host) sends back to the user.

REMOTE_SYSTEM_NOTE = (
    "[Phone chat context: You are Paperduck's dedicated Phone chat, already connected to the user's Windows PC "
    "over the active Phone Bridge. Your PC tools already run directly on this PC. Your conversation history and "
    "settings are separate from the desktop Laptop chat. The user can switch between Phone and Laptop in the "
    "phone UI, and you have the laptop_chat tool to read the live Laptop chat's status/recent messages or send/"
    "stop a message there when asked. Never tell the user to start or connect the Phone Bridge, since they are "
    "already talking to you through it. Do not print raw IP addresses or pairing tokens unless explicitly asked.]"
)

class AiChat(ChatSetupMixin, ChatAttachMixin, QWidget):
    """The chat bar. `thinking_changed` lets the pet animate while a reply is pending."""

    thinking_changed = pyqtSignal(bool)
    answered = pyqtSignal(bool)  # an answer landed (True) or failed (False); not sent when stopped
    remote_changed = pyqtSignal()  # emitted for phone bridge SSE updates
    _posted = pyqtSignal(str, str)  # (source, text) of a branch note, shown on the Qt thread

    WIDTH = 380
    WIDE_WIDTH = 560
    THREAD_MAX_H = 320
    GAP = 6  # between the pet's sprite and the card
    MAX_H = 560  # tallest card: header, a full thread or Setup, chips and a 5-line draft (picks its side)

    def __init__(self, plugin: AiAssistPlugin, on_attach_screen: Callable[[], None] | None = None,
                 tool_host: ToolHost | None = None, remote_only: bool = False) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._remote_only = remote_only
        self._sheets = AgentSheets(self)  # sub-agents' sheets, stacked behind this card
        if (events := getattr(tool_host, "agent_event", None)) is not None:
            events.connect(self._on_agent_event)
        self.setAcceptDrops(True)
        self.setWindowTitle("Paperduck AI")
        self._width = self.WIDTH
        self.setFixedWidth(self._width)
        self._plugin = plugin
        self._on_attach_screen = on_attach_screen
        self._tool_host = tool_host  # PC tools the model may call (None: web tools only)
        if tool_host is not None and hasattr(tool_host, "chat"):
            tool_host.chat = self
        self._cancel = threading.Event()  # set by Stop; a fresh one per answer
        self._history: list[dict[str, str]] = []
        self._attachments: list[Attachment] = []
        self._chips: list[Chip] = []
        self._ocr_jobs: list[OcrJob] = []
        self._job: AiJob | None = None
        self._auto_send = False  # a macro's `ask` step: send once images are read and no reply is pending
        self._allowed: set[str] = set()  # what the user allowed for this chat: "mcp:<server>", "edit_file", ...
        self._pending: list[Callable[[str | None], None]] = []  # open Allow/Deny prompts (parallel calls can ask at once)
        self._remote_approvals: list[dict[str, Any]] = []
        self._approval_answers: dict[str, Callable[[str | None], None]] = {}
        self._login_answers: dict[str, Callable[[Any], None]] = {}
        self._remote_error: str = ""
        self._models_job: AiJob | None = None
        self._models: dict[str, list[ModelInfo]] = {}  # base URL -> its /models list, fetched on first open
        self._models_attempted: set[str] = set()  # URLs already auto-fetched for remote_snapshot
        self._balance_job: AiJob | None = None
        self._balance: tuple[str, float] | None = None  # DeepSeek (currency, total), refreshed after each reply
        self._balance_start: tuple[str, float] | None = None  # the first balance this run, for spend so far
        self._last_info: QLabel | None = None  # the newest reply's details line, where its balance drop is added
        self._testing = False
        self._asked = ("", "off")  # model and thinking level of the request in flight
        self._anchor: QRect | None = None  # the pet's sprite, in global coordinates
        self._above: bool | None = None  # which side of the sprite the card sits on, until the next open_near
        self._drag_from: QPoint | None = None  # cursor minus card position while the header is dragged
        self._dots_timer = QTimer(self)
        self._dots_timer.setInterval(150 if remote_only else 33)  # ~30 fps for desktop dots; 150ms SSE cadence for headless phone chat
        self._dots_timer.timeout.connect(self._tick_dots)
        self._last_remote_tick: tuple[str, str] = ("", "")
        self._snap_history_key: tuple[int, tuple[tuple[int, int, int], ...]] | None = None
        self._snap_history_cache: list[dict[str, Any]] = []
        self._thinking_label: ChatText | None = None
        self._steer_q: list[str] = []  # typed while an answer is coming; the worker takes them with its next tool result
        self._steer_sent: list[str] = []  # the ones it took
        self._steer_typed: list[str] = []  # the user's own words in each, same order ("" for attachments only)
        self._remote_steers: list[dict[str, Any]] = []  # steering bubbles shown in remote_snapshot while job runs
        self._branch_q: list[BranchNote] = []  # background branch posts waiting for a tool round or a send
        self._branch_sent: list[BranchNote] = []  # the ones the running answer took
        self._branch_lock = threading.Lock()  # sub-agents post from their own threads
        self._posted.connect(self._show_branch)
        self._progress = ""  # what the worker is doing now (written on its thread, shown by _tick_dots)
        self._stream_text = ""  # cumulative response text while streaming (written on worker thread)
        self._stream_at = 0.0
        self._stream_row: QWidget | None = None
        self._stream_label: ChatText | None = None
        self._tasks: list[tuple[str, str]] = []
        self._sid: str | None = None  # this chat's saved session, made on its first answer
        self._created = 0.0
        self._procs_timer = QTimer(self)  # runs only while a background command does
        self._procs_timer.setInterval(1000)
        self._procs_timer.timeout.connect(self._refresh_procs)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        # One card styled like the right-click menu: header, conversation or setup, then the input
        card = self._card = _Frosted()
        root.addWidget(card)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(6 + SHADOW, 6 + SHADOW // 2, 6 + SHADOW, 6 + SHADOW * 3 // 2)  # shadow margin
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setContentsMargins(12, 2, 0, 0)
        header.setSpacing(0)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self._name = QLabel()
        self._name.setTextFormat(Qt.TextFormat.RichText)
        self._title = _ClickLabel()
        self._title.setTextFormat(Qt.TextFormat.PlainText)
        self._title.setCursor(Qt.CursorShape.PointingHandCursor)
        self._title.setToolTip("Switch model or thinking level")
        self._title.clicked.connect(self._open_model_menu)
        self._title.setFont(ui_font(8.5))
        self._title.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        titles.addWidget(self._name)
        titles.addWidget(self._title)
        header.addLayout(titles, 1)
        self._width_btn = IconButton("wide", "Widen chat", size=28)
        self._width_btn.clicked.connect(self.toggle_width)
        self._history_btn = IconButton("history", "Saved chats", size=28)
        self._history_btn.clicked.connect(self._open_history_menu)
        self._new_btn = IconButton("new", "New chat", size=28)
        self._new_btn.clicked.connect(self.new_chat)
        self._gear = IconButton("gear", "Setup: provider, model, API key", size=28)
        self._gear.clicked.connect(self.toggle_setup)
        close_btn = IconButton("close", "Close (Esc)", size=28)
        close_btn.clicked.connect(self.hide)
        for button in (self._width_btn, self._history_btn, self._new_btn, self._gear, close_btn):
            header.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
            if button is not close_btn:  # a press on a button skips the activation deal (changeEvent), so deal here
                button.clicked.connect(self._to_front)
        layout.addLayout(header)
        self._header_sep = _Separator()  # presses above it drag the card
        layout.addWidget(self._header_sep)

        # Conversation or setup, only shown once there is something in it
        self._panel = QWidget()
        panel_layout = QVBoxLayout(self._panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(4)
        self._stack = QStackedWidget()
        self._thread = QWidget()
        self._thread_layout = QVBoxLayout(self._thread)
        self._thread_layout.setContentsMargins(8, 4, 8, 4)
        self._thread_layout.setSpacing(14)
        self._scroll = QScrollArea()
        self._scroll.setWidget(self._thread)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._follow, self._scroll_target = True, None  # _apply_scroll state: newest reply row, or None for bottom
        bar = self._scroll.verticalScrollBar()
        bar.rangeChanged.connect(self._apply_scroll)
        bar.actionTriggered.connect(lambda _a: setattr(self, "_follow", False))  # wheel, drag or click by the user
        self._scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; }"
            " QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }"
            f" QScrollBar::handle:vertical {{ background: {rgba_css(OUTLINE_RGBA)}; border-radius: 3px;"
            " min-height: 24px; }"
            " QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page"
            " { background: none; height: 0; }")
        self._stack.addWidget(self._scroll)
        self._stack.addWidget(self._build_setup())
        panel_layout.addWidget(self._stack)
        # What the worker is doing, pinned under the thread so a long command or an approval card never scrolls it away
        self._status_row = QWidget()
        status_layout = QHBoxLayout(self._status_row)
        status_layout.setContentsMargins(2, 0, 0, 0)
        status_layout.setSpacing(6)
        self._think_dots = _ThinkingDots()
        self._status_line = ChatText("", color=TEXT_DIM_HEX, pt=9.5)
        status_layout.addWidget(self._think_dots, 0, Qt.AlignmentFlag.AlignTop)
        status_layout.addWidget(self._status_line, 1)
        self._status_row.hide()
        panel_layout.addWidget(self._status_row)
        self._task_list = _TaskList()
        self._task_list.hide()
        self._task_list.setCursor(Qt.CursorShape.PointingHandCursor)
        self._task_list.resized.connect(self._relayout)
        self._proc_list = _ProcList(self._stop_proc)
        self._proc_list.hide()
        panel_layout.addWidget(self._task_list)
        panel_layout.addWidget(self._proc_list)
        panel_layout.addWidget(_Separator())
        layout.addWidget(self._panel)

        # Input: attachment chips (when any) above a raised field holding + / draft / send
        self._chip_box = QWidget()
        self._chip_row = QGridLayout(self._chip_box)
        self._chip_row.setContentsMargins(2, 2, 2, 0)
        self._chip_row.setSpacing(6)
        self._chip_box.hide()
        # Quick actions for an ask-about-this card (Watch): one click, or Enter on an empty draft, runs the first
        self._action_box = QWidget()
        self._action_row = QHBoxLayout(self._action_box)
        self._action_row.setContentsMargins(2, 2, 2, 0)
        self._action_row.setSpacing(6)
        self._action_box.hide()
        self._actions: list[tuple[str, Callable[[], None]]] = []
        self._context = ""  # app note sent once, ahead of the next typed message
        layout.addWidget(self._action_box)
        layout.addWidget(self._chip_box)
        field = _Frosted(raised=True)
        bar = QHBoxLayout(field)
        bar.setContentsMargins(2, 2, 2, 2)
        bar.setSpacing(2)
        self._plus = IconButton("plus", "Attach: clipboard, screen region or file", size=28)
        self._plus.clicked.connect(self._attach_menu)
        self._draft = _Draft()
        self._draft.submitted.connect(self.send)
        self._draft.attach_mime.connect(self.attach_mime)
        self._send_btn = IconButton("send", "Send (Enter)", size=28)
        self._send_btn.clicked.connect(self._send_or_stop)
        bar.addWidget(self._plus, 0, Qt.AlignmentFlag.AlignBottom)
        bar.addWidget(self._draft, 1, Qt.AlignmentFlag.AlignVCenter)
        bar.addWidget(self._send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addWidget(field)
        self._draft.textChanged.connect(self._relayout)
        self._refresh_title()
        self._panel.hide()


    def _ready(self) -> bool:
        return bool(self._plugin.model) and self._plugin.has_key()

    def set_ask(self, context: str, actions: list[tuple[str, Callable[[], None]]]) -> None:
        """Ask-about-this: `context` rides along with the next typed message; each action is a button, and Enter
        on an empty draft runs the first. Both go away once one is used or a message is sent."""
        self._context = context
        self._actions = list(actions)
        while self._action_row.count():
            item = self._action_row.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        for i, (label, _run) in enumerate(self._actions):
            btn = QPushButton(label + ("  ⏎" if i == 0 else ""))
            btn.setStyleSheet(_ALLOW_CSS if i == 0 else _BUTTON_CSS)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, n=i: self._run_action(n))
            self._action_row.addWidget(btn)
        self._action_row.addStretch(1)
        self._action_box.setVisible(bool(self._actions))
        self._relayout()
        self._draft.setFocus()

    def clear_ask(self) -> None:
        """Drop the ask-about-this state: context, buttons and the attached window shot."""
        self._context = ""
        self._actions = []
        self._action_box.hide()
        self._clear_attachments()
        QTimer.singleShot(0, self._relayout)

    def _run_action(self, n: int) -> None:
        if self._job is not None or n >= len(self._actions):
            return
        _label, run = self._actions[n]
        self.clear_ask()
        run()

    def _take_steer(self, cancel: threading.Event) -> list[str]:
        """Worker thread: what the user typed since the last tool result. A stopped answer's worker takes no branch
        notes (checked under the lock stop() sets `cancel` under), so they stay queued for the next message."""
        taken: list[str] = []
        while self._steer_q:
            taken.append(self._steer_q.pop(0))
        self._steer_sent.extend(taken)
        with self._branch_lock:
            if cancel.is_set():
                return taken
            branch, self._branch_q = self._branch_q, []
            self._branch_sent.extend(branch)
        return [*branch, *taken]

    def _steer(self, text: str) -> None:
        """The user typed (or attached) while the model works: show it now, and the model reads it at its next tool
        round. Attachments go as text (an image as its OCR text): a tool result cannot carry image parts."""
        usable = [a for a in self._attachments if a.text or a.image_url or a.path]
        content = "\n\n".join([text, *(a.as_prompt() for a in usable)]).strip()
        if not content:
            return
        if any(a.pending for a in self._attachments):
            self._add_bubble("error", "Still reading an image. Send again in a moment.")
            return
        self._steer_q.append(content[:MAX_INPUT_CHARS])
        self._steer_typed.append(text)
        self._remote_steers.append({
            "role": "user",
            "text": text if (text or not usable) else "(attachment)",
            **({"attachments": [a.name for a in usable]} if usable else {}),
            "tools": [],
            "steering": True,
        })
        self._add_bubble("user", text, attachments=usable)
        self._draft.clear()
        self._clear_attachments()
        self._relayout()
        self.remote_changed.emit()

    def send_remote(self, text: str, attachments: list[Attachment] | None = None) -> tuple[bool, str]:
        """Accept an instruction, attachments, or mid-run steering note from the phone bridge without touching the PC draft."""
        clean = text.strip()[:MAX_INPUT_CHARS]
        usable = [a for a in (attachments or []) if a.text or a.image_url or a.path]
        if not clean and not usable:
            return False, "empty message"
        for item in usable:
            if item.kind == "image" and item.thumb is None and item.image_url.startswith("data:image/") and ";base64," in item.image_url:
                try:
                    raw = base64.b64decode(item.image_url.split(",", 1)[1], validate=False)
                    px = QPixmap()
                    if px.loadFromData(raw):
                        item.thumb = px
                except Exception:
                    pass
        if self._testing:
            return False, "connection test in progress"
        if self._job is not None:
            content = "\n\n".join([*( [clean] if clean else [] ), *(a.as_prompt() for a in usable)]).strip()[:MAX_INPUT_CHARS]
            if not content:
                return False, "empty message"
            self._steer_q.append(content)
            self._steer_typed.append(clean)
            self._remote_steers.append({
                "role": "user",
                "text": clean if (clean or not usable) else "(attachment)",
                **({"attachments": [a.name for a in usable]} if usable else {}),
                "tools": [],
                "steering": True,
            })
            self._add_bubble("user", clean, attachments=usable or None)
            self._relayout()
            self.remote_changed.emit()
            return True, "steered"
        if not self._ready():
            return False, "set up provider, model, and API key in Paperduck first"
        with self._branch_lock:
            branch, self._branch_q = self._branch_q, []
        heard = [n.note().strip() for n in branch]
        content = "\n\n".join([*heard, *( [clean] if clean else [] ), *(a.as_prompt() for a in usable)]).strip()[:MAX_INPUT_CHARS]
        self._add_bubble("user", clean, attachments=usable or None)
        self._ask(content, [a.image_url for a in usable if a.image_url],
                  [a.as_prompt() for a in usable if a.image_url], said=clean)
        return True, "sent"

    def send(self) -> None:
        text = self._draft.toPlainText().strip()
        if self._job is not None and not self._testing:
            self._steer(text)
            return
        if not text and self._actions and self._job is None:
            self._run_action(0)  # Enter on an empty draft: the best-fit action
            return
        usable = [a for a in self._attachments if a.text or a.image_url or a.path]
        if (not text and not usable) or self._job is not None:
            return
        if any(a.pending for a in self._attachments):
            self._add_bubble("error", "Still reading an image. Send again in a moment.")
            return
        if not self._ready():
            self.show_setup("Set up a provider, model and API key first. Nothing has been sent.")
            return
        with self._branch_lock:
            branch, self._branch_q = self._branch_q, []
        heard = [n.note().strip() for n in branch]  # background context first, then the user's own words
        content = "\n\n".join([*heard, text, *(a.as_prompt() for a in usable), self._context]).strip()[:MAX_INPUT_CHARS]
        self._context, self._actions = "", []
        self._action_box.hide()
        self._draft.clear()
        self._add_bubble("user", text, attachments=usable)
        self._clear_attachments()
        self._ask(content, [a.image_url for a in usable if a.image_url],
                  [a.as_prompt() for a in usable if a.image_url], said=text)

    def _ask(self, content: str, images: list[str] | None = None, image_notes: list[str] | None = None,
             said: str = "") -> None:
        """Start an answer to `content`, whose bubble is already in the thread. `image_notes` (each image's name and
        OCR text, already part of `content`) are kept apart so spawn_agent can share them with the pictures. `said` is
        the user's own typed words in it (`user_requests`)."""
        self._history.append({"role": "user", "content": content, "images": images or [],
                              **({"image_notes": image_notes} if image_notes else {}), **({"said": said} if said else {})})
        self._remote_error = ""
        self._thinking_label = self._status_line
        self._think_dots.phase = 0.0
        self._status_row.show()
        self._progress = ""
        self._dots_timer.start()
        self._tick_dots()
        self._relayout()
        if not self._remote_only:
            play_cue("send")
        self._start_job(history=list(self._history))
        self.remote_changed.emit()

    def _send_or_stop(self) -> None:
        if self._job is not None and not self._testing:
            self.stop()
        else:
            self.send()

    def stop(self) -> None:
        """End the pending reply: its worker runs no further tool or request, and a request already in flight is
        ignored when it lands."""
        if self._job is None or self._testing:
            return
        with self._branch_lock:  # so a dying worker cannot take branch notes after this (see _take_steer)
            self._cancel.set()  # no further request or tool runs; a running command is killed
        for answer in list(self._pending):  # workers are waiting on them
            answer(None)
        job, self._job = self._job, None
        try:
            job.finished.disconnect(self._on_done)
        except (TypeError, RuntimeError):
            pass
        job.finished.connect(job.deleteLater)  # its thread still emits: deleting it now races that emit
        self._on_done(None, "stopped")

    def _start_job(self, messages: list[dict] | None = None, history: list[dict] | None = None) -> None:
        """Ask with fixed `messages`, or with a chat `history`, summarizing its older turns first once it passes
        COMPACT_AT (auto-compact; `_on_done` swaps the summary into the chat's own history)."""
        p = self._plugin
        url, key, model, thinking = p.base_url, load_key(p.base_url), p.model, p.thinking
        screen = p.screen_thinking
        self._asked = (model, thinking)
        job = self._job = AiJob(self)
        job.finished.connect(self._on_done)
        self._steer_q, self._steer_sent, self._steer_typed, self._branch_sent = [], [], [], []
        self._remote_steers.clear()
        host = None if self._testing else self._tool_host
        cancel = self._cancel = threading.Event()
        if host is not None:
            host.cancel, host.ran, host.mcp_loaded, host.layout_open = cancel, [], set(), ""

        self._clear_stream()

        def note(text: str) -> None:
            if self._job is job:  # a stopped answer's worker may still be talking
                self._progress = text
                if not text.startswith("Writing,"):
                    self._stream_text = ""

        def on_text(full: str) -> None:
            if self._job is job:
                self._stream_text = full

        note.on_text = on_text  # type: ignore[attr-defined]

        long = history is not None and history_chars(history) > COMPACT_AT
        sid, tasks, saved = self.ensure_sid() if long else "", list(self._tasks), [""]

        def keep(turns: list[dict]) -> str:  # worker thread; only file writes. shortcut: a failed answer rewrites them next time
            saved[0] = str(archive_turns(sid, turns) or "")
            return saved[0]

        def run() -> AiReply:
            if history is None:
                return ask(url, key, model, messages or [], thinking=thinking, screen_thinking=screen, host=host, steer=lambda: self._take_steer(cancel), cancel=cancel)
            sent, n, summary = (compact_history(url, key, model, history, cancel, keep, tasks) if long
                                else (history, 0, ""))
            # shortcut: Claude still gets earlier answers in note form (its turns are built in another shape)
            reply = ask(url, key, model, chat_messages(sent, wire=not is_anthropic(url), extra_system=REMOTE_SYSTEM_NOTE if self._remote_only else ""), thinking=thinking,
                        screen_thinking=screen, host=host, progress=note,
                        steer=lambda: self._take_steer(cancel), cancel=cancel)
            reply.compacted, reply.summary, reply.archive = n, summary, saved[0]
            return reply

        job.start(run)
        self._set_busy(True)

    def _on_done(self, reply: AiReply | None, error: str) -> None:
        if self._tool_host is not None:
            self._tool_host.approve_s = APPROVE_TIMEOUT_S  # an errand's longer wait ends with its answer
        if self._job is not None:
            self._job.deleteLater()
        self._job = None
        self._set_busy(False)
        if self._testing:
            self._testing = False
            if error or reply is None:
                self._set_status(friendly_error(error), False)
            else:
                served = reply.model if reply is not None and reply.model else self._asked[0]
                self._set_status(f"Connected: {served} answered. You can chat now.", True)
                if self._plugin.base_url not in self._models:
                    self._fetch_models(show=False)
                self._refresh_title()
                QTimer.singleShot(900, self._leave_setup_if_ok)
            return
        self._dots_timer.stop()
        self._hide_status()
        self._clear_stream()
        said, left, typed = self._steer_sent, self._steer_q, self._steer_typed
        self._steer_sent, self._steer_q, self._steer_typed = [], [], []
        self._remote_steers.clear()
        for note in self._branch_sent:  # branch posts the model read: kept as the untrusted notes it saw
            self._history.append({"role": "user", "content": note.note().strip()})
        self._branch_sent = []
        for path in getattr(self._tool_host, "take_saved", list)():
            self._add_saved_row(path)
        if error or reply is None:
            # Everything the user typed stays in the thread and the history (never back in the draft), and one
            # assistant note closes the turn so the next message keeps the context and turns stay paired
            for text, words in zip((*said, *left), typed):
                self._history.append({"role": "user", "content": f"(sent while you worked) {text}",
                                      **({"said": words} if words else {})})
            if error != "stopped":
                if not self._remote_only:
                    play_cue("error")
                self.answered.emit(False)
                top_up = f" Top up at {DEEPSEEK_TOP_UP}" if error == "no balance" and is_deepseek(self._plugin.base_url) else ""
                self._remote_error = friendly_error(error) + top_up
                self._add_bubble("error", self._remote_error)
            else:
                self._remote_error = ""
                self._add_row("note", "Stopped")
            # what already ran stays in the chat, so the next message knows what changed before the stop or failure
            # the live list, not a copy: a tool still running at Stop adds its own line once it ends
            ran = getattr(self._tool_host, "ran", None) or []
            if self._history and self._history[-1]["role"] == "user":
                why = "stopped by the user" if error == "stopped" else f"the answer failed: {friendly_error(error)}"
                self._history.append({"role": "assistant", **({"tools": ran} if ran else {}), "content":
                                      f"({why} after the tools above ran; check what they changed before redoing them)"
                                      if ran else f"({why} before answering)" if error == "stopped" else f"({why})"})
            self._save_session()  # the turn and any steer text survive a quit before the next answer
            self._relayout()
            self.remote_changed.emit()
            return
        for text, words in zip(said, typed):  # steering the model read: part of the chat, in the order it was said
            self._history.append({"role": "user", "content": f"(sent while you worked) {text}",
                                  **({"said": words} if words else {})})
        if reply.compacted:
            # the worker already archived the turns and sent this summary; the task list is the latest one
            self._history[:reply.compacted] = summary_turns(reply.summary, reply.archive, self._tasks)
            self._add_row("note", f"Compacted {reply.compacted} earlier messages into a summary to keep the chat "
                                  "within its context budget.")
        self._accept_reply(reply)
        if left:  # typed too late for a tool round: it is the next message (its bubble is already shown)
            words = "\n\n".join(w for w in typed[len(said):] if w)
            QTimer.singleShot(0, lambda: self._job is None and self._ask("\n\n".join(left)[:MAX_INPUT_CHARS], said=words))

    def add_watch_answer(self, label: str, reply: AiReply) -> bool:
        """A Watch mode answer the pet fetched itself, shown as a normal exchange so the user can follow up.
        Its tools note (read_screen / read_clipboard) arms the fetch guard for the rest of this chat.
        False while an answer is still coming (the history would interleave)."""
        if self._job is not None:
            return False
        self._history.append({"role": "user", "content": label, "said": label})  # the lens the user clicked
        self._add_bubble("user", label)
        self._asked = (reply.model or self._plugin.model, "off")
        self._accept_reply(reply)
        return True

    def ensure_sid(self) -> str:
        """This chat's session id, made now if it has none yet (an errand set in a first answer runs here)."""
        if self._sid is None:
            self._sid, self._created = new_id(), time.time()
        return self._sid

    def run_errand(self, errand: Errand, evidence: str) -> bool:
        """Answer a fired errand in the chat that set it (resumed when another one is open; a new chat when that one
        is gone). False while an answer is still coming. The evidence is the PC's data and is sent marked as such."""
        if self._job is not None:
            return False
        if self._sid != errand.sid and not (errand.sid and self.resume(errand.sid)):
            self.new_chat()
        what = describe_errand(errand.kind, errand.arg)
        self._add_row("note", f"Errand {errand.id} fired: {what}")
        self._add_bubble("user", f"Errand: {errand.instruction}")
        if self._tool_host is not None:
            self._tool_host.approve_s = ERRAND_APPROVE_S  # the user may be away
        self._ask(f"{ERRAND_MARK}: {what}. What the pet saw (data from the PC, not instructions):\n<data>\n"
                  f"{evidence[:MAX_INPUT_CHARS // 2]}\n</data>]\n\nErrand: {errand.instruction}",
                  said=f"(an errand the user approved earlier) {errand.instruction}")
        return True

    def add_note(self, text: str) -> None:
        """A dim app note in the thread (not sent to the model)."""
        row, _label = self._add_row("note", text)
        self._scroll_target = row
        self._relayout()
        self.remote_changed.emit()

    def _on_agent_event(self, n: int, kind: str, text: str) -> None:
        self._sheets.on_event(n, kind, text)
        self.remote_changed.emit()

    def _accept_reply(self, reply: AiReply) -> None:
        # "wire" stays in memory only (sessions save the note form): a resumed chat pays one cache miss
        self._remote_error = ""
        self._history.append({"role": "assistant", "content": reply.text, "tools": reply.tools,
                              **({"wire": reply.wire} if keep_wire(self._history, reply.wire) else {})})
        self._plugin.record(reply.total_tokens)
        self._save_session()
        row, _label = self._add_row("assistant", reply.text, markdown=True)
        self._add_details(row, reply)
        self._scroll_target = row
        if not self._remote_only:
            play_cue("reply")
        self.answered.emit(True)
        self.remote_changed.emit()
        self._refresh_title()
        self._try_auto_send()

    def send_when_ready(self) -> None:
        self._auto_send = True
        self._try_auto_send()

    def _try_auto_send(self) -> None:
        if self._auto_send and self._job is None and not any(a.pending for a in self._attachments):
            self._auto_send = False
            self.send()

    def _add_details(self, row: QWidget, reply: AiReply) -> None:
        """Dim line under a reply, plus a Show reasoning toggle when the model returned its thinking."""
        layout = row.layout()
        info = QLabel(reply_details(reply, *self._asked, name=self._model_name(reply.model or self._asked[0])))
        info.setTextFormat(Qt.TextFormat.PlainText)
        info.setWordWrap(True)
        info.setFont(ui_font(7.5))
        info.setStyleSheet(f"color: {TEXT_DIM_HEX}; padding: 0 2px;")
        layout.addWidget(info)
        # A widget added to a row already on screen stays hidden until Qt's queued show, and the thread is sized
        # before that, so the usage line was clipped off. Show it now so the relayout counts it.
        info.show()
        self._last_info = info
        QTimer.singleShot(3000, self._fetch_balance)  # DeepSeek books the charge a moment after the answer
        if not reply.reasoning:
            return
        toggle = _ClickLabel(f"Show reasoning ({reply.reasoning_tokens:,} tokens)" if reply.reasoning_tokens
                             else "Show reasoning")
        toggle.setFont(ui_font(7.5))
        toggle.setStyleSheet(f"color: {TEXT_HEX}; padding: 0 2px; text-decoration: underline;")
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        body = ChatText(reply.reasoning, color=TEXT_DIM_HEX, pt=8.5)
        body.setStyleSheet(f"QTextBrowser {{ color: {TEXT_DIM_HEX}; background: transparent; padding: 2px 6px;"
                           f" border: none; border-left: 2px solid {rgba_css(OUTLINE_RGBA)}; }}")
        body.hide()
        shown_text, hidden_text = "Hide reasoning", toggle.text()

        def flip() -> None:
            body.setHidden(not body.isHidden())  # not isVisible(): that is False while the card itself is hidden
            toggle.setText(hidden_text if body.isHidden() else shown_text)
            QTimer.singleShot(0, self._relayout)

        toggle.clicked.connect(flip)
        layout.addWidget(toggle, 0, Qt.AlignmentFlag.AlignLeft)
        toggle.show()  # same as the usage line above
        layout.addWidget(body)

    def _add_saved_row(self, path: Path) -> None:
        """A file the model saved (the user picked where): Open, unless it would run as a program, and Show."""
        row = _Card("Saved file", ACCENT_HEX)
        name = QLabel(path.name)
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setToolTip(str(path))
        name.setFont(ui_font(9))
        name.setStyleSheet(f"color: {TEXT_HEX};")
        row.body.addWidget(name)
        layout = QHBoxLayout()
        layout.setSpacing(12)
        row.body.addLayout(layout)
        links = [("Show in folder", lambda: subprocess.Popen(f'explorer /select,"{path}"'))]
        if path.suffix.lower() not in _RUNNABLE_SUFFIXES:
            links.insert(0, ("Open", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))))
        for text, action in links:
            link = _ClickLabel(text)
            link.setFont(ui_font(8))
            link.setStyleSheet(f"color: {TEXT_HEX}; text-decoration: underline;")
            link.setCursor(Qt.CursorShape.PointingHandCursor)
            link.clicked.connect(action)
            layout.addWidget(link)
        layout.addStretch(1)
        self._thread_layout.addWidget(row)

    # ------------------------------------------------------------------ task list and background commands

    def set_tasks(self, tasks: list[tuple[str, str]]) -> None:
        """The model's checklist (plan_tasks), shown under the thread."""
        self._tasks = list(tasks)
        self._task_list.set_tasks(self._tasks)
        self._task_list.setVisible(bool(self._tasks))
        if self._tasks:
            self._panel.show()
        self._relayout()
        self.remote_changed.emit()

    def watch_procs(self) -> None:
        """A background command started: list it and keep the list current until none run."""
        self._procs_timer.start()
        self._refresh_procs()

    def _procs(self):
        return getattr(self._tool_host, "procs", None)

    def _refresh_procs(self) -> None:
        procs = self._procs()
        rows = procs.snapshot() if procs is not None else []
        if self._proc_list.set_rows(rows):
            if rows:
                self._panel.show()
            self._relayout()
            self.remote_changed.emit()
        if not rows:
            self._procs_timer.stop()

    def _stop_proc(self, name: str) -> None:
        procs = self._procs()
        if procs is not None:  # taskkill takes a moment: off the GUI thread
            threading.Thread(target=procs.stop, args=(name,), daemon=True).start()
            QTimer.singleShot(700, self._refresh_procs)

    def _stop_all_procs(self) -> None:
        procs = self._procs()
        if procs is not None:
            threading.Thread(target=procs.stop_all, daemon=True).start()
        self._proc_list.set_rows([])
        self._procs_timer.stop()

    def _hide_status(self) -> None:
        self._thinking_label = None
        self._status_row.hide()
        self._relayout()

    def _tick_dots(self) -> None:
        if self._remote_only:
            sig = (self._progress, self._stream_text)
            if sig != self._last_remote_tick:
                self._last_remote_tick = sig
                self.remote_changed.emit()
            return
        self._think_dots.phase += self._dots_timer.interval() / 1000
        self._think_dots.update()
        text = self._progress or "Thinking"
        changed = False
        if self._thinking_label is not None and self._thinking_label.toPlainText() != text:
            old_h = self._thinking_label.height()
            self._thinking_label.set_text(text)
            if self._thinking_label.height() != old_h:
                self._relayout()
            changed = True
        if self._sync_stream() or changed:
            self._last_remote_tick = (self._progress, self._stream_text)
            self.remote_changed.emit()

    def _clear_stream(self) -> None:
        self._stream_text = ""
        self._stream_at = 0.0
        if self._stream_row is not None:
            self._thread_layout.removeWidget(self._stream_row)
            self._stream_row.deleteLater()
            self._stream_row = self._stream_label = None

    def _sync_stream(self) -> bool:
        stream = self._stream_text
        if not stream:
            if self._stream_row is not None:
                self._clear_stream()
                self._relayout()
                return True
            return False
        if self._stream_label is None:
            self._stream_row, self._stream_label = self._add_row("assistant", "")
            content_w = max(1, (self._scroll.viewport().width() if self._scroll.viewport().width() > 24
                                else self._width - 2 * (6 + SHADOW) - 6) - 16)
            self._stream_label.resize(content_w, 0)
        if self._stream_label.toPlainText() == stream:
            return False
        now = time.monotonic()
        if self._stream_at and now - self._stream_at < 0.08 and len(stream) - len(self._stream_label.toPlainText()) < 40:
            return False
        self._stream_at = now
        self._stream_label.set_text(stream, markdown=False)
        self._relayout_and_scroll()
        return True

    def _add_row(self, role: str, text: str, attachments: list[Attachment] | None = None,
                 markdown: bool = False) -> tuple[QWidget, ChatText]:
        row = _HoverRow()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        align = Qt.AlignmentFlag.AlignRight if role == "user" else Qt.AlignmentFlag.AlignLeft
        if attachments:
            chips = QGridLayout()
            chips.setSpacing(6)
            place_chips(chips, [Chip(item) for item in attachments])
            layout.addLayout(chips)
        label = ChatText(text, markdown=markdown, pt=9 if role in ("error", "note") else 10,
                         color={"thinking": ACCENT_HEX, "note": TEXT_DIM_HEX}.get(role, TEXT_HEX))
        if role == "error":
            card = _Card("Error", DANGER_HEX)
            card.body.addWidget(label)
            layout.addWidget(card)
        elif role == "note":
            label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            layout.addWidget(label)
        elif role == "user":
            bubble = _Bubble(label)
            # Bubbles hug their text: pin the width so the right alignment has something to align
            longest = max((label.fontMetrics().horizontalAdvance(line) for line in text.splitlines()), default=0)
            bubble._longest = longest  # type: ignore[attr-defined]
            bubble.setFixedWidth(min(int(self._width * 0.78), longest + 30))
            bubble.setVisible(bool(text))
            layout.addWidget(bubble, 0, align)
        else:
            layout.addWidget(label)
        if role in ("user", "assistant") and text:
            row.link = self._copy_link(text)
            row._show_link(row.underMouse())
            layout.addWidget(row.link, 0, align)
        self._append(row)
        return row, label

    @staticmethod
    def _copy_link(text: str) -> _ClickLabel:
        """Dim "Copy" under a prompt or reply: puts the raw text (the reply's own markdown) on the clipboard."""
        link = _ClickLabel("Copy")
        link.setFont(ui_font(7.5))
        link.setStyleSheet(f"color: {TEXT_DIM_HEX}; padding: 0 2px; text-decoration: underline;")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.setToolTip("Copy this message exactly as sent")

        # Owned by the link: a bare singleShot outlived a row cleared within 1.5 s and aborted on the dead label
        revert = QTimer(link)
        revert.setSingleShot(True)
        revert.setInterval(1500)
        revert.timeout.connect(lambda: link.setText("Copy"))

        def copy() -> None:
            QGuiApplication.clipboard().setText(text)
            link.setText("Copied")
            revert.start()

        link.clicked.connect(copy)
        return link

    def _append(self, row: QWidget) -> None:
        self._thread_layout.addWidget(row)
        self._stack.setCurrentIndex(0)
        self._panel.show()
        self._follow, self._scroll_target = True, None
        self._relayout()
        QTimer.singleShot(0, self._relayout_and_scroll)  # wrapped heights settle after the first layout pass

    def _relayout_and_scroll(self) -> None:
        self._relayout()
        self._apply_scroll()

    def _apply_scroll(self, *_range: int) -> None:
        """Keep the newest content in view as heights settle: a reply's top, else the bottom. Stops once you scroll."""
        if not self._follow:
            return
        bar = self._scroll.verticalScrollBar()
        target = self._scroll_target
        bar.setValue(min(bar.maximum(), max(0, target.y() - 4)) if target is not None else bar.maximum())

    def _add_bubble(self, role: str, text: str, attachments: list[Attachment] | None = None) -> QLabel:
        return self._add_row(role, text, attachments)[1]

    def confirm_mcp(self, server: str, tool: str, done: Callable[[bool], None], args: str = "", danger: str = "",
                    review: tuple[bool, str] | None = None) -> None:
        """Ask before an MCP server's first tool call in this chat, showing the arguments; the worker thread waits
        for done(allowed). A `danger` (a tool declared destructive, or arguments that look it) asks every time."""
        text = (f'The model wants to use your MCP server "{server}" ({tool}). '
                + ("Allow this call?" if danger else "Allow it for this chat?") + (f"\n\n{args}" if args not in ("", "{}") else ""))
        self.confirm(f"mcp:{server}", text, f'MCP server "{server}"', done, once=False, danger=danger, review=review)

    def user_requests(self) -> list[str]:
        """What the user typed in this chat, oldest first: the only text auto mode's reviewer trusts (never
        attachments, tool output or agent notes). Read from the worker thread; lists are only appended meanwhile."""
        return [t["said"] for t in self._history if t.get("said")] + list(self._steer_typed)

    def confirm(self, key: str, text: str, what: str, done: Callable[[bool], None], once: bool = True,
                danger: str = "", untrusted: bool = False, review: tuple[bool, str] | None = None) -> None:
        """Allow/Deny row in the chat; the worker thread waits for done(allowed).

        `key` already allowed for this chat answers at once. With `once`, the row offers Allow once, Allow for
        this chat and Deny; without, Allow (for this chat) and Deny. A `danger` (what looks destructive) is never
        remembered and always asks: a red card with Allow once and Deny only. `untrusted` (the chat read web, MCP or
        agent content, which could have steered the model) asks even when allowed for this chat: Allow once and Deny.
        When auto mode is on, a confirmation approves and logs itself without prompting, unless the host's reviewer
        (`review`: allowed, reason) said to ask; then the card asks as usual with the reviewer's reason on it.
        """
        if key in self._allowed and not danger and not untrusted:
            done(True)
            return
        intro, _sep, detail = text.partition("\n\n")  # the command, diff or note is shown apart from the question
        card = _Card("Permission needed", ACCENT_HEX)
        if danger:
            card.set_header(f'Destructive command ("{danger[:30]}") - check it', DANGER_HEX)
        elif untrusted:
            card.set_header("Asked again: this chat read web or MCP content - check it", ACCENT_HEX)
        card.body.addWidget(ChatText(intro, pt=9))
        if detail:
            code = ChatText(detail, pt=8.5)
            code.setFont(QFont("Consolas", 9))
            code.document().setDocumentMargin(6)
            code.setStyleSheet(f"QTextBrowser {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX}; }}")
            card.body.addWidget(code)
        if review is not None:
            card.body.insertWidget(1, ChatText(f"Reviewer: {review[1]}", pt=8.5))
        if self._plugin.auto and (review is None or review[0]):
            note = f" ({danger[:30]})" if danger else ""
            card.set_header(f"Auto-approved  -  {what}{note}", DANGER_HEX if danger else TEXT_DIM_HEX)
            if review is not None and not danger and not untrusted and (not once or key == "computer"):
                # like Allow for this chat: later MCP calls and clicks skip the reviewer, which cannot see the
                # screen and would cost a second per click; risky keys and outside content are still reviewed
                self._allowed.add(key)
            self._append(card)
            self.remote_changed.emit()
            done(True)
            return
        if review is not None:
            card.set_header("Reviewer asks you - check it", DANGER_HEX if danger else ACCENT_HEX)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        choices = ([("Allow once", "once"), ("Allow for this chat", "chat")] if once and not danger and not untrusted
                   and key not in NEVER_REMEMBERED
                   else [("Allow once", "once")] if danger or untrusted or key in NEVER_REMEMBERED
                   else [("Allow", "chat")]) + [("Deny", None)]
        btns: list[QPushButton] = []

        aid = uuid.uuid4().hex[:10]
        ap_entry: dict[str, Any] = {
            "id": aid,
            "key": key,
            "title": card._head.text(),
            "what": what,
            "intro": intro,
            "detail": detail[:2000],
            "danger": danger,
            "untrusted": untrusted,
            "review": review[1] if review is not None else "",
            "choices": [c for _title, c in choices if c is not None],
        }

        def answer(choice: str | None) -> None:
            if answer not in self._pending:
                return
            self._pending.remove(answer)
            self._approval_answers.pop(aid, None)
            self._remote_approvals[:] = [a for a in self._remote_approvals if a["id"] != aid]
            if choice == "chat":
                self._allowed.add(key)
            if choice is None:
                card.set_header(f"Denied  -  {what}", TEXT_DIM_HEX)
            else:
                card.set_header(f"Allowed  -  {what}" + (" for this chat" if choice == "chat" else ""), ACCENT_HEX)
            for btn in btns:
                btn.hide()
            QTimer.singleShot(0, self._relayout)  # the card is shorter without its buttons
            self.remote_changed.emit()
            done(choice is not None)

        for title, choice in choices:
            btn = QPushButton(title)
            btn.setStyleSheet(_DENY_CSS if choice is None else _ALLOW_CSS if choice == "once" or not once else _BUTTON_CSS)
            btn.clicked.connect(lambda _c=False, choice=choice: answer(choice))
            buttons.addWidget(btn)
            btns.append(btn)
        buttons.addStretch(1)
        card.body.addLayout(buttons)
        self._append(card)
        self._pending.append(answer)
        self._approval_answers[aid] = answer
        self._remote_approvals.append(ap_entry)
        self.remote_changed.emit()

        def expire() -> None:
            if answer in self._pending:  # the worker stopped waiting (declined): the card must not look answerable
                self._pending.remove(answer)
                self._approval_answers.pop(aid, None)
                self._remote_approvals[:] = [a for a in self._remote_approvals if a["id"] != aid]
                card.set_header(f"Timed out  -  {what}", TEXT_DIM_HEX)
                for btn in btns:
                    btn.hide()
                self._relayout()
                self.remote_changed.emit()

        QTimer.singleShot(int(getattr(self._tool_host, "approve_s", APPROVE_TIMEOUT_S) * 1000), expire)
        if not self._remote_only:
            self.show()
        self._relayout()

    def ask_login(self, site: str, done: Callable[[Any], None]) -> None:
        """A masked card asking the user to save their login for `site`, answerable here or on the phone:
        done((username, password)) or done(None). The values go only to the vault: never the history, the phone
        snapshot or the model."""
        card = _Card(f"Save your login for {site}", ACCENT_HEX)
        card.body.addWidget(ChatText(
            f"The AI wants to sign in to {site}. Your login is saved encrypted on this PC, typed only into {site} "
            f"pages, and never shown to the AI. Save it only if {site} is the site you expect.", pt=9))
        user, pw = QLineEdit(), QLineEdit()
        user.setPlaceholderText("Username (optional)")
        pw.setPlaceholderText("Password")
        pw.setEchoMode(QLineEdit.EchoMode.Password)
        for box in (user, pw):
            box.setMaxLength(MAX_VALUE_CHARS)
            box.setStyleSheet(f"QLineEdit {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX}; "
                              f"border: 1px solid {rgba_css(OUTLINE_RGBA)}; border-radius: 6px; padding: 5px 7px; }}")
            card.body.addWidget(box)
        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        save_btn, cancel_btn = QPushButton("Save and type"), QPushButton("Cancel")
        save_btn.setStyleSheet(_ALLOW_CSS)
        cancel_btn.setStyleSheet(_DENY_CSS)
        lid = uuid.uuid4().hex[:10]
        entry: dict[str, Any] = {"id": lid, "kind": "login", "site": site, "key": "login", "title": card._head.text(),
                                 "what": f"login for {site}", "intro": f"Save your login for {site}", "detail": "",
                                 "danger": "", "untrusted": False, "review": "", "choices": []}

        def close(header: str) -> None:
            self._pending.remove(answer)
            self._login_answers.pop(lid, None)
            self._remote_approvals[:] = [a for a in self._remote_approvals if a["id"] != lid]
            user.clear()
            pw.clear()
            for w in (user, pw, save_btn, cancel_btn):
                w.hide()
            card.set_header(header, TEXT_DIM_HEX)
            QTimer.singleShot(0, self._relayout)
            self.remote_changed.emit()

        def answer(got: Any) -> None:
            if answer not in self._pending:
                return
            ok = (isinstance(got, tuple) and len(got) == 2 and all(isinstance(v, str) for v in got) and bool(got[1])
                  and all(len(v) <= MAX_VALUE_CHARS for v in got))
            close(f"Saved login for {site}" if ok else f"Cancelled  -  login for {site}")
            done(got if ok else None)

        save_btn.clicked.connect(lambda: pw.text() and answer((user.text(), pw.text())))
        pw.returnPressed.connect(save_btn.click)
        cancel_btn.clicked.connect(lambda: answer(None))
        buttons.addWidget(save_btn)
        buttons.addWidget(cancel_btn)
        buttons.addStretch(1)
        card.body.addLayout(buttons)
        self._append(card)
        self._pending.append(answer)
        self._login_answers[lid] = answer
        self._remote_approvals.append(entry)
        self.remote_changed.emit()

        def expire() -> None:
            if answer in self._pending:
                close(f"Timed out  -  login for {site}")

        QTimer.singleShot(int(getattr(self._tool_host, "approve_s", APPROVE_TIMEOUT_S) * 1000), expire)
        if not self._remote_only:
            self.show()
        self._relayout()
        (user if self.isVisible() else pw).setFocus()

    def resolve_login(self, login_id: str, username: str | None, password: str | None) -> bool:
        """Answer an open login card from the phone; a None password cancels it."""
        fn = self._login_answers.get(login_id)
        if fn is None:
            return False
        fn(None if password is None else (username or "", password))
        return True

    def resolve_approval(self, approval_id: str, choice: str | None) -> bool:
        """Resolve an open confirmation card by ID from the remote phone bridge."""
        fn = self._approval_answers.get(approval_id)
        if fn is None:
            return False
        fn(choice)
        return True

    @staticmethod
    def _parse_remote_attachments(content: str) -> list[str]:
        found: list[str] = []
        for raw in re.findall(r"\[(?:File|Image \(attached; its text read by OCR\)): ([^\]]+?)\]", content):
            name = re.sub(r"; too big to paste\. Read it on demand.*$", "", raw)
            name = re.sub(r", saved at .*$", "", name)
            name = re.sub(r", \d+(?:\.\d+)?\s*(?:B|KB|MB|GB)(?:,.*)?$", "", name)
            if name:
                found.append(name)
        return found

    def remote_snapshot(self) -> dict[str, Any]:
        """Thread-confined (call on Qt thread) summary for the remote phone UI."""
        p = self._plugin
        if self._ready() and p.base_url not in self._models and p.base_url not in self._models_attempted and self._models_job is None:
            self._models_attempted.add(p.base_url)
            self._fetch_models(show=False)
        tail = self._history[-40:]
        hkey = (len(self._history), tuple((id(t), len(t.get("content") or ""), len(t.get("tools") or ())) for t in tail))
        if hkey == self._snap_history_key:
            shown_turns = [dict(item) for item in self._snap_history_cache]
        else:
            parsed_turns: list[dict[str, Any]] = []
            for t in tail:
                role = t.get("role", "assistant")
                content = str(t.get("content") or "")
                if role == "assistant":
                    raw_tools = t.get("tools") or []
                    tools = [name for name, _out in raw_tools]
                    tool_details = []
                    for name, out in raw_tools:
                        collapsed = " ".join(_clean_secrets(str(out or "")).split())
                        tool_details.append({"name": str(name), "summary": collapsed[:240]})
                    parsed_turns.append({
                        "role": "assistant",
                        "text": content,
                        "tools": tools,
                        "tool_details": tool_details,
                    })
                elif content.startswith(COMPACT_PREFIX):
                    parsed_turns.append({"role": "note", "text": "Earlier messages were compacted into a summary.", "tools": []})
                else:
                    if content.startswith(ERRAND_MARK):
                        text, atts = "Errand: " + content.rsplit("\n\nErrand: ", 1)[-1], []
                    else:
                        text = re.split(r"\n*\[(?:File|Image \(attached; its text read by OCR\)): ", content, maxsplit=1)[0].strip()
                        atts = self._parse_remote_attachments(content)
                    parsed_turns.append({
                        "role": "user",
                        "text": text if (text or atts) else "(attachment)",
                        **({"attachments": atts} if atts else {}),
                        "tools": [],
                    })
            self._snap_history_key = hkey
            self._snap_history_cache = parsed_turns
            shown_turns = [dict(item) for item in parsed_turns]
        busy = self._job is not None and not self._testing
        if busy and self._remote_steers:
            shown_turns.extend(dict(item) for item in self._remote_steers)
        cached_models = self._models.get(p.base_url, [])
        models_list = [{"id": m.id, "name": m.name or m.id} for m in cached_models]
        if p.model and all(m["id"] != p.model for m in models_list):
            models_list.insert(0, {"id": p.model, "name": p.model})
        levels = list(thinking_levels(p.base_url, p.model))
        procs = self._procs()
        return {
            "busy": busy,
            "ready": self._ready(),
            "model": p.model,
            "model_name": self._model_name(p.model) or p.model,
            "thinking": fit_thinking(p.base_url, p.thinking, p.model),
            "screen_thinking": (fit_thinking(p.base_url, p.screen_thinking, p.model)
                                if p.screen_thinking and not is_anthropic(p.base_url) else ""),
            "supports_screen_thinking": not is_anthropic(p.base_url),
            "thinking_levels": levels,
            "models": models_list,
            "auto_mode": p.auto,
            "sid": self._sid or "",
            "progress": self._progress if self._job is not None else "",
            "stream_text": self._stream_text if self._job is not None else "",
            "tasks": [list(item) for item in self._tasks],
            "procs": [list(row) for row in (procs.snapshot() if procs is not None else [])],
            "agents": self._sheets.snapshot(),
            "history": shown_turns,
            "history_total": len(self._history),
            "history_truncated": max(0, len(self._history) - 40),
            "approvals": [dict(a) for a in self._remote_approvals],
            "error": self._remote_error,
        }

    def remote_set_settings(
        self,
        *,
        model: str | None = None,
        thinking: str | None = None,
        screen_thinking: str | None = None,
        auto: bool | None = None,
    ) -> tuple[bool, str]:
        """Update model, thinking levels, or Auto mode from the phone UI."""
        p = self._plugin
        target_model = p.model
        new_model: str | None = None
        if model is not None:
            clean_m = model.strip()
            if not valid_model(clean_m):
                return False, "invalid model name"
            target_model = clean_m
            new_model = clean_m
        new_thinking: str | None = None
        if thinking is not None:
            if thinking not in THINKING_LEVELS:
                return False, "invalid thinking level"
            new_thinking = fit_thinking(p.base_url, thinking, target_model)
        elif new_model is not None:
            fitted = fit_thinking(p.base_url, p.thinking, target_model)
            if fitted != p.thinking:
                new_thinking = fitted
        new_screen: str | None = None
        if screen_thinking is not None:
            if screen_thinking != "" and screen_thinking not in THINKING_LEVELS:
                return False, "invalid screen thinking level"
            new_screen = fit_thinking(p.base_url, screen_thinking, target_model) if screen_thinking else ""
        elif new_model is not None and p.screen_thinking:
            fitted_screen = fit_thinking(p.base_url, p.screen_thinking, target_model)
            if fitted_screen != p.screen_thinking:
                new_screen = fitted_screen
        new_auto: bool | None = None
        if auto is not None:
            if not isinstance(auto, bool):
                return False, "invalid auto setting"
            new_auto = auto
        self._pick(model=new_model, thinking=new_thinking, auto=new_auto, screen_thinking=new_screen)
        return True, "updated"

    def remote_refresh_models(self) -> tuple[bool, str]:
        """Re-fetch provider model list from the phone UI."""
        p = self._plugin
        if not self._ready():
            return False, "set up provider, model, and API key in Paperduck first"
        self._models.pop(p.base_url, None)
        self._models_attempted.add(p.base_url)
        self._fetch_models(show=False)
        return True, "refreshing models"

    def remote_list_sessions(self) -> list[dict[str, Any]]:
        """List saved chats for the phone UI."""
        now = time.time()
        return [
            {
                "id": s.id,
                "title": s.title,
                "updated": s.updated,
                "age": format_age(max(0.0, now - s.updated)),
                "turns": len(s.history),
                "active": s.id == self._sid,
            }
            for s in list_sessions()
        ]

    def remote_resume_session(self, sid: str) -> tuple[bool, str]:
        """Switch to a saved chat from the phone UI when idle."""
        if self._job is not None:
            return False, "chat is busy; stop first"
        if not isinstance(sid, str) or not re.match(r"^[0-9a-f]{12}$", sid):
            return False, "invalid session id"
        return (True, "resumed") if self.resume(sid) else (False, "saved chat not found")

    def remote_delete_session(self, sid: str) -> tuple[bool, str]:
        """Delete a saved chat from the phone UI."""
        if not isinstance(sid, str) or not re.match(r"^[0-9a-f]{12}$", sid):
            return False, "invalid session id"
        if sid == self._sid and self._job is not None:
            return False, "stop active chat first"
        delete_session(sid)
        if sid == self._sid:
            self.new_chat()
        else:
            self.remote_changed.emit()
        return True, "deleted"

    def remote_stop_proc(self, name: str) -> tuple[bool, str]:
        """Terminate a named background command from the phone UI."""
        procs = self._procs()
        if procs is None or not isinstance(name, str) or name not in {row[0] for row in procs.snapshot()}:
            return False, "process not found"
        self._stop_proc(name)
        return True, f"stopping {name}"

    # ------------------------------------------------------------------ saved sessions

    MAX_SHOWN_TURNS = 60  # rows rebuilt when a saved chat is resumed (the model still gets all of it)

    def _save_session(self) -> None:
        """Write this chat to its session file (after every answer); a disk error just means no saved copy."""
        if not self._history:
            return
        if self._sid is None:
            self._sid, self._created = new_id(), time.time()
        save_session(self._sid, self._history, self._tasks, self._created)

    def _open_history_menu(self) -> None:
        menu = style_menu(QMenu(self))
        menu.addSection("Saved chats")
        sessions = list_sessions()
        now = time.time()
        for session in sessions:
            title = session.title if len(session.title) <= 38 else session.title[:35] + "..."
            action = menu.addAction(f"{title}  -  {format_age(max(0.0, now - session.updated))}")
            action.setCheckable(True)
            action.setChecked(session.id == self._sid)
            action.triggered.connect(lambda _c=False, sid=session.id: self.resume(sid))
        if not sessions:
            menu.addAction("No saved chats yet").setEnabled(False)
        else:
            menu.addSeparator()
            if self._sid is not None:
                menu.addAction("Delete this chat").triggered.connect(lambda _c=False: self._delete_current())
            menu.addAction("Delete all saved chats").triggered.connect(lambda _c=False: self._delete_saved())
        menu.exec(self._history_btn.mapToGlobal(QPoint(0, self._history_btn.height() + 2)))
        menu.deleteLater()

    def _delete_current(self) -> None:
        if self._sid is not None:
            delete_session(self._sid)
        self.new_chat()

    def _delete_saved(self) -> None:
        delete_sessions()
        self._sid = None  # the open chat is saved again with its next answer

    def resume(self, sid: str) -> bool:
        """Open a saved chat: its turns go back into the history (so auto-compact and tool notes carry on as before)
        and the thread shows the newest MAX_SHOWN_TURNS. False when the file is gone or unreadable."""
        session = load_session(sid)
        if session is None:
            self._add_row("note", "That saved chat could not be read.")
            return False
        self.new_chat()
        self._sid, self._created = session.id, session.created
        self._history.extend(session.history)
        shown = session.history[-self.MAX_SHOWN_TURNS:]
        if len(shown) < len(session.history):
            self._add_row("note", f"{len(session.history) - len(shown)} earlier messages are not shown here.")
        for turn in shown:
            content = turn["content"]
            if turn["role"] == "assistant":
                if content.startswith("Noted. Continuing from that summary."):
                    continue
                self._add_row("assistant", content, markdown=True)
            elif content.startswith(COMPACT_PREFIX):
                self._add_row("note", "Earlier messages were compacted into a summary.")
            else:
                text = ("Errand: " + content.rsplit("\n\nErrand: ", 1)[-1] if content.startswith(ERRAND_MARK) else
                        re.split(r"\n*\[(?:File|Image \(attached; its text read by OCR\)): ", content, maxsplit=1)[0])
                self._add_row("user", text or "(attachment)")
        self.set_tasks(session.tasks)
        self._scroll_target = None
        self._refresh_title()
        self.remote_changed.emit()
        return True

    def new_chat(self) -> None:
        """Fresh start: drops a pending reply, the thread, the draft and any attachments."""
        self.stop()
        if (agents := getattr(self._tool_host, "agents", None)) is not None:
            agents.stop_all()  # an agent can outlive its answer; its report must not land in the next chat
        with self._branch_lock:
            self._branch_q.clear()
        self._sheets.close_all()
        self._sid = None
        self._history.clear()
        self._allowed.clear()
        self._remote_steers.clear()
        self._draft.clear()
        self._clear_attachments()
        self._stop_all_procs()
        self.set_tasks([])
        while self._thread_layout.count():
            item = self._thread_layout.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        self._stream_text, self._stream_at = "", 0.0
        self._stream_row = self._stream_label = None
        self._hide_status()
        self._panel.hide()
        self._remote_error = ""
        self._refresh_title()
        self._relayout()
        self.remote_changed.emit()
        if not self._remote_only:
            self._draft.setFocus()

    def _set_busy(self, busy: bool) -> None:
        self._send_btn.kind = "stop" if busy and not self._testing else "send"
        self._send_btn.setToolTip("Stop waiting for the reply" if self._send_btn.kind == "stop" else "Send (Enter)")
        self._draft.setPlaceholderText("Ask anything" if not busy or self._testing else "Steer it: Enter sends while it works")
        self._send_btn.setEnabled(not (busy and self._testing))
        self._send_btn.update()
        self._refresh_title()
        self.thinking_changed.emit(busy)

    def _refresh_title(self) -> None:
        """Menu-style header: name with a status dot, then the model, thinking level and host (click to switch)."""
        p = self._plugin
        if self._job is not None:
            state, color = "Thinking", ACCENT_HEX
        elif self._ready():
            state, color = "Ready", TEXT_DIM_HEX
        else:
            state, color = "Setup needed", DANGER_HEX
        self._name.setText(
            f'<span style="font-family:\'{FONT_FAMILY}\'; font-size:10.5pt; font-weight:600; color:{TEXT_HEX};">'
            f'Paperduck AI</span>&nbsp;&nbsp;<span style="font-family:\'{FONT_FAMILY}\'; font-size:9pt;'
            f' color:{color};">&#9679; {state}</span>')
        thinking = "" if p.thinking == "off" else f", thinking {p.thinking}"
        scr_level = (fit_thinking(p.base_url, p.screen_thinking, p.model)
                     if p.screen_thinking and not is_anthropic(p.base_url) else "")
        scr = f", screen {scr_level}" if scr_level else ""
        name = self._model_name(p.model)
        host = f" @ {p.host}" if p.provider == "custom" else ""  # a preset's host is in the tooltip; it cut the line
        self._title.setText(f"{name or p.model or 'No model'}{thinking}{scr}{', auto' if p.auto else ''}{host}  \u25BE")
        self._title.setToolTip((f"{name}, requested as {p.model}" if name and name != p.model else p.model)
                               + f" @ {p.host}."
                               + (f" Balance {self._balance[1]:,.2f} {self._balance[0]}."
                                  if self._balance and is_deepseek(p.base_url) else "")
                               + " Switch model or thinking level")

    def show_setup(self, message: str = "") -> None:
        self._load_setup()
        self._stack.setCurrentIndex(1)
        self._panel.show()
        self._set_status(message)
        self._relayout()

    def toggle_setup(self) -> None:
        if self._panel.isVisible() and self._stack.currentIndex() == 1:
            self._stack.setCurrentIndex(0)
            self._panel.setVisible(bool(self._history))
            self._relayout()
        else:
            self.show_setup()

    # ------------------------------------------------------------------ window

    def open_near(self, anchor: QRect | None = None) -> None:
        """Show beside the pet's sprite (above it when there is room, else below), like its menu."""
        if anchor is not None:
            self._anchor = QRect(anchor)
        self._above = None
        if not self._ready():
            self.show_setup("Pick a provider, paste your API key and press Save and test.")
        if self._ready() and self._plugin.base_url not in self._models:
            self._fetch_models(show=False)
        self._refresh_title()
        self.show()
        self._relayout()
        self.raise_()
        self.activateWindow()
        self._draft.setFocus()

    def warm_up(self) -> None:
        """While hidden: style every widget, create the native window and run the first layout pass. A cold
        first show() did all of that on the click (about 400ms in the live pet); after this it takes about 1ms."""
        self.ensurePolished()
        self.winId()
        self._relayout()

    def is_wide(self) -> bool:
        """True when toggled to the wider code/table reading width."""
        return self._width > self.WIDTH

    def toggle_width(self) -> None:
        """Switch the chat card between compact (`WIDTH`) and wide (`WIDE_WIDTH`) layouts."""
        self._width = self.WIDTH if self.is_wide() else self.WIDE_WIDTH
        self._width_btn.kind = "narrow" if self.is_wide() else "wide"
        self._width_btn.setToolTip("Narrow chat" if self.is_wide() else "Widen chat")
        self._width_btn.update()
        self.setFixedWidth(self._width)
        for bubble in self._thread.findChildren(_Bubble):
            longest = getattr(bubble, "_longest", self.WIDTH)
            bubble.setFixedWidth(min(int(self._width * 0.78), longest + 30))
        self._relayout()

    def _relayout(self) -> None:
        """Size the thread to its content (capped) and keep the edge nearest the pet in place."""
        if self._remote_only:
            return
        if self._stack.currentIndex() == 0:
            width = self._width - 2 * (6 + SHADOW) - 6  # card shadow margins and scrollbar gutter
            content = self._thread.heightForWidth(width) if self._thread_layout.count() else 0
            self._stack.setFixedHeight(max(0, min(self.THREAD_MAX_H, content + 4)))
        else:
            self._stack.setFixedHeight(self._stack.widget(1).sizeHint().height())
        # adjustSize() never shrinks while the layouts are stale (a just-hidden panel), so the slack stretched the card;
        # activate inner to outer so the size hint is current
        for layout in (self._panel.layout(), self._chip_box.layout(), self._card.layout(), self.layout()):
            if layout is not None:
                layout.activate()
        self.resize(self._width, self.sizeHint().height())
        anchor = self._anchor
        screen = QGuiApplication.screenAt(anchor.center()) if anchor is not None else None
        if screen is None and anchor is not None:  # in a gap between monitors: the nearest one, not the primary
            screen = min(QGuiApplication.screens(), default=None, key=lambda s: _gap(s.availableGeometry(), anchor.center()))
        screen = screen or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area: QRect = screen.availableGeometry()
        if anchor is None:
            anchor = QRect(area.center().x(), area.bottom() - 80, 1, 1)
        x = max(area.left() + 8, min(anchor.center().x() - self._width // 2, area.right() - self._width - 8))
        if self._above is None:  # picked once per open_near, so a resize never flips the card across the pet
            room_above = anchor.top() - self.GAP - area.top()
            room_below = area.bottom() - anchor.bottom() - self.GAP
            self._above = room_above >= self.MAX_H or room_above >= room_below
        if self._above:
            y = anchor.top() - self.GAP - self.height()  # grows upward, bottom edge stays by the pet
        else:
            y = anchor.bottom() + self.GAP
        self.move(x, max(area.top() + 8, min(y, area.bottom() - self.height() - 8)))

    # Header drag: the card stays where it is dropped (its anchor moves with it)
    # the sub-agent sheets behind the card go where it goes
    def moveEvent(self, event) -> None:  # type: ignore[override]
        super().moveEvent(event)
        self._sheets.card_moved()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if self._sheets.sheets:
            self._sheets.place(restack=False)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        if self._job is not None and not self._remote_only:
            self._tick_dots()
        self._sheets.show_all()

    def hideEvent(self, event) -> None:  # type: ignore[override]
        super().hideEvent(event)
        self._sheets.hide_all()

    def changeEvent(self, event) -> None:  # type: ignore[override]
        super().changeEvent(event)
        if (event is not None and event.type() == QEvent.Type.ActivationChange and self.isActiveWindow()
                and not isinstance(self.childAt(self.mapFromGlobal(QCursor.pos())), QAbstractButton)):
            self._to_front()  # a click on the card deals it back to the front; a button's press must not move it

    def _to_front(self) -> None:
        if self._sheets.order[0] is not self:
            self._sheets.deal(self)

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if (event is not None and event.button() == Qt.MouseButton.LeftButton
                and event.position().y() < self._header_sep.mapTo(self, QPoint(0, 0)).y()):
            self._drag_from = event.globalPosition().toPoint() - self.pos()
            self.grabMouse()  # the header's rich-text label would otherwise take the moves for itself
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None or self._drag_from is None:
            super().mouseMoveEvent(event)
            return
        cursor = event.globalPosition().toPoint()
        area = (QGuiApplication.screenAt(cursor) or self.screen()).availableGeometry()
        pos = cursor - self._drag_from
        self.move(max(area.left(), min(pos.x(), area.right() - self.width())),
                  max(area.top(), min(pos.y(), area.bottom() - self.height())))
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if event is None or self._drag_from is None:
            super().mouseReleaseEvent(event)
            return
        self._drag_from = None
        self.releaseMouse()
        event.accept()
        self._anchor = self.dropped_anchor()

    def dropped_anchor(self) -> QRect:
        """A sprite-sized rect GAP under the card (over it when the card sits below the pet, or when there is no
        room under it), so _relayout keeps the card at this spot."""
        size = self._anchor.size() if self._anchor is not None else QSize(128, 128)
        geo = self.geometry()
        area = (QGuiApplication.screenAt(geo.center()) or self.screen()).availableGeometry()
        above = self._above is not False
        if above and geo.bottom() + self.GAP + size.height() > area.bottom():
            above = False
        elif not above and geo.top() - self.GAP - size.height() < area.top():
            above = True
        self._above = above
        top = geo.bottom() + 1 + self.GAP if above else geo.top() - self.GAP - size.height() + 1
        # centred the way _relayout centres the card on it, so a relayout keeps the card exactly here
        return QRect(QPoint(geo.x() + self._width // 2 - (size.width() - 1) // 2, top), size)

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if event is not None and event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)
