"""Runs one macro recipe step by step on the Qt thread; snips and OCR resume the chain when they finish."""

from __future__ import annotations

import urllib.parse
from typing import Any

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QColor, QCursor, QGuiApplication, QImage

from src.tools.macros import Macro, parse_recipe
from src.ui.quick_tools import OcrJob, SnipSession
from src.ui.sfx import play_cue
from src.ui.tool_wheel import google_search_url

_BAD, _GOOD, _BUSY = QColor("#EF7D57"), QColor("#38B764"), QColor("#FFCD75")


class MacroRunner:
    """`window` is the PetWindow (toasts, hide/show for clean grabs, tool dispatch, the AI chat)."""

    def __init__(self, window: Any) -> None:
        self._win = window
        self._steps: list[tuple[str, str]] = []
        self._prompt: list[str] = []
        self._text = ""
        self._image: QImage | None = None
        self._ocr: OcrJob | None = None

    @property
    def running(self) -> bool:
        return bool(self._steps)

    def run(self, macro: Macro) -> None:
        if self.running:
            self._win._show_toast("BUSY", _BUSY)
            return
        try:
            self._steps = parse_recipe(macro.recipe)
        except ValueError:
            self._win._show_toast("BAD MACRO", _BAD)
            return
        self._prompt, self._text, self._image = [], "", None
        play_cue("macro")
        self._win._anim.play_oneshot("spin")
        self._win._schedule_next_anim_tick()
        self._win._show_toast(macro.name.upper()[:12], QColor("#B48EF2"))
        self._next()

    def _stop(self, toast: str = "") -> None:
        self._steps = []
        if toast:
            self._win._show_toast(toast, _BAD)
            play_cue("error")

    def _words(self, sep: str = "\n\n") -> str:
        return sep.join(p for p in (*self._prompt, self._text) if p.strip()).strip()

    def _next(self) -> None:
        if not self._steps or self._win._is_shutting_down:
            self._steps = []
            return
        name, arg = self._steps.pop(0)
        if not self._steps and name != "_end":
            self._steps = [("_end", "")]  # keeps `running` true until this last step has finished
        getattr(self, f"_do_{name.lstrip('_')}")(arg)

    # ---------------------------------------------------------------- inputs

    def _hide_then(self, then: Any) -> None:
        self._win.hide()  # keep the pet and the chat out of the frozen grab
        if self._win._ai_chat is not None:
            self._win._ai_chat.hide()
        QTimer.singleShot(180, then)

    def _do_region(self, _arg: str) -> None:
        def begin() -> None:
            if self._win._snip_session is not None:  # a plain snip is already open: never stack overlays
                self._win.show()
                self._stop("BUSY")
                return
            self._win._snip_session = SnipSession("image", done)

        def done(_mode: str, result: object | None) -> None:
            self._win._snip_session = None
            self._win.show()
            if not isinstance(result, QImage):
                self._stop("CANCEL")
                return
            self._image = result
            self._next()

        self._hide_then(begin)

    def _do_screen(self, _arg: str) -> None:
        def grab() -> None:
            screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            self._image = screen.grabWindow(0).toImage() if screen is not None else None
            self._win.show()
            if self._image is None or self._image.isNull():
                self._stop("NO SCREEN")
                return
            self._next()

        self._hide_then(grab)

    def _do_clipboard(self, _arg: str) -> None:
        cb = QGuiApplication.clipboard()
        mime = cb.mimeData() if cb is not None else None
        if mime is not None and mime.hasImage():
            image = cb.image()  # type: ignore[union-attr]
            self._image = image if not image.isNull() else None
        if mime is not None and mime.hasText():
            self._text = mime.text().strip()
        if self._image is None and not self._text:
            self._stop("CLIP EMPTY")
            return
        self._next()

    def _do_text(self, arg: str) -> None:
        self._prompt.append(arg)
        self._next()

    def _do_ambient(self, _arg: str) -> None:
        ambient = getattr(self._win, "ambient", None)
        if ambient is not None and (snap := ambient.snapshot_prompt()):
            self._prompt.append(snap)
        self._next()

    def _do_quick(self, _arg: str) -> None:
        self._win.open_quick_bar()
        bar = getattr(self._win, "_quick_bar", None)
        words = self._words(" ")
        if bar is not None and words:
            bar._input.setText(words)
            bar._input.setCursorPosition(len(words))
        self._next()

    def _do_ocr(self, _arg: str) -> None:
        if self._image is None:
            self._stop("NO IMAGE")
            return
        self._win._show_toast("READING", _BUSY)
        self._ocr = OcrJob(self._win)
        self._ocr.finished.connect(self._on_ocr)
        self._ocr.start(self._image)

    def _on_ocr(self, text: str) -> None:
        if self._ocr is not None:
            self._ocr.deleteLater()
            self._ocr = None
        if not text.strip():
            self._stop("NO TEXT")
            return
        self._text = text.strip()
        self._next()

    # ---------------------------------------------------------------- outputs

    def _do_ask(self, _arg: str, send: bool = True) -> None:
        chat = self._win.ensure_ai_chat()
        if chat is None:
            self._stop("NO AI")
            return
        if self._image is not None:
            chat.attach_image(self._image, "Macro snip")
        chat.insert_text(self._words())
        if send:
            chat.send_when_ready()
        self._next()

    def _do_chat(self, arg: str) -> None:
        self._do_ask(arg, send=False)

    def _do_copy(self, _arg: str, tries: int = 0) -> None:
        cb = QGuiApplication.clipboard()
        if cb is not None and self._words():
            cb.setText(self._words())
            landed = cb.mimeData().hasText()  # type: ignore[union-attr]
        elif cb is not None and self._image is not None:
            cb.setImage(self._image)
            landed = cb.mimeData().hasImage()  # type: ignore[union-attr]
        else:
            self._stop("NOTHING")
            return
        if not landed:  # the write fails silently while Windows clipboard history still reads a big clip
            if tries < 15:
                QTimer.singleShot(80, lambda: self._do_copy(_arg, tries + 1))
            else:
                self._stop("CLIP BUSY")
            return
        self._win._show_toast("COPIED", _GOOD)
        self._next()

    def _do_save(self, _arg: str) -> None:
        saved = self._win._save_snip_png(self._image) if self._image is not None else None
        if saved is None:
            self._stop("NOT SAVED")
            return
        self._win.recent.add(saved)
        self._win._show_toast("SAVED", _GOOD)
        self._next()

    def _do_search(self, _arg: str) -> None:
        query = " ".join(self._words(" ").split())[:500]
        if not query:
            self._stop("NO TEXT")
            return
        self._win.trigger_tool_action("web:" + google_search_url(query))
        self._next()

    def _do_open(self, arg: str) -> None:
        url = arg.replace("{text}", urllib.parse.quote_plus(" ".join(self._words(" ").split())[:500]))
        self._win.trigger_tool_action("web:" + url)
        self._next()

    def _do_tool(self, arg: str) -> None:
        self._win.trigger_tool_action(arg)
        self._next()

    def _do_end(self, _arg: str) -> None:
        self._steps = []
