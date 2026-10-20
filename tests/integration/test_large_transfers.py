"""Reads and writes larger than one packet, on the real handlers.

The bridge splits a read or write into ACTION_READ or ACTION_WRITE packets
of at most IO_CHUNK bytes. Sent as one packet, a request larger than the
free emulated memory (about 8 MiB) could not be allocated, and on OFS a
read or write of about 1 MiB kept the handler working for 10 million
cycles or more. Each test works on a copy of a volume through the FUSE
callbacks a mounted volume receives.
"""

import os
import shutil

import pytest

from amitools.fs import DosType
from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice

from amifuse.fuse_fs import IO_CHUNK, AmigaFuseFS, HandlerBridge
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
    driver_name, image_name, partition, size = _VOLUMES[request.param]
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
