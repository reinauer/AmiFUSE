"""Files above 4 GiB on pfs3aio's large file build, through the 64 bit packets.

Runs when the fixtures hold drivers/pfs3aio-largefile, a pfs3aio built with
LARGE_FILE_SIZE=1. The image is sparse, and the file grows through the
handler, which writes no data for it, so the 6 GiB take little space.
"""

import os
from contextlib import ExitStack

import pytest
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge, format_volume
from tests.fixtures.paths import DRIVERS_DIR

DRIVER = DRIVERS_DIR / "pfs3aio-largefile" if DRIVERS_DIR else None
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(DRIVER is None or not DRIVER.exists(),
                       reason="no pfs3aio-largefile driver in the fixtures"),
]

GIB = 1 << 30
PFS3_DOSTYPE = 0x50465303
# pfs3aio's packet that switches a volume's large file flag on.
ACTION_SET_LARGEFILE, ID_PFS2_DISK = 2223, 0x50465302


def _volume(path):
    """A 6 GiB disk with one partition of 4096 byte blocks, which pfs3aio
    formats as PFS2, the type that carries the large file flag."""
    raw = RawBlockDevice(str(path), read_only=False)
    with ExitStack() as cleanup:
        raw.create(12288 * 16 * 64)
        cleanup.callback(raw.close)
        disk = RDisk(raw)
        cleanup.callback(disk.close)
        disk.create(DiskGeometry(12288, 16, 64), rdb_cyls=1)
        disk.add_partition(FSString("DH0"), (1, 12287), dos_type=PFS3_DOSTYPE,
                           fs_block_size=4096)
        raw.flush()
    format_volume(path, DRIVER, None, "DH0", "Big")
    bridge = HandlerBridge(path, DRIVER, read_only=False)
    with bridge._lock:
        pkt, _ = bridge.launcher.send_packet(
            bridge.state, ACTION_SET_LARGEFILE, [ID_PFS2_DISK, 1])
        reply = bridge._run_until_reply(pkt, "largefile")
    if reply != (-1, 1):
        bridge.close()
        pytest.fail(f"the large file flag did not switch on: {reply}")
    return bridge


def test_a_file_past_4_gib(tmp_path):
    bridge = _volume(tmp_path / "big.hdf")
    try:
        fs = AmigaFuseFS(bridge, debug=False, icons=False)
        assert bridge.max_file_size() == 2 ** 63 - 1
        (fh_addr, _), _ = bridge.open_file_reply("/BIG", os.O_RDWR | os.O_CREAT)
        assert bridge.set_handle_size(fh_addr, 5 * GIB) == 5 * GIB
        bridge.close_file(fh_addr)
        fh = fs.open("/BIG", os.O_RDWR)
        assert fs.write("/BIG", b"TAIL", 5 * GIB - 4, fh) == 4
        assert fs.read("/BIG", 4, 5 * GIB - 4, fh) == b"TAIL"
        assert fs.read("/BIG", 4, 4 * GIB, fh) == bytes(4)
        assert fs.getattr("/BIG", fh)["st_size"] == 5 * GIB
        fs.truncate("/BIG", 4 * GIB + 2, fh)
        fs.truncate("/BIG", 4 * GIB + 4096, fh)
        assert fs.read("/BIG", 4096, 4 * GIB, fh) == bytes(4096)
        assert fs.getattr("/BIG", fh)["st_size"] == 4 * GIB + 4096
        fs.release("/BIG", fh)
        sizes = {e["name"]: e["size"] for e in bridge.list_dir_path("/")}
        assert sizes["BIG"] == 4 * GIB + 4096
    finally:
        bridge.close()
