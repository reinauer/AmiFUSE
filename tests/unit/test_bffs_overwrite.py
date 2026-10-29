"""Overwriting a file on BFFS, and opening a file another handle holds.

BFFS refuses every ACTION_SET_FILE_SIZE, so a truncate to 0 cannot empty
a file. Its ACTION_FINDOUTPUT leaves an existing file as it was, and
ACTION_END cuts the file to the furthest byte the handle read or wrote,
but only once the handle has written. A truncate to 0 that BFFS refuses
therefore ends the handle and opens the file again with
ACTION_FINDOUTPUT, and a handle opened that way that writes nothing gets
a zero-length ACTION_WRITE before ACTION_END.

_FakeBFFS follows fs/packets.c and fs/system.c in cdhooper/bffs 949061a.
There is no BFFS image among the fixtures; the integration tests in
tests/integration/test_overwrite_handlers.py run the same calls on the
real handlers there.
"""

import errno
import itertools
import os
from unittest.mock import MagicMock

import pytest

from amifuse import fuse_fs
from tests.unit.test_fuse_fs import NO_REPLY, PKT, _fake_write_bridge, _reply

ORIGINAL = bytes(range(256)) * 12  # 3072 bytes
NEW = b"n" * 100
ROOT_LOCK = 0x500

FINDINPUT, FINDUPDATE, FINDOUTPUT = "FINDINPUT", "FINDUPDATE", "FINDOUTPUT"


class _FakeBFFS:
    """The file packets of BFFS's read-write build on one directory.

    - ACTION_FINDINPUT and ACTION_FINDUPDATE are the same (PFindInput): a
      shared lock, and the file is never cut at the end.
    - ACTION_FINDOUTPUT (PFindOutput) takes an exclusive lock, refused with
      ERROR_OBJECT_IN_USE while any other handle holds the file, creates a
      missing file and leaves an existing one as it was.
    - ACTION_READ and ACTION_WRITE move the handle and raise its furthest
      position; ACTION_SEEK moves it within the file only and leaves the
      furthest position alone. The first ACTION_WRITE, even of no bytes,
      switches the handle to write mode.
    - ACTION_END (PEnd) of a FINDOUTPUT handle in write mode cuts the file
      to the furthest position (truncate_file).
    - ACTION_SET_FILE_SIZE (PSetFileSize) is refused with DOSFALSE and
      ERROR_OBJECT_NOT_FOUND.

    open_replies maps the number of an open, counted from 0, to the reply
    it gets instead, and write_replies likewise for ACTION_WRITE.
    """

    def __init__(self, files):
        self.files = {name: bytearray(data) for name, data in files.items()}
        self.handles = {}
        self.opens = []    # (action, name) of every open
        self.writes = []   # length of every ACTION_WRITE
        self.ends = []     # address of every handle ACTION_END was sent for
        self.set_sizes = []
        self.open_replies = {}
        self.write_replies = {}
        self.buf = b""
        self.reply = NO_REPLY

    # The memory and packet interface _fake_write_bridge wires up.

    def w_block(self, addr, data):
        self.buf = bytes(data)

    def r_block(self, addr, size):
        return self.buf[:size]

    def find(self, action, name, fh_addr):
        number = len(self.opens)
        self.opens.append((action, name))
        if number in self.open_replies:
            self.reply = self.open_replies[number]
            return fh_addr, PKT, 0
        holders = [h for h in self.handles.values() if h["name"] == name]
        if action == FINDOUTPUT:
            if holders:
                self.reply = _reply(0, fuse_fs.ERROR_OBJECT_IN_USE)
                return fh_addr, PKT, 0
            self.files.setdefault(name, bytearray())
        elif name not in self.files:
            self.reply = _reply(0, fuse_fs.ERROR_OBJECT_NOT_FOUND)
            return fh_addr, PKT, 0
        elif any(h["exclusive"] for h in holders):
            self.reply = _reply(0, fuse_fs.ERROR_OBJECT_IN_USE)
            return fh_addr, PKT, 0
        self.handles[fh_addr] = {
            "name": name, "pos": 0, "max": 0, "write": False,
            "exclusive": action == FINDOUTPUT, "truncate": action == FINDOUTPUT,
        }
        self.reply = _reply(-1)
        return fh_addr, PKT, 0

    def read(self, state, fh_addr, addr, size):
        h = self.handles[fh_addr]
        data = self.files[h["name"]]
        self.buf = bytes(data[h["pos"]:h["pos"] + size])
        h["pos"] += len(self.buf)
        h["max"] = max(h["max"], h["pos"])
        self.reply = _reply(len(self.buf))
        return PKT, 0

    def write(self, state, fh_addr, addr, size):
        number = len(self.writes)
        self.writes.append(size)
        if number in self.write_replies:
            self.reply = self.write_replies[number]
            return PKT, 0
        h = self.handles[fh_addr]
        h["write"] = True
        data = self.files[h["name"]]
        data[h["pos"]:h["pos"] + size] = self.buf[:size]
        h["pos"] += size
        h["max"] = max(h["max"], h["pos"])
        self.reply = _reply(size)
        return PKT, 0

    def seek(self, state, fh_addr, offset, mode):
        h = self.handles[fh_addr]
        end = len(self.files[h["name"]])
        base = {fuse_fs.OFFSET_BEGINNING: 0, fuse_fs.OFFSET_CURRENT: h["pos"],
                fuse_fs.OFFSET_END: end}[mode]
        if 0 <= base + offset <= end:
            self.reply = _reply(h["pos"])
            h["pos"] = base + offset
        else:
            self.reply = _reply(-1, fuse_fs.ERROR_SEEK_ERROR)
        return PKT, 0

    def set_size(self, state, fh_addr, size, mode):
        self.set_sizes.append(size)
        self.reply = _reply(0, fuse_fs.ERROR_OBJECT_NOT_FOUND)
        return PKT, 0

    def end(self, state, fh_addr):
        self.ends.append(fh_addr)
        h = self.handles.pop(fh_addr)
        if h["write"] and h["truncate"]:
            del self.files[h["name"]][h["max"]:]
        self.reply = _reply(-1)
        return PKT, 0


