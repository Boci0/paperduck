"""AI chat building blocks: attachment chips, text rows, bubbles, cards, the draft and the small helpers they use."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QMimeData,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    QUrl,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QColor,
    QDesktopServices,
    QImage,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextOption,
)
from PyQt6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from src.tools.ai_assist import AiReply, ModelInfo
from src.tools.web_drop import strip_metadata
from src.ui.theme import (
    ACCENT_HEX,
    BUBBLE_RGBA,
    DANGER_HEX,
    GLASS_RAISED_RGBA,
    GLASS_RGBA,
    HAIRLINE_RGBA,
    HOVER_RGBA,
    OUTLINE_RGBA,
    SHADOW,
    TEXT_DIM_HEX,
    TEXT_HEX,
    paint_panel,
    qcolor,
    rgba_css,
    ui_font,
)

MAX_FILE_BYTES = 4 * 1024 * 1024  # most that is read for a text chip; a bigger file is attached by path
INLINE_FILE_CHARS = 100_000  # a text file longer than this goes by path too (a message holds MAX_INPUT_CHARS)
REF_HEAD_CHARS = 3000
MAX_ATTACHMENTS = 4
MAX_BRANCH_CHARS = 40_000  # background branch text held for the next message (the oldest posts go); 4 agent reports fit
MAX_IMAGE_SIDE = 1568  # longest side sent to vision models: bigger only costs tokens
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp")
# A saved file with these would run rather than open, so its row offers only Show in folder
_RUNNABLE_SUFFIXES = {".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".exe", ".com",
                      ".scr", ".msi", ".lnk", ".url", ".reg", ".py", ".pyw", ".jar", ".cpl", ".msc", ".pif"}
_FRIENDLY_ERRORS = {
    "bad key": "The API key was rejected. Check it in Setup (gear).",
    "no balance": "Your balance with the provider is empty. Top it up, then try again.",
    "rate limited": "The provider is rate limiting you. Wait a moment and try again.",
    "bad model or url": "Model or URL not found. Check them in Setup (gear).",
    "network": "Could not reach the provider. Check your connection.",
    "forbidden": "The provider refused this key for that model.",
    "bad request": "The provider rejected the request. If Thinking is on, this model may not support it: set it Off.",
    "refused mid-answer": ("The provider rejected the request partway through the job (often the chat got too long). "
                           "Send a message to continue from here, or start a new chat."),
    "cut off": "The model spent its whole token budget thinking. Try a lower Thinking level.",
}
_FIELD_CSS = (
    f"background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_HEX}; border: 1px solid {rgba_css(OUTLINE_RGBA)};"
    " border-radius: 6px; padding: 5px 8px;"
)
_BUTTON_CSS = (
    f"QPushButton {{ color: {TEXT_HEX}; background: transparent; border: 1px solid {rgba_css(OUTLINE_RGBA)};"
    " border-radius: 6px; padding: 5px 14px; }"
    f" QPushButton:hover {{ background: {rgba_css(HOVER_RGBA)}; color: {ACCENT_HEX}; }}"
)
_ALLOW_CSS = _BUTTON_CSS.replace(f"border: 1px solid {rgba_css(OUTLINE_RGBA)}", f"border: 1px solid {ACCENT_HEX}")
_DENY_CSS = _BUTTON_CSS.replace(f"color: {ACCENT_HEX}; }}", f"color: {DANGER_HEX}; }}")


def image_data_url(image: QImage) -> str:
    """PNG data URL for the model, without the image's text tags (they would reach the provider)."""
    image = strip_metadata(image)
    if max(image.width(), image.height()) > MAX_IMAGE_SIDE:
        image = image.scaled(MAX_IMAGE_SIDE, MAX_IMAGE_SIDE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    return "data:image/png;base64," + bytes(data.toBase64().data()).decode("ascii")


def reply_details(reply: AiReply, asked_model: str, thinking: str, name: str = "") -> str:
    """One dim line under a reply: the model that answered (and the alias it came through) with its listed
    name, e.g. the version behind an alias like deepseek-flash, then thinking and tokens."""
    served = reply.model or asked_model
    parts = [(served if served == asked_model else f"{asked_model} > {served}")
             + (f" ({name})" if name and name != served else "")]
    if thinking != "off":
        parts.append(f"thinking {thinking}")
    if reply.total_tokens:
        cached = min(reply.cached_tokens, reply.prompt_tokens)
        split = [f"{reply.prompt_tokens:,} in"] + ([f"{cached * 100 // reply.prompt_tokens}% cached"] if cached else [])
        if reply.reasoning_tokens:
            split.append(f"{reply.reasoning_tokens:,} thinking")
        split.append(f"{max(0, reply.completion_tokens - reply.reasoning_tokens):,} answer")
        parts.append(f"{reply.total_tokens:,} tokens ({', '.join(split)})")
    if reply.cost:
        parts.append(f"${reply.cost:.4f}")
    elif reply.cost_estimate:
        parts.append(f"~${reply.cost_estimate:.4f}")
    if reply.cut_off:
        parts.append("cut off at the limit")
    return "  -  ".join(parts)


def balance_note(start: tuple[str, float], now: tuple[str, float]) -> str:
    """DeepSeek reports whole cents, so one reply rarely moves it; the spend since the pet started adds up."""
    spent = start[1] - now[1] if start[0] == now[0] else 0.0
    return f"balance {now[1]:,.2f} {now[0]}" + (f" ({spent:,.2f} spent since start)" if spent >= 0.005 else "")


def model_label(model: ModelInfo) -> str:
    label = f"{model.name}  ({model.id})" if model.name and model.name != model.id else model.id
    return label + ("  -  sees images" if model.vision else "")


def friendly_error(error: str) -> str:
    return _FRIENDLY_ERRORS.get(error, f"Request failed ({error}).")


def read_text_file(path: Path) -> str:
    """Content of a small text file; raises ValueError('too large' / 'not text')."""
    with path.open("rb") as f:  # never pull a multi-GB file into RAM on the GUI thread
        data = f.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("too large")
    if b"\x00" in data[:8192]:
        raise ValueError("not text")
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("not text")


def _size_text(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.1f} KB"


@dataclass
class Attachment:
    kind: str  # "image" or "file"
    name: str
    text: str = ""
    detail: str = ""
    pending: bool = False
    thumb: QPixmap | None = None
    image_url: str = ""  # PNG data URL, sent as an image part
    path: str = ""  # set when the file is too big to paste: only its start goes in, the model reads the rest

    def as_prompt(self) -> str:
        if self.path:
            return (f"[File: {self.name}, {self.detail}; too big to paste. Read it on demand with read_file "
                    f"(start_line) and search_text, path {self.path}. Its start:]\n{self.text[:REF_HEAD_CHARS]}")
        label = "Image (attached; its text read by OCR)" if self.kind == "image" else "File"
        return f"[{label}: {self.name}]\n{self.text}"


class _Frosted(QFrame):
    """The chat's paper panel: the card itself is dog-eared with a soft shadow; raised fields are plain sheets."""

    def __init__(self, raised: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._raised = raised

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        if self._raised:
            paint_panel(painter, self.rect(), qcolor(GLASS_RAISED_RGBA), radius=8, fold=False)
        else:
            paint_panel(painter, self.rect(), qcolor(GLASS_RGBA), shadow=SHADOW)
        painter.end()


class IconButton(QAbstractButton):
    """Small round button with a painted line icon (plus, send, gear, close): no font or emoji glyphs."""

    def __init__(self, kind: str, tip: str, size: int = 32, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setFixedSize(size, size)
        self.setToolTip(tip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = self.width() / 32.0
        painter.scale(s, s)
        if self.underMouse() and self.isEnabled():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(qcolor(HOVER_RGBA))
            painter.drawRoundedRect(QRectF(3, 3, 26, 26), 7, 7)
        color = QColor((ACCENT_HEX if self.underMouse() else TEXT_HEX) if self.isEnabled() else TEXT_DIM_HEX)
        painter.setPen(QPen(color, 1.6 / max(s, 0.6), cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == "plus":
            painter.drawLine(QPointF(16, 10), QPointF(16, 22))
            painter.drawLine(QPointF(10, 16), QPointF(22, 16))
        elif self.kind == "send":  # return arrow, as on a keyboard
            path = QPainterPath(QPointF(21, 10))
            path.lineTo(21, 19)
            path.lineTo(11, 19)
            painter.drawPath(path)
            painter.drawLine(QPointF(11, 19), QPointF(14.5, 15.5))
            painter.drawLine(QPointF(11, 19), QPointF(14.5, 22.5))
        elif self.kind == "gear":
            painter.drawEllipse(QPointF(16, 16), 3.2, 3.2)
            painter.drawEllipse(QPointF(16, 16), 6.4, 6.4)
            for i in range(8):
                painter.save()
                painter.translate(16, 16)
                painter.rotate(i * 45)
                painter.drawLine(QPointF(0, -6.4), QPointF(0, -8.6))
                painter.restore()
        elif self.kind == "close":
            painter.drawLine(QPointF(11.5, 11.5), QPointF(20.5, 20.5))
            painter.drawLine(QPointF(20.5, 11.5), QPointF(11.5, 20.5))
        elif self.kind == "new":  # compose: open square with a pencil stroke
            path = QPainterPath(QPointF(16, 10))
            for x, y in ((10, 10), (10, 22), (22, 22), (22, 16)):
                path.lineTo(x, y)
            painter.drawPath(path)
            painter.drawLine(QPointF(15, 17), QPointF(22, 10))
        elif self.kind == "history":  # clock face: saved chats
            painter.drawEllipse(QPointF(16, 16), 7, 7)
            painter.drawPolyline([QPointF(16, 11.5), QPointF(16, 16), QPointF(19.5, 18)])
        elif self.kind == "stop":
            painter.setBrush(color)
            painter.drawRoundedRect(QRectF(11.5, 11.5, 9, 9), 2, 2)
        painter.end()


def _gap(area: QRect, point: QPoint) -> int:
    """Manhattan distance from `point` to `area` (0 inside it)."""
    return (max(area.left() - point.x(), 0, point.x() - area.right())
            + max(area.top() - point.y(), 0, point.y() - area.bottom()))


class _Separator(QWidget):
    """Hairline like QMenu::separator (1px, 6px side margins)."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(9)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.fillRect(QRect(6, 4, self.width() - 12, 1), qcolor(HAIRLINE_RGBA))
        painter.end()


class _FileGlyph(QWidget):
    """Folded-corner document icon for file chips."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedSize(36, 36)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(TEXT_DIM_HEX), 1.4, join=Qt.PenJoinStyle.RoundJoin))
        page = QPainterPath(QPointF(11, 6))
        for x, y in ((21, 6), (27, 12), (27, 30), (11, 30), (11, 6)):
            page.lineTo(x, y)
        painter.drawPath(page)
        painter.drawPolyline([QPointF(21, 6), QPointF(21, 12), QPointF(27, 12)])
        for y in (18, 22, 26):
            painter.drawLine(QPointF(15, y), QPointF(23, y))
        painter.end()


class Chip(_Frosted):
    """One attachment: thumbnail or document icon, name, status, and an x when removable."""

    def __init__(self, item: Attachment, on_remove: Callable[[], None] | None = None) -> None:
        super().__init__(raised=True)
        self.item = item
        row = QHBoxLayout(self)
        row.setContentsMargins(4, 4, 2 if on_remove else 8, 4)
        row.setSpacing(6)
        if item.thumb is not None:
            thumb = QLabel()
            thumb.setFixedSize(36, 36)
            thumb.setPixmap(item.thumb.scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                              Qt.TransformationMode.SmoothTransformation).copy(0, 0, 36, 36))
            row.addWidget(thumb)
        else:
            row.addWidget(_FileGlyph())
        text = QVBoxLayout()
        text.setSpacing(0)
        name = QLabel(item.name if len(item.name) <= 18 else item.name[:15] + "...")
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setFont(ui_font(8.5, bold=True))
        name.setStyleSheet(f"color: {TEXT_HEX};")
        self.status = QLabel()
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setFont(ui_font(7.5))
        text.addWidget(name)
        text.addWidget(self.status)
        row.addLayout(text)
        if on_remove is not None:
            close = IconButton("close", "Remove", size=22)
            close.clicked.connect(lambda _checked=False: on_remove())  # never leak `checked` into on_remove's defaults
            row.addWidget(close, 0, Qt.AlignmentFlag.AlignTop)
        self.refresh()

    def refresh(self) -> None:
        item = self.item
        bad = item.detail.startswith("Can't")
        self.status.setText("reading..." if item.pending else item.detail)
        self.status.setStyleSheet(f"color: {DANGER_HEX if bad else TEXT_DIM_HEX};")


class _Draft(QPlainTextEdit):
    """Message box: Enter sends, Shift+Enter adds a line, grows to 5 lines; pasted images or files attach."""

    submitted = pyqtSignal()
    attach_mime = pyqtSignal(QMimeData)
    MAX_LINES = 5

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFont(ui_font(10.5))
        self.setPlaceholderText("Ask anything")
        self.setStyleSheet(f"background: transparent; color: {TEXT_HEX};")
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTabChangesFocus(True)
        self.textChanged.connect(self._fit)
        self._fit()

    def keyPressEvent(self, event: QKeyEvent | None) -> None:
        if (event is not None and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.submitted.emit()
            return
        super().keyPressEvent(event)

    def canInsertFromMimeData(self, source: QMimeData | None) -> bool:
        return source is not None and (source.hasImage() or source.hasUrls() or super().canInsertFromMimeData(source))

    def insertFromMimeData(self, source: QMimeData | None) -> None:
        if source is not None and (source.hasImage() or any(u.isLocalFile() for u in source.urls())):
            self.attach_mime.emit(source)
            return
        super().insertFromMimeData(source)

    def _fit(self) -> None:
        line = self.fontMetrics().lineSpacing()
        lines = max(1, min(self.MAX_LINES, int(self.document().size().height())))
        self.setFixedHeight(lines * line + 10)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded if lines >= self.MAX_LINES
                                        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


class _ThinkingDots(QWidget):
    """Three yolk dots that rise and brighten in a travelling wave while an answer is coming."""

    def __init__(self) -> None:
        super().__init__()
        self.phase = 0.0  # seconds since the answer started; advanced by the chat's timer
        self.setFixedSize(26, 16)

    def paintEvent(self, _event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        color = QColor(ACCENT_HEX)
        for i in range(3):
            wave = max(0.0, math.sin((self.phase * 1.4 - i * 0.18) * 2 * math.pi))  # each dot a beat behind, then a rest
            color.setAlphaF(0.35 + 0.65 * wave)
            painter.setBrush(color)
            painter.drawEllipse(QPointF(4 + i * 9, 10 - 4 * wave), 2.6, 2.6)


class ChatText(QTextBrowser):
    """Read-only chat text sized to its content. Wraps long tokens anywhere, so a command or URL never widens the
    thread past the card (a QLabel only breaks at spaces). Markdown is for model replies only."""

    _MD = QTextDocument.MarkdownFeature(QTextDocument.MarkdownFeature.MarkdownDialectGitHub.value
                                        | QTextDocument.MarkdownFeature.MarkdownNoHTML.value)

    def __init__(self, text: str = "", *, markdown: bool = False, color: str = TEXT_HEX, pt: float = 10.0) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setOpenLinks(False)  # anchorClicked opens http(s) only; file: or app links in a reply are ignored
        self.anchorClicked.connect(lambda url: QDesktopServices.openUrl(url) if url.scheme() in ("http", "https") else None)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                     | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        self.setFont(ui_font(pt))
        self.setStyleSheet(f"QTextBrowser {{ background: transparent; color: {color}; }}")
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Link, QColor(ACCENT_HEX))
        self.setPalette(palette)
        self.document().setDocumentMargin(2)
        self.document().setIndentWidth(16)  # Qt's 40px list indent ate a ninth of the card
        policy = QSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.set_text(text, markdown=markdown)

    def set_text(self, text: str, *, markdown: bool = False) -> None:
        if markdown:
            self.document().setMarkdown(text, self._MD)
        else:
            self.setPlainText(text)
        # Airier lines and paragraph gaps; headings a step above body (Qt's are poster-sized); pet-palette links and code
        spans: list[tuple[int, int, QTextCharFormat]] = []
        block = self.document().begin()
        while block.isValid():
            cursor = QTextCursor(block)
            fmt = block.blockFormat()
            fmt.setLineHeight(125, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            fmt.setNonBreakableLines(False)  # code blocks wrap too: a wider document scrolled every line sideways
            # Qt tags code block lines on the block, not the characters
            code_block = markdown and any(fmt.hasProperty(prop) for prop in (
                QTextFormat.Property.BlockCodeFence, QTextFormat.Property.BlockCodeLanguage))
            if markdown:
                fmt.setBottomMargin(0 if code_block else 6)
            if code_block:  # one full-width panel, not a highlight per run
                fmt.setBackground(qcolor(GLASS_RAISED_RGBA))
            cursor.setBlockFormat(fmt)
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                char, style = frag.charFormat(), QTextCharFormat()
                if fmt.headingLevel():
                    style.setProperty(QTextCharFormat.Property.FontSizeAdjustment, 0)
                    style.setFontPointSize(self.font().pointSizeF() + (2 if fmt.headingLevel() == 1 else 1))
                    style.setFontWeight(700)
                if char.isAnchor():
                    style.setForeground(QColor(ACCENT_HEX))
                if code_block or char.fontFixedPitch():
                    style.setFontFamilies(["Cascadia Mono", "Consolas"])
                    style.setFontPointSize(self.font().pointSizeF() - 1)
                    style.setBackground(qcolor(GLASS_RAISED_RGBA))
                if style.properties():
                    spans.append((frag.position(), frag.position() + frag.length(), style))
                it += 1
            block = block.next()
        cursor = QTextCursor(self.document())
        for start, end, style in spans:  # applied after the walk: merging mid-walk splits the fragments being read
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            cursor.mergeCharFormat(style)
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self.horizontalScrollBar().setValue(0)
        if self.width() > 1:  # new text keeps no stale height (an approval's Allowed line was cut off)
            self.setFixedHeight(self.heightForWidth(self.width()))
        self.updateGeometry()

    def loadResource(self, _type: int, _name: QUrl) -> object:  # type: ignore[override]
        return None  # never load images or files a reply points at

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        # cached: every draft keystroke relays the whole thread, and an uncached call clones each row's document
        doc = self.document()
        key = (width, doc.revision(), doc.documentMargin(), self.font().key())
        if getattr(self, "_hfw_key", None) != key:
            measure = doc.clone()
            measure.setTextWidth(max(1, width))
            self._hfw_key, self._hfw = key, int(measure.size().height()) + 2
        return self._hfw

    def sizeHint(self) -> QSize:
        return QSize(200, self.heightForWidth(self.width() if self.width() > 1 else 340))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, 0)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self.setFixedHeight(self.heightForWidth(self.width()))
        self.horizontalScrollBar().setValue(0)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        event.ignore()  # let the thread scroll; this view never scrolls itself


class _Bubble(QFrame):
    """Your message in a soft rounded bubble, as in Claude Code; the text inside stays a plain ChatText."""

    def __init__(self, text: ChatText) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(text)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(qcolor(BUBBLE_RGBA))
        painter.drawRoundedRect(QRectF(self.rect()), 12, 12)
        painter.end()


class _Card(QFrame):
    """A non-reply row (permission, error, saved file): a raised paper sheet with a hairline edge, a round
    marker in the status colour and a dim header; errors and destructive prompts get a vermilion edge."""

    def __init__(self, title: str, color: str) -> None:
        super().__init__()
        self._color = QColor(color)
        self._head = QLabel()
        self._head.setTextFormat(Qt.TextFormat.PlainText)
        self._head.setFont(ui_font(8))
        self._head.setWordWrap(True)
        self._head.setContentsMargins(12, 0, 0, 0)  # room for the marker
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(8, 7, 8, 8)
        self.body.setSpacing(6)
        self.body.addWidget(self._head)
        self.set_header(title, color)

    def set_header(self, title: str, color: str) -> None:
        self._color = QColor(color)
        self._head.setText(title)
        self._head.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        alert = self._color.name().lower() == DANGER_HEX.lower()  # a coral frame only for errors and denials
        paint_panel(painter, self.rect(), qcolor(GLASS_RAISED_RGBA), self._color if alert else None, radius=8,
                    fold=False)
        head = self._head.geometry()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(QPointF(13, head.center().y() + 0.5), 3, 3)
        painter.end()


class _ClickLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class _HoverRow(QWidget):
    """A thread row whose Copy link shows only while the pointer is over the row; it keeps its space, so nothing jumps."""

    def __init__(self) -> None:
        super().__init__()
        self.link: QLabel | None = None

    def _show_link(self, shown: bool) -> None:
        if self.link is not None:
            self.link.setStyleSheet(f"color: {TEXT_DIM_HEX if shown else 'transparent'}; padding: 0 2px;"
                                    " text-decoration: underline;")

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self._show_link(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self._show_link(False)
        super().leaveEvent(event)


class _TaskList(QWidget):
    """The model's checklist (plan_tasks): one dim header, then a pixel marker and a line per task. A marker is hollow
    for pending, ice-blue for in progress and dim for done (that line is struck through). Clicking the header
    folds the list down to that header."""

    ROW = 17
    resized = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self._tasks: list[tuple[str, str]] = []
        self._collapsed = False
        self.setFont(ui_font(8.5))

    def set_tasks(self, tasks: list[tuple[str, str]]) -> None:
        self._tasks = list(tasks)
        rows = 1 if self._collapsed else len(tasks) + 1
        self.setFixedHeight(rows * self.ROW + 6 if tasks else 0)
        self.update()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event is not None and event.button() == Qt.MouseButton.LeftButton and event.position().y() < self.ROW + 2:
            self._collapsed = not self._collapsed
            self.set_tasks(self._tasks)
            self.resized.emit()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        dim, ice, text = QColor(TEXT_DIM_HEX), QColor(ACCENT_HEX), QColor(TEXT_HEX)
        font = self.font()
        painter.setFont(ui_font(8))
        painter.setPen(dim)
        done = sum(1 for _t, status in self._tasks if status == "done")
        painter.drawText(QRect(12, 2, self.width() - 16, self.ROW), Qt.AlignmentFlag.AlignVCenter,
                         f"{'+' if self._collapsed else '-'} Tasks  {done} of {len(self._tasks)} done")
        for n, (label, status) in enumerate([] if self._collapsed else self._tasks, 1):
            top = 2 + n * self.ROW
            dot = QPointF(15, top + self.ROW / 2)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            if status == "pending":
                painter.setPen(QPen(dim, 1.2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
            else:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(ice if status == "in_progress" else dim)
            painter.drawEllipse(dot, 3, 3)
            font.setStrikeOut(status == "done")
            painter.setFont(font)
            painter.setPen(dim if status == "done" else text)
            room = self.width() - 28
            painter.drawText(QRect(24, top, room, self.ROW), Qt.AlignmentFlag.AlignVCenter,
                             painter.fontMetrics().elidedText(label, Qt.TextElideMode.ElideRight, room - 8))
        painter.end()


class _ProcList(QWidget):
    """Background commands that are running, each with a Stop link."""

    def __init__(self, on_stop: Callable[[str], None]) -> None:
        super().__init__()
        self._on_stop = on_stop
        self._rows: list[tuple[str, str]] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 2, 8, 2)
        self._layout.setSpacing(2)

    def set_rows(self, rows: list[tuple[str, str]]) -> bool:
        """Rebuild when the running set changed; True if it did."""
        if rows == self._rows:
            return False
        self._rows = list(rows)
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        if rows:
            head = QLabel(f"Running in the background  {len(rows)}")
            head.setFont(ui_font(8))
            head.setStyleSheet(f"color: {TEXT_DIM_HEX};")
            self._layout.addWidget(head)
        for name, command in rows:
            line = QHBoxLayout()
            line.setContentsMargins(0, 0, 0, 0)
            label = QLabel()
            label.setFont(ui_font(8.5))
            label.setStyleSheet(f"color: {TEXT_HEX};")
            label.setToolTip(command)
            label.setText(label.fontMetrics().elidedText(f"{name}  {command}", Qt.TextElideMode.ElideRight, 250))
            stop = _ClickLabel("Stop")
            stop.setFont(ui_font(8))
            stop.setStyleSheet(f"color: {DANGER_HEX}; text-decoration: underline;")
            stop.setCursor(Qt.CursorShape.PointingHandCursor)
            stop.clicked.connect(lambda n=name: self._on_stop(n))
            line.addWidget(label, 1)
            line.addWidget(stop)
            holder = QWidget()
            holder.setLayout(line)
            self._layout.addWidget(holder)
        self.setVisible(bool(rows))
        return True
