"""Unit tests for amifuse.fuse_fs module.

Tests for platform-specific FUSE option handling. The fuse_mock fixture
from tests/conftest.py allows importing amifuse.fuse_fs without fusepy
installed.
"""

import argparse
import json
import signal
import sys
from types import SimpleNamespace
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

    def test_darwin_mount_keeps_xattrs_enabled(self, monkeypatch, mock_mount_fuse_deps):
        """On macOS, mount_fuse() must not disable xattrs used by host copy tools."""
        monkeypatch.setattr("sys.platform", "darwin")
        import amifuse.platform as plat_mod
        from amifuse.icon_darwin import get_darwin_mount_options
        from amifuse.fuse_fs import mount_fuse

        monkeypatch.setattr(
            plat_mod,
            "get_mount_options",
            lambda **kwargs: get_darwin_mount_options(**kwargs),
        )

        mount_fuse(
            image=Path("/tmp/test.hdf"),
            driver=None,
            mountpoint=None,
            block_size=None,
        )

        kwargs = mock_mount_fuse_deps["fuse_kwargs"]
        assert kwargs is not None, "FUSE was not called"
        assert kwargs["volname"] == "TestVol"
        assert kwargs["noappledouble"] is True
        assert "noapplexattr" not in kwargs

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


class TestFormatVolume:
    def test_format_stops_after_flush_and_syncs_backend(self, monkeypatch, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        fake_dostype = MagicMock()
        fake_dostype.num_to_tag_str.return_value = "SFS0"
        monkeypatch.setitem(sys.modules, "amitools", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

        monkeypatch.setattr(
            fuse_fs_mod,
            "get_partition_info",
            lambda image, block_size, partition: {"dostype": 0x53465300},
        )

        mock_bridge = MagicMock()
        mock_bridge.state.crashed = False
        mock_bridge.launcher = MagicMock()
        mock_bridge.launcher.alloc_bstr.return_value = (None, 0x1234)
        mock_bridge._run_until_replies.return_value = [(0, 0, 1, 0)]
        monkeypatch.setattr(
            fuse_fs_mod, "HandlerBridge", MagicMock(return_value=mock_bridge)
        )

        fuse_fs_mod.format_volume(
            image=Path("/tmp/test.hdf"),
            driver=Path("/tmp/test.handler"),
            block_size=None,
            partition="SDH0",
            volname="SFSFmt",
        )

        assert mock_bridge.launcher.send_inhibit.call_count == 1
        mock_bridge.launcher.send_inhibit.assert_called_once_with(mock_bridge.state, True)
        mock_bridge.launcher.send_flush.assert_called_once_with(mock_bridge.state)
        mock_bridge.backend.sync.assert_called_once_with()
        mock_bridge.close.assert_called_once()


class TestUnmountCommand:
    """Tests for the unmount subcommand helper."""

    def test_unmount_runs_platform_command(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: True)
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: ["umount", "-f", str(mountpoint)],
        )
        called = {}

        def fake_run(cmd, check=False, **kwargs):
            called["cmd"] = cmd
            called["check"] = check
            return argparse.Namespace(returncode=0)

        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod.subprocess, "run", fake_run)

        mountpoint = Path("/mnt/amiga")
        fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=mountpoint))

        assert called["cmd"] == ["umount", "-f", str(mountpoint)]
        assert called["check"] is False

    def test_unmount_rejects_non_mountpoint(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: False)
        monkeypatch.setattr("amifuse.platform._is_stale_mountpoint", lambda path: False)
        import amifuse.fuse_fs as fuse_fs_mod

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/amiga")))

        assert "is not currently mounted" in str(exc_info.value)

    def test_unmount_uses_process_kill_when_no_command(self, monkeypatch, fuse_mock):
        """When platform returns no unmount command, go straight to process kill."""
        monkeypatch.setattr("os.path.ismount", lambda path: True)
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: [],
        )
        import amifuse.fuse_fs as fuse_fs_mod

        killed_pids = [42]
        monkeypatch.setattr(
            fuse_fs_mod, "_kill_mount_owner_processes",
            lambda mp: killed_pids,
        )

        fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/amiga")))
        # Should succeed (no SystemExit)

    def test_unmount_no_command_no_process_found(self, monkeypatch, fuse_mock):
        """When no unmount command and no process found, report error."""
        monkeypatch.setattr("os.path.ismount", lambda path: True)
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: [],
        )
        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(
            fuse_fs_mod, "_kill_mount_owner_processes",
            lambda mp: [],
        )

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/amiga")))

        assert "No amifuse process found" in str(exc_info.value)

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

        def fake_run(cmd, check=False, **kwargs):
            called["cmd"] = cmd
            return argparse.Namespace(returncode=0)

        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(fuse_fs_mod.subprocess, "run", fake_run)

        mountpoint = Path("/mnt/broken")
        fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=mountpoint))

        assert called["cmd"] == ["umount", "-f", str(mountpoint)]

    def test_unmount_kills_hanging_mount_owner_after_failed_unmount(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: True)
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: ["umount", "-f", str(mountpoint)],
        )

        run_calls = {"unmount": 0}

        def fake_run(cmd, check=False, capture_output=False, text=False, **kwargs):
            if cmd[:2] == ["ps", "-axo"]:
                return argparse.Namespace(
                    returncode=0,
                    stdout="123  1 python3 -m amifuse mount disk.iso --mountpoint ./mnt\n",
                )
            run_calls["unmount"] += 1
            return argparse.Namespace(returncode=1 if run_calls["unmount"] == 1 else 0)

        killed = []

        def fake_kill(pid, sig):
            if sig != 0:
                killed.append((pid, sig))
            else:
                if any(saved_pid == pid and saved_sig == plat_mod._SIGKILL for saved_pid, saved_sig in killed):
                    raise ProcessLookupError()

        import amifuse.fuse_fs as fuse_fs_mod
        import amifuse.platform as plat_mod

        monkeypatch.setattr("sys.platform", "linux")
        # subprocess.run for unmount is called from fuse_fs, but kill functions
        # use platform.subprocess.run and platform.os.kill
        monkeypatch.setattr(fuse_fs_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(plat_mod.subprocess, "run", fake_run)
        monkeypatch.setattr(plat_mod.os, "kill", fake_kill)
        monkeypatch.setattr(plat_mod.os, "getpid", lambda: 999)

        fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("./mnt")))

        assert run_calls["unmount"] == 2
        assert (123, signal.SIGTERM) in killed


class TestPidExists:
    """Tests for _pid_exists() cross-platform behaviour.

    _pid_exists moved to platform.py in Phase 8.
    """

    def test_pid_exists_returns_true_for_live_process(self, fuse_mock, monkeypatch):
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod.sys, "platform", "linux")
        monkeypatch.setattr(plat_mod.os, "kill", lambda pid, sig: None)
        assert plat_mod._pid_exists(12345) is True

    def test_pid_exists_returns_false_for_dead_process(self, fuse_mock, monkeypatch):
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod.sys, "platform", "linux")

        def raise_lookup(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(plat_mod.os, "kill", raise_lookup)
        assert plat_mod._pid_exists(12345) is False

    def test_pid_exists_returns_true_on_permission_error(self, fuse_mock, monkeypatch):
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod.sys, "platform", "linux")

        def raise_perm(pid, sig):
            raise PermissionError("Access denied")

        monkeypatch.setattr(plat_mod.os, "kill", raise_perm)
        assert plat_mod._pid_exists(12345) is True

    def test_pid_exists_returns_false_on_windows_oserror(self, fuse_mock, monkeypatch):
        """Generic OSError means process is dead (Unix path)."""
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod.sys, "platform", "linux")

        def raise_oserror(pid, sig):
            raise OSError(22, "Invalid argument")

        monkeypatch.setattr(plat_mod.os, "kill", raise_oserror)
        assert plat_mod._pid_exists(12345) is False


