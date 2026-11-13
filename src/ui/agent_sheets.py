"""Sub-agent sheets: each running agent gets a paper sheet stacked with the chat card like a deck of paper, every
sheet behind the front one showing its header strip above it. A click on any sheet (the card included) deals it to
the front and the rest line up behind it in order, each snapping into its slot; a click on the front sheet brings
the card back. Every place comes from one base point, so dragging whichever sheet is in front moves the deck."""

from __future__ import annotations

import time

from PyQt6.QtCore import QPoint, QPointF, QRect, QSize, Qt, QTimer
from PyQt6.QtGui import QColor, QMouseEvent, QPainter
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QScrollArea, QVBoxLayout, QWidget

from src.ui.chat_widgets import ChatText, _Separator, _ThinkingDots
from src.ui.theme import (ACCENT_HEX, DANGER_HEX, FONT_FAMILY, GLASS_RAISED_RGBA, GLASS_RGBA, SHADOW, TEXT_DIM_HEX,
                          TEXT_HEX, paint_panel, qcolor, rgba_css, ui_font)

PEEK = 28  # px of each sheet's header strip showing above the sheet in front of it
STEP_X = 10  # each sheet further back sits this much to the right
MAX_LOG_LINES = 400
CLICK_SLOP = 4  # px a press may move and still count as a click (front / back), not a drag
LOG_DONE_H = 76  # px the tool log shrinks to once the report is in
FRONT_MIN_H = 360  # a sheet in front grows to at least this tall, so its log is readable over a short card


class _Marker(QWidget):
    """The 6px round status marker of the chat card's rows; stands in for the thinking dots once an agent stops."""

    def __init__(self) -> None:
        super().__init__()
        self.color = QColor(TEXT_DIM_HEX)
        self.setFixedSize(26, 16)  # the size of _ThinkingDots, which it replaces

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self.color)
        painter.drawEllipse(QPointF(7, 8), 3, 3)
        painter.end()


