import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple, Union

REPO_ROOT = Path(__file__).resolve().parents[1]
AMITOOLS_PATH = REPO_ROOT / "amitools"

# Prefer local checkout of amitools if it is not installed
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(AMITOOLS_PATH) not in sys.path:
    sys.path.insert(0, str(AMITOOLS_PATH))

from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice  # type: ignore  # noqa: E402
from amitools.fs.rdb.RDisk import RDisk  # type: ignore  # noqa: E402
import amitools.fs.DosType as DosType  # type: ignore  # noqa: E402
from amitools.util.Win32Disk import image_size, open_image  # type: ignore  # noqa: E402


# ADF geometry constants
ADF_DD_SIZE = 901120   # 80 cylinders * 2 heads * 11 sectors * 512 bytes
ADF_HD_SIZE = 1802240  # 80 cylinders * 2 heads * 22 sectors * 512 bytes


@dataclass
class ADFInfo:
    """Information about an ADF (floppy) image."""
    dos_type: int           # DOS type from boot block (0x444F5300-0x444F5307)
    is_hd: bool             # True if HD floppy (22 sectors/track), False for DD (11)
    cylinders: int          # Always 80 for floppies
    heads: int              # Always 2 for floppies
    sectors_per_track: int  # 11 for DD, 22 for HD
    block_size: int         # Always 512
    total_blocks: int       # Total number of blocks


@dataclass
class ISOInfo:
    """Information about an ISO 9660 image."""
    block_size: int         # Always 2048 for ISO 9660
    cylinders: int          # Synthetic geometry
    heads: int              # Always 1
    sectors_per_track: int  # Always 1
    total_blocks: int       # image_size // block_size
    volume_id: str          # Volume identifier from PVD


# MBR partition type for Amiga RDB partition (used by Emu68)
MBR_TYPE_AMIGA_RDB = 0x76
MBR_SECTOR_SIZE = 512  # MBR entries count 512-byte sectors


@dataclass
class MBRPartition:
    """Information about a single MBR partition entry."""
    index: int              # Partition index (0-3)
    bootable: bool          # Boot indicator (0x80 = bootable)
    partition_type: int     # Partition type byte
    start_lba: int          # Starting LBA sector
    num_sectors: int        # Number of sectors


@dataclass
class MBRInfo:
    """Information about an MBR partition table."""
    partitions: list        # List of MBRPartition entries (only non-empty ones)
    has_amiga_partitions: bool  # True if any 0x76 partitions exist


@dataclass
class MBRContext:
    """Context for an RDB opened alongside or within an MBR partition.

    For Emu68-style disks, the RDB lives inside an MBR partition of type 0x76.
    For Parceiro-style disks, the MBR and RDB coexist at the disk level: the
    MBR occupies block 0 and the RDB starts at block 1+, with no block offset.
    """
    mbr_info: MBRInfo           # Full MBR partition table info
    mbr_partition: Optional[MBRPartition]  # 0x76 partition (Emu68), None for Parceiro
    offset_blocks: int          # In 512-byte MBR sectors, not RDB blocks (0 for Parceiro)
    scheme: str = "emu68"       # "emu68" or "parceiro"


@dataclass
class PartInfo:
    """One RDB partition, as surfaced by ``list_partitions``.

    ``index`` is the amitools ``Partition.num`` -- the per-RDB positional index.
    It restarts at 0 within each RDB, so it is NOT unique across a multi-RDB
    image; use ``name`` (the drive name) as the stable fan-out key
    (``--partition <name>``), never the index.
    """
    index: int          # Partition.num (per-RDB positional index; collides across RDBs)
    name: str           # drive name -- the --partition key for the fan-out
    dostype: int        # dos_env.dos_type
    dostype_str: str    # DosType tag string (e.g. "DOS1")


@dataclass
class PartitionList:
    """Result of ``list_partitions``: the partitions plus an optional fallback signal.

    ``partitions`` is the stable-ordered list of :class:`PartInfo` the caller
    should fan out over (one drive per entry). Normally it holds every partition
    across the image's RDB(s).

    ``fallback_reason`` is ``None`` in the normal case. It is set to a
    human-readable string ONLY when a partition name collided across multiple
    RDBs (which would make name-keyed ``--partition`` routing ambiguous), forcing
    a fall back to first-partition-only: in that case ``partitions`` holds exactly
    ONE ``PartInfo`` (index 0 of the first RDB) and ``fallback_reason`` explains
    why. The caller is responsible for logging it and naming it in the summary
    dialog; ``list_partitions`` performs no I/O of its own.
    """
    partitions: List[PartInfo]
    fallback_reason: Optional[str] = None


def detect_mbr(image: Path) -> Optional[MBRInfo]:
    """Detect and parse MBR partition table.

    Returns MBRInfo if a valid MBR is found, None otherwise.
    Only returns partitions that are non-empty (have sectors).
    """
    try:
        with open_image(image) as f:
            block0 = f.read(512)
    except OSError:
        return None

    if len(block0) < 512:
        return None

    # Check MBR signature at offset 0x1FE-0x1FF
    if block0[0x1FE:0x200] != b'\x55\xAA':
        return None

    partitions = []
    has_amiga = False

    # Parse 4 partition entries starting at offset 0x1BE
    for i in range(4):
        offset = 0x1BE + i * 16
        entry = block0[offset:offset + 16]

        bootable = entry[0] == 0x80
        partition_type = entry[4]
        # LBA values are little-endian
        start_lba = int.from_bytes(entry[8:12], 'little')
        num_sectors = int.from_bytes(entry[12:16], 'little')

        # Skip empty partitions
        if partition_type == 0 or num_sectors == 0:
            continue

        part = MBRPartition(
            index=i,
            bootable=bootable,
            partition_type=partition_type,
            start_lba=start_lba,
            num_sectors=num_sectors,
        )
        partitions.append(part)

        if partition_type == MBR_TYPE_AMIGA_RDB:
            has_amiga = True

    if not partitions:
        return None

    return MBRInfo(partitions=partitions, has_amiga_partitions=has_amiga)


