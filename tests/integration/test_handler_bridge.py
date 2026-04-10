"""HandlerBridge integration tests -- real fixtures, real m68k emulation.

These tests exercise code paths that unit tests with mocked machine68k
cannot cover: actual handler startup, packet exchange, resource lifecycle.
"""

import pytest
from pathlib import Path

from amifuse.fuse_fs import HandlerBridge

pytestmark = pytest.mark.integration


# A. Handler startup -- 3 tests


class TestHandlerBridgeStartup:
    """Test handler initialization and startup paths."""

    def test_pfs3_bridge_starts_and_lists_root(self, pfs3_image, pfs3_driver):
        """Verify HandlerBridge can start PFS3 handler and list root."""
        bridge = HandlerBridge(pfs3_image, pfs3_driver)
        try:
            entries = bridge.list_dir_path("/")
            assert isinstance(entries, list)
        finally:
            bridge.close()

    def test_bridge_with_nonexistent_image_raises(self, pfs3_driver):
        """HandlerBridge should raise when image file doesn't exist."""
        with pytest.raises((FileNotFoundError, OSError, SystemExit)):
            HandlerBridge(Path("/nonexistent/image.hdf"), pfs3_driver)

    def test_bridge_with_nonexistent_driver_raises(self, pfs3_image):
        """HandlerBridge should raise when driver binary doesn't exist."""
        with pytest.raises((FileNotFoundError, OSError, RuntimeError, SystemExit)):
            HandlerBridge(pfs3_image, Path("/nonexistent/pfs3aio"))


# B. Resource lifecycle -- 3 tests


class TestHandlerBridgeResourceLifecycle:
    """Test resource acquisition and release."""

    def test_close_is_idempotent(self, pfs3_image, pfs3_driver):
        """Calling close() multiple times should not raise."""
        bridge = HandlerBridge(pfs3_image, pfs3_driver)
        bridge.close()
        bridge.close()  # Second call should be no-op
        bridge.close()  # Third call should be no-op

    def test_close_releases_backend(self, pfs3_image, pfs3_driver):
        """After close(), backend should be None."""
        bridge = HandlerBridge(pfs3_image, pfs3_driver)
        bridge.close()
        assert bridge.backend is None

    def test_close_releases_vamos_runtime(self, pfs3_image, pfs3_driver):
        """After close(), vh (VamosHandlerRuntime) should be None."""
        bridge = HandlerBridge(pfs3_image, pfs3_driver)
        bridge.close()
        assert bridge.vh is None


# C. Directory operations -- 3 tests


class TestHandlerBridgeDirectoryOps:
    """Test directory listing operations."""

    def test_list_dir_path_returns_dicts(self, pfs3_image, pfs3_driver):
        """Each entry from list_dir_path should be a dict with name and dir_type."""
        bridge = HandlerBridge(pfs3_image, pfs3_driver)
        try:
            entries = bridge.list_dir_path("/")
            for entry in entries:
                assert "name" in entry
                assert "dir_type" in entry
        finally:
            bridge.close()

    def test_list_nonexistent_path_returns_empty(self, pfs3_image, pfs3_driver):
        """Listing a non-existent path should return an empty list."""
        bridge = HandlerBridge(pfs3_image, pfs3_driver)
        try:
            entries = bridge.list_dir_path("/nonexistent/deep/path")
            assert entries == []
        finally:
            bridge.close()

    def test_machine68k_probe_subprocess(self):
        """Meta-test: verify the machine68k subprocess probe works."""
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "-c", "import machine68k; machine68k.CPU(1)"],
            capture_output=True, timeout=10,
        )
        # On platforms where machine68k works, this passes.
        # On platforms where it segfaults, the integration conftest
        # would have already skipped us. So if we reach here, it must work.
        assert result.returncode == 0
