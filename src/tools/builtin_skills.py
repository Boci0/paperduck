"""Skills built into Paperduck: listed with the user's skills and loaded the same way, but shipped and tested
with the app (tests/test_builtin_skills.py runs every helper script in a hidden Office instance). A user skill
folder with the same name overrides one, so update_skill and add_skill still work. load_skill writes a skill's
helper script to the scratch folder and its body names that exact path ({helpers})."""
from __future__ import annotations

from pathlib import Path

from src.tools.ai_tools import scratch_dir

WORD_HELPERS_PS = r"""# Paperduck word-docs helpers (written by load_skill). Dot-source after setting $doc.
# Every helper works on the global $doc (a Word Document) and returns the Range or Paragraph it made.

function WColor([string]$Hex) {
    # Word colours are BGR integers: '#A8482B' -> 0x2B48A8
    $h = $Hex.TrimStart('#')
    [int]('0x' + $h.Substring(4, 2)) * 65536 + [int]('0x' + $h.Substring(2, 2)) * 256 + [int]('0x' + $h.Substring(0, 2))
}

function Cm([double]$Value) { [single]($Value * 28.3465) }  # Word measures in points

# Add one paragraph at the end. Lines joined with "`v" stay one paragraph (one style, one border, tight leading).
# Options (all optional): font size color bold italic caps align(left|center|right|justify) before after
# line (exact leading in pt) left right first (indents in cm) spacing (letter spacing in pt) keep (keep with next)
function Add-Para([string]$Text, [hashtable]$o = @{}) {
    $r = $doc.Content
    $r.Collapse(0)
    $r.InsertAfter($Text)
    $r.InsertParagraphAfter()
    $f = $r.Font
    $f.Name = if ($o.font) { [string]$o.font } else { 'Georgia' }
    $f.Size = [single]$(if ($o.size) { $o.size } else { 11 })
    $f.Color = WColor $(if ($o.color) { $o.color } else { '#1A1A1A' })
    $f.Bold = [int][bool]$o.bold
    $f.Italic = [int][bool]$o.italic
    $f.SmallCaps = [int][bool]$o.caps
    $f.Spacing = [single]$(if ($o.spacing) { $o.spacing } else { 0 })
    $p = $r.ParagraphFormat
    $p.Alignment = @{ left = 0; center = 1; right = 2; justify = 3 }[$(if ($o.align) { $o.align } else { 'left' })]
    $p.SpaceBefore = [single]$(if ($o.before) { $o.before } else { 0 })
    $p.SpaceAfter = [single]$(if ($null -ne $o.after) { $o.after } else { 6 })
    if ($o.line) { $p.LineSpacingRule = 4; $p.LineSpacing = [single]$o.line } else { $p.LineSpacingRule = 0 }
    $p.LeftIndent = Cm $(if ($o.left) { $o.left } else { 0 })
    $p.RightIndent = Cm $(if ($o.right) { $o.right } else { 0 })
    $p.FirstLineIndent = Cm $(if ($o.first) { $o.first } else { 0 })
    $p.KeepWithNext = $(if ($o.keep) { -1 } else { 0 })  # Word refuses 1 here: true is -1
    $r.Borders.Enable = 0
    $r.Shading.BackgroundPatternColor = -16777216  # automatic: no fill carried over from the paragraph before
    $r
}

# A line on one side of a paragraph Range: side top|left|bottom|right, width in eighths of a point (4 6 8 12 18 24)
function Set-Rule($Range, [string]$Side, [string]$Hex, [int]$Width = 8, [double]$GapPt = 4) {
    $b = $Range.ParagraphFormat.Borders.Item(@{ top = -1; left = -2; bottom = -3; right = -4 }[$Side])
    $b.LineStyle = 1; $b.LineWidth = $Width; $b.Color = WColor $Hex
    $d = $Range.ParagraphFormat.Borders
    if ($Side -eq 'left') { $d.DistanceFromLeft = [int]$GapPt } elseif ($Side -eq 'right') { $d.DistanceFromRight = [int]$GapPt }
    elseif ($Side -eq 'top') { $d.DistanceFromTop = [int]$GapPt } else { $d.DistanceFromBottom = [int]$GapPt }
}

# A colour band behind a paragraph Range with $PadPt of colour around the text (borders in the band colour,
# which Word fills with the shading). Indents on the paragraph move the band's edges.
function Set-Band($Range, [string]$Hex, [int]$PadPt = 14) {
    $pf = $Range.ParagraphFormat
    $pf.Shading.BackgroundPatternColor = WColor $Hex
    foreach ($side in -1, -2, -3, -4) {
        $b = $pf.Borders.Item($side); $b.LineStyle = 1; $b.LineWidth = 4; $b.Color = WColor $Hex
    }
    $pf.Borders.DistanceFromTop = $PadPt; $pf.Borders.DistanceFromBottom = $PadPt
    $pf.Borders.DistanceFromLeft = $PadPt; $pf.Borders.DistanceFromRight = $PadPt
}

# Start a new page that is also a new section, so it can have its own margins and columns
function New-Section([double]$Left = 2.5, [double]$Right = 2.5, [double]$Top = 2.5, [double]$Bottom = 2.5, [int]$Columns = 1) {
    $r = $doc.Content; $r.Collapse(0); $r.InsertBreak(2)  # wdSectionBreakNextPage
    $s = $doc.Sections.Item($doc.Sections.Count)
    Set-Margins $s $Left $Right $Top $Bottom
    $s.PageSetup.TextColumns.SetCount($Columns)
    $s
}

function Set-Margins($Section, [double]$Left, [double]$Right, [double]$Top, [double]$Bottom) {
    $ps = $Section.PageSetup
    $ps.LeftMargin = Cm $Left; $ps.RightMargin = Cm $Right; $ps.TopMargin = Cm $Top; $ps.BottomMargin = Cm $Bottom
}

# A borderless grid for cards or columns of short items; fill cells with Set-Cell
function Add-Grid([int]$Rows, [int]$Cols) {
    $r = $doc.Content; $r.Collapse(0)
    $t = $doc.Tables.Add($r, $Rows, $Cols)
    $t.Borders.Enable = 0
    $t.TopPadding = Cm 0.2; $t.BottomPadding = Cm 0.4
    $doc.Content.InsertParagraphAfter()
    $t
}

function Set-Cell($Table, [int]$Row, [int]$Col, [string]$Text, [hashtable]$o = @{}) {
    $r = $Table.Cell($Row, $Col).Range
    $r.Text = $Text
    $r.Font.Name = if ($o.font) { [string]$o.font } else { 'Georgia' }
    $r.Font.Size = [single]$(if ($o.size) { $o.size } else { 11 })
    $r.Font.Color = WColor $(if ($o.color) { $o.color } else { '#1A1A1A' })
    $r.Font.Bold = [int][bool]$o.bold
    $r.Font.Italic = [int][bool]$o.italic
    $r.ParagraphFormat.SpaceAfter = [single]$(if ($null -ne $o.after) { $o.after } else { 4 })
    $r
}

# Save as .docx (path must be a plain "..." string, not a Join-Path result) and report what a reader can check
function Save-Doc([string]$Path) {
    # A rebuild saving over the last run's result, still open in Word, would fail "already open elsewhere":
    # close exactly that document (found by its file in Word's running table), never a whole Word.
    if ((Test-Path -LiteralPath $Path) -and $doc.FullName -ne $Path) {
        $locked = try { [IO.File]::Open($Path, 'Open', 'ReadWrite', 'None').Close(); $false } catch { $true }
        if ($locked) {
            try { [Runtime.InteropServices.Marshal]::BindToMoniker($Path).Close([ref]0) } catch { }
        }
    }
    $doc.SaveAs2([string]$Path, 16)  # wdFormatDocumentDefault
    "Saved $Path : $($doc.ComputeStatistics(2)) pages, $($doc.Sections.Count) sections, $($doc.Tables.Count) tables"
}
"""

