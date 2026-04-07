"""HandlerBridge integration tests -- real fixtures, real m68k emulation.

These tests exercise code paths that unit tests with mocked machine68k
cannot cover: actual handler startup, packet exchange, resource lifecycle.
"""

import pytest
from pathlib import Path

from amifuse.fuse_fs import HandlerBridge

pytestmark = pytest.mark.integration


# A. Handler startup -- 4 tests


class TestHandlerBridgeStartup:
    """Test handler initialization and startup paths."""

    def test_pfs3_bridge_starts_and_lists_root(self, pfs3_8mb_image, pfs3_driver):
        """Verify HandlerBridge can start PFS3 handler and list root."""
        bridge = HandlerBridge(pfs3_8mb_image, pfs3_driver)
        try:
            entries = bridge.list_dir_path("/")
            assert isinstance(entries, list)
        finally:
            bridge.close()

    def test_pfs3_unformatted_partition_lists_empty(self, pfs3_test_image, pfs3_driver):
        """Unformatted PFS3 partition should start but list zero entries.

        The handler accepts the disk (the partition has a valid PFS3 signature
        from creation) but there are no user files, so root listing is empty.
        """
        bridge = HandlerBridge(pfs3_test_image, pfs3_driver)
        try:
            entries = bridge.list_dir_path("/")
            assert entries == []
        finally:
            bridge.close()

    def test_bridge_with_nonexistent_image_raises(self, pfs3_driver):
        """HandlerBridge should raise when image file doesn't exist."""
        with pytest.raises((FileNotFoundError, OSError, SystemExit)):
            HandlerBridge(Path("/nonexistent/image.hdf"), pfs3_driver)

    def test_bridge_with_nonexistent_driver_raises(self, pfs3_8mb_image):
        """HandlerBridge should raise when driver binary doesn't exist."""
        with pytest.raises((FileNotFoundError, OSError, RuntimeError, SystemExit)):
            HandlerBridge(pfs3_8mb_image, Path("/nonexistent/pfs3aio"))


# B. Resource lifecycle -- 3 tests


class TestHandlerBridgeResourceLifecycle:
    """Test resource acquisition and release."""

    def test_close_is_idempotent(self, pfs3_8mb_image, pfs3_driver):
        """Calling close() multiple times should not raise."""
        bridge = HandlerBridge(pfs3_8mb_image, pfs3_driver)
        bridge.close()
        bridge.close()  # Second call should be no-op
        bridge.close()  # Third call should be no-op

    def test_close_releases_backend(self, pfs3_8mb_image, pfs3_driver):
        """After close(), backend should be None."""
        bridge = HandlerBridge(pfs3_8mb_image, pfs3_driver)
        bridge.close()
        assert bridge.backend is None

    def test_close_releases_vamos_runtime(self, pfs3_8mb_image, pfs3_driver):
        """After close(), vh (VamosHandlerRuntime) should be None."""
        bridge = HandlerBridge(pfs3_8mb_image, pfs3_driver)
        bridge.close()
        assert bridge.vh is None


# C. Directory operations -- 2 tests


class TestHandlerBridgeDirectoryOps:
    """Test directory listing operations."""

    def test_list_dir_path_returns_dicts(self, pfs3_8mb_image, pfs3_driver):
        """Each entry from list_dir_path should be a dict with name and dir_type."""
        bridge = HandlerBridge(pfs3_8mb_image, pfs3_driver)
        try:
            entries = bridge.list_dir_path("/")
            for entry in entries:
                assert "name" in entry
                assert "dir_type" in entry
        finally:
            bridge.close()

    def test_list_nonexistent_path_returns_empty(self, pfs3_8mb_image, pfs3_driver):
        """Listing a non-existent path should return an empty list."""
        bridge = HandlerBridge(pfs3_8mb_image, pfs3_driver)
        try:
            entries = bridge.list_dir_path("/nonexistent/deep/path")
            assert entries == []
        finally:
            bridge.close()
