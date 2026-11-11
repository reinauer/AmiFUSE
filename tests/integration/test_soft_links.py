"""Soft links are reported as symbolic links (#434).

A handler refuses a lock on a soft link, so it can't be examined: getattr()
takes its entry from the directory listing, and readlink() reads its target
with ACTION_READ_LINK. The tests call the AmigaFuseFS callbacks directly, as
a mount would.
"""

import errno
import stat
import struct

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from amifuse.emu_thread import on_emu_thread
from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf
from tests.integration.test_ls_recursive_ffs import BLOCK, _edit_block, _xdftool
from tests.integration.test_rename_replace_handlers import _read, _write, ops  # noqa: F401

pytestmark = pytest.mark.timeout(120)

ACTION_MAKE_LINK = 1021
LINK_SOFT = 1
ST_FILE = -3
ST_SOFTLINK = 3


@pytest.fixture(autouse=True)
def needs_cpu():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


def _errno(func, *args):
    with pytest.raises(FuseOSError) as exc:
        func(*args)
    return exc.value.errno


@on_emu_thread
def _make_soft_link(bridge, name, target):
    """Make soft link name in the root, as MakeLink() does."""
    with bridge._lock:
        root, _ = bridge.locate(0, "")
        _, name_bptr = bridge._alloc_bstr(name)
        target_mem = bridge.vh.alloc.alloc_memory(len(target) + 1, label="LINK_TARGET")
        bridge.mem.w_block(target_mem.addr, target.encode("latin-1") + b"\x00")
        pkt_addr, _ = bridge.launcher.send_packet(
            bridge.state, ACTION_MAKE_LINK, [root, name_bptr, target_mem.addr, LINK_SOFT])
        reply = bridge._run_until_reply(pkt_addr, "make_link")
        bridge.free_lock(root)
    assert reply is not None and reply[0], reply


