"""Unit tests for amifuse.rdb_inspect detection functions and OffsetBlockDevice.

All tests request the ``amitools_mock`` fixture because ``rdb_inspect.py`` has
top-level ``amitools`` imports that fail without it.  The tested functions
themselves are pure Python and do not call into amitools.
"""
import struct

import pytest


# ---------------------------------------------------------------------------
# A. detect_adf() -- ADF floppy detection (6 tests)
# ---------------------------------------------------------------------------

ADF_DD_SIZE = 901120
ADF_HD_SIZE = 1802240


def _write_adf(path, *, size, header=b"DOS\x00"):
    """Write a minimal ADF-like file: *header* followed by zero-padding to *size*."""
    with open(path, "wb") as f:
        f.write(header)
        f.write(b"\x00" * (size - len(header)))


def test_detect_adf_dd_floppy(amitools_mock, tmp_path):
    """DD floppy: 901,120 bytes with DOS\\x00 header."""
    from amifuse.rdb_inspect import detect_adf

    img = tmp_path / "dd.adf"
    _write_adf(img, size=ADF_DD_SIZE, header=b"DOS\x00")

    info = detect_adf(img)
    assert info is not None
    assert info.is_hd is False
    assert info.sectors_per_track == 11
    assert info.dos_type == 0x444F5300
    assert info.cylinders == 80
    assert info.heads == 2
    assert info.block_size == 512
    assert info.total_blocks == 80 * 2 * 11


def test_detect_adf_hd_floppy(amitools_mock, tmp_path):
    """HD floppy: 1,802,240 bytes with DOS\\x01 header."""
    from amifuse.rdb_inspect import detect_adf

    img = tmp_path / "hd.adf"
    _write_adf(img, size=ADF_HD_SIZE, header=b"DOS\x01")

    info = detect_adf(img)
    assert info is not None
    assert info.is_hd is True
    assert info.sectors_per_track == 22
    assert info.dos_type == 0x444F5301
    assert info.total_blocks == 80 * 2 * 22


def test_detect_adf_all_variants(amitools_mock, tmp_path):
    """DOS type variants 0-7 are all accepted."""
    from amifuse.rdb_inspect import detect_adf

    for variant in range(8):
        img = tmp_path / f"v{variant}.adf"
        _write_adf(img, size=ADF_DD_SIZE, header=bytes([0x44, 0x4F, 0x53, variant]))

        info = detect_adf(img)
        assert info is not None, f"variant {variant} should be accepted"
        assert info.dos_type == 0x444F5300 | variant


def test_detect_adf_wrong_size(amitools_mock, tmp_path):
    """File not matching DD or HD size returns None."""
    from amifuse.rdb_inspect import detect_adf

    img = tmp_path / "bad_size.adf"
    # A size that is neither 901120 nor 1802240
    _write_adf(img, size=500000, header=b"DOS\x00")

    assert detect_adf(img) is None


def test_detect_adf_bad_header(amitools_mock, tmp_path):
    """Correct size but wrong header returns None."""
    from amifuse.rdb_inspect import detect_adf

    img = tmp_path / "bad_hdr.adf"
    _write_adf(img, size=ADF_DD_SIZE, header=b"XXX\x00")

    assert detect_adf(img) is None


def test_detect_adf_variant_out_of_range(amitools_mock, tmp_path):
    """DOS\\x08 (variant > 7) returns None."""
    from amifuse.rdb_inspect import detect_adf

    img = tmp_path / "v8.adf"
    _write_adf(img, size=ADF_DD_SIZE, header=b"DOS\x08")

    assert detect_adf(img) is None


# ---------------------------------------------------------------------------
# B. detect_iso() -- ISO 9660 detection (4 tests)
# ---------------------------------------------------------------------------

ISO_BLOCK_SIZE = 2048
ISO_PVD_OFFSET = 16 * ISO_BLOCK_SIZE  # byte offset of PVD = 32768


def _write_iso(path, *, volume_id="TEST_VOLUME", size=None):
    """Write a minimal ISO image with a valid PVD at sector 16.

    The PVD consists of type byte 0x01, identifier 'CD001', and a
    volume identifier at bytes 40-71 (32 chars, space-padded).
    """
    # PVD block: 2048 bytes
    pvd = bytearray(ISO_BLOCK_SIZE)
    pvd[0] = 0x01  # type: Primary Volume Descriptor
    pvd[1:6] = b"CD001"
    # Volume identifier at bytes 40-71 (32 chars, space-padded)
    vol_bytes = volume_id.encode("ascii")[:32].ljust(32, b" ")
    pvd[40:72] = vol_bytes

    # Minimum file size: PVD offset + one full block
    min_size = ISO_PVD_OFFSET + ISO_BLOCK_SIZE
    total_size = size if size is not None else min_size

    with open(path, "wb") as f:
        # Zero-fill up to PVD offset
        f.write(b"\x00" * ISO_PVD_OFFSET)
        f.write(pvd)
        # Pad to total size if needed
        remaining = total_size - ISO_PVD_OFFSET - ISO_BLOCK_SIZE
        if remaining > 0:
            f.write(b"\x00" * remaining)


