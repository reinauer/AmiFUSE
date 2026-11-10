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

    monkeypatch.setattr(ops, "_list_names", refuse_listing)
    assert ops.getpath("/canonical/mixedname") == "/Canonical/MixedName"


@pytest.mark.parametrize("readdir_plus", [False, True])
def test_getpath_lists_only_the_parent_of_a_refused_lock(ops, monkeypatch, readdir_plus):
    # With READDIR_PLUS readdir() gives (name, attributes, offset) tuples;
    # getpath compares names alone.
    if ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops._readdir_plus = readdir_plus
    ops.mkdir("/Canonical", 0o755)
    _write(ops, "/Canonical/MixedName", b"data")
    stat_path, list_names = ops.bridge.stat_path, ops._list_names
    listed = []

    def stat(path):
        return None if path == "/Canonical/mixedname" else stat_path(path)

    def listing(path, names):
        listed.append(path)
        return list_names(path, names)

    monkeypatch.setattr(ops.bridge, "stat_path", stat)
    monkeypatch.setattr(ops, "_list_names", listing)
    assert ops.getpath("/canonical/mixedname") == "/Canonical/MixedName"
    assert listed == ["/Canonical"]


@pytest.mark.parametrize("recreate", [False, True])
def test_windows_delete_access_after_file_rename(open_rename, recreate):
    from amifuse.winfsp_fuse import DELETE_OK

    ops = open_rename
    ops._winfsp = True
    _write(ops, "/before", OLD)
    handle = ops.open("/before", os.O_RDONLY)
    try:
        assert ops.rename("/before", "/after") == 0
        if recreate:
            _write(ops, "/before", NEW)
        assert ops.access("/before", DELETE_OK) == 0
        assert ops.unlink("/before") == 0
    finally:
        ops.release("/before", handle)
    assert "after" not in _on_disk(ops)
    if recreate:
        assert _read(ops, "/before") == NEW
    assert not ops._pending_deletes


@pytest.mark.parametrize("recreate", [False, True])
def test_windows_delete_access_after_directory_rename(ops, recreate):
    from amifuse.winfsp_fuse import DELETE_OK

    ops._winfsp = True
    assert ops.mkdir("/before", 0o755) == 0
    handle = ops.opendir("/before")
    try:
        assert ops.rename("/before", "/after") == 0
        if recreate:
            assert ops.mkdir("/before", 0o755) == 0
        assert ops.access("/before", DELETE_OK) == 0
        assert ops.rmdir("/before") == 0
    finally:
        ops.releasedir("/before", handle)
    assert "after" not in _on_disk(ops)
    assert ("before" in _on_disk(ops)) == recreate
    assert not ops._pending_deletes


def test_getpath_of_a_created_file_does_not_list_its_parent(ops, monkeypatch):
    # WinFsp calls getpath right after create(), while the new file is open
    # for writing and the handler may refuse its lock. Listing the parent
    # there made every copy onto a mount quadratic in the folder's size.
    if ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops.mkdir("/Canonical", 0o755)
    list_names = ops._list_names

    def refuse_listing(*args):
        raise AssertionError("getpath listed the parent of a created file")

    handle = ops.create("/Canonical/NewFile", 0o644)
    try:
        monkeypatch.setattr(ops, "_list_names", refuse_listing)
        assert ops.getpath("/canonical/newfile", handle) == "/Canonical/NewFile"
    finally:
        monkeypatch.setattr(ops, "_list_names", list_names)
        ops.release("/Canonical/NewFile", handle)


def test_getpath_does_not_trust_an_opened_handles_spelling(ops, monkeypatch):
    # open() may name the file in any case; only the disk has its spelling.
    if ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops.mkdir("/Canonical", 0o755)
    _write(ops, "/Canonical/MixedName", b"data")
    handle = ops.open("/canonical/mixedname", os.O_RDONLY)
    stat_path = ops.bridge.stat_path
    try:
        monkeypatch.setattr(ops.bridge, "stat_path", lambda path: (
            None if path == "/Canonical/mixedname" else stat_path(path)))
        assert ops.getpath("/canonical/mixedname", handle) == "/Canonical/MixedName"
    finally:
        ops.release("/canonical/mixedname", handle)


