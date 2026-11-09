"""fusepy's operations table laid out as WinFsp's (#434)."""

import ctypes
import types

from amifuse.fuse_fs import _winfsp_operations_order

GETATTR = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
READLINK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                            ctypes.c_size_t)


def _fusepy():
    class Operations(ctypes.Structure):
        # fusepy's order, which is libfuse's.
        _fields_ = [("getattr", GETATTR), ("readlink", READLINK),
                    ("getdir", ctypes.c_void_p), ("mknod", ctypes.c_void_p)]

    return types.SimpleNamespace(fuse_operations=Operations)


def _names(module):
    return [field[0] for field in module.fuse_operations._fields_]


def test_readlink_follows_getdir_as_in_winfsp():
    module = _fusepy()
    _winfsp_operations_order(module)
    assert _names(module) == ["getattr", "getdir", "readlink", "mknod"]
    fields = dict(module.fuse_operations._fields_)
    assert fields["readlink"] is READLINK
    assert module.fuse_operations.readlink.offset == 2 * ctypes.sizeof(ctypes.c_void_p)


def test_a_table_in_winfsp_order_stays():
    module = _fusepy()
    _winfsp_operations_order(module)
    table = module.fuse_operations
    _winfsp_operations_order(module)
    assert module.fuse_operations is table
