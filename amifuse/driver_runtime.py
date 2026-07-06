"""
Block-device backend that maps an Amiga disk image (plain RDB, Emu68-style
MBR, ADF, or ISO) onto host file I/O for the filesystem handler runtime.
"""

import sys
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
AMITOOLS_PATH = REPO_ROOT / "amitools"

# Prefer local checkout of amitools if it is not installed
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(AMITOOLS_PATH) not in sys.path:
    sys.path.insert(0, str(AMITOOLS_PATH))

from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice  # type: ignore  # noqa: E402
from amitools.fs.rdb.RDisk import RDisk  # type: ignore  # noqa: E402


class BlockDeviceBackend:
    """Thin wrapper around a host file to provide block reads/writes."""

    def __init__(self, image: Path, block_size: Optional[int] = None, read_only=True,
                 adf_info=None, iso_info=None, mbr_partition_index=None):
        self.image = image
        # Keep the caller's request separate from the effective size:
        # None means auto-detect, which open_rdisk needs to see as None.
        self._requested_block_size = block_size
        self.block_size = block_size or 512
        self.read_only = read_only
        self.blkdev: Optional[RawBlockDevice] = None
        self.rdb: Optional[RDisk] = None
        self.adf_info = adf_info  # ADFInfo if this is a floppy image
        self.iso_info = iso_info  # ISOInfo if this is an ISO image
        self.mbr_partition_index = mbr_partition_index  # For MBR disks with multiple 0x76 partitions
        self.mbr_context = None  # MBRContext if opened via MBR partition

    def _setup_geometry(self):
        """Set geometry fields from the open RDB."""
        pd = self.rdb.rdb.phy_drv
        self.block_size = self.blkdev.block_bytes
        self.cyls = pd.cyls
        self.heads = pd.heads
        self.secs = pd.secs
        self.total_blocks = pd.cyls * pd.heads * pd.secs

    def open(self):
        from .rdb_inspect import open_rdisk

        # For ADF images, skip RDB/MBR parsing and use synthetic geometry
        if self.adf_info is not None:
            self.blkdev = RawBlockDevice(
                str(self.image), read_only=self.read_only, block_bytes=self.block_size
            )
            self.blkdev.open()
            self.rdb = None
            self.block_size = self.adf_info.block_size
            self.cyls = self.adf_info.cylinders
            self.heads = self.adf_info.heads
            self.secs = self.adf_info.sectors_per_track
            self.total_blocks = self.adf_info.total_blocks
            return

        # For ISO images, skip RDB/MBR parsing and use synthetic geometry
        if self.iso_info is not None:
            self.blkdev = RawBlockDevice(
                str(self.image), read_only=self.read_only,
                block_bytes=self.iso_info.block_size
            )
            self.blkdev.open()
            self.rdb = None
            self.block_size = self.iso_info.block_size
            self.cyls = self.iso_info.cylinders
            self.heads = self.iso_info.heads
            self.secs = self.iso_info.sectors_per_track
            self.total_blocks = self.iso_info.total_blocks
            return

        # RDB image (plain, Parceiro-style MBR+RDB, or inside an Emu68-style
        # 0x76 MBR partition): delegate scanning, lenient parsing, and MBR
        # handling to open_rdisk so the logic lives in one place.
        self.blkdev, self.rdb, self.mbr_context = open_rdisk(
            self.image,
            block_size=self._requested_block_size,
            mbr_partition_index=self.mbr_partition_index,
            read_only=self.read_only,
        )
        self._setup_geometry()

    def close(self):
        if self.rdb:
            self.rdb.close()
            self.rdb = None
        if self.blkdev:
            self.blkdev.close()
            self.blkdev = None

    def read_blocks(self, blk_num: int, num_blks: int = 1) -> bytes:
        if not self.blkdev:
            raise RuntimeError("Block device not open")
        return self.blkdev.read_block(blk_num, num_blks)

    def write_blocks(self, blk_num: int, data: bytes, num_blks: int = 1):
        if not self.blkdev:
            raise RuntimeError("Block device not open")
        if self.read_only:
            raise PermissionError("Backend opened read-only")
        self.blkdev.write_block(blk_num, data, num_blks)

    def sync(self):
        """Flush any buffered writes to the underlying file."""
        if self.blkdev:
            self.blkdev.flush()

    def describe(self) -> str:
        if self.adf_info is not None:
            floppy_type = "HD" if self.adf_info.is_hd else "DD"
            return (
                f"{self.image} ADF ({floppy_type}) cyls={self.cyls} heads={self.heads} "
                f"secs={self.secs} block={self.block_size}"
            )
        if self.iso_info is not None:
            return (
                f"{self.image} ISO 9660 ({self.iso_info.volume_id}) "
                f"blocks={self.total_blocks} block={self.block_size}"
            )
        assert self.rdb is not None
        pd = self.rdb.rdb.phy_drv
        base_desc = (
            f"{self.image} cyls={pd.cyls} heads={pd.heads} secs={pd.secs} "
            f"block={self.blkdev.block_bytes if self.blkdev else self.block_size}"
        )
        # Parceiro-style coexistence has mbr_context with mbr_partition=None
        if self.mbr_context is not None and self.mbr_context.mbr_partition is not None:
            mbr_part = self.mbr_context.mbr_partition
            base_desc += (
                f" [MBR partition {mbr_part.index}: "
                f"start={mbr_part.start_lba} size={mbr_part.num_sectors}]"
            )
        return base_desc