class RDBContentError(OSError):
    """The disk's contents, not a failing read, rule out an RDB.

    open_rdisk skips a 0x76 partition for this error and lets any other
    OSError, such as a device or media error, end the open.
    """


class OffsetBlockDevice:
    """Block device wrapper that adds an offset to all block operations.

    This allows treating an MBR partition as if it were a standalone disk,
    so RDB parsing can work within the partition boundaries.
    """

    def __init__(self, base_blkdev, offset_blocks: int, num_blocks: int):
        """Create an offset block device.

        Args:
            base_blkdev: The underlying block device (must be open)
            offset_blocks: Starting block offset within base device
            num_blocks: Number of blocks in this slice
        """
        self.base = base_blkdev
        self.offset = offset_blocks
        self.num_blocks = num_blocks
        self.block_bytes = base_blkdev.block_bytes
        self.block_longs = self.block_bytes // 4

    def read_block(self, blk_num: int, num_blks: int = 1) -> bytes:
        """Read blocks with offset applied."""
        if blk_num + num_blks > self.num_blocks:
            # The partition is shorter than the RDB scan, or the RDB points
            # outside it.
            raise RDBContentError(
                f"Read beyond partition: {blk_num}+{num_blks} > {self.num_blocks}")
        return self.base.read_block(self.offset + blk_num, num_blks)

    def write_block(self, blk_num: int, data: bytes, num_blks: int = 1):
        """Write blocks with offset applied."""
        if blk_num + num_blks > self.num_blocks:
            raise IOError(f"Write beyond partition: {blk_num}+{num_blks} > {self.num_blocks}")
        self.base.write_block(self.offset + blk_num, data, num_blks)

    def flush(self):
        """Flush underlying device."""
        self.base.flush()

    def close(self):
        """Close the underlying base device."""
        if self.base is not None:
            self.base.close()
            self.base = None

    def open(self):
        """Open does nothing - base device should already be open."""
        pass


def detect_adf(image: Path) -> Optional[ADFInfo]:
    """Detect if image is an ADF (Amiga floppy disk) based on content.

    Checks:
    1. First 3 bytes are "DOS" (0x44 0x4F 0x53)
    2. 4th byte is 0-7 (DOS type variant)
    3. File size matches DD (901120) or HD (1802240) floppy size

    Returns ADFInfo if detected, None otherwise.
    """
    try:
        size = image_size(image)
    except OSError:
        return None

    # Check file size matches floppy geometry
    if size == ADF_DD_SIZE:
        is_hd = False
        sectors_per_track = 11
    elif size == ADF_HD_SIZE:
        is_hd = True
        sectors_per_track = 22
    else:
        return None

    # Read first 4 bytes and check for DOS signature
    try:
        with open_image(image) as f:
            header = f.read(4)
    except OSError:
        return None

    if len(header) < 4:
        return None

    # Check for "DOS" signature (bytes 0-2) and valid variant (byte 3)
    if header[0:3] != b'DOS':
        return None

    variant = header[3]
    if variant > 7:
        return None

    # Build DOS type: 'DOS\x00' = 0x444F5300, 'DOS\x01' = 0x444F5301, etc.
    dos_type = 0x444F5300 | variant

    total_blocks = 80 * 2 * sectors_per_track

    return ADFInfo(
        dos_type=dos_type,
        is_hd=is_hd,
        cylinders=80,
        heads=2,
        sectors_per_track=sectors_per_track,
        block_size=512,
        total_blocks=total_blocks,
    )


# ISO 9660 constants
ISO_BLOCK_SIZE = 2048
ISO_PVD_SECTOR = 16  # Primary Volume Descriptor is at sector 16


def detect_iso(image: Path) -> Optional[ISOInfo]:
    """Detect if image is an ISO 9660 filesystem.

    Checks for the Primary Volume Descriptor at sector 16:
    - Byte 0: type code 0x01 (PVD)
    - Bytes 1-5: "CD001" standard identifier

    Returns ISOInfo if detected, None otherwise.
    """
    try:
        size = image_size(image)
    except OSError:
        return None

    # Must be large enough to contain the PVD
    pvd_offset = ISO_PVD_SECTOR * ISO_BLOCK_SIZE
    if size < pvd_offset + ISO_BLOCK_SIZE:
        return None

    try:
        with open_image(image) as f:
            f.seek(pvd_offset)
            pvd = f.read(ISO_BLOCK_SIZE)
    except OSError:
        return None

    if len(pvd) < 6:
        return None

    # Check PVD signature: type 0x01, identifier "CD001"
    if pvd[0] != 0x01 or pvd[1:6] != b'CD001':
        return None

    # Extract volume identifier (bytes 40-71, 32 chars, space-padded)
    volume_id = pvd[40:72].decode('ascii', errors='replace').rstrip()

    total_blocks = size // ISO_BLOCK_SIZE

    return ISOInfo(
        block_size=ISO_BLOCK_SIZE,
        cylinders=total_blocks,
        heads=1,
        sectors_per_track=1,
        total_blocks=total_blocks,
        volume_id=volume_id,
    )


