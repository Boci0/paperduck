# Paperduck installer / updater for Windows 10 and 11 (per-user, no admin).
#
#   From a clone:   powershell -ExecutionPolicy Bypass -File .\install.ps1
#   One line:       irm https://raw.githubusercontent.com/Boci0/paperduck/main/install.ps1 | iex
#
# Downloads install the release's standalone build (paperduck-win64.zip, no Python needed):
#          %LOCALAPPDATA%\Paperduck\app   (Paperduck.exe, _internal, uninstall.ps1, .installed marker)
# A clone, or a release without that build, installs from source into a private Python environment:
#          %LOCALAPPDATA%\Paperduck\app   (src, assets, uninstall.ps1, .installed marker)
#          %LOCALAPPDATA%\Paperduck\venv
# Settings and state live in %USERPROFILE%\.paperduck and are kept across updates.
# Re-running it updates an existing install in place; a standalone install replaces an older venv one.
# An old ByteSprite install (any version) is migrated: its settings, chats, skills and MCP servers are copied
# over (files Paperduck already has are kept), its Start with Windows and Desktop shortcut carry over, and the
# old program, shortcuts and registry entries are removed. The old data folders stay as *-backup.

param(
    [switch]$DesktopShortcut,
    [switch]$StartWithWindows,
    [switch]$NoLaunch
)

$ErrorActionPreference = "Stop"
$Repo = "Boci0/paperduck"
$Root = Join-Path $env:LOCALAPPDATA "Paperduck"
$App = Join-Path $Root "app"
$Venv = Join-Path $Root "venv"
$StandaloneAsset = "paperduck-win64.zip"
# Must match RELEASE_PUBLIC_KEY in src/updater.py and sha256(src/signing.py); tests/test_updater.py checks both
$ReleasePublicKey = "f0cc212a80d53fe877827253c020e2c01d023c4280eebf47423c6032a0259316"
$SigningPySha256 = "B415F26BF5BA35C3BA626DCB6D22FED740714BBBC5E185B5A99B766B7B73ED15"

# A shell started by a packaged app (such as the Claude desktop app) writes new AppData files to that app's private
# copy while deletes still hit the real folder, so installing from it destroys the real install (writes_redirected)
New-Item -ItemType Directory -Force $Root | Out-Null
$probe = ".write-probe-$PID"
Set-Content -LiteralPath (Join-Path $Root $probe) -Value ""
$sandboxed = Test-Path (Join-Path $env:LOCALAPPDATA "Packages\*\LocalCache\Local\Paperduck\$probe")
Remove-Item -LiteralPath (Join-Path $Root $probe) -Force -ErrorAction SilentlyContinue
if ($sandboxed) { throw "This shell runs inside another app's sandbox (its AppData writes are redirected). Run the installer from a normal PowerShell window." }

function Find-Python {
    $ErrorActionPreference = "Continue"  # a missing runtime writes to stderr, which 5.1 would make fatal
    foreach ($cmd in @(@("py", "-3.12"), @("py", "-3"), @("python"))) {
        $exe = Get-Command $cmd[0] -ErrorAction SilentlyContinue
        if (-not $exe) { continue }
        $pyArgs = @($cmd | Select-Object -Skip 1)
        $ver = & $exe.Source @pyArgs -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]"3.11") {
            return , (@($exe.Source) + $pyArgs)  # leading comma: never unroll to a bare string
        }
    }
    throw "Python 3.11+ not found. Install it with:  winget install Python.Python.3.12"
}

function ConvertFrom-Hex([string]$Text) {
    [byte[]]($Text.Trim() -split '(..)' -ne '' | ForEach-Object { [Convert]::ToByte($_, 16) })
}

