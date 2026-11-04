import argparse
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

REPO_ROOT = Path(__file__).resolve().parents[1]
AMITOOLS_PATH = REPO_ROOT / "amitools"

# Prefer local checkout of amitools if it is not installed
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(AMITOOLS_PATH) not in sys.path:
    sys.path.insert(0, str(AMITOOLS_PATH))

from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice  # type: ignore  # noqa: E402
from amitools.fs.rdb.RDisk import RDisk  # type: ignore  # noqa: E402
from amitools.fs.block.rdb.PartitionBlock import PartitionBlock  # type: ignore  # noqa: E402
import amitools.fs.DosType as DosType  # type: ignore  # noqa: E402
from amitools.util.Win32Disk import image_size, open_image  # type: ignore  # noqa: E402


# ADF geometry constants
ADF_DD_SIZE = 901120   # 80 cylinders * 2 heads * 11 sectors * 512 bytes
ADF_HD_SIZE = 1802240  # 80 cylinders * 2 heads * 22 sectors * 512 bytes
ADF_DEFAULT_DOS_TYPE = 0x444F5300  # DOS0: a floppy drive's own, and xdftool's


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
    image; use ``name`` as the stable fan-out key (``--partition <name>``),
    never the index. ``name`` is the drive name as AmigaOS mounts it, so a
    repeated drive name reads ``DH0.1`` (:func:`mount_names`); it is None
    for a damaged drive name, which no ``--partition`` name selects.
    ``mbr_partition_index`` tells which RDB holds the partition, as
    :func:`open_each_rdb` yields it and :class:`PartitionRef` carries it.
    """
    index: int          # Partition.num (per-RDB positional index; collides across RDBs)
    name: Optional[str]  # unique drive name -- the --partition key for the fan-out
    dostype: int        # dos_env.dos_type
    dostype_str: str    # DosType tag string (e.g. "DOS1")
    mbr_partition_index: Optional[int] = None  # its RDB's; None for the default open


@dataclass
class PartitionList:
    """Result of ``list_partitions``: the partitions plus an optional fallback signal.

    ``partitions`` is the stable-ordered list of :class:`PartInfo` the caller
    should fan out over (one drive per entry). It holds every partition across
    the image's RDB(s), also one whose damaged drive name gives a ``name`` of
    None, which has no name to fan out by.

    ``fallback_reason``, when set, is a human-readable string saying why
    ``partitions`` holds fewer than every partition. The caller is responsible
    for logging it and naming it in the summary dialog. ``list_partitions``
    renames a repeated drive name instead of falling back to the first
    partition; it sets this only when an RDB of the disk fails to open, the
    disk's own beside its MBR or a 0x76 entry's, naming each one that fails.

    ``default_open_fails`` is True when the disk's own RDB beside the MBR
    fails (DiskRDBError), which ends the open without ``--partition``: then
    even a lone partition opens only by name. A read error in an entry
    ends that open only if no RDB before it opens, and then no name
    resolves before it either, so it is not flagged.
    """
    partitions: List[PartInfo]
    fallback_reason: Optional[str] = None
    default_open_fails: bool = False


def escape_text(text) -> str:
    """Return a string from an image for printing to a terminal.

    The image chooses its bytes, so a character outside 0x20-0x7E is shown
    escaped, e.g. "\\x1b", and never reaches the terminal. A backslash is
    escaped too, as "\\x5c", so that an escape cannot be faked.
    """
    return "".join(
        ch if " " <= ch <= "~" and ch != "\\" else f"\\x{ord(ch):02x}"
        for ch in str(text)
    )


def dos_type_tag(dos_type: int) -> str:
    """Return a DOS type's tag, e.g. "PFS3", for printing (escape_text)."""
    return escape_text(DosType.num_to_tag_str(dos_type))


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
    return parse_mbr(block0)


def parse_mbr(block0: bytes) -> Optional[MBRInfo]:
    """Parse the MBR partition table in sector 0's bytes, as detect_mbr does.

    Returns None if they hold no table with a non-empty entry.
    """
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


class DiskRDBError(OSError):
    """The disk's own RDB, beside an MBR listing 0x76 entries, does not open.

    open_rdisk raises it for the default open. The entries' own RDBs may
    still open, and open_each_rdb reports this error once, with index None.
    """


class NoEntryRDBError(OSError):
    """The selected 0x76 entry holds no RDSK block that _scan_for_rdb accepts.

    Beside the disk's own RDB, open_each_rdb skips such an entry without a
    message; an entry whose RDB is found but fails to open is reported.
    """