WORD_DOCS = r"""Building a Word document is one script, one preview, at most two fix passes. Do not write raw COM for paragraphs,
rules, bands, sections or grids: dot-source the tested helpers, so the first run works.

## 1. The script skeleton (run_powershell)
```
$ErrorActionPreference = 'Stop'
$word = New-Object -ComObject Word.Application; $word.Visible = $true
$doc = $word.Documents.Add()          # or: $doc = $word.Documents.Open("C:\full\path.docx")
. "{helpers}"
Set-Margins $doc.Sections.Item(1) 3 3 6 3                     # cm: left right top bottom
$null = Add-Para 'POEMS' @{ font='Segoe UI'; size=9; color='#A8482B'; bold=1; spacing=3; align='center'; after=18 }
$h = Add-Para 'Tide Tables' @{ size=28; caps=1; after=24; right=9 }
Set-Rule $h 'bottom' '#A8482B' 8 6                            # short rule under a title (right indent sets its length)
$s = New-Section 4 3 3 3                                      # new page with its own margins (and columns: 6th arg)
$poem = Add-Para ("line one`vline two`vline three") @{ size=11; line=20; left=0.6 }
Set-Rule $poem 'left' '#A8482B' 12 10                         # one unbroken rule down the whole stanza
$band = Add-Para 'III  /  HAIKU' @{ font='Segoe UI'; size=9; color='#F4EFE6'; bold=1; spacing=3 }
Set-Band $band '#1F2A2E' 14                                   # dark band with 14 pt padding
$g = Add-Grid 3 2; $null = Set-Cell $g 1 1 "01`vLow tide." @{ size=10 }
Save-Doc "C:\Users\me\Documents\Poems.docx"
```
Add-Para options: font size color bold italic caps align(left|center|right|justify) before after (pt) line (exact
leading, pt) left right first (indents, cm) spacing (letter spacing, pt) keep (keep with next).

## 2. Word traps (the Office ones in the system prompt apply too)
- Save-Doc closes the last run's copy of the same file before saving over it, so a rebuild needs no clean-up.
  Never stop a Word process: that closes the user's own documents too.
- Formatting lives on `Range.Font` and `Range.ParagraphFormat`. There is no `.Space` on a border and no
  `SpaceAfter` on PageSetup. `if` used as a value needs `$( )` and a cast: `[single]$(if ($x) { 1 } else { 0 })`.
- A drop cap is set on the Paragraph, not its Range: `$p.DropCap.Enable(); $p.DropCap.LinesToDrop = 3`.
- Poem lines: join them with "`v" (line break) inside one paragraph. Separate paragraphs per line break borders,
  leading and keep-together.

## 3. Check with preview_document, not the window
Save, then call preview_document on the file once: every page in one image. Do not page through Word with
screen_map. Fix everything you see in ONE script (open the saved file, change, save), preview once more, stop.
Number cards in reading order (left to right, then down), not down the columns.
"""

