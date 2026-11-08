"""AI chat attachments: chips from files, images, the clipboard and drag-and-drop (mixed into AiChat)."""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QMimeData, QPoint, QTimer
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QGuiApplication, QImage, QPixmap
from PyQt6.QtWidgets import QFileDialog, QMenu

from src.tools.ai_assist import MAX_INPUT_CHARS, BranchNote
from src.ui.chat_widgets import (
    _IMAGE_SUFFIXES,
    INLINE_FILE_CHARS,
    MAX_ATTACHMENTS,
    MAX_BRANCH_CHARS,
    Attachment,
    Chip,
    _size_text,
    place_chips,
    image_data_url,
    read_text_file,
)
from src.ui.context_menu import style_menu
from src.ui.quick_tools import OcrJob


class ChatAttachMixin:
    """Methods of AiChat; widgets and state live on AiChat itself."""


    def _attach_menu(self) -> None:
        menu = style_menu(QMenu(self))
        cb = QGuiApplication.clipboard()
        mime = cb.mimeData() if cb is not None else None
        has_clip = mime is not None and (mime.hasImage() or mime.hasUrls() or bool(mime.text().strip()))
        clip = menu.addAction("Clipboard")
        clip.setEnabled(has_clip)
        clip.triggered.connect(lambda _c=False: self.attach_mime(mime) if mime is not None else None)
        region = menu.addAction("Screen region")
        region.setEnabled(self._on_attach_screen is not None)
        region.triggered.connect(lambda _c=False: self._on_attach_screen() if self._on_attach_screen else None)
        menu.addAction("File...").triggered.connect(lambda _c=False: self._pick_files())
        menu.exec(self._plus.mapToGlobal(QPoint(0, -menu.sizeHint().height() - 4)))
        menu.deleteLater()

    # ------------------------------------------------------------------ branch inbox

    def add_branch_note(self, text: str, source: str) -> None:
        """Any thread: a background branch's post, held for the next tool round or send (oldest dropped past
        MAX_BRANCH_CHARS). The note row is drawn on the Qt thread."""
        text = text.strip()
        if not text:
            return
        with self._branch_lock:
            self._branch_q.append(BranchNote(text, source))
            while sum(map(len, self._branch_q)) > MAX_BRANCH_CHARS and len(self._branch_q) > 1:
                self._branch_q.pop(0)
        self._posted.emit(source, text)

    def _show_branch(self, source: str, text: str) -> None:
        self._add_row("note", f"{source}: {text[:240]}" + ("..." if len(text) > 240 else ""))
        self._relayout()

    def _pick_files(self) -> None:
        paths, _flt = QFileDialog.getOpenFileNames(self, "Paperduck AI - Attach Files")
        for path in paths:
            self.attach_path(Path(path))

    def attach_mime(self, mime: QMimeData) -> None:
        """Clipboard, paste or drop: images and local files become chips, plain text goes into the draft."""
        if mime.hasImage():
            image = mime.imageData()
            if isinstance(image, QImage) and not image.isNull():
                self.attach_image(image, "Pasted image")
                return
        files = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
        if files:
            for path in files:
                self.attach_path(path)
            return
        self.insert_text(mime.text())

    def attach_path(self, path: Path) -> None:
        if path.suffix.lower() in _IMAGE_SUFFIXES:
            image = QImage(str(path))
            if not image.isNull():
                self.attach_image(image, path.name)
                return
        item = Attachment("file", path.name)
        try:
            try:
                item.text = read_text_file(path)
            except ValueError as exc:
                if str(exc) != "too large":
                    raise
                item.path = str(path)  # no text read: the model opens it itself
                item.detail = f"{_size_text(path.stat().st_size)}, read on demand"
            else:
                lines = item.text.count("\n") + 1
                item.detail = f"{_size_text(path.stat().st_size)}, {lines} lines"
                if len(item.text) > INLINE_FILE_CHARS:
                    item.path = str(path)
                    item.detail += ", read on demand"
        except ValueError:
            item.detail = "Can't read: not a text file"
        except OSError:
            item.detail = "Can't read this file"
        self._add_attachment(item)

    def attach_image(self, image: QImage, name: str) -> None:
        """Image chips are sent as images, plus their offline Windows OCR text for models without vision."""
        item = Attachment("image", name, pending=True, thumb=QPixmap.fromImage(image), image_url=image_data_url(image))
        if not self._add_attachment(item):
            return
        job = OcrJob(self)
        job.finished.connect(lambda text, j=job, it=item: self._on_ocr(j, it, text))
        self._ocr_jobs.append(job)
        job.start(image)

    def _on_ocr(self, job: OcrJob, item: Attachment, text: str) -> None:
        if job in self._ocr_jobs:
            self._ocr_jobs.remove(job)
        job.deleteLater()
        item.pending = False
        item.text = text.strip()
        words = len(item.text.split())
        item.detail = f"{words} words read" if words else "Image, no text found"
        for chip in self._chips:
            if chip.item is item:
                chip.refresh()
        self._try_auto_send()

    def _add_attachment(self, item: Attachment) -> bool:
        if len(self._attachments) >= MAX_ATTACHMENTS:
            return False
        self._attachments.append(item)
        chip = Chip(item, on_remove=lambda it=item: self._remove_attachment(it))
        self._chips.append(chip)
        place_chips(self._chip_row, self._chips)
        self._chip_box.show()
        self._relayout()
        self._draft.setFocus()
        return True

    def _remove_attachment(self, item: Attachment) -> None:
        for chip in [c for c in self._chips if c.item is item]:
            self._chips.remove(chip)
            chip.deleteLater()
        place_chips(self._chip_row, self._chips)
        self._attachments = [a for a in self._attachments if a is not item]
        self._chip_box.setVisible(bool(self._attachments))
        QTimer.singleShot(0, self._relayout)

    def _clear_attachments(self) -> None:
        for chip in self._chips:
            chip.deleteLater()
        self._chips.clear()
        place_chips(self._chip_row, self._chips)
        self._attachments.clear()
        self._chip_box.hide()

    def insert_text(self, text: str) -> None:
        """Add text to the draft (never sends): the user still reviews it and presses Enter."""
        text = text.strip()[:MAX_INPUT_CHARS]
        if not text:
            return
        current = self._draft.toPlainText()
        self._draft.setPlainText(f"{current}\n\n{text}" if current.strip() else text)
        cursor = self._draft.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._draft.setTextCursor(cursor)
        self._draft.setFocus()

    def dragEnterEvent(self, event: QDragEnterEvent | None) -> None:
        if event is not None and event.mimeData() is not None and (
                event.mimeData().hasImage() or event.mimeData().hasUrls()):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent | None) -> None:
        if event is not None and event.mimeData() is not None:
            self.attach_mime(event.mimeData())
            event.acceptProposedAction()

    # ------------------------------------------------------------------ chat
