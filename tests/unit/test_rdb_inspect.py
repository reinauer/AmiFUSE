"""Unit tests for amifuse.rdb_inspect detection functions and OffsetBlockDevice.

All tests request the ``amitools_mock`` fixture because ``rdb_inspect.py`` has
top-level ``amitools`` imports that fail without it.  The tested functions
themselves are pure Python and do not call into amitools.
"""
import struct
import types

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


# ---------------------------------------------------------------------------
# E. list_partitions() -- RDB partition enumeration (5 tests)
#
# list_partitions() calls into open_rdisk / rdisk.parts / DosType, so these
# tests mock those at the module level (per the suite's "patch where names are
# looked up" convention) rather than driving real amitools block I/O. The
# amitools_mock fixture stubs DosType without num_to_tag_str, so each test
# patches in a faithful stand-in.
# ---------------------------------------------------------------------------

# FFS / DOS1 -- the dostype of every partition in the hd0-ericA3000.hdf fixture.
_FFS_DOS1 = 0x444F5301


def _fake_num_to_tag_str(dos_type):
    """Faithful stand-in for amitools DosType.num_to_tag_str.

    Mirrors the real algorithm: three ASCII bytes then the low byte rendered as
    a digit when < 32 (e.g. 0x444F5301 -> "DOS1").
    """
    tag = bytes(
        ((dos_type >> 24) & 0xFF, (dos_type >> 16) & 0xFF, (dos_type >> 8) & 0xFF)
    ).decode("latin-1")
    last = dos_type & 0xFF
    return tag + (chr(last + 48) if last < 32 else chr(last))


class _FakeDriveName:
    """Stands in for the amitools BSTR drv_name; str() yields the drive name."""

    def __init__(self, name):
        self._name = name

    def __str__(self):
        return self._name


class _FakePartition:
    """Minimal amitools Partition.

    Exposes the three fields list_partitions reads: ``num`` (per-RDB index),
    ``get_drive_name()`` (BSTR), and ``part_blk.dos_env.dos_type``.
    """

    def __init__(self, num, name, dos_type):
        self.num = num
        self._name = name
        self.part_blk = types.SimpleNamespace(
            dos_env=types.SimpleNamespace(dos_type=dos_type)
        )

    def get_drive_name(self):
        return _FakeDriveName(self._name)


class _FakeRDisk:
    """Records close() so tests can assert the rdisk handle is released."""

    def __init__(self, parts):
        self.parts = parts
        self.closed = False

    def close(self):
        self.closed = True


class _FakeBlkDev:
    """Records close() so tests can assert the block device is released."""

    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _patch_dostype(monkeypatch, rdb_inspect):
    monkeypatch.setattr(
        rdb_inspect.DosType, "num_to_tag_str", _fake_num_to_tag_str, raising=False
    )


def test_list_partitions_plain_rdb(amitools_mock, monkeypatch, tmp_path):
    """Plain RDB: enumerate all 3 fixture partitions, keyed by name, close both.

    Reproduces the hd0-ericA3000.hdf layout (WB_1.3, WB_2.x, Work -- all
    FFS/DOS1). No MBR -> the plain-RDB path opens once with no
    mbr_partition_index and closes both the rdisk and block device.
    """
    import amifuse.rdb_inspect as rdb_inspect

    _patch_dostype(monkeypatch, rdb_inspect)
    monkeypatch.setattr(rdb_inspect, "detect_mbr", lambda image: None)

    parts = [
        _FakePartition(0, "WB_1.3", _FFS_DOS1),
        _FakePartition(1, "WB_2.x", _FFS_DOS1),
        _FakePartition(2, "Work", _FFS_DOS1),
    ]
    rdisk = _FakeRDisk(parts)
    blkdev = _FakeBlkDev()
    calls = []

    def fake_open_rdisk(image, block_size=None, mbr_partition_index=None):
        calls.append((block_size, mbr_partition_index))
        return blkdev, rdisk, None

    monkeypatch.setattr(rdb_inspect, "open_rdisk", fake_open_rdisk)

    result = rdb_inspect.list_partitions(tmp_path / "hd0-ericA3000.hdf")

    assert result.fallback_reason is None
    assert [p.name for p in result.partitions] == ["WB_1.3", "WB_2.x", "Work"]
    assert [p.index for p in result.partitions] == [0, 1, 2]
    assert all(p.dostype == _FFS_DOS1 for p in result.partitions)
    assert all(p.dostype_str == "DOS1" for p in result.partitions)
    # Plain-RDB path: opened exactly once, with no MBR partition index.
    assert calls == [(None, None)]
    # Both handles released (RDisk.close() alone would leak the image handle).
    assert rdisk.closed is True
    assert blkdev.closed is True


