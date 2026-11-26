"""Cursor-anchored 1-line Quick Command Bar for zero-friction AI prompts."""

from __future__ import annotations

from PyQt6.QtCore import QEvent, QPoint, Qt, pyqtSignal
from PyQt6.QtGui import QCursor, QGuiApplication, QKeyEvent, QMouseEvent, QPainter
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from src.ui.theme import (
    ACCENT_HEX,
    HOVER_RGBA,
    OUTLINE_RGBA,
    SHADOW,
    TEXT_DIM_HEX,
    TEXT_HEX,
    paint_panel,
    rgba_css,
    ui_font,
)


class QuickCommandBar(QWidget):
    """Frameless, draggable 1-line prompt bar that opens at the mouse cursor."""

    submitted = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setFixedWidth(440)
        self._drag_offset: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SHADOW + 12, SHADOW // 2 + 8, SHADOW + 10, SHADOW * 3 // 2 + 8)
        layout.setSpacing(8)

        self._ctx = QLabel("", self)
        self._ctx.setFont(ui_font(8.5, bold=True))
        self._ctx.setFixedHeight(24)
        self._ctx.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._ctx.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._ctx.setStyleSheet(
            f"color: {ACCENT_HEX}; background: {rgba_css(OUTLINE_RGBA)}; "
            "border-radius: 6px; padding: 0 8px;"
        )
        self._ctx.hide()
        layout.addWidget(self._ctx, 0, Qt.AlignmentFlag.AlignVCenter)

        self._input = QLineEdit(self)
        self._input.setFont(ui_font(10))
        self._input.setFixedHeight(26)
        self._input.setPlaceholderText("Ask Paperduck...")
        self._input.setStyleSheet(
            f"QLineEdit {{ border: none; background: transparent; color: {TEXT_HEX}; padding: 0; }}"
        )
        self._input.returnPressed.connect(self._on_submit)
        layout.addWidget(self._input, 1, Qt.AlignmentFlag.AlignVCenter)

        self._close = QPushButton("×", self)
        self._close.setFont(ui_font(11, bold=True))
        self._close.setFixedSize(24, 24)
        self._close.setToolTip("Close (Esc)")
        self._close.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close.setStyleSheet(
            f"QPushButton {{ border: none; border-radius: 6px; color: {TEXT_DIM_HEX}; background: transparent; padding: 0; }}"
            f"QPushButton:hover {{ color: {TEXT_HEX}; background: {rgba_css(HOVER_RGBA)}; }}"
        )
        self._close.clicked.connect(self.hide)
        layout.addWidget(self._close, 0, Qt.AlignmentFlag.AlignVCenter)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        paint_panel(p, self.rect(), radius=12, fold=False, shadow=SHADOW)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_offset is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_offset is not None:
            self._drag_offset = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.ActivationChange and not self.isActiveWindow() and self._drag_offset is None:
            self.hide()
        super().changeEvent(event)

    def open_at_cursor(self, context_label: str = "") -> None:
        clean = " ".join((context_label or "").split())[:26]
        if clean:
            self._ctx.setText(clean)
            self._ctx.show()
        else:
            self._ctx.clear()
            self._ctx.hide()
        self._input.clear()
        self.adjustSize()

        pos = QCursor.pos()
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            x = max(area.left() + 8, min(pos.x() + 12, area.right() - self.width() - 8))
            y = max(area.top() + 8, min(pos.y() + 14, area.bottom() - self.height() - 8))
            self.move(x, y)
        self.show()
        self.raise_()
        self.activateWindow()
        self._input.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def _on_submit(self) -> None:
        text = self._input.text().strip()
        self.hide()
        if text:
            self.submitted.emit(text)
