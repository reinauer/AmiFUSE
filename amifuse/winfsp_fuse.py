"""WinFsp capabilities that fusepy's init wrapper does not expose.

The layout and flags are WinFsp's public inc/fuse/fuse_common.h ABI.
Keep this adapter Windows-only; other FUSE implementations differ.
"""

import ctypes

CASE_INSENSITIVE = 1 << 29
DELETE_ACCESS = 1 << 24
DELETE_OK = 0x40000000


class _Connection(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint) for name in (
        "proto_major", "proto_minor", "async_read", "max_write",
        "max_readahead", "capable", "want",
    )] + [("reserved", ctypes.c_uint * 25)]


def with_capabilities(fuse_class):
    class WinFspFUSE(fuse_class):
        def init(self, conn):
            if conn:
                info = ctypes.cast(conn, ctypes.POINTER(_Connection)).contents
                wanted = DELETE_ACCESS
                if self.operations._name_fold is not None:
                    wanted |= CASE_INSENSITIVE
                info.want |= info.capable & wanted
            return super().init(conn)

    return WinFspFUSE