# Ed25519 signature check (RFC 8032), a port of verify() in src/signing.py, so the standalone install needs no
# Python; tests/test_updater.py checks it against the Python signer
Add-Type -AssemblyName System.Numerics
function Test-Ed25519Signature([byte[]]$PublicKey, [string]$File, [byte[]]$Signature) {
    # PowerShell names are case-insensitive: constants get long names so locals like $p or $d cannot shadow them
    $BigInt = [System.Numerics.BigInteger]
    $Prime = $BigInt::Pow(2, 255) - 19
    $Order = $BigInt::Pow(2, 252) + $BigInt::Parse("27742317777372353535851937790883648493")
    function EdMod($x) { $r = $x % $Prime; if ($r.Sign -lt 0) { $r + $Prime } else { $r } }
    function EdInv($x) { $BigInt::ModPow($x, $Prime - 2, $Prime) }
    function EdInt([byte[]]$le) { New-Object System.Numerics.BigInteger -ArgumentList (, [byte[]]($le + [byte]0)) }  # little-endian, kept positive
    $Curve = EdMod ((EdInv 121666) * -121665)  # BigInteger on the left: an int there would mean double math
    $SqrtM1 = $BigInt::ModPow(2, ($Prime - 1) / 4, $Prime)
    function EdRecoverX($y, $sign) {
        if ($y -ge $Prime) { return $null }
        $x2 = EdMod (($y * $y - 1) * (EdInv ($Curve * $y * $y + 1)))
        if ($x2.IsZero) { if ($sign) { return $null } else { return $BigInt::Zero } }
        $x = $BigInt::ModPow($x2, ($Prime + 3) / 8, $Prime)
        if (-not (EdMod ($x * $x - $x2)).IsZero) { $x = EdMod ($x * $SqrtM1) }
        if (-not (EdMod ($x * $x - $x2)).IsZero) { return $null }
        if ([int](-not $x.IsEven) -ne $sign) { $Prime - $x } else { $x }
    }
    function EdDecompress([byte[]]$b) {
        $y = EdInt $b
        $top = $BigInt::Pow(2, 255)
        $sign = [int]($y -ge $top)
        $y = $y % $top
        $x = EdRecoverX $y $sign
        if ($null -eq $x) { $null } else { , @($x, $y, $BigInt::One, (EdMod ($x * $y))) }
    }
    function EdAdd($pt, $q) {
        $a = EdMod (($pt[1] - $pt[0]) * ($q[1] - $q[0])); $b = EdMod (($pt[1] + $pt[0]) * ($q[1] + $q[0]))
        $c = EdMod ($pt[3] * $q[3] * $Curve * 2); $d = EdMod ($pt[2] * $q[2] * 2)
        $e = $b - $a; $f = $d - $c; $g = $d + $c; $h = $b + $a
        , @((EdMod ($e * $f)), (EdMod ($g * $h)), (EdMod ($f * $g)), (EdMod ($e * $h)))
    }
    function EdMul($s, $pt) {
        $q = @($BigInt::Zero, $BigInt::One, $BigInt::One, $BigInt::Zero)
        while ($s.Sign -gt 0) {
            if (-not $s.IsEven) { $q = EdAdd $q $pt }
            $pt = EdAdd $pt $pt
            $s = $s / 2
        }
        , $q
    }
    if ($PublicKey.Length -ne 32 -or $Signature.Length -ne 64) { return $false }
    $gy = EdMod ((EdInv 5) * 4)
    $G = @((EdRecoverX $gy 0), $gy, $BigInt::One, $BigInt::Zero); $G[3] = EdMod ($G[0] * $G[1])
    $A = EdDecompress $PublicKey; $R = EdDecompress $Signature[0..31]
    if ($null -eq $A -or $null -eq $R) { return $false }
    $s = EdInt $Signature[32..63]
    if ($s -ge $Order) { return $false }
    $sha = [System.Security.Cryptography.SHA512]::Create()
    [void]$sha.TransformBlock([byte[]]$Signature[0..31], 0, 32, $null, 0)
    [void]$sha.TransformBlock($PublicKey, 0, 32, $null, 0)
    $stream = [System.IO.File]::OpenRead($File)
    try { $buf = New-Object byte[] 1048576; while (($got = $stream.Read($buf, 0, $buf.Length)) -gt 0) { [void]$sha.TransformBlock($buf, 0, $got, $null, 0) } }
    finally { $stream.Dispose() }
    [void]$sha.TransformFinalBlock([byte[]]@(), 0, 0)
    $h = (EdInt $sha.Hash) % $Order
    $left = EdMul $s $G; $right = EdAdd $R (EdMul $h $A)
    (EdMod ($left[0] * $right[2] - $right[0] * $left[2])).IsZero -and (EdMod ($left[1] * $right[2] - $right[1] * $left[2])).IsZero
}

