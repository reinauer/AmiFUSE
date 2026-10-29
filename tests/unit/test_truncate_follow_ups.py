"""truncate() follow-ups: where it leaves handles, and replies it must not miss.

- After a shrink, SmartFilesystem on a nearly full volume fails a write at
  the new end of file until the handle moves, and a seek to where the
  handle already is does not count. truncate() through an open handle
  leaves it at the start of the file.
- A shrink whose reply comes just after the wait runs out gets a second
  wait for the same packet, which sends nothing.
- AROS afs refuses a shrink past another open handle with ERROR_SEEK_ERROR
  (219), which sends the shrink again after the other handles move, as
  ERROR_OBJECT_IN_USE from SmartFilesystem does.
- A pfs3aio shrink moves every handle on the file past the new end to it,
  including one past 0x7FFFFFFF, which no seek can return to its cached
  position. A zero-length OFFSET_CURRENT seek checks each such handle.
"""

import errno
import os
from unittest.mock import MagicMock

import pytest

from amifuse import fuse_fs
from tests.unit.test_fuse_fs import (
    NO_REPLY,
    PKT,
    _FakeHandlerFile,
    _fake_write_bridge,
    _fake_write_fs,
    _reply,
)

CONTENT = bytes(range(256)) * 12  # 3072 bytes
FH_ADDR = 0x3000  # what every open but _open_other's returns
OTHER = 0x3100
FAR = 0x3200

WAYS = pytest.mark.parametrize("use_handle", [False, True],
                               ids=["path", "handle"])


def _open_other(fs, handler, pos, fh_addr=OTHER, path="/file"):
    """Open another handle on the file, left at pos as by a read."""
    fs.bridge.open_file.return_value = (fh_addr, 0)
    handle = fs.open(path, os.O_RDONLY)
    fs.bridge.open_file.return_value = (FH_ADDR, 0)
    handler.others[fh_addr] = pos
    fs._fh_cache[handle]["pos"] = pos
    return handle


class _SfsAfterShrink(_FakeHandlerFile):
    """SmartFilesystem on a nearly full volume, after a shrink.

    The truncating handle fails every write, with dp_Res2 83 as SFS does,
    until a seek or read moves it; a seek to where it already is does not
    count.
    """

    def __init__(self, data):
        super().__init__(data)
        self.stuck = False

    def set_size(self, state, fh_addr, size, mode):
        self.stuck = size < len(self.data)
        return super().set_size(state, fh_addr, size, mode)

    def seek(self, state, fh_addr, offset, mode):
        before = self._pos(fh_addr)
        packet = super().seek(state, fh_addr, offset, mode)
        if fh_addr not in self.others and self._pos(fh_addr) != before:
            self.stuck = False
        return packet

    def write(self, state, fh_addr, addr, size):
        if self.stuck and fh_addr not in self.others:
            self.writes.append(size)
            self.reply = _reply(-1, 83)
            return PKT, 0
        return super().write(state, fh_addr, addr, size)


class _AfsFile(_FakeHandlerFile):
    """AROS afs: a shrink past another handle is refused with (-1, 219).

    afs setFileSize sets ERROR_SEEK_ERROR first, then fails while any
    other handle on the file is past the new size.
    """

    def set_size(self, state, fh_addr, size, mode):
        if any(pos > size for pos in self.others.values()):
            self.set_sizes.append(size)
            self.reply = _reply(-1, 219)
            return PKT, 0
        return super().set_size(state, fh_addr, size, mode)


