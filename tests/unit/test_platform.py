"""Unit tests for amifuse.platform module.

All functions in platform.py branch on sys.platform -- we use monkeypatch to
control the platform string. Functions that import from icon_darwin need
careful handling: icon_darwin is pure Python (no OS-specific C extensions)
and can be imported on all platforms, so we import it directly in Darwin tests.

Mock targets are patched at the module level where they are looked up:
    - amifuse.platform.os.path.exists
    - amifuse.platform.shutil.which
"""

import errno
import sys
import types
from pathlib import Path, PurePosixPath

import pytest


# ---------------------------------------------------------------------------
# A. get_default_mountpoint() -- 3 tests
# ---------------------------------------------------------------------------


class TestGetDefaultMountpoint:
    """Tests for get_default_mountpoint(volname)."""

    def test_default_mountpoint_darwin(self, monkeypatch):
        """On macOS, returns /Volumes/{volname}."""
        monkeypatch.setattr("sys.platform", "darwin")
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result == Path("/Volumes/TestVol")

    def test_default_mountpoint_linux(self, monkeypatch):
        """On Linux, returns None (explicit mountpoint required)."""
        monkeypatch.setattr("sys.platform", "linux")
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result is None

    def test_default_mountpoint_windows(self, monkeypatch):
        """On Windows, returns first available drive letter as Path.

        Mocks os.path.exists at the module level to simulate D: being in use
        and E: being available.
        """
        monkeypatch.setattr("sys.platform", "win32")

        def fake_exists(path):
            # D: is taken, E: is free
            return path == "D:"

        monkeypatch.setattr("amifuse.platform.os.path.exists", fake_exists)
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result == Path("E:")


# ---------------------------------------------------------------------------
# B. should_auto_create_mountpoint() -- 3 tests
# ---------------------------------------------------------------------------