def _bffs_bridge(volume):
    """A writable bridge on the fake volume, running the real open_file()
    and close_file()."""
    bridge = _fake_write_bridge(fuse_fs, volume)
    bridge._write_enabled = True
    bridge._neg_cache = {}
    bridge._neg_cache_ttl = 0
    bridge.locate_path = MagicMock(side_effect=lambda path: (0, 0, []))
    bridge.locate = MagicMock(return_value=(ROOT_LOCK, 0))
    bridge.free_lock = MagicMock()
    bridge._alloc_bstr = lambda name: (0, name)
    addresses = itertools.count(0x3000, 0x100)
    bridge._alloc_fh = lambda: next(addresses)
    bridge._free_fh = MagicMock()
    launcher = bridge.launcher
    for action in (FINDINPUT, FINDUPDATE, FINDOUTPUT):
        getattr(launcher, f"send_{action.lower()}").side_effect = (
            lambda state, name, lock, fh_addr, action=action:
            volume.find(action, name, fh_addr))
    launcher.send_end_handle.side_effect = volume.end
    # BFFS reports the inode's size, which an ACTION_FINDOUTPUT handle
    # changes only at its end.
    bridge.stat_path = lambda path: (
        {"dir_type": -3, "size": len(volume.files[path[1:]]), "protection": 0,
         "num_blocks": 1} if path[1:] in volume.files else None)
    return bridge


def _bffs_fs(volume):
    return fuse_fs.AmigaFuseFS(_bffs_bridge(volume), debug=False, icons=False)


@pytest.mark.parametrize("new", [NEW, ORIGINAL + NEW], ids=["shorter", "longer"])
def test_overwrite_through_open_handle(fuse_mock, new):
    """WinFsp's overwrite: an open without O_TRUNC, ftruncate(fh, 0), then
    the new data."""
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    fh = fs.open("/file", os.O_RDWR)

    assert fs.truncate("/file", 0, fh) == 0
    assert fs.getattr("/file", fh)["st_size"] == 0
    assert fs.write("/file", new, 0, fh) == len(new)
    assert fs.getattr("/file", fh)["st_size"] == len(new)
    fs.release("/file", fh)

    assert volume.files["file"] == new
    assert [action for action, _ in volume.opens] == [FINDUPDATE, FINDOUTPUT]
    assert volume.handles == {}
    assert fs.getattr("/file")["st_size"] == len(new)