class TestWindowsProcessDiscovery:
    """Tests for _find_mount_owner_pids() wrapper around platform.find_amifuse_mounts().

    The raw wmic/ps parsing has moved to platform.py and is tested in
    test_status.py. These tests verify the wrapper in platform.py
    correctly filters by mountpoint and handles errors.

    _find_mount_owner_pids moved to platform.py in Phase 8.
    """

    def test_finds_amifuse_pid_from_wmic_output(self, fuse_mock, monkeypatch):
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", lambda: [
            {"mountpoint": "Z:", "image": "disk.hdf", "pid": 4567,
             "uptime_seconds": None, "filesystem_type": None},
        ])

        pids = plat_mod._find_mount_owner_pids(Path("Z:"))
        assert 4567 in pids

    def test_excludes_non_matching_mountpoint(self, fuse_mock, monkeypatch):
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", lambda: [
            {"mountpoint": "Z:", "image": "disk.hdf", "pid": 999,
             "uptime_seconds": None, "filesystem_type": None},
        ])

        pids = plat_mod._find_mount_owner_pids(Path("Y:"))
        assert pids == []

    def test_returns_empty_on_discovery_failure(self, fuse_mock, monkeypatch):
        import amifuse.platform as plat_mod

        def _raise():
            raise OSError("wmic not found")
        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", _raise)

        pids = plat_mod._find_mount_owner_pids(Path("Z:"))
        assert pids == []

    def test_returns_empty_on_oserror(self, fuse_mock, monkeypatch):
        import amifuse.platform as plat_mod

        def _raise():
            raise OSError("ps not found")
        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", _raise)

        pids = plat_mod._find_mount_owner_pids(Path("Z:"))
        assert pids == []

    def test_filters_multiple_mounts(self, fuse_mock, monkeypatch):
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", lambda: [
            {"mountpoint": "Z:", "image": "a.hdf", "pid": 100,
             "uptime_seconds": None, "filesystem_type": None},
            {"mountpoint": "Y:", "image": "b.hdf", "pid": 200,
             "uptime_seconds": None, "filesystem_type": None},
            {"mountpoint": "Z:", "image": "c.hdf", "pid": 300,
             "uptime_seconds": None, "filesystem_type": None},
        ])

        pids = plat_mod._find_mount_owner_pids(Path("Z:"))
        assert sorted(pids) == [100, 300]


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


class TestHandlerBridgeReadBuf:
    def test_alloc_read_buf_reuses_existing_buffer(self, fuse_mock):
        from amifuse.fuse_fs import HandlerBridge

        mem_obj = SimpleNamespace(addr=0x1000, size=16)
        alloc = MagicMock()
        mem = MagicMock()
        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge.vh = SimpleNamespace(alloc=alloc)
        bridge.mem = mem
        bridge._read_buf_mem = mem_obj
        bridge._read_buf_size = 16

        result = bridge._alloc_read_buf(8)

        assert result is mem_obj
        alloc.alloc_memory.assert_not_called()
        alloc.free_memory.assert_not_called()
        mem.w_block.assert_called_once_with(mem_obj.addr, b"\x00" * 8)

    def test_alloc_read_buf_grows_and_frees_old_buffer(self, fuse_mock):
        from amifuse.fuse_fs import HandlerBridge

        old_mem = SimpleNamespace(addr=0x1000, size=8)
        new_mem = SimpleNamespace(addr=0x2000, size=32)
        alloc = MagicMock()
        alloc.alloc_memory.return_value = new_mem
        mem = MagicMock()
        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge.vh = SimpleNamespace(alloc=alloc)
        bridge.mem = mem
        bridge._read_buf_mem = old_mem
        bridge._read_buf_size = 8

        result = bridge._alloc_read_buf(32)

        assert result is new_mem
        assert bridge._read_buf_mem is new_mem
        assert bridge._read_buf_size == 32
        alloc.alloc_memory.assert_called_once_with(32, label="FUSE_READBUF")
        alloc.free_memory.assert_called_once_with(old_mem)
        mem.w_block.assert_called_once_with(new_mem.addr, b"\x00" * 32)

    def test_alloc_read_buf_failed_growth_keeps_old_buffer(self, fuse_mock):
        from amifuse.fuse_fs import HandlerBridge

        old_mem = SimpleNamespace(addr=0x1000, size=8)
        alloc = MagicMock()
        alloc.alloc_memory.side_effect = RuntimeError("oom")
        mem = MagicMock()
        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge.vh = SimpleNamespace(alloc=alloc)
        bridge.mem = mem
        bridge._read_buf_mem = old_mem
        bridge._read_buf_size = 8

        with pytest.raises(RuntimeError, match="oom"):
            bridge._alloc_read_buf(32)

        assert bridge._read_buf_mem is old_mem
        assert bridge._read_buf_size == 8
        alloc.free_memory.assert_not_called()
        mem.w_block.assert_not_called()


class TestHandlerBridgeBstrRing:
    """Verify bstr ring buffer frees old allocation on growth."""

    def test_alloc_bstr_reuses_existing_slot(self, fuse_mock):
        from amifuse.fuse_fs import HandlerBridge

        existing_mem = SimpleNamespace(addr=0x3000, size=16)
        alloc = MagicMock()
        mem = MagicMock()
        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge.vh = SimpleNamespace(alloc=alloc)
        bridge.mem = mem
        bridge._bstr_ring = [existing_mem] + [None] * 7
        bridge._bstr_sizes = [16] + [0] * 7
        bridge._bstr_ring_size = 8
        bridge._bstr_index = 0

        bridge._alloc_bstr("hi")

        alloc.alloc_memory.assert_not_called()
        alloc.free_memory.assert_not_called()

    def test_alloc_bstr_grows_and_frees_old_slot(self, fuse_mock):
        from amifuse.fuse_fs import HandlerBridge

        old_mem = SimpleNamespace(addr=0x3000, size=4)
        new_mem = SimpleNamespace(addr=0x4000, size=64)
        alloc = MagicMock()
        alloc.alloc_memory.return_value = new_mem
        mem = MagicMock()
        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge.vh = SimpleNamespace(alloc=alloc)
        bridge.mem = mem
        bridge._bstr_ring = [old_mem] + [None] * 7
        bridge._bstr_sizes = [4] + [0] * 7
        bridge._bstr_ring_size = 8
        bridge._bstr_index = 0

        bridge._alloc_bstr("a" * 50)

        alloc.alloc_memory.assert_called_once()
        alloc.free_memory.assert_called_once_with(old_mem)
        assert bridge._bstr_ring[0] is new_mem

    def test_alloc_bstr_failed_growth_keeps_old_slot(self, fuse_mock):
        from amifuse.fuse_fs import HandlerBridge

        old_mem = SimpleNamespace(addr=0x3000, size=4)
        alloc = MagicMock()
        alloc.alloc_memory.side_effect = RuntimeError("oom")
        mem = MagicMock()
        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge.vh = SimpleNamespace(alloc=alloc)
        bridge.mem = mem
        bridge._bstr_ring = [old_mem] + [None] * 7
        bridge._bstr_sizes = [4] + [0] * 7
        bridge._bstr_ring_size = 8
        bridge._bstr_index = 0

        with pytest.raises(RuntimeError, match="oom"):
            bridge._alloc_bstr("a" * 50)

        assert bridge._bstr_ring[0] is old_mem
        assert bridge._bstr_sizes[0] == 4
        alloc.free_memory.assert_not_called()


