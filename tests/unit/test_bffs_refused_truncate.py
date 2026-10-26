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
@LENGTHS
@BFFS_REFUSALS
def test_truncate_raises_on_refusal(fuse_mock, use_handle, length, res2,
                                    expected):
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
    first, as an open with O_TRUNC does, and BFFS refuses it even when the
    file is already empty. The file then has the length asked for."""
    handler = _FakeHandlerFile(b"", set_size_reply=_reply(0, 205))
    fs = _fake_write_fs(fuse_fs, handler)
    handle = fs.open("/file", os.O_RDWR) if use_handle else None

    assert fs.truncate("/file", 0, handle) == 0
    assert handler.set_sizes == [0]
    assert handler.data == b""


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
