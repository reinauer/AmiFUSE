"""Refused and deferred rmdir() on real handlers.

- rmdir() of a directory that holds an open, unlinked file next to a live
  entry fails with ENOTEMPTY, and the directory stays visible (#168).
- A deferred rmdir() leaves the handles open on other files usable on
  SmartFilesystem, and an in-use rmdir() with a file open does not stop
  pfs3aio (#129).
- rmdir() of a directory that holds a file that is open, not unlinked,
  fails with ENOTEMPTY while the directory is open too, and the directory
  stays visible after releasedir() (#197).
- rmdir() still defers behind unlinked open files whose names the handler
  stores differently, and fails with EBUSY, leaving the directory visible,
  when a lock that an open file keeps on the directory it was renamed out
  of refuses it.

The tests call the AmigaFuseFS callbacks directly, as a mount would, on
copies of the fixture images or on an FFS floppy made with xdftool for the
bundled FastFileSystem. No host FUSE driver is needed.
"""

import contextlib
import errno
import os
import shutil

import pytest

from amifuse import driver_resolver
from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf
from tests.integration.test_bundled_ffs import _xdftool

PFS = ("pfs.hdf", "pfs3aio")
SFS = ("sfs.hdf", "SmartFilesystem")
OFS = ("ofs.adf", "FastFileSystem")


def _fixture(handler):
    return pytest.param(handler, marks=pytest.mark.integration, id=handler[1])


BUNDLED_FFS = pytest.param(None, id="bundled-FastFileSystem")


@pytest.fixture
def open_ops(request, tmp_path):
    """Open AmigaFuseFS over a copy of a fixture image, or over a new FFS
    floppy with the bundled FastFileSystem for None."""
    stack = contextlib.ExitStack()

    def open_ops(handler):
        if handler is None:
            from tests.integration.conftest import _machine68k_works

            if not _machine68k_works():
                pytest.skip("machine68k CPU not functional")
            image = tmp_path / "ffs.adf"
            _xdftool(image, "create", "+", "format", "FFSTest", "ffs")
            driver = driver_resolver.BUNDLED_DRIVER
        else:
            fixture_root = request.getfixturevalue("fixture_root")
            image_name, driver_name = handler
            source = fixture_root / "fixtures" / "readonly" / image_name
            driver = fixture_root / "drivers" / driver_name
            if not source.exists() or not driver.exists():
                pytest.skip(f"Missing {image_name} or {driver_name}")
            image = tmp_path / image_name
            shutil.copy2(source, image)
        bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
        stack.callback(bridge.close)
        return AmigaFuseFS(bridge)

    with stack:
        yield open_ops


def _make(ops, path, data):
    fh = ops.create(path, 0o644)
    ops.write(path, data, 0, fh)
    ops.release(path, fh)


def _errno_of(call, *args):
    with pytest.raises(FuseOSError) as exc_info:
        call(*args)
    return exc_info.value.args[0]


def _listed(ops, path):
    return sorted(e["name"] for e in ops.bridge.list_dir_path(path))


def _shown(ops, path):
    parent, name = path.rsplit("/", 1)
    return bool(ops.getattr(path)["st_mode"] & 0o040000) and name in ops.readdir(parent or "/", None)


@pytest.mark.parametrize("handler", [_fixture(OFS), BUNDLED_FFS, _fixture(PFS), _fixture(SFS)])
def test_rmdir_with_a_live_entry_next_to_an_unlinked_open_file_is_not_empty(open_ops, handler):
    ops = open_ops(handler)
    ops.mkdir("/g", 0o755)
    _make(ops, "/g/w", b"w" * 100)
    _make(ops, "/g/z", b"z" * 100)
    hz = ops.open("/g/z", os.O_RDONLY)
    assert ops.unlink("/g/z") == 0

    assert _errno_of(ops.rmdir, "/g") == errno.ENOTEMPTY
    ops.release("/g/z", hz)

    assert ops._pending_deletes == set()
    assert ops._pending_dir_deletes == {}
    assert _shown(ops, "/g")
    assert _listed(ops, "/g") == ["w"]