def _scan_for_rdb(blkdev, block_size: Optional[int] = None):
    """Scan blocks 0-15 for RDB signature.

    Returns (rdb_block, new_block_size) where:
    - rdb_block: The RDBlock object if found, None otherwise
    - new_block_size: If non-None, caller must reopen device with this block size
                      and call _scan_for_rdb again
    """
    from amitools.fs.block.rdb.RDBlock import RDBlock

    for blk_num in range(16):
        rdb = RDBlock(blkdev, blk_num)
        if rdb.read():
            # Skip an RDSK block declaring a block size other than a power of
            # two from 512 to 32768, as for a bad checksum. Other sizes do not
            # map onto 512-byte MBR sectors, and 0 or huge ones cannot be read.
            size = rdb.block_size
            if not 512 <= size <= 32768 or size & (size - 1):
                continue
            # Check if we need to adjust block size
            if block_size is None and rdb.block_size != blkdev.block_bytes:
                # Need to reopen with correct block size
                return None, rdb.block_size
            return rdb, None
    return None, None


def _is_parceiro_checksum(block) -> bool:
    """Check if a block has a Parceiro-style checksum.

    The Parceiro firmware computes LSEG checksums over the first ``size``
    longs (as given by the size field at long 1) rather than the entire
    block.  The standard Amiga algorithm always checksums every long in
    the block regardless of the size field.
    """
    if block.valid_chksum or not block.valid_types:
        return False
    size = block._get_long(1)
    if size < 5 or size > block.block_longs:
        return False
    chksum = 0
    for i in range(size):
        if i != block.chk_loc:
            chksum += block._get_long(i)
    return block.got_chksum == ((-chksum) & 0xFFFFFFFF)


def _read_fs_parceiro(blkdev, fs_blk_num, fs_num):
    """Try reading a FileSystem accepting Parceiro-style LSEG checksums.

    The Parceiro firmware checksums only the first ``size`` longs of each
    LSEG block instead of the full block.  This function accepts such
    blocks as valid, producing usable filesystem driver data.

    Returns (FileSystem, parceiro_count) where parceiro_count is the number
    of LSEG blocks with Parceiro checksums, or (None, 0) on failure.
    """
    from amitools.fs.block.rdb.FSHeaderBlock import FSHeaderBlock
    from amitools.fs.block.rdb.LoadSegBlock import LoadSegBlock
    from amitools.fs.block.Block import Block
    from amitools.fs.rdb.FileSystem import FileSystem

    fshd = FSHeaderBlock(blkdev, fs_blk_num)
    if not fshd.read():
        return None, 0

    lseg_blk = fshd.dev_node.seg_list_blk
    lsegs = []
    data = b""
    parceiro_count = 0

    while lseg_blk != 0xFFFFFFFF:
        ls = LoadSegBlock(blkdev, lseg_blk)
        Block.read(ls)  # Load data, validate type and checksum

        if ls.valid:
            pass  # Standard checksum OK
        elif _is_parceiro_checksum(ls):
            ls.valid = True
            parceiro_count += 1
        else:
            return None, 0  # Genuinely corrupt

        # Parse LSEG fields (data already loaded by Block.read)
        ls.size = ls._get_long(1)
        ls.host_id = ls._get_long(3)
        ls.next = ls._get_long(4)

        lseg_blk = ls.next
        data += ls.get_data()
        lsegs.append(ls)

    fs = FileSystem(blkdev, fs_blk_num, fs_num)
    fs.fshd = fshd
    fs.lsegs = lsegs
    fs.data = data
    fs.valid = True

    return fs, parceiro_count


def _lenient_rdisk_open(rdisk) -> List[str]:
    """Open an RDisk leniently, tolerating corrupt filesystem blocks.

    Replicates the logic of RDisk.open() but continues past corrupt
    filesystem (LSEG) blocks instead of failing.  Partition blocks are
    still required to be valid.

    LSEG blocks with Parceiro-style checksums (off by exactly one) are
    accepted and the filesystem driver data is made available.

    Returns a list of warning strings (empty if everything parsed cleanly).
    """
    from amitools.fs.block.Block import Block
    from amitools.fs.block.rdb.PartitionBlock import PartitionBlock
    from amitools.fs.rdb.Partition import Partition
    from amitools.fs.rdb.FileSystem import FileSystem

    rdb = rdisk.rdb
    if rdb.block_size != rdisk.rawblk.block_bytes:
        raise ValueError(
            "block size mismatch: rdb=%d != device=%d"
            % (rdb.block_size, rdisk.rawblk.block_bytes)
        )
    rdisk.block_bytes = rdb.block_size
    rdisk.used_blks = [rdb.blk_num]
    warnings = []

    # Read partitions (critical — fail on errors)
    part_blk = rdb.part_list
    rdisk.parts = []
    num = 0
    while part_blk != Block.no_blk:
        p = Partition(rdisk.rawblk, part_blk, num, rdb.log_drv.cyl_blks, rdisk)
        num += 1
        if not p.read():
            raise RDBContentError(f"Corrupt partition block at block {part_blk}")
        rdisk.parts.append(p)
        rdisk.used_blks.append(p.get_blk_num())
        part_blk = p.get_next_partition_blk()

    # Read filesystems (non-critical — warn on errors)
    fs_blk = rdb.fs_list
    rdisk.fs = []
    num = 0
    while fs_blk != PartitionBlock.no_blk:
        fs = FileSystem(rdisk.rawblk, fs_blk, num)
        num += 1
        if not fs.read():
            if fs.fshd is not None and fs.fshd.valid:
                # FSHD OK but LSEG chain failed — try Parceiro tolerance
                parceiro_fs, parceiro_count = _read_fs_parceiro(
                    rdisk.rawblk, fs_blk, fs.num
                )
                if parceiro_fs is not None and parceiro_count > 0:
                    dt = parceiro_fs.fshd.dos_type
                    dt_str = DosType.num_to_tag_str(dt)
                    warnings.append(
                        f"Filesystem #{parceiro_fs.num} ({dt_str}/0x{dt:08x}): "
                        f"Parceiro checksum in {parceiro_count} LSEG block(s) (accepted)"
                    )
                    rdisk.fs.append(parceiro_fs)
                    rdisk.used_blks += parceiro_fs.get_blk_nums()
                    fs_blk = parceiro_fs.get_next_fs_blk()
                else:
                    dt = fs.fshd.dos_type
                    dt_str = DosType.num_to_tag_str(dt)
                    warnings.append(
                        f"Filesystem #{fs.num} ({dt_str}/0x{dt:08x}): "
                        f"corrupt data block in LSEG chain (driver data unavailable)"
                    )
                    fs_blk = fs.fshd.next
            else:
                warnings.append(
                    f"Corrupt filesystem header at block {fs_blk} "
                    f"(remaining filesystem entries skipped)"
                )
                break
            continue
        rdisk.fs.append(fs)
        rdisk.used_blks += fs.get_blk_nums()
        fs_blk = fs.get_next_fs_blk()

    rdisk.valid = True
    rdisk.max_blks = rdb.log_drv.rdb_blk_hi + 1
    return warnings