def _soft_link_image(tmp_path):
    """An FFS floppy with a/target.txt and a/sl, a soft link to it.

    xdftool can't write a soft link, so a file's header is turned into an
    ST_SOFTLINK block, with the target where a file keeps its block table.
    """
    image = tmp_path / "softlink.adf"
    content, empty = tmp_path / "content.txt", tmp_path / "empty.txt"
    content.write_bytes(b"target\n")
    empty.write_bytes(b"")
    _xdftool(image, "create", "+", "format", "Links", "ffs", "+", "makedir", "a",
             "+", "write", str(content), "a/target.txt", "+", "write", str(empty), "a/sl")
    data = bytearray(image.read_bytes())
    for key in range(len(data) // BLOCK):
        block = data[key * BLOCK:(key + 1) * BLOCK]
        if (struct.unpack(">I", block[:4])[0] == 2
                and struct.unpack(">i", block[-4:])[0] == ST_FILE
                and block[433:433 + block[432]] == b"sl"):
            break
    else:
        raise AssertionError("no file header for sl")

    def to_soft_link(block):
        struct.pack_into(">i", block, BLOCK - 4, ST_SOFTLINK)
        block[24:35] = b"target.txt\x00"

    _edit_block(data, key, to_soft_link)
    image.write_bytes(data)
    return image


@pytest.mark.parametrize("read_only", [True, False], ids=["read-only", "write"])
def test_soft_link_is_a_symbolic_link(tmp_path, read_only):
    image = _soft_link_image(tmp_path)
    bridge = HandlerBridge(image, BUNDLED_DRIVER, read_only=read_only,
                           adf_info=detect_adf(image))
    try:
        ops = AmigaFuseFS(bridge)
        # A read-only mount answers getattr() from what readdir() cached; a
        # write mount asks the handler, which won't lock the link.
        assert "sl" in ops.readdir("/a", None)
        assert stat.S_ISLNK(ops.getattr("/a/sl")["st_mode"])
        assert ops.readlink("/a/sl") == "target.txt"
        assert stat.S_ISREG(ops.getattr("/a/target.txt")["st_mode"])
        assert _errno(ops.readlink, "/a/target.txt") == errno.EINVAL
        assert _errno(ops.readlink, "/a/missing") == errno.ENOENT
        assert _errno(ops.readlink, "/") == errno.EINVAL
    finally:
        bridge.close()


@pytest.mark.parametrize("target, expected", [
    ("target.txt", "target.txt"),
    ("d/f", "d/f"),
    ("/up", "../up"),
    ("Elsewhere:d/f", "Elsewhere:d/f"),
])
def test_readlink_reports_the_target_relative_to_the_link(ops, target, expected):
    _make_soft_link(ops.bridge, "sl", target)
    assert stat.S_ISLNK(ops.getattr("/sl")["st_mode"])
    assert stat.S_ISLNK(ops.getattr("/SL")["st_mode"])
    assert ops.readlink("/sl") == expected


def test_readlink_names_this_volume_as_the_root(ops):
    _make_soft_link(ops.bridge, "sl", ops.bridge.volume_name() + ":d/f")
    assert ops.readlink("/sl") == "d/f"


def test_readlink_names_this_drive_as_the_root(ops):
    # A link made from a device path keeps the drive name, which AmigaDOS
    # resolves to this volume as well (#447). A floppy records no drive.
    drive = ops.bridge.drive_name()
    if drive is None:
        assert ops.bridge.backend.rdb is None
        _make_soft_link(ops.bridge, "sl", "DF0:d/f")
        assert ops.readlink("/sl") == "DF0:d/f"
        return
    _make_soft_link(ops.bridge, "sl", drive + ":d/f")
    _make_soft_link(ops.bridge, "lower", drive.lower() + ":d/f")
    assert ops.readlink("/sl") == "d/f"
    assert ops.readlink("/lower") == "d/f"


@pytest.mark.parametrize("spelling", ["/sl", "/SL"])
def test_an_exclusive_create_or_mkdir_at_a_soft_link_s_name(ops, spelling):
    # WinFsp sends CREATE_NEW to create() without opening the name, and the
    # handler can't lock a soft link to show it is there: create() opened
    # the name, which the handler refused, and both failed with EIO (#448).
    if spelling != "/sl" and ops._name_fold is None:
        pytest.skip("handler does not advertise case folding")
    ops._winfsp = True
    _write(ops, "/target", b"data")
    _make_soft_link(ops.bridge, "sl", "target")
    assert _errno(ops.create, spelling, 0o644) == errno.EEXIST
    assert _errno(ops.mkdir, spelling, 0o755) == errno.EEXIST
    assert stat.S_ISLNK(ops.getattr("/sl")["st_mode"])
    assert ops.readlink("/sl") == "target"
    assert _read(ops, "/target") == b"data"


def test_mkdir_at_a_file_s_name_says_it_exists(ops):
    _write(ops, "/file", b"data")
    assert _errno(ops.mkdir, "/file", 0o755) == errno.EEXIST
    assert _read(ops, "/file") == b"data"


def test_readlink_of_what_is_not_a_soft_link(ops):
    _write(ops, "/file", b"data")
    ops.mkdir("/dir", 0o755)
    assert _errno(ops.readlink, "/file") == errno.EINVAL
    assert _errno(ops.readlink, "/dir") == errno.EINVAL
    assert _errno(ops.readlink, "/missing") == errno.ENOENT


def test_unlink_deletes_the_soft_link_not_its_target(ops):
    _write(ops, "/target", b"data")
    _make_soft_link(ops.bridge, "sl", "target")
    assert ops.unlink("/sl") == 0
    assert _errno(ops.getattr, "/sl") == errno.ENOENT
    assert _read(ops, "/target") == b"data"


def test_rename_moves_the_soft_link(ops, request):
    _make_soft_link(ops.bridge, "sl", "target")
    if request.node.callspec.params["ops"][1] == "SmartFilesystem":
        assert _errno(ops.rename, "/sl", "/moved") == errno.EPERM
        return
    ops.rename("/sl", "/moved")
    assert ops.readlink("/moved") == "target"
    assert _errno(ops.getattr, "/sl") == errno.ENOENT
