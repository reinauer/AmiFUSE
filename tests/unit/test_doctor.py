"""Unit tests for amifuse.doctor module.

Tests cover CheckResult dataclass, run_checks() with mocked dependencies,
cmd_doctor() output modes (human, JSON, fix), exit codes, and platform-specific
behavior. All platform-specific APIs are mocked so tests run on any OS.

Mock targets are patched at the module level where they are looked up:
    - amifuse.doctor.subprocess.run
    - amifuse.doctor.shutil.which
    - amifuse.doctor.sys.version_info
"""

import dataclasses
import json
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from amifuse.doctor import CheckResult, run_checks, cmd_doctor, _print_human


# ---------------------------------------------------------------------------
# A. CheckResult dataclass
# ---------------------------------------------------------------------------


class TestCheckResult:
    """Tests for the CheckResult dataclass."""

    def test_required_fields(self):
        r = CheckResult(name="test", status="ok", message="all good")
        assert r.name == "test"
        assert r.status == "ok"
        assert r.message == "all good"

    def test_default_fixable_false(self):
        r = CheckResult(name="t", status="ok", message="m")
        assert r.fixable is False

    def test_default_fix_fn_none(self):
        r = CheckResult(name="t", status="ok", message="m")
        assert r.fix_fn is None

    def test_default_fix_description_none(self):
        r = CheckResult(name="t", status="ok", message="m")
        assert r.fix_description is None

    def test_field_types(self):
        fields = {f.name: f.type for f in dataclasses.fields(CheckResult)}
        # Python 3.14+ returns actual types; earlier versions return strings
        assert fields["name"] in ("str", str)
        assert fields["status"] in ("str", str)
        assert fields["message"] in ("str", str)
        assert fields["fixable"] in ("bool", bool)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _VersionInfo:
    """Mimics sys.version_info for monkeypatching."""
    def __init__(self, major, minor, micro=0):
        self.major = major
        self.minor = minor
        self.micro = micro
    def __ge__(self, other):
        return (self.major, self.minor, self.micro) >= other
    def __lt__(self, other):
        return (self.major, self.minor, self.micro) < other


def _make_version_info(major, minor, micro=0):
    return _VersionInfo(major, minor, micro)


def _make_mock_args(json_output=False, fix=False):
    args = MagicMock()
    args.json = json_output
    args.fix = fix
    return args


def _patch_all_checks(**overrides):
    """Return a list of patches that isolate run_checks() from real system state.

    Keyword overrides:
        version_info: sys.version_info value (default 3.12.0)
        amitools_import: True (importable) or False (ImportError)
        subprocess_rc: return code for machine68k subprocess check
        subprocess_timeout: if True, raise TimeoutExpired
        fuse_import: True (importable) or False (ImportError)
        fuse_version: __version__ value on the fuse module (or None)
        backend: dict for detect_fuse_backend return value
        platform: sys.platform value
        which_amifuse: return value for shutil.which("amifuse")
        shell_registered: True/False/None (None = skip, i.e. non-windows)
    """
    patches = []
    vi = overrides.get("version_info", _make_version_info(3, 12, 0))
    patches.append(patch("amifuse.doctor.sys.version_info", vi))
    patches.append(patch("amifuse.doctor.sys.platform",
                         overrides.get("platform", "linux")))

    # amitools import
    amitools_ok = overrides.get("amitools_import", True)
    if amitools_ok:
        mock_amitools = types.ModuleType("amitools")
        mock_amitools.__version__ = "0.8.0"
    else:
        mock_amitools = None  # sentinel

    # fuse import
    fuse_ok = overrides.get("fuse_import", True)
    fuse_ver = overrides.get("fuse_version", "1.0.0")
    if fuse_ok:
        mock_fuse = types.ModuleType("fuse")
        if fuse_ver is not None:
            mock_fuse.__version__ = fuse_ver
        # else no __version__ attribute
    else:
        mock_fuse = None

    # Build a custom __import__ to intercept amitools and fuse
    real_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else __import__

    def custom_import(name, *args, **kwargs):
        if name == "amitools":
            if mock_amitools is None:
                raise ImportError("No module named 'amitools'")
            return mock_amitools
        if name == "fuse":
            if mock_fuse is None:
                raise ImportError("No module named 'fuse'")
            return mock_fuse
        return real_import(name, *args, **kwargs)

    patches.append(patch("builtins.__import__", side_effect=custom_import))

    # subprocess for machine68k
    timeout = overrides.get("subprocess_timeout", False)
    if timeout:
        patches.append(patch("amifuse.doctor.subprocess.run",
                             side_effect=subprocess.TimeoutExpired(cmd="test", timeout=10)))
    else:
        rc = overrides.get("subprocess_rc", 0)
        mock_proc = MagicMock()
        mock_proc.returncode = rc
        patches.append(patch("amifuse.doctor.subprocess.run", return_value=mock_proc))

    # detect_fuse_backend
    backend = overrides.get("backend", {"installed": True, "name": "FUSE", "version": None})
    patches.append(patch("amifuse.platform.detect_fuse_backend", return_value=backend))

    # shutil.which for PATH check
    which_val = overrides.get("which_amifuse", "/usr/bin/amifuse")
    patches.append(patch("amifuse.doctor.shutil.which", return_value=which_val))

    # Shell registration (Windows only) -- mock the import inside run_checks
    shell_reg = overrides.get("shell_registered", None)
    if overrides.get("platform", "linux").startswith("win") and shell_reg is not None:
        mock_ws = types.ModuleType("amifuse.windows_shell")
        mock_ws.is_registered = lambda: shell_reg
        mock_ws.register = MagicMock()
        patches.append(patch.dict("sys.modules", {"amifuse.windows_shell": mock_ws}))

    return patches