class TestShouldAutoCreateMountpoint:
    """Tests for should_auto_create_mountpoint(mountpoint)."""

    def test_auto_create_darwin_volumes(self, monkeypatch):
        """macOS with /Volumes/X path returns True (macFUSE creates it).

        Uses PurePosixPath to avoid Windows path normalization converting
        forward slashes to backslashes (which would break the startswith check).
        """
        monkeypatch.setattr("sys.platform", "darwin")
        from amifuse.platform import should_auto_create_mountpoint

        assert should_auto_create_mountpoint(PurePosixPath("/Volumes/MyDisk")) is True

    def test_auto_create_linux(self, monkeypatch):
        """Linux returns False for any path."""
        monkeypatch.setattr("sys.platform", "linux")
        from amifuse.platform import should_auto_create_mountpoint

        assert should_auto_create_mountpoint(PurePosixPath("/mnt/amiga")) is False

    def test_auto_create_windows(self, monkeypatch):
        """Windows returns True for drive letter mountpoints (WinFSP creates them)."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import should_auto_create_mountpoint

        assert should_auto_create_mountpoint(Path("D:")) is True

    def test_auto_create_windows_drive_letter(self, monkeypatch):
        """Windows drive letter (E:) returns True."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import should_auto_create_mountpoint

        assert should_auto_create_mountpoint(Path("E:")) is True

    def test_auto_create_windows_directory_path(self, monkeypatch):
        r"""Windows directory path (C:\mnt\amiga) returns False (needs mkdir)."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import should_auto_create_mountpoint

        assert should_auto_create_mountpoint(Path(r"C:\mnt\amiga")) is False


# ---------------------------------------------------------------------------
# C. get_unmount_command() -- 3 tests
# ---------------------------------------------------------------------------


class TestGetUnmountCommand:
    """Tests for get_unmount_command(mountpoint)."""

    def test_unmount_command_darwin(self, monkeypatch):
        """macOS returns ['umount', '-f', path].

        Uses PurePosixPath to avoid Windows path normalization.
        """
        monkeypatch.setattr("sys.platform", "darwin")
        from amifuse.platform import get_unmount_command

        mp = PurePosixPath("/Volumes/TestVol")
        result = get_unmount_command(mp)
        assert result == ["umount", "-f", "/Volumes/TestVol"]

    def test_unmount_command_linux_fusermount(self, monkeypatch):
        """Linux with fusermount available returns ['fusermount', '-u', path].

        Mocks shutil.which at amifuse.platform module level to simulate
        fusermount being installed. Uses PurePosixPath to avoid Windows
        path normalization.
        """
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(
            "amifuse.platform.shutil.which",
            lambda cmd: "/usr/bin/fusermount" if cmd == "fusermount" else None,
        )
        from amifuse.platform import get_unmount_command

        mp = PurePosixPath("/mnt/amiga")
        result = get_unmount_command(mp)
        assert result == ["fusermount", "-u", "/mnt/amiga"]

    def test_unmount_command_linux_no_fusermount(self, monkeypatch):
        """Linux without fusermount falls back to ['umount', '-f', path].

        Mocks shutil.which returning None to simulate fusermount not installed.
        Uses PurePosixPath to avoid Windows path normalization.
        """
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(
            "amifuse.platform.shutil.which",
            lambda cmd: None,
        )
        from amifuse.platform import get_unmount_command

        mp = PurePosixPath("/mnt/amiga")
        result = get_unmount_command(mp)
        assert result == ["umount", "-f", "/mnt/amiga"]

    def test_unmount_command_windows_drive_letter(self, monkeypatch):
        """On Windows, drive-letter mounts return empty (process kill path)."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import get_unmount_command

        result = get_unmount_command(Path("D:"))
        assert result == []

    def test_unmount_command_windows_non_drive_returns_empty(self, monkeypatch):
        """On Windows, non-drive-letter mounts have no standalone unmount CLI."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import get_unmount_command

        result = get_unmount_command(Path(r"C:\mnt\amiga"))
        assert result == []


# ---------------------------------------------------------------------------
# D. get_mount_options() -- 4 tests
# ---------------------------------------------------------------------------


class TestGetMountOptions:
    """Tests for get_mount_options(volname, ...)."""

    def test_mount_options_linux_empty(self, monkeypatch):
        """Non-macOS platforms return empty dict (no special mount options)."""
        monkeypatch.setattr("sys.platform", "linux")
        from amifuse.platform import get_mount_options

        result = get_mount_options("TestVol")
        assert result == {}

    def test_mount_options_windows_volname(self, monkeypatch):
        """On Windows, returns dict with volname and FileSystemName."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import get_mount_options

        result = get_mount_options("TestVol")
        assert result == {"volname": "TestVol", "FileSystemName": "AmiFUSE"}

    def test_mount_options_windows_ignores_icon_args(self, monkeypatch):
        """On Windows, icon args are ignored (macOS-only)."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import get_mount_options

        result = get_mount_options(
            "TestVol", volicon_path="/tmp/icon.icns", icons_enabled=True
        )
        assert result == {"volname": "TestVol", "FileSystemName": "AmiFUSE"}

    def test_mount_options_darwin_unchanged(self, monkeypatch):
        """On darwin, get_mount_options returns a dict containing 'volname' key.

        Verifies the macOS code path is unchanged by the Windows additions.
        """
        monkeypatch.setattr("sys.platform", "darwin")
        from amifuse.platform import get_mount_options

        result = get_mount_options("TestVol")
        assert "volname" in result

    def test_mount_options_darwin_keeps_xattrs_enabled(self, monkeypatch):
        """On darwin, writable copies should not be blocked by noapplexattr."""
        monkeypatch.setattr("sys.platform", "darwin")
        from amifuse.platform import get_mount_options

        result = get_mount_options("TestVol")
        assert result["volname"] == "TestVol"
        assert result["noappledouble"] is True
        assert "noapplexattr" not in result


# ---------------------------------------------------------------------------
# E. supports_icons() and get_icon_handler() -- 2 tests
# ---------------------------------------------------------------------------


class TestIconSupport:
    """Tests for supports_icons() and get_icon_handler()."""

    @pytest.mark.parametrize(
        "platform,expected",
        [
            ("darwin", True),
            ("linux", False),
            ("win32", False),
        ],
    )
    def test_supports_icons_darwin_only(self, monkeypatch, platform, expected):
        """supports_icons() returns True only on darwin."""
        monkeypatch.setattr("sys.platform", platform)
        from amifuse.platform import supports_icons

        assert supports_icons() is expected

    def test_icon_handler_disabled(self, monkeypatch):
        """get_icon_handler with icons_enabled=False returns None on any platform."""
        for platform in ("darwin", "linux", "win32"):
            monkeypatch.setattr("sys.platform", platform)
            from amifuse.platform import get_icon_handler

            result = get_icon_handler(icons_enabled=False)
            assert result is None, f"Expected None on {platform} with icons disabled"


# ---------------------------------------------------------------------------
# F. get_icon_file_names() -- 2 tests
# ---------------------------------------------------------------------------


class TestGetIconFileNames:
    """Tests for get_icon_file_names()."""

    def test_icon_file_names_darwin(self, monkeypatch):
        """On darwin, returns tuple of icon file name constants from icon_darwin."""
        monkeypatch.setattr("sys.platform", "darwin")
        from amifuse.platform import get_icon_file_names

        result = get_icon_file_names()
        assert isinstance(result, tuple)
        assert len(result) == 2
        # Verify against the actual constants from icon_darwin
        from amifuse.icon_darwin import ICON_FILE, VOLUME_ICON_FILE

        assert result == (ICON_FILE, VOLUME_ICON_FILE)
        # Sanity check the values
        assert result[0] == "Icon\r"
        assert result[1] == ".VolumeIcon.icns"

    @pytest.mark.parametrize("platform", ["linux", "win32"])
    def test_icon_file_names_non_darwin(self, monkeypatch, platform):
        """On non-darwin platforms, returns (None, None)."""
        monkeypatch.setattr("sys.platform", platform)
        from amifuse.platform import get_icon_file_names

        result = get_icon_file_names()
        assert result == (None, None)


# ---------------------------------------------------------------------------
# G. check_fuse_available() -- 7 tests
# ---------------------------------------------------------------------------


class _FakeWinreg:
    """Fake winreg module for testing on non-Windows platforms.

    winreg is a Windows-only stdlib module. When monkeypatching sys.platform
    to 'win32' on macOS/Linux, `import winreg` inside check_fuse_available()
    would fail. This fake module provides the minimal interface needed by the
    function: OpenKey, QueryValueEx, and HKEY_LOCAL_MACHINE.
    """

    HKEY_LOCAL_MACHINE = 0x80000002

    def __init__(self, install_dir=None, raise_on_open=False):
        """Configure fake registry behavior.

        Args:
            install_dir: Value to return from QueryValueEx, or None
            raise_on_open: If True, OpenKey raises OSError (key not found)
        """
        self._install_dir = install_dir
        self._raise_on_open = raise_on_open

    def OpenKey(self, hkey, sub_key):
        if self._raise_on_open:
            raise OSError("Registry key not found")
        return self

    def QueryValueEx(self, key, value_name):
        return (self._install_dir, 1)  # (value, type)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture
def fake_winreg_module():
    """Create a fake winreg module suitable for monkeypatch.setitem(sys.modules, ...).

    Returns a factory function that accepts configuration and returns a
    properly set up fake winreg module.
    """
    def _make(install_dir=None, raise_on_open=False):
        mod = types.ModuleType("winreg")
        fake = _FakeWinreg(install_dir=install_dir, raise_on_open=raise_on_open)
        mod.HKEY_LOCAL_MACHINE = _FakeWinreg.HKEY_LOCAL_MACHINE
        mod.OpenKey = fake.OpenKey
        mod.QueryValueEx = fake.QueryValueEx
        return mod
    return _make


class TestCheckFuseAvailable:
    """Tests for check_fuse_available().

    This function checks for WinFSP installation on Windows and is a no-op
    on macOS and Linux. On non-Windows test platforms, we inject a fake
    winreg module via sys.modules since winreg only exists on Windows.
    """

    def test_check_fuse_noop_on_darwin(self, monkeypatch):
        """On macOS, check_fuse_available() returns None (no-op)."""
        monkeypatch.setattr("sys.platform", "darwin")
        from amifuse.platform import check_fuse_available

        result = check_fuse_available()
        assert result is None

    def test_check_fuse_noop_on_linux(self, monkeypatch):
        """On Linux, check_fuse_available() returns None (no-op)."""
        monkeypatch.setattr("sys.platform", "linux")
        from amifuse.platform import check_fuse_available

        result = check_fuse_available()
        assert result is None

    def test_check_fuse_windows_registry(self, monkeypatch, fake_winreg_module):
        """On Windows, finding WinFSP via registry succeeds without error."""
        monkeypatch.setattr("sys.platform", "win32")

        # Inject fake winreg with a valid install dir
        fake_mod = fake_winreg_module(install_dir=r"C:\Program Files (x86)\WinFsp")
        monkeypatch.setitem(sys.modules, "winreg", fake_mod)

        # Mock os.path.isdir to confirm the directory exists
        monkeypatch.setattr(
            "amifuse.platform.os.path.isdir",
            lambda path: path == r"C:\Program Files (x86)\WinFsp",
        )

        from amifuse.platform import check_fuse_available

        # Should not raise
        result = check_fuse_available()
        assert result is None

    def test_check_fuse_windows_env_var(self, monkeypatch, fake_winreg_module):
        """On Windows, falls back to WINFSP_INSTALL_DIR env var when registry fails."""
        monkeypatch.setattr("sys.platform", "win32")

        # Registry key not found
        fake_mod = fake_winreg_module(raise_on_open=True)
        monkeypatch.setitem(sys.modules, "winreg", fake_mod)

        # Set env var
        monkeypatch.setenv("WINFSP_INSTALL_DIR", r"D:\Tools\WinFsp")

        # Only the env var path is valid
        monkeypatch.setattr(
            "amifuse.platform.os.path.isdir",
            lambda path: path == r"D:\Tools\WinFsp",
        )

        from amifuse.platform import check_fuse_available

        # Should not raise
        result = check_fuse_available()
        assert result is None

    def test_check_fuse_windows_default_dir(self, monkeypatch, fake_winreg_module):
        """On Windows, falls back to default install path when registry and env var fail."""
        monkeypatch.setattr("sys.platform", "win32")

        # Registry key not found
        fake_mod = fake_winreg_module(raise_on_open=True)
        monkeypatch.setitem(sys.modules, "winreg", fake_mod)

        # No env var
        monkeypatch.delenv("WINFSP_INSTALL_DIR", raising=False)

        # Only default dir exists
        monkeypatch.setattr(
            "amifuse.platform.os.path.isdir",
            lambda path: path == r"C:\Program Files (x86)\WinFsp",
        )

        from amifuse.platform import check_fuse_available

        # Should not raise
        result = check_fuse_available()
        assert result is None

    def test_check_fuse_windows_not_installed(self, monkeypatch, fake_winreg_module):
        """On Windows, raises SystemExit when WinFSP is not found anywhere."""
        monkeypatch.setattr("sys.platform", "win32")

        # Registry key not found
        fake_mod = fake_winreg_module(raise_on_open=True)
        monkeypatch.setitem(sys.modules, "winreg", fake_mod)

        # No env var
        monkeypatch.delenv("WINFSP_INSTALL_DIR", raising=False)

        # No directories exist
        monkeypatch.setattr("amifuse.platform.os.path.isdir", lambda path: False)

        from amifuse.platform import check_fuse_available

        with pytest.raises(SystemExit) as exc_info:
            check_fuse_available()

        msg = str(exc_info.value)
        assert "WinFSP is not installed" in msg
        assert "https://winfsp.dev" in msg

    def test_check_fuse_windows_error_message_actionable(
        self, monkeypatch, fake_winreg_module
    ):
        """Error message contains install URL, restart hint, and env var fallback."""
        monkeypatch.setattr("sys.platform", "win32")

        # Registry key not found
        fake_mod = fake_winreg_module(raise_on_open=True)
        monkeypatch.setitem(sys.modules, "winreg", fake_mod)

        # No env var
        monkeypatch.delenv("WINFSP_INSTALL_DIR", raising=False)

        # No directories exist
        monkeypatch.setattr("amifuse.platform.os.path.isdir", lambda path: False)

        from amifuse.platform import check_fuse_available

        with pytest.raises(SystemExit) as exc_info:
            check_fuse_available()

        msg = str(exc_info.value)
        # Verify all three actionable elements
        assert "https://winfsp.dev/rel/" in msg
        assert "restart your terminal" in msg.lower() or "Restart your terminal" in msg
        assert "WINFSP_INSTALL_DIR" in msg


# ---------------------------------------------------------------------------
# H. validate_mountpoint() -- 7 tests
# ---------------------------------------------------------------------------


class TestValidateMountpoint:
    """Tests for validate_mountpoint().

    Uses os.path.exists and os.path.ismount (string-based) rather than
    Path.exists() for testability across platforms. All mocks target
    amifuse.platform.os.path.* to match the module-level lookup.
    """

    def test_validate_drive_letter_available(self, monkeypatch):
        r"""On Windows, D: with D:\ not existing returns None (available)."""
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: False,
        )
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(Path("D:"))
        assert result is None

    def test_validate_drive_letter_in_use(self, monkeypatch):
        r"""On Windows, D: with D:\ existing returns error string."""
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: path == "D:\\",
        )
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(Path("D:"))
        assert result is not None
        assert "already in use" in result

    def test_validate_unix_mountpoint_available(self, monkeypatch):
        """On Linux, path that doesn't exist returns None (available)."""
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: False,
        )
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(PurePosixPath("/mnt/amiga"))
        assert result is None

    def test_validate_unix_mountpoint_mounted(self, monkeypatch):
        """On Linux, path that exists and is a mount returns amifuse unmount hint."""
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: True,
        )
        monkeypatch.setattr(
            "amifuse.platform.os.path.ismount",
            lambda path: True,
        )
        monkeypatch.setattr(
            "amifuse.platform.shutil.which",
            lambda cmd: "/usr/bin/fusermount" if cmd == "fusermount" else None,
        )
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(PurePosixPath("/mnt/amiga"))
        assert result is not None
        assert "already a mount" in result
        assert "amifuse unmount /mnt/amiga" in result

    def test_validate_unix_mountpoint_exists_not_mounted(self, monkeypatch):
        """On Linux, path that exists but is not a mount returns None (fine to use)."""
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: True,
        )
        monkeypatch.setattr(
            "amifuse.platform.os.path.ismount",
            lambda path: False,
        )
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(PurePosixPath("/mnt/amiga"))
        assert result is None

    def test_validate_windows_dir_mountpoint_mounted(self, monkeypatch):
        r"""On Windows, non-drive-letter path (C:\mnt\amiga) that is mounted returns error."""
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: True,
        )
        monkeypatch.setattr(
            "amifuse.platform.os.path.ismount",
            lambda path: True,
        )
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(Path(r"C:\mnt\amiga"))
        assert result is not None
        assert "already a mount" in result

    def test_validate_windows_dir_mountpoint_available(self, monkeypatch):
        r"""On Windows, non-drive-letter path (C:\mnt\amiga) that doesn't exist returns None."""
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: False,
        )
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(Path(r"C:\mnt\amiga"))
        assert result is None

    def test_validate_unix_mountpoint_stale_inaccessible(self, monkeypatch):
        """On Unix, EIO from lstat returns a stale-mount error."""
        monkeypatch.setattr("sys.platform", "darwin")

        def fake_lstat(path):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr("amifuse.platform.os.lstat", fake_lstat)
        monkeypatch.setattr(
            "amifuse.platform.shutil.which",
            lambda cmd: "/sbin/umount" if cmd == "umount" else None,
        )
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(PurePosixPath("/mnt/amiga"))
        assert result is not None
        assert "stale or broken mount" in result


