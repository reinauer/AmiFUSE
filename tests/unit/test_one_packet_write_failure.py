"""A failed write of one packet that grew the file is undone.

FastFileSystem 47.4 answers an ACTION_WRITE that runs out of space with
dp_Res1 = -1 and ERROR_DISK_FULL, but keeps the part of the packet that
fit, and the file has grown by it. A write of at most IO_CHUNK bytes is
one packet, and was not undone: write() failed with ENOSPC while the file
kept the data, getattr() of a file open from create() reported the old
size, and an append at that size landed inside the leftover.
"""

import errno
import os

import pytest

from amifuse import fuse_fs
from tests.unit.test_fuse_fs import (
    PKT,
    _FakeHandlerFile,
    _fake_write_bridge,
    _fake_write_fs,
    _pattern,
    _reply,
)

FH_ADDR = 0x3000
ERROR_DISK_FULL = 221
HEAD = b"A" * 1000


class _KeepsWhatFits(_FakeHandlerFile):
    """A fake file whose ACTION_WRITE past capacity writes what fits, then
    fails with ERROR_DISK_FULL, as FastFileSystem 47.4 does."""

    def write(self, state, fh_addr, addr, size):
        pos = self._pos(fh_addr)
        if pos + size <= self.capacity:
            return super().write(state, fh_addr, addr, size)
        self.writes.append(size)
        kept = max(self.capacity - pos, 0)
        self.data[pos:pos + kept] = self.buf[:kept]
        self._move(fh_addr, pos + kept)
        self.reply = _reply(-1, ERROR_DISK_FULL)
        return PKT, 0


@pytest.mark.parametrize("start", [len(HEAD), 400], ids=["at-end", "inside"])
def test_bridge_shrinks_back_a_failed_packet(fuse_mock, start):
    """A one-packet write that fails past the old end shrinks the file back
    to that end and keeps the error. Bytes it overwrote inside the file
    stay."""
    handler = _KeepsWhatFits(HEAD, capacity=len(HEAD) + 300)
    handler.pos = start
    bridge = _fake_write_bridge(fuse_fs, handler)
    data = _pattern(fuse_fs.IO_CHUNK)

    assert bridge.write_handle_reply(FH_ADDR, data) == (-1, ERROR_DISK_FULL)
    assert handler.writes == [len(data)]
    assert handler.set_sizes == [len(HEAD)]
    assert handler.data == HEAD[:start] + data[:len(HEAD) - start]
    assert handler.pos == 0


@pytest.mark.parametrize("how", ["create", "open"])
def test_fuse_failed_write_leaves_file_and_size(fuse_mock, how):
    """The reproduction from the bug report: 64 KiB at the end of a 1000-byte
    file with about 300 bytes left. write() fails with ENOSPC, the file is
    as it was, getattr() of a file open from create() agrees with it, and
    an append at that size lands at the end of file."""
    handler = _KeepsWhatFits(b"", capacity=len(HEAD) + 300)
    fs = _fake_write_fs(fuse_fs, handler)
    if how == "create":
        handle = fs.create("/a", 0o644)
    else:
        handle = fs.open("/a", os.O_RDWR)
    assert fs.write("/a", HEAD, 0, handle) == len(HEAD)

    # At the cached position, so it goes straight to write_handle_reply.
    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        fs.write("/a", b"B" * fuse_fs.IO_CHUNK, len(HEAD), handle)
    assert exc_info.value.args[0] == errno.ENOSPC
    assert handler.data == HEAD
    assert handler.set_sizes == [len(HEAD)]
    if how == "create":
        assert fs.getattr("/a", handle)["st_size"] == len(HEAD)
    assert fs.write("/a", b"APPEND", len(HEAD), handle) == 6
    assert handler.data == HEAD + b"APPEND"
    if how == "create":
        assert fs.getattr("/a", handle)["st_size"] == len(HEAD) + 6
