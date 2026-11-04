"""A packet that keeps a real handler busy for many bursts gets its reply."""

import os

import pytest

from amitools.fs import DosType

from amifuse.fuse_fs import HandlerBridge
from tests.fixtures.images import make_ffs_partition
from tests.fixtures.paths import BUNDLED_FFS

pytestmark = [pytest.mark.integration, pytest.mark.no_fixtures]

# FastFileSystem moves OFS data 488 bytes per block. One ACTION_WRITE of
# 1.5 MiB keeps it working for about 100 bursts of 200,000 cycles, and one
# ACTION_READ of it for about 80. The bridge used to give up after 50.
SIZE = 1536 * 1024
DATA = b"".join(k.to_bytes(4, "big") for k in range(SIZE // 4))


@pytest.fixture
def ofs_image(tmp_path):
    """A new image holding one formatted 16 MiB OFS partition, DH0."""
    image = make_ffs_partition(tmp_path / "ofs.hdf", DosType.DOS0, "Long")
    return image, BUNDLED_FFS


def _wait_for_reply(bridge, pkt_addr, label):
    """Wait for the reply to one packet; return it and the runs it took.

    The test sends each packet itself, in one piece, so that the wait stays
    long however the bridge's own read and write calls split their data.
    Each run gives the handler _RUN_CHUNK_CYCLES cycles.
    """
    runs = []
    run = bridge.launcher.run

    def counted(*args, **kwargs):
        runs.append(None)
        return run(*args, **kwargs)

    bridge.launcher.run = counted
    try:
        return bridge._run_until_reply(pkt_addr, label), len(runs)
    finally:
        del bridge.launcher.run


# The old wait budget: 50 bursts of 200,000 cycles. A packet that takes
# more runs than that budget held is what this test is about.
OLD_BUDGET_RUNS = 50 * 200_000 // HandlerBridge._RUN_CHUNK_CYCLES


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
        reply, runs = _wait_for_reply(bridge, pkt_addr, "write")
        assert reply is not None
        assert reply[0] == SIZE
        # Each packet must outlast the old budget, or this test proves nothing.
        assert runs > OLD_BUDGET_RUNS
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
        reply, runs = _wait_for_reply(bridge, pkt_addr, "read")
        assert reply is not None
        assert reply[0] == SIZE
        assert runs > OLD_BUDGET_RUNS
        assert bytes(bridge.mem.r_block(buf.addr, SIZE)) == DATA
        bridge.close_file(fh_addr)
    finally:
        bridge.close()
