from contextlib import ExitStack, suppress
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk
from amitools.vamos.disk import DiskImage, PartitionFileLock
from amitools.vamos.lib.ScsiDevice import (
    CMD_READ, CMD_WRITE, HD_SCSICMD, ScsiDevice, TDERR_NOT_SPECIFIED,
)
from amitools.vamos.libstructs import IORequestStruct, SCSICmdStruct
from amitools.vamos.machine.mock import MockMemory
from amitools.vamos.mem import MemoryAlloc

from amifuse.driver_runtime import BlockDeviceBackend


def _make_rdb(path, partitions=(("DH0", (1, 9)),)):
    raw = RawBlockDevice(str(path), read_only=False, block_bytes=512)
    with ExitStack() as cleanup:
        raw.create(320)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(10, 1, 32), rdb_cyls=1)
        for name, bounds in partitions:
            rdisk.add_partition(FSString(name), bounds)
        raw.flush()


@pytest.mark.parametrize("read_only", [True, False])
def test_backend_shares_exclusive_lock_with_vamos(tmp_path, read_only):
    image_path = tmp_path / "disk.hdf"
    _make_rdb(image_path)
    backend = BlockDeviceBackend(image_path, read_only=read_only)
    try:
        backend.open()
        assert backend.exclusive is True
        # Both ends must remain readable through the separate data handle,
        # including Windows where a buffered read can reach the lock byte.
        assert len(backend.read_blocks(0)) == 512
        assert backend.read_blocks(319) == bytes(512)
        if not read_only:
            backend.write_blocks(319, b"x" * 512)
            assert backend.read_blocks(319) == b"x" * 512
        other = DiskImage(image_path)
        try:
            with pytest.raises(IOError, match="exclusively lock"):
                other.open()
        finally:
            other.close()
    finally:
        backend.close()
    assert backend.exclusive is False
    image = DiskImage(image_path).open()
    try:
        assert image.read_blocks(0)[:4] == b"RDSK"
    finally:
        image.close()


@pytest.mark.parametrize("failure", [RuntimeError("open failed"), SystemExit(1)])
def test_failed_open_preserves_error_and_releases_lock(tmp_path, monkeypatch, failure):
    image_path = tmp_path / "disk.hdf"
    _make_rdb(image_path)
    backend = BlockDeviceBackend(image_path)
    backend.rdb = Mock()
    backend.rdb.close.side_effect = OSError("cleanup failed")
    monkeypatch.setattr(backend, "_open_image", Mock(side_effect=failure))
    try:
        with pytest.raises(type(failure)) as caught:
            backend.open()
        assert caught.value is failure
        assert backend.exclusive is False
        image = DiskImage(image_path).open()
        image.close()
    finally:
        with suppress(OSError):
            backend.close()


@pytest.mark.parametrize("read_only", [True, False])
def test_partition_sessions_are_independent(tmp_path, read_only):
    image = tmp_path / "two.hdf"
    _make_rdb(image, (("DH0", (1, 4)), ("DH1", (5, 9))))
    original = image.read_bytes()
    first = BlockDeviceBackend(image, partition_scope=True, partition="DH0",
                               read_only=read_only)
    second = BlockDeviceBackend(image, partition_scope=True, partition="DH1",
                                read_only=False)
    with ExitStack() as cleanup:
        cleanup.callback(first.close)
        cleanup.callback(second.close)
        first.open()
        second.open()
        assert not first.exclusive
        for block in (160, 319):
            second.write_blocks(block, b"b" * 512)
        with pytest.raises(OSError):
            DiskImage(image).open()
        conflict = BlockDeviceBackend(image, partition_scope=True,
                                      partition="DH0", read_only=False)
        with pytest.raises(OSError):
            conflict.open()
        assert not conflict.host_lock.is_locked
        first.close()
        assert second.read_blocks(319) == b"b" * 512
        second.sync()
        assert image.read_bytes()[:160 * 512] == original[:160 * 512]
    second.open()
    try:
        assert second.read_blocks(319) == b"b" * 512
    finally:
        second.close()
    raw = DiskImage(image).open()
    raw.close()


@pytest.mark.parametrize("block,count", [(0, 1), (31, 1), (159, 2), (160, 1),
                                         (-1, 1), (32, -1)])
def test_handler_cannot_escape_partition(tmp_path, block, count):
    image = tmp_path / "two.hdf"
    _make_rdb(image, (("DH0", (1, 4)), ("DH1", (5, 9))))
    original = image.read_bytes()
    backend = BlockDeviceBackend(image, partition_scope=True, read_only=False)
    backend.open()
    try:
        with pytest.raises(OSError):
            backend.read_blocks(block, count)
        with pytest.raises(OSError):
            backend.write_blocks(block, b"x" * (max(count, 0) * 512), count)
        with pytest.raises(OSError):
            backend.write_blocks(32, b"x" * 1024)
    finally:
        backend.close()
    assert image.read_bytes() == original


