"""HandlerBridge write-path integration tests.

Uses temporary copies of fixture images to test write operations
without modifying source fixture images.

HandlerBridge write API (verified against fuse_fs.py):
  open_file(path, flags) -> Optional[Tuple[fh_addr, dir_lock]]
  write_handle(fh_addr, data) -> int (bytes written)
  close_file(fh_addr)
  create_dir(parent_lock_bptr, name) -> Tuple[lock_bptr, res2]
  locate(lock_bptr, name) -> Tuple[lock_bptr, res2]
  free_lock(lock_bptr)
  flush_volume()
"""

import errno
import os
import shutil

import pytest
from pathlib import Path

from amitools.fs import DosType
from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice

from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf

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


# B. Writes past end of file -- 4 tests


def _make_ffs_adf(path):
    """Create an FFS floppy image holding one small file."""
    blkdev = ADFBlockDevice(str(path))
    blkdev.create()
    volume = ADFSVolume(blkdev)
    volume.create(FSString("Probe"), dos_type=DosType.DOS1)
    volume.write_file(b"FFS probe file\n", FSString("probe.txt"))
    volume.close()
    blkdev.close()


# (handler, image under fixtures/readonly or None to create one, file in it)
_VOLUMES = {
    "pfs3": ("pfs3aio", "pfs.hdf", "/Identify.info"),
    "sfs": ("SmartFilesystem", "sfs.hdf", "/Prefs.info"),
    "ofs": ("FastFileSystem", "ofs.adf", "/OFS_README.txt"),
    "ffs": ("FastFileSystem", None, "/probe.txt"),
}


@pytest.fixture(params=list(_VOLUMES))
def writable_volume(request, fixture_root, tmp_path):
    """A copy of a volume per handler, as (image, driver, existing file)."""
    driver_name, image_name, path = _VOLUMES[request.param]
    driver = fixture_root / "drivers" / driver_name
    if not driver.exists():
        pytest.skip(f"{driver_name} handler not found")
    if image_name is None:
        image = tmp_path / "ffs.adf"
        _make_ffs_adf(image)
    else:
        source = fixture_root / "fixtures" / "readonly" / image_name
        if not source.exists():
            pytest.skip(f"{image_name} not found")
        image = tmp_path / image_name
        shutil.copy2(source, image)
    return image, driver, path


def _bridge(image, driver, read_only=True):
    return HandlerBridge(image, driver, read_only=read_only,
                         adf_info=detect_adf(image))


