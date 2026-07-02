#Requires -Version 5.1
<#
.SYNOPSIS
    One-command Windows bootstrap for AmiFUSE.
.DESCRIPTION
    Runs UNELEVATED as the current (standard) user. Obtains a compatible Python
    (3.9-3.13, provisioning per-user if needed), ensures WinFSP is present
    (elevating ONLY that one machine-wide step), creates a per-user venv,
    installs AmiFUSE and its dependencies, then runs amifuse doctor --fix and
    registers the Explorer shell integration in the user's own HKCU hive.
    Idempotent -- safe to run multiple times.

    Elevation model = SPLIT. Everything user-scoped (Python, venv, pip, HKCU
    registration) stays unelevated so it lands in the double-clicking user's
    profile and hive. Only the WinFSP kernel-driver install elevates.
.PARAMETER Uninstall
    Remove AmiFUSE venv, registry keys, and shell extensions.
#>

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# G0 -- Ensure this process can run scripts even under a Restricted policy.
if ((Get-ExecutionPolicy -Scope Process) -eq 'Restricted') {
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
}

# ---------------------------------------------------------------------------
# Constants (target Python + provisioning pins)
# ---------------------------------------------------------------------------
$TargetPyVersion = "3.13.14"
$PyInstallerUrl  = "https://www.python.org/ftp/python/3.13.14/python-3.13.14-amd64.exe"
$PyInstallerSha  = "c54d9b9bbb8a36e6489363ddd01139707fd781d72f1f9e90c7ec65d0061368e0"
$PerUserPy313    = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"

# WinFSP: pinned MSI (account-agnostic install via msiexec; see G2). Verified.
$WinFspVersion   = "2.1"
$WinFspMsiUrl    = "https://github.com/winfsp/winfsp/releases/download/v2.1/winfsp-2.1.25156.msi"
$WinFspMsiSha    = "073a70e00f77423e34bed98b86e600def93393ba5822204fac57a29324db9f7a"

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

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

function Test-IsAdmin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]$identity
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-HasWinget {
    return ($null -ne (Get-Command winget -ErrorAction SilentlyContinue))
}

# Run <exe> --version and return version info, or $null if it isn't a usable
# Python. Never throws.
function Get-PythonVersionInfo {
    param([string]$ExePath)
    if (-not $ExePath -or -not (Test-Path $ExePath)) { return $null }
    try {
        $out = & $ExePath --version 2>&1
    } catch {
        return $null
    }
    $text = ($out | Out-String)
    if ($text -match 'Python\s+(\d+)\.(\d+)\.(\d+)') {
        return [pscustomobject]@{
            Path    = $ExePath
            Major   = [int]$Matches[1]
            Minor   = [int]$Matches[2]
            Version = "$($Matches[1]).$($Matches[2]).$($Matches[3])"
        }
    }
    return $null
}

# The ceiling: accept ONLY 3.9 <= minor <= 13 (machine68k-amifuse ships
# cp39..cp313 win_amd64 wheels; 3.14 has no wheel and would fail late).
function Test-InRange {
    param($Info)
    if ($null -eq $Info) { return $false }
    return ($Info.Major -eq 3 -and $Info.Minor -ge 9 -and $Info.Minor -le 13)
}

# Enumerate candidate interpreters WITHOUT relying on a stale in-session PATH.
# Primary source is the py launcher (`py -0p`), which reads the live registry;
# bare python/python3 on PATH are added as a fallback for machines lacking the
# launcher. Microsoft Store alias stubs (WindowsApps) are skipped -- executing
# one when no real Python is installed pops the Store.
function Get-InstalledPythons {
    $paths = New-Object System.Collections.Generic.List[string]
    try {
        $lines = & py -0p 2>&1
        if ($LASTEXITCODE -eq 0) {
            foreach ($line in ($lines -split "`r?`n")) {
                if ($line -match '([A-Za-z]:\\[^\r\n]*?python\.exe)') {
                    $p = $Matches[1].Trim()
                    if ($p -notmatch '\\WindowsApps\\') { $paths.Add($p) }
                }
            }
        }
    } catch { }
    foreach ($name in @('python.exe', 'python3.exe')) {
        try {
            $found = & where.exe $name 2>$null
            foreach ($f in ($found -split "`r?`n")) {
                $f = $f.Trim()
                if ($f -and ($f -notmatch '\\WindowsApps\\') -and (Test-Path $f)) {
                    $paths.Add($f)
                }
            }
        } catch { }
    }
    return ($paths | Select-Object -Unique)
}