# ---------------------------------------------------------------------------
# I. Windows mountpoint edge cases -- 3 tests
# ---------------------------------------------------------------------------


class TestWindowsMountpointEdgeCases:
    """Tests for get_default_mountpoint() Windows edge cases.

    Verifies drive letter exhaustion, priority ordering, and the first-available
    logic in get_default_mountpoint() on Windows.
    """

    def test_default_mountpoint_windows_all_taken(self, monkeypatch):
        """On Windows, all D-Z drive letters taken returns None."""
        monkeypatch.setattr("sys.platform", "win32")
        # All drive letters exist (are in use)
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: True,
        )
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result is None

    def test_default_mountpoint_windows_first_available(self, monkeypatch):
        """On Windows, D-F taken and G free returns Path('G:')."""
        monkeypatch.setattr("sys.platform", "win32")
        taken = {"D:", "E:", "F:"}
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: path in taken,
        )
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result == Path("G:")

    def test_default_mountpoint_windows_d_available(self, monkeypatch):
        """On Windows, all drives free returns Path('D:') (first checked)."""
        monkeypatch.setattr("sys.platform", "win32")
        monkeypatch.setattr(
            "amifuse.platform.os.path.exists",
            lambda path: False,
        )
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result == Path("D:")


# ---------------------------------------------------------------------------
# J. mount_runs_in_foreground_by_default() -- 3 tests
# ---------------------------------------------------------------------------


class TestMountRunsInForegroundByDefault:
    """Tests for mount_runs_in_foreground_by_default()."""

    @pytest.mark.parametrize(
        "platform,expected",
        [
            ("darwin", False),
            ("linux", False),
            ("win32", True),
        ],
    )
    def test_default_mode_by_platform(self, monkeypatch, platform, expected):
        monkeypatch.setattr("sys.platform", platform)
        from amifuse.platform import mount_runs_in_foreground_by_default

        assert mount_runs_in_foreground_by_default() is expected


# ---------------------------------------------------------------------------
# K. Windows unmount command tests -- 3 tests
# ---------------------------------------------------------------------------


class TestWindowsUnmountCommand:
    """Tests for _get_windows_unmount_command() and get_unmount_command() on Windows."""

    def test_drive_letter_returns_empty(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import _get_windows_unmount_command

        result = _get_windows_unmount_command(Path("Z:"))
        assert result == []

    def test_non_drive_letter_returns_empty(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import _get_windows_unmount_command

        result = _get_windows_unmount_command(Path(r"C:\mnt\amiga"))
        assert result == []

    def test_get_unmount_command_windows_drive_letter(self, monkeypatch):
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import get_unmount_command

        result = get_unmount_command(Path("Z:"))
        assert result == []