# Close running copies of this install (graceful first, so state.json is saved): their files are in use
function Stop-Paperduck {
    $procs = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='Paperduck.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase) }
    Stop-Tree $procs
}

# Graceful first; /T because a venv's pythonw.exe is a launcher whose child runs the pet
function Stop-Tree($procs) {
    foreach ($p in $procs) { taskkill /T /PID $p.ProcessId 2>$null | Out-Null }
    foreach ($p in $procs) { Wait-Process -Id $p.ProcessId -Timeout 10 -ErrorAction SilentlyContinue }
    foreach ($p in $procs) { taskkill /T /F /PID $p.ProcessId 2>$null | Out-Null }
}

# ByteSprite (every version up to 2.0.2) -> Paperduck. Returns @(had Start with Windows, had a Desktop shortcut).
function Move-ByteSprite([string]$Reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion",
                         [string]$Programs = [Environment]::GetFolderPath("Programs"),
                         [string]$Desktop = [Environment]::GetFolderPath("Desktop")) {
    $oldRoot = Join-Path $env:LOCALAPPDATA "ByteSprite"
    $oldHome = Join-Path $env:USERPROFILE ".bytesprite"
    $links = @((Join-Path $Programs "ByteSprite.lnk"), (Join-Path $Desktop "ByteSprite.lnk"))
    $hadRun = [bool](Get-ItemProperty "$Reg\Run" -Name ByteSprite -ErrorAction SilentlyContinue)
    $hadDesktop = Test-Path $links[1]
    if (-not ((Test-Path $oldRoot) -or (Test-Path $oldHome) -or $hadRun -or $hadDesktop)) { return , @($false, $false) }
    Write-Host "Moving ByteSprite's settings, chats and skills to Paperduck..."
    Stop-Tree (Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe' OR Name='ByteSprite.exe'" |
        Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($oldRoot, [StringComparison]::OrdinalIgnoreCase) })
    # /XC /XN /XO: copy only files Paperduck does not have yet, never overwrite
    if (Test-Path $oldHome) {
        robocopy $oldHome (Join-Path $env:USERPROFILE ".paperduck") /E /XC /XN /XO /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "Copying $oldHome failed (robocopy $LASTEXITCODE); nothing was removed" }
    }
    $program = @("app", "app.old", "app.new", "venv") | ForEach-Object { Join-Path $oldRoot $_ }
    if (Test-Path $oldRoot) {
        robocopy $oldRoot $Root /E /XC /XN /XO /XD @program /XF *.log *.log.1 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "Copying $oldRoot failed (robocopy $LASTEXITCODE); nothing was removed" }
    }
    Remove-ItemProperty "$Reg\Run" -Name ByteSprite -ErrorAction SilentlyContinue
    Remove-Item "$Reg\Uninstall\ByteSprite" -Recurse -Force -ErrorAction SilentlyContinue
    foreach ($old in $links + $program) { Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue }
    foreach ($old in $program) { if (Test-Path $old) { Write-Warning "Could not remove $old (still in use?); delete it by hand" } }
    # Renamed, so a later install does not copy them again (and bring back what was deleted in Paperduck)
    foreach ($old in $oldHome, $oldRoot) {
        if (Test-Path $old) {
            Rename-Item $old "$(Split-Path $old -Leaf)-backup" -ErrorAction SilentlyContinue
            if (Test-Path $old) { Write-Warning "Could not rename $old; it is copied already, delete it by hand" }
            else { Write-Host "Old data kept as a backup in $old-backup (delete it once Paperduck looks right)" }
        }
    }
    , @($hadRun, $hadDesktop)
}

