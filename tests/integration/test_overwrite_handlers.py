"""Overwriting a file, and opening a file another handle holds, on real
handlers in write mode.

The calls are the ones tests/unit/test_bffs_overwrite.py makes on a
scripted BFFS: these handlers empty a file at ACTION_FINDOUTPUT and shrink
it with ACTION_SET_FILE_SIZE, so the zero-length write before ACTION_END
must change nothing. The tests call the AmigaFuseFS callbacks directly,
as a mount would, on copies of the fixture images and on an ADF that
xdftool formats for the bundled FastFileSystem. No host FUSE driver is
needed.
"""

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
AMITOOLS_ROOT = REPO_ROOT / "amitools"
BUNDLED_FFS = REPO_ROOT / "amifuse" / "drivers" / "FastFileSystem.OS3"

OLD = bytes((i * 7) % 251 for i in range(3000))
NEW = b"new data\n" * 11

HANDLERS = [
    ("pfs.hdf", "pfs3aio"),
    ("sfs.hdf", "SmartFilesystem"),
    ("ofs.adf", "FastFileSystem"),
    ("bundled", "FastFileSystem.OS3"),
]


def _bundled_image(tmp_path):
    image = tmp_path / "ffs.adf"
    subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(image),
         "format", "Overwrite", "DOS3"],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        check=True, capture_output=True)
    return image, BUNDLED_FFS


@pytest.fixture(params=HANDLERS, ids=[f"{i}-{d}" for i, d in HANDLERS])
def ops(request, fixture_root, tmp_path):
    image_name, driver_name = request.param
    if image_name == "bundled":
        image, driver = _bundled_image(tmp_path)
    else:
        source = fixture_root / "fixtures" / "readonly" / image_name
        driver = fixture_root / "drivers" / driver_name
        if not source.exists() or not driver.exists():
            pytest.skip(f"Missing {image_name} or {driver_name}")
        image = tmp_path / image_name
        shutil.copy2(source, image)
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    fs = AmigaFuseFS(bridge)
    fs.handler_name = driver_name
    try:
        yield fs
    finally:
        bridge.close()


def _write_file(ops, path, data):
    fh = ops.create(path, 0o644)
    try:
        assert ops.write(path, data, 0, fh) == len(data)
    finally:
        ops.release(path, fh)


def _read_file(ops, path):
    fh = ops.open(path, os.O_RDONLY)
    try:
        return ops.read(path, 1 << 16, 0, fh)
    finally:
        ops.release(path, fh)


@pytest.mark.parametrize("new", [NEW, OLD + NEW], ids=["shorter", "longer"])
def test_overwrite_through_open_handle(ops, new):
    """WinFsp's overwrite: an open without O_TRUNC, ftruncate(fh, 0), then
    the new data."""
    _write_file(ops, "/o.bin", OLD)
    fh = ops.open("/o.bin", os.O_RDWR)
    try:
        assert ops.truncate("/o.bin", 0, fh) == 0
        assert ops.write("/o.bin", new, 0, fh) == len(new)
    finally:
        ops.release("/o.bin", fh)

    assert ops.getattr("/o.bin")["st_size"] == len(new)
    assert _read_file(ops, "/o.bin") == new


def test_truncate_by_path_then_write(ops):
    _write_file(ops, "/p.bin", OLD)
    assert ops.truncate("/p.bin", 0) == 0
    assert ops.getattr("/p.bin")["st_size"] == 0

    fh = ops.open("/p.bin", os.O_WRONLY)
    try:
        assert ops.write("/p.bin", NEW, 0, fh) == len(NEW)
    finally:
        ops.release("/p.bin", fh)
    assert _read_file(ops, "/p.bin") == NEW


@pytest.mark.parametrize("data", [b"", NEW], ids=["nothing", "data"])
def test_truncating_open(ops, data):
    """An O_TRUNC open that writes nothing empties the file; one that
    writes leaves just that."""
    _write_file(ops, "/t.bin", OLD)
    fh = ops.open("/t.bin", os.O_WRONLY | os.O_TRUNC)
    try:
        if data:
            assert ops.write("/t.bin", data, 0, fh) == len(data)
    finally:
        ops.release("/t.bin", fh)

    assert ops.getattr("/t.bin")["st_size"] == len(data)
    assert _read_file(ops, "/t.bin") == data


def test_created_file_that_writes_nothing_is_empty(ops):
    ops.release("/e.bin", ops.create("/e.bin", 0o644))

    assert ops.getattr("/e.bin")["st_size"] == 0
    assert _read_file(ops, "/e.bin") == b""


def test_file_held_by_create_is_busy(ops):
    """Every handler refuses to open a file again while create() holds it
    (ERROR_OBJECT_IN_USE), so open() and truncate() by path fail with
    EBUSY rather than ENOENT."""
    fh = ops.create("/c.bin", 0o644)
    try:
        ops.write("/c.bin", NEW, 0, fh)
        for call in (lambda: ops.open("/c.bin", os.O_RDWR),
                     lambda: ops.truncate("/c.bin", 10)):
            with pytest.raises(FuseOSError) as exc_info:
                call()
            assert exc_info.value.args[0] == errno.EBUSY
    finally:
        ops.release("/c.bin", fh)
    assert _read_file(ops, "/c.bin") == NEW


def test_truncate_by_path_beside_a_writer(ops):
    """pfs3aio refuses a second write handle while one is open, so
    truncate() by path is EBUSY there; the other handlers allow it."""
    _write_file(ops, "/w.bin", OLD)
    fh = ops.open("/w.bin", os.O_RDWR)
    try:
        if ops.handler_name == "pfs3aio":
            with pytest.raises(FuseOSError) as exc_info:
                ops.truncate("/w.bin", 10)
            assert exc_info.value.args[0] == errno.EBUSY
        else:
            assert ops.truncate("/w.bin", 10) == 0
    finally:
        ops.release("/w.bin", fh)
