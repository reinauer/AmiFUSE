"""An RDB inside an MBR 0x76 partition may use blocks larger than sectors."""

from contextlib import ExitStack
import struct

import pytest

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

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
    (512, 64), (1024, 64), (2048, 64),
    # At LBA 64 a 4096-byte block 8 is the partition's first sector, which
    # the direct scan reaches before the MBR path (#93).
    (4096, 2048),
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