class TestWritePastEndOfFile:
    """Writes past end of file land at their offset, as on POSIX.

    AmigaDOS cannot seek past end of file, so the mount fills the gap with
    zeros before writing. Each test writes through the FUSE callbacks a
    mounted volume receives and checks the result from a fresh session.
    """

    def test_gap_reads_as_zeros(self, writable_volume):
        image, driver, path = writable_volume
        bridge = _bridge(image, driver)
        try:
            original = bridge.read_file(path, 1 << 20, 0)
        finally:
            bridge.close()
        size = len(original)
        assert size > 0

        bridge = _bridge(image, driver, read_only=False)
        try:
            ops = AmigaFuseFS(bridge)
            # The second write follows the first and needs no seek.
            handle = ops.open(path, os.O_RDWR)
            assert ops.write(path, b"A" * 8, size + 100, handle) == 8
            assert ops.write(path, b"B" * 8, size + 108, handle) == 8
            ops.release(path, handle)

            # Blocks freed by a deleted file still hold its data. A new file
            # that reuses them must still read zeros in its gap.
            handle = ops.create("/stale.bin", 0o644)
            assert ops.write("/stale.bin", b"\xa5" * 32768, 0, handle) == 32768
            ops.release("/stale.bin", handle)
            ops.unlink("/stale.bin")
            handle = ops.create("/sparse.bin", 0o644)
            assert ops.write("/sparse.bin", b"HEADER", 4096, handle) == 6
            ops.release("/sparse.bin", handle)
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge = _bridge(image, driver)
        try:
            after = bridge.read_file(path, 1 << 20, 0)
            sparse = bridge.read_file("/sparse.bin", 1 << 20, 0)
        finally:
            bridge.close()
        assert after == original + bytes(100) + b"A" * 8 + b"B" * 8
        assert sparse == bytes(4096) + b"HEADER"

    def test_gap_beyond_volume_fails_without_writing(self, writable_volume):
        image, driver, path = writable_volume
        bridge = _bridge(image, driver, read_only=False)
        try:
            original = bridge.read_file(path, 1 << 20, 0)
            ops = AmigaFuseFS(bridge)
            handle = ops.open(path, os.O_RDWR)
            # Every test volume is far smaller than 100 MiB, and a file may
            # not grow past 2 GiB - 1, the largest offset ACTION_SEEK takes.
            for offset, expected in ((100 << 20, errno.ENOSPC),
                                     (1 << 31, errno.EFBIG)):
                with pytest.raises(FuseOSError) as exc_info:
                    ops.write(path, b"data", offset, handle)
                assert exc_info.value.args[0] == expected
            ops.release(path, handle)
            assert bridge.read_file(path, 1 << 20, 0) == original
        finally:
            bridge.close()

    def test_append_after_truncate_lands_at_new_end(self, writable_volume):
        image, driver, _ = writable_volume
        bridge = _bridge(image, driver, read_only=False)
        try:
            ops = AmigaFuseFS(bridge)
            handle = ops.create("/log.txt", 0o644)
            assert ops.write("/log.txt", b"x" * 1000, 0, handle) == 1000
            ops.truncate("/log.txt", 0, handle)
            # An O_APPEND write arrives at the size getattr reports.
            size = ops.getattr("/log.txt")["st_size"]
            assert ops.write("/log.txt", b"append", size, handle) == 6
            ops.release("/log.txt", handle)
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge = _bridge(image, driver)
        try:
            assert bridge.read_file("/log.txt", 1 << 20, 0) == b"append"
        finally:
            bridge.close()

    def test_fill_that_runs_out_of_space_is_undone(self, writable_volume):
        image, driver, path = writable_volume
        bridge = _bridge(image, driver, read_only=False)
        try:
            original = bridge.read_file(path, 1 << 20, 0)
            info = bridge.get_disk_info()
            free = info["num_blocks"] - info["num_blocks_used"]
            # Block overhead can leave less room than the free space the
            # handler reports. Report 64 blocks too many, so the fill passes
            # the free-space check and runs out part way.
            bridge.get_disk_info = lambda: dict(
                info, num_blocks_used=info["num_blocks_used"] - 64)
            ops = AmigaFuseFS(bridge)
            handle = ops.open(path, os.O_RDWR)
            offset = len(original) + (free + 32) * info["bytes_per_block"]
            with pytest.raises(FuseOSError) as exc_info:
                ops.write(path, b"data", offset, handle)
            assert exc_info.value.args[0] == errno.ENOSPC
            # SmartFilesystem failed this write until the handle moved.
            assert ops.write(path, b"tail", len(original), handle) == 4
            ops.release(path, handle)
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge = _bridge(image, driver)
        try:
            assert bridge.read_file(path, 1 << 20, 0) == original + b"tail"
            after = bridge.get_disk_info()
        finally:
            bridge.close()
        # The zeros written before the handler ran out are gone again.
        assert after["num_blocks_used"] == info["num_blocks_used"]


# C. Truncate length limit -- 1 test


