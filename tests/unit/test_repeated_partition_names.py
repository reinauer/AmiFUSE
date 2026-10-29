"""A repeated drive name mounts as AmigaOS names it: DH0, DH0.1, DH0.2.

amitools accepts two partitions of one name in an RDB (#162). Each later
copy is named as the A4091 mounter's CheckAndFixDevName names it, ignoring
case, so that every name selects one partition.
"""

import json
from contextlib import ExitStack

import pytest

from amitools.fs import DosType
from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.block.rdb.PartitionBlock import PartitionBlock
from amitools.fs.rdb.RDisk import RDisk

from amifuse.bootstrap import BootstrapAllocator
from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.fuse_fs import get_partition_info, main
from amifuse.rdb_inspect import (PartitionRef, _unique_name, find_partition,
                                 list_partitions)
from tests.unit.test_bootstrap_block_size import _bootstrap_open
from tests.unit.test_mbr_rdb_block_size import _make_mbr

DOS3 = DosType.DOS3
DOS7 = DosType.DOS7


def _image(tmp_path, parts, block_bytes=512, name="repeated.hdf"):
    """One RDB of 10 cylinders of 16 blocks, one partition of (name, DOS
    type[, flags]) per two cylinders from cylinder 1."""
    image = tmp_path / name
    raw = RawBlockDevice(str(image), read_only=False, block_bytes=block_bytes)
    with ExitStack() as cleanup:
        raw.create(160)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(10, 1, 16), rdb_cyls=1)
        for i, (drive, dos_type, *flags) in enumerate(parts):
            assert rdisk.add_partition(FSString(drive), (1 + 2 * i, 2 + 2 * i),
                                       flags=flags[0] if flags else 0,
                                       dos_type=dos_type)
        raw.flush()
    return image


@pytest.mark.parametrize("name,taken,expected", [
    ("DH0", set(), "DH0"),
    ("DH0", {"dh0"}, "DH0.1"),
    ("dh0", {"dh0", "dh0.1"}, "dh0.2"),
    # A name that already ends in a dot and a digit has the digit raised.
    ("DH0.1", {"dh0.1"}, "DH0.2"),
    # 9 is not raised further.
    ("DH0.9", {"dh0.9"}, "DH0.9.1"),
    # Two characters are too short to end in a dot and a digit.
    (".1", {".1"}, ".1.1"),
])
def test_unique_name(name, taken, expected):
    assert _unique_name(name, taken) == expected


def test_repeated_names_in_one_rdb_are_renamed(tmp_path):
    from amifuse.launcher import _enumerate_mount_units

    image = _image(tmp_path, [("DH0", DOS3), ("dh0", DOS7), ("DH0", DOS3)])
    listed = list_partitions(image)
    assert [(p.index, p.name) for p in listed.partitions] == [
        (0, "DH0"), (1, "dh0.1"), (2, "DH0.2")]
    assert listed.fallback_reason is None
    units, fallback_reason = _enumerate_mount_units(str(image))
    assert [u.name for u in units] == ["DH0", "dh0.1", "DH0.2"]
    assert fallback_reason is None


def test_each_name_selects_its_partition(tmp_path):
    image = _image(tmp_path, [("DH0", DOS3), ("DH0", DOS7)])
    assert find_partition(image, None, "DH0") == PartitionRef(None, 0, "DH0")
    assert find_partition(image, None, "dh0.1") == PartitionRef(None, 1, "DH0.1")
    assert find_partition(image, None, "1") == PartitionRef(None, 1, "DH0.1")
    assert find_partition(image, None, "DH0.2") is None
    assert get_partition_info(image, None, "DH0.1") == {"name": "DH0.1",
                                                        "dostype": DOS7}

    backend = BlockDeviceBackend(image, partition_scope=True,
                                 partition="DH0.1")
    backend.open()
    try:
        assert backend.partition_number == 1
        # Cylinders 3 and 4, of 16 blocks each.
        assert backend._block_range == (48, 80)
    finally:
        backend.close()


def test_a_name_taken_by_a_rename_is_renamed_too(tmp_path):
    # The second DH0 is DH0.1, so the partition named DH0.1 mounts as
    # DH0.2, as AmigaOS mounts it, and DH0.1 selects the second DH0.
    image = _image(tmp_path, [("DH0", DOS3), ("DH0", DOS3), ("DH0.1", DOS7)])
    assert [p.name for p in list_partitions(image).partitions] == [
        "DH0", "DH0.1", "DH0.2"]
    assert find_partition(image, None, "DH0.1").number == 1
    assert get_partition_info(image, None, "DH0.2") == {"name": "DH0.2",
                                                        "dostype": DOS7}


