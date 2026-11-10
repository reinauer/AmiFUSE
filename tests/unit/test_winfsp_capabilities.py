"""Negotiate the WinFsp ABI without importing a host FUSE library."""

import ctypes
from types import SimpleNamespace

import pytest

from amifuse.winfsp_fuse import (
    CASE_INSENSITIVE, READDIR_PLUS, _Connection, with_capabilities)


@pytest.mark.parametrize("fold", [None, {97: 65}])
@pytest.mark.parametrize("capable", [0, CASE_INSENSITIVE])
@pytest.mark.parametrize("version,supported", [
    (None, False), ("unknown", False), ("1.10.0.0", False),
    ("1.11.0.0", True), ("2.1.0.0", True),
])
def test_case_folding_capability(monkeypatch, fold, capable, version, supported):
    monkeypatch.setattr("amifuse.platform.detect_fuse_backend",
                        lambda: {"version": version})
    conn = _Connection()
    conn.capable = capable
    conn.want = 32

    class FUSE:
        operations = SimpleNamespace(_name_fold=fold)

        def init(self, pointer):
            assert pointer is not None
            return "initialized"

    assert object.__new__(with_capabilities(FUSE)).init(ctypes.byref(conn)) == "initialized"
    assert conn.want == 32 | (capable if supported and fold is not None else 0)
    assert conn.capable == capable



@pytest.mark.parametrize("version", [None, "1.10.0.0", "2.1.0.0"])
@pytest.mark.parametrize("capable", [
    0, READDIR_PLUS, READDIR_PLUS | 1, READDIR_PLUS | CASE_INSENSITIVE])
def test_readdir_plus_is_asked_for_when_offered(monkeypatch, capable, version):
    # READDIR_PLUS doesn't depend on getpath, so any WinFsp version gets it.
    monkeypatch.setattr("amifuse.platform.detect_fuse_backend",
                        lambda: {"version": version})
    conn = _Connection()
    conn.capable = capable
    conn.want = 32

    class FUSE:
        def init(self, pointer):
            assert pointer is not None
            return "initialized"

    adapter = object.__new__(with_capabilities(FUSE))
    adapter.operations = SimpleNamespace(_readdir_plus=False, _name_fold=None)
    assert adapter.init(ctypes.byref(conn)) == "initialized"
    granted = bool(capable & READDIR_PLUS)
    assert conn.want == 32 | (READDIR_PLUS if granted else 0)
    assert conn.capable == capable
    assert adapter.operations._readdir_plus is granted


def test_connection_layout_matches_winfsp():
    # struct fuse_conn_info: seven unsigned fields, then reserved[25].
    assert _Connection.want.offset == 6 * ctypes.sizeof(ctypes.c_uint)
    assert ctypes.sizeof(_Connection) == 32 * ctypes.sizeof(ctypes.c_uint)

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
    adapter.operations = lambda op, path, fh: "/Caf\N{LATIN SMALL LETTER E WITH ACUTE}"
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


@pytest.mark.parametrize("fh", [None, 7])
def test_getpath_passes_the_handle_winfsp_gives(fh):
    class FileInfo(ctypes.Structure):
        _fields_ = [("flags", ctypes.c_int), ("fh", ctypes.c_uint64)]

    calls = []
    adapter = object.__new__(with_capabilities(object))
    adapter.encoding = "utf-8"
    adapter.operations = lambda op, path, handle: calls.append((op, path, handle)) or path
    fi = None if fh is None else ctypes.pointer(FileInfo(0, fh))
    buffer = ctypes.create_string_buffer(16)
    assert adapter.getpath(b"/new", buffer, 16, fi) == 0
    assert calls == [("getpath", "/new", fh)]
