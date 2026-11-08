"""Files and images the pet produces carry no metadata from their source: no text tags, no EXIF/GPS."""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import pytest
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QApplication

from src.tools import file_forge
from src.tools.web_drop import save_clean_png, strip_metadata

MARK = b"SECRET_GPS_51.5N"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])  # type: ignore[return-value]


def _jpeg_markers(data: bytes) -> list[int]:
    """Segment markers before the image data (a byte search would also hit FF FE inside compressed pixels)."""
    markers, i = [], 2
    while i + 4 <= len(data) and data[i] == 0xFF and data[i + 1] != 0xDA:
        markers.append(data[i + 1])
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    return markers


def _png_chunks(data: bytes) -> list[bytes]:
    chunks, i = [], 8
    while i < len(data):
        size = struct.unpack(">I", data[i:i + 4])[0]
        chunks.append(data[i + 4:i + 8])
        i += 12 + size
    return chunks


def _leaks(data: bytes) -> list[str]:
    found = [name for name, needle in {"marker": MARK, "author": b"Jane Victim"}.items() if needle in data]
    if data.startswith(b"\x89PNG"):
        found += [c.decode() for c in _png_chunks(data) if c in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME")]
    elif data.startswith(b"\xff\xd8"):
        found += [hex(m) for m in _jpeg_markers(data) if m in (0xE1, 0xED, 0xFE)]  # EXIF/XMP, IPTC, comment
    return found


@pytest.fixture
def tagged(qapp, tmp_path: Path) -> list[Path]:  # noqa: ANN001
    """A PNG with tEXt tags plus an eXIf chunk, and a JPEG with an APP1 Exif block plus a COM comment."""
    img = QImage(40, 30, QImage.Format.Format_RGB32)
    img.fill(QColor("#3366aa"))
    img.setPixelColor(3, 4, QColor("#ff0000"))
    img.setText("Author", "Jane Victim")
    img.setText("Comment", MARK.decode())
    png, jpg = tmp_path / "src.png", tmp_path / "src.jpg"
    assert img.save(str(png), "PNG") and img.save(str(jpg), "JPEG")
    exif = b"Exif\0\0MM\0*\0\0\0\x08" + MARK
    raw = png.read_bytes()
    at = raw.index(b"IDAT") - 4
    body = b"eXIf" + exif[6:]
    png.write_bytes(raw[:at] + struct.pack(">I", len(exif) - 6) + body + struct.pack(">I", zlib.crc32(body)) + raw[at:])
    raw = jpg.read_bytes()
    jpg.write_bytes(raw[:2] + b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif
                    + b"\xff\xfe" + struct.pack(">H", len(MARK) + 2) + MARK + raw[2:])
    for path in (png, jpg):
        assert MARK in path.read_bytes()
    return [png, jpg]


def test_strip_metadata_keeps_pixels_and_drops_text(qapp) -> None:  # noqa: ANN001
    for fmt in (QImage.Format.Format_ARGB32, QImage.Format.Format_Indexed8):
        img = QImage(9, 7, QImage.Format.Format_ARGB32)
        img.fill(QColor(10, 200, 30, 128))
        img = img.convertToFormat(fmt)
        img.setText("Author", "Jane Victim")
        clean = strip_metadata(img)
        assert clean.textKeys() == [] and clean == img and img.textKeys() == ["Author"]


def test_web_drops_forge_outputs_and_ai_images_carry_no_metadata(tagged: list[Path], tmp_path: Path) -> None:
    for src in tagged:
        image = QImage(str(src))
        assert image.textKeys()  # Qt does carry the tags in: this is what must not come out
        assert _leaks(save_clean_png(image, tmp_path / "drops", "drop").read_bytes()) == []
        from src.ui.chat_widgets import image_data_url

        assert _leaks(base64.b64decode(image_data_url(image).split(",", 1)[1])) == []
        for fmt in ("png", "jpg", "webp", "bmp", "ico"):
            out = tmp_path / f"forge_{src.suffix[1:]}.{fmt}"
            assert file_forge.convert_image_file(src, fmt, output_path=out).ok
            assert _leaks(out.read_bytes()) == [], (src.name, fmt)


def _chrome_like_pdf(title: bytes = b"(page.html)") -> bytes:
    """A small PDF shaped like headless Chrome's: an uncompressed Info dict, a classic xref table."""
    info = (b"<</Title " + title + rb" /Creator (Mozilla/5.0 \(Windows NT 10.0\) HeadlessChrome/156.0 Edg/156)"
            b" /Producer (Skia/PDF m156) /Author <FEFF004A0061006E0065> /CreationDate (D:20261106094642+08'00')"
            b" /ModDate (D:20261106094642+08'00')>>")
    objs = [info, b"<</Type /Catalog /Pages 3 0 R /Lang (en-US)>>", b"<</Type /Pages /Kids [4 0 R] /Count 1>>",
            b"<</Type /Page /Parent 3 0 R /MediaBox [0 0 200 200]>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<</Size %d /Root 2 0 R /Info 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


def _xref_ok(data: bytes) -> bool:
    import re

    start = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
    rows = re.findall(rb"(\d{10}) (\d{5}) n", data[start:])
    return all(re.match(rb"%d\s+0\s+obj" % (n + 1), data[int(off):]) for n, (off, _g) in enumerate(rows))


def _info(data: bytes) -> dict[bytes, bytes]:
    import re

    body = data[data.index(b"1 0 obj"):data.index(b"endobj")]
    return dict(re.findall(rb"/(\w+) \(([^)]*)\)", body))


def test_scrub_pdf_swaps_maker_and_dates_for_the_duck_in_place(tmp_path: Path) -> None:
    pdf = tmp_path / "out.pdf"
    pdf.write_bytes(_chrome_like_pdf())
    before = pdf.read_bytes()
    assert file_forge.scrub_metadata(pdf, dry_run=True) == "would clean" and pdf.read_bytes() == before
    assert file_forge.scrub_metadata(pdf) == "cleaned"
    after = pdf.read_bytes()
    assert len(after) == len(before) and _xref_ok(after)  # same length: every offset still points at its object
    for leak in (b"HeadlessChrome", b"Skia", b"D:2026", b"page.html", b"004A0061", b"Author", b"Date"):
        assert leak not in after, leak
    info = _info(after)
    assert info.keys() == {b"Producer", b"Creator"} and info[b"Producer"] == file_forge.PDF_PRODUCER
    assert info[b"Creator"] in file_forge.PDF_CREATORS
    assert b"/Lang (en-US)" in after and b"/Type /Catalog" in after and b"/Info 1 0 R" in after
    assert file_forge.scrub_metadata(pdf) == "nothing to remove" and pdf.read_bytes() == after  # stable joke

    for title in (rb"(Quarterly report \(draft\))", b"(Report: Q3 2026)"):
        pdf.write_bytes(_chrome_like_pdf(title))
        assert file_forge.scrub_metadata(pdf) == "cleaned"
        assert title in pdf.read_bytes() and _xref_ok(pdf.read_bytes())  # a real title the user wrote stays
    for path_title in (rb"(C:\\Users\\me\\page.html)", b"(file:///C:/Users/me/x)", b"(report.html)"):
        pdf.write_bytes(_chrome_like_pdf(path_title))
        assert file_forge.scrub_metadata(pdf) == "cleaned" and b"Title" not in pdf.read_bytes()


def test_scrub_png_and_jpeg_are_lossless(tagged: list[Path]) -> None:
    for src in tagged:
        pixels = QImage(str(src))
        assert _leaks(src.read_bytes())
        assert file_forge.scrub_metadata(src) == "cleaned"
        assert _leaks(src.read_bytes()) == [] and QImage(str(src)) == pixels.convertToFormat(QImage(str(src)).format())
        assert file_forge.scrub_metadata(src) == "nothing to remove"


def test_scrub_refuses_what_it_cannot_clean_safely(tmp_path: Path) -> None:
    for name, data in {"a.txt": b"hello", "cut.png": b"\x89PNG\r\n\x1a\n\0\0\0\x10IHDR",
                       "packed.pdf": b"%PDF-1.7\n1 0 obj<</Type /ObjStm>>endobj trailer<</Info 1 0 R>>"}.items():
        path = tmp_path / name
        path.write_bytes(data)
        assert file_forge.scrub_metadata(path).startswith("error:")
        assert path.read_bytes() == data
    assert file_forge.scrub_metadata(tmp_path / "missing.pdf").startswith("error:")


def test_print_to_pdf_paths(tmp_path: Path) -> None:
    from src.tools.ai_tools import print_to_pdf_paths

    home = tmp_path.resolve()
    assert print_to_pdf_paths('msedge --headless --print-to-pdf="a b.pdf" file:///x.html', home) == [home / "a b.pdf"]
    assert print_to_pdf_paths(r"msedge --headless --print-to-pdf=out\r.pdf page.html", home) == [home / "out" / "r.pdf"]
    assert print_to_pdf_paths("chrome --headless --print-to-pdf page.html", home) == [home / "output.pdf"]
    assert print_to_pdf_paths("npx playwright pdf page.html", home) == []


def test_agent_commands_and_tool_leave_clean_files(qapp, tmp_path: Path) -> None:  # noqa: ANN001
    import threading
    from types import SimpleNamespace

    from PyQt6.QtCore import QObject

    from src.ui.ai_tool_host import AiToolHost

    (tmp_path / "src.pdf").write_bytes(_chrome_like_pdf())
    (tmp_path / "loose.pdf").write_bytes(_chrome_like_pdf())

    class Chat:
        def confirm(self, key, text, what, done):  # noqa: ANN001
            done(True)

    class Window(QObject):
        _ai_chat = Chat()
        _state_machine = SimpleNamespace(registry=SimpleNamespace(get_plugin=lambda _n: SimpleNamespace(repo=tmp_path)))

    host = AiToolHost(Window(), mcp=SimpleNamespace(server_of=lambda n: None, specs=lambda: [], close=lambda: None))
    results: list[str] = []

    def worker() -> None:
        # stands in for msedge: makes the file, and names it the way a headless print does
        results.append(host.run("run_command", {"command": "copy /y src.pdf made.pdf >nul & rem --print-to-pdf=made.pdf"}))
        results.append(host.run("clean_metadata", {"path": "loose.pdf"}))
        results.append(host.run("clean_metadata", {"path": "nope.pdf"}))

    thread = threading.Thread(target=worker)
    thread.start()
    while thread.is_alive():
        qapp.processEvents()
    assert results[0].endswith("[Paperduck: made.pdf: metadata cleaned]"), results[0]
    assert results[1:] == ["cleaned", "error: not a file"]
    for name in ("made.pdf", "loose.pdf"):
        assert b"HeadlessChrome" not in (tmp_path / name).read_bytes()
    assert b"HeadlessChrome" in (tmp_path / "src.pdf").read_bytes()  # only what the command made is touched


def test_wait_for_new_file_waits_for_a_late_or_rewritten_file(tmp_path: Path) -> None:
    import threading
    import time

    from src.tools.ai_tools import file_stamp, wait_for_new_file

    late = tmp_path / "late.pdf"
    threading.Timer(0.8, lambda: late.write_bytes(b"%PDF-new")).start()  # msedge.exe returns before this
    t = time.monotonic()
    assert wait_for_new_file(late, file_stamp(late), 5) and 0.8 <= time.monotonic() - t < 3

    old = tmp_path / "old.pdf"
    old.write_bytes(b"%PDF-old")
    before = file_stamp(old)
    assert not wait_for_new_file(old, before, 0.6)  # an untouched old file is not taken for the new one
    threading.Timer(0.5, lambda: old.write_bytes(b"%PDF-newer one")).start()
    assert wait_for_new_file(old, before, 5) and old.read_bytes() == b"%PDF-newer one"
    assert not wait_for_new_file(tmp_path / "never.pdf", None, 0.5)


def test_commands_that_make_tagged_files_get_a_reminder(qapp, tagged: list[Path], tmp_path: Path) -> None:  # noqa: ANN001
    import shutil
    import threading
    from types import SimpleNamespace

    from PyQt6.QtCore import QObject

    from src.ui.ai_tool_host import AiToolHost

    work = tmp_path / "work"
    work.mkdir()
    shutil.copy(tagged[0], work / "tagged_src.png")
    (work / "already.png").write_bytes((work / "tagged_src.png").read_bytes())  # there before: not this command's

    class Chat:
        def confirm(self, key, text, what, done):  # noqa: ANN001
            done(True)

    class Window(QObject):
        _ai_chat = Chat()
        _state_machine = SimpleNamespace(registry=SimpleNamespace(get_plugin=lambda _n: SimpleNamespace(repo=work)))

    host = AiToolHost(Window(), mcp=SimpleNamespace(server_of=lambda n: None, specs=lambda: [], close=lambda: None))
    results: list[str] = []

    def worker() -> None:
        results.append(host.run("run_command", {"command": "copy /y tagged_src.png chart.png >nul"}))
        results.append(host.run("clean_metadata", {"path": "chart.png"}))
        results.append(host.run("run_command", {"command": "copy /y chart.png chart2.png >nul"}))  # clean copy

    thread = threading.Thread(target=worker)
    thread.start()
    while thread.is_alive():
        qapp.processEvents()
    assert "chart.png, which still carry metadata" in results[0] and "already.png" not in results[0]
    assert "tagged_src.png" not in results[0] and results[1] == "cleaned"
    assert "[Paperduck" not in results[2]
    assert _leaks((work / "chart.png").read_bytes()) == [] and _leaks((work / "already.png").read_bytes())
