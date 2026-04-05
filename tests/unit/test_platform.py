"""Unit tests for amifuse.platform module.

All functions in platform.py branch on sys.platform -- we use monkeypatch to
control the platform string. Functions that import from icon_darwin need
careful handling: icon_darwin is pure Python (no OS-specific C extensions)
and can be imported on all platforms, so we import it directly in Darwin tests.

Mock targets are patched at the module level where they are looked up:
    - amifuse.platform.os.path.exists
    - amifuse.platform.shutil.which
"""

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
        """Windows returns True (WinFsp handles drive letter mountpoints)."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.platform import should_auto_create_mountpoint

        assert should_auto_create_mountpoint(Path("D:")) is True


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


# ---------------------------------------------------------------------------
# D. get_mount_options() -- 1 test
# ---------------------------------------------------------------------------


class TestGetMountOptions:
    """Tests for get_mount_options(volname, ...)."""

    def test_mount_options_linux_empty(self, monkeypatch):
        """Non-macOS platforms return empty dict (no special mount options)."""
        monkeypatch.setattr("sys.platform", "linux")
        from amifuse.platform import get_mount_options

        result = get_mount_options("TestVol")
        assert result == {}


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
