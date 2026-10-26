"""fsyncdir() on a real handler: a rename is on the image once it returns.

The test saves a file the safe way (write a temporary file, fsync() it,
rename it over the target, fsync() the directory), copies the image while
the writing bridge is still open, and lists the copy with a fresh bridge.
"""

import os
import shutil

import pytest

from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration

HANDLERS = [
    ("pfs.hdf", "pfs3aio"),
    ("sfs.hdf", "SmartFilesystem"),
]


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


def _root_names(image, driver):
    bridge = HandlerBridge(image, driver, read_only=True,
                           adf_info=detect_adf(image))
    try:
        return {entry["name"] for entry in bridge.list_dir_path("/")}
    finally:
        bridge.close()


@pytest.mark.parametrize("image_name,driver_name", HANDLERS,
                         ids=[d for _, d in HANDLERS])
def test_fsyncdir_puts_a_rename_on_the_image(image_name, driver_name,
                                              fixture_root, tmp_path):
    source = fixture_root / "fixtures" / "readonly" / image_name
    driver = fixture_root / "drivers" / driver_name
    if not source.exists() or not driver.exists():
        pytest.skip(f"Missing {image_name} or {driver_name}")
    image = tmp_path / image_name
    shutil.copy2(source, image)
    snapshot = tmp_path / ("snapshot-" + image_name)

    bridge = HandlerBridge(image, driver, read_only=False,
                           adf_info=detect_adf(image))
    try:
        ops = AmigaFuseFS(bridge, debug=False, icons=False)
        fh = ops.create("/tmp.txt", 0o644)
        assert ops.write("/tmp.txt", b"x" * 1000, 0, fh) == 1000
        assert ops.fsync("/tmp.txt", 0, fh) == 0
        ops.release("/tmp.txt", fh)
        assert ops.rename("/tmp.txt", "/final.txt") == 0
        dh = ops.opendir("/")
        try:
            assert ops.fsyncdir("/", 0, dh) == 0
        finally:
            ops.releasedir("/", dh)
        _snapshot(image, snapshot)
    finally:
        bridge.close()

    names = _root_names(snapshot, driver)
    assert "final.txt" in names
    assert "tmp.txt" not in names
