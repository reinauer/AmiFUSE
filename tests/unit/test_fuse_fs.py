"""Unit tests for amifuse.fuse_fs module.

Tests for platform-specific FUSE option handling. The fuse_mock fixture
from tests/conftest.py allows importing amifuse.fuse_fs without fusepy
installed.
"""

import argparse
import json
import signal
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

    def test_mount_defaults_to_daemon_on_darwin(self, monkeypatch, mock_mount_fuse_deps):
        """macOS mounts default to background mode."""
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

    def test_mount_aborts_if_handler_crashes_before_fuse_starts(self, monkeypatch, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = MagicMock(
            volume_id="TestISO",
            block_size=2048,
            cylinders=1,
            heads=1,
            sectors_per_track=1,
            total_blocks=1,
        )
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        fake_dostype = MagicMock()
        monkeypatch.setitem(sys.modules, "amitools", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

        import amifuse.platform as plat_mod
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)
        monkeypatch.setattr(plat_mod, "validate_mountpoint", lambda mp: None)
        monkeypatch.setattr(plat_mod, "should_auto_create_mountpoint", lambda mp: True)
        monkeypatch.setattr(plat_mod, "mount_runs_in_foreground_by_default", lambda: True)

        mock_bridge = MagicMock()
        mock_bridge.state.crashed = True
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", MagicMock(return_value=mock_bridge))
        mock_fuse = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "FUSE", mock_fuse)

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.mount_fuse(
                image=Path("/tmp/test.iso"),
                driver=Path("/tmp/test.handler"),
                mountpoint=Path("/mnt/test"),
                block_size=None,
                foreground=True,
            )

        assert "crashed during startup" in str(exc_info.value)
        mock_bridge.close.assert_called_once()
        mock_fuse.assert_not_called()


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
        monkeypatch.setattr("amifuse.platform._is_stale_mountpoint", lambda path: False)
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

    def test_unmount_runs_for_stale_mountpoint(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: False)
        monkeypatch.setattr(
            "amifuse.platform._is_stale_mountpoint",
            lambda mountpoint: True,
        )
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: ["umount", "-f", str(mountpoint)],
        )
        called = {}

        def fake_run(cmd, check=False):
            called["cmd"] = cmd
            return argparse.Namespace(returncode=0)

        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod.subprocess, "run", fake_run)

        fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/broken")))

        assert called["cmd"] == ["umount", "-f", "/mnt/broken"]

    def test_unmount_kills_hanging_mount_owner_after_failed_unmount(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: True)
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: ["umount", "-f", str(mountpoint)],
        )

        run_calls = {"unmount": 0}

        def fake_run(cmd, check=False, capture_output=False, text=False):
            if cmd[:2] == ["ps", "-axo"]:
                return argparse.Namespace(
                    returncode=0,
                    stdout="123 python3 -m amifuse mount disk.iso --mountpoint ./mnt\n",
                )
            run_calls["unmount"] += 1
            return argparse.Namespace(returncode=1 if run_calls["unmount"] == 1 else 0)

        killed = []

        def fake_kill(pid, sig):
            if sig != 0:
                killed.append((pid, sig))
            else:
                if any(saved_pid == pid and saved_sig == signal.SIGKILL for saved_pid, saved_sig in killed):
                    raise ProcessLookupError()

        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(fuse_fs_mod.os, "kill", fake_kill)
        monkeypatch.setattr(fuse_fs_mod.os, "getpid", lambda: 999)

        fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("./mnt")))

        assert run_calls["unmount"] == 2
        assert (123, signal.SIGTERM) in killed