class TestShrinkLeavesHandleAtStart:
    """SmartFilesystem: a write at the new end works after the shrink."""

    def test_handle_is_left_at_start(self, fuse_mock):
        handler = _FakeHandlerFile(CONTENT)
        fs = _fake_write_fs(fuse_fs, handler)
        handle = fs.open("/file", os.O_RDWR)

        assert fs.truncate("/file", 1000, handle) == 0
        assert handler.seeks[-1] == (FH_ADDR, 0, fuse_fs.OFFSET_BEGINNING)
        assert handler.pos == 0
        assert fs._fh_cache[handle]["pos"] == 0

    @pytest.mark.parametrize("short", ["stopped", "retry-seek-unanswered"])
    def test_no_seek_until_length_is_reached(self, fuse_mock, short):
        """A truncate that falls short sends no seek to the start: one
        stopped at a handle the mount cannot move, or one whose retry seek
        may still be with the handler. Once the truncate reaches its
        length, the seek goes out."""
        handler = _FakeHandlerFile(CONTENT)
        fs = _fake_write_fs(fuse_fs, handler)
        handle = fs.open("/file", os.O_RDWR)
        seek = handler.seek
        if short == "stopped":
            handler.stop_at = 2000
        else:
            _open_other(fs, handler, 2000)

            def retry_seek_unanswered(state, fh_addr, offset, mode):
                packet = seek(state, fh_addr, offset, mode)
                if fh_addr == OTHER and len(handler.set_sizes) == 1:
                    handler.reply = NO_REPLY
                return packet

            fs.bridge.launcher.send_seek_handle.side_effect = (
                retry_seek_unanswered)

        with pytest.raises(fuse_fs.FuseOSError) as exc_info:
            fs.truncate("/file", 100, handle)
        assert exc_info.value.args[0] == errno.EIO
        start = (FH_ADDR, 0, fuse_fs.OFFSET_BEGINNING)
        assert start not in handler.seeks

        # The unanswered retry seek still moved the other handle to 0.
        handler.stop_at = 0
        fs.bridge.launcher.send_seek_handle.side_effect = seek
        assert fs.truncate("/file", 100, handle) == 0
        assert handler.seeks[-1] == start

    def test_write_at_new_end_lands(self, fuse_mock):
        handler = _SfsAfterShrink(CONTENT)
        fs = _fake_write_fs(fuse_fs, handler)
        handle = fs.open("/file", os.O_RDWR)

        assert fs.truncate("/file", 1000, handle) == 0
        assert fs.write("/file", b"tail", 1000, handle) == 4
        assert handler.data == CONTENT[:1000] + b"tail"


class TestShrinkReplyAfterWait:
    """A shrink answered just after its wait runs out is not a failure."""

    def test_reply_in_second_wait_is_the_answer(self, fuse_mock):
        handler = _FakeHandlerFile(CONTENT)
        bridge = _fake_write_bridge(fuse_fs, handler)
        bridge._run_until_replies = MagicMock(side_effect=[NO_REPLY, _reply(4)])

        assert bridge.set_handle_size_reply(FH_ADDR, 4) == (4, 0)
        assert bridge.launcher.send_set_file_size.call_count == 1

    def test_no_reply_in_either_wait_sends_once(self, fuse_mock):
        """The handler may still hold the packet, so it is not sent again."""
        handler = _FakeHandlerFile(CONTENT)
        bridge = _fake_write_bridge(fuse_fs, handler)
        bridge._run_until_replies = MagicMock(side_effect=[NO_REPLY, NO_REPLY])

        assert bridge.set_handle_size_reply(FH_ADDR, 4) == (-1, 0)
        assert bridge.launcher.send_set_file_size.call_count == 1
        assert bridge._run_until_replies.call_count == 2

    def test_late_stop_is_retried_and_pinned_size_follows(self, fuse_mock):
        """FFS stops the shrink at a reader and the reply comes late; the
        retry still runs, and getattr of a file open from create() reports
        the size the handler reached."""
        handler = _FakeHandlerFile(b"")
        fs = _fake_write_fs(fuse_fs, handler)
        handle = fs.create("/file", 0o644)
        assert fs.write("/file", CONTENT, 0, handle) == len(CONTENT)
        _open_other(fs, handler, 2000)
        late = []
        set_size = handler.set_size

        def set_size_once_late(*args):
            packet = set_size(*args)
            if handler.set_sizes == [100]:
                late.append(True)
            return packet

        def wait():
            if late:
                late.clear()
                return NO_REPLY
            return handler.reply

        fs.bridge.launcher.send_set_file_size.side_effect = set_size_once_late
        fs.bridge._run_until_replies = MagicMock(side_effect=wait)

        assert fs.truncate("/file", 100, handle) == 0
        assert handler.set_sizes == [100, 100]
        assert handler.data == CONTENT[:100]
        assert fs.getattr("/file")["st_size"] == 100


class TestAfsShrinkPastAnotherHandle:
    """AROS afs: the retry runs after ERROR_SEEK_ERROR too."""

    @WAYS
    def test_shrink_is_retried(self, fuse_mock, use_handle):
        handler = _AfsFile(CONTENT)
        fs = _fake_write_fs(fuse_fs, handler)
        other = _open_other(fs, handler, 2000)
        handle = fs.open("/file", os.O_RDWR) if use_handle else None

        assert fs.truncate("/file", 100, handle) == 0
        assert handler.set_sizes == [100, 100]
        assert handler.data == CONTENT[:100]
        assert handler.others[OTHER] == 0
        assert fs._fh_cache[other]["pos"] is None


