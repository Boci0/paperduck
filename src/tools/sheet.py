"""make_sheet: a one-page A4 handout (cheat sheet, summary, revision sheet) from a title and sections of points.

The layout is a fixed HTML template, not the model's clicking: columns, styled headings and even margins come from
CSS, and a script in the page sizes the font so the content fills exactly one page. Headless Edge (or Chrome) prints
it to PDF and takes a preview PNG. Text is escaped; only `code` and **bold** marks become markup."""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from src.tools.ai_tools import file_stamp, wait_for_new_file
from src.tools.file_forge import scrub_metadata

MAX_SECTIONS = 24
MAX_POINTS = 40  # per section
MAX_SHEET_CHARS = 12000  # all text: past this a page is unreadable whatever the size
ACCENTS = {"blue": "#1f4e79", "teal": "#0f6b66", "plum": "#6b2d6b", "rust": "#a2401d", "slate": "#2f3b4a"}
RENDER_TIMEOUT_S = 30.0
_MARK = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*")

_CSS = """
@page { size: A4; margin: 0 }
html, body { margin: 0; background: #fff }
:root { --fs: 12px; --accent: %(accent)s; --cols: %(cols)d }
.page { width: 210mm; height: 297mm; box-sizing: border-box; padding: 12mm 12mm 10mm; display: flex;
  flex-direction: column; font-family: "Segoe UI", Arial, sans-serif; color: #1b1f24; overflow: hidden }
body.over .page { height: auto; overflow: visible }
header { display: flex; align-items: baseline; justify-content: space-between; gap: 6mm;
  border-bottom: 1.6px solid #1b1f24; padding-bottom: 2.2mm; margin-bottom: 4mm }
h1 { margin: 0; font-size: calc(var(--fs) * 2.1); line-height: 1.1; font-weight: 700; letter-spacing: -0.01em }
.sub { font-size: calc(var(--fs) * 0.95); color: #5b636e; text-align: right }
.cols { flex: 1; min-height: 0; column-count: var(--cols); column-gap: 7mm; column-rule: 0.6px solid #d9dde2;
  column-fill: balance; font-size: var(--fs); line-height: 1.36 }
.sec { break-inside: avoid; margin: 0 0 1.05em }
body.loose .sec { break-inside: auto }
h2 { margin: 0 0 0.45em; padding-bottom: 0.2em; border-bottom: 1.2px solid var(--accent); color: var(--accent);
  font-size: calc(var(--fs) * 0.95); font-weight: 700; text-transform: uppercase; letter-spacing: 0.07em }
ul { margin: 0; padding-left: 0.95em }
li { margin: 0 0 0.3em; overflow-wrap: anywhere }
li::marker { content: "\\2013  "; color: #9aa1a9 }
b { font-weight: 600 }
code { font-family: Consolas, "Cascadia Mono", monospace; font-size: 0.95em; color: var(--accent) }
"""

# Largest font (px at 96 dpi) at which nothing overflows; sections may split across columns only as a last resort
_FIT = """
const root = document.documentElement, cols = document.querySelector('.cols');
const fits = () => cols.scrollHeight <= cols.clientHeight + 1 && cols.scrollWidth <= cols.clientWidth + 1;
function best() {
  let lo = 7, hi = 15;
  root.style.setProperty('--fs', hi + 'px');
  if (fits()) return hi;
  for (let i = 0; i < 14; i++) {
    const mid = (lo + hi) / 2;
    root.style.setProperty('--fs', mid + 'px');
    if (fits()) lo = mid; else hi = mid;
  }
  root.style.setProperty('--fs', lo + 'px');
  return fits() ? lo : 0;
}
if (!best()) { document.body.classList.add('loose'); if (!best()) document.body.classList.add('over'); }
"""


def _inline(text: str) -> str:
    """Escaped text with `code` and **bold** turned into markup."""
    out, at = [], 0
    for m in _MARK.finditer(text):
        out.append(html.escape(text[at:m.start()]))
        out.append(f"<code>{html.escape(m.group(1))}</code>" if m.group(1) is not None
                   else f"<b>{html.escape(m.group(2))}</b>")
        at = m.end()
    out.append(html.escape(text[at:]))
    return "".join(out)


