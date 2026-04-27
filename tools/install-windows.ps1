#Requires -Version 5.1
<#
.SYNOPSIS
    One-command Windows bootstrap for AmiFUSE.
.DESCRIPTION
    Installs Python, WinFSP, creates a venv, installs AmiFUSE and dependencies,
    then runs amifuse doctor --fix.  Idempotent -- safe to run multiple times.
#>

$ErrorActionPreference = "Stop"

function Write-Banner {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  AmiFUSE Windows Installer" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
}

function Write-Step($msg) {
    Write-Host "[*] $msg" -ForegroundColor Yellow
}

function Write-Ok($msg) {
    Write-Host "[+] $msg" -ForegroundColor Green
}

function Write-Err($msg) {
    Write-Host "[!] $msg" -ForegroundColor Red
}

# ---------------------------------------------------------------------------
# 1. Banner
# ---------------------------------------------------------------------------
Write-Banner

# ---------------------------------------------------------------------------
# 2. Detect Python 3.9+
# ---------------------------------------------------------------------------
Write-Step "Detecting Python..."

$python = $null
foreach ($candidate in @("py -3", "python3", "python")) {
    try {
        $tokens = $candidate -split " "
        $ver = & $tokens[0] $tokens[1..($tokens.Length-1)] --version 2>&1
        if ($ver -match "Python\s+(\d+)\.(\d+)") {
            $major = [int]$Matches[1]
            $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 9) {
                $python = $tokens
                Write-Ok "Found $ver ($candidate)"
                break
            }
        }
    } catch { }
}

if (-not $python) {
    Write-Err "Python 3.9+ not found."
    Write-Host ""
    Write-Host "Install Python with:" -ForegroundColor White
    Write-Host "  winget install Python.Python.3.12" -ForegroundColor White
    Write-Host ""
    Write-Host "Then re-run this script." -ForegroundColor White
    exit 1
}

# ---------------------------------------------------------------------------
# 3. Detect WinFSP
# ---------------------------------------------------------------------------
Write-Step "Detecting WinFSP..."

$winfspDir = $null
$winfspRegPaths = @(
    "HKLM:\SOFTWARE\WinFsp",
    "HKLM:\SOFTWARE\WOW6432Node\WinFsp"
)

function Find-WinFsp {
    foreach ($regPath in $winfspRegPaths) {
        try {
            $dir = (Get-ItemProperty $regPath -Name InstallDir -ErrorAction Stop).InstallDir
            if ($dir -and (Test-Path $dir)) { return $dir }
        } catch { }
    }
    # Filesystem fallback
    foreach ($candidate in @(
        "${env:ProgramFiles}\WinFsp",
        "${env:ProgramFiles(x86)}\WinFsp"
    )) {
        if (Test-Path (Join-Path $candidate "bin\winfsp-x64.dll")) { return $candidate }
    }
    return $null
}

$winfspDir = Find-WinFsp

if ($winfspDir) {
    Write-Ok "WinFSP found at $winfspDir"
} else {
    Write-Err "WinFSP not found."
    Write-Host ""
    Write-Host "WinFSP requires elevated (admin) installation." -ForegroundColor White
    $reply = Read-Host "Install WinFSP via winget now? (y/n)"
    if ($reply -eq "y") {
        Write-Step "Installing WinFSP (may prompt for elevation)..."
        winget install WinFsp.WinFsp --accept-source-agreements --accept-package-agreements
        # Verify
        $winfspDir = Find-WinFsp
        if ($winfspDir) {
            Write-Ok "WinFSP installed at $winfspDir"
        } else {
            Write-Err "WinFSP install could not be verified. You may need to restart this script."
            exit 1
        }
    } else {
        Write-Err "WinFSP is required. Install it manually from https://winfsp.dev/ and re-run."
        exit 1
    }
}

# ---------------------------------------------------------------------------
# 4. Detect / create venv
# ---------------------------------------------------------------------------
Write-Step "Setting up virtual environment..."

$activateScript = $null

if ($env:VIRTUAL_ENV -and -not (Test-Path "$env:VIRTUAL_ENV\Scripts\Activate.ps1")) {
    Write-Host "[!] VIRTUAL_ENV points to a broken venv. Clearing and continuing..."
    $env:VIRTUAL_ENV = $null
}

