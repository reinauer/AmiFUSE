"""Unit tests for amifuse.fuse_fs module.

Tests for platform-specific FUSE option handling. The fuse_mock fixture
from tests/conftest.py allows importing amifuse.fuse_fs without fusepy
installed.
"""

import argparse
import builtins
import errno
import io
import json
import os
import signal
import sys
import tempfile
import types
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# The fuse_mock fixture stubs amitools.vamos with dummies, so the real
# MockMemory and InfoDataStruct have to be captured at import time, before
# any fixture patches sys.modules. TestQueryDiskInfo needs the genuine
# struct to pin the InfoData field offsets.
try:
    from amitools.vamos.machine.mock.mem import MockMemory as _RealMockMemory
    from amitools.vamos.libstructs.dos import InfoDataStruct as _RealInfoData
except ImportError:  # pragma: no cover - amitools submodule not checked out
    _RealMockMemory = None
    _RealInfoData = None


def _make_mock_bridge():
    """Build the bridge mock the cmd_* fixtures hand to their subcommand.

    A bare MagicMock is a trap for any bridge method whose result the caller
    unpacks. `mounted, dinfo, reason = bridge.is_mounted()` gets a MagicMock,
    which iterates as empty and raises ValueError; the subcommand's blanket
    `except Exception` turns that into a HANDLER_ERROR envelope and exits 1,
    so the test dies with SystemExit nowhere near the real cause. Stubbing it
    here means wiring is_mounted() into another subcommand cannot silently
    break that subcommand's whole test class.

    The default is a healthy, mounted PFS volume; tests that care override it.
    """
    import amifuse.fuse_fs as fuse_fs_mod

    bridge = MagicMock()
    bridge.is_mounted.return_value = fuse_fs_mod.MountState(
        True, {"disk_type": 0x50465303, "volume_node": 1}, None,
    )
    return bridge


# ---------------------------------------------------------------------------
# A. TestMountFuseOptions -- subtype guard tests
# ---------------------------------------------------------------------------


class TestFuseLazyLoad:
    def test_uses_injected_fuse_without_importing(self, monkeypatch):
        import amifuse.fuse_fs as fuse_fs_mod

        sentinel = object()
        monkeypatch.setattr(fuse_fs_mod, "FUSE", sentinel)

        assert fuse_fs_mod._require_fuse() is sentinel

    def test_imports_and_caches_fuse_on_demand(self, monkeypatch):
        import amifuse.fuse_fs as fuse_fs_mod

        sentinel = object()
        fake_fuse = types.ModuleType("fuse")
        fake_fuse.FUSE = sentinel
        monkeypatch.setitem(sys.modules, "fuse", fake_fuse)
        monkeypatch.setattr(fuse_fs_mod, "FUSE", None)

        assert fuse_fs_mod._require_fuse() is sentinel
        assert fuse_fs_mod.FUSE is sentinel

    @pytest.mark.parametrize("failure", [ImportError, OSError])
    def test_reports_unavailable_fuse(self, monkeypatch, failure):
        import amifuse.fuse_fs as fuse_fs_mod

        real_import = builtins.__import__

        def failing_import(name, *args, **kwargs):
            if name == "fuse":
                raise failure("fuse import failed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(fuse_fs_mod, "FUSE", None)
        monkeypatch.delitem(sys.modules, "fuse", raising=False)
        monkeypatch.setattr(builtins, "__import__", failing_import)

        with pytest.raises(SystemExit, match="native FUSE library"):
            fuse_fs_mod._require_fuse()

    def test_operations_defaults_match_fusepy(self):
        from amifuse.fuse_fs import _FuseOperations, FuseOSError

        operations = _FuseOperations()

        assert operations.bmap is None
        assert operations.lock is None
        assert operations.fsyncdir("/", False, 0) == 0
        with pytest.raises(FuseOSError) as exc_info:
            operations.readlink("/missing")
        assert exc_info.value.errno == errno.ENOENT


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

        monkeypatch.setattr("amifuse.windows_unmount.UnmountControl", MagicMock())
        captured = {"fuse_kwargs": None}

        # Patch FUSE to capture kwargs
        def fake_fuse(fs_instance, mountpoint, **kwargs):
            captured["fuse_kwargs"] = kwargs
            captured["mountpoint"] = mountpoint
            captured["operations"] = fs_instance

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

    @pytest.mark.parametrize("host", ["win32", "linux"])
    @pytest.mark.parametrize("debug", [False, True])
    def test_mount_failure_keeps_cleanup(
        self, monkeypatch, mock_mount_fuse_deps, tmp_path, host, debug
    ):
        from amifuse import fuse_fs, platform, windows_unmount

        driver, icon = tmp_path / "driver", tmp_path / "icon"
        driver.write_bytes(b"driver")
        icon.write_bytes(b"icon")
        monkeypatch.setattr(fuse_fs, "sys", SimpleNamespace(platform=host))
        monkeypatch.setattr(platform, "get_fuse_mountpoint", lambda mp: str(mp))
        monkeypatch.setattr(
            fuse_fs, "extract_embedded_driver",
            lambda *a, **kw: (driver, "DOS3", 0x444F5303),
        )
        monkeypatch.setattr(platform, "pre_generate_volume_icon", lambda *a, **kw: icon)
        failure = RuntimeError(1)
        fuse = MagicMock(side_effect=failure)
        monkeypatch.setattr(fuse_fs, "FUSE", fuse)
        bridge = fuse_fs.HandlerBridge.return_value
        control = windows_unmount.UnmountControl.return_value

        translated = host == "win32" and not debug
        with pytest.raises(SystemExit if translated else RuntimeError) as caught:
            fuse_fs.mount_fuse(
                Path("test.hdf"), None, Path("R:"), None,
                icons=True, debug=debug, foreground=True,
            )
        if translated:
            assert str(caught.value) == "WinFsp could not mount 'R:': 1"
        else:
            assert caught.value is failure
        fuse.assert_called_once()
        bridge.close.assert_called_once()
        if host == "win32":
            control.close.assert_called_once()
        else:
            windows_unmount.UnmountControl.assert_not_called()
        assert not driver.exists()
        assert not icon.exists()

    @pytest.mark.parametrize("elevated, expected", [(True, "\\\\.\\R:"), (False, "R:")])
    def test_windows_drive_passed_to_fuse(self, monkeypatch, mock_mount_fuse_deps, elevated, expected):
        from amifuse import platform, fuse_fs
        from types import SimpleNamespace

        monkeypatch.setattr(fuse_fs, "sys", SimpleNamespace(platform="win32"))
        monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
        monkeypatch.setattr(platform, "is_windows_admin", lambda: elevated)
        monkeypatch.setattr(Path, "mkdir", MagicMock(side_effect=AssertionError("drive mkdir")))
        fuse_fs.mount_fuse(Path("test.hdf"), Path("driver"), Path("r:\\"),
                           block_size=512, partition="DH0")
        assert mock_mount_fuse_deps["mountpoint"] == expected
        assert mock_mount_fuse_deps["fuse_kwargs"]["uid"] == -1
        assert mock_mount_fuse_deps["fuse_kwargs"]["gid"] == -1

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

    def test_mount_defaults_to_daemon_on_windows(self, monkeypatch, mock_mount_fuse_deps):
        """Windows defaults to daemon mode now that unmount is available."""
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
        assert kwargs["foreground"] is False

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

    @pytest.mark.parametrize("error", [
        OSError("cannot lock partition in /tmp/test.iso: busy"),
        ValueError("Partition DH0 overlaps RDB metadata"),
    ])
    @pytest.mark.parametrize("debug", [False, True])
    def test_mount_reports_bridge_refusal_without_traceback(
            self, monkeypatch, fuse_mock, error, debug):
        import amifuse.fuse_fs as fuse_fs_mod

        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = MagicMock(
            volume_id="TestISO", block_size=2048, cylinders=1, heads=1,
            sectors_per_track=1, total_blocks=1,
        )
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)
        monkeypatch.setitem(sys.modules, "amitools", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", MagicMock())

        import amifuse.platform as plat_mod
        monkeypatch.setattr(plat_mod, "check_fuse_available", lambda: None)
        monkeypatch.setattr(plat_mod, "validate_mountpoint", lambda mp: None)
        monkeypatch.setattr(plat_mod, "should_auto_create_mountpoint", lambda mp: True)
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", MagicMock(side_effect=error))
        mock_fuse = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "FUSE", mock_fuse)

        expected = type(error) if debug else SystemExit
        with pytest.raises(expected) as exc_info:
            fuse_fs_mod.mount_fuse(
                image=Path("/tmp/test.iso"),
                driver=Path("/tmp/test.handler"),
                mountpoint=Path("/mnt/test"),
                block_size=None,
                foreground=True,
                debug=debug,
            )

        if debug:
            assert exc_info.value is error
        else:
            assert str(exc_info.value) == f"Cannot mount {Path('/tmp/test.iso')}: {error}"
            assert exc_info.value.__cause__ is error
        mock_fuse.assert_not_called()