# 1. Source: this folder when run from a clone, else the latest GitHub release (its standalone build if it has one)
$Source = $PSScriptRoot
$Temp = $null
$Standalone = $false
# Any failure below still removes the download folder; break re-throws the error
trap { if ($Temp) { Remove-Item $Temp -Recurse -Force -ErrorAction SilentlyContinue }; break }
if (-not $Source -or -not (Test-Path (Join-Path $Source "src\main.py"))) {
    Write-Host "Downloading the latest Paperduck release..."
    $release = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases/latest"
    $Temp = Join-Path $env:TEMP ("paperduck_" + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $Temp | Out-Null
    $Standalone = [bool]($release.assets | Where-Object name -eq $StandaloneAsset)
    $assetName = if ($Standalone) { $StandaloneAsset } else { "paperduck.zip" }
    $zip = Join-Path $Temp $assetName
    $sig = "$zip.sig"
    foreach ($pair in @(@($assetName, $zip), @("$assetName.sig", $sig))) {
        $asset = $release.assets | Where-Object name -eq $pair[0]
        if (-not $asset) { throw "Release $($release.tag_name) has no $($pair[0]); refusing an unsigned install" }
        Invoke-WebRequest $asset.browser_download_url -OutFile $pair[1] -UseBasicParsing
    }
    if ($Standalone) {
        Write-Host "Checking the release signature..."
        if (-not (Test-Ed25519Signature (ConvertFrom-Hex $ReleasePublicKey) $zip (ConvertFrom-Hex (Get-Content $sig -Raw)))) {
            throw "Release signature check failed; nothing was installed"
        }
        Expand-Archive $zip -DestinationPath $Temp
        $Source = Join-Path $Temp "Paperduck"
        $versionFile = Join-Path $Source "VERSION"
        # An older signed zip re-attached to a new tag must not install (same rule as the updater)
        if (-not (Test-Path $versionFile) -or (Get-Content $versionFile -Raw).Trim() -ne $release.tag_name.TrimStart("v")) {
            throw "Release $($release.tag_name) contains a different version; nothing was installed"
        }
        if (-not (Test-Path (Join-Path $Source "Paperduck.exe"))) { throw "Release $($release.tag_name) has no Paperduck.exe" }
    } else {
        $py = Find-Python
        $verifier = Join-Path $Temp "signing.py"
        # The verifier comes from the repo, pinned by hash so it is exactly as trusted as this script
        Invoke-WebRequest "https://raw.githubusercontent.com/$Repo/main/src/signing.py" -OutFile $verifier -UseBasicParsing
        if ((Get-FileHash $verifier -Algorithm SHA256).Hash -ne $SigningPySha256) { throw "signing.py does not match the pinned hash" }
        $check = "import importlib.util as u,sys;s=u.spec_from_file_location('signing',sys.argv[1]);m=u.module_from_spec(s);s.loader.exec_module(m);" +
                 "sys.exit(0 if m.verify(bytes.fromhex(sys.argv[4]),open(sys.argv[2],'rb').read(),bytes.fromhex(open(sys.argv[3]).read().strip())) else 1)"
        $pyArgs = @($py | Select-Object -Skip 1)
        & $py[0] @pyArgs -I -c $check $verifier $zip $sig $ReleasePublicKey
        if ($LASTEXITCODE -ne 0) { throw "Release signature check failed; nothing was installed" }
        Expand-Archive $zip -DestinationPath $Temp
        $Source = Join-Path $Temp "paperduck"
        $init = Get-Content (Join-Path $Source "src\__init__.py") -Raw
        if ($init -notmatch '__version__\s*=\s*["'']([^"'']+)' -or $Matches[1] -ne $release.tag_name.TrimStart("v")) {
            throw "Release $($release.tag_name) contains a different version; nothing was installed"
        }
    }
}

$moved = Move-ByteSprite
if ($moved[0]) { $StartWithWindows = $true }
if ($moved[1]) { $DesktopShortcut = $true }

Write-Host "Installing to $Root"
if ($Standalone) {
    # 2. Replace the app folder with the standalone build (mirrored, so an older venv install's src goes too)
    Stop-Paperduck
    robocopy $Source $App /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying the app failed (robocopy $LASTEXITCODE)" }
    # Leftovers of an in-app update, and the Python environment a standalone install no longer uses
    foreach ($old in "$App.old", "$App.new", $Venv) { Remove-Item $old -Recurse -Force -ErrorAction SilentlyContinue }
    Set-Content (Join-Path $App ".installed") "installed by install.ps1" -Encoding ascii
    $Target = Join-Path $App "Paperduck.exe"
    $TargetArgs = ""
    $WorkDir = $Root  # outside app\, so an in-app update can rename it while Paperduck runs
    $Icon = $Target
    $RunValue = "`"$Target`""
    $version = (Get-Content (Join-Path $App "VERSION") -Raw).Trim()
} else {
    if (-not $py) { $py = Find-Python }
    # 2. Copy the app (src is mirrored so deleted modules do not linger)
    New-Item -ItemType Directory -Force -Path $App | Out-Null
    robocopy (Join-Path $Source "src") (Join-Path $App "src") /MIR /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying src failed (robocopy $LASTEXITCODE)" }
    robocopy (Join-Path $Source "assets") (Join-Path $App "assets") /MIR /NFL /NDL /NJH /NJS /NP | Out-Null
    foreach ($f in "requirements.txt", "uninstall.ps1", "README.md", "paperduck.pyw") {
        $p = Join-Path $Source $f
        if (Test-Path $p) { Copy-Item $p $App -Force }  # signed bundles omit the legacy paperduck.pyw
    }
    Set-Content (Join-Path $App ".installed") "installed by install.ps1" -Encoding ascii

    # 3. Private venv + dependencies
    if (-not (Test-Path (Join-Path $Venv "Scripts\pythonw.exe"))) {
        Write-Host "Creating Python environment..."
        $pyArgs = @($py | Select-Object -Skip 1)
        & $py[0] @pyArgs -m venv $Venv
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    }
    $VenvPy = Join-Path $Venv "Scripts\python.exe"
    $Pythonw = Join-Path $Venv "Scripts\pythonw.exe"
    # No --require-hashes: pip checks hashes whenever the file has them, and older signed releases have none
    & $VenvPy -m pip install --disable-pip-version-check -q -r (Join-Path $App "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
    $Target = $Pythonw
    $TargetArgs = "-m src.main"
    $WorkDir = $App
    $Icon = Join-Path $App "assets\paperduck.ico"
    $RunValue = "`"$Pythonw`" `"$App\src\autostart.pyw`""
    $version = (& $VenvPy -c "import sys;sys.path.insert(0,r'$App');import src;print(src.__version__)").Trim()
}

# 4. Shortcuts (an existing Desktop one into this install is refreshed, since its target may have moved)
$shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$Path) {
    $lnk = $shell.CreateShortcut($Path)
    $lnk.TargetPath = $Target
    $lnk.Arguments = $TargetArgs
    $lnk.WorkingDirectory = $WorkDir
    $lnk.IconLocation = $Icon
    $lnk.Description = "Paperduck desktop companion"
    $lnk.Save()
}
function Test-OurShortcut([string]$Path) {
    (Test-Path $Path) -and $shell.CreateShortcut($Path).TargetPath.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)
}
$programs = [Environment]::GetFolderPath("Programs")
foreach ($dir in $programs, [Environment]::GetFolderPath("Desktop")) {
    $lnk = Join-Path $dir "Paperduck.lnk"
    if ($dir -eq $programs -or $DesktopShortcut -or (Test-OurShortcut $lnk)) { New-Shortcut $lnk }
}