def test_list_partitions_single_partition_rdb(amitools_mock, monkeypatch, tmp_path):
    """Single-partition RDB (N=1): one PartInfo, no fallback, both handles closed."""
    import amifuse.rdb_inspect as rdb_inspect

    _patch_dostype(monkeypatch, rdb_inspect)
    monkeypatch.setattr(rdb_inspect, "detect_mbr", lambda image: None)

    rdisk = _FakeRDisk([_FakePartition(0, "DH0", _FFS_DOS1)])
    blkdev = _FakeBlkDev()
    monkeypatch.setattr(
        rdb_inspect,
        "open_rdisk",
        lambda image, block_size=None, mbr_partition_index=None: (blkdev, rdisk, None),
    )

    result = rdb_inspect.list_partitions(tmp_path / "one.hdf")

    assert result.fallback_reason is None
    assert len(result.partitions) == 1
    assert result.partitions[0].name == "DH0"
    assert result.partitions[0].dostype_str == "DOS1"
    assert rdisk.closed is True
    assert blkdev.closed is True


def test_list_partitions_single_amiga_mbr_uses_plain_path(
    amitools_mock, monkeypatch, tmp_path
):
    """A single 0x76 MBR partition takes the plain-RDB path (no mbr index).

    With len(amiga_parts) <= 1 there is nothing to fan out across, so
    open_rdisk is called once with mbr_partition_index=None (it auto-selects the
    only 0x76 RDB).
    """
    import amifuse.rdb_inspect as rdb_inspect
    from amifuse.rdb_inspect import MBRInfo, MBRPartition, MBR_TYPE_AMIGA_RDB

    _patch_dostype(monkeypatch, rdb_inspect)
    mbr = MBRInfo(
        partitions=[
            MBRPartition(
                index=0,
                bootable=False,
                partition_type=MBR_TYPE_AMIGA_RDB,
                start_lba=1,
                num_sectors=1000,
            )
        ],
        has_amiga_partitions=True,
    )
    monkeypatch.setattr(rdb_inspect, "detect_mbr", lambda image: mbr)

    rdisk = _FakeRDisk([_FakePartition(0, "DH0", _FFS_DOS1)])
    blkdev = _FakeBlkDev()
    calls = []

    def fake_open_rdisk(image, block_size=None, mbr_partition_index=None):
        calls.append((block_size, mbr_partition_index))
        return blkdev, rdisk, None

    monkeypatch.setattr(rdb_inspect, "open_rdisk", fake_open_rdisk)

    result = rdb_inspect.list_partitions(tmp_path / "emu68.img")

    assert result.fallback_reason is None
    assert [p.name for p in result.partitions] == ["DH0"]
    assert calls == [(None, None)]  # plain path, no per-RDB index
    assert rdisk.closed is True
    assert blkdev.closed is True