class TestFormatVolume:
    def test_format_stops_after_flush_and_syncs_backend(
            self, monkeypatch, fuse_mock, tmp_path):
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.hdf"
        image.write_bytes(b"")
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
            image=image,
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

    @pytest.mark.parametrize("make_error", [
        lambda mod: PermissionError(errno.EACCES, "Permission denied", "test.hdf"),
        lambda mod: mod.ImageInUseError(
            "test.hdf is in use by another AmiFUSE or vamos session"),
        lambda mod: ValueError("Partition DH0 overlaps RDB metadata"),
    ], ids=["read-only", "in-use", "lock-refused"])
    @pytest.mark.parametrize("debug", [False, True])
    def test_format_reports_bridge_refusal_without_traceback(
            self, monkeypatch, tmp_path, make_error, debug):
        """An image the handler cannot open for writing fails with one line.

        A read-only image, another session's lock and a refused partition
        lock all stop HandlerBridge before a handler exists, and --debug
        keeps the original exception. The "Error: ..." line deliberately
        matches the file commands and format's existing busy-image
        message, not mount's "Cannot mount <image>: ...".
        """
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.hdf"
        image.write_bytes(b"")
        error = make_error(fuse_fs_mod)
        monkeypatch.setattr(
            fuse_fs_mod,
            "get_partition_info",
            lambda image, block_size, partition: {"dostype": 0x50465303},
        )
        bridge_cls = MagicMock(side_effect=error)
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", bridge_cls)

        expected = type(error) if debug else SystemExit
        with pytest.raises(expected) as exc_info:
            fuse_fs_mod.format_volume(
                image=image,
                driver=Path("/tmp/test.handler"),
                block_size=None,
                partition="DH0",
                debug=debug,
            )

        if debug:
            assert exc_info.value is error
        else:
            assert str(exc_info.value) == f"Error: {error}"
            assert exc_info.value.__cause__ is error
        bridge_cls.assert_called_once()

    @pytest.mark.parametrize("kind", ["missing", "directory"])
    def test_format_reports_missing_image_or_directory(
            self, monkeypatch, tmp_path, kind):
        """A path that is not an image file fails with one plain line.

        The wording matches the file commands, rather than the RDB
        reader's "Can't read from image file" or "Empty image file
        detected!".
        """
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.hdf"
        if kind == "directory":
            image.mkdir()
        bridge_cls = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", bridge_cls)

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.main(["format", str(image), "DH0"])

        # A string exit code is printed to stderr with exit status 1.
        assert exc_info.value.code == {
            "missing": f"Error: image file not found: {image}",
            "directory": f"Error: image is a directory: {image}",
        }[kind]
        bridge_cls.assert_not_called()

    def test_format_reports_unusable_image_without_traceback(
            self, monkeypatch, tmp_path):
        """An image without a readable RDB fails with one line.

        Reading the partition's dostype is format's first look inside the
        image, so an unreadable file, or one with no RDB such as an ADF,
        fails there, before any handler starts.
        """
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.hdf"
        image.write_bytes(bytes(16 * 512))
        bridge_cls = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", bridge_cls)

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.format_volume(
                image=image,
                driver=tmp_path / "test.handler",
                block_size=None,
                partition="DH0",
            )

        assert str(exc_info.value).startswith(
            "Error: No valid RDB found in blocks 0-15 at ")
        assert isinstance(exc_info.value.__cause__, OSError)
        bridge_cls.assert_not_called()

    @pytest.mark.parametrize("failing, error", [
        ("get_partition_info", ValueError("block size mismatch: rdb=512 != device=1024")),
        ("extract_embedded_driver", OSError(errno.ENOSPC, "No space left on device")),
    ], ids=["partition-info", "embedded-driver"])
    @pytest.mark.parametrize("debug", [False, True])
    def test_format_reports_rdb_read_failure_without_traceback(
            self, monkeypatch, tmp_path, failing, error, debug):
        """Reading the RDB before the handler starts fails with one line.

        Both RDB reads report OSError and ValueError the same way: an
        "Error: ..." line, or the original exception with --debug.
        """
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.hdf"
        image.write_bytes(b"")
        monkeypatch.setattr(
            fuse_fs_mod,
            "get_partition_info",
            lambda image, block_size, partition: {"dostype": 0x50465303},
        )
        monkeypatch.setattr(fuse_fs_mod, failing, MagicMock(side_effect=error))
        bridge_cls = MagicMock()
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", bridge_cls)

        expected = type(error) if debug else SystemExit
        with pytest.raises(expected) as exc_info:
            fuse_fs_mod.format_volume(
                image=image,
                driver=None,
                block_size=None,
                partition="DH0",
                debug=debug,
            )

        if debug:
            assert exc_info.value is error
        else:
            assert str(exc_info.value) == f"Error: {error}"
            assert exc_info.value.__cause__ is error
        bridge_cls.assert_not_called()

    @pytest.mark.parametrize("failure, expected", [
        ("bridge-refused", SystemExit),
        ("unprintable-volname", UnicodeEncodeError),
    ], ids=["bridge-refused", "unprintable-volname"])
    def test_format_removes_extracted_driver_on_failure(
            self, monkeypatch, tmp_path, failure, expected):
        """The handler extracted from the RDB is removed when format fails.

        That holds for a refused image, and for a progress line the console
        cannot print, such as a volume name a cp1252 console cannot encode.
        """
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.hdf"
        image.write_bytes(b"")
        handler = tmp_path / "amifuse_test.handler"
        handler.write_bytes(b"handler")
        monkeypatch.setattr(
            fuse_fs_mod,
            "get_partition_info",
            lambda image, block_size, partition: {"dostype": 0x50465303},
        )
        monkeypatch.setattr(
            fuse_fs_mod,
            "extract_embedded_driver",
            lambda image, block_size, partition: (handler, "PFS3", 0x50465303),
        )
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge", MagicMock(
            side_effect=PermissionError(errno.EACCES, "Permission denied", str(image))))
        volname = "Empty"
        if failure == "unprintable-volname":
            monkeypatch.setattr(
                sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="cp1252"))
            volname = "Snow\u2603"

        with pytest.raises(expected):
            fuse_fs_mod.format_volume(
                image=image,
                driver=None,
                block_size=None,
                partition="DH0",
                volname=volname,
            )

        assert not handler.exists()


