"""A partition of a Parceiro disk's own RDB, beside the MBR, is checked
against the MBR entries, as one in an RDB inside a 0x76 entry is.

An entry starting at the partition's first sector is that entry's Amiga
view, as the FAT\\1 partition over the FAT32 entry of a real Parceiro card
is, and may end before the partition does. A 0x76 entry starts with its own
RDB, and an extended entry with the table of its own partitions, so neither
has a view.
"""

import struct
from contextlib import ExitStack

import pytest
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.block.Block import Block
from amitools.fs.block.rdb.RDBlock import RDBlock
from amitools.fs.rdb.RDisk import RDisk

from amifuse.driver_runtime import BlockDeviceBackend

DOS0 = 0x444F5300
FAT1 = 0x46415401  # FAT\1
OVERLAP = "Overlapping MBR containers cannot be shared"


def _rdb(tmp_path, partitions, block_bytes=512):
    """A 160-block RDB, 16 blocks to a cylinder, with (name, cylinders,
    dos_type) partitions. Its RDSK block is at block 0, its PART blocks
    follow it, and blocks 0-15 are reserved for it."""
    image = tmp_path / "rdb.hdf"
    raw = RawBlockDevice(str(image), read_only=False, block_bytes=block_bytes)
    with ExitStack() as cleanup:
        raw.create(160)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(10, 1, 16), rdb_cyls=1)
        for name, cyls, dos_type in partitions:
            rdisk.add_partition(FSString(name), cyls, dos_type=dos_type)
        raw.flush()
    return image.read_bytes()


def _entry(data, index, ptype, lba, sectors):
    data[446 + 16 * index + 4] = ptype
    struct.pack_into("<II", data, 446 + 16 * index + 8, lba, sectors)


def _parceiro(tmp_path, partitions, entries, block_bytes=512):
    """The RDB as a Parceiro disk's own, its RDSK block moved to the free
    block 3, beside an MBR listing (type, start, sectors) entries."""
    data = bytearray(_rdb(tmp_path, partitions, block_bytes))
    data[3 * block_bytes:4 * block_bytes] = data[:block_bytes]
    data[:block_bytes] = bytes(block_bytes)
    for index, entry in enumerate(entries):
        _entry(data, index, *entry)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "parceiro.hdf"
    image.write_bytes(bytes(data))
    return image


def _open(image, name):
    """Open partition name for writing, then close it."""
    backend = BlockDeviceBackend(image, read_only=False, partition_scope=True,
                                 partition=name)
    backend.open()
    try:
        assert backend.mbr_context is not None
        return backend._block_range
    finally:
        backend.close()


def test_parceiro_partition_over_an_entry_is_refused(tmp_path):
    # DH0 on blocks 16-159, a FAT32 entry on sectors 64-127 inside it.
    image = _parceiro(tmp_path, [("DH0", (1, 9), DOS0)], [(0x0C, 64, 64)])
    with pytest.raises(ValueError, match=OVERLAP):
        _open(image, "DH0")


def test_partition_in_a_0x76_entry_over_another_entry_is_refused(tmp_path):
    # The same RDB inside a 0x76 entry at sector 256, a FAT32 entry on
    # sectors 320-383 inside it.
    data = bytearray(256 * 512) + bytearray(_rdb(tmp_path, [("DH0", (1, 9), DOS0)]))
    _entry(data, 0, 0x76, 256, 160)
    _entry(data, 1, 0x0C, 320, 64)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "emu68.hdf"
    image.write_bytes(bytes(data))
    with pytest.raises(ValueError, match=OVERLAP):
        _open(image, "DH0")


def test_fat_view_running_past_its_entry_into_free_space_opens(tmp_path):
    # FAT on blocks 80-159 starts at the FAT32 entry on sectors 80-143 and
    # runs past its end, as SD3 on a real Parceiro card does.
    image = _parceiro(tmp_path, [("DH0", (1, 4), DOS0), ("FAT", (5, 9), FAT1)],
                      [(0x0C, 80, 64)])
    assert _open(image, "FAT") == (80, 160)
    assert _open(image, "DH0") == (16, 80)


