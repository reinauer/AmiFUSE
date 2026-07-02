"""Unit tests for amifuse.launcher module.

Mocks subprocess.Popen and ctypes.windll so tests run on all platforms.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


@pytest.fixture
def mock_popen(monkeypatch):
    """Mock subprocess.Popen and return the mock."""
    mock = MagicMock()
    monkeypatch.setattr("amifuse.launcher.subprocess.Popen", mock)
    return mock


@pytest.fixture
def mock_windll(monkeypatch):
    """Mock ctypes.windll and return it."""
    mock = MagicMock()
    import ctypes
    monkeypatch.setattr(ctypes, "windll", mock)
    return mock


@pytest.fixture
def mock_exit(monkeypatch):
    """Mock os._exit to prevent test process from exiting."""
    mock = MagicMock()
    monkeypatch.setattr("amifuse.launcher.os._exit", mock)
    return mock


@pytest.fixture
def no_sleep(monkeypatch):
    """Make the mount poll loops run without real delays."""
    monkeypatch.setattr("time.sleep", lambda s: None)


@pytest.fixture
def drive_env(monkeypatch, no_sleep):
    """Configure a deterministic drive environment for the launcher.

    Returns a ``configure(allocated, present)`` callable:

    - ``allocated``: iterable of bare uppercase letters the shared helper
      ``platform._windows_allocated_drive_letters`` should report as taken.
    - ``present``: iterable of drive paths (e.g. ``{"D:\\\\"}``) that
      ``os.path.exists`` should report True for -- i.e. mounts that "appeared".

    Selection is driven purely by ``allocated`` (the GetLogicalDrives bitmask
    stand-in); the mount-appeared poll is driven purely by ``present``. Keeping
    the two independent is what lets the Task 2 tests prove selection no longer
    uses ``os.path.exists``.
    """
    def _configure(allocated=("A", "B", "C"), present=()):
        monkeypatch.setattr(
            "amifuse.launcher.platform._windows_allocated_drive_letters",
            lambda: set(allocated),
        )
        present_set = set(present)
        monkeypatch.setattr(
            "amifuse.launcher.os.path.exists",
            lambda p: str(p) in present_set,
        )
    return _configure


class TestMountCommand:
    def test_mount_command_includes_daemon(self, mock_popen, mock_windll, mock_exit, drive_env):
        """--daemon is included in mount command."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        # Mutex exists (tray already running)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        args = mock_popen.call_args_list[0]
        cmd = args[0][0]
        assert "--daemon" in cmd

    def test_mount_command_includes_write_flag(self, mock_popen, mock_windll, mock_exit, drive_env):
        """--write is included when specified."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "--write", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert "--write" in cmd

    def test_mount_creation_flags_include_breakaway(self, mock_popen, mock_windll, mock_exit, drive_env):
        """Mount uses DETACHED flags with CREATE_BREAKAWAY_FROM_JOB."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import (
            main, DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP,
            CREATE_NO_WINDOW, CREATE_BREAKAWAY_FROM_JOB,
        )
        main(["mount", "test.hdf"])

        expected_flags = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB
        )
        kwargs = mock_popen.call_args_list[0][1]
        assert kwargs["creationflags"] == expected_flags

    def test_mount_falls_back_without_breakaway(self, mock_popen, mock_windll, mock_exit, drive_env):
        """If CREATE_BREAKAWAY_FROM_JOB fails, retry without it."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        # First Popen raises (breakaway denied), second succeeds
        mock_popen.side_effect = [OSError("breakaway denied"), MagicMock()]

        from amifuse.launcher import (
            main, DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW,
        )
        main(["mount", "test.hdf"])

        expected_fallback = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        kwargs = mock_popen.call_args_list[1][1]
        assert kwargs["creationflags"] == expected_fallback


class TestInspectCommand:
    def test_inspect_uses_create_new_console(self, mock_popen, mock_windll, mock_exit):
        """Inspect uses CREATE_NEW_CONSOLE flag."""
        from amifuse.launcher import main, CREATE_NEW_CONSOLE
        main(["inspect", "test.hdf"])

        kwargs = mock_popen.call_args_list[0][1]
        assert kwargs["creationflags"] == CREATE_NEW_CONSOLE

    def test_inspect_command_launches_python_directly(self, mock_popen, mock_windll, mock_exit):
        """Inspect launches python -c wrapper directly (no cmd.exe for security)."""
        from amifuse.launcher import main
        main(["inspect", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert cmd[0] == sys.executable
        assert cmd[1] == "-c"
        # Image path passed as positional arg (sys.argv), not interpolated into code
        assert cmd[-1] == "test.hdf"


class TestEnsureTrayRunning:
    def test_ensure_tray_running_skips_when_running(self, mock_popen, mock_windll, mock_exit, drive_env):
        """When mutex exists, no tray Popen is spawned."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 42  # non-zero = exists

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        # First call is mount Popen; should be no second call for tray
        assert mock_popen.call_count == 1

    def test_ensure_tray_running_spawns_when_not_running(self, mock_popen, mock_windll, mock_exit, monkeypatch, drive_env):
        """When mutex doesn't exist, tray is spawned."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 0  # 0 = not found
        # Make tray exe not exist so it falls back to python -m
        monkeypatch.setattr("amifuse.launcher.os.path.isfile", lambda p: False)

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        # Two Popen calls: mount + tray
        assert mock_popen.call_count == 2


class TestMainExits:
    def test_main_calls_os_exit(self, mock_popen, mock_windll, mock_exit, drive_env):
        """main() calls os._exit(0) for immediate exit."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "test.hdf"])
        mock_exit.assert_called_once_with(0)

    def test_inspect_calls_os_exit(self, mock_popen, mock_windll, mock_exit):
        """Inspect path also calls os._exit(0)."""
        from amifuse.launcher import main
        main(["inspect", "test.hdf"])
        mock_exit.assert_called_once_with(0)