def test_list_partitions_multi_rdb_distinct_names(
    amitools_mock, monkeypatch, tmp_path
):
    """Multi-RDB with distinct names: enumerate across ALL RDBs, no fallback.

    Two 0x76 RDBs (DH0/DH1 in the first, DH2 in the second) enumerate to three
    PartInfos keyed by name. Note ``.num`` collides across RDBs (0,1 then 0) --
    that is expected and harmless because the fan-out keys on name, not index.
    Every per-RDB handle is closed inside the loop.
    """
    import amifuse.rdb_inspect as rdb_inspect
    from amifuse.rdb_inspect import MBRInfo, MBRPartition, MBR_TYPE_AMIGA_RDB

    _patch_dostype(monkeypatch, rdb_inspect)
    mbr = MBRInfo(
        partitions=[
            MBRPartition(
                index=0,
                bootable=False,
                partition_type=MBR_TYPE_AMIGA_RDB,
                start_lba=1,
                num_sectors=1000,
            ),
            MBRPartition(
                index=1,
                bootable=False,
                partition_type=MBR_TYPE_AMIGA_RDB,
                start_lba=2000,
                num_sectors=1000,
            ),
        ],
        has_amiga_partitions=True,
    )
    monkeypatch.setattr(rdb_inspect, "detect_mbr", lambda image: mbr)

    rdb0 = _FakeRDisk(
        [_FakePartition(0, "DH0", _FFS_DOS1), _FakePartition(1, "DH1", _FFS_DOS1)]
    )
    rdb1 = _FakeRDisk([_FakePartition(0, "DH2", _FFS_DOS1)])
    blk0, blk1 = _FakeBlkDev(), _FakeBlkDev()
    rets = {0: (blk0, rdb0, None), 1: (blk1, rdb1, None)}

    def fake_open_rdisk(image, block_size=None, mbr_partition_index=None):
        return rets[mbr_partition_index]

    monkeypatch.setattr(rdb_inspect, "open_rdisk", fake_open_rdisk)

    result = rdb_inspect.list_partitions(tmp_path / "multi.hdf")

    assert result.fallback_reason is None
    assert [p.name for p in result.partitions] == ["DH0", "DH1", "DH2"]
    assert [p.index for p in result.partitions] == [0, 1, 0]  # .num collides; name keys
    assert rdb0.closed and blk0.closed
    assert rdb1.closed and blk1.closed


def test_list_partitions_duplicate_name_fallback(amitools_mock, monkeypatch, tmp_path):
    """Name colliding across RDBs -> first-partition-only fallback + reason.

    Two 0x76 RDBs both hold a ``DH0``. Because ``--partition <name>`` cannot be
    routed unambiguously, list_partitions falls back to index 0 of the FIRST RDB
    and surfaces a reason string for the caller's summary dialog. Every opened
    handle (including the RDB that triggered the fallback) is closed.
    """
    import amifuse.rdb_inspect as rdb_inspect
    from amifuse.rdb_inspect import MBRInfo, MBRPartition, MBR_TYPE_AMIGA_RDB

    _patch_dostype(monkeypatch, rdb_inspect)
    mbr = MBRInfo(
        partitions=[
            MBRPartition(
                index=0,
                bootable=False,
                partition_type=MBR_TYPE_AMIGA_RDB,
                start_lba=1,
                num_sectors=1000,
            ),
            MBRPartition(
                index=1,
                bootable=False,
                partition_type=MBR_TYPE_AMIGA_RDB,
                start_lba=2000,
                num_sectors=1000,
            ),
        ],
        has_amiga_partitions=True,
    )
    monkeypatch.setattr(rdb_inspect, "detect_mbr", lambda image: mbr)

    rdb0 = _FakeRDisk(
        [_FakePartition(0, "DH0", _FFS_DOS1), _FakePartition(1, "DH1", _FFS_DOS1)]
    )
    rdb1 = _FakeRDisk([_FakePartition(0, "DH0", _FFS_DOS1)])  # collides with rdb0
    blk0, blk1 = _FakeBlkDev(), _FakeBlkDev()
    rets = {0: (blk0, rdb0, None), 1: (blk1, rdb1, None)}

    def fake_open_rdisk(image, block_size=None, mbr_partition_index=None):
        return rets[mbr_partition_index]

    monkeypatch.setattr(rdb_inspect, "open_rdisk", fake_open_rdisk)

    result = rdb_inspect.list_partitions(tmp_path / "collide.hdf")

    assert result.fallback_reason is not None
    assert "colliding partition names" in result.fallback_reason
    assert "mounted first only" in result.fallback_reason
    # Fell back to index 0 of the FIRST RDB, exactly one unit.
    assert len(result.partitions) == 1
    assert result.partitions[0].name == "DH0"
    assert result.partitions[0].index == 0
    # No leaked handles, including the collision-triggering RDB.
    assert rdb0.closed and blk0.closed
    assert rdb1.closed and blk1.closed
