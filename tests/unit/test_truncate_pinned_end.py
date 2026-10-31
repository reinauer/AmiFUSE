"""truncate() and write() take a pinned size as the handle's end of file.

A file held on an exclusive lock, from create(), an O_TRUNC open or the
reopen of a truncate to 0 on BFFS, holds only what its handle wrote, which
its pinned size counts.

- BFFS keeps an existing file's old bytes until such a handle ends, and
  seeks into them, so a truncate past what the handle wrote and a write
  past it fill the gap from the pinned size, not the handler's end.
- A shrink that gets no reply in either wait may still be carried out.
  The handles past 0x7FFFFFFF forget their positions, and a shrink below
  the pinned size has the pin measured again at the next getattr().
"""

import errno
import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amifuse import fuse_fs

PKT = 0x9999  # every packet the fake answers
NO_REPLY = []  # _run_until_replies timed out
CONTENT = bytes(range(256)) * 12  # 3072 bytes
NEW = b"n" * 100
FAR_POS = 0x80000000


def _reply(res1, res2=0):
    return [(0, PKT, res1, res2)]


class _FakeVolume:
    """One file's packets on BFFS or pfs3aio, every handle on that file.

    ACTION_SEEK moves a handle within the file only, and refuses anything
    else with ERROR_SEEK_ERROR. ACTION_READ and ACTION_WRITE move the
    handle and raise its furthest position.

    - BFFS (fs/packets.c and fs/system.c in cdhooper/bffs 949061a): an
      output open (O_TRUNC) leaves the file as it was, the end of such a
      handle cuts the file to its furthest position (close_file's
      zero-length write puts every output handle in write mode), and
      ACTION_SET_FILE_SIZE is refused with ERROR_OBJECT_NOT_FOUND.
    - pfs3aio: an output open empties the file, and ACTION_SET_FILE_SIZE
      resizes it and moves every handle past the new end to it.

    With withhold_sizes, withhold_writes or withhold_seeks set,
    ACTION_SET_FILE_SIZE, ACTION_WRITE or ACTION_SEEK gets no reply; land()
    carries out the withheld size change, as a handler that finishes it
    late does.
    """

    def __init__(self, data, bffs):
        self.data = bytearray(data)
        self.bffs = bffs
        self.handles = {}
        self.next_addr = 0x3000
        self.seeks = []
        self.set_sizes = []
        self.withhold_sizes = False
        self.withhold_writes = False
        self.withhold_seeks = False
        self.pending = None
        self.buf = b""
        self.reply = NO_REPLY

    def open(self, flags):
        addr, self.next_addr = self.next_addr, self.next_addr + 0x100
        output = bool(flags & os.O_TRUNC)
        if output and not self.bffs:
            del self.data[:]
        self.handles[addr] = {"pos": 0, "max": 0, "output": output}
        return addr, 0

    def end(self, fh_addr):
        h = self.handles.pop(fh_addr)
        if self.bffs and h["output"]:
            del self.data[h["max"]:]

    def w_block(self, addr, data):
        self.buf = bytes(data)

    def r_block(self, addr, size):
        return self.buf[:size]

    def seek(self, state, fh_addr, offset, mode):
        self.seeks.append((fh_addr, offset, mode))
        if self.withhold_seeks:
            self.reply = NO_REPLY
            return PKT, 0
        h = self.handles[fh_addr]
        base = {fuse_fs.OFFSET_BEGINNING: 0, fuse_fs.OFFSET_CURRENT: h["pos"],
                fuse_fs.OFFSET_END: len(self.data)}[mode]
        if 0 <= base + offset <= len(self.data):
            self.reply = _reply(h["pos"])
            h["pos"] = base + offset
        else:
            self.reply = _reply(-1, fuse_fs.ERROR_SEEK_ERROR)
        return PKT, 0

    def read(self, state, fh_addr, addr, size):
        h = self.handles[fh_addr]
        self.buf = bytes(self.data[h["pos"]:h["pos"] + size])
        h["pos"] += len(self.buf)
        h["max"] = max(h["max"], h["pos"])
        self.reply = _reply(len(self.buf))
        return PKT, 0

    def write(self, state, fh_addr, addr, size):
        if self.withhold_writes:
            self.reply = NO_REPLY
            return PKT, 0
        h = self.handles[fh_addr]
        self.data[h["pos"]:h["pos"] + size] = self.buf[:size]
        h["pos"] += size
        h["max"] = max(h["max"], h["pos"])
        self.reply = _reply(size)
        return PKT, 0

    def set_size(self, state, fh_addr, size, mode):
        self.set_sizes.append(size)
        if self.withhold_sizes:
            self.pending = size
            self.reply = NO_REPLY
        elif self.bffs:
            self.reply = _reply(0, fuse_fs.ERROR_OBJECT_NOT_FOUND)
        else:
            self._resize(size)
            self.reply = _reply(size)
        return PKT, 0

    def land(self):
        self._resize(self.pending)

    def _resize(self, size):
        del self.data[size:]
        self.data += b"\xa5" * (size - len(self.data))
        for h in self.handles.values():
            h["pos"] = min(h["pos"], size)