def _run_with_patches(patches):
    """Apply patches and run run_checks(), returning results."""
    for p in patches:
        p.start()
    try:
        return run_checks()
    finally:
        for p in reversed(patches):
            p.stop()


def _find_check(results, name):
    for r in results:
        if r.name == name:
            return r
    return None


# ---------------------------------------------------------------------------
# B. run_checks()
# ---------------------------------------------------------------------------


class TestRunChecks:
    """Tests for run_checks() with mocked dependencies."""

    def test_returns_list_of_check_results(self):
        patches = _patch_all_checks()
        results = _run_with_patches(patches)
        assert isinstance(results, list)
        assert all(isinstance(r, CheckResult) for r in results)
        names = [r.name for r in results]
        assert "python" in names
        assert "amitools" in names
        assert "machine68k" in names
        assert "fusepy" in names
        assert "fuse_backend" in names
        assert "path" in names

    def test_python_version_ok(self):
        patches = _patch_all_checks(version_info=_make_version_info(3, 12, 0))
        results = _run_with_patches(patches)
        c = _find_check(results, "python")
        assert c.status == "ok"
        assert "3.12.0" in c.message

    def test_python_version_error(self):
        patches = _patch_all_checks(version_info=_make_version_info(3, 8, 0))
        results = _run_with_patches(patches)
        c = _find_check(results, "python")
        assert c.status == "error"
        assert "3.9+" in c.message

    def test_amitools_importable(self):
        patches = _patch_all_checks(amitools_import=True)
        results = _run_with_patches(patches)
        c = _find_check(results, "amitools")
        assert c.status == "ok"
        assert "0.8.0" in c.message

    def test_amitools_not_installed(self):
        patches = _patch_all_checks(amitools_import=False)
        results = _run_with_patches(patches)
        c = _find_check(results, "amitools")
        assert c.status == "error"
        assert c.fixable is True
        assert "not installed" in c.message

    def test_machine68k_ok(self):
        patches = _patch_all_checks(subprocess_rc=0)
        results = _run_with_patches(patches)
        c = _find_check(results, "machine68k")
        assert c.status == "ok"
        assert "working" in c.message

    def test_machine68k_import_error(self):
        patches = _patch_all_checks(subprocess_rc=1)
        results = _run_with_patches(patches)
        c = _find_check(results, "machine68k")
        assert c.status == "error"
        assert "not installed" in c.message

    def test_machine68k_segfault_windows(self):
        patches = _patch_all_checks(subprocess_rc=-1073741819)
        results = _run_with_patches(patches)
        c = _find_check(results, "machine68k")
        assert c.status == "warning"
        assert "segfault" in c.message.lower()

    def test_machine68k_segfault_unix(self):
        patches = _patch_all_checks(subprocess_rc=-11)
        results = _run_with_patches(patches)
        c = _find_check(results, "machine68k")
        assert c.status == "warning"
        assert "segfault" in c.message.lower()

    def test_machine68k_timeout(self):
        patches = _patch_all_checks(subprocess_timeout=True)
        results = _run_with_patches(patches)
        c = _find_check(results, "machine68k")
        assert c.status == "warning"
        assert "timed out" in c.message

    def test_machine68k_subprocess_command_has_correct_args(self):
        """Verify subprocess call uses Machine(0, 1024) positional args."""
        real_import = __import__

        def custom_import(name, *args, **kwargs):
            if name == "amitools":
                m = types.ModuleType("amitools")
                m.__version__ = "0.8.0"
                return m
            if name == "fuse":
                m = types.ModuleType("fuse")
                m.__version__ = "1.0.0"
                return m
            return real_import(name, *args, **kwargs)

        with patch("amifuse.doctor.subprocess.run", return_value=MagicMock(returncode=0)) as mock_run, \
             patch("amifuse.doctor.sys.version_info", _make_version_info(3, 12, 0)), \
             patch("amifuse.doctor.sys.platform", "linux"), \
             patch("builtins.__import__", side_effect=custom_import), \
             patch("amifuse.doctor.shutil.which", return_value="/usr/bin/amifuse"), \
             patch("amifuse.platform.detect_fuse_backend",
                   return_value={"installed": True, "name": "FUSE", "version": None}):
            run_checks()

            assert mock_run.called
            cmd_args = mock_run.call_args[0][0]
            code_arg = cmd_args[2]  # the -c argument
            assert "Machine(0, 1024)" in code_arg

    def test_fusepy_importable_with_version(self):
        patches = _patch_all_checks(fuse_import=True, fuse_version="1.0.0")
        results = _run_with_patches(patches)
        c = _find_check(results, "fusepy")
        assert c.status == "ok"
        assert "1.0.0" in c.message

    def test_fusepy_importable_no_version(self):
        """When fusepy has no __version__, message should say 'installed' not 'unknown'."""
        patches = _patch_all_checks(fuse_import=True, fuse_version=None)
        results = _run_with_patches(patches)
        c = _find_check(results, "fusepy")
        assert c.status == "ok"
        assert "installed" in c.message
        assert "unknown" not in c.message.lower()

    def test_fusepy_not_installed(self):
        patches = _patch_all_checks(fuse_import=False)
        results = _run_with_patches(patches)
        c = _find_check(results, "fusepy")
        assert c.status == "error"
        assert c.fixable is True

    def test_fuse_backend_installed(self):
        patches = _patch_all_checks(
            backend={"installed": True, "name": "WinFSP", "version": "2.0"})
        results = _run_with_patches(patches)
        c = _find_check(results, "fuse_backend")
        assert c.status == "ok"
        assert "WinFSP" in c.message
        assert "2.0" in c.message

    def test_fuse_backend_not_installed(self):
        patches = _patch_all_checks(
            backend={"installed": False, "name": "FUSE", "version": None})
        results = _run_with_patches(patches)
        c = _find_check(results, "fuse_backend")
        assert c.status == "error"
        assert c.fixable is False
        assert c.fix_description is not None

    def test_shell_registration_registered(self):
        patches = _patch_all_checks(platform="win32", shell_registered=True)
        results = _run_with_patches(patches)
        c = _find_check(results, "shell_registration")
        assert c is not None
        assert c.status == "ok"

    def test_shell_registration_not_registered(self):
        patches = _patch_all_checks(platform="win32", shell_registered=False)
        results = _run_with_patches(patches)
        c = _find_check(results, "shell_registration")
        assert c is not None
        assert c.status == "warning"
        assert c.fixable is True

    def test_shell_registration_skipped_non_windows(self):
        patches = _patch_all_checks(platform="linux")
        results = _run_with_patches(patches)
        c = _find_check(results, "shell_registration")
        assert c is None

    def test_path_found(self):
        patches = _patch_all_checks(which_amifuse="/usr/bin/amifuse")
        results = _run_with_patches(patches)
        c = _find_check(results, "path")
        assert c.status == "ok"

    def test_path_not_found(self):
        patches = _patch_all_checks(which_amifuse=None)
        results = _run_with_patches(patches)
        c = _find_check(results, "path")
        assert c.status == "warning"
        assert c.fixable is True