def _parse_rdb(dev, rdb_block) -> RDisk:
    """Parse the RDB that _scan_for_rdb found on dev.

    Tries the strict RDisk.open() first, then the lenient parse that
    tolerates corrupt filesystem blocks. Raises RDBContentError, saying
    why, if neither works or the RDB uses another block size than dev.
    Other OSErrors, such as a failing read, propagate.
    """
    if rdb_block.block_size != dev.block_bytes:
        # Only a forced block size gets here: otherwise _scan_for_rdb has
        # the device reopened at the RDB's size. Both parses below would
        # raise ValueError.
        raise RDBContentError(
            f"RDB uses {rdb_block.block_size}-byte blocks, "
            f"but the device was opened with {dev.block_bytes}-byte blocks"
        )
    rdisk = RDisk(dev)
    rdisk.rdb = rdb_block
    rdisk.rdb_warnings = []
    if not rdisk.open():
        # Strict open failed - try lenient parse (tolerates corrupt FS blocks)
        rdisk = RDisk(dev)
        rdisk.rdb = rdb_block
        rdisk.rdb_warnings = _lenient_rdisk_open(rdisk)
    return rdisk


def _mbr_partition_device(blkdev, mbr_part) -> Optional[OffsetBlockDevice]:
    """Map an MBR partition, given in 512-byte sectors, onto blkdev's blocks.

    Returns None if the partition does not start on a block boundary, since
    an RDB using larger blocks cannot then be addressed inside it.
    """
    ratio = blkdev.block_bytes // MBR_SECTOR_SIZE
    if ratio < 1 or mbr_part.start_lba % ratio:
        return None
    return OffsetBlockDevice(
        blkdev, mbr_part.start_lba // ratio, mbr_part.num_sectors // ratio
    )


def _no_valid_rdb_message(image: Path, count: int) -> str:
    """Describe an MBR none of whose `count` 0x76 partitions has an RDB."""
    return (f"MBR with {count} Amiga partition(s) found, "
            f"but none contain a valid RDB: {image}")


def open_rdisk(
    image: Path,
    block_size: Optional[int] = None,
    mbr_partition_index: Optional[int] = None,
    read_only: bool = True,
) -> Tuple[Union[RawBlockDevice, 'OffsetBlockDevice'], RDisk, Optional[MBRContext]]:
    """Open an RDB image and return the block device + parsed RDisk.

    Scans blocks 0-15 for the RDB signature (RDSK), as the RDB can be located
    at any of these blocks depending on the disk geometry.

    Supports three disk layouts:
    - Plain RDB: RDSK at block 0 (standard Amiga hard disk)
    - Parceiro-style: MBR at block 0, RDSK at block 1+, coexisting at the
      disk level.  Non-Amiga partitions (e.g. FAT32) live in MBR entries.
    - Emu68-style: MBR with 0x76 (Amiga RDB) partition, RDSK inside that
      partition.

    Tolerates corrupt filesystem driver (LSEG) blocks in the RDB — partition
    data is parsed strictly, but corrupt FS entries are skipped with warnings
    stored in ``rdisk.rdb_warnings``.

    Args:
        image: Path to the disk image
        block_size: Force a specific block size (default: auto-detect)
        mbr_partition_index: For MBR disks with multiple 0x76 partitions,
            select which one to use (0-based). Default: first 0x76 partition.
        read_only: Open the underlying image read-only (default True).

    Returns:
        Tuple of (block_device, rdisk, mbr_context).
        mbr_context is None for plain RDB disks, or MBRContext for MBR disks.

    Raises:
        IOError: If no RDB can be opened. An RDB found by the direct scan
            inside a 0x76 partition's sectors belongs to that partition and
            is opened only through the MBR, so a failure reports the MBR
            error. Any other direct RDB that fails to parse raises "Failed
            to parse RDB" without trying the 0x76 partitions. A 0x76
            partition is skipped only for RDBContentError; any other
            OSError in it, such as a device or media error, ends the open.
    """
    # Read the MBR first: a Windows physical disk opened outside a mount
    # session is locked against other readers until blkdev closes.
    mbr_info = detect_mbr(image)
    blkdev = None

    def open_at(block_bytes):
        # Close the previous device first: each open of a physical disk
        # takes the drive's lock.
        nonlocal blkdev
        if blkdev is not None:
            old, blkdev = blkdev, None
            old.close()
        dev = RawBlockDevice(str(image), read_only=read_only, block_bytes=block_bytes)
        dev.open()
        blkdev = dev
        return dev

    # Close the device on any failure; a device that is returned stays open.
    try:
        return _locate_rdisk(image, block_size, mbr_partition_index, mbr_info,
                             open_at)
    except BaseException:
        if blkdev is not None:
            try:
                blkdev.close()
            except BaseException:
                pass  # Report the error that ended the open, not this one.
        raise