class EntryRDBError(OSError):
    """The selected 0x76 entry's RDB is found but its contents do not open.

    A block carrying the RDSK id with a bad checksum counts as found. A
    device or media error is not this error: it propagates as it is.
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

    @property
    def readable_blocks(self):
        """Blocks present in the image, without changing declared geometry."""
        return max(0, min(self.num_blocks, self.base.num_blocks - self.offset))

    def read_block(self, blk_num: int, num_blks: int = 1) -> bytes:
        """Read blocks with offset applied."""
        if blk_num + num_blks > self.num_blocks:
            # The partition is shorter than the RDB scan, or the RDB points
            # outside it.
            raise RDBContentError(
                f"Read beyond partition: {blk_num}+{num_blks} > {self.num_blocks}")
        if self.offset + blk_num + num_blks > self.base.num_blocks:
            # The image ends inside the partition: its contents, not a
            # failing read, rule the blocks out.
            raise RDBContentError(
                f"Read beyond image end: {self.offset + blk_num}+{num_blks} "
                f"> {self.base.num_blocks}")
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
    if size not in (ADF_DD_SIZE, ADF_HD_SIZE):
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

    return _adf_info(size, dos_type)


def _adf_info(size: int, dos_type: int) -> ADFInfo:
    """Return the ADFInfo of a floppy image of a size detect_adf accepts."""
    is_hd = size == ADF_HD_SIZE
    sectors_per_track = 22 if is_hd else 11
    return ADFInfo(
        dos_type=dos_type,
        is_hd=is_hd,
        cylinders=80,
        heads=2,
        sectors_per_track=sectors_per_track,
        block_size=512,
        total_blocks=80 * 2 * sectors_per_track,
    )


def floppy_to_format(
    image: Path,
    dos_type: Optional[int] = None,
    block_size: Optional[int] = None,
) -> Optional[ADFInfo]:
    """Return the ADFInfo `amifuse format` gives image, or None for an RDB disk.

    Only format calls this, since only format can use a blank image: every
    other command goes through detect_adf, which needs a DOS type in block 0.

    A floppy is what mount takes for one, or an image of a floppy's size that
    is all zeros. dos_type, which is --dos-type, overrides the DOS type in
    block 0. A blank image has none, so without dos_type it gets DOS0.

    Any other image of a floppy's size may hold anything, so it is formatted
    only when dos_type asks for it. One with an RDSK block in the blocks
    open_rdisk scans, or an MBR, is left to the RDB path even then, so that
    a damaged RDB is not taken for a floppy.

    Raises ValueError for an image of a floppy's size that is none of these
    and has no dos_type, and OSError if the image cannot be read.
    """
    adf_info = detect_adf(image)
    if adf_info is not None:
        return adf_info if dos_type is None else replace(adf_info, dos_type=dos_type)

    try:
        size = image_size(image)
    except OSError:
        return None  # The RDB path names the problem, as for any other image.
    if size not in (ADF_DD_SIZE, ADF_HD_SIZE):
        return None

    with open_image(image) as f:
        data = f.read(size)
    if len(data) == size and not data.strip(b"\0"):
        return _adf_info(size, ADF_DEFAULT_DOS_TYPE if dos_type is None else dos_type)

    step = block_size or 512
    if parse_mbr(data[:512]) or any(
            data[blk * step:blk * step + 4] == b"RDSK" for blk in range(16)):
        return None
    if dos_type is None:
        raise ValueError(
            f"{image} has the size of a floppy, but block 0 holds no DOS type "
            "from DOS0 to DOS7, and it has no RDB and is not blank. Use "
            "--dos-type to format it as a floppy, which overwrites it.")
    return _adf_info(size, dos_type)


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
    volume_id = pvd[40:72].decode('latin-1').rstrip(' ')

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

    Returns (rdb_block, new_block_size, bad_checksum_blk) where:
    - rdb_block: The RDBlock object if found, None otherwise
    - new_block_size: If non-None, caller must reopen device with this block size
                      and call _scan_for_rdb again
    - bad_checksum_blk: The first block scanned that carries the RDSK id but
                        fails its checksum, None if none: with no rdb_block,
                        a damaged RDB rather than none
    """
    from amitools.fs.block.rdb.RDBlock import RDBlock

    bad_checksum_blk = None
    for blk_num in range(16):
        rdb = RDBlock(blkdev, blk_num)
        try:
            valid = rdb.read()
        except RDBContentError:
            # The partition or the image ends within the scan. A damaged
            # RDSK block before that end is still a damaged RDB.
            if bad_checksum_blk is None:
                raise
            break
        if valid:
            # Skip an RDSK block declaring a block size other than a power of
            # two from 512 to 32768, as if it held no RDSK id. Other sizes do
            # not map onto 512-byte MBR sectors, and 0 or huge ones cannot be
            # read.
            size = rdb.block_size
            if not 512 <= size <= 32768 or size & (size - 1):
                continue
            # Check if we need to adjust block size
            if block_size is None and rdb.block_size != blkdev.block_bytes:
                # Need to reopen with correct block size
                return None, rdb.block_size, bad_checksum_blk
            return rdb, None, bad_checksum_blk
        if bad_checksum_blk is None and rdb.valid_types:
            bad_checksum_blk = blk_num
    return None, None, bad_checksum_blk


def _bad_checksum(blk_num: int) -> str:
    """Describe the RDSK block _scan_for_rdb found with a bad checksum."""
    return f"RDSK block at block {blk_num} fails its checksum"


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
    """Read a FileSystem as FileSystem.read() does, also accepting
    Parceiro-style LSEG checksums and stopping at an LSEG chain that loops.

    The Parceiro firmware checksums only the first ``size`` longs of each
    LSEG block instead of the full block.  This function accepts such
    blocks as valid, producing usable filesystem driver data.

    An LSEG link past the end of blkdev is a problem too, and is not read.

    Returns (FileSystem, parceiro_count, problem). fs.fshd is the header
    block, valid or not. problem is None when the whole entry reads, and
    parceiro_count is then the number of LSEG blocks with Parceiro
    checksums; otherwise problem says what is wrong and fs.valid is False.
    """
    from amitools.fs.block.rdb.FSHeaderBlock import FSHeaderBlock
    from amitools.fs.block.rdb.LoadSegBlock import LoadSegBlock
    from amitools.fs.block.Block import Block
    from amitools.fs.rdb.FileSystem import FileSystem

    fs = FileSystem(blkdev, fs_blk_num, fs_num)
    fs.fshd = FSHeaderBlock(blkdev, fs_blk_num)
    if not fs.fshd.read():
        return fs, 0, "corrupt filesystem header"

    lseg_blk = fs.fshd.dev_node.seg_list_blk
    lsegs = []
    data = b""
    parceiro_count = 0
    seen = set()

    while lseg_blk != 0xFFFFFFFF:
        if lseg_blk in seen:
            return fs, 0, f"LSEG chain loops back to block {lseg_blk}"
        if lseg_blk >= getattr(blkdev, "readable_blocks", blkdev.num_blocks):
            # Reading it would raise the IOError of a failing read.
            return fs, 0, (f"LSEG chain links to block {lseg_blk}, "
                           f"past the end of the device")
        seen.add(lseg_blk)
        ls = LoadSegBlock(blkdev, lseg_blk)
        Block.read(ls)  # Load data, validate type and checksum

        if ls.valid:
            pass  # Standard checksum OK
        elif _is_parceiro_checksum(ls):
            ls.valid = True
            parceiro_count += 1
        else:
            return fs, 0, "corrupt data block in LSEG chain"

        # Parse LSEG fields (data already loaded by Block.read)
        ls.size = ls._get_long(1)
        ls.host_id = ls._get_long(3)
        ls.next = ls._get_long(4)

        lseg_blk = ls.next
        data += ls.get_data()
        lsegs.append(ls)

    fs.lsegs = lsegs
    fs.data = data
    fs.valid = True

    return fs, parceiro_count, None


