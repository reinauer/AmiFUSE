"""WinFsp capabilities and canonical paths missing from fusepy.

The layout and flags are WinFsp's public inc/fuse/fuse_common.h and fuse.h ABI.
Keep this adapter Windows-only; other FUSE implementations differ.
"""

import ctypes
import errno

CASE_INSENSITIVE = 1 << 29


class _Connection(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint) for name in (
        "proto_major", "proto_minor", "async_read", "max_write",
        "max_readahead", "capable", "want",
    )] + [("reserved", ctypes.c_uint * 25)]


def _extend_operations(fuse):
    """Append the WinFsp getpath slot to fusepy's FUSE 2.8 table."""
    fields = fuse.fuse_operations._fields_
    if any(field[0] == "getpath" for field in fields):
        return
    # WinFsp preserves the FUSE 2.9 slots before its getpath extension.
    # fusepy 3.0.1 ends at ioctl; reject another layout rather than guess.
    if fields[-1][0] != "ioctl":
        raise RuntimeError("Unsupported fusepy operations layout for WinFsp")

    class WinFspOperations(ctypes.Structure):
        _fields_ = fields + [
            (name, ctypes.c_void_p)
            for name in ("poll", "write_buf", "read_buf", "flock", "fallocate")
        ] + [("getpath", ctypes.CFUNCTYPE(
            ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t,
            ctypes.POINTER(fuse.fuse_file_info)))]

    fuse.fuse_operations = WinFspOperations


def with_capabilities(fuse_class):
    class WinFspFUSE(fuse_class):
        def __init__(self, *args, **kwargs):
            import fuse
            _extend_operations(fuse)
            super().__init__(*args, **kwargs)

        def getpath(self, path, buffer, size, fi):
            canonical = self.operations("getpath", path.decode(self.encoding))
            encoded = canonical.encode(self.encoding) + b"\0"
            if len(encoded) > size:
                return -errno.ENAMETOOLONG
            ctypes.memmove(buffer, encoded, len(encoded))
            return 0

        def init(self, conn):
            if conn and self.operations._name_fold is not None:
                info = ctypes.cast(conn, ctypes.POINTER(_Connection)).contents
                info.want |= info.capable & CASE_INSENSITIVE
            return super().init(conn)

    return WinFspFUSE