# 5. Apps & features entry (HKCU, so no admin)
$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Paperduck"
New-Item -Path $key -Force | Out-Null
$uninstall = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$App\uninstall.ps1`""
@{
    DisplayName = "Paperduck"; DisplayVersion = $version; Publisher = "Paperduck"
    DisplayIcon = $Icon; InstallLocation = $Root
    UninstallString = $uninstall; URLInfoAbout = "https://github.com/$Repo"
}.GetEnumerator() | ForEach-Object { Set-ItemProperty -Path $key -Name $_.Key -Value $_.Value }
Set-ItemProperty -Path $key -Name NoModify -Value 1 -Type DWord
Set-ItemProperty -Path $key -Name NoRepair -Value 1 -Type DWord

# 6. Start with Windows (same Run value the in-app toggle writes); an existing one for this install is refreshed
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$current = (Get-ItemProperty $run -Name Paperduck -ErrorAction SilentlyContinue).Paperduck
if ($StartWithWindows -or ($current -and $current.Contains($Root))) {
    Set-ItemProperty -Path $run -Name Paperduck -Value $RunValue
}

if ($Temp) { Remove-Item $Temp -Recurse -Force -ErrorAction SilentlyContinue }
Write-Host "Paperduck $version installed. Start Menu: Paperduck"
if (-not $NoLaunch) {
    if ($TargetArgs) { Start-Process $Target -ArgumentList $TargetArgs -WorkingDirectory $WorkDir }
    else { Start-Process $Target -WorkingDirectory $WorkDir }
}
