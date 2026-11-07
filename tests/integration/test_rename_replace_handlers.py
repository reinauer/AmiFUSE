"""rename() onto an existing name, and of an open file, on real handlers.

AmigaDOS refuses a rename onto an existing name with ERROR_OBJECT_EXISTS,
while rename(2), Python's os.replace() and Windows' MoveFileEx with
MOVEFILE_REPLACE_EXISTING replace it. The tests call the AmigaFuseFS
callbacks directly, as a mount would, on copies of the fixture images and
on an ADF the bundled FastFileSystem serves. No host FUSE driver is needed.
"""

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from amifuse.fuse_fs import (
    ERROR_DISK_FULL,
    AmigaFuseFS,
    FuseOSError,
    HandlerBridge,
)
from amifuse.rdb_inspect import detect_adf

AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"
BUNDLED = "bundled FastFileSystem"

HANDLERS = [
    pytest.param(("pfs.hdf", "pfs3aio"), marks=pytest.mark.integration, id="pfs3aio"),
    pytest.param(("sfs.hdf", "SmartFilesystem"), marks=pytest.mark.integration,
                 id="SmartFilesystem"),
    pytest.param(("ofs.adf", "FastFileSystem"), marks=pytest.mark.integration,
                 id="FastFileSystem"),
    # Not integration-marked, as in test_bundled_ffs: the image is built here.
    pytest.param((None, BUNDLED), id="bundled"),
]

NEW = b"new data\n" * 300
OLD = b"old\n"


def _bundled_image(tmp_path: Path) -> Path:
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")
    image = tmp_path / "bundled.adf"
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(image), "format", "Gen", "DOS3"],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return image


@pytest.fixture(params=HANDLERS)
def ops(request, tmp_path):
    image_name, driver_name = request.param
    if driver_name == BUNDLED:
        image, driver = _bundled_image(tmp_path), BUNDLED_DRIVER
    else:
        fixture_root = request.getfixturevalue("fixture_root")
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


@pytest.fixture
def open_rename(ops, request):
    """ops, on a handler that renames an open file.

    The bundled v0.2 handler refuses with ERROR_OBJECT_IN_USE.
    """
    if request.node.callspec.params["ops"][1] == BUNDLED:
        pytest.skip("the bundled FastFileSystem refuses to rename an open file")
    return ops


def _write(ops, path, data):
    fh = ops.create(path, 0o644)
    assert ops.write(path, data, 0, fh) == len(data)
    ops.release(path, fh)


def _read(ops, path):
    fh = ops.open(path, os.O_RDONLY)
    try:
        return ops.read(path, 1 << 16, 0, fh)
    finally:
        ops.release(path, fh)


def _on_disk(ops, dir_path="/"):
    return {e["name"] for e in ops.bridge.list_dir_path(dir_path)}


def _errno(call, *args):
    with pytest.raises(FuseOSError) as exc_info:
        call(*args)
    return exc_info.value.args[0]


def _no_temp_names(ops, *dirs):
    for d in ("/",) + dirs:
        assert not [n for n in _on_disk(ops, d) if n.startswith(".amifuse-")], d


@pytest.mark.parametrize("src", ["/a", "/d/a"], ids=["same-dir", "other-dir"])
def test_rename_replaces_a_file(ops, src):
    """os.replace() semantics: the target holds the source's data, and the
    source name is gone."""
    ops.mkdir("/d", 0o755)
    _write(ops, src, NEW)
    _write(ops, "/b", OLD)

    assert ops.rename(src, "/b") == 0

    assert _read(ops, "/b") == NEW
    assert ops.getattr("/b")["st_size"] == len(NEW)
    assert _errno(ops.getattr, src) == errno.ENOENT
    assert "b" in _on_disk(ops) and "a" not in _on_disk(ops) | _on_disk(ops, "/d")
    _no_temp_names(ops, "/d")


def test_failed_rename_keeps_the_target(ops, monkeypatch):
    """When the source can't take the name once the target has moved aside,
    the target is renamed back."""
    _write(ops, "/a", NEW)
    _write(ops, "/b", OLD)
    rename_object = ops.bridge.rename_object
    calls = []

    def refuse_source_after_temp(src_lock, src_name, dst_lock, dst_name):
        calls.append((src_name, dst_name))
        if src_name == "a" and any(d.startswith(".amifuse-") for _, d in calls):
            return 0, ERROR_DISK_FULL
        return rename_object(src_lock, src_name, dst_lock, dst_name)

    monkeypatch.setattr(ops.bridge, "rename_object", refuse_source_after_temp)

    assert _errno(ops.rename, "/a", "/b") == errno.ENOSPC

    monkeypatch.undo()
    temp = calls[1][1]
    assert calls == [("a", "b"), ("b", temp), ("a", "b"), (temp, "b")]
    assert _read(ops, "/b") == OLD
    assert _read(ops, "/a") == NEW
    _no_temp_names(ops)


