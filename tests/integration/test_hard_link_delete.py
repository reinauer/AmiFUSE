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
"""

import errno
import os
import struct

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
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
