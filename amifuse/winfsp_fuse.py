"""WinFsp capabilities and canonical paths missing from fusepy.

The layout and flags are WinFsp's public inc/fuse/fuse_common.h and fuse.h ABI.
Keep this adapter Windows-only; other FUSE implementations differ.
"""

import ctypes
import errno

READDIR_PLUS = 1 << 21  # FSP_FUSE_CAP_READDIR_PLUS
CASE_INSENSITIVE = 1 << 29
DELETE_ACCESS = 1 << 24
DELETE_OK = 0x40000000


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
    from .platform import detect_fuse_backend

    version = detect_fuse_backend().get("version")
    try:
        parts = tuple(int(part) for part in version.split(".")[:2])
        getpath_supported = len(parts) == 2 and parts >= (1, 11)
    except (AttributeError, ValueError):
        getpath_supported = False

    class WinFspFUSE(fuse_class):
        def __init__(self, *args, **kwargs):
            import fuse
            if getpath_supported:
                _extend_operations(fuse)
            super().__init__(*args, **kwargs)

        def getpath(self, path, buffer, size, fi):
            # After create() WinFsp passes the new file's handle, which
            # spares listing its parent while the handler refuses its lock.
            fh = fi.contents.fh if fi else None
            canonical = self.operations("getpath", path.decode(self.encoding), fh)
            encoded = canonical.encode(self.encoding) + b"\0"
            if len(encoded) > size:
                return -errno.ENAMETOOLONG
            ctypes.memmove(buffer, encoded, len(encoded))
            return 0

        def init(self, conn):
            if conn:
                info = ctypes.cast(conn, ctypes.POINTER(_Connection)).contents
                # With READDIR_PLUS, WinFsp takes each entry's attributes
                # from readdir(). Without it, it asks getattr() for each,
                # under the path the directory was opened with, which a
                # rename through that handle has made stale: every entry
                # then fails and the listing comes back empty (#427).
                if info.capable & READDIR_PLUS:
                    info.want |= READDIR_PLUS
                    self.operations._readdir_plus = True
                # Pre-1.11 WinFsp drops getpath from the operations table;
                # declaring case insensitivity there makes case-only
                # renames silent no-ops. Unknown versions keep the old
                # behavior too. Windows folds Unicode even on DOS0/DOS1's
                # ASCII-only fold, so non-ASCII case pairs can still alias
                # on those volumes.
                wanted = DELETE_ACCESS
                if getpath_supported and self.operations._name_fold is not None:
                    wanted |= CASE_INSENSITIVE
                info.want |= info.capable & wanted
            return super().init(conn)

    return WinFspFUSE