def _hard_link_image(tmp_path):
    """An FFS floppy with lf, a hard link to TargetFile.txt, and L, a hard
    link to the directory TargetDir, which holds inner.txt."""
    import struct
    from tests.integration.test_ls_recursive_ffs import BLOCK, _edit_block, _xdftool

    image = tmp_path / "links.adf"
    content, empty = tmp_path / "content.txt", tmp_path / "empty.txt"
    content.write_bytes(b"target\n")
    empty.write_bytes(b"")
    _xdftool(image, "create", "+", "format", "Links", "ffs",
             "+", "write", str(content), "TargetFile.txt",
             "+", "write", str(empty), "lf",
             "+", "makedir", "TargetDir", "+", "write", str(content), "TargetDir/inner.txt",
             "+", "makedir", "L")
    data = bytearray(image.read_bytes())

    def header(name, sec_type):
        for key in range(len(data) // BLOCK):
            block = data[key * BLOCK:(key + 1) * BLOCK]
            if (struct.unpack(">I", block[:4])[0] == 2
                    and struct.unpack(">i", block[-4:])[0] == sec_type
                    and block[433:433 + block[432]] == name):
                return key
        raise AssertionError(f"no header for {name!r}")

    for link, target, sec_type, link_type in (
            (b"lf", b"TargetFile.txt", -3, -4),  # ST_FILE -> ST_LINKFILE
            (b"L", b"TargetDir", 2, 4)):         # ST_USERDIR -> ST_LINKDIR
        link_key, target_key = header(link, sec_type), header(target, sec_type)

        def to_link(block, target_key=target_key, link_type=link_type):
            struct.pack_into(">i", block, BLOCK - 4, link_type)
            struct.pack_into(">I", block, BLOCK - 11 * 4, target_key)  # real_entry

        def link_back(block, link_key=link_key):
            struct.pack_into(">I", block, BLOCK - 10 * 4, link_key)  # next_link

        _edit_block(data, link_key, to_link)
        _edit_block(data, target_key, link_back)
    image.write_bytes(data)
    return image


def test_exclusive_create_of_an_existing_name_fails_on_winfsp(ops):
    # WinFsp sends CREATE_NEW to create() without opening the name first,
    # and the handler matches a name in any case: without the check, this
    # create emptied CaseX.
    if ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops._winfsp = True
    _write(ops, "/CaseX", b"keep me")
    for name in ("/CaseX", "/CASEX"):
        assert _errno(ops.create, name, 0o644) == errno.EEXIST
    assert _read(ops, "/casex") == b"keep me"
    assert "CaseX" in _on_disk(ops)


def test_exclusive_create_of_a_held_file_fails_on_winfsp(ops):
    # A file still open from its create() can't be examined; its pin
    # tells that it exists.
    if ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops._winfsp = True
    fh = ops.create("/Held", 0o644)
    try:
        assert ops.write("/Held", b"data", 0, fh) == 4
        assert _errno(ops.create, "/HELD", 0o644) == errno.EEXIST
    finally:
        ops.release("/Held", fh)
    assert _read(ops, "/Held") == b"data"


def test_a_name_whose_delete_is_pending_is_not_taken_for_existing(ops):
    # A pending delete hides its file, and create() takes the name over.
    ops._winfsp = True
    _write(ops, "/Pending", b"old")
    fh = ops.open("/Pending", os.O_RDONLY)
    try:
        assert ops.unlink("/Pending") == 0
        if "/Pending" not in ops._pending_deletes:
            pytest.skip("handler deletes an open file at once")
        ops._refuse_existing("/PENDING" if ops._name_fold else "/Pending")
    finally:
        ops.release("/Pending", fh)



@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
@pytest.mark.parametrize("old_first", [True, False], ids=["old-first", "new-first"])
@pytest.mark.parametrize("spelling", ["/Pend", "/PEND"])
def test_create_at_the_name_of_a_file_deleted_while_open(ops, request, winfsp,
                                                         old_first, spelling):
    # The handler deletes the file only at its last release(), and it keeps
    # its name on disk until then; a create of the name opened the held
    # file, which the handler refused, and create() failed with EIO (#439).
    # The held file moves to a temporary name, which frees the name.
    if spelling != "/Pend" and ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops._winfsp = winfsp
    _write(ops, "/Pend", OLD)
    old = ops.open("/Pend", os.O_RDONLY)
    new = None
    try:
        assert ops.unlink("/Pend") == 0
        if "/Pend" not in ops._pending_deletes:
            pytest.skip("handler deletes an open file at once")
        if request.node.callspec.params["ops"][1] == BUNDLED:
            # It refuses to rename an open file: the name is free only
            # once the file is deleted, at its release().
            assert _errno(ops.create, spelling, 0o644) == errno.EACCES
            ops.release("/Pend", old)
            old = None
        new = ops.create(spelling, 0o644)
        assert ops.write(spelling, NEW, 0, new) == len(NEW)
        if old is not None:
            assert ops.read("/Pend", 1 << 16, 0, old) == OLD
            first, second = (old, new) if old_first else (new, old)
            ops.release(spelling if first is new else "/Pend", first)
            ops.release(spelling if second is new else "/Pend", second)
        else:
            ops.release(spelling, new)
        new = old = None
    finally:
        if new is not None:
            ops.release(spelling, new)
        if old is not None:
            ops.release("/Pend", old)
    assert {n for n in _on_disk(ops) if n.lower() == "pend"} == {spelling[1:]}
    _no_temp_names(ops)
    assert _read(ops, spelling) == NEW
    assert not ops._pending_deletes


def test_mkdir_at_the_name_of_a_file_deleted_while_open(open_rename):
    ops = open_rename
    ops._winfsp = True
    _write(ops, "/Pend", OLD)
    old = ops.open("/Pend", os.O_RDONLY)
    try:
        assert ops.unlink("/Pend") == 0
        if "/Pend" not in ops._pending_deletes:
            pytest.skip("handler deletes an open file at once")
        assert ops.mkdir("/Pend", 0o755) == 0
        assert ops.read("/Pend", 1 << 16, 0, old) == OLD
    finally:
        ops.release("/Pend", old)
    assert {n for n in _on_disk(ops) if n.lower() == "pend"} == {"Pend"}
    _no_temp_names(ops)
    assert ops.getattr("/Pend")["st_mode"] & 0o170000 == 0o040000
    assert not ops._pending_deletes

def test_getpath_names_a_hard_link_not_its_target(tmp_path):
    # FastFileSystem resolves a hard link when it locks it, so Examine on
    # the lock names the target. getpath must give the link's own name, or
    # WinFsp rejects every open through it.
    image = _hard_link_image(_bundled_image(tmp_path).parent)
    bridge = HandlerBridge(image, BUNDLED_DRIVER, read_only=False,
                           adf_info=detect_adf(image))
    try:
        ops = AmigaFuseFS(bridge)
        assert ops._name_fold is not None
        assert ops.getpath("/lf") == "/lf"
        assert ops.getpath("/LF") == "/lf"
        assert ops.getpath("/l") == "/L"
        assert ops.getpath("/l/INNER.TXT") == "/L/inner.txt"
        assert ops.getpath("/targetfile.txt") == "/TargetFile.txt"
    finally:
        bridge.close()


def test_windows_reader_uses_independent_offsets(ops):
    ops._winfsp = True
    writer = ops.create("/busy", 0o644)
    reader = None
    try:
        ops.write("/busy", b"first", 0, writer)
        reader = ops.open("/busy", os.O_RDONLY)
        assert ops.getattr("/busy", reader)["st_size"] == 5
        assert ops.read("/busy", 2, 1, reader) == b"ir"
        ops.write("/busy", b"tail", 5, writer)
        assert ops.read("/busy", 100, 0, reader) == b"firsttail"
        ops.release("/busy", reader)
        reader = None
        assert ops.getattr("/busy")["st_size"] == 9
    finally:
        if reader is not None:
            ops.release("/busy", reader)
        ops.release("/busy", writer)
    assert _read(ops, "/busy") == b"firsttail"


def test_windows_metadata_open_allows_rename(ops):
    ops._winfsp = True
    _write(ops, "/before", b"keep")
    handle = ops.open("/before", os.O_RDONLY)
    try:
        ops.rename("/before", "/after")
        assert ops.getattr("/before", handle)["st_size"] == 4
        assert ops.read("/before", 100, 0, handle) == b"keep"
    finally:
        ops.release("/before", handle)
    assert "before" not in ops.readdir("/", None)
    assert _read(ops, "/after") == b"keep"


def test_windows_logical_reader_keeps_replaced_content(ops):
    # The callback also supports replacement requested with POSIX rename
    # semantics; ordinary WinFsp MoveFileEx refuses an open destination.
    ops._winfsp = True
    _write(ops, "/new", NEW)
    _write(ops, "/old", OLD)
    handle = ops.open("/old", os.O_RDONLY)
    try:
        ops.rename("/new", "/old")
        assert _read(ops, "/old") == NEW
        assert ops.read("/old", 100, 0, handle) == OLD
    finally:
        ops.release("/old", handle)
    _no_temp_names(ops)


def test_windows_metadata_open_allows_truncate(ops):
    ops._winfsp = True
    _write(ops, "/before", b"keep")
    handle = ops.open("/before", os.O_RDONLY)
    try:
        assert ops.truncate("/before", 0, handle) == 0
        assert ops.getattr("/before", handle)["st_size"] == 0
        assert ops.read("/before", 100, 0, handle) == b""
    finally:
        ops.release("/before", handle)
    assert ops.getattr("/before")["st_size"] == 0
    assert not ops._fh_cache
    assert _read(ops, "/before") == b""


def test_windows_reader_truncates_through_this_mounts_writer(ops):
    # CREATE_ALWAYS with GENERIC_READ while this mount holds the file open
    # for writing: pfs3aio refuses a second writer, so the truncate goes
    # through the one already open.
    ops._winfsp = True
    _write(ops, "/w", NEW)
    writer = ops.open("/w", os.O_RDWR)
    reader = None
    try:
        reader = ops.open("/w", os.O_RDONLY)
        assert ops.truncate("/w", 0, reader) == 0
        assert ops.getattr("/w", reader)["st_size"] == 0
        assert ops.read("/w", 100, 0, reader) == b""
    finally:
        if reader is not None:
            ops.release("/w", reader)
        ops.release("/w", writer)
    assert not ops._fh_cache
    assert _read(ops, "/w") == b""


def test_windows_read_only_concurrent_first_reads_share_one_handle(ops, monkeypatch):
    # A read-only mount runs multithreaded. A read that arrives while the
    # first one is still opening the data handle must use that handle, not
    # open (and leak) another under the first one's cached position.
    import threading
    from unittest.mock import Mock

    _write(ops, "/data", NEW)
    ops._winfsp = True
    monkeypatch.setattr(ops.bridge, "_write_enabled", False)
    opened = Mock(wraps=ops.bridge.open_file_reply)
    closed = Mock(wraps=ops.bridge.close_file)
    monkeypatch.setattr(ops.bridge, "open_file_reply", opened)
    monkeypatch.setattr(ops.bridge, "close_file", closed)

    first_in, first_go = threading.Event(), threading.Event()
    real_read_at = ops.bridge.read_handle_at

    def held_read_at(*args):
        if not first_in.is_set():
            first_in.set()
            assert first_go.wait(10)
        return real_read_at(*args)

    monkeypatch.setattr(ops.bridge, "read_handle_at", held_read_at)
    handle = ops.open("/data", os.O_RDONLY)

    class WatchedLock:
        """The entry's lock; tells when a second read waits for it."""
        def __init__(self, lock):
            self.lock, self.waiting = lock, threading.Event()

        def __enter__(self):
            if not self.lock.acquire(blocking=False):
                self.waiting.set()
                self.lock.acquire()

        def __exit__(self, *exc):
            self.lock.release()

    entry = ops._fh_cache[handle]
    entry["lock"] = watched = WatchedLock(entry["lock"])
    results = {}

    def read(name, offset):
        results[name] = ops.read("/data", 1000, offset, handle)

    first = threading.Thread(target=read, args=("first", 0))
    second = threading.Thread(target=read, args=("second", 1000))
    try:
        first.start()
        assert first_in.wait(10)
        second.start()
        assert watched.waiting.wait(10)
    finally:
        first_go.set()
        first.join(10)
        second.join(10)
    assert results == {"first": NEW[:1000], "second": NEW[1000:2000]}
    # and a sequential read after them reads on from the second
    assert ops.read("/data", 1000, 2000, handle) == NEW[2000:3000]
    assert opened.call_count == 1
    ops.release("/data", handle)
    assert closed.call_count == 1
    assert not ops._fh_cache


def test_windows_read_only_reuses_data_handle(ops, monkeypatch):
    from unittest.mock import Mock

    _write(ops, "/data", NEW)
    ops._winfsp = True
    monkeypatch.setattr(ops.bridge, "_write_enabled", False)
    opened = Mock(wraps=ops.bridge.open_file_reply)
    closed = Mock(wraps=ops.bridge.close_file)
    monkeypatch.setattr(ops.bridge, "open_file_reply", opened)
    monkeypatch.setattr(ops.bridge, "close_file", closed)
    handle = ops.open("/data", os.O_RDONLY)
    assert opened.call_count == 0
    for offset in (0, 128, 256, 7):
        assert ops.read("/data", 128, offset, handle) == NEW[offset:offset + 128]
    assert opened.call_count == 1
    assert closed.call_count == 0
    ops.release("/data", handle)
    assert closed.call_count == 1
    assert not ops._fh_cache


def test_windows_pinned_reader_avoids_metadata_packets(ops, monkeypatch):
    ops._winfsp = True
    writer = ops.create("/busy", 0o644)
    reader = None
    try:
        ops.write("/busy", NEW, 0, writer)
        reader = ops.open("/busy", os.O_RDONLY)
        def unexpected(*args):
            pytest.fail("pinned reader must not examine or enumerate")
        monkeypatch.setattr(ops.bridge, "stat_path", unexpected)
        monkeypatch.setattr(ops.bridge, "list_dir_path", unexpected)
        for offset in (0, 128, 256, 7):
            assert ops.read("/busy", 128, offset, reader) == NEW[offset:offset + 128]
    finally:
        if reader is not None:
            ops.release("/busy", reader)
        ops.release("/busy", writer)


def test_directory_renamed_through_its_handle_lists_with_attributes(ops):
    # WinFsp lists a directory handle under the path it was opened with,
    # and asks getattr() under that path for any entry given without
    # attributes. After a rename through the handle that path is stale, so
    # every entry failed and the listing came back empty (#427). With
    # READDIR_PLUS granted, readdir() gives each entry's attributes.
    # test_windows_renamed_directory checks the same through WinFsp.
    ops._winfsp = True
    ops._readdir_plus = True
    ops.mkdir("/L2a", 0o755)
    _write(ops, "/L2a/f", b"data")
    handle = ops.opendir("/L2a")
    try:
        assert ops.rename("/L2a", "/L2b") == 0
        listing = ops.readdir("/L2a", handle)
        assert _errno(ops.getattr, "/L2a/f") == errno.ENOENT
        attrs = {name: st for name, st, offset in listing}
        assert sorted(attrs) == [".", "..", "f"]
        assert attrs["f"]["st_size"] == 4
        assert attrs["f"]["st_mode"] & 0o170000 == 0o100000
        assert attrs["."]["st_mode"] & 0o170000 == 0o040000
        assert attrs[".."] == ops.getattr("/")
        # and by the new path, as a second handle lists it
        assert sorted(name for name, _, _ in ops.readdir("/L2b", None)) == [".", "..", "f"]
    finally:
        ops.releasedir("/L2a", handle)


def test_listing_gives_a_pending_grow_its_logical_length(ops):
    # truncate() defers the zeros of a grow; getattr() reports the length
    # the file will have, and so must the listing WinFsp now takes it from.
    ops._winfsp = True
    ops._readdir_plus = True
    _write(ops, "/grown", b"abc")
    fh = ops.open("/grown", os.O_RDWR)
    try:
        assert ops.truncate("/grown", 8192, fh) == 0
        ops._dir_cache.clear()
        attrs = {name: st for name, st, _ in ops.readdir("/", None)}
        assert attrs["grown"]["st_size"] == ops.getattr("/grown")["st_size"] == 8192
    finally:
        ops.release("/grown", fh)


def test_listing_leaves_a_soft_link_to_getattr(tmp_path):
    # The handler refuses a lock on a soft link, so getattr() fails on it,
    # and WinFsp drops an entry whose getattr() fails. Its listed
    # attributes, a directory's, would list it as one nothing can open.
    import struct
    from tests.integration.test_ls_recursive_ffs import BLOCK, _edit_block, _xdftool

    image = _bundled_image(tmp_path)
    empty = tmp_path / "empty"
    empty.write_bytes(b"")
    _xdftool(image, "write", str(empty), "sl", "+", "makedir", "Target")
    data = bytearray(image.read_bytes())
    key = next(k for k in range(len(data) // BLOCK)
               if struct.unpack(">I", data[k * BLOCK:k * BLOCK + 4])[0] == 2
               and data[k * BLOCK + 433:k * BLOCK + 433 + data[k * BLOCK + 432]] == b"sl")

    def to_soft_link(block):
        struct.pack_into(">i", block, BLOCK - 4, 3)  # ST_SOFTLINK
        block[24:24 + 7] = b"Target\0"  # the link's path, in the hash table

    _edit_block(data, key, to_soft_link)
    image.write_bytes(data)
    bridge = HandlerBridge(image, BUNDLED_DRIVER, read_only=False,
                           adf_info=detect_adf(image))
    try:
        ops = AmigaFuseFS(bridge)
        ops._winfsp = True
        ops._readdir_plus = True
        assert any(e["name"] == "sl" and e["dir_type"] == 3
                   for e in bridge.list_dir_path("/"))
        attrs = {name: st for name, st, _ in ops.readdir("/", None)}
        assert "sl" in attrs and attrs["sl"] is None
        assert attrs["Target"]["st_mode"] & 0o170000 == 0o040000
    finally:
        bridge.close()


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


@pytest.mark.parametrize("replacement", [False, True])
def test_windows_readdir_follows_renamed_directory(ops, replacement):
    ops._winfsp = True
    assert ops.mkdir("/before", 0o755) == 0
    _write(ops, "/before/child", OLD)
    handle = ops.opendir("/before")
    try:
        assert ops.rename("/before", "/after") == 0
        if replacement:
            assert ops.mkdir("/before", 0o755) == 0
        assert "child" in ops.readdir("/before", handle)
        assert "child" not in ops.readdir("/before", None)
    finally:
        ops.releasedir("/before", handle)
    assert _read(ops, "/after/child") == OLD


@pytest.mark.parametrize("back", [False, True])
def test_windows_repeated_file_rename_uses_opened_name(open_rename, back):
    ops = open_rename
    ops._winfsp = True
    _write(ops, "/before", OLD)
    handle = ops.open("/before", os.O_RDONLY)
    target = "/before" if back else "/last"
    try:
        assert ops.rename("/before", "/after") == 0
        assert ops.rename("/before", target) == 0
        assert ops._fh_cache[handle]["path"] == target
    finally:
        ops.release("/before", handle)
    assert "after" not in _on_disk(ops)
    assert _read(ops, target) == OLD


@pytest.mark.parametrize("target", ["/before", "/last", "/BEFORE"])
def test_windows_repeated_directory_rename_uses_opened_name(ops, target):
    ops._winfsp = True
    assert ops.mkdir("/before", 0o755) == 0
    handle = ops.opendir("/before")
    try:
        assert ops.rename("/before", "/after") == 0
        assert ops.rename("/before", target) == 0
        assert ops._fh_cache[handle]["path"] == target
    finally:
        ops.releasedir("/before", handle)
    assert "after" not in _on_disk(ops)
    assert target[1:] in _on_disk(ops)