# From a set of interpreter paths, pick the highest one in 3.9-3.13.
function Select-BestPython {
    param([string[]]$Paths)
    $best = $null
    foreach ($p in $Paths) {
        $info = Get-PythonVersionInfo $p
        if (Test-InRange $info) {
            if ($null -eq $best -or $info.Minor -gt $best.Minor) { $best = $info }
        }
    }
    return $best
}

# After provisioning, locate the interpreter deterministically (B6): known
# per-user path first, then `py -3.13` (the running launcher sees a freshly
# registered 3.13 without a restart), then re-enumerate. Never re-probe bare
# `python` (PATH is stale in-session).
function Resolve-ProvisionedPython {
    $info = Get-PythonVersionInfo $PerUserPy313
    if (Test-InRange $info) { return $info }
    try {
        $out = & py -3.13 -c "import sys; print(sys.executable)" 2>&1
        if ($LASTEXITCODE -eq 0) {
            $line = (($out -split "`r?`n") | Where-Object { $_.Trim() } | Select-Object -Last 1)
            if ($line) {
                $info = Get-PythonVersionInfo ($line.Trim())
                if (Test-InRange $info) { return $info }
            }
        }
    } catch { }
    return (Select-BestPython (Get-InstalledPythons))
}

# Provision Python 3.13 per-user, UNELEVATED: winget first, then a verified
# python.org silent install. Returns version info or $null.
function Invoke-PythonProvision {
    if (Test-HasWinget) {
        Write-Step "Installing Python 3.13 via winget (per-user, no admin)..."
        winget install Python.Python.3.13 --scope user --silent --accept-source-agreements --accept-package-agreements 2>&1 | ForEach-Object { Write-Host $_ }
        $info = Resolve-ProvisionedPython
        if (Test-InRange $info) { return $info }
        Write-Err "winget did not yield a usable Python 3.13; falling back to direct download."
    }

    $dl = Join-Path $env:TEMP "python-$TargetPyVersion-amd64.exe"
    Write-Step "Downloading Python $TargetPyVersion (~27 MB) from python.org..."
    $prev = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    $downloaded = $false
    try {
        Invoke-WebRequest -Uri $PyInstallerUrl -OutFile $dl -UseBasicParsing
        $downloaded = $true
    } catch {
        Write-Err "Download failed: $($_.Exception.Message)"
    } finally {
        $ProgressPreference = $prev
    }
    if (-not $downloaded) { return $null }
    Write-Ok "Download complete."

    Write-Step "Verifying SHA256..."
    $hash = (Get-FileHash -Algorithm SHA256 -Path $dl).Hash
    if ($hash -ne $PyInstallerSha.ToUpper()) {
        Write-Err "SHA256 mismatch -- refusing to run the installer."
        Write-Err "  expected: $($PyInstallerSha.ToUpper())"
        Write-Err "  actual:   $hash"
        Remove-Item -Force $dl -ErrorAction SilentlyContinue
        return $null
    }
    Write-Ok "SHA256 verified."

    Write-Step "Installing Python $TargetPyVersion (per-user, no admin)..."
    $proc = Start-Process -FilePath $dl -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_pip=1'
    )
    Remove-Item -Force $dl -ErrorAction SilentlyContinue
    if ($proc.ExitCode -ne 0) {
        Write-Err "Python installer exited with code $($proc.ExitCode); will verify anyway..."
    }
    return (Resolve-ProvisionedPython)
}

# Detect an existing WinFSP install (registry InstallDir, then filesystem).
function Find-WinFsp {
    $regPaths = @(
        "HKLM:\SOFTWARE\WinFsp",
        "HKLM:\SOFTWARE\WOW6432Node\WinFsp"
    )
    foreach ($regPath in $regPaths) {
        try {
            $dir = (Get-ItemProperty $regPath -Name InstallDir -ErrorAction Stop).InstallDir
            if ($dir -and (Test-Path $dir)) { return $dir }
        } catch { }
    }
    foreach ($candidate in @(
        "${env:ProgramFiles}\WinFsp",
        "${env:ProgramFiles(x86)}\WinFsp"
    )) {
        if (Test-Path (Join-Path $candidate "bin\winfsp-x64.dll")) { return $candidate }
    }
    return $null
}

