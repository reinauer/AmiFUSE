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


# D. Read failures -- 2 tests


def _root_files(bridge):
    """Return {path: content} for every non-empty file in the root directory."""
    files = {}
    for entry in bridge.list_dir_path("/"):
        if entry.get("dir_type", 0) < 0 and entry.get("size", 0) > 0:
            path = "/" + entry["name"]
            data = bridge.read_file(path, entry["size"], 0)
            assert data is not None and len(data) == entry["size"], path
            files[path] = data
    return files


class TestHandlerBridgeReadFailures:
    """Test that failed reads and seeks are not reported as end of file."""

    def test_seek_past_end_reads_as_eof(self, pfs3_image, pfs3_driver):
        """Reads at or past end of file return b"" even after an earlier read moved the handle."""
        bridge = HandlerBridge(pfs3_image, pfs3_driver)
        try:
            path, data = next(iter(_root_files(bridge).items()))
            size = len(data)
            fh_addr, _ = bridge.open_file(path)
            try:
                assert bridge.read_handle_at(fh_addr, 0, 4) == data[:4]
                assert bridge.read_handle_at(fh_addr, size + 1, 16) == b""
                assert bridge.read_handle_at(fh_addr, size, 16) == b""
                assert bridge.read_handle_at(fh_addr, 0, size) == data
            finally:
                bridge.close_file(fh_addr)
        finally:
            bridge.close()

    def test_read_past_truncated_image_fails(self, pfs3_image, pfs3_driver, tmp_path):
        """A file whose data lies past the end of a truncated image fails to read."""
        cut = 2 * 1024 * 1024
        raw = pfs3_image.read_bytes()
        bridge = HandlerBridge(pfs3_image, pfs3_driver)
        try:
            files = _root_files(bridge)
        finally:
            bridge.close()
        # Content first found at or past the cut has no copy before it, so
        # the truncated image cannot hold that file's first data block.
        lost = [p for p, d in files.items() if raw.find(d[:512]) >= cut]
        kept = [p for p, d in files.items() if 0 <= raw.find(d) <= cut - len(d)]
        assert lost, "no root file stores its first data block past the cut"
        assert kept, "no root file is stored wholly before the cut"

        truncated = tmp_path / "pfs-truncated.hdf"
        truncated.write_bytes(raw[:cut])
        bridge = HandlerBridge(truncated, pfs3_driver)
        try:
            path = lost[0]
            size = len(files[path])
            assert bridge.read_file(path, size, 0) is None
            fh_addr, _ = bridge.open_file(path)
            try:
                assert bridge.read_handle_at(fh_addr, 0, size) is None
            finally:
                bridge.close_file(fh_addr)
            assert bridge.read_file(kept[0], len(files[kept[0]]), 0) == files[kept[0]]
        finally:
            bridge.close()
