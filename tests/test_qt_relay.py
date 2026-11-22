"""Worker results reach the Qt thread through the relay and are dropped once the job's owner is deleted."""

from __future__ import annotations

import threading

from PyQt6.QtCore import QCoreApplication, QEvent, QObject
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from src.tools.ai_assist import AiJob


def test_job_result_arrives_on_the_gui_thread_and_skips_a_deleted_job() -> None:
    app = QApplication.instance() or QApplication([])  # noqa: F841 - must stay referenced
    got: list[tuple[object, str, threading.Thread]] = []

    owner = QObject()
    job = AiJob(owner)
    job.finished.connect(lambda result, error: got.append((result, error, threading.current_thread())))
    job.start(lambda: "ok")
    for _ in range(100):
        QTest.qWait(10)
        if got:
            break
    assert got == [("ok", "", threading.main_thread())]

    # Owner closed while the worker still runs: nothing is delivered and nothing touches the dead job
    late: list[object] = []
    gate = threading.Event()
    owner2 = QObject()
    job2 = AiJob(owner2)
    job2.finished.connect(lambda result, error: late.append(result))
    job2.start(lambda: gate.wait(2) and "late")
    owner2.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    gate.set()
    QTest.qWait(200)
    assert late == []
    owner.deleteLater()
