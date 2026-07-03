"""Unit tests for amifuse.launcher module.

Mocks subprocess.Popen and ctypes.windll. This module is Windows-only
(``pytestmark`` skips it off Windows); the cross-platform guard test lives
in ``test_launcher_guard.py``.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


def _msg_text(mock_windll) -> str:
    """Extract the message string from the last MessageBoxW call.

    The message arg is a ``ctypes.c_wchar_p``; return its ``.value`` (or the raw
    arg if it is already a plain string).
    """
    arg = mock_windll.user32.MessageBoxW.call_args[0][1]
    return arg.value if hasattr(arg, "value") else arg


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


class TestSelectDriveLetters:
    """Task 3: _select_drive_letters(n) -> (chosen, total_free) selector."""

    @pytest.fixture
    def allocated(self, monkeypatch):
        """Return a setter that mocks the GetLogicalDrives allocation helper.

        Selection is driven purely by the bitmask stand-in; no real drives are
        touched. The helper returns bare uppercase letters (e.g. ``{"A","B"}``),
        matching platform._windows_allocated_drive_letters.
        """
        def _set(letters):
            monkeypatch.setattr(
                "amifuse.launcher.platform._windows_allocated_drive_letters",
                lambda: set(letters),
            )
        return _set

    def test_returns_n_distinct_free_letters_in_order(self, allocated):
        """(a) n distinct free letters are returned in D-start alphabet order."""
        allocated(("A", "B", "C"))  # only reserved letters taken
        from amifuse.launcher import _select_drive_letters

        chosen, total_free = _select_drive_letters(3)
        assert chosen == ["D:", "E:", "F:"]
        assert len(set(chosen)) == 3  # distinct

    def test_total_free_reflects_true_availability(self, allocated):
        """(b) total_free counts every free letter in the D-start alphabet."""
        # A,B,C reserved + D,E taken -> free = F..Z = 21 letters.
        allocated(("A", "B", "C", "D", "E"))
        from amifuse.launcher import _select_drive_letters

        chosen, total_free = _select_drive_letters(2)
        assert chosen == ["F:", "G:"]
        assert total_free == 21

    def test_partial_when_total_free_below_n(self, allocated):
        """(c) when total_free < n, only total_free letters are returned."""
        # Everything except X,Y,Z allocated -> 3 free, request 5.
        allocated(set("ABCDEFGHIJKLMNOPQRSTUVW"))
        from amifuse.launcher import _select_drive_letters

        chosen, total_free = _select_drive_letters(5)
        assert chosen == ["X:", "Y:", "Z:"]
        assert total_free == 3
        assert len(chosen) == total_free  # partial, not padded

    def test_no_free_letters_returns_empty(self, allocated):
        """(c') full exhaustion -> empty chosen, total_free 0 (no IndexError)."""
        allocated(set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        from amifuse.launcher import _select_drive_letters

        chosen, total_free = _select_drive_letters(3)
        assert chosen == []
        assert total_free == 0

    def test_single_wrapper_matches_first_free_letter(self, allocated):
        """(d) _select_drive_letter() still returns the same first free letter."""
        allocated(("A", "B", "C", "D"))  # first free is E:
        from amifuse.launcher import _select_drive_letter, _select_drive_letters

        assert _select_drive_letter() == "E:"
        # Identity: the N=1 wrapper is exactly the first chosen letter, i.e.
        # _select_drive_letters(1) -> (["E:"], total); chosen[0] == "E:".
        chosen, _total = _select_drive_letters(1)
        assert _select_drive_letter() == chosen[0]

    def test_single_wrapper_returns_none_when_no_free_letter(self, allocated):
        """(d') the N=1 wrapper preserves the None contract on exhaustion."""
        allocated(set("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
        from amifuse.launcher import _select_drive_letter

        assert _select_drive_letter() is None

    def test_abc_never_returned_or_counted_when_free(self, allocated):
        """(e) D-start alphabet: A/B/C are never chosen and never counted.

        Report A,B,C as free-in-bitmask-terms (nothing allocated at all). An A-Z
        implementation would return A:/B:/C: and count them; the D-start alphabet
        must skip them for both the chosen list and total_free.
        """
        allocated(())  # bitmask reports every letter, incl. A/B/C, as free
        from amifuse.launcher import _select_drive_letters

        chosen, total_free = _select_drive_letters(3)
        assert chosen == ["D:", "E:", "F:"]  # not A:/B:/C:
        assert "A:" not in chosen and "B:" not in chosen and "C:" not in chosen
        assert total_free == 23  # D..Z, not 26 -- A/B/C excluded from the count


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


@pytest.fixture
def fanout(monkeypatch, no_sleep):
    """Drive mount_image_all with controlled enumeration + drive environment.

    Returns ``(configure, opened)``. ``configure`` patches:

    - ``_enumerate_mount_units`` -> ``_MountUnit`` list from ``units_spec`` (each
      entry a partition name, or ``None`` for the collapsed no-``--partition``
      unit) plus ``fallback_reason``. Enumeration is mocked so the fan-out logic
      is tested in isolation from rdb_inspect/amitools.
    - the GetLogicalDrives allocation helper (letter selection) from ``allocated``.
    - ``os.path.exists`` (the arrival poll) from ``present``.
    - ``os.startfile`` -- recorded into the returned ``opened`` list.
    """
    opened: list[str] = []

    def _configure(units_spec, allocated, present, fallback_reason=None):
        from amifuse.launcher import _MountUnit
        units = [_MountUnit(name=n, label=(n or "disk image")) for n in units_spec]
        monkeypatch.setattr(
            "amifuse.launcher._enumerate_mount_units",
            lambda image: (units, fallback_reason),
        )
        monkeypatch.setattr(
            "amifuse.launcher.platform._windows_allocated_drive_letters",
            lambda: set(allocated),
        )
        present_set = set(present)
        monkeypatch.setattr(
            "amifuse.launcher.os.path.exists", lambda p: str(p) in present_set
        )
        monkeypatch.setattr(
            "amifuse.launcher.os.startfile", lambda p: opened.append(str(p))
        )

    return _configure, opened


class TestMountImageAllFanOut:
    """Task 4: the mount_image_all fan-out core behind _do_open / _do_mount."""

    def test_open_spawns_one_mount_per_partition_by_name(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """open -> one --write spawn per partition, keyed by --partition <name>."""
        configure, _opened = fanout
        configure(["WB_1.3", "WB_2.x", "Work"], ("A", "B", "C"),
                  {"D:\\", "E:\\", "F:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1  # tray already running

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        cmds = [c[0][0] for c in mock_popen.call_args_list]
        assert len(cmds) == 3  # three mount spawns, no tray spawn (mutex present)
        expected = [("WB_1.3", "D:"), ("WB_2.x", "E:"), ("Work", "F:")]
        for cmd, (name, letter) in zip(cmds, expected):
            assert cmd[cmd.index("--partition") + 1] == name
            assert cmd[cmd.index("--mountpoint") + 1] == letter
            assert "--write" in cmd
            assert cmd[cmd.index("--daemon") + 1] == "img.hdf"

    def test_mountro_spawns_all_partitions_without_write(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """mountro -> one spawn per partition, each with --partition, no --write."""
        configure, _opened = fanout
        configure(["WB_1.3", "WB_2.x", "Work"], ("A", "B", "C"),
                  {"D:\\", "E:\\", "F:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "img.hdf"])

        cmds = [c[0][0] for c in mock_popen.call_args_list]
        assert len(cmds) == 3
        for cmd in cmds:
            assert "--partition" in cmd
            assert "--write" not in cmd  # mountro never passes --write

    def test_open_opens_one_window_per_confirmed_drive(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """open opens exactly one Explorer window per confirmed drive."""
        configure, opened = fanout
        configure(["WB_1.3", "WB_2.x", "Work"], ("A", "B", "C"),
                  {"D:\\", "E:\\", "F:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        assert sorted(opened) == ["D:\\", "E:\\", "F:\\"]

    def test_mountro_opens_no_windows(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """mountro opens no Explorer windows (init() refresh handles arrival)."""
        configure, opened = fanout
        configure(["WB_1.3", "WB_2.x", "Work"], ("A", "B", "C"),
                  {"D:\\", "E:\\", "F:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "img.hdf"])

        assert opened == []

    def test_tray_started_once(
        self, mock_popen, mock_windll, mock_exit, monkeypatch, fanout
    ):
        """The tray is started exactly once after the aggregate poll, not per spawn."""
        configure, _opened = fanout
        configure(["WB_1.3", "WB_2.x", "Work"], ("A", "B", "C"),
                  {"D:\\", "E:\\", "F:\\"})
        tray = MagicMock()
        monkeypatch.setattr("amifuse.launcher._ensure_tray_running", tray)

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        tray.assert_called_once()

    def test_partial_on_letter_exhaustion_names_skipped(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """Fewer letters than partitions -> mount what fits, skip the rest, and
        show ONE summary dialog naming the skipped partition."""
        configure, opened = fanout
        # Only D: and E: free (everything else allocated) -> total_free == 2.
        allocated = set("ABC") | set("FGHIJKLMNOPQRSTUVWXYZ")
        configure(["WB_1.3", "WB_2.x", "Work"], allocated, {"D:\\", "E:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        # Two spawns only (Work skipped -- no free letter).
        assert len(mock_popen.call_args_list) == 2
        # A window is opened for each CONFIRMED drive (D:, E:) and none for the
        # skipped partition -- the open path only follows through on real drives.
        assert sorted(opened) == ["D:\\", "E:\\"]
        # Exactly one summary dialog, naming the skipped partition + reason.
        mock_windll.user32.MessageBoxW.assert_called_once()
        msg = _msg_text(mock_windll)
        assert "Work" in msg
        assert "no free drive letter" in msg
        assert "WB_1.3 (D:)" in msg and "WB_2.x (E:)" in msg  # succeeded named

    def test_fallback_reason_surfaced_in_summary(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """A duplicate-name fallback_reason is surfaced in the summary dialog even
        though the (single) mount itself succeeds."""
        configure, _opened = fanout
        reason = ("multiple RDBs with colliding partition names "
                  "('DH0'); mounted first only")
        configure([None], ("A", "B", "C"), {"D:\\"}, fallback_reason=reason)
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        mock_windll.user32.MessageBoxW.assert_called_once()
        assert "colliding partition names" in _msg_text(mock_windll)

    def test_timeout_reports_pending_softly(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """A drive that never appears is reported with soft, non-definitive wording."""
        configure, _opened = fanout
        configure(["WB_1.3"], ("A", "B", "C"), present=())  # never appears
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        mock_windll.user32.MessageBoxW.assert_called_once()
        assert "did not appear" in _msg_text(mock_windll).lower()

    def test_deadline_scales_with_spawn_count(
        self, mock_popen, mock_windll, mock_exit, monkeypatch, fanout
    ):
        """The poll uses the aggregate deadline sized by the spawn count (N=3),
        not a flat per-mount 15s."""
        configure, _opened = fanout
        configure(["WB_1.3", "WB_2.x", "Work"], ("A", "B", "C"),
                  {"D:\\", "E:\\", "F:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1
        spy = MagicMock(return_value=25.0)
        monkeypatch.setattr("amifuse.launcher._aggregate_timeout", spy)

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        spy.assert_called_once_with(3)

    def test_spawn_failure_is_definite_and_isolated(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """One partition whose _spawn_detached raises lands in the spawn-failed
        bucket with DEFINITE wording (not the soft "may still be starting"), and
        the other partitions still mount and open windows -- independent
        processes, so one never-started mount does not sink the others."""
        configure, opened = fanout
        configure(["WB_1.3", "WB_2.x", "Work"], ("A", "B", "C"),
                  {"D:\\", "E:\\", "F:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        # WB_2.x (E:) never launches: raise a NON-OSError so _spawn_detached
        # propagates immediately (its retry only catches OSError). The other two
        # spawns succeed.
        def _side(cmd, *a, **kw):
            if "--partition" in cmd and cmd[cmd.index("--partition") + 1] == "WB_2.x":
                raise RuntimeError("CreateProcess failed")
            return MagicMock()
        mock_popen.side_effect = _side

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        # Isolation: the two healthy partitions still mounted and each opened a
        # window; the never-started one opened none.
        assert sorted(opened) == ["D:\\", "F:\\"]

        # One dialog, definite wording for the spawn failure, and NO soft
        # "may still be starting" phrasing (nothing timed out here).
        mock_windll.user32.MessageBoxW.assert_called_once()
        msg = _msg_text(mock_windll)
        assert "Could not start mount for WB_2.x (E:)" in msg
        assert "CreateProcess failed" in msg          # the underlying reason
        assert "may still be starting" not in msg      # not softened
        assert "WB_1.3 (D:)" in msg and "Work (F:)" in msg  # healthy ones named

    def test_skipped_and_timeout_share_one_dialog(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """Letter exhaustion (skipped) and an arrival timeout (failed) co-occur
        in a SINGLE summary dialog naming both buckets -- one _show_error call."""
        configure, _opened = fanout
        # Only D: and E: free -> Work is skipped; D:/E: spawn but never appear.
        allocated = set("ABC") | set("FGHIJKLMNOPQRSTUVWXYZ")
        configure(["WB_1.3", "WB_2.x", "Work"], allocated, present=())
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        # Exactly one dialog carrying BOTH buckets.
        mock_windll.user32.MessageBoxW.assert_called_once()
        msg = _msg_text(mock_windll)
        # Skipped bucket (letter exhaustion).
        assert "Work" in msg and "no free drive letter" in msg
        # Failed bucket (arrival timeout, soft wording), naming both drives.
        assert "did not appear" in msg
        assert "WB_1.3 (D:)" in msg and "WB_2.x (E:)" in msg


class TestN1Identity:
    """Task 4 coherence guarantee: a single unit reproduces the legacy path."""

    def test_open_single_unit_is_byte_for_byte_legacy_cmd(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """A lone unit spawns the exact legacy open command: no --partition,
        --write before --mountpoint, one window, and NO dialog on success."""
        configure, opened = fanout
        configure([None], ("A", "B", "C"), {"D:\\"})  # single collapsed unit
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["open", "img.hdf"])

        assert len(mock_popen.call_args_list) == 1
        cmd = mock_popen.call_args_list[0][0][0]
        assert "--partition" not in cmd
        # Byte-for-byte legacy open tail.
        assert cmd[-6:] == [
            "mount", "--write", "--mountpoint", "D:", "--daemon", "img.hdf",
        ]
        assert opened == ["D:\\"]              # one window
        mock_windll.user32.MessageBoxW.assert_not_called()  # no dialog on success

    def test_mountro_single_unit_is_byte_for_byte_legacy_cmd(
        self, mock_popen, mock_windll, mock_exit, fanout
    ):
        """A lone unit on mountro spawns the legacy read-only command and opens
        no window."""
        configure, opened = fanout
        configure([None], ("A", "B", "C"), {"D:\\"})
        mock_windll.kernel32.OpenMutexW.return_value = 1

        from amifuse.launcher import main
        main(["mount", "img.hdf"])

        assert len(mock_popen.call_args_list) == 1
        cmd = mock_popen.call_args_list[0][0][0]
        assert "--partition" not in cmd
        assert "--write" not in cmd
        assert cmd[-5:] == [
            "mount", "--mountpoint", "D:", "--daemon", "img.hdf",
        ]
        assert opened == []


class TestAggregateTimeout:
    """Task 4: the min(15 + 5*(n-1), 45) aggregate deadline formula."""

    def test_n1_stays_at_legacy_15s(self):
        """N=1 keeps the unchanged single-partition 15s budget."""
        from amifuse.launcher import _aggregate_timeout, MOUNT_POLL_TIMEOUT
        assert _aggregate_timeout(1) == 15.0 == MOUNT_POLL_TIMEOUT

    def test_scales_5s_per_extra_partition(self):
        """Each extra concurrent partition adds 5s of cold-init headroom."""
        from amifuse.launcher import _aggregate_timeout
        assert _aggregate_timeout(2) == 20.0
        assert _aggregate_timeout(3) == 25.0

    def test_capped_at_45s(self):
        """The budget caps at 45s no matter how many partitions."""
        from amifuse.launcher import _aggregate_timeout
        assert _aggregate_timeout(10) == 45.0
        assert _aggregate_timeout(100) == 45.0

    def test_zero_spawns_is_harmless(self):
        """n<=1 (incl. 0 spawned) yields the base budget, no negative/odd value."""
        from amifuse.launcher import _aggregate_timeout
        assert _aggregate_timeout(0) == 15.0


class TestEnumerateMountUnits:
    """Task 4: enumeration order, single-unit collapse, and graceful fallback."""

    @pytest.fixture
    def rdb(self, monkeypatch):
        """Patch the three rdb_inspect enumeration entry points the core uses.

        set(adf=..., iso=..., parts=...) where ``parts`` is either a fake
        PartitionList, an Exception instance to raise, or a MagicMock. Fakes use
        SimpleNamespace so no real amitools objects are constructed.
        """
        from types import SimpleNamespace

        def _plist(names, fallback_reason=None):
            return SimpleNamespace(
                partitions=[SimpleNamespace(name=n) for n in names],
                fallback_reason=fallback_reason,
            )

        def _set(adf=None, iso=None, parts=None):
            monkeypatch.setattr("amifuse.rdb_inspect.detect_adf", lambda img: adf)
            monkeypatch.setattr("amifuse.rdb_inspect.detect_iso", lambda img: iso)
            if isinstance(parts, Exception):
                def _raise(img, *a, **k):
                    raise parts
                monkeypatch.setattr("amifuse.rdb_inspect.list_partitions", _raise)
            elif parts is not None:
                monkeypatch.setattr(
                    "amifuse.rdb_inspect.list_partitions", lambda img, *a, **k: parts
                )
            return SimpleNamespace(plist=_plist)

        return _set

    def test_adf_short_circuits_before_list_partitions(self, rdb, monkeypatch):
        """detect_adf non-None -> single unit, list_partitions never called."""
        list_parts = MagicMock()
        rdb(adf=object(), iso=None)
        monkeypatch.setattr("amifuse.rdb_inspect.list_partitions", list_parts)

        from amifuse.launcher import _enumerate_mount_units
        units, fallback = _enumerate_mount_units("floppy.adf")

        assert len(units) == 1 and units[0].name is None
        assert fallback is None
        list_parts.assert_not_called()

    def test_iso_short_circuits_before_list_partitions(self, rdb, monkeypatch):
        """detect_iso non-None -> single unit, list_partitions never called."""
        list_parts = MagicMock()
        rdb(adf=None, iso=object())
        monkeypatch.setattr("amifuse.rdb_inspect.list_partitions", list_parts)

        from amifuse.launcher import _enumerate_mount_units
        units, fallback = _enumerate_mount_units("cd.iso")

        assert len(units) == 1 and units[0].name is None
        assert fallback is None
        list_parts.assert_not_called()

    def test_single_partition_rdb_collapses_to_no_partition(self, rdb):
        """A single-partition RDB collapses to a no---partition unit (byte-for-byte
        legacy) while keeping the partition name as the dialog label."""
        helper = rdb(adf=None, iso=None)
        rdb(adf=None, iso=None, parts=helper.plist(["WB_1.3"]))

        from amifuse.launcher import _enumerate_mount_units
        units, fallback = _enumerate_mount_units("single.hdf")

        assert len(units) == 1
        assert units[0].name is None        # collapsed -> omit --partition
        assert units[0].label == "WB_1.3"   # label preserved for any dialog
        assert fallback is None

    def test_multi_partition_rdb_keys_by_name(self, rdb):
        """A multi-partition RDB emits one named unit per partition, in order."""
        helper = rdb(adf=None, iso=None)
        rdb(adf=None, iso=None,
            parts=helper.plist(["WB_1.3", "WB_2.x", "Work"]))

        from amifuse.launcher import _enumerate_mount_units
        units, fallback = _enumerate_mount_units("multi.hdf")

        assert [u.name for u in units] == ["WB_1.3", "WB_2.x", "Work"]
        assert fallback is None

    def test_duplicate_name_fallback_returns_reason_and_single_unit(self, rdb):
        """The duplicate-name fallback (partitions == [first], fallback_reason set)
        collapses to one no---partition unit and propagates the reason."""
        helper = rdb(adf=None, iso=None)
        reason = "multiple RDBs with colliding partition names ('DH0'); mounted first only"
        rdb(adf=None, iso=None,
            parts=helper.plist(["WB_1.3"], fallback_reason=reason))

        from amifuse.launcher import _enumerate_mount_units
        units, fallback = _enumerate_mount_units("colliding.hdf")

        assert len(units) == 1 and units[0].name is None
        assert fallback == reason

    def test_enumeration_error_falls_back_to_single_unit(self, rdb):
        """An IOError from list_partitions (non-RDB / unreadable) degrades to a
        single no---partition unit -- the launcher never crashes on enumeration."""
        rdb(adf=None, iso=None, parts=IOError("not an RDB image"))

        from amifuse.launcher import _enumerate_mount_units
        units, fallback = _enumerate_mount_units("bogus.hdf")

        assert len(units) == 1 and units[0].name is None
        assert units[0].label == "disk image"
        assert fallback is None
