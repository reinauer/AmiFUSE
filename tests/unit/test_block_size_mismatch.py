"""A --block-size that the RDB does not use is an open error, not a crash."""

import errno
import struct

import pytest

from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice

import amifuse.rdb_inspect as rdb_inspect
from amifuse.rdb_inspect import find_partition_mbr_index, open_rdisk
from tests.unit.test_mbr_rdb_block_size import (
    _add_amiga_partition, _make_mbr, _make_mbr_rdb, _make_parceiro,
    _make_rdb, _with_partition_list,
)


@pytest.fixture
def devices(monkeypatch):
    # Count the block devices open_rdisk opens and closes.
    counts = {"open": 0, "close": 0}

    class CountingDevice(RawBlockDevice):
        def open(self):
            super().open()
            counts["open"] += 1

        def close(self):
            super().close()
            counts["close"] += 1

    monkeypatch.setattr(rdb_inspect, "RawBlockDevice", CountingDevice)
    return counts


@pytest.mark.parametrize("rdb_bytes,forced", [
    (512, 1024), (512, 2048), (512, 4096), (1024, 512), (2048, 512),
])
def test_direct_rdb_in_other_block_size_is_refused(tmp_path, devices,
                                                    rdb_bytes, forced):
    _make_rdb(tmp_path, rdb_bytes)
    image = tmp_path / "rdb-DH0.hdf"
    with pytest.raises(OSError) as caught:
        open_rdisk(image, block_size=forced)
    assert str(caught.value) == (
        f"Failed to parse RDB at {image}: RDB uses {rdb_bytes}-byte blocks, "
        f"but the device was opened with {forced}-byte blocks")
    assert devices == {"open": 1, "close": 1}


@pytest.mark.parametrize("amiga_entry", [False, True])
def test_parceiro_rdb_in_other_block_size_is_refused(tmp_path, devices,
                                                     amiga_entry):
    # The RDB at LBA 2 lies outside every 0x76 entry, so it is parsed at
    # disk level and its error is not replaced by another partition's.
    image = _make_parceiro(tmp_path, _make_rdb(tmp_path, 512))
    if amiga_entry:
        _add_amiga_partition(image, 4096, _make_rdb(tmp_path, 1024, "DH1"))
    with pytest.raises(OSError) as caught:
        open_rdisk(image, block_size=1024)
    assert str(caught.value) == (
        f"Failed to parse RDB at {image}: RDB uses 512-byte blocks, "
        f"but the device was opened with 1024-byte blocks")
    assert devices == {"open": 1, "close": 1}


@pytest.mark.parametrize("start_lba", [
    2048,
    # The direct scan reaches this RDB too, at block 4, inside the
    # partition, so it is left to the MBR path.
    8,
])
def test_partition_rdb_in_other_block_size_is_refused(tmp_path, devices,
                                                       start_lba):
    image = _make_mbr_rdb(tmp_path, 512, start_lba)
    with pytest.raises(OSError) as caught:
        open_rdisk(image, block_size=1024)
    message = str(caught.value)
    assert "none contain a valid RDB" in message
    assert message.endswith(
        f"; partition 0 at LBA {start_lba}: RDB uses 512-byte blocks, "
        f"but the device was opened with 1024-byte blocks")
    assert devices == {"open": 1, "close": 1}


def _two_rdbs(tmp_path):
    # DH0's RDB uses 512-byte blocks, DH1's 1024-byte blocks.
    return _make_mbr(tmp_path, [(64, _make_rdb(tmp_path, 512, "DH0")),
                                (256, _make_rdb(tmp_path, 1024, "DH1"))])


def test_later_partition_in_forced_block_size_opens(tmp_path, devices):
    image = _two_rdbs(tmp_path)
    blkdev, rdisk, ctx = open_rdisk(image, block_size=1024)
    try:
        assert ctx.mbr_partition.index == 1
        assert str(rdisk.get_partition(0).get_drive_name()) == "DH1"
    finally:
        rdisk.close()
        blkdev.close()
    assert devices == {"open": 1, "close": 1}