# ---------------------------------------------------------------------------
# C. cmd_doctor()
# ---------------------------------------------------------------------------


class TestCmdDoctor:
    """Tests for cmd_doctor() output formatting and exit codes."""

    def _run_doctor(self, capsys, monkeypatch, checks, args, version="v0.5.0"):
        """Run cmd_doctor with mocked run_checks and __version__."""
        monkeypatch.setattr("amifuse.doctor.run_checks", lambda: checks)
        try:
            cmd_doctor(args)
        except SystemExit:
            pass
        return capsys.readouterr()

    def test_human_output_format(self, capsys, monkeypatch):
        checks = [
            CheckResult("python", "ok", "Python 3.12.0"),
            CheckResult("amitools", "error", "not installed"),
        ]
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        out = self._run_doctor(capsys, monkeypatch, checks, _make_mock_args())
        assert "environment check" in out.out
        assert "python" in out.out
        assert "Overall:" in out.out

    def test_ascii_symbols_on_windows(self, capsys, monkeypatch):
        """On Windows, symbols should be ASCII: +, !, X (no Unicode)."""
        checks = [
            CheckResult("python", "ok", "Python 3.12.0"),
            CheckResult("amitools", "warning", "old version"),
            CheckResult("machine68k", "error", "missing"),
        ]
        monkeypatch.setattr("amifuse.doctor.sys.platform", "win32")
        out = self._run_doctor(capsys, monkeypatch, checks, _make_mock_args())
        assert "[+]" in out.out
        assert "[!]" in out.out
        assert "[X]" in out.out
        # No Unicode check/cross marks
        assert "✔" not in out.out  # checkmark
        assert "✘" not in out.out  # cross
        assert "⚠" not in out.out  # warning

    def test_unicode_symbols_on_unix(self, capsys, monkeypatch):
        checks = [
            CheckResult("python", "ok", "Python 3.12.0"),
            CheckResult("amitools", "warning", "old version"),
            CheckResult("machine68k", "error", "missing"),
        ]
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        out = self._run_doctor(capsys, monkeypatch, checks, _make_mock_args())
        assert "✔" in out.out  # checkmark
        assert "⚠" in out.out  # warning
        assert "✘" in out.out  # cross

    def test_json_output_valid(self, capsys, monkeypatch):
        checks = [CheckResult("python", "ok", "Python 3.12.0")]
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        out = self._run_doctor(capsys, monkeypatch, checks,
                               _make_mock_args(json_output=True))
        data = json.loads(out.out)
        assert isinstance(data, dict)

    def test_json_schema(self, capsys, monkeypatch):
        checks = [CheckResult("python", "ok", "Python 3.12.0")]
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        out = self._run_doctor(capsys, monkeypatch, checks,
                               _make_mock_args(json_output=True))
        data = json.loads(out.out)
        assert "overall_status" in data
        assert "platform" in data
        assert "version" in data
        assert "checks" in data
        assert isinstance(data["checks"], list)

    def test_json_check_fields(self, capsys, monkeypatch):
        checks = [CheckResult("python", "ok", "Python 3.12.0",
                              fixable=True, fix_description="upgrade")]
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        out = self._run_doctor(capsys, monkeypatch, checks,
                               _make_mock_args(json_output=True))
        data = json.loads(out.out)
        c = data["checks"][0]
        assert c["name"] == "python"
        assert c["status"] == "ok"
        assert c["message"] == "Python 3.12.0"
        assert c["fixable"] is True
        assert c["fix_description"] == "upgrade"

    def test_json_no_fix_fn(self, capsys, monkeypatch):
        """fix_fn must NOT appear in JSON output."""
        fn = lambda: None
        checks = [CheckResult("test", "ok", "m", fix_fn=fn)]
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        out = self._run_doctor(capsys, monkeypatch, checks,
                               _make_mock_args(json_output=True))
        data = json.loads(out.out)
        assert "fix_fn" not in data["checks"][0]

    def test_exit_code_ready(self, monkeypatch):
        checks = [CheckResult("python", "ok", "ok")]
        monkeypatch.setattr("amifuse.doctor.run_checks", lambda: checks)
        with pytest.raises(SystemExit) as exc_info:
            cmd_doctor(_make_mock_args())
        assert exc_info.value.code == 0

    def test_exit_code_not_ready(self, monkeypatch):
        checks = [CheckResult("python", "error", "bad")]
        monkeypatch.setattr("amifuse.doctor.run_checks", lambda: checks)
        with pytest.raises(SystemExit) as exc_info:
            cmd_doctor(_make_mock_args())
        assert exc_info.value.code == 1

    def test_exit_code_degraded(self, monkeypatch):
        checks = [CheckResult("path", "warning", "not on path")]
        monkeypatch.setattr("amifuse.doctor.run_checks", lambda: checks)
        with pytest.raises(SystemExit) as exc_info:
            cmd_doctor(_make_mock_args())
        assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# D. Fix mode