class TestHandlesPastLongMax:
    """A handle past 0x7FFFFFFF forgets its position only if it moved.

    The mount caches 0x80000000 for each such handle. The fake holds one
    file: a handle on it at 2000 stands in for one past 2 GiB on the same
    file, and a handle at 0, which no shrink stops at, for one on another
    file.
    """

    def _open_far(self, fs, handler, fh_addr=FAR, pos=0):
        far = _open_other(fs, handler, pos, fh_addr=fh_addr)
        fs._fh_cache[far]["pos"] = 0x80000000
        return far

    def _far_seeks(self, handler):
        return [s for s in handler.seeks if s[0] >= FAR]

    def test_moved_reader_reads_no_new_bytes(self, fuse_mock):
        """pfs3aio moved the reader to the new end of file, where XYZ is
        written next. Its read at its old offset no longer returns XYZ."""
        handler = _FakeHandlerFile(CONTENT)
        handler.moves_others = True
        fs = _fake_write_fs(fuse_fs, handler)
        far = self._open_far(fs, handler, pos=2000)
        handle = fs.open("/file", os.O_RDWR)

        assert fs.truncate("/file", 100, handle) == 0
        assert fs.write("/file", b"XYZ", 100, handle) == 3
        # No seek reaches 0x80000000, so the read fails.
        with pytest.raises(fuse_fs.FuseOSError) as exc_info:
            fs.read("/file", 16, 0x80000000, far)
        assert exc_info.value.args[0] == errno.EIO
        assert self._far_seeks(handler)[0] == (FAR, 0, fuse_fs.OFFSET_CURRENT)

    @pytest.mark.parametrize("reply", [
        _reply(-1, 219), _reply(-1, 0), _reply(-0x80000000),
    ], ids=["pfs3aio-refusal", "ffs-v0.2-no-code", "sfs-negative"])
    def test_unmoved_handle_keeps_its_position(self, fuse_mock, reply):
        """pfs3aio refuses the check past 2 GiB, FastFileSystem v0.2
        answers -1 with no error code, and SFS answers with a position that
        a signed LONG carries as negative."""
        handler = _FakeHandlerFile(CONTENT)
        fs = _fake_write_fs(fuse_fs, handler)
        far = self._open_far(fs, handler)
        seek = handler.seek

        def far_seek(state, fh_addr, offset, mode):
            packet = seek(state, fh_addr, offset, mode)
            if fh_addr == FAR:
                handler.reply = reply
            return packet

        fs.bridge.launcher.send_seek_handle.side_effect = far_seek

        assert fs.truncate("/file", 100) == 0
        assert self._far_seeks(handler) == [(FAR, 0, fuse_fs.OFFSET_CURRENT)]
        assert fs._fh_cache[far]["pos"] == 0x80000000

    def test_unanswered_check_stops_the_checks(self, fuse_mock):
        """A check that gets no reply may still be with the handler, so no
        other follows it, and the handles not checked forget too."""
        handler = _FakeHandlerFile(CONTENT)
        fs = _fake_write_fs(fuse_fs, handler)
        first = self._open_far(fs, handler)
        second = self._open_far(fs, handler, fh_addr=FAR + 1)
        seek = handler.seek

        def far_seek(state, fh_addr, offset, mode):
            packet = seek(state, fh_addr, offset, mode)
            if fh_addr >= FAR:
                handler.reply = NO_REPLY
            return packet

        fs.bridge.launcher.send_seek_handle.side_effect = far_seek

        assert fs.truncate("/file", 100) == 0
        assert len(self._far_seeks(handler)) == 1
        assert fs._fh_cache[first]["pos"] is None
        assert fs._fh_cache[second]["pos"] is None

    def test_unanswered_seek_to_start_sends_no_check(self, fuse_mock):
        """The truncating handle's seek to the start after the shrink may
        still be with the handler, so no check follows it, and the far
        handle forgets its position."""
        handler = _FakeHandlerFile(CONTENT)
        fs = _fake_write_fs(fuse_fs, handler)
        far = self._open_far(fs, handler)
        handle = fs.open("/file", os.O_RDWR)
        seek = handler.seek

        def start_seek_unanswered(state, fh_addr, offset, mode):
            packet = seek(state, fh_addr, offset, mode)
            if handler.set_sizes and fh_addr == FH_ADDR:
                handler.reply = NO_REPLY
            return packet

        fs.bridge.launcher.send_seek_handle.side_effect = start_seek_unanswered

        assert fs.truncate("/file", 100, handle) == 0
        assert handler.seeks[-1] == (FH_ADDR, 0, fuse_fs.OFFSET_BEGINNING)
        assert self._far_seeks(handler) == []
        assert fs._fh_cache[far]["pos"] is None
        assert fs._fh_cache[handle]["pos"] is None
