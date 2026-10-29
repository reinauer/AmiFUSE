"""A failed write of one packet on a full FFS floppy leaves the file as it was.

FastFileSystem 47.4 fails an ACTION_WRITE that runs out of space but keeps
the part of the packet that fit. A write of at most IO_CHUNK bytes is one
packet, and was not undone: the file kept about 31 KiB of a write that
failed with ENOSPC, while getattr() of the file, still open from create(),
reported the old size. The bundled FastFileSystem keeps nothing of a packet
it fails. The volume is a new DD floppy, at most 880 KiB of writes to fill.
"""

import errno

import pytest

from amitools.fs import DosType
from amitools.fs.ADFSVolume import ADFSVolume
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.ADFBlockDevice import ADFBlockDevice

from amifuse.driver_resolver import BUNDLED_DRIVER
from amifuse.fuse_fs import IO_CHUNK, AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration

PATH = "/a"
HEAD = b"A" * 1000


def _make_ffs_adf(path):
    blkdev = ADFBlockDevice(str(path))
    blkdev.create()
    volume = ADFSVolume(blkdev)
    volume.create(FSString("Full"), dos_type=DosType.DOS1)
    volume.close()
    blkdev.close()


@pytest.mark.parametrize("handler", ["FastFileSystem", "bundled"])
def test_failed_write_on_full_volume_is_undone(fixture_root, tmp_path, handler):
    """With about 31 KiB left, a write of IO_CHUNK bytes at the end of a
    1000-byte file fails with ENOSPC and frees what it took, getattr()
    reports 1000 bytes, and an append there lands at the end of file."""
    if handler == "bundled":
        driver = BUNDLED_DRIVER
    else:
        driver = fixture_root / "drivers" / handler
        if not driver.exists():
            pytest.skip(f"{handler} handler not found")
    image = tmp_path / "full.adf"
    _make_ffs_adf(image)

    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    try:
        ops = AmigaFuseFS(bridge)

        def free():
            info = bridge.get_disk_info()
            return info["num_blocks"] - info["num_blocks_used"]

        fill, pos = ops.create("/fill", 0o644), 0
        for size, left in ((IO_CHUNK, 200), (512, 65)):
            while free() > left:
                pos += ops.write("/fill", b"\x55" * size, pos, fill)
        ops.release("/fill", fill)
        handle = ops.create(PATH, 0o644)
        assert ops.write(PATH, HEAD, 0, handle) == len(HEAD)
        left = free()

        with pytest.raises(FuseOSError) as exc_info:
            ops.write(PATH, b"B" * IO_CHUNK, len(HEAD), handle)
        assert exc_info.value.args[0] == errno.ENOSPC
        assert free() == left
        assert ops.getattr(PATH, handle)["st_size"] == len(HEAD)
        assert ops.write(PATH, b"APPEND", len(HEAD), handle) == 6
        ops.release(PATH, handle)
        bridge.flush_volume()
    finally:
        bridge.close()

    bridge = HandlerBridge(image, driver, read_only=True, adf_info=detect_adf(image))
    try:
        assert bridge.read_file(PATH, 1 << 20, 0) == HEAD + b"APPEND"
    finally:
        bridge.close()
