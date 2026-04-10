"""HandlerBridge write-path integration tests.

Uses temporary copies of fixture images to test write operations
without modifying committed fixtures.

HandlerBridge write API (verified against fuse_fs.py):
  open_file(path, flags) -> Optional[Tuple[fh_addr, dir_lock]]
  write_handle(fh_addr, data) -> int (bytes written)
  close_file(fh_addr)
  create_dir(parent_lock_bptr, name) -> Tuple[lock_bptr, res2]
  locate(lock_bptr, name) -> Tuple[lock_bptr, res2]
  free_lock(lock_bptr)
  flush_volume()
"""

import os
import shutil

import pytest
from pathlib import Path

from amifuse.fuse_fs import HandlerBridge

pytestmark = pytest.mark.integration


@pytest.fixture
def writable_pfs3_image(pfs3_image, tmp_path):
    """Create a writable copy of the PFS3 test image."""
    copy = tmp_path / "pfs3_writable.hdf"
    shutil.copy2(pfs3_image, copy)
    return copy


# A. Write operations -- 3 tests


class TestHandlerBridgeWriteOps:
    """Test write operations on a copy of the PFS3 image."""

    def test_write_and_read_back_file(self, writable_pfs3_image, pfs3_driver):
        """Write a file and verify it appears in directory listing."""
        bridge = HandlerBridge(writable_pfs3_image, pfs3_driver, read_only=False)
        try:
            test_data = b"Integration test data\n"
            # open_file returns (fh_addr, dir_lock) or None on failure
            result = bridge.open_file(
                "/test_write.txt",
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            )
            assert result is not None, "open_file should return (fh_addr, dir_lock)"
            fh_addr, dir_lock = result
            assert fh_addr > 0, "fh_addr should be positive"
            written = bridge.write_handle(fh_addr, test_data)
            assert written == len(test_data)
            bridge.close_file(fh_addr)
            bridge.flush_volume()
        finally:
            bridge.close()

        # Re-open read-only to verify persistence
        bridge2 = HandlerBridge(writable_pfs3_image, pfs3_driver, read_only=True)
        try:
            entries = bridge2.list_dir_path("/")
            names = [e["name"] for e in entries]
            assert "test_write.txt" in names
        finally:
            bridge2.close()

    def test_create_directory(self, writable_pfs3_image, pfs3_driver):
        """Create a directory and verify it appears in listing."""
        bridge = HandlerBridge(writable_pfs3_image, pfs3_driver, read_only=False)
        try:
            # create_dir needs a parent lock BPTR, not a path string.
            # Get root lock via locate(0, ""), then create_dir under it.
            root_lock, res2 = bridge.locate(0, "")
            assert root_lock != 0, "Failed to get root lock"
            try:
                lock, res2 = bridge.create_dir(root_lock, "TestDir")
                assert lock != 0, f"create_dir failed with res2={res2}"
                bridge.free_lock(lock)
            finally:
                bridge.free_lock(root_lock)
            entries = bridge.list_dir_path("/")
            dir_names = [e["name"] for e in entries if e["dir_type"] > 0]
            assert "TestDir" in dir_names
            bridge.flush_volume()
        finally:
            bridge.close()

    def test_flush_volume_before_close(self, writable_pfs3_image, pfs3_driver):
        """Verify flush_volume() does not raise on a writable bridge."""
        bridge = HandlerBridge(writable_pfs3_image, pfs3_driver, read_only=False)
        try:
            bridge.flush_volume()
        finally:
            bridge.close()
