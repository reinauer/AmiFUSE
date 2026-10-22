"""fsync() on a real handler: the data is on the image while the file is open.

The test copies the image after fsync() and before release(), and reads the
copy with a fresh bridge once the writing bridge has closed.
"""

import os
import shutil

import pytest

from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge
from amifuse.rdb_inspect import detect_adf
from tests.fixtures.paths import FFS_DRIVER

pytestmark = pytest.mark.integration

DATA = b"fsync must reach the image\n" * 400


def _snapshot(image, copy):
    # The writing session locks one byte past the end of the image, so
    # read exactly its size rather than up to end of file.
    remaining = image.stat().st_size
    fd = os.open(image, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        with open(copy, "wb") as out:
            while remaining:
                chunk = os.read(fd, min(remaining, 1 << 20))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
    finally:
        os.close(fd)


def _read_back(image, driver):
    bridge = HandlerBridge(image, driver, read_only=True,
                           adf_info=detect_adf(image))
    try:
        opened = bridge.open_file("/fsync.txt", os.O_RDONLY)
        assert opened is not None, "fsync.txt is not on the image"
        data = bridge.read_handle_at(opened[0], 0, len(DATA) + 16)
        bridge.close_file(opened[0])
        return data
    finally:
        bridge.close()


@pytest.fixture(params=["pfs3", "ofs"])
def writable_image(request, tmp_path):
    if request.param == "pfs3":
        image = request.getfixturevalue("pfs3_image")
        driver = request.getfixturevalue("pfs3_driver")
    else:
        image = request.getfixturevalue("ofs_adf_image")
        if FFS_DRIVER is None or not FFS_DRIVER.exists():
            pytest.skip("FastFileSystem handler not found")
        driver = FFS_DRIVER
    work = tmp_path / ("work" + image.suffix)
    shutil.copy2(image, work)
    return work, driver


def test_fsync_data_is_on_the_image_before_release(writable_image, tmp_path):
    image, driver = writable_image
    snapshot = tmp_path / ("snapshot" + image.suffix)
    bridge = HandlerBridge(image, driver, read_only=False,
                           adf_info=detect_adf(image))
    try:
        ops = AmigaFuseFS(bridge, debug=False, icons=False)
        fh = ops.create("/fsync.txt", 0o644)
        assert ops.write("/fsync.txt", DATA, 0, fh) == len(DATA)
        assert ops.fsync("/fsync.txt", 0, fh) == 0
        _snapshot(image, snapshot)
        # The handle stays usable after the volume flush.
        assert ops.write("/fsync.txt", b"tail", len(DATA), fh) == 4
        ops.release("/fsync.txt", fh)
        bridge.flush_volume()
    finally:
        bridge.close()

    assert _read_back(snapshot, driver) == DATA
    assert _read_back(image, driver) == DATA + b"tail"