# ---------------------------------------------------------------------------
# Uninstall mode
# ---------------------------------------------------------------------------
if ($Uninstall) {
    Write-Banner
    Write-Host "  Uninstalling AmiFUSE..." -ForegroundColor Yellow
    Write-Host ""

    $removed = @()

    # 1. Remove venv (run unregister first while amifuse is still available)
    $venvPath = Join-Path $env:LOCALAPPDATA "amifuse\venv"
    if (Test-Path $venvPath) {
        $venvPython = Join-Path $venvPath "Scripts\python.exe"
        if (Test-Path $venvPython) {
            Write-Step "Unregistering shell extensions..."
            try {
                & $venvPython -m amifuse unregister 2>&1 | Out-Null
                $removed += "Shell extensions (unregistered)"
            } catch { }
        }
        Write-Step "Removing virtual environment..."
        Remove-Item -Recurse -Force $venvPath
        $removed += "Venv: $venvPath"
    }

    # 2. Remove registry keys (ProgID and file associations)
    $regKeys = @(
        "HKCU:\Software\Classes\AmiFUSE.DiskImage",
        "HKCU:\Software\Classes\AmiFUSE.DiskImage.HDF",
        "HKCU:\Software\Classes\AmiFUSE.DiskImage.ADF",
        "HKCU:\Software\Classes\.hdf\OpenWithProgids",
        "HKCU:\Software\Classes\.adf\OpenWithProgids"
    )
    foreach ($key in $regKeys) {
        if (Test-Path $key) {
            # For OpenWithProgids, only remove our entries, not the whole key
            if ($key -match 'OpenWithProgids$') {
                try {
                    Remove-ItemProperty -Path $key -Name "AmiFUSE.DiskImage.HDF" -ErrorAction SilentlyContinue
                    Remove-ItemProperty -Path $key -Name "AmiFUSE.DiskImage.ADF" -ErrorAction SilentlyContinue
                    Remove-ItemProperty -Path $key -Name "AmiFUSE.DiskImage" -ErrorAction SilentlyContinue
                    $removed += "Registry: $key (AmiFUSE entries)"
                } catch { }
            } else {
                Remove-Item -Recurse -Force $key -ErrorAction SilentlyContinue
                $removed += "Registry: $key"
            }
        }
    }

    # 3. Remove amifuse app data directory (if empty after venv removal)
    $appDir = Join-Path $env:LOCALAPPDATA "amifuse"
    if ((Test-Path $appDir) -and ((Get-ChildItem $appDir -Force | Measure-Object).Count -eq 0)) {
        Remove-Item -Force $appDir
        $removed += "App directory: $appDir"
    }

    # 4. Summary
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  Uninstall Complete" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    if ($removed.Count -gt 0) {
        Write-Host "  Removed:" -ForegroundColor White
        foreach ($item in $removed) {
            Write-Host "    - $item" -ForegroundColor White
        }
    } else {
        Write-Host "  Nothing to remove (AmiFUSE was not installed)." -ForegroundColor White
    }
    Write-Host ""
    Write-Host "  Note: WinFSP was NOT removed (shared dependency)." -ForegroundColor Yellow
    Write-Host "  To remove WinFSP: use Windows 'Apps & features' (search WinFSP)." -ForegroundColor White
    Write-Host ""
    exit 0
}

# ---------------------------------------------------------------------------
# Banner  (installer runs UNELEVATED -- no admin gate; see split-elevation note)
# ---------------------------------------------------------------------------
Write-Banner

# ---------------------------------------------------------------------------
# G1 -- Compatible Python (3.9-3.13): reuse, else provision per-user unelevated
# ---------------------------------------------------------------------------
Write-Step "Detecting a compatible Python (3.9-3.13)..."

$pythonInfo = Select-BestPython (Get-InstalledPythons)
if (Test-InRange $pythonInfo) {
    Write-Ok "Found compatible Python $($pythonInfo.Version) at $($pythonInfo.Path)"
} else {
    Write-Step "No compatible Python (3.9-3.13) present; provisioning Python $TargetPyVersion..."
    $pythonInfo = Invoke-PythonProvision
    if (-not (Test-InRange $pythonInfo)) {
        Write-Err "Could not obtain a compatible Python (3.9-3.13)."
        Write-Host "  Install Python 3.13 from python.org and re-run." -ForegroundColor White
        exit 1
    }
    Write-Ok "Provisioned Python $($pythonInfo.Version) at $($pythonInfo.Path)"
}
$pythonExe = $pythonInfo.Path

# ---------------------------------------------------------------------------
# G2 -- WinFSP (machine-wide kernel driver): the ONLY elevated step
# ---------------------------------------------------------------------------
Write-Step "Detecting WinFSP..."
$winfspDir = Find-WinFsp

