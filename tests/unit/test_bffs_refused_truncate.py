"""A truncate that BFFS refuses fails instead of passing as a truncate to 0.

BFFS does not implement ACTION_SET_FILE_SIZE. Its read-write build answers
every one with dp_Res1 = DOSFALSE (0) and dp_Res2 = ERROR_OBJECT_NOT_FOUND
(205), and its read-only build with ERROR_DISK_WRITE_PROTECTED (214), and
leaves the file as it was. A dp_Res1 of 0 is also the reply to a successful
truncate to 0, so only dp_Res2 tells the two apart.
"""

import errno
import os

import pytest

from amifuse import fuse_fs
from tests.unit.test_fuse_fs import (
    NO_REPLY,
    _FakeHandlerFile,
    _fake_write_bridge,
    _fake_write_fs,
    _reply,
)

ORIGINAL = b"0123456789"
FH_ADDR = 0x3000

BFFS_REFUSALS = pytest.mark.parametrize("res2, expected", [
    (205, errno.EIO),
    (214, errno.EROFS),
], ids=["read-write", "read-only"])
LENGTHS = pytest.mark.parametrize("length", [0, 5], ids=["empty", "shrink"])


@LENGTHS
@BFFS_REFUSALS
def test_truncate_handle_fails_on_refusal(fuse_mock, length, res2, expected):
    handler = _FakeHandlerFile(ORIGINAL, set_size_reply=_reply(0, res2))
    bridge = _fake_write_bridge(fuse_fs, handler)

    assert bridge.truncate_handle(FH_ADDR, length) == (-1, res2)
    assert handler.set_sizes == [length]
    assert handler.data == ORIGINAL


@pytest.mark.parametrize("use_handle", [False, True], ids=["path", "handle"])
@pytest.mark.parametrize("res2, length, expected", [
    (205, 5, errno.EIO),
    (214, 0, errno.EROFS),
    (214, 5, errno.EROFS),
], ids=["read-write-shrink", "read-only-empty", "read-only-shrink"])
def test_truncate_raises_on_refusal(fuse_mock, use_handle, length, res2,
                                    expected):
    """The read-write build's refusal of a truncate to 0 is handled by
    opening the file again: test_bffs_overwrite.py."""
    handler = _FakeHandlerFile(ORIGINAL, set_size_reply=_reply(0, res2))
    fs = _fake_write_fs(fuse_fs, handler)
    handle = fs.open("/file", os.O_RDWR) if use_handle else None

    with pytest.raises(fuse_fs.FuseOSError) as exc_info:
        fs.truncate("/file", length, handle)
    assert exc_info.value.args[0] == expected
    assert handler.set_sizes == [length]
    assert handler.data == ORIGINAL


@pytest.mark.parametrize("use_handle", [False, True], ids=["path", "handle"])
def test_refused_truncate_of_empty_file_succeeds(fuse_mock, use_handle):
    """A truncate to 0 sends ACTION_SET_FILE_SIZE without measuring the end
    first, and BFFS refuses it even when the file is already empty, as one
    that create() made is. The file then has the length asked for."""
    handler = _FakeHandlerFile(b"", set_size_reply=_reply(0, 205))
    fs = _fake_write_fs(fuse_fs, handler)
    handle = fs.open("/file", os.O_RDWR) if use_handle else None

    assert fs.truncate("/file", 0, handle) == 0
    assert handler.set_sizes == [0]
    assert handler.data == b""


@pytest.mark.parametrize("data, expected", [
    (ORIGINAL, (-1, 205)),
    (b"", (0, 0)),
], ids=["full", "empty"])
def test_refusal_with_other_handles_is_not_sent_again(fuse_mock, data,
                                                      expected):
    """BFFS's refusal is not about other handles, so they stay where they
    are and the truncate is sent once."""
    other = 0x4000
    handler = _FakeHandlerFile(data, set_size_reply=_reply(0, 205))
    handler.others = {other: len(data)}
    bridge = _fake_write_bridge(fuse_fs, handler)

    assert bridge.truncate_handle(FH_ADDR, 0, [other]) == expected
    assert handler.set_sizes == [0]
    assert all(seek[0] != other for seek in handler.seeks)


def test_handler_refusing_every_packet_is_not_empty(fuse_mock):
    """A handler with no disk refuses the seeks with DOSFALSE too, which
    reads as an end of 0, so only BFFS's refusal measures the end."""
    handler = _FakeHandlerFile(ORIGINAL, set_size_reply=_reply(0, 226),
                               seek_reply=_reply(0, 226))
    bridge = _fake_write_bridge(fuse_fs, handler)

    assert bridge.truncate_handle(FH_ADDR, 0) == (-1, 226)
    assert handler.seeks == []
    assert handler.data == ORIGINAL


def test_refusal_when_end_cannot_be_measured_fails(fuse_mock):
    """Only a file measured as empty turns BFFS's refusal into success."""
    handler = _FakeHandlerFile(b"", set_size_reply=_reply(0, 205),
                               seek_mode_reply=(fuse_fs.OFFSET_END,
                                                _reply(-1, 225)))
    bridge = _fake_write_bridge(fuse_fs, handler)

    assert bridge.truncate_handle(FH_ADDR, 0) == (-1, 205)
    assert handler.set_sizes == [0]


@pytest.mark.parametrize("refusal, expected", [
    (NO_REPLY, (-1, 0)),
    (_reply(-1, 202), (-1, 202)),
], ids=["no-reply", "in-use"])
def test_other_refusal_of_empty_file_sends_no_seek(fuse_mock, refusal,
                                                   expected):
    """Only BFFS's DOSFALSE reply measures the end afterwards. With no
    reply, the handler may still hold the packet."""
    handler = _FakeHandlerFile(b"", set_size_reply=refusal)
    bridge = _fake_write_bridge(fuse_fs, handler)

    assert bridge.truncate_handle(FH_ADDR, 0) == expected
    assert handler.set_sizes == [0]
    assert handler.seeks == []
