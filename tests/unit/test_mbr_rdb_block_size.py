"""Locating an RDB on a disk with an MBR.

An RDB inside an MBR 0x76 partition may use blocks larger than sectors,
which lets the direct scan of blocks 0-15 reach it. Such an RDB belongs to
its partition and opens through the MBR. A Parceiro-style RDB outside every
0x76 entry opens directly, and a failure to parse it is reported as such.
"""

from contextlib import ExitStack
import struct

import pytest

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.rdb_inspect import open_rdisk


def _make_rdb(tmp_path, block_bytes, drive="DH0"):
    rdb_image = tmp_path / f"rdb-{drive}.hdf"
    raw = RawBlockDevice(str(rdb_image), read_only=False, block_bytes=block_bytes)
    with ExitStack() as cleanup:
        raw.create(160)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(10, 1, 16), rdb_cyls=1)
        rdisk.add_partition(FSString(drive), (1, 9))
        raw.flush()
    return rdb_image.read_bytes()


def _make_mbr(tmp_path, partitions):
    """Place (start_lba, rdb_bytes) pairs into 0x76 entries of one image."""
    end = max(lba * 512 + len(rdb) for lba, rdb in partitions)
    data = bytearray(end)
    for index, (lba, rdb) in enumerate(partitions):
        entry = 446 + 16 * index
        data[entry + 4] = 0x76
        struct.pack_into("<II", data, entry + 8, lba, len(rdb) // 512)
        data[lba * 512:lba * 512 + len(rdb)] = rdb
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "mbr.hdf"
    image.write_bytes(bytes(data))
    return image


def _make_mbr_rdb(tmp_path, block_bytes, start_lba):
    return _make_mbr(tmp_path, [(start_lba, _make_rdb(tmp_path, block_bytes))])


def _open(image, **kwargs):
    blkdev, rdisk, ctx = open_rdisk(image, **kwargs)
    try:
        return (blkdev.block_bytes, blkdev.offset, blkdev.num_blocks,
                str(rdisk.get_partition(0).get_drive_name()), ctx.offset_blocks)
    finally:
        rdisk.close()
        blkdev.close()


@pytest.mark.parametrize("block_bytes,start_lba", [
    (512, 64), (1024, 64), (2048, 64), (4096, 2048),
    # The direct scan reaches these RDBs when forced, and at LBA 8
    # also when auto-detected, after reopening at 2048 bytes.
    (4096, 64), (2048, 32), (2048, 8),
])
@pytest.mark.parametrize("forced", [False, True])
def test_rdb_block_size_inside_mbr_partition(tmp_path, block_bytes, start_lba,
                                             forced):
    image = _make_mbr_rdb(tmp_path, block_bytes, start_lba)
    size, offset, num_blocks, name, sectors = _open(
        image, block_size=block_bytes if forced else None)
    assert size == block_bytes
    assert offset * block_bytes == start_lba * 512
    assert num_blocks == 160
    assert name == "DH0"
    # inspect reports the MBR offset in sectors.
    assert sectors == start_lba


@pytest.mark.parametrize("forced", [False, True])
def test_partition_not_aligned_to_rdb_blocks_is_refused(tmp_path, forced):
    image = _make_mbr_rdb(tmp_path, 1024, start_lba=63)
    with pytest.raises(OSError) as caught:
        open_rdisk(image, block_size=1024 if forced else None)
    message = str(caught.value)
    assert "none contain a valid RDB" in message
    assert "partition 0 at LBA 63 is not aligned to the RDB's 1024-byte blocks" in message


def _with_partition_list(rdb, block_bytes, block):
    data = bytearray(rdb)
    struct.pack_into(">I", data, 28, block)  # rdb_PartitionList
    summed = struct.unpack_from(">I", data, 4)[0]  # rdb_SummedLongs
    struct.pack_into(">I", data, 8, 0)  # rdb_ChkSum
    total = sum(struct.unpack_from(f">{summed}I", data, 0)) & 0xFFFFFFFF
    struct.pack_into(">I", data, 8, -total & 0xFFFFFFFF)
    assert not any(data[block * block_bytes:(block + 1) * block_bytes])
    return bytes(data)


@pytest.mark.parametrize("first_lba", [63, 64])
def test_later_partition_is_scanned_in_its_own_block_size(tmp_path, first_lba):
    first = _make_rdb(tmp_path, 1024, "DH0")
    if first_lba == 64:
        # A valid RDB whose partition list points at an empty block, so
        # its partition is rejected after reopening at 1024 bytes.
        first = _with_partition_list(first, 1024, 5)
    image = _make_mbr(tmp_path, [(first_lba, first),
                                 (385, _make_rdb(tmp_path, 512, "DH1"))])
    size, offset, _, name, sectors = _open(image)
    assert (size, offset, name, sectors) == (512, 385, "DH1", 385)


@pytest.mark.parametrize("forced", [False, True])
def test_unreadable_rdb_in_scanned_partition_reports_mbr_error(tmp_path,
                                                                forced):
    # Forced, the direct scan finds this RDB at block 8, inside the
    # partition, so only the MBR path opens it; report it as
    # auto-detection does.
    rdb = _with_partition_list(_make_rdb(tmp_path, 4096), 4096, 5)
    image = _make_mbr(tmp_path, [(64, rdb)])
    with pytest.raises(OSError) as caught:
        open_rdisk(image, block_size=4096 if forced else None)
    assert str(caught.value) == (
        f"MBR with 1 Amiga partition(s) found, but none contain a valid RDB: "
        f"{image}")


@pytest.mark.parametrize("forced", [False, True])
def test_partition_rdb_readable_at_disk_level_opens_in_partition(tmp_path,
                                                                 forced):
    # A copy of the PART block at disk-level block 1 lets this RDB parse
    # with disk-relative block numbers when the forced scan finds it at
    # block 8. It lies inside the partition, so it opens there instead.
    rdb = _make_rdb(tmp_path, 4096)
    part = struct.unpack_from(">I", rdb, 28)[0]  # rdb_PartitionList
    image = _make_mbr(tmp_path, [(64, rdb)])
    data = bytearray(image.read_bytes())
    assert not any(data[part * 4096:(part + 1) * 4096])
    data[part * 4096:(part + 1) * 4096] = rdb[part * 4096:(part + 1) * 4096]
    image.write_bytes(bytes(data))
    assert _open(image, block_size=4096 if forced else None) == (
        4096, 8, 160, "DH0", 64)


def _make_parceiro(tmp_path, rdb):
    """Move a 512-byte-block RDB to block 2, behind a FAT32-only MBR."""
    data = bytearray(rdb)
    assert not any(data[1024:1536])
    data[1024:1536] = data[:512]
    data[:512] = bytes(512)
    data[446 + 4] = 0x0C
    struct.pack_into("<II", data, 446 + 8, len(rdb) // 512, 64)
    data += bytes(64 * 512)
    data[510:512] = b"\x55\xaa"
    image = tmp_path / "parceiro.hdf"
    image.write_bytes(bytes(data))
    return image


def _add_amiga_partition(image, lba, rdb):
    """Add a 0x76 entry holding another RDB, far from the direct one."""
    data = bytearray(image.read_bytes())
    data += bytes(lba * 512 + len(rdb) - len(data))
    data[446 + 16 + 4] = 0x76
    struct.pack_into("<II", data, 446 + 16 + 8, lba, len(rdb) // 512)
    data[lba * 512:] = rdb
    image.write_bytes(bytes(data))


@pytest.mark.parametrize("amiga_entry", [False, True])
@pytest.mark.parametrize("forced", [False, True])
def test_parceiro_rdb_after_mbr_opens_directly(tmp_path, forced, amiga_entry):
    image = _make_parceiro(tmp_path, _make_rdb(tmp_path, 512))
    if amiga_entry:
        _add_amiga_partition(image, 4096, _make_rdb(tmp_path, 512, "DH1"))
    blkdev, rdisk, ctx = open_rdisk(image, block_size=512 if forced else None)
    try:
        assert isinstance(blkdev, RawBlockDevice)
        assert rdisk.rdb.blk_num == 2
        assert str(rdisk.get_partition(0).get_drive_name()) == "DH0"
        assert (ctx.scheme, ctx.mbr_partition, ctx.offset_blocks) == (
            "parceiro", None, 0)
    finally:
        rdisk.close()
        blkdev.close()


@pytest.mark.parametrize("amiga_entry", [False, True])
@pytest.mark.parametrize("forced", [False, True])
def test_unreadable_parceiro_rdb_is_not_retried_through_mbr(tmp_path, forced,
                                                            amiga_entry):
    # The RDB lies outside every 0x76 entry, so another partition's RDB
    # must not stand in for it, nor turn its error into "not found".
    rdb = _with_partition_list(_make_rdb(tmp_path, 512), 512, 5)
    image = _make_parceiro(tmp_path, rdb)
    if amiga_entry:
        _add_amiga_partition(image, 4096, _make_rdb(tmp_path, 512, "DH1"))
    block_size = 512 if forced else None
    with pytest.raises(OSError) as caught:
        open_rdisk(image, block_size=block_size)
    assert str(caught.value) == f"Failed to parse RDB at {image}"
    backend = BlockDeviceBackend(image, block_size, partition_scope=True,
                                 partition="DH0")
    with pytest.raises(OSError, match="Failed to parse RDB"):
        backend.open()