class AgentSheet(QWidget):
    """One sub-agent's live view: a header strip (status marker, number, status, effort, calls, time), its task, a
    compact tool log, then the report. The strip alone says running, done or failed when the sheet is at the back."""

    def __init__(self, stack: AgentSheets, n: int, effort: str, task: str) -> None:
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)  # appearing must not steal the draft's focus
        self.setWindowTitle(f"Paperduck agent {n}")
        self._stack, self.n, self.effort = stack, n, effort
        self.status, self.calls, self.started, self.ended = "running", 0, time.monotonic(), 0.0
        self._press: QPoint | None = None
        self._moved = False
        box = QVBoxLayout(self)
        box.setContentsMargins(14, 8, 14 + SHADOW, 12 + SHADOW)
        box.setSpacing(6)
        top = QHBoxLayout()
        top.setSpacing(2)
        self.dots = _ThinkingDots()  # while it runs; a still marker once it has stopped
        self.marker = _Marker()
        top.addWidget(self.dots)
        top.addWidget(self.marker)
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.RichText)
        self.title.setStyleSheet(f"color: {TEXT_HEX}; font-family: '{FONT_FAMILY}'; font-size: 9.5pt;")
        top.addWidget(self.title, 1)
        self.close_btn = QLabel("Close")  # hides only the view: a running agent keeps working
        self.close_btn.setStyleSheet(f"color: {TEXT_DIM_HEX}; font-family: '{FONT_FAMILY}'; font-size: 8.5pt;")
        # the sheet takes every press (click to deal, drag the deck), so Close is hit-tested in mouseReleaseEvent:
        # live, the label's own link click never fired
        self.close_btn.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        top.addWidget(self.close_btn)
        box.addLayout(top)
        self.task = QLabel(task[:400])
        self.task.setTextFormat(Qt.TextFormat.PlainText)
        self.task.setWordWrap(True)
        self.task.setStyleSheet(f"color: {TEXT_DIM_HEX}; font-family: '{FONT_FAMILY}'; font-size: 8.5pt;")
        box.addWidget(self.task)
        box.addWidget(_Separator())
        self.log = QPlainTextEdit()  # a raised Consolas panel in dim ink, so the calls never outweigh the report
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(MAX_LOG_LINES)
        self.log.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.log.setPlaceholderText("Waiting for its first tool call")
        self.log.setStyleSheet(f"QPlainTextEdit {{ background: {rgba_css(GLASS_RAISED_RGBA)}; color: {TEXT_DIM_HEX}; "
                               f"border-radius: 6px; padding: 2px 4px; font-family: 'Cascadia Mono', Consolas; "
                               f"font-size: 8pt; }}")
        box.addWidget(self.log, 1)
        self.report_head = QLabel()
        self.report_head.setFont(ui_font(8))
        self.report_head.setStyleSheet(f"color: {TEXT_DIM_HEX};")
        self.report = QScrollArea()  # the final report, rendered like a chat reply
        self.report.setWidgetResizable(True)
        self.report.setFrameShape(QFrame.Shape.NoFrame)
        self.report.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.report.setStyleSheet("QScrollArea { background: transparent; }")
        self.report.viewport().setAutoFillBackground(False)
        self.report_text: ChatText | None = None
        box.addWidget(self.report_head)
        box.addWidget(self.report, 1)
        self.report_head.hide()
        self.report.hide()
        self._wave = QTimer(self)  # the thinking dots' beat, only while running
        self._wave.setInterval(60)
        self._wave.timeout.connect(self._beat)
        self._wave.start()
        self.refresh()

    def _beat(self) -> None:
        self.dots.phase += 0.06
        self.dots.update()

    def add(self, kind: str, text: str) -> None:
        if kind == "tool":
            self.calls += 1
            self.log.appendPlainText(f"- {text}")
        else:
            self.status, self.ended = kind, time.monotonic()
            failed = kind == "failed"
            self.report_text = ChatText(text, markdown=not failed, color=DANGER_HEX if failed else TEXT_HEX, pt=9.5)
            self.report.setWidget(self.report_text)
            self.report_head.setText("Failed" if failed else "Report")
            self.report_head.show()
            self.report.show()
            self.log.setMaximumHeight(LOG_DONE_H)  # the report takes the room; the log keeps its newest lines
            self.log.verticalScrollBar().setValue(self.log.verticalScrollBar().maximum())
            self._wave.stop()
        self.refresh()

    def refresh(self) -> None:
        seconds = int((self.ended or time.monotonic()) - self.started)
        color = {"running": ACCENT_HEX, "failed": DANGER_HEX}.get(self.status, TEXT_DIM_HEX)
        running = self.status == "running"
        self.dots.setVisible(running)
        self.marker.setVisible(not running)
        self.marker.color = QColor(color)
        self.marker.update()
        self.title.setText(f"<b>Agent {self.n}</b> &nbsp;<span style='color:{color}'>{self.status}</span> &nbsp;"
                           f"<span style='color:{TEXT_DIM_HEX}'>{self.effort} effort, {self.calls} calls, "
                           f"{seconds} s</span>")
        self.update()

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        failed = self.status == "failed"  # a vermilion edge, like the chat card's error rows: visible on the strip
        paint_panel(painter, self.rect(), qcolor(GLASS_RGBA), QColor(DANGER_HEX) if failed else None, shadow=SHADOW)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent | None) -> None:
        if event is not None and event.button() == Qt.MouseButton.LeftButton:
            self._press, self._moved = event.globalPosition().toPoint(), False
            self.grabMouse()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent | None) -> None:
        if event is None or self._press is None:
            super().mouseMoveEvent(event)
            return
        now = event.globalPosition().toPoint()
        if not self._moved and (now - self._press).manhattanLength() < CLICK_SLOP:
            return
        self._moved = True
        self._stack.drag_by(now - self._press, self)
        self._press = now
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent | None) -> None:
        if self._press is None:
            super().mouseReleaseEvent(event)
            return
        self.releaseMouse()
        self._press = None
        if self._moved:
            return
        if event is not None and self.close_btn.geometry().contains(event.position().toPoint()):
            self._stack.close_sheet(self)
        else:
            self._stack.clicked(self)