def _lenient_rdisk_open(rdisk) -> List[str]:
    """Open an RDisk leniently, tolerating corrupt filesystem blocks.

    Replicates the logic of RDisk.open() but continues past corrupt
    filesystem (LSEG) blocks instead of failing.  Partition blocks are
    still required to be valid.

    LSEG blocks with Parceiro-style checksums (off by exactly one) are
    accepted and the filesystem driver data is made available.

    A partition list, filesystem list or LSEG chain that links back to a
    block it has already read, or past the end of the device, ends there,
    with a warning; the link is not read. Only an RDB's first link to a
    partition is read even past the end, and fails the RDB: with no
    partition read, there is nothing to keep.

    Returns a list of warning strings (empty if everything parsed cleanly).
    A partition list that ends so is also recorded, by its warning, in
    rdisk.damaged_part_list, otherwise None: the partitions after it are
    unknown. Each filesystem entry it drops is also recorded in
    rdisk.damaged_fs as (DOS type, warning, entry's number); the DOS type is
    None when the header is unreadable or the list loops or links past the
    end, hiding the entries after it.
    """
    from amitools.fs.block.Block import Block
    from amitools.fs.block.rdb.PartitionBlock import PartitionBlock
    from amitools.fs.rdb.Partition import Partition

    rdb = rdisk.rdb
    if rdb.block_size != rdisk.rawblk.block_bytes:
        raise ValueError(
            "block size mismatch: rdb=%d != device=%d"
            % (rdb.block_size, rdisk.rawblk.block_bytes)
        )
    rdisk.block_bytes = rdb.block_size
    rdisk.used_blks = [rdb.blk_num]
    warnings = []
    rdisk.damaged_fs = []

    # Read partitions (critical — fail on errors, but keep those before a
    # broken link)
    part_blk = rdb.part_list
    rdisk.parts = []
    rdisk.damaged_part_list = None
    num = 0
    seen = set()
    while part_blk != Block.no_blk:
        if part_blk in seen:
            warnings.append(
                f"Partition list loops back to block {part_blk} "
                f"(remaining partitions skipped)"
            )
            rdisk.damaged_part_list = warnings[-1]
            break
        if rdisk.parts and part_blk >= getattr(
                rdisk.rawblk, "readable_blocks", rdisk.rawblk.num_blocks):
            # Not read: it would raise the IOError of a failing read.
            warnings.append(
                f"Partition list links to block {part_blk}, past the end of "
                f"the device (remaining partitions skipped)"
            )
            rdisk.damaged_part_list = warnings[-1]
            break
        if (not rdisk.parts and not isinstance(rdisk.rawblk, OffsetBlockDevice)
                and part_blk >= rdisk.rawblk.num_blocks):
            raise RDBContentError(
                f"Invalid image file block num: got {part_blk} "
                f"but max is {rdisk.rawblk.num_blocks}")
        seen.add(part_blk)
        p = Partition(rdisk.rawblk, part_blk, num, rdb.log_drv.cyl_blks, rdisk)
        num += 1
        if not p.read():
            reason = f"Corrupt partition block at block {part_blk}"
            if not rdisk.parts:
                raise RDBContentError(reason)
            warnings.append(f"{reason} (remaining partitions skipped)")
            rdisk.damaged_part_list = warnings[-1]
            break
        rdisk.parts.append(p)
        rdisk.used_blks.append(p.get_blk_num())
        part_blk = p.get_next_partition_blk()

    # Read filesystems (non-critical — warn on errors)
    fs_blk = rdb.fs_list
    rdisk.fs = []
    num = 0
    seen = set()
    while fs_blk != PartitionBlock.no_blk:
        if fs_blk in seen:
            # A broken link, like a corrupt header: whatever it should
            # point to can't be reached.
            warnings.append(
                f"Filesystem list loops back to block {fs_blk} "
                f"(remaining filesystem entries skipped)"
            )
            rdisk.damaged_fs.append((None, warnings[-1], num))
            break
        if fs_blk >= getattr(rdisk.rawblk, "readable_blocks", rdisk.rawblk.num_blocks):
            # Not read: it would raise the IOError of a failing read.
            warnings.append(
                f"Filesystem list links to block {fs_blk}, past the end of "
                f"the device (remaining filesystem entries skipped)"
            )
            rdisk.damaged_fs.append((None, warnings[-1], num))
            break
        seen.add(fs_blk)
        # Not FileSystem.read(): its LSEG walk has no bound.
        fs, parceiro_count, problem = _read_fs_parceiro(rdisk.rawblk, fs_blk, num)
        num += 1
        if not fs.fshd.valid:
            warnings.append(
                f"Corrupt filesystem header at block {fs_blk} "
                f"(remaining filesystem entries skipped)"
            )
            rdisk.damaged_fs.append((None, warnings[-1], fs.num))
            break
        dt = fs.fshd.dos_type
        dt_str = dos_type_tag(dt)
        if problem is not None:
            warnings.append(
                f"Filesystem #{fs.num} ({dt_str}/0x{dt:08x}): "
                f"{problem} (driver data unavailable)"
            )
            rdisk.damaged_fs.append((dt, warnings[-1], fs.num))
            fs_blk = fs.fshd.next
            continue
        if parceiro_count > 0:
            warnings.append(
                f"Filesystem #{fs.num} ({dt_str}/0x{dt:08x}): "
                f"Parceiro checksum in {parceiro_count} LSEG block(s) (accepted)"
            )
        rdisk.fs.append(fs)
        rdisk.used_blks += fs.get_blk_nums()
        fs_blk = fs.get_next_fs_blk()

    rdisk.valid = True
    rdisk.max_blks = rdb.log_drv.rdb_blk_hi + 1
    return warnings


def _parse_rdb(dev, rdb_block) -> RDisk:
    """Parse the RDB that _scan_for_rdb found on dev.

    Uses the lenient parse, which reads each partition as strictly as
    RDisk.open(), ends a broken partition list, and tolerates corrupt
    filesystem blocks. Raises RDBContentError, saying why, if it fails or
    the RDB uses another block size than dev. Other OSErrors, such as a
    failing read, propagate.
    """
    if rdb_block.block_size != dev.block_bytes:
        # Only a forced block size gets here: otherwise _scan_for_rdb has
        # the device reopened at the RDB's size. The parse below would raise
        # ValueError.
        raise RDBContentError(
            f"RDB uses {rdb_block.block_size}-byte blocks, "
            f"but the device was opened with {dev.block_bytes}-byte blocks"
        )
    # Not RDisk.open(): its walks of the partition list and of an LSEG chain
    # have no bound, so one that links back on itself never ends. The
    # lenient parse reads each partition as strictly, and stops at such a
    # loop with a warning.
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


def _no_rdb_opens_message(image: Path) -> str:
    """Describe a disk whose own RDB and 0x76 entries' RDBs all fail."""
    return f"No RDB on this disk opens: {image}"


def _nothing_opens_message(image: Path, amiga_parts, failures) -> str:
    """The error print_inspect_report ends with when no RDB opens.

    failures are the (rdb_idx, error) pairs open_each_rdb gave, with None
    for the disk's own RDB. rdb-inspect --json gives the same line first.
    """
    own = [error for rdb_idx, error in failures if rdb_idx is None]
    if len(amiga_parts) > 1:
        if own:
            return _no_rdb_opens_message(image)
        return _no_valid_rdb_message(image, len(amiga_parts))
    if own and not (amiga_parts and isinstance(own[0], DiskRDBError)):
        return str(own[0])
    return _no_rdb_opens_message(image)