@pytest.mark.parametrize("handler", [_fixture(SFS), _fixture(PFS), _fixture(OFS)])
def test_deferred_rmdir_leaves_another_open_file_usable(open_ops, handler):
    ops = open_ops(handler)
    _make(ops, "/keep.txt", b"keep-" * 20)
    hk = ops.open("/keep.txt", os.O_RDWR)
    ops.mkdir("/d", 0o755)
    _make(ops, "/d/x", b"x" * 100)
    hx = ops.open("/d/x", os.O_RDONLY)
    assert ops.unlink("/d/x") == 0
    assert ops.rmdir("/d") == 0

    ops.release("/d/x", hx)

    assert ops.read("/keep.txt", 10, 0, hk) == b"keep-keep-"
    assert ops.write("/keep.txt", b"K", 0, hk) == 1
    ops.release("/keep.txt", hk)
    assert "d" not in _listed(ops, "/")
    assert ops._pending_deletes == set()


@pytest.mark.parametrize("handler", [_fixture(PFS), _fixture(SFS)])
def test_in_use_rmdir_two_levels_down_keeps_the_handler_answering(open_ops, handler):
    ops = open_ops(handler)
    ops.mkdir("/g", 0o755)
    ops.mkdir("/g/d", 0o755)
    _make(ops, "/g/d/y", b"y" * 10)
    _make(ops, "/g/d/x", b"x" * 100)
    hx = ops.open("/g/d/x", os.O_RDONLY)
    assert ops.unlink("/g/d/x") == 0

    assert _errno_of(ops.rmdir, "/g/d") == errno.ENOTEMPTY
    assert ops.read("/g/d/x", 5, 0, hx) == b"xxxxx"
    ops.release("/g/d/x", hx)

    assert _listed(ops, "/g/d") == ["y"]
    assert _shown(ops, "/g/d")
    ops.bridge.flush_volume()


@pytest.mark.parametrize("handler", [_fixture(PFS), _fixture(SFS), _fixture(OFS)])
def test_rmdir_of_an_open_directory_holding_an_open_file_is_not_empty(open_ops, handler):
    ops = open_ops(handler)
    ops.mkdir("/d", 0o755)
    _make(ops, "/d/x", b"x" * 100)
    hx = ops.open("/d/x", os.O_RDONLY)  # open, not unlinked
    dh = ops.opendir("/d")

    assert _errno_of(ops.rmdir, "/d") == errno.ENOTEMPTY
    ops.releasedir("/d", dh)
    ops.release("/d/x", hx)

    assert ops._pending_deletes == set()
    assert _shown(ops, "/d")
    assert _listed(ops, "/d") == ["x"]


@pytest.mark.parametrize("handler", [_fixture(OFS), BUNDLED_FFS, _fixture(PFS), _fixture(SFS)])
def test_rmdir_defers_behind_unlinked_open_files_the_handler_names_differently(open_ops, handler):
    ops = open_ops(handler)
    ops.mkdir("/g", 0o755)
    # Stored as "?uro.txt", and cut to the handler's name length.
    names = ["/g/\u20acuro.txt", "/g/a_rather_long_file_name_past_thirty_chars.txt"]
    handles = []
    for name in names:
        _make(ops, name, b"n" * 50)
        handles.append(ops.open(name, os.O_RDONLY))
        assert ops.unlink(name) == 0

    assert ops.rmdir("/g") == 0
    for name, fh in zip(names, handles):
        ops.release(name, fh)

    assert "g" not in _listed(ops, "/")
    assert ops._pending_deletes == set()
    assert ops._pending_dir_deletes == {}


@pytest.mark.parametrize("handler", [_fixture(PFS), _fixture(SFS), _fixture(OFS)])
def test_rmdir_refused_by_a_lock_an_open_file_holds_is_busy(open_ops, handler, monkeypatch):
    ops = open_ops(handler)
    ops.mkdir("/d", 0o755)
    _make(ops, "/d/x", b"x" * 100)
    hx = ops.open("/d/x", os.O_RDONLY)  # keeps its lock on /d when the new directory can't be locked
    move = ops._move_parent_lock

    def new_directory_unlockable(entry, dir_path):
        with monkeypatch.context() as m:
            m.setattr(ops.bridge, "locate_path", lambda path: (0, 0, []))
            m.setattr(ops.bridge, "locate", lambda lock, name: (0, 0))
            move(entry, dir_path)

    with monkeypatch.context() as m:
        m.setattr(ops, "_move_parent_lock", new_directory_unlockable)
        assert ops.rename("/d/x", "/x") == 0
    dh = ops.opendir("/d")

    assert _errno_of(ops.rmdir, "/d") == errno.EBUSY
    ops.releasedir("/d", dh)
    assert ops._pending_deletes == set()
    assert _shown(ops, "/d")

    ops.release("/x", hx)
    assert ops.rmdir("/d") == 0
    assert "d" not in _listed(ops, "/")
