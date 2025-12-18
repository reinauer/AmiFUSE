"""
Skeleton packet loop for driving an Amiga filesystem handler once vamos hooks
are in place. The handler expects Exec/DOS packet-based I/O (ACTION_* codes)
and a trackdisk-style device. This file captures the interfaces we need and
keeps the call sites small; the actual 68k execution will be wired in next.
"""

from dataclasses import dataclass
from typing import Optional, Protocol


class BlockBackend(Protocol):
    """Minimal interface we need from a block backend to drive packets."""

    def read_blocks(self, blk_num: int, num_blks: int = 1) -> bytes:
        ...

    def write_blocks(self, blk_num: int, data: bytes, num_blks: int = 1):
        ...

    @property
    def blkdev(self):
        ...

    @property
    def rdb(self):
        ...


# A very small subset of packet types the handler will care about; more will
# be added as we wire the handler.
ACTION_READ = 0x52
ACTION_WRITE = 0x57
ACTION_SEEK = 0x4E
ACTION_DISK_INFO = 0x69


@dataclass
class Packet:
    action: int
    arg1: int = 0
    arg2: int = 0
    arg3: int = 0
    # In real use, pkt.res1/res2/ptr fields are memory addresses. For now we
    # treat them as plain values and let the handler runner translate.
    res1: int = 0
    res2: int = 0


class HandlerPacketLoop:
    """
    Placeholder that will become the execution loop feeding packets into the
    Amiga handler entry point. Right now it just sketches the control flow and
    routes disk I/O to the backend for future integration tests.
    """

    def __init__(self, backend: BlockBackend):
        self.backend = backend
        self.running = False

    def start(self):
        # TODO: wire this to the handler's entry using vamos + task scheduler.
        self.running = True
        # For now just signal that the loop is a stub and stop cleanly.
        self.running = False
        return "stub-not-implemented"

    # These helpers show how trackdisk-style requests will get translated.
    def handle_read(self, blk_num: int, num_blks: int) -> bytes:
        return self.backend.read_blocks(blk_num, num_blks)

    def handle_write(self, blk_num: int, data: bytes, num_blks: Optional[int] = None):
        if num_blks is None:
            # default to length/blk_size if caller omits num_blks
            num_blks = len(data) // (self.backend.blkdev.block_bytes)
        self.backend.write_blocks(blk_num, data, num_blks)

    def handle_seek(self, offset_blocks: int):
        # Trackdisk seeks are implicit; included for completeness.
        return offset_blocks

    def handle_disk_info(self):
        if not self.backend.rdb:
            return None
        pd = self.backend.rdb.rdb.phy_drv
        return {
            "cyls": pd.cyls,
            "heads": pd.heads,
            "secs": pd.secs,
            "block_bytes": self.backend.blkdev.block_bytes,
        }
