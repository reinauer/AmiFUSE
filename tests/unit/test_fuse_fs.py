"""Unit tests for amifuse.fuse_fs module.

Tests for platform-specific FUSE option handling. The fuse_mock fixture
from tests/conftest.py allows importing amifuse.fuse_fs without fusepy
installed.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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