def test_partition_lookup_skips_rdb_in_other_block_size(tmp_path, devices):
    # --partition DH1 searches each RDB in turn, then opens the one found.
    image = _two_rdbs(tmp_path)
    index = find_partition_mbr_index(image, 1024, "DH1")
    assert index == 1
    blkdev, rdisk, _ = open_rdisk(image, block_size=1024,
                                  mbr_partition_index=index)
    try:
        assert rdisk.find_partition_by_string("DH1") is not None
    finally:
        rdisk.close()
        blkdev.close()
    assert devices["open"] == devices["close"]


@pytest.mark.parametrize("later", ["DH0", "DH1"])
def test_partition_lookup_refuses_name_in_skipped_rdb(tmp_path, devices,
                                                      later):
    # Entry 0's DH0 is the one auto-detection finds. Forced to 1024
    # bytes, that RDB is skipped, so the lookup must not settle for a
    # later DH0 or report DH0 missing.
    image = _make_mbr(tmp_path, [(64, _make_rdb(tmp_path, 512, "DH0")),
                                 (256, _make_rdb(tmp_path, 1024, later))])
    with pytest.raises(OSError) as caught:
        find_partition_mbr_index(image, 1024, "DH0")
    assert str(caught.value) == (
        "Partition DH0 is in an RDB that does not open with 1024-byte blocks")
    assert devices["open"] == devices["close"]


def _damaged_then(tmp_path, rdb_bytes):
    # A 4096-byte-block RDB at LBA 64 whose partition list points at an
    # empty block, then DH1 at LBA 2048. Forced to 4096 bytes, the direct
    # scan finds the damaged RDB at block 8, inside partition 0, and
    # leaves both partitions to the MBR path.
    damaged = _with_partition_list(_make_rdb(tmp_path, 4096), 4096, 5)
    return _make_mbr(tmp_path, [(64, damaged),
                                (2048, _make_rdb(tmp_path, rdb_bytes, "DH1"))])


def test_damaged_rdb_then_rdb_in_other_block_size(tmp_path, devices):
    image = _damaged_then(tmp_path, 512)
    with pytest.raises(OSError) as caught:
        open_rdisk(image, block_size=4096)
    assert str(caught.value).endswith(
        "; partition 0 at LBA 64: Corrupt partition block at block 5"
        "; partition 1 at LBA 2048: RDB uses 512-byte blocks, "
        "but the device was opened with 4096-byte blocks")
    assert devices == {"open": 1, "close": 1}


def test_damaged_rdb_then_rdb_in_forced_block_size(tmp_path, devices):
    image = _damaged_then(tmp_path, 4096)
    blkdev, rdisk, ctx = open_rdisk(image, block_size=4096)
    try:
        assert ctx.mbr_partition.index == 1
        assert str(rdisk.get_partition(0).get_drive_name()) == "DH1"
    finally:
        rdisk.close()
        blkdev.close()
    assert devices == {"open": 1, "close": 1}


# An unreadable 0x76 entry is skipped like one without a valid RDB, so a
# later entry still opens and the device is closed once either way.
def _unreadable_then_valid(tmp_path, past_eof):
    image = _make_mbr(tmp_path, [(64, bytes(512)),
                                 (256, _make_rdb(tmp_path, 512, "DH1"))])
    if past_eof:
        data = bytearray(image.read_bytes())
        struct.pack_into("<I", data, 446 + 8, 1 << 20)  # entry 0 start LBA
        image.write_bytes(bytes(data))
    return image


@pytest.mark.parametrize("past_eof,reason", [
    # The scan of 1-block entry 0 reads beyond it at block 1.
    (False, "partition 0 at LBA 64: Read beyond partition: 1+1 > 1"),
    (True, "partition 0 at LBA 1048576 starts past the end of the image"),
], ids=["one-block", "past-eof"])
@pytest.mark.parametrize("forced", [False, True])
def test_unreadable_partition_is_skipped(tmp_path, devices, past_eof, reason,
                                         forced):
    image = _unreadable_then_valid(tmp_path, past_eof)
    blkdev, rdisk, ctx = open_rdisk(image, block_size=512 if forced else None)
    try:
        assert ctx.mbr_partition.index == 1
        assert str(rdisk.get_partition(0).get_drive_name()) == "DH1"
    finally:
        rdisk.close()
        blkdev.close()
    assert devices == {"open": 1, "close": 1}

    # Alone, the entry is reported with the reason.
    with pytest.raises(OSError) as caught:
        open_rdisk(image, block_size=512 if forced else None,
                   mbr_partition_index=0)
    assert reason in str(caught.value)
    assert devices == {"open": 2, "close": 2}