def _disk_rdb_context(mbr_info: MBRInfo) -> MBRContext:
    """The MBRContext of a disk's own RDB beside its MBR (Parceiro-style)."""
    return MBRContext(
        mbr_info=mbr_info,
        mbr_partition=None,
        offset_blocks=0,
        scheme="parceiro",
    )


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
    stored in ``rdisk.rdb_warnings``, and recorded in ``rdisk.damaged_fs``.
    A partition list that loops or links on past the end of the device
    ends there with a warning, recorded in ``rdisk.damaged_part_list``.

    Args:
        image: Path to the disk image
        block_size: Force a specific block size (default: auto-detect)
        mbr_partition_index: For MBR disks with multiple 0x76 partitions,
            select which one to use (0-based); its own RDB opens even when
            the disk has one beside the MBR. Default: the RDB beside the
            MBR if there is one, else the first 0x76 partition's.
        read_only: Open the underlying image read-only (default True).

    Returns:
        Tuple of (block_device, rdisk, mbr_context).
        mbr_context is None for plain RDB disks, or MBRContext for MBR disks.

    Raises:
        IOError: If no RDB can be opened. An RDB found by the direct scan
            inside a 0x76 partition's sectors belongs to that partition and
            is opened only through the MBR, so a failure reports the MBR
            error. Any other direct RDB that fails to parse raises "Failed
            to parse RDB" without trying the 0x76 partitions (DiskRDBError
            beside 0x76 entries); a selected 0x76 partition opens its own
            RDB without parsing it. A 0x76 partition is skipped only for
            RDBContentError; any other OSError in it, such as a device or
            media error, ends the open. A selected partition with no RDSK
            block raises NoEntryRDBError, and one whose RDB is found but
            does not open EntryRDBError. A block carrying the RDSK id with
            a bad checksum is an RDB that does not open, not none.
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
    rdb_block, new_block_size, bad_blk = _scan_for_rdb(blkdev, block_size)

    if new_block_size is not None:
        # Need to reopen with correct block size and rescan
        blkdev = open_at(new_block_size)
        rdb_block, _, bad_blk = _scan_for_rdb(blkdev, block_size)

    def in_amiga_partition(blk_num):
        offset = blk_num * blkdev.block_bytes
        return mbr_info is not None and any(
            p.partition_type == MBR_TYPE_AMIGA_RDB
            and p.start_lba * MBR_SECTOR_SIZE <= offset
            < (p.start_lba + p.num_sectors) * MBR_SECTOR_SIZE
            for p in mbr_info.partitions)

    # The scan can reach the RDB of a 0x76 partition (block 8 of a forced
    # 4096-byte size is LBA 64). Its block numbers are relative to that
    # partition, so leave it, intact or not, to the MBR path below even if
    # it would parse here. That path reuses blkdev or reopens it.
    if rdb_block is not None and in_amiga_partition(rdb_block.blk_num):
        rdb_block = None
    if bad_blk is not None and in_amiga_partition(bad_blk):
        bad_blk = None

    # A selected 0x76 partition holds its own RDB, if any, beside this one,
    # so this one is not parsed: its failure must not end that open.
    parse_direct = (mbr_partition_index is None
                    or not _amiga_partitions(mbr_info))
    if rdb_block is None and bad_blk is not None and parse_direct:
        # An RDSK block with a bad checksum is a damaged RDB, not none: the
        # 0x76 partitions are not tried in its place.
        error = DiskRDBError if _amiga_partitions(mbr_info) else IOError
        raise error(f"Failed to parse RDB at {image}: "
                    f"{_bad_checksum(bad_blk)}")
    if rdb_block is not None and parse_direct:
        try:
            rdisk = _parse_rdb(blkdev, rdb_block)
        except RDBContentError as e:
            error = DiskRDBError if _amiga_partitions(mbr_info) else IOError
            raise error(f"Failed to parse RDB at {image}: {e}")

        # Check for Parceiro-style MBR+RDB coexistence. An RDB at block 0
        # beside an MBR that lists 0x76 entries is the disk's own as well.
        mbr_ctx = None
        if mbr_info is not None and (rdb_block.blk_num > 0
                                     or _amiga_partitions(mbr_info)):
            mbr_ctx = _disk_rdb_context(mbr_info)
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
        holds_rdb = False  # A scan found an RDSK block in a partition.
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
                rdb_block, new_block_size, bad_blk = _scan_for_rdb(
                    offset_dev, block_size)
            except RDBContentError as e:
                skipped.append((mbr_part, f": {e}"))
                continue
            holds_rdb = (holds_rdb or rdb_block is not None
                         or new_block_size is not None or bad_blk is not None)

            if new_block_size is not None:
                # Need to reopen base device with new block size and rescan
                blkdev = open_at(new_block_size)
                offset_dev = _mbr_partition_device(blkdev, mbr_part)
                if offset_dev is None:
                    skipped.append((mbr_part, " is not aligned to the RDB's "
                                              f"{new_block_size}-byte blocks"))
                    continue
                try:
                    rdb_block, _, bad_blk = _scan_for_rdb(offset_dev, block_size)
                except RDBContentError as e:
                    skipped.append((mbr_part, f": {e}"))
                    continue

            if rdb_block is None and bad_blk is not None:
                reason = _bad_checksum(bad_blk)
                skipped.append((mbr_part, f": {reason}"))
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
        error = IOError
        if mbr_partition_index is not None:
            found = (f"MBR partition {amiga_parts[0].index} does not contain "
                     f"a valid RDB: {image}")
            error = EntryRDBError if holds_rdb else NoEntryRDBError
        else:
            found = _no_valid_rdb_message(image, len(amiga_parts))
        raise error(f"{found}{reasons}")

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


@dataclass
class PartitionRef:
    """Where ``--partition`` finds a partition: see :func:`find_partition`."""
    mbr_partition_index: Optional[int]  # open_rdisk's index of its RDB
    number: int         # its number in that RDB, for RDisk.get_partition
    name: Optional[str]  # its name on this disk; None for a damaged one


def _unique_name(name: str, taken: set) -> str:
    """The name AmigaOS mounts a partition as, after the names in taken.

    taken holds lower-cased names. As the A4091 mounter's
    CheckAndFixDevName does, a name already taken gets ".1", and one that
    already ends in a dot and a digit below 9 has that digit raised, until
    the name is free, ignoring case: DH0, DH0.1, DH0.2.
    """
    while name.lower() in taken:
        if len(name) > 2 and name[-2] == "." and "0" <= name[-1] < "9":
            name = name[:-1] + chr(ord(name[-1]) + 1)
        else:
            name += ".1"
    return name


def mount_names(rdb_parts: list) -> List[List[Optional[str]]]:
    """Name the partitions of a disk as AmigaOS mounts them.

    rdb_parts holds each RDB's partitions (RDisk.parts), in the order the
    disk's RDBs open; the result holds their names in the same shape. The
    A4091 mounter names only the partitions it mounts, so those without the
    no-automount flag are named first, in that order, and then the others,
    which AmiFUSE mounts too, take names left free. A damaged drive name
    gives None and takes no name.
    """
    names = [[None] * len(parts) for parts in rdb_parts]
    taken: set = set()
    for automount in (True, False):
        for rdb_names, parts in zip(names, rdb_parts):
            for i, p in enumerate(parts):
                no_automount = p.get_flags() & PartitionBlock.FLAG_NO_AUTOMOUNT
                drive_name = p.get_drive_name()
                if bool(no_automount) == automount or drive_name is None:
                    continue
                rdb_names[i] = _unique_name(str(drive_name), taken)
                taken.add(rdb_names[i].lower())
    return names


def _failed_rdb(amiga_parts: List[MBRPartition], rdb_idx: Optional[int],
                error: OSError) -> Optional[str]:
    """Name an RDB that open_each_rdb yields as failing, for a message.

    Returns None if it shifts no names. An RDB found but not opening, the
    disk's own beside the MBR (DiskRDBError) or an entry's (EntryRDBError),
    shifts the names of the RDBs after it; an entry holding no RDB
    (NoEntryRDBError) shifts none. Raises any other OSError in an entry,
    such as a device or media error, as it ends open_rdisk, and one with an
    errno, a failing read, in the default open of a disk with 0x76 entries:
    that open stands for a single entry, and its own RDB failures are plain
    IOErrors without one. Those leave no RDB open whose names could shift.
    """
    if isinstance(error, (DiskRDBError, EntryRDBError)):
        return ("the RDB beside the MBR" if rdb_idx is None else
                f"the RDB in {rdb_label(amiga_parts, rdb_idx)}")
    if rdb_idx is None:
        if amiga_parts and error.errno is not None:
            raise error
    elif not isinstance(error, NoEntryRDBError):
        raise error
    return None