SLIDE_HELPERS_PS = r"""# Paperduck slides helpers (written by load_skill). Dot-source after setting $deck (a PowerPoint Presentation).
# Positions and sizes are points: a 16:9 deck is 960 x 540. Every helper returns the Slide or Shape it made.

function PColor([string]$Hex) {
    # Office colours are BGR integers: '#A8482B' -> 0x2B48A8
    $h = $Hex.TrimStart('#')
    [int]('0x' + $h.Substring(4, 2)) * 65536 + [int]('0x' + $h.Substring(2, 2)) * 256 + [int]('0x' + $h.Substring(0, 2))
}

# x and width of column $i (1-based) of $n across the slide, inside $Margin with $Gap between: @(x, w)
function Col([int]$i, [int]$n, [double]$Margin = 60, [double]$Gap = 24) {
    $w = ($deck.PageSetup.SlideWidth - 2 * $Margin - ($n - 1) * $Gap) / $n
    @([single]($Margin + ($i - 1) * ($w + $Gap)), [single]$w)
}

# A blank slide at the end with a solid background
function Add-Slide([string]$Bg = '#FFFFFF') {
    $s = $deck.Slides.Add($deck.Slides.Count + 1, 12)  # ppLayoutBlank
    $s.FollowMasterBackground = 0
    $s.Background.Fill.Solid()
    $s.Background.Fill.ForeColor.RGB = PColor $Bg
    $s
}

# A text box with exact type. Lines joined with "`r" are paragraphs. Options: font size color bold italic caps
# align(left|center|right) valign(top|middle|bottom) spacing (letter spacing, pt) line (line spacing, multiple) after (pt)
function Add-Text($Slide, [string]$Text, [double]$X, [double]$Y, [double]$W, [double]$H, [hashtable]$o = @{}) {
    $sh = $Slide.Shapes.AddTextbox(1, $X, $Y, $W, $H)
    $tf = $sh.TextFrame2
    $tf.AutoSize = 0; $tf.WordWrap = -1
    $tf.MarginLeft = 0; $tf.MarginRight = 0; $tf.MarginTop = 0; $tf.MarginBottom = 0
    $tf.VerticalAnchor = @{ top = 1; middle = 3; bottom = 4 }[$(if ($o.valign) { $o.valign } else { 'top' })]
    $tf.TextRange.Text = $Text
    $f = $tf.TextRange.Font
    $f.Name = if ($o.font) { [string]$o.font } else { 'Segoe UI' }
    $f.Size = [single]$(if ($o.size) { $o.size } else { 18 })
    $f.Fill.ForeColor.RGB = PColor $(if ($o.color) { $o.color } else { '#1A1A1A' })
    $f.Bold = [int][bool]$o.bold * -1
    $f.Italic = [int][bool]$o.italic * -1
    if ($o.caps) { $f.Allcaps = -1 }
    if ($o.spacing) { $f.Spacing = [single]$o.spacing }
    $p = $tf.TextRange.ParagraphFormat
    $p.Alignment = @{ left = 1; center = 2; right = 3 }[$(if ($o.align) { $o.align } else { 'left' })]
    if ($o.line) { $p.SpaceWithin = [single]$o.line }
    if ($o.after) { $p.SpaceAfter = [single]$o.after }
    $sh
}

# A rectangle (or rounded: @{ radius = 0.1 }) filled with $Fill; @{ line = '#hex'; weight = 1 } adds an outline
function Add-Box($Slide, [double]$X, [double]$Y, [double]$W, [double]$H, [string]$Fill, [hashtable]$o = @{}) {
    $sh = $Slide.Shapes.AddShape($(if ($o.radius) { 5 } else { 1 }), $X, $Y, $W, $H)
    if ($o.radius) { $sh.Adjustments.Item(1) = [single]$o.radius }
    if ($Fill) { $sh.Fill.Solid(); $sh.Fill.ForeColor.RGB = PColor $Fill } else { $sh.Fill.Visible = 0 }
    if ($o.line) { $sh.Line.ForeColor.RGB = PColor $o.line; $sh.Line.Weight = [single]$(if ($o.weight) { $o.weight } else { 1 }) }
    else { $sh.Line.Visible = 0 }
    $sh.Shadow.Visible = 0
    $sh
}

# A thin straight line (connectors, rules)
function Add-Line($Slide, [double]$X1, [double]$Y1, [double]$X2, [double]$Y2, [string]$Hex = '#1A1A1A', [double]$Weight = 1) {
    $sh = $Slide.Shapes.AddLine($X1, $Y1, $X2, $Y2)
    $sh.Line.ForeColor.RGB = PColor $Hex
    $sh.Line.Weight = [single]$Weight
    $sh
}

# A picture at ($X, $Y), $W wide, height kept in proportion
function Add-Image($Slide, [string]$Path, [double]$X, [double]$Y, [double]$W) {
    $sh = $Slide.Shapes.AddPicture([string]$Path, 0, -1, $X, $Y)
    $sh.LockAspectRatio = -1
    $sh.Width = [single]$W
    $sh
}

# Save as .pptx (a plain "..." path, not a Join-Path result) and report what a reader can check
function Save-Deck([string]$Path) {
    $deck.SaveAs([string]$Path, 24)  # ppSaveAsOpenXMLPresentation
    "Saved $Path : $($deck.Slides.Count) slides, $($deck.PageSetup.SlideWidth) x $($deck.PageSetup.SlideHeight) pt"
}
"""

