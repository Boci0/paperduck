"""Web image drops: fetch an image dragged out of a browser and save it as a re-encoded PNG.

Untrusted bytes are never written to disk or executed. They must start with a known image
signature, are decoded in memory by Qt and only the freshly encoded PNG is saved, so a renamed
executable is rejected and polyglot payloads do not survive.
"""

from __future__ import annotations

import base64
import binascii
import html
import http.client
import re
import sys
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtGui import QImage, QImageReader

from src.tools.file_forge import _unique_output_path

MAX_WEB_IMAGE_BYTES: int = 20 * 1024 * 1024
FETCH_TIMEOUT_S: float = 15.0
WEB_SCHEMES: frozenset[str] = frozenset({"http", "https"})
_STEM_RE = re.compile(r"[^A-Za-z0-9_-]+")


def sniff_image_format(head: bytes) -> str | None:
    """Return the image format from magic bytes, or None when it is not a known image."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    return None


def is_web_image_source(url: str) -> bool:
    scheme = urllib.parse.urlsplit(url).scheme.lower()
    return scheme in WEB_SCHEMES or url.lower().startswith("data:image/")


_IMG_SRC = re.compile(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def html_image_src(markup: str) -> str | None:
    """The first http(s)/data: <img src> in a browser drag's HTML (the picture, not the link around it)."""
    for src in _IMG_SRC.findall(markup or ""):
        url = html.unescape(src).strip()
        if is_web_image_source(url):
            return url
    return None


def web_image_stem(url: str) -> str:
    """Safe file stem from the URL's last path segment (never the server's extension)."""
    if url.lower().startswith("data:"):
        return "web_image"
    name = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem
    return _STEM_RE.sub("_", name).strip("_")[:40] or "web_image"


def fetch_image_bytes(
    url: str,
    *,
    max_bytes: int = MAX_WEB_IMAGE_BYTES,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> bytes:
    """Download (or decode a data: URL) and verify the bytes are an image. Raises ValueError."""
    if url.lower().startswith("data:"):
        header, _, payload = url.partition(",")
        if not header.lower().startswith("data:image/") or ";base64" not in header.lower():
            raise ValueError("unsupported data url")
        if len(payload) > max_bytes * 4 // 3 + 4:
            raise ValueError("too large")
        try:
            data = base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("bad data url") from exc
    else:
        if urllib.parse.urlsplit(url).scheme.lower() not in WEB_SCHEMES:
            raise ValueError("only http and https")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 Paperduck"})
        with opener(req, timeout=FETCH_TIMEOUT_S) as resp:  # type: ignore[operator]
            # Redirects may not leave http(s) (file:, ftp:, ...)
            if urllib.parse.urlsplit(resp.geturl()).scheme.lower() not in WEB_SCHEMES:
                raise ValueError("redirected off the web")
            data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("too large")
    if sniff_image_format(data[:16]) is None:
        raise ValueError("not an image")
    return data


def strip_metadata(image: QImage) -> QImage:
    """The same pixels with no text tags (Author, Comment, Description, ...), which Qt carries from a loaded file
    into PNG tEXt and JPEG COM on every save. Qt already drops EXIF (GPS, camera) blocks when it decodes."""
    if image.isNull() or not image.textKeys():
        return image
    bits = image.constBits()
    bits.setsize(image.sizeInBytes())
    clean = QImage(bits, image.width(), image.height(), image.bytesPerLine(), image.format()).copy()
    clean.setColorTable(image.colorTable())
    clean.setDotsPerMeterX(image.dotsPerMeterX())
    clean.setDotsPerMeterY(image.dotsPerMeterY())
    clean.setDevicePixelRatio(image.devicePixelRatio())
    return clean


def save_clean_png(image: QImage, dest_dir: Path, stem: str, source_url: str = "") -> Path:
    """Write a re-encoded PNG without metadata and tag it with the Windows Mark of the Web."""
    if image.isNull():
        raise ValueError("not an image")
    image = strip_metadata(image)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_output_path(dest_dir, stem, ".png")
    if not image.save(str(dest), "PNG"):
        raise OSError("could not save image")
    if sys.platform == "win32":
        host = source_url if source_url.lower().startswith(("http://", "https://")) else ""
        try:
            with open(f"{dest}:Zone.Identifier", "w", encoding="ascii", errors="ignore") as ads:
                ads.write(f"[ZoneTransfer]\nZoneId=3\nHostUrl={host}\n")  # text mode writes CRLF
        except OSError:
            pass  # non-NTFS volume: the re-encode is the real protection
    return dest


def decode_image_bytes(data: bytes) -> QImage:
    QImageReader.setAllocationLimit(256)  # MiB; stops decompression bombs
    image = QImage()
    if not image.loadFromData(data):
        raise ValueError("not an image")
    return image


def load_image_file(path: Path) -> QImage:
    """An image file for a model to look at: size capped, known formats only, decode allocation limited.
    Raises ValueError with a short reason."""
    if not path.is_file():
        raise ValueError("not a file")
    if path.stat().st_size > MAX_WEB_IMAGE_BYTES:
        raise ValueError("image larger than 20 MB")
    with path.open("rb") as fh:
        data = fh.read(MAX_WEB_IMAGE_BYTES + 1)
    if sniff_image_format(data[:16]) is None:
        raise ValueError("not a png, jpg, gif, webp or bmp image")
    return decode_image_bytes(data)


class WebImageJob(QObject):
    """Fetches and sanitizes a dropped web image on a worker thread.

    `finished` delivers (saved PNG path or None, error text) on the Qt main thread.
    """

    finished = pyqtSignal(object, str)

    def start(self, source: str | QImage, dest_dir: Path) -> None:
        threading.Thread(target=self._run, args=(source, dest_dir), daemon=True).start()

    def _run(self, source: str | QImage, dest_dir: Path) -> None:
        try:
            if isinstance(source, QImage):
                path = save_clean_png(source, dest_dir, "web_image")
            else:
                image = decode_image_bytes(fetch_image_bytes(source))
                path = save_clean_png(image, dest_dir, web_image_stem(source), source)
        except (ValueError, OSError, http.client.HTTPException) as exc:
            self.finished.emit(None, str(exc))
            return
        self.finished.emit(path, "")
