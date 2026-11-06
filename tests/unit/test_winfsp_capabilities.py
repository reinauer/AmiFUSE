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


def test_delete_access_capability():
    from amifuse.winfsp_fuse import DELETE_ACCESS
    conn = _Connection()
    conn.capable = DELETE_ACCESS

    class FUSE:
        operations = SimpleNamespace(_name_fold=None)

        def init(self, pointer):
            return 0

    with_capabilities(FUSE)().init(ctypes.byref(conn))
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