SLIDES = r"""Building a deck is one script, one preview, at most two fix passes. Do not write raw COM for slides, text,
boxes or lines: dot-source the tested helpers, so the first run works. Positions are points (16:9 is 960 x 540).

## 1. The script skeleton (run_powershell)
```
$ErrorActionPreference = 'Stop'
$ppt = New-Object -ComObject PowerPoint.Application   # ONE shared instance: the user's open decks live here too
$deck = $ppt.Presentations.Add(-1)                     # or: $deck = $ppt.Presentations.Open("C:\full\path.pptx")
. "{helpers}"
$deck.PageSetup.SlideWidth = 960; $deck.PageSetup.SlideHeight = 540
$s = Add-Slide '#F4EFE6'
$null = Add-Text $s 'RISK  /  WEEK 7' 60 70 400 20 @{ size=11; color='#A8482B'; bold=1; spacing=3 }
$null = Add-Text $s 'Managing project risk' 60 100 600 80 @{ font='Georgia'; size=48; color='#1A2B33' }
$null = Add-Line $s 60 200 160 200 '#A8482B' 2
foreach ($i in 1..3) {
    $c = Col $i 3                                      # x and width of column i of 3, 60 pt margins, 24 pt gaps
    $null = Add-Box $s $c[0] 260 $c[1] 180 '#FFFFFF' @{ line='#D8D0C2'; weight=0.75 }
    $null = Add-Text $s "Identify`rList what could go wrong." ($c[0]+20) 280 ($c[1]-40) 140 @{ size=16; line=1.2 }
}
Save-Deck "C:\Users\me\Documents\Risk.pptx"
```
Add-Text options: font size color bold italic caps align(left|center|right) valign(top|middle|bottom) spacing
(letter spacing, pt) line (line spacing, multiple) after (pt). "`r" starts a new paragraph. Add-Box: @{ radius=0.15 }
for rounded corners, @{ line='#hex'; weight=1 } for an outline. Add-Image $s "C:\pic.png" x y width.

## 2. Traps
- Never call $ppt.Quit() on the shared instance while other decks are open (it closes the user's work). Leave the
  finished deck open for the user, or $deck.Close() a scratch one.
- Shapes are placed by numbers, so derive them from a grid (Col, fixed margins, a baseline y per row), never by
  eye. Keep 60 pt margins clear on every slide.
- Text boxes do not grow: a box too small for its text spills over the next shape. Give it room, or cut words.
- Body text at least 16 pt (20 when projected), titles at least 2.5x the body. One idea per slide.

## 3. Check with preview_document
Save, then preview_document on the .pptx: every slide in one image. Fix everything you see in ONE script
(open the saved deck, change, save), preview once more, stop.
"""

