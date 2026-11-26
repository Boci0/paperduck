"""Streaming zero-dependency File Converter and Compressor plugin for Paperduck."""

from __future__ import annotations

import bz2
import concurrent.futures
import csv
import datetime as dt
import gzip
import hashlib
import json
import lzma
import os
import random
import re
import shutil
import struct
import tarfile
import threading
import tomllib
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from src.engine.models import CompanionState, HudBar, HudData, ToolMenuItem

STREAM_CHUNK_BYTES: int = 1_048_576  # 1 MiB constant-memory streaming buffer
DEFAULT_MAX_EXTRACT_BYTES: int = 2 * 1024 * 1024 * 1024  # 2 GiB archive-bomb guard

# Formats with high internal entropy stored via ZIP_STORED to avoid wasted deflate CPU
_PRECOMPRESSED_EXTS: frozenset[str] = frozenset(
    {
        ".zip",
        ".gz",
        ".tgz",
        ".xz",
        ".txz",
        ".bz2",
        ".tbz2",
        ".7z",
        ".rar",
        ".zst",
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".mp3",
        ".aac",
        ".ogg",
        ".opus",
        ".flac",
        ".m4a",
        ".jpg",
        ".jpeg",
        ".webp",
        ".avif",
        ".heic",
        ".png",
        ".woff2",
    }
)

_IMAGE_EXTS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".jfif",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
        ".gif",
        ".svg",
        ".svgz",
        ".ico",
        ".icns",
        ".tga",
        ".ppm",
        ".pgm",
        ".pbm",
        ".xbm",
        ".xpm",
    }
)
_IMAGE_SUFFIXES: tuple[str, ...] = tuple(_IMAGE_EXTS)  # str.endswith needs a tuple

_COMPOUND_ARCHIVE_SUFFIXES: tuple[str, ...] = (
    ".tar.xz",
    ".tar.gz",
    ".tar.bz2",
)

_ARCHIVE_EXTS: frozenset[str] = frozenset(
    {
        ".zip",
        ".tar",
        ".tgz",
        ".txz",
        ".tbz2",
        ".gz",
        ".xz",
        ".bz2",
    }
)

_DATA_EXTS: frozenset[str] = frozenset({".json", ".csv", ".tsv", ".toml"})

_TEXT_EXTS: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".log",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".html",
        ".css",
        ".xml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".sql",
        ".sh",
        ".ps1",
        ".bat",
        ".c",
        ".cpp",
        ".h",
        ".rs",
        ".go",
        ".java",
        ".dart",
    }
)


class ForgeMode(str, Enum):
    """Operational conversion/compression preset for FileForgePlugin."""

    AUTO = "auto"
    ZIP = "zip"
    TAR_XZ = "txz"
    TAR_GZ = "tgz"
    GZIP = "gz"
    EXTRACT = "extract"
    WEBP = "webp"
    PNG = "png"
    JPG = "jpg"
    ICO = "ico"
    BMP = "bmp"
    DATA = "data"
    JSON_PRETTY = "json_pretty"
    JSON_MIN = "json_min"
    CSV = "csv"
    TSV = "tsv"
    CLEAN_TEXT = "clean_txt"
    SHA256 = "sha256"


@dataclass(frozen=True)
class ForgeWheelOption:
    """Single radial wheel slice specification for interactive file hover selection."""

    mode: ForgeMode
    label: str
    subtitle: str
    color_hex: str
    preview_suffix: str


@dataclass(frozen=True)
class ForgeResult:
    """Outcome summary of a completed conversion or compression batch."""

    ok: bool
    output_paths: tuple[Path, ...]
    source_bytes: int
    output_bytes: int
    toast_text: str
    toast_color: str

    @property
    def saved_percent(self) -> float:
        if not self.ok or self.source_bytes <= 0 or self.output_bytes >= self.source_bytes:
            return 0.0
        ratio = (1.0 - (self.output_bytes / self.source_bytes)) * 100.0
        return max(0.0, min(100.0, round(ratio, 1)))


def _is_archive_path(path: Path) -> bool:
    lower_name = path.name.lower()
    if any(lower_name.endswith(sfx) for sfx in _COMPOUND_ARCHIVE_SUFFIXES):
        return True
    return path.suffix.lower() in _ARCHIVE_EXTS


def _strip_archive_stem(path: Path) -> str:
    lower_name = path.name.lower()
    for sfx in _COMPOUND_ARCHIVE_SUFFIXES:
        if lower_name.endswith(sfx) and len(path.name) > len(sfx):
            return path.name[: -len(sfx)]
    return path.stem or "extracted"


def _unique_output_path(
    directory: Path,
    stem: str,
    suffix: str,
    avoid_path: Path | None = None,
) -> Path:
    """Return a collision-free sibling file path without overwriting user files."""
    clean_stem = stem or "output"
    candidate = directory / f"{clean_stem}{suffix}"
    if avoid_path is not None:
        try:
            if candidate.resolve() == avoid_path.resolve():
                clean_stem = f"{clean_stem}_opt"
                candidate = directory / f"{clean_stem}{suffix}"
        except OSError:
            pass

    counter = 1
    while candidate.exists():
        candidate = directory / f"{clean_stem}_{counter}{suffix}"
        counter += 1
    return candidate


def _unique_output_dir(directory: Path, folder_name: str) -> Path:
    """Return a collision-free destination directory path."""
    clean_name = folder_name or "extracted"
    candidate = directory / clean_name
    counter = 1
    while candidate.exists():
        candidate = directory / f"{clean_name}_{counter}"
        counter += 1
    return candidate


def _temp_sibling_path(dest: Path) -> Path:
    return dest.with_name(f".forge_{os.getpid()}_{threading.get_ident()}_{dest.name}.tmp")


def _scan_files_fast(
    roots: Sequence[Path],
    skip_resolved: set[Path],
) -> tuple[list[tuple[Path, str, int]], list[str], int]:
    """Iteratively walk roots via os.scandir using cached Win32 DirEntry sizes."""
    files: list[tuple[Path, str, int]] = []
    empty_dirs: list[str] = []
    total_bytes = 0
    multi = len(roots) > 1

    for root in roots:
        try:
            resolved_root = root.resolve()
        except OSError:
            continue
        if resolved_root in skip_resolved or not root.exists():
            continue

        if root.is_file():
            try:
                sz = root.stat().st_size
            except OSError:
                continue
            files.append((root, root.name, sz))
            total_bytes += sz
            continue

        if not root.is_dir():
            continue

        base_parent = root.parent if multi else root
        prefix_root = root.name if multi else ""
        stack: list[Path] = [root]

        while stack:
            current_dir = stack.pop()
            has_entries = False
            try:
                with os.scandir(current_dir) as it:
                    entries = sorted(it, key=lambda e: e.name.lower())
            except OSError:
                continue

            sub_dirs: list[Path] = []
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    entry_path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        has_entries = True
                        sub_dirs.append(entry_path)
                    elif entry.is_file(follow_symlinks=False):
                        if entry_path.name.startswith(".forge_") and entry_path.name.endswith(".tmp"):
                            continue
                        if entry_path.resolve() in skip_resolved:
                            continue
                        has_entries = True
                        sz = entry.stat(follow_symlinks=False).st_size
                        arcname = entry_path.relative_to(base_parent).as_posix()
                        files.append((entry_path, arcname, sz))
                        total_bytes += sz
                except OSError:
                    continue

            if not has_entries:
                rel_dir = current_dir.relative_to(base_parent).as_posix()
                if rel_dir and rel_dir != ".":
                    empty_dirs.append(rel_dir.rstrip("/") + "/")
                elif prefix_root:
                    empty_dirs.append(prefix_root.rstrip("/") + "/")

            for d in reversed(sub_dirs):
                stack.append(d)

    return (files, empty_dirs, total_bytes)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("Operation cancelled")