def test_invalid_partition_releases_guard(tmp_path):
    image = tmp_path / "disk.hdf"
    _make_rdb(image)
    backend = BlockDeviceBackend(image, partition_scope=True, partition="missing")
    with pytest.raises(ValueError):
        backend.open()
    assert not backend.host_lock.is_locked
    raw = DiskImage(image).open()
    raw.close()


def test_mbr_partition_lock_uses_absolute_offset(tmp_path):
    image = tmp_path / "mbr.hdf"
    _make_rdb(image)
    rdb = image.read_bytes()
    prefix = bytearray(32 * 512)
    prefix[446 + 4] = 0x76
    struct.pack_into("<II", prefix, 446 + 8, 32, 320)
    prefix[510:512] = b"\x55\xaa"
    image.write_bytes(prefix + rdb)
    backend = BlockDeviceBackend(image, partition_scope=True, read_only=False)
    backend.open()
    try:
        assert backend.read_blocks(32) == bytes(512)
        overlapping = PartitionFileLock(image, read_only=False)
        overlapping.acquire()
        try:
            with pytest.raises(OSError):
                overlapping.lock_range(64 * 512, 512)
            # The RDB-relative offset alone would incorrectly lock this byte.
            overlapping.lock_range(32 * 512, 512)
        finally:
            overlapping.release()
        backend.write_blocks(319, b"z" * 512)
    finally:
        backend.close()
    assert image.read_bytes()[:64 * 512] == (prefix + rdb)[:64 * 512]
    assert image.read_bytes()[-512:] == b"z" * 512


def test_partition_cannot_extend_truncated_image(tmp_path):
    image = tmp_path / "short.hdf"
    _make_rdb(image)
    with image.open("r+b") as stream:
        stream.truncate(200 * 512)
    backend = BlockDeviceBackend(image, partition_scope=True, read_only=False)
    backend.open()
    try:
        with pytest.raises(OSError, match="exceeds disk image"):
            backend.write_blocks(199, b"x" * 1024, 2)
    finally:
        backend.close()
    assert image.stat().st_size == 200 * 512


def test_partition_cannot_overlap_rdb_metadata(tmp_path):
    image = tmp_path / "bad.hdf"
    _make_rdb(image)
    raw = RawBlockDevice(str(image), read_only=False)
    raw.open()
    disk = RDisk(raw)
    try:
        assert disk.open()
        part = disk.get_partition(0).part_blk
        part.dos_env.low_cyl = 0
        part.write()
    finally:
        disk.close()
        raw.close()
    backend = BlockDeviceBackend(image, partition_scope=True, read_only=False)
    with pytest.raises(ValueError, match="RDB metadata"):
        backend.open()
    assert not backend.host_lock.is_locked


@pytest.mark.parametrize("write", [False, True])
@pytest.mark.parametrize("direct_scsi", [False, True])
def test_scsi_request_recovers_after_partition_error(tmp_path, write, direct_scsi):
    image = tmp_path / "two.hdf"
    _make_rdb(image, (("DH0", (1, 4)), ("DH1", (5, 9))))
    backend = BlockDeviceBackend(image, partition_scope=True, read_only=False)
    backend.open()
    try:
        mem = MockMemory(size_kib=64)
        ctx = SimpleNamespace(mem=mem, alloc=MemoryAlloc(mem))
        dev = ScsiDevice(backend)
        ior = IORequestStruct(mem, 0x1000)
        mem.w_block(0x2000, b"x" * 512)
        for block, rejected in [(160, True), (32, False)]:
            if direct_scsi:
                ior.command.val = HD_SCSICMD
                ior.data.val = 0x3000
                ior.length.val = SCSICmdStruct.get_byte_size()
                scsi = SCSICmdStruct(mem, 0x3000)
                scsi.scsi_Data.val = 0x2000
                scsi.scsi_Length.val = 512
                scsi.scsi_Command.val = 0x4000
                scsi.scsi_CmdLength.val = 10
                scsi.scsi_SenseData.val = 0x5000
                scsi.scsi_SenseLength.val = 18
                mem.w8(0x4000, 0x2A if write else 0x28)
                mem.w32(0x4002, block)
                mem.w16(0x4007, 1)
            else:
                ior.command.val = CMD_WRITE if write else CMD_READ
                ior.offset.val = block * 512
                ior.data.val = 0x2000
                ior.length.val = 512
            dev.BeginIO(ctx, 0x1000)
            if direct_scsi:
                assert scsi.scsi_Status.val == (2 if rejected else 0)
                assert scsi.scsi_Actual.val == (0 if rejected else 512)
                if rejected:
                    assert mem.r8(0x5002) == 0x03
                    assert mem.r8(0x500c) == (0x0C if write else 0x11)
            else:
                assert ior.error.val == (TDERR_NOT_SPECIFIED if rejected else 0)
                assert ior.actual.val == (0 if rejected else 512)
        assert backend.read_blocks(32) == (b"x" * 512 if write else bytes(512))
    finally:
        backend.close()