def _locate_rdisk(image, block_size, mbr_partition_index, mbr_info, open_at):
    """Find and parse the RDB for open_rdisk, which owns the device.

    open_at(block_bytes) opens the image at that block size, closing the
    device it opened before, and returns it.
    """
    initial_block_size = block_size or 512
    blkdev = open_at(initial_block_size)

    # First try direct RDB scan
    rdb_block, new_block_size = _scan_for_rdb(blkdev, block_size)

    if new_block_size is not None:
        # Need to reopen with correct block size and rescan
        blkdev = open_at(new_block_size)
        rdb_block, _ = _scan_for_rdb(blkdev, block_size)

    if rdb_block is not None and mbr_info is not None:
        # The scan can reach the RDB of a 0x76 partition (block 8 of a
        # forced 4096-byte size is LBA 64). Its block numbers are relative
        # to that partition, so leave it to the MBR path below even if it
        # would parse here. That path reuses blkdev or reopens it.
        offset = rdb_block.blk_num * blkdev.block_bytes
        if any(p.partition_type == MBR_TYPE_AMIGA_RDB
               and p.start_lba * MBR_SECTOR_SIZE <= offset
               < (p.start_lba + p.num_sectors) * MBR_SECTOR_SIZE
               for p in mbr_info.partitions):
            rdb_block = None

    if rdb_block is not None:
        try:
            rdisk = _parse_rdb(blkdev, rdb_block)
        except IOError as e:
            raise IOError(f"Failed to parse RDB at {image}: {e}")

        # Check for Parceiro-style MBR+RDB coexistence
        mbr_ctx = None
        if mbr_info is not None and rdb_block.blk_num > 0:
            mbr_ctx = MBRContext(
                mbr_info=mbr_info,
                mbr_partition=None,
                offset_blocks=0,
                scheme="parceiro",
            )
        return blkdev, rdisk, mbr_ctx

    # No direct RDB outside the 0x76 partitions - check those partitions
    if mbr_info is not None and mbr_info.has_amiga_partitions:
        # Find 0x76 partitions
        amiga_parts = [p for p in mbr_info.partitions if p.partition_type == MBR_TYPE_AMIGA_RDB]

        if mbr_partition_index is not None:
            if mbr_partition_index >= len(amiga_parts):
                raise IOError(
                    f"MBR partition index {mbr_partition_index} out of range "
                    f"(found {len(amiga_parts)} Amiga partitions)"
                )
            amiga_parts = [amiga_parts[mbr_partition_index]]

        # Try each 0x76 partition until we find one with a valid RDB
        # (partition, reason) for the error below; each reason reads on
        # from "partition N at LBA L".
        skipped = []
        for mbr_part in amiga_parts:
            # An earlier partition may have reopened the device with its
            # RDB's block size; map and scan each one in its own units.
            if blkdev.block_bytes != initial_block_size:
                blkdev = open_at(initial_block_size)
            offset_dev = _mbr_partition_device(blkdev, mbr_part)
            if offset_dev is None:
                skipped.append((mbr_part, " is not aligned to the RDB's "
                                          f"{initial_block_size}-byte blocks"))
                continue
            if offset_dev.offset >= blkdev.num_blocks:
                skipped.append((mbr_part, " starts past the end of the image"))
                continue

            # An entry whose contents rule out an RDB is skipped with its
            # reason; any other OSError ends the open. Leave blkdev open:
            # closing offset_dev closes it, and the next entry may reuse it.
            try:
                rdb_block, new_block_size = _scan_for_rdb(offset_dev, block_size)
            except RDBContentError as e:
                skipped.append((mbr_part, f": {e}"))
                continue

            if new_block_size is not None:
                # Need to reopen base device with new block size and rescan
                blkdev = open_at(new_block_size)
                offset_dev = _mbr_partition_device(blkdev, mbr_part)
                if offset_dev is None:
                    skipped.append((mbr_part, " is not aligned to the RDB's "
                                              f"{new_block_size}-byte blocks"))
                    continue
                try:
                    rdb_block, _ = _scan_for_rdb(offset_dev, block_size)
                except RDBContentError as e:
                    skipped.append((mbr_part, f": {e}"))
                    continue

            if rdb_block is not None:
                # Found RDB in this partition
                try:
                    rdisk = _parse_rdb(offset_dev, rdb_block)
                except RDBContentError as e:
                    skipped.append((mbr_part, f": {e}"))
                    continue

                mbr_ctx = MBRContext(
                    mbr_info=mbr_info,
                    mbr_partition=mbr_part,
                    offset_blocks=mbr_part.start_lba,
                )
                return offset_dev, rdisk, mbr_ctx

        # No valid RDB found in any 0x76 partition
        reasons = "".join(
            f"; partition {part.index} at LBA {part.start_lba}{reason}"
            for part, reason in skipped
        )
        if mbr_partition_index is not None:
            found = (f"MBR partition {amiga_parts[0].index} does not contain "
                     f"a valid RDB: {image}")
        else:
            found = _no_valid_rdb_message(image, len(amiga_parts))
        raise IOError(f"{found}{reasons}")

    # Check for other partition types to give helpful error messages
    error_msg = f"No valid RDB found in blocks 0-15 at {image}"
    try:
        block0 = blkdev.read_block(0)
        block1 = blkdev.read_block(1)
        if len(block0) >= 512 and block0[0x1FE:0x200] == b'\x55\xAA':
            error_msg = f"MBR partition table detected but no Amiga (0x76) partitions found: {image}"
        elif len(block1) >= 8 and block1[0:8] == b'EFI PART':
            error_msg = f"GPT partition table detected (not supported): {image}"
    except Exception:
        pass

    raise IOError(error_msg)


