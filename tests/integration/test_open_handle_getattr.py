"""getattr() while a file handle is open, on real handlers in write mode.

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
    ("ofs.adf", "FastFileSystem"),
    ("emu68k.hdf", "FastFileSystem"),
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


def _write_file(ops, path, size):
    fh = ops.create(path, 0o644)
    ops.write(path, b"x" * size, 0, fh)
    ops.release(path, fh)


def test_shared_handle_reports_handler_size_and_mode(ops):
    """FINDINPUT and FINDUPDATE handles leave the file visible to the handler."""
    _write_file(ops, "/f.bin", 3000)
    closed = ops.getattr("/f.bin")
    assert closed["st_size"] == 3000

    fh = ops.open("/f.bin", os.O_RDONLY)
    try:
        for attrs in (ops.getattr("/f.bin"), ops.getattr("/f.bin", fh)):
            assert attrs["st_size"] == 3000
            assert attrs["st_mode"] == closed["st_mode"]
    finally:
        ops.release("/f.bin", fh)

    fh = ops.open("/f.bin", os.O_RDWR)
    try:
        ops.write("/f.bin", b"y" * 500, 3000, fh)
        attrs = ops.getattr("/f.bin", fh)
        assert attrs["st_size"] == 3500
        assert attrs["st_mode"] == closed["st_mode"]
    finally:
        ops.release("/f.bin", fh)


@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_RDWR], ids=["O_RDONLY", "O_RDWR"])
def test_renamed_file_leaves_no_entry_under_its_old_name(ops, flags):
    """A shared handle does not keep the name it was opened under alive."""
    _write_file(ops, "/a.bin", 3000)
    fh = ops.open("/a.bin", flags)
    try:
        if flags == os.O_RDWR:
            ops.write("/a.bin", b"w" * 10, 0, fh)
        ops.rename("/a.bin", "/b.bin")
        with pytest.raises(FuseOSError) as exc_info:
            ops.getattr("/a.bin")
        assert exc_info.value.args[0] == errno.ENOENT
        assert ops.getattr("/b.bin")["st_size"] == 3000
    finally:
        ops.release("/b.bin", fh)


def test_shared_handle_answers_under_the_name_it_was_opened_with(ops):
    """WinFsp goes on passing the old name for a handle after a rename."""
    _write_file(ops, "/w.bin", 3000)
    fh = ops.open("/w.bin", os.O_RDWR)
    try:
        ops.rename("/w.bin", "/v.bin")
        assert ops.getattr("/w.bin", fh)["st_size"] == 3000
        ops.write("/w.bin", b"w" * 500, 3000, fh)
        assert ops.getattr("/w.bin", fh)["st_size"] == 3500
    finally:
        ops.release("/w.bin", fh)
    assert ops.getattr("/v.bin")["st_size"] == 3500


def test_exclusive_handle_follows_a_file_rename(ops):
    """libfuse renames a file that is open when unlinked to .fuse_hidden*.

    pfs3aio and SmartFilesystem rename a file held exclusively;
    FastFileSystem refuses, and then nothing moves.
    """
    fh = ops.create("/c.bin", 0o644)
    name = "/c.bin"
    try:
        ops.write(name, b"c" * 2000, 0, fh)
        try:
            ops.rename("/c.bin", "/.fuse_hidden0001")
            name = "/.fuse_hidden0001"
        except FuseOSError:
            pass
        assert ops.getattr(name)["st_size"] == 2000
        assert ops.getattr(name, fh)["st_size"] == 2000
        ops.write(name, b"c" * 500, 2000, fh)
        assert ops.getattr(name, fh)["st_size"] == 2500
        if name != "/c.bin":
            with pytest.raises(FuseOSError) as exc_info:
                ops.getattr("/c.bin")
            assert exc_info.value.args[0] == errno.ENOENT
    finally:
        ops.release(name, fh)
    assert ops.getattr(name)["st_size"] == 2500
    assert not ops._pinned_stats


def test_exclusive_handle_follows_a_directory_rename(ops):
    ops.mkdir("/dir", 0o755)
    fh = ops.create("/dir/f.bin", 0o644)
    try:
        ops.write("/dir/f.bin", b"f" * 700, 0, fh)
        ops.rename("/dir", "/dir2")
        assert ops.getattr("/dir2/f.bin")["st_size"] == 700
        assert ops.getattr("/dir2/f.bin", fh)["st_size"] == 700
        with pytest.raises(FuseOSError) as exc_info:
            ops.getattr("/dir/f.bin")
        assert exc_info.value.args[0] == errno.ENOENT
        ops.write("/dir2/f.bin", b"f" * 300, 700, fh)
        assert ops.getattr("/dir2/f.bin", fh)["st_size"] == 1000
    finally:
        ops.release("/dir2/f.bin", fh)
    assert ops.getattr("/dir2/f.bin")["st_size"] == 1000
    assert not ops._pinned_stats


def test_delete_deferred_after_a_rename_runs_at_the_last_release(ops):
    """WinFsp passes release() the name a file was opened under."""
    _write_file(ops, "/w.bin", 3000)
    h1 = ops.open("/w.bin", os.O_RDWR)
    ops.rename("/w.bin", "/v.bin")
    h2 = ops.open("/v.bin", os.O_RDONLY)
    assert ops.unlink("/v.bin") == 0  # in use: deferred to release()
    ops.release("/v.bin", h2)
    assert ops.bridge.stat_path("/v.bin") is not None
    ops.release("/w.bin", h1)
    assert ops.bridge.stat_path("/v.bin") is None
    assert not ops._pending_deletes


def test_delete_deferred_before_a_directory_rename_follows_it(ops):
    """The delete runs under the new path, and a file created later under
    the old one survives its release().
    """
    ops.mkdir("/d", 0o755)
    _write_file(ops, "/d/x", 3000)
    fh = ops.open("/d/x", os.O_RDWR)
    assert ops.unlink("/d/x") == 0  # in use: deferred to release()
    ops.rename("/d", "/e")
    ops.release("/d/x", fh)
    assert ops.bridge.stat_path("/e/x") is None
    assert not ops._pending_deletes

    ops.mkdir("/d", 0o755)
    _write_file(ops, "/d/x", 100)
    assert ops.bridge.stat_path("/d/x")["size"] == 100
    assert ops.getattr("/d/x")["st_size"] == 100


def test_exclusive_handle_reports_written_size(ops):
    """FINDOUTPUT handles hide the file from the handler until release()."""
    _write_file(ops, "/f.bin", 3000)
    fh = ops.open("/f.bin", os.O_WRONLY | os.O_TRUNC)
    try:
        assert ops.bridge.stat_path("/f.bin") is None
        assert ops.getattr("/f.bin", fh)["st_size"] == 0
        ops.write("/f.bin", b"z" * 1234, 0, fh)
        assert ops.getattr("/f.bin", fh)["st_size"] == 1234
        with pytest.raises(FuseOSError) as exc_info:
            ops.getattr("/missing.bin")
        assert exc_info.value.args[0] == errno.ENOENT
    finally:
        ops.release("/f.bin", fh)
    assert ops.getattr("/f.bin")["st_size"] == 1234

    fh = ops.create("/new.bin", 0o644)
    try:
        ops.write("/new.bin", b"w" * 2000, 0, fh)
        assert ops.getattr("/new.bin", fh)["st_size"] == 2000
    finally:
        ops.release("/new.bin", fh)
    assert ops.getattr("/new.bin")["st_size"] == 2000