SHEET_HELPERS_PS = r"""# Paperduck excel-sheets helpers (written by load_skill). Dot-source after setting $book (an Excel Workbook).
# Every helper takes a Worksheet or Range and returns the Range or Chart it made. Return a Range from your own
# function as ,$range: PowerShell otherwise unrolls it into single cells.

function XColor([string]$Hex) {
    # Office colours are BGR integers: '#A8482B' -> 0x2B48A8
    $h = $Hex.TrimStart('#')
    [int]('0x' + $h.Substring(4, 2)) * 65536 + [int]('0x' + $h.Substring(2, 2)) * 256 + [int]('0x' + $h.Substring(0, 2))
}

# Write rows (an array of arrays, the first usually the header) in one call from $TopLeft; returns the whole Range
function Set-Table($Sheet, [object[]]$Rows, [string]$TopLeft = 'A1') {
    $cols = ($Rows | ForEach-Object { @($_).Count } | Measure-Object -Maximum).Maximum
    $data = New-Object 'object[,]' $Rows.Count, $cols
    for ($r = 0; $r -lt $Rows.Count; $r++) { $row = @($Rows[$r]); for ($c = 0; $c -lt $row.Count; $c++) { $data[$r, $c] = $row[$c] } }
    $start = $Sheet.Range($TopLeft)
    $range = $start.Resize($Rows.Count, $cols)
    $range.Value2 = $data
    ,$range  # the comma keeps PowerShell from unrolling the Range into cells
}

# Font, fill and layout on a Range. Options: font size color bold italic fill align(left|center|right) wrap
# format (number format, e.g. '#,##0' '0.0%' 'yyyy-mm-dd' '"RM "#,##0.00') border (hex, thin line under each row)
function Set-Style($Range, [hashtable]$o = @{}) {
    if ($o.font) { $Range.Font.Name = [string]$o.font }
    if ($o.size) { $Range.Font.Size = [single]$o.size }
    if ($o.color) { $Range.Font.Color = XColor $o.color }
    if ($null -ne $o.bold) { $Range.Font.Bold = [bool]$o.bold }
    if ($null -ne $o.italic) { $Range.Font.Italic = [bool]$o.italic }
    if ($o.fill) { $Range.Interior.Color = XColor $o.fill }
    if ($o.align) { $Range.HorizontalAlignment = @{ left = -4131; center = -4108; right = -4152 }[$o.align] }
    if ($null -ne $o.wrap) { $Range.WrapText = [bool]$o.wrap }
    if ($o.format) { $Range.NumberFormat = [string]$o.format }
    if ($o.border) { $b = $Range.Borders.Item(12); $b.LineStyle = 1; $b.Weight = 2; $b.Color = XColor $o.border  # inside horizontal
        $b = $Range.Borders.Item(9); $b.LineStyle = 1; $b.Weight = 2; $b.Color = XColor $o.border }  # bottom edge
    $Range.VerticalAlignment = -4108
    ,$Range
}

# Column widths in characters: Set-Widths $sheet @{ A = 14; B = 36 }; no table: autofit every used column
function Set-Widths($Sheet, [hashtable]$Widths = @{}) {
    if ($Widths.Count -eq 0) { $null = $Sheet.UsedRange.Columns.AutoFit(); return }
    foreach ($k in $Widths.Keys) { $Sheet.Columns.Item([string]$k).ColumnWidth = [double]$Widths[$k] }
}

# Every other row of $Range tinted $Hex (a rule, so it stays right after sorting)
function Add-Banding($Range, [string]$Hex) {
    $fc = $Range.FormatConditions.Add(2, 0, '=MOD(ROW(),2)=0')  # xlExpression
    $fc.Interior.Color = XColor $Hex
    $fc.StopIfTrue = $false  # or data bars and other rules on banded rows never draw
    ,$Range
}

# In-cell bars for a numeric column, measured from zero; use a light tint so the numbers stay readable
function Add-DataBars($Range, [string]$Hex) {
    $db = $Range.FormatConditions.AddDatabar()
    $db.BarColor.Color = XColor $Hex
    $db.BarFillType = 0  # solid
    $db.MinPoint.Modify(0, 0)  # xlConditionValueNumber 0: the smallest value still gets a bar
    ,$Range
}

# A chart of $Data (header row included) at ($X, $Y) points, $W x $H. Types: column 51, bar 57, line 4,
# pie 5, scatter -4169. Options: title color (series fill) legend (show it) gridlines (keep them)
function Add-Chart($Sheet, $Data, [int]$Type, [double]$X, [double]$Y, [double]$W, [double]$H, [hashtable]$o = @{}) {
    $ch = $Sheet.Shapes.AddChart2(-1, $Type, $X, $Y, $W, $H).Chart
    $ch.SetSourceData($Data)
    $ch.HasTitle = [bool]$o.title
    if ($o.title) { $ch.ChartTitle.Text = [string]$o.title }
    $ch.HasLegend = [bool]$o.legend
    if ($o.color -and $Type -ne 5) {
        $s = $ch.SeriesCollection(1); $s.Format.Fill.ForeColor.RGB = XColor $o.color; $s.Format.Line.ForeColor.RGB = XColor $o.color
    }
    if (-not $o.gridlines -and $Type -ne 5) { $ch.Axes(2).HasMajorGridlines = $false }
    $ch
}

# Print setup so the sheet prints (and previews) on whole pages: fit to $Wide pages across, optional landscape
function Set-Print($Sheet, [int]$Wide = 1, [switch]$Landscape) {
    $ps = $Sheet.PageSetup
    $ps.Zoom = $false; $ps.FitToPagesWide = $Wide; $ps.FitToPagesTall = $false
    if ($Landscape) { $ps.Orientation = 2 }
    $ps.CenterHorizontally = $true
}

# Save as .xlsx (a plain "..." path, not a Join-Path result) and report what a reader can check
function Save-Book([string]$Path) {
    $book.SaveAs([string]$Path, 51)  # xlOpenXMLWorkbook
    "Saved $Path : $($book.Worksheets.Count) sheets, used range $($book.Worksheets.Item(1).UsedRange.Address(0, 0))"
}
"""

