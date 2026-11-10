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


def test_delete_access_capability():
    from amifuse.winfsp_fuse import DELETE_ACCESS
    conn = _Connection()
    conn.capable = DELETE_ACCESS

    class FUSE:
        operations = SimpleNamespace(_name_fold=None)

        def init(self, pointer):
            return 0

    object.__new__(with_capabilities(FUSE)).init(ctypes.byref(conn))
    assert conn.want == DELETE_ACCESS


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("busy", [False, True])
def test_delete_access_checks_amiga_protection(directory, busy):
    import errno
    from amifuse.fuse_fs import AmigaFuseFS, FuseOSError
    from amifuse.winfsp_fuse import DELETE_OK
    info = {"name": "locked", "protection": 1,
            "dir_type": 2 if directory else -3}
    bridge = SimpleNamespace(_write_enabled=True,
        state=SimpleNamespace(crashed=False),
        stat_path=lambda path: None if busy else info,
        list_dir_path=lambda path: [info])
    ops = AmigaFuseFS(bridge)
    ops._winfsp = True
    with pytest.raises(FuseOSError) as caught:
        ops.access("/locked", DELETE_OK)
    assert caught.value.errno == errno.EACCES
    info["protection"] = 0
    assert ops.access("/locked", DELETE_OK) == 0

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


def test_delete_access_on_read_only_mount():
    import errno
    import os
    from amifuse.fuse_fs import AmigaFuseFS, FuseOSError
    from amifuse.winfsp_fuse import DELETE_OK

    ops = AmigaFuseFS(SimpleNamespace(_write_enabled=False))
    ops._winfsp = True
    with pytest.raises(FuseOSError) as caught:
        ops.access("/file", DELETE_OK)
    assert caught.value.errno == errno.EROFS
    assert ops.access("/file", os.R_OK) == 0


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("protected", [False, True])
@pytest.mark.parametrize("replacement", [None, 0, 1])
def test_delete_access_uses_renamed_object_protection(directory, protected, replacement):
    import errno
    from amifuse.fuse_fs import AmigaFuseFS, FuseOSError
    from amifuse.winfsp_fuse import DELETE_OK

    paths = []
    def stat(path):
        paths.append(path)
        if path == "/after":
            return {"name": "after", "protection": int(protected)}
        return None if replacement is None else {"name": "before", "protection": replacement}
    bridge = SimpleNamespace(_write_enabled=True,
        state=SimpleNamespace(crashed=False), stat_path=stat,
        list_dir_path=lambda path: [])
    ops = AmigaFuseFS(bridge)
    ops._winfsp = True
    ops._fh_cache[42] = {"path": "/after", "opened_as": "/before",
        **({"dir_handle": True} if directory else {"fh_addr": 7})}
    # The same call comes when /before is opened for delete-on-close, so an
    # object at /before is checked too, and either one's protection
    # refuses the delete (#438).
    if protected or replacement == 1:
        with pytest.raises(FuseOSError) as caught:
            ops.access("/before", DELETE_OK)
        assert caught.value.errno == errno.EACCES
    else:
        assert ops.access("/before", DELETE_OK) == 0
    assert paths == (["/after"] if protected else ["/after", "/before"])


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