def _fs(volume, free_blocks=4096):
    """A writable AmigaFuseFS whose bridge runs the real handle code down
    to the fake's packets, on a volume with free_blocks free 512-byte
    blocks."""
    bridge = object.__new__(fuse_fs.HandlerBridge)
    bridge._lock = threading.RLock()
    bridge._debug = False
    bridge._write_enabled = True
    bridge._unwritten_outputs = set()
    bridge.state = MagicMock()
    bridge.launcher = MagicMock()
    bridge.launcher.send_seek_handle.side_effect = volume.seek
    bridge.launcher.send_read_handle.side_effect = volume.read
    bridge.launcher.send_write_handle.side_effect = volume.write
    bridge.launcher.send_set_file_size.side_effect = volume.set_size
    bridge.mem = MagicMock()
    bridge.mem.w_block.side_effect = volume.w_block
    bridge.mem.r_block.side_effect = volume.r_block
    bridge._alloc_read_buf = MagicMock(return_value=SimpleNamespace(addr=0x2000))
    bridge._run_until_replies = MagicMock(side_effect=lambda: volume.reply)
    bridge.get_disk_info = MagicMock(return_value={
        "num_blocks": 8192, "num_blocks_used": 8192 - free_blocks,
        "bytes_per_block": 512})
    bridge.open_file = lambda path, flags: volume.open(flags)
    bridge.open_file_reply = lambda path, flags: (volume.open(flags), 0)
    bridge.close_file = volume.end
    bridge.free_lock = MagicMock()
    return fuse_fs.AmigaFuseFS(bridge, debug=False, icons=False)


def _addr(fs, fh):
    return fs._fh_cache[fh]["fh_addr"]


def _eio(call):
    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        call()
    assert exc_info.value.args[0] == errno.EIO


# --- #221: BFFS, a file opened with O_TRUNC --------------------------------

@pytest.mark.parametrize("written, length", [
    (b"", 100), (NEW, 200), (NEW, len(NEW)),
], ids=["nothing-written", "past-written", "at-written"])
def test_truncate_from_what_the_handle_wrote(fuse_mock, written, length):
    """A truncate at or past what the handle wrote succeeds and writes
    zeros up to its length, which BFFS keeps when the handle ends. No
    ACTION_SET_FILE_SIZE goes out, which BFFS would refuse."""
    volume = _FakeVolume(CONTENT, bffs=True)
    fs = _fs(volume)
    fh = fs.open("/file", os.O_RDWR | os.O_TRUNC)
    if written:
        assert fs.write("/file", written, 0, fh) == len(written)

    assert fs.truncate("/file", length, fh) == 0
    assert fs.getattr("/file", fh)["st_size"] == length
    fs.release("/file", fh)

    assert volume.set_sizes == []
    assert volume.data == written + bytes(length - len(written))


@pytest.mark.parametrize("written, offset", [(b"", 10), (NEW, 150)],
                         ids=["nothing-written", "after-written"])
def test_gapped_write_fills_from_what_the_handle_wrote(fuse_mock, written, offset):
    """The gap below a write past what the handle wrote reads as zeros,
    not the old file's bytes."""
    volume = _FakeVolume(CONTENT, bffs=True)
    fs = _fs(volume)
    fh = fs.open("/file", os.O_WRONLY | os.O_TRUNC)
    if written:
        fs.write("/file", written, 0, fh)

    assert fs.write("/file", b"abc", offset, fh) == 3
    assert fs.getattr("/file", fh)["st_size"] == offset + 3
    fs.release("/file", fh)

    assert volume.data == written + bytes(offset - len(written)) + b"abc"


def test_fill_over_old_blocks_needs_no_new_space(fuse_mock):
    """A copy that sets the new size first, on a nearly full volume: the
    zeros up to the old file's end go into blocks the file still holds,
    so only the 1024 bytes past it need free space."""
    volume = _FakeVolume(CONTENT, bffs=True)
    fs = _fs(volume, free_blocks=2)
    fh = fs.open("/file", os.O_WRONLY | os.O_TRUNC)

    assert fs.truncate("/file", len(CONTENT) + 1024, fh) == 0
    fs.release("/file", fh)

    assert volume.data == bytes(len(CONTENT) + 1024)