EXCEL_SHEETS = r"""Building a workbook is one script, one preview, at most two fix passes. Do not write cells one by one or style
with raw COM: dot-source the tested helpers.

## 1. The script skeleton (run_powershell)
```
$ErrorActionPreference = 'Stop'
$xl = New-Object -ComObject Excel.Application; $xl.Visible = $true   # its own instance
$book = $xl.Workbooks.Add()                                          # or: $book = $xl.Workbooks.Open("C:\full\path.xlsx")
. "{helpers}"
$sh = $book.Worksheets.Item(1); $sh.Name = 'Sales'
$t = Set-Table $sh @(@('Region','Units','Revenue','Growth'), @('North',1200,18450.5,0.12), @('South',860,12010,-0.03)) 'B2'
$null = Set-Style $t @{ font='Segoe UI'; size=10; color='#1A2B33'; border='#D8D0C2' }
$null = Set-Style $t.Rows.Item(1) @{ bold=1; fill='#1F2A2E'; color='#F4EFE6' }
$null = Set-Style $sh.Range('D3:D4') @{ format='"RM "#,##0.00' }
$null = Set-Style $sh.Range('E3:E4') @{ format='0.0%' }
$null = Add-Banding $sh.Range('B3:E4') '#F4EFE6'
$null = Add-DataBars $sh.Range('C3:C4') '#E9C9BD'                   # a light tint keeps the numbers readable
Set-Widths $sh @{ A=3; B=14; C=12; D=16; E=10 }
$null = Add-Chart $sh $sh.Range('B2:C4') 51 20 100 360 200 @{ title='Units by region'; color='#A8482B' }
Set-Print $sh 1 -Landscape
Save-Book "C:\Users\me\Documents\Sales.xlsx"
```
Set-Style options: font size color bold italic fill align(left|center|right) wrap format border. Chart types:
column 51, bar 57, line 4, pie 5, scatter -4169; options title color legend gridlines.

## 2. Traps
- An Excel Range returned from your own function unrolls into single cells: return it as ,$range.
- Write a whole table with one Set-Table (cell-by-cell COM is very slow). Put numbers and dates in as numbers
  ([datetime] for dates), never as text, or formats, sorting and charts break. Formulas: $sh.Range('C7').Formula = '=SUM(C3:C6)'.
- One rule colour per meaning: header fill, one accent for charts and bars, a pale band. No rainbow series.
- Quit only an instance you started hidden; a visible workbook is the user's result, leave it open.

## 3. Check with preview_document
Set-Print first (so the sheet fits one page wide), save, then preview_document on the .xlsx. Fix everything you
see in ONE script, preview once more, stop.
"""

