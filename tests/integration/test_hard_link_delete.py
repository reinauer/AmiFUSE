"""Deleting a file while it is open through a hard link to it (#425).

FastFileSystem refuses to delete any name of a file while a handle opened
through another of its names is open (ERROR_OBJECT_IN_USE). WinFsp sends
unlink() at cleanup and discards its answer, so the delete has to wait for
the release() of that handle. The tests call the AmigaFuseFS callbacks
directly, as a mount would, on an ADF the bundled FastFileSystem serves.
xdftool can't write a hard link, so a file's header is turned into an
ST_LINKFILE block, as test_ls_recursive_ffs makes its directory link.
The handles are opened read/write: on WinFsp a read-only handle holds no
AmigaDOS lock (#408), so FastFileSystem deletes the file at once and the
link takes over its data.

pfs3aio refuses to delete the file's own name the same way, though not a
link's. The handle is found by the lock's fl_Key (see _parse_fib). Those
tests copy the fixture images and make the link with ACTION_MAKE_LINK.

Renaming one name of a file onto another of its names does nothing, as
rename(2) says; replacing the second name would delete it. Unless that
name was deleted while the file is open: then it is hidden, and the file
moves to it.
"""

import errno
import os
import shutil
import struct

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from amifuse.emu_thread import on_emu_thread
from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf
from tests.integration.test_ls_recursive_ffs import BLOCK, _edit_block, _xdftool

pytestmark = pytest.mark.timeout(120)

ST_FILE = -3
ST_LINKFILE = -4


@pytest.fixture(autouse=True)
def needs_cpu():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