class TestMountUsesPythonw:
    def test_mount_uses_pythonw(self, mock_popen, mock_windll, mock_exit, monkeypatch, drive_env):
        """Mount subprocess uses pythonw.exe."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1
        monkeypatch.setattr("sys.executable", r"C:\Python\python.exe")
        monkeypatch.setattr(
            "amifuse.launcher.os.path.isfile",
            lambda p: p == r"C:\Python\pythonw.exe",
        )

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert cmd[0] == r"C:\Python\pythonw.exe"


class TestLauncherUsesFileLogging:
    def test_launcher_uses_file_logging(self, mock_popen, mock_windll, mock_exit, tmp_path, monkeypatch):
        """Logging writes via open/write/close, not logging module."""
        mock_windll.kernel32.OpenMutexW.return_value = 1
        monkeypatch.setattr("amifuse.launcher._LOG_DIR", tmp_path)

        from amifuse.launcher import _log
        _log("test message")

        log_file = tmp_path / "launcher.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "test message" in content


class TestOpenCommand:
    """Verify the open (mount + Explorer) command."""

    def test_open_subparser_exists(self, mock_popen, mock_windll, mock_exit, monkeypatch, drive_env):
        """Launcher accepts 'open' subcommand without error."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        monkeypatch.setattr("amifuse.launcher.os.startfile", lambda p: None)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "test.hdf"])

        # Mount subprocess was launched
        assert mock_popen.call_count >= 1
        cmd = mock_popen.call_args_list[0][0][0]
        assert "--mountpoint" in cmd

    def test_do_open_function_exists(self):
        """_do_open function is defined."""
        from amifuse import launcher
        assert hasattr(launcher, '_do_open')
        assert callable(launcher._do_open)

    def test_show_error_function_exists(self):
        """_show_error function is defined."""
        from amifuse import launcher
        assert hasattr(launcher, '_show_error')
        assert callable(launcher._show_error)

    def test_open_shows_error_on_no_drive(self, mock_popen, mock_windll, mock_exit, monkeypatch, drive_env):
        """Shows error when no drive letter is available."""
        # Every letter allocated -> helper reports no free letter.
        drive_env(allocated=set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), present=())
        monkeypatch.setattr("amifuse.launcher.os.path.isfile", lambda p: False)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "test.hdf"])

        # No mount subprocess should be spawned when no letter is free.
        assert mock_popen.call_count == 0
        # MessageBoxW should have been called with error
        mock_windll.user32.MessageBoxW.assert_called_once()
        call_args = mock_windll.user32.MessageBoxW.call_args[0]
        # call_args[1] is ctypes.c_wchar_p; extract .value for string comparison
        msg = call_args[1].value if hasattr(call_args[1], 'value') else call_args[1]
        assert "No available drive letter" in msg

    def test_open_includes_daemon_and_mountpoint(self, mock_popen, mock_windll, mock_exit, monkeypatch, drive_env):
        """Open command passes --mountpoint and --daemon to mount subprocess."""
        # C: taken, so first free letter is D:.
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        monkeypatch.setattr("amifuse.launcher.os.startfile", lambda p: None)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert "--daemon" in cmd
        assert "--mountpoint" in cmd
        assert "--write" in cmd
        mp_idx = cmd.index("--mountpoint")
        assert cmd[mp_idx + 1] == "D:"