def find_partition_mbr_index(
    image: Path, block_size: Optional[int], partition_name: str
) -> Optional[int]:
    """Find which 0x76 MBR partition index contains a named Amiga partition.

    For MBR images with multiple 0x76 partitions, searches each RDB for the
    named partition.  Returns the 0-based index into the list of 0x76
    partitions, or None if the image is not MBR, has only one 0x76 partition,
    or the partition is found in the default (first) RDB.

    Raises IOError if a forced block_size rules out an RDB that holds the
    partition at its own block size.
    """
    mbr_info = detect_mbr(image)
    if mbr_info is None or not mbr_info.has_amiga_partitions:
        return None
    amiga_parts = [p for p in mbr_info.partitions if p.partition_type == MBR_TYPE_AMIGA_RDB]
    if len(amiga_parts) <= 1:
        return None

    def holds_partition(idx, size):
        # True or False if 0x76 partition idx opens at size, None if not.
        try:
            blkdev, rdisk, _ = open_rdisk(image, block_size=size, mbr_partition_index=idx)
            try:
                return rdisk.find_partition_by_string(str(partition_name)) is not None
            finally:
                rdisk.close()
                blkdev.close()
        except IOError:
            return None

    for idx in range(len(amiga_parts)):
        found = holds_partition(idx, block_size)
        if found:
            return idx
        if found is None and block_size is not None and holds_partition(idx, None):
            # The forced size skips an RDB holding the name, so a later match,
            # or none, would differ from what auto-detection resolves.
            raise IOError(
                f"Partition {partition_name} is in an RDB that does not open "
                f"with {block_size}-byte blocks"
            )
    return None


def _part_info(p) -> PartInfo:
    """Build a :class:`PartInfo` from an amitools ``Partition``.

    Reads only already-parsed, in-memory fields (``num``, the drive-name BSTR,
    and ``dos_env.dos_type``), so it is safe to call after the block device is
    closed. ``str(p.get_drive_name())`` matches the existing name idiom in
    ``fuse_fs.get_partition_info``.
    """
    dos_type = p.part_blk.dos_env.dos_type
    return PartInfo(
        index=p.num,
        name=str(p.get_drive_name()),
        dostype=dos_type,
        dostype_str=DosType.num_to_tag_str(dos_type),
    )


def list_partitions(
    image: Path, block_size: Optional[int] = None
) -> PartitionList:
    """Enumerate every partition in an RDB image, keyed by drive name.

    Opens the image via :func:`open_rdisk`, walks ``rdisk.parts``, and returns
    one :class:`PartInfo` per partition, then closes BOTH the rdisk and the
    underlying block device (``rdisk.close(); blkdev.close()`` -- the established
    pattern; ``RDisk.close()`` only nulls its state, so the block device must be
    closed separately or the image handle leaks).

    For multi-RDB images (Emu68/Parceiro disks with more than one ``0x76`` MBR
    partition, each carrying its own RDB) it enumerates across ALL ``0x76`` RDBs,
    mirroring the multi-RDB loop in ``rdb_inspect.main`` / ``fuse_fs.cmd_inspect``.
    Names -- not indices -- are the fan-out key, because ``Partition.num``
    restarts at 0 in each RDB and so collides across RDBs; every emitted name
    re-resolves through the existing, tested ``find_partition_mbr_index`` name
    path when the caller spawns ``--partition <name>``.

    RDB-ONLY: the caller performs the non-RDB short-circuit (``detect_adf`` /
    ``detect_iso``) BEFORE calling this. ``list_partitions`` does not probe for
    ADF/ISO and propagates :class:`IOError` from :func:`open_rdisk` on a non-RDB
    image.

    Duplicate-name guard (narrow): amitools does not enforce drive-name
    uniqueness across RDBs, and ``--partition`` resolves name-first, so two RDBs
    each holding e.g. ``DH0`` would be ambiguous to route. When (and only when) a
    name collides ACROSS RDBs, this falls back to first-partition-only (index 0
    of the first RDB) and sets ``PartitionList.fallback_reason``. The guard never
    fires for a plain RDB or for a multi-RDB image whose names are all distinct.

    Args:
        image: Path to the RDB disk image.
        block_size: Force a specific block size (default: auto-detect).

    Returns:
        A :class:`PartitionList`. ``.partitions`` is in stable RDB order
        (normally >= 1 entry); ``.fallback_reason`` is ``None`` normally and, on
        the duplicate-name fallback, a string with ``.partitions`` holding a
        single entry. The caller logs and surfaces ``.fallback_reason``.
    """
    amiga_parts = amiga_rdb_partitions(image)

    # Plain RDB (or a single 0x76 RDB): open once, enumerate, close both handles.
    if len(amiga_parts) <= 1:
        blkdev, rdisk, _mbr_ctx = open_rdisk(image, block_size=block_size)
        try:
            parts = [_part_info(p) for p in rdisk.parts]
        finally:
            rdisk.close()
            blkdev.close()
        return PartitionList(partitions=parts)

    # Multi-RDB: enumerate across every 0x76 RDB, keyed by name. Each iteration
    # opens its own (blkdev, rdisk) and MUST close both inside the loop or N
    # block-device handles leak.
    all_parts: List[PartInfo] = []
    seen_names: dict = {}   # lower-cased names from previously-processed RDBs
    first_part: Optional[PartInfo] = None
    for rdb_idx in range(len(amiga_parts)):
        try:
            blkdev, rdisk, _mbr_ctx = open_rdisk(
                image, block_size=block_size, mbr_partition_index=rdb_idx
            )
        except IOError:
            continue
        try:
            rdb_parts = [_part_info(p) for p in rdisk.parts]
        finally:
            rdisk.close()
            blkdev.close()

        if first_part is None and rdb_parts:
            first_part = rdb_parts[0]

        # A name shared with a PRIOR RDB makes name-keyed fan-out unsafe ->
        # fall back to first-partition-only for the whole image.
        for info in rdb_parts:
            if info.name.lower() in seen_names:
                reason = (
                    "multiple RDBs with colliding partition names "
                    "(%r); mounted first only" % info.name
                )
                return PartitionList(partitions=[first_part], fallback_reason=reason)

        for info in rdb_parts:
            seen_names[info.name.lower()] = info
            all_parts.append(info)

    return PartitionList(partitions=all_parts)