def check_spec(spec: dict[str, Any]) -> dict[str, Any] | str:
    """The sheet's fields, cleaned, or an error string for the model."""
    title = spec.get("title")
    if not isinstance(title, str) or not title.strip():
        return "error: give a title"
    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        return "error: give sections: a list of {heading, points}"
    if len(sections) > MAX_SECTIONS:
        return f"error: at most {MAX_SECTIONS} sections"
    clean = []
    for i, sec in enumerate(sections, 1):
        if not isinstance(sec, dict) or not isinstance(sec.get("heading"), str) or not sec["heading"].strip():
            return f"error: section {i} needs a heading"
        points = sec.get("points")
        if (not isinstance(points, list) or not points or len(points) > MAX_POINTS
                or not all(isinstance(p, str) and p.strip() for p in points)):
            return f"error: section {i} needs 1 to {MAX_POINTS} points, each a non-empty string"
        clean.append({"heading": sec["heading"].strip(), "points": [p.strip() for p in points]})
    subtitle = spec.get("subtitle") if isinstance(spec.get("subtitle"), str) else ""
    total = len(title) + len(subtitle) + sum(len(s["heading"]) + sum(map(len, s["points"])) for s in clean)
    if total > MAX_SHEET_CHARS:
        return f"error: {total} characters is too much for one page (at most {MAX_SHEET_CHARS}); cut it down"
    accent = spec.get("accent") if spec.get("accent") in ACCENTS else "blue"
    columns = spec.get("columns")
    if not (isinstance(columns, int) and not isinstance(columns, bool) and 1 <= columns <= 3):
        columns = 3 if total > 3500 else 2 if total > 600 else 1
    name = spec.get("file_name") if isinstance(spec.get("file_name"), str) else ""
    name = re.sub(r"[^\w\- ]+", "", name.removesuffix(".pdf")).strip()[:60] or "sheet"
    return {"title": title.strip(), "subtitle": subtitle.strip(), "sections": clean, "accent": accent,
            "columns": columns, "file_name": name, "chars": total}


def sheet_html(spec: dict[str, Any]) -> str:
    """The page for a spec from `check_spec`."""
    secs = "".join(
        f'<section class="sec"><h2>{_inline(s["heading"])}</h2><ul>'
        + "".join(f"<li>{_inline(p)}</li>" for p in s["points"]) + "</ul></section>"
        for s in spec["sections"])
    sub = f'<div class="sub">{_inline(spec["subtitle"])}</div>' if spec["subtitle"] else ""
    return ("<!doctype html><html><head><meta charset='utf-8'><title></title><style>"
            + _CSS % {"accent": ACCENTS[spec["accent"]], "cols": spec["columns"]}
            + f"</style></head><body><div class='page'><header><h1>{_inline(spec['title'])}</h1>{sub}</header>"
            + f"<div class='cols'>{secs}</div></div><script>{_FIT}</script></body></html>")


def find_browser() -> Path | None:
    """Edge, else Chrome: both print a page to PDF headless."""
    for env, rel in (("ProgramFiles(x86)", r"Microsoft\Edge\Application\msedge.exe"),
                     ("ProgramFiles", r"Microsoft\Edge\Application\msedge.exe"),
                     ("ProgramFiles", r"Google\Chrome\Application\chrome.exe"),
                     ("LOCALAPPDATA", r"Google\Chrome\Application\chrome.exe")):
        base = os.environ.get(env)
        if base and (Path(base) / rel).is_file():
            return Path(base) / rel
    found = shutil.which("msedge") or shutil.which("chrome")
    return Path(found) if found else None


def page_count(pdf: Path) -> int:
    """Pages in a PDF Chrome wrote (its page objects are plain `/Type /Page` entries)."""
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", pdf.read_bytes()))


def render_sheet(spec: dict[str, Any], out_dir: Path, cancel: threading.Event | None = None) -> tuple[Path, Path] | str:
    """(PDF, preview PNG) in `out_dir`, or an error string. A throwaway browser profile keeps the user's own
    Edge (its tabs, sign-ins) out of it."""
    browser = find_browser()
    if browser is None:
        return "error: make_sheet needs Microsoft Edge or Google Chrome installed"
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf, png = out_dir / f"{spec['file_name']}.pdf", out_dir / f"{spec['file_name']}.png"
    work = Path(tempfile.mkdtemp(prefix="paperduck_sheet_"))
    try:
        page = work / "sheet.html"
        page.write_text(sheet_html(spec), encoding="utf-8")
        common = [str(browser), "--headless=new", "--disable-gpu", "--no-first-run", "--disable-extensions",
                  f"--user-data-dir={work / 'profile'}", "--virtual-time-budget=4000", "--hide-scrollbars"]
        for flag, target in ((f"--print-to-pdf={pdf}", pdf), (f"--screenshot={png}", png)):
            before = file_stamp(target)
            # 2x preview: sharp enough to place in Canva as the page itself
            extra = (["--no-pdf-header-footer"] if target is pdf
                     else ["--window-size=794,1123", "--force-device-scale-factor=2"])
            try:
                subprocess.run([*common, *extra, flag, page.as_uri()], capture_output=True, timeout=RENDER_TIMEOUT_S,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"error: the browser did not finish ({type(exc).__name__})"
            # msedge.exe can hand off to a child and return before the file is written
            if not wait_for_new_file(target, before, RENDER_TIMEOUT_S, cancel):
                return f"error: the browser wrote no {target.suffix[1:].upper()}"
        for made in (pdf, png):
            scrub_metadata(made)
        return pdf, png
    finally:
        shutil.rmtree(work, ignore_errors=True)
