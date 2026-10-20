"""A packet that keeps a real handler busy for many bursts gets its reply."""

from contextlib import ExitStack
import os

import pytest

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.fuse_fs import HandlerBridge, format_volume

pytestmark = pytest.mark.integration

# FastFileSystem moves OFS data 488 bytes per block. One ACTION_WRITE of
# 1.5 MiB keeps it working for about 100 bursts of 200,000 cycles, and one
# ACTION_READ of it for about 80. The bridge used to give up after 50.
SIZE = 1536 * 1024
DATA = b"".join(k.to_bytes(4, "big") for k in range(SIZE // 4))


@pytest.fixture
def ofs_image(fixture_root, tmp_path):
    """A new image holding one formatted 16 MiB OFS partition, DH0."""
    driver = fixture_root / "drivers" / "FastFileSystem"
    if not driver.exists():
        pytest.skip("FastFileSystem handler not found")
    image = tmp_path / "ofs.hdf"
    raw = RawBlockDevice(str(image), read_only=False)
    with ExitStack() as cleanup:
        raw.create(64 * 16 * 32)
        cleanup.callback(raw.close)
        disk = RDisk(raw)
        cleanup.callback(disk.close)
        disk.create(DiskGeometry(64, 16, 32), rdb_cyls=1)
        disk.add_partition(FSString("DH0"), (1, 63), dos_type=0x444F5300)
        raw.flush()
    format_volume(image, driver, None, "DH0", "Long")
    return image, driver


def _wait_for_reply(bridge, pkt_addr, label):
    """Wait for the reply to one packet; return it and the bursts it took.

    The test sends each packet itself, in one piece, so that the wait stays
    long however the bridge's own read and write calls split their data.
    """
    bursts = []
    run_burst = bridge.launcher.run_burst

    def counted(*args, **kwargs):
        bursts.append(None)
        return run_burst(*args, **kwargs)

    bridge.launcher.run_burst = counted
    try:
        return bridge._run_until_reply(pkt_addr, label), len(bursts)
    finally:
        bridge.launcher.run_burst = run_burst


def test_one_large_write_and_read_on_ofs(ofs_image):
    image, driver = ofs_image
    bridge = HandlerBridge(image, driver, read_only=False, partition="DH0")
    try:
        opened = bridge.open_file("/big.bin", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        assert opened is not None
        fh_addr, _ = opened
        buf = bridge._alloc_read_buf(SIZE)
        bridge.mem.w_block(buf.addr, DATA)
        pkt_addr, _ = bridge.launcher.send_write_handle(
            bridge.state, fh_addr, buf.addr, SIZE)
        reply, bursts = _wait_for_reply(bridge, pkt_addr, "write")
        assert reply is not None
        assert reply[0] == SIZE
        # Each packet must outlast the old budget, or this test proves nothing.
        assert bursts > 50
        bridge.close_file(fh_addr)
        bridge.flush_volume()
    finally:
        bridge.close()

    bridge = HandlerBridge(image, driver, partition="DH0")
    try:
        opened = bridge.open_file("/big.bin", os.O_RDONLY)
        assert opened is not None
        fh_addr, _ = opened
        buf = bridge._alloc_read_buf(SIZE)
        pkt_addr, _ = bridge.launcher.send_read_handle(
            bridge.state, fh_addr, buf.addr, SIZE)
        reply, bursts = _wait_for_reply(bridge, pkt_addr, "read")
        assert reply is not None
        assert reply[0] == SIZE
        assert bursts > 50
        assert bytes(bridge.mem.r_block(buf.addr, SIZE)) == DATA
        bridge.close_file(fh_addr)
    finally:
        bridge.close()
