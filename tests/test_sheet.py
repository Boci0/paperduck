"""make_sheet: spec checks, escaping, page counting and saving next to (never over) an existing PDF."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from src.tools import sheet


def _spec(**extra):
    return {"title": "LU5", "sections": [{"heading": "Connect", "points": ["`mysqli_connect()` opens", "**bold** <b>"]}],
            **extra}


def test_spec_is_checked_and_cleaned() -> None:
    got = sheet.check_spec(_spec(file_name="../Week 8?.pdf", accent="neon", columns=True))
    assert got["file_name"] == "Week 8" and got["accent"] == "blue" and got["columns"] == 1
    assert sheet.check_spec({"title": "", "sections": []}).startswith("error: give a title")
    assert sheet.check_spec({"title": "t", "sections": [{"heading": "h", "points": []}]}).startswith("error: section 1")
    assert "too much" in sheet.check_spec({"title": "t", "sections": [{"heading": "h", "points": ["x" * 13000]}]})
    big = sheet.check_spec({"title": "t", "sections": [{"heading": "h", "points": ["x" * 3600]}]})
    assert big["columns"] == 3


def test_html_escapes_text_and_marks_only_code_and_bold() -> None:
    page = sheet.sheet_html(sheet.check_spec(_spec()))
    assert "<code>mysqli_connect()</code> opens" in page and "<b>bold</b> &lt;b&gt;" in page
    assert "<script>" in page and page.count("<script>") == 1  # only the fitting script


def test_page_count_reads_page_objects(tmp_path: Path) -> None:
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 1 0 obj <</Type /Pages /Kids [2 0 R 3 0 R]>> 2 0 obj <</Type /Page>> "
                    b"3 0 obj <</Type/Page /Parent 1 0 R>>")
    assert sheet.page_count(pdf) == 2


def test_host_saves_beside_an_existing_pdf_and_attaches_the_preview(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.ui import ai_tool_host
    from src.ui.ai_tool_host import AiToolHost

    QApplication.instance() or QApplication([])
    out = tmp_path / "render"
    out.mkdir()
    pdf, png = out / "sheet.pdf", out / "sheet.png"
    pdf.write_bytes(b"%PDF <</Type /Page>>")
    img = QImage(20, 28, QImage.Format.Format_RGB32)
    img.fill(QColor("white"))
    img.save(str(png))
    monkeypatch.setattr(sheet, "render_sheet", lambda spec, folder, cancel=None: (pdf, png))
    docs = tmp_path / "Docs"
    monkeypatch.setattr(ai_tool_host.QStandardPaths, "writableLocation", lambda _loc: str(docs))
    (docs / "Paperduck").mkdir(parents=True)
    (docs / "Paperduck" / "sheet.pdf").write_bytes(b"the user's own")
    fake = SimpleNamespace(cancel=threading.Event(), _shot="")
    got = AiToolHost._make_sheet(fake, _spec())
    assert "Paperduck" in got and "sheet (2).pdf" in got and "fits on one page" in got
    assert (docs / "Paperduck" / "sheet.pdf").read_bytes() == b"the user's own"
    assert fake._shot.startswith("data:image/png")
    assert AiToolHost._make_sheet(fake, {"title": "x"}).startswith("error:")
    # Controlled Folder Access blocks Documents: fall back to Downloads
    (tmp_path / "Locked").write_bytes(b"")  # a file, so mkdir under it fails
    downloads = tmp_path / "Dl"
    monkeypatch.setattr(ai_tool_host.QStandardPaths, "writableLocation", lambda loc: str(
        downloads if loc == ai_tool_host.QStandardPaths.StandardLocation.DownloadLocation else tmp_path / "Locked"))
    got = AiToolHost._make_sheet(fake, _spec())
    assert (downloads / "Paperduck" / "sheet.pdf").exists() and (downloads / "Paperduck" / "sheet.png").exists()
    assert str(downloads / "Paperduck" / "sheet.png") in got and "Set image as background" in got
