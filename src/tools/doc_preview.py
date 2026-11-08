"""preview_document: every page of a Word, PowerPoint, Excel or PDF file as one contact-sheet image, so a model
checks a layout in one call instead of paging through the open window with screen_map."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PyQt6.QtCore import QRect, Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter

OFFICE = {".doc": "word", ".docx": "word", ".docm": "word", ".rtf": "word", ".odt": "word",
          ".ppt": "powerpoint", ".pptx": "powerpoint", ".pptm": "powerpoint", ".odp": "powerpoint",
          ".xls": "excel", ".xlsx": "excel", ".xlsm": "excel", ".ods": "excel"}
KINDS = {".pdf", *OFFICE}
MAX_PAGES = 12  # on one sheet; past it the model asks for a page range
SHEET_SIDE = 1568  # MAX_IMAGE_SIDE: anything bigger is scaled down before it is sent
TIMEOUT_S = 150.0  # Office cold start plus export of a long file

# $Path is a private copy, opened read-only (hidden Word stalls exporting a writable one) in Office's own hidden
# instance, which alone is quit, so the user's open windows and the original file are never touched. office.pid
# names that instance so a timed-out run can end it. PowerShell names ignore case: never reuse a param's name.
PREVIEW_PS = r"""
param([string]$Path, [string]$Out, [string]$Kind, [int]$First, [int]$Last, [int]$Width)
$ErrorActionPreference = 'Stop'
$pdf = "$Out\doc.pdf"  # a plain string: COM gets a Join-Path result wrapped and Word stalls
$exe = @{ word = 'WINWORD'; powerpoint = 'POWERPNT'; excel = 'EXCEL' }[$Kind]
if ($exe) {
    $before = @(Get-Process $exe -ErrorAction SilentlyContinue | ForEach-Object Id)
    $app = New-Object -ComObject "$Kind.Application"
    $mine = @(Get-Process $exe -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } | ForEach-Object Id)
    Set-Content (Join-Path $Out 'office.pid') ($mine -join ' ')
    $was = $app.AutomationSecurity
    $app.AutomationSecurity = 3  # ForceDisable: COM-opened files otherwise run their auto macros, unasked
}
if ($Kind -eq 'word') {
    $app.Visible = $false; $app.DisplayAlerts = 0
    try { $d = $app.Documents.Open($Path, $false, $true, $false); $d.ExportAsFixedFormat($pdf, 17) }
    finally { $app.Quit([ref]0) }  # Close(0)/Quit(0) throw in PowerShell and would leave Word running
} elseif ($Kind -eq 'powerpoint') {
    try { $d = $app.Presentations.Open($Path, -1, 0, 0); $d.SaveAs($pdf, 32); $d.Close() }
    finally {  # one shared instance: keep the user's decks and settings
        $app.AutomationSecurity = $was
        if ($mine -and $app.Presentations.Count -eq 0) { $app.Quit() }
    }
} elseif ($Kind -eq 'excel') {
    $app.Visible = $false; $app.DisplayAlerts = $false
    try { $d = $app.Workbooks.Open($Path, 0, $true); $d.ExportAsFixedFormat(0, $pdf); $d.Close($false) }
    finally { $app.Quit() }
} else { $pdf = $Path }
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Data.Pdf.PdfDocument, Windows.Data.Pdf, ContentType=WindowsRuntime]
$ext = [System.WindowsRuntimeSystemExtensions].GetMethods()
$asTask = ($ext | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asAction = ($ext | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $null = $task.Wait(-1); $task.Result }
function Done($op) { $null = $asAction.Invoke($null, @($op)).Wait(-1) }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($pdf)) ([Windows.Storage.StorageFile])
$doc = Await ([Windows.Data.Pdf.PdfDocument]::LoadFromFileAsync($file)) ([Windows.Data.Pdf.PdfDocument])
$folder = Await ([Windows.Storage.StorageFolder]::GetFolderFromPathAsync($Out)) ([Windows.Storage.StorageFolder])
"pages $($doc.PageCount)"
$Last = [Math]::Min($Last, [int]$doc.PageCount)
for ($i = $First; $i -le $Last; $i++) {
    $page = $doc.GetPage($i - 1)
    $opts = New-Object Windows.Data.Pdf.PdfPageRenderOptions
    $opts.DestinationWidth = $Width
    $png = Await ($folder.CreateFileAsync("p$i.png", 1)) ([Windows.Storage.StorageFile])
    $stream = Await ($png.OpenAsync(1)) ([Windows.Storage.Streams.IRandomAccessStream])
    Done ($page.RenderToStreamAsync($stream, $opts))
    $stream.Dispose(); $page.Dispose()
    "page $i"
}
"""


def page_range(pages: object, count: int = MAX_PAGES) -> tuple[int, int]:
    """(first, last) 1-based from '3' or '2-5' (None: the first MAX_PAGES), or ValueError."""
    if pages in (None, ""):
        return 1, count
    text = str(pages).strip()
    first, _, last = text.partition("-")
    try:
        a, b = int(first), int(last or first)
    except ValueError:
        raise ValueError("pages is a page number such as 3 or a range such as 2-5") from None
    if a < 1 or b < a:
        raise ValueError("pages is a page number such as 3 or a range such as 2-5")
    return a, min(b, a + count - 1)


def grid(n: int) -> tuple[int, int]:
    """(columns, rows) for n pages: as square as fits, wider than tall."""
    cols = 1 if n <= 1 else 2 if n <= 4 else 3 if n <= 9 else 4
    return cols, -(-n // cols)


def contact_sheet(pages: list[tuple[int, QImage]]) -> QImage:
    """The pages side by side on a grey ground, each labelled with its page number, at most SHEET_SIDE wide/tall."""
    cols, rows = grid(len(pages))
    gap, label = 16, 22
    pw = max(p.width() for _, p in pages)
    ph = max(p.height() for _, p in pages)
    scale = min(1.0, (SHEET_SIDE - gap * (cols + 1)) / (pw * cols), (SHEET_SIDE - (gap + label) * rows - gap) / (ph * rows))
    w, h = max(1, int(pw * scale)), max(1, int(ph * scale))
    sheet = QImage(gap + cols * (w + gap), gap + rows * (h + gap + label), QImage.Format.Format_RGB32)
    sheet.fill(QColor("#9a9a9a"))
    painter = QPainter(sheet)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
    for k, (number, image) in enumerate(pages):
        x, y = gap + (k % cols) * (w + gap), gap + (k // cols) * (h + gap + label)
        painter.setPen(QColor("#1a1a1a"))
        painter.drawText(QRect(x, y, w, label), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"page {number}")
        painter.drawImage(QRect(x, y + label, int(image.width() * scale), int(image.height() * scale)), image)
    painter.end()
    return sheet


def _end_office(pid_file: Path) -> None:
    """End the hidden Office instance a timed-out preview started (only ids that were not running before it)."""
    try:
        ids = [int(p) for p in pid_file.read_text(encoding="utf-8-sig").split()]
    except (OSError, ValueError):
        return
    for pid in ids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def preview_document(path: Path, pages: object = None) -> tuple[str, QImage | None]:
    """(text for the model, contact sheet) for a document; the text starts with 'error:' and the image is None
    when it fails."""
    kind = path.suffix.lower()
    if kind not in KINDS:
        return f"error: preview_document reads {', '.join(sorted(KINDS))}, not {kind or 'a file without an extension'}", None
    if not path.is_file():
        return "error: not a file", None
    try:
        first, last = page_range(pages)
    except ValueError as exc:
        return f"error: {exc}", None
    width = 1400 if first == last else 900  # one page alone is shown big enough to read body text
    with tempfile.TemporaryDirectory(prefix="pd_preview_", ignore_cleanup_errors=True) as tmp:
        out = Path(tmp)
        copy = out / f"source{kind}"
        try:
            shutil.copyfile(path, copy)  # never open the user's file itself: it may be open (and locked) in Office
        except OSError as exc:
            return f"error: cannot read the file ({exc.strerror or exc})", None
        script = out / "preview.ps1"
        script.write_text(PREVIEW_PS, encoding="utf-8-sig")
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                 "-Path", str(copy), "-Out", str(out), "-Kind", OFFICE.get(kind, "pdf"),
                 "-First", str(first), "-Last", str(last), "-Width", str(width)],
                capture_output=True, timeout=TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            _end_office(out / "office.pid")
            return f"error: the preview took longer than {TIMEOUT_S:g} s (is an Office dialog open?)", None
        except OSError as exc:
            return f"error: cannot start PowerShell ({exc})", None
        text = proc.stdout.decode("utf-8", errors="replace")
        total = next((int(line.split()[1]) for line in text.splitlines() if line.startswith("pages ")), 0)
        shot = [(n, QImage(str(out / f"p{n}.png"))) for n in range(first, min(last, total) + 1)]
        shot = [(n, img) for n, img in shot if not img.isNull()]
        if proc.returncode != 0 or not shot:
            why = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
            if total and first > total:
                return f"error: the document has {total} page{'' if total == 1 else 's'}", None
            return "error: could not render the document" + (f": {why[0][:300]}" if why else ""), None
        sheet = contact_sheet(shot)
    shown = f"page {first}" if len(shot) == 1 else f"pages {shot[0][0]} to {shot[-1][0]}"
    more = f" Pass pages (e.g. {last + 1}-{min(total, last + MAX_PAGES)}) to see the rest." if total > shot[-1][0] and pages in (None, "") else ""
    return (f"{path.name}: {total} page{'' if total == 1 else 's'}; the image shows {shown} as printed (saved file, not unsaved edits). "
            f"For a closer look at one page pass pages with its number.{more}"), sheet
