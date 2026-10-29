"""rmdir() of a directory holding an unlinked open file, on real handlers.

pfs3aio refuses to delete such a directory with ERROR_OBJECT_IN_USE (202)
until the file is closed; SmartFilesystem answers ERROR_DIRECTORY_NOT_EMPTY
(216). Either way the delete is deferred to the file's release(), and the
path stays hidden only while its delete is pending.

The tests call the AmigaFuseFS callbacks directly, as a mount would, on
copies of the fixture images. No host FUSE driver is needed.
"""

import errno
import os
import shutil

import pytest

from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf

pytestmark = pytest.mark.integration

HANDLERS = [
    ("pfs.hdf", "pfs3aio"),
    ("sfs.hdf", "SmartFilesystem"),
]


@pytest.fixture(params=HANDLERS, ids=[f"{d}-{i}" for i, d in HANDLERS])
def ops(request, fixture_root, tmp_path):
    image_name, driver_name = request.param
    source = fixture_root / "fixtures" / "readonly" / image_name
    driver = fixture_root / "drivers" / driver_name
    if not source.exists() or not driver.exists():
        pytest.skip(f"Missing {image_name} or {driver_name}")
    image = tmp_path / image_name
    shutil.copy2(source, image)
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    try:
        yield AmigaFuseFS(bridge)
    finally:
        bridge.close()


def _on_disk(ops, path):
    return path.lstrip("/") in {e["name"] for e in ops.bridge.list_dir_path("/")}


def _unlink_and_rmdir_while_open(ops):
    """Leave /d/x unlinked but open, and /d deferred behind it; release it."""
    ops.mkdir("/d", 0o755)
    fh = ops.create("/d/x", 0o644)
    ops.write("/d/x", b"x" * 100, 0, fh)
    ops.release("/d/x", fh)
    fh = ops.open("/d/x", os.O_RDONLY)
    assert ops.unlink("/d/x") == 0
    assert ops.rmdir("/d") == 0
    with pytest.raises(FuseOSError) as exc_info:
        ops.getattr("/d")
    assert exc_info.value.args[0] == errno.ENOENT
    ops.release("/d/x", fh)


def test_deferred_rmdir_runs_at_release(ops):
    _unlink_and_rmdir_while_open(ops)

    assert not _on_disk(ops, "/d")
    assert ops._pending_deletes == set()
    assert ops._pending_dir_deletes == {}


def test_directory_made_again_is_visible(ops):
    _unlink_and_rmdir_while_open(ops)
    if _on_disk(ops, "/d"):
        # The deferred delete failed at release; a later rmdir() removes it.
        assert ops.rmdir("/d") == 0
        assert not _on_disk(ops, "/d")
    assert ops._pending_deletes == set()

    assert ops.mkdir("/d", 0o755) == 0
    assert ops.getattr("/d")["st_mode"] & 0o040000
    assert "d" in ops.readdir("/", None)
