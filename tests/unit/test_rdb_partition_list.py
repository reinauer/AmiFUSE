"""An RDB whose partition list links back on itself or past the end.

The list ends at such a link, with a warning, and keeps the partitions read
so far, as the filesystem list does (test_rdb_chain_loops). The partitions
after the link are unknown, so for writes the RDB counts as one that fails
(test_selector_failed_rdb). pb_Next is long 4 of a PART block, and block
numbers are relative to the RDB's device. A first link past the end, with no
partition to keep, still fails the RDB (test_open_rdisk_errors).
"""

import pytest

from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.rdb_inspect import (find_partition, list_partitions, open_rdisk,
                                 print_inspect_report)
from tests.unit.test_mbr_rdb_block_size import _make_mbr, _make_rdb
from tests.unit.test_parceiro_partitions import _disk_rdb
from tests.unit.test_rdb_chain_loops import _edit
from tests.unit.test_repeated_partition_names import DOS3, _image
from tests.unit.test_selector_failed_rdb import (_default_refused, _open,
                                                 _refused, _selector_refused)

PAST_END = 100000


def _loop(blk):
    return f"Partition list loops back to block {blk} (remaining partitions skipped)"


def _past(blk):
    return (f"Partition list links to block {blk}, past the end of the device "
            f"(remaining partitions skipped)")


def _cut(rdb):
    return f"the rest of the partition list of the {rdb}"


def _two_parts(tmp_path, last_next):
    """A plain disk: DH0 and DH1 on PART blocks 1 and 2, DH1 linking on to
    block last_next."""
    image = _image(tmp_path, [("DH0", DOS3), ("DH1", DOS3)])
    _edit(image, 2, {4: last_next})
    return image


def _parsed(image):
    blkdev, rdisk, _ = open_rdisk(image)
    try:
        return ([str(p.get_drive_name()) for p in rdisk.parts],
                rdisk.rdb_warnings, rdisk.damaged_part_list)
    finally:
        rdisk.close()
        blkdev.close()


@pytest.mark.timeout(10)
@pytest.mark.parametrize("blk", [1, 2], ids=["earlier", "self"])
def test_loop_ends_the_list(tmp_path, blk):
    assert _parsed(_two_parts(tmp_path, blk)) == (
        ["DH0", "DH1"], [_loop(blk)], _loop(blk))


def test_link_past_the_end_keeps_the_partitions_before_it(tmp_path):
    assert _parsed(_two_parts(tmp_path, PAST_END)) == (
        ["DH0", "DH1"], [_past(PAST_END)], _past(PAST_END))


def test_first_block_past_the_last_is_past_the_end(tmp_path):
    # A 160-block disk has no block 160.
    assert _parsed(_two_parts(tmp_path, 160))[2] == _past(160)


@pytest.mark.timeout(10)
@pytest.mark.parametrize("last_next", [1, PAST_END], ids=["loop", "past_end"])
def test_writes_are_refused_on_a_plain_disk(tmp_path, last_next):
    image = _two_parts(tmp_path, last_next)
    for selector in ("DH0", "DH1", "1", "2"):
        assert _refused(image, selector) == _selector_refused(selector,
                                                              _cut("RDB"))
    assert _refused(image, None) == _default_refused(_cut("RDB"))
    with pytest.raises(OSError) as caught:
        find_partition(image, 512, "DH0", write=True)
    assert str(caught.value) == _selector_refused("DH0", _cut("RDB"))

    # Read-only, the partitions read still open.
    assert _open(image, "DH1") == (None, "DH1")
    assert _open(image) == (None, "DH0")


@pytest.mark.timeout(10)
def test_inspect_reports_the_broken_link(tmp_path, capsys):
    print_inspect_report(_two_parts(tmp_path, 1), None, False)
    out = capsys.readouterr().out
    assert "DH1" in out
    assert _loop(1) in out


@pytest.mark.timeout(10)
def test_list_partitions_says_the_list_is_cut_short(tmp_path):
    from amifuse.launcher import _enumerate_mount_units

    image = _two_parts(tmp_path, 1)
    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["DH0", "DH1"]
    assert listed.fallback_reason == f"RDB: {_loop(1)}"
    assert not listed.default_open_fails
    units, reason = _enumerate_mount_units(str(image))
    assert [u.name for u in units] == ["DH0", "DH1"]
    assert reason == f"RDB: {_loop(1)}"