# ---------------------------------------------------------------------------


class TestFixMode:
    """Tests for --fix mode behavior."""

    def test_fix_calls_fixable_checks(self, capsys, monkeypatch):
        fix_fn = MagicMock()
        checks = [CheckResult("test", "error", "broken", fixable=True,
                               fix_fn=fix_fn, fix_description="fix it")]
        monkeypatch.setattr("amifuse.doctor.run_checks", lambda: list(checks))
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        cmd_doctor(_make_mock_args(fix=True))
        fix_fn.assert_called_once()

    def test_fix_skips_unfixable(self, capsys, monkeypatch):
        fix_fn = MagicMock()
        checks = [CheckResult("test", "error", "broken", fixable=False,
                               fix_fn=fix_fn, fix_description="manual fix")]
        monkeypatch.setattr("amifuse.doctor.run_checks", lambda: list(checks))
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        cmd_doctor(_make_mock_args(fix=True))
        fix_fn.assert_not_called()

    def test_fix_reports_unfixable_description(self, capsys, monkeypatch):
        checks = [CheckResult("test", "error", "broken", fixable=False,
                               fix_description="install manually")]
        monkeypatch.setattr("amifuse.doctor.run_checks", lambda: list(checks))
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        cmd_doctor(_make_mock_args(fix=True))
        out = capsys.readouterr()
        assert "install manually" in out.out

    def test_fix_reruns_checks(self, monkeypatch, capsys):
        """Fix mode should call run_checks twice (before fix, after fix)."""
        call_count = 0

        def counting_run_checks():
            nonlocal call_count
            call_count += 1
            return [CheckResult("test", "ok", "fixed")]

        monkeypatch.setattr("amifuse.doctor.run_checks", counting_run_checks)
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        cmd_doctor(_make_mock_args(fix=True))
        assert call_count == 2

    def test_fix_exit_code_zero(self, monkeypatch, capsys):
        """Fix mode always exits 0 (returns normally, no sys.exit)."""
        checks = [CheckResult("test", "error", "broken")]
        monkeypatch.setattr("amifuse.doctor.run_checks", lambda: list(checks))
        monkeypatch.setattr("amifuse.doctor.sys.platform", "linux")
        # Should NOT raise SystemExit
        cmd_doctor(_make_mock_args(fix=True))


