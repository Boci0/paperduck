import pytest


@pytest.fixture(autouse=True)
def _no_explorer_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    # Drop tests would otherwise open real Explorer windows on pytest's temp folders
    import src.ui.pet_tools as pet_tools

    monkeypatch.setattr(pet_tools, "open_folder_in_background", lambda folder: None)


@pytest.fixture(autouse=True)
def _no_real_skills(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The user's own AI chat skills would otherwise leak into system prompts under test
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # Hundreds of test windows (pets, menus, wheels) left for interpreter exit to tear down crashed the process
    # on CI about 2 runs in 3 (0xC0000005 or exit 1 after every test passed). Delete them while Qt is whole.
    # deleteLater, not close: a PetWindow's closeEvent saves state, and LOCALAPPDATA is real again by now.
    try:
        from PyQt6.QtCore import QCoreApplication, QEvent
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        return
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    app.processEvents()