def test_failed_delete_puts_both_back(ops, monkeypatch):
    """When the replaced file can't be deleted, both names are put back."""
    _write(ops, "/a", NEW)
    _write(ops, "/b", OLD)
    deletes = []

    def refuse_delete(path):
        deletes.append(path)
        return 0, 222  # ERROR_DELETE_PROTECTED

    monkeypatch.setattr(ops.bridge, "delete_dir_atomic", refuse_delete)

    assert _errno(ops.rename, "/a", "/b") == errno.EACCES

    monkeypatch.undo()
    assert len(deletes) == 1 and deletes[0].startswith("/.amifuse-")
    assert _read(ops, "/b") == OLD
    assert _read(ops, "/a") == NEW
    _no_temp_names(ops)


NAME_LIMITS = {"pfs3aio": 31, "SmartFilesystem": 107, "FastFileSystem": 30, BUNDLED: 30}


def test_save_over_a_long_name(ops, request):
    """The handler keeps a name longer than it allows cut to its limit (#173).
    A save that renames a new file onto the long name replaces that file."""
    cut = "/" + "L" * NAME_LIMITS[request.node.callspec.params["ops"][1]]
    long = cut + "-extra"
    _write(ops, long, OLD)
    _write(ops, "/src", NEW)

    assert ops.rename("/src", long) == 0

    assert _read(ops, long) == NEW
    assert _on_disk(ops) >= {cut[1:]} and "src" not in _on_disk(ops)
    _no_temp_names(ops)


def test_rename_replaces_an_empty_directory(ops):
    ops.mkdir("/rn-s", 0o755)
    _write(ops, "/rn-s/f", OLD)
    ops.mkdir("/rn-t", 0o755)

    assert ops.rename("/rn-s", "/rn-t") == 0

    assert "rn-s" not in _on_disk(ops)
    assert _on_disk(ops, "/rn-t") == {"f"}
    assert _read(ops, "/rn-t/f") == OLD
    _no_temp_names(ops)


@pytest.mark.parametrize("src_dir, dst_dir, expected", [
    (False, True, errno.EISDIR),
    (True, False, errno.ENOTDIR),
    (True, True, errno.ENOTEMPTY),
], ids=["file-onto-dir", "dir-onto-file", "dir-onto-full-dir"])
def test_refused_replace_leaves_both(ops, src_dir, dst_dir, expected):
    for path, is_dir in (("/rn-s", src_dir), ("/rn-t", dst_dir)):
        if is_dir:
            ops.mkdir(path, 0o755)
            _write(ops, path + "/f", path.encode())
        else:
            _write(ops, path, path.encode())

    assert _errno(ops.rename, "/rn-s", "/rn-t") == expected

    assert {"rn-s", "rn-t"} <= _on_disk(ops)
    for path, is_dir in (("/rn-s", src_dir), ("/rn-t", dst_dir)):
        assert _read(ops, path + "/f" if is_dir else path) == path.encode()
    _no_temp_names(ops)


def test_open_target_is_replaced_and_deleted_at_release(open_rename):
    """An open target is replaced, as on POSIX: its handle still reads the
    old data, its temporary name stays hidden and is deleted at release."""
    ops = open_rename
    _write(ops, "/a", NEW)
    _write(ops, "/b", OLD)
    fh = ops.open("/b", os.O_RDONLY)

    assert ops.rename("/a", "/b") == 0

    assert _read(ops, "/b") == NEW
    assert ops.read("/b", 100, 0, fh) == OLD
    assert not [n for n in ops.readdir("/", None) if n.startswith(".amifuse-")]
    ops.release("/b", fh)
    _no_temp_names(ops)
    assert ops._pending_deletes == set()
    assert _read(ops, "/b") == NEW


def test_source_still_open_from_create(ops, request):
    """A file still open from create(), which the handler can't examine,
    replaces another. FastFileSystem refuses to rename a file held on an
    exclusive lock, and both files stay as they were."""
    _write(ops, "/b", OLD)
    fh = ops.create("/a", 0o644)
    ops.write("/a", NEW, 0, fh)
    driver = request.node.callspec.params["ops"][1]

    if driver in ("pfs3aio", "SmartFilesystem"):
        assert ops.rename("/a", "/b") == 0
        assert ops.getattr("/b")["st_size"] == len(NEW)
        ops.release("/b", fh)
        assert _read(ops, "/b") == NEW
        assert _errno(ops.getattr, "/a") == errno.ENOENT
    else:
        assert _errno(ops.rename, "/a", "/b") == errno.EBUSY
        ops.release("/a", fh)
        assert _read(ops, "/a") == NEW
        assert _read(ops, "/b") == OLD
    _no_temp_names(ops)


