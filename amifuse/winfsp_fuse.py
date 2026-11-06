"""WinFsp capabilities that fusepy's init wrapper does not expose.

The layout and flags are WinFsp's public inc/fuse/fuse_common.h ABI.
Keep this adapter Windows-only; other FUSE implementations differ.
"""

import ctypes

CASE_INSENSITIVE = 1 << 29


class _Connection(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint) for name in (
        "proto_major", "proto_minor", "async_read", "max_write",
        "max_readahead", "capable", "want",
    )] + [("reserved", ctypes.c_uint * 25)]


def with_capabilities(fuse_class):
    class WinFspFUSE(fuse_class):
        def init(self, conn):
            if conn and self.operations._name_fold is not None:
                info = ctypes.cast(conn, ctypes.POINTER(_Connection)).contents
                info.want |= info.capable & CASE_INSENSITIVE
            return super().init(conn)

    return WinFspFUSE