def _opened_rdb(mbr_ctx: Optional[MBRContext]) -> str:
    """Name an RDB that opened. Unlike rdb_label, tells a plain disk's RDB
    and a single entry's, both index None in open_each_rdb, apart."""
    if _is_entry_rdb(mbr_ctx):
        return f"RDB in MBR partition [{mbr_ctx.mbr_partition.index}]"
    return "RDB" if mbr_ctx is None else "RDB beside the MBR"


def _cut_short_rdb(rdisk: RDisk, mbr_ctx: Optional[MBRContext]) -> Optional[str]:
    """Name an RDB that opened with a partition list that ends at a broken
    link, for a message as _failed_rdb's, else None. The partitions after
    that link are unknown, so it counts as an RDB that fails."""
    if getattr(rdisk, "damaged_part_list", None) is None:
        return None
    return f"the rest of the partition list of the {_opened_rdb(mbr_ctx)}"


def _disk_names(
    image: Path, block_size: Optional[int],
) -> Tuple[List[Tuple[Optional[int], int, List[Optional[str]]]], List[str],
           Optional[Tuple[OSError, set]]]:
    """(mbr_partition_index, RDB offset, mount_names) per RDB that opens,
    each RDB that is found but does not open or opens with its partition
    list cut short, named for a message (_failed_rdb, _cut_short_rdb), and
    what ended the walk, or None: a device or media error in an entry's RDB,
    or in the default open of a disk with a single entry.

    After such an error, only the RDBs before that entry are listed, and
    the walk's end holds the error with the (mbr_partition_index, number)
    of each partition there whose name that entry can change: one with the
    no-automount flag, which is named after every RDB's others
    (mount_names).
    """
    rdbs = []
    rdb_parts = []
    failed = []
    error = None
    amiga_parts = amiga_rdb_partitions(image)
    for rdb_idx, opened in open_each_rdb(image, block_size, len(amiga_parts)):
        if isinstance(opened, IOError):
            try:
                name = _failed_rdb(amiga_parts, rdb_idx, opened)
            except OSError:
                error = opened
                break
            if name is not None:
                failed.append(name)
            continue
        blkdev, rdisk, mbr_ctx = opened
        try:
            rdbs.append((rdb_idx, _rdb_offset(blkdev, rdisk, mbr_ctx)))
            rdb_parts.append(list(rdisk.parts))
            name = _cut_short_rdb(rdisk, mbr_ctx)
            if name is not None:
                failed.append(name)
        finally:
            rdisk.close()
            blkdev.close()
    end = None
    if error is not None:
        end = error, {
            (rdb_idx, number)
            for (rdb_idx, _offset), parts in zip(rdbs, rdb_parts)
            for number, p in enumerate(parts)
            if p.get_flags() & PartitionBlock.FLAG_NO_AUTOMOUNT}
    return [(rdb_idx, offset, names) for (rdb_idx, offset), names
            in zip(rdbs, mount_names(rdb_parts))], failed, end


def _refuse_write(refusal: str, failed: List[str], shift: str) -> None:
    """Raise IOError for a writable open if an RDB in failed does not open."""
    if failed:
        verb = "does" if len(failed) == 1 else "do"
        raise IOError(f"{refusal}: "
                      f"{' and '.join(failed)} on this disk {verb} not open, "
                      f"so {shift}")


def _find_in(rdbs, partition: str):
    """(PartitionRef, RDB offset) for partition in _disk_names, or None.

    Looks the name up in every RDB first, then the number.
    """
    lowered = partition.lower()
    for rdb_idx, offset, names in rdbs:
        for number, name in enumerate(names):
            if name is not None and name.lower() == lowered:
                return PartitionRef(rdb_idx, number, name), offset
    try:
        number = int(partition)
    except ValueError:
        return None
    # As RDisk.get_partition, which numbers each RDB from 0. A negative
    # number selects none, rather than one counted from the end.
    for rdb_idx, offset, names in rdbs:
        if 0 <= number < len(names):
            return PartitionRef(rdb_idx, number, names[number]), offset
    return None


def _find_before(rdbs, partition: str, end):
    """_find_in for _disk_names' RDBs and the end of its walk.

    If a device or media error ended the walk, raises it unless partition
    is a name in the RDBs before the failing entry that the entry cannot
    change. A number raises it too: names outrank numbers, and the entry
    may hold a partition named as the number.
    """
    found = _find_in(rdbs, partition)
    if end is None:
        return found
    error, unsettled = end
    if found is not None:
        ref = found[0]
        # _find_in matches names before numbers, so a partition named as
        # partition was found by that name.
        if (ref.name is not None and ref.name.lower() == partition.lower()
                and (ref.mbr_partition_index, ref.number) not in unsettled):
            return found
    raise error


def find_partition(
    image: Path, block_size: Optional[int], partition: str, write: bool = False
) -> Optional[PartitionRef]:
    """Find the partition that ``--partition`` names, or None.

    Each partition is named as AmigaOS would mount it (:func:`mount_names`)
    in the order of :func:`open_each_rdb`: the disk's own RDB beside the
    MBR, then each 0x76 entry's own RDB. A repeated name, within one RDB or
    across RDBs, gets ".1", ".2" and so on. partition matches a name,
    ignoring case; failing that in every RDB, a number selects the first
    RDB that has a partition of that number. open_rdisk with the returned
    mbr_partition_index opens its RDB, and get_partition(number) the
    partition.

    Raises IOError if a forced block_size rules out an RDB, so that the
    partition found differs from the one auto-detection finds. With write,
    raises IOError too if an RDB on the disk is found but does not open,
    the disk's own beside the MBR or an entry's, or opens with its
    partition list cut short: its partitions are unknown, so any name or
    number may select another partition than with that RDB intact. A
    device or media error in an entry's RDB, or in the default open of a
    disk with a single entry, ends the lookup with that error; read-only,
    only if partition is not a name in an RDB before that entry that the
    entry cannot change (_find_before).
    """
    partition = str(partition)
    rdbs, failed, end = _disk_names(image, block_size)
    auto_rdbs, auto_end = rdbs, end
    if block_size is not None:
        # The forced size can fail an RDB; the shift that causes is refused
        # below, against the auto-detected RDBs. Only the auto-detected
        # open decides whether an RDB fails.
        auto_rdbs, failed, auto_end = _disk_names(image, None)
    if write:
        for walk_end in (end, auto_end):
            if walk_end is not None:
                raise walk_end[0]
        _refuse_write(
            f"Partition {partition} is not opened for writing", failed,
            "partition names and numbers may select another partition")
    found = _find_before(rdbs, partition, end)
    if block_size is None:
        return None if found is None else found[0]
    auto = _find_before(auto_rdbs, partition, auto_end)
    # Tell the RDBs apart by offset: the default open, index None, can
    # reach a different RDB at the forced size.
    if auto is None or (found is not None and
                        (found[1], found[0].number) == (auto[1], auto[0].number)):
        return None if found is None else found[0]
    # inspect looks up the names in the image, so partition can be one.
    shown = escape_text(partition)
    if auto[1] not in {offset for _idx, offset, _names in rdbs}:
        raise IOError(
            f"Partition {shown} is in an RDB that does not open "
            f"with {block_size}-byte blocks"
        )
    if found is None:
        raise IOError(
            f"Partition {shown} is not found with {block_size}-byte "
            f"blocks, as an RDB on this disk does not open with them"
        )
    raise IOError(
        f"Partition {shown} names another partition with {block_size}-byte "
        f"blocks, as an RDB on this disk does not open with them"
    )


