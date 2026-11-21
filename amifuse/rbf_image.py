"""
Detection of OS-9 RBF disk images that have no RDB.

OS-9 disks hold no Amiga partition table: an OS-9/68k disk starts with its
identification sector (LSN 0), an OS-9000 disk with its ID block. A PC
OS-9000 floppy or partition starts with a boot sector, with the ID block in
the sector after it, and a PC hard disk holds its OS-9000 partitions in an
MBR. os9-rbf-handler mounts all three formats; this module finds the
volume, so that it can be given to the handler as a partition with
synthetic geometry, as an ADF is given to FastFileSystem.

The handler finds the format and byte order itself. The DOS type recorded
here is the one its mount entry would have (OS9\\0, OS9\\1 or OS9\\2), which
picks what ACTION_FORMAT writes, and names the volume's type in messages.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from amitools.util.Win32Disk import image_size, open_image  # type: ignore

# The DOS types of os9-rbf-handler's mount entries.
DOS_OS9_68K = 0x4F533900    # "OS9\0": OS-9/68k RBF
DOS_OS9K_BE = 0x4F533901    # "OS9\1": OS-9000 RBF, big-endian
DOS_OS9K_LE = 0x4F533902    # "OS9\2": OS-9000 RBF, little-endian
OS9_FAMILY = 0x4F533900
DRIVER_NAME = "os9-rbf-handler"
RELEASES_URL = "https://github.com/cdhooper/amiga_os9_rbf_handler/releases"

# A PC boot sector comes before the ID block on PC OS-9000 disks.
PC_BOOT_SECTOR = 512
MBR_SECTOR = 512
MBR_TYPE_AMIGA_RDB = 0x76

_OS9K_SYNC = b"\xad\xb0\xb0\xad"
_FMT_NAMES = {
    DOS_OS9_68K: "OS-9/68k",
    DOS_OS9K_BE: "OS-9000 (big-endian)",
    DOS_OS9K_LE: "OS-9000 (little-endian)",
}


def is_os9_type(dos_type: Optional[int]) -> bool:
    """Return True for a DOS type os9-rbf-handler serves (OS9\\0-OS9\\2)."""
    return dos_type is not None and DOS_OS9_68K <= dos_type <= DOS_OS9K_LE


def format_name(dos_type: int) -> str:
    """Name an OS-9 DOS type's disk format, e.g. "OS-9000 (big-endian)"."""
    return _FMT_NAMES.get(dos_type, f"OS-9 (0x{dos_type:08x})")


@dataclass
class RBFInfo:
    """An OS-9 RBF volume in an image without an RDB.

    The handler sees the image as one surface of one block per track, so a
    cylinder is a block, and the volume as the cylinders low_block to
    low_block + num_blocks - 1. num_blocks reaches the end of the image or of
    the volume's MBR partition, which the volume may not fill.
    """
    dos_type: int               # DOS_OS9_68K, DOS_OS9K_BE or DOS_OS9K_LE
    volume_name: str            # From LSN 0 or the ID block
    block_size: int             # The RBF sector size, 256 to 4096 bytes
    low_block: int              # First block of the volume, in block_size units
    num_blocks: int             # Blocks from low_block to the container's end
    fs_blocks: int              # Blocks the volume's own header records
    mbr_index: Optional[int] = None     # MBR entry (0-3) holding it, if any
    boot_sector: bool = False   # A PC boot sector precedes the ID block

    # The geometry the synthetic DosEnvec declares.
    heads: int = 1
    sectors_per_track: int = 1

    @property
    def cylinders(self) -> int:
        return self.low_block + self.num_blocks

    @property
    def total_blocks(self) -> int:
        return self.num_blocks

    @property
    def format_name(self) -> str:
        return format_name(self.dos_type)

    def where(self) -> str:
        """Where the volume lies in the image, for messages."""
        place = []
        if self.mbr_index is not None:
            place.append(f"MBR partition {self.mbr_index}")
        if self.boot_sector:
            place.append("after a PC boot sector")
        return ", ".join(place)


def _get(data: bytes, offset: int, size: int, little: bool) -> int:
    return int.from_bytes(data[offset:offset + size], "little" if little else "big")


def _volume_name(raw: bytes) -> str:
    """Decode a volume name: NUL terminated, or high bit on its last char."""
    out = []
    for b in raw:
        if b == 0:
            break
        out.append(chr(b & 0x7F))
        if b & 0x80:
            break
    return "".join(out).strip()


def _log2_ok(n: int) -> bool:
    return 256 <= n <= 4096 and n & (n - 1) == 0


class _Reader:
    """Reads byte ranges of an open image, None past its end."""

    def __init__(self, f, size: int):
        self.f = f
        self.size = size

    def read(self, offset: int, length: int) -> Optional[bytes]:
        if offset < 0 or offset + length > self.size:
            return None
        self.f.seek(offset)
        data = self.f.read(length)
        return data if len(data) == length else None


def _probe_os9k(rd: _Reader, base: int, avail: int):
    """(dos_type, block size, total blocks, name) of an OS-9000 ID block at
    base, or None. Follows the handler's checks on the ID block."""
    idb = rd.read(base, 96)
    if idb is None or idb[:4] != _OS9K_SYNC:
        return None
    flag = idb[88:89]
    if flag == b"L":
        little = True
    elif flag == b"B":
        little = False
    else:
        little = not _log2_ok(_get(idb, 20, 2, False))
    bs = _get(idb, 20, 2, little)
    total = _get(idb, 8, 4, little)
    root = _get(idb, 40, 4, little)
    bitmap = _get(idb, 28, 4, little)
    if not _log2_ok(bs) or total < 8 or total * bs > avail:
        return None
    if not (0 < root < total and 0 < bitmap < total):
        return None
    dos_type = DOS_OS9K_LE if little else DOS_OS9K_BE
    return dos_type, bs, total, _volume_name(idb[56:88])