def test_detect_iso_valid(amitools_mock, tmp_path):
    """Valid ISO with PVD at sector 16."""
    from amifuse.rdb_inspect import detect_iso

    img = tmp_path / "test.iso"
    _write_iso(img, volume_id="MY_DISK")

    info = detect_iso(img)
    assert info is not None
    assert info.block_size == 2048
    assert info.volume_id == "MY_DISK"
    assert info.heads == 1
    assert info.sectors_per_track == 1


def test_detect_iso_too_small(amitools_mock, tmp_path):
    """File smaller than PVD offset + one block returns None."""
    from amifuse.rdb_inspect import detect_iso

    img = tmp_path / "tiny.iso"
    # Write fewer bytes than required (PVD offset + block size)
    with open(img, "wb") as f:
        f.write(b"\x00" * (ISO_PVD_OFFSET + ISO_BLOCK_SIZE - 1))

    assert detect_iso(img) is None


def test_detect_iso_bad_signature(amitools_mock, tmp_path):
    """Correct size but wrong PVD signature returns None."""
    from amifuse.rdb_inspect import detect_iso

    img = tmp_path / "bad_sig.iso"
    # Write enough data but with wrong signature
    total_size = ISO_PVD_OFFSET + ISO_BLOCK_SIZE
    with open(img, "wb") as f:
        f.write(b"\x00" * total_size)

    assert detect_iso(img) is None


def test_detect_iso_volume_id(amitools_mock, tmp_path):
    """Volume identifier is extracted and trailing spaces stripped."""
    from amifuse.rdb_inspect import detect_iso

    img = tmp_path / "vol.iso"
    _write_iso(img, volume_id="AMIGA")

    info = detect_iso(img)
    assert info is not None
    # "AMIGA" padded to 32 chars with spaces, then rstripped
    assert info.volume_id == "AMIGA"


# ---------------------------------------------------------------------------
# C. detect_mbr() -- MBR partition table detection (5 tests)
# ---------------------------------------------------------------------------


def _build_mbr_block(partitions=None):
    """Build a 512-byte MBR block with the given partition entries.

    Each entry in *partitions* is a dict with keys:
        bootable (bool), type (int), start_lba (int), num_sectors (int)
    Up to 4 entries; missing slots are zeroed.
    """
    block = bytearray(512)

    if partitions:
        for i, p in enumerate(partitions[:4]):
            offset = 0x1BE + i * 16
            entry = bytearray(16)
            entry[0] = 0x80 if p.get("bootable", False) else 0x00
            entry[4] = p.get("type", 0)
            struct.pack_into("<I", entry, 8, p.get("start_lba", 0))
            struct.pack_into("<I", entry, 12, p.get("num_sectors", 0))
            block[offset : offset + 16] = entry

    # MBR signature
    block[0x1FE] = 0x55
    block[0x1FF] = 0xAA
    return bytes(block)


def test_detect_mbr_valid(amitools_mock, tmp_path):
    """Valid MBR with one non-empty partition entry."""
    from amifuse.rdb_inspect import detect_mbr

    img = tmp_path / "disk.img"
    mbr = _build_mbr_block(
        partitions=[{"type": 0x0B, "start_lba": 2048, "num_sectors": 65536}]
    )
    img.write_bytes(mbr)

    info = detect_mbr(img)
    assert info is not None
    assert len(info.partitions) == 1
    assert info.partitions[0].partition_type == 0x0B
    assert info.partitions[0].start_lba == 2048
    assert info.partitions[0].num_sectors == 65536
    assert info.partitions[0].index == 0
    assert info.has_amiga_partitions is False


def test_detect_mbr_amiga_partition(amitools_mock, tmp_path):
    """Partition type 0x76 sets has_amiga_partitions=True."""
    from amifuse.rdb_inspect import detect_mbr

    img = tmp_path / "amiga.img"
    mbr = _build_mbr_block(
        partitions=[{"type": 0x76, "start_lba": 1, "num_sectors": 100000}]
    )
    img.write_bytes(mbr)

    info = detect_mbr(img)
    assert info is not None
    assert info.has_amiga_partitions is True
    assert info.partitions[0].partition_type == 0x76


def test_detect_mbr_no_signature(amitools_mock, tmp_path):
    """Missing 0x55AA signature returns None."""
    from amifuse.rdb_inspect import detect_mbr

    img = tmp_path / "nosig.img"
    block = bytearray(512)
    # Write a partition entry but no signature
    block[0x1BE + 4] = 0x0B  # type
    struct.pack_into("<I", block, 0x1BE + 12, 1000)  # num_sectors
    img.write_bytes(bytes(block))

    assert detect_mbr(img) is None


