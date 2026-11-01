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