@pytest.mark.parametrize("block", [64, 65], ids=["scan", "parse"])
def test_failing_read_in_partition_is_not_skipped(tmp_path, devices,
                                                  monkeypatch, block):
    # A read error says nothing about entry 0's contents, so it ends the
    # open instead of opening entry 1. Block 64 holds entry 0's RDB and
    # block 65 its partition block.
    image = _make_mbr(tmp_path, [(64, _make_rdb(tmp_path, 512, "DH0")),
                                 (256, _make_rdb(tmp_path, 512, "DH1"))])

    class FailingRead(rdb_inspect.RawBlockDevice):
        def read_block(self, blk_num, num_blks=1):
            if blk_num == block:
                raise OSError(errno.EIO, "injected")
            return super().read_block(blk_num, num_blks)

    monkeypatch.setattr(rdb_inspect, "RawBlockDevice", FailingRead)
    with pytest.raises(OSError) as caught:
        open_rdisk(image)
    assert caught.value.errno == errno.EIO
    assert devices == {"open": 1, "close": 1}


# open_rdisk closes its device whatever fails, and only once.
@pytest.mark.parametrize("forced", [False, True])
def test_image_shorter_than_scan_is_closed(tmp_path, devices, forced):
    # The scan of blocks 0-15 reads past the end of this 8-block image.
    image = tmp_path / "short.hdf"
    image.write_bytes(bytes(8 * 512))
    with pytest.raises(OSError):
        open_rdisk(image, block_size=512 if forced else None)
    assert devices == {"open": 1, "close": 1}


@pytest.mark.parametrize("in_mbr", [False, True])
def test_unexpected_error_closes_device(tmp_path, devices, monkeypatch,
                                       in_mbr):
    if in_mbr:
        image = _make_mbr_rdb(tmp_path, 512, 2048)
    else:
        _make_rdb(tmp_path, 512)
        image = tmp_path / "rdb-DH0.hdf"

    def fail(self):
        raise RuntimeError("injected")

    monkeypatch.setattr(rdb_inspect.RDisk, "open", fail)
    with pytest.raises(RuntimeError, match="injected"):
        open_rdisk(image)
    assert devices == {"open": 1, "close": 1}


def test_failed_reopen_leaves_nothing_open(tmp_path, devices, monkeypatch):
    # Auto-detection reopens at the RDB's 1024-byte blocks; that fails.
    _make_rdb(tmp_path, 1024)
    image = tmp_path / "rdb-DH0.hdf"

    class FailsAt1024(rdb_inspect.RawBlockDevice):
        def open(self):
            if self.img_file.block_bytes == 1024:
                raise OSError("injected")
            super().open()

    monkeypatch.setattr(rdb_inspect, "RawBlockDevice", FailsAt1024)
    with pytest.raises(OSError, match="injected"):
        open_rdisk(image)
    assert devices == {"open": 1, "close": 1}


def test_failed_close_keeps_the_open_error(tmp_path, devices, monkeypatch):
    # The error that ended the open is the one to report, not a failure
    # of the cleanup that followed it.
    _make_rdb(tmp_path, 512)
    image = tmp_path / "rdb-DH0.hdf"

    class FailingClose(rdb_inspect.RawBlockDevice):
        def close(self):
            super().close()
            raise OSError("close failed")

    monkeypatch.setattr(rdb_inspect, "RawBlockDevice", FailingClose)
    with pytest.raises(OSError, match="^Failed to parse RDB"):
        open_rdisk(image, block_size=1024)
    assert devices == {"open": 1, "close": 1}
