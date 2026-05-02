"""
Diagnostic checks for amifuse environment readiness.

Provides structured check results with optional --fix mode and JSON output.
"""

import dataclasses
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional


@dataclasses.dataclass
class CheckResult:
    name: str
    status: str  # "ok", "warning", "error"
    message: str
    fixable: bool = False
    fix_fn: Optional[Callable] = None
    fix_description: Optional[str] = None


def run_checks() -> List[CheckResult]:
    """Run all diagnostic checks and return results."""
    results = []

    # 1. Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if sys.version_info >= (3, 9):
        results.append(CheckResult("python", "ok", f"Python {py_ver}"))
    else:
        results.append(CheckResult("python", "error", f"Python {py_ver} (requires 3.9+)"))

    # 2. amitools
    try:
        import amitools  # type: ignore
        ver = getattr(amitools, "__version__", None)
        msg = f"amitools {ver}" if ver else "amitools is installed"
        results.append(CheckResult("amitools", "ok", msg))
    except ImportError:
        results.append(CheckResult(
            "amitools", "error", "amitools is not installed",
            fixable=True, fix_description="pip install amitools-amifuse[vamos]",
        ))

    # 3. machine68k (subprocess isolation -- segfaults can kill the process)
    try:
        proc = subprocess.run(
            [sys.executable, "-c",
             "import machine68k; m = machine68k.Machine(0, 1024); del m"],
            capture_output=True, timeout=10,
        )
        rc = proc.returncode
        if rc == 0:
            results.append(CheckResult("machine68k", "ok", "machine68k is working"))
        elif rc in (-1073741819, -11):
            # Segfault on Windows (-1073741819 = 0xC0000005) or Unix (-11 = SIGSEGV)
            results.append(CheckResult(
                "machine68k", "warning",
                "machine68k segfaults -- install machine68k-amifuse fork",
                fixable=True, fix_description="pip install machine68k-amifuse",
            ))
        else:
            # rc == 1 or other non-zero: likely ImportError
            results.append(CheckResult(
                "machine68k", "error", "machine68k is not installed",
                fixable=True, fix_description="pip install machine68k",
            ))
    except subprocess.TimeoutExpired:
        results.append(CheckResult("machine68k", "warning", "machine68k check timed out"))
    except OSError:
        results.append(CheckResult("machine68k", "error", "Could not run machine68k check"))

    # 4. fusepy (no __version__ attribute -- don't say "unknown version")
    try:
        import fuse  # type: ignore
        ver = getattr(fuse, "__version__", None)
        msg = f"fusepy {ver}" if ver else "fusepy is installed"
        results.append(CheckResult("fusepy", "ok", msg))
    except ImportError:
        results.append(CheckResult(
            "fusepy", "error", "fusepy is not installed",
            fixable=True, fix_description="pip install fusepy",
        ))

    # 5. FUSE backend
    from . import platform as plat
    backend = plat.detect_fuse_backend()
    if backend["installed"]:
        ver_str = f" {backend['version']}" if backend.get("version") else ""
        results.append(CheckResult(
            "fuse_backend", "ok", f"{backend['name']}{ver_str} is installed",
        ))
    else:
        if sys.platform.startswith("win"):
            fix_desc = "Install WinFSP from https://winfsp.dev or: winget install WinFsp.WinFsp"
        elif sys.platform.startswith("darwin"):
            fix_desc = "Install macFUSE: brew install --cask macfuse"
        else:
            fix_desc = "Install FUSE: sudo apt install fuse3 libfuse-dev (or equivalent)"
        results.append(CheckResult(
            "fuse_backend", "error", f"{backend['name']} is not installed",
            fixable=False, fix_description=fix_desc,
        ))

    # 6. Driver availability
    from . import platform as plat
    driver_found = False
    driver_path = None
    for search_dir in plat.get_driver_search_dirs():
        candidate = search_dir / "FastFileSystem"
        if candidate.is_file():
            driver_found = True
            driver_path = candidate
            break

    if driver_found:
        results.append(CheckResult(
            "drivers", "ok", f"FastFileSystem found at {driver_path}",
        ))
    else:
        primary_dir = plat.get_primary_driver_dir()
        results.append(CheckResult(
            "drivers", "warning",
            "FastFileSystem not found",
            fixable=False,
            fix_description=f"Copy FastFileSystem to {primary_dir} for ADF floppy mounting",
        ))

    # 7. Shell registration (Windows only)
    if sys.platform.startswith("win"):
        try:
            from .windows_shell import is_registered, register
            if is_registered():
                results.append(CheckResult(
                    "shell_registration", "ok", "File associations are registered",
                ))
            else:
                results.append(CheckResult(
                    "shell_registration", "warning",
                    "File associations are not registered",
                    fixable=True, fix_fn=register,
                    fix_description="Run: amifuse register",
                ))
        except ImportError:
            pass  # windows_shell not available, skip check

    # 8. PATH check
    if shutil.which("amifuse"):
        results.append(CheckResult("path", "ok", "amifuse is on PATH"))
    else:
        # Use the Python executable's directory directly (not Scripts subdir)
        scripts_dir = str(Path(sys.executable).parent)
        if sys.platform.startswith("win"):
            def _fix_path_windows():
                import winreg
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, r"Environment", 0,
                    winreg.KEY_READ | winreg.KEY_WRITE,
                ) as key:
                    try:
                        current, _ = winreg.QueryValueEx(key, "Path")
                    except FileNotFoundError:
                        current = ""
                    if scripts_dir.lower() not in current.lower():
                        new_path = f"{current};{scripts_dir}" if current else scripts_dir
                        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
                        print(f"Added {scripts_dir} to user PATH (restart terminal to take effect)")

            results.append(CheckResult(
                "path", "warning", f"amifuse not found on PATH (expected in {scripts_dir})",
                fixable=True, fix_fn=_fix_path_windows,
                fix_description=f"Add {scripts_dir} to user PATH",
            ))
        else:
            # Unix: prefer zshrc on macOS, bashrc on Linux
            shell_rc = None
            if sys.platform.startswith("darwin"):
                for rc in (Path.home() / ".zshrc", Path.home() / ".bashrc"):
                    if rc.exists():
                        shell_rc = rc
                        break
                if shell_rc is None:
                    shell_rc = Path.home() / ".zshrc"
            else:
                for rc in (Path.home() / ".bashrc", Path.home() / ".zshrc"):
                    if rc.exists():
                        shell_rc = rc
                        break
                if shell_rc is None:
                    shell_rc = Path.home() / ".bashrc"

            def _fix_path_unix(rc_file=shell_rc, path_dir=scripts_dir):
                line = f'\nexport PATH="$PATH:{path_dir}"\n'
                with open(rc_file, "a") as f:
                    f.write(line)
                print(f"Appended PATH export to {rc_file} (restart terminal to take effect)")

            results.append(CheckResult(
                "path", "warning", f"amifuse not found on PATH (expected in {scripts_dir})",
                fixable=True, fix_fn=_fix_path_unix,
                fix_description=f"Add {scripts_dir} to {shell_rc}",
            ))

    return results