def test_truncate_by_path_empties_file(fuse_mock):
    """Linux's truncate to 0 with no handle open: the temporary handle is
    replaced too, and writes nothing."""
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)

    assert fs.truncate("/file", 0) == 0

    assert volume.files["file"] == b""
    assert volume.writes == [0]
    assert volume.handles == {}
    assert fs.bridge.free_lock.call_count == 2  # each handle's parent lock


def test_parent_lock_is_swapped(fuse_mock):
    """The old handle's parent lock is freed at the reopen, and the new
    one at release."""
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    locks = iter([(0x501, 0), (0x502, 0)])
    fs.bridge.locate = MagicMock(side_effect=lambda lock, name: next(locks))
    fh = fs.open("/file", os.O_RDWR)

    fs.truncate("/file", 0, fh)
    assert [c.args for c in fs.bridge.free_lock.call_args_list] == [(0x501,)]
    fs.release("/file", fh)
    assert [c.args for c in fs.bridge.free_lock.call_args_list] == [
        (0x501,), (0x502,)]


def test_refused_reopen_keeps_a_handle(fuse_mock):
    """BFFS refuses the ACTION_FINDOUTPUT while another handle holds the
    file, and truncate() says the file is busy. The file is opened again
    as before and keeps its data."""
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    reader = fs.open("/file", os.O_RDONLY)
    fh = fs.open("/file", os.O_RDWR)

    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        fs.truncate("/file", 0, fh)
    assert exc_info.value.args[0] == errno.EBUSY
    assert [action for action, _ in volume.opens] == [
        FINDINPUT, FINDUPDATE, FINDOUTPUT, FINDUPDATE]
    assert fs.write("/file", b"X", 0, fh) == 1
    assert fs.read("/file", 4, 0, reader) == b"X" + ORIGINAL[1:4]
    fs.release("/file", fh)
    fs.release("/file", reader)

    assert volume.files["file"] == b"X" + ORIGINAL[1:]
    assert volume.handles == {}
    # The refused open's lock, the old handle's, then at release the
    # handle's and the reader's.
    assert fs.bridge.free_lock.call_count == 4


@pytest.mark.parametrize("replies, opens, expected, freed", [
    ({1: _reply(0, fuse_fs.ERROR_OBJECT_IN_USE),
      2: _reply(0, fuse_fs.ERROR_OBJECT_IN_USE)},
     [FINDUPDATE, FINDOUTPUT, FINDUPDATE], errno.EBUSY, 3),
    ({1: NO_REPLY}, [FINDUPDATE, FINDOUTPUT], errno.EIO, 2),
], ids=["refused-twice", "no-reply"])
def test_entry_with_no_handle_left_is_closed(fuse_mock, replies, opens,
                                             expected, freed):
    """With no handle left, every later call fails with EIO, and release()
    sends no ACTION_END for the handle that ended and frees no lock. After
    an open that got no reply, no other open is sent."""
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    fh = fs.open("/file", os.O_RDWR)
    volume.open_replies = replies

    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        fs.truncate("/file", 0, fh)
    assert exc_info.value.args[0] == expected
    assert [action for action, _ in volume.opens] == opens
    ended = volume.ends[:]
    assert len(ended) == 1 and volume.handles == {}
    for call in (lambda: fs.write("/file", b"X", 0, fh),
                 lambda: fs.read("/file", 4, 0, fh),
                 lambda: fs.truncate("/file", 0, fh)):
        with pytest.raises(fuse_fs.FuseOSError) as exc_info:
            call()
        assert exc_info.value.args[0] == errno.EIO
    fs.release("/file", fh)

    assert volume.ends == ended
    assert volume.files["file"] == ORIGINAL
    assert fs.bridge.free_lock.call_count == freed