def _file_header(data, name):
    for key in range(len(data) // BLOCK):
        block = data[key * BLOCK:(key + 1) * BLOCK]
        if (struct.unpack(">I", block[:4])[0] == 2
                and struct.unpack(">i", block[-4:])[0] == ST_FILE
                and block[433:433 + block[432]] == name):
            return key
    raise AssertionError(f"no file header for {name!r}")


def _link_image(tmp_path, link, target, *names):
    """An FFS floppy with each of names, a file or (ending in /) a directory,
    and link, a file hard link to target, which is one of them."""
    image = tmp_path / "links.adf"
    content, empty = tmp_path / "content.txt", tmp_path / "empty.txt"
    content.write_bytes(b"target\n")
    empty.write_bytes(b"")
    commands = []
    for name in names:
        if name.endswith("/"):
            commands += ["+", "makedir", name.rstrip("/")]
        else:
            commands += ["+", "write", str(content), name]
    _xdftool(image, "create", "+", "format", "Links", "ffs", *commands,
             "+", "write", str(empty), link)
    data = bytearray(image.read_bytes())
    link_key = _file_header(data, link.rsplit("/", 1)[-1].encode())
    target_key = _file_header(data, target.rsplit("/", 1)[-1].encode())

    def to_link(block):
        struct.pack_into(">i", block, BLOCK - 4, ST_LINKFILE)
        struct.pack_into(">I", block, BLOCK - 11 * 4, target_key)  # real_entry

    def link_back(block):
        struct.pack_into(">I", block, BLOCK - 10 * 4, link_key)  # next_link

    _edit_block(data, link_key, to_link)
    _edit_block(data, target_key, link_back)
    image.write_bytes(data)
    return image


def _mounted(image):
    bridge = HandlerBridge(image, BUNDLED_DRIVER, read_only=False,
                           adf_info=detect_adf(image))
    try:
        yield AmigaFuseFS(bridge)
    finally:
        bridge.close()


@pytest.fixture
def ops(tmp_path):
    """TargetFile.txt, lf, a hard link to it, and other.txt."""
    yield from _mounted(_link_image(tmp_path, "lf", "TargetFile.txt",
                                    "TargetFile.txt", "other.txt"))


@pytest.fixture
def folder_ops(tmp_path):
    """D/T.txt, and l, a hard link to it, outside D."""
    yield from _mounted(_link_image(tmp_path, "l", "D/T.txt", "D/", "D/T.txt"))


def _names(ops):
    ops._dir_cache.clear()
    return {n for n in ops.readdir("/", None) if n not in (".", "..")}


@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
@pytest.mark.parametrize("deleted, held", [("/TargetFile.txt", "/lf"),
                                           ("/lf", "/TargetFile.txt")])
def test_delete_waits_for_a_handle_open_through_a_link(ops, winfsp, deleted, held):
    ops._winfsp = winfsp
    fh = ops.open(held, os.O_RDWR)
    try:
        # Answered as done: WinFsp would discard a refusal, and the file goes
        # once the handle on its other name closes.
        assert ops.unlink(deleted) == 0
        assert os.path.basename(deleted) not in _names(ops)
        with pytest.raises(FuseOSError) as exc:
            ops.getattr(deleted)
        assert exc.value.errno == errno.ENOENT
        assert ops.read(held, 7, 0, fh) == b"target\n"
    finally:
        ops.release(held, fh)
    assert _names(ops) == {os.path.basename(held), "other.txt"}
    assert ops.bridge.stat_path(deleted) is None
    fh = ops.open(held, os.O_RDWR)
    try:
        assert ops.read(held, 7, 0, fh) == b"target\n"
    finally:
        ops.release(held, fh)


def test_delete_waits_for_the_last_handle_through_a_link(ops):
    ops._winfsp = True
    first = ops.open("/lf", os.O_RDWR)
    second = ops.open("/lf", os.O_RDWR)
    assert ops.unlink("/TargetFile.txt") == 0
    ops.release("/lf", first)
    assert ops.bridge.stat_path("/TargetFile.txt") is not None
    assert "TargetFile.txt" not in _names(ops)
    ops.release("/lf", second)
    assert ops.bridge.stat_path("/TargetFile.txt") is None


def test_delete_with_a_handle_on_both_names(ops):
    # The deleted name's own release() is refused while the link's handle is
    # open; the link's release() sends the delete again.
    ops._winfsp = True
    own = ops.open("/TargetFile.txt", os.O_RDWR)
    link = ops.open("/lf", os.O_RDWR)
    assert ops.unlink("/TargetFile.txt") == 0
    ops.release("/TargetFile.txt", own)
    assert "TargetFile.txt" not in _names(ops)
    assert ops.bridge.stat_path("/TargetFile.txt") is not None
    ops.release("/lf", link)
    assert ops.bridge.stat_path("/TargetFile.txt") is None
    assert _names(ops) == {"lf", "other.txt"}


def test_an_unrelated_open_file_does_not_hold_a_delete(ops):
    ops._winfsp = True
    fh = ops.open("/other.txt", os.O_RDWR)
    try:
        assert ops._link_holders("/TargetFile.txt") == set()
        assert ops.unlink("/TargetFile.txt") == 0
        assert ops.bridge.stat_path("/TargetFile.txt") is None
    finally:
        ops.release("/other.txt", fh)


def test_folder_delete_waits_for_a_file_held_through_a_link(folder_ops):
    # The file is hidden, so WinFsp finds the folder empty and deletes it;
    # the handler refuses while the file is there, so the folder's delete
    # waits for the file's, as for a file open under its own name.
    ops = folder_ops
    ops._winfsp = True
    fh = ops.open("/l", os.O_RDWR)
    try:
        assert ops.unlink("/D/T.txt") == 0
        assert ops.readdir("/D", None) == [".", ".."]
        assert ops.rmdir("/D") == 0
        assert "D" not in _names(ops)
        assert ops.bridge.stat_path("/D") is not None
    finally:
        ops.release("/l", fh)
    assert ops.bridge.stat_path("/D/T.txt") is None
    assert ops.bridge.stat_path("/D") is None
    assert _names(ops) == {"l"}
    fh = ops.open("/l", os.O_RDWR)
    try:
        assert ops.read("/l", 7, 0, fh) == b"target\n"
    finally:
        ops.release("/l", fh)


ACTION_MAKE_LINK = 1021
LINK_HARD = 0


@on_emu_thread
def _make_hard_link(bridge, directory, name, target):
    """Make hard link name in directory to target, as MakeLink() does."""
    with bridge._lock:
        if directory == "/":
            dir_lock, _ = bridge.locate(0, "")
            locks = [dir_lock]
        else:
            dir_lock, _, locks = bridge.locate_path(directory)
        target_lock, _, target_locks = bridge.locate_path(target)
        _, name_bptr = bridge._alloc_bstr(name)
        pkt_addr, _ = bridge.launcher.send_packet(
            bridge.state, ACTION_MAKE_LINK, [dir_lock, name_bptr, target_lock, LINK_HARD])
        reply = bridge._run_until_reply(pkt_addr, "make_link")
        for lock in reversed(locks + target_locks):
            bridge.free_lock(lock)
    assert reply is not None and reply[0], reply


@pytest.fixture(params=[
    pytest.param(("pfs.hdf", "pfs3aio"), marks=pytest.mark.integration, id="pfs3aio"),
    pytest.param(("ofs.adf", "FastFileSystem"), marks=pytest.mark.integration,
                 id="FastFileSystem"),
])
def handler_ops(request, tmp_path):
    """A copy of a fixture image with D/T.txt, E/T.txt, T.txt, l, a hard
    link to D/T.txt, and E/l, a hard link to T.txt."""
    image_name, driver_name = request.param
    fixture_root = request.getfixturevalue("fixture_root")
    source = fixture_root / "fixtures" / "readonly" / image_name
    driver = fixture_root / "drivers" / driver_name
    if not source.exists() or not driver.exists():
        pytest.skip(f"Missing {image_name} or {driver_name}")
    image = tmp_path / image_name
    shutil.copy2(source, image)
    bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
    try:
        ops = AmigaFuseFS(bridge)
        ops.mkdir("/D", 0o755)
        ops.mkdir("/E", 0o755)
        for path in ("/D/T.txt", "/E/T.txt", "/T.txt"):
            fh = ops.create(path, 0o644)
            ops.write(path, b"target\n", 0, fh)
            ops.release(path, fh)
        _make_hard_link(bridge, "/", "l", "/D/T.txt")
        _make_hard_link(bridge, "/E", "l", "/T.txt")
        yield ops
    finally:
        bridge.close()


@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
@pytest.mark.parametrize("deleted, held", [("/D/T.txt", "/l"), ("/T.txt", "/E/l")])
def test_delete_waits_for_a_handle_through_a_link_in_another_directory(
        handler_ops, winfsp, deleted, held):
    # pfs3aio examined the two names through locks at different addresses.
    ops = handler_ops
    ops._winfsp = winfsp
    fh = ops.open(held, os.O_RDWR)
    try:
        assert ops.unlink(deleted) == 0
        with pytest.raises(FuseOSError) as exc:
            ops.getattr(deleted)
        assert exc.value.errno == errno.ENOENT
        assert ops.read(held, 7, 0, fh) == b"target\n"
    finally:
        ops.release(held, fh)
    assert ops.bridge.stat_path(deleted) is None
    fh = ops.open(held, os.O_RDWR)
    try:
        assert ops.read(held, 7, 0, fh) == b"target\n"
    finally:
        ops.release(held, fh)


def test_a_file_of_the_same_name_does_not_hold_a_delete(handler_ops):
    ops = handler_ops
    fh = ops.open("/E/T.txt", os.O_RDWR)
    try:
        assert ops._link_holders("/D/T.txt") == set()
    finally:
        ops.release("/E/T.txt", fh)


def _rename_does_nothing(ops, winfsp, old, new):
    ops._winfsp = winfsp
    dirs = {path.rsplit("/", 1)[0] or "/" for path in (old, new)}

    def listings():
        return {d: sorted(e["name"] for e in ops.bridge.list_dir_path(d)) for d in dirs}

    before = listings()
    assert ops.rename(old, new) == 0
    assert listings() == before
    for path in (old, new):
        fh = ops.open(path, os.O_RDONLY)
        try:
            assert ops.read(path, 7, 0, fh) == b"target\n"
        finally:
            ops.release(path, fh)


@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
@pytest.mark.parametrize("old, new", [("/l", "/D/T.txt"), ("/D/T.txt", "/l")])
def test_a_rename_onto_another_name_of_the_file_does_nothing(handler_ops, winfsp, old, new):
    _rename_does_nothing(handler_ops, winfsp, old, new)


@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
@pytest.mark.parametrize("old, new", [("/lf", "/TargetFile.txt"), ("/TargetFile.txt", "/lf")])
def test_a_rename_onto_another_name_of_the_file_does_nothing_bundled(ops, winfsp, old, new):
    _rename_does_nothing(ops, winfsp, old, new)


def _rename_onto_a_deleted_name(ops, winfsp, held, deleted, refused=False):
    """Rename held, open, onto deleted, another name of its file, deleted
    first. A delete of the file's own name waits for that handle, hidden;
    a link's name (the cases with the file's own name held) is deleted at
    once, and the rename is a plain one. The file takes the name, or, where
    the handler refuses to rename an open file, keeps its own."""
    ops._winfsp = winfsp
    dirs = {path.rsplit("/", 1)[0] or "/" for path in (held, deleted)}
    fh = ops.open(held, os.O_RDWR)
    try:
        assert ops.unlink(deleted) == 0
        if refused:
            with pytest.raises(FuseOSError) as exc:
                ops.rename(held, deleted)
            assert exc.value.errno in (errno.EACCES, errno.EBUSY)
            held, deleted = deleted, held
        else:
            assert ops.rename(held, deleted) == 0
        # The handle follows the file to its new name.
        assert ops.read(deleted, 7, 0, fh) == b"target\n"
        assert ops.getattr(deleted)
        with pytest.raises(FuseOSError) as exc:
            ops.getattr(held)
        assert exc.value.errno == errno.ENOENT
    finally:
        ops.release(deleted, fh)
    assert ops.bridge.stat_path(held) is None
    assert not any(e["name"].startswith(".amifuse")
                   for d in dirs for e in ops.bridge.list_dir_path(d))
    fh = ops.open(deleted, os.O_RDWR)
    try:
        assert ops.read(deleted, 7, 0, fh) == b"target\n"
    finally:
        ops.release(deleted, fh)


@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
@pytest.mark.parametrize("held, deleted", [("/l", "/D/T.txt"), ("/D/T.txt", "/l")])
def test_a_rename_onto_a_deleted_name_of_the_file_moves_it(handler_ops, winfsp, held, deleted):
    _rename_onto_a_deleted_name(handler_ops, winfsp, held, deleted)


@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
@pytest.mark.parametrize("held, deleted", [("/lf", "/TargetFile.txt"), ("/TargetFile.txt", "/lf")])
def test_a_rename_onto_a_deleted_name_of_the_file_is_refused_bundled(ops, winfsp, held, deleted):
    # The bundled FastFileSystem refuses to rename a file while it is open.
    _rename_onto_a_deleted_name(ops, winfsp, held, deleted, refused=True)


def _rename_another_file_onto_a_deleted_name(ops, winfsp, held, deleted, other,
                                             refused=False):
    """Rename other, another file, onto deleted, whose delete waits for a
    handle open through held, a hard link to it. other takes the name, and
    the replaced file stays readable through the handle and through held;
    or, where the handler refuses to rename the held file aside, nothing
    moves."""
    ops._winfsp = winfsp
    dirs = {path.rsplit("/", 1)[0] or "/" for path in (held, deleted, other)}
    fh = ops.open(other, os.O_RDWR)
    ops.write(other, b"OTHER!\n", 0, fh)
    ops.release(other, fh)
    fh = ops.open(held, os.O_RDWR)
    try:
        assert ops.unlink(deleted) == 0
        if refused:
            with pytest.raises(FuseOSError) as exc:
                ops.rename(other, deleted)
            assert exc.value.errno in (errno.EACCES, errno.EBUSY)
            with pytest.raises(FuseOSError) as exc:
                ops.getattr(deleted)
            assert exc.value.errno == errno.ENOENT
        else:
            assert ops.rename(other, deleted) == 0
            with pytest.raises(FuseOSError) as exc:
                ops.getattr(other)
            assert exc.value.errno == errno.ENOENT
        assert ops.read(held, 7, 0, fh) == b"target\n"
    finally:
        ops.release(held, fh)
    assert not any(e["name"].startswith(".amifuse")
                   for d in dirs for e in ops.bridge.list_dir_path(d))
    survivors = {held: b"target\n", (other if refused else deleted): b"OTHER!\n"}
    for path, data in survivors.items():
        fh = ops.open(path, os.O_RDWR)
        try:
            assert ops.read(path, 7, 0, fh) == data
        finally:
            ops.release(path, fh)
    if refused:
        assert ops.bridge.stat_path(deleted) is None


@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
def test_a_rename_onto_a_name_deleted_while_open_through_a_link(handler_ops, winfsp):
    _rename_another_file_onto_a_deleted_name(handler_ops, winfsp, "/l", "/D/T.txt", "/E/T.txt")


@pytest.mark.parametrize("winfsp", [True, False], ids=["winfsp", "posix"])
def test_a_rename_onto_a_name_deleted_while_open_through_a_link_bundled(ops, winfsp):
    # The bundled FastFileSystem refuses to rename a file while it is open.
    _rename_another_file_onto_a_deleted_name(
        ops, winfsp, "/lf", "/TargetFile.txt", "/other.txt", refused=True)