def test_file_moved_out_while_open_leaves_its_directory_removable(open_rename):
    """An open file renamed out of a directory takes its lock along, so the
    emptied directory is deleted at once, and the file still reads. The
    directory handle is open across the rmdir(), as for a WinFsp delete.
    A directory moved with a directory handle open has no lock to move."""
    ops = open_rename
    for d in ("/d", "/m", "/n"):
        ops.mkdir(d, 0o755)
    _write(ops, "/d/x", b"x" * 100)
    mh = ops.opendir("/m")
    assert ops.rename("/m", "/n/m") == 0
    assert ops.releasedir("/m", mh) == 0
    fh = ops.open("/d/x", os.O_RDONLY)

    assert ops.rename("/d/x", "/x") == 0
    dh = ops.opendir("/d")
    assert ops.rmdir("/d") == 0
    assert ops.releasedir("/d", dh) == 0

    assert "d" not in _on_disk(ops)
    assert ops._pending_deletes == set()
    assert ops.read("/x", 5, 0, fh) == b"xxxxx"
    assert ops.release("/x", fh) == 0
    assert _read(ops, "/x") == b"x" * 100
    assert _on_disk(ops, "/n") == {"m"}


@pytest.fixture
def winfsp_rename(ops, request, monkeypatch):
    """ops, called as WinFsp calls it, on a handler that renames a file open
    from create(). WinFsp passes each handle's open-time name to unlink()
    and release(), also after a rename through that handle."""
    if request.node.callspec.params["ops"][1] not in ("pfs3aio", "SmartFilesystem"):
        pytest.skip("FastFileSystem refuses to rename a file open from create()")
    monkeypatch.setattr(AmigaFuseFS, "_winfsp", True)
    return ops


DOOMED = b"renamed, then deleted on close\n"
KEEP = b"another file, keep me\n"


def _create_renamed(ops, old, new):
    """A file created as old, then renamed to new while its handle is open."""
    fh = ops.create(old, 0o644)
    assert ops.write(old, DOOMED, 0, fh) == len(DOOMED)
    assert ops.rename(old, new) == 0
    return fh


@pytest.mark.parametrize("file_at_old_name", [False, True],
                         ids=["old-name-free", "new-file-at-old-name"])
def test_delete_on_close_after_rename_through_the_handle(winfsp_rename, file_at_old_name):
    """A file renamed through its handle and then deleted on close is deleted,
    under its new name, and a file created at its old name meanwhile stays
    (#290). The calls are WinFsp's, from the issue's log."""
    ops = winfsp_rename
    fh = _create_renamed(ops, "/b-old.txt", "/b-new.txt")
    if file_at_old_name:
        _write(ops, "/b-old.txt", KEEP)

    assert ops.unlink("/b-old.txt") == 0
    assert ops.release("/b-old.txt", fh) == 0

    assert "b-new.txt" not in _on_disk(ops)
    if file_at_old_name:
        assert _read(ops, "/b-old.txt") == KEEP
    else:
        assert "b-old.txt" not in _on_disk(ops)
    assert ops._pending_deletes == set()


def test_delete_file_at_the_old_name_while_the_renamed_file_is_open(winfsp_rename):
    """DeleteFile() of a file at the old name holds its own handle there when
    its unlink() comes: that file is deleted, and the renamed one stays."""
    ops = winfsp_rename
    fh = _create_renamed(ops, "/b-old.txt", "/b-new.txt")
    _write(ops, "/b-old.txt", KEEP)
    dh = ops.open("/b-old.txt", os.O_RDONLY)

    assert ops.unlink("/b-old.txt") == 0
    assert ops.release("/b-old.txt", dh) == 0
    assert "b-old.txt" not in _on_disk(ops)
    assert ops.release("/b-old.txt", fh) == 0

    assert _read(ops, "/b-new.txt") == DOOMED
    assert ops._pending_deletes == set()


