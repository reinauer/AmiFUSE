from contextlib import ExitStack, suppress
import errno
import os
import struct
import sys
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

import amifuse.driver_runtime as driver_runtime
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
    with pytest.raises(ValueError, match="Partition DH0 overlaps RDB metadata"):
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


def test_partition_with_invalid_geometry_is_named(tmp_path):
    image = tmp_path / "bad.hdf"
    _make_rdb(image)
    raw = RawBlockDevice(str(image), read_only=False)
    raw.open()
    disk = RDisk(raw)
    try:
        assert disk.open()
        part = disk.get_partition(0).part_blk
        part.dos_env.high_cyl = part.dos_env.low_cyl - 1
        part.write()
    finally:
        disk.close()
        raw.close()
    backend = BlockDeviceBackend(image, partition_scope=True, read_only=False)
    with pytest.raises(ValueError, match="Partition DH0 has invalid geometry"):
        backend.open()
    assert not backend.host_lock.is_locked


def test_partition_past_truncated_image_opens_for_diagnosis(tmp_path):
    # HandlerBridge reports the truncation; the backend must not refuse the
    # partition as invalid first, and must still bound its I/O.
    image = tmp_path / "short.hdf"
    _make_rdb(image, (("DH0", (1, 4)), ("DH1", (5, 9))))
    with image.open("r+b") as stream:
        stream.truncate(150 * 512)
    backend = BlockDeviceBackend(image, partition_scope=True, partition="DH1")
    backend.open()
    try:
        assert backend._block_range == (160, 320)
        with pytest.raises(OSError, match="exceeds disk image"):
            backend.read_blocks(160)
    finally:
        backend.close()


@pytest.mark.parametrize("platform,pointer,expected", [
    ("win32", 8, True),
    ("darwin", 8, True),
    ("linux", 8, True),
    ("linux", 4, False),
    ("freebsd14", 8, False),
])
def test_range_lock_support_by_platform(monkeypatch, platform, pointer, expected):
    monkeypatch.setattr(driver_runtime.sys, "platform", platform)
    monkeypatch.setattr(driver_runtime.ctypes, "sizeof", lambda _type: pointer)
    assert driver_runtime._range_locks_supported() is expected


def test_partition_session_keeps_whole_image_lock_without_range_locks(
        tmp_path, monkeypatch):
    image = tmp_path / "two.hdf"
    _make_rdb(image, (("DH0", (1, 4)), ("DH1", (5, 9))))
    monkeypatch.setattr(driver_runtime, "_range_locks_supported", lambda: False)
    backend = BlockDeviceBackend(image, partition_scope=True, partition="DH1")
    backend.open()
    try:
        assert backend.exclusive
        assert backend._block_range == (160, 320)
        with pytest.raises(OSError):
            backend.read_blocks(0)
    finally:
        backend.close()


@pytest.mark.parametrize("partition_scope", [True, False])
def test_whole_image_conflict_reports_image_in_use(tmp_path, partition_scope):
    image = tmp_path / "disk.hdf"
    _make_rdb(image)
    holder = DiskImage(image).open()
    try:
        backend = BlockDeviceBackend(image, partition_scope=partition_scope)
        with pytest.raises(driver_runtime.ImageInUseError) as caught:
            backend.open()
        assert str(caught.value).startswith(
            "disk.hdf is in use by another AmiFUSE or vamos session")
        assert not backend.host_lock.is_locked
    finally:
        holder.close()


def test_partition_conflict_reports_partition_in_use(tmp_path):
    image = tmp_path / "two.hdf"
    _make_rdb(image, (("DH0", (1, 4)), ("DH1", (5, 9))))
    first = BlockDeviceBackend(image, partition_scope=True, partition="DH0",
                               read_only=False)
    first.open()
    try:
        second = BlockDeviceBackend(image, partition_scope=True, partition="DH0")
        with pytest.raises(driver_runtime.ImageInUseError,
                           match="^Partition DH0 of two.hdf is in use"):
            second.open()
        assert not second.host_lock.is_locked
    finally:
        first.close()


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="POSIX file permissions")
def test_unreadable_image_is_not_reported_in_use(tmp_path):
    image = tmp_path / "disk.hdf"
    _make_rdb(image)
    image.chmod(0)
    try:
        backend = BlockDeviceBackend(image)
        with pytest.raises(PermissionError) as caught:
            backend.open()
        assert not isinstance(caught.value, driver_runtime.ImageInUseError)
    finally:
        image.chmod(0o644)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root can write read-only files")
@pytest.mark.parametrize("partition_scope", [True, False])
def test_read_only_image_opened_for_writing_is_not_reported_in_use(
        tmp_path, partition_scope):
    # Windows reports both this and msvcrt lock contention as EACCES.
    image = tmp_path / "disk.hdf"
    _make_rdb(image)
    image.chmod(0o444)
    try:
        backend = BlockDeviceBackend(image, partition_scope=partition_scope,
                                     read_only=False)
        with pytest.raises(PermissionError) as caught:
            backend.open()
        assert not isinstance(caught.value, driver_runtime.ImageInUseError)
        assert caught.value.filename == str(image)
        assert not backend.host_lock.is_locked
    finally:
        image.chmod(0o644)


def test_physical_disk_lock_failure_keeps_amitools_message(tmp_path, monkeypatch):
    image = tmp_path / "disk.hdf"
    _make_rdb(image)
    backend = BlockDeviceBackend(image)
    monkeypatch.setattr(driver_runtime, "is_windows_disk", lambda path: True)
    busy = _wrapped(BlockingIOError(errno.EAGAIN, "busy"))
    # Returns without raising, so open() re-raises the original error.
    assert backend._raise_if_in_use(busy) is None


def _wrapped(cause):
    try:
        try:
            raise cause
        except OSError as exc:
            raise OSError("cannot exclusively lock disk image x: %s" % exc) from exc
    except OSError as exc:
        return exc


def _winerror(code):
    exc = OSError(errno.EACCES, "denied")
    exc.winerror = code
    return exc


@pytest.mark.parametrize("exc,expected", [
    (_wrapped(PermissionError(errno.EACCES, "Permission denied")), True),
    (_wrapped(BlockingIOError(errno.EWOULDBLOCK, "busy")), True),
    (BlockingIOError(errno.EAGAIN, "busy"), True),
    (_wrapped(_winerror(33)), True),
    # A sharing violation only reaches here from a physical disk held by
    # another program; image files report it as an open() error.
    (_wrapped(_winerror(32)), False),
    # Access denied on a physical disk needs elevation, not an unmount.
    (_wrapped(_winerror(5)), False),
    (PermissionError(errno.EACCES, "Permission denied", "disk.hdf"), False),
    (_wrapped(OSError(errno.EIO, "I/O error")), False),
    (OSError("partition locks require a regular image file"), False),
])
def test_lock_conflict_classification(exc, expected):
    assert driver_runtime._is_lock_conflict(exc) is expected