def cmd_doctor(args) -> None:
    """Main entry point for the doctor subcommand."""
    try:
        from importlib.metadata import version as _pkg_version
        __version__ = f"v{_pkg_version('amifuse')}"
    except Exception:
        __version__ = "v0.5.0"

    checks = run_checks()

    # Determine overall status
    statuses = [c.status for c in checks]
    if "error" in statuses:
        overall = "not_ready"
    elif "warning" in statuses:
        overall = "degraded"
    else:
        overall = "ready"

    # JSON output
    if getattr(args, "json", False):
        output = {
            "overall_status": overall,
            "platform": sys.platform,
            "version": __version__,
            "checks": [
                {
                    "name": c.name,
                    "status": c.status,
                    "message": c.message,
                    "fixable": c.fixable,
                    "fix_description": c.fix_description,
                }
                for c in checks
            ],
        }
        print(json.dumps(output, indent=2))
        if not getattr(args, "fix", False):
            sys.exit({"ready": 0, "not_ready": 1, "degraded": 2}[overall])
        return

    # Fix mode
    if getattr(args, "fix", False):
        if sys.platform.startswith("win"):
            symbols = {"ok": "+", "warning": "!", "error": "X", "fix": "*"}
        else:
            symbols = {"ok": "✔", "warning": "⚠", "error": "✘", "fix": "\U0001f527"}

        print(f"amifuse {__version__} -- fixing issues\n")
        for check in checks:
            if check.status == "ok":
                continue
            if check.fixable and check.fix_fn:
                print(f"  [{symbols['fix']}] {check.name}: {check.fix_description}")
                try:
                    check.fix_fn()
                except Exception as e:
                    print(f"      Failed: {e}")
            elif check.fix_description:
                print(f"  [{symbols[check.status]}] {check.name}: {check.fix_description}")

        # Re-run checks and show updated status
        print("\nRe-checking...\n")
        checks = run_checks()
        statuses = [c.status for c in checks]
        if "error" in statuses:
            overall = "not_ready"
        elif "warning" in statuses:
            overall = "degraded"
        else:
            overall = "ready"

        _print_human(checks, overall, __version__)
        return  # fix mode always exits 0

    # Default human-readable output
    _print_human(checks, overall, __version__)
    sys.exit({"ready": 0, "not_ready": 1, "degraded": 2}[overall])


def _print_human(checks: List[CheckResult], overall: str, version: str) -> None:
    """Print human-readable check results."""
    if sys.platform.startswith("win"):
        symbols = {"ok": "+", "warning": "!", "error": "X"}
    else:
        symbols = {"ok": "✔", "warning": "⚠", "error": "✘"}

    print(f"amifuse {version} environment check\n")
    for check in checks:
        sym = symbols[check.status]
        print(f"  [{sym}] {check.name}: {check.message}")
    print(f"\nOverall: {overall}")