def _default_rdb(
    image: Path, block_size: Optional[int],
) -> Tuple[Optional[int], List[str]]:
    """(RDB offset, or None) of the first RDB that opens, which the open
    without --partition reaches, and each RDB before it that is found but
    does not open (_failed_rdb, which also raises a device or media
    error), and the RDB reached if its partition list is cut short
    (_cut_short_rdb). An RDB after it cannot move that open. The disk's
    own RDB failing (DiskRDBError) ends that open, so no RDB is reached."""
    amiga_parts = amiga_rdb_partitions(image)
    failed = []
    for rdb_idx, opened in open_each_rdb(image, block_size, len(amiga_parts)):
        if isinstance(opened, IOError):
            name = _failed_rdb(amiga_parts, rdb_idx, opened)
            if name is not None:
                failed.append(name)
            if isinstance(opened, DiskRDBError):
                return None, failed
            continue
        blkdev, rdisk, mbr_ctx = opened
        try:
            name = _cut_short_rdb(rdisk, mbr_ctx)
            if name is not None:
                failed.append(name)
            return _rdb_offset(blkdev, rdisk, mbr_ctx), failed
        finally:
            rdisk.close()
            blkdev.close()
    return None, failed


def check_default_write(image: Path, block_size: Optional[int] = None) -> None:
    """Refuse a writable open without ``--partition`` as find_partition does.

    The open without it reaches partition 0 of the first RDB that opens
    (open_rdisk). Raises IOError if an RDB before that one is found but
    does not open, so that it may be another RDB's partition than with
    that RDB intact, or if that one's own partition list is cut short;
    like find_partition, only the auto-detected open decides whether an
    RDB fails. With a forced block_size, raises IOError too if the open
    reaches another RDB than the auto-detected one.
    """
    offset, failed = _default_rdb(image, None)
    _refuse_write("No partition is opened for writing without --partition",
                  failed, "the partition opened may be another one")
    if block_size is not None:
        forced, _failed = _default_rdb(image, block_size)
        if None not in (offset, forced) and forced != offset:
            raise IOError(
                f"No partition is opened for writing without --partition with "
                f"{block_size}-byte blocks: an RDB on this disk does not open "
                f"with them, so the partition opened is another one")


def find_partition_mbr_index(
    image: Path, block_size: Optional[int], partition_name: str
) -> Optional[int]:
    """open_rdisk's mbr_partition_index for the RDB holding a partition.

    The partition is found as :func:`find_partition` finds it. None selects
    the default open: the disk's own RDB beside the MBR if there is one,
    else the first 0x76 entry's, or a plain RDB. None also stands for a
    partition not found.

    Raises IOError if a forced block_size rules out an RDB, so that the
    partition found differs from the one auto-detection finds.
    """
    found = find_partition(image, block_size, partition_name)
    return None if found is None else found.mbr_partition_index


def partition_label(part) -> str:
    """How a message names an amitools ``Partition`` it has opened.

    The drive name; a damaged one, which amitools reads as None, by the
    partition's number: #0.
    """
    drive_name = part.get_drive_name()
    return f"#{part.num}" if drive_name is None else str(drive_name)


