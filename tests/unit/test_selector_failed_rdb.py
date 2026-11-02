"""Selectors on a disk where one RDB is found but does not open.

The names and numbers of the RDBs after it shift, so a writable open by
name, by number or without --partition is refused (#304), a disk RDSK
block failing its checksum counts as such an RDB (#305), and a device or
media error in an entry's RDB ends the lookup (#267), read-only only if
the selector is not a name before that entry. Read-only opens of the RDBs
that open are unchanged.
"""

import errno

import pytest
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice

from amifuse.driver_runtime import BlockDeviceBackend
from amifuse import rdb_inspect
from amifuse.rdb_inspect import (DiskRDBError, NoEntryRDBError,
                                 PartitionRef, find_partition,
                                 list_partitions, open_rdisk)
from tests.unit.test_mbr_rdb_block_size import _make_mbr, _make_rdb
from tests.unit.test_parceiro_partitions import _disk_rdb
from tests.unit.test_repeated_partition_names import (DOS3, NO_AUTOMOUNT,
                                                      _image)


def _flip(image, block, byte=40):
    """Flip a byte of a 512-byte block, so its checksum fails."""
    data = bytearray(image.read_bytes())
    data[block * 512 + byte] ^= 0xFF
    image.write_bytes(bytes(data))


def _two_dh0_entries(tmp_path, first=True):
    """An Emu68 disk: entries at LBA 200 and 400, each with an RDB holding
    DH0 (the second DH0.1 while both open); first=False leaves entry 0
    without an RDB."""
    return _make_mbr(tmp_path, [
        (200, _make_rdb(tmp_path, 512, "DH0") if first else bytes(160 * 512)),
        (400, _make_rdb(tmp_path, 512, "DH0"))])


def _open(image, partition=None, read_only=True):
    """(MBR entry index or None, drive name) that the backend opens."""
    backend = BlockDeviceBackend(image, partition_scope=True,
                                 partition=partition, read_only=read_only)
    backend.open()
    try:
        number = backend.partition_number or 0
        ctx = backend.mbr_context
        entry = ctx.mbr_partition.index if ctx and ctx.mbr_partition else None
        return entry, str(backend.rdb.get_partition(number).get_drive_name())
    finally:
        backend.close()


def _refused(image, partition):
    backend = BlockDeviceBackend(image, partition_scope=True,
                                 partition=partition, read_only=False)
    with pytest.raises(OSError) as caught:
        backend.open()
    assert backend.blkdev is None
    return str(caught.value)


def _selector_refused(selector, rdb):
    return (f"Partition {selector} is not opened for writing: {rdb} on this "
            f"disk does not open, so partition names and numbers may select "
            f"another partition")


def _default_refused(rdb):
    return (f"No partition is opened for writing without --partition: {rdb} "
            f"on this disk does not open, so the partition opened may be "
            f"another one")


ENTRY_0 = "the RDB in MBR partition [0]"
DISK = "the RDB beside the MBR"


@pytest.mark.parametrize("selector", ["DH0", "0"])
def test_entry_rdb_that_fails_refuses_writes(tmp_path, selector):
    image = _two_dh0_entries(tmp_path)
    _flip(image, 201)  # entry 0's PART block
    with pytest.raises(OSError) as caught:
        find_partition(image, None, selector, write=True)
    assert str(caught.value) == _selector_refused(selector, ENTRY_0)
    assert _refused(image, selector) == _selector_refused(selector, ENTRY_0)
    assert _refused(image, None) == _default_refused(ENTRY_0)
    with pytest.raises(rdb_inspect.EntryRDBError):
        open_rdisk(image, mbr_partition_index=0)

    # Read-only, entry 1's partition still opens, by selector and without.
    assert _open(image, selector) == (1, "DH0")
    assert _open(image) == (1, "DH0")
    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["DH0"]
    assert listed.fallback_reason.startswith(
        "MBR partition [0]: MBR partition 0 does not contain a valid RDB")


def test_writes_open_beside_an_entry_holding_no_rdb(tmp_path):
    image = _two_dh0_entries(tmp_path, first=False)
    with pytest.raises(NoEntryRDBError):
        open_rdisk(image, mbr_partition_index=0)
    assert _open(image, "DH0", read_only=False) == (1, "DH0")
    assert _open(image, read_only=False) == (1, "DH0")


def test_writes_open_on_an_intact_disk(tmp_path):
    image = _two_dh0_entries(tmp_path)
    assert _open(image, "DH0.1", read_only=False) == (1, "DH0")
    assert _open(image, read_only=False) == (0, "DH0")
    plain = tmp_path / "plain.hdf"
    plain.write_bytes(_make_rdb(tmp_path, 512, "DH0"))
    assert _open(plain, read_only=False) == (None, "DH0")