class TestOpenDriveSelection:
    """Task 2: _do_open selects letters via the shared allocated-letters helper."""

    def test_open_skips_allocated_letters(self, mock_popen, mock_windll, mock_exit, monkeypatch, drive_env):
        """Selection skips allocated letters and lands on the first free one.

        A,B,C,D allocated -> first free letter is E:. Crucially, os.path.exists
        reports True ONLY for E:\\ (the poll target) -- so if selection wrongly
        used os.path.exists it would pick D: (which reads as absent). Landing on
        E: proves selection is driven by the GetLogicalDrives helper, not the
        media probe.
        """
        drive_env(allocated=("A", "B", "C", "D"), present={"E:\\"})
        monkeypatch.setattr("amifuse.launcher.os.startfile", lambda p: None)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        mp_idx = cmd.index("--mountpoint")
        assert cmd[mp_idx + 1] == "E:"

    def test_open_consults_allocated_helper(self, mock_popen, mock_windll, mock_exit, monkeypatch):
        """_do_open selects via platform._windows_allocated_drive_letters()."""
        helper = MagicMock(return_value={"A", "B", "C"})
        monkeypatch.setattr(
            "amifuse.launcher.platform._windows_allocated_drive_letters", helper,
        )
        monkeypatch.setattr("amifuse.launcher.os.path.exists", lambda p: str(p) == "D:\\")
        monkeypatch.setattr("amifuse.launcher.os.startfile", lambda p: None)
        monkeypatch.setattr("time.sleep", lambda s: None)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "test.hdf"])

        helper.assert_called()

    def test_open_preserves_write_flag(self, mock_popen, mock_windll, mock_exit, monkeypatch, drive_env):
        """open always mounts writable -- --write must remain in the command."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        monkeypatch.setattr("amifuse.launcher.os.startfile", lambda p: None)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert "--write" in cmd


class TestMountSurfacesErrors:
    """Task 3: the mount (mountro verb) surfaces failures via a dialog."""

    def test_mount_success_no_dialog(self, mock_popen, mock_windll, mock_exit, drive_env):
        """When the mount appears, no error dialog is shown."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        mock_windll.user32.MessageBoxW.assert_not_called()

    def test_mount_timeout_shows_error(self, mock_popen, mock_windll, mock_exit, monkeypatch, drive_env):
        """When the mount never appears, a single error dialog is shown and the
        tray is started (a slow cold mount may still be initializing)."""
        # present=() -> os.path.exists always False -> poll times out.
        drive_env(allocated=("A", "B", "C"), present=())
        mock_windll.kernel32.OpenMutexW.return_value = 1
        tray = MagicMock()
        monkeypatch.setattr("amifuse.launcher._ensure_tray_running", tray)

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        # Tray is started even on timeout (slow-but-successful cold mount).
        tray.assert_called_once()
        # Dialog shown exactly once, with the softened (non-definitive) wording.
        mock_windll.user32.MessageBoxW.assert_called_once()
        call_args = mock_windll.user32.MessageBoxW.call_args[0]
        msg = call_args[1].value if hasattr(call_args[1], "value") else call_args[1]
        assert "did not appear" in msg.lower()

    def test_mount_spawn_failure_shows_error(self, mock_popen, mock_windll, mock_exit, drive_env):
        """A spawn failure surfaces an error dialog instead of exiting silently."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        # Both breakaway and fallback Popen raise -> _spawn_detached propagates.
        mock_popen.side_effect = OSError("spawn failed")
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        mock_windll.user32.MessageBoxW.assert_called_once()

    def test_mount_no_free_letter_shows_error(self, mock_popen, mock_windll, mock_exit, drive_env):
        """No free drive letter -> error dialog, no subprocess spawned."""
        drive_env(allocated=set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"), present=())
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        assert mock_popen.call_count == 0
        mock_windll.user32.MessageBoxW.assert_called_once()

    def test_mount_passes_mountpoint_and_write(self, mock_popen, mock_windll, mock_exit, drive_env):
        """A write mount passes explicit --mountpoint and --write."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "--write", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert "--mountpoint" in cmd
        mp_idx = cmd.index("--mountpoint")
        assert cmd[mp_idx + 1] == "D:"
        assert "--write" in cmd

    def test_mount_readonly_omits_write(self, mock_popen, mock_windll, mock_exit, drive_env):
        """A read-only mount (mountro verb) passes --mountpoint but not --write."""
        drive_env(allocated=("A", "B", "C"), present={"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert "--mountpoint" in cmd
        mp_idx = cmd.index("--mountpoint")
        assert cmd[mp_idx + 1] == "D:"
        assert "--write" not in cmd
