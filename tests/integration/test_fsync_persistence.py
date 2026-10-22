"""fsync() on a real handler: the data is on the image while the file is open.

A fresh bridge in another process (one emulator per process) reads a copy
of the image taken after fsync() and before release().
"""

import os
import shutil
import subprocess
import sys

import pytest

from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge
from amifuse.rdb_inspect import detect_adf
from tests.fixtures.paths import FFS_DRIVER, REPO_ROOT

pytestmark = pytest.mark.integration

DATA = b"fsync must reach the image\n" * 400

_READER = """
import os, sys
from pathlib import Path
from amifuse.fuse_fs import HandlerBridge
from amifuse.rdb_inspect import detect_adf
image, driver, size = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
bridge = HandlerBridge(image, driver, read_only=True, adf_info=detect_adf(image))
try:
    opened = bridge.open_file("/fsync.txt", os.O_RDONLY)
    if opened is None:
        sys.exit("fsync.txt is not on the image")
    data = bridge.read_handle_at(opened[0], 0, size)
    bridge.close_file(opened[0])
    sys.stdout.write(data.hex())
finally:
    bridge.close()
"""


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


def _read_in_fresh_bridge(image, driver):
    env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    result = subprocess.run(
        [sys.executable, "-c", _READER, str(image), str(driver), str(len(DATA) + 16)],
        capture_output=True, text=True, timeout=50, env=env, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return bytes.fromhex(result.stdout.strip().splitlines()[-1])


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
    bridge = HandlerBridge(image, driver, read_only=False,
                           adf_info=detect_adf(image))
    try:
        ops = AmigaFuseFS(bridge, debug=False, icons=False)
        fh = ops.create("/fsync.txt", 0o644)
        assert ops.write("/fsync.txt", DATA, 0, fh) == len(DATA)
        assert ops.fsync("/fsync.txt", 0, fh) == 0
        snapshot = tmp_path / ("snapshot" + image.suffix)
        _snapshot(image, snapshot)
        # The handle stays usable after the volume flush.
        assert ops.write("/fsync.txt", b"tail", len(DATA), fh) == 4
        ops.release("/fsync.txt", fh)
        bridge.flush_volume()
    finally:
        bridge.close()

    assert _read_in_fresh_bridge(snapshot, driver) == DATA
    assert _read_in_fresh_bridge(image, driver) == DATA + b"tail"
