"""Reads and writes larger than one packet, on the real handlers.

The bridge splits a read or write into ACTION_READ or ACTION_WRITE packets
of at most IO_CHUNK bytes. Sent as one packet, a request larger than the
free emulated memory (about 8 MiB) could not be allocated, and on OFS a
read or write of about 1 MiB kept the handler working for 10 million
cycles or more. Each test works on a copy of a volume through the FUSE
callbacks a mounted volume receives.
"""

import errno
import os
import shutil

import pytest

from amitools.fs import DosType
from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice

from amifuse.fuse_fs import IO_CHUNK, AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration

PATH = "/large.bin"


def _pattern(size):
    """size bytes in which word k (4 bytes, big-endian) holds k."""
    words = (size + 3) // 4
    return b"".join(k.to_bytes(4, "big") for k in range(words))[:size]


def _make_hd_adf(path, dos_type):
    """Create an empty HD floppy image of the given DOS type."""
    blkdev = ADFBlockDevice(str(path), hd=True)
    blkdev.create()
    volume = ADFSVolume(blkdev)
    volume.create(FSString("Large"), dos_type=dos_type)
    volume.close()
    blkdev.close()


# name: (handler, image, partition, file size). The image is a file under
# fixtures/readonly, or the DOS type of an empty HD floppy to create. On
# OFS, one packet takes the handler about 9.8 million cycles per MiB read
# and 16.3 million per MiB written, so a read reaches 10 million cycles
# only above 1 MiB, which only an HD floppy has room for. Only the FFS
# partition has room for a file larger than the emulated memory.
_VOLUMES = {
    "ofs": ("FastFileSystem", DosType.DOS0, None, 1536 * 1024),
    "ffs": ("FastFileSystem", DosType.DOS1, None, 1536 * 1024),
    "pfs3": ("pfs3aio", "pfs.hdf", None, 1536 * 1024),
    "sfs": ("SmartFilesystem", "sfs.hdf", None, 1536 * 1024),
    "ffs-partition": ("FastFileSystem", "emu68k.hdf", "UDH0", (9 << 20) + 5),
}


@pytest.fixture(params=list(_VOLUMES))
def large_volume(request, fixture_root, tmp_path):
    """A copy of a volume per handler, as (image, driver, partition, size)."""
    return _copy_volume(request.param, fixture_root, tmp_path)


# The FFS partition is left out: filling its 40 MiB of free space would
# take a write of over 40 MiB to check what the FFS floppy checks.
@pytest.fixture(params=["ofs", "ffs", "pfs3", "sfs"])
def small_volume(request, fixture_root, tmp_path):
    """A copy of a volume with at most 6 MiB free, as large_volume returns it."""
    return _copy_volume(request.param, fixture_root, tmp_path)


def _copy_volume(name, fixture_root, tmp_path):
    driver_name, image_name, partition, size = _VOLUMES[name]
    driver = fixture_root / "drivers" / driver_name
    if not driver.exists():
        pytest.skip(f"{driver_name} handler not found")
    if isinstance(image_name, int):
        image = tmp_path / "large.adf"
        _make_hd_adf(image, image_name)
    else:
        source = fixture_root / "fixtures" / "readonly" / image_name
        if not source.exists():
            pytest.skip(f"{image_name} not found")
        image = tmp_path / image_name
        shutil.copy2(source, image)
    return image, driver, partition, size


def _bridge(image, driver, partition, read_only=True):
    return HandlerBridge(image, driver, read_only=read_only,
                         partition=partition, adf_info=detect_adf(image))


def _write(image, driver, partition, parts):
    """Create PATH and write parts to it, one write() each, in order."""
    bridge = _bridge(image, driver, partition, read_only=False)
    try:
        ops = AmigaFuseFS(bridge)
        handle = ops.create(PATH, 0o644)
        offset = 0
        for part in parts:
            assert ops.write(PATH, part, offset, handle) == len(part)
            offset += len(part)
        ops.release(PATH, handle)
        bridge.flush_volume()
    finally:
        bridge.close()