def _probe_os9_68k(rd: _Reader, base: int, avail: int):
    """(block size, total sectors, name) of an OS-9/68k LSN 0 at base, or
    None. RBF has no magic number: like the handler, this checks the layout
    and that the root directory starts with ".." and "." entries."""
    lsn0 = rd.read(base, 256)
    if lsn0 is None:
        return None
    lsnsize = _get(lsn0, 0x68, 2, False)
    sizes = [lsnsize] if lsnsize else [256, 512, 1024, 2048, 4096]
    tot = _get(lsn0, 0x00, 3, False)
    bit = _get(lsn0, 0x06, 2, False) or 1
    mapb = _get(lsn0, 0x04, 2, False)
    root = _get(lsn0, 0x08, 3, False)
    maplsn = _get(lsn0, 0x64, 4, False)
    if tot < 4 or bit & (bit - 1) or mapb * 8 < (tot + bit - 1) // bit:
        return None
    if maplsn not in (0, 1):
        return None
    for ss in sizes:
        if not _log2_ok(ss) or tot * ss > avail:
            continue
        map_sectors = (mapb + ss - 1) // ss
        if root < 1 + map_sectors or root >= tot:
            continue
        fd = rd.read(base + root * ss, 256)
        if fd is None or not fd[0] & 0x80:      # FD_ATT: a directory
            continue
        size = _get(fd, 0x09, 4, False)
        seg = _get(fd, 0x10, 3, False)
        seglen = _get(fd, 0x13, 2, False)
        if size < 64 or seglen == 0 or seg < 1 + map_sectors or seg + seglen > tot:
            continue
        ents = rd.read(base + seg * ss, 64)
        if ents is None:
            continue
        if (ents[0:2] == b".\xae" and ents[32] == 0xAE
                and _get(ents, 32 + 28, 4, False) == root):
            return ss, tot, _volume_name(lsn0[0x1F:0x3F])
    return None


def _probe_at(rd: _Reader, base: int, end: int, mbr_index: Optional[int]) -> Optional[RBFInfo]:
    """The RBF volume at byte base, or in the sector after it, before end."""
    for boot in (False, True):
        start = base + (PC_BOOT_SECTOR if boot else 0)
        avail = end - start
        if avail <= 0:
            return None
        found = _probe_os9k(rd, start, avail)
        if found is not None:
            dos_type, bs, total, name = found
        elif not boot:
            found68 = _probe_os9_68k(rd, start, avail)
            if found68 is None:
                continue
            bs, total, name = found68
            dos_type = DOS_OS9_68K
        else:
            continue
        if start % bs:
            # The handler starts the volume on a block of its size.
            continue
        return RBFInfo(
            dos_type=dos_type, volume_name=name, block_size=bs,
            low_block=start // bs, num_blocks=avail // bs, fs_blocks=total,
            mbr_index=mbr_index, boot_sector=boot,
        )
    return None


def find_rbf_volumes(image: Path) -> List[RBFInfo]:
    """Every OS-9 RBF volume of an image without an RDB.

    That is the image itself, or the volumes of its MBR partitions. An
    image with an RDSK block in its first 16 sectors, or an MBR entry for
    an RDB (type 0x76), is left to the RDB code, and gives none.
    """
    try:
        size = image_size(image)
        with open_image(image) as f:
            rd = _Reader(f, size)
            head = rd.read(0, min(size, 16 * MBR_SECTOR)) or b""
            if any(head[i:i + 4] == b"RDSK" for i in range(0, len(head), MBR_SECTOR)):
                return []

            whole = _probe_at(rd, 0, size, None)
            if whole is not None:
                return [whole]

            from .rdb_inspect import parse_mbr
            mbr = parse_mbr(head[:MBR_SECTOR])
            if mbr is None or mbr.has_amiga_partitions:
                return []
            found = []
            for part in mbr.partitions:
                start = part.start_lba * MBR_SECTOR
                end = min(size, start + part.num_sectors * MBR_SECTOR)
                vol = _probe_at(rd, start, end, part.index)
                if vol is not None:
                    found.append(vol)
            return found
    except OSError:
        return []


def select_rbf_volume(volumes: List[RBFInfo], partition: Optional[str]) -> RBFInfo:
    """The volume --partition names: its MBR entry number (0-3) or its
    volume name, ignoring case. The first volume without --partition.

    Raises ValueError naming the volumes when none matches.
    """
    if partition is None:
        return volumes[0]
    for vol in volumes:
        if vol.mbr_index is not None and partition == str(vol.mbr_index):
            return vol
    for vol in volumes:
        if vol.volume_name.lower() == partition.lower():
            return vol
    names = ", ".join(
        f"{vol.mbr_index} ({vol.volume_name})" if vol.mbr_index is not None
        else vol.volume_name for vol in volumes)
    raise ValueError(f"No OS-9 volume '{partition}' in this image; it holds: {names}")


def detect_rbf(image: Path) -> Optional[RBFInfo]:
    """The first OS-9 RBF volume of an image without an RDB, or None."""
    volumes = find_rbf_volumes(image)
    return volumes[0] if volumes else None