def _part_info(p) -> PartInfo:
    """Build a :class:`PartInfo` from an amitools ``Partition``.

    Reads only already-parsed, in-memory fields (``num``, the drive-name BSTR,
    and ``dos_env.dos_type``), so it is safe to call after the block device is
    closed. A damaged drive name, which amitools reads as None, gives a
    ``name`` of None: no ``--partition`` name selects that partition.
    """
    dos_type = p.part_blk.dos_env.dos_type
    drive_name = p.get_drive_name()
    return PartInfo(
        index=p.num,
        name=None if drive_name is None else str(drive_name),
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

    For multi-RDB images (Emu68-style disks with more than one ``0x76`` MBR
    partition, each carrying its own RDB) it enumerates across ALL ``0x76`` RDBs,
    mirroring the multi-RDB loop in ``rdb_inspect.main`` / ``fuse_fs.cmd_inspect``.
    Names -- not indices -- are the fan-out key, because ``Partition.num``
    restarts at 0 in each RDB and so collides across RDBs; every emitted name
    but None (a damaged drive name) re-resolves through
    :func:`find_partition` when the caller spawns
    ``--partition <name>``. A Parceiro disk's own RDB, beside its MBR, comes
    first, then each ``0x76`` entry's own RDB (:func:`open_each_rdb`).

    RDB-ONLY: the caller performs the non-RDB short-circuit (``detect_adf`` /
    ``detect_iso``) BEFORE calling this. ``list_partitions`` does not probe for
    ADF/ISO and propagates :class:`IOError` from :func:`open_rdisk` on a non-RDB
    image.

    Repeated names: amitools does not enforce drive-name uniqueness, within
    one RDB or across RDBs. Each partition is named as AmigaOS mounts it
    (:func:`mount_names`), so a repeated ``DH0`` is listed as ``DH0.1``,
    ``DH0.2``, and every name selects one partition.

    Args:
        image: Path to the RDB disk image.
        block_size: Force a specific block size (default: auto-detect).

    Returns:
        A :class:`PartitionList`. ``.partitions`` is in stable RDB order
        (normally >= 1 entry); ``.fallback_reason`` is ``None`` unless an
        RDB failed to open, the disk's own RDB beside the MBR or a 0x76
        entry's, or opened with its partition list cut short, each named on
        its own line with its error. An entry that holds no RDB is not
        named.
    """
    amiga_parts = amiga_rdb_partitions(image)

    # Enumerate across every RDB, keyed by name. Each iteration opens its own
    # (blkdev, rdisk) and MUST close both inside the loop or N block-device
    # handles leak.
    rdb_parts = []
    rdb_indices = []
    default_error = None
    # Each RDB that fails, named as inspect names it: its partitions are
    # missing from the list. An entry holding no RDB misses none.
    failures = []
    for rdb_idx, opened in open_each_rdb(image, block_size, len(amiga_parts)):
        if isinstance(opened, IOError):
            if rdb_idx is None:
                default_error = opened
            if not isinstance(opened, NoEntryRDBError):
                failures.append(f"{rdb_label(amiga_parts, rdb_idx)}: {opened}")
            continue
        blkdev, rdisk, mbr_ctx = opened
        try:
            rdb_parts.append(list(rdisk.parts))
            rdb_indices.append(rdb_idx)
            damaged = getattr(rdisk, "damaged_part_list", None)
            if damaged is not None:
                failures.append(f"{_opened_rdb(mbr_ctx)}: {damaged}")
        finally:
            rdisk.close()
            blkdev.close()
    if default_error is not None and not rdb_parts:
        # A plain or single-0x76 image whose one RDB does not open, or a
        # disk's own RDB that does not open beside entries holding none.
        raise default_error

    # The partitions' parsed fields outlive the closed handles (_part_info).
    all_parts: List[PartInfo] = []
    for rdb_idx, parts, names in zip(rdb_indices, rdb_parts,
                                     mount_names(rdb_parts)):
        for p, name in zip(parts, names):
            info = _part_info(p)
            info.mbr_partition_index = rdb_idx
            if name is not None:
                info.name = name
            all_parts.append(info)
    return PartitionList(partitions=all_parts,
                         fallback_reason="\n".join(failures) or None,
                         default_open_fails=default_error is not None)


def format_fs_summary(rdisk: RDisk):
    lines = []
    for fs in rdisk.fs:
        dt = fs.fshd.dos_type
        dt_str = dos_type_tag(dt)
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


def _is_entry_rdb(mbr_ctx: Optional[MBRContext]) -> bool:
    """Whether open_rdisk opened an RDB inside a 0x76 entry."""
    return mbr_ctx is not None and mbr_ctx.mbr_partition is not None


def _rdb_offset(blkdev, rdisk: RDisk, mbr_ctx: Optional[MBRContext]) -> int:
    """The byte offset of an opened RDB's RDSK block on the disk."""
    start = mbr_ctx.offset_blocks * MBR_SECTOR_SIZE if mbr_ctx else 0
    return start + rdisk.rdb.blk_num * blkdev.block_bytes


def open_each_rdb(
    image: Path, block_size: Optional[int], count: int,
) -> Iterator[Tuple[Optional[int], Union[
        Tuple[Union[RawBlockDevice, OffsetBlockDevice], RDisk, Optional[MBRContext]],
        OSError]]]:
    """Open each RDB of an MBR disk with `count` 0x76 entries, once.

    Yields (mbr_partition_index, opened). The disk's own RDB beside the
    MBR comes first, with index None, then each 0x76 entry's own RDB, in
    MBR order. opened is open_rdisk's (blkdev, rdisk, mbr_ctx), which the
    caller closes, or the IOError the RDB failed with. The disk's own RDB
    that does not open gives its DiskRDBError once, with index None, and
    the entries are still opened. An entry whose RDB is found but does not
    open gives EntryRDBError; a device or media error in it, that error.
    Beside the disk's own RDB, entries that hold no RDB (NoEntryRDBError)
    are skipped. An RDB already yielded, as
    by two entries starting at the same sector, is skipped too. With fewer
    than two entries, yields the default open, or its error, with index
    None, then beside the disk's own RDB a single entry's own.
    """
    if count <= 1:
        try:
            opened = open_rdisk(image, block_size=block_size)
        except IOError as e:
            opened = e
        yield None, opened
        if isinstance(opened, IOError):
            if count == 0 or not isinstance(opened, DiskRDBError):
                return
        elif count == 0 or _is_entry_rdb(opened[2]):
            return
        try:
            opened = open_rdisk(image, block_size=block_size,
                                mbr_partition_index=0)
        except NoEntryRDBError:
            return  # The entry holds no RDB of its own.
        except IOError as e:
            opened = e
        yield 0, opened
        return
    seen = set()
    disk_rdb = False
    for index in [None, *range(count)]:
        try:
            opened = open_rdisk(image, block_size=block_size,
                                mbr_partition_index=index)
        except IOError as e:
            if index is None:
                # Content failures in entries recur in their own turns.
                # A device error may instead belong to the disk's RDB,
                # which selected entry opens deliberately do not parse.
                if isinstance(e, DiskRDBError):
                    disk_rdb = True
                    yield index, e
                elif not isinstance(e, (RDBContentError, EntryRDBError,
                                        NoEntryRDBError)) and e.errno is not None:
                    yield index, e
            elif not (disk_rdb and isinstance(e, NoEntryRDBError)):
                # With no RDB beside the MBR every entry gives its error;
                # beside it, only an entry that holds an RDB.
                yield index, e
            continue
        blkdev, rdisk, mbr_ctx = opened
        # Tell RDBs apart by the byte offset of their RDSK block.
        offset = _rdb_offset(blkdev, rdisk, mbr_ctx)
        if (index is None and _is_entry_rdb(mbr_ctx)) or offset in seen:
            # The default open fell back to an entry's RDB, which comes in
            # its turn, or this RDB came already.
            rdisk.close()
            blkdev.close()
            continue
        disk_rdb = disk_rdb or index is None
        seen.add(offset)
        yield index, opened


def build_rdisk_desc(rdisk: RDisk, mbr_ctx: Optional[MBRContext]) -> dict:
    """Build the JSON-able description of a parsed RDisk.

    Folds the MBR context and lenient-parse warnings into the amitools
    desc dict. Shared by `rdb-inspect --json` and `amifuse inspect --json`.
    A damaged drive name, which amitools reads as None and its desc gives as
    "None", is given as None, to tell it from a partition named None.
    """
    desc = rdisk.get_desc()
    for part_desc, part in zip(desc["partitions"], rdisk.parts):
        if part.get_drive_name() is None:
            part_desc["name"] = None
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
    for part in rdisk.parts:
        if part.get_drive_name() is None:
            # amitools' line reads as if the partition were named None.
            prefix = f"Partition: #{part.num} 'None'"
            lines = [
                f"Partition: #{part.num} (damaged drive name)"
                + line[len(prefix):] if line.startswith(prefix) else line
                for line in lines
            ]
    # amitools builds these lines with the image's drive names and DOS types.
    lines = [escape_text(line) for line in lines]
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
    SystemExit when the image cannot be opened, or when no RDB of an image
    with 0x76 entries can; per-RDB failures on a disk with more than one RDB
    are reported inline and skipped.
    """
    mbr_info = detect_mbr(image)
    amiga_parts = _amiga_partitions(mbr_info)
    if len(amiga_parts) > 1:
        # Open every RDB before printing, so the MBR header can describe the
        # first one that opened. Each section holds one partition's report
        # lines, or the reason it could not be opened.
        header_ctx = None
        disk_error = None
        opened_any = False
        sections = []
        for rdb_idx, opened in open_each_rdb(image, block_size, len(amiga_parts)):
            if isinstance(opened, IOError):
                if rdb_idx is None:
                    # The disk's own RDB, beside the MBR, did not open.
                    disk_error = opened
                    header_ctx = _disk_rdb_context(mbr_info)
                sections.append([f"\n{rdb_label(amiga_parts, rdb_idx)}: "
                                 f"Error: {opened}"])
                continue
            blkdev, rdisk, mbr_ctx = opened
            try:
                if mbr_ctx.mbr_partition is None:
                    # Parceiro-style: the disk's own RDB, beside the MBR.
                    # Report it as for a single RDB.
                    lines = [""]
                else:
                    lines = [_entry_heading(mbr_ctx)]
                lines += format_rdisk_report(rdisk, full=full)
            finally:
                rdisk.close()
                blkdev.close()
            if header_ctx is None:
                header_ctx = mbr_ctx
            opened_any = True
            sections.append(lines)

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
        if not opened_any:
            if disk_error is not None:
                # Its error is already printed above.
                raise SystemExit(f"Error: {_no_rdb_opens_message(image)}")
            raise SystemExit(
                f"Error: {_no_valid_rdb_message(image, len(amiga_parts))}"
            )
        return

    # Single RDB (plain or Parceiro-style), then beside a Parceiro disk's own
    # RDB its single 0x76 entry's own RDB, if it holds one.
    opened_any = False
    for rdb_idx, opened in open_each_rdb(image, block_size, len(amiga_parts)):
        if isinstance(opened, IOError):
            if rdb_idx is None:
                if not (amiga_parts and isinstance(opened, DiskRDBError)):
                    raise SystemExit(f"Error: {opened}")
                # The disk's own RDB, beside the MBR, did not open; its
                # 0x76 entry's own RDB comes next.
                for line in format_mbr_info(_disk_rdb_context(mbr_info)):
                    print(line)
            print(f"\n{rdb_label(amiga_parts, rdb_idx)}: Error: {opened}")
            continue
        opened_any = True
        blkdev, rdisk, mbr_ctx = opened
        try:
            if rdb_idx is not None:
                print(_entry_heading(mbr_ctx))
            elif mbr_ctx is not None:
                for line in format_mbr_info(mbr_ctx):
                    print(line)
                print()
            for line in format_rdisk_report(rdisk, full=full):
                print(line)
        finally:
            rdisk.close()
            blkdev.close()
    if not opened_any:
        # The disk's own RDB failed, and its error is already printed above.
        raise SystemExit(f"Error: {_no_rdb_opens_message(image)}")


def rdb_label(amiga_parts: List[MBRPartition], rdb_idx: Optional[int]) -> str:
    """How inspect names the RDB that open_each_rdb yields at rdb_idx."""
    if rdb_idx is None:
        return "RDB beside the MBR"
    return f"MBR partition [{amiga_parts[rdb_idx].index}]"


def _entry_heading(mbr_ctx: MBRContext) -> str:
    """The report heading of an RDB inside a 0x76 entry."""
    return (f"\n=== Amiga RDB in MBR partition [{mbr_ctx.mbr_partition.index}]"
            f" (offset: {mbr_ctx.offset_blocks} sectors) ===\n")


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


def filesystem_entry(rdisk: RDisk, num: int):
    """The filesystem entry the report shows as FS #num.

    The lenient parse numbers entries by their place in the RDB's list,
    dropped ones included, so this is not rdisk.get_filesystem(), which
    indexes the entries kept. Raises IndexError saying why there is none.
    """
    for fs in rdisk.fs:
        if fs.num == num:
            return fs
    for dos_type, warning, damaged_num in getattr(rdisk, "damaged_fs", ()):
        # A DOS type of None hid the entries after that one too.
        if num == damaged_num or (dos_type is None and num > damaged_num):
            raise IndexError(f"Cannot extract filesystem #{num}: {warning}")
    raise IndexError(f"No filesystem #{num} in RDB.")


def extract_fs(rdisk: RDisk, index: int, out_path: Path) -> Path:
    data = filesystem_entry(rdisk, index).get_data()
    out_path.write_bytes(data)
    return out_path


def block_size_arg(text: str) -> int:
    """argparse type for --block-size: a power of two from 512 to 32768.

    The rule _scan_for_rdb applies to a block size an RDB declares. 0 is
    refused rather than read as "auto-detect": leaving the option out
    detects the size.
    """
    try:
        size = int(text)
    except ValueError:
        size = None
    if size == 0:
        raise argparse.ArgumentTypeError(
            "0 is not a block size; leave the option out to detect it")
    if size is None or not 512 <= size <= 32768 or size & (size - 1):
        raise argparse.ArgumentTypeError(
            f"must be a power of two from 512 to 32768, not {text!r}")
    return size


def dos_type_arg(text: str) -> int:
    """argparse type for format's --dos-type: DOS0 to DOS7, as a DOS type.

    These are the types detect_adf accepts, so that mount finds the floppy.
    """
    tag = text.upper()
    if len(tag) != 4 or not tag.startswith("DOS") or tag[3] not in "01234567":
        raise argparse.ArgumentTypeError(f"must be DOS0 to DOS7, not {text!r}")
    return ADF_DEFAULT_DOS_TYPE + int(tag[3])


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect an RDB image and optionally extract filesystem drivers."
    )
    parser.add_argument("image", type=Path, help="Path to the RDB image (raw file)")
    parser.add_argument(
        "--block-size",
        type=block_size_arg,
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
        help="Extract filesystem entry N (0-based), the report's FS #N, to a host file.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="Output path when extracting a filesystem (default derives from dostype).",
    )
    args = parser.parse_args(argv)

    if args.json:
        # JSON mode: collect all RDBs into a single JSON structure,
        # skipping RDBs that fail to open, unless none opens.
        all_rdbs = []
        failures = []
        amiga_parts = amiga_rdb_partitions(args.image)
        for rdb_idx, opened in open_each_rdb(args.image, args.block_size,
                                             len(amiga_parts)):
            if isinstance(opened, IOError):
                failures.append((rdb_idx, opened))
                continue
            blkdev, rdisk, mbr_ctx = opened
            try:
                all_rdbs.append(build_rdisk_desc(rdisk, mbr_ctx))
            finally:
                rdisk.close()
                blkdev.close()
        if not all_rdbs:
            summary = _nothing_opens_message(args.image, amiga_parts, failures)
            if [summary] == [str(error) for _, error in failures]:
                raise SystemExit(f"Error: {summary}")
            raise SystemExit("\n".join(
                [f"Error: {summary}"]
                + [f"{rdb_label(amiga_parts, rdb_idx)}: {error}"
                   for rdb_idx, error in failures]))
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
            try:
                fs_obj = filesystem_entry(rdisk, args.extract_fs)
            except IndexError as exc:
                raise SystemExit(str(exc))
            dt = fs_obj.fshd.dos_type
            # By number: the tag's characters come from the image.
            default_name = f"fs{args.extract_fs}_{dt:08x}.bin"
            out_path = args.out or Path(default_name)
            saved_to = extract_fs(rdisk, args.extract_fs, out_path)
            print(f"Wrote filesystem #{args.extract_fs} ({hex(dt)}) to {saved_to}")
        finally:
            rdisk.close()
            blkdev.close()


if __name__ == "__main__":
    main()