class TestExtractEmbeddedDriver:
    """The filesystem handler extracted from the RDB to a temp file."""

    DATA = bytes(range(256)) * 300

    @pytest.fixture(autouse=True)
    def rdisk(self, monkeypatch):
        import amifuse.rdb_inspect as rdb_inspect

        fs = MagicMock()
        fs.fshd.dos_type = 0x50465303
        fs.get_data.return_value = self.DATA
        rdisk = MagicMock()
        rdisk.find_partition_by_string.return_value.part_blk.dos_env.dos_type = 0x50465303
        rdisk.fs = [fs]
        monkeypatch.setattr(rdb_inspect, "find_partition_mbr_index", lambda *a: None)
        monkeypatch.setattr(
            rdb_inspect, "open_rdisk", lambda *a, **kw: (MagicMock(), rdisk, None))
        return rdisk

    def test_write_failure_removes_temp_file(self, monkeypatch, tmp_path):
        """A write the OS refuses closes and removes the temp file and names it."""
        import amifuse.fuse_fs as fuse_fs_mod

        handler = tmp_path / "amifuse_test.handler"
        fds = []

        # A descriptor the OS refuses to write through stands in for a full
        # temp directory, wherever the write fails.
        def read_only_mkstemp(suffix="", prefix="", dir=None, text=False):
            handler.write_bytes(b"")
            fds.append(os.open(handler, os.O_RDONLY))
            return fds[-1], str(handler)

        monkeypatch.setattr(tempfile, "mkstemp", read_only_mkstemp)

        with pytest.raises(OSError) as exc_info:
            fuse_fs_mod.extract_embedded_driver(tmp_path / "test.hdf", None, "DH0")

        # The message names the temp file, not the image.
        assert str(handler) in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, OSError)
        assert not handler.exists()
        with pytest.raises(OSError):
            os.fstat(fds[0])

    def test_short_writes_still_write_whole_handler(self, monkeypatch, tmp_path):
        """The driver is not written with a bare os.write, which can write short."""
        import amifuse.fuse_fs as fuse_fs_mod

        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        real_write = os.write
        monkeypatch.setattr(
            os, "write", lambda fd, data: real_write(fd, bytes(data[:4096])))

        path, _, _ = fuse_fs_mod.extract_embedded_driver(
            tmp_path / "test.hdf", None, "DH0")

        assert path.parent == tmp_path
        assert path.read_bytes() == self.DATA


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
        monkeypatch.setattr("amifuse.platform.is_stale_mountpoint", lambda path: False)
        import amifuse.fuse_fs as fuse_fs_mod

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/amiga")))

        assert "is not currently mounted" in str(exc_info.value)

    def test_unmount_uses_process_kill_without_unix_command(self, monkeypatch, fuse_mock):
        """Unix retains its process fallback; Windows uses cooperative control."""
        monkeypatch.setattr("os.path.ismount", lambda path: True)
        monkeypatch.setattr(
            "amifuse.platform.get_unmount_command",
            lambda mountpoint: [],
        )
        import amifuse.fuse_fs as fuse_fs_mod

        killed_pids = [42]
        monkeypatch.setattr(fuse_fs_mod, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(
            fuse_fs_mod, "kill_mount_owner_processes",
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

        monkeypatch.setattr(fuse_fs_mod, "sys", SimpleNamespace(platform="linux"))
        monkeypatch.setattr(
            fuse_fs_mod, "kill_mount_owner_processes",
            lambda mp: [],
        )

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_unmount(argparse.Namespace(mountpoint=Path("/mnt/amiga")))

        assert "No amifuse process found" in str(exc_info.value)

    def test_unmount_runs_for_stale_mountpoint(self, monkeypatch, fuse_mock):
        monkeypatch.setattr("os.path.ismount", lambda path: False)
        monkeypatch.setattr(
            "amifuse.platform.is_stale_mountpoint",
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
            if cmd[0] == "ps":
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


PKT = 0x9999        # every packet the mock launcher sends
STALE_PKT = 0x7777  # a packet whose request timed out earlier


def _reply(res1, res2=0, pkt=PKT):
    """One queued packet reply as poll_replies returns it: (msg, pkt, res1, res2)."""
    return [(0, pkt, res1, res2)]


# Reply lists for the read and seek tests below. dp_Res1 is a signed LONG.
READ_OK = _reply(5)                  # 5 bytes read
READ_EOF = _reply(0)                 # end of file
READ_ERROR = _reply(-1, 225)         # handler failed the read
SEEK_OK = _reply(0)                  # old position 0
SEEK_PAST_EOF = _reply(-1, 219)      # ERROR_SEEK_ERROR
SEEK_ERROR = _reply(-1, 225)         # any other refusal is a failure
NO_REPLY = []                        # _run_until_replies timed out
STALE_READ = _reply(5, pkt=STALE_PKT)  # late answer to a timed-out READ
STALE_SEEK = _reply(4, pkt=STALE_PKT)  # late answer to a timed-out SEEK


def _past_end(size):
    """Replies to a seek refused with ERROR_SEEK_ERROR in a file of size bytes.

    The bridge then seeks to the end and asks for the position there.
    """
    return [SEEK_PAST_EOF, SEEK_OK, _reply(size)]


class TestHandlerBridgeReadReplies:
    """Tell end of file (b"") apart from a failed read or seek (None)."""

    FH_ADDR = 0x3000
    PAYLOAD = b"hello world"
    LOCKS = [0x10, 0x11]

    def _bridge(self, fuse_fs_mod, *replies):
        """Build a bridge whose handler answers with the given reply lists in order."""
        import threading

        bridge = object.__new__(fuse_fs_mod.HandlerBridge)
        bridge._lock = threading.RLock()
        bridge._debug = False
        bridge.state = MagicMock()
        bridge.launcher = MagicMock()
        for send in ("send_seek_handle", "send_read_handle", "send_end_handle"):
            getattr(bridge.launcher, send).return_value = (PKT, 0)
        bridge.launcher.send_findinput.return_value = (self.FH_ADDR, PKT, 0)
        bridge.mem = MagicMock()
        bridge.mem.r_block.side_effect = lambda addr, n: self.PAYLOAD[:n]
        bridge._alloc_read_buf = MagicMock(return_value=SimpleNamespace(addr=0x2000))
        bridge._run_until_replies = MagicMock(side_effect=list(replies))
        # read_file opens the file itself: one FINDINPUT inside /dir.
        bridge._is_neg_cached = MagicMock(return_value=False)
        bridge.locate_path = MagicMock(return_value=(self.LOCKS[-1], 0, list(self.LOCKS)))
        bridge._alloc_bstr = MagicMock(return_value=(0x4000, 0x1000))
        bridge._alloc_fh = MagicMock(return_value=self.FH_ADDR)
        bridge._free_fh = MagicMock()
        bridge.free_lock = MagicMock()
        bridge._set_neg_cached = MagicMock()
        return bridge

    @pytest.mark.parametrize("read, expected", [
        (READ_OK, b"hello"),
        (READ_EOF, b""),
        (READ_ERROR, None),
        (NO_REPLY, None),
        (_reply(9), None),
    ], ids=["data", "eof", "handler-error", "no-reply", "more-than-asked"])
    def test_read_handle(self, fuse_mock, read, expected):
        """read_handle returns b"" only at end of file and None on failure."""
        import amifuse.fuse_fs as fuse_fs_mod

        bridge = self._bridge(fuse_fs_mod, read)

        assert bridge.read_handle(self.FH_ADDR, 8) == expected

    @pytest.mark.parametrize("seek, read, expected", [
        (SEEK_OK, READ_OK, b"hello"),
        (SEEK_OK, READ_EOF, b""),
        (SEEK_OK, READ_ERROR, None),
        (SEEK_OK, NO_REPLY, None),
        (SEEK_ERROR, None, None),
        (NO_REPLY, None, None),
    ], ids=["data", "eof", "read-error", "read-no-reply",
            "seek-error", "seek-no-reply"])
    def test_read_handle_at(self, fuse_mock, seek, read, expected):
        """read_handle_at checks the seek before it reads."""
        import amifuse.fuse_fs as fuse_fs_mod

        replies = [seek] if read is None else [seek, read]
        bridge = self._bridge(fuse_fs_mod, *replies)

        assert bridge.read_handle_at(self.FH_ADDR, 4, 8) == expected
        bridge.launcher.send_seek_handle.assert_called_once_with(
            bridge.state, self.FH_ADDR, 4, fuse_fs_mod.OFFSET_BEGINNING)
        assert bridge.launcher.send_read_handle.called == (read is not None)

    @pytest.mark.parametrize("reply, outcome, position", [
        (_reply(7), "OK", 7),
        (SEEK_PAST_EOF, "PAST_END", None),
        (SEEK_ERROR, "FAILED", None),
        (NO_REPLY, "FAILED", None),
    ], ids=["ok", "past-end", "error", "no-reply"])
    def test_seek_handle_reports_the_reply(self, fuse_mock, reply, outcome, position):
        """seek_handle returns the old position, or why the seek failed."""
        import amifuse.fuse_fs as fuse_fs_mod

        bridge = self._bridge(fuse_fs_mod, reply)

        result = bridge.seek_handle(self.FH_ADDR, 4, fuse_fs_mod.OFFSET_END)
        assert result == (fuse_fs_mod.SeekResult[outcome], position)
        bridge.launcher.send_seek_handle.assert_called_once_with(
            bridge.state, self.FH_ADDR, 4, fuse_fs_mod.OFFSET_END)

    @pytest.mark.parametrize("offset", [0x80000000, 2**32 + 4])
    def test_offset_past_long_range_is_refused(self, fuse_mock, offset):
        """An offset that dp_Arg2 cannot hold fails before any packet is sent."""
        import amifuse.fuse_fs as fuse_fs_mod

        # Sent anyway, the offset would wrap and read from another position.
        bridge = self._bridge(fuse_fs_mod, SEEK_OK, READ_OK)

        assert bridge.read_handle_at(self.FH_ADDR, offset, 8) is None
        bridge.launcher.send_seek_handle.assert_not_called()
        bridge._run_until_replies.assert_not_called()

    @pytest.mark.parametrize("probe, expected", [
        ([SEEK_OK, _reply(4)], b""),
        ([SEEK_OK, _reply(3)], b""),
        ([SEEK_OK, _reply(10)], None),
        ([SEEK_ERROR], None),
        ([NO_REPLY], None),
        ([SEEK_OK, SEEK_ERROR], None),
    ], ids=["at-end", "past-end", "inside-file", "end-seek-error",
            "end-seek-no-reply", "position-error"])
    def test_seek_error_is_eof_only_past_the_end(self, fuse_mock, probe, expected):
        """ERROR_SEEK_ERROR reads as end of file only if the offset is past the end."""
        import amifuse.fuse_fs as fuse_fs_mod

        # AROS afs also answers ERROR_SEEK_ERROR when it cannot follow a
        # damaged file's extension blocks, so the bridge measures the file.
        bridge = self._bridge(fuse_fs_mod, SEEK_PAST_EOF, *probe)

        assert bridge.read_handle_at(self.FH_ADDR, 4, 8) == expected
        seeks = [c.args[1:] for c in bridge.launcher.send_seek_handle.call_args_list]
        assert seeks == [
            (self.FH_ADDR, 4, fuse_fs_mod.OFFSET_BEGINNING),
            (self.FH_ADDR, 0, fuse_fs_mod.OFFSET_END),
            (self.FH_ADDR, 0, fuse_fs_mod.OFFSET_CURRENT),
        ][:1 + len(probe)]
        bridge.launcher.send_read_handle.assert_not_called()

    @pytest.mark.parametrize("replies, expected", [
        ([STALE_READ, SEEK_OK, READ_OK], b"hello"),
        ([STALE_READ + SEEK_OK, READ_OK], b"hello"),
        ([SEEK_OK, STALE_SEEK, READ_OK], b"hello"),
        ([STALE_READ, NO_REPLY], None),
        ([STALE_READ] * 9, None),
    ], ids=["alone", "with-answer", "before-read", "then-timeout", "endless"])
    def test_late_reply_is_not_taken_as_the_answer(self, fuse_mock, replies, expected):
        """A reply to an earlier packet is dropped, not read as this packet's answer."""
        import amifuse.fuse_fs as fuse_fs_mod

        # Taken as the seek's answer, a late READ byte count would pass for
        # the old position; taken as the read's, a seek position would pass
        # for a byte count.
        bridge = self._bridge(fuse_fs_mod, *replies)

        assert bridge.read_handle_at(self.FH_ADDR, 4, 8) == expected

    @pytest.mark.parametrize("offset, seeks, read, expected", [
        (0, [], READ_OK, b"hello"),
        (0, [], READ_EOF, b""),
        (0, [], READ_ERROR, None),
        (0, [], NO_REPLY, None),
        (4, [SEEK_OK], READ_OK, b"hello"),
        (4, _past_end(4), None, b""),
        (4, [SEEK_ERROR], None, None),
        (4, [NO_REPLY], None, None),
    ], ids=["data", "eof", "read-error", "read-no-reply",
            "seek-data", "seek-past-eof", "seek-error", "seek-no-reply"])
    def test_read_file_result_and_cleanup(self, fuse_mock, offset, seeks, read, expected):
        """read_file reports failure as None and still releases everything."""
        import amifuse.fuse_fs as fuse_fs_mod

        findinput = _reply(-1)  # DOSTRUE
        end = _reply(-1)
        replies = [findinput, *seeks]
        if read is not None:
            replies.append(read)
        replies.append(end)
        bridge = self._bridge(fuse_fs_mod, *replies)

        assert bridge.read_file("/dir/file", 8, offset) == expected
        assert bridge.launcher.send_seek_handle.called == bool(seeks)
        assert bridge.launcher.send_read_handle.called == (read is not None)
        bridge.launcher.send_end_handle.assert_called_once_with(bridge.state, self.FH_ADDR)
        bridge._free_fh.assert_called_once_with(self.FH_ADDR)
        assert [c.args[0] for c in bridge.free_lock.call_args_list] == self.LOCKS[::-1]
        assert bridge._run_until_replies.call_count == len(replies)

    @pytest.mark.parametrize("findinput, expected", [
        (_reply(0, 205), b""),
        (_reply(0, 204), b""),
        (_reply(0, 225), None),
        (NO_REPLY, None),
    ], ids=["object-not-found", "dir-not-found", "handler-error", "no-reply"])
    def test_read_file_open_failure(self, fuse_mock, findinput, expected):
        """Only a file that does not exist reads as empty when FINDINPUT fails."""
        import amifuse.fuse_fs as fuse_fs_mod

        bridge = self._bridge(fuse_fs_mod, findinput)

        assert bridge.read_file("/dir/file", 8, 0) == expected
        assert bridge._set_neg_cached.called == (expected is not None)
        bridge.launcher.send_read_handle.assert_not_called()
        bridge._free_fh.assert_called_once_with(self.FH_ADDR)
        assert [c.args[0] for c in bridge.free_lock.call_args_list] == self.LOCKS[::-1]

    @pytest.mark.parametrize("res2, expected", [(205, b""), (225, None)],
                             ids=["not-found", "handler-error"])
    def test_read_file_parent_failure(self, fuse_mock, res2, expected):
        """A parent directory that fails to lock for another reason is not empty."""
        import amifuse.fuse_fs as fuse_fs_mod

        bridge = self._bridge(fuse_fs_mod)
        bridge.locate_path.return_value = (0, res2, [])

        assert bridge.read_file("/dir/file", 8, 0) == expected
        assert bridge._set_neg_cached.called == (expected is not None)
        bridge.launcher.send_findinput.assert_not_called()


class TestFuseReadErrors:
    """AmigaFuseFS.read turns a failed bridge read into EIO.

    The FUSE layer caches each handle's file position to skip the seek on
    sequential reads. After a failed read or seek the handler's position is
    unknown, and an empty read may come from a refused seek past end of file
    that left the position where it was, so in all three cases the next read
    must seek explicitly rather than trust the cache.
    """

    FH_ADDR = 0x3000

    def _open(self, fs_with_mock_bridge):
        """Open /file on the mock bridge and return (fs, bridge, handle)."""
        import os

        fs, bridge = fs_with_mock_bridge
        bridge.open_file.return_value = (self.FH_ADDR, 0)
        return fs, bridge, fs.open("/file", os.O_RDONLY)

    def test_failed_read_raises_eio_and_next_read_seeks(self, fs_with_mock_bridge):
        """A failed sequential read raises EIO and the retry seeks first."""
        import amifuse.fuse_fs as fuse_fs_mod

        fs, bridge, handle = self._open(fs_with_mock_bridge)
        bridge.read_handle_at.return_value = b"hello"
        assert fs.read("/file", 5, 0, handle) == b"hello"

        bridge.read_handle.return_value = None
        with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
            fs.read("/file", 6, 5, handle)
        assert exc_info.value.args[0] == errno.EIO
        bridge.read_handle.assert_called_once_with(self.FH_ADDR, 6)

        bridge.read_handle_at.return_value = b" world"
        assert fs.read("/file", 6, 5, handle) == b" world"
        bridge.read_handle_at.assert_called_with(self.FH_ADDR, 5, 6)
        bridge.read_handle.assert_called_once()

    def test_failed_seek_raises_eio_and_next_read_seeks(self, fs_with_mock_bridge):
        """A failed seek-and-read raises EIO and forgets the cached position."""
        import amifuse.fuse_fs as fuse_fs_mod

        fs, bridge, handle = self._open(fs_with_mock_bridge)
        bridge.read_handle_at.return_value = b"hello"
        fs.read("/file", 5, 0, handle)

        bridge.read_handle_at.return_value = None
        with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
            fs.read("/file", 8, 100, handle)
        assert exc_info.value.args[0] == errno.EIO

        bridge.read_handle_at.return_value = b" world"
        assert fs.read("/file", 6, 5, handle) == b" world"
        bridge.read_handle_at.assert_called_with(self.FH_ADDR, 5, 6)
        bridge.read_handle.assert_not_called()

    def test_empty_read_does_not_cache_position(self, fs_with_mock_bridge):
        """An empty read past end of file does not claim the handle moved."""
        fs, bridge, handle = self._open(fs_with_mock_bridge)
        bridge.read_handle_at.return_value = b"hello"
        fs.read("/file", 5, 0, handle)

        bridge.read_handle_at.return_value = b""
        assert fs.read("/file", 8, 500, handle) == b""
        assert fs.read("/file", 8, 500, handle) == b""
        bridge.read_handle_at.assert_called_with(self.FH_ADDR, 500, 8)
        bridge.read_handle.assert_not_called()

    def test_path_read_failure_raises_eio(self, fs_with_mock_bridge):
        """A read without an open handle raises EIO when read_file fails."""
        import amifuse.fuse_fs as fuse_fs_mod

        fs, bridge = fs_with_mock_bridge
        bridge.read_file.return_value = None
        with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
            fs.read("/file", 8, 0, 12345)
        assert exc_info.value.args[0] == errno.EIO


class _FakeHandlerFile:
    """One open file with the seek and write behaviour of a real handler.

    ACTION_SEEK refuses to move past end of file with ERROR_SEEK_ERROR and
    leaves the handle where it was, as pfs3aio, SFS and FFS do. ACTION_WRITE
    writes at the handle's position and fails with ERROR_DISK_FULL when the
    file would grow past capacity. seek_reply, when set, answers every
    seek instead. Each send returns the packet address its reply carries.
    """

    def __init__(self, data, capacity=1 << 20, seek_reply=None):
        self.data = bytearray(data)
        self.pos = 0
        self.capacity = capacity
        self.seek_reply = seek_reply
        self.writes = []  # length of every ACTION_WRITE
        self.buf = b""
        self.reply = NO_REPLY

    def w_block(self, addr, data):
        self.buf = bytes(data)

    def seek(self, state, fh_addr, offset, mode):
        # OFFSET_BEGINNING, OFFSET_CURRENT, OFFSET_END
        base = {-1: 0, 0: self.pos, 1: len(self.data)}[mode]
        if self.seek_reply is not None:
            self.reply = self.seek_reply
        elif 0 <= base + offset <= len(self.data):
            self.reply = _reply(self.pos)
            self.pos = base + offset
        else:
            self.reply = _reply(-1, 219)
        return PKT, 0

    def write(self, state, fh_addr, addr, size):
        self.writes.append(size)
        if self.pos + size > self.capacity:
            self.reply = _reply(-1, 221)
        else:
            self.data[self.pos:self.pos + size] = self.buf[:size]
            self.pos += size
            self.reply = _reply(size)
        return PKT, 0


def _fake_write_bridge(fuse_fs_mod, handler, free_blocks=2048):
    """Build a bridge whose handler is the fake file.

    The volume reports free_blocks free 512-byte blocks, or no disk info
    when free_blocks is None.
    """
    import threading

    bridge = object.__new__(fuse_fs_mod.HandlerBridge)
    bridge._lock = threading.RLock()
    bridge._debug = False
    bridge.state = MagicMock()
    bridge.launcher = MagicMock()
    bridge.launcher.send_seek_handle.side_effect = handler.seek
    bridge.launcher.send_write_handle.side_effect = handler.write
    bridge.mem = MagicMock()
    bridge.mem.w_block.side_effect = handler.w_block
    bridge._alloc_read_buf = MagicMock(return_value=SimpleNamespace(addr=0x2000))
    bridge._run_until_replies = MagicMock(side_effect=lambda: handler.reply)
    info = None
    if free_blocks is not None:
        info = {"num_blocks": 4096, "num_blocks_used": 4096 - free_blocks,
                "bytes_per_block": 512}
    bridge.get_disk_info = MagicMock(return_value=info)
    return bridge


class TestHandlerBridgeWriteAt:
    """write_handle_at puts the data at the requested offset or fails.

    AmigaDOS cannot seek past end of file, so a write there must first fill
    the gap with zeros. A failure must never turn into a write at the
    handle's old position.
    """

    FH_ADDR = 0x3000
    ORIGINAL = b"0123456789"
    DATA = b"DATA"

    @pytest.mark.parametrize("offset", [0, 6, 10, 11, 110],
                             ids=["start", "across-eof", "at-eof",
                                  "one-past-eof", "past-eof"])
    def test_write_lands_at_offset(self, fuse_mock, offset):
        import amifuse.fuse_fs as fuse_fs_mod

        handler = _FakeHandlerFile(self.ORIGINAL)
        handler.pos = 3  # an earlier read left the handle inside the file
        bridge = _fake_write_bridge(fuse_fs_mod, handler)

        assert bridge.write_handle_at(self.FH_ADDR, offset, self.DATA) == (4, 0)
        # Earlier bytes are untouched and a gap reads as zeros.
        expected = bytearray(self.ORIGINAL.ljust(offset, b"\0"))
        expected[offset:offset + 4] = self.DATA
        assert handler.data == expected
        assert handler.pos == offset + 4
        # Only a write past end of file sends zeros first.
        gap = max(offset - len(self.ORIGINAL), 0)
        assert handler.writes == ([gap] if gap else []) + [4]

    def test_large_gap_is_filled_in_chunks(self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        chunk = fuse_fs_mod.ZERO_FILL_CHUNK
        handler = _FakeHandlerFile(self.ORIGINAL)
        bridge = _fake_write_bridge(fuse_fs_mod, handler)
        offset = len(self.ORIGINAL) + 2 * chunk + 5

        assert bridge.write_handle_at(self.FH_ADDR, offset, self.DATA) == (4, 0)
        assert handler.writes == [chunk, chunk, 5, 4]
        assert handler.data == self.ORIGINAL + bytes(2 * chunk + 5) + self.DATA
        # No buffer larger than one chunk, whatever the gap.
        sizes = [c.args[0] for c in bridge._alloc_read_buf.call_args_list]
        assert max(sizes) == chunk

    @pytest.mark.parametrize("refusal", [_reply(-1, 225), NO_REPLY],
                             ids=["seek-error", "seek-no-reply"])
    def test_refused_seek_fails_without_writing(self, fuse_mock, refusal):
        import amifuse.fuse_fs as fuse_fs_mod

        handler = _FakeHandlerFile(self.ORIGINAL, seek_reply=refusal)
        bridge = _fake_write_bridge(fuse_fs_mod, handler)

        assert bridge.write_handle_at(self.FH_ADDR, 4, self.DATA) == (-1, 0)
        assert handler.writes == []
        assert handler.data == self.ORIGINAL

    # need is the gap plus the data. The file ends 10 bytes into a 512-byte
    # block, so 502 more bytes fit before the handler needs a free block.
    @pytest.mark.parametrize("free_blocks, need, expected", [
        (2, 1024, (4, 0)),
        (None, 1024, (4, 0)),
        (1, 1024, (-1, 221)),
        (1, 512 + 502, (4, 0)),
        (1, 512 + 503, (-1, 221)),
    ], ids=["fits", "no-disk-info", "too-large", "fits-last-block",
            "one-byte-over"])
    def test_gap_must_fit_in_free_space(self, fuse_mock, free_blocks, need, expected):
        import amifuse.fuse_fs as fuse_fs_mod

        handler = _FakeHandlerFile(self.ORIGINAL)
        bridge = _fake_write_bridge(fuse_fs_mod, handler, free_blocks)
        offset = len(self.ORIGINAL) + need - len(self.DATA)

        assert bridge.write_handle_at(self.FH_ADDR, offset, self.DATA) == expected
        if expected[0] < 0:
            # Refused before anything was written.
            assert handler.writes == []
            assert handler.data == self.ORIGINAL
        else:
            assert len(handler.data) == offset + 4

    def test_disk_full_during_fill_keeps_handler_error(self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        chunk = fuse_fs_mod.ZERO_FILL_CHUNK
        # The volume reports room, but the handler runs out in the second chunk.
        handler = _FakeHandlerFile(self.ORIGINAL, capacity=chunk + 100)
        bridge = _fake_write_bridge(fuse_fs_mod, handler)
        offset = len(self.ORIGINAL) + 2 * chunk

        assert bridge.write_handle_at(self.FH_ADDR, offset, self.DATA) == (-1, 221)
        assert handler.writes == [chunk, chunk]

    # A late READ reply in the same batch must not pass for the write's.
    @pytest.mark.parametrize("reply, expected", [
        (_reply(4), (4, 0)),
        (_reply(-1, 221), (-1, 221)),
        (NO_REPLY, (-1, 0)),
        (_reply(-1, 221) + STALE_READ, (-1, 221)),
    ], ids=["written", "handler-error", "no-reply", "stale-reply"])
    def test_write_handle_reply_keeps_res2(self, fuse_mock, reply, expected):
        import amifuse.fuse_fs as fuse_fs_mod

        handler = _FakeHandlerFile(b"")
        bridge = _fake_write_bridge(fuse_fs_mod, handler)
        bridge._run_until_replies = MagicMock(side_effect=[reply, reply])

        assert bridge.write_handle_reply(self.FH_ADDR, self.DATA) == expected
        # write_handle keeps returning only the count, for the CLI.
        assert bridge.write_handle(self.FH_ADDR, self.DATA) == expected[0]


class TestFuseWriteErrors:
    """AmigaFuseFS.write reports a failed write with the closest errno.

    The FUSE layer caches each handle's position to skip the seek on
    sequential writes. After a failed write or seek the handler's position
    is unknown, so the next write must seek again.
    """

    FH_ADDR = 0x3000

    def _open(self, write_fs):
        import os

        fs, bridge = write_fs
        bridge.open_file.return_value = (self.FH_ADDR, 0)
        return fs, bridge, fs.open("/file", os.O_RDWR)

    @pytest.mark.parametrize("res2, expected", [
        (221, errno.ENOSPC),
        (214, errno.EROFS),
        (225, errno.EIO),
        (0, errno.EIO),
    ], ids=["disk-full", "write-protected", "other", "no-reply"])
    def test_failed_write_maps_errno_and_next_write_seeks(self, write_fs, res2, expected):
        import amifuse.fuse_fs as fuse_fs_mod

        fs, bridge, handle = self._open(write_fs)
        bridge.write_handle_at.return_value = (5, 0)
        assert fs.write("/file", b"hello", 0, handle) == 5

        bridge.write_handle_reply.return_value = (-1, res2)
        with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
            fs.write("/file", b" world", 5, handle)
        assert exc_info.value.args[0] == expected
        bridge.write_handle_reply.assert_called_once_with(self.FH_ADDR, b" world")

        # The retry seeks instead of trusting the cached position.
        bridge.write_handle_at.return_value = (6, 0)
        assert fs.write("/file", b" world", 5, handle) == 6
        bridge.write_handle_at.assert_called_with(self.FH_ADDR, 5, b" world")
        bridge.write_handle_reply.assert_called_once()

    def test_failed_seek_write_forgets_position(self, write_fs):
        import amifuse.fuse_fs as fuse_fs_mod

        fs, bridge, handle = self._open(write_fs)
        bridge.write_handle_at.return_value = (5, 0)
        fs.write("/file", b"hello", 0, handle)

        bridge.write_handle_at.return_value = (-1, 0)
        with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
            fs.write("/file", b"later", 100, handle)
        assert exc_info.value.args[0] == errno.EIO

        bridge.write_handle_at.return_value = (6, 0)
        fs.write("/file", b" world", 5, handle)
        bridge.write_handle_at.assert_called_with(self.FH_ADDR, 5, b" world")
        bridge.write_handle_reply.assert_not_called()

    def test_offset_beyond_seek_range_is_efbig(self, write_fs):
        import amifuse.fuse_fs as fuse_fs_mod

        fs, bridge, handle = self._open(write_fs)
        top = fuse_fs_mod._LONG_MAX
        # Packet arguments are 32 bits, so 4 GiB + 4 would land at offset 4.
        for offset in (top + 1, (1 << 32) + 4):
            with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
                fs.write("/file", b"data", offset, handle)
            assert exc_info.value.args[0] == errno.EFBIG
        bridge.write_handle_at.assert_not_called()

        bridge.write_handle_at.return_value = (4, 0)
        assert fs.write("/file", b"data", top, handle) == 4
        bridge.write_handle_at.assert_called_once_with(self.FH_ADDR, top, b"data")

    def test_writes_past_end_of_file_land_at_their_offsets(self, write_fs):
        import amifuse.fuse_fs as fuse_fs_mod

        # The reproduction from the bug report: two writes past end of file,
        # the second one sequential to the first.
        fs, bridge, handle = self._open(write_fs)
        original = bytes(range(32))
        handler = _FakeHandlerFile(original)
        fake = _fake_write_bridge(fuse_fs_mod, handler)
        bridge.write_handle_at = fake.write_handle_at
        bridge.write_handle_reply = fake.write_handle_reply

        assert fs.write("/file", b"A" * 8, 132, handle) == 8
        assert fs.write("/file", b"B" * 8, 140, handle) == 8
        assert handler.data == original + bytes(100) + b"A" * 8 + b"B" * 8
        # The zero fill, then both writes, the second without a seek.
        assert handler.writes == [100, 8, 8]


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
    """Tests for kill_mount_owner_processes() edge cases.

    kill_mount_owner_processes moved to platform.py in Phase 8.
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
        result = plat_mod.kill_mount_owner_processes(Path("/mnt/amiga"))
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

    @pytest.fixture(autouse=True)
    def _mock_amitools_dostype(self, monkeypatch):
        """Mock amitools.fs.DosType so the lazy import works without amitools installed."""
        fake_dostype = MagicMock()
        fake_dostype.num_to_tag_str.return_value = "DOS0"
        monkeypatch.setitem(sys.modules, "amitools", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

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

    @pytest.mark.parametrize("use_json", [True, False])
    def test_bridge_image_in_use_is_not_a_handler_error(
        self, fuse_mock, monkeypatch, tmp_path, capsys, use_json,
    ):
        """A busy image gets IMAGE_IN_USE, not HANDLER_ERROR."""
        import amifuse.fuse_fs as fuse_fs_mod
        ImageInUseError = fuse_fs_mod.ImageInUseError

        image = tmp_path / "test.hdf"
        image.write_bytes(b"\x00" * 1024)
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = None
        fake_rdb.detect_iso.return_value = None
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)
        message = "test.hdf is in use by another AmiFUSE or vamos session"
        monkeypatch.setattr(fuse_fs_mod, "HandlerBridge",
                            MagicMock(side_effect=ImageInUseError(message)))

        args = argparse.Namespace(
            image=image,
            json=use_json,
            partition=None,
            driver=tmp_path / "driver",
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit) as caught:
            fuse_fs_mod._create_bridge_from_args(args, "ls")
        if use_json:
            assert caught.value.code == 1
            error = json.loads(capsys.readouterr().out)["error"]
            assert error["code"] == "IMAGE_IN_USE"
            assert error["message"] == message
        else:
            assert str(caught.value) == f"Error: {message}"

    def test_bridge_adf_no_driver_json_error(
        self, fuse_mock, monkeypatch, tmp_path, capsys,
    ):
        """ADF without --driver: verify JSON body."""
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.adf"
        image.write_bytes(b"\x00" * 1024)

        fake_adf_info = MagicMock()
        fake_adf_info.dos_type = 0x444F5300  # DOS0
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = fake_adf_info
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        # Ensure no driver is found on disk
        monkeypatch.setattr(
            "amifuse.platform.find_driver_for_dostype", lambda dt: None,
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
# TestAdfDriverAutoResolution -- ADF driver auto-resolution tests
# ---------------------------------------------------------------------------


class TestAdfDriverAutoResolution:
    """Tests for ADF auto-resolution of driver via find_driver_for_dostype."""

    def test_adf_auto_resolves_driver(self, monkeypatch, tmp_path):
        """mount_fuse auto-resolves driver for ADF via find_driver_for_dostype."""
        import types
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.adf"
        image.write_bytes(b"\x00" * 1024)

        driver_file = tmp_path / "FastFileSystem"
        driver_file.write_bytes(b"\x00")

        fake_adf_info = MagicMock()
        fake_adf_info.dos_type = 0x444F5300  # DOS0
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = fake_adf_info
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        # Mock DosType with proper module hierarchy
        fake_dostype = types.ModuleType("amitools.fs.DosType")
        fake_dostype.num_to_tag_str = lambda dt: "DOS0"
        fake_amitools = types.ModuleType("amitools")
        fake_amitools_fs = types.ModuleType("amitools.fs")
        fake_amitools.fs = fake_amitools_fs
        fake_amitools_fs.DosType = fake_dostype
        monkeypatch.setitem(sys.modules, "amitools", fake_amitools)
        monkeypatch.setitem(sys.modules, "amitools.fs", fake_amitools_fs)
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

        # find_driver_for_dostype returns the driver
        monkeypatch.setattr(
            "amifuse.platform.find_driver_for_dostype",
            lambda dt: driver_file,
        )

        # Mock HandlerBridge to capture the driver arg
        captured = {}
        mock_bridge = MagicMock()

        def fake_bridge(*args, **kwargs):
            captured["args"] = args
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
        bridge, info = fuse_fs_mod._create_bridge_from_args(args, "test")
        # The auto-resolved driver path should be the second positional arg
        assert captured["args"][1] == str(driver_file)

    def test_adf_no_driver_found_error(self, monkeypatch, tmp_path):
        """mount_fuse raises SystemExit when no driver found for ADF."""
        import types
        import amifuse.fuse_fs as fuse_fs_mod

        image = tmp_path / "test.adf"
        image.write_bytes(b"\x00" * 1024)

        fake_adf_info = MagicMock()
        fake_adf_info.dos_type = 0x444F5300
        fake_rdb = MagicMock()
        fake_rdb.detect_adf.return_value = fake_adf_info
        monkeypatch.setitem(sys.modules, "amifuse.rdb_inspect", fake_rdb)

        fake_dostype = types.ModuleType("amitools.fs.DosType")
        fake_dostype.num_to_tag_str = lambda dt: "DOS0"
        fake_amitools = types.ModuleType("amitools")
        fake_amitools_fs = types.ModuleType("amitools.fs")
        fake_amitools.fs = fake_amitools_fs
        fake_amitools_fs.DosType = fake_dostype
        monkeypatch.setitem(sys.modules, "amitools", fake_amitools)
        monkeypatch.setitem(sys.modules, "amitools.fs", fake_amitools_fs)
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

        monkeypatch.setattr(
            "amifuse.platform.find_driver_for_dostype", lambda dt: None,
        )

        args = argparse.Namespace(
            image=image,
            json=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod._create_bridge_from_args(args, "test")
        assert "DOS0" in str(exc_info.value)
        assert "FastFileSystem" in str(exc_info.value)


# TestCmdLs -- ls command tests
# ---------------------------------------------------------------------------


class TestCmdLs:
    """Tests for cmd_ls() subcommand."""

    @pytest.fixture
    def mock_bridge_for_ls(self, fuse_mock, monkeypatch):
        """Set up mocked bridge for ls tests."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = _make_mock_bridge()
        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd, read_only=True: (mock_bridge, None),
        )
        return mock_bridge, fuse_fs_mod

    @staticmethod
    def _ls_args(**overrides):
        base = dict(
            image=Path("/fake/test.hdf"),
            json=True,
            path="/",
            recursive=False,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def test_ls_unmounted_root_json(self, mock_bridge_for_ls, capsys):
        """The default `amifuse ls image.hdf` must not report an empty volume."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, {"disk_type": 0x4E444F53, "volume_node": 0},
            "not a DOS disk",
        )
        mock_bridge.list_dir_path.return_value = []

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_ls(self._ls_args())
        data = json.loads(capsys.readouterr().out)

        assert exc_info.value.code == 1
        assert data["status"] == "error"
        assert data["error"]["code"] == "NOT_MOUNTED"
        assert "not a DOS disk" in data["error"]["message"]
        assert data["error"]["details"]["disk_type"] == 0x4E444F53
        mock_bridge.list_dir_path.assert_not_called()

    def test_ls_unmounted_recursive_json(self, mock_bridge_for_ls, capsys):
        """-R bypassed the stat fallback entirely, so it needs the guard too."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, {"disk_type": 0x4E444F53, "volume_node": 0},
            "not a DOS disk",
        )

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_ls(self._ls_args(recursive=True))
        data = json.loads(capsys.readouterr().out)

        assert exc_info.value.code == 1
        assert data["error"]["code"] == "NOT_MOUNTED"

    def test_ls_unmounted_human_exits_nonzero(self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, None, "no disk present",
        )

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_ls(self._ls_args(json=False))

        # SystemExit carries the message; a bare listing printed nothing before
        assert "no usable volume mounted" in str(exc_info.value.code)
        assert "no disk present" in str(exc_info.value.code)

    def test_ls_refused_disk_info_reports_dos_error(self, mock_bridge_for_ls, capsys):
        """A refusal has no id_DiskType, so res2 is the machine-readable handle."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, None, "not a DOS disk", 225)

        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_ls(self._ls_args())
        data = json.loads(capsys.readouterr().out)

        assert data["error"]["code"] == "NOT_MOUNTED"
        assert data["error"]["details"] == {"dos_error": 225}
        # The two provenances yield the same reason string, so details is
        # what tells a consumer which one it got
        assert "disk_type" not in data["error"]["details"]

    def test_ls_path_not_found_carries_unknown_mount_state(
            self, mock_bridge_for_ls, capsys):
        """A missing path on an unconfirmed volume must not look healthy."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            None, None, "handler did not report disk info")
        mock_bridge.list_dir_path.return_value = []
        mock_bridge.stat_path.return_value = None

        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_ls(self._ls_args(path="S"))
        data = json.loads(capsys.readouterr().out)

        assert exc_info.value.code == 1
        assert data["error"]["code"] == "FILE_NOT_FOUND"
        assert data["error"]["details"]["filesystem_responsive"] is None
        assert data["error"]["details"]["mount_state_reason"] == (
            "handler did not report disk info")

    def test_ls_path_not_found_on_healthy_volume_is_distinguishable(
            self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.list_dir_path.return_value = []
        mock_bridge.stat_path.return_value = None

        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_ls(self._ls_args(path="S"))
        data = json.loads(capsys.readouterr().out)

        assert data["error"]["code"] == "FILE_NOT_FOUND"
        assert data["error"]["details"]["filesystem_responsive"] is True
        assert "mount_state_reason" not in data["error"]["details"]

    def test_ls_unknown_mount_state_json(self, mock_bridge_for_ls, capsys):
        """An inconclusive mount is reported, but still lists and exits 0."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            None, None, "handler did not report disk info",
        )
        mock_bridge.list_dir_path.return_value = []

        fuse_fs_mod.cmd_ls(self._ls_args())
        data = json.loads(capsys.readouterr().out)

        assert data["status"] == "ok"
        assert data["entries"] == []
        assert data["filesystem_responsive"] is None

    def test_ls_unknown_mount_state_warns_on_empty_human(
            self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            None, None, "handler did not report disk info",
        )
        mock_bridge.list_dir_path.return_value = []

        fuse_fs_mod.cmd_ls(self._ls_args(json=False))
        captured = capsys.readouterr()

        assert captured.out == ""
        assert "mount state unknown" in captured.err
        assert "handler did not report disk info" in captured.err

    def test_ls_healthy_volume_reports_responsive(self, mock_bridge_for_ls, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_ls
        mock_bridge.list_dir_path.return_value = [
            {"name": "file1.txt", "dir_type": 0, "size": 100, "protection": 0},
        ]

        fuse_fs_mod.cmd_ls(self._ls_args())
        data = json.loads(capsys.readouterr().out)

        assert data["status"] == "ok"
        assert data["filesystem_responsive"] is True
        assert len(data["entries"]) == 1

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
# TestQueryDiskInfo -- ACTION_DISK_INFO memory reads
# ---------------------------------------------------------------------------


class TestQueryDiskInfo:
    """Drive _query_disk_info() through real InfoData memory.

    The is_mounted() tests below stub _query_disk_info, so they exercise the
    decision table but never the part that can silently be wrong: which
    InfoData offset each field is read from, and which slot of the reply
    tuple carries res1 and res2. Offset and BPTR/APTR confusion in direct
    memory reads is exactly what the repo's gotchas call out as hardest to
    debug after the fact, so these tests plant known values through the real
    InfoDataStruct field names and assert they come back out.
    """

    INFO_ADDR = 0x1000

    # Fields the handler is expected to fill in, plus decoys in the
    # neighbouring slots. The decoys are the point: reading id_DiskState (8)
    # or id_InUse (32) instead of id_DiskType (24) or id_VolumeNode (28)
    # must not go unnoticed.
    PLANTED = {
        "id_NumSoftErrors": 7,
        "id_UnitNumber": 3,
        "id_DiskState": 81,  # ID_VALIDATING -- decoy for disk_type
        "id_NumBlocks": 12288,
        "id_NumBlocksUsed": 4096,
        "id_BytesPerBlock": 512,
        "id_DiskType": 0x444F5303,  # 'DOS\3'
        "id_VolumeNode": 0xABCD00,
        "id_InUse": 1,  # decoy for volume_node
    }

    def _query(self, fuse_fs_mod, monkeypatch, replies, fields=None):
        """Run _query_disk_info() against a bridge backed by MockMemory."""
        import threading

        if _RealMockMemory is None:
            pytest.skip("amitools submodule not available")
        MockMemory = _RealMockMemory
        InfoDataStruct = _RealInfoData
        # fuse_mock bound a dummy InfoDataStruct into the module; the real
        # one is the whole point of these tests.
        monkeypatch.setattr(fuse_fs_mod, "InfoDataStruct", InfoDataStruct)

        mem = MockMemory(size_kib=64)
        bridge = object.__new__(fuse_fs_mod.HandlerBridge)
        bridge._lock = threading.RLock()
        bridge.mem = mem
        bridge.state = MagicMock()
        bridge.launcher = MagicMock()
        bridge.vh = MagicMock()
        info_mem = SimpleNamespace(addr=self.INFO_ADDR)
        bridge.vh.alloc.alloc_memory.return_value = info_mem

        def fake_run_until_replies():
            # The real handler fills InfoData in place before replying, so
            # plant the fields here -- the buffer is zeroed before the send.
            if fields:
                info = InfoDataStruct(mem, self.INFO_ADDR)
                for name, value in fields.items():
                    # Write through the field's own address rather than .val:
                    # id_VolumeNode is a BPTR whose typed setter rejects a
                    # raw int. The address still comes from the real struct,
                    # so a production offset mistake still fails the test.
                    mem.w32(getattr(info, name).addr, value)
            return replies

        bridge._run_until_replies = fake_run_until_replies
        result = bridge._query_disk_info()
        return result, bridge, info_mem

    def test_success_reads_every_field_from_its_own_offset(self, fuse_mock, monkeypatch):
        import amifuse.fuse_fs as fuse_fs_mod

        # (msg, pkt_addr, res1, res2) -- res1 nonzero means DOSTRUE
        replies = [(0, 0x9999, 1, 0)]
        (info, res2), bridge, info_mem = self._query(
            fuse_fs_mod, monkeypatch, replies, self.PLANTED)

        assert res2 is None
        assert info == {
            "num_blocks": 12288,
            "num_blocks_used": 4096,
            "bytes_per_block": 512,
            "disk_type": 0x444F5303,
            "volume_node": 0xABCD00,
        }
        # The decoy values must not have leaked into any returned field
        assert 81 not in info.values()
        assert 7 not in info.values()
        bridge.vh.alloc.free_memory.assert_called_once_with(info_mem)

    def test_dosfalse_reply_yields_res2_not_the_packet_address(self, fuse_mock, monkeypatch):
        """res2 is slot 3 of the reply; slot 1 is the packet address."""
        import amifuse.fuse_fs as fuse_fs_mod

        replies = [(0, 0x9999, 0, 225)]
        (info, res2), bridge, info_mem = self._query(
            fuse_fs_mod, monkeypatch, replies, self.PLANTED)

        # res1 == 0 means the buffer is not trustworthy even though this
        # test planted values in it
        assert info is None
        assert res2 == 225
        assert res2 != 0x9999
        bridge.vh.alloc.free_memory.assert_called_once_with(info_mem)

    def test_no_reply_is_silence_not_refusal(self, fuse_mock, monkeypatch):
        import amifuse.fuse_fs as fuse_fs_mod

        (info, res2), bridge, info_mem = self._query(fuse_fs_mod, monkeypatch, [])

        assert info is None
        assert res2 is None
        bridge.vh.alloc.free_memory.assert_called_once_with(info_mem)

    def test_zeroed_buffer_when_handler_fills_nothing(self, fuse_mock, monkeypatch):
        """A handler that replies DOSTRUE but fills nothing reads back zeros."""
        import amifuse.fuse_fs as fuse_fs_mod

        (info, res2), _, _ = self._query(fuse_fs_mod, monkeypatch, [(0, 0x9999, 1, 0)])

        assert res2 is None
        assert info == {
            "num_blocks": 0,
            "num_blocks_used": 0,
            "bytes_per_block": 0,
            "disk_type": 0,
            "volume_node": 0,
        }

    def test_end_to_end_refusal_is_not_mounted(self, fuse_mock, monkeypatch):
        """The offsets and the decision table agree on a real refusal."""
        import amifuse.fuse_fs as fuse_fs_mod

        mem_bridge = self._query(fuse_fs_mod, monkeypatch, [(0, 0x9999, 0, 225)])[1]
        state = mem_bridge.is_mounted()

        assert state.mounted is False
        assert state.info is None
        assert state.reason == "not a DOS disk"

    def test_end_to_end_ndos_disk_type_is_not_mounted(self, fuse_mock, monkeypatch):
        """An NDOS id_DiskType read from offset 24 drives the same verdict."""
        import amifuse.fuse_fs as fuse_fs_mod

        fields = dict(self.PLANTED, id_DiskType=0x4E444F53)
        mem_bridge = self._query(fuse_fs_mod, monkeypatch, [(0, 0x9999, 1, 0)], fields)[1]
        state = mem_bridge.is_mounted()

        assert state.mounted is False
        assert state.info["disk_type"] == 0x4E444F53
        assert state.reason == "not a DOS disk"


# ---------------------------------------------------------------------------
# TestHandlerBridgeMountState -- mounted-volume checks
# ---------------------------------------------------------------------------


class TestHandlerBridgeMountState:
    """Tests for HandlerBridge.is_mounted()."""

    @staticmethod
    def _mount_state(fuse_fs_mod, info, res2=None):
        bridge = object.__new__(fuse_fs_mod.HandlerBridge)
        bridge._query_disk_info = MagicMock(return_value=(info, res2))
        return bridge.is_mounted()

    def test_missing_disk_info_is_inconclusive(self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        state = self._mount_state(fuse_fs_mod, None)

        assert state.mounted is None
        assert state.info is None
        assert state.reason == "handler did not report disk info"

    @pytest.mark.parametrize(("disk_type", "expected_reason"), [
        (0xFFFFFFFF, "no disk present"),
        (0x42414400, "unreadable disk"),
        (0x4E444F53, "not a DOS disk"),
        (0x42555359, "disk busy or inhibited"),
        (0x4B49434B, "Kickstart disk has no DOS volume"),
    ])
    def test_unusable_disk_type_is_not_mounted(
            self, fuse_mock, disk_type, expected_reason):
        import amifuse.fuse_fs as fuse_fs_mod

        disk_info = {"disk_type": disk_type, "volume_node": 1}
        state = self._mount_state(fuse_fs_mod, disk_info)

        assert state.mounted is False
        assert state.info is disk_info
        assert state.reason == expected_reason

    def test_disk_type_is_authoritative_without_volume_node(self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        disk_info = {"disk_type": 0x50465303, "volume_node": 0}
        state = self._mount_state(fuse_fs_mod, disk_info)

        assert state.mounted is True
        assert state.info is disk_info
        assert state.reason is None

    def test_msdos_disk_type_can_be_mounted(self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        disk_info = {"disk_type": 0x4D534400, "volume_node": 0}
        state = self._mount_state(fuse_fs_mod, disk_info)

        assert state.mounted is True
        assert state.info is disk_info
        assert state.reason is None

    def test_unknown_disk_type_and_volume_node_is_inconclusive(
            self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        disk_info = {"disk_type": 0, "volume_node": 0}
        state = self._mount_state(fuse_fs_mod, disk_info)

        assert state.mounted is None
        assert state.info is disk_info
        assert state.reason == "handler reported no disk type or volume node"

    def test_volume_node_confirms_unknown_disk_type(self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        disk_info = {"disk_type": 0, "volume_node": 1}
        state = self._mount_state(fuse_fs_mod, disk_info)

        assert state.mounted is True
        assert state.info is disk_info
        assert state.reason is None

    def test_valid_volume_is_mounted(self, fuse_mock):
        import amifuse.fuse_fs as fuse_fs_mod

        disk_info = {"disk_type": 0x50465303, "volume_node": 1}
        state = self._mount_state(fuse_fs_mod, disk_info)

        assert state.mounted is True
        assert state.info is disk_info
        assert state.reason is None

    @pytest.mark.parametrize(("res2", "expected_reason"), [
        (225, "not a DOS disk"),  # ERROR_NOT_A_DOS_DISK
        (226, "no disk present"),  # ERROR_NO_DISK
        (218, "device not mounted"),  # ERROR_DEVICE_NOT_MOUNTED
    ])
    def test_refusal_with_diagnostic_error_is_not_mounted(
            self, fuse_mock, res2, expected_reason):
        """A DOSFALSE reply naming an unusable volume is a negative answer."""
        import amifuse.fuse_fs as fuse_fs_mod

        state = self._mount_state(fuse_fs_mod, None, res2)

        assert state.mounted is False
        assert state.info is None
        assert state.reason == expected_reason

    def test_unimplemented_packet_is_inconclusive(self, fuse_mock):
        """ERROR_ACTION_NOT_KNOWN says nothing about the volume."""
        import amifuse.fuse_fs as fuse_fs_mod

        state = self._mount_state(fuse_fs_mod, None, 209)

        assert state.mounted is None
        assert state.info is None
        assert "209" in state.reason

    def test_refusal_with_unknown_error_is_inconclusive(self, fuse_mock):
        """An undiagnostic refusal must not condemn a possibly healthy image."""
        import amifuse.fuse_fs as fuse_fs_mod

        state = self._mount_state(fuse_fs_mod, None, 202)

        assert state.mounted is None
        assert state.info is None
        assert "202" in state.reason

    def test_silence_is_distinct_from_refusal(self, fuse_mock):
        """No reply at all keeps the old inconclusive wording."""
        import amifuse.fuse_fs as fuse_fs_mod

        state = self._mount_state(fuse_fs_mod, None, None)

        assert state.mounted is None
        assert state.info is None
        assert state.reason == "handler did not report disk info"

    def test_get_disk_info_returns_only_the_dict(self, fuse_mock):
        """The public accessor keeps its Optional[Dict] contract."""
        import amifuse.fuse_fs as fuse_fs_mod

        bridge = object.__new__(fuse_fs_mod.HandlerBridge)
        disk_info = {"disk_type": 0x444F5301, "volume_node": 1}
        bridge._query_disk_info = MagicMock(return_value=(disk_info, None))

        assert bridge.get_disk_info() is disk_info

        bridge._query_disk_info = MagicMock(return_value=(None, 225))

        assert bridge.get_disk_info() is None


# ---------------------------------------------------------------------------
# TestCmdVerify -- verify command tests
# ---------------------------------------------------------------------------


class TestCmdVerify:
    """Tests for cmd_verify() subcommand."""

    @pytest.fixture
    def mock_bridge_for_verify(self, fuse_mock, monkeypatch):
        """Set up mocked bridge for verify tests."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = _make_mock_bridge()
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
        assert data["filesystem_responsive"] is True

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

    def test_verify_unmounted_volume_json(self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, {"disk_type": 0x4E444F53, "volume_node": 0},
            "not a DOS disk",
        )

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
        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert exc_info.value.code == 1
        assert data["error"]["code"] == "NOT_MOUNTED"
        assert "not a DOS disk" in data["error"]["message"]
        assert data["error"]["details"] == {
            "disk_type": 0x4E444F53,
            "volume_node": 0,
        }
        mock_bridge.stat_path.assert_not_called()
        mock_bridge.list_dir_path.assert_not_called()
        mock_bridge.volume_name.assert_not_called()

    def test_verify_unmounted_volume_human(self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, {"disk_type": 0x42414400, "volume_node": 0},
            "unreadable disk",
        )

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file=None,
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out

        assert exc_info.value.code == 1
        assert "Volume: (none)" in output
        assert "Filesystem responsive: NO" in output
        assert "unreadable disk" in output
        assert "id_DiskType: 0x42414400" in output
        mock_bridge.stat_path.assert_not_called()
        mock_bridge.list_dir_path.assert_not_called()
        mock_bridge.volume_name.assert_not_called()

    def test_verify_refused_disk_info_json(self, mock_bridge_for_verify, capsys):
        """A refusal has no InfoData, so res2 stands in for id_DiskType."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, None, "not a DOS disk", 225)

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
        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert exc_info.value.code == 1
        assert data["error"]["code"] == "NOT_MOUNTED"
        assert "not a DOS disk" in data["error"]["message"]
        # No InfoData, so no id_DiskType -- but res2 keeps the branch
        # machine-readable rather than leaving only prose in the message
        assert data["error"]["details"] == {"dos_error": 225}
        mock_bridge.stat_path.assert_not_called()
        mock_bridge.list_dir_path.assert_not_called()

    def test_verify_refused_disk_info_human(self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(False, None, "no disk present")

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file=None,
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out

        assert exc_info.value.code == 1
        assert "Volume: (none)" in output
        assert "Filesystem responsive: NO" in output
        assert "no disk present" in output
        assert "id_DiskType" not in output

    def test_verify_unmounted_file_json(self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, {"disk_type": 0x4E444F53, "volume_node": 0},
            "not a DOS disk",
        )

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="/",
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out
        data = json.loads(output)

        assert exc_info.value.code == 1
        assert data["error"]["code"] == "NOT_MOUNTED"
        mock_bridge.stat_path.assert_not_called()

    def test_verify_unmounted_file_human_names_the_file(
            self, mock_bridge_for_verify, capsys):
        """The diagnosis must stay attached to the file the user asked about."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            False, {"disk_type": 0x4E444F53, "volume_node": 0},
            "not a DOS disk",
        )

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file="S/Startup-Sequence",
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out

        assert exc_info.value.code == 1
        assert "File: S/Startup-Sequence" in output
        assert "Volume: (none)" not in output
        assert "Filesystem responsive: NO -- not a DOS disk" in output

    def test_verify_file_not_found_carries_unknown_mount_state(
            self, mock_bridge_for_verify, capsys):
        """FILE_NOT_FOUND must not look identical on a healthy volume."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            None, None, "handler did not report disk info",
        )
        mock_bridge.stat_path.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S/Startup-Sequence",
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit) as exc_info:
            fuse_fs_mod.cmd_verify(args)
        data = json.loads(capsys.readouterr().out)

        assert exc_info.value.code == 1
        assert data["error"]["code"] == "FILE_NOT_FOUND"
        assert data["error"]["details"]["filesystem_responsive"] is None
        assert data["error"]["details"]["mount_state_reason"] == (
            "handler did not report disk info")

    def test_verify_file_not_found_on_healthy_volume_is_distinguishable(
            self, mock_bridge_for_verify, capsys):
        """The confirmed-mounted case reports true, not null."""
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.stat_path.return_value = None

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="S/Startup-Sequence",
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        with pytest.raises(SystemExit):
            fuse_fs_mod.cmd_verify(args)
        data = json.loads(capsys.readouterr().out)

        assert data["error"]["code"] == "FILE_NOT_FOUND"
        assert data["error"]["details"]["filesystem_responsive"] is True
        assert "mount_state_reason" not in data["error"]["details"]

    def test_verify_unknown_mount_status_is_reported_for_file_json(
            self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            None, None, "handler did not report disk info",
        )
        mock_bridge.stat_path.return_value = {
            "dir_type": 2, "size": 0, "name": "",
            "protection": 0,
        }

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=True,
            file="/",
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
        assert data["filesystem_responsive"] is None
        mock_bridge.stat_path.assert_called_once_with("/")

    def test_verify_unknown_mount_status_is_reported_for_volume_json(
            self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            None, None, "handler did not report disk info",
        )
        mock_bridge.list_dir_path.return_value = []
        mock_bridge.volume_name.return_value = "Unknown"

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
        assert data["filesystem_responsive"] is None

    def test_verify_unknown_mount_status_is_reported_for_file_human(
            self, mock_bridge_for_verify, capsys):
        mock_bridge, fuse_fs_mod = mock_bridge_for_verify
        mock_bridge.is_mounted.return_value = fuse_fs_mod.MountState(
            None, None, "handler did not report disk info",
        )
        mock_bridge.stat_path.return_value = {
            "dir_type": 2, "size": 0, "name": "", "protection": 0,
        }

        args = argparse.Namespace(
            image=Path("/fake/test.hdf"),
            json=False,
            file="/",
            expect_size=None,
            partition=None,
            driver=None,
            block_size=None,
            debug=False,
        )
        fuse_fs_mod.cmd_verify(args)
        output = capsys.readouterr().out

        assert "Filesystem responsive: unknown" in output
        assert "handler did not report disk info" in output

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

    The doctor command moved to doctor.py in Phase 8 with a new JSON format:
    - checks is a list of dicts (not a dict keyed by name)
    - top-level keys: overall_status, platform, version, checks
    - no command, status, missing, suggestions keys
    """

    @pytest.fixture
    def doctor_args(self):
        """Return a minimal args namespace for cmd_doctor."""
        args = MagicMock()
        args.json = True
        args.fix = False
        return args

    def _get_check(self, checks_list, name):
        """Find a check by name in the checks list."""
        for c in checks_list:
            if c["name"] == name:
                return c
        return None

    def test_doctor_json_output_structure(self, fuse_mock, doctor_args, capsys):
        """JSON output has all required envelope keys and check categories."""
        import amifuse.fuse_fs as fuse_fs_mod

        try:
            fuse_fs_mod.cmd_doctor(doctor_args)
        except SystemExit:
            pass

        output = capsys.readouterr().out
        data = json.loads(output)

        # Required envelope keys
        for key in ("overall_status", "platform", "version", "checks"):
            assert key in data, f"Missing key: {key}"

        # checks is a list
        assert isinstance(data["checks"], list)

        # Required check categories present
        check_names = {c["name"] for c in data["checks"]}
        for name in ("python", "amitools", "machine68k", "fusepy", "fuse_backend"):
            assert name in check_names, f"Missing check: {name}"

        # Each check has expected fields
        for check in data["checks"]:
            for field in ("name", "status", "message", "fixable", "fix_description"):
                assert field in check, f"Missing field {field} in check {check.get('name')}"

    def test_doctor_human_output(self, fuse_mock, capsys):
        """Human-readable output contains expected text."""
        import amifuse.fuse_fs as fuse_fs_mod

        args = MagicMock()
        args.json = False
        args.fix = False

        try:
            fuse_fs_mod.cmd_doctor(args)
        except SystemExit:
            pass

        output = capsys.readouterr().out
        assert "environment check" in output
        assert "python" in output.lower()
        assert "Overall:" in output

# ---------------------------------------------------------------------------
# TestCmdHash -- hash command tests
# ---------------------------------------------------------------------------


class TestCmdHash:
    """Tests for cmd_hash() subcommand."""

    @pytest.fixture
    def mock_bridge_for_hash(self, fuse_mock, monkeypatch):
        """Set up mocked bridge for hash tests."""
        import amifuse.fuse_fs as fuse_fs_mod

        mock_bridge = _make_mock_bridge()
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
        fake_rdb_inspect.amiga_rdb_partitions.return_value = []
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

        mock_blkdev = MagicMock()
        mock_rdisk = MagicMock()
        mock_rdisk.get_desc.return_value = {
            "partitions": [{"name": "DH0", "size_blocks": 100}],
        }
        mock_rdisk.rdb_warnings = []
        fake_rdb.open_rdisk.return_value = (mock_blkdev, mock_rdisk, None)
        # build_rdisk_desc lives in the mocked module; pass through to get_desc
        fake_rdb.build_rdisk_desc.side_effect = lambda rdisk, mbr_ctx: rdisk.get_desc()

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
        fake_rdb.amiga_rdb_partitions.return_value = [part0, part1]
        # build_rdisk_desc lives in the mocked module; pass through to get_desc
        fake_rdb.build_rdisk_desc.side_effect = lambda rdisk, mbr_ctx: rdisk.get_desc()

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

        mock_bridge = _make_mock_bridge()
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

        mock_bridge = _make_mock_bridge()
        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd, read_only=True: (mock_bridge, None),
        )
        # Also mock _ensure_parent_dirs to be a no-op by default
        monkeypatch.setattr(
            fuse_fs_mod, "_ensure_parent_dirs",
            lambda bridge, path, use_json=False, debug=False, error_details=None: None,
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
        mock_bridge = _make_mock_bridge()
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
            lambda bridge, path, use_json=False, debug=False, error_details=None: None,
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

        mock_bridge = _make_mock_bridge()
        mock_bridge.open_file.return_value = (0x1000, 0x2000)
        mock_bridge.write_handle.return_value = 5
        mock_bridge.close_file.return_value = None
        mock_bridge.flush_volume.return_value = None

        monkeypatch.setattr(
            fuse_fs_mod, "_create_bridge_from_args",
            lambda args, cmd, read_only=True: (mock_bridge, None),
        )

        captured_calls = []

        def capture_ensure(bridge, path, use_json=False, debug=False, error_details=None):
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
        mock_bridge = _make_mock_bridge()
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
            lambda bridge, path, use_json=False, debug=False, error_details=None: None,
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

    @pytest.fixture(autouse=True)
    def _mock_amitools_dostype(self, monkeypatch):
        """Mock amitools.fs.DosType so the lazy import works without amitools installed."""
        fake_dostype = MagicMock()
        fake_dostype.num_to_tag_str.return_value = "DOS0"
        monkeypatch.setitem(sys.modules, "amitools", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs", MagicMock())
        monkeypatch.setitem(sys.modules, "amitools.fs.DosType", fake_dostype)

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


# ---------------------------------------------------------------------------
# TestPinnedStatCache -- regression tests for the create -> getattr race
# ---------------------------------------------------------------------------


@pytest.fixture
def write_fs(fuse_mock):
    """Write-enabled AmigaFuseFS over a MagicMock bridge.

    Returns (fs, bridge). The bridge has _write_enabled=True so cache TTLs
    are set to 0 in __init__, which is the configuration where the
    create -> getattr race manifested.
    """
    from amifuse.fuse_fs import AmigaFuseFS

    bridge = MagicMock()
    bridge._write_enabled = True
    bridge.state = None  # _check_handler_alive treats this as "not crashed"
    fs = AmigaFuseFS(bridge, debug=False, icons=False)
    return fs, bridge


class TestPinnedStatCache:
    """Stat cache must survive the create() -> getattr() -> release() window
    in write mode. Without pinning, libfuse2's post-create getattr probe
    falls through to the handler, which returns ERROR_OBJECT_IN_USE on the
    still-open FINDOUTPUT handle. The kernel then translates that to ENOENT
    and cp on Linux fails with "No such file or directory" while leaving a
    zero-byte stub on disk.
    """

    def test_write_mode_uses_zero_cache_ttls(self, write_fs):
        """Precondition: write mode does set _cache_ttl to 0 (the trigger)."""
        fs, _ = write_fs
        assert fs._cache_ttl == 0.0
        assert fs._neg_cache_ttl == 0.0
        assert fs._dir_cache_ttl == 0.0

    def test_create_populates_stat_cache_and_pins_path(self, write_fs):
        """create() must seed _stat_cache AND add the path to _pinned_stats
        so the entry survives the TTL=0 check.
        """
        fs, bridge = write_fs
        # bridge.open_file returns (fh_addr, parent_lock)
        bridge.open_file.return_value = (0x4abbc, 0)

        fh = fs.create("/Makefile", 0o100664)

        assert fh > 0
        assert "/Makefile" in fs._stat_cache
        assert "/Makefile" in fs._pinned_stats
        bridge.open_file.assert_called_once()

    def test_get_cached_stat_returns_pinned_entry_despite_zero_ttl(self, write_fs):
        """The bug: _get_cached_stat used to return None for entries set
        in write mode (TTL=0) even when they had just been written. With
        pinning, the entry survives.
        """
        fs, bridge = write_fs
        bridge.open_file.return_value = (0x4abbc, 0)
        fs.create("/Makefile", 0o100664)

        cached = fs._get_cached_stat("/Makefile")

        assert cached is not None, (
            "create()-primed entry must be visible to the immediate "
            "follow-up getattr; without pinning libfuse2 returns ENOENT "
            "to userspace and cp errors out."
        )
        assert cached["st_mode"] == 0o100644

    def test_getattr_after_create_does_not_hit_handler(self, write_fs):
        """Whole-stack reproduction: getattr right after create() must be
        served from the pinned cache, not from bridge.stat_path() (which
        would return ERROR_OBJECT_IN_USE on the still-open handle).
        """
        fs, bridge = write_fs
        bridge.open_file.return_value = (0x4abbc, 0)
        fs.create("/Makefile", 0o100664)

        # Tripwire: any call to stat_path means the cache miss happened
        # and the bug is back. The actual bug path returned None here.
        bridge.stat_path.side_effect = AssertionError(
            "post-create getattr fell through to handler instead of "
            "hitting the pinned stat cache"
        )

        attrs = fs.getattr("/Makefile")

        assert attrs["st_mode"] == 0o100644
        bridge.stat_path.assert_not_called()

    def test_release_unpins_path(self, write_fs):
        """After release(), the file handle is closed and PFS3 can serve
        fresh stats again; the pinned entry must be dropped so subsequent
        getattr re-queries the handler (and sees real size, mtime, etc.).
        """
        fs, bridge = write_fs
        bridge.open_file.return_value = (0x4abbc, 0)
        bridge.close_file.return_value = None
        fh = fs.create("/Makefile", 0o100664)

        fs.release("/Makefile", fh)

        assert "/Makefile" not in fs._pinned_stats
        # In write mode (TTL=0) the entry itself is also dropped, so the
        # next getattr will go to the handler.
        assert "/Makefile" not in fs._stat_cache
        bridge.close_file.assert_called_once_with(0x4abbc)

    def test_write_keeps_pinned_size_in_sync(self, write_fs):
        """While a file is pinned (created but not yet released), write()
        must update the cached size so a concurrent getattr does not
        observe size=0 after data has been written.
        """
        fs, bridge = write_fs
        bridge.open_file.return_value = (0x4abbc, 0)
        # write() picks write_handle_at when entry["pos"] is None (which it
        # is right after create()).
        bridge.write_handle_at.return_value = (100, 0)
        fh = fs.create("/Makefile", 0o100664)
        # Initial primed size is 0.
        assert fs._stat_cache["/Makefile"][1]["st_size"] == 0

        fs.write("/Makefile", b"x" * 100, 0, fh)

        assert fs._stat_cache["/Makefile"][1]["st_size"] == 100

    @pytest.mark.parametrize("length", [0, 2000], ids=["shrink", "grow"])
    def test_truncate_keeps_pinned_size_in_sync(self, write_fs, length):
        """ftruncate on a pinned file must set the cached size too.
        libfuse2's setattr reply and WinFsp's append offset come from
        getattr, so a stale size would send the next O_APPEND write past
        the real end of file, where the gap is filled with zeros.
        """
        fs, bridge = write_fs
        bridge.open_file.return_value = (0x4abbc, 0)
        bridge.write_handle_at.return_value = (1000, 0)
        bridge.set_handle_size.return_value = length
        fh = fs.create("/log", 0o100664)
        fs.write("/log", b"x" * 1000, 0, fh)

        fs.truncate("/log", length, fh)

        assert fs.getattr("/log")["st_size"] == length

    def test_unlink_discards_pin(self, write_fs):
        """unlink() must clean up _pinned_stats so a later create() of the
        same name starts from a fresh slate.
        """
        fs, bridge = write_fs
        bridge.open_file.return_value = (0x4abbc, 0)
        bridge.close_file.return_value = None
        bridge.locate_path.return_value = (0x6855, 0, [])
        bridge.delete_object.return_value = (-1, 0)  # res1=-1 means success
        fh = fs.create("/Makefile", 0o100664)
        fs.release("/Makefile", fh)
        # Manually re-pin to simulate the "pinned but not yet released" case
        # so we can prove unlink scrubs it.
        fs._pinned_stats.add("/Makefile")

        fs.unlink("/Makefile")

        assert "/Makefile" not in fs._pinned_stats
        assert "/Makefile" not in fs._stat_cache

    def test_rename_discards_pins_on_both_paths(self, write_fs):
        """rename() must clean both source and destination pin entries to
        avoid stale stat being served under the new name.
        """
        fs, bridge = write_fs
        bridge.locate_path.return_value = (0x6855, 0, [])
        bridge.rename_object.return_value = (-1, 0)
        fs._pinned_stats.update({"/old", "/new"})
        fs._stat_cache["/old"] = (0.0, {"st_size": 1})
        fs._stat_cache["/new"] = (0.0, {"st_size": 2})

        fs.rename("/old", "/new")

        assert "/old" not in fs._pinned_stats
        assert "/new" not in fs._pinned_stats
        assert "/old" not in fs._stat_cache
        assert "/new" not in fs._stat_cache


class TestCacheLocking:
    """Verify _cache_lock protects cache dicts from concurrent access."""

    def test_cache_lock_exists_in_init(self):
        """AmigaFuseFS.__init__ creates a _cache_lock RLock."""
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS.__init__)
        assert "_cache_lock" in source
        assert "RLock()" in source

    def test_get_cached_stat_uses_lock(self):
        """_get_cached_stat acquires _cache_lock."""
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS._get_cached_stat)
        assert "_cache_lock" in source

    def test_set_cached_stat_uses_lock(self):
        """_set_cached_stat acquires _cache_lock."""
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS._set_cached_stat)
        assert "_cache_lock" in source

    def test_is_neg_cached_uses_lock(self):
        """_is_neg_cached acquires _cache_lock."""
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS._is_neg_cached)
        assert "_cache_lock" in source

    def test_set_neg_cached_uses_lock(self):
        """_set_neg_cached acquires _cache_lock."""
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS._set_neg_cached)
        assert "_cache_lock" in source