def test_fat_view_running_into_a_second_entry_is_refused(tmp_path):
    image = _parceiro(tmp_path, [("DH0", (1, 4), DOS0), ("FAT", (5, 9), FAT1)],
                      [(0x0C, 80, 64), (0x83, 144, 16)])
    with pytest.raises(ValueError, match=OVERLAP):
        _open(image, "FAT")
    assert _open(image, "DH0") == (16, 80)


def test_partition_starting_at_a_0x76_entry_is_refused(tmp_path):
    # A 0x76 entry's first sector holds its own RDB, so a partition starting
    # there is not a view of it.
    image = _parceiro(tmp_path, [("DH0", (1, 4), DOS0), ("DH1", (5, 9), DOS0)],
                      [(0x76, 80, 80)])
    with pytest.raises(ValueError, match=OVERLAP):
        _open(image, "DH1")
    assert _open(image, "DH0") == (16, 80)


@pytest.mark.parametrize("ptype", [0x05, 0x0F, 0x85],
                         ids=lambda t: f"0x{t:02X}")
def test_partition_starting_at_an_extended_entry_is_refused(tmp_path, ptype):
    # An extended entry's first sector holds the table of its own
    # partitions, so a partition starting there is not a view of it.
    image = _parceiro(tmp_path, [("DH0", (1, 4), DOS0), ("DH1", (5, 9), DOS0)],
                      [(ptype, 80, 80)])
    with pytest.raises(ValueError, match=OVERLAP):
        _open(image, "DH1")
    assert _open(image, "DH0") == (16, 80)


def test_rdb_blocks_are_compared_as_512_byte_sectors(tmp_path):
    # 1024-byte blocks: DH0 on sectors 32-159 and FAT on sectors 160-319.
    parts = [("DH0", (1, 4), DOS0), ("FAT", (5, 9), FAT1)]
    (tmp_path / "view").mkdir()
    image = _parceiro(tmp_path / "view", parts, [(0x0C, 160, 128)], 1024)
    assert _open(image, "FAT") == (80, 160)
    assert _open(image, "DH0") == (16, 80)
    # An entry on sectors 200-231 lies inside FAT, though not on its blocks.
    (tmp_path / "inside").mkdir()
    image = _parceiro(tmp_path / "inside", parts, [(0x0C, 200, 32)], 1024)
    with pytest.raises(ValueError, match=OVERLAP):
        _open(image, "FAT")
    assert _open(image, "DH0") == (16, 80)


# The checks below read the disk through the device the open already holds:
# a second open of a Windows physical disk fails while it is held.

def _open_as(image, read_only=False, **kwargs):
    """Open a partition, then close it, returning its 512-byte sector
    range on the disk."""
    backend = BlockDeviceBackend(image, read_only=read_only,
                                 partition_scope=True, **kwargs)
    backend.open()
    try:
        start, end = backend._block_range
        offset = getattr(backend.blkdev, "offset", 0)
        scale = backend.block_size // 512
        return (offset + start) * scale, (offset + end) * scale
    finally:
        backend.close()


def _no_second_open(monkeypatch):
    """Fail any open of the image while _lock_partition runs."""
    from amitools.fs.blkdev.ImageFile import ImageFile
    from amifuse import rdb_inspect

    def refuse(*args, **kwargs):
        raise AssertionError("the image was opened a second time")

    lock_partition = BlockDeviceBackend._lock_partition

    def guarded(self, *args, **kwargs):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(ImageFile, "open", refuse)
            for name in ("open_image", "open_rdisk", "detect_mbr"):
                patch.setattr(rdb_inspect, name, refuse)
            return lock_partition(self, *args, **kwargs)

    monkeypatch.setattr(BlockDeviceBackend, "_lock_partition", guarded)