class TestLargeTransfers:
    """One read() returns every byte asked for, up to end of file, and one
    write() stores every byte it is given."""

    def test_read_larger_than_memory(self, large_volume):
        image, driver, partition, size = large_volume
        content = _pattern(size)
        _write(image, driver, partition,
               [content[p:p + IO_CHUNK] for p in range(0, size, IO_CHUNK)])

        # More than the emulated machine's 8 MiB of memory in one request.
        request = size + (8 << 20)
        # 2 MiB from an odd offset: past end of file on the 1.5 MiB files.
        offset, length = 1000001, 2 << 20
        middle = content[offset:offset + length]
        bridge = _bridge(image, driver, partition)
        try:
            ops = AmigaFuseFS(bridge)
            handle = ops.open(PATH, os.O_RDONLY)
            # More than 1 MiB, which in one packet would keep OFS working
            # for more than 10 million cycles.
            assert ops.read(PATH, size, 0, handle) == content
            assert ops.read(PATH, request, 0, handle) == content
            # The next read follows on from where that one ended.
            assert ops.read(PATH, 4096, size, handle) == b""
            assert ops.read(PATH, length, offset, handle) == middle
            ops.release(PATH, handle)
            # Without an open handle.
            assert bridge.read_file(PATH, request, 0) == content
            assert bridge.read_file(PATH, length, offset) == middle
        finally:
            bridge.close()

    def test_write_whole_file_in_one_call(self, large_volume):
        image, driver, partition, size = large_volume
        content = _pattern(size)
        # The second write follows the first and needs no seek.
        _write(image, driver, partition, [content, b"tail"])

        bridge = _bridge(image, driver, partition)
        try:
            ops = AmigaFuseFS(bridge)
            handle = ops.open(PATH, os.O_RDONLY)
            parts, offset = [], 0
            while True:
                part = ops.read(PATH, IO_CHUNK, offset, handle)
                if not part:
                    break
                parts.append(part)
                offset += len(part)
            ops.release(PATH, handle)
        finally:
            bridge.close()
        assert b"".join(parts) == content + b"tail"

    def test_write_larger_than_free_space_is_undone(self, small_volume):
        """One write() of more than IO_CHUNK bytes at end of file that runs
        out of space leaves the file at its old size and frees every block
        it took, and the next write on the same handle lands at that end.

        In one packet such a write left nothing behind on pfs3aio and
        SmartFilesystem. In packets, those before the failed one were on
        the disk and kept the free space they had used. After the shrink,
        SmartFilesystem failed the next write at the new end until the
        handle moved.
        """
        image, driver, partition, _ = small_volume
        head = _pattern(1000)
        bridge = _bridge(image, driver, partition, read_only=False)
        try:
            ops = AmigaFuseFS(bridge)
            handle = ops.create(PATH, 0o644)
            assert ops.write(PATH, head, 0, handle) == len(head)
            info = bridge.get_disk_info()
            free = info["num_blocks"] - info["num_blocks_used"]
            data = _pattern(free * info["bytes_per_block"] + (512 << 10))
            with pytest.raises(FuseOSError) as exc_info:
                ops.write(PATH, data, len(head), handle)
            assert exc_info.value.args[0] == errno.ENOSPC
            # What getattr reports for a new file until it is closed.
            assert ops.getattr(PATH)["st_size"] == len(head)
            assert ops.write(PATH, b"tail", len(head), handle) == 4
            ops.release(PATH, handle)
            bridge.flush_volume()
        finally:
            bridge.close()

        bridge = _bridge(image, driver, partition)
        try:
            assert bridge.read_file(PATH, 1 << 20, 0) == head + b"tail"
            after = bridge.get_disk_info()
        finally:
            bridge.close()
        assert after["num_blocks_used"] == info["num_blocks_used"]