def _two_entries(tmp_path):
    """Entries at LBA 200 and 400, of 160 sectors each, with RDBs holding DH0
    and WORK. Each PART block is block 1 of its RDB."""
    return _make_mbr(tmp_path, [(200, _make_rdb(tmp_path, 512, "DH0")),
                                (400, _make_rdb(tmp_path, 512, "WORK"))])


def test_link_past_the_end_of_an_entry(tmp_path):
    # Block 170 of entry 0 lies on the disk, but not in the entry.
    image = _two_entries(tmp_path)
    _edit(image, 201, {4: 170})
    entry_0 = _cut("RDB in MBR partition [0]")
    assert _refused(image, "WORK") == _selector_refused("WORK", entry_0)
    assert _refused(image, None) == _default_refused(entry_0)
    assert _open(image, "DH0") == (0, "DH0")
    assert _open(image, "WORK") == (1, "WORK")
    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["DH0", "WORK"]
    assert listed.fallback_reason == f"RDB in MBR partition [0]: {_past(170)}"


@pytest.mark.timeout(10)
def test_default_write_opens_before_a_later_entry_cut_short(tmp_path):
    # As for an RDB that fails after the one that open reaches.
    image = _two_entries(tmp_path)
    _edit(image, 401, {4: 1})
    entry_1 = _cut("RDB in MBR partition [1]")
    assert _refused(image, "DH0") == _selector_refused("DH0", entry_1)
    assert _open(image, read_only=False) == (0, "DH0")


@pytest.mark.timeout(10)
def test_single_entry_is_named_though_the_default_open_reaches_it(tmp_path):
    # open_each_rdb gives a lone entry's RDB index None.
    image = _make_mbr(tmp_path, [(200, _make_rdb(tmp_path, 512, "DH0"))])
    _edit(image, 201, {4: 1})
    entry_0 = _cut("RDB in MBR partition [0]")
    assert _refused(image, "DH0") == _selector_refused("DH0", entry_0)
    assert _refused(image, None) == _default_refused(entry_0)
    assert _open(image, "DH0") == (0, "DH0")


@pytest.mark.timeout(10)
def test_rdb_beside_the_mbr_cut_short(tmp_path):
    # The own RDB's PART blocks are 3 and 4, of DH0 and DH1.
    image = _disk_rdb(tmp_path, ("PI0", "PI1"), 2)
    _edit(image, 4, {4: 3})
    own = _cut("RDB beside the MBR")
    assert _refused(image, "PI0") == _selector_refused("PI0", own)
    assert _refused(image, None) == _default_refused(own)
    assert _open(image, "DH1") == (None, "DH1")
    assert _open(image, "PI0") == (0, "PI0")
    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["DH0", "DH1", "PI0", "PI1"]
    assert listed.fallback_reason == f"RDB beside the MBR: {_loop(3)}"


@pytest.mark.timeout(10)
def test_own_rdb_loop_does_not_hang_the_overlap_check(tmp_path):
    # An entry's open reads the disk's own RDB for its partitions.
    image = _disk_rdb(tmp_path, ("PI0", "PI1"), 2)
    _edit(image, 4, {4: 3})
    backend = BlockDeviceBackend(image, partition_scope=True, partition="PI1")
    backend.open()
    try:
        assert backend.mbr_context.mbr_partition.index == 1
    finally:
        backend.close()


@pytest.mark.parametrize("target", [0, 100, "bad-checksum"])
def test_corrupt_later_part_block_keeps_readable_prefix(tmp_path, target):
    if target == "bad-checksum":
        image = _image(tmp_path, [("DH0", DOS3), ("DH1", DOS3), ("DH2", DOS3)])
        from tests.unit.test_rdb_chain_loops import _corrupt
        _corrupt(image, 3)
        target = 3
    else:
        image = _two_parts(tmp_path, target)
    names, warnings, damaged = _parsed(image)
    assert names == ["DH0", "DH1"]
    assert f"Corrupt partition block at block {target}" in damaged
    assert warnings == [damaged]
    assert [p.name for p in list_partitions(image).partitions] == names
    for selector in ("DH0", "DH1", "0", "1"):
        assert find_partition(image, None, selector) is not None
        with pytest.raises(OSError, match="not opened for writing"):
            find_partition(image, None, selector, write=True)
    assert _refused(image, None) == _default_refused(_cut("RDB"))


def test_corrupt_first_part_block_still_fails(tmp_path):
    image = _two_parts(tmp_path, 0xffffffff)
    from tests.unit.test_rdb_chain_loops import _corrupt
    _corrupt(image, 1)
    with pytest.raises(OSError, match="Corrupt partition block at block 1"):
        open_rdisk(image)
