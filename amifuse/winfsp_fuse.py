"""WinFsp capabilities missing from fusepy.

The layout and flags are WinFsp's public inc/fuse/fuse_common.h ABI. Keep
this adapter Windows-only; other FUSE implementations differ.
"""

import ctypes

READDIR_PLUS = 1 << 21  # FSP_FUSE_CAP_READDIR_PLUS


class _Connection(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint) for name in (
        "proto_major", "proto_minor", "async_read", "max_write",
        "max_readahead", "capable", "want",
    )] + [("reserved", ctypes.c_uint * 25)]


def with_capabilities(fuse_class):
    class WinFspFUSE(fuse_class):
        def init(self, conn):
            # With READDIR_PLUS, WinFsp takes each entry's attributes from
            # readdir(). Without it, it asks getattr() for each, under the
            # path the directory was opened with, which a rename through
            # that handle has made stale: every entry then fails and the
            # listing comes back empty (#427).
            if conn:
                info = ctypes.cast(conn, ctypes.POINTER(_Connection)).contents
                if info.capable & READDIR_PLUS:
                    info.want |= READDIR_PLUS
                    self.operations._readdir_plus = True
            return super().init(conn)

    return WinFspFUSE