# name -> (description for the system prompt, body, helper script, its file name)
BUILTIN = {
    "word-docs": ("Load before building or styling a Word document through COM: tested helper functions, the COM "
                  "traps that waste runs, and the build, preview, fix loop", WORD_DOCS, WORD_HELPERS_PS,
                  "word_helpers.ps1"),
    "slides": ("Load before building or styling a PowerPoint deck through COM: tested helpers for slides, text, "
               "boxes, lines and a column grid, and the build, preview, fix loop", SLIDES, SLIDE_HELPERS_PS,
               "slide_helpers.ps1"),
    "excel-sheets": ("Load before building or styling an Excel workbook through COM: tested helpers for tables, "
                     "number formats, banding, data bars, charts and print setup, and the build, preview, fix loop",
                     EXCEL_SHEETS, SHEET_HELPERS_PS, "sheet_helpers.ps1"),
}


def helpers_path(name: str) -> Path:
    """A built-in skill's helper script in the scratch folder, (re)written when missing or out of date."""
    _desc, _body, script, file_name = BUILTIN[name]
    path = scratch_dir() / file_name
    try:
        if not path.is_file() or path.read_text(encoding="utf-8-sig") != script:
            path.write_text(script, encoding="utf-8-sig")  # BOM: Windows PowerShell 5.1 reads UTF-8 then
    except OSError:
        pass  # the body still names the path; the model's run reports the real error
    return path


def builtin_body(name: str) -> str | None:
    """A built-in skill's instructions, or None when there is no such built-in skill."""
    if name not in BUILTIN:
        return None
    return BUILTIN[name][1].replace("{helpers}", str(helpers_path(name))).strip()
