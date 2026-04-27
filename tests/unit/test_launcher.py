"""Unit tests for amifuse.launcher module.

Mocks subprocess.Popen and ctypes.windll so tests run on all platforms.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest


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


class TestMountCommand:
    def test_mount_command_includes_daemon(self, mock_popen, mock_windll, mock_exit):
        """--daemon is included in mount command."""
        # Mutex exists (tray already running)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        args = mock_popen.call_args_list[0]
        cmd = args[0][0]
        assert "--daemon" in cmd

    def test_mount_command_includes_write_flag(self, mock_popen, mock_windll, mock_exit):
        """--write is included when specified."""
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "--write", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert "--write" in cmd

    def test_mount_creation_flags_include_breakaway(self, mock_popen, mock_windll, mock_exit):
        """Mount uses DETACHED flags with CREATE_BREAKAWAY_FROM_JOB."""
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

    def test_mount_falls_back_without_breakaway(self, mock_popen, mock_windll, mock_exit):
        """If CREATE_BREAKAWAY_FROM_JOB fails, retry without it."""
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

    def test_inspect_command_uses_cmd_k(self, mock_popen, mock_windll, mock_exit):
        """Inspect command starts with ["cmd", "/k", ...]."""
        from amifuse.launcher import main
        main(["inspect", "test.hdf"])

        cmd = mock_popen.call_args_list[0][0][0]
        assert cmd[0] == "cmd"
        assert cmd[1] == "/k"


class TestEnsureTrayRunning:
    def test_ensure_tray_running_skips_when_running(self, mock_popen, mock_windll, mock_exit, monkeypatch):
        """When mutex exists, no tray Popen is spawned."""
        mock_windll.kernel32.OpenMutexW.return_value = 42  # non-zero = exists

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        # First call is mount Popen; should be no second call for tray
        assert mock_popen.call_count == 1

    def test_ensure_tray_running_spawns_when_not_running(self, mock_popen, mock_windll, mock_exit, monkeypatch):
        """When mutex doesn't exist, tray is spawned."""
        mock_windll.kernel32.OpenMutexW.return_value = 0  # 0 = not found
        # Make tray exe not exist so it falls back to python -m
        monkeypatch.setattr("amifuse.launcher.os.path.isfile", lambda p: False)

        from amifuse.launcher import main
        main(["mount", "test.hdf"])

        # Two Popen calls: mount + tray
        assert mock_popen.call_count == 2


class TestMainExits:
    def test_main_calls_os_exit(self, mock_popen, mock_windll, mock_exit):
        """main() calls os._exit(0) for immediate exit."""
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
    def test_mount_uses_pythonw(self, mock_popen, mock_windll, mock_exit, monkeypatch):
        """Mount subprocess uses pythonw.exe."""
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
