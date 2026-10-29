"""On macOS, BlockDeviceBackend.fsync() must also flush the drive's cache.

macOS fsync() hands the data to the drive and leaves it in the drive's
write cache, which a power loss still loses. An image file needs
fcntl(F_FULLFSYNC); a disk device needs the DKIOCSYNCHRONIZECACHE ioctl.
The tests fake macOS with a stand-in fcntl module; the last one makes the
real call on a macOS host.
"""

import errno
import os
import stat
import sys
from types import SimpleNamespace

import pytest

from amifuse import driver_runtime
from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.rdb_inspect import ADF_DD_SIZE, detect_adf

F_FULLFSYNC = 51  # <sys/fcntl.h>
DKIOCSYNCHRONIZECACHE = 0x20006416  # <sys/disk.h>, _IO('d', 22)
# Errors that mean the call isn't accepted, not that a write failed.
REFUSALS = pytest.mark.parametrize(
    "refusal",
    [errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOTTY, errno.EINVAL, errno.ENOSYS],
    ids=["ENOTSUP", "EOPNOTSUPP", "ENOTTY", "EINVAL", "ENOSYS"])


@pytest.fixture
def backend(tmp_path):
    image = tmp_path / "disk.adf"
    image.write_bytes(b"DOS\0" + bytes(ADF_DD_SIZE - 4))
    backend = BlockDeviceBackend(image, read_only=False,
                                 adf_info=detect_adf(image))
    backend.open()
    yield backend
    backend.close()


@pytest.fixture
def calls(monkeypatch):
    """The host calls fsync() made, in order."""
    log = []
    monkeypatch.setattr(os, "fsync", lambda fd: log.append(("fsync", fd)))
    return log


def _fake_fcntl(calls, fcntl_error=None, ioctl_error=None, full_fsync=True):
    def fcntl(fd, cmd, arg=0):
        calls.append(("fcntl", fd, cmd))
        if fcntl_error is not None:
            raise OSError(fcntl_error, os.strerror(fcntl_error))
        return 0

    def ioctl(fd, request, arg=0):
        calls.append(("ioctl", fd, request))
        if ioctl_error is not None:
            raise OSError(ioctl_error, os.strerror(ioctl_error))
        return 0

    module = SimpleNamespace(fcntl=fcntl, ioctl=ioctl)
    if full_fsync:
        module.F_FULLFSYNC = F_FULLFSYNC
    return module


def _fsync_on(backend, monkeypatch, platform, fake_fcntl, st_mode=None):
    """Run backend.fsync() as if on `platform`, and on nothing else."""
    with monkeypatch.context() as m:
        m.setattr(driver_runtime.sys, "platform", platform)
        m.setattr(driver_runtime, "fcntl", fake_fcntl, raising=False)
        if st_mode is not None:
            m.setattr(driver_runtime.os, "fstat",
                      lambda fd: SimpleNamespace(st_mode=st_mode))
        backend.fsync()


def _fd(backend):
    return backend.blkdev.img_file.fobj.fileno()


def test_macos_image_gets_full_fsync(backend, calls, monkeypatch):
    _fsync_on(backend, monkeypatch, "darwin", _fake_fcntl(calls))
    assert calls == [("fcntl", _fd(backend), F_FULLFSYNC)]


@REFUSALS
def test_macos_falls_back_to_fsync_where_refused(backend, calls, monkeypatch,
                                                 refusal):
    # SMB shares and some other filesystems don't implement F_FULLFSYNC.
    _fsync_on(backend, monkeypatch, "darwin",
              _fake_fcntl(calls, fcntl_error=refusal))
    fd = _fd(backend)
    assert calls == [("fcntl", fd, F_FULLFSYNC), ("fsync", fd)]


def test_macos_full_fsync_write_error_fails_every_later_fsync(
        backend, calls, monkeypatch):
    # A failed write is not a refusal: a fallback fsync() could succeed
    # with the data lost.
    fake = _fake_fcntl(calls, fcntl_error=errno.EIO)
    for _ in range(2):
        with pytest.raises(OSError) as caught:
            _fsync_on(backend, monkeypatch, "darwin", fake)
        assert caught.value.errno == errno.EIO
    assert calls == [("fcntl", _fd(backend), F_FULLFSYNC)]


def test_macos_failed_fallback_fsync_fails_every_later_fsync(
        backend, calls, monkeypatch):
    def fsync(fd):
        calls.append(("fsync", fd))
        raise OSError(errno.EIO, os.strerror(errno.EIO))

    monkeypatch.setattr(os, "fsync", fsync)
    fake = _fake_fcntl(calls, fcntl_error=errno.ENOTSUP)
    for _ in range(2):
        with pytest.raises(OSError) as caught:
            _fsync_on(backend, monkeypatch, "darwin", fake)
        assert caught.value.errno == errno.EIO
    fd = _fd(backend)
    assert calls == [("fcntl", fd, F_FULLFSYNC), ("fsync", fd)]


@pytest.mark.parametrize("st_mode", [stat.S_IFCHR | 0o640, stat.S_IFBLK | 0o640],
                         ids=["rdisk", "disk"])
def test_macos_disk_device_flushes_the_drive_cache(backend, calls, monkeypatch,
                                                   st_mode):
    # F_FULLFSYNC is for files on a filesystem; fsync() writes out a block
    # device's buffer cache and does nothing for a character device.
    _fsync_on(backend, monkeypatch, "darwin", _fake_fcntl(calls), st_mode=st_mode)
    fd = _fd(backend)
    assert calls == [("fsync", fd), ("ioctl", fd, DKIOCSYNCHRONIZECACHE)]


@REFUSALS
def test_macos_disk_device_without_cache_flush(backend, calls, monkeypatch,
                                               refusal):
    _fsync_on(backend, monkeypatch, "darwin",
              _fake_fcntl(calls, ioctl_error=refusal),
              st_mode=stat.S_IFCHR | 0o640)
    fd = _fd(backend)
    assert calls == [("fsync", fd), ("ioctl", fd, DKIOCSYNCHRONIZECACHE)]


def test_macos_disk_device_cache_flush_error_is_raised(backend, calls,
                                                       monkeypatch):
    with pytest.raises(OSError) as caught:
        _fsync_on(backend, monkeypatch, "darwin",
                  _fake_fcntl(calls, ioctl_error=errno.EIO),
                  st_mode=stat.S_IFBLK | 0o640)
    assert caught.value.errno == errno.EIO


def test_macos_without_full_fsync_uses_fsync(backend, calls, monkeypatch):
    _fsync_on(backend, monkeypatch, "darwin",
              _fake_fcntl(calls, full_fsync=False))
    assert calls == [("fsync", _fd(backend))]


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_other_hosts_use_fsync(backend, calls, monkeypatch, platform):
    # Linux fsync() and Windows FlushFileBuffers already flush the drive.
    _fsync_on(backend, monkeypatch, platform, _fake_fcntl(calls))
    assert calls == [("fsync", _fd(backend))]


@pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS")
def test_real_full_fsync_on_macos(backend, monkeypatch):
    import fcntl

    made = []
    real_fcntl = fcntl.fcntl

    def spy(fd, cmd, *args):
        result = real_fcntl(fd, cmd, *args)
        made.append((fd, cmd))
        return result

    monkeypatch.setattr(fcntl, "fcntl", spy)
    backend.write_blocks(0, b"x" * backend.block_size)
    backend.sync()
    backend.fsync()
    assert made == [(_fd(backend), fcntl.F_FULLFSYNC)]