if ($env:VIRTUAL_ENV) {
    $venvPath = $env:VIRTUAL_ENV
    $activateScript = Join-Path $venvPath "Scripts\Activate.ps1"
    Write-Ok "Detected active venv at $venvPath"
} else {
    $venvPath = Join-Path $env:LOCALAPPDATA "amifuse\venv"
    $activateScript = Join-Path $venvPath "Scripts\Activate.ps1"

    if (Test-Path $venvPath) {
        if (Test-Path $activateScript) {
            Write-Ok "Existing venv found at $venvPath"
        } else {
            Write-Step "Venv at $venvPath is broken (missing Activate.ps1). Removing..."
            Remove-Item -Recurse -Force $venvPath
            Write-Ok "Removed broken venv."
        }
    }

    if (-not (Test-Path $venvPath)) {
        Write-Step "Creating venv at $venvPath..."
        & $python[0] $python[1..($python.Length-1)] -m venv $venvPath
        if (-not (Test-Path $activateScript)) {
            Write-Err "Venv creation failed -- $activateScript not found after creation."
            exit 1
        }
        Write-Ok "Venv created."
    }
}

# Always activate -- even if $env:VIRTUAL_ENV was set, this process needs
# the venv's Scripts on PATH and its python as the default interpreter.
. $activateScript

if (-not $env:VIRTUAL_ENV) {
    Write-Err "Venv activation failed -- VIRTUAL_ENV not set after sourcing Activate.ps1."
    Write-Err "Try deleting $venvPath and re-running."
    exit 1
}
Write-Ok "Venv activated at $venvPath"

# ---------------------------------------------------------------------------
# 5. Install AmiFUSE
# ---------------------------------------------------------------------------
Write-Step "Installing AmiFUSE..."

$devMode = $false
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..") -ErrorAction SilentlyContinue).Path
$pyprojectPath = if ($repoRoot) { Join-Path $repoRoot "pyproject.toml" } else { $null }
if ($pyprojectPath -and (Test-Path $pyprojectPath)) {
    $content = Get-Content $pyprojectPath -Raw
    if ($content -match 'name\s*=\s*"amifuse"') {
        $devMode = $true
    }
}

if ($devMode) {
    Write-Step "Dev checkout detected -- installing in editable mode with [windows] extras..."
    python -m pip install -e "$repoRoot[windows]"
    Write-Ok "Editable install complete (dependencies including machine68k-amifuse pulled from pyproject.toml)."
} else {
    Write-Step "Installing from PyPI..."
    python -m pip install amifuse
    python -m pip install pystray Pillow
    # machine68k-amifuse only needed separately for PyPI installs
    Write-Step "Installing machine68k-amifuse (Windows-compatible fork)..."
    python -m pip install machine68k-amifuse
}

# ---------------------------------------------------------------------------
# 6. Run doctor --fix
# ---------------------------------------------------------------------------
Write-Step "Running amifuse doctor --fix..."
$ErrorActionPreference = "Continue"
python -m amifuse doctor --fix 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Err "amifuse doctor --fix exited with code $LASTEXITCODE."
    Write-Host "This may happen if your installed version doesn't support --fix yet." -ForegroundColor White
    Write-Host "You can run 'amifuse doctor' or 'amifuse doctor --fix' manually later." -ForegroundColor White
}
$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------------------
# 7. Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Installation Complete" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Python:   $(python --version)" -ForegroundColor White
Write-Host "  Venv:     $venvPath" -ForegroundColor White
Write-Host "  WinFSP:   $winfspDir" -ForegroundColor White
if ($devMode) {
    Write-Host "  Mode:     editable (dev)" -ForegroundColor White
} else {
    Write-Host "  Mode:     PyPI release" -ForegroundColor White
}
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  amifuse mount <image> <drive-letter>   Mount an Amiga disk image" -ForegroundColor White
Write-Host "  amifuse doctor                         Check system health" -ForegroundColor White
Write-Host "  amifuse-tray                           Start the system tray app" -ForegroundColor White
Write-Host ""
