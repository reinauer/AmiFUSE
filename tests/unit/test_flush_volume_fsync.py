"""flush_volume() must ask the host OS to write the image to stable storage.

fsync() and a clean unmount report the data as written. Python's flush()
only moves it into the host's cache, which a power loss or an OS crash
still loses.
"""

import os
import threading
from types import SimpleNamespace

import pytest

from amifuse import driver_runtime
from amifuse import fuse_fs as fs
from amifuse.driver_runtime import BlockDeviceBackend
from amifuse.rdb_inspect import ADF_DD_SIZE, OffsetBlockDevice, detect_adf

FLUSH_PKT = 0x2100
ACK = (-1, 0)  # DOSTRUE: the handler wrote its buffers
NAK = (0, 0)


@pytest.fixture
def adf(tmp_path):
    image = tmp_path / "disk.adf"
    image.write_bytes(b"DOS\0" + bytes(ADF_DD_SIZE - 4))
    return image


@pytest.fixture
def events(monkeypatch):
    """What reached the image, in order: the flush reply, sync, fsync."""
    log = []
    real_fsync = os.fsync

    def fsync(fd):
        log.append(("fsync", fd))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    return log


def _bridge(backend, events, reply=ACK):
    bridge = fs.HandlerBridge.__new__(fs.HandlerBridge)
    bridge._debug = False
    bridge._lock = threading.RLock()
    bridge.state = SimpleNamespace(crashed=False)
    bridge.launcher = SimpleNamespace(send_flush=lambda state: (FLUSH_PKT, 0))

    def run_until_reply(pkt_addr, label, **wait):
        assert pkt_addr == FLUSH_PKT
        events.append("reply")
        return reply

    bridge._run_until_reply = run_until_reply
    sync = backend.sync

    def logged_sync():
        events.append("sync")
        sync()

    backend.sync = logged_sync
    bridge.backend = backend
    return bridge


def _backend(image, read_only):
    backend = BlockDeviceBackend(image, read_only=read_only,
                                 adf_info=detect_adf(image))
    backend.open()
    return backend


@pytest.mark.parametrize("wrap", [False, True], ids=["image", "mbr-partition"])
def test_flush_fsyncs_the_image_after_the_flush_reply(adf, events, wrap):
    backend = _backend(adf, read_only=False)
    try:
        if wrap:
            # An RDB inside an MBR partition is addressed through a wrapper.
            backend.blkdev = OffsetBlockDevice(backend.blkdev, 0,
                                               backend.blkdev.num_blocks)
        fd = getattr(backend.blkdev, "base", backend.blkdev).img_file.fobj.fileno()
        backend.write_blocks(0, b"x" * backend.block_size)
        _bridge(backend, events).flush_volume()
        assert events == ["reply", "sync", ("fsync", fd)]
    finally:
        backend.close()


def test_read_only_mount_never_fsyncs(adf, events):
    backend = _backend(adf, read_only=True)
    try:
        _bridge(backend, events).flush_volume()
        assert events == ["reply", "sync"]
    finally:
        backend.close()


def test_unacknowledged_flush_does_not_fsync(adf, events):
    backend = _backend(adf, read_only=False)
    try:
        with pytest.raises(OSError, match="ACTION_FLUSH"):
            _bridge(backend, events, reply=NAK).flush_volume()
        assert events == ["reply", "sync"]
    finally:
        backend.close()


def test_physical_disk_relies_on_its_own_flush(adf, events, monkeypatch):
    # A Windows physical disk is a DiskStream without a descriptor; its
    # flush() already calls FlushFileBuffers.
    backend = _backend(adf, read_only=False)
    try:
        monkeypatch.setattr(driver_runtime, "is_windows_disk", lambda path: True)
        _bridge(backend, events).flush_volume()
        assert events == ["reply", "sync"]
    finally:
        backend.close()
