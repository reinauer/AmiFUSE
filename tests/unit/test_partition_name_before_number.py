"""--partition on a disk with several 0x76 partitions: a name before a number.

amitools' find_partition_by_string falls back to a partition number when
no partition of an RDB has the name, so asking each RDB in turn let an
earlier RDB's partition number 1 win over a later RDB's partition named 1.
"""

from contextlib import ExitStack

import pytest

from amitools.fs.DosType import DOS3, DOS7
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.fuse_fs import get_partition_info
from amifuse.rdb_inspect import find_partition_mbr_index
from tests.unit.test_mbr_rdb_block_size import _make_mbr


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


def _image(tmp_path, second, second_bytes=512, first_bytes=512):
    # The first RDB holds DH0 and DH1, so its partition number 1 is DH1.
    first = _make_rdb_parts(tmp_path, first_bytes, [("DH0", DOS3), ("DH1", DOS7)])
    return _make_mbr(tmp_path, [(64, first),
                                (448, _make_rdb_parts(tmp_path, second_bytes, second))])


def test_name_in_later_rdb_wins_over_number_in_earlier(tmp_path):
    image = _image(tmp_path, [("1", DOS3)])
    assert find_partition_mbr_index(image, None, "1") == 1
    # mount --partition 1 opens the partition named 1, not DH1.
    assert get_partition_info(image, None, "1") == {"name": "1",
                                                    "dostype": DOS3}


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
    first = _make_rdb_parts(tmp_path, 512, [("DH0", DOS3)])
    second = _make_rdb_parts(tmp_path, 512, [("DH2", DOS3), ("DH3", DOS7)])
    image = _make_mbr(tmp_path, [(64, first), (448, second)])
    assert find_partition_mbr_index(image, None, "1") == 1
    assert get_partition_info(image, None, "1") == {"name": "DH3",
                                                    "dostype": DOS7}


def test_forced_size_skipping_an_rdb_without_the_name_finds_it(tmp_path):
    # Forced to 1024 bytes, the first RDB does not open. Its partition
    # number 1 is no reason to refuse: the name is in the second RDB.
    image = _image(tmp_path, [("1", DOS3)], second_bytes=1024)
    assert find_partition_mbr_index(image, 1024, "1") == 1


def test_forced_size_skipping_the_rdb_with_the_name_is_refused(tmp_path):
    # Forced to 512 bytes, the second RDB, which holds the name, does not
    # open; the first RDB's partition number 1 must not stand in for it.
    image = _image(tmp_path, [("1", DOS3)], second_bytes=1024)
    with pytest.raises(OSError) as caught:
        find_partition_mbr_index(image, 512, "1")
    assert str(caught.value) == (
        "Partition 1 is in an RDB that does not open with 512-byte blocks")