def test_read_error_in_an_entry_rdb_ends_the_lookup(tmp_path, monkeypatch):
    image = _two_dh0_entries(tmp_path)
    read_block = RawBlockDevice.read_block

    def failing(self, blk_num, num_blks=1, **kwargs):
        start = blk_num * self.block_bytes
        end = start + num_blks * self.block_bytes
        if start < 216 * 512 and 200 * 512 < end:  # entry 0's blocks 0-15
            raise OSError(errno.EIO, "Input/output error")
        return read_block(self, blk_num, num_blks, **kwargs)

    monkeypatch.setattr(RawBlockDevice, "read_block", failing)
    for write in (False, True):
        with pytest.raises(OSError) as caught:
            find_partition(image, None, "DH0", write=write)
        assert caught.value.errno == errno.EIO
    for read_only in (True, False):
        backend = BlockDeviceBackend(image, partition_scope=True,
                                     partition="DH0", read_only=read_only)
        with pytest.raises(OSError) as caught:
            backend.open()
        assert caught.value.errno == errno.EIO
        assert backend.blkdev is None


def _later_entry_fails(tmp_path, monkeypatch, first):
    """Entries at LBA 64 and 256: entry 0's RDB holds a partition per
    (name, DOS type[, flags]) in first, entry 1's WORK, and reading entry
    1's blocks 0-15 fails."""
    a = _image(tmp_path, first, name="a.hdf")
    b = _image(tmp_path, [("WORK", DOS3)], name="b.hdf")
    image = _make_mbr(tmp_path, [(64, a.read_bytes()), (256, b.read_bytes())])
    read_block = RawBlockDevice.read_block

    def failing(self, blk_num, num_blks=1, **kwargs):
        start = blk_num * self.block_bytes
        end = start + num_blks * self.block_bytes
        if start < 272 * 512 and 256 * 512 < end:  # entry 1's blocks 0-15
            raise OSError(errno.EIO, "Input/output error")
        return read_block(self, blk_num, num_blks, **kwargs)

    monkeypatch.setattr(RawBlockDevice, "read_block", failing)
    return image


def _eio(image, partition, block_size=None, write=False):
    with pytest.raises(OSError) as caught:
        find_partition(image, block_size, partition, write=write)
    assert caught.value.errno == errno.EIO


@pytest.mark.parametrize("selector,number,name", [("DH0", 0, "DH0"),
                                                  ("dh1", 1, "DH1")])
def test_read_error_in_a_later_entry_leaves_earlier_names(
        tmp_path, monkeypatch, selector, number, name):
    # A read error in entry 1 cannot change what a name in entry 0 resolves
    # to, so read-only opens it; writes are still refused.
    image = _later_entry_fails(tmp_path, monkeypatch,
                               [("DH0", DOS3), ("DH1", DOS3)])
    assert find_partition(image, None, selector) == PartitionRef(0, number,
                                                                 name)
    assert find_partition(image, 512, selector) == PartitionRef(0, number,
                                                                name)
    assert _open(image, selector) == (0, name)
    _eio(image, selector, write=True)
    backend = BlockDeviceBackend(image, partition_scope=True,
                                 partition=selector, read_only=False)
    with pytest.raises(OSError) as caught:
        backend.open()
    assert caught.value.errno == errno.EIO
    assert backend.blkdev is None


@pytest.mark.parametrize("selector", ["WORK", "DH0.1", "0", "1", "2"])
def test_read_error_ends_a_lookup_not_named_before_it(tmp_path, monkeypatch,
                                                      selector):
    # WORK is in entry 1 and DH0.1 is no name in entry 0, so each ends with
    # the read error rather than "not found". A number does too, even one
    # entry 0 has: entry 1 may hold a partition named as the number, which
    # names outrank.
    image = _later_entry_fails(tmp_path, monkeypatch,
                               [("DH0", DOS3), ("DH1", DOS3)])
    _eio(image, selector)
    _eio(image, selector, 512)


def test_read_error_ends_a_lookup_of_a_name_a_later_rdb_can_take(
        tmp_path, monkeypatch):
    # Entry 0's DH0 is not mounted by AmigaOS, so a mounted DH0 in entry 1
    # takes the name (mount_names) and the read error ends the lookup.
    image = _later_entry_fails(tmp_path, monkeypatch,
                               [("DH0", DOS3, NO_AUTOMOUNT)])
    _eio(image, "DH0")
    _eio(image, "DH0", 512)


