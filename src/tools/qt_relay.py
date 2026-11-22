"""Deliver a worker thread's signal on the Qt thread without touching a QObject the GUI may be deleting.

Emitting a job's own signal from its worker raced the GUI deleting that job (its chat closed): PyQt freed the
slot's lambda while the queued call was in flight, and Qt later called freed memory (native crash).
"""

from __future__ import annotations

import threading
from typing import Any

from PyQt6 import sip
from PyQt6.QtCore import QObject, pyqtSignal


class _Relay(QObject):
    call = pyqtSignal(object)


_relay: _Relay | None = None


def ensure_relay() -> None:
    """Call on the Qt thread (a job's start) so the relay, and the calls it runs, live there."""
    global _relay
    if _relay is None or sip.isdeleted(_relay):
        _relay = _Relay()
        _relay.call.connect(lambda fn: fn())


def emit_later(obj: QObject, signal: str, *args: Any) -> None:
    """From a worker thread: emit `obj.<signal>(*args)` on the Qt thread, skipped once `obj` was deleted."""
    if threading.current_thread() is threading.main_thread():  # already on the Qt thread: nothing to race
        getattr(obj, signal).emit(*args)
        return
    relay = _relay
    if relay is None:
        raise RuntimeError("ensure_relay() was not called on the Qt thread")
    try:
        relay.call.emit(lambda: None if sip.isdeleted(obj) else getattr(obj, signal).emit(*args))
    except RuntimeError:  # the app is quitting
        pass
