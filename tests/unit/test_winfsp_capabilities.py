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

    assert with_capabilities(FUSE)().init(ctypes.byref(conn)) == "initialized"
    assert conn.want == 32 | (capable if fold is not None else 0)
    assert conn.capable == capable