def _bad_disk_rdsk(tmp_path, entries=("DH0", "PI1")):
    """A Parceiro disk: its own RDB (DH0, DH1) with the RDSK block at block 2
    failing its checksum, beside 0x76 entries at LBA 200 and 400."""
    image = _disk_rdb(tmp_path, entries, 2)
    _flip(image, 2)
    return image


def _bad(block):
    return f"RDSK block at block {block} fails its checksum"


def _bad_disk_rdsk_error(image):
    return f"Failed to parse RDB at {image}: {_bad(2)}"


@pytest.mark.parametrize("selector", ["DH0", "0"])
def test_disk_rdsk_with_a_bad_checksum_refuses_writes(tmp_path, selector):
    image = _bad_disk_rdsk(tmp_path)
    with pytest.raises(DiskRDBError) as caught:
        open_rdisk(image)
    assert str(caught.value) == _bad_disk_rdsk_error(image)

    with pytest.raises(OSError) as caught:
        find_partition(image, None, selector, write=True)
    assert str(caught.value) == _selector_refused(selector, DISK)
    assert _refused(image, selector) == _selector_refused(selector, DISK)
    assert _refused(image, None) == _default_refused(DISK)

    # Read-only, the entries' partitions still open by name.
    assert _open(image, selector) == (0, "DH0")
    assert _open(image, "PI1") == (1, "PI1")
    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["DH0", "PI1"]
    assert listed.fallback_reason == (
        f"RDB beside the MBR: {_bad_disk_rdsk_error(image)}")


def test_rdsk_with_a_bad_checksum_before_a_valid_one_is_passed_over(tmp_path):
    # A damaged copy at block 1 and the disk's RDSK block at block 2.
    image = _disk_rdb(tmp_path, ("DH0", "PI1"), 2)
    data = bytearray(image.read_bytes())
    data[512:1024] = data[1024:1536]
    image.write_bytes(bytes(data))
    _flip(image, 1)
    ref = find_partition(image, None, "DH0", write=True)
    assert ref.mbr_partition_index is None
    assert list_partitions(image).fallback_reason is None


def test_entry_rdsk_with_a_bad_checksum_is_an_rdb_that_fails(tmp_path):
    image = _two_dh0_entries(tmp_path)
    _flip(image, 200)  # entry 0's RDSK block
    with pytest.raises(OSError) as caught:
        open_rdisk(image, mbr_partition_index=0)
    assert str(caught.value) == (
        f"MBR partition 0 does not contain a valid RDB: {image}; partition 0 "
        f"at LBA 200: {_bad(0)}")
    assert isinstance(caught.value, rdb_inspect.EntryRDBError)
    assert _refused(image, "DH0") == _selector_refused("DH0", ENTRY_0)
    assert _open(image, "DH0") == (1, "DH0")


def test_plain_rdsk_with_a_bad_checksum_names_the_block(tmp_path):
    image = tmp_path / "plain.hdf"
    image.write_bytes(_make_rdb(tmp_path, 512, "DH0"))
    _flip(image, 0)
    with pytest.raises(OSError) as caught:
        open_rdisk(image)
    assert not isinstance(caught.value, DiskRDBError)
    assert str(caught.value) == f"Failed to parse RDB at {image}: {_bad(0)}"


def test_bad_rdsk_inside_an_entry_is_that_entry_s(tmp_path):
    # Entry 0 at LBA 2: the direct scan of blocks 0-15 reaches its RDSK
    # block, which belongs to the entry, not to the disk.
    image = _make_mbr(tmp_path, [(2, _make_rdb(tmp_path, 512, "DH0")),
                                 (400, _make_rdb(tmp_path, 512, "WORK"))])
    _flip(image, 2)
    listed = list_partitions(image)
    assert [p.name for p in listed.partitions] == ["WORK"]
    assert listed.fallback_reason == (
        f"MBR partition [0]: MBR partition 0 does not contain a valid RDB: "
        f"{image}; partition 0 at LBA 2: {_bad(0)}")
    assert _refused(image, "WORK") == _selector_refused("WORK", ENTRY_0)