class TestCommandMatchesMountpoint:
    """Tests for mountpoint matching via _find_mount_owner_pids wrapper.

    The _command_matches_mountpoint helper was removed during refactoring.
    Mountpoint matching is now tested through the wrapper. Detailed token
    parsing is covered in test_status.py::TestParseMountTokens.

    _find_mount_owner_pids moved to platform.py in Phase 8.
    """

    def test_matches_literal_mountpoint(self, fuse_mock, monkeypatch):
        """Matches when mountpoint value equals the raw mountpoint string."""
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", lambda: [
            {"mountpoint": "/mnt/amiga", "image": "disk.hdf", "pid": 42,
             "uptime_seconds": None, "filesystem_type": None},
        ])
        pids = plat_mod._find_mount_owner_pids(Path("/mnt/amiga"))
        assert pids == [42]

    def test_no_match_different_mountpoint(self, fuse_mock, monkeypatch):
        """Does not match when the mountpoint value differs."""
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", lambda: [
            {"mountpoint": "/mnt/other", "image": "disk.hdf", "pid": 42,
             "uptime_seconds": None, "filesystem_type": None},
        ])
        pids = plat_mod._find_mount_owner_pids(Path("/mnt/amiga"))
        assert pids == []

    def test_no_match_empty_mounts(self, fuse_mock, monkeypatch):
        """Returns empty when no mounts are active."""
        import amifuse.platform as plat_mod

        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", lambda: [])
        pids = plat_mod._find_mount_owner_pids(Path("/mnt/amiga"))
        assert pids == []

    def test_matches_resolved_path(self, fuse_mock, monkeypatch, tmp_path):
        """Matches when the mountpoint arg resolves to the same absolute path."""
        import amifuse.platform as plat_mod

        abs_mp = str(tmp_path / "amiga")
        monkeypatch.setattr(plat_mod, "find_amifuse_mounts", lambda: [
            {"mountpoint": abs_mp, "image": "disk.hdf", "pid": 42,
             "uptime_seconds": None, "filesystem_type": None},
        ])
        pids = plat_mod._find_mount_owner_pids(Path(abs_mp))
        assert pids == [42]