def _rdb0_mbr(tmp_path, partitions, entries):
    """An RDB with its RDSK block at block 0, whose sector also holds an MBR
    listing (type, start, sectors) entries. The RDSK checksum holds over
    the whole block, MBR table included, and over its first SummedLongs
    (64) longs, whichever amitools checks: long 100, in the MBR boot code
    area, balances the upper half."""
    data = bytearray(_rdb(tmp_path, partitions))
    for index, entry in enumerate(entries):
        _entry(data, index, *entry)
    data[510:512] = b"\x55\xaa"
    assert struct.unpack_from(">I", data, 4)[0] == 64
    struct.pack_into(">I", data, 400, 0)
    struct.pack_into(">I", data, 400,
                     -sum(struct.unpack(">64I", data[256:512])) & 0xFFFFFFFF)
    struct.pack_into(">I", data, 8, 0)
    struct.pack_into(">I", data, 8,
                     -sum(struct.unpack(">128I", data[:512])) & 0xFFFFFFFF)
    image = tmp_path / "rdb0-mbr.hdf"
    image.write_bytes(bytes(data))
    return image


def test_rdb_at_block_0_partition_over_an_mbr_entry_is_refused(tmp_path, monkeypatch):
    # DH1 on blocks 80-159, a FAT32 entry on sectors 96-111 inside it.
    image = _rdb0_mbr(tmp_path, [("DH0", (1, 4), DOS0), ("DH1", (5, 9), DOS0)],
                      [(0x0C, 96, 16)])
    before = image.read_bytes()
    _no_second_open(monkeypatch)
    with pytest.raises(ValueError, match=OVERLAP):
        _open_as(image, partition="DH1")
    assert _open_as(image, partition="DH0") == (16, 80)
    assert image.read_bytes() == before


def test_rdb_at_block_0_with_an_unreadable_sector_0(tmp_path, monkeypatch):
    # A writable open does not go on without the MBR check; a read-only
    # open does.
    image = _rdb0_mbr(tmp_path, [("DH0", (1, 9), DOS0)], [(0x0C, 64, 64)])
    open_image = BlockDeviceBackend._open_image

    def failing_reads(self):
        open_image(self)

        def read_block(*args, **kwargs):
            raise OSError("media error")

        self.blkdev.read_block = read_block

    monkeypatch.setattr(BlockDeviceBackend, "_open_image", failing_reads)
    with pytest.raises(ValueError, match="sector 0 .* media error"):
        _open_as(image, partition="DH0")
    assert _open_as(image, read_only=True, partition="DH0") == (16, 160)


def test_plain_rdb_at_block_0_opens(tmp_path):
    image = tmp_path / "plain.hdf"
    image.write_bytes(_rdb(tmp_path, [("DH0", (1, 9), DOS0)]))
    assert _open_as(image, partition="DH0") == (16, 160)