# ---------------------------------------------------------------------------
# TestStatfs -- disk space reporting
# ---------------------------------------------------------------------------


class TestStatfs:
    """Verify statfs implementation."""

    def test_statfs_method_exists(self):
        """AmigaFuseFS has a statfs method."""
        from amifuse.fuse_fs import AmigaFuseFS
        assert hasattr(AmigaFuseFS, 'statfs')

    def test_get_disk_info_method_exists(self):
        """HandlerBridge has a get_disk_info method."""
        from amifuse.fuse_fs import HandlerBridge
        assert hasattr(HandlerBridge, 'get_disk_info')

    def test_statfs_returns_required_keys(self):
        """statfs returns dict with all required statvfs keys."""
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS.statfs)
        for key in ['f_bsize', 'f_frsize', 'f_blocks', 'f_bfree', 'f_bavail', 'f_namemax']:
            assert key in source

    def test_disk_info_cache_ttl_readonly(self):
        """Read-only mount uses infinite disk info cache TTL."""
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS.__init__)
        assert "_disk_info_ttl" in source

    def test_disk_info_cache_ttl_write(self):
        """Write mount uses 30s disk info cache TTL."""
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS.__init__)
        assert "30.0" in source or "30" in source


# ---------------------------------------------------------------------------
# TestFuseInit -- shell notification integration
# ---------------------------------------------------------------------------


class TestFuseInit:
    """Verify init() method exists for shell notification."""

    def test_init_method_exists(self):
        from amifuse.fuse_fs import AmigaFuseFS
        assert hasattr(AmigaFuseFS, "init")

    def test_destroy_has_notification(self):
        import inspect
        from amifuse.fuse_fs import AmigaFuseFS
        source = inspect.getsource(AmigaFuseFS.destroy)
        assert "notify_shell_drive_change" in source
