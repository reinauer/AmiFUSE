"""
Block-device backend that maps an Amiga disk image (plain RDB, Emu68-style
MBR, ADF, or ISO) onto host file I/O for the filesystem handler runtime.
"""

import sys
import stat
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
from amitools.vamos.disk import HostFileLock, PartitionFileLock  # type: ignore  # noqa: E402
from amitools.util.Win32Disk import is_windows_disk  # noqa: E402


class BlockDeviceBackend:
    """Thin wrapper around a host file to provide block reads/writes."""

    def __init__(self, image: Path, block_size: Optional[int] = None, read_only=True,
                 adf_info=None, iso_info=None, mbr_partition_index=None,
                 partition_scope=False, partition=None):
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
        self.host_lock = HostFileLock(image, read_only=read_only)
        self.partition_scope = partition_scope and adf_info is None and iso_info is None
        self.partition = partition
        self._block_range = None
        # Physical disks keep their existing whole-device exclusion policy.
        if (self.partition_scope and not is_windows_disk(image)
                and stat.S_ISREG(Path(image).stat().st_mode)):
            self.host_lock = PartitionFileLock(image, read_only=read_only)

    @property
    def exclusive(self):
        return isinstance(self.host_lock, HostFileLock) and self.host_lock.is_locked

    def _setup_geometry(self):
        """Set geometry fields from the open RDB."""
        pd = self.rdb.rdb.phy_drv
        self.block_size = self.blkdev.block_bytes
        self.cyls = pd.cyls
        self.heads = pd.heads
        self.secs = pd.secs
        self.total_blocks = pd.cyls * pd.heads * pd.secs

    def open(self):
        if self.blkdev is not None:
            return
        self.host_lock.acquire()
        try:
            if self.partition_scope and self.partition is not None:
                from .rdb_inspect import find_partition_mbr_index
                self.mbr_partition_index = find_partition_mbr_index(
                    self.image, self._requested_block_size, self.partition)
            self._open_image()
            if self.partition_scope:
                self._lock_partition()
        except BaseException:
            # Cleanup must also run for SystemExit during image validation,
            # while preserving the failure that caused the open to abort.
            try:
                self.close()
            except BaseException:
                pass
            raise

    def _lock_partition(self):
        part = (self.rdb.get_partition(0) if self.partition is None else
                self.rdb.find_partition_by_string(str(self.partition)))
        if part is None:
            raise ValueError(f"Partition not found: {self.partition}")
        env = part.part_blk.dos_env
        cyl_blocks = env.surfaces * env.blk_per_trk
        start = env.low_cyl * cyl_blocks
        end = (env.high_cyl + 1) * cyl_blocks
        reserved_end = self.rdb.rdb.log_drv.rdb_blk_hi + 1
        if (env.surfaces <= 0 or env.blk_per_trk <= 0 or start < reserved_end
                or end <= start or start >= self.blkdev.num_blocks
                or any(start <= block < end for block in self.rdb.get_used_blocks())):
            raise ValueError("Invalid partition bounds or overlap with RDB metadata")
        # OffsetBlockDevice addresses an RDB within an MBR container. Locks
        # must use absolute host offsets, while handler I/O stays RDB-relative.
        offset = getattr(self.blkdev, "offset", 0)
        if self.mbr_context is not None and self.mbr_context.mbr_partition is not None:
            container = self.mbr_context.mbr_partition
            if end > self.blkdev.num_blocks:
                raise ValueError("Partition extends beyond its MBR container")
            for other in self.mbr_context.mbr_info.partitions:
                if other.index == container.index or not other.num_sectors:
                    continue
                if (container.start_lba < other.start_lba + other.num_sectors
                        and other.start_lba < container.start_lba + container.num_sectors):
                    raise ValueError("Overlapping MBR containers cannot be shared")
        if isinstance(self.host_lock, PartitionFileLock):
            self.host_lock.lock_range((offset + start) * self.block_size,
                                      (end - start) * self.block_size)
        self._block_range = (start, end)

    def _open_image(self):
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
        rdisk = self.rdb
        blkdev = self.blkdev
        self.rdb = None
        self.blkdev = None
        self._block_range = None
        first_error = None
        try:
            if rdisk:
                rdisk.close()
        except BaseException as exc:
            first_error = exc
        try:
            if blkdev:
                blkdev.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        try:
            self.host_lock.release()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error

    def read_blocks(self, blk_num: int, num_blks: int = 1) -> bytes:
        if not self.blkdev:
            raise RuntimeError("Block device not open")
        self._check_range(blk_num, num_blks)
        if num_blks == 0:
            return b""
        return self.blkdev.read_block(blk_num, num_blks)

    def _check_range(self, blk_num, num_blks):
        # Device I/O maps OSError to a completed error reply. ValueError
        # would escape the emulator and strand the handler request.
        if blk_num < 0 or num_blks < 0:
            raise OSError("Negative block range")
        if self._block_range is not None:
            start, end = self._block_range
            if blk_num < start or blk_num + num_blks > end:
                raise OSError("Block range exceeds selected partition")
            base = getattr(self.blkdev, "base", self.blkdev)
            offset = getattr(self.blkdev, "offset", 0)
            if offset + blk_num + num_blks > base.num_blocks:
                raise OSError("Block range exceeds disk image")

    def write_blocks(self, blk_num: int, data: bytes, num_blks: int = 1):
        if not self.blkdev:
            raise RuntimeError("Block device not open")
        if self.read_only:
            raise PermissionError("Backend opened read-only")
        self._check_range(blk_num, num_blks)
        if len(data) != num_blks * self.block_size:
            raise OSError("Block write size does not match block count")
        if num_blks == 0:
            return
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
