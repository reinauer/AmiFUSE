"""--partition on a disk with several 0x76 partitions: a name before a number.

amitools' find_partition_by_string falls back to a partition number when
no partition of an RDB has the name, so asking each RDB in turn let an
earlier RDB's partition number 1 win over a later RDB's partition named 1.
"""

from contextlib import ExitStack
import json
import struct
import types

import pytest

from amitools.fs.DosType import DOS3, DOS7
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.block.rdb.PartitionBlock import PartitionBlock
from amitools.fs.block.rdb.RDBlock import RDBlock
from amitools.fs.rdb.RDisk import RDisk

from amifuse import rdb_inspect
from amifuse.fuse_fs import get_partition_info, main
from amifuse.rdb_inspect import (MBRContext, MBRInfo, MBRPartition,
                                 find_partition_mbr_index)
from tests.unit.test_mbr_rdb_block_size import _make_mbr, _make_parceiro

# The first RDB's partition number 1 is DH1.
FIRST = [("DH0", DOS3), ("DH1", DOS7)]


def _make_rdb_parts(tmp_path, block_bytes, parts):
    """An RDB holding (drive name, DOS type) partitions, in that order."""
    names = "-".join(name for name, _ in parts)
    rdb_image = tmp_path / f"rdb-{block_bytes}-{names}.hdf"
    raw = RawBlockDevice(str(rdb_image), read_only=False, block_bytes=block_bytes)
    cyl_ranges = {1: [(1, 9)], 2: [(1, 4), (5, 9)]}[len(parts)]
    with ExitStack() as cleanup:
        raw.create(160)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(10, 1, 16), rdb_cyls=1)
        for (name, dos_type), cyls in zip(parts, cyl_ranges):
            rdisk.add_partition(FSString(name), cyls, dos_type=dos_type)
        raw.flush()
    return rdb_image.read_bytes()


def _image(tmp_path, second, second_bytes=512, first=FIRST):
    return _make_mbr(tmp_path, [
        (64, _make_rdb_parts(tmp_path, 512, first)),
        (448, _make_rdb_parts(tmp_path, second_bytes, second))])


def _damage_first_name(tmp_path, rdb):
    """The 512-byte-block RDB with its first partition's drive name 40
    bytes long, past the 31 a partition block holds, and the checksum
    redone. amitools then reads the name as None."""
    damaged = tmp_path / "damaged.hdf"
    damaged.write_bytes(rdb)
    raw = RawBlockDevice(str(damaged), read_only=False, block_bytes=512)
    raw.open()
    try:
        rdb_blk = RDBlock(raw, 0)
        assert rdb_blk.read()
        part_blk = PartitionBlock(raw, rdb_blk.part_list)
        assert part_blk.read()
        part_blk.data[36] = 40  # drive name length, long 9
        part_blk._put_chksum()
        part_blk._write_data()
        raw.flush()
    finally:
        raw.close()
    return damaged.read_bytes()


def test_name_in_later_rdb_wins_over_number_in_earlier(tmp_path):
    image = _image(tmp_path, [("1", DOS3)])
    assert find_partition_mbr_index(image, None, "1") == 1
    # mount --partition 1 opens the partition named 1, not DH1.
    assert get_partition_info(image, None, "1") == {"name": "1",
                                                    "dostype": DOS3}