# ---------------------------------------------------------------------------
# E. PATH fix
# ---------------------------------------------------------------------------


class TestPathFix:
    """Tests for PATH fix behavior on Windows and Unix."""

    def test_path_fix_windows_no_doubling(self):
        """PATH fix should use parent directory directly, not Scripts\\Scripts."""
        patches = _patch_all_checks(platform="win32", which_amifuse=None)
        results = _run_with_patches(patches)
        c = _find_check(results, "path")
        assert c.fixable is True
        # The scripts_dir should be sys.executable's parent, not have doubled "Scripts"
        expected_dir = str(Path(sys.executable).parent)
        assert expected_dir in c.message
        # Should not contain doubled path segments
        assert "Scripts\\Scripts" not in c.message
        assert "scripts\\scripts" not in c.message.lower().replace("/", "\\")

    def test_path_fix_windows_uses_parent_directly(self):
        """Verify fix uses Path(sys.executable).parent, not a Scripts subdir."""
        patches = _patch_all_checks(platform="win32", which_amifuse=None)
        results = _run_with_patches(patches)
        c = _find_check(results, "path")
        expected = str(Path(sys.executable).parent)
        assert expected in c.fix_description

    def test_path_fix_unix_appends_to_shell_profile(self):
        """Unix PATH fix should reference a shell profile file."""
        patches = _patch_all_checks(platform="linux", which_amifuse=None)
        results = _run_with_patches(patches)
        c = _find_check(results, "path")
        assert c.fixable is True
        # Should mention a shell rc file
        desc = c.fix_description
        assert ".bashrc" in desc or ".zshrc" in desc


