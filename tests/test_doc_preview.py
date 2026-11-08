import sys
from pathlib import Path

import pytest

from src.tools.doc_preview import grid, page_range, preview_document


def test_page_range_reads_numbers_and_ranges_and_caps_them() -> None:
    assert page_range(None) == (1, 12) and page_range("3") == (3, 3) and page_range("2-5") == (2, 5)
    assert page_range("1-40") == (1, 12)  # one sheet holds MAX_PAGES
    for bad in ("0", "5-2", "x", "3-y"):
        with pytest.raises(ValueError):
            page_range(bad)


def test_grid_is_wider_than_tall() -> None:
    assert [grid(n) for n in (1, 2, 4, 5, 9, 12)] == [(1, 1), (2, 1), (2, 2), (3, 2), (3, 3), (4, 3)]


def test_macros_are_forced_off_before_any_office_file_opens() -> None:
    from src.tools.doc_preview import PREVIEW_PS

    off = PREVIEW_PS.index("$app.AutomationSecurity = 3")
    assert all(off < PREVIEW_PS.index(opener) for opener in ("Documents.Open", "Presentations.Open", "Workbooks.Open"))
    assert "$app.AutomationSecurity = $was" in PREVIEW_PS  # PowerPoint's shared instance gets the user's setting back


def test_bad_input_is_an_error_without_an_image(tmp_path: Path) -> None:
    assert preview_document(tmp_path / "a.txt")[0].startswith("error: preview_document reads")
    assert preview_document(tmp_path / "missing.pdf") == ("error: not a file", None)


@pytest.mark.skipif(sys.platform != "win32", reason="pages are drawn by Windows.Data.Pdf")
def test_a_pdf_becomes_one_contact_sheet(tmp_path: Path) -> None:
    from PyQt6.QtGui import QGuiApplication, QPainter, QPdfWriter
    from PyQt6.QtWidgets import QApplication

    _app = QApplication.instance() or QApplication([])
    pdf = tmp_path / "two pages.pdf"
    writer = QPdfWriter(str(pdf))
    painter = QPainter(writer)
    painter.drawText(100, 100, "first")
    writer.newPage()
    painter.drawText(100, 100, "second")
    painter.end()
    text, sheet = preview_document(pdf)
    assert text.startswith("two pages.pdf: 2 pages; the image shows pages 1 to 2") and sheet is not None
    assert sheet.width() > sheet.height() and max(sheet.width(), sheet.height()) <= 1568  # side by side, fits
    text, one = preview_document(pdf, "2")
    assert "shows page 2" in text and one is not None and one.height() > one.width()
    assert preview_document(pdf, "5")[0] == "error: the document has 2 pages"
    assert QGuiApplication.instance() is not None
