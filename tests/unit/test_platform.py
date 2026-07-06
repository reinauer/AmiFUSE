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
import signal
import sys
import types
from pathlib import Path, PurePosixPath

import pytest


# ---------------------------------------------------------------------------
# Helpers for mocking the Windows GetLogicalDrives bitmask deterministically.
#
# The drive-letter probe now uses ctypes.windll.kernel32.GetLogicalDrives()
# instead of os.path.exists(). These helpers install a fake windll so the
# Windows drive-letter tests never depend on the real machine's drive state.
# ---------------------------------------------------------------------------


def _mask_for(letters):
    """Build a GetLogicalDrives-style bitmask for the given letters.

    Bit 0 = A:, bit 1 = B:, ... bit 25 = Z:.
    """
    mask = 0
    for ch in letters:
        mask |= 1 << (ord(ch.upper()) - ord("A"))
    return mask


def _install_fake_get_logical_drives(monkeypatch, mask, calls=None):
    """Patch ctypes.windll.kernel32.GetLogicalDrives to return a fixed mask.

    Works cross-platform: on non-Windows, ctypes has no ``windll`` attribute,
    so we add it with raising=False. When ``calls`` is provided, each call
    appends to it, letting a test assert the API was (or was not) invoked.
    """
    def get_logical_drives():
        if calls is not None:
            calls.append(True)
        return mask

    kernel32 = types.SimpleNamespace(GetLogicalDrives=get_logical_drives)
    fake_windll = types.SimpleNamespace(kernel32=kernel32)
    monkeypatch.setattr("ctypes.windll", fake_windll, raising=False)


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
        """On Windows, returns first unallocated drive letter as Path.

        Mocks GetLogicalDrives so C: and D: are allocated and E: is free.
        """
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, _mask_for("CD"))
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

    def test_unmount_command_linux_fusermount3_fallback(self, monkeypatch):
        """When fusermount is absent but fusermount3 exists, use fusermount3.

        Uses PurePosixPath to avoid Windows path normalization.
        """
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(
            "amifuse.platform.shutil.which",
            lambda cmd: "/usr/bin/fusermount3" if cmd == "fusermount3" else None,
        )
        from amifuse.platform import get_unmount_command

        mp = PurePosixPath("/mnt/amiga")
        result = get_unmount_command(mp)
        assert result == ["fusermount3", "-u", "/mnt/amiga"]

    def test_unmount_command_linux_prefers_fusermount(self, monkeypatch):
        """When both fusermount and fusermount3 exist, prefer fusermount.

        Uses PurePosixPath to avoid Windows path normalization.
        """
        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(
            "amifuse.platform.shutil.which",
            lambda cmd: f"/usr/bin/{cmd}" if cmd in ("fusermount", "fusermount3") else None,
        )
        from amifuse.platform import get_unmount_command

        mp = PurePosixPath("/mnt/amiga")
        result = get_unmount_command(mp)
        assert result == ["fusermount", "-u", "/mnt/amiga"]

    def test_unmount_command_linux_no_fusermount(self, monkeypatch):
        """Linux without fusermount or fusermount3 falls back to ['umount', '-f', path].

        Mocks shutil.which returning None to simulate neither installed.
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
        """On Windows, returns dict with volname, FileSystemName, and FileInfoTimeout."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import get_mount_options

        result = get_mount_options("TestVol")
        assert result == {"volname": "TestVol", "FileSystemName": "AmiFUSE", "FileInfoTimeout": "1000"}

    def test_mount_options_windows_ignores_icon_args(self, monkeypatch):
        """On Windows, icon args are ignored (macOS-only)."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import get_mount_options

        result = get_mount_options(
            "TestVol", volicon_path="/tmp/icon.icns", icons_enabled=True
        )
        assert result == {"volname": "TestVol", "FileSystemName": "AmiFUSE", "FileInfoTimeout": "1000"}

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
        """On Windows, D: unallocated returns None (available)."""
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, _mask_for("C"))
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(Path("D:"))
        assert result is None

    def test_validate_drive_letter_in_use(self, monkeypatch):
        """On Windows, an allocated D: returns an 'allocated' error string."""
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, _mask_for("CD"))
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(Path("D:"))
        assert result is not None
        assert "already allocated" in result

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
        """On Windows, all A-Z drive letters allocated returns None."""
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(
            monkeypatch, _mask_for("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        )
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result is None

    def test_default_mountpoint_windows_first_available(self, monkeypatch):
        """On Windows, C-F allocated and G free returns Path('G:')."""
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, _mask_for("CDEF"))
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result == Path("G:")

    def test_default_mountpoint_windows_d_available(self, monkeypatch):
        """On Windows, only C allocated returns Path('D:') (first checked)."""
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, _mask_for("C"))
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result == Path("D:")


# ---------------------------------------------------------------------------
# I2. _windows_allocated_drive_letters() and the GetLogicalDrives rewire
# ---------------------------------------------------------------------------


class TestWindowsAllocatedDriveLetters:
    """Tests for the GetLogicalDrives-based drive-letter probe (Task 1).

    Covers the helper's decode contract, the empirical oracle from the
    investigation, the conservative zero/failure fallback, and the
    cross-platform guard that keeps windll untouched off Windows.
    """

    def test_decode_returns_bare_uppercase_letters(self, monkeypatch):
        """Helper returns bare uppercase single letters (e.g. {'C', 'D'})."""
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, _mask_for("CD"))
        from amifuse.platform import _windows_allocated_drive_letters

        assert _windows_allocated_drive_letters() == {"C", "D"}

    def test_empirical_oracle_selects_I(self, monkeypatch):
        """Assigned C,D,E,F,G,H,S-Z -> get_default_mountpoint returns I:.

        This is the empirical oracle captured on the investigation machine:
        D-H are empty removable card-reader slots (allocated but mediumless)
        and S-Z are network drives. The old os.path.exists probe false-selected
        the empty D:; the bitmask probe skips all allocated letters and lands on
        the first genuinely-free letter, I:.
        """
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(
            monkeypatch, _mask_for("CDEFGHSTUVWXYZ")
        )
        from amifuse.platform import get_default_mountpoint

        assert get_default_mountpoint("TestVol") == Path("I:")

    def test_zero_return_does_not_resurrect_D_bug(self, monkeypatch):
        """GetLogicalDrives() -> 0 (API failure) must not re-select D:.

        A naive decode of 0 yields an empty set, making every letter look free
        and regressing to the empty D:. The conservative fallback treats every
        letter as allocated, so auto-selection declines rather than picking D:.
        """
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, 0)
        from amifuse.platform import get_default_mountpoint

        result = get_default_mountpoint("TestVol")
        assert result != Path("D:")
        assert result is None

    def test_zero_return_all_letters_allocated(self, monkeypatch):
        """On a 0 return the helper reports every letter as allocated."""
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, 0)
        from amifuse.platform import _windows_allocated_drive_letters

        assert _windows_allocated_drive_letters() == set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )

    def test_exception_falls_back_to_all_allocated(self, monkeypatch):
        """If the windll call raises, fall back to treating all as allocated."""
        monkeypatch.setattr("sys.platform", "win32")

        class _ExplodingWinDLL:
            def __getattr__(self, name):
                raise OSError("simulated GetLogicalDrives failure")

        monkeypatch.setattr("ctypes.windll", _ExplodingWinDLL(), raising=False)
        from amifuse.platform import _windows_allocated_drive_letters

        assert _windows_allocated_drive_letters() == set(
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )

    def test_guard_no_windll_on_non_windows(self, monkeypatch):
        """On non-Windows the helper returns set() without touching windll.

        The GetLogicalDrives sentinel raises if any attribute is accessed. A
        broken guard would enter the try-body, hit the sentinel (caught by the
        except -> all-allocated set), and the empty-set assertion would fail.
        """
        monkeypatch.setattr("sys.platform", "linux")

        class _ExplodingWinDLL:
            def __getattr__(self, name):
                raise AssertionError(f"windll.{name} accessed on non-Windows")

        monkeypatch.setattr("ctypes.windll", _ExplodingWinDLL(), raising=False)
        from amifuse.platform import _windows_allocated_drive_letters

        assert _windows_allocated_drive_letters() == set()

    def test_validate_lowercase_letter_normalized(self, monkeypatch):
        """An explicit lowercase 'd:' is still caught when D: is allocated."""
        monkeypatch.setattr("sys.platform", "win32")
        _install_fake_get_logical_drives(monkeypatch, _mask_for("CD"))
        from amifuse.platform import validate_mountpoint

        result = validate_mountpoint(Path("d:"))
        assert result is not None
        assert "already allocated" in result


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


# ---------------------------------------------------------------------------
# L. _pid_exists -- 4 tests
# ---------------------------------------------------------------------------


class TestPidExists:
    """Tests for _pid_exists() cross-platform behaviour."""

    def test_pid_exists_true_for_live_pid(self, monkeypatch):
        from amifuse.platform import _pid_exists

        monkeypatch.setattr("amifuse.platform.sys.platform", "linux")
        monkeypatch.setattr("amifuse.platform.os.kill", lambda pid, sig: None)
        assert _pid_exists(12345) is True

    def test_pid_exists_false_for_dead_pid(self, monkeypatch):
        from amifuse.platform import _pid_exists

        monkeypatch.setattr("amifuse.platform.sys.platform", "linux")

        def raise_lookup(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr("amifuse.platform.os.kill", raise_lookup)
        assert _pid_exists(12345) is False

    def test_pid_exists_true_on_permission_error(self, monkeypatch):
        from amifuse.platform import _pid_exists

        monkeypatch.setattr("amifuse.platform.sys.platform", "linux")

        def raise_perm(pid, sig):
            raise PermissionError("Access denied")

        monkeypatch.setattr("amifuse.platform.os.kill", raise_perm)
        assert _pid_exists(12345) is True

    def test_pid_exists_false_on_generic_oserror(self, monkeypatch):
        from amifuse.platform import _pid_exists

        monkeypatch.setattr("amifuse.platform.sys.platform", "linux")

        def raise_oserror(pid, sig):
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr("amifuse.platform.os.kill", raise_oserror)
        assert _pid_exists(12345) is False


# ---------------------------------------------------------------------------
# M. _deduplicate_fusepy_children -- 2 tests
# ---------------------------------------------------------------------------


class TestDeduplicateFusepyChildren:
    """Tests for _deduplicate_fusepy_children()."""

    def test_deduplicate_filters_child(self):
        from amifuse.platform import _deduplicate_fusepy_children

        mounts = [
            {"pid": 100, "parent_pid": 1, "mountpoint": "/mnt/a"},
            {"pid": 200, "parent_pid": 100, "mountpoint": None},
        ]
        result = _deduplicate_fusepy_children(mounts)
        assert len(result) == 1
        assert result[0]["pid"] == 100

    def test_deduplicate_keeps_unrelated_processes(self):
        from amifuse.platform import _deduplicate_fusepy_children

        mounts = [
            {"pid": 100, "parent_pid": 1, "mountpoint": "/mnt/a"},
            {"pid": 200, "parent_pid": 1, "mountpoint": "/mnt/b"},
        ]
        result = _deduplicate_fusepy_children(mounts)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# N. kill_pids -- 4 tests
# ---------------------------------------------------------------------------


class TestKillPids:
    """Tests for kill_pids() graceful-then-force strategy."""

    def test_kill_pids_sends_sigterm_on_unix(self, monkeypatch):
        """On Unix, sends SIGTERM then checks pid existence."""
        import signal
        from amifuse.platform import kill_pids

        monkeypatch.setattr("sys.platform", "linux")
        signals_sent = []

        def fake_kill(pid, sig):
            signals_sent.append((pid, sig))
            if sig == 0:
                raise ProcessLookupError()

        monkeypatch.setattr("amifuse.platform.os.kill", fake_kill)

        result = kill_pids([42], timeout=0.1)
        assert 42 in result
        assert (42, signal.SIGTERM) in signals_sent

    @pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="SIGKILL not available on Windows")
    def test_kill_pids_force_kills_with_sigkill_unix(self, monkeypatch):
        """On Unix, sends SIGKILL when pid survives SIGTERM."""
        import signal
        from amifuse.platform import kill_pids, _SIGKILL

        monkeypatch.setattr("sys.platform", "linux")
        signals_sent = []
        alive = {42}

        def fake_kill(pid, sig):
            signals_sent.append((pid, sig))
            if sig == 0:
                if pid in alive:
                    return  # still alive
                raise ProcessLookupError()
            if sig == _SIGKILL:
                alive.discard(pid)

        monkeypatch.setattr("amifuse.platform.os.kill", fake_kill)
        monkeypatch.setattr("amifuse.platform.time.time", lambda: 999999)

        result = kill_pids([42], timeout=0.0)
        assert 42 in result
        assert (42, signal.SIGKILL) in signals_sent

    @pytest.mark.skipif(not hasattr(signal, "CTRL_BREAK_EVENT"), reason="Windows-only signal")
    def test_kill_pids_sends_taskkill_graceful_on_windows(self, monkeypatch):
        """On Windows, uses taskkill /PID (graceful WM_CLOSE) instead of CTRL_BREAK_EVENT."""
        from unittest.mock import MagicMock
        from amifuse.platform import kill_pids

        monkeypatch.setattr("sys.platform", "win32")
        taskkill_calls = []

        def fake_run(cmd, check=False, capture_output=False, creationflags=0):
            taskkill_calls.append(cmd)
            return MagicMock(returncode=0)

        def fake_pid_exists(pid):
            return False

        monkeypatch.setattr("amifuse.platform.subprocess.run", fake_run)
        monkeypatch.setattr("amifuse.platform._pid_exists", fake_pid_exists)
        monkeypatch.setattr("amifuse.platform._verify_amifuse_process", lambda pid: True)

        result = kill_pids([42], timeout=0.1)
        assert 42 in result
        # Phase 1 should call taskkill without /F (graceful)
        assert ["taskkill", "/PID", "42"] in taskkill_calls

    @pytest.mark.skipif(not hasattr(signal, "CTRL_BREAK_EVENT"), reason="Windows-only signal")
    def test_kill_pids_force_kills_with_taskkill(self, monkeypatch):
        """On Windows, uses taskkill /F when pid survives graceful taskkill."""
        import signal
        from unittest.mock import MagicMock
        from amifuse.platform import kill_pids

        monkeypatch.setattr("sys.platform", "win32")
        alive = {42}
        taskkill_called = []

        def fake_run(cmd, check=False, capture_output=False, creationflags=0):
            taskkill_called.append(cmd)
            if "/F" in cmd:
                alive.discard(42)
            return MagicMock(returncode=0)

        def fake_pid_exists(pid):
            return pid in alive

        monkeypatch.setattr("amifuse.platform.subprocess.run", fake_run)
        monkeypatch.setattr("amifuse.platform._pid_exists", fake_pid_exists)
        monkeypatch.setattr("amifuse.platform._verify_amifuse_process", lambda pid: True)
        monkeypatch.setattr("amifuse.platform.time.time", lambda: 999999)

        result = kill_pids([42], timeout=0.0)
        assert 42 in result
        assert any("/F" in cmd for cmd in taskkill_called)


# ---------------------------------------------------------------------------
# O. CIM fallback -- 1 test
# ---------------------------------------------------------------------------


class TestCimFallback:
    """Tests for _find_amifuse_mounts_cim fallback."""

    def test_wmic_oserror_falls_through_to_cim(self, monkeypatch):
        from unittest.mock import MagicMock
        from amifuse.platform import _find_amifuse_mounts_windows

        monkeypatch.setattr("sys.platform", "win32")

        call_count = {"n": 0}

        def fake_run(cmd, **kwargs):
            call_count["n"] += 1
            if cmd[0] == "wmic":
                raise OSError("wmic not found")
            # powershell fallback
            result = MagicMock()
            result.returncode = 0
            result.stdout = b'[]'
            return result

        monkeypatch.setattr("amifuse.platform.subprocess.run", fake_run)

        mounts = _find_amifuse_mounts_windows()
        assert mounts == []
        # Should have tried wmic then PowerShell
        assert call_count["n"] >= 2


# ---------------------------------------------------------------------------
# P. find_mounts wmic uses CREATE_NO_WINDOW -- 1 test
# ---------------------------------------------------------------------------


class TestWmicCreationFlags:
    """Verify wmic subprocess call uses CREATE_NO_WINDOW."""

    def test_find_mounts_wmic_uses_create_no_window(self, monkeypatch):
        from unittest.mock import MagicMock
        from amifuse.platform import _find_amifuse_mounts_windows, _CREATE_NO_WINDOW

        monkeypatch.setattr("sys.platform", "win32")

        captured_kwargs = {}

        def fake_run(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            return result

        monkeypatch.setattr("amifuse.platform.subprocess.run", fake_run)

        _find_amifuse_mounts_windows()
        assert captured_kwargs.get("creationflags") == _CREATE_NO_WINDOW


# ---------------------------------------------------------------------------
# TestNotifyShellDriveChange
# ---------------------------------------------------------------------------


class TestNotifyShellDriveChange:
    """Verify notify_shell_drive_change function."""

    def test_function_exists(self):
        from amifuse.platform import notify_shell_drive_change
        assert callable(notify_shell_drive_change)

    def test_noop_on_non_windows(self, monkeypatch):
        """Does not crash on non-Windows platforms."""
        import amifuse.platform as plat
        monkeypatch.setattr(sys, "platform", "linux")
        # Should return without error
        plat.notify_shell_drive_change("D:", added=True)
        plat.notify_shell_drive_change("D:", added=False)


# ---------------------------------------------------------------------------
# TestParseMountTokensQuoting -- quote-stripping in _parse_mount_tokens
# ---------------------------------------------------------------------------


class TestParseMountTokensQuoting:
    """Verify _parse_mount_tokens strips a matched surrounding quote pair.

    On Windows the command line is tokenized with shlex.split(posix=False),
    which retains the literal quotes that list2cmdline adds around spaced paths.
    The parser must return image/mountpoint without those surrounding quotes,
    while leaving unbalanced input untouched (no crash).
    """

    def test_quoted_spaced_image_and_mountpoint_stripped(self):
        # Tokens as produced by shlex.split(cmdline, posix=False) on Windows:
        # the spaced image path and mountpoint keep their surrounding quotes.
        from amifuse.platform import _parse_mount_tokens

        tokens = [
            "pythonw", "-m", "amifuse", "mount",
            "--mountpoint", "I:",
            "--write", "--daemon",
            '"U:\\thomas\\Test Fixtures\\hd0-ericA3000.hdf"',
        ]
        image, mountpoint = _parse_mount_tokens(tokens)
        assert image == "U:\\thomas\\Test Fixtures\\hd0-ericA3000.hdf"
        assert mountpoint == "I:"
        # No surrounding quotes leaked through.
        assert not image.startswith('"') and not image.endswith('"')

    def test_quoted_spaced_mountpoint_stripped(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = [
            "amifuse", "mount",
            "--mountpoint", '"/Volumes/My Disk"',
            "/path/plain.hdf",
        ]
        image, mountpoint = _parse_mount_tokens(tokens)
        assert mountpoint == "/Volumes/My Disk"
        assert image == "/path/plain.hdf"

    def test_unbalanced_leading_quote_left_untouched(self):
        # Only a leading quote (no matching trailing one): must not be stripped
        # and must not raise.
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", '"U:\\thomas\\weird.hdf']
        image, mountpoint = _parse_mount_tokens(tokens)
        assert image == '"U:\\thomas\\weird.hdf'
        assert mountpoint is None

    def test_unbalanced_trailing_quote_left_untouched(self):
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", 'U:\\thomas\\weird.hdf"']
        image, mountpoint = _parse_mount_tokens(tokens)
        assert image == 'U:\\thomas\\weird.hdf"'

    def test_unquoted_image_unchanged(self):
        # Unix posix=True path already strips quotes: tokens have none, so the
        # strip is a harmless no-op.
        from amifuse.platform import _parse_mount_tokens

        tokens = [
            "python", "-m", "amifuse", "mount",
            "--mountpoint", "/mnt/amiga",
            "/home/user/disk.hdf",
        ]
        image, mountpoint = _parse_mount_tokens(tokens)
        assert image == "/home/user/disk.hdf"
        assert mountpoint == "/mnt/amiga"

    def test_quoted_spaced_unc_image_stripped(self):
        # UNC path with a space: a single surrounding quote pair is stripped
        # while the internal backslashes (including the leading \\) survive.
        from amifuse.platform import _parse_mount_tokens

        tokens = [
            "amifuse", "mount",
            '"\\\\192.168.3.3\\share\\my disk.hdf"',
        ]
        image, mountpoint = _parse_mount_tokens(tokens)
        assert image == "\\\\192.168.3.3\\share\\my disk.hdf"
        assert mountpoint is None
        # No surrounding quotes leaked; the leading UNC \\ is intact.
        assert not image.startswith('"') and not image.endswith('"')
        assert image.startswith("\\\\")

    def test_lone_quote_token_unchanged(self):
        # A token of exactly one double-quote (len 1) exercises the
        # len(value) >= 2 guard: it must be returned unchanged, not stripped.
        from amifuse.platform import _parse_mount_tokens

        tokens = ["amifuse", "mount", '"']
        image, mountpoint = _parse_mount_tokens(tokens)
        assert image == '"'
        assert mountpoint is None

    def test_quoted_spaced_image_and_mountpoint_both_stripped(self):
        # Both image and mountpoint quoted-and-spaced in a single call.
        from amifuse.platform import _parse_mount_tokens

        tokens = [
            "amifuse", "mount",
            "--mountpoint", '"/Volumes/My Disk"',
            '"/path/My Disk.hdf"',
        ]
        image, mountpoint = _parse_mount_tokens(tokens)
        assert image == "/path/My Disk.hdf"
        assert mountpoint == "/Volumes/My Disk"