def format_fs_summary(rdisk: RDisk):
    lines = []
    for fs in rdisk.fs:
        dt = fs.fshd.dos_type
        dt_str = DosType.num_to_tag_str(dt)
        lines.append(
            f"FS #{fs.num}: {dt_str}/0x{dt:08x} version={fs.fshd.get_version_string()} "
            f"size={len(fs.get_data())} flags={fs.get_flags_info()}"
        )
    return lines


def amiga_rdb_partitions(image: Path) -> List[MBRPartition]:
    """Return the 0x76 (Amiga RDB) MBR partitions of an image, [] if none.

    More than one entry means the image carries multiple independent RDBs
    (Emu68-style multi-RDB disk).
    """
    return _amiga_partitions(detect_mbr(image))


def _amiga_partitions(mbr_info: Optional[MBRInfo]) -> List[MBRPartition]:
    """Return the 0x76 entries of an already parsed MBR, [] if none."""
    if mbr_info is None or not mbr_info.has_amiga_partitions:
        return []
    return [p for p in mbr_info.partitions if p.partition_type == MBR_TYPE_AMIGA_RDB]


def build_rdisk_desc(rdisk: RDisk, mbr_ctx: Optional[MBRContext]) -> dict:
    """Build the JSON-able description of a parsed RDisk.

    Folds the MBR context and lenient-parse warnings into the amitools
    desc dict. Shared by `rdb-inspect --json` and `amifuse inspect --json`.
    """
    desc = rdisk.get_desc()
    if mbr_ctx is not None:
        mbr_desc = {
            "scheme": mbr_ctx.scheme,
            "offset_blocks": mbr_ctx.offset_blocks,
            "all_partitions": [
                {
                    "index": p.index,
                    "type": p.partition_type,
                    "bootable": p.bootable,
                    "start_lba": p.start_lba,
                    "num_sectors": p.num_sectors,
                }
                for p in mbr_ctx.mbr_info.partitions
            ],
        }
        if mbr_ctx.mbr_partition is not None:
            mbr_desc["partition_index"] = mbr_ctx.mbr_partition.index
            mbr_desc["partition_size"] = mbr_ctx.mbr_partition.num_sectors
        desc["mbr"] = mbr_desc
    warnings = getattr(rdisk, "rdb_warnings", [])
    if warnings:
        desc["warnings"] = warnings
    return desc


def format_rdisk_report(rdisk: RDisk, full: bool = False) -> List[str]:
    """Format RDB info, filesystem drivers, and warnings as printable lines."""
    lines = list(rdisk.get_info(full=full))
    fs_lines = format_fs_summary(rdisk)
    if fs_lines:
        lines.append("")
        lines.append("Filesystem drivers:")
        for line in fs_lines:
            lines.append(f"  {line}")
    warnings = getattr(rdisk, "rdb_warnings", [])
    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in warnings:
            lines.append(f"  {w}")
    return lines


def print_inspect_report(image: Path, block_size: Optional[int], full: bool) -> None:
    """Print the human-readable inspection report for an RDB image.

    Handles plain-RDB, Parceiro-style MBR+RDB, and Emu68-style multi-RDB
    layouts. Shared by `amifuse inspect` and the rdb-inspect tool. Raises
    SystemExit when the image cannot be opened, or when no RDB of a multi-RDB
    image can; per-RDB failures in a multi-RDB image are reported inline and
    skipped.
    """
    mbr_info = detect_mbr(image)
    amiga_parts = _amiga_partitions(mbr_info)
    if len(amiga_parts) > 1:
        # Open every RDB before printing, so the MBR header can describe the
        # first one that opened. Each section holds one partition's report
        # lines, or the reason it could not be opened.
        header_ctx = None
        sections = []
        for rdb_idx, mbr_part in enumerate(amiga_parts):
            try:
                blkdev, rdisk, mbr_ctx = open_rdisk(
                    image, block_size=block_size, mbr_partition_index=rdb_idx
                )
            except IOError as e:
                sections.append([f"\nMBR partition [{mbr_part.index}]: Error: {e}"])
                continue
            try:
                if mbr_ctx.mbr_partition is None:
                    # Parceiro-style: open_rdisk found the disk's own RDB,
                    # which it does whichever 0x76 partition is selected.
                    # Report it once, as for a single RDB.
                    lines = [""]
                else:
                    lines = [f"\n=== Amiga RDB in MBR partition [{mbr_ctx.mbr_partition.index}]"
                             f" (offset: {mbr_ctx.offset_blocks} sectors) ===\n"]
                lines += format_rdisk_report(rdisk, full=full)
            finally:
                rdisk.close()
                blkdev.close()
            if header_ctx is None:
                header_ctx = mbr_ctx
            sections.append(lines)
            if mbr_ctx.mbr_partition is None:
                break

        if header_ctx is not None:
            header = format_mbr_info(header_ctx)
        else:
            # Nothing opened, so no partition is active: show the table alone.
            header = ["MBR Partition Table detected (Emu68-style)", ""]
            header += format_mbr_table(mbr_info)
        for line in header:
            print(line)
        for lines in sections:
            for line in lines:
                print(line)
        if header_ctx is None:
            raise SystemExit(
                f"Error: {_no_valid_rdb_message(image, len(amiga_parts))}"
            )
        return

    # Single RDB (plain or Parceiro-style)
    blkdev = None
    rdisk = None
    try:
        try:
            blkdev, rdisk, mbr_ctx = open_rdisk(image, block_size=block_size)
        except IOError as e:
            raise SystemExit(f"Error: {e}")
        if mbr_ctx is not None:
            for line in format_mbr_info(mbr_ctx):
                print(line)
            print()
        for line in format_rdisk_report(rdisk, full=full):
            print(line)
    finally:
        if rdisk is not None:
            rdisk.close()
        if blkdev is not None:
            blkdev.close()