# --- #241: a truncate that gets no reply in either wait --------------------

def _open_far(fs):
    """Another handle, cached past 0x7FFFFFFF."""
    far = fs.open("/far", os.O_RDONLY)
    fs._fh_cache[far]["pos"] = FAR_POS
    return far


def test_lost_shrink_clears_far_positions(fuse_mock):
    """pfs3aio may still carry the shrink out, which moves a far handle,
    so it forgets its cached position without a packet."""
    volume = _FakeVolume(CONTENT, bffs=False)
    fs = _fs(volume)
    far = _open_far(fs)
    fh = fs.open("/file", os.O_RDWR)
    volume.withhold_sizes = True

    _eio(lambda: fs.truncate("/file", 100, fh))
    assert volume.set_sizes == [100]
    assert fs._fh_cache[far]["pos"] is None
    assert [s for s in volume.seeks if s[0] == _addr(fs, far)] == []


def test_late_shrink_shows_in_getattr(fuse_mock):
    """A created file's lost shrink that the handler finishes later: the
    next getattr() measures the end through the handle, once, and leaves
    the handle where it was."""
    volume = _FakeVolume(b"", bffs=False)
    fs = _fs(volume)
    fh = fs.create("/file", 0o644)
    fs.write("/file", CONTENT, 0, fh)
    volume.withhold_sizes = True

    _eio(lambda: fs.truncate("/file", 100, fh))
    volume.land()
    handle = volume.handles[_addr(fs, fh)]
    handle["pos"] = 40

    assert fs.getattr("/file", fh)["st_size"] == 100
    assert handle["pos"] == 40
    seeks = len(volume.seeks)
    assert fs.getattr("/file", fh)["st_size"] == 100
    assert len(volume.seeks) == seeks
    fs.release("/file", fh)


def test_unanswered_measure_is_tried_again(fuse_mock):
    """A measure whose seek gets no reply leaves the pin, and the handle
    forgets its position, as the seek may have moved it. The next
    getattr() measures again."""
    volume = _FakeVolume(b"", bffs=False)
    fs = _fs(volume)
    fh = fs.create("/file", 0o644)
    fs.write("/file", CONTENT, 0, fh)
    volume.withhold_sizes = True

    _eio(lambda: fs.truncate("/file", 100, fh))
    volume.land()
    fs._fh_cache[fh]["pos"] = 40  # as a read would leave it
    volume.withhold_seeks = True
    assert fs.getattr("/file", fh)["st_size"] == len(CONTENT)
    assert fs._fh_cache[fh]["pos"] is None
    volume.withhold_seeks = False
    assert fs.getattr("/file", fh)["st_size"] == 100
    fs.release("/file", fh)


def test_lost_grow_leaves_the_pin(fuse_mock):
    """A grow whose zeros get no reply was shrunk back, so the pin is not
    measured again; the far handles still forget their positions."""
    volume = _FakeVolume(b"", bffs=False)
    fs = _fs(volume)
    far = _open_far(fs)
    fh = fs.create("/file", 0o644)
    fs.write("/file", CONTENT, 0, fh)
    volume.withhold_writes = True

    _eio(lambda: fs.truncate("/file", 5000, fh))
    assert fs._fh_cache[far]["pos"] is None
    seeks = len(volume.seeks)
    assert fs.getattr("/file", fh)["st_size"] == len(CONTENT)
    assert len(volume.seeks) == seeks
    fs.release("/file", fh)


def test_measured_pin_never_rises(fuse_mock):
    """BFFS's end for an O_TRUNC handle still counts the old file. The
    pin stays at what the handle wrote, and a later write past it still
    fills its gap with zeros."""
    volume = _FakeVolume(CONTENT, bffs=True)
    fs = _fs(volume)
    fh = fs.open("/file", os.O_RDWR | os.O_TRUNC)
    data = b"w" * 2000
    fs.write("/file", data, 0, fh)
    volume.withhold_sizes = True

    _eio(lambda: fs.truncate("/file", 100, fh))
    volume.withhold_sizes = False
    seeks = len(volume.seeks)
    assert fs.getattr("/file", fh)["st_size"] == len(data)
    assert len(volume.seeks) == seeks + 2  # the end was measured
    assert fs.write("/file", b"abc", 2500, fh) == 3
    fs.release("/file", fh)

    assert volume.data == data + bytes(500) + b"abc"
