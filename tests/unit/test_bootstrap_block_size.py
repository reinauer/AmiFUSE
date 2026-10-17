"""Handler sessions on RDBs whose block size is not 512 bytes."""

import struct
from unittest.mock import Mock

import pytest

from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from amifuse.bootstrap import BootstrapAllocator
from amifuse.fuse_fs import HandlerBridge
from amifuse.rdb_inspect import ADF_DD_SIZE, detect_adf, open_rdisk
from tests.unit.test_mbr_rdb_block_size import _make_mbr, _make_rdb


class _Opened(Exception):
    pass


class _Started(Exception):
    pass


def _bootstrap_open(monkeypatch, image, reader, **kwargs):
    # Run the real backend and the real bootstrap reader, then stop before
    # any handler state is allocated; the 68k runtime is a mock.
    seen = {}
    real = getattr(BootstrapAllocator, reader)

    def opened(self):
        _, blk, rdisk, part = real(self)
        seen["block_bytes"] = blk.block_bytes
        seen["offset"] = getattr(blk, "offset", 0)
        if rdisk is not None:
            seen["drive"] = str(part.get_drive_name())
            rdisk.close()
        blk.close()
        raise _Opened

    monkeypatch.setattr(BootstrapAllocator, reader, opened)
    monkeypatch.setattr("amifuse.fuse_fs.VamosHandlerRuntime", Mock)
    with pytest.raises(_Opened):
        HandlerBridge(image, image.parent / "handler", **kwargs)
    return seen


def _start_session(monkeypatch, image, **kwargs):
    # Run the real backend and partition reader, then stop where the handler
    # would be given the device; the 68k runtime is a mock.
    def alloc_all(self, **_):
        _, blk, rdisk, part = self._read_partition_env()
        rdisk.close()
        blk.close()
        return {"part": part}

    runtime = Mock()
    runtime.set_scsi_backend.side_effect = _Started
    monkeypatch.setattr(BootstrapAllocator, "alloc_all", alloc_all)
    monkeypatch.setattr("amifuse.fuse_fs.VamosHandlerRuntime", lambda: runtime)
    HandlerBridge(image, image.parent / "handler", **kwargs)


def _with_block_bytes(rdb, block_bytes):
    # Declare another rdb_BlockBytes in the RDSK block, keeping it valid.
    data = bytearray(rdb)
    struct.pack_into(">I", data, 16, block_bytes)
    summed = struct.unpack_from(">I", data, 4)[0]  # rdb_SummedLongs
    struct.pack_into(">I", data, 8, 0)  # rdb_ChkSum
    total = sum(struct.unpack_from(f">{summed}I", data, 0)) & 0xFFFFFFFF
    struct.pack_into(">I", data, 8, -total & 0xFFFFFFFF)
    return bytes(data)


def _emu68_with_768_byte_rdb(tmp_path):
    # 768-byte blocks are not a whole number of 512-byte sectors. Mapped in
    # such blocks, the 0x76 container starts 1.5 times too far in, where this
    # image holds the RDB's metadata, and its partition runs into the FAT data.
    rdb = _make_rdb(tmp_path, 768)
    start, sectors = 64, len(rdb) // 512
    fat = start + sectors
    data = bytearray((fat + sectors) * 512)
    for index, (kind, lba) in enumerate([(0x76, start), (0x0C, fat)]):
        entry = 446 + 16 * index
        data[entry + 4] = kind
        struct.pack_into("<II", data, entry + 8, lba, sectors)
    data[510:512] = b"\x55\xaa"
    data[fat * 512:] = b"FAT!" * (sectors * 128)
    # The scan finds the RDSK block in the container's first sector ...
    data[start * 512:start * 512 + 768] = rdb[:768]
    # ... and cylinder 0 of the RDB where 768-byte blocks map the container.
    data[start * 768:(start + 16) * 768] = rdb[:16 * 768]
    image = tmp_path / "emu68.hdf"
    image.write_bytes(bytes(data))
    return image


@pytest.mark.parametrize("block_bytes", [512, 1024, 2048, 4096, 8192, 16384,
                                         32768])