def test_detect_mbr_empty_partitions(amitools_mock, tmp_path):
    """Valid signature but all partition entries empty returns None."""
    from amifuse.rdb_inspect import detect_mbr

    img = tmp_path / "empty.img"
    # Build MBR with signature but no partitions filled in
    mbr = _build_mbr_block(partitions=[])
    img.write_bytes(mbr)

    assert detect_mbr(img) is None


def test_detect_mbr_multiple_partitions(amitools_mock, tmp_path):
    """Four non-empty partitions, all parsed correctly."""
    from amifuse.rdb_inspect import detect_mbr

    partitions = [
        {"type": 0x0B, "start_lba": 2048, "num_sectors": 10000, "bootable": True},
        {"type": 0x83, "start_lba": 12048, "num_sectors": 20000},
        {"type": 0x76, "start_lba": 32048, "num_sectors": 50000},
        {"type": 0x82, "start_lba": 82048, "num_sectors": 8000},
    ]
    img = tmp_path / "multi.img"
    mbr = _build_mbr_block(partitions=partitions)
    img.write_bytes(mbr)

    info = detect_mbr(img)
    assert info is not None
    assert len(info.partitions) == 4
    assert info.has_amiga_partitions is True  # partition at index 2 is 0x76

    # Verify each partition was parsed correctly
    assert info.partitions[0].index == 0
    assert info.partitions[0].partition_type == 0x0B
    assert info.partitions[0].bootable is True
    assert info.partitions[0].start_lba == 2048
    assert info.partitions[0].num_sectors == 10000

    assert info.partitions[1].index == 1
    assert info.partitions[1].partition_type == 0x83
    assert info.partitions[1].bootable is False
    assert info.partitions[1].start_lba == 12048
    assert info.partitions[1].num_sectors == 20000

    assert info.partitions[2].index == 2
    assert info.partitions[2].partition_type == 0x76
    assert info.partitions[2].start_lba == 32048
    assert info.partitions[2].num_sectors == 50000

    assert info.partitions[3].index == 3
    assert info.partitions[3].partition_type == 0x82
    assert info.partitions[3].start_lba == 82048
    assert info.partitions[3].num_sectors == 8000


# ---------------------------------------------------------------------------
# D. OffsetBlockDevice (3 tests)
# ---------------------------------------------------------------------------


class _MockBlockDevice:
    """Minimal mock block device for OffsetBlockDevice tests."""

    def __init__(self, block_bytes=512):
        self.block_bytes = block_bytes
        self._blocks = {}
        self._read_log = []
        self._write_log = []

    def read_block(self, blk_num, num_blks=1):
        self._read_log.append((blk_num, num_blks))
        # Return zeroed data
        return b"\x00" * (self.block_bytes * num_blks)

    def write_block(self, blk_num, data, num_blks=1):
        self._write_log.append((blk_num, data, num_blks))


def test_offset_block_device_read(amitools_mock):
    """read_block adds offset to the underlying device block number."""
    from amifuse.rdb_inspect import OffsetBlockDevice

    base = _MockBlockDevice(block_bytes=512)
    offset_blocks = 100
    num_blocks = 50
    obd = OffsetBlockDevice(base, offset_blocks, num_blocks)

    # Read block 5 from offset device -> should read block 105 from base
    obd.read_block(5)
    assert base._read_log == [(105, 1)]

    # Read multiple blocks
    obd.read_block(10, 3)
    assert base._read_log[-1] == (110, 3)

    # Verify stored attributes
    assert obd.block_bytes == 512
    assert obd.block_longs == 128  # 512 // 4
    assert obd.num_blocks == num_blocks
    assert obd.offset == offset_blocks


def test_offset_block_device_boundary_check(amitools_mock):
    """Reading beyond num_blocks raises OSError."""
    from amifuse.rdb_inspect import OffsetBlockDevice

    base = _MockBlockDevice(block_bytes=512)
    obd = OffsetBlockDevice(base, offset_blocks=0, num_blocks=10)

    # Exactly at boundary: block 9, 1 block -> 9+1=10, not > 10 -> OK
    obd.read_block(9, 1)

    # Beyond boundary: block 9, 2 blocks -> 9+2=11 > 10 -> error
    with pytest.raises(OSError, match="Read beyond partition"):
        obd.read_block(9, 2)

    # Way beyond: block 15 > 10
    with pytest.raises(OSError, match="Read beyond partition"):
        obd.read_block(15)


def test_offset_block_device_write_boundary(amitools_mock):
    """Writing beyond num_blocks raises OSError."""
    from amifuse.rdb_inspect import OffsetBlockDevice

    base = _MockBlockDevice(block_bytes=512)
    obd = OffsetBlockDevice(base, offset_blocks=0, num_blocks=10)

    # Valid write
    obd.write_block(9, b"\x00" * 512, 1)
    assert len(base._write_log) == 1

    # Beyond boundary
    with pytest.raises(OSError, match="Write beyond partition"):
        obd.write_block(9, b"\x00" * 1024, 2)

    with pytest.raises(OSError, match="Write beyond partition"):
        obd.write_block(10, b"\x00" * 512, 1)