if ($winfspDir) {
    Write-Ok "WinFSP found at $winfspDir"
} else {
    Write-Step "WinFSP not found. It is a machine-wide driver and needs a one-time elevation."

    # Download the MSI UNELEVATED, as the standard user, into that user's TEMP.
    # This is the account-agnostic path: the only elevated action is msiexec.exe
    # (a System32 binary present for EVERY account, with no winget/MSIX alias or
    # per-profile dependency). Under over-the-shoulder UAC the elevated admin
    # token can still READ the standard user's TEMP file. We verify SHA256 before
    # handing the MSI to the elevated installer.
    $msi = Join-Path $env:TEMP "winfsp-$WinFspVersion.msi"
    Write-Step "Downloading WinFSP $WinFspVersion (~2 MB)..."
    $prev = $ProgressPreference
    $ProgressPreference = 'SilentlyContinue'
    $downloaded = $false
    try {
        Invoke-WebRequest -Uri $WinFspMsiUrl -OutFile $msi -UseBasicParsing
        $downloaded = $true
    } catch {
        Write-Err "WinFSP download failed: $($_.Exception.Message)"
    } finally {
        $ProgressPreference = $prev
    }
    if (-not $downloaded) {
        Write-Err "Could not download the WinFSP installer."
        Write-Err "Install WinFSP manually from https://winfsp.dev/ and re-run."
        exit 1
    }
    Write-Ok "Download complete."

    Write-Step "Verifying SHA256..."
    $hash = (Get-FileHash -Algorithm SHA256 -Path $msi).Hash
    if ($hash -ne $WinFspMsiSha.ToUpper()) {
        Write-Err "SHA256 mismatch -- refusing to install WinFSP."
        Write-Err "  expected: $($WinFspMsiSha.ToUpper())"
        Write-Err "  actual:   $hash"
        Remove-Item -Force $msi -ErrorAction SilentlyContinue
        exit 1
    }
    Write-Ok "SHA256 verified."

    Write-Step "Installing WinFSP (requires administrator)..."
    $msiArgs = "/i `"$msi`" /qn /norestart"
    try {
        if (Test-IsAdmin) {
            # Already elevated (rare for this flow) -- run msiexec directly, no 2nd UAC.
            $p = Start-Process -FilePath "msiexec.exe" -Wait -PassThru -ArgumentList $msiArgs
        } else {
            # Normal case: elevate JUST msiexec (a UAC prompt will appear).
            $p = Start-Process -FilePath "msiexec.exe" -Verb RunAs -Wait -PassThru -ArgumentList $msiArgs
        }
        if ($p.ExitCode -ne 0) {
            Write-Err "WinFSP installer (msiexec) reported exit code $($p.ExitCode); will verify..."
        }
    } catch {
        Write-Err "Elevation for the WinFSP install was cancelled or failed."
        Write-Err "WinFSP is required. Re-run and approve the prompt, or install from https://winfsp.dev/."
        Remove-Item -Force $msi -ErrorAction SilentlyContinue
        exit 1
    }
    Remove-Item -Force $msi -ErrorAction SilentlyContinue

    $winfspDir = Find-WinFsp
    if ($winfspDir) {
        Write-Ok "WinFSP installed at $winfspDir"
    } else {
        Write-Err "WinFSP install could not be verified -- restart and re-run, or install from https://winfsp.dev/."
        exit 1
    }
}

# ---------------------------------------------------------------------------
# G3 -- venv (per-user; lands in the standard user's %LOCALAPPDATA%)
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
        $venvPython = Join-Path $venvPath "Scripts\python.exe"
        if (-not (Test-Path $activateScript) -or -not (Test-Path $venvPython)) {
            Write-Step "Venv at $venvPath is broken (missing core files). Removing..."
            Remove-Item -Recurse -Force $venvPath
            Write-Ok "Removed broken venv."
        } else {
            Write-Ok "Existing venv found at $venvPath"
        }
    }

    if (-not (Test-Path $venvPath)) {
        Write-Step "Creating venv at $venvPath..."
        & $pythonExe -m venv $venvPath
        if (-not (Test-Path $activateScript)) {
            Write-Err "Venv creation failed -- $activateScript not found after creation."
            Write-Host "  Delete $venvPath and re-run." -ForegroundColor White
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

# Persist the venv Scripts dir to the standard user's PATH ourselves (HKCU),
# independent of doctor. After dot-sourcing Activate.ps1 above, `amifuse` is on
# THIS process's PATH, so doctor's shutil.which("amifuse") returns non-None and
# it SKIPS its persistent HKCU\Environment\Path write -- leaving a fresh terminal
# with no `amifuse`. Writing it here (unelevated) lands in the standard user's
# own hive, consistent with the split. Idempotent.
$venvScripts = Join-Path $venvPath "Scripts"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (($userPath -split ';') -notcontains $venvScripts) {
    $userPath = if ($userPath) { "$userPath;$venvScripts" } else { $venvScripts }
    [Environment]::SetEnvironmentVariable("Path", $userPath, "User")
    Write-Ok "Added $venvScripts to your user PATH (open a new terminal to use 'amifuse')."
} else {
    Write-Ok "$venvScripts already on your user PATH."
}

# ---------------------------------------------------------------------------
# G4 -- pip bootstrap / upgrade in the venv
# ---------------------------------------------------------------------------
Write-Step "Bootstrapping pip in the venv..."
python -m ensurepip --upgrade 2>&1 | ForEach-Object { Write-Host $_ }
python -m pip install --upgrade pip 2>&1 | ForEach-Object { Write-Host $_ }

$pipVer = python -m pip --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Err "pip bootstrap failed in venv."
    exit 1
}
Write-Ok "pip ready: $pipVer"

# ---------------------------------------------------------------------------
# G5 -- Install AmiFUSE + dependencies
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
    # G1 guarantees the venv python is 3.9-3.13, so the machine68k-amifuse wheel
    # pulled transitively (via amitools-amifuse[vamos]) resolves without an sdist
    # build. The import check below is the fast/clear guard.
    #
    # setuptools_scm (upstream pyproject build config) derives the version from git
    # and lists tracked files via os.path.relpath(file, cwd). That crashes when the
    # repo is on a mapped network drive (git returns UNC paths, cwd is a drive letter
    # -- "path is on mount '\\host\share', start on mount 'U:'") and also when the
    # repo came from a GitHub ZIP (no .git -> "unable to determine version"). Pinning
    # SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AMIFUSE bypasses both the file-finder and
    # version derivation. The package-scoped form (suffix canonicalized to "amifuse")
    # targets ONLY our package -- the unscoped var would force this version onto any
    # other setuptools_scm-based package built from sdist in the same isolated build
    # env (e.g. transitive amitools-amifuse). Prefer the latest git tag (plain git
    # works on the network drive; only setuptools_scm's relpath fails), else a
    # harmless static fallback -- the version is cosmetic for an editable dev install.
    # Only call git if it exists: a ZIP-download user on a fresh box may not have it,
    # and with $ErrorActionPreference = "Stop" a bare git call would raise a
    # terminating CommandNotFoundException (2>$null does NOT suppress that).
    $scmVersion = $null
    if (Get-Command git -ErrorAction SilentlyContinue) {
        $scmVersion = (git -C $repoRoot describe --tags --abbrev=0 2>$null)
    }
    # Decide on the STRING being empty, NOT $LASTEXITCODE -- when git is skipped
    # $LASTEXITCODE holds a stale value from an earlier external command.
    if ([string]::IsNullOrWhiteSpace($scmVersion)) {
        $scmVersion = "0.0.0"
    } else {
        $scmVersion = $scmVersion.Trim() -replace '^v', ''
    }
    $prevScmVersion = $env:SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AMIFUSE
    try {
        $env:SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AMIFUSE = $scmVersion
        python -m pip install -e "$repoRoot[windows]" 2>&1 | ForEach-Object { Write-Host $_ }
        # Capture the pip exit code BEFORE finally runs -- any command in finally
        # (e.g. Remove-Item) could clobber $LASTEXITCODE before the check below.
        $pipExit = $LASTEXITCODE
    } finally {
        if ($null -eq $prevScmVersion) {
            Remove-Item Env:SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AMIFUSE -ErrorAction SilentlyContinue
        } else {
            $env:SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AMIFUSE = $prevScmVersion
        }
    }
    if ($pipExit -ne 0) {
        Write-Err "Editable install failed."
        exit 1
    }
    Write-Ok "Editable install complete."
} else {
    # Install machine68k-amifuse FIRST, --only-binary=:all:, so an out-of-range
    # Python fails FAST and CLEAR here (B8). If we installed `amifuse` first it
    # would pull machine68k-amifuse transitively (via amitools-amifuse[vamos])
    # WITHOUT --only-binary, hitting a cryptic sdist C-build failure before this
    # guard is ever reached. G1 should keep us in 3.9-3.13, so this normally just
    # installs a wheel.
    Write-Step "Installing machine68k-amifuse (wheel only)..."
    python -m pip install --only-binary=:all: machine68k-amifuse 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        $pv = (python --version 2>&1)
        Write-Err "No compatible machine68k-amifuse wheel for this Python -- need Python 3.9-3.13 (have $pv)."
        exit 1
    }
    Write-Step "Installing AmiFUSE from PyPI..."
    python -m pip install amifuse pystray Pillow 2>&1 | ForEach-Object { Write-Host $_ }
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to install amifuse/pystray/Pillow from PyPI."
        exit 1
    }
}

# Final G5 verification: machine68k must import (wheel installed correctly).
Write-Step "Verifying machine68k import..."
python -c "import machine68k" 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    $pv = (python --version 2>&1)
    Write-Err "No compatible machine68k-amifuse wheel for this Python -- need Python 3.9-3.13 (have $pv)."
    exit 1
}
$machine68kOk = $true
Write-Ok "machine68k import OK."

# ---------------------------------------------------------------------------
# G6 -- doctor --fix  /  G7 -- HKCU shell registration (both UNELEVATED)
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

# G7 -- verify Explorer integration landed in THIS user's hive (non-fatal).
$shellRegistered = Test-Path "HKCU:\Software\Classes\AmiFUSE.DiskImage"
if ($shellRegistered) {
    Write-Ok "Explorer integration registered for current user (HKCU)."
} else {
    Write-Err "Explorer integration not registered for current user (HKCU:\Software\Classes\AmiFUSE.DiskImage missing)."
    Write-Host "You can register it later with: amifuse register" -ForegroundColor White
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
if (Test-IsAdmin) { $modeText = "elevated (WinFSP-only elevation not needed)" } else { $modeText = "unelevated (user scope)" }

# $ready is the ground truth for the summary text: every core capability must
# actually be in place. WinFSP and machine68k are guaranteed by earlier exits,
# but shell registration (G6/G7) is non-fatal and may be missing -- in which case
# the summary must say "with warnings", not an unqualified "Complete".
$ready = ([bool]$winfspDir) -and ($machine68kOk -eq $true) -and $shellRegistered

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
if ($ready) {
    Write-Host "  Installation Complete" -ForegroundColor Cyan
} else {
    Write-Host "  Installation completed with warnings" -ForegroundColor Yellow
}
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Python:   $(python --version) (base: $pythonExe)" -ForegroundColor White
Write-Host "  Venv:     $venvPath" -ForegroundColor White
Write-Host "  WinFSP:   $winfspDir" -ForegroundColor White
Write-Host "  Mode:     $modeText" -ForegroundColor White
if ($devMode) {
    Write-Host "  Install:  editable (dev)" -ForegroundColor White
} else {
    Write-Host "  Install:  PyPI release" -ForegroundColor White
}
Write-Host ""
if (-not $ready) {
    Write-Host "Warnings (install is degraded):" -ForegroundColor Yellow
    if (-not $winfspDir)      { Write-Host "  - WinFSP not verified -- mounts will fail until WinFSP is installed." -ForegroundColor Yellow }
    if ($machine68kOk -ne $true) { Write-Host "  - machine68k did not import -- the m68k core is unavailable." -ForegroundColor Yellow }
    if (-not $shellRegistered) { Write-Host "  - Explorer integration not registered -- run 'amifuse register' to add it." -ForegroundColor Yellow }
    Write-Host ""
}

# Final read-only health check (no --fix). Informational only: the $ready banner
# above is the authoritative status. Non-fatal -- a non-zero exit (e.g. amifuse
# not importable) must not abort the install, so bracket with Continue/Stop.
Write-Host ""
Write-Host "Final health check (amifuse doctor):" -ForegroundColor Cyan
$ErrorActionPreference = "Continue"
python -m amifuse doctor 2>&1 | ForEach-Object { Write-Host $_ }
if ($LASTEXITCODE -ne 0) {
    Write-Host "  (doctor reported items above; see details.)" -ForegroundColor White
}
$ErrorActionPreference = "Stop"

Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  amifuse mount <image> <drive-letter>   Mount an Amiga disk image" -ForegroundColor White
Write-Host "  amifuse doctor                         Check system health" -ForegroundColor White
Write-Host "  amifuse-tray                           Start the system tray app" -ForegroundColor White
Write-Host ""