def test_delete_file_at_the_old_name_from_a_handle_a_failed_truncate_closed(winfsp_rename):
    """A handle a failed truncate() reopen left closed is still one on the old
    name: its delete-on-close deletes that file, not the renamed one."""
    ops = winfsp_rename
    fh = _create_renamed(ops, "/b-old.txt", "/b-new.txt")
    _write(ops, "/b-old.txt", KEEP)
    dh = ops.open("/b-old.txt", os.O_RDWR)
    entry = ops._fh_cache[dh]
    ops.bridge.close_file(entry["fh_addr"])
    if entry.get("parent_lock"):
        ops.bridge.free_lock(entry["parent_lock"])
    entry.update(fh_addr=0, parent_lock=0, closed=True)  # as _reopen_for_output

    assert ops.unlink("/b-old.txt") == 0
    assert ops.release("/b-old.txt", dh) == 0
    assert "b-old.txt" not in _on_disk(ops)
    assert ops.release("/b-old.txt", fh) == 0

    assert _read(ops, "/b-new.txt") == DOOMED
    assert ops._pending_deletes == set()


def test_delete_on_close_after_another_file_was_renamed_onto_the_old_name(winfsp_rename):
    """A save that renames the old version away through its handle, renames
    the new version onto its name, then deletes the old version on close
    keeps the new version. WinFsp names the new version's handle by the name
    it was opened under, so it can't be the one deleting."""
    ops = winfsp_rename
    _write(ops, "/doc.txt", DOOMED)
    old = ops.open("/doc.txt", os.O_RDWR)
    new = ops.create("/new.txt", 0o644)
    assert ops.write("/new.txt", KEEP, 0, new) == len(KEEP)
    assert ops.rename("/doc.txt", "/doc.bak") == 0
    assert ops.rename("/new.txt", "/doc.txt") == 0

    assert ops.unlink("/doc.txt") == 0
    assert ops.release("/doc.txt", old) == 0
    assert ops.release("/new.txt", new) == 0

    assert "doc.bak" not in _on_disk(ops)
    assert _read(ops, "/doc.txt") == KEEP
    assert ops._pending_deletes == set()


def test_delete_on_close_while_a_directory_renamed_from_the_name_is_open(winfsp_rename):
    """Only a file handle deletes a file: a directory handle renamed from the
    same name leaves the renamed file the one to delete."""
    ops = winfsp_rename
    ops.mkdir("/p", 0o755)
    dh = ops.opendir("/p")
    assert ops.rename("/p", "/q") == 0
    fh = _create_renamed(ops, "/p", "/r")

    assert ops.unlink("/p") == 0
    assert ops.release("/p", fh) == 0
    assert ops.releasedir("/p", dh) == 0

    assert "r" not in _on_disk(ops)
    assert "q" in _on_disk(ops)
    assert ops._pending_deletes == set()


def test_getpath_returns_stored_parent_and_child_spelling(ops):
    if ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops.mkdir("/Canonical", 0o755)
    _write(ops, "/Canonical/MixedName", b"data")
    assert ops.getpath("/") == "/"
    assert ops.getpath("/canonical/mixedname") == "/Canonical/MixedName"
    ops.rename("/Canonical/MixedName", "/Canonical/MIXEDNAME")
    assert ops.getpath("/canonical/mixedname") == "/Canonical/MIXEDNAME"
    assert _errno(ops.getpath, "/canonical/missing") == errno.ENOENT


def test_getpath_does_not_enumerate_unlocked_directories(ops, monkeypatch):
    if ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops.mkdir("/Canonical", 0o755)
    _write(ops, "/Canonical/MixedName", b"data")

    def refuse_listing(*args):
        raise AssertionError("getpath enumerated an unlocked directory")

    monkeypatch.setattr(ops, "readdir", refuse_listing)
    assert ops.getpath("/canonical/mixedname") == "/Canonical/MixedName"


def test_getpath_lists_only_the_parent_of_a_refused_lock(ops, monkeypatch):
    if ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops.mkdir("/Canonical", 0o755)
    _write(ops, "/Canonical/MixedName", b"data")
    stat_path, readdir = ops.bridge.stat_path, ops.readdir
    listed = []

    def stat(path):
        return None if path == "/Canonical/mixedname" else stat_path(path)

    def listing(path, fh):
        listed.append(path)
        return readdir(path, fh)

    monkeypatch.setattr(ops.bridge, "stat_path", stat)
    monkeypatch.setattr(ops, "readdir", listing)
    assert ops.getpath("/canonical/mixedname") == "/Canonical/MixedName"
    assert listed == ["/Canonical"]


def test_delete_renamed_directory_preserves_old_name(ops):
    ops._winfsp = True
    ops.mkdir("/before", 0o755)
    handle = ops.opendir("/before")
    assert ops.rename("/before", "/after") == 0
    ops.mkdir("/before", 0o755)
    assert ops.rmdir("/before") == 0
    assert ops.releasedir("/before", handle) == 0
    assert "before" in _on_disk(ops)
    assert "after" not in _on_disk(ops)
    assert ops._pending_deletes == set()