def test_refused_zero_length_write_fails_the_truncate(fuse_mock):
    """BFFS refuses the write mode when it cannot extend the file's last
    block on a full volume. The truncate then fails with ENOSPC, the new
    handle ends without cutting the file, and the file is opened again as
    before, so a later write cannot cut it either."""
    volume = _FakeBFFS({"file": ORIGINAL})
    volume.write_replies = {0: _reply(-1, fuse_fs.ERROR_DISK_FULL)}
    fs = _bffs_fs(volume)
    fh = fs.open("/file", os.O_RDWR)

    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        fs.truncate("/file", 0, fh)
    assert exc_info.value.args[0] == errno.ENOSPC
    assert [action for action, _ in volume.opens] == [
        FINDUPDATE, FINDOUTPUT, FINDUPDATE]
    assert fs.getattr("/file", fh)["st_size"] == len(ORIGINAL)
    assert fs.write("/file", b"X", 0, fh) == 1
    fs.release("/file", fh)

    assert volume.writes == [0, 1]
    assert volume.files["file"] == b"X" + ORIGINAL[1:]
    assert volume.handles == {}


def test_unanswered_zero_length_write_keeps_the_new_handle(fuse_mock):
    """The handler may still carry out the write, so no other packet goes
    out for it: no open, no second zero-length write. The entry keeps the
    new handle, which release() ends."""
    volume = _FakeBFFS({"file": ORIGINAL})
    volume.write_replies = {0: NO_REPLY}
    fs = _bffs_fs(volume)
    fh = fs.open("/file", os.O_RDWR)

    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        fs.truncate("/file", 0, fh)
    assert exc_info.value.args[0] == errno.EIO
    assert [action for action, _ in volume.opens] == [FINDUPDATE, FINDOUTPUT]
    fs.release("/file", fh)

    assert volume.writes == [0]
    assert len(volume.ends) == 2 and volume.handles == {}


@pytest.mark.parametrize("truncate", [True, False],
                         ids=["reopen", "O_TRUNC-open"])
def test_read_stops_at_what_the_handle_wrote(fuse_mock, truncate):
    """BFFS still holds the old data until the handle ends, and a read of
    it would move the point BFFS cuts the file at. A read past what the
    handle wrote returns nothing and sends no packet."""
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    if truncate:
        fh = fs.open("/file", os.O_RDWR)
        fs.truncate("/file", 0, fh)
    else:
        fh = fs.open("/file", os.O_RDWR | os.O_TRUNC)

    assert fs.read("/file", 4096, 0, fh) == b""
    fs.write("/file", NEW, 0, fh)
    assert fs.read("/file", 4096, 0, fh) == NEW
    assert fs.read("/file", 10, len(NEW) - 4, fh) == NEW[-4:]
    fs.release("/file", fh)

    assert volume.files["file"] == NEW


def test_truncate_to_nonzero_length_is_not_reopened(fuse_mock):
    """Only a truncate to 0 can be done by a reopen."""
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    fh = fs.open("/file", os.O_RDWR)

    with pytest.raises(fuse_fs.FuseOSError):
        fs.truncate("/file", 10, fh)
    fs.release("/file", fh)

    assert [action for action, _ in volume.opens] == [FINDUPDATE]
    assert volume.files["file"] == ORIGINAL


@pytest.mark.parametrize("flags", [os.O_WRONLY | os.O_TRUNC,
                                   os.O_RDWR | os.O_TRUNC],
                         ids=["O_WRONLY", "O_RDWR"])
def test_truncating_open_that_writes_nothing_empties_file(fuse_mock, flags):
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    fh = fs.open("/file", flags)
    assert fs.getattr("/file", fh)["st_size"] == 0
    fs.release("/file", fh)

    assert volume.files["file"] == b""
    assert volume.writes == [0]
    assert fs.getattr("/file")["st_size"] == 0


