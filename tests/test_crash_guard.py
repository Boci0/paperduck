"""The crash guard keeps the app running after an error in a Qt slot and logs it."""

from __future__ import annotations

import faulthandler
import logging
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

import src.main as main_mod


def test_error_in_a_slot_is_logged_and_the_app_keeps_running(tmp_path, monkeypatch: pytest.MonkeyPatch,
                                                             caplog: pytest.LogCaptureFixture) -> None:
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(main_mod, "log_path", lambda: tmp_path / "paperduck.log")
    monkeypatch.setattr(main_mod, "_crash_file", None)
    monkeypatch.setattr(main_mod, "MAX_LOGGED_ERRORS", 2)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # restored after the test
    toasts: list[str] = []
    main_mod.install_crash_guard(lambda: toasts.append("OOPS"))
    try:
        def boom() -> None:
            raise RuntimeError("boom in a slot")

        alive: list[bool] = []
        for delay in (5, 10, 15):
            QTimer.singleShot(delay, boom)
        QTimer.singleShot(60, lambda: (alive.append(True), app.quit()))
        with caplog.at_level(logging.ERROR, logger="src.main"):
            app.exec()
        assert alive == [True]  # PyQt6 would have aborted the process with its default hook
        assert toasts == ["OOPS"] * 3
        assert sum("boom in a slot" in r.exc_text for r in caplog.records if r.exc_text) == 2  # capped
        assert (tmp_path / "crash.log").exists()
    finally:
        faulthandler.disable()
        if main_mod._crash_file is not None:
            main_mod._crash_file.close()


def test_crash_log_drops_handled_com_errors_and_keeps_real_crashes(tmp_path) -> None:
    stack = ('\nThread 0x000070b0 (most recent call first):\n  File "src/ui/quick_tools.py", line 436 in _run\n\n'
             'Current thread 0x000069a8 (most recent call first):\n  File "src/main.py", line 377 in main\n')
    real = "Windows fatal exception: access violation\n" + stack
    fatal = "Fatal Python error: Segmentation fault\n" + stack
    log = tmp_path / "crash.log"
    log.write_text("Windows fatal exception: code 0x8001010d\n" + stack + real
                   + "Windows fatal exception: code 0x80010108\n" + stack + fatal, encoding="utf-8")
    main_mod.prune_crash_log(log)
    assert log.read_text(encoding="utf-8") == real + fatal
    main_mod.prune_crash_log(tmp_path / "missing.log")  # no file: nothing to do, no error