class TestKillEscalation:
    """Tests for _kill_mount_owner_processes() edge cases.

    _kill_mount_owner_processes moved to platform.py in Phase 8.
    """

    def test_oserror_during_sigterm_does_not_crash(self, fuse_mock, monkeypatch):
        """OSError(22) during SIGTERM is caught and the process is skipped."""
        import amifuse.platform as plat_mod

        monkeypatch.setattr(
            plat_mod, "_find_mount_owner_pids", lambda mp: [1234]
        )

        def fake_kill(pid, sig):
            if sig == signal.SIGTERM:
                raise OSError(22, "Invalid argument")

        monkeypatch.setattr(plat_mod.os, "kill", fake_kill)

        # Should not raise; OSError during SIGTERM is caught
        result = plat_mod._kill_mount_owner_processes(Path("/mnt/amiga"))
        assert result == [1234]


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
            lambda args, cmd, read_only=True: (mock_bridge, None),
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
            lambda args, cmd, read_only=True: (mock_bridge, None),
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

    def test_doctor_degraded_fuse_backend_system_exit(self, fuse_mock, monkeypatch, doctor_args, capsys):
        """FUSE backend raising SystemExit = degraded status, exit code 2.

        check_fuse_available() raises SystemExit (not ImportError) when the
        native FUSE backend is missing. cmd_doctor catches this explicitly.
        """
        import types

        import amifuse.fuse_fs as fuse_fs_mod
        import amifuse.platform as plat_mod

        # Core deps available
        monkeypatch.setitem(sys.modules, "amitools", types.ModuleType("amitools"))
        monkeypatch.setitem(sys.modules, "machine68k", types.ModuleType("machine68k"))

        # fusepy available
        sys.modules["fuse"].__version__ = "1.0.0"

        # FUSE backend check raises SystemExit (e.g. WinFSP not installed)
        def raise_system_exit():
            raise SystemExit("WinFSP not installed")

        monkeypatch.setattr(plat_mod, "check_fuse_available", raise_system_exit)
        monkeypatch.setattr("sys.platform", "win32")

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_doctor(doctor_args)

        assert exc_info.value.code == 2

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["overall"] == "degraded"
        assert data["checks"]["fuse_backend"]["ok"] is False
        assert data["checks"]["fuse_backend"]["installed"] is False
        assert data["checks"]["fuse_backend"]["name"] == "WinFSP"

    def test_doctor_not_ready_without_machine68k(self, fuse_mock, monkeypatch, doctor_args, capsys):
        """Missing machine68k alone = not_ready status, exit code 1."""
        import builtins
        import types

        import amifuse.fuse_fs as fuse_fs_mod
        import amifuse.platform as plat_mod

        # amitools available
        monkeypatch.setitem(sys.modules, "amitools", types.ModuleType("amitools"))

        # machine68k NOT available
        monkeypatch.delitem(sys.modules, "machine68k", raising=False)
        real_import = builtins.__import__

        def block_machine68k(name, *args, **kwargs):
            if name == "machine68k":
                raise ImportError("No module named 'machine68k'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", block_machine68k)

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
        assert data["checks"]["machine68k"]["ok"] is False
        assert data["checks"]["machine68k"]["available"] is False
        assert "machine68k" in data["missing"]

    def test_doctor_fusepy_version_fallback_unknown(self, fuse_mock, monkeypatch, doctor_args, capsys):
        """fusepy without __version__ reports version as 'unknown'."""
        import types

        import amifuse.fuse_fs as fuse_fs_mod
        import amifuse.platform as plat_mod

        # Core deps available
        monkeypatch.setitem(sys.modules, "amitools", types.ModuleType("amitools"))
        monkeypatch.setitem(sys.modules, "machine68k", types.ModuleType("machine68k"))

        # fusepy available but WITHOUT __version__
        fake_fuse = sys.modules["fuse"]
        if hasattr(fake_fuse, "__version__"):
            monkeypatch.delattr(fake_fuse, "__version__")

        # FUSE backend check passes
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)

        fuse_fs_mod.cmd_doctor(doctor_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["checks"]["fusepy"]["ok"] is True
        assert data["checks"]["fusepy"]["installed"] is True
        assert data["checks"]["fusepy"]["version"] == "unknown"

# ---------------------------------------------------------------------------
# TestCmdHash -- hash command tests
# ---------------------------------------------------------------------------


class TestCmdHash:
    """Tests for cmd_hash() subcommand."""

    @pytest.fixture
    def mock_bridge_for_hash(self, fuse_mock, monkeypatch):
        """Set up mocked bridge for hash tests."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd, read_only=True: (mock_bridge, None),
        )
        return mock_bridge, fuse_fs_mod

    def test_hash_json_output_structure(self, mock_bridge_for_hash, capsys):
        import hashlib

        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello", b""]
        mock_bridge.close_file.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S/Startup-Sequence",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "hash"
        assert "version" in data
        assert "hash" in data
        assert data["algorithm"] == "sha256"
        assert data["size"] == 5

    def test_hash_sha256_correct(self, mock_bridge_for_hash, capsys):
        import hashlib

        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello", b""]
        mock_bridge.close_file.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        expected = hashlib.sha256(b"hello").hexdigest()
        assert data["hash"] == expected

    def test_hash_md5(self, mock_bridge_for_hash, capsys):
        import hashlib

        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello", b""]
        mock_bridge.close_file.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            algorithm="md5",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["algorithm"] == "md5"
        expected = hashlib.md5(b"hello").hexdigest()
        assert data["hash"] == expected

    def test_hash_file_not_found(self, mock_bridge_for_hash, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="nonexistent.txt",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "FILE_NOT_FOUND"

    def test_hash_directory_rejected(self, mock_bridge_for_hash, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = {"dir_type": 2, "size": 0}

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "INVALID_ARGUMENT"

    def test_hash_unsupported_algorithm(self, fuse_mock):
        """Unsupported algorithm is caught by argparse choices validation."""
        import amifuse.fuse_fs as fuse_fs_mod

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.main(["hash", "test.hdf", "--file", "x", "--algorithm", "blake2"])
        # argparse exits with code 2 for invalid arguments
        assert exc_info.value.code == 2

    def test_hash_uses_sequential_read(self, mock_bridge_for_hash, capsys):
        """Verify seek_handle is called once at offset 0, and read_handle
        (not read_handle_at) is used for chunk reads."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = {"size": 10, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        # Two chunks: 10 bytes total
        mock_bridge.read_handle.side_effect = [b"hello", b"world", b""]
        mock_bridge.close_file.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_hash(args)

        # seek_handle should be called exactly once at offset 0
        mock_bridge.seek_handle.assert_called_once_with(0x1000, 0)
        # read_handle should be used (not read_handle_at)
        assert mock_bridge.read_handle.call_count >= 2
        mock_bridge.read_handle_at.assert_not_called()
        # close_file should always be called
        mock_bridge.close_file.assert_called_once_with(0x1000)

    def test_hash_human_output(self, mock_bridge_for_hash, capsys):
        """Human-readable output contains file path and hash, not JSON."""
        import hashlib

        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello", b""]
        mock_bridge.close_file.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file="S/Startup-Sequence",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out

        expected_hash = hashlib.sha256(b"hello").hexdigest()
        assert "S/Startup-Sequence" in output
        assert expected_hash in output
        # Must not look like JSON
        assert "status" not in output
        assert "{" not in output

    def test_hash_open_file_failure(self, mock_bridge_for_hash, capsys):
        """open_file returning None emits HANDLER_ERROR JSON."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "HANDLER_ERROR"

    def test_hash_bridge_exception_json(self, mock_bridge_for_hash, capsys):
        """Generic exception from bridge emits HANDLER_ERROR with message."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.side_effect = RuntimeError("handler crashed")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "HANDLER_ERROR"
        assert "handler crashed" in data["error"]["message"]

    def test_hash_empty_file(self, mock_bridge_for_hash, capsys):
        """Empty file (size=0) produces correct empty-data hash."""
        import hashlib

        mock_bridge, fuse_fs_mod = mock_bridge_for_hash
        mock_bridge.stat_path.return_value = {"size": 0, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.close_file.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="empty.txt",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_hash(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        expected = hashlib.sha256(b"").hexdigest()
        assert data["hash"] == expected
        assert data["size"] == 0
        assert data["bytes_read"] == 0
        # read_handle should NOT be called for empty file
        mock_bridge.read_handle.assert_not_called()
        # seek and close should still be called
        mock_bridge.seek_handle.assert_called_once_with(0x1000, 0)
        mock_bridge.close_file.assert_called_once_with(0x1000)


# ---------------------------------------------------------------------------
# TestCmdInspectJson -- JSON output for inspect subcommand
# ---------------------------------------------------------------------------


class TestCmdInspectJson:
    """Tests for --json output from cmd_inspect().

    Uses mock detect_adf / detect_iso to avoid needing real image files.
    """

    @pytest.fixture
    def mock_adf_info(self):
        """Return a mock ADFInfo-like object for DD floppy."""
        info = MagicMock()
        info.dos_type = 0x444F5300
        info.is_hd = False
        info.cylinders = 80
        info.heads = 2
        info.sectors_per_track = 11
        info.block_size = 512
        info.total_blocks = 1760
        return info

    @pytest.fixture
    def inspect_args(self, tmp_path):
        """Return a minimal args namespace for cmd_inspect."""
        image = tmp_path / "test.adf"
        image.write_bytes(b"\x00" * 512)
        args = MagicMock()
        args.image = image
        args.block_size = None
        args.full = False
        args.json = True
        return args

    @pytest.fixture
    def mock_inspect_deps(self, monkeypatch, mock_adf_info, fuse_mock):
        """Wire up amitools + rdb_inspect mocks for cmd_inspect tests."""
        import types

        import amifuse.fuse_fs as fuse_fs_mod

        fake_amitools = types.ModuleType("amitools")
        fake_amitools_fs = types.ModuleType("amitools.fs")
        fake_dostype = MagicMock()
        fake_dostype.num_to_tag_str.return_value = "DOS0"

        fake_amitools.fs = fake_amitools_fs
        fake_amitools_fs.DosType = fake_dostype

        monkeypatch.setitem(sys.modules, "amitools", fake_amitools)
        monkeypatch.setitem(sys.modules, "amitools.fs", fake_amitools_fs)
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

        fake_rdb_inspect = MagicMock()
        fake_rdb_inspect.detect_adf.return_value = mock_adf_info
        fake_rdb_inspect.detect_iso.return_value = None
        fake_rdb_inspect.detect_mbr.return_value = None
        fake_rdb_inspect.MBR_TYPE_AMIGA_RDB = 0x76
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb_inspect)

        return fuse_fs_mod, fake_rdb_inspect

    def test_inspect_adf_json(self, mock_inspect_deps, inspect_args, capsys):
        """ADF image with --json produces valid JSON envelope."""
        fuse_fs_mod, _ = mock_inspect_deps

        fuse_fs_mod.cmd_inspect(inspect_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "inspect"
        assert data["image_type"] == "adf"
        assert data["floppy_type"] == "DD"
        assert data["cylinders"] == 80
        assert data["heads"] == 2
        assert data["sectors_per_track"] == 11
        assert data["block_size"] == 512
        assert data["total_blocks"] == 1760
        assert data["dos_type"] == "DOS0"
        assert data["dos_type_raw"] == "0x444f5300"

    def test_inspect_adf_human_unchanged(self, mock_inspect_deps, inspect_args, capsys):
        """Human-readable ADF output is unchanged when --json is not set."""
        fuse_fs_mod, _ = mock_inspect_deps

        inspect_args.json = False

        fuse_fs_mod.cmd_inspect(inspect_args)
        output = capsys.readouterr().out

        with pytest.raises(json.JSONDecodeError):
            json.loads(output)

        assert "ADF Floppy Image:" in output
        assert "DD" in output
        assert "11 sectors/track" in output

    def test_inspect_json_image_not_found(self, fuse_mock, monkeypatch, capsys, tmp_path):
        """When image does not exist and --json is set, emit error envelope."""
        import types

        import amifuse.fuse_fs as fuse_fs_mod

        fake_amitools = types.ModuleType("amitools")
        fake_amitools_fs = types.ModuleType("amitools.fs")
        fake_dostype = MagicMock()
        fake_amitools.fs = fake_amitools_fs
        fake_amitools_fs.DosType = fake_dostype
        monkeypatch.setitem(sys.modules, "amitools", fake_amitools)
        monkeypatch.setitem(sys.modules, "amitools.fs", fake_amitools_fs)
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

        fake_rdb_inspect = MagicMock()
        fake_rdb_inspect.MBR_TYPE_AMIGA_RDB = 0x76
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb_inspect)

        args = MagicMock()
        args.image = tmp_path / "nonexistent.hdf"
        args.block_size = None
        args.full = False
        args.json = True

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_inspect(args)
        assert exc_info.value.code == 1
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "error"
        assert data["command"] == "inspect"
        assert data["error"]["code"] == "IMAGE_NOT_FOUND"
        assert "nonexistent.hdf" in data["error"]["message"]

    def test_inspect_json_has_version(self, mock_inspect_deps, inspect_args, capsys):
        """JSON output includes the version field."""
        fuse_fs_mod, _ = mock_inspect_deps

        fuse_fs_mod.cmd_inspect(inspect_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert "version" in data
        assert data["version"] == fuse_fs_mod.__version__

    # -- Gap 1: ISO JSON path --

    def test_inspect_iso_json(self, mock_inspect_deps, inspect_args, capsys):
        """ISO 9660 detection produces correct JSON envelope."""
        fuse_fs_mod, fake_rdb = mock_inspect_deps
        from dataclasses import dataclass

        @dataclass
        class ISOInfo:
            volume_id: str
            block_size: int
            total_blocks: int

        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = ISOInfo(
            volume_id="AMIGA_CD", block_size=2048, total_blocks=5000,
        )

        fuse_fs_mod.cmd_inspect(inspect_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "inspect"
        assert "version" in data
        assert data["image_type"] == "iso"
        assert data["volume_id"] == "AMIGA_CD"
        assert data["block_size"] == 2048
        assert data["total_blocks"] == 5000
        assert data["image"] == str(inspect_args.image)

    # -- Gap 2: RDB JSON path --

    def test_inspect_rdb_json(self, mock_inspect_deps, inspect_args, capsys):
        """Single-RDB detection produces correct JSON envelope with merged data."""
        fuse_fs_mod, fake_rdb = mock_inspect_deps

        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        fake_rdb.detect_mbr.return_value = None

        mock_blkdev = MagicMock()
        mock_rdisk = MagicMock()
        mock_rdisk.get_desc.return_value = {
            "partitions": [{"name": "DH0", "size_blocks": 100}],
        }
        mock_rdisk.rdb_warnings = []
        fake_rdb.open_rdisk.return_value = (mock_blkdev, mock_rdisk, None)

        fuse_fs_mod.cmd_inspect(inspect_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "inspect"
        assert "version" in data
        assert data["image_type"] == "rdb"
        assert data["image"] == str(inspect_args.image)
        # RDB data is merged into envelope (not nested under "rdbs")
        assert data["partitions"] == [{"name": "DH0", "size_blocks": 100}]
        assert "rdbs" not in data

    # -- Gap 3: ISO human-readable output --

    def test_inspect_iso_human(self, mock_inspect_deps, inspect_args, capsys):
        """ISO 9660 detection produces human-readable output (not JSON)."""
        fuse_fs_mod, fake_rdb = mock_inspect_deps
        from dataclasses import dataclass

        @dataclass
        class ISOInfo:
            volume_id: str
            block_size: int
            total_blocks: int

        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = ISOInfo(
            volume_id="AMIGA_CD", block_size=2048, total_blocks=5000,
        )
        inspect_args.json = False

        fuse_fs_mod.cmd_inspect(inspect_args)
        output = capsys.readouterr().out

        assert "ISO 9660 Image" in output
        assert "AMIGA_CD" in output
        assert "2048" in output
        assert "5000" in output
        # Human output must not contain JSON braces
        assert "{" not in output
        assert "}" not in output

    # -- Gap 4: RDB open failure JSON path --

    def test_inspect_rdb_open_failure_json(self, mock_inspect_deps, inspect_args, capsys):
        """open_rdisk IOError produces JSON error envelope with exit code 1."""
        fuse_fs_mod, fake_rdb = mock_inspect_deps

        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        fake_rdb.detect_mbr.return_value = None
        fake_rdb.open_rdisk.side_effect = IOError("No valid RDB found")

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_inspect(inspect_args)
        assert exc_info.value.code == 1

        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["status"] == "error"
        assert data["command"] == "inspect"
        assert data["error"]["code"] == "OPEN_FAILED"
        assert "No valid RDB found" in data["error"]["message"]
        assert data["image"] == str(inspect_args.image)

    # -- Gap 5: Multi-RDB JSON path --

    def test_inspect_multi_rdb_json(self, mock_inspect_deps, inspect_args, capsys):
        """Multiple 0x76 MBR partitions produce JSON with 'rdbs' array."""
        fuse_fs_mod, fake_rdb = mock_inspect_deps
        from dataclasses import dataclass

        @dataclass
        class FakeMBRPartition:
            index: int
            bootable: bool
            partition_type: int
            start_lba: int
            num_sectors: int

        @dataclass
        class FakeMBRInfo:
            partitions: list
            has_amiga_partitions: bool

        part0 = FakeMBRPartition(
            index=0, bootable=False, partition_type=0x76,
            start_lba=2048, num_sectors=100000,
        )
        part1 = FakeMBRPartition(
            index=1, bootable=False, partition_type=0x76,
            start_lba=200000, num_sectors=100000,
        )
        mbr_info = FakeMBRInfo(
            partitions=[part0, part1],
            has_amiga_partitions=True,
        )

        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        fake_rdb.detect_mbr.return_value = mbr_info

        # open_rdisk called twice (once per 0x76 partition)
        def make_rdisk_result(partition_index):
            mock_blkdev = MagicMock()
            mock_rdisk = MagicMock()
            mock_rdisk.get_desc.return_value = {
                "partitions": [{"name": f"DH{partition_index}"}],
            }
            mock_rdisk.rdb_warnings = []
            mbr_ctx = MagicMock()
            mbr_ctx.scheme = "emu68"
            mbr_ctx.offset_blocks = [part0, part1][partition_index].start_lba
            mbr_ctx.mbr_info = mbr_info
            mbr_ctx.mbr_partition = [part0, part1][partition_index]
            return (mock_blkdev, mock_rdisk, mbr_ctx)

        fake_rdb.open_rdisk.side_effect = [
            make_rdisk_result(0),
            make_rdisk_result(1),
        ]

        fuse_fs_mod.cmd_inspect(inspect_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "inspect"
        assert data["image_type"] == "rdb"
        assert data["image"] == str(inspect_args.image)
        # Multi-RDB: data stored as 'rdbs' array, not merged
        assert "rdbs" in data
        assert isinstance(data["rdbs"], list)
        assert len(data["rdbs"]) == 2
        assert data["rdbs"][0]["partitions"] == [{"name": "DH0"}]
        assert data["rdbs"][1]["partitions"] == [{"name": "DH1"}]

    # -- Gap 6: Verify image, version, status, command fields --

    def test_inspect_envelope_fields(self, mock_inspect_deps, inspect_args, capsys):
        """Verify all standard envelope fields are present and correctly typed."""
        fuse_fs_mod, fake_rdb = mock_inspect_deps
        from dataclasses import dataclass

        @dataclass
        class ISOInfo:
            volume_id: str
            block_size: int
            total_blocks: int

        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = ISOInfo(
            volume_id="TEST", block_size=2048, total_blocks=100,
        )

        fuse_fs_mod.cmd_inspect(inspect_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        # All standard envelope fields
        assert data["status"] == "ok"
        assert data["command"] == "inspect"
        assert isinstance(data["version"], str)
        assert data["version"].startswith("v")
        assert data["image"] == str(inspect_args.image)
        # image_type is always present for successful inspect
        assert "image_type" in data


# ---------------------------------------------------------------------------
# TestCmdRead -- read subcommand tests
# ---------------------------------------------------------------------------


class TestCmdRead:
    """Tests for cmd_read() subcommand."""

    @pytest.fixture
    def mock_bridge_for_read(self, fuse_mock, monkeypatch):
        """Set up mocked bridge for read tests."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd, read_only=True: (mock_bridge, None),
        )
        return mock_bridge, fuse_fs_mod

    def test_read_json_output_structure(self, mock_bridge_for_read, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello", b""]
        mock_bridge.close_file.return_value = None

        out_file = str(tmp_path / "output.bin")
        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S/Startup-Sequence",
            out=out_file,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_read(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "read"
        assert "version" in data
        assert data["size"] == 5
        assert data["bytes_read"] == 5
        assert data["output"] == out_file
        assert data["file"] == "S/Startup-Sequence"
        assert data["target"] == str(Path("/fake/test.hdf"))

    def test_read_writes_correct_data(self, mock_bridge_for_read, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"size": 11, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello world", b""]
        mock_bridge.close_file.return_value = None

        out_file = tmp_path / "output.bin"
        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file="test.txt",
            out=str(out_file),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_read(args)

        assert out_file.read_bytes() == b"hello world"

    def test_read_stdout_mode(self, mock_bridge_for_read, monkeypatch):
        from io import BytesIO

        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"size": 11, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"binary data", b""]
        mock_bridge.close_file.return_value = None

        fake_buffer = BytesIO()
        monkeypatch.setattr(sys, "stdout", MagicMock(buffer=fake_buffer))

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file="test.bin",
            out="-",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_read(args)

        assert fake_buffer.getvalue() == b"binary data"

    def test_read_default_output_name(self, mock_bridge_for_read, capsys, tmp_path,
                                      monkeypatch):
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello", b""]
        mock_bridge.close_file.return_value = None

        # Change to tmp_path so the default file gets written there
        monkeypatch.chdir(tmp_path)

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S/Startup-Sequence",
            out=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_read(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        # Default output name should be the basename
        assert data["output"] == "Startup-Sequence"
        # Verify the file was actually written
        assert (tmp_path / "Startup-Sequence").read_bytes() == b"hello"

    def test_read_file_not_found(self, mock_bridge_for_read, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="nonexistent.txt",
            out="output.bin",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_read(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "FILE_NOT_FOUND"

    def test_read_directory_rejected(self, mock_bridge_for_read, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"dir_type": 2, "size": 0}

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S",
            out="output.bin",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_read(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "IS_DIRECTORY"

    def test_read_open_file_fails(self, mock_bridge_for_read, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            out="output.bin",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_read(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "HANDLER_ERROR"

    def test_read_human_output(self, mock_bridge_for_read, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"size": 5, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello", b""]
        mock_bridge.close_file.return_value = None

        out_file = str(tmp_path / "output.bin")
        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file="S/Startup-Sequence",
            out=out_file,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_read(args)
        output = capsys.readouterr().out

        assert "Extracted:" in output
        assert "Size:" in output
        assert "Output:" in output

    def test_read_uses_sequential_read(self, mock_bridge_for_read, capsys, tmp_path):
        """Verify seek_handle is called once at offset 0, and read_handle
        (not read_handle_at) is used for chunk reads."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"size": 10, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = [b"hello", b"world", b""]
        mock_bridge.close_file.return_value = None

        out_file = str(tmp_path / "output.bin")
        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            out=out_file,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_read(args)

        # seek_handle should be called exactly once at offset 0
        mock_bridge.seek_handle.assert_called_once_with(0x1000, 0)
        # read_handle should be used (not read_handle_at)
        assert mock_bridge.read_handle.call_count >= 2
        mock_bridge.read_handle_at.assert_not_called()
        # close_file should always be called
        mock_bridge.close_file.assert_called_once_with(0x1000)

    def test_read_close_on_error(self, mock_bridge_for_read, capsys, tmp_path):
        """Verify close_file is called even when read_handle raises."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_read
        mock_bridge.stat_path.return_value = {"size": 100, "dir_type": -3}
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.seek_handle.return_value = None
        mock_bridge.read_handle.side_effect = RuntimeError("handler crashed")
        mock_bridge.close_file.return_value = None

        out_file = str(tmp_path / "output.bin")
        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            out=out_file,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_read(args)

        # close_file must still be called (try/finally)
        mock_bridge.close_file.assert_called_once_with(0x1000)

    def test_read_stdout_json_conflict(self, fuse_mock, capsys):
        """--out - with --json is rejected before bridge creation."""
        import amifuse.fuse_fs as fuse_fs_mod

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            out="-",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_read(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "STDOUT_JSON_CONFLICT"
        assert "Cannot use --out - with --json" in data["error"]["message"]


# ---------------------------------------------------------------------------
# TestEnsureParentDirs -- parent directory creation helper
# ---------------------------------------------------------------------------


class TestEnsureParentDirs:
    """Tests for _ensure_parent_dirs() helper."""

    def test_no_parents_needed(self, fuse_mock, monkeypatch):
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        fuse_fs_mod._ensure_parent_dirs(mock_bridge, "/test.txt")

        mock_bridge.stat_path.assert_not_called()
        mock_bridge.create_dir.assert_not_called()

    def test_single_parent_exists(self, fuse_mock, monkeypatch):
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        mock_bridge.stat_path.return_value = {"dir_type": 2, "size": 0}

        fuse_fs_mod._ensure_parent_dirs(mock_bridge, "/S/Startup-Sequence")

        mock_bridge.stat_path.assert_called_once_with("/S")
        mock_bridge.create_dir.assert_not_called()

    def test_single_parent_created(self, fuse_mock, monkeypatch):
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        root_lock = 100
        new_lock = 200
        mock_bridge.stat_path.return_value = None
        mock_bridge.locate_path.return_value = (root_lock, 0, [])
        mock_bridge.create_dir.return_value = (new_lock, 0)

        fuse_fs_mod._ensure_parent_dirs(mock_bridge, "/S/Startup-Sequence")

        mock_bridge.create_dir.assert_called_once_with(root_lock, "S")
        mock_bridge.free_lock.assert_any_call(new_lock)

    def test_single_parent_created_in_root(self, fuse_mock, monkeypatch):
        """When locate_path('/') returns lock=0, use locate(0, '') to
        obtain the root lock for create_dir()."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        root_lock = 100
        new_lock = 200
        mock_bridge.stat_path.return_value = None
        mock_bridge.locate_path.return_value = (0, 0, [])
        mock_bridge.locate.return_value = (root_lock, 0)
        mock_bridge.create_dir.return_value = (new_lock, 0)

        fuse_fs_mod._ensure_parent_dirs(mock_bridge, "/S/Startup-Sequence")

        mock_bridge.locate.assert_called_once_with(0, "")
        mock_bridge.create_dir.assert_called_once_with(root_lock, "S")
        mock_bridge.free_lock.assert_any_call(new_lock)

    def test_nested_parents_created(self, fuse_mock, monkeypatch):
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        mock_bridge.stat_path.return_value = None
        mock_bridge.locate_path.return_value = (100, 0, [])
        mock_bridge.create_dir.return_value = (200, 0)

        fuse_fs_mod._ensure_parent_dirs(mock_bridge, "/Devs/DOSDrivers/FAT95")

        assert mock_bridge.create_dir.call_count == 2

    def test_parent_is_file_error(self, fuse_mock, monkeypatch, capsys):
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        mock_bridge.stat_path.return_value = {"dir_type": -3, "size": 100}

        with pytest.raises(SystemExit):
            fuse_fs_mod._ensure_parent_dirs(mock_bridge, "/README/subfile",
                                            use_json=True)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "HANDLER_ERROR"

    def test_create_dir_fails_error(self, fuse_mock, monkeypatch, capsys):
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        mock_bridge.stat_path.return_value = None
        mock_bridge.locate_path.return_value = (100, 0, [])
        mock_bridge.create_dir.return_value = (0, 232)

        with pytest.raises(SystemExit):
            fuse_fs_mod._ensure_parent_dirs(mock_bridge, "/S/Startup-Sequence",
                                            use_json=True)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "HANDLER_ERROR"

    def test_debug_logging(self, fuse_mock, monkeypatch, capsys):
        """Verify debug output when creating directories."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        mock_bridge.stat_path.return_value = None
        mock_bridge.locate_path.return_value = (100, 0, [])
        mock_bridge.create_dir.return_value = (200, 0)

        fuse_fs_mod._ensure_parent_dirs(mock_bridge, "/S/Startup-Sequence",
                                        debug=True)
        output = capsys.readouterr().out
        assert "[amifuse] Created directory: /S" in output


# ---------------------------------------------------------------------------
# TestCmdWrite -- write command tests
# ---------------------------------------------------------------------------


class TestCmdWrite:
    """Tests for cmd_write() subcommand."""

    @pytest.fixture
    def mock_bridge_for_write(self, fuse_mock, monkeypatch):
        """Set up mocked bridge for write tests."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd, read_only=True: (mock_bridge, None),
        )
        # Also mock _ensure_parent_dirs to be a no-op by default
        monkeypatch.setattr(
            fuse_fs_mod, "_ensure_parent_dirs",
            lambda bridge, path, use_json=False, debug=False: None,
        )
        return mock_bridge, fuse_fs_mod

    def test_write_json_output_structure(self, mock_bridge_for_write, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 5
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_write(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert data["status"] == "ok"
        assert data["command"] == "write"
        assert data["size"] == 5
        assert data["bytes_written"] == 5
        assert "source" in data
        assert data["file"] == "test.txt"

    def test_write_calls_flush_volume(self, mock_bridge_for_write, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 5
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_write(args)
        mock_bridge.flush_volume.assert_called()

    def test_write_no_double_flush_on_success(self, mock_bridge_for_write, capsys,
                                               tmp_path):
        """On success, flush_volume is called exactly once."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 5
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_write(args)
        assert mock_bridge.flush_volume.call_count == 1

    def test_write_creates_bridge_with_read_only_false(self, fuse_mock, monkeypatch,
                                                        tmp_path, capsys):
        import amifuse.fuse_fs as fuse_fs_mod

        captured = {}
        mock_bridge = MagicMock()
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 5
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        def fake_create(args, cmd, read_only=True):
            captured["read_only"] = read_only
            return mock_bridge, None

        monkeypatch.setattr(fuse_fs_mod, "_create_bridge_from_args", fake_create)
        monkeypatch.setattr(
            fuse_fs_mod, "_ensure_parent_dirs",
            lambda bridge, path, use_json=False, debug=False: None,
        )

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_write(args)
        assert captured["read_only"] is False

    def test_write_source_not_found(self, fuse_mock, monkeypatch, capsys):
        import amifuse.fuse_fs as fuse_fs_mod

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input="/nonexistent/path/file.txt",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_write(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "SOURCE_NOT_FOUND"

    def test_write_source_is_directory(self, fuse_mock, monkeypatch, capsys, tmp_path):
        import amifuse.fuse_fs as fuse_fs_mod

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(tmp_path),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_write(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "INVALID_ARGUMENT"

    def test_write_open_file_fails(self, mock_bridge_for_write, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_write(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "HANDLER_ERROR"

    def test_write_negative_write_handle(self, mock_bridge_for_write, capsys, tmp_path):
        """Verify details dict structure for negative write_handle."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = -1
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_write(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "WRITE_ERROR"
        assert data["error"]["details"]["bytes_written"] == 0
        assert data["error"]["details"]["expected"] == 5

    def test_write_zero_write_handle(self, mock_bridge_for_write, capsys, tmp_path):
        """write_handle returning 0 (DOSFALSE) is a distinct failure."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 0
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_write(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "WRITE_ERROR"
        assert "DOSFALSE" in data["error"]["message"]
        assert data["error"]["details"]["bytes_written"] == 0
        assert data["error"]["details"]["expected"] == 5

    def test_write_partial_write(self, mock_bridge_for_write, capsys, tmp_path):
        """Partial write includes bytes_written and expected in details."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 50
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"x" * 100)

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_write(args)
        output = capsys.readouterr().out
        data = json.loads(output)
        assert data["error"]["code"] == "WRITE_ERROR"
        assert "disk full?" in data["error"]["message"]
        assert data["error"]["details"]["bytes_written"] == 50
        assert data["error"]["details"]["expected"] == 100

    def test_write_safety_warning_on_stderr(self, mock_bridge_for_write, capsys,
                                             tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 5
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_write(args)
        stderr = capsys.readouterr().err
        assert "WARNING" in stderr
        assert "backup" in stderr

    def test_write_human_output(self, mock_bridge_for_write, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 5
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_write(args)
        output = capsys.readouterr().out
        assert "Written:" in output
        assert "Source:" in output
        assert "Size:" in output
        assert "Bytes written:" in output

    def test_write_calls_ensure_parent_dirs(self, fuse_mock, monkeypatch, capsys,
                                             tmp_path):
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = MagicMock()
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 5
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd, read_only=True: (mock_bridge, None),
        )

        captured_calls = []

        def capture_ensure(bridge, path, use_json=False, debug=False):
            captured_calls.append(path)

        monkeypatch.setattr(fuse_fs_mod, "_ensure_parent_dirs", capture_ensure)

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="Devs/DOSDrivers/FAT95",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_write(args)
        assert captured_calls == ["/Devs/DOSDrivers/FAT95"]

    def test_write_close_file_on_error(self, mock_bridge_for_write, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.side_effect = RuntimeError("handler crashed")
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_write(args)
        mock_bridge.close_file.assert_called_once_with(0x1000)

    def test_write_flush_on_error_path(self, mock_bridge_for_write, capsys, tmp_path):
        mock_bridge, fuse_fs_mod = mock_bridge_for_write
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.side_effect = RuntimeError("handler crashed")
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        source = tmp_path / "source.txt"
        source.write_bytes(b"hello")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_write(args)
        # flush_volume should be called in the finally block on error paths
        mock_bridge.flush_volume.assert_called()

    def test_write_then_verify_with_hash(self, fuse_mock, monkeypatch, capsys,
                                          tmp_path):
        """Integration-style: write data, then hash it and verify match."""
        import hashlib
        import amifuse.fuse_fs as fuse_fs_mod

        written_data = bytearray()
        mock_bridge = MagicMock()
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None
        mock_bridge.seek_handle.return_value = None

        def fake_write(fh, data):
            written_data.extend(data)
            return len(data)

        mock_bridge.write_handle.side_effect = fake_write

        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd, read_only=True: (mock_bridge, None),
        )
        monkeypatch.setattr(
            fuse_fs_mod, "_ensure_parent_dirs",
            lambda bridge, path, use_json=False, debug=False: None,
        )

        source = tmp_path / "source.txt"
        original_data = b"Hello from AmiFUSE write test!"
        source.write_bytes(original_data)

        # Step 1: Write
        write_args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S/test.txt",
            input=str(source),
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_write(write_args)

        # Step 2: Set up mock for hash (reading back written data)
        mock_bridge.stat_path.return_value = {
            "size": len(written_data), "dir_type": -3
        }
        read_pos = [0]

        def fake_read(fh, size):
            start = read_pos[0]
            chunk = bytes(written_data[start:start + size])
            read_pos[0] += len(chunk)
            return chunk

        mock_bridge.read_handle.side_effect = fake_read

        # Clear stdout for hash output
        capsys.readouterr()

        hash_args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S/test.txt",
            algorithm="sha256",
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_hash(hash_args)
        output = capsys.readouterr().out
        data = json.loads(output)

        expected_hash = hashlib.sha256(original_data).hexdigest()
        assert data["hash"] == expected_hash


# ---------------------------------------------------------------------------
# TestHandlerBridgePortDrain -- port drain helper tests
# ---------------------------------------------------------------------------


class TestHandlerBridgePortDrain:
    def test_drain_preserves_child_ports(self, fuse_mock):
        from amifuse.fuse_fs import HandlerBridge

        class FakePortMgr:
            def __init__(self):
                self.ports = {
                    0x10: object(),
                    0x20: object(),
                    0x30: object(),
                    0x40: object(),
                }
                self.drained = []

            def has_msg(self, addr):
                return addr == 0x40 and addr not in self.drained

            def get_msg(self, addr):
                self.drained.append(addr)
                return 0xDEADBEEF

        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge.state = type("State", (), {"port_addr": 0x10, "reply_port_addr": 0x20})()
        bridge.proc_mgr = type(
            "ProcMgr",
            (),
            {
                "processes": {
                    1: type(
                        "Proc",
                        (),
                        {"is_child": True, "exited": False, "port_addr": 0x30},
                    )()
                }
            },
        )()
        pmgr = FakePortMgr()

        bridge._drain_non_essential_ports(pmgr)

        assert pmgr.drained == [0x40]


# ---------------------------------------------------------------------------
# TestCreateBridgeReadOnlyParam -- shared infrastructure tests
# ---------------------------------------------------------------------------


class TestCreateBridgeReadOnlyParam:
    """Tests for the read_only parameter on _create_bridge_from_args()."""

    def test_bridge_default_read_only(self, fuse_mock, monkeypatch, tmp_path):
        """Calling without read_only creates a bridge with read_only=True."""
        import amifuse.fuse_fs as fuse_fs_mod

        captured = {}

        # Create a real image file so exists() passes
        image = tmp_path / "test.hdf"
        image.write_bytes(b"\x00" * 1024)

        # Mock detect_adf/detect_iso
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        # Mock extract_embedded_driver
        temp_driver = tmp_path / "temp.handler"
        temp_driver.write_text("fake")
        monkeypatch.setattr(
            fuse_fs_mod, "extract_embedded_driver",
            lambda *a, **kw: (temp_driver, "DOS3", 0x444F5303),
        )

        # Mock HandlerBridge to capture kwargs
        mock_bridge = MagicMock()

        def fake_bridge(*args, **kwargs):
            captured.update(kwargs)
            return mock_bridge

        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", fake_bridge)

        args = argparse.Namespace(
            image=image,
            json=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod._create_bridge_from_args(args, "test")
        assert captured.get("read_only") is True

    def test_bridge_explicit_read_only_false(self, fuse_mock, monkeypatch, tmp_path):
        """Passing read_only=False creates a bridge with read_only=False."""
        import amifuse.fuse_fs as fuse_fs_mod

        captured = {}

        # Create a real image file
        image = tmp_path / "test.hdf"
        image.write_bytes(b"\x00" * 1024)

        # Mock detect_adf/detect_iso
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        # Mock extract_embedded_driver
        temp_driver = tmp_path / "temp.handler"
        temp_driver.write_text("fake")
        monkeypatch.setattr(
            fuse_fs_mod, "extract_embedded_driver",
            lambda *a, **kw: (temp_driver, "DOS3", 0x444F5303),
        )

        # Mock HandlerBridge to capture kwargs
        mock_bridge = MagicMock()

        def fake_bridge(*args, **kwargs):
            captured.update(kwargs)
            return mock_bridge

        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", fake_bridge)

        args = argparse.Namespace(
            image=image,
            json=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod._create_bridge_from_args(args, "test", read_only=False)
        assert captured.get("read_only") is False