@pytest.mark.parametrize("selector,low_cyl", [("DH0", 1), ("dh0.1", 3),
                                              ("1", 3)])
def test_handler_gets_the_selected_partition(tmp_path, monkeypatch, selector,
                                             low_cyl):
    # The handler's DosEnvec comes from BootstrapAllocator, which takes the
    # partition number the backend found.
    image = _image(tmp_path, [("DH0", DOS3), ("DH0", DOS7)])
    real = BootstrapAllocator._read_partition_env
    cyls = {}

    def spy(self):
        de, blk, rd, part = real(self)
        cyls["low"] = de.low_cyl
        return de, blk, rd, part

    monkeypatch.setattr(BootstrapAllocator, "_read_partition_env", spy)
    seen = _bootstrap_open(monkeypatch, image, "_read_partition_env",
                           partition=selector)
    assert seen["drive"] == "DH0"
    assert cyls["low"] == low_cyl


NO_AUTOMOUNT = PartitionBlock.FLAG_NO_AUTOMOUNT


def test_a_partition_not_mounted_takes_a_name_last(tmp_path):
    # The A4091 mounter names only the partitions it mounts, so the second
    # DH0 mounts as DH0, and the first, not mounted by AmigaOS, is DH0.1.
    image = _image(tmp_path, [("DH0", DOS3, NO_AUTOMOUNT), ("DH0", DOS7),
                              ("DH0", DOS3, NO_AUTOMOUNT)])
    assert [p.name for p in list_partitions(image).partitions] == [
        "DH0.1", "DH0", "DH0.2"]
    assert find_partition(image, None, "DH0") == PartitionRef(None, 1, "DH0")
    assert find_partition(image, None, "DH0.1") == PartitionRef(None, 0, "DH0.1")


def test_a_partition_not_mounted_yields_its_name_to_a_later_rdb(
        tmp_path, user_driver_dir, capsys):
    first =_image(tmp_path, [("DH0", DOS3, NO_AUTOMOUNT)], name="a.hdf")
    second = _image(tmp_path, [("DH0", DOS3)], name="b.hdf")
    image = _make_mbr(tmp_path, [(64, first.read_bytes()),
                                 (256, second.read_bytes())])
    assert [p.name for p in list_partitions(image).partitions] == [
        "DH0.1", "DH0"]
    assert find_partition(image, None, "DH0") == PartitionRef(1, 0, "DH0")

    main(["inspect", "--json", str(image)])
    rdbs = json.loads(capsys.readouterr().out)["rdbs"]
    assert [[p.get("mount_name") for p in rdb["partitions"]]
            for rdb in rdbs] == [["DH0.1"], [None]]


def _two_sizes(tmp_path, second):
    """Entry 0: a 512-byte-block RDB holding DH0. Entry 1: a 1024-byte-block
    RDB holding a partition per name in second."""
    first = _image(tmp_path, [("DH0", DOS3)], name="a.hdf")
    later = _image(tmp_path, [(drive, DOS3) for drive in second],
                   block_bytes=1024, name="b.hdf")
    return _make_mbr(tmp_path, [(64, first.read_bytes()),
                                (256, later.read_bytes())])


@pytest.mark.parametrize("second,selector,message", [
    # Forced to 1024 bytes, entry 0's RDB does not open, and with it the
    # DH0 that auto-detection selects.
    (["DH0"], "DH0", "is in an RDB that does not open with 1024-byte blocks"),
    # Without entry 0's DH0, entry 1's DH0 keeps its name, so no partition
    # is named DH0.1.
    (["DH0"], "DH0.1", "is not found with 1024-byte blocks, as an RDB on "
                       "this disk does not open with them"),
    # DH0.1 is entry 1's second partition, not its first.
    (["DH0", "DH0"], "DH0.1", "names another partition with 1024-byte "
                              "blocks, as an RDB on this disk does not open "
                              "with them"),
])
def test_forced_size_that_renames_otherwise_is_refused(tmp_path, second,
                                                        selector, message):
    image = _two_sizes(tmp_path, second)
    assert find_partition(image, None, selector) is not None
    with pytest.raises(OSError) as caught:
        find_partition(image, 1024, selector)
    assert str(caught.value) == f"Partition {selector} {message}"