def test_name_in_the_rdb_beside_the_mbr_is_found_first(tmp_path):
    # A Parceiro disk's own RDB, beside the MBR, holds DH0, and so does the
    # RDB of its second 0x76 entry. None selects the disk's own RDB, which
    # the default open returns.
    image = _make_parceiro(tmp_path, _make_rdb_parts(tmp_path, 512, [("DH0", DOS3)]))
    data = bytearray(image.read_bytes())
    for slot, name in ((1, "PI0"), (2, "DH0")):
        rdb = _make_rdb_parts(tmp_path, 512, [(name, DOS3)])
        entry = 446 + 16 * slot
        data[entry + 4] = 0x76
        struct.pack_into("<II", data, entry + 8, len(data) // 512, len(rdb) // 512)
        data += rdb
    image.write_bytes(bytes(data))
    assert find_partition_mbr_index(image, None, "DH0") is None


class _FakeRDisk:
    """The parts of an RDisk the lookup reads: drive names, flags and
    numbers, and its RDSK block."""

    def __init__(self, names):
        self.parts = [types.SimpleNamespace(get_drive_name=lambda n=n: FSString(n),
                                            get_flags=lambda: 0)
                      for n in names]
        self.rdb = types.SimpleNamespace(blk_num=1)

    def get_partition(self, num):
        return self.parts[num] if 0 <= num < len(self.parts) else None

    def close(self):
        pass


@pytest.mark.parametrize("beside_mbr,expected", [
    # The default open gives the disk's own RDB (DH0); index 0 gives the
    # entry's own RDB (PI0), as it does once an entry index opens it.
    (True, [None, 0, None]),
    # No RDB beside the MBR: the default open gives the entry's RDB, the
    # disk's one RDB, and the lookup leaves it to that open.
    (False, [None, None, None]),
])
def test_single_entry_beside_the_disk_rdb_is_searched(monkeypatch,
                                                      beside_mbr, expected):
    entry = MBRPartition(index=1, bootable=False, partition_type=0x76,
                         start_lba=200, num_sectors=160)
    mbr = MBRInfo(partitions=[entry], has_amiga_partitions=True)

    def fake_open_rdisk(image, block_size=None, mbr_partition_index=None):
        if mbr_partition_index is None and beside_mbr:
            names, ctx = ["DH0"], MBRContext(mbr, None, 0, "parceiro")
        else:
            names, ctx = ["PI0"], MBRContext(mbr, entry, 200)
        blkdev = types.SimpleNamespace(close=lambda: None, block_bytes=512)
        return blkdev, _FakeRDisk(names), ctx

    monkeypatch.setattr(rdb_inspect, "detect_mbr", lambda image: mbr)
    monkeypatch.setattr(rdb_inspect, "open_rdisk", fake_open_rdisk)
    assert [find_partition_mbr_index("disk.hdf", None, name)
            for name in ("DH0", "PI0", "XX")] == expected


def test_inspect_json_gives_the_named_partition_a_driver(
        tmp_path, capsys, user_driver_dir):
    # inspect --json said another RDB had a partition named 1.
    image = _image(tmp_path, [("1", DOS3)])
    main(["inspect", "--json", str(image)])
    rdbs = json.loads(capsys.readouterr().out)["rdbs"]
    (named,) = rdbs[1]["partitions"]
    assert named["name"] == "1"
    assert named["driver"] is not None
    assert "driver_note" not in named


@pytest.mark.parametrize("second,number,index,name", [
    # The first RDB with a partition of that number holds it.
    ([("DH2", DOS3)], "1", 0, "DH1"),
    ([("DH2", DOS3)], "0", 0, "DH0"),
    # Numbers restart in each RDB, so neither RDB has a partition 2.
    ([("DH2", DOS3), ("DH3", DOS3)], "2", None, "AmigaFS"),
])
def test_number_selects_a_partition_when_no_rdb_has_the_name(
        tmp_path, second, number, index, name):
    image = _image(tmp_path, second)
    assert find_partition_mbr_index(image, None, number) == index
    assert get_partition_info(image, None, number)["name"] == name


def test_number_in_later_rdb_when_earlier_lacks_it(tmp_path):
    # The first RDB holds only DH0, so partition number 1 is the second
    # RDB's second partition.
    image = _image(tmp_path, [("DH2", DOS3), ("DH3", DOS7)],
                   first=[("DH0", DOS3)])
    assert find_partition_mbr_index(image, None, "1") == 1
    assert get_partition_info(image, None, "1") == {"name": "DH3",
                                                    "dostype": DOS7}


@pytest.mark.parametrize("selector,index", [("1", 0), ("DH9", None)])
def test_damaged_name_in_later_rdb_is_not_a_match(tmp_path, selector,
                                                  index):
    # Looking for the name in every RDB reaches the second RDB, whose
    # damaged drive name must not end a lookup that finds DH1 by number.
    damaged = _damage_first_name(
        tmp_path, _make_rdb_parts(tmp_path, 512, [("DH2", DOS3)]))
    image = _make_mbr(tmp_path, [(64, _make_rdb_parts(tmp_path, 512, FIRST)),
                                 (448, damaged)])
    assert find_partition_mbr_index(image, None, selector) == index


def test_forced_size_skipping_an_rdb_without_the_name_finds_it(tmp_path):
    # Forced to 1024 bytes, the first RDB does not open. Its partition
    # number 1 is no reason to refuse: the name is in the second RDB.
    image = _image(tmp_path, [("1", DOS3)], second_bytes=1024)
    assert find_partition_mbr_index(image, 1024, "1") == 1


@pytest.mark.parametrize("second,first", [
    # The second RDB holds the name; the first RDB's partition number 1
    # must not stand in for it.
    ([("1", DOS3)], FIRST),
    # No RDB has the name, and only the second has a partition number 1.
    ([("DH2", DOS3), ("DH3", DOS7)], [("DH0", DOS3)]),
])
def test_forced_size_skipping_the_rdb_with_the_partition_is_refused(
        tmp_path, second, first):
    # Forced to 512 bytes, the second RDB does not open.
    image = _image(tmp_path, second, second_bytes=1024, first=first)
    with pytest.raises(OSError) as caught:
        find_partition_mbr_index(image, 512, "1")
    assert str(caught.value) == (
        "Partition 1 is in an RDB that does not open with 512-byte blocks")
