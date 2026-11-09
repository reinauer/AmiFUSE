"""Negotiate the WinFsp capabilities without a host FUSE library."""

import ctypes
from types import SimpleNamespace

import pytest

from amifuse.winfsp_fuse import READDIR_PLUS, _Connection, with_capabilities


@pytest.mark.parametrize("capable", [0, READDIR_PLUS, READDIR_PLUS | 1])
def test_readdir_plus_is_asked_for_when_offered(capable):
    conn = _Connection()
    conn.capable = capable
    conn.want = 32

    class FUSE:
        def init(self, pointer):
            assert pointer is not None
            return "initialized"

    adapter = object.__new__(with_capabilities(FUSE))
    adapter.operations = SimpleNamespace(_readdir_plus=False)
    assert adapter.init(ctypes.byref(conn)) == "initialized"
    granted = bool(capable & READDIR_PLUS)
    assert conn.want == 32 | (READDIR_PLUS if granted else 0)
    assert conn.capable == capable
    assert adapter.operations._readdir_plus is granted


def test_connection_layout_matches_winfsp():
    # struct fuse_conn_info: seven unsigned fields, then reserved[25].
    assert _Connection.want.offset == 6 * ctypes.sizeof(ctypes.c_uint)
    assert ctypes.sizeof(_Connection) == 32 * ctypes.sizeof(ctypes.c_uint)
