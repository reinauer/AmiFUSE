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
        from .rdb_inspect import (
            OffsetBlockDevice, MBRContext, detect_mbr, MBR_TYPE_AMIGA_RDB,
            _scan_for_rdb, _lenient_rdisk_open,
        )

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

        # Try opening as direct RDB first (scan blocks 0-15)
        self.blkdev = RawBlockDevice(
            str(self.image), read_only=self.read_only, block_bytes=self.block_size
        )
        self.blkdev.open()

        rdb_block, new_block_size = _scan_for_rdb(self.blkdev, self.block_size)

        if new_block_size is not None:
            self.blkdev.close()
            self.blkdev = RawBlockDevice(
                str(self.image), read_only=self.read_only, block_bytes=new_block_size
            )
            self.blkdev.open()
            rdb_block, _ = _scan_for_rdb(self.blkdev, self.block_size)

        if rdb_block is not None:
            self.rdb = RDisk(self.blkdev)
            self.rdb.rdb = rdb_block
            if self.rdb.open():
                # Direct RDB success
                self._setup_geometry()
                return
            # Strict open failed — try lenient parse (Parceiro checksums)
            rdisk2 = RDisk(self.blkdev)
            rdisk2.rdb = rdb_block
            try:
                rdisk2.rdb_warnings = _lenient_rdisk_open(rdisk2)
                self.rdb = rdisk2
                self._setup_geometry()
                return
            except IOError:
                pass  # Fall through to MBR check

        # No direct RDB - check for MBR with 0x76 partitions
        mbr_info = detect_mbr(self.image)
        if mbr_info is not None and mbr_info.has_amiga_partitions:
            amiga_parts = [p for p in mbr_info.partitions if p.partition_type == MBR_TYPE_AMIGA_RDB]

            if self.mbr_partition_index is not None:
                if self.mbr_partition_index >= len(amiga_parts):
                    self.close()
                    raise IOError(
                        f"MBR partition index {self.mbr_partition_index} out of range "
                        f"(found {len(amiga_parts)} Amiga partitions)"
                    )
                amiga_parts = [amiga_parts[self.mbr_partition_index]]

            # Try each 0x76 partition
            for mbr_part in amiga_parts:
                offset_dev = OffsetBlockDevice(self.blkdev, mbr_part.start_lba, mbr_part.num_sectors)

                test_rdb = RDisk(offset_dev)
                peeked = test_rdb.peek_block_size()
                if peeked:
                    if peeked != self.blkdev.block_bytes:
                        # Need to reopen with correct block size
                        self.blkdev.close()
                        self.blkdev = RawBlockDevice(
                            str(self.image), read_only=self.read_only, block_bytes=peeked
                        )
                        self.blkdev.open()
                        offset_dev = OffsetBlockDevice(self.blkdev, mbr_part.start_lba, mbr_part.num_sectors)

                    self.rdb = RDisk(offset_dev)
                    if self.rdb.open():
                        # Success - set up geometry and context
                        pd = self.rdb.rdb.phy_drv
                        self.block_size = offset_dev.block_bytes
                        self.cyls = pd.cyls
                        self.heads = pd.heads
                        self.secs = pd.secs
                        self.total_blocks = pd.cyls * pd.heads * pd.secs
                        # OffsetBlockDevice.close() will close the underlying raw device
                        self.blkdev = offset_dev
                        self.mbr_context = MBRContext(
                            mbr_info=mbr_info,
                            mbr_partition=mbr_part,
                            offset_blocks=mbr_part.start_lba,
                        )
                        return

            self.close()
            raise IOError(
                f"MBR with Amiga partition(s) found, but none contain a valid RDB: {self.image}"
            )

        self.close()
        raise IOError(f"Failed to parse RDB on {self.image}")

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
        if self.mbr_context is not None:
            mbr_part = self.mbr_context.mbr_partition
            base_desc += (
                f" [MBR partition {mbr_part.index}: "
                f"start={mbr_part.start_lba} size={mbr_part.num_sectors}]"
            )
        return base_desc