_MBR_TYPE_NAMES = {
    MBR_TYPE_AMIGA_RDB: "Amiga RDB",
    0x01: "FAT12",
    0x04: "FAT16 <32M",
    0x06: "FAT16",
    0x07: "NTFS/exFAT",
    0x0B: "W95 FAT32",
    0x0C: "W95 FAT32 (LBA)",
    0x0E: "W95 FAT16 (LBA)",
    0x0F: "W95 Extended (LBA)",
    0x82: "Linux swap",
    0x83: "Linux",
    0xEE: "GPT protective",
}


def format_mbr_info(mbr_ctx: MBRContext) -> List[str]:
    """Format MBR partition info as a list of lines for display."""
    lines = []
    if mbr_ctx.scheme == "parceiro":
        lines.append("MBR + RDB coexistence detected (Parceiro-style)")
        lines.append("  MBR at block 0, RDB at block 1+")
    else:
        lines.append("MBR Partition Table detected (Emu68-style)")
        lines.append(f"  Active partition: MBR slot {mbr_ctx.mbr_partition.index}")
        lines.append(f"  Partition offset: {mbr_ctx.offset_blocks} sectors ({mbr_ctx.offset_blocks * 512 // 1024 // 1024} MB)")
        lines.append(f"  Partition size: {mbr_ctx.mbr_partition.num_sectors} sectors ({mbr_ctx.mbr_partition.num_sectors * 512 // 1024 // 1024} MB)")
    lines.append("")
    lines.extend(format_mbr_table(mbr_ctx.mbr_info))
    return lines


def format_mbr_table(mbr_info: MBRInfo) -> List[str]:
    """Format the MBR partition entries as a list of lines for display."""
    lines = ["  MBR partitions:"]
    for p in mbr_info.partitions:
        type_str = _MBR_TYPE_NAMES.get(p.partition_type, f"0x{p.partition_type:02x}")
        boot_str = " (bootable)" if p.bootable else ""
        size_mb = p.num_sectors * 512 // 1024 // 1024
        lines.append(f"    [{p.index}] Type: {type_str}{boot_str}, Start: {p.start_lba}, Size: {p.num_sectors} ({size_mb} MB)")
    return lines


def extract_fs(rdisk: RDisk, index: int, out_path: Path) -> Path:
    fs = rdisk.get_filesystem(index)
    if fs is None:
        raise IndexError(f"Filesystem #{index} not found")
    data = fs.get_data()
    out_path.write_bytes(data)
    return out_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect an RDB image and optionally extract filesystem drivers."
    )
    parser.add_argument("image", type=Path, help="Path to the RDB image (raw file)")
    parser.add_argument(
        "--block-size",
        type=int,
        help="Block size in bytes (defaults to auto/512).",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Show full partition details (matching amitools' rdbtool).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Dump the parsed RDB as JSON instead of text summary.",
    )
    parser.add_argument(
        "--extract-fs",
        type=int,
        metavar="N",
        help="Extract filesystem entry N (0-based) to a host file.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output path when extracting a filesystem (default derives from dostype).",
    )
    args = parser.parse_args(argv)

    if args.json:
        # JSON mode: collect all RDBs into a single JSON structure,
        # skipping RDBs that fail to open.
        amiga_parts = amiga_rdb_partitions(args.image)
        multi_rdb = len(amiga_parts) > 1
        all_rdbs = []
        for rdb_idx in range(len(amiga_parts) if multi_rdb else 1):
            mbr_partition_index = rdb_idx if multi_rdb else None
            try:
                blkdev, rdisk, mbr_ctx = open_rdisk(
                    args.image, block_size=args.block_size, mbr_partition_index=mbr_partition_index
                )
            except IOError:
                continue
            try:
                all_rdbs.append(build_rdisk_desc(rdisk, mbr_ctx))
            finally:
                rdisk.close()
                blkdev.close()
        if len(all_rdbs) == 1:
            print(json.dumps(all_rdbs[0], indent=2))
        else:
            print(json.dumps(all_rdbs, indent=2))
    else:
        print_inspect_report(args.image, args.block_size, args.full)

    if args.extract_fs is not None:
        # Extract from first RDB (use --mbr-partition for specific one in future)
        blkdev, rdisk, _ = open_rdisk(args.image, block_size=args.block_size)
        try:
            fs_obj = rdisk.get_filesystem(args.extract_fs)
            if fs_obj is None:
                raise SystemExit(f"No filesystem #{args.extract_fs} in RDB.")
            dt = fs_obj.fshd.dos_type
            default_name = f"fs{args.extract_fs}_{DosType.num_to_tag_str(dt)}.bin"
            out_path = args.out or Path(default_name)
            saved_to = extract_fs(rdisk, args.extract_fs, out_path)
            print(f"Wrote filesystem #{args.extract_fs} ({hex(dt)}) to {saved_to}")
        finally:
            rdisk.close()
            blkdev.close()


if __name__ == "__main__":
    main()
