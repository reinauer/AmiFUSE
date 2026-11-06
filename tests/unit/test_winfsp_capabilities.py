"""Negotiate the WinFsp ABI without importing a host FUSE library."""

import ctypes
from types import SimpleNamespace

import pytest

from amifuse.winfsp_fuse import CASE_INSENSITIVE, _Connection, with_capabilities


@pytest.mark.parametrize("fold", [None, {97: 65}])
@pytest.mark.parametrize("capable", [0, CASE_INSENSITIVE])
def test_case_folding_capability(fold, capable):
    conn = _Connection()
    conn.capable = capable
    conn.want = 32

    class FUSE:
        operations = SimpleNamespace(_name_fold=fold)

        def init(self, pointer):
            assert pointer is not None
            return "initialized"

    assert object.__new__(with_capabilities(FUSE)).init(ctypes.byref(conn)) == "initialized"
    assert conn.want == 32 | (capable if fold is not None else 0)
    assert conn.capable == capable


def test_getpath_extension_preserves_existing_offsets():
    from amifuse.winfsp_fuse import _extend_operations

    class Original(ctypes.Structure):
        _fields_ = [("getattr", ctypes.c_void_p), ("ioctl", ctypes.c_void_p)]

    fuse = SimpleNamespace(fuse_operations=Original, fuse_file_info=ctypes.c_uint)
    _extend_operations(fuse)
    extended = fuse.fuse_operations
    assert extended.ioctl.offset == Original.ioctl.offset
    assert extended.getpath.offset == Original.ioctl.offset + 6 * ctypes.sizeof(ctypes.c_void_p)
    assert not extended().getpath
    _extend_operations(fuse)
    assert fuse.fuse_operations is extended


@pytest.mark.parametrize("size", [1, 7, 8, 20])
def test_getpath_buffer_is_terminated_and_bounded(size):
    import errno

    adapter = object.__new__(with_capabilities(object))
    adapter.encoding = "utf-8"
    adapter.operations = lambda op, path: "/Caf\N{LATIN SMALL LETTER E WITH ACUTE}"
    buffer = ctypes.create_string_buffer(b"x" * 20)
    expected = "/Caf\N{LATIN SMALL LETTER E WITH ACUTE}".encode() + b"\0"
    result = adapter.getpath(b"/caf\xc3\xa9", buffer, size, None)
    if size < len(expected):
        assert result == -errno.ENAMETOOLONG
        assert buffer.raw == b"x" * 20 + b"\0"
    else:
        assert result == 0
        assert buffer.raw[:len(expected)] == expected
        assert buffer.raw[len(expected):20] == b"x" * (20 - len(expected))