def test_bridge_output_handle_that_writes_nothing_empties_file(fuse_mock):
    """amifuse write with an empty source opens and closes the
    destination with nothing in between."""
    volume = _FakeBFFS({"file": ORIGINAL})
    bridge = _bffs_bridge(volume)
    fh_addr, _ = bridge.open_file("/file", os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    bridge.close_file(fh_addr)

    assert volume.files["file"] == b""
    assert volume.writes == [0]
    assert bridge._unwritten_outputs == set()


def test_reused_file_handle_carries_no_mark(fuse_mock):
    """A FileHandle back from the pool starts unmarked: only the output
    handle that wrote nothing gets the zero-length write."""
    volume = _FakeBFFS({"file": ORIGINAL})
    bridge = _bffs_bridge(volume)
    pool = []
    addresses = itertools.count(0x3000, 0x100)
    bridge._alloc_fh = lambda: pool.pop() if pool else next(addresses)
    bridge._free_fh = pool.append

    output, _ = bridge.open_file("/file", os.O_WRONLY | os.O_TRUNC)
    bridge.close_file(output)
    update, _ = bridge.open_file("/file", os.O_RDWR)
    bridge.close_file(update)

    assert update == output
    assert volume.writes == [0]


def test_failed_first_write_still_gets_zero_length_write(fuse_mock):
    """BFFS can fail a write before the handle switches to write mode,
    when it cannot extend the file's last block, so a failed write leaves
    the mark. Here the zero-length write at the end succeeds, as when the
    first write failed after the switch or space came free since."""
    volume = _FakeBFFS({"file": ORIGINAL})
    volume.write_replies = {0: _reply(-1, fuse_fs.ERROR_DISK_FULL)}
    fs = _bffs_fs(volume)
    fh = fs.open("/file", os.O_WRONLY | os.O_TRUNC)

    with pytest.raises(fuse_fs.FuseOSError):
        fs.write("/file", NEW, 0, fh)
    fs.release("/file", fh)

    assert volume.writes == [len(NEW), 0]
    assert volume.files["file"] == b""


def test_handle_that_wrote_gets_no_zero_length_write(fuse_mock):
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    fh = fs.open("/file", os.O_WRONLY | os.O_TRUNC)
    fs.write("/file", NEW, 0, fh)
    fs.release("/file", fh)

    assert volume.writes == [len(NEW)]
    assert volume.files["file"] == NEW


@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_RDWR],
                         ids=["O_RDONLY", "O_RDWR"])
def test_shared_handle_gets_no_zero_length_write(fuse_mock, flags):
    volume = _FakeBFFS({"file": ORIGINAL})
    fs = _bffs_fs(volume)
    fs.release("/file", fs.open("/file", flags))

    assert volume.writes == []
    assert volume.files["file"] == ORIGINAL


@pytest.mark.parametrize("flags", [os.O_RDONLY, os.O_RDWR],
                         ids=["O_RDONLY", "O_RDWR"])
def test_open_of_file_in_use_is_ebusy(fuse_mock, flags):
    """A file that create() holds cannot be opened again, and the error
    says it is busy, not missing."""
    volume = _FakeBFFS({})
    fs = _bffs_fs(volume)
    fh = fs.create("/new", 0o644)

    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        fs.open("/new", flags)
    assert exc_info.value.args[0] == errno.EBUSY
    fs.release("/new", fh)


def test_truncate_by_path_of_file_in_use_is_ebusy(fuse_mock):
    """truncate() by path opens a temporary handle, which the handler
    refuses while another handle holds the file."""
    volume = _FakeBFFS({})
    fs = _bffs_fs(volume)
    fh = fs.create("/new", 0o644)
    fs.write("/new", NEW, 0, fh)

    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        fs.truncate("/new", 10)
    assert exc_info.value.args[0] == errno.EBUSY
    fs.release("/new", fh)
    assert volume.files["new"] == NEW


@pytest.mark.parametrize("reply, expected", [
    (_reply(0, fuse_fs.ERROR_OBJECT_IN_USE), (None, 202)),
    (_reply(0, fuse_fs.ERROR_OBJECT_NOT_FOUND), (None, 205)),
    (NO_REPLY, (None, 0)),
], ids=["in-use", "not-found", "no-reply"])
def test_open_file_reply_carries_res2(fuse_mock, reply, expected):
    volume = _FakeBFFS({"file": ORIGINAL})
    volume.open_replies = {0: reply}
    bridge = _bffs_bridge(volume)

    assert bridge.open_file_reply("/file", os.O_RDWR) == expected
    assert bridge.open_file("/file", os.O_RDWR) is not None