# ---------------------------------------------------------------------------
# F. Platform-specific check descriptions
# ---------------------------------------------------------------------------


class TestPlatformSpecificChecks:
    """Tests for platform-specific fix descriptions."""

    def test_fuse_backend_fix_description_windows(self):
        patches = _patch_all_checks(
            platform="win32",
            backend={"installed": False, "name": "WinFSP", "version": None})
        results = _run_with_patches(patches)
        c = _find_check(results, "fuse_backend")
        assert "WinFsp.WinFsp" in c.fix_description or "winfsp.dev" in c.fix_description

    def test_fuse_backend_fix_description_macos(self):
        patches = _patch_all_checks(
            platform="darwin",
            backend={"installed": False, "name": "macFUSE", "version": None})
        results = _run_with_patches(patches)
        c = _find_check(results, "fuse_backend")
        assert "brew install" in c.fix_description
        assert "macfuse" in c.fix_description

    def test_fuse_backend_fix_description_linux(self):
        patches = _patch_all_checks(
            platform="linux",
            backend={"installed": False, "name": "FUSE", "version": None})
        results = _run_with_patches(patches)
        c = _find_check(results, "fuse_backend")
        assert "apt install" in c.fix_description or "sudo" in c.fix_description

    def test_machine68k_fix_description_segfault(self):
        patches = _patch_all_checks(subprocess_rc=-11)
        results = _run_with_patches(patches)
        c = _find_check(results, "machine68k")
        assert c.fix_description == "pip install machine68k-amifuse"

    def test_machine68k_fix_description_missing(self):
        patches = _patch_all_checks(subprocess_rc=1)
        results = _run_with_patches(patches)
        c = _find_check(results, "machine68k")
        assert c.fix_description == "pip install machine68k"


# ---------------------------------------------------------------------------
# K. Drivers check -- 2 tests
# ---------------------------------------------------------------------------


class TestDriversCheck:
    """Tests for the drivers availability check in run_checks()."""

    def test_drivers_found(self, monkeypatch, tmp_path):
        """Drivers check reports ok when FastFileSystem found."""
        driver_dir = tmp_path / "drivers"
        driver_dir.mkdir()
        (driver_dir / "FastFileSystem").write_bytes(b"\x00")

        monkeypatch.setattr(
            "amifuse.platform.get_driver_search_dirs",
            lambda: [driver_dir],
        )

        patches = _patch_all_checks()
        results = _run_with_patches(patches)
        c = _find_check(results, "drivers")
        assert c.status == "ok"
        assert "FastFileSystem" in c.message

    def test_drivers_not_found(self, monkeypatch, tmp_path):
        """Drivers check reports warning when no drivers found."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        monkeypatch.setattr(
            "amifuse.platform.get_driver_search_dirs",
            lambda: [empty_dir],
        )

        patches = _patch_all_checks()
        results = _run_with_patches(patches)
        c = _find_check(results, "drivers")
        assert c.status == "warning"
        assert "not found" in c.message