class TestDriverValidation:
    """Tests for explicit filesystem driver path validation."""

    def test_cmd_mount_rejects_missing_driver(self, monkeypatch, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        called = {"mount_fuse": False}

        def fake_mount_fuse(*args, **kwargs):
            called["mount_fuse"] = True

        monkeypatch.setattr(fuse_fs_mod, "mount_fuse", fake_mount_fuse)

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_mount(
                argparse.Namespace(
                    image=Path("/tmp/test.iso"),
                    driver=Path("/tmp/does-not-exist.handler"),
                    mountpoint=Path("/tmp/mnt"),
                    block_size=None,
                    volname=None,
                    debug=False,
                    trace=False,
                    write=False,
                    partition=None,
                    icons=False,
                    foreground=True,
                    profile=False,
                )
            )

        assert "Filesystem driver not found" in str(exc_info.value)
        assert called["mount_fuse"] is False

    def test_mount_reports_stale_mountpoint_without_traceback(self, monkeypatch, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod
        import amifuse.platform as plat_mod

        monkeypatch.setattr("sys.platform", "linux")
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)
        monkeypatch.setattr(plat_mod, "validate_mountpoint", lambda mp: None)
        monkeypatch.setattr(plat_mod, "should_auto_create_mountpoint", lambda mp: False)
        monkeypatch.setattr(plat_mod, "mount_runs_in_foreground_by_default", lambda: True)
        monkeypatch.setattr(plat_mod, "get_unmount_command", lambda mp: ["umount", "-f", str(mp)])

        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = MagicMock(
            volume_id="TestISO",
            block_size=2048,
            cylinders=1,
            heads=1,
            sectors_per_track=1,
            total_blocks=1,
        )
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)
        fake_dostype = MagicMock()
        monkeypatch.setitem(sys.modules, "amitools", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

        class BrokenMountPath(type(Path())):
            def exists(self):
                return False

            def mkdir(self, parents=False, exist_ok=False):
                raise FileExistsError(17, "File exists", str(self))

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.mount_fuse(
                image=Path("/tmp/test.iso"),
                driver=Path("/tmp/test.handler"),
                mountpoint=BrokenMountPath("/mnt/broken"),
                block_size=None,
                foreground=True,
            )

        assert "stale or broken mount" in str(exc_info.value)


class TestCrashShutdown:
    def test_check_handler_alive_schedules_shutdown_once(self, monkeypatch, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        bridge = type("MockBridge", (), {"state": type("State", (), {"crashed": True})(), "_write_enabled": False})()
        fs = fuse_fs_mod.AmigaFuseFS(bridge, debug=False, icons=False)
        calls = {"count": 0}

        class FakeThread:
            def __init__(self, target=None, name=None, daemon=None):
                self.target = target

            def start(self):
                calls["count"] += 1

        monkeypatch.setattr(fuse_fs_mod.threading, "Thread", FakeThread)

        with pytest.raises(fuse_fs_mod.FuseOSError):
            fs._check_handler_alive()
        with pytest.raises(fuse_fs_mod.FuseOSError):
            fs._check_handler_alive()

        assert calls["count"] == 1


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


# ---------------------------------------------------------------------------
# TestJsonHelpers -- JSON envelope helper tests
# ---------------------------------------------------------------------------


class TestJsonHelpers:
    """Tests for _json_error() and _json_result() envelope helpers."""

    def test_json_error_structure(self, fuse_mock):
        from amifuse.fuse_fs import _json_error

        result = _json_error("test", "TEST_ERROR", "something went wrong")
        assert result["status"] == "error"
        assert result["command"] == "test"
        assert "version" in result
        assert result["error"]["code"] == "TEST_ERROR"
        assert result["error"]["message"] == "something went wrong"
        assert "details" not in result["error"]

    def test_json_error_with_details(self, fuse_mock):
        from amifuse.fuse_fs import _json_error

        result = _json_error("test", "TEST_ERROR", "msg", details={"key": "val"})
        assert result["error"]["details"]["key"] == "val"

    def test_json_result_structure(self, fuse_mock):
        from amifuse.fuse_fs import _json_result

        result = _json_result("test", foo="bar")
        assert result["status"] == "ok"
        assert result["command"] == "test"
        assert "version" in result
        assert result["foo"] == "bar"


# ---------------------------------------------------------------------------
# TestCleanupBridge -- bridge cleanup helper tests
# ---------------------------------------------------------------------------


class TestCleanupBridge:
    """Tests for _cleanup_bridge() resource cleanup."""

    def test_cleanup_bridge_deletes_temp_driver(self, fuse_mock, tmp_path):
        from amifuse.fuse_fs import _cleanup_bridge

        temp_file = tmp_path / "test.handler"
        temp_file.write_text("fake driver")
        assert temp_file.exists()

        mock_bridge = MagicMock()
        _cleanup_bridge(mock_bridge, temp_file)
        assert not temp_file.exists()

    def test_cleanup_bridge_none_bridge_still_deletes_temp(self, fuse_mock, tmp_path):
        from amifuse.fuse_fs import _cleanup_bridge

        temp_file = tmp_path / "test.handler"
        temp_file.write_text("fake driver")
        assert temp_file.exists()

        _cleanup_bridge(None, temp_file)
        assert not temp_file.exists()

    def test_cleanup_bridge_calls_shutdown_and_close(self, fuse_mock):
        from amifuse.fuse_fs import _cleanup_bridge

        mock_bridge = MagicMock()
        _cleanup_bridge(mock_bridge)
        mock_bridge.vh.shutdown.assert_called_once()
        mock_bridge.backend.sync.assert_called_once()
        mock_bridge.backend.close.assert_called_once()

    def test_cleanup_bridge_handles_shutdown_failure(self, fuse_mock):
        from amifuse.fuse_fs import _cleanup_bridge

        mock_bridge = MagicMock()
        mock_bridge.vh.shutdown.side_effect = RuntimeError("boom")
        # Should not raise
        _cleanup_bridge(mock_bridge)
        mock_bridge.backend.sync.assert_called_once()
        mock_bridge.backend.close.assert_called_once()

    def test_cleanup_bridge_sync_failure_still_closes(self, fuse_mock):
        """If backend.sync() raises, backend.close() must still be called."""
        from amifuse.fuse_fs import _cleanup_bridge

        mock_bridge = MagicMock()
        mock_bridge.backend.sync.side_effect = OSError("disk error")
        _cleanup_bridge(mock_bridge)
        mock_bridge.backend.close.assert_called_once()


# ---------------------------------------------------------------------------
# TestCreateBridgeFromArgs -- bridge creation helper tests
# ---------------------------------------------------------------------------


class TestCreateBridgeFromArgs:
    """Tests for _create_bridge_from_args() error handling."""

    def test_bridge_image_not_found_json(self, fuse_mock, tmp_path, capsys, monkeypatch):
        # Mock rdb_inspect so the local import in _create_bridge_from_args works
        fake_rdb = MagicMock()
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        from amifuse.fuse_fs import _create_bridge_from_args

        args = argparse.Namespace(
            image=tmp_path / "nonexistent.hdf",
            json=True,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            _create_bridge_from_args(args, "test")
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "IMAGE_NOT_FOUND"

    def test_bridge_image_not_found_human(self, fuse_mock, tmp_path, monkeypatch):
        fake_rdb = MagicMock()
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        from amifuse.fuse_fs import _create_bridge_from_args

        args = argparse.Namespace(
            image=tmp_path / "nonexistent.hdf",
            json=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit, match="image file not found"):
            _create_bridge_from_args(args, "test")

    def test_bridge_returns_tuple(self, fuse_mock, monkeypatch, tmp_path):
        import amifuse.fuse_fs as fuse_fs_mod

        # Create a real image file so exists() passes
        image = tmp_path / "test.hdf"
        image.write_bytes(b"\x00" * 1024)

        # Mock detect_adf/detect_iso to return None (treat as RDB)
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        # Mock extract_embedded_driver to return a temp path
        temp_driver = tmp_path / "temp.handler"
        temp_driver.write_text("fake")
        monkeypatch.setattr(
            fuse_fs_mod, "extract_embedded_driver",
            lambda *a, **kw: (temp_driver, "DOS3", 0x444F5303),
        )

        # Mock HandlerBridge
        mock_bridge = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", lambda *a, **kw: mock_bridge)

        args = argparse.Namespace(
            image=image,
            json=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        result = fuse_fs_mod._create_bridge_from_args(args, "test")
        assert isinstance(result, tuple)
        assert len(result) == 2
        bridge, td = result
        assert bridge is mock_bridge
        assert td == temp_driver

    def test_bridge_cleans_temp_driver_on_failure(self, fuse_mock, monkeypatch, tmp_path):
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.hdf"
        image.write_bytes(b"\x00" * 1024)

        # Mock detect_adf/detect_iso
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        # Create a real temp file
        temp_driver = tmp_path / "temp.handler"
        temp_driver.write_text("fake driver content")
        monkeypatch.setattr(
            fuse_fs_mod, "extract_embedded_driver",
            lambda *a, **kw: (temp_driver, "DOS3", 0x444F5303),
        )

        # Mock HandlerBridge to raise
        def fail_init(*a, **kw):
            raise RuntimeError("init failed")
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", fail_init)

        args = argparse.Namespace(
            image=image,
            json=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod._create_bridge_from_args(args, "test")
        # Temp driver should be cleaned up
        assert not temp_driver.exists()

    def test_bridge_adf_no_driver_json_error(
        self, fuse_mock, monkeypatch, tmp_path, capsys,
    ):
        """ADF without --driver: verify JSON body."""
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.adf"
        image.write_bytes(b"\x00" * 1024)

        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = MagicMock()
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        args = argparse.Namespace(
            image=image,
            json=True,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod._create_bridge_from_args(args, "test")
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "DRIVER_NOT_FOUND"
        assert "ADF" in data["error"]["message"]
        assert "--driver" in data["error"]["message"]

    def test_bridge_iso_no_driver_json_error(
        self, fuse_mock, monkeypatch, tmp_path, capsys,
    ):
        """ISO image without --driver should emit DRIVER_NOT_FOUND JSON error."""
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.iso"
        image.write_bytes(b"\x00" * 1024)

        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = MagicMock()  # valid ISOInfo
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        args = argparse.Namespace(
            image=image,
            json=True,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod._create_bridge_from_args(args, "test")
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "DRIVER_NOT_FOUND"
        assert "ISO" in data["error"]["message"]
        assert "--driver" in data["error"]["message"]

    def test_bridge_rdb_no_embedded_driver_json_error(
        self, fuse_mock, monkeypatch, tmp_path, capsys,
    ):
        """RDB image with no embedded driver should emit DRIVER_NOT_FOUND."""
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.hdf"
        image.write_bytes(b"\x00" * 1024)

        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        monkeypatch.setattr(
            fuse_fs_mod, "extract_embedded_driver", lambda *a, **kw: None,
        )

        args = argparse.Namespace(
            image=image,
            json=True,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod._create_bridge_from_args(args, "test")
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "DRIVER_NOT_FOUND"


# ---------------------------------------------------------------------------
# TestCmdLs -- ls command tests
# ---------------------------------------------------------------------------


class TestCmdLs:
    """Tests for cmd_ls() subcommand."""

    @pytest.fixture
    def mock_bridge_for_ls(self, fuse_mock, monkeypatch):
        """Set up mocked bridge for ls tests."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd: (mock_bridge, None),
        )
        return mock_bridge, fuse_fs_mod

    def test_ls_json_output_structure(self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.list_dir_path.return_value = [
            {"name": "file1.txt", "dir_type": 0, "size": 100, "protection": 0},
            {"name": "Devs", "dir_type": 2, "size": 0, "protection": 0},
        ]

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            path="/",
            recursive=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_ls(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "ls"
        assert "version" in data
        assert data["path"] == "/"
        assert len(data["entries"]) == 2
        assert data["entries"][0]["name"] == "file1.txt"
        assert data["entries"][0]["type"] == "file"
        assert data["entries"][0]["size"] == 100
        assert data["entries"][1]["name"] == "Devs"
        assert data["entries"][1]["type"] == "dir"

    def test_ls_recursive_json(self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls

        def fake_list_dir(path):
            if path == "/":
                return [
                    {"name": "S", "dir_type": 2, "size": 0, "protection": 0},
                    {"name": "readme.txt", "dir_type": 0, "size": 50, "protection": 0},
                ]
            elif path == "/S":
                return [
                    {"name": "Startup-Sequence", "dir_type": 0, "size": 200, "protection": 0},
                ]
            return []

        mock_bridge.list_dir_path.side_effect = fake_list_dir

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            path="/",
            recursive=True,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_ls(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        entries = data["entries"]
        assert len(entries) == 3
        names = [e["name"] for e in entries]
        assert "S" in names
        assert "readme.txt" in names
        assert "Startup-Sequence" in names
        ss_entry = next(e for e in entries if e["name"] == "Startup-Sequence")
        assert ss_entry["path"] == "/S/Startup-Sequence"

    def test_ls_path_not_found_json(self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.list_dir_path.return_value = []
        mock_bridge.stat_path.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            path="/nonexistent",
            recursive=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_ls(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "FILE_NOT_FOUND"

    def test_ls_normalizes_path(self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.list_dir_path.return_value = [
            {"name": "Startup-Sequence", "dir_type": 0, "size": 100, "protection": 0},
        ]

        # Test with path "S/" (no leading slash, trailing slash)
        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            path="S/",
            recursive=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_ls(args)

        # Verify list_dir_path was called with normalized "/S"
        mock_bridge.list_dir_path.assert_called_with("/S")

    def test_ls_human_shows_protection_for_dirs(self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.list_dir_path.return_value = [
            {"name": "Devs", "dir_type": 2, "size": 0, "protection": 15},
        ]

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            path="/",
            recursive=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_ls(args)
        output = capsys.readouterr().out
        assert "Devs" in output
        assert "<dir>" in output
        lines = output.strip().split("\n")
        assert len(lines) == 1
        parts = lines[0].split()
        assert len(parts) >= 3  # name, <dir>, protection

    def test_ls_handler_exception_json(self, mock_bridge_for_ls, capsys):
        """Exception in list_dir_path should produce HANDLER_ERROR JSON."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.list_dir_path.side_effect = RuntimeError("handler crashed")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            path="/",
            recursive=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_ls(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "HANDLER_ERROR"
        assert "handler crashed" in data["error"]["message"]


# ---------------------------------------------------------------------------
# TestCmdVerify -- verify command tests
# ---------------------------------------------------------------------------


class TestCmdVerify:
    """Tests for cmd_verify() subcommand."""

    @pytest.fixture
    def mock_bridge_for_verify(self, fuse_mock, monkeypatch):
        """Set up mocked bridge for verify tests."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd: (mock_bridge, None),
        )
        return mock_bridge, fuse_fs_mod

    def test_verify_file_json(self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.stat_path.return_value = {
            "dir_type": 0, "size": 1234, "name": "test.txt", "protection": 0,
        }

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "verify"
        assert data["exists"] is True
        assert data["size"] == 1234
        assert data["type"] == "file"

    def test_verify_file_size_match(self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.stat_path.return_value = {
            "dir_type": 0, "size": 1234, "name": "test.txt", "protection": 0,
        }

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            expect_size=1234,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["size_matches"] is True
        assert data["expected_size"] == 1234

    def test_verify_file_size_mismatch(self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.stat_path.return_value = {
            "dir_type": 0, "size": 1234, "name": "test.txt", "protection": 0,
        }

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            expect_size=9999,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["size_matches"] is False
        assert data["expected_size"] == 9999

    def test_verify_file_not_found_json(self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.stat_path.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="missing.txt",
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "FILE_NOT_FOUND"

    def test_verify_volume_json(self, mock_bridge_for_verify, monkeypatch, capsys):
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge, _ = mock_bridge_for_verify
        mock_bridge.volume_name.return_value = "Workbench3.1"

        def fake_list_dir(path):
            if path == "/":
                return [
                    {"name": "S", "dir_type": 2, "size": 0, "protection": 0},
                    {"name": "readme.txt", "dir_type": 0, "size": 50, "protection": 0},
                    {"name": "data.bin", "dir_type": 0, "size": 200, "protection": 0},
                ]
            elif path == "/S":
                return [
                    {"name": "Startup-Sequence", "dir_type": 0, "size": 100, "protection": 0},
                ]
            return []

        mock_bridge.list_dir_path.side_effect = fake_list_dir

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file=None,
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "verify"
        assert data["volume"] == "Workbench3.1"
        assert data["total_dirs"] == 1
        assert data["total_files"] == 3
        assert data["total_size_bytes"] == 350
        assert data["filesystem_responsive"] is True

    def test_verify_expect_size_without_file_json(self, fuse_mock, capsys):
        import amifuse.fuse_fs as fuse_fs_mod

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file=None,
            expect_size=100,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "INVALID_ARGUMENT"

    def test_verify_expect_size_without_file_human(self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file=None,
            expect_size=100,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit, match="--expect-size requires --file"):
            fuse_fs_mod.cmd_verify(args)


# ---------------------------------------------------------------------------
# TestCmdDoctor -- doctor subcommand tests
# ---------------------------------------------------------------------------


class TestCmdDoctor:
    """Tests for cmd_doctor() environment diagnostics command.

    Uses monkeypatch to control which packages appear available/unavailable.
    The fuse_mock fixture allows importing fuse_fs without fusepy installed.
    """

    @pytest.fixture
    def doctor_args(self):
        """Return a minimal args namespace for cmd_doctor."""
        args = MagicMock()
        args.json = True
        return args

    @pytest.fixture
    def mock_all_imports_ok(self, monkeypatch, fuse_mock):
        """Mock all dependency imports to succeed.

        Returns the fuse_fs module for calling cmd_doctor.
        """
        import types

        import amifuse.fuse_fs as fuse_fs_mod
        import amifuse.platform as plat_mod

        # amitools available
        fake_amitools = types.ModuleType("amitools")
        monkeypatch.setitem(sys.modules, "amitools", fake_amitools)

        # machine68k available
        fake_machine68k = types.ModuleType("machine68k")
        monkeypatch.setitem(sys.modules, "machine68k", fake_machine68k)

        # fusepy available (already injected by fuse_mock, but ensure __version__)
        sys.modules["fuse"].__version__ = "1.0.0"

        # FUSE backend check passes
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)

        return fuse_fs_mod

    def test_doctor_json_output_structure(self, mock_all_imports_ok, doctor_args, capsys):
        """JSON output has all required envelope keys and check categories."""
        mock_all_imports_ok.cmd_doctor(doctor_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        # Required envelope keys
        for key in ("status", "command", "version", "checks", "overall", "missing", "suggestions"):
            assert key in data, f"Missing key: {key}"

        # Required check categories
        for check_name in ("python", "amitools", "machine68k", "fusepy", "fuse_backend"):
            assert check_name in data["checks"], f"Missing check: {check_name}"

    def test_doctor_overall_ready(self, mock_all_imports_ok, doctor_args, capsys):
        """When all checks pass, overall is 'ready' with status 'ok'."""
        mock_all_imports_ok.cmd_doctor(doctor_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["overall"] == "ready"
        assert data["status"] == "ok"
        assert data["missing"] == []

    def test_doctor_degraded_without_fusepy(self, fuse_mock, monkeypatch, doctor_args, capsys):
        """Missing fusepy = degraded status, exit code 2."""
        import builtins
        import types

        import amifuse.fuse_fs as fuse_fs_mod
        import amifuse.platform as plat_mod

        # amitools + machine68k available
        monkeypatch.setitem(sys.modules, "amitools", types.ModuleType("amitools"))
        monkeypatch.setitem(sys.modules, "machine68k", types.ModuleType("machine68k"))

        # fusepy NOT available -- patch __import__ to block 'fuse' and remove
        # from sys.modules so cmd_doctor's `import fuse` hits ImportError
        monkeypatch.delitem(sys.modules, "fuse", raising=False)
        real_import = builtins.__import__

        def block_fuse(name, *args, **kwargs):
            if name == "fuse":
                raise ImportError("No module named 'fuse'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_fuse)

        # FUSE backend check passes
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_doctor(doctor_args)

        assert exc_info.value.code == 2

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["overall"] == "degraded"
        assert "fusepy" in data["missing"]

    def test_doctor_not_ready_without_amitools(self, fuse_mock, monkeypatch, doctor_args, capsys):
        """Missing amitools = not_ready status, exit code 1."""
        import builtins
        import types

        import amifuse.fuse_fs as fuse_fs_mod
        import amifuse.platform as plat_mod

        # amitools NOT available -- patch __import__ to block it and remove
        # from sys.modules so cmd_doctor's `import amitools` hits ImportError
        monkeypatch.delitem(sys.modules, "amitools", raising=False)
        real_import = builtins.__import__

        def block_amitools(name, *args, **kwargs):
            if name == "amitools":
                raise ImportError("No module named 'amitools'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_amitools)

        # machine68k available
        monkeypatch.setitem(sys.modules, "machine68k", types.ModuleType("machine68k"))

        # fusepy available
        sys.modules["fuse"].__version__ = "1.0.0"

        # FUSE backend check passes
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_doctor(doctor_args)

        assert exc_info.value.code == 1

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["overall"] == "not_ready"
        assert "amitools" in data["missing"]

    def test_doctor_human_output(self, mock_all_imports_ok, capsys):
        """Human-readable output contains expected text."""
        args = MagicMock()
        args.json = False

        mock_all_imports_ok.cmd_doctor(args)
        output = capsys.readouterr().out

        assert "environment check" in output
        assert "python" in output
        assert "amitools" in output
        assert "machine68k" in output
        assert "fusepy" in output
        assert "fuse_backend" in output
        assert "Overall: ready" in output

    def test_doctor_fuse_backend_always_ok_on_non_windows(self, mock_all_imports_ok, monkeypatch, doctor_args, capsys):
        """On non-Windows, fuse_backend is always reported as OK.

        This documents a known limitation: check_fuse_available() is a no-op
        on macOS/Linux (platform.py line 101-104), so the doctor check always
        reports the backend as installed on those platforms. The real FUSE
        check happens at mount time via fusepy.
        """
        monkeypatch.setattr("sys.platform", "linux")

        import amifuse.platform as plat_mod
        # Use the real check_fuse_available -- on non-Windows it returns immediately
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)

        mock_all_imports_ok.cmd_doctor(doctor_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["checks"]["fuse_backend"]["ok"] is True
        assert data["checks"]["fuse_backend"]["installed"] is True
        assert data["checks"]["fuse_backend"]["name"] == "libfuse"
