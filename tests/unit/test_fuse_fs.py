"""Unit tests for amifuse.fuse_fs module.

Tests for platform-specific FUSE option handling. The fuse_mock fixture
from tests/conftest.py allows importing amifuse.fuse_fs without fusepy
installed.
"""

import argparse
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# A. TestMountFuseOptions -- subtype guard tests
# ---------------------------------------------------------------------------


class TestMountFuseOptions:
    """Tests for the subtype guard in mount_fuse().

    mount_fuse() has many internal dependencies (detect_adf, detect_iso,
    HandlerBridge, FUSE, etc.) that all need mocking. We use a comprehensive
    fixture to capture the FUSE kwargs without actually mounting anything.
    """

    @pytest.fixture
    def mock_mount_fuse_deps(self, monkeypatch, fuse_mock):
        """Patch all dependencies of mount_fuse() to capture FUSE kwargs.

        Returns a dict with a 'fuse_kwargs' key that will be populated
        with the kwargs passed to FUSE() when mount_fuse() is called.
        """
        # Import after fuse_mock has injected the fake fuse module
        import amifuse.fuse_fs as fuse_fs_mod

        captured = {"fuse_kwargs": None}

        # Patch FUSE to capture kwargs
        def fake_fuse(fs_instance, mountpoint, **kwargs):
            captured["fuse_kwargs"] = kwargs

        monkeypatch.setattr(fuse_fs_mod, "FUSE", fake_fuse)

        # Patch detect_adf and detect_iso (imported locally in mount_fuse)
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        # Patch get_partition_name and extract_embedded_driver
        monkeypatch.setattr(
            fuse_fs_mod, "get_partition_name", lambda *a, **kw: "DH0"
        )
        monkeypatch.setattr(
            fuse_fs_mod,
            "extract_embedded_driver",
            lambda *a, **kw: (Path("/tmp/fake.handler"), "DOS3", 0x444F5303),
        )

        # Patch HandlerBridge
        mock_bridge_instance = MagicMock()
        mock_bridge_instance.volume_name.return_value = "TestVol"
        mock_bridge_class = MagicMock(return_value=mock_bridge_instance)
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", mock_bridge_class)

        # Patch platform module functions
        import amifuse.platform as plat_mod

        monkeypatch.setattr(
            plat_mod, "get_default_mountpoint", lambda v: Path("/mnt/test")
        )
        monkeypatch.setattr(
            plat_mod, "should_auto_create_mountpoint", lambda mp: True
        )
        # Platform mount options are mocked to {} to isolate FUSE-level kwargs.
        # Tests for platform option merging should override this mock.
        monkeypatch.setattr(
            plat_mod, "get_mount_options", lambda **kw: {}
        )
        monkeypatch.setattr(
            plat_mod, "pre_generate_volume_icon", lambda *a, **kw: None
        )
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)
        monkeypatch.setattr(plat_mod, "validate_mountpoint", lambda mp: None)

        # Patch os.path.ismount to return False (mountpoint not in use)
        monkeypatch.setattr("os.path.ismount", lambda p: False)

        # Patch Path.exists for mountpoint checks
        original_exists = Path.exists
        monkeypatch.setattr(
            Path, "exists", lambda self: False if str(self) == "/mnt/test" else original_exists(self)
        )

        # Patch AmigaFuseFS to avoid full filesystem init
        mock_fuse_fs = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "AmigaFuseFS", mock_fuse_fs)

        # Patch DosType import used in mount_fuse
        fake_dostype = MagicMock()
        monkeypatch.setitem(sys.modules, "amitools", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

        return captured

    def test_subtype_included_on_linux(self, monkeypatch, mock_mount_fuse_deps):
        """On Linux, the 'subtype' kwarg is included and set to 'amifuse'."""
        monkeypatch.setattr("sys.platform", "linux")
        from amifuse.fuse_fs import mount_fuse

        mount_fuse(
            image=Path("/tmp/test.hdf"),
            driver=None,
            mountpoint=None,
            block_size=None,
        )

        kwargs = mock_mount_fuse_deps["fuse_kwargs"]
        assert kwargs is not None, "FUSE was not called"
        assert "subtype" in kwargs
        assert kwargs["subtype"] == "amifuse"

    def test_subtype_excluded_on_windows(self, monkeypatch, mock_mount_fuse_deps):
        """On Windows, the 'subtype' kwarg is NOT included."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.fuse_fs import mount_fuse

        mount_fuse(
            image=Path("/tmp/test.hdf"),
            driver=None,
            mountpoint=None,
            block_size=None,
        )

        kwargs = mock_mount_fuse_deps["fuse_kwargs"]
        assert kwargs is not None, "FUSE was not called"
        assert "subtype" not in kwargs

    def test_subtype_excluded_on_darwin(self, monkeypatch, mock_mount_fuse_deps):
        """On macOS, the 'subtype' kwarg is NOT included."""
        monkeypatch.setattr("sys.platform", "darwin")
        from amifuse.fuse_fs import mount_fuse

        mount_fuse(
            image=Path("/tmp/test.hdf"),
            driver=None,
            mountpoint=None,
            block_size=None,
        )

        kwargs = mock_mount_fuse_deps["fuse_kwargs"]
        assert kwargs is not None, "FUSE was not called"
        assert "subtype" not in kwargs

    def test_mount_defaults_to_daemon_on_linux(self, monkeypatch, mock_mount_fuse_deps):
        """Linux mounts default to background mode."""
        monkeypatch.setattr("sys.platform", "linux")
        from amifuse.fuse_fs import mount_fuse

        mount_fuse(
            image=Path("/tmp/test.hdf"),
            driver=None,
            mountpoint=None,
            block_size=None,
        )

        kwargs = mock_mount_fuse_deps["fuse_kwargs"]
        assert kwargs is not None, "FUSE was not called"
        assert kwargs["foreground"] is False

    def test_mount_defaults_to_foreground_on_windows(self, monkeypatch, mock_mount_fuse_deps):
        """Windows keeps the mount attached by default."""
        monkeypatch.setattr("sys.platform", "win32")
        from amifuse.fuse_fs import mount_fuse

        mount_fuse(
            image=Path("/tmp/test.hdf"),
            driver=None,
            mountpoint=None,
            block_size=None,
        )

        kwargs = mock_mount_fuse_deps["fuse_kwargs"]
        assert kwargs is not None, "FUSE was not called"
        assert kwargs["foreground"] is True

    def test_mount_respects_explicit_interactive_mode(self, monkeypatch, mock_mount_fuse_deps):
        """An explicit foreground request overrides the platform default."""
        monkeypatch.setattr("sys.platform", "linux")
        from amifuse.fuse_fs import mount_fuse

        mount_fuse(
            image=Path("/tmp/test.hdf"),
            driver=None,
            mountpoint=None,
            block_size=None,
            foreground=True,
        )

        kwargs = mock_mount_fuse_deps["fuse_kwargs"]
        assert kwargs is not None, "FUSE was not called"
        assert kwargs["foreground"] is True

    def test_mount_rejects_daemon_mode_without_unmount_command(
        self, monkeypatch, mock_mount_fuse_deps
    ):
        """Background mode is rejected if the platform cannot unmount it later."""
        monkeypatch.setattr("sys.platform", "win32")
        import amifuse.platform as plat_mod
        from amifuse.fuse_fs import mount_fuse

        monkeypatch.setattr(plat_mod, "get_unmount_command", lambda mp: [])

        with pytest.raises(SystemExit) as exc_info:
            mount_fuse(
                image=Path("/tmp/test.hdf"),
                driver=None,
                mountpoint=None,
                block_size=None,
                foreground=False,
            )

        assert "Daemon mode is not supported" in str(exc_info.value)


class TestUnmountCommand:
    """Tests for the unmount subcommand helper."""

    def test_unmount_runs_platform_command(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: True)
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: ["umount", "-f", str(mountpoint)],
        )
        called = {}

        def fake_run(cmd, check=False):
            called["cmd"] = cmd
            called["check"] = check
            return argparse.Namespace(returncode=0)

        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod.subprocess, "run", fake_run)

        fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/amiga")))

        assert called["cmd"] == ["umount", "-f", "/mnt/amiga"]
        assert called["check"] is False

    def test_unmount_rejects_non_mountpoint(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: False)
        import amifuse.fuse_fs as fuse_fs_mod

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/amiga")))

        assert "is not currently mounted" in str(exc_info.value)

    def test_unmount_rejects_platforms_without_command(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: True)
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: [],
        )
        import amifuse.fuse_fs as fuse_fs_mod

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/amiga")))

        assert "does not provide a standalone unmount command" in str(exc_info.value)


# ---------------------------------------------------------------------------
# B. TestPlatformSpecialFiles -- platform-aware file filtering tests
# ---------------------------------------------------------------------------


@pytest.fixture
def fs_instance(fuse_mock):
    """Create an AmigaFuseFS instance with a mock bridge for testing."""
    from amifuse.fuse_fs import AmigaFuseFS

    bridge = type("MockBridge", (), {"_write_enabled": False})()
    return AmigaFuseFS(bridge, debug=False, icons=False)


class TestPlatformSpecialFiles:
    """Tests for _is_platform_special() cross-platform filtering.

    Verifies that macOS special files are only filtered on macOS,
    Windows Explorer probe files only on Windows, and Linux has
    no special file filtering (intentional behavioral fix).
    """

    # -- macOS tests --

    def test_macos_special_ds_store(self, monkeypatch, fs_instance):
        """On macOS, .DS_Store is filtered."""
        monkeypatch.setattr("sys.platform", "darwin")
        assert fs_instance._is_platform_special("/.DS_Store") is True

    def test_macos_special_spotlight(self, monkeypatch, fs_instance):
        """On macOS, .Spotlight-V100 is filtered."""
        monkeypatch.setattr("sys.platform", "darwin")
        assert fs_instance._is_platform_special("/.Spotlight-V100") is True

    def test_macos_special_appledouble(self, monkeypatch, fs_instance):
        """On macOS, AppleDouble resource fork files (._prefix) are filtered."""
        monkeypatch.setattr("sys.platform", "darwin")
        assert fs_instance._is_platform_special("/dir/._file") is True

    def test_macos_normal_file(self, monkeypatch, fs_instance):
        """On macOS, normal files are not filtered."""
        monkeypatch.setattr("sys.platform", "darwin")
        assert fs_instance._is_platform_special("/readme.txt") is False

    # -- Windows tests --

    def test_windows_special_desktop_ini(self, monkeypatch, fs_instance):
        """On Windows, desktop.ini is filtered."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/desktop.ini") is True

    def test_windows_special_thumbs_db(self, monkeypatch, fs_instance):
        """On Windows, Thumbs.db is filtered."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/Thumbs.db") is True

    def test_windows_special_recycle_bin(self, monkeypatch, fs_instance):
        """On Windows, $RECYCLE.BIN is filtered."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/$RECYCLE.BIN") is True

    def test_windows_special_system_volume_info(self, monkeypatch, fs_instance):
        """On Windows, System Volume Information is filtered."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/System Volume Information") is True

    def test_windows_special_autorun_inf(self, monkeypatch, fs_instance):
        """On Windows, autorun.inf is filtered."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/autorun.inf") is True

    def test_windows_special_folder_jpg(self, monkeypatch, fs_instance):
        """On Windows, Folder.jpg is filtered."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/Folder.jpg") is True

    def test_windows_normal_file(self, monkeypatch, fs_instance):
        """On Windows, normal files are not filtered."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/readme.txt") is False

    # -- Linux tests --

    def test_linux_no_special_files(self, monkeypatch, fs_instance):
        """On Linux, macOS special files are NOT filtered.

        This is an intentional behavioral fix: the old _is_macos_special()
        incorrectly filtered macOS files on all platforms. Linux desktop
        environments don't probe with these files.
        """
        monkeypatch.setattr("sys.platform", "linux")
        assert fs_instance._is_platform_special("/.DS_Store") is False

    def test_linux_normal_file(self, monkeypatch, fs_instance):
        """On Linux, normal files are not filtered."""
        monkeypatch.setattr("sys.platform", "linux")
        assert fs_instance._is_platform_special("/readme.txt") is False

    # -- Cross-platform isolation tests --

    def test_macos_special_not_filtered_on_windows(self, monkeypatch, fs_instance):
        """On Windows, macOS-specific files (.DS_Store) are NOT filtered."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/.DS_Store") is False

    def test_windows_special_not_filtered_on_macos(self, monkeypatch, fs_instance):
        """On macOS, Windows-specific files (desktop.ini) are NOT filtered."""
        monkeypatch.setattr("sys.platform", "darwin")
        assert fs_instance._is_platform_special("/desktop.ini") is False

    # -- Path handling tests --

    def test_nested_path_extracts_filename(self, monkeypatch, fs_instance):
        """Filtering is based on filename, not full path."""
        monkeypatch.setattr("sys.platform", "win32")
        assert fs_instance._is_platform_special("/some/deep/path/desktop.ini") is True


@pytest.fixture
def fs_with_mock_bridge(fuse_mock):
    """Create an AmigaFuseFS with a mock bridge for destroy() testing."""
    from amifuse.fuse_fs import AmigaFuseFS
    bridge = MagicMock()
    bridge._write_enabled = False
    bridge.vh = MagicMock()
    bridge.backend = MagicMock()
    fs = AmigaFuseFS(bridge, debug=False, icons=False)
    return fs, bridge


class TestDestroyCleanup:
    """Tests for AmigaFuseFS.destroy() resource cleanup."""

    def test_destroy_closes_backend(self, fs_with_mock_bridge):
        fs, bridge = fs_with_mock_bridge
        fs.destroy("/")
        bridge.backend.sync.assert_called_once()
        bridge.backend.close.assert_called_once()

    def test_destroy_shuts_down_runtime(self, fs_with_mock_bridge):
        fs, bridge = fs_with_mock_bridge
        fs.destroy("/")
        bridge.vh.shutdown.assert_called_once()

    def test_destroy_flushes_when_write_enabled(self, fs_with_mock_bridge):
        fs, bridge = fs_with_mock_bridge
        bridge._write_enabled = True
        fs.destroy("/")
        bridge.flush_volume.assert_called_once()

    def test_destroy_skips_flush_when_read_only(self, fs_with_mock_bridge):
        fs, bridge = fs_with_mock_bridge
        bridge._write_enabled = False
        fs.destroy("/")
        bridge.flush_volume.assert_not_called()

    def test_destroy_continues_after_flush_failure(self, fs_with_mock_bridge):
        fs, bridge = fs_with_mock_bridge
        bridge._write_enabled = True
        bridge.flush_volume.side_effect = RuntimeError("flush failed")
        fs.destroy("/")
        bridge.vh.shutdown.assert_called_once()
        bridge.backend.close.assert_called_once()

    def test_destroy_continues_after_shutdown_failure(self, fs_with_mock_bridge):
        fs, bridge = fs_with_mock_bridge
        bridge.vh.shutdown.side_effect = RuntimeError("shutdown failed")
        fs.destroy("/")
        bridge.backend.close.assert_called_once()

    def test_destroy_handles_missing_vh(self, fs_with_mock_bridge):
        fs, bridge = fs_with_mock_bridge
        bridge.vh = None
        fs.destroy("/")
        bridge.backend.close.assert_called_once()

    def test_destroy_handles_missing_backend(self, fs_with_mock_bridge):
        fs, bridge = fs_with_mock_bridge
        bridge.backend = None
        fs.destroy("/")


class TestDirectoryListingCap:
    """Tests for list_dir() iteration limit.

    Source inspection tests: fragile by design, to be replaced with
    functional tests when integration test infrastructure is available (Phase 5).
    """

    def test_list_dir_cap_removed(self, fuse_mock):
        """Verify list_dir() no longer has a 256-entry hard cap."""
        import inspect
        from amifuse.fuse_fs import HandlerBridge
        source = inspect.getsource(HandlerBridge.list_dir)
        assert "range(256)" not in source
        assert "range(65536)" in source

    def test_list_dir_still_has_safety_limit(self, fuse_mock):
        """Verify list_dir() retains a safety limit (not while True)."""
        import inspect
        from amifuse.fuse_fs import HandlerBridge
        source = inspect.getsource(HandlerBridge.list_dir)
        assert "for iter_num in range(" in source
        assert "while True" not in source