def compress_to_zip(
    paths: Sequence[Path],
    output_path: Path | None = None,
    *,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ForgeResult:
    """Stream files/directories into a .zip archive with pre-compressed fast-bypass."""
    valid = [Path(p) for p in paths if Path(p).exists()]
    if not valid:
        return ForgeResult(False, (), 0, 0, "NO FILE", "#EF7D57")

    first = valid[0]
    stem = first.stem if (len(valid) == 1 and first.is_file()) else (first.name if len(valid) == 1 else f"{first.stem}_bundle")
    dest = output_path or _unique_output_path(first.parent, stem, ".zip", avoid_path=first)
    temp_path = _temp_sibling_path(dest)
    skip = {dest.resolve(), temp_path.resolve()} if dest.parent.exists() else set()

    files, empty_dirs, total_bytes = _scan_files_fast(valid, skip)
    processed = 0
    buf = bytearray(STREAM_CHUNK_BYTES)
    view = memoryview(buf)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(
            temp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as zf:
            for d_arc in empty_dirs:
                _check_cancel(cancel_event)
                zf.mkdir(d_arc)

            for src_file, arcname, _sz in files:
                _check_cancel(cancel_event)
                ext = src_file.suffix.lower()
                method = (
                    zipfile.ZIP_STORED
                    if ext in _PRECOMPRESSED_EXTS
                    else zipfile.ZIP_DEFLATED
                )
                try:
                    zinfo = zipfile.ZipInfo.from_file(src_file, arcname=arcname)
                except ValueError:
                    zinfo = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                    zinfo.file_size = src_file.stat().st_size
                if zinfo.date_time[0] < 1980:
                    zinfo.date_time = (1980, 1, 1, 0, 0, 0)
                zinfo.compress_type = method

                with src_file.open("rb") as fin, zf.open(zinfo, mode="w") as fout:
                    while True:
                        _check_cancel(cancel_event)
                        n = fin.readinto(buf)
                        if not n:
                            break
                        fout.write(view[:n])
                        processed += n
                        if on_progress is not None and total_bytes > 0:
                            on_progress(min(99.0, (processed / total_bytes) * 100.0))

        _check_cancel(cancel_event)
        os.replace(temp_path, dest)
        out_bytes = dest.stat().st_size
        if on_progress is not None:
            on_progress(100.0)
        res = ForgeResult(True, (dest,), total_bytes, out_bytes, "ZIP OK", "#38B764")
        saved = int(round(res.saved_percent))
        toast = f"-{saved}% ZIP" if saved >= 2 else "ZIP OK"
        return ForgeResult(True, (dest,), total_bytes, out_bytes, toast, "#38B764")
    except InterruptedError:
        return ForgeResult(False, (), total_bytes, 0, "ABORTED", "#8A9AF2")
    except OSError:
        return ForgeResult(False, (), total_bytes, 0, "ZIP ERR", "#EF7D57")
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def compress_to_tar_xz(
    paths: Sequence[Path],
    output_path: Path | None = None,
    *,
    preset: int = 6,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ForgeResult:
    """Stream files/directories into a high-ratio LZMA2 .tar.xz archive."""
    valid = [Path(p) for p in paths if Path(p).exists()]
    if not valid:
        return ForgeResult(False, (), 0, 0, "NO FILE", "#EF7D57")

    first = valid[0]
    stem = first.stem if (len(valid) == 1 and first.is_file()) else (first.name if len(valid) == 1 else f"{first.stem}_bundle")
    dest = output_path or _unique_output_path(first.parent, stem, ".tar.xz", avoid_path=first)
    temp_path = _temp_sibling_path(dest)
    skip = {dest.resolve(), temp_path.resolve()} if dest.parent.exists() else set()

    files, empty_dirs, total_bytes = _scan_files_fast(valid, skip)
    processed = 0

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with lzma.open(temp_path, mode="wb", preset=preset) as xz_stream:
            with tarfile.open(mode="w", fileobj=xz_stream) as tf:
                for d_arc in empty_dirs:
                    _check_cancel(cancel_event)
                    tinfo = tarfile.TarInfo(name=d_arc.rstrip("/"))
                    tinfo.type = tarfile.DIRTYPE
                    tinfo.mode = 0o755
                    tf.addfile(tinfo)

                for src_file, arcname, _sz in files:
                    _check_cancel(cancel_event)
                    tinfo = tf.gettarinfo(str(src_file), arcname=arcname)
                    with src_file.open("rb") as fin:
                        tf.addfile(tinfo, fileobj=fin)
                    processed += tinfo.size
                    if on_progress is not None and total_bytes > 0:
                        on_progress(min(99.0, (processed / total_bytes) * 100.0))

        _check_cancel(cancel_event)
        os.replace(temp_path, dest)
        out_bytes = dest.stat().st_size
        if on_progress is not None:
            on_progress(100.0)
        res = ForgeResult(True, (dest,), total_bytes, out_bytes, "TXZ OK", "#38B764")
        saved = int(round(res.saved_percent))
        toast = f"-{saved}% TXZ" if saved >= 2 else "TXZ OK"
        return ForgeResult(True, (dest,), total_bytes, out_bytes, toast, "#38B764")
    except InterruptedError:
        return ForgeResult(False, (), total_bytes, 0, "ABORTED", "#8A9AF2")
    except (OSError, lzma.LZMAError, tarfile.TarError):
        return ForgeResult(False, (), total_bytes, 0, "TXZ ERR", "#EF7D57")
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def compress_to_tar_gz(
    paths: Sequence[Path],
    output_path: Path | None = None,
    *,
    compresslevel: int = 6,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ForgeResult:
    """Stream files/directories into a .tar.gz archive."""
    valid = [Path(p) for p in paths if Path(p).exists()]
    if not valid:
        return ForgeResult(False, (), 0, 0, "NO FILE", "#EF7D57")

    first = valid[0]
    stem = (
        first.stem
        if (len(valid) == 1 and first.is_file())
        else (first.name if len(valid) == 1 else f"{first.stem}_bundle")
    )
    dest = output_path or _unique_output_path(first.parent, stem, ".tar.gz", avoid_path=first)
    temp_path = _temp_sibling_path(dest)
    skip = {dest.resolve(), temp_path.resolve()} if dest.parent.exists() else set()

    files, empty_dirs, total_bytes = _scan_files_fast(valid, skip)
    processed = 0

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(temp_path, mode="wb", compresslevel=compresslevel) as gz_stream:
            with tarfile.open(mode="w", fileobj=gz_stream) as tf:
                for d_arc in empty_dirs:
                    _check_cancel(cancel_event)
                    tinfo = tarfile.TarInfo(name=d_arc.rstrip("/"))
                    tinfo.type = tarfile.DIRTYPE
                    tinfo.mode = 0o755
                    tf.addfile(tinfo)

                for src_file, arcname, _sz in files:
                    _check_cancel(cancel_event)
                    tinfo = tf.gettarinfo(str(src_file), arcname=arcname)
                    with src_file.open("rb") as fin:
                        tf.addfile(tinfo, fileobj=fin)
                    processed += tinfo.size
                    if on_progress is not None and total_bytes > 0:
                        on_progress(min(99.0, (processed / total_bytes) * 100.0))

        _check_cancel(cancel_event)
        os.replace(temp_path, dest)
        out_bytes = dest.stat().st_size
        if on_progress is not None:
            on_progress(100.0)
        res = ForgeResult(True, (dest,), total_bytes, out_bytes, "TGZ OK", "#38B764")
        saved = int(round(res.saved_percent))
        toast = f"-{saved}% TGZ" if saved >= 2 else "TGZ OK"
        return ForgeResult(True, (dest,), total_bytes, out_bytes, toast, "#38B764")
    except InterruptedError:
        return ForgeResult(False, (), total_bytes, 0, "ABORTED", "#8A9AF2")
    except (OSError, tarfile.TarError):
        return ForgeResult(False, (), total_bytes, 0, "TGZ ERR", "#EF7D57")
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def compress_single_gzip(
    src_path: Path,
    *,
    compresslevel: int = 6,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ForgeResult:
    """Stream a single file into a raw .gz file using constant 1 MiB chunks."""
    src = Path(src_path)
    if not src.is_file():
        return compress_to_tar_gz([src], compresslevel=compresslevel, on_progress=on_progress, cancel_event=cancel_event)

    src_bytes = src.stat().st_size
    dest = _unique_output_path(src.parent, src.name, ".gz", avoid_path=src)
    temp_path = _temp_sibling_path(dest)
    buf = bytearray(STREAM_CHUNK_BYTES)
    view = memoryview(buf)
    processed = 0

    try:
        with src.open("rb") as fin, gzip.open(temp_path, mode="wb", compresslevel=compresslevel) as fout:
            while True:
                _check_cancel(cancel_event)
                n = fin.readinto(buf)
                if not n:
                    break
                fout.write(view[:n])
                processed += n
                if on_progress is not None and src_bytes > 0:
                    on_progress(min(99.0, (processed / src_bytes) * 100.0))
        _check_cancel(cancel_event)
        os.replace(temp_path, dest)
        out_bytes = dest.stat().st_size
        if on_progress is not None:
            on_progress(100.0)
        res = ForgeResult(True, (dest,), src_bytes, out_bytes, "GZ OK", "#38B764")
        saved = int(round(res.saved_percent))
        toast = f"-{saved}% GZ" if saved >= 2 else "GZ OK"
        return ForgeResult(True, (dest,), src_bytes, out_bytes, toast, "#38B764")
    except InterruptedError:
        return ForgeResult(False, (), src_bytes, 0, "ABORTED", "#8A9AF2")
    except OSError:
        return ForgeResult(False, (), src_bytes, 0, "GZ ERR", "#EF7D57")
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _resolve_safe_member_path(dest_root: Path, member_name: str) -> Path:
    """Validate archive member path against Zip-Slip / Tar-Slip directory traversal."""
    normalized = member_name.replace("\\", "/")
    if not normalized or normalized.startswith("/") or ":" in normalized.split("/")[0]:
        raise ValueError(f"Unsafe archive member path: {member_name}")
    parts = [p for p in normalized.split("/") if p and p != "."]
    if any(p == ".." for p in parts):
        raise ValueError(f"Path traversal in archive member: {member_name}")
    if not parts:
        return dest_root
    target = dest_root.joinpath(*parts).resolve()
    if not target.is_relative_to(dest_root.resolve()):
        raise ValueError(f"Archive member escapes target root: {member_name}")
    return target


def _carry_mark_of_web(archive: Path, outputs: tuple[Path, ...]) -> None:
    """Copy a downloaded archive's Mark of the Web (its Zone.Identifier stream) to everything extracted from it, as
    Explorer does, so SmartScreen and Office Protected View still see the files as from the internet."""
    try:
        mark = Path(f"{archive}:Zone.Identifier").read_bytes()
    except OSError:
        return  # made on this PC, or not NTFS
    for out in outputs:
        for path in (p for p in out.rglob("*") if p.is_file()) if out.is_dir() else (out,):
            try:
                Path(f"{path}:Zone.Identifier").write_bytes(mark)
            except OSError:
                pass  # FAT or exFAT has no streams


def extract_archive_safe(
    archive_path: Path,
    output_dir: Path | None = None,
    *,
    max_extract_bytes: int = DEFAULT_MAX_EXTRACT_BYTES,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ForgeResult:
    """Atomically extract an archive with Zip-Slip and decompression-bomb protection."""
    src = Path(archive_path)
    if not src.is_file():
        return ForgeResult(False, (), 0, 0, "NO ARCH", "#EF7D57")

    src_bytes = src.stat().st_size
    lower_name = src.name.lower()

    # Single-stream compressed file (.gz / .xz / .bz2 that is NOT a .tar.*)
    is_compound_tar = any(lower_name.endswith(sfx) for sfx in _COMPOUND_ARCHIVE_SUFFIXES) or lower_name.endswith(
        (".tgz", ".txz", ".tbz2", ".tar")
    )
    if not is_compound_tar and src.suffix.lower() in {".gz", ".xz", ".bz2"}:
        return _decompress_single_stream(
            src,
            max_extract_bytes=max_extract_bytes,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    dest_dir = output_dir or _unique_output_dir(src.parent, _strip_archive_stem(src))
    staging_parent = dest_dir.parent if dest_dir.name else dest_dir
    staging_name = f".forge_extract_{os.getpid()}_{threading.get_ident()}_{dest_dir.name or 'root'}"
    staging_dir = staging_parent / staging_name
    extracted_bytes = 0
    buf = bytearray(STREAM_CHUNK_BYTES)
    view = memoryview(buf)

    try:
        staging_dir.mkdir(parents=True, exist_ok=False)
        staging_root = staging_dir.resolve()

        if lower_name.endswith(".zip"):
            with zipfile.ZipFile(src, mode="r") as zf:
                infos = zf.infolist()
                total_uncomp = sum(max(0, info.file_size) for info in infos)
                if total_uncomp > max_extract_bytes:
                    raise ValueError("Archive exceeds safe extraction limit")

                for info in infos:
                    _check_cancel(cancel_event)
                    target = _resolve_safe_member_path(staging_root, info.filename)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    # Reject Unix symlinks encoded in external_attr (S_IFLNK = 0o120000)
                    unix_mode = info.external_attr >> 16
                    if (unix_mode & 0o170000) == 0o120000:
                        raise ValueError("Symlinks in archives are blocked")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info, mode="r") as fin, target.open("wb") as fout:
                        while True:
                            _check_cancel(cancel_event)
                            n = fin.readinto(buf)
                            if not n:
                                break
                            extracted_bytes += n
                            if extracted_bytes > max_extract_bytes:
                                raise ValueError("Archive bomb limit exceeded")
                            fout.write(view[:n])
                            if on_progress is not None and total_uncomp > 0:
                                on_progress(min(99.0, (extracted_bytes / total_uncomp) * 100.0))
        else:
            tar_total = max(src_bytes, 1)
            with tarfile.open(src, mode="r:*") as tf:
                for member in tf:
                    _check_cancel(cancel_event)
                    if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                        raise ValueError("Unsafe special/link tar member blocked")
                    target = _resolve_safe_member_path(staging_root, member.name)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                        tf.members.clear()
                        continue
                    if not member.isfile():
                        tf.members.clear()
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    fin_obj = tf.extractfile(member)
                    if fin_obj is None:
                        tf.members.clear()
                        continue
                    with fin_obj, target.open("wb") as fout:
                        while True:
                            _check_cancel(cancel_event)
                            chunk = fin_obj.read(STREAM_CHUNK_BYTES)
                            if not chunk:
                                break
                            extracted_bytes += len(chunk)
                            if extracted_bytes > max_extract_bytes:
                                raise ValueError("Archive bomb limit exceeded")
                            fout.write(chunk)
                            if on_progress is not None:
                                on_progress(min(99.0, (extracted_bytes / tar_total) * 100.0))
                    tf.members.clear()

        _check_cancel(cancel_event)
        for attempt in range(6):
            try:
                os.replace(staging_dir, dest_dir)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05)
        _carry_mark_of_web(src, (dest_dir,))
        if on_progress is not None:
            on_progress(100.0)
        return ForgeResult(True, (dest_dir,), src_bytes, extracted_bytes, "UNZIP OK", "#73EFF7")
    except InterruptedError:
        return ForgeResult(False, (), src_bytes, 0, "ABORTED", "#8A9AF2")
    except ValueError:
        return ForgeResult(False, (), src_bytes, 0, "UNSAFE!", "#EF7D57")
    except (OSError, zipfile.BadZipFile, tarfile.TarError, lzma.LZMAError, EOFError):
        return ForgeResult(False, (), src_bytes, 0, "BAD ARCH", "#EF7D57")
    finally:
        if staging_dir.exists():
            shutil.rmtree(staging_dir, ignore_errors=True)


def _decompress_single_stream(
    src: Path,
    *,
    max_extract_bytes: int,
    on_progress: Callable[[float], None] | None,
    cancel_event: threading.Event | None,
) -> ForgeResult:
    src_bytes = src.stat().st_size
    raw_stem = src.stem or "decompressed"
    inner_p = Path(raw_stem)
    dest = _unique_output_path(src.parent, inner_p.stem or raw_stem, inner_p.suffix, avoid_path=src)
    temp_path = _temp_sibling_path(dest)
    ext = src.suffix.lower()

    opener: Callable[..., Any]
    if ext == ".gz":
        opener = gzip.open
    elif ext == ".xz":
        opener = lzma.open
    else:
        opener = bz2.open

    written = 0
    try:
        with opener(src, "rb") as fin, temp_path.open("wb") as fout:
            while True:
                _check_cancel(cancel_event)
                chunk = fin.read(STREAM_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_extract_bytes:
                    raise ValueError("Decompression limit exceeded")
                fout.write(chunk)
        os.replace(temp_path, dest)
        _carry_mark_of_web(src, (dest,))
        if on_progress is not None:
            on_progress(100.0)
        return ForgeResult(True, (dest,), src_bytes, written, "UNPACKED", "#73EFF7")
    except ValueError:
        return ForgeResult(False, (), src_bytes, 0, "UNSAFE!", "#EF7D57")
    except (OSError, EOFError, lzma.LZMAError):
        return ForgeResult(False, (), src_bytes, 0, "BAD ARCH", "#EF7D57")
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _write_ico_from_qimage(base_img: Any, dest_tmp: Path) -> None:
    """Encode multi-resolution Windows .ico (16..256px) using crisp pixel or smooth scaling."""
    from PyQt6.QtCore import QBuffer, QIODevice, Qt

    is_pixel_art = base_img.width() <= 64 and base_img.height() <= 64
    transform = (
        Qt.TransformationMode.FastTransformation
        if is_pixel_art
        else Qt.TransformationMode.SmoothTransformation
    )
    sizes = (16, 32, 48, 64, 128, 256)
    png_blobs: list[tuple[int, bytes]] = []
    for sz in sizes:
        scaled = base_img.scaled(sz, sz, Qt.AspectRatioMode.KeepAspectRatio, transform)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        scaled.save(buf, "PNG")
        png_blobs.append((sz, bytes(buf.data())))

    header = struct.pack("<HHH", 0, 1, len(png_blobs))
    offset = 6 + (16 * len(png_blobs))
    entries = bytearray()
    payload = bytearray()
    for sz, blob in png_blobs:
        dim_byte = 0 if sz >= 256 else sz
        entries.extend(struct.pack("<BBBBHHII", dim_byte, dim_byte, 0, 0, 1, 32, len(blob), offset))
        payload.extend(blob)
        offset += len(blob)

    dest_tmp.write_bytes(header + bytes(entries) + bytes(payload))


def convert_image_file(
    src_path: Path,
    target_format: str = "webp",
    *,
    quality: int | None = None,
    output_path: Path | None = None,
) -> ForgeResult:
    """Convert or optimize an image using Qt's native C++ QImageReader/QImageWriter."""
    from PyQt6.QtCore import QFile, QIODevice
    from PyQt6.QtGui import QColor, QGuiApplication, QImage, QImageReader, QImageWriter, QPainter

    src = Path(src_path)
    if not src.is_file():
        return ForgeResult(False, (), 0, 0, "NO IMG", "#EF7D57")

    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        QGuiApplication([])

    fmt = target_format.lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt not in {"webp", "png", "jpg", "bmp", "ico"}:
        return ForgeResult(False, (), 0, 0, "BAD FMT", "#EF7D57")

    src_bytes = src.stat().st_size
    QImageReader.setAllocationLimit(256)
    qsrc = QFile(str(src))
    if not qsrc.open(QIODevice.OpenModeFlag.ReadOnly):
        return ForgeResult(False, (), src_bytes, 0, "IMG ERR", "#EF7D57")
    try:
        reader = QImageReader(qsrc)
        reader.setAutoTransform(True)
        image = reader.read()
        del reader
    finally:
        qsrc.close()

    if image.isNull():
        return ForgeResult(False, (), src_bytes, 0, "IMG ERR", "#EF7D57")
    from src.tools.web_drop import strip_metadata

    image = strip_metadata(image)  # outputs never carry the source's Author/Comment tags

    dest = output_path or _unique_output_path(src.parent, src.stem, f".{fmt}", avoid_path=src)
    temp_path = _temp_sibling_path(dest)

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "ico":
            _write_ico_from_qimage(image, temp_path)
        else:
            out_img = image
            if fmt == "jpg" and image.hasAlphaChannel():
                flattened = QImage(image.size(), QImage.Format.Format_RGB32)
                flattened.fill(QColor("#FFFFFF"))
                painter = QPainter(flattened)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
                painter.drawImage(0, 0, image)
                painter.end()
                out_img = flattened

            qt_fmt = "jpeg" if fmt == "jpg" else fmt
            qout = QFile(str(temp_path))
            if not qout.open(QIODevice.OpenModeFlag.WriteOnly):
                return ForgeResult(False, (), src_bytes, 0, "IMG ERR", "#EF7D57")
            try:
                writer = QImageWriter(qout, qt_fmt.encode("ascii"))
                if fmt == "webp":
                    writer.setQuality(85 if quality is None else quality)
                elif fmt == "jpg":
                    writer.setQuality(88 if quality is None else quality)
                    writer.setOptimizedWrite(True)
                    writer.setProgressiveScanWrite(True)
                elif fmt == "png":
                    # In Qt PNG handler, lower quality (10) maps to highest zlib compression (9)
                    writer.setQuality(10 if quality is None else quality)
                wrote_ok = writer.write(out_img)
                del writer
            finally:
                qout.close()

            if not wrote_ok:
                return ForgeResult(False, (), src_bytes, 0, "IMG ERR", "#EF7D57")

        os.replace(temp_path, dest)
        out_bytes = dest.stat().st_size
        res = ForgeResult(True, (dest,), src_bytes, out_bytes, f"-> {fmt.upper()}", "#38B764")
        saved = int(round(res.saved_percent))
        toast = f"-{saved}% {fmt.upper()}" if saved >= 5 else f"-> {fmt.upper()}"
        return ForgeResult(True, (dest,), src_bytes, out_bytes, toast, "#38B764")
    except OSError:
        return ForgeResult(False, (), src_bytes, 0, "IMG ERR", "#EF7D57")
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _json_default_serializer(obj: Any) -> Any:
    if isinstance(obj, (dt.datetime, dt.date, dt.time)):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


# Info entries that say who or what made a PDF and when (headless Chrome/Edge writes HeadlessChrome and Skia here)
_PDF_INFO_KEYS = (b"Author", b"Creator", b"Producer", b"CreationDate", b"ModDate", b"Subject", b"Keywords")
# A Title that is only a file name, path or URL (Chrome's fallback when the page has none) goes; a real one stays
_PDF_TITLE_FALLBACK = re.compile(rb"^(file|https?):|[\\/]|\.(html?|xhtml|svg|txt|pdf)$", re.I)
# JPEG segments kept: APP0 (JFIF), APP2 (ICC colour profile), APP14 (Adobe colour transform); other APPn and COM go
_JPEG_DROP = {0xE1, *range(0xE3, 0xEE), 0xEF, 0xFE}
_PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}
# What a cleaned PDF says made it: plainly a joke, never a real program's name
PDF_PRODUCER = b"Paperduck"
PDF_CREATORS = (b"a very determined duck", b"quack quack, machine translated", b"one duck, two wings, zero comments",
                b"a duck who folded it by hand", b"bread, mostly", b"the pond's finest paperwork")


def _pdf_string_end(data: bytes, start: int) -> int:
    """Index of the byte closing the PDF string that opens at `start` ('(' literal or '<' hex)."""
    if data[start] == 0x3C:
        return data.index(b">", start)
    depth, i = 0, start
    while True:
        ch = data[i]
        if ch == 0x5C:  # backslash escapes the next byte
            i += 2
            continue
        depth += ch == 0x28
        depth -= ch == 0x29
        if depth == 0:
            return i
        i += 1


def _scrub_pdf(data: bytes) -> bytes | None:
    """Delete the Info entries (key and value) and write the duck's own Producer and Creator into the space they
    freed. Removed bytes become spaces of the same length (whitespace is valid anywhere between PDF tokens), so
    every xref offset stays valid and the file needs no rebuild. A file already carrying the duck's entries is
    left as it is."""
    if b"/ObjStm" in data or b"xmpmeta" in data:
        return None  # Info or XMP inside compressed streams: not something to remove in place
    out = bytearray(data)
    for ref in re.finditer(rb"/Info\s+(\d+)\s+(\d+)\s+R", data):
        obj = re.search(rb"(?<!\d)" + ref.group(1) + rb"\s+" + ref.group(2) + rb"\s+obj\b", data)
        if obj is None:
            return None
        end = data.find(b"endobj", obj.end())
        if end == -1:
            return None
        removed = False
        for key in re.compile(rb"/([A-Za-z]+)\s*([(<])").finditer(data, obj.end(), end):
            name, close = key.group(1), _pdf_string_end(data, key.start(2))
            value = data[key.start(2) + 1:close]
            ours = (name == b"Producer" and value == PDF_PRODUCER) or (name == b"Creator" and value in PDF_CREATORS)
            if not ours and (name in _PDF_INFO_KEYS or (
                    name == b"Title" and _PDF_TITLE_FALLBACK.search(re.sub(rb"\\([()\\])", rb"\1", value).strip()))):
                out[key.start():close + 1] = b" " * (close + 1 - key.start())
                removed = True
        if removed:
            joke = b"/Producer (" + PDF_PRODUCER + b") /Creator (" + random.choice(PDF_CREATORS) + b")"
            gap = re.compile(rb" {%d,}" % (len(joke) + 2)).search(out, obj.end(), end)
            if gap is not None:  # always room after Chrome's long user agent; otherwise the entries just go
                out[gap.start() + 1:gap.start() + 1 + len(joke)] = joke
    return bytes(out)


def _scrub_png(data: bytes) -> bytes | None:
    out, i = bytearray(data[:8]), 8
    while i < len(data):
        size = struct.unpack(">I", data[i:i + 4])[0]
        chunk = data[i:i + 12 + size]
        if len(chunk) < 12 + size:
            return None
        if chunk[4:8] not in _PNG_DROP:
            out += chunk
        i += 12 + size
    return bytes(out)


def _scrub_jpeg(data: bytes) -> bytes | None:
    out, i = bytearray(data[:2]), 2
    while i + 4 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xDA:  # start of scan: the rest is image data
            return bytes(out + data[i:])
        size = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker not in _JPEG_DROP:
            out += data[i:i + 2 + size]
        i += 2 + size
    return None


def scrub_metadata(path: Path, dry_run: bool = False) -> str:
    """Remove who-made-it metadata from a PDF, PNG or JPEG in place, without re-encoding: PDF Info entries
    (Author, Creator, Producer, dates, a file-name Title; a duck joke takes their place), PNG text/EXIF/time
    chunks, JPEG EXIF/XMP/IPTC and comments. Returns "cleaned" ("would clean" with `dry_run`, which writes
    nothing), "nothing to remove" or "error: ..."."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        return f"error: {exc.strerror or exc}"
    try:
        if data.startswith(b"%PDF-"):
            clean = _scrub_pdf(data)
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            clean = _scrub_png(data)
        elif data.startswith(b"\xff\xd8"):
            clean = _scrub_jpeg(data)
        else:
            return "error: only PDF, PNG and JPEG files are cleaned"
    except (ValueError, IndexError, struct.error):
        clean = None
    if clean is None:
        return "error: this file's layout is not one Paperduck can clean safely; it was left as it was"
    if clean == data:
        return "nothing to remove"
    if dry_run:
        return "would clean"
    temp = _temp_sibling_path(path)
    try:
        temp.write_bytes(clean)
        os.replace(temp, path)
    except OSError as exc:
        temp.unlink(missing_ok=True)
        return f"error: {exc.strerror or exc}"
    return "cleaned"


def convert_data_file(
    src_path: Path,
    *,
    allow_minify_fallback: bool = True,
    output_path: Path | None = None,
) -> ForgeResult:
    """Convert structured data between JSON <-> CSV and TOML -> JSON."""
    src = Path(src_path)
    if not src.is_file():
        return ForgeResult(False, (), 0, 0, "NO DATA", "#EF7D57")

    src_bytes = src.stat().st_size
    ext = src.suffix.lower()
    temp_path: Path | None = None

    try:
        if ext == ".csv":
            dest = output_path or _unique_output_path(src.parent, src.stem, ".json", avoid_path=src)
            temp_path = _temp_sibling_path(dest)
            with src.open("r", encoding="utf-8-sig", newline="") as fin:
                rows = list(csv.DictReader(fin))
            with temp_path.open("w", encoding="utf-8", newline="\n") as fout:
                json.dump(rows, fout, indent=2, ensure_ascii=False)
            os.replace(temp_path, dest)
            temp_path = None
            return ForgeResult(True, (dest,), src_bytes, dest.stat().st_size, "-> JSON", "#73EFF7")

        if ext == ".toml":
            dest = output_path or _unique_output_path(src.parent, src.stem, ".json", avoid_path=src)
            temp_path = _temp_sibling_path(dest)
            with src.open("rb") as fin:
                parsed = tomllib.load(fin)
            with temp_path.open("w", encoding="utf-8", newline="\n") as fout:
                json.dump(parsed, fout, indent=2, ensure_ascii=False, default=_json_default_serializer)
            os.replace(temp_path, dest)
            temp_path = None
            return ForgeResult(True, (dest,), src_bytes, dest.stat().st_size, "-> JSON", "#73EFF7")

        if ext == ".json":
            raw_text = src.read_text(encoding="utf-8-sig")
            payload = json.loads(raw_text)

            records: list[dict[str, Any]] | None = None
            if isinstance(payload, list) and payload and all(isinstance(r, dict) for r in payload):
                records = payload
            elif isinstance(payload, dict):
                list_vals = [
                    v for v in payload.values()
                    if isinstance(v, list) and v and all(isinstance(r, dict) for r in v)
                ]
                if len(list_vals) == 1:
                    records = list_vals[0]
                elif payload and all(not isinstance(v, (dict, list)) for v in payload.values()):
                    records = [{"key": str(k), "value": v} for k, v in payload.items()]

            if records is not None:
                dest = output_path or _unique_output_path(src.parent, src.stem, ".csv", avoid_path=src)
                temp_path = _temp_sibling_path(dest)
                fieldnames: list[str] = []
                seen: set[str] = set()
                for row in records:
                    for k in row:
                        ks = str(k)
                        if ks not in seen:
                            seen.add(ks)
                            fieldnames.append(ks)
                with temp_path.open("w", encoding="utf-8", newline="") as fout:
                    writer = csv.DictWriter(fout, fieldnames=fieldnames)
                    writer.writeheader()
                    for row in records:
                        clean_row = {
                            str(k): (
                                json.dumps(v, ensure_ascii=False)
                                if isinstance(v, (dict, list))
                                else ("" if v is None else v)
                            )
                            for k, v in row.items()
                        }
                        writer.writerow(clean_row)
                os.replace(temp_path, dest)
                temp_path = None
                return ForgeResult(True, (dest,), src_bytes, dest.stat().st_size, "-> CSV", "#73EFF7")

            if allow_minify_fallback:
                dest = output_path or _unique_output_path(src.parent, f"{src.stem}.min", ".json", avoid_path=src)
                temp_path = _temp_sibling_path(dest)
                minified = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
                temp_path.write_text(minified, encoding="utf-8")
                os.replace(temp_path, dest)
                temp_path = None
                out_sz = dest.stat().st_size
                res = ForgeResult(True, (dest,), src_bytes, out_sz, "MIN JSON", "#38B764")
                saved = int(round(res.saved_percent))
                toast = f"-{saved}% JSON" if saved >= 2 else "MIN JSON"
                return ForgeResult(True, (dest,), src_bytes, out_sz, toast, "#38B764")

        return ForgeResult(False, (), src_bytes, 0, "BAD DATA", "#EF7D57")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, csv.Error, tomllib.TOMLDecodeError, TypeError):
        return ForgeResult(False, (), src_bytes, 0, "DATA ERR", "#EF7D57")
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def _load_structured_payload(src: Path) -> Any:
    ext = src.suffix.lower()
    if ext == ".csv":
        with src.open("r", encoding="utf-8-sig", newline="") as fin:
            return list(csv.DictReader(fin))
    if ext == ".tsv":
        with src.open("r", encoding="utf-8-sig", newline="") as fin:
            return list(csv.DictReader(fin, delimiter="\t"))
    if ext == ".toml":
        with src.open("rb") as fin:
            return tomllib.load(fin)
    return json.loads(src.read_text(encoding="utf-8-sig"))


def _payload_to_records(payload: Any) -> list[dict[str, Any]] | None:
    if isinstance(payload, list) and payload and all(isinstance(r, dict) for r in payload):
        return payload
    if isinstance(payload, dict):
        list_vals = [
            v
            for v in payload.values()
            if isinstance(v, list) and v and all(isinstance(r, dict) for r in v)
        ]
        if len(list_vals) == 1:
            return list_vals[0]
        if payload:
            return [
                {
                    "key": str(k),
                    "value": (
                        json.dumps(v, ensure_ascii=False, default=_json_default_serializer)
                        if isinstance(v, (dict, list))
                        else v
                    ),
                }
                for k, v in payload.items()
            ]
    return None


def convert_data_explicit(src_path: Path, target_mode: ForgeMode) -> ForgeResult:
    """Convert structured data explicitly to JSON (pretty/minified), CSV, or TSV."""
    src = Path(src_path)
    if not src.is_file():
        return ForgeResult(False, (), 0, 0, "NO DATA", "#EF7D57")
    src_bytes = src.stat().st_size
    temp_path: Path | None = None

    try:
        payload = _load_structured_payload(src)

        if target_mode in {ForgeMode.JSON_PRETTY, ForgeMode.JSON_MIN}:
            is_min = target_mode == ForgeMode.JSON_MIN
            stem = f"{src.stem}.min" if is_min else (f"{src.stem}_fmt" if src.suffix.lower() == ".json" else src.stem)
            dest = _unique_output_path(src.parent, stem, ".json", avoid_path=src)
            temp_path = _temp_sibling_path(dest)
            with temp_path.open("w", encoding="utf-8", newline="\n") as fout:
                if is_min:
                    json.dump(
                        payload,
                        fout,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        default=_json_default_serializer,
                    )
                else:
                    json.dump(
                        payload,
                        fout,
                        indent=2,
                        ensure_ascii=False,
                        default=_json_default_serializer,
                    )
            os.replace(temp_path, dest)
            temp_path = None
            out_sz = dest.stat().st_size
            res = ForgeResult(True, (dest,), src_bytes, out_sz, "MIN JSON" if is_min else "-> JSON", "#38B764" if is_min else "#73EFF7")
            if is_min and res.saved_percent >= 2:
                return ForgeResult(True, (dest,), src_bytes, out_sz, f"-{int(round(res.saved_percent))}% JSON", "#38B764")
            return res

        if target_mode in {ForgeMode.CSV, ForgeMode.TSV}:
            records = _payload_to_records(payload)
            if not records:
                return ForgeResult(False, (), src_bytes, 0, "NO TABLE", "#EF7D57")
            is_tsv = target_mode == ForgeMode.TSV
            ext_out = ".tsv" if is_tsv else ".csv"
            delim = "\t" if is_tsv else ","
            dest = _unique_output_path(src.parent, src.stem, ext_out, avoid_path=src)
            temp_path = _temp_sibling_path(dest)

            fieldnames: list[str] = []
            seen: set[str] = set()
            for row in records:
                for k in row:
                    ks = str(k)
                    if ks not in seen:
                        seen.add(ks)
                        fieldnames.append(ks)
            with temp_path.open("w", encoding="utf-8", newline="") as fout:
                writer = csv.DictWriter(fout, fieldnames=fieldnames, delimiter=delim)
                writer.writeheader()
                for row in records:
                    clean_row = {
                        str(k): (
                            json.dumps(v, ensure_ascii=False, default=_json_default_serializer)
                            if isinstance(v, (dict, list))
                            else ("" if v is None else v)
                        )
                        for k, v in row.items()
                    }
                    writer.writerow(clean_row)
            os.replace(temp_path, dest)
            temp_path = None
            return ForgeResult(
                True,
                (dest,),
                src_bytes,
                dest.stat().st_size,
                "-> TSV" if is_tsv else "-> CSV",
                "#73EFF7",
            )

        return ForgeResult(False, (), src_bytes, 0, "BAD DATA", "#EF7D57")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, csv.Error, tomllib.TOMLDecodeError, TypeError):
        return ForgeResult(False, (), src_bytes, 0, "DATA ERR", "#EF7D57")
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def clean_text_file(src_path: Path) -> ForgeResult:
    """Normalize CRLF line endings to LF and strip trailing whitespace atomically."""
    src = Path(src_path)
    if not src.is_file():
        return ForgeResult(False, (), 0, 0, "NO FILE", "#EF7D57")
    src_bytes = src.stat().st_size
    suffix = src.suffix or ".txt"
    dest = _unique_output_path(src.parent, f"{src.stem}_clean", suffix, avoid_path=src)
    temp_path = _temp_sibling_path(dest)
    try:
        with src.open("r", encoding="utf-8-sig", newline="") as fin:
            raw = fin.read()
        while "\r\r" in raw:
            raw = raw.replace("\r\r", "\r")
        lines = [ln.rstrip(" \t") for ln in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        cleaned = "\n".join(lines).strip() + "\n"
        temp_path.write_text(cleaned, encoding="utf-8", newline="\n")
        os.replace(temp_path, dest)
        temp_path = None  # type: ignore[assignment]
        return ForgeResult(True, (dest,), src_bytes, dest.stat().st_size, "CLEAN OK", "#38B764")
    except (OSError, UnicodeDecodeError):
        return ForgeResult(False, (), src_bytes, 0, "TXT ERR", "#EF7D57")
    finally:
        if temp_path is not None and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def write_sha256_manifest(
    paths: Sequence[Path],
    *,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ForgeResult:
    """Compute C-accelerated SHA-256 digests and write a .sha256 manifest file."""
    valid = [Path(p) for p in paths if Path(p).exists()]
    if not valid:
        return ForgeResult(False, (), 0, 0, "NO FILE", "#EF7D57")

    first = valid[0]
    if len(valid) == 1 and first.is_file():
        dest = _unique_output_path(first.parent, first.name, ".sha256", avoid_path=first)
    else:
        base_name = first.name if len(valid) == 1 else f"{first.stem}_bundle"
        dest = _unique_output_path(first.parent, f"{base_name}_checksums", ".sha256")

    temp_path = _temp_sibling_path(dest)
    skip = {dest.resolve(), temp_path.resolve()} if dest.parent.exists() else set()
    files, _empty, total_bytes = _scan_files_fast(valid, skip)
    if not files:
        return ForgeResult(False, (), 0, 0, "EMPTY", "#EF7D57")

    processed = 0
    try:
        lines: list[str] = []
        for src_file, arcname, sz in files:
            _check_cancel(cancel_event)
            with src_file.open("rb") as fin:
                digest = hashlib.file_digest(fin, "sha256").hexdigest()
            lines.append(f"{digest} *{arcname}")
            processed += sz
            if on_progress is not None and total_bytes > 0:
                on_progress(min(99.0, (processed / total_bytes) * 100.0))

        temp_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        os.replace(temp_path, dest)
        if on_progress is not None:
            on_progress(100.0)
        return ForgeResult(True, (dest,), total_bytes, dest.stat().st_size, "SHA256 OK", "#FFCD75")
    except InterruptedError:
        return ForgeResult(False, (), total_bytes, 0, "ABORTED", "#8A9AF2")
    except OSError:
        return ForgeResult(False, (), total_bytes, 0, "HASH ERR", "#EF7D57")
    finally:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def get_wheel_options_for_paths(paths: Sequence[Path | str]) -> list[ForgeWheelOption]:
    """Return context-tailored radial conversion and compression options for hovered paths."""
    names = [str(p) for p in paths if str(p).strip()]
    if not names:
        return [
            ForgeWheelOption(ForgeMode.ZIP, "ZIP", "FAST", "#38B764", ".zip"),
            ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA", "#FFCD75", ".tar.xz"),
        ]

    # Single or batch images: offer all image conversions + archive compression
    # shortcut: suffix-only (a folder named x.png would get image slices); is_dir per file stalls big drops.
    # Raw-string endswith: a Path parse or splitext per file adds up over thousands of files.
    if all(n.lower().endswith(_IMAGE_SUFFIXES) for n in names):
        lead_ext = os.path.splitext(names[0])[1].lower() if len(names) == 1 else ""
        return [
            ForgeWheelOption(
                ForgeMode.WEBP,
                "WEBP",
                "RE-OPT" if lead_ext == ".webp" else "SMALLER",
                "#38B764",
                ".webp",
            ),
            ForgeWheelOption(
                ForgeMode.PNG,
                "PNG",
                "OPT PNG" if lead_ext == ".png" else "LOSSLESS",
                "#73EFF7",
                ".png",
            ),
            ForgeWheelOption(ForgeMode.JPG, "JPG", "PHOTO", "#41A6F6", ".jpg"),
            ForgeWheelOption(ForgeMode.ICO, "ICO", "WIN ICON", "#FF77A8", ".ico"),
            ForgeWheelOption(ForgeMode.BMP, "BMP", "BITMAP", "#A7F070", ".bmp"),
            ForgeWheelOption(ForgeMode.ZIP, "ZIP", "COMPRESS", "#FFCD75", ".zip"),
            ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA XZ", "#EF7D57", ".tar.xz"),
        ]

    if len(names) == 1 and not Path(names[0]).is_dir():
        single = Path(names[0])
        ext = single.suffix.lower()

        if _is_archive_path(single):
            return [
                ForgeWheelOption(ForgeMode.EXTRACT, "EXTRACT", "UNPACK", "#38B764", "FOLDER/"),
                ForgeWheelOption(ForgeMode.ZIP, "TO ZIP", "REPACK", "#73EFF7", ".zip"),
                ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA XZ", "#FFCD75", ".tar.xz"),
                ForgeWheelOption(ForgeMode.SHA256, "SHA-256", "CHECKSUM", "#FF77A8", ".sha256"),
            ]

        if ext == ".json":
            return [
                ForgeWheelOption(ForgeMode.CSV, "CSV", "TABLE", "#73EFF7", ".csv"),
                ForgeWheelOption(ForgeMode.TSV, "TSV", "TABS", "#41A6F6", ".tsv"),
                ForgeWheelOption(ForgeMode.JSON_MIN, "MIN.JSON", "MINIFY", "#38B764", ".min.json"),
                ForgeWheelOption(ForgeMode.JSON_PRETTY, "FMT.JSON", "PRETTY", "#A7F070", "_fmt.json"),
                ForgeWheelOption(ForgeMode.ZIP, "ZIP", "FAST ZIP", "#FFCD75", ".zip"),
                ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA XZ", "#FF77A8", ".tar.xz"),
            ]

        if ext == ".csv":
            return [
                ForgeWheelOption(ForgeMode.JSON_PRETTY, "JSON", "PRETTY", "#73EFF7", ".json"),
                ForgeWheelOption(ForgeMode.JSON_MIN, "MIN.JSON", "COMPACT", "#38B764", ".min.json"),
                ForgeWheelOption(ForgeMode.TSV, "TSV", "TO TABS", "#41A6F6", ".tsv"),
                ForgeWheelOption(ForgeMode.ZIP, "ZIP", "FAST ZIP", "#FFCD75", ".zip"),
                ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA XZ", "#FF77A8", ".tar.xz"),
                ForgeWheelOption(ForgeMode.GZIP, "GZIP", "RAW .GZ", "#A7F070", ".csv.gz"),
            ]

        if ext == ".tsv":
            return [
                ForgeWheelOption(ForgeMode.CSV, "CSV", "COMMAS", "#73EFF7", ".csv"),
                ForgeWheelOption(ForgeMode.JSON_PRETTY, "JSON", "PRETTY", "#38B764", ".json"),
                ForgeWheelOption(ForgeMode.JSON_MIN, "MIN.JSON", "COMPACT", "#41A6F6", ".min.json"),
                ForgeWheelOption(ForgeMode.ZIP, "ZIP", "FAST ZIP", "#FFCD75", ".zip"),
                ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA XZ", "#FF77A8", ".tar.xz"),
            ]

        if ext == ".toml":
            return [
                ForgeWheelOption(ForgeMode.JSON_PRETTY, "JSON", "PRETTY", "#73EFF7", ".json"),
                ForgeWheelOption(ForgeMode.JSON_MIN, "MIN.JSON", "COMPACT", "#38B764", ".min.json"),
                ForgeWheelOption(ForgeMode.CSV, "CSV", "TABLE", "#41A6F6", ".csv"),
                ForgeWheelOption(ForgeMode.ZIP, "ZIP", "FAST ZIP", "#FFCD75", ".zip"),
                ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA XZ", "#FF77A8", ".tar.xz"),
            ]

        if ext in _TEXT_EXTS:
            return [
                ForgeWheelOption(ForgeMode.ZIP, "ZIP", "FAST ZIP", "#38B764", ".zip"),
                ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA XZ", "#FFCD75", ".tar.xz"),
                ForgeWheelOption(ForgeMode.GZIP, "GZIP", "RAW .GZ", "#73EFF7", ".gz"),
                ForgeWheelOption(ForgeMode.TAR_GZ, "TAR.GZ", "ARCHIVE", "#41A6F6", ".tar.gz"),
                ForgeWheelOption(ForgeMode.CLEAN_TEXT, "CLEAN", "LF+TRIM", "#A7F070", "_clean"),
                ForgeWheelOption(ForgeMode.SHA256, "SHA-256", "CHECKSUM", "#FF77A8", ".sha256"),
            ]

    # Folders, multi-file bundles, or general binaries
    return [
        ForgeWheelOption(ForgeMode.ZIP, "ZIP", "FAST ZIP", "#38B764", ".zip"),
        ForgeWheelOption(ForgeMode.TAR_XZ, "TAR.XZ", "ULTRA XZ", "#FFCD75", ".tar.xz"),
        ForgeWheelOption(ForgeMode.TAR_GZ, "TAR.GZ", "TAR.GZ", "#73EFF7", ".tar.gz"),
        ForgeWheelOption(ForgeMode.SHA256, "SHA-256", "CHECKSUM", "#FF77A8", ".sha256"),
    ]


def process_paths_sync(
    paths: Sequence[Path | str],
    mode: ForgeMode = ForgeMode.AUTO,
    *,
    max_extract_bytes: int = DEFAULT_MAX_EXTRACT_BYTES,
    on_progress: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> ForgeResult:
    """Execute a synchronous conversion/compression job across one or more paths."""
    resolved_paths = [Path(p) for p in paths if str(p).strip()]
    existing = [p for p in resolved_paths if p.exists()]
    if not existing:
        return ForgeResult(False, (), 0, 0, "NO FILE", "#EF7D57")

    if mode == ForgeMode.ZIP:
        return compress_to_zip(existing, on_progress=on_progress, cancel_event=cancel_event)

    if mode == ForgeMode.TAR_XZ:
        return compress_to_tar_xz(existing, on_progress=on_progress, cancel_event=cancel_event)

    if mode == ForgeMode.TAR_GZ:
        return compress_to_tar_gz(existing, on_progress=on_progress, cancel_event=cancel_event)

    if mode == ForgeMode.GZIP:
        if len(existing) == 1 and existing[0].is_file():
            return compress_single_gzip(existing[0], on_progress=on_progress, cancel_event=cancel_event)
        return compress_to_tar_gz(existing, on_progress=on_progress, cancel_event=cancel_event)

    if mode == ForgeMode.SHA256:
        return write_sha256_manifest(existing, on_progress=on_progress, cancel_event=cancel_event)

    if mode == ForgeMode.CLEAN_TEXT:
        outputs_txt: list[Path] = []
        src_sum = 0
        out_sum = 0
        try:
            for idx, p in enumerate(existing):
                _check_cancel(cancel_event)
                r = clean_text_file(p)
                if not r.ok:
                    return r
                outputs_txt.extend(r.output_paths)
                src_sum += r.source_bytes
                out_sum += r.output_bytes
                if on_progress is not None:
                    on_progress(((idx + 1) / len(existing)) * 100.0)
        except InterruptedError:
            return ForgeResult(False, (), src_sum, 0, "ABORTED", "#8A9AF2")
        return ForgeResult(True, tuple(outputs_txt), src_sum, out_sum, "CLEAN OK", "#38B764")

    if mode in {ForgeMode.JSON_PRETTY, ForgeMode.JSON_MIN, ForgeMode.CSV, ForgeMode.TSV}:
        outputs_explicit: list[Path] = []
        src_sum = 0
        out_sum = 0
        last_toast = "-> DATA"
        last_col = "#73EFF7"
        for idx, p in enumerate(existing):
            _check_cancel(cancel_event)
            r = convert_data_explicit(p, mode)
            if not r.ok:
                return r
            outputs_explicit.extend(r.output_paths)
            src_sum += r.source_bytes
            out_sum += r.output_bytes
            last_toast = r.toast_text
            last_col = r.toast_color
            if on_progress is not None:
                on_progress(((idx + 1) / len(existing)) * 100.0)
        return ForgeResult(True, tuple(outputs_explicit), src_sum, out_sum, last_toast, last_col)

    if mode == ForgeMode.EXTRACT:
        outputs: list[Path] = []
        src_sum = 0
        out_sum = 0
        for idx, p in enumerate(existing):
            _check_cancel(cancel_event)
            r = extract_archive_safe(
                p,
                max_extract_bytes=max_extract_bytes,
                cancel_event=cancel_event,
            )
            if not r.ok:
                return r
            outputs.extend(r.output_paths)
            src_sum += r.source_bytes
            out_sum += r.output_bytes
            if on_progress is not None:
                on_progress(((idx + 1) / len(existing)) * 100.0)
        return ForgeResult(True, tuple(outputs), src_sum, out_sum, "UNZIP OK", "#73EFF7")

    if mode in {ForgeMode.WEBP, ForgeMode.PNG, ForgeMode.JPG, ForgeMode.ICO, ForgeMode.BMP}:
        outputs = []
        src_sum = 0
        out_sum = 0
        for idx, p in enumerate(existing):
            _check_cancel(cancel_event)
            r = convert_image_file(p, target_format=mode.value)
            if not r.ok:
                return r
            outputs.extend(r.output_paths)
            src_sum += r.source_bytes
            out_sum += r.output_bytes
            if on_progress is not None:
                on_progress(((idx + 1) / len(existing)) * 100.0)
        agg = ForgeResult(True, tuple(outputs), src_sum, out_sum, f"-> {mode.value.upper()}", "#38B764")
        saved = int(round(agg.saved_percent))
        toast = (
            f"{len(outputs)}x {mode.value.upper()}"
            if len(outputs) > 1
            else (f"-{saved}% {mode.value.upper()}" if saved >= 5 else f"-> {mode.value.upper()}")
        )
        return ForgeResult(True, tuple(outputs), src_sum, out_sum, toast, "#38B764")

    if mode == ForgeMode.DATA:
        outputs = []
        src_sum = 0
        out_sum = 0
        for idx, p in enumerate(existing):
            _check_cancel(cancel_event)
            r = convert_data_file(p, allow_minify_fallback=True)
            if not r.ok:
                return r
            outputs.extend(r.output_paths)
            src_sum += r.source_bytes
            out_sum += r.output_bytes
            if on_progress is not None:
                on_progress(((idx + 1) / len(existing)) * 100.0)
        label = outputs[-1].suffix.lstrip(".").upper() if outputs else "DATA"
        return ForgeResult(True, tuple(outputs), src_sum, out_sum, f"-> {label}", "#73EFF7")

    # AUTO smart routing
    if len(existing) > 1:
        if all(p.is_file() and p.suffix.lower() in _IMAGE_EXTS for p in existing):
            return process_paths_sync(
                existing,
                ForgeMode.WEBP,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
        return compress_to_zip(existing, on_progress=on_progress, cancel_event=cancel_event)

    single = existing[0]
    if single.is_dir():
        return compress_to_zip([single], on_progress=on_progress, cancel_event=cancel_event)

    if _is_archive_path(single):
        return extract_archive_safe(
            single,
            max_extract_bytes=max_extract_bytes,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )

    ext = single.suffix.lower()
    if ext in _IMAGE_EXTS:
        # Convert raster/vector images to optimized WebP (or PNG if source is already .ico)
        target_fmt = "png" if ext == ".ico" else "webp"
        q = 80 if ext == ".webp" else 85
        res = convert_image_file(single, target_format=target_fmt, quality=q)
        if on_progress is not None and res.ok:
            on_progress(100.0)
        return res

    if ext in _DATA_EXTS:
        res = convert_data_file(single, allow_minify_fallback=True)
        if on_progress is not None and res.ok:
            on_progress(100.0)
        return res

    return compress_to_zip([single], on_progress=on_progress, cancel_event=cancel_event)


class FileForgePlugin:
    """Async streaming File Converter and Compressor plugin for Paperduck."""

    SUBMENU_NAME: str = "File Forge (Drop / Convert)"

    def __init__(self, *, max_extract_bytes: int = DEFAULT_MAX_EXTRACT_BYTES) -> None:
        self._mode: ForgeMode = ForgeMode.AUTO
        self._max_extract_bytes = max(1024, int(max_extract_bytes))
        self._lock = threading.Lock()
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._cancel_event = threading.Event()

        self._busy: bool = False
        self._progress: float = 0.0
        self._last_saved_pct: float = 0.0
        self._completed_jobs: int = 0
        self._status_badge: str = "DROP"
        self._status_color: str = "#38B764"
        self._dirty: bool = False
        self._worker_notification: tuple[str, str] | None = None
        self._finished_outputs: tuple[Path, ...] | None = None
        self._ready_notification: tuple[str, str] | None = None

    @property
    def tool_id(self) -> str:
        return "forge"

    @property
    def display_name(self) -> str:
        return "File Forge"

    @property
    def tick_interval_ms(self) -> int:
        with self._lock:
            if self._busy or self._dirty or self._worker_notification is not None:
                return 150
            return 2_000

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._busy

    @property
    def has_pending_update(self) -> bool:
        with self._lock:
            return (
                self._busy
                or self._dirty
                or self._worker_notification is not None
                or self._ready_notification is not None
            )

    @property
    def mode(self) -> ForgeMode:
        return self._mode

    @mode.setter
    def mode(self, value: ForgeMode) -> None:
        if isinstance(value, ForgeMode):
            self._mode = value

    @property
    def completed_jobs(self) -> int:
        with self._lock:
            return self._completed_jobs

    @property
    def last_saved_percent(self) -> float:
        with self._lock:
            return self._last_saved_pct

    def submit_paths(
        self,
        paths: Sequence[Path | str],
        *,
        mode_override: ForgeMode | None = None,
    ) -> bool:
        """Enqueue files/directories onto the background worker thread."""
        # Called on the GUI thread: Path conversion and blank filtering happen in process_paths_sync
        # on the worker, since parsing thousands of paths here stalls the drop
        clean = list(paths)
        if not any(str(p).strip() for p in clean):
            return False

        with self._lock:
            if self._busy:
                return False
            self._busy = True
            self._progress = 2.0
            self._status_badge = "WORK"
            self._status_color = "#FFCD75"
            self._dirty = True
            self._cancel_event.clear()
            mode_snapshot = mode_override if isinstance(mode_override, ForgeMode) else self._mode

            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=1,
                    thread_name_prefix="paperduck-forge",
                )
            self._executor.submit(self._run_worker, clean, mode_snapshot)
        return True

    def _run_worker(self, paths: list[Path | str], mode: ForgeMode) -> None:
        def _update_progress(pct: float) -> None:
            with self._lock:
                clamped = max(0.0, min(100.0, float(pct)))
                if abs(clamped - self._progress) >= 1.0:
                    self._progress = clamped
                    self._dirty = True

        try:
            result = process_paths_sync(
                paths,
                mode,
                max_extract_bytes=self._max_extract_bytes,
                on_progress=_update_progress,
                cancel_event=self._cancel_event,
            )
        except Exception:
            result = ForgeResult(False, (), 0, 0, "FORGE ERR", "#EF7D57")

        with self._lock:
            self._busy = False
            if result.ok:
                self._progress = 100.0
                self._completed_jobs += 1
                self._last_saved_pct = result.saved_percent
                self._status_badge = "DONE"
                self._status_color = "#38B764"
            else:
                self._progress = 0.0
                self._status_badge = "ERR"
                self._status_color = "#EF7D57"
            self._worker_notification = (result.toast_text, result.toast_color)
            self._finished_outputs = result.output_paths if result.ok else ()
            self._dirty = True

    def pop_finished_outputs(self) -> tuple[Path, ...] | None:
        """Output paths of the last finished job (empty on failure), or None if none finished."""
        with self._lock:
            outputs = self._finished_outputs
            self._finished_outputs = None
            return outputs

    def pop_notification(self) -> tuple[str, str] | None:
        with self._lock:
            note = self._ready_notification
            self._ready_notification = None
            return note

    def tick(self) -> bool:
        with self._lock:
            if self._worker_notification is not None:
                self._ready_notification = self._worker_notification
                self._worker_notification = None
                self._dirty = False
                return True
            if not self._dirty:
                return False
            self._dirty = False
            return True

    def desired_state(self) -> CompanionState | None:
        with self._lock:
            return CompanionState.FOCUS if self._busy else None

    def get_hud_data(self) -> HudData:
        with self._lock:
            progress = self._progress
            saved_pct = self._last_saved_pct
            jobs = self._completed_jobs
            status_badge = self._status_badge
            status_color = self._status_color
            busy = self._busy

        mode_tag = self._mode.value.upper()[:4]
        badge_right = "WORK" if busy else (status_badge if status_badge != "DROP" else mode_tag)
        jobs_bar_val = min(100.0, float(jobs) * 10.0)
        if busy:
            detail = f"Working... {int(progress)}%"
        elif status_badge == "ERR":
            detail = "Last job failed"
        elif status_badge == "DONE":
            detail = f"Done, saved {saved_pct:.0f}% of size"
        else:
            detail = "Drop files on me to convert"

        return HudData(
            badge_left="FILE.FRG",
            badge_right=badge_right,
            badge_right_color=status_color,
            bars=(
                HudBar(
                    label="Job",
                    value=progress,
                    color_hex="#FFCD75" if busy else "#38B764",
                ),
                HudBar(
                    label="Saved",
                    value=saved_pct,
                    color_hex="#73EFF7",
                    display_text=f"{-int(round(saved_pct)):+d}%" if round(saved_pct) else "0%",
                ),
                HudBar(
                    label="Total",
                    value=jobs_bar_val,
                    color_hex="#FF77A8",
                    display_text=f"{min(999, jobs):3d}",
                ),
            ),
            detail=detail,
        )

    def get_menu_items(self) -> list[ToolMenuItem]:
        is_busy = self.busy
        sub = self.SUBMENU_NAME
        mode_defs: tuple[tuple[ForgeMode, str], ...] = (
            (ForgeMode.AUTO, "Mode: Smart Auto (Drop Router)"),
            (ForgeMode.ZIP, "Mode: Compress -> .zip (Fast)"),
            (ForgeMode.TAR_XZ, "Mode: Compress -> .tar.xz (Ultra)"),
            (ForgeMode.EXTRACT, "Mode: Extract Archive"),
            (ForgeMode.WEBP, "Mode: Image -> WebP (Optimize)"),
            (ForgeMode.PNG, "Mode: Image -> PNG"),
            (ForgeMode.JPG, "Mode: Image -> JPG"),
            (ForgeMode.ICO, "Mode: Image -> Windows .ICO"),
            (ForgeMode.DATA, "Mode: Data JSON <-> CSV / TOML"),
        )
        items = [
            ToolMenuItem(
                label="Select File(s) to Convert / Compress...",
                callback_id="forge:pick_files",
                enabled=not is_busy,
                submenu=sub,
            ),
            ToolMenuItem(
                label="Compress Folder...",
                callback_id="forge:pick_folder",
                enabled=not is_busy,
                submenu=sub,
            ),
            ToolMenuItem(
                label="Process Copied File(s) from Clipboard",
                callback_id="forge:clipboard_files",
                enabled=not is_busy,
                submenu=sub,
            ),
        ]
        for m_enum, label in mode_defs:
            items.append(
                ToolMenuItem(
                    label=label,
                    callback_id=f"forge:mode:{m_enum.value}",
                    checkable=True,
                    checked=(self._mode == m_enum),
                    submenu=sub,
                )
            )
        return items

    def execute_action(self, callback_id: str) -> tuple[bool, str, str]:
        if callback_id.startswith("forge:mode:"):
            raw_mode = callback_id.split("forge:mode:", 1)[1]
            try:
                self._mode = ForgeMode(raw_mode)
            except ValueError:
                return (False, "", "")
            with self._lock:
                if not self._busy:
                    self._status_badge = self._mode.value.upper()[:4]
                    self._status_color = "#73EFF7"
            return (True, f"FRG {self._mode.value.upper()[:4]}", "#73EFF7")
        return (False, "", "")

    def close(self) -> None:
        """Signal active chunk loops to abort and release worker thread pool."""
        self._cancel_event.set()
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "mode": self._mode.value,
                "completed_jobs": self._completed_jobs,
                "last_saved_pct": round(self._last_saved_pct, 1),
            }

    def load_dict(self, data: dict[str, Any]) -> None:
        if not isinstance(data, dict):
            return
        raw_mode = data.get("mode", ForgeMode.AUTO.value)
        try:
            self._mode = ForgeMode(raw_mode)
        except ValueError:
            self._mode = ForgeMode.AUTO
        try:
            with self._lock:
                self._completed_jobs = max(0, int(data.get("completed_jobs", 0)))
                saved = float(data.get("last_saved_pct", 0.0))
                self._last_saved_pct = max(0.0, min(100.0, saved))
        except (TypeError, ValueError):
            pass