def test_image_ending_inside_an_entry_rdb_is_an_rdb_that_fails(tmp_path):
    # The image ends after entry 1's RDSK block, before its PART block.
    image = _make_mbr(tmp_path, [(200, _make_rdb(tmp_path, 512, "DH0")),
                                 (400, _make_rdb(tmp_path, 512, "WORK"))])
    image.write_bytes(image.read_bytes()[:401 * 512])
    entry_1 = "the RDB in MBR partition [1]"
    assert _refused(image, "DH0") == _selector_refused("DH0", entry_1)
    with pytest.raises(rdb_inspect.EntryRDBError):
        open_rdisk(image, mbr_partition_index=1)

    # Read-only, entry 0's partition opens by name, and the launcher's lone
    # unit without --partition reaches it too.
    from amifuse.launcher import _enumerate_mount_units

    assert _open(image, "DH0") == (0, "DH0")
    units, _reason = _enumerate_mount_units(str(image))
    assert [u.name for u in units] == [None]
    assert _open(image) == (0, "DH0")


def test_damaged_rdsk_where_the_image_ends_is_an_rdb_that_fails(tmp_path):
    # Entry 0 lies last on the disk; its RDSK block is damaged and the image
    # ends 5 sectors into it, inside the scan of its first 16 blocks.
    image = _make_mbr(tmp_path, [(400, _make_rdb(tmp_path, 512, "DH0")),
                                 (200, _make_rdb(tmp_path, 512, "DH0"))])
    _flip(image, 400)
    image.write_bytes(image.read_bytes()[:405 * 512])
    assert _refused(image, "DH0") == _selector_refused("DH0", ENTRY_0)
    assert list_partitions(image).fallback_reason.endswith(
        f"partition 0 at LBA 400: {_bad(0)}")
    assert _open(image, "DH0") == (1, "DH0")


def test_default_write_opens_before_an_rdb_that_fails(tmp_path):
    # Entry 1's RDB fails after entry 0's, which the open without
    # --partition reaches either way; a selector is still refused.
    image = _two_dh0_entries(tmp_path)
    _flip(image, 401)  # entry 1's PART block
    assert _open(image, read_only=False) == (0, "DH0")
    entry_1 = "the RDB in MBR partition [1]"
    assert _refused(image, "DH0") == _selector_refused("DH0", entry_1)


def test_default_write_with_a_forced_size_reaching_another_rdb(tmp_path):
    # Entry 0's RDB uses 512-byte blocks, entry 1's 1024: with 1024 forced,
    # the open without --partition passes over entry 0 to entry 1.
    image = _make_mbr(tmp_path, [(200, _make_rdb(tmp_path, 512, "DH0")),
                                 (400, _make_rdb(tmp_path, 1024, "WORK"))])
    backend = BlockDeviceBackend(image, 1024, partition_scope=True,
                                 read_only=False)
    with pytest.raises(OSError) as caught:
        backend.open()
    assert str(caught.value) == (
        "No partition is opened for writing without --partition with "
        "1024-byte blocks: an RDB on this disk does not open with them, so "
        "the partition opened is another one")
    assert backend.blkdev is None
    backend = BlockDeviceBackend(image, 1024, partition_scope=True)
    backend.open()
    try:
        assert str(backend.rdb.get_partition(0).get_drive_name()) == "WORK"
    finally:
        backend.close()
    assert _open(image, read_only=False) == (0, "DH0")


def test_launcher_names_a_lone_partition_beside_an_rdb_that_fails(tmp_path):
    from amifuse.launcher import _enumerate_mount_units

    image = _bad_disk_rdsk(tmp_path, ("PI0",))
    units, fallback_reason = _enumerate_mount_units(str(image))
    # Without --partition the open stops on the disk's RDB.
    assert [u.name for u in units] == ["PI0"]
    assert fallback_reason == (
        f"RDB beside the MBR: {_bad_disk_rdsk_error(image)}")
    assert _open(image, "PI0") == (0, "PI0")


def test_launcher_mounts_a_lone_partition_before_a_read_error(tmp_path,
                                                              monkeypatch):
    from amifuse.launcher import _enumerate_mount_units

    # Entry 1's blocks fail to read; the open without --partition reaches
    # entry 0's DH0 first, so the launcher still mounts it that way.
    image = _make_mbr(tmp_path, [(200, _make_rdb(tmp_path, 512, "DH0")),
                                 (400, _make_rdb(tmp_path, 512, "WORK"))])
    read_block = RawBlockDevice.read_block

    def failing(self, blk_num, num_blks=1, **kwargs):
        if (blk_num + num_blks) * self.block_bytes > 400 * 512:
            raise OSError(errno.EIO, "Input/output error")
        return read_block(self, blk_num, num_blks, **kwargs)

    monkeypatch.setattr(RawBlockDevice, "read_block", failing)
    units, fallback_reason = _enumerate_mount_units(str(image))
    assert [u.name for u in units] == [None]
    assert fallback_reason.startswith("MBR partition [1]: ")
    assert _open(image, read_only=False) == (0, "DH0")
