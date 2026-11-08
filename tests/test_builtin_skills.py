import subprocess
import sys
from pathlib import Path

import pytest

from src.tools.ai_tools import load_skill, skills_prompt
from src.tools.builtin_skills import BUILTIN, helpers_path


def test_every_builtin_is_listed_and_loads_with_a_real_helpers_path(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    prompt = skills_prompt(root)
    for name, (desc, _body, script, _file) in BUILTIN.items():
        assert f"- {name}: {desc}" in prompt
        body = load_skill(root, name)
        assert f'. "{helpers_path(name)}"' in body and "{helpers}" not in body
        assert helpers_path(name).read_text(encoding="utf-8-sig") == script


def test_a_user_folder_overrides_a_builtin(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    (root / "word-docs").mkdir(parents=True)
    (root / "word-docs" / "SKILL.md").write_text("---\ndescription: mine\n---\nMy way.", encoding="utf-8")
    assert load_skill(root, "word-docs") == "My way."
    assert "- word-docs: mine" in skills_prompt(root) and "- word-docs: Load before" not in skills_prompt(root)


def _has(prog_id: str) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog_id + r"\CLSID"))
    except OSError:
        return False
    return True


def _run(tmp_path: Path, script: str) -> str:
    path = tmp_path / "build.ps1"
    path.write_text("$ErrorActionPreference = 'Stop'\n" + script, encoding="utf-8-sig")
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                           str(path)], capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[:600]
    return proc.stdout.decode("utf-8", "replace")


@pytest.mark.skipif(not _has("Word.Application"), reason="needs Microsoft Word")
def test_the_word_helpers_build_a_document_in_a_hidden_word(tmp_path: Path) -> None:
    out = tmp_path / "helpers.docx"
    text = _run(tmp_path, f"""
$word = New-Object -ComObject Word.Application; $word.Visible = $false; $word.DisplayAlerts = 0
try {{
    $doc = $word.Documents.Add()
    . "{helpers_path('word-docs')}"
    Set-Margins $doc.Sections.Item(1) 3 3 6 3
    $h = Add-Para 'Tide Tables' @{{ size = 28; caps = 1; after = 24; right = 9; keep = 1 }}
    Set-Rule $h 'bottom' '#A8482B' 8 6
    $s = New-Section 4 3 3 3 2
    $poem = Add-Para ("one`vtwo`vthree") @{{ line = 20; left = 0.6; italic = 1 }}
    Set-Rule $poem 'left' '#A8482B' 12 10
    $band = Add-Para 'III  /  HAIKU' @{{ font = 'Segoe UI'; color = '#F4EFE6'; bold = 1 }}
    Set-Band $band '#1F2A2E' 14
    $g = Add-Grid 1 2; $null = Set-Cell $g 1 2 "02`vGull" @{{ size = 10 }}
    Save-Doc "{out}"
    "poem lines: $($poem.Paragraphs.Count) rule: $($poem.ParagraphFormat.Borders.Item(-2).LineStyle) cols: $($s.PageSetup.TextColumns.Count) keep: $($h.ParagraphFormat.KeepWithNext)"
}} finally {{ $word.Quit([ref]0) }}
""")
    assert "2 sections, 1 tables" in text and "poem lines: 1 rule: 1 cols: 2 keep: -1" in text and out.is_file()


@pytest.mark.skipif(not _has("Word.Application"), reason="needs Microsoft Word")
def test_save_doc_saves_over_a_result_another_word_still_holds(tmp_path: Path) -> None:
    out = tmp_path / "rebuild.docx"
    text = _run(tmp_path, f"""
$held = New-Object -ComObject Word.Application; $held.Visible = $false; $held.DisplayAlerts = 0
$word = New-Object -ComObject Word.Application; $word.Visible = $false; $word.DisplayAlerts = 0
try {{
    $first = $held.Documents.Add(); $first.SaveAs2("{out}", 16)   # the last run's result, still open
    $doc = $word.Documents.Add()
    . "{helpers_path('word-docs')}"
    $null = Add-Para 'second build'
    Save-Doc "{out}"
    "held docs left: $($held.Documents.Count)"
}} finally {{ $held.Quit([ref]0); $word.Quit([ref]0) }}
""")
    assert "Saved " in text and "held docs left: 0" in text