def _rdb_image(path, blocks, name, cyls, block_bytes=512):
    """The bytes of a blocks-long RDB, 16 blocks to a cylinder, holding one
    partition on cylinders cyls."""
    raw = RawBlockDevice(str(path), read_only=False, block_bytes=block_bytes)
    with ExitStack() as cleanup:
        raw.create(blocks)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(blocks // 16, 1, 16), rdb_cyls=1)
        rdisk.add_partition(FSString(name), cyls)
        raw.flush()
    return bytearray(path.read_bytes())


def _parceiro_76(tmp_path, own_cyls, own_sector=2, own_bytes=512,
                 inner_bytes=512):
    """A 640-sector Parceiro disk: its own RDB, with its RDSK block moved to
    own_sector and DH0 on own_cyls, beside a 0x76 entry on sectors 256-415
    holding an RDB with DH1 on its cylinders 1-4 (1024-byte blocks) or 1-9."""
    own_blocks = 640 * 512 // own_bytes
    data = _rdb_image(tmp_path / "own.hdf", own_blocks, "DH0", own_cyls,
                      own_bytes)
    rdsk = bytes(data[:own_bytes])
    data[:own_bytes] = bytes(own_bytes)
    data[own_sector * 512:own_sector * 512 + own_bytes] = rdsk
    inner_blocks = 160 * 512 // inner_bytes
    inner_cyls = (1, inner_blocks // 16 - 1)
    data[256 * 512:416 * 512] = _rdb_image(
        tmp_path / "inner.hdf", inner_blocks, "DH1", inner_cyls, inner_bytes)
    _entry(data, 0, 0x76, 256, 160)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "parceiro76.hdf"
    image.write_bytes(bytes(data))
    return image


def test_0x76_entry_inside_an_own_rdb_partition_is_refused(tmp_path, monkeypatch):
    # DH0 on sectors 16-639 covers the 0x76 entry holding DH1.
    image = _parceiro_76(tmp_path, (1, 39))
    before = image.read_bytes()
    _no_second_open(monkeypatch)
    with pytest.raises(ValueError, match=OVERLAP):
        _open_as(image, partition="DH1")
    with pytest.raises(ValueError, match=OVERLAP):
        _open_as(image, partition="DH0")
    assert image.read_bytes() == before


def test_0x76_entry_beside_the_own_rdb_partitions_opens(tmp_path, monkeypatch):
    # DH0 on sectors 16-255 ends where the 0x76 entry starts.
    image = _parceiro_76(tmp_path, (1, 15))
    _no_second_open(monkeypatch)
    assert _open_as(image, partition="DH1") == (272, 416)
    assert _open_as(image, partition="DH0") == (16, 256)


def test_0x76_entry_on_a_disk_without_its_own_rdb_opens(tmp_path):
    data = bytearray(256 * 512) + _rdb_image(tmp_path / "inner.hdf", 160,
                                             "DH1", (1, 9))
    _entry(data, 0, 0x76, 256, 160)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "emu68.hdf"
    image.write_bytes(bytes(data))
    assert _open_as(image, partition="DH1") == (272, 416)


def test_rdsk_block_with_a_size_open_rdisk_skips_is_no_own_rdb(tmp_path):
    # Sector 2 holds an RDSK block with a valid checksum declaring 520-byte
    # blocks, which open_rdisk skips as if it held no RDSK id.
    data = bytearray(256 * 512) + _rdb_image(tmp_path / "inner.hdf", 160,
                                             "DH1", (1, 9))
    rdsk = bytearray(data[256 * 512:257 * 512])
    struct.pack_into(">I", rdsk, 16, 520)
    struct.pack_into(">I", rdsk, 8, 0)
    struct.pack_into(">I", rdsk, 8,
                     -sum(struct.unpack(">128I", rdsk)) & 0xFFFFFFFF)
    data[2 * 512:3 * 512] = rdsk
    _entry(data, 0, 0x76, 256, 160)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "emu68.hdf"
    image.write_bytes(bytes(data))
    assert _open_as(image, partition="DH1") == (272, 416)


def test_0x76_entry_rdsk_block_in_sectors_0_to_15_is_not_the_disks_own(tmp_path):
    # The entry starts at sector 8, so its RDSK block is the first one in
    # sectors 0-15. It belongs to the entry, and the disk has no own RDB.
    data = bytearray(8 * 512) + _rdb_image(tmp_path / "inner.hdf", 160,
                                           "DH1", (1, 9))
    _entry(data, 0, 0x76, 8, 160)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "emu68.hdf"
    image.write_bytes(bytes(data))
    assert _open_as(image, partition="DH1") == (24, 168)


def test_0x76_entry_with_a_failing_read_of_the_disk(tmp_path, monkeypatch):
    # A writable open does not go on without the own-RDB check; a
    # read-only open does.
    image = _parceiro_76(tmp_path, (1, 15))
    open_image = BlockDeviceBackend._open_image

    def failing_reads(self):
        open_image(self)

        def read_block(*args, **kwargs):
            raise OSError("media error")

        self.blkdev.base.read_block = read_block

    monkeypatch.setattr(BlockDeviceBackend, "_open_image", failing_reads)
    with pytest.raises(ValueError, match="own RDB can't be checked.*media error"):
        _open_as(image, partition="DH1")
    assert _open_as(image, read_only=True, partition="DH1") == (272, 416)


def _select_entry_dh1(monkeypatch):
    """Have --partition DH1 select entry 0's partition 0 without looking at
    the disk's own RDB, so that only _lock_partition judges that RDB: a
    selector may refuse to write beside an RDB that does not open."""
    from amifuse import rdb_inspect

    def find_partition(*args, **kwargs):
        return rdb_inspect.PartitionRef(mbr_partition_index=0, number=0,
                                        name="DH1")

    monkeypatch.setattr(rdb_inspect, "find_partition", find_partition)


def test_0x76_entry_beside_an_invalid_own_rdsk_block(tmp_path, monkeypatch):
    # The own RDSK block's checksum is broken: its partitions are unknown.
    image = _parceiro_76(tmp_path, (1, 39))
    data = bytearray(image.read_bytes())
    data[2 * 512 + 8] ^= 0xFF
    image.write_bytes(bytes(data))
    _select_entry_dh1(monkeypatch)
    with pytest.raises(ValueError, match="own RDB can't be checked.*sectors \\[2\\]"):
        _open_as(image, partition="DH1")
    assert _open_as(image, read_only=True, partition="DH1") == (272, 416)


def test_own_rdsk_block_is_valid_as_amitools_checks_it(tmp_path, monkeypatch):
    # The own RDSK block holds data past its first SummedLongs longs, and
    # amitools sets its checksum, over the whole block or over those longs
    # only: the block is as valid to the own-RDB check as to amitools.
    image = _parceiro_76(tmp_path, (1, 39))
    raw = RawBlockDevice(str(image), read_only=False)
    raw.open()
    try:
        rdsk = RDBlock(raw, 2)
        assert rdsk.read()
        rdsk._put_long(100, 0x12345678)
        Block.write(rdsk)
    finally:
        raw.close()
    _select_entry_dh1(monkeypatch)
    _no_second_open(monkeypatch)
    with pytest.raises(ValueError, match=OVERLAP):
        _open_as(image, partition="DH1")


def test_invalid_own_rdsk_block_before_a_0x76_entry_rdsk_block(tmp_path, monkeypatch):
    # The entry starts at sector 8, and sector 3 holds an RDSK block with a
    # broken checksum: the first valid RDSK block is the entry's, but the
    # disk's own RDB is still unknown.
    data = bytearray(8 * 512) + _rdb_image(tmp_path / "inner.hdf", 160,
                                           "DH1", (1, 9))
    data[3 * 512:4 * 512] = data[8 * 512:9 * 512]
    data[3 * 512 + 8] ^= 0xFF
    _entry(data, 0, 0x76, 8, 160)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "emu68.hdf"
    image.write_bytes(bytes(data))
    _select_entry_dh1(monkeypatch)
    with pytest.raises(ValueError, match="own RDB can't be checked.*sectors \\[3\\]"):
        _open_as(image, partition="DH1")
    assert _open_as(image, read_only=True, partition="DH1") == (24, 168)


def test_own_rdsk_block_is_found_in_512_byte_sectors(tmp_path, monkeypatch):
    # The entry's RDB has 1024-byte blocks, so the device is open with them,
    # and the own RDSK block (512-byte blocks) sits at the odd sector 3,
    # which no 1024-byte block starts with. Its partitions can't be read
    # through that device.
    image = _parceiro_76(tmp_path, (1, 39), own_sector=3, inner_bytes=1024)
    _no_second_open(monkeypatch)
    with pytest.raises(ValueError,
                       match="sector 3 uses 512-byte blocks.*1024-byte"):
        _open_as(image, partition="DH1")
    assert _open_as(image, read_only=True, partition="DH1") == (288, 416)


def test_own_rdb_with_1024_byte_blocks_is_checked(tmp_path, monkeypatch):
    # Own RDB and entry RDB both have 1024-byte blocks; the own RDSK block
    # is block 3 (sector 6), and DH0 covers sectors 32-639.
    image = _parceiro_76(tmp_path, (1, 19), own_sector=6, own_bytes=1024,
                         inner_bytes=1024)
    _no_second_open(monkeypatch)
    with pytest.raises(ValueError, match=OVERLAP):
        _open_as(image, partition="DH1")
    # DH0 on sectors 32-255 only.
    (tmp_path / "beside").mkdir()
    image = _parceiro_76(tmp_path / "beside", (1, 7), own_sector=6,
                         own_bytes=1024, inner_bytes=1024)
    assert _open_as(image, partition="DH1") == (288, 416)
    assert _open_as(image, partition="DH0") == (32, 256)
