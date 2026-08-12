import pytest

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk
from amitools.vamos.disk import DiskImage

from amifuse.driver_runtime import BlockDeviceBackend


def _make_rdb(path):
    raw = RawBlockDevice(str(path), read_only=False, block_bytes=512)
    raw.create(320)
    rdisk = RDisk(raw)
    rdisk.create(DiskGeometry(10, 1, 32), rdb_cyls=1)
    rdisk.add_partition(FSString("DH0"), (1, 9))
    raw.flush()
    rdisk.close()
    raw.close()


def test_backend_shares_exclusive_lock_with_vamos(tmp_path):
    image_path = tmp_path / "disk.hdf"
    _make_rdb(image_path)
    backend = BlockDeviceBackend(image_path)
    backend.open()

    assert backend.exclusive is True
    with pytest.raises(IOError, match="exclusively lock"):
        DiskImage(image_path).open()

    backend.close()
    assert backend.exclusive is False
    image = DiskImage(image_path).open()
    image.close()