@pytest.mark.parametrize("forced", [False, True])
def test_plain_rdb_opens_at_its_block_size(tmp_path, monkeypatch, block_bytes,
                                           forced):
    image = tmp_path / "rdb.hdf"
    image.write_bytes(_make_rdb(tmp_path, block_bytes))
    seen = _bootstrap_open(monkeypatch, image, "_read_partition_env",
                           block_size=block_bytes if forced else None)
    assert seen == {"block_bytes": block_bytes, "offset": 0, "drive": "DH0"}


@pytest.mark.parametrize("start_lba,forced", [
    (2048, False), (2048, True),
    # A forced 4096 at LBA 64 also needs the fix for #93.
    (64, False),
])
def test_rdb_with_large_blocks_inside_mbr_partition(tmp_path, monkeypatch,
                                                    start_lba, forced):
    image = _make_mbr(tmp_path, [(start_lba, _make_rdb(tmp_path, 4096))])
    seen = _bootstrap_open(monkeypatch, image, "_read_partition_env",
                           block_size=4096 if forced else None)
    assert seen == {"block_bytes": 4096, "offset": start_lba * 512 // 4096,
                    "drive": "DH0"}


@pytest.mark.parametrize("layout", ["plain", "emu68"])
def test_rdb_with_768_byte_blocks_starts_no_handler(tmp_path, monkeypatch,
                                                    layout):
    if layout == "plain":
        image = tmp_path / "rdb.hdf"
        image.write_bytes(_make_rdb(tmp_path, 768))
    else:
        image = _emu68_with_768_byte_rdb(tmp_path)
    before = image.read_bytes()
    runtime = Mock(side_effect=_Started)
    monkeypatch.setattr("amifuse.fuse_fs.VamosHandlerRuntime", runtime)
    with pytest.raises(OSError, match="valid RDB"):
        HandlerBridge(image, tmp_path / "handler", read_only=False)
    runtime.assert_not_called()
    assert image.read_bytes() == before


@pytest.mark.parametrize("block_bytes", [0, 256, 65536, 0xFFFFFFFF])
@pytest.mark.parametrize("forced", [False, True])
def test_rdb_with_invalid_block_size_is_not_found(tmp_path, block_bytes,
                                                  forced):
    image = tmp_path / "rdb.hdf"
    image.write_bytes(_with_block_bytes(_make_rdb(tmp_path, 512), block_bytes))
    with pytest.raises(OSError, match="No valid RDB found in blocks 0-15"):
        open_rdisk(image, block_size=512 if forced else None)


@pytest.mark.parametrize("size_block,read_only,refused", [
    (2048, False, True),
    (2048, True, False),
    (1024, False, False),
])
def test_writable_session_needs_size_block_to_match_rdb(
        tmp_path, monkeypatch, size_block, read_only, refused):
    # The partition lock counts cylinders in RDB blocks, the handler in
    # de_SizeBlock units.
    image = tmp_path / "rdb.hdf"
    image.write_bytes(_make_rdb(tmp_path, 1024))
    raw = RawBlockDevice(str(image), read_only=False, block_bytes=1024)
    raw.open()
    disk = RDisk(raw)
    try:
        assert disk.open()
        part = disk.get_partition(0).part_blk
        part.dos_env.block_size = size_block // 4
        part.write()
    finally:
        disk.close()
        raw.close()
    before = image.read_bytes()
    with pytest.raises(SystemExit if refused else _Started) as caught:
        _start_session(monkeypatch, image, read_only=read_only,
                       partition="DH0")
    if refused:
        assert str(caught.value) == (
            "Partition 'DH0' declares 2048-byte blocks but its RDB uses "
            "1024-byte blocks; it cannot be opened for writing")
    assert image.read_bytes() == before


@pytest.mark.parametrize("block_size", [None, 1024])
def test_adf_keeps_512_byte_blocks(tmp_path, monkeypatch, block_size):
    # The commands pass the requested size, None by default, for floppies too.
    image = tmp_path / "disk.adf"
    image.write_bytes(b"DOS\0".ljust(ADF_DD_SIZE, b"\0"))
    seen = _bootstrap_open(monkeypatch, image, "_read_adf_env",
                           block_size=block_size, adf_info=detect_adf(image))
    assert seen == {"block_bytes": 512, "offset": 0}