class TestTruncateLengthLimit:
    """truncate() refuses a length that ACTION_SET_FILE_SIZE cannot carry."""

    def test_out_of_range_length_leaves_file_unchanged(self, writable_volume):
        """Lengths outside 0..0x7FFFFFFF fail; the file is unchanged.

        4 GiB + 4, 4 GiB and 2 GiB fail with EFBIG, and -1 and -4 GiB + 4
        with EINVAL. ACTION_SET_FILE_SIZE takes a signed 32-bit size, so
        without the checks 4 GiB + 4 and -4 GiB + 4 reached the handler as 4
        and cut the file to 4 bytes.
        """
        image, driver, path = writable_volume
        bridge = _bridge(image, driver, read_only=False)
        try:
            original = bridge.read_file(path, 1 << 20, 0)
            assert len(original) > 4
            ops = AmigaFuseFS(bridge)
            for length, expected in (((1 << 32) + 4, errno.EFBIG),
                                     (1 << 32, errno.EFBIG),
                                     (0x80000000, errno.EFBIG),
                                     (-(1 << 32) + 4, errno.EINVAL),
                                     (-1, errno.EINVAL)):
                with pytest.raises(FuseOSError) as exc_info:
                    ops.truncate(path, length)
                assert exc_info.value.args[0] == expected
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge = _bridge(image, driver)
        try:
            assert bridge.read_file(path, 1 << 20, 0) == original
        finally:
            bridge.close()


# D. Truncate that grows a file -- 2 tests


class TestTruncateGrow:
    """truncate() fills the range it adds with zeros, as on POSIX.

    The handlers leave a range ACTION_SET_FILE_SIZE adds holding whatever
    its blocks held before, so the mount writes zeros instead.
    """

    def test_grown_range_reads_as_zeros(self, writable_volume):
        image, driver, path = writable_volume
        # Blocks freed by a deleted file still hold its data. SFS moves a
        # deleted file to .recycled, and reuses its blocks only once that
        # copy is deleted too and the volume reopened.
        bridge = _bridge(image, driver, read_only=False)
        try:
            ops = AmigaFuseFS(bridge)
            handle = ops.create("/stale.bin", 0o644)
            assert ops.write("/stale.bin", b"\xa5" * 32768, 0, handle) == 32768
            ops.release("/stale.bin", handle)
            ops.unlink("/stale.bin")
            if driver.name == "SmartFilesystem":
                ops.unlink("/.recycled/stale.bin")
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge = _bridge(image, driver, read_only=False)
        try:
            original = bridge.read_file(path, 1 << 20, 0)
            ops = AmigaFuseFS(bridge)
            # ftruncate() on a file created in this session, whose size
            # getattr serves from its cache until release.
            handle = ops.create("/new.bin", 0o644)
            assert ops.write("/new.bin", b"x", 0, handle) == 1
            assert ops.truncate("/new.bin", 32768, handle) == 0
            assert ops.getattr("/new.bin")["st_size"] == 32768
            assert ops.read("/new.bin", 32768, 0, handle) == b"x" + bytes(32767)
            # The grow moved the handle, so this write must seek.
            assert ops.write("/new.bin", b"y", 1, handle) == 1
            ops.release("/new.bin", handle)
            # truncate() by path on an existing file.
            assert ops.truncate(path, len(original) + 4096) == 0
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge = _bridge(image, driver)
        try:
            new = bridge.read_file("/new.bin", 1 << 20, 0)
            grown = bridge.read_file(path, 1 << 20, 0)
        finally:
            bridge.close()
        assert new == b"xy" + bytes(32766)
        assert grown == original + bytes(4096)

    def test_grow_beyond_volume_fails_without_writing(self, writable_volume):
        image, driver, path = writable_volume
        bridge = _bridge(image, driver, read_only=False)
        try:
            original = bridge.read_file(path, 1 << 20, 0)
            used = bridge.get_disk_info()["num_blocks_used"]
            ops = AmigaFuseFS(bridge)
            # Every test volume is far smaller than 100 MiB.
            with pytest.raises(FuseOSError) as exc_info:
                ops.truncate(path, 100 << 20)
            assert exc_info.value.args[0] == errno.ENOSPC
            assert bridge.read_file(path, 1 << 20, 0) == original
            assert bridge.get_disk_info()["num_blocks_used"] == used
        finally:
            bridge.close()