class AgentSheets:
    """The deck of one chat card and its agents' sheets. `order` is front to back; slot k sits STEP_X * k right of
    and PEEK * k above `base`. `on_event(n, kind, text)` takes Agents' notify events on the Qt thread."""

    def __init__(self, card: QWidget) -> None:
        self.card = card
        self.sheets: list[AgentSheet] = []  # in start order
        self.order: list[QWidget] = [card]  # front to back
        self.base: QPoint | None = None  # where the front sheet's top-left sits
        self._placing = False
        self._timer = QTimer(card)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    @property
    def front(self) -> AgentSheet | None:
        """The agent sheet dealt in front of the card, if any."""
        top = self.order[0]
        return top if isinstance(top, AgentSheet) else None

    def on_event(self, n: int, kind: str, text: str) -> None:
        sheet = next((s for s in self.sheets if s.n == n), None)
        if kind == "start":
            effort, _, task = text.partition("\n")
            if sheet is not None:  # a new chat numbers its agents from 1 again
                self.close_sheet(sheet)
            sheet = AgentSheet(self, n, effort, task)
            self.sheets.append(sheet)
            self.order.append(sheet)  # new sheets join at the back
            if self.card.isVisible():
                sheet.show()
                self.place()
            self._timer.start()
        elif sheet is not None:
            sheet.add(kind, text)

    @staticmethod
    def _offset(k: int) -> QPoint:
        return QPoint(STEP_X * k, -PEEK * k)

    def place(self, restack: bool = True) -> None:
        """Snaps every sheet into its slot, sized like the front one, then (if `restack`) stacks them back to front.
        A drag passes restack=False: raising each window on every move flashes the back sheets over the front."""
        if self.base is None:
            self.base = self.card.pos() - self._offset(self.order.index(self.card))
        card = self.card.size()
        size = card if self.order[0] is self.card else QSize(card.width(), max(card.height(), FRONT_MIN_H))
        # shortcut: a deck at the very top of the screen pushes the back strips off it; deal downward if that annoys
        self._placing = True
        try:
            for k, widget in enumerate(self.order):
                pos = self.base + self._offset(k)
                if widget is self.card:
                    if self.card.pos() != pos:
                        self.card.move(pos)
                else:
                    widget.setGeometry(QRect(pos, size))
        finally:
            self._placing = False
        if restack:
            for widget in reversed(self.order):
                widget.raise_()

    def card_moved(self) -> None:
        """The card moved itself (dragged, or placed by the pet): the deck follows it from the card's slot."""
        if self._placing or not self.sheets:
            return
        self.base = self.card.pos() - self._offset(self.order.index(self.card))
        self.place(restack=False)

    def deal(self, widget: QWidget) -> None:
        """Brings `widget` to the front; the others keep their order behind it."""
        self.order.remove(widget)
        self.order.insert(0, widget)
        self.place()

    def clicked(self, sheet: AgentSheet) -> None:
        self.deal(self.card if sheet is self.order[0] else sheet)

    def drag_by(self, delta: QPoint, _sheet: AgentSheet) -> None:
        """Dragging the front sheet moves the deck."""
        if self.base is not None:
            self.base += delta
            self.place(restack=False)

    def close_sheet(self, sheet: AgentSheet) -> None:
        """Only the view: a running agent keeps working and still reports to the chat."""
        if sheet in self.sheets:
            self.sheets.remove(sheet)
        if sheet in self.order:
            self.order.remove(sheet)
        sheet.hide()
        sheet.deleteLater()
        if self.sheets:
            self.place()
        else:
            self.base = None

    def close_all(self) -> None:
        for sheet in list(self.sheets):
            self.close_sheet(sheet)

    def show_all(self) -> None:
        """The card opened again: it comes back to the front at its new place."""
        if not self.sheets:
            return
        self.order.remove(self.card)
        self.order.insert(0, self.card)
        self.base = None
        for sheet in self.sheets:
            sheet.show()
        self.place()

    def hide_all(self) -> None:
        for sheet in self.sheets:
            sheet.hide()

    def _tick(self) -> None:
        running = [s for s in self.sheets if s.status == "running"]
        for sheet in running:
            sheet.refresh()
        if not running:
            self._timer.stop()
