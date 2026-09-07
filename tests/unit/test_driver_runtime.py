from contextlib import ExitStack, suppress
from unittest.mock import Mock

import pytest

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk
from amitools.vamos.disk import DiskImage

from amifuse.driver_runtime import BlockDeviceBackend


def _make_rdb(path):
    raw = RawBlockDevice(str(path), read_only=False, block_bytes=512)
    with ExitStack() as cleanup:
        raw.create(320)
        cleanup.callback(raw.close)
        rdisk = RDisk(raw)
        cleanup.callback(rdisk.close)
        rdisk.create(DiskGeometry(10, 1, 32), rdb_cyls=1)
        rdisk.add_partition(FSString("DH0"), (1, 9))
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