@pytest.mark.skipif(not _has("PowerPoint.Application"), reason="needs Microsoft PowerPoint")
def test_the_slide_helpers_build_a_deck(tmp_path: Path) -> None:
    out = tmp_path / "helpers.pptx"
    text = _run(tmp_path, f"""
$ppt = New-Object -ComObject PowerPoint.Application
$had = $ppt.Presentations.Count
try {{
    $deck = $ppt.Presentations.Add(0)
    . "{helpers_path('slides')}"
    $deck.PageSetup.SlideWidth = 960; $deck.PageSetup.SlideHeight = 540
    $s = Add-Slide '#F4EFE6'
    $t = Add-Text $s 'Managing risk' 60 100 600 80 @{{ font = 'Georgia'; size = 48; caps = 1; spacing = 2; align = 'center'; valign = 'middle' }}
    $null = Add-Line $s 60 200 160 200 '#A8482B' 2
    $c = Col 3 3
    $b = Add-Box $s $c[0] 260 $c[1] 180 '#FFFFFF' @{{ line = '#D8D0C2'; radius = 0.1 }}
    Save-Deck "{out}"
    "font: $($t.TextFrame2.TextRange.Font.Name) $($t.TextFrame2.TextRange.Font.Size) col3: $([int]$c[0]) $([int]$c[1]) box: $($b.AutoShapeType)"
    $deck.Close()
}} finally {{ if ($had -eq 0 -and $ppt.Presentations.Count -eq 0) {{ $ppt.Quit() }} }}
""")
    assert "1 slides, 960 x 540 pt" in text and "font: Georgia 48 col3: 636 264 box: 5" in text and out.is_file()


@pytest.mark.skipif(not _has("Excel.Application"), reason="needs Microsoft Excel")
def test_the_sheet_helpers_build_a_workbook(tmp_path: Path) -> None:
    out = tmp_path / "helpers.xlsx"
    text = _run(tmp_path, f"""
$xl = New-Object -ComObject Excel.Application; $xl.Visible = $false; $xl.DisplayAlerts = $false
try {{
    $book = $xl.Workbooks.Add()
    . "{helpers_path('excel-sheets')}"
    $sh = $book.Worksheets.Item(1)
    $t = Set-Table $sh @(@('Region', 'Units', 'Growth'), @('North', 1200, 0.12), @('South', 860, -0.03)) 'B2'
    $null = Set-Style $t @{{ font = 'Segoe UI'; size = 10; border = '#D8D0C2' }}
    $null = Set-Style $t.Rows.Item(1) @{{ bold = 1; fill = '#1F2A2E'; color = '#F4EFE6' }}
    $null = Set-Style $sh.Range('D3:D4') @{{ format = '0.0%' }}
    $null = Add-Banding $sh.Range('B3:D4') '#F4EFE6'
    $null = Add-DataBars $sh.Range('C3:C4') '#E9C9BD'
    Set-Widths $sh @{{ B = 14 }}
    $ch = Add-Chart $sh $sh.Range('B2:C4') 51 20 100 300 180 @{{ title = 'Units'; color = '#A8482B' }}
    Set-Print $sh 1 -Landscape
    Save-Book "{out}"
    "range: $($t.Address(0, 0)) d3: $($sh.Range('D3').Text) rules: $($sh.Range('C3').FormatConditions.Count) chart: $($ch.ChartTitle.Text)"
    $book.Close($false)
}} finally {{ $xl.Quit() }}
""")
    assert "used range B2:D4" in text and "range: B2:D4 d3: 12.0% rules: 2 chart: Units" in text and out.is_file()
