"""
FUSE bridge that forwards basic filesystem ops into the Amiga handler
via our vamos bootstrap. Read/write is experimental and enabled with --write.
"""

import cProfile
import pstats

import argparse
import errno
import itertools
import math
import operator
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import unicodedata

from enum import Enum
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from .platform import kill_mount_owner_processes

FUSE = None


class FuseOSError(OSError):
    """FUSE-compatible error that does not require importing fusepy."""

    def __init__(self, error):
        super().__init__(error, os.strerror(error))


# Host names the handler can't take as typed would name another file. In
# AmigaDOS a colon ends a device or volume name, so the handler would read a
# name holding one as another path (#242). The handler takes names in
# Latin-1, so a character outside it would reach the handler as "?" (#258).
# Creating such a name gets EINVAL and looking one up gets ENOENT, before any
# packet. mknod, symlink and link refuse every call already.
#
# A name counts in NFC. macOS can pass "cafe" and U+0301, and only the composed
# form fits Latin-1. Names the handler lists are NFC, as Latin-1 text always
# is, so both spellings of a host name are one object on the volume, and
# readdir shows the composed one.
_COLON_CREATE_OPS = frozenset(("create", "mkdir"))
_COLON_UNCHECKED_OPS = frozenset(("mknod", "symlink", "link"))


def _unsendable_path_reason(path):
    """Why the handler can't take volume path `path` as typed, or None."""
    path = unicodedata.normalize("NFC", path)
    if ":" in path:
        return "a colon ends an AmigaDOS device or volume name"
    try:
        path.encode("latin-1")
    except UnicodeEncodeError as exc:
        return (f"character U+{ord(path[exc.start]):04X} is outside Latin-1, "
                "the character set of AmigaDOS names")
    return None


def _nfc_path_args(op, args):
    """`args` with the volume paths of `op` in NFC.

    At dispatch, so that the path caches and the handle table, which are
    keyed on the path, see one spelling of a name.
    """
    if op in _COLON_UNCHECKED_OPS:
        return args
    count = 2 if op == "rename" else 1
    return tuple(unicodedata.normalize("NFC", arg)
                 if index < count and isinstance(arg, str) else arg
                 for index, arg in enumerate(args))


def _colon_path_errno(op, args):
    """The errno refusing a volume path in `op`'s args that the handler
    can't take as typed (a colon or a character outside Latin-1), or None."""
    if op in _COLON_UNCHECKED_OPS:
        return None
    paths = args[:2] if op == "rename" else args[:1]
    for index, path in enumerate(paths):
        if isinstance(path, str) and _unsendable_path_reason(path):
            if op in _COLON_CREATE_OPS or (op == "rename" and index == 1):
                return errno.EINVAL
            return errno.ENOENT
    return None


class _FuseOperations:
    """The fusepy Operations defaults used by AmigaFuseFS, plus the refusal
    at dispatch of host names the handler can't take as typed."""

    bmap = None
    lock = None

    def __call__(self, op, *args):
        if not hasattr(self, op):
            raise FuseOSError(errno.EFAULT)
        args = _nfc_path_args(op, args)
        error = _colon_path_errno(op, args)
        if error is not None:
            raise FuseOSError(error)
        return getattr(self, op)(*args)

    def fsyncdir(self, path, datasync, fh):
        return 0

    def ioctl(self, path, cmd, arg, fip, flags, data):
        raise FuseOSError(errno.ENOTTY)

    def link(self, target, source):
        raise FuseOSError(errno.EROFS)

    def mknod(self, path, mode, dev):
        raise FuseOSError(errno.EROFS)

    def readlink(self, path):
        raise FuseOSError(errno.ENOENT)

    def symlink(self, target, source):
        raise FuseOSError(errno.EROFS)

from . import driver_resolver
from .amiga_time import host_time, to_datestamp
from .driver_resolver import DriverNotFound, ResolvedDriver
from .driver_runtime import BlockDeviceBackend, ImageInUseError
from .rdb_inspect import dos_type_tag, escape_text
from .vamos_runner import VamosHandlerRuntime
from .bootstrap import BootstrapAllocator
from .startup_runner import (
    ACTION_CREATE_DIR,
    ACTION_FINDINPUT,
    ACTION_FINDOUTPUT,
    ACTION_FINDUPDATE,
    ACTION_LOCATE_OBJECT,
    HandlerLauncher,
    OFFSET_BEGINNING,
    OFFSET_CURRENT,
    OFFSET_END,
    _packet_type_name,
)
from amitools.vamos.schedule import RunResult  # type: ignore
from .emu_thread import EmuThread, route_methods_through_emu
from amitools.vamos.libstructs.dos import FileInfoBlockStruct, FileHandleStruct, DosPacketStruct, InfoDataStruct  # type: ignore
from amitools.vamos.lib.dos.DosProtection import DosProtection  # type: ignore
from amitools.util.Win32Disk import is_windows_disk  # type: ignore

from . import __version__

__banner__ = f"amifuse {__version__} - Copyright (C) 2025-2026 by Stefan Reinauer"


# AmigaDOS error codes that mean the object genuinely does not exist. Only
# these justify a negative-cache entry: transient failures such as
# ERROR_OBJECT_IN_USE (202) must not make an existing file report ENOENT
# for the cache TTL.
ERROR_OBJECT_IN_USE = 202
ERROR_DIR_NOT_FOUND = 204
ERROR_OBJECT_NOT_FOUND = 205
_NOT_FOUND_ERRORS = frozenset({ERROR_DIR_NOT_FOUND, ERROR_OBJECT_NOT_FOUND})

# ACTION_SEEK fails with this code when asked to move past the end of the
# file, which AmigaDOS does not allow. The handle stays where it was. AROS
# afs also uses it for a negative offset and for a file whose extension
# blocks it cannot read, so it does not prove the offset is past the end.
ERROR_SEEK_ERROR = 219

# ExamineNext signals normal directory exhaustion with this error.
ERROR_NO_MORE_ENTRIES = 232

# ACTION_SEEK and ACTION_SET_FILE_SIZE carry the offset in dp_Arg2, a
# signed LONG.
_LONG_MIN = -0x80000000
_LONG_MAX = 0x7FFFFFFF

# The largest file a write may leave. Sequential writes could grow a file
# further, but a seek could not reach its end and fib_Size, also a signed
# LONG, could not hold its size.
_MAX_FILE_SIZE = _LONG_MAX


class SeekResult(Enum):
    """How the handler answered ACTION_SEEK; see HandlerBridge.seek_handle."""

    OK = "ok"
    PAST_END = "past_end"  # refused with ERROR_SEEK_ERROR
    FAILED = "failed"  # any other refusal, no reply, or an offset out of range


# The most bytes one ACTION_READ or ACTION_WRITE carries. Larger reads and
# writes go out in packets of this size, so that the buffer in emulated
# memory stays small and each packet is a short piece of work for the
# handler. Another size must still fit two limits:
# - The buffer is FUSE_READBUF, which grows to the largest packet and stays
#   for the life of the mount, out of 8 MiB of emulated memory.
# - The handler's work on one packet, on OFS about 16.3 million cycles per
#   MiB written and 9.8 million per MiB read, must end within the reply wait
#   of _run_until_replies.
IO_CHUNK = 64 * 1024

# The size of the host-side buffer of zeros that fills the gap before a
# write past end of file, one packet's worth, so a large gap needs no more
# memory than an ordinary write.
ZERO_FILL_CHUNK = IO_CHUNK

# AmigaDOS error codes from a failed ACTION_WRITE or ACTION_SET_FILE_SIZE
# that have a closer errno than EIO. ERROR_WRITE_PROTECTED is a file whose
# protection bits forbid writing.
ERROR_DISK_WRITE_PROTECTED = 214
ERROR_DISK_FULL = 221
ERROR_WRITE_PROTECTED = 223
_WRITE_ERRNOS = {
    ERROR_DISK_WRITE_PROTECTED: errno.EROFS,
    ERROR_DISK_FULL: errno.ENOSPC,
    ERROR_WRITE_PROTECTED: errno.EACCES,
}

# AmigaDOS error codes from a failed ACTION_RENAME_OBJECT, and the errno
# rename() reports for each. FastFileSystem, pfs3aio and SmartFilesystem
# all refuse a rename onto an existing name with ERROR_OBJECT_EXISTS,
# whether the existing object is a file or a directory.
ERROR_OBJECT_EXISTS = 203
ERROR_DIRECTORY_NOT_EMPTY = 216
ERROR_DELETE_PROTECTED = 222
_RENAME_ERRNOS = {
    **_WRITE_ERRNOS,
    ERROR_OBJECT_IN_USE: errno.EBUSY,
    ERROR_OBJECT_EXISTS: errno.EEXIST,
    ERROR_DIR_NOT_FOUND: errno.ENOENT,
    ERROR_OBJECT_NOT_FOUND: errno.ENOENT,
    ERROR_DIRECTORY_NOT_EMPTY: errno.ENOTEMPTY,
    ERROR_DELETE_PROTECTED: errno.EACCES,
}

# Busy-delete deferral and non-empty handling stay in the callbacks.
# Other refusals use the same file/disk errors.
_DELETE_ERRNOS = {
    **_WRITE_ERRNOS,
    ERROR_OBJECT_IN_USE: errno.EBUSY,
    ERROR_OBJECT_NOT_FOUND: errno.ENOENT,
    ERROR_DELETE_PROTECTED: errno.EACCES,
}

# AmigaDOS error codes from a failed ACTION_SET_DATE, and the errno
# utimens() reports for each.
_SET_DATE_ERRNOS = {
    **_WRITE_ERRNOS,
    ERROR_OBJECT_IN_USE: errno.EBUSY,
    ERROR_OBJECT_NOT_FOUND: errno.ENOENT,
}

# AmigaDOS error codes a handler can return when it refuses ACTION_DISK_INFO.
# ERROR_ACTION_NOT_KNOWN is deliberately absent from the unmounted set in
# HandlerBridge._UNMOUNTED_DISK_ERRORS: it means the handler does not
# implement the packet, which says nothing about the volume.
ERROR_ACTION_NOT_KNOWN = 209
ERROR_DEVICE_NOT_MOUNTED = 218
ERROR_NOT_A_DOS_DISK = 225
ERROR_NO_DISK = 226

# id_DiskType values from dos/dos.h. ID_MSDOS_DISK is not an unusable disk:
# CrossDOS reports it for a volume it has successfully mounted.
ID_NO_DISK_PRESENT = 0xFFFFFFFF  # -1
ID_UNREADABLE_DISK = 0x42414400  # 'BAD\0'
ID_NOT_REALLY_DOS = 0x4E444F53  # 'NDOS'
ID_KICKSTART_DISK = 0x4B49434B  # 'KICK'
ID_MSDOS_DISK = 0x4D534400  # 'MSD\0'
ID_BUSY = 0x42555359  # 'BUSY'


class MountState(NamedTuple):
    """What HandlerBridge.is_mounted() concluded, and what it saw.

    Attribute access rather than unpacking: this result has grown a field
    twice now, and each time every call site and test mock had to change.

    mounted is None when the handler gave too little to decide. info is the
    ACTION_DISK_INFO payload, absent when the handler refused the packet
    rather than answering it -- in which case dos_error carries the res2
    code that produced the verdict.
    """
    mounted: Optional[bool]
    info: Optional[Dict]
    reason: Optional[str]
    dos_error: Optional[int] = None


class StartupDiskRejected(SystemExit):
    """An explicit negative startup reply, rather than a handler failure."""

    def __init__(self, res1: int, res2: int):
        self.res1 = res1
        self.res2 = res2
        reasons = {
            0: "no reason code supplied (wrong --driver for this filesystem?)",
            218: "device not mounted",
            225: "not a DOS disk",
            226: "no disk present",
        }
        reason = reasons.get(res2, f"DOS error {res2}")
        self.reason = f"handler rejected the disk: {reason}"
        super().__init__(f"Error: no usable volume mounted: {self.reason}")

# Upper bound for the path-keyed metadata caches. They are TTL-based but
# otherwise unbounded, so a full traversal of a huge image would pin every
# path in memory for the TTL. Entries are inserted in timestamp order, so
# dropping from the front of the dict approximates dropping the oldest.
_CACHE_MAX_ENTRIES = 50_000


def _prune_cache(cache: Dict, keep: Set = frozenset()) -> None:
    """Drop the oldest half of a cache dict once it reaches the size cap.

    Keys in keep stay. The caller must hold the lock protecting the cache.
    """
    if len(cache) < _CACHE_MAX_ENTRIES:
        return
    for key in list(cache.keys())[: len(cache) // 2]:
        if key not in keep:
            del cache[key]


# How handlers compare names, as str.translate() tables. The bridge sends a
# name in Latin-1 (_alloc_bstr), so a character's code is the byte the handler
# compares, and any other character can't reach it (_FuseOperations).
# str.lower() is not the rule: it folds \xc9 where a handler folds only a-z,
# and the Kelvin sign to k. From looking up every byte in another spelling on
# each handler: 0xFF does not fold onto 0xDF, nor 0xF7 onto 0xD7.
_FOLD_ASCII = {c: c - 0x20 for c in range(0x61, 0x7B)}
_FOLD_LATIN1 = {**_FOLD_ASCII,
                **{c: c - 0x20 for c in range(0xE0, 0xFF) if c != 0xF7}}
_NAME_FOLDS = {"ascii": _FOLD_ASCII, "latin1": _FOLD_LATIN1}

# DOS types with the low byte masked off, for HandlerBridge.name_fold().
_DOS_FAMILY = 0x444F5300  # "DOS\x"
_PFS_FAMILIES = (0x50465300, 0x50445300)  # "PFS\x", "PDS\x"
_SFS_FAMILY = 0x53465300  # "SFS\x"
# struct fsRootBlock in SmartFilesystem: the block's id, and its bits field.
_SFS_ROOT_ID = b"SFS\0"
_SFS_ROOT_BITS_OFFSET = 20
_SFS_ROOTBITS_CASESENSITIVE = 0x80


def _renamed_path(path: str, old: str, new: str) -> Optional[str]:
    """Return what path becomes when old is renamed to new.

    None when path is neither old nor below it.
    """
    if path == old:
        return new
    if path.startswith(old + "/"):
        return new + path[len(old):]
    return None


def _handle_path(entry: Optional[Dict], path: str) -> str:
    """Return the path of the file or directory an open handle names.

    rename() keeps a handle's path current, while WinFsp goes on passing
    the name the file was opened under, so the handle's path wins over
    the caller's. path is the answer for a handle that has none.
    """
    return (entry or {}).get("path") or path


def _require_fuse():
    global FUSE

    if FUSE is not None:
        return FUSE

    try:
        from fuse import FUSE as fuse_class  # type: ignore
    except (ImportError, OSError):
        raise SystemExit(
            "Python FUSE bindings or a native FUSE library are unavailable. "
            "Install fusepy (pip install fusepy) and a supported FUSE backend."
        ) from None

    FUSE = fuse_class
    return FUSE


def _handler_has_crashed(obj) -> bool:
    state = getattr(obj, "state", None)
    return getattr(state, "crashed", False) is True


def _raise_if_handler_crashed(obj, context: str):
    if _handler_has_crashed(obj):
        raise SystemExit(f"Filesystem handler crashed during {context}.")


# The bridge's FileInfoBlock is 4 bytes longer than the struct's 260. Its
# fib_FileName holds 108 bytes, but SmartFilesystem copies a volume name of up
# to 255 characters into it whole when the root is examined, as a BSTR at
# offset 8 (#287).
_FIB_SIZE = 8 + 256


def _parse_fib(mem, fib_addr: int) -> Dict:
    """Decode a FileInfoBlock into a simple dict.

    Uses direct memory reads with known offsets from FileInfoBlockStruct:
      fib_DiskKey: ULONG at 0
      fib_DirEntryType: LONG at 4
      fib_FileName: ARRAY(UBYTE,108) at 8
      fib_Protection: LONG at 116
      fib_EntryType: LONG at 120
      fib_Size: ULONG at 124
      fib_NumBlocks: LONG at 128
      fib_Date: DateStamp (ds_Days, ds_Minute, ds_Tick: 3 LONGs) at 132

    "mtime" is fib_Date as host time. "disk_key" is only a handler's own
    number: pfs3aio and ODFileSystem give it to many entries.

    A name is read no further than fib_FileName, so a longer one gives its
    first 107 characters: the handler fills in the fields after it too, and
    SmartFilesystem's empty fib_Comment puts a NUL into a name that reaches it.
    """
    disk_key = mem.r32(fib_addr)        # fib_DiskKey (ULONG)
    dir_type = mem.r32s(fib_addr + 4)   # fib_DirEntryType (signed LONG)
    protection = mem.r32(fib_addr + 116)  # fib_Protection (LONG)
    size = mem.r32(fib_addr + 124)      # fib_Size (ULONG)
    num_blocks = mem.r32s(fib_addr + 128)      # fib_NumBlocks (LONG)
    name_bytes = mem.r_block(fib_addr + 8, 108)  # fib_FileName starts at offset 8
    name_len = name_bytes[0]
    name = name_bytes[1 : 1 + name_len].decode("latin-1", errors="ignore")
    mtime = host_time(mem.r32s(fib_addr + 132), mem.r32s(fib_addr + 136),
                      mem.r32s(fib_addr + 140))
    return {
        "disk_key": disk_key,
        "dir_type": dir_type,
        "size": size,
        "name": name,
        "protection": protection,
        "num_blocks": num_blocks,
        "mtime": mtime,
    }


class HandlerBridge:
    """Maintains the handler state and issues DOS packets synchronously."""

    def __init__(
        self,
        image: Path,
        driver: Path,
        block_size: Optional[int] = None,
        read_only: bool = True,
        debug: bool = False,
        trace: bool = False,
        partition: Optional[str] = None,
        driver_name: Optional[str] = None,
        adf_info=None,
        iso_info=None,
        remove_driver: bool = False,
    ):
        self._lock = threading.RLock()  # Reentrant lock for thread safety
        self._debug = debug
        # What load errors call the driver; see load_handler.
        self._driver_name = driver_name
        self._adf_info = adf_info
        self._iso_info = iso_info
        # driver is a temporary file, removed once it is loaded.
        self._remove_driver = remove_driver
        self.backend = None
        self.vh = None
        self._closed = False
        # Set when a wait runs out of busy cycles; see _run_until_replies.
        self._busy_ran_out = False
        # Cycles the idle hook has run such a handler on without it waiting
        # or replying; see run_due_timers.
        self._idle_busy_cycles = 0
        # Called from the idle hook once the handler is found crashed.
        self._on_crash = None
        # The emulation runs on this thread: the machine and the scheduler
        # are made here, and every method below runs here (emu_thread.py).
        self._emu = EmuThread()
        self._emu.start()
        try:
            self._emu.call(self._initialize, image, driver, block_size, read_only,
                           debug, trace, partition, adf_info, iso_info)
        except BaseException:
            # A failed constructor has no caller that can close it. Preserve
            # the startup error even if tearing down partial state also fails.
            try:
                self.close()
            except BaseException:
                pass
            raise

    def _initialize(self, image, driver, block_size, read_only, debug,
                    trace, partition, adf_info, iso_info):
        self.backend = BlockDeviceBackend(
            image, block_size=block_size, read_only=read_only, adf_info=adf_info,
            iso_info=iso_info, partition_scope=True, partition=partition,
        )
        self.backend.open()
        mbr_idx = self.backend.mbr_partition_index
        self.vh = VamosHandlerRuntime()
        # Use 68020 CPU for compatibility with SFS and other modern handlers
        self.vh.setup(cpu="68020")
        if trace:
            self.vh.enable_trace()
        self.mem = self.vh.alloc.get_mem()
        seg_baddr = self.vh.load_handler(driver, self._driver_name)
        # Nothing reads the file again. Removing an extracted driver before
        # any handler code runs leaves nothing behind when the process is
        # killed, as while a handler hangs: a kill skips the caller's
        # cleanup, which is left to report a removal that fails here.
        if self._remove_driver:
            try:
                Path(driver).unlink(missing_ok=True)
            except OSError:
                pass
        # Build DeviceNode/FSSM using seglist bptr. Pass the backend's block
        # size request, so both open the disk the same way.
        ba = BootstrapAllocator(
            self.vh, image, block_size=block_size, partition=partition, adf_info=adf_info,
            iso_info=iso_info, mbr_partition_index=mbr_idx,
            partition_number=self.backend.partition_number,
        )
        if adf_info:
            handler_name = "DF0:"
        elif iso_info:
            handler_name = "CD0:"
        else:
            handler_name = "DH0:"
        boot = ba.alloc_all(handler_seglist_baddr=seg_baddr, handler_seglist_bptr=seg_baddr, handler_name=handler_name)
        self._partition_index = boot["part"].num if boot.get("part") else 0
        # What name_fold() goes by.
        self._dos_type = adf_info.dos_type if adf_info else None
        self._first_block = None
        if boot.get("part") and not adf_info and not iso_info:
            dos_env = boot["part"].part_blk.dos_env
            self._dos_type = dos_env.dos_type
            self._first_block = dos_env.low_cyl * dos_env.surfaces * dos_env.blk_per_trk

        # Log partition geometry for debugging.
        if boot.get("part") and debug:
            part = boot["part"]
            if adf_info:
                # For ADF, use the synthetic info
                print(f"[amifuse] ADF geometry:")
                floppy_type = "HD" if adf_info.is_hd else "DD"
                print(f"[amifuse]   Type: {floppy_type} floppy")
                print(f"[amifuse]   Cylinders={adf_info.cylinders} Heads={adf_info.heads} Sectors={adf_info.sectors_per_track}")
                print(f"[amifuse]   Total blocks: {adf_info.total_blocks}")
            elif iso_info:
                print(f"[amifuse] ISO 9660 geometry:")
                print(f"[amifuse]   Volume: {escape_text(iso_info.volume_id)}")
                print(f"[amifuse]   Block size: {iso_info.block_size}")
                print(f"[amifuse]   Total blocks: {iso_info.total_blocks}")
            else:
                dos_env = part.part_blk.dos_env
                # Use partition geometry from DosEnvec, not disk geometry
                blk_per_cyl = dos_env.surfaces * dos_env.blk_per_trk
                start_cyl = dos_env.low_cyl
                end_cyl = dos_env.high_cyl
                num_cyl = end_cyl - start_cyl + 1
                start_blk = start_cyl * blk_per_cyl
                num_blk = num_cyl * blk_per_cyl

                print(f"[amifuse] Partition geometry:")
                print(f"[amifuse]   DosEnvec: LowCyl={start_cyl} HighCyl={end_cyl} Surfaces={dos_env.surfaces} BlkPerTrack={dos_env.blk_per_trk}")
                print(f"[amifuse]   Calculated: blk_per_cyl={blk_per_cyl} start_blk={start_blk} num_blk={num_blk}")

        # The backend confines I/O to the partition counted in RDB blocks,
        # while the handler counts the same cylinders in de_SizeBlock units.
        # If they differ, part of a write would be rejected midway.
        if boot.get("part") and not adf_info and not iso_info and not read_only:
            sector_bytes = boot["part"].part_blk.dos_env.block_size * 4
            if sector_bytes != self.backend.block_size:
                part_name = partition or f"#{boot['part'].num}"
                raise SystemExit(
                    f"Partition '{part_name}' declares {sector_bytes}-byte blocks "
                    f"but its RDB uses {self.backend.block_size}-byte blocks; "
                    f"it cannot be opened for writing"
                )

        # Check that the partition data is within the image file
        if boot.get("part") and not adf_info and not iso_info:
            dos_env = boot["part"].part_blk.dos_env
            blk_per_cyl = dos_env.surfaces * dos_env.blk_per_trk
            part_end_byte = (dos_env.high_cyl + 1) * blk_per_cyl * self.backend.block_size
            blkdev = self.backend.blkdev
            if hasattr(blkdev, "img_file"):
                image_size = blkdev.img_file.size
            else:
                image_size = blkdev.num_blocks * blkdev.block_bytes
            if part_end_byte > image_size:
                part_start_byte = dos_env.low_cyl * blk_per_cyl * self.backend.block_size
                part_name = partition or f"#{boot['part'].num}"
                if part_start_byte >= image_size:
                    raise SystemExit(
                        f"Partition '{part_name}' starts at byte {part_start_byte:,} "
                        f"but image is only {image_size:,} bytes ({image_size/1024/1024:.0f} MB). "
                        f"The image appears to be truncated."
                    )
                else:
                    print(
                        f"[amifuse] WARNING: Partition extends to byte {part_end_byte:,} "
                        f"but image is only {image_size:,} bytes. Data may be incomplete.",
                        file=sys.stderr,
                    )

        self.vh.set_scsi_backend(self.backend, debug=debug)

        # Handler entry point is at segment start (byte 0).
        # AmigaOS starts C/assembler handlers at the first byte of the first segment.
        # The handler's own startup code will set up registers and call WaitPort/GetMsg
        # to retrieve the startup packet from pr_MsgPort.
        seg_info = self.vh.slm.seg_loader.infos[seg_baddr]
        seglist = seg_info.seglist
        seg_addr = seglist.get_segment().get_addr()
        seg_size = seglist.get_segment().get_size()
        if debug:
            print(f"[amifuse] Segment: addr=0x{seg_addr:x} size={seg_size} end=0x{seg_addr+seg_size:x}")
        self.launcher = HandlerLauncher(self.vh, boot, seg_addr)
        self.launcher._emu = self._emu
        self.state = self.launcher.launch_with_startup(debug=debug)

        # Run startup to completion. The handler, and the child processes
        # it creates (SFS's DosList handler), run under the scheduler
        # until all of them wait.
        replies = self._run_startup_until_replies()
        # Check if startup succeeded - res1=0 means the handler rejected the disk
        pkt = DosPacketStruct(self.mem, self.state.stdpkt_addr)
        startup_res1 = pkt.res1.val
        startup_res2 = pkt.res2.val
        if self._debug:
            print(f"[amifuse] Startup packet result: res1={startup_res1} res2={startup_res2}")
        if startup_res1 == 0:
            if not replies:
                # A crash also stops the replies; report it as a crash.
                _raise_if_handler_crashed(self, "startup")
                raise SystemExit("Filesystem handler did not reply to the startup packet")
            raise StartupDiskRejected(startup_res1, startup_res2)
        self._update_handler_port_from_startup()
        self.state.initialized = True
        # Run the deferred initialisation: PFS3 validates the volume on a
        # timer request, which is due at once here, as every pending one.
        self._flush_pending_signals()
        # cache a best-effort volume name
        self._volname = None
        self._fib_mem = None
        self._datestamp_mem = None
        self._read_buf_mem = None
        self._read_buf_size = 0
        self._bstr_ring = []
        self._bstr_sizes = []
        self._bstr_index = 0
        self._bstr_ring_size = 8
        self._fh_pool: List[int] = []
        self._fh_mem: Dict[int, object] = {}
        # Handles opened with ACTION_FINDOUTPUT that have written nothing
        # yet; close_file() sends them a zero-length write.
        self._unwritten_outputs: Set[int] = set()
        self._neg_cache: Dict[str, float] = {}
        # Short negative cache for handler lookups; tune for RW.
        # A newly created file might be invisible for up to TTL seconds, then it will appear normally.
        self._neg_cache_ttl = 10.0
        self._write_enabled = not read_only
        if self._write_enabled:
            self._neg_cache_ttl = 0.0
        if self._debug:
            print(
                f"[amifuse] handler loaded seg_baddr=0x{seg_baddr:x} seg_addr=0x{seg_addr:x} "
                f"port=0x{self.state.port_addr:x} reply=0x{self.state.reply_port_addr:x}"
            )

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        emu = getattr(self, "_emu", None)
        try:
            if emu is not None and emu.alive:
                emu.set_idle(None)
                emu.call(self._close_runtime)
            else:
                self._close_runtime()
        finally:
            if emu is not None:
                emu.stop()

    def shutdown_runtime(self):
        """Shut the vamos runtime down, as destroy() does before the mount ends."""
        runtime = getattr(self, "vh", None)
        if runtime is not None:
            runtime.shutdown()

    def start_idle_timers(self, on_crash=None):
        """Reply to due timer requests while no call is pending.

        The emulation thread runs run_due_timers() whenever it has waited
        as long as that asked for; a handler that crashes there is reported
        to on_crash() once. Replaces the mount's timer thread.
        """
        self._on_crash = on_crash
        self._emu.set_idle(self._idle_timers)

    def stop_idle_timers(self):
        self._emu.set_idle(None)

    def _idle_timers(self):
        if self.state.crashed:
            on_crash, self._on_crash = self._on_crash, None
            if on_crash is not None:
                on_crash()
            return None
        try:
            timeout = self.run_due_timers()
        except Exception:
            # As a mount goes on when a FUSE operation raises in the same
            # bridge code: the next call runs the handler on.
            if self._debug:
                import traceback
                print("[amifuse] timer: error delivering timers:", flush=True)
                traceback.print_exc()
            timeout = self._TIMER_RETRY
        if self.state.crashed:
            on_crash, self._on_crash = self._on_crash, None
            if on_crash is not None:
                on_crash()
            return None
        if timeout is not None:
            # At least one VBLANK tick, even when a request is due again at
            # once, so that calls waiting for the thread get their turn.
            timer = self._timer_device()
            if timer is not None:
                timeout = max(timeout, timer.MIN_DELAY)
        return timeout

    def _close_runtime(self):
        runtime = getattr(self, "vh", None)
        backend = getattr(self, "backend", None)
        self.vh = None
        self.backend = None
        first_error = None
        try:
            if runtime is not None:
                runtime.shutdown()
        except BaseException as exc:
            first_error = exc
        try:
            if backend is not None:
                backend.close()
        except BaseException as exc:
            if first_error is None:
                first_error = exc
        if first_error is not None:
            raise first_error

    def _reserved_port_addrs(self) -> set[int]:
        reserved = {self.state.port_addr, self.state.reply_port_addr}
        # The handler process's own pr_MsgPort stays its packet port after
        # state.port_addr switches to the port the startup reply names.
        # SmartFilesystem's front process forwards packets to it.
        process_addr = getattr(self.state, "process_addr", 0)
        if process_addr:
            from amitools.vamos.libstructs.dos import ProcessStruct  # type: ignore
            msg_port_off = ProcessStruct.sdef.find_field_def_by_name("pr_MsgPort").offset
            reserved.add(process_addr + msg_port_off)
        # The ports of the child processes the handler created, which
        # dos.library frees with them when they end.
        dos_impl = getattr(getattr(self.vh, "slm", None), "dos_impl", None)
        for child in list(getattr(dos_impl, "child_procs", {}).values()):
            try:
                reserved.add(child.proc.msg_port.addr)
            except Exception:
                continue
        return reserved

    def _drain_non_essential_ports(self, pmgr):
        reserved_ports = self._reserved_port_addrs()
        timer = self._timer_device()
        mem = getattr(self, "mem", None)
        for port_addr in list(pmgr.ports.keys()):
            if port_addr in reserved_ports:
                continue
            kept = []
            while pmgr.has_msg(port_addr):
                msg_addr = pmgr.get_msg(port_addr)
                # A request timer.device delayed is replied at its deadline,
                # and the handler takes the reply. Replies that come at once,
                # from other devices or from timer requests that complete at
                # once, go.
                if timer is not None and timer.delays(msg_addr):
                    kept.append(msg_addr)
                elif mem is not None:
                    self._unlink_drained(mem, msg_addr)
            for msg_addr in kept:
                pmgr.put_msg(port_addr, msg_addr)

    @staticmethod
    def _unlink_drained(mem, msg_addr: int):
        """Take a drained message off its mp_MsgList, as GetMsg() does.

        Only a node its neighbours point back at is linked: a message that
        PutMsg() queued through the port manager is not, and its links may
        be stale, into another port's list or freed memory. An unlinked node
        is linked to itself: the handler may still WaitIO() on the request,
        which unlinks it again, and so takes no later message with it.
        Zeroed links would have a Remove() write to addresses 0 and 4
        (ExecBase).
        """
        try:
            succ = mem.r32(msg_addr)  # ln_Succ
            pred = mem.r32(msg_addr + 4)  # ln_Pred
            if not (succ and pred and mem.r32(pred) == msg_addr
                    and mem.r32(succ + 4) == msg_addr):
                return
            mem.w32(pred, succ)
            mem.w32(succ + 4, pred)
            mem.w32(msg_addr, msg_addr)
            mem.w32(msg_addr + 4, msg_addr)
        except Exception:
            pass

    def _timer_device(self):
        """The runtime's AmiFuseTimerDevice, or None."""
        return getattr(getattr(self, "vh", None), "timer_device", None)

    def _deliver_timers(self, fast_forward: bool = False) -> int:
        """Reply to due timer requests and end due Delay()s; return how many.

        A reply signals its port's task, which makes that task ready: the
        next run resumes it inside its Wait(). With fast_forward every
        pending request is due, so that a handler's initialisation needs
        no real time.
        """
        timer = self._timer_device()
        if timer is None:
            return 0

        def count():
            # requests replied and Delay()s ended
            return timer.deliveries + getattr(timer, "delays_ended", 0)

        before = count()
        signals = timer.deliver(fast_forward=fast_forward)
        replied = count() - before
        if replied and self._debug:
            print(
                f"[amifuse] timer: replied to {replied} request(s), signals "
                f"{ {hex(task): hex(bits) for task, bits in signals.items()} }"
                f"{' (fast-forward)' if fast_forward else ''}"
            )
        return replied

    # How long after its start a packet wait may still sleep until a timer
    # request falls due; see _sleep_to_timer_in_packet_wait.
    _PACKET_TIMER_WAIT = 1.0

    # How long a packet wait without a deadline may sleep to Delay()s in all;
    # see _sleep_to_delay.
    _DELAY_WAIT = 15.0

    # Emulated cycles the handler and its children run per scheduler call
    # before the bridge looks at the reply port again.
    _RUN_CHUNK_CYCLES = 1_000_000

    def _sleep_to_timer_in_packet_wait(self, wait_until: float) -> bool:
        """Sleep to the next timer deadline, for a wait about to end without a reply.

        Every task waits, so only time can bring the reply: the handler,
        or a child, waits on a timer request before it replies. If a
        request falls due by wait_until, on the device clock, the wait
        sleeps to its deadline and replies to the due requests. Returns
        True once a request fell due by wait_until, so the wait runs on,
        even if the request turned out freed and went unreplied. The sleep
        holds the bridge lock, as the whole wait does, so a wait that would
        have failed at once costs up to _PACKET_TIMER_WAIT more.
        """
        timer = self._timer_device()
        if timer is None:
            return False
        deadline = timer.next_deadline()
        if deadline > wait_until:
            return False
        # Until the device clock has passed the deadline: on Windows before
        # Python 3.13 it ticks every 15.6 ms, so one sleep may end short.
        while True:
            delay = deadline - timer.clock()
            if delay <= 0:
                break
            time.sleep(delay)
        self._deliver_timers()
        return True

    def _sleep_to_delay(self, deadline: Optional[float]) -> bool:
        """Sleep until a pending Delay() ends, for a wait in which every task waits.

        A timer request may never fall due within the wait, but a Delay()
        ends: a handler that calls Delay() while it serves a packet, or
        polls a child with short ones, replies afterwards. So while a task
        delays, the wait sleeps to the earliest deadline, the delay's or a
        timer request's, however far off, replies to what fell due and runs
        on. A deadline, on the time.monotonic() clock, cuts the sleep short.
        _run_until_replies passes the wait's own, or _DELAY_WAIT from its
        start: a task may also delay in a loop that never ends, and the wait
        holds the emulation thread. Returns whether a task was delaying.
        """
        timer = self._timer_device()
        if timer is None or timer.next_delay() == math.inf:
            return False
        until = timer.next_deadline()
        while True:
            left = until - timer.clock()
            if left <= 0:
                break
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return True
                left = min(left, remaining)
            time.sleep(left)
        self._deliver_timers()
        return True

    def _run(self, max_cycles: Optional[int] = None) -> RunResult:
        """Run the handler and its children; see HandlerLauncher.run."""
        return self.launcher.run(max_cycles)

    # How long the mount's timer thread waits before it looks again at a
    # handler it could not wake (busy past its budget, or waiting somewhere
    # else).
    _TIMER_RETRY = 1.0

    def run_due_timers(self) -> Optional[float]:
        """Run a busy handler on, then reply to due timer requests and run it.

        Called by the mount's timer thread between FUSE operations. A
        handler whose packet wait ran out is still working on that packet;
        each call runs it on for at most _SHORT_BUSY_CYCLES cycles, so
        FUSE operations get the lock in between, and asks for the next
        call a tick later while it stays busy. Due timer requests are then
        replied and the handler runs until it waits again; stray replies
        it left are drained then, as after a packet wait. Once it has run
        the handler _MAX_BUSY_CYCLES this way without it waiting or
        replying, it is caught in a loop and is left alone until the next
        packet's wait. Returns the seconds until the next call is due, or
        None when nothing is pending.
        """
        with self._lock:
            timer = self._timer_device()
            if timer is None or self.state.crashed or self.state.exited:
                return None
            pmgr = self.vh.slm.exec_impl.port_mgr
            if self._busy_ran_out:
                if self._idle_busy_cycles >= self._MAX_BUSY_CYCLES:
                    # Caught in a loop. A pending timer request may still
                    # want its reply once the handler waits again.
                    if timer.next_deadline() == math.inf:
                        return None
                    return self._TIMER_RETRY
                if self._run(self._SHORT_BUSY_CYCLES) == RunResult.BUSY:
                    self._idle_busy_cycles += self._SHORT_BUSY_CYCLES
                    return timer.MIN_DELAY
                # It waited or exited, so it is not caught in a loop.
                self._busy_ran_out = False
                self._idle_busy_cycles = 0
                self._drain_non_essential_ports(pmgr)
            if self._deliver_timers():
                if self._run(self._SHORT_BUSY_CYCLES) == RunResult.BUSY:
                    self._busy_ran_out = True
                    self._idle_busy_cycles += self._SHORT_BUSY_CYCLES
                    return timer.MIN_DELAY
                self._drain_non_essential_ports(pmgr)
            deadline = timer.next_deadline()
            if deadline == math.inf:
                return None
            return max(0.0, deadline - timer.clock())

    def _run_startup_until_replies(self, max_iters: int = 200, cycles: Optional[int] = None):
        """Run the handler until it has replied to the startup packet and waits.

        Each run goes on until every task waits, or for cycles emulated
        cycles. A handler that waits before it has replied gets every
        pending timer request replied, as if its time had passed: one that
        waits on its own timer during startup gets it at once, as it
        always did. The run ends once the reply is there and the handler
        and its children wait, when the handler exits, or when nothing can
        wake it any more.
        """
        if cycles is None:
            cycles = self._RUN_CHUNK_CYCLES
        replies = []
        if self.state.crashed:
            if self._debug:
                print("[amifuse] startup: handler crashed, returning empty")
            return []
        for i in range(max_iters):
            result = self._run(cycles)
            if self.state.crashed:
                if self._debug:
                    print(f"[amifuse] startup: handler crashed at iter {i}")
                return replies
            polled = self.launcher.poll_replies(
                self.state.reply_port_addr, debug=self._debug and i < 3
            )
            if polled:
                replies.extend(polled)
                self.state.startup_replied = True
            if result == RunResult.BUSY:
                continue
            if result == RunResult.DONE:
                # The handler exited: the packet holds whatever it set.
                break
            if replies:
                if self._debug:
                    print(f"[amifuse] startup: {len(replies)} reply/replies after {i + 1} run(s)")
                return replies
            # Parked before replying: a handler that waits on its own timer
            # during startup gets it at once. Nothing else can wake it.
            if self._deliver_timers(fast_forward=True) == 0:
                break
        if self._debug and not replies:
            print(f"[amifuse] startup: no reply after {max_iters} run(s)")
        return replies

    # Emulated cycles a handler may work before the bridge gives up on a
    # reply, so that a handler caught in a loop still fails the packet. The
    # longest wait seen is pfs3aio freeing the blocks of a deleted 2 GiB
    # file, about 2.8e9 cycles.
    _MAX_BUSY_CYCLES = 10_000_000_000
    # The busy limit once a wait has run out, until the handler replies,
    # waits or exits: 50 bursts of 200,000 cycles, the whole of a default
    # wait before the busy limit existed, so that no packet to a handler
    # caught in a loop fails later than it did then.
    _SHORT_BUSY_CYCLES = 10_000_000

    def _run_until_replies(self, max_iters: int = 50, cycles: Optional[int] = None, sleep_base: float = 0.0005, sleep_max: float = 0.01,
                           deadline: Optional[float] = None):
        """Run the handler until at least one reply is queued.

        Each run (HandlerLauncher.run) runs the handler and its children
        for at most cycles emulated cycles, or until every task waits. A
        run that ends with a task still runnable counts against a busy
        limit of _MAX_BUSY_CYCLES. Returns [] when every task waits with no
        reply queued and no timer to wake one, or when the busy limit runs
        out first, and says so on stderr when it is _MAX_BUSY_CYCLES.

        A handler whose wait ran out is still running, and the next packet
        would resume it with the full busy limit again. Until it replies to
        any packet or waits, later waits get _SHORT_BUSY_CYCLES instead,
        and a mount's timer thread runs it on while the mount idles
        (run_due_timers). A reply may belong to an earlier packet, so every
        packet wait must go through _run_until_reply, which drops such a
        reply.

        Timer requests that are due when the wait starts are replied first,
        so their signals wake the handler with the packet's. Once every
        task waits without a reply, the wait sleeps to the next timer
        deadline if it comes within _PACKET_TIMER_WAIT of the wait's start,
        replies to it and runs on: a handler may reply only once its own
        timer fires, or a child may wait on one. While a task delays, the
        wait sleeps to the next deadline however far off, but without a
        deadline of its own for _DELAY_WAIT in all, as a task may delay in a
        loop that never ends (_sleep_to_delay). Between packets, a mount's
        timer thread replies to them (run_due_timers). The limits count
        cycles, not time: Delay() waits as a task and spends no cycles. A
        deadline, on the time.monotonic() clock, also ends the wait before
        the next run once it has passed, whatever limit the handler has
        then; a handler still at work then gets the short limit next, as
        when a limit runs out, without the warning.

        The other ports are drained only after a wait that leaves the
        handler waiting, as the idle timers do (run_due_timers): in the
        middle of its work, a SendIO() reply it has not taken yet is still
        its own (#240). A wait can end so on a reply, too, since a handler
        may answer a packet and go straight on to the next.

        Known limit: a packet whose wait ran out, or ended at its
        deadline, stays queued, or held by the handler, until the handler
        answers it. It stays in HandlerLauncher.pending until then, and
        neither it nor the bridge's memory its arguments name is reused
        meanwhile; the handler's own locks and file entries it names are
        not held back.
        """
        if cycles is None:
            cycles = self._RUN_CHUNK_CYCLES
        replies = []
        busy_cycles = 0
        idle_iters = 0
        timer = self._timer_device()
        timer_wait_until = (
            -math.inf if timer is None else timer.clock() + self._PACKET_TIMER_WAIT
        )
        delay_until = (
            time.monotonic() + self._DELAY_WAIT if deadline is None else deadline
        )
        busy_limit = self._MAX_BUSY_CYCLES

        # Check if handler has crashed - if so, don't try to run it
        if self.state.crashed:
            if self._debug:
                print("[amifuse] _run_until_replies: handler crashed, returning empty")
            return []

        # Reply to timer requests that fell due since the handler last ran.
        # Their signals wake it with the packet's.
        self._deliver_timers()

        busy = timed_out = False
        for i in itertools.count():
            busy_limit = (
                self._SHORT_BUSY_CYCLES if self._busy_ran_out
                else self._MAX_BUSY_CYCLES
            )
            if busy_cycles >= busy_limit:
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            if idle_iters >= max_iters:
                break
            result = self._run(cycles)
            if self.state.crashed:
                if self._debug:
                    print(f"[amifuse] _run_until_replies: handler crashed at iter {i}")
                return []
            # A run that ends with a task still runnable stopped the handler
            # in the middle of its work.
            busy = result == RunResult.BUSY
            if busy:
                busy_cycles += cycles
            # A wait can take thousands of busy runs, so --debug reports
            # only every 1000th of them, with the busy cycles so far, and
            # the poll of a busy run only then or when a reply is queued.
            show = self._debug and (not busy or busy_cycles % (1000 * cycles) == 0)
            if show and busy:
                print(
                    f"[amifuse] _run_until_replies: busy for {busy_cycles:,} "
                    f"cycles"
                )
            debug_poll = show or (
                self._debug
                and self.vh.slm.exec_impl.port_mgr.has_msg(self.state.reply_port_addr)
            )
            polled = self.launcher.poll_replies(
                self.state.reply_port_addr, debug=debug_poll
            )
            if polled:
                replies = polled
                if self._debug and i > 5:
                    print(
                        f"[amifuse] _run_until_replies: got {len(replies)} "
                        f"replies after {i} iters"
                    )
                break
            if busy:
                continue
            # It waited or exited, so it is not caught in a loop.
            self._busy_ran_out = False
            self._idle_busy_cycles = 0
            if result == RunResult.DONE:
                break
            # Every task waits: only time can bring the reply now. The wait
            # sleeps to a Delay(), however far off, until delay_until. Past
            # that the sleep returns at once, so the check is here too: the
            # wait would spin, as the loop's top sees only its own deadline.
            if time.monotonic() < delay_until and self._sleep_to_delay(delay_until):
                continue
            idle_iters += 1
            # A timer request is slept to only within the wait's window.
            if self._sleep_to_timer_in_packet_wait(timer_wait_until):
                continue
            break
        if self._debug and not replies:
            print(
                f"[amifuse] _run_until_replies: no replies after {idle_iters} "
                f"idle iters and {busy_cycles:,} of {busy_limit:,} busy cycles"
            )
        if replies:
            # It answered a packet, so it is not caught in a loop, even if it
            # went on to the next packet without waiting.
            self._busy_ran_out = False
            self._idle_busy_cycles = 0
        elif busy_cycles >= busy_limit or (timed_out and busy):
            # A wait that ends at its deadline in the middle of the
            # handler's work leaves it running, as one that runs out does.
            if busy_cycles >= busy_limit and busy_limit == self._MAX_BUSY_CYCLES:
                # Shown even without --debug, as in --interactive mounts and
                # CLI commands. It is lost where stderr is discarded: at
                # /dev/null by libfuse's daemonize, or at DEVNULL for a mount
                # the Windows launcher starts.
                pkt_type = self.launcher.last_pkt_type
                to = "" if pkt_type is None else f" to {_packet_type_name(pkt_type)}"
                print(
                    f"[amifuse] WARNING: handler did not reply{to} within the "
                    f"busy limit of {busy_limit:,} emulated cycles",
                    file=sys.stderr,
                )
            self._busy_ran_out = True
            if timer is not None:
                # The mount's timer thread runs the handler on while idle.
                timer.event.set()

        # Drain stray IO replies that accumulated during packet processing:
        # SendIO queues them at once. timer.device replies stay, since they
        # come only when due and the handler takes them.
        if not busy and not self._busy_ran_out:
            pmgr = self.vh.slm.exec_impl.port_mgr
            self._drain_non_essential_ports(pmgr)

        return replies

    def _flush_pending_signals(self, max_rounds: int = 20):
        """Run the handler's deferred initialisation.

        PFS3 defers its volume validation to a timer request and to signals
        it sends itself; FFS arms its tick. Each round replies to every
        pending timer request as if its time had passed and runs the
        handler until it waits again, until a round replied to nothing and
        the handler still waits: what is left needs real time or a packet.
        Stray replies are drained afterwards, unless the rounds ran out with
        the handler still at work (#240); timer.device replies stay for the
        handler, since they come only when due.
        """
        if self.state.crashed or self.state.exited:
            return
        for rnd in range(max_rounds):
            replied = self._deliver_timers(fast_forward=True)
            result = self._run(self._RUN_CHUNK_CYCLES)
            # Drain any spurious replies (timer completion etc.)
            self.launcher.poll_replies(self.state.reply_port_addr)
            if self._debug:
                print(
                    f"[amifuse] _flush round {rnd}: timer replies={replied} "
                    f"result={result.name} crashed={self.state.crashed}"
                )
            if self.state.crashed or result == RunResult.DONE:
                break
            if result == RunResult.IDLE and not replied:
                break
        # Drain stray replies from non-essential ports (disk change etc.).
        if result != RunResult.BUSY:
            self._drain_non_essential_ports(self.vh.slm.exec_impl.port_mgr)

    def _update_handler_port_from_startup(self):
        pkt = DosPacketStruct(self.mem, self.state.stdpkt_addr)
        res1 = pkt.res1.val
        res2 = pkt.res2.val
        if res1:
            # Check dp_Port - many handlers set this to their message port in the reply
            dp_port = pkt.port.aptr
            alt_port = pkt.arg4.val
            if self._debug:
                print(f"[amifuse] Startup reply: dp_Port=0x{dp_port:x} dp_Arg4=0x{alt_port:x} (current=0x{self.state.port_addr:x})")
            # Prefer dp_Port if it's different and valid
            new_port = None
            if dp_port and dp_port != self.state.reply_port_addr and dp_port != self.state.port_addr:
                new_port = dp_port
            elif alt_port and alt_port != self.state.port_addr:
                new_port = alt_port

            # Some handlers (CDFileSystem) set dn_Task to a different port
            # than dp_Port in the startup reply.  Check dn_Task as fallback.
            if not new_port:
                from .amiga_structs import DeviceNodeStruct
                dn_addr = self.launcher.boot["dn_addr"]
                dn_task = self.mem.r32(
                    dn_addr
                    + DeviceNodeStruct.sdef.find_field_def_by_name(
                        "dol_Task"
                    ).offset
                )
                if dn_task and dn_task != self.state.port_addr:
                    new_port = dn_task
                    if self._debug:
                        print(f"[amifuse] dn_Task port: 0x{dn_task:x}")

            if new_port:
                pmgr = self.vh.slm.exec_impl.port_mgr
                if not pmgr.has_port(new_port):
                    pmgr.register_port(new_port)
                if self._debug:
                    print(f"[amifuse] Switching to handler port 0x{new_port:x}")
                self.state.port_addr = new_port
    def _log_replies(self, label: str, replies):
        if not self._debug:
            return
        for _, pkt_addr, res1, res2 in replies:
            print(f"[amifuse][{label}] pkt=0x{pkt_addr:x} res1={res1} res2={res2}")

    # A handler answers each packet once, so late replies run out; this only
    # bounds the wait on a handler that never stops replying.
    _MAX_REPLY_ROUNDS = 8

    def _run_until_reply(self, pkt_addr: int, label: str, **wait) -> Optional[Tuple[int, int]]:
        """Run the handler until it answers the packet at pkt_addr.

        Returns (res1, res2), or None when no answer arrives in time. A
        packet that timed out is still answered later, while the bridge
        waits for the next one. Such a reply is dropped: taken as this
        packet's answer, a READ byte count would pass for a seek position.

        Each round is one _run_until_replies wait, which gets wait as its
        keyword arguments and its own busy limit. A late reply ends the
        short limit, so the rounds after it get the full one. What a
        dropped reply grants is given back (_give_back_late_reply) once
        the wait has ended, so that nothing is queued behind this packet.
        """
        answer = None
        dropped = []
        for _ in range(self._MAX_REPLY_ROUNDS):
            sent = dict(self._pending_packets())
            replies = self._run_until_replies(**wait)
            self._log_replies(label, replies)
            for _, reply_pkt, res1, res2 in replies:
                if reply_pkt == pkt_addr:
                    answer = (res1, res2)
                    continue
                if self._debug:
                    print(f"[amifuse][{label}] dropped late reply for "
                          f"pkt=0x{reply_pkt:x}")
                dropped.append((sent.get(reply_pkt), res1))
            if not replies or answer is not None:
                break
        for packet, res1 in dropped:
            self._give_back_late_reply(packet, res1)
        if dropped:
            # The memory those packets held back is no packet's now.
            self._free_retired(self._held_args())
        return answer

    def _give_back_late_reply(self, packet, res1: int) -> None:
        """Free the lock or end the file that a dropped late reply grants.

        packet is the (dp_Type, args) the reply answers, or None. A LOCATE
        or CREATE_DIR that succeeded holds a lock, and an open a file that
        no caller will close, each until the mount ends. The FREE_LOCK or
        END goes out without a wait of its own, so that it cannot take a
        reply another wait is for; its reply comes as another late one.
        """
        if packet is None or res1 == 0:
            return
        pkt_type, args = packet
        if pkt_type in (ACTION_LOCATE_OBJECT, ACTION_CREATE_DIR):
            self.launcher.send_free_lock(self.state, res1)
        elif pkt_type in (ACTION_FINDINPUT, ACTION_FINDUPDATE, ACTION_FINDOUTPUT):
            # Arg1 is the FileHandle's BPTR; the handler filled in its fh_Args.
            self.launcher.send_end_handle(self.state, args[0] << 2)

    # How long unmount, or a flush, runs the handler for packets still out:
    # well within the 30 s that the unmount requesters wait for the mount
    # to end. Every other call queues behind a wait this long.
    _PENDING_WAIT = 15.0

    def wait_for_pending(self, timeout: float = _PENDING_WAIT) -> List[str]:
        """Run the handler until every packet sent has its reply.

        A packet whose wait ran out may still be at work in the handler,
        which can answer later packets, ACTION_FLUSH included, before it.
        Stopping it then leaves the volume half changed, so flushes wait
        here first. The handler runs in waits of the short busy limit
        until no packet is out, it parks without replying, or timeout
        seconds have passed. Each wait also ends at that deadline: a
        handler that parks during a wait has the full busy limit for the
        rest of it. Late replies are given back as in
        _run_until_reply. _pending_wait_parked says whether it stopped
        because the handler parked or exited, not at the deadline or with no
        packet left. Returns the names of the packets still out.
        """
        with self._lock:
            self._pending_wait_parked = False
            deadline = time.monotonic() + timeout
            while (self._pending_packets() and not self.state.crashed
                   and time.monotonic() < deadline):
                sent = dict(self._pending_packets())
                self._busy_ran_out = True
                replies = self._run_until_replies(deadline=deadline)
                self._log_replies("pending", replies)
                for _, reply_pkt, res1, _ in replies:
                    self._give_back_late_reply(sent.get(reply_pkt), res1)
                if replies:
                    self._free_retired(self._held_args())
                if not replies and not self._busy_ran_out:
                    self._pending_wait_parked = True
                    break  # parked, or exited: nothing more comes
            return [_packet_type_name(pkt_type)
                    for pkt_type, _ in self._pending_packets().values()]

    # Whether the last wait_for_pending stopped at a park; see there.
    _pending_wait_parked = False

    # Bridge memory taken out of use while a packet still out points to it;
    # see _free_retired. The small buffers: the FIB, the BSTRs, the
    # buffer of the write of no bytes and the DateStamp, at most 264 bytes
    # each.
    _retired = ()
    # The read and write buffers, up to IO_CHUNK each.
    _retired_data = ()
    # The most of those at once: 16 full read buffers, an eighth of the
    # 8 MiB the handler and the bridge share (VamosHandlerRuntime's
    # ram_size). The small ones have no cap: each packet still out holds
    # at most two, so tens of thousands of packets would have to go
    # unanswered before they took that much. That is no long wait: a
    # handler hung in Wait() leaves a packet unanswered after 50 bursts,
    # about half a millisecond to two milliseconds, or up to a second more
    # when one of its timer requests falls due, so a busy indexer could get
    # there in minutes.
    # But a cap on them would fail a lookup halfway through a path, or the
    # undo of a rename, with the locks it took still held.
    _RETIRED_MAX = 16 * IO_CHUNK
    # write_empty_reply's buffer; see _alloc_empty_buf.
    _empty_buf_mem = None

    def _pending_packets(self) -> Dict[int, Tuple[int, Tuple[int, ...]]]:
        """The launcher's packets with no reply yet, packet -> (type, args)."""
        pending = getattr(getattr(self, "launcher", None), "pending", None)
        return pending if isinstance(pending, dict) else {}

    def _held_args(self) -> Set[int]:
        """Every argument of the packets still out.

        A handler reaches the bridge's memory only through a packet's
        arguments, as the start address or BPTR of a buffer, so every
        buffer the bridge passes must be passed at its start. Memory named
        there is the packet's until its reply comes: the handler may still
        read it, or write its answer into it. A number that only looks like
        such an address keeps the memory out of use a little longer, no
        more.
        """
        return {arg for _, args in self._pending_packets().values() for arg in args}

    @staticmethod
    def _is_held(addr: int, held: Set[int]) -> bool:
        return addr in held or addr >> 2 in held

    def _free_retired(self, held: Set[int]) -> None:
        """Free retired memory that no packet still out points to."""
        def keep(retired):
            kept = []
            for mem_obj in retired:
                if self._is_held(mem_obj.addr, held):
                    kept.append(mem_obj)
                else:
                    self.vh.alloc.free_memory(mem_obj)
            return kept
        self._retired = keep(self._retired)
        self._retired_data = keep(self._retired_data)

    def _retire_if_held(self, mem_obj, held: Set[int], data: bool = False) -> bool:
        """Retire mem_obj if a packet still out points to it; say whether.

        A read or write buffer (data) past _RETIRED_MAX stays where it is,
        out of use, and the request fails with EIO instead, so that a
        handler which leaves read after read unanswered cannot run the
        emulated RAM out. Before it fails, it takes the replies already
        queued, without running the handler: the timer thread runs the
        handler on while the mount idles, but takes no replies. So reads
        and writes work again as soon as replies that free enough of it
        have come. A small buffer is always retired.
        """
        if mem_obj is None or not self._is_held(mem_obj.addr, held):
            return False
        if not data:
            self._retired = [*self._retired, mem_obj]
            return True
        if sum(m.size for m in self._retired_data) + mem_obj.size > self._RETIRED_MAX:
            ran_out = self._busy_ran_out
            self._take_late_replies()
            held = self._held_args()
            self._free_retired(held)
            if not self._is_held(mem_obj.addr, held):
                return False
            if sum(m.size for m in self._retired_data) + mem_obj.size > self._RETIRED_MAX:
                # No packet goes out, so no wait runs the handler on: only
                # the timer thread can bring the replies that free the rest.
                self._busy_ran_out = ran_out
                raise FuseOSError(errno.EIO)
        self._retired_data = [*self._retired_data, mem_obj]
        return True

    def _take_late_replies(self) -> None:
        """Take the replies queued for packets whose waits ran out.

        No wait is in progress, so every reply queued is a late one. Each
        is given back as in _run_until_reply, without running the handler.
        A reply ends the short limit, as in _run_until_replies.
        """
        sent = dict(self._pending_packets())
        replies = self.launcher.poll_replies(self.state.reply_port_addr)
        self._log_replies("late", replies)
        for _, reply_pkt, res1, _ in replies:
            self._give_back_late_reply(sent.get(reply_pkt), res1)
        if replies:
            # It answered a packet, so it is not caught in a loop, even if it
            # went on to the next packet without waiting.
            self._busy_ran_out = False
            self._idle_busy_cycles = 0

    def _alloc_fib(self):
        held = self._held_args()
        self._free_retired(held)
        if self._retire_if_held(self._fib_mem, held):
            self._fib_mem = None
        if self._fib_mem is None:
            self._fib_mem = self.vh.alloc.alloc_struct(
                FileInfoBlockStruct, size=_FIB_SIZE, label="FUSE_FIB"
            )
            if self._debug:
                fib_end = self._fib_mem.addr + _FIB_SIZE
                print(f"[amifuse] FIB allocated at 0x{self._fib_mem.addr:x}-0x{fib_end:x} (size={_FIB_SIZE})")
        self.mem.w_block(self._fib_mem.addr, b"\x00" * _FIB_SIZE)
        return self._fib_mem

    def _alloc_read_buf(self, size: int):
        held = self._held_args()
        self._free_retired(held)
        if self._retire_if_held(self._read_buf_mem, held, data=True):
            # A READ or WRITE still out may yet use it: this one gets its own.
            self._read_buf_mem = None
        if self._read_buf_mem is None:
            self._read_buf_mem = self.vh.alloc.alloc_memory(size, label="FUSE_READBUF")
            self._read_buf_size = size
        elif size > self._read_buf_size:
            old_mem = self._read_buf_mem
            self._read_buf_mem = self.vh.alloc.alloc_memory(size, label="FUSE_READBUF")
            self._read_buf_size = size
            self.vh.alloc.free_memory(old_mem)
        self.mem.w_block(self._read_buf_mem.addr, b"\x00" * size)
        return self._read_buf_mem

    def _alloc_empty_buf(self):
        """The buffer for write_empty_reply's write of no bytes.

        A small buffer of its own, not the read buffer, so that the write
        never fails at _RETIRED_MAX.
        """
        held = self._held_args()
        self._free_retired(held)
        if self._retire_if_held(self._empty_buf_mem, held):
            self._empty_buf_mem = None
        if self._empty_buf_mem is None:
            self._empty_buf_mem = self.vh.alloc.alloc_memory(4, label="FUSE_EMPTYBUF")
        return self._empty_buf_mem

    def _alloc_datestamp(self):
        """The struct DateStamp for set_date's ACTION_SET_DATE.

        One that a SET_DATE still out names stays where it is, so that a
        later set_date cannot change the date the handler has yet to set.
        """
        held = self._held_args()
        self._free_retired(held)
        if self._retire_if_held(self._datestamp_mem, held):
            self._datestamp_mem = None
        if self._datestamp_mem is None:
            self._datestamp_mem = self.vh.alloc.alloc_memory(12, label="FUSE_DATESTAMP")
        return self._datestamp_mem

    def _alloc_bstr(self, text: str):
        # Strict: a name past the checks fails here, not as another file.
        encoded = unicodedata.normalize("NFC", text).encode("latin-1")
        if len(encoded) > 255:
            encoded = encoded[:255]
        # Keep one trailing NUL byte so handlers can temporarily treat the
        # counted string payload as a C string without overrunning our buffer.
        data = bytes([len(encoded)]) + encoded + b"\x00"
        if not self._bstr_ring:
            self._bstr_ring = [None] * self._bstr_ring_size
            self._bstr_sizes = [0] * self._bstr_ring_size
        idx = self._bstr_index
        self._bstr_index = (idx + 1) % self._bstr_ring_size
        mem_obj = self._bstr_ring[idx]
        held = self._held_args()
        self._free_retired(held)
        if self._retire_if_held(mem_obj, held):
            # A packet still out names it: the slot gets a new BSTR.
            mem_obj = self._bstr_ring[idx] = None
        if mem_obj is None or len(data) > self._bstr_sizes[idx]:
            new_mem = self.vh.alloc.alloc_memory(len(data), label=f"FUSE_BSTR_{idx}")
            if mem_obj is not None:
                self.vh.alloc.free_memory(mem_obj)
            mem_obj = new_mem
            self._bstr_ring[idx] = mem_obj
            self._bstr_sizes[idx] = len(data)
        self.mem.w_block(mem_obj.addr, data)
        return mem_obj.addr, mem_obj.addr >> 2

    def _alloc_fh(self) -> int:
        """Take a FileHandle from the pool, or allocate one.

        A pooled FileHandle stays there while a packet still out names it:
        an open whose reply has not come (its BPTR), or a packet on the
        file it opened (its fh_Args), ACTION_END included.
        """
        held = self._held_args()
        for i in range(len(self._fh_pool) - 1, -1, -1):
            addr = self._fh_pool[i]
            if held:
                fh_args = FileHandleStruct(self.mem, addr).args.val
                if addr >> 2 in held or (fh_args and fh_args in held):
                    continue
            del self._fh_pool[i]
            mem_obj = self._fh_mem.get(addr)
            if mem_obj:
                self.mem.w_block(addr, b"\x00" * FileHandleStruct.get_size())
            return addr
        mem_obj = self.vh.alloc.alloc_struct(FileHandleStruct, label="FUSE_FH")
        self.mem.w_block(mem_obj.addr, b"\x00" * FileHandleStruct.get_size())
        self._fh_mem[mem_obj.addr] = mem_obj
        return mem_obj.addr

    def _free_fh(self, fh_addr: int):
        # Pooled twice, one FileHandle would go to two opens at once.
        if fh_addr in self._fh_mem and fh_addr not in self._fh_pool:
            self._fh_pool.append(fh_addr)

    def _is_neg_cached(self, path: str) -> bool:
        if self._neg_cache_ttl <= 0:
            return False
        if path in self._neg_cache:
            if time.time() - self._neg_cache[path] < self._neg_cache_ttl:
                return True
            del self._neg_cache[path]
        return False

    def _set_neg_cached(self, path: str):
        if self._neg_cache_ttl <= 0:
            return
        _prune_cache(self._neg_cache)
        self._neg_cache[path] = time.time()

    def locate(self, lock_bptr: int, name: str):
        with self._lock:
            _, name_bptr = self._alloc_bstr(name)
            pkt_addr, _ = self.launcher.send_locate(self.state, lock_bptr, name_bptr)
            # A late reply to a packet that timed out would pass for the lock.
            reply = self._run_until_reply(pkt_addr, "locate")
            if self._debug:
                if reply is not None:
                    print(f"[amifuse] locate(lock=0x{lock_bptr:x}, name='{name}'): res1=0x{reply[0]:x} res2={reply[1]}")
                else:
                    print(f"[amifuse] locate(lock=0x{lock_bptr:x}, name='{name}'): NO REPLIES")
            return reply if reply is not None else (0, -1)

    def free_lock(self, lock_bptr: int):
        with self._lock:
            if lock_bptr:
                pkt_addr, _ = self.launcher.send_free_lock(self.state, lock_bptr)
                self._run_until_reply(pkt_addr, "free_lock")

    def locate_path(self, path: str) -> Tuple[int, int, List[int]]:
        """Return (lock BPTR, res2, locks_to_free) for the given absolute path.

        A parent lock is only needed to resolve the next path component, so
        intermediate locks are freed while descending; locks_to_free holds at
        most the final lock. Ancestor locks must not accumulate in handler
        memory: they are never freed by callers, which only release the
        returned list.

        Each LOCATE takes only its own reply. A late reply to a packet that
        timed out, a WRITE's byte count say, would otherwise pass for the
        lock and be examined and freed in its place. The real lock would
        stay held, and the object in use, for the rest of the mount.
        """
        with self._lock:
            if path and path != "/" and self._is_neg_cached(path):
                return 0, -1, []
            parts = [p for p in path.split("/") if p]
            lock = 0
            res2 = 0
            if not parts:
                return lock, res2, []
            for comp in parts:
                _, name_bptr = self._alloc_bstr(comp)
                pkt_addr, _ = self.launcher.send_locate(self.state, lock, name_bptr)
                reply = self._run_until_reply(pkt_addr, "locate")
                parent = lock
                lock, res2 = reply if reply is not None else (0, -1)
                if parent:
                    self.free_lock(parent)
                if lock == 0:
                    break
            if lock == 0 and path and path != "/" and res2 in _NOT_FOUND_ERRORS:
                self._set_neg_cached(path)
            return lock, res2, [lock] if lock else []

    def open_file(self, path: str, flags: int = os.O_RDONLY) -> Optional[Tuple[int, int]]:
        """Open a file via FINDINPUT/FINDUPDATE/FINDOUTPUT.

        Returns (FileHandle address, parent directory lock), or None when
        the open fails. Callers that need the handler's error code want
        open_file_reply().
        """
        return self.open_file_reply(path, flags)[0]

    def open_file_reply(self, path: str,
                        flags: int = os.O_RDONLY) -> Tuple[Optional[Tuple[int, int]], int]:
        """Open a file as open_file() does and return (result, dp_Res2).

        result is open_file()'s. When it is None, dp_Res2 holds the error
        code of the packet that failed, or 0 when no packet was sent or
        none got a reply.
        """
        with self._lock:
            if path and path != "/" and self._is_neg_cached(path):
                return None, 0
            parts = [p for p in path.split("/") if p]
            if not parts:
                return None, 0
            name = parts[-1]
            dir_path = "/" + "/".join(parts[:-1])
            dir_lock, dir_res2, locks = self.locate_path(dir_path)
            if dir_path == "/" and dir_lock == 0:
                dir_lock, _ = self.locate(0, "")
                if dir_lock:
                    locks.append(dir_lock)
            if dir_lock == 0 and dir_path != "/":
                if path and path != "/" and dir_res2 in _NOT_FOUND_ERRORS:
                    self._set_neg_cached(path)
                # locate_path() gives -1 for no reply or a cached miss.
                return None, max(dir_res2, 0)
            _, name_bptr = self._alloc_bstr(name)
            mode = flags & getattr(os, 'O_ACCMODE', 3)
            output = mode != os.O_RDONLY and bool(flags & os.O_TRUNC)
            if mode == os.O_RDONLY:
                fh_addr = self._alloc_fh()
                _, pkt_addr, _ = self.launcher.send_findinput(
                    self.state, name_bptr, dir_lock, fh_addr)
            else:
                if not self._write_enabled:
                    return None, 0
                if output:
                    fh_addr = self._alloc_fh()
                    if self._debug:
                        print(f"[amifuse][open_file] FINDOUTPUT name={name!r} dir_lock=0x{dir_lock:x} fh_addr=0x{fh_addr:x}", flush=True)
                    _, pkt_addr, _ = self.launcher.send_findoutput(
                        self.state, name_bptr, dir_lock, fh_addr)
                else:
                    fh_addr = self._alloc_fh()
                    if self._debug:
                        print(f"[amifuse][open_file] FINDUPDATE name={name!r} dir_lock=0x{dir_lock:x} fh_addr=0x{fh_addr:x}", flush=True)
                    _, pkt_addr, _ = self.launcher.send_findupdate(
                        self.state, name_bptr, dir_lock, fh_addr)
            # A late nonzero res1 would pass for DOSTRUE on a failed open.
            reply = self._run_until_reply(pkt_addr, "find")
            if reply is None or reply[0] == 0:
                res2 = reply[1] if reply else 0
                if self._debug:
                    print(f"[amifuse][open_file] FAILED: replies={reply is not None} res1={reply[0] if reply else 'none'} res2={res2}", flush=True)
                if path and path != "/" and res2 in _NOT_FOUND_ERRORS:
                    self._set_neg_cached(path)
                self._free_fh(fh_addr)
                for l in reversed(locks):
                    self.free_lock(l)
                return None, res2
            if path:
                self._neg_cache.pop(path, None)
            if self._write_enabled:
                # Writing may create new entries; clear stale negative cache.
                self._neg_cache.clear()
            if output:
                self._unwritten_outputs.add(fh_addr)
            return (fh_addr, dir_lock), 0

    def list_dir(self, lock_bptr: int, path: Optional[str] = None) -> List[Dict]:
        with self._lock:
            # A path is supplied by list_dir_path; direct callers may only
            # have a lock. Escape names before including them in warnings.
            shown = escape_text(path) if path is not None else (
                "/" if lock_bptr == 0 else f"lock 0x{lock_bptr:x}")
            # ensure we have a lock; lock_bptr=0 -> root
            root_lock = 0
            if lock_bptr == 0:
                root_lock, _ = self.locate(0, "")
                lock_bptr = root_lock
                if self._debug:
                    print(f"[amifuse] list_dir: obtained root_lock=0x{root_lock:x}")
            fib_mem = self._alloc_fib()
            # First Examine returns info about the directory itself, not contents
            pkt_addr, _ = self.launcher.send_examine(self.state, lock_bptr, fib_mem.addr)
            reply = self._run_until_reply(pkt_addr, "examine")
            entries: List[Dict] = []
            if reply is None or reply[0] == 0:
                if self._debug:
                    res2 = reply[1] if reply else -1
                    print(f"[amifuse] list_dir: Examine FAILED lock=0x{lock_bptr:x} res2={res2}")
                if root_lock:
                    self.free_lock(root_lock)
                return entries
            if self._debug:
                dir_info = _parse_fib(self.mem, fib_mem.addr)
                print(f"[amifuse] list_dir: Examine OK dir_name='{dir_info['name']}' type={dir_info['dir_type']}")
            # Don't add the first entry - it's the directory itself, not a child
            # Iterate via ExamineNext to get actual directory contents
            # Safety limit: Amiga filesystems can have thousands of entries per
            # directory. The old limit of 256 silently truncated large directories.
            # 65536 is generous; ExamineNext will return ERROR_NO_MORE_ENTRIES
            # (res1=0) when the directory is exhausted, which breaks the loop.
            unnamed = 0
            for iter_num in range(65536):
                pkt_addr, _ = self.launcher.send_examine_next(
                    self.state, lock_bptr, fib_mem.addr)
                reply = self._run_until_reply(pkt_addr, "examine_next")
                if reply is None or reply[0] == 0:
                    if self._debug:
                        res2 = reply[1] if reply else -1
                        print(f"[amifuse] list_dir: ExamineNext #{iter_num} ended res2={res2}")
                    if reply is not None and reply[1] != ERROR_NO_MORE_ENTRIES:
                        print(f"Warning: listing {shown} stopped early "
                              f"with error {reply[1]}; it may be incomplete",
                              file=sys.stderr)
                    break
                entry = _parse_fib(self.mem, fib_mem.addr)
                if self._debug:
                    print(f"[amifuse] list_dir: ExamineNext #{iter_num} name='{entry['name']}' type={entry['dir_type']}")
                if not entry["name"]:
                    unnamed += 1
                    continue
                entries.append(entry)
            if unnamed:
                detail = ("entry with an empty name" if unnamed == 1
                          else "entries with empty names")
                print(f"Warning: listing {shown}: skipped {unnamed} directory "
                      f"{detail}", file=sys.stderr)
            if root_lock:
                self.free_lock(root_lock)
            return entries

    def list_dir_path(self, path: str) -> List[Dict]:
        """List directory contents by path."""
        with self._lock:
            if path == "/":
                return self.list_dir(0, path=path)
            lock, _, locks = self.locate_path(path)
            if lock == 0:
                return []
            entries = self.list_dir(lock, path=path)
            for l in reversed(locks):
                self.free_lock(l)
            return entries

    def stat_path(self, path: str) -> Optional[Dict]:
        with self._lock:
            lock, _, locks = self.locate_path(path)
            if lock == 0 and path != "/":
                return None
            if path == "/":
                return {"dir_type": 2, "size": 0, "name": "", "protection": 0}
            fib_mem = self._alloc_fib()
            pkt_addr, _ = self.launcher.send_examine(self.state, lock, fib_mem.addr)
            reply = self._run_until_reply(pkt_addr, "examine")
            if reply is None or reply[0] == 0:
                for l in reversed(locks):
                    self.free_lock(l)
                return None
            info = _parse_fib(self.mem, fib_mem.addr)
            for l in reversed(locks):
                self.free_lock(l)
            return info

    def get_disk_info(self) -> Optional[Dict]:
        """Query filesystem geometry via ACTION_DISK_INFO.

        Returns a dict containing num_blocks, num_blocks_used,
        bytes_per_block, disk_type, and volume_node.
        Returns None if the handler doesn't respond or refuses the packet;
        callers that need to tell those apart want _query_disk_info().
        """
        return self._query_disk_info()[0]

    def name_fold(self) -> Optional[str]:
        """Return how the volume's handler folds a name's case.

        A key of _NAME_FOLDS, or None for a volume whose names must match
        exactly or whose rule is not known. A wrong fold would make two
        files one, so a volume is folded only by a rule seen on its handler.

        FastFileSystem folds by the disk's own type, which it reports in
        id_DiskType and which an RDB partition's DOS type need not match:
        a-z on DOS0 and DOS1, Latin-1 on DOS2 to DOS7. pfs3aio folds Latin-1,
        and reports DOS3. SmartFilesystem folds Latin-1 unless the volume was
        formatted case-sensitive, which its root block says. BFFS reports
        DOS1 too, but its partition's own type keeps it exact: a UFS volume
        from NetBSD can hold names that differ only in case.
        """
        dos_type = getattr(self, "_dos_type", None)
        if dos_type is None:
            return None
        family = dos_type & 0xFFFFFF00
        if family == _DOS_FAMILY:
            disk_type = (self.get_disk_info() or {}).get("disk_type", 0)
            if disk_type & 0xFFFFFF00 == _DOS_FAMILY:
                dos_type = disk_type
            variant = dos_type & 0xFF
            return "ascii" if variant < 2 else "latin1" if variant < 8 else None
        if family in _PFS_FAMILIES:
            return "latin1"
        if family == _SFS_FAMILY and self._first_block is not None:
            try:
                root = self.backend.read_blocks(self._first_block, 1)
            except OSError:
                return None
            if (root[:len(_SFS_ROOT_ID)] == _SFS_ROOT_ID
                    and not root[_SFS_ROOT_BITS_OFFSET] & _SFS_ROOTBITS_CASESENSITIVE):
                return "latin1"
        return None

    def _query_disk_info(self) -> Tuple[Optional[Dict], Optional[int]]:
        """Send ACTION_DISK_INFO and return (info, res2).

        A handler can decline this packet two ways, and they mean different
        things. Silence (no reply at all) says nothing about the volume; an
        explicit DOSFALSE reply carries a res2 error code that often does.
        info is None in both cases, and res2 tells them apart: None for
        silence, the DOS error code for a refusal. A late reply to an
        earlier packet is not an answer.
        """
        with self._lock:
            # Allocate InfoDataStruct (9 LONGs = 36 bytes)
            info_mem = self.vh.alloc.alloc_memory(InfoDataStruct.get_size(), label="InfoData")
            self.mem.w_block(info_mem.addr, b"\x00" * InfoDataStruct.get_size())

            # send_disk_info takes the buffer address (passes as BPTR internally)
            pkt_addr, _ = self.launcher.send_disk_info(self.state, info_mem.addr)
            reply = self._run_until_reply(pkt_addr, "disk_info")

            if reply is None:
                # The handler can still answer later and fill the buffer
                # then, so it stays allocated.
                return None, None
            res1, res2 = reply
            if res1 == 0:
                # DOSFALSE: the handler answered, refusing the request. The
                # InfoData buffer is not filled in, but res2 says why.
                self.vh.alloc.free_memory(info_mem)
                return None, res2

            # Read InfoDataStruct fields (all LONGs at known offsets):
            # id_NumBlocks: 12, id_NumBlocksUsed: 16,
            # id_BytesPerBlock: 20, id_DiskType: 24, id_VolumeNode: 28
            num_blocks = self.mem.r32(info_mem.addr + 12)
            num_blocks_used = self.mem.r32(info_mem.addr + 16)
            bytes_per_block = self.mem.r32(info_mem.addr + 20)
            disk_type = self.mem.r32(info_mem.addr + 24)
            volume_node = self.mem.r32(info_mem.addr + 28)

            self.vh.alloc.free_memory(info_mem)
            return {
                "num_blocks": num_blocks,
                "num_blocks_used": num_blocks_used,
                "bytes_per_block": bytes_per_block,
                "disk_type": disk_type,
                "volume_node": volume_node,
            }, None

    # AmigaDOS id_DiskType sentinels meaning "no usable volume mounted"
    _UNMOUNTED_DISK_REASONS = {
        ID_NO_DISK_PRESENT: "no disk present",
        ID_UNREADABLE_DISK: "unreadable disk",
        ID_NOT_REALLY_DOS: "not a DOS disk",
        ID_BUSY: "disk busy or inhibited",
        ID_KICKSTART_DISK: "Kickstart disk has no DOS volume",
    }

    # res2 codes from a refused ACTION_DISK_INFO that positively identify an
    # unusable volume. A handler can reject a disk this way instead of filling
    # in an NDOS id_DiskType the way pfs3aio does. Only diagnostic codes belong
    # here: any other refusal stays inconclusive rather than risk calling a
    # healthy image broken.
    _UNMOUNTED_DISK_ERRORS = {
        ERROR_NOT_A_DOS_DISK: "not a DOS disk",
        ERROR_NO_DISK: "no disk present",
        ERROR_DEVICE_NOT_MOUNTED: "device not mounted",
    }

    def is_mounted(self) -> MountState:
        """Decide whether a usable volume is mounted.

        The state is None when the handler does not provide enough information
        to decide. Any nonzero type except a known unusable sentinel counts as
        mounted, allowing custom filesystem identifiers. id_VolumeNode can
        confirm a mount when the type is zero. A handler that refuses the
        packet outright is judged by its res2 error code instead, and the
        returned InfoData is None in that case.
        """
        info, res2 = self._query_disk_info()
        if not info:
            if res2 is None:
                return MountState(
                    None, None, "handler did not report disk info")
            reason = self._UNMOUNTED_DISK_ERRORS.get(res2)
            if reason is not None:
                return MountState(False, None, reason, res2)
            return MountState(
                None, None,
                f"handler refused disk info with error {res2}", res2)

        disk_type = info.get("disk_type", 0)
        reason = self._UNMOUNTED_DISK_REASONS.get(disk_type)
        if reason is not None:
            return MountState(False, info, reason)
        if disk_type != 0:
            return MountState(True, info, None)
        if info.get("volume_node", 0) != 0:
            return MountState(True, info, None)
        return MountState(
            None, info, "handler reported no disk type or volume node")

    def volume_name(self) -> str:
        """Best-effort name: the root's, else the opened partition's, else fallback."""
        with self._lock:
            if self._volname:
                return self._volname
            # try root FIB name (usually the volume name)
            try:
                fib_mem = self._alloc_fib()
                # lock_bptr=0 -> current volume root
                root_lock, _ = self.locate(0, "")
                pkt_addr, _ = self.launcher.send_examine(self.state, root_lock, fib_mem.addr)
                reply = self._run_until_reply(pkt_addr, "examine")
                if reply is not None and reply[0]:
                    info = _parse_fib(self.mem, fib_mem.addr)
                    if info.get("name"):
                        self._volname = info["name"]
                        if root_lock:
                            self.free_lock(root_lock)
                        return self._volname
                if root_lock:
                    self.free_lock(root_lock)
            except Exception:
                pass
            # try the name of the partition the backend opened
            try:
                part = None
                if self.backend.rdb:
                    # No number: the backend opened the first partition.
                    part = self.backend.rdb.get_partition(
                        self.backend.partition_number or 0)
                if part is not None:
                    # The name --partition found it by first, as the mount
                    # banner and the default mountpoint use it: DH0.1 for a
                    # repeated DH0. An FSString is true even when empty, and
                    # None for a damaged name; callers expect a str (verify
                    # --json serializes it).
                    name = self.backend.partition_name
                    if not name:
                        drive_name = part.get_drive_name()
                        name = str(drive_name) if drive_name is not None else ""
                    self._volname = name or f"#{part.num}"
                    return self._volname
            except Exception:
                pass
            self._volname = "AmigaFS"
            return self._volname

    def read_file(self, path: str, size: int, offset: int) -> Optional[bytes]:
        """Open path, read up to size bytes at offset, and close it again.

        Returns b"" at end of file or when the file does not exist, and None
        when the handler fails the open, the seek or the read.
        """
        with self._lock:
            if path and path != "/" and self._is_neg_cached(path):
                return b""
            # split into parent lock + name
            parts = [p for p in path.split("/") if p]
            if not parts:
                return b""
            name = parts[-1]
            dir_path = "/" + "/".join(parts[:-1])
            dir_lock, dir_res2, locks = self.locate_path(dir_path)
            if dir_lock == 0 and dir_path != "/":
                if path and path != "/" and dir_res2 in _NOT_FOUND_ERRORS:
                    self._set_neg_cached(path)
                return b"" if dir_res2 in _NOT_FOUND_ERRORS else None
            _, name_bptr = self._alloc_bstr(name)
            fh_addr = self._alloc_fh()
            _, pkt_addr, _ = self.launcher.send_findinput(
                self.state, name_bptr, dir_lock, fh_addr)
            reply = self._run_until_reply(pkt_addr, "findinput")
            if reply is None or reply[0] == 0:
                res2 = reply[1] if reply else -1
                if path and path != "/" and res2 in _NOT_FOUND_ERRORS:
                    self._set_neg_cached(path)
                self._free_fh(fh_addr)
                for l in reversed(locks):
                    self.free_lock(l)
                return b"" if res2 in _NOT_FOUND_ERRORS else None
            # optional seek
            if offset:
                data = self.read_handle_at(fh_addr, offset, size)
            else:
                data = self.read_handle(fh_addr, size)
            pkt_addr, _ = self.launcher.send_end_handle(self.state, fh_addr)
            self._run_until_reply(pkt_addr, "end")
            self._free_fh(fh_addr)
            for l in reversed(locks):
                self.free_lock(l)
            return data

    def seek_handle(self, fh_addr: int, offset: int,
                    mode: int = OFFSET_BEGINNING) -> Tuple[SeekResult, Optional[int]]:
        """Move the handle offset bytes from mode, as AmigaDOS Seek() does.

        Returns (SeekResult.OK, old position) on success. A refusal with
        ERROR_SEEK_ERROR is PAST_END, and any other refusal or no reply is
        FAILED; the position is then None. An offset that does not fit in a
        LONG is FAILED without reaching the handler, which would see it
        wrapped.
        """
        if not _LONG_MIN <= offset <= _LONG_MAX:
            return SeekResult.FAILED, None
        with self._lock:
            pkt_addr, _ = self.launcher.send_seek_handle(self.state, fh_addr, offset, mode)
            reply = self._run_until_reply(pkt_addr, "seek")
        if reply is None:
            return SeekResult.FAILED, None
        res1, res2 = reply
        # Seek() fails only with -1; any other dp_Res1 is the old position.
        if res1 == -1:
            if res2 == ERROR_SEEK_ERROR:
                return SeekResult.PAST_END, None
            return SeekResult.FAILED, None
        return SeekResult.OK, res1

    def handle_position(self, fh_addr: int) -> Tuple[bool, Optional[int]]:
        """Return the handler's position for the handle, without moving it.

        A zero-length OFFSET_CURRENT seek answers with the position.
        Returns (False, None) when it gets no reply: the handler may still
        hold the packet. Otherwise (True, position), where position is None
        when the answer is no position from 0 to 0x7FFFFFFF. Past 2 GiB,
        pfs3aio refuses with ERROR_SEEK_ERROR, FastFileSystem v0.2 answers
        -1 with no error code, and SmartFilesystem answers with a position
        that dp_Res1, a signed LONG, carries as negative.
        """
        with self._lock:
            pkt_addr, _ = self.launcher.send_seek_handle(
                self.state, fh_addr, 0, OFFSET_CURRENT)
            reply = self._run_until_reply(pkt_addr, "seek")
        if reply is None:
            return False, None
        position = reply[0]
        return True, (position if 0 <= position <= _LONG_MAX else None)

    def _handle_end(self, fh_addr: int) -> Optional[int]:
        """Move the handle to end of file and return that position.

        Returns None when either seek fails. ACTION_SEEK answers with the
        old position, so a zero-length OFFSET_CURRENT seek reads the end.
        dp_Res1 is a signed LONG, so the end of a file of 2 GiB or more
        arrives negative; that is None too.
        """
        with self._lock:
            if self.seek_handle(fh_addr, 0, OFFSET_END)[0] is not SeekResult.OK:
                return None
            result, end = self.seek_handle(fh_addr, 0, OFFSET_CURRENT)
        return end if result is SeekResult.OK and end >= 0 else None

    def _handle_extent(self, fh_addr: int) -> Optional[Tuple[Optional[int], Optional[int]]]:
        """Return (position, end of file) and leave the handle where it was.

        ACTION_SEEK answers with the old position, so a seek to the end
        reads the position and the seek back reads the end. Returns None
        when the handle may have moved: the seek to the end failed other
        than with ERROR_SEEK_ERROR or got no reply, or the seek back did
        not succeed.

        The end is None when it cannot be measured but the handle stayed.
        pfs3aio refuses the seek to the end of a file of 2 GiB or more with
        ERROR_SEEK_ERROR, which leaves the handle unmoved, and the position
        is then None as well. SmartFilesystem answers the seek back with an
        end that dp_Res1, a signed LONG, carries as negative.
        """
        with self._lock:
            result, start = self.seek_handle(fh_addr, 0, OFFSET_END)
            if result is SeekResult.PAST_END:
                return None, None
            if result is not SeekResult.OK:
                return None
            result, end = self.seek_handle(fh_addr, start)
        if result is not SeekResult.OK:
            return None
        return start, (end if end >= 0 else None)

    def read_handle(self, fh_addr: int, size: int) -> Optional[bytes]:
        """Read up to size bytes from the handle's current position.

        The read goes out as ACTION_READ packets of at most IO_CHUNK bytes,
        one after another, and ends at the first short one, which is end of
        file. Returns b"" at end of file, and None when the handler fails
        any packet (dp_Res1 is -1), claims more bytes than were asked for,
        or does not reply in time, so that a damaged file cannot pass for a
        short one.

        One call holds the bridge lock for the whole read, so the other
        callbacks, which a read-only mount runs on threads of their own,
        wait for it: about 0.2 s per MiB on OFS. Every packet's bytes stay
        in host memory until the read ends, and joining them briefly takes
        twice that.
        """
        with self._lock:
            parts = []
            while size > 0:
                n = min(size, IO_CHUNK)
                try:
                    buf_mem = self._alloc_read_buf(n)
                except FuseOSError:
                    return None  # past _RETIRED_MAX; nothing was sent
                pkt_addr, _ = self.launcher.send_read_handle(
                    self.state, fh_addr, buf_mem.addr, n)
                reply = self._run_until_reply(pkt_addr, "read")
                if reply is None:
                    return None
                res1 = reply[0]
                # More than n would mean the handler wrote past the buffer.
                if res1 < 0 or res1 > n:
                    return None
                if res1:
                    parts.append(bytes(self.mem.r_block(buf_mem.addr, res1)))
                if res1 < n:
                    break
                size -= n
            return b"".join(parts)


    def read_handle_at(self, fh_addr: int, offset: int, size: int) -> Optional[bytes]:
        """Seek to offset and read up to size bytes from there.

        Returns b"" at or past end of file, and None when the seek or the
        read fails or the offset does not fit in a LONG.
        """
        with self._lock:
            result, _ = self.seek_handle(fh_addr, offset)
            if result is SeekResult.PAST_END:
                # End of file only if the offset is past the real end: AROS
                # afs refuses the same way inside a file it cannot follow.
                end = self._handle_end(fh_addr)
                if end is None or offset < end:
                    return None
                return b""
            if result is not SeekResult.OK:
                # A refused seek leaves the handle at its old position, and
                # reading there would return the wrong bytes.
                return None
            return self.read_handle(fh_addr, size)


    def write_handle(self, fh_addr: int, data: bytes) -> int:
        """Write data at the handle's current position.

        Returns the number of bytes written, or -1 when the write fails.
        Callers that need the handler's error code want write_handle_reply().
        """
        return self.write_handle_reply(fh_addr, data)[0]

    def write_handle_reply(self, fh_addr: int, data: bytes) -> Tuple[int, int]:
        """Write data at the handle's position and return (dp_Res1, dp_Res2).

        The data goes out as ACTION_WRITE packets of at most IO_CHUNK bytes,
        one after another, and a short one ends the write. dp_Res1 is the
        number of bytes written, or -1 when the handler fails any packet or
        claims more bytes than it was sent, and dp_Res2 then holds its
        AmigaDOS error code. A packet that gets no reply fails the write
        with (-1, 0).

        A failed write that would have extended the file shrinks it back
        to its old end and keeps the error, as _write_past_end does after
        a failed fill. The packets before the failed one are on disk, and
        FastFileSystem also keeps the part of the failed packet that fit;
        pfs3aio and SmartFilesystem keep nothing of a packet they fail.
        Every write with data measures the old end first, with two seeks,
        because only the handler knows it: a size cached by the caller can
        be wrong, and a shrink to a wrong end would cut data or grow the
        file over old blocks. A write into a file whose end cannot be
        measured goes out without the undo. On pfs3aio and SmartFilesystem that is
        a file of 2 GiB or more, which the write cannot grow, since
        AmigaFuseFS ends every write at or below 0x7FFFFFFF. Only when the
        seeks may have moved the handle does the write fail before sending
        any data. Inside the old end a failure cannot be undone: the
        packets before it stay written. A handler can write part of the
        packet it fails, so the handle's position is unknown after any
        failure.

        The undo calls set_handle_size directly: the end is measured
        already, and a shrink needs no fill. Nor does it need a retry for
        another handle, since FastFileSystem stops a shrink only at another
        handle past the new end, and every other handle is at or below the
        old end.

        One call holds the bridge lock for the whole write, about 0.4 s per
        MiB on OFS, and a writable mount serves one request at a time, so
        every other callback waits for it. The caller's data stays in host
        memory until the write ends, and each packet copies at most
        IO_CHUNK bytes of it.
        """
        with self._lock:
            if not data:
                return 0, 0
            extent = self._handle_extent(fh_addr)
            if extent is None:
                return -1, 0
            start, end = extent
            written, res2 = self._write_packets(fh_addr, data)
            if written < 0 and end is not None and start + len(data) > end:
                # A failure before the old end left the file that size, and
                # shrinking to it then changes nothing.
                self._shrink_back(fh_addr, end)
            return written, res2

    def _write_packets(self, fh_addr: int, data: bytes) -> Tuple[int, int]:
        """Send data as ACTION_WRITE packets, as write_handle_reply() does.

        Unlike write_handle_reply(), this never measures or shrinks the
        file, for callers that undo a failure themselves.
        """
        with self._lock:
            done, res2 = 0, 0
            while done < len(data):
                chunk = data[done:done + IO_CHUNK]
                try:
                    buf_mem = self._alloc_read_buf(len(chunk))
                except FuseOSError:
                    return -1, 0  # past _RETIRED_MAX; nothing was sent
                self.mem.w_block(buf_mem.addr, chunk)
                pkt_addr, _ = self.launcher.send_write_handle(
                    self.state, fh_addr, buf_mem.addr, len(chunk))
                reply = self._run_until_reply(pkt_addr, "write")
                # A write the handler accepted, or one it may still carry
                # out, needs no zero-length write from close_file().
                if reply is None:
                    self._unwritten_outputs.discard(fh_addr)
                    return -1, 0
                res1, res2 = reply
                if res1 < 0 or res1 > len(chunk):
                    return -1, res2
                self._unwritten_outputs.discard(fh_addr)
                done += res1
                if res1 < len(chunk):
                    break
            return done, res2

    def _shrink_back(self, fh_addr: int, end: int) -> None:
        """Shrink the file back to end after a failed write, then seek to 0.

        Shrinking never exposes old blocks. If it fails, what was written
        stays, and the write keeps its own error.

        SmartFilesystem needs the seek. After a write that ran out of space
        on a full volume once at least one of its packets was written, and
        the shrink back, it fails a write at the new end until the handle
        moves. A seek to where the handle already is does not count, so
        the handle goes to the start of the file. The seek's result is not
        checked: if the seek fails, SmartFilesystem can fail the next write
        at that end. AmigaFuseFS.write drops its cached position after a
        failed write, so its next write seeks from 0.
        """
        with self._lock:
            self.set_handle_size(fh_addr, end)
            self.seek_handle(fh_addr, 0)

    def write_handle_at(self, fh_addr: int, offset: int, data: bytes,
                        end: Optional[int] = None) -> Tuple[int, int]:
        """Write data at offset and return (dp_Res1, dp_Res2) like write_handle_reply().

        A write past end of file first fills the gap with zeros, as POSIX
        requires. Any other failed seek, including one to an offset that
        does not fit in a LONG, fails the write instead of writing at the
        handle's old position.

        end, when given, is the end of file the caller holds for the
        handle, and a write past it fills the gap from it. BFFS keeps an
        existing file's old bytes past what an ACTION_FINDOUTPUT handle
        wrote, and seeks into them, until the handle ends
        (AmigaFuseFS._pinned_end).
        """
        with self._lock:
            if end is not None and offset > end:
                return self._write_past_end(fh_addr, offset, data, end)
            result, _ = self.seek_handle(fh_addr, offset)
            if result is SeekResult.PAST_END:
                return self._write_past_end(fh_addr, offset, data)
            if result is not SeekResult.OK:
                return -1, 0
            return self.write_handle_reply(fh_addr, data)

    def _write_past_end(self, fh_addr: int, offset: int, data: bytes,
                        end: Optional[int] = None) -> Tuple[int, int]:
        """Fill the file with zeros from its end to offset, then write data.

        AmigaDOS cannot seek past end of file, so this writes the gap
        instead. Returns (dp_Res1, dp_Res2) like write_handle_reply(). With
        no data, the file only grows to offset, as truncate() needs. A
        write that fails once the fill has begun shrinks the file back to
        its old end, so the zeros do not stay behind and fill the volume.

        end, when given, is the end of file to fill from in place of the
        handler's, as write_handle_at() takes it. The handler's end is
        still measured: when it is lower, the fill starts there, and
        otherwise the fill up to it overwrites blocks the file already
        holds, as BFFS keeps the old file's, which the space check does
        not count as new.

        The bridge lock is held until the fill ends, and a writable mount
        serves one request at a time, so a large gap stalls the whole mount.

        ACTION_SET_FILE_SIZE would extend the file in one packet, but FFS,
        OFS, pfs3aio and SFS leave the new range holding whatever its blocks
        held before, such as a deleted file's data.
        """
        with self._lock:
            measured = self._handle_end(fh_addr)
            if end is None or (measured is not None and measured < end):
                end = measured
            elif self.seek_handle(fh_addr, end)[0] is not SeekResult.OK:
                return -1, 0
            # ERROR_SEEK_ERROR inside the file (AROS afs cannot follow a
            # damaged file's extension blocks) is a failure, not a gap.
            if end is None or end > offset:
                return -1, 0
            # The file holds blocks up to the handler's end.
            held_end = max(end, measured or 0)
            # Fail before writing anything when the volume cannot hold the
            # gap and the data. Block overhead can still run the fill out of
            # space near the limit, and the handler then reports disk full.
            info = self.get_disk_info()
            if info and info["bytes_per_block"]:
                bpb = info["bytes_per_block"]
                free_blocks = max(info["num_blocks"] - info["num_blocks_used"], 0)
                # The file's last block may still have room before a new one
                # is needed.
                if (offset + len(data) - held_end
                        > free_blocks * bpb + (-held_end % bpb)):
                    return -1, ERROR_DISK_FULL
            zeros = bytes(min(offset - end, ZERO_FILL_CHUNK))
            written, res2 = 0, 0
            pos = end
            while pos < offset:
                n = min(offset - pos, len(zeros))
                written, res2 = self._write_packets(fh_addr, zeros[:n])
                if written != n:
                    written = -1
                    break
                pos += n
            if written >= 0:
                written, res2 = self._write_packets(fh_addr, data)
            if written < 0:
                # Shrinking never exposes old blocks. If it fails as well,
                # the zeros written so far stay.
                self._shrink_back(fh_addr, end)
            return written, res2

    def truncate_handle(self, fh_addr: int, length: int,
                        others: Sequence[int] = (),
                        end: Optional[int] = None,
                        past_end: bool = False) -> Tuple[int, int]:
        """Make the file length bytes long, as POSIX ftruncate() does.

        Returns (new size, 0), or (-1, error) when the change fails: a
        failed grow carries ERROR_DISK_FULL when the volume cannot hold
        it, or the error code of the write that failed, and a failed
        shrink carries the handler's error code. A shrink whose reply
        carries an error code fails even when dp_Res1 is a size: BFFS
        refuses every ACTION_SET_FILE_SIZE with DOSFALSE, which reads as a
        size of 0. A truncate to 0 that BFFS refuses with DOSFALSE still
        returns (0, 0) when the file is already empty. A length outside 0
        to 0x7FFFFFFF, a failed seek or a missing reply carries 0.

        A grow writes zeros from the old end of file, as a write past it
        does, and fails before writing anything when the volume cannot
        hold them. ACTION_SET_FILE_SIZE would add the range in one packet,
        but AmigaDOS does not define its bytes, and FFS, OFS, pfs3aio and
        SFS leave whatever the new blocks held before, such as a deleted
        file's data. A shrink is one ACTION_SET_FILE_SIZE. A truncate to
        the measured end of file sends none: BFFS refuses every
        ACTION_SET_FILE_SIZE, even one that would change nothing.

        The end of file tells a grow from a shrink; a seek to the length
        cannot. BFFS built with ALLOW_SPARSE_FILES accepts a seek past end
        of file, and AROS afs refuses one inside a damaged file. Only when
        the end cannot be measured does the seek decide, as it does in
        write_handle_at: pfs3aio refuses the seek to the end of a file of
        2 GiB or more, SFS answers it with an end dp_Res1 cannot carry,
        and AROS afs can fail the seek to the end of a damaged file.

        end, when given, is the end of file the caller holds for the
        handle, as in write_handle_at(). A grow past it writes zeros from
        it, or from the handler's end where that is lower
        (_write_past_end), and a shrink below it goes out as
        ACTION_SET_FILE_SIZE, which BFFS refuses. For a length from 1 up
        to end the handler's end is measured too, and a length past that
        lower end is a grow from it.

        past_end says the file may hold data past end, from a write that
        failed (AmigaFuseFS._pinned_end). The handler's end, which counts
        that data, then decides for any length from 1: a length below it
        is a shrink, and any other a grow that writes zeros from end, so
        the file ends at length.

        SetFileSize() leaves no other handle on the file past its new end.
        FFS stops a shrink at such a handle and replies with the size it
        reached, SFS refuses it with ERROR_OBJECT_IN_USE, and AROS afs with
        ERROR_SEEK_ERROR. After any of these replies, each handle in others
        moves to the start of its file and the shrink is sent once more. A
        shrink that gets no reply is not sent again: the handler may still
        hold its packet, and more packets would reuse its memory sooner
        (set_handle_size_reply waits for it twice). For the same reason the
        moves end at the first seek that fails, and the shrink is then not
        sent again. The size returned can still differ from length.
        """
        # A fill past 0x7FFFFFFF would leave a file no seek can reach the
        # end of, and ACTION_SET_FILE_SIZE carries the size in a LONG.
        if not 0 <= length <= _LONG_MAX:
            return -1, 0
        with self._lock:
            grow = False
            # A truncate to 0 still goes out at a caller's end of 0: after
            # a write that got no reply the file may hold more, and BFFS's
            # refusal then has truncate() empty it (_reopen_for_output).
            if end is not None and length:
                grow = length > end and not past_end
                if not grow:
                    # The caller's end can be above the handler's, as after
                    # a lost shrink not yet measured: a length past the
                    # handler's end is then a grow, which
                    # ACTION_SET_FILE_SIZE would fill with old blocks.
                    # BFFS's end, which counts its old bytes, is never
                    # below the caller's.
                    measured = self._handle_end(fh_addr)
                    if measured is not None:
                        # At the handler's end, past_end's zeros replace the
                        # failed write's data, and on BFFS the old bytes,
                        # and BFFS's end then stays at length.
                        grow = (length >= measured if past_end
                                else length > measured)
                    else:
                        result, _ = self.seek_handle(fh_addr, length)
                        if result is SeekResult.FAILED:
                            return -1, 0
                        grow = result is SeekResult.PAST_END
                    if not grow and end == length and not past_end:
                        # Nothing to change, and BFFS would refuse the
                        # packet.
                        return length, 0
            elif length:
                # A truncate to 0 cannot grow the file, so it skips the seeks.
                measured = self._handle_end(fh_addr)
                if measured == length:
                    # Nothing to change, and BFFS would refuse the packet.
                    return length, 0
                if measured is not None:
                    grow = length > measured
                else:
                    result, _ = self.seek_handle(fh_addr, length)
                    if result is SeekResult.FAILED:
                        return -1, 0
                    grow = result is SeekResult.PAST_END
            if grow:
                # _write_past_end measures the end again. Two more seeks
                # cost little next to the zeros it then writes.
                written, res2 = self._write_past_end(fh_addr, length, b"", end)
                return (length, 0) if written >= 0 else (-1, res2)
            size, res2 = self.set_handle_size_reply(fh_addr, length)
            # Send it again only after a reply that shows the stop: a size
            # past length, as from FFS, ERROR_OBJECT_IN_USE (202) from SFS,
            # or ERROR_SEEK_ERROR (219) from AROS afs. afs sets 219 before
            # it checks anything, so a refusal for another reason costs one
            # more packet. Any other refusal, such as BFFS's DOSFALSE, is
            # not about other handles, and with no reply the handler may
            # still hold the packet.
            if others and (size > length
                           or (size < 0 and res2 in (ERROR_OBJECT_IN_USE,
                                                     ERROR_SEEK_ERROR))):
                # A seek to 0 is not expected to be refused, so one that
                # fails may have had no reply and still be with the handler.
                # all() stops at it, and the shrink is not sent again: the
                # first reply stands, and truncate() fails with EIO.
                if all(self.seek_handle(other, 0)[0] is SeekResult.OK
                       for other in others):
                    size, res2 = self.set_handle_size_reply(fh_addr, length)
            if size >= 0 and not res2:
                return size, 0
            # A truncate to 0 skipped the end check, and BFFS refuses even
            # one that changes nothing, so its refusal of an empty file is
            # no failure. Only BFFS's own reply can be such a refusal: with
            # no reply the handler may still hold the packet, and a handler
            # that refuses every packet with DOSFALSE, such as one with no
            # disk, answers the seeks the same way, so they would read as
            # an end of 0.
            if (not length and size == 0
                    and res2 in (ERROR_OBJECT_NOT_FOUND,
                                 ERROR_DISK_WRITE_PROTECTED)
                    and self._handle_end(fh_addr) == 0):
                return 0, 0
            return -1, res2

    def set_handle_size(self, fh_addr: int, size: int, mode: int = OFFSET_BEGINNING) -> int:
        """Make the file size bytes long, as AmigaDOS SetFileSize() does.

        Returns the new size, or -1 when the change fails. Callers that
        need the handler's error code want set_handle_size_reply().
        """
        return self.set_handle_size_reply(fh_addr, size, mode)[0]

    def set_handle_size_reply(self, fh_addr: int, size: int,
                              mode: int = OFFSET_BEGINNING) -> Tuple[int, int]:
        """Send ACTION_SET_FILE_SIZE and return (dp_Res1, dp_Res2).

        dp_Res1 is the new size, or -1 when the handler fails the change,
        and dp_Res2 then holds its AmigaDOS error code. A change that gets
        no reply returns (-1, 0). Only an integer size from 0 to 0x7FFFFFFF
        with mode OFFSET_BEGINNING reaches the handler; anything else is
        (-1, 0) without a packet. dp_Arg2 is a signed LONG, so a larger
        size would arrive wrapped: 4 GiB + 4 would cut the file to 4
        bytes. From OFFSET_CURRENT or OFFSET_END the size resolves against
        a position the bridge does not track, and FastFileSystem takes a
        result below 0 or above 0x7FFFFFFF as a grow.

        A size in range is no safe grow either. FastFileSystem answers a
        grow larger than the free space by growing the file until the
        volume is full, then failing: 0x7FFFFFFF does this on any volume
        with less than 2 GiB free.

        A -1 for no reply does not mean the file is unchanged: on an OFS
        volume such a grow can outlast the wait and still finish later.
        So a change that gets no reply in time gets a second wait for the
        same packet, which sends nothing: a reply that comes just after
        the first wait would otherwise be dropped as late while the caller
        reports a failure. Only then is it (-1, 0). The packet is not sent
        again, because the handler may still hold it.
        """
        try:
            size, mode = operator.index(size), operator.index(mode)
        except TypeError:
            return -1, 0
        if mode != OFFSET_BEGINNING or not 0 <= size <= _LONG_MAX:
            return -1, 0
        with self._lock:
            pkt_addr, _ = self.launcher.send_set_file_size(
                self.state, fh_addr, size, mode)
            reply = self._run_until_reply(pkt_addr, "setsize")
            if reply is None:
                reply = self._run_until_reply(pkt_addr, "setsize")
        return (-1, 0) if reply is None else reply

    def delete_object(self, parent_lock_bptr: int, name: str) -> Tuple[int, int]:
        with self._lock:
            _, name_bptr = self._alloc_bstr(name)
            pkt_addr, _ = self.launcher.send_delete_object(
                self.state, parent_lock_bptr, name_bptr)
            reply = self._run_until_reply(pkt_addr, "delete")
            return reply if reply is not None else (0, -1)

    def delete_dir_atomic(self, path: str) -> Tuple[int, int]:
        """Atomically locate parent and delete directory, preventing concurrent
        locates on the target from creating locks that block deletion."""
        with self._lock:
            dir_path, name = path.rsplit("/", 1) if "/" in path.lstrip("/") else ("/", path.strip("/"))
            if not dir_path:
                dir_path = "/"
            parts = [p for p in dir_path.split("/") if p]
            lock = 0
            for comp in parts:
                _, name_bptr = self._alloc_bstr(comp)
                pkt_addr, _ = self.launcher.send_locate(self.state, lock, name_bptr)
                reply = self._run_until_reply(pkt_addr, "locate")
                parent = lock
                lock = reply[0] if reply is not None else 0
                if parent:
                    # Free intermediate locks while descending (see locate_path).
                    self.free_lock(parent)
                if lock == 0:
                    break
            locks: List[int] = [lock] if lock else []
            if dir_path == "/" and lock == 0:
                lock, _ = self.locate(0, "")
                if lock:
                    locks.append(lock)
            if lock == 0 and dir_path != "/":
                for l in reversed(locks):
                    self.free_lock(l)
                return 0, -1
            _, name_bptr = self._alloc_bstr(name)
            pkt_addr, _ = self.launcher.send_delete_object(self.state, lock, name_bptr)
            reply = self._run_until_reply(pkt_addr, "delete_dir_atomic")
            for l in reversed(locks):
                self.free_lock(l)
            return reply if reply is not None else (0, -1)

    def rename_object(
        self, src_lock_bptr: int, src_name: str, dst_lock_bptr: int, dst_name: str
    ) -> Tuple[int, int]:
        with self._lock:
            _, src_bptr = self._alloc_bstr(src_name)
            _, dst_bptr = self._alloc_bstr(dst_name)
            pkt_addr, _ = self.launcher.send_rename_object(
                self.state, src_lock_bptr, src_bptr, dst_lock_bptr, dst_bptr
            )
            # A late reply to a packet that timed out, a WRITE's byte count
            # say, would otherwise pass for DOSTRUE.
            reply = self._run_until_reply(pkt_addr, "rename")
            return reply if reply is not None else (0, -1)

    def create_dir(self, parent_lock_bptr: int, name: str) -> Tuple[int, int]:
        with self._lock:
            _, name_bptr = self._alloc_bstr(name)
            pkt_addr, _ = self.launcher.send_create_dir(
                self.state, parent_lock_bptr, name_bptr)
            reply = self._run_until_reply(pkt_addr, "mkdir")
            return reply if reply is not None else (0, -1)

    def set_date(self, parent_lock_bptr: int, name: str, when: float) -> Tuple[int, int]:
        """Date name in the directory with ACTION_SET_DATE, to Unix time when.

        Returns (dp_Res1, dp_Res2), or (0, -1) when no reply comes.
        """
        with self._lock:
            ds_addr = self._alloc_datestamp().addr
            for offset, value in zip((0, 4, 8), to_datestamp(when)):
                self.mem.w32(ds_addr + offset, value)
            _, name_bptr = self._alloc_bstr(name)
            pkt_addr, _ = self.launcher.send_set_date(
                self.state, parent_lock_bptr, name_bptr, ds_addr)
            reply = self._run_until_reply(pkt_addr, "set_date")
            return reply if reply is not None else (0, -1)

    def write_empty_reply(self, fh_addr: int) -> Tuple[int, int]:
        """Send a zero-length ACTION_WRITE and return (dp_Res1, dp_Res2).

        BFFS's ACTION_FINDOUTPUT leaves an existing file as it was and cuts
        it at ACTION_END to the furthest byte the handle reached, but only
        once the handle has written, and a write of no bytes counts. BFFS
        can refuse even that one, with ERROR_DISK_FULL when it cannot
        extend the file's last block. Returns (0, 0), or (-1, dp_Res2) when
        the handler fails the write, or (-1, 0) when it gets no reply.
        close_file() sends no second one either way.
        """
        with self._lock:
            self._unwritten_outputs.discard(fh_addr)
            buf_mem = self._alloc_empty_buf()
            pkt_addr, _ = self.launcher.send_write_handle(
                self.state, fh_addr, buf_mem.addr, 0)
            reply = self._run_until_reply(pkt_addr, "write")
        if reply is None:
            return -1, 0
        # A refusal can come as DOSFALSE: BFFS's read-only checks reply 0.
        return (0, 0) if reply == (0, 0) else (-1, reply[1])

    def close_file(self, fh_addr: int):
        """End the handle with ACTION_END and free its FileHandle.

        A handle opened with ACTION_FINDOUTPUT that wrote nothing first
        gets a zero-length ACTION_WRITE (write_empty_reply()), so that BFFS
        empties the file. FastFileSystem, pfs3aio and SmartFilesystem empty
        it at the open, and the write changes nothing.
        """
        with self._lock:
            if fh_addr in self._unwritten_outputs:
                self.write_empty_reply(fh_addr)
            pkt_addr, _ = self.launcher.send_end_handle(self.state, fh_addr)
            self._run_until_reply(pkt_addr, "end")
            # Note: ACTION_FLUSH is for flushing volume buffers, not file-specific.
            # We call flush_volume() on unmount instead of after every file close.
            self._free_fh(fh_addr)

    def flush_volume(self, wait_pending: bool = True):
        """Flush the handler's buffers to disk.

        ACTION_FLUSH proves nothing while an earlier packet is unanswered:
        a handler may answer it at once, in the middle of that packet's
        work (#118). So the packets still out get wait_for_pending's bound
        first, and OSError names those that stay out: the volume may be
        half changed, and the caller must not report it as written. A
        handler at work at the bound gets no flush; one that waits does,
        since it may answer only once a packet wakes it, and the work that
        ends then gets a flush of its own.

        With wait_pending False the flush goes out at once: unmount waits
        by itself, and flushes what it can even with packets left out.
        """
        with self._lock:
            if self.state.crashed:
                raise OSError("handler crashed before volume flush")
            if wait_pending:
                unanswered = self.wait_for_pending()
                # Not after a wait that ended at its deadline: a reply taken
                # there leaves _busy_ran_out clear with the handler at work.
                if unanswered and self._pending_wait_parked:
                    try:
                        self._flush_now()
                    except OSError:
                        pass  # the packets still out decide
                    unanswered = self.wait_for_pending(timeout=1.0)
                if unanswered and not self.state.crashed:
                    raise OSError(
                        f"the handler has not answered {', '.join(unanswered)}; "
                        f"the volume may be left half changed")
            self._flush_now()

    def _flush_now(self):
        """Send ACTION_FLUSH and bring what it wrote to stable storage."""
        if self.state.crashed:
            raise OSError("handler crashed before volume flush")
        if self._debug:
            print("[amifuse] Flushing volume buffers to disk...", flush=True)
        pkt_addr, _ = self.launcher.send_flush(self.state)
        # Only the flush's own reply says that the buffers are written.
        reply = self._run_until_reply(pkt_addr, "flush_volume")
        # Sync the underlying file to disk
        self.backend.sync()
        if self.state.crashed or reply is None or reply[0] == 0:
            raise OSError("filesystem handler did not acknowledge ACTION_FLUSH")
        # fsync() and a clean unmount promise data on stable storage.
        self.backend.fsync()
        if self._debug:
            print("[amifuse] Volume flush complete", flush=True)

    def inhibit_cycle(self):
        """Inhibit then uninhibit the handler to clear all internal state.

        Skipped if a packet is still out after a second, as the callers
        skip it while a file is open: an inhibit that reaches a handler in
        the middle of a packet's work is the same risk (#129, #265). The
        wait also takes replies that are only not polled yet.
        """
        with self._lock:
            if self.state.crashed:
                return
            unanswered = self.wait_for_pending(timeout=1.0)
            if unanswered:
                if self._debug:
                    print(f"[amifuse] inhibit cycle skipped: "
                          f"{', '.join(unanswered)} still out", flush=True)
                return
            # Each wait takes its own packet's reply: a late reply to another
            # packet would end the inhibit's wait before the handler inhibits.
            pkt_addr, _ = self.launcher.send_inhibit(self.state, True)
            self._run_until_reply(pkt_addr, "inhibit")
            pkt_addr, _ = self.launcher.send_inhibit(self.state, False)
            reply = self._run_until_reply(pkt_addr, "uninhibit")
            # Sync the underlying file to disk
            self.backend.sync()
            if self._debug:
                if reply is not None and reply[0] != 0:
                    print("[amifuse] Volume flush complete", flush=True)
                else:
                    print("[amifuse] Volume flush may have failed", flush=True)


# Every bridge method may run or wake a task, so all of them run on the
# bridge's emulation thread (emu_thread.py). close() ends that thread, and
# the idle-timer switches only post to it.
route_methods_through_emu(
    HandlerBridge,
    exclude={"close", "start_idle_timers", "stop_idle_timers", "_timer_device"},
)


class AmigaFuseFS(_FuseOperations):
    # Mounted through WinFsp, which goes on passing a handle's callbacks the
    # name the file was opened under after rename() moved it (_handle_path),
    # and passes utimens() an unchanged time as getattr() reports it. The
    # kernel's FUSE on Linux and macOS passes the current name, and libfuse
    # both times as asked for.
    _winfsp = sys.platform.startswith("win")
    # macOS special files we should reject immediately without calling handler.
    # Note: "Icon\r" and ".VolumeIcon.icns" are NOT in this list - we handle them for custom icons.
    _MACOS_SPECIAL = frozenset([
        "._.", ".hidden", ".Spotlight-V100", ".fseventsd",
        ".metadata_never_index", ".com.apple.timemachine.donotpresent",
        ".DS_Store", ".ql_disablethumbnails",
        ".localized", ".TemporaryItems", ".DocumentRevisions-V100",
        ".vol", ".file", ".hotfiles.btree", ".quota.user", ".quota.group",
        ".apdisk", ".com.apple.NetBootX", "mach_kernel", ".PKInstallSandboxManager",
        ".PKInstallSandboxManager-SystemSoftware", "Backups.backupdb",
    ])

    # Windows Explorer probe files we should reject immediately.
    # Includes common casings since Amiga handler uses case-sensitive lookups.
    _WINDOWS_SPECIAL = frozenset([
        "desktop.ini", "Desktop.ini",
        "Thumbs.db", "thumbs.db",
        "$RECYCLE.BIN", "$Recycle.Bin",
        "System Volume Information",
        "autorun.inf", "Autorun.inf",
        "RECYCLER", "Recycler",
        "Folder.jpg", "folder.jpg", "Folder.gif", "folder.gif",
        "AlbumArtSmall.jpg",
    ])

    def __init__(
        self,
        bridge: HandlerBridge,
        debug: bool = False,
        icons: bool = False,
        mountpoint: Optional[Path] = None, ready_fd=None):
        self.bridge = bridge
        self._debug = debug
        self._mountpoint = mountpoint
        self._unmount_control = None
        self._mount_record = None  # for platform.write_mount_record
        self._uid = getattr(os, 'getuid', lambda: 0)()
        self._gid = getattr(os, 'getgid', lambda: 0)()
        self._stat_cache: Dict[str, Tuple[float, Dict]] = {}  # path -> (timestamp, stat_result)
        self._cache_ttl = 3600.0  # Cache for 1 hour - read-only FS never changes
        self._neg_cache: Dict[str, float] = {}  # path -> timestamp for ENOENT results
        self._neg_cache_ttl = 3600.0  # Cache negative results for 1 hour
        self._dir_cache: Dict[str, Tuple[float, List[str]]] = {}  # path -> (timestamp, entries)
        self._dir_cache_ttl = 3600.0  # Cache directory listings for 1 hour
        # Paths whose stat cache entry must survive even at TTL=0. libfuse2's
        # high-level API calls getattr immediately after create() to fill the
        # entry reply; if that probe hit PFS3 it would get ERROR_OBJECT_IN_USE
        # while the new file's handle is still open, and the kernel would
        # return ENOENT to userspace. Populated by create() and an O_TRUNC
        # open(), cleared by release() and by an unlink() that deletes at
        # once; a deferred delete keeps it until release().
        self._pinned_stats: Set[str] = set()
        if self.bridge._write_enabled:
            self._cache_ttl = 0.0
            self._neg_cache_ttl = 0.0
            self._dir_cache_ttl = 0.0
        # Disk info cache: infinite for read-only (geometry never changes),
        # 30s for writable (free space changes as files are written)
        self._disk_info_ttl = -1  # -1 means infinite (cache forever)
        self._disk_info_cache = None  # (timestamp, result) tuple
        if self.bridge._write_enabled:
            self._disk_info_ttl = 30.0
        # Protects _stat_cache, _dir_cache, _neg_cache.
        # Note: _disk_info_cache is intentionally excluded -- it uses a simpler
        # atomic-assignment pattern (single tuple pointer write under CPython GIL,
        # acceptable since statfs is a single-value cache with no compound
        # read-modify-write).
        # Lock ordering invariant: _cache_lock must always be acquired before
        # _fh_lock, never in reverse, to prevent deadlocks.
        self._cache_lock = threading.RLock()
        self._fh_lock = threading.Lock()
        self._fh_cache: Dict[int, Dict[str, object]] = {}
        self._next_fh = 1
        self._pending_deletes: set = set()
        self._pending_dir_deletes: Dict[str, set] = {}  # dir_path -> set of pending child paths blocking removal
        # path -> the date utimens() set on an open file, sent at its last
        # release(). Guarded by _fh_lock.
        self._held_dates: Dict[str, float] = {}
        # How the handler folds names (_state_path). Only a write mount keeps
        # the state above, so the others send no packet to find out.
        self._name_fold = None
        if self.bridge._write_enabled:
            name_fold = getattr(self.bridge, "name_fold", None)
            self._name_fold = _NAME_FOLDS.get(name_fold() if name_fold else None)
        self._last_op_time = time.time()
        self._op_count = 0
        # Whether the bridge replies to timer.device requests while the
        # mount is idle; see _start_timer_thread.
        self._timers_started = False
        # Daemon mode: the pipe to tell the parent the mount is up; see
        # _fork_daemon_parent.
        self._ready_fd = ready_fd
        # Icon support - use platform-specific handler
        self._icons_enabled = icons
        self._icon_parser = None
        self._icon_cache = None
        self._icon_existence_cache = None
        self._icon_handler = None
        self._crash_shutdown_started = False
        # The timer thread and FUSE operations may both find the crash.
        self._crash_shutdown_lock = threading.Lock()
        if icons:
            from .icon_parser import IconParser
            from .icon_cache import IconCache, IconExistenceCache
            from . import platform
            self._icon_parser = IconParser(debug=debug)
            self._icon_cache = IconCache()
            self._icon_existence_cache = IconExistenceCache()
            self._icon_handler = platform.get_icon_handler(icons_enabled=True, debug=debug)

    def _is_platform_special(self, path: str) -> bool:
        """Return True if path is an OS special file we should reject.

        Filters macOS system files (Spotlight, .DS_Store, etc.) and Windows
        Explorer probe files (desktop.ini, Thumbs.db, etc.) to avoid
        unnecessary handler invocations.

        Note: On Linux, returns False (no special file filtering). The prior
        code (_is_macos_special) incorrectly filtered macOS special files on
        ALL platforms. This refactoring fixes that: each platform only filters
        its own OS-specific probes. Linux desktop environments don't probe
        mounted filesystems with special files the way macOS and Windows do.

        The AppleDouble (._) prefix check is inside the darwin branch because
        AppleDouble resource fork files are macOS-specific. On non-macOS
        platforms, files starting with ._ are not filtered.
        """
        name = path.rsplit("/", 1)[-1]
        if sys.platform.startswith("darwin"):
            if name.startswith("._"):  # AppleDouble resource fork files
                return True
            # .Trashes plus per-uid variants like .Trashes.501 -- match by
            # prefix instead of hardcoding a uid in the set.
            if name == ".Trashes" or name.startswith(".Trashes."):
                return True
            # Icon files are handled specially when icons enabled
            if self._icon_handler:
                from . import platform
                icon_file, volume_icon_file = platform.get_icon_file_names()
                if icon_file and name == icon_file:
                    return False  # Let it through - we handle it in getattr
                if volume_icon_file and name == volume_icon_file:
                    return False  # Let it through - we handle it in getattr
            return name in self._MACOS_SPECIAL
        if sys.platform.startswith("win"):
            return name in self._WINDOWS_SPECIAL
        return False

    def _is_icon_file(self, path: str) -> bool:
        """Return True if path is the virtual icon file for custom folder icons."""
        if self._icon_handler:
            return self._icon_handler.is_icon_file(path)
        return False

    def _is_volume_icon_file(self, path: str) -> bool:
        """Return True if path is the virtual volume icon file."""
        if self._icon_handler:
            return self._icon_handler.is_volume_icon_file(path)
        return False

    def _get_parent_dir(self, path: str) -> str:
        """Get the parent directory of a path."""
        if path == "/" or "/" not in path.lstrip("/"):
            return "/"
        return "/" + path.lstrip("/").rsplit("/", 1)[0]

    def _folded(self, path: str) -> str:
        """Return path with its case folded as the handler compares names."""
        return path if self._name_fold is None else path.translate(self._name_fold)

    def _state_path(self, path: str) -> str:
        """Return path as the per-path state spells it.

        Handlers find a name in any letter case, so a host program can reach
        one object under several spellings, while pinned entries, open
        handles, pending deletes and held dates are all kept under the path
        they were first given. Each callback that takes a path, and has no
        handle to name its object, starts here, so that an object or a
        directory with state has one spelling and the rest of the code can
        compare exact strings. The longest leading part of path that the
        state knows takes the state's spelling; what follows stays as given.

        The caller must not hold _fh_lock.
        """
        if self._name_fold is None:
            return path
        with self._cache_lock, self._fh_lock:
            known = set(self._pinned_stats)
            known.update(self._pending_deletes, self._pending_dir_deletes,
                         self._held_dates)
            known.update(v.get("path") for v in self._fh_cache.values())
        known.discard(None)
        if not known or path in known:
            return path
        spellings = {}
        for spelled in known:
            while len(spelled) > 1:
                spellings[self._folded(spelled)] = spelled
                spelled = spelled.rsplit("/", 1)[0]
        lead = self._folded(path)
        while len(lead) > 1:
            if lead in spellings:
                return spellings[lead] + path[len(lead):]
            lead = lead.rsplit("/", 1)[0]
        return path

    def _get_cached_stat(self, path: str) -> Optional[Dict]:
        """Return cached stat result if still valid, else None.

        A stale pin is measured first (_measure_stale_pin), which can send
        packets and takes the handle's lock, so the caller must not hold
        it.
        """
        self._measure_stale_pin(path)
        with self._cache_lock:
            if path in self._stat_cache:
                ts, result = self._stat_cache[path]
                if (time.time() - ts < self._cache_ttl) or path in self._pinned_stats:
                    return result
                del self._stat_cache[path]
        return None

    def _measure_stale_pin(self, path: str) -> None:
        """Lower path's pinned size to the end a late shrink left.

        truncate() marks a handle (stale_pin) when its shrink below the
        pinned size got no reply in either wait: the handler may still
        carry it out. The end is measured through that handle, until a
        measure gets its replies, and the pin becomes the smaller of the
        two. A handler that takes its
        packets in turn ends a shrink still under way before it answers
        the seeks. The pin never rises: a BFFS ACTION_FINDOUTPUT
        handle's end still counts the old file's bytes past what the
        handle wrote (_pinned_end).

        _handle_extent leaves the handle where it was; when the handle may
        have moved, it forgets its cached position. The caller holds
        neither _cache_lock nor the handle's lock.
        """
        if path not in self._pinned_stats:
            return
        with self._fh_lock:
            stale = [e for e in self._fh_cache.values()
                     if e.get("stale_pin") and _handle_path(e, "") == path]
        for entry in stale:
            with entry["lock"]:
                if not entry.get("stale_pin") or entry.get("closed"):
                    continue
                extent = self.bridge._handle_extent(entry["fh_addr"])
                if extent is None:
                    # The handler may still be busy with the shrink: the
                    # next callback measures again.
                    entry["pos"] = None
                    continue
                del entry["stale_pin"]
                end = extent[1]
                with self._cache_lock:
                    cached = self._stat_cache.get(path)
                    if (end is not None and path in self._pinned_stats
                            and cached is not None
                            and end < cached[1]["st_size"]):
                        self._pin_stat(path, end)

    def _set_cached_stat(self, path: str, result: Dict):
        """Cache a stat result."""
        with self._cache_lock:
            _prune_cache(self._stat_cache, self._pinned_stats)
            self._stat_cache[path] = (time.time(), result)

    def _is_neg_cached(self, path: str) -> bool:
        """Return True if path is in negative cache (known non-existent)."""
        if self._neg_cache_ttl <= 0:
            return False
        with self._cache_lock:
            if path in self._neg_cache:
                if time.time() - self._neg_cache[path] < self._neg_cache_ttl:
                    return True
                del self._neg_cache[path]
        return False

    def _set_neg_cached(self, path: str):
        """Cache a negative (ENOENT) result."""
        if self._neg_cache_ttl <= 0:
            return
        with self._cache_lock:
            _prune_cache(self._neg_cache)
            self._neg_cache[path] = time.time()

    def _track_op(self, op: str, path: str, cached: bool = False):
        """Track operations for debugging."""
        if not self._debug:
            return
        self._op_count += 1
        now = time.time()
        elapsed = now - self._last_op_time
        # Report every 10 seconds
        if elapsed > 10.0:
            rate = self._op_count / elapsed
            print(f"[FUSE] {self._op_count} ops in {elapsed:.1f}s ({rate:.1f}/s), last: {op}({path}) cached={cached}")
            self._op_count = 0
            self._last_op_time = now

    def _root_stat(self):
        now = int(time.time())
        if sys.platform.startswith("win"):
            # WinFsp maps group/other permissions to host ACLs. A global
            # drive must not grant other accounts root create/delete access.
            # uid/gid=-1 in mount_fuse keeps ownership with the mounting user.
            perm = 0o755 if self.bridge._write_enabled else 0o555
        else:
            perm = 0o777 if self.bridge._write_enabled else 0o755
        return {
            "st_mode": (perm | 0o040000),
            "st_nlink": 2,
            "st_size": 0,
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": self._uid,
            "st_gid": self._gid,
        }

    def _split_path(self, path: str) -> Tuple[str, str]:
        parts = [p for p in path.split("/") if p]
        if not parts:
            return "/", ""
        name = parts[-1]
        dir_path = "/" + "/".join(parts[:-1]) if len(parts) > 1 else "/"
        return dir_path, name

    def _check_handler_alive(self):
        """Check if handler is alive and raise EIO if crashed."""
        if _handler_has_crashed(self.bridge):
            if self._debug and not getattr(self, '_crash_reported', False):
                print(f"[FUSE] Handler crashed - all operations returning EIO", file=sys.stderr, flush=True)
                self._crash_reported = True
            self._schedule_crash_shutdown()
            raise FuseOSError(errno.EIO)

    def _schedule_crash_shutdown(self):
        with self._crash_shutdown_lock:
            if self._crash_shutdown_started:
                return
            self._crash_shutdown_started = True
        thread = threading.Thread(
            target=self._terminate_mount_after_crash,
            name="amifuse-crash-shutdown",
            daemon=True,
        )
        thread.start()

    def _terminate_mount_after_crash(self):
        # Give the failing FUSE operation a moment to return EIO before
        # terminating the process and letting the kernel drop the mount.
        time.sleep(0.5)
        print("[amifuse] Handler crashed - sending SIGTERM to unmount", flush=True)
        os.kill(os.getpid(), signal.SIGTERM)

    def _log_op(self, op: str, path: str, extra: str = ""):
        """Log operation if debug is enabled."""
        if self._debug:
            msg = f"[FUSE][{op}] {path}"
            if extra:
                msg += f" {extra}"
            print(msg, file=sys.stderr, flush=True)

    # --- FUSE operations ---
    def getattr(self, path, fh=None):
        self._check_handler_alive()
        open_file = False
        if fh is not None:
            with self._fh_lock:
                entry = self._fh_cache.get(fh)
                open_file = bool(entry and entry.get("path") and "fh_addr" in entry)
                path = _handle_path(entry, path)
        path = self._state_path(path)
        # Reject OS special files immediately without calling handler
        if self._is_platform_special(path):
            self._track_op("getattr", path, cached=True)
            raise FuseOSError(errno.ENOENT)
        if path == "/":
            self._track_op("getattr", path, cached=True)
            return self._root_stat()
        # Handle virtual Icon file for custom folder icons (platform-specific)
        if self._icon_handler and self._is_icon_file(path):
            parent_dir = self._get_parent_dir(path)
            # Check if parent directory has a custom icon
            if self._has_valid_icon(parent_dir):
                icns_data = self._get_icon_for_path(parent_dir)
                icns_size = len(icns_data) if icns_data else 0
                return self._icon_handler.get_icon_file_stat(icns_size, self._uid, self._gid)
            raise FuseOSError(errno.ENOENT)
        # Handle virtual volume icon file (platform-specific)
        # Note: macFUSE's volicon module handles this at mount time, but we keep
        # this as a fallback for older versions or non-macOS systems
        if self._icon_handler and self._is_volume_icon_file(path):
            if self._has_valid_icon("/"):
                icns_data = self._get_icon_for_path("/")
                if icns_data:
                    return self._icon_handler.get_volume_icon_stat(len(icns_data), self._uid, self._gid)
            raise FuseOSError(errno.ENOENT)
        # Check negative cache first (known non-existent paths)
        if self._is_neg_cached(path):
            self._track_op("getattr", path, cached=True)
            raise FuseOSError(errno.ENOENT)
        # A pending (deferred) delete is hidden from lookups by name, but a
        # file handle still open on it describes it until release() (#244).
        with self._fh_lock:
            pending = path in self._pending_deletes
        if pending and not open_file:
            self._track_op("getattr", path, cached=True)
            raise FuseOSError(errno.ENOENT)
        # Check positive cache
        cached = self._get_cached_stat(path)
        if cached is not None:
            self._track_op("getattr", path, cached=True)
            return cached
        # A file held on an exclusive lock, from create() or an O_TRUNC
        # open(), was answered from its pinned entry above: the handler
        # cannot examine it until release(). Any other open file is shared,
        # and the handler reports its current size and protection. This
        # relies on write mounts running single-threaded (mount_fuse sets
        # nothreads): no getattr() runs between the handler granting an
        # exclusive lock and the pin being set.
        self._track_op("getattr", path, cached=False)
        info = self.bridge.stat_path(path)
        if not info:
            # Cache negative result
            self._set_neg_cached(path)
            raise FuseOSError(errno.ENOENT)
        result = self._stat_from_fib(info, path, int(time.time()))
        self._set_cached_stat(path, result)
        return result

    def _stat_from_fib(self, ent: Dict, name: str, now: int) -> Dict:
        """Build a FUSE stat dict from a parsed FileInfoBlock entry.

        ``name`` is only used for the trailing ``.info`` check, so both a
        full path (getattr) and a bare entry name (readdir) work. The
        times are the entry's date, or ``now`` for an entry with none (a
        pinned one). AmigaDOS keeps no access or change time.
        """
        mtime = ent.get("mtime", now)
        is_dir = ent["dir_type"] >= 0
        # Convert Amiga protection bits to Unix mode
        prot = DosProtection(ent.get("protection", 0))
        base_mode = prot.to_host_mode()
        # For read-only mounts, strip write bits
        if not self.bridge._write_enabled:
            base_mode &= ~(0o222)
        # WinFsp builds each file's ACL from the mode, granting group and
        # other to the mounting user's group and Everyone. Amiga files rarely
        # carry GRP/OTR bits, so without this any other Windows account (a
        # global drive letter, a second logged-in user) can list the root but
        # open nothing beneath it. Mirror the owner's read/execute bits only;
        # write access for other accounts stays opt-in via the Amiga bits.
        if sys.platform.startswith("win"):
            base_mode |= ((base_mode & 0o500) >> 3) | ((base_mode & 0o500) >> 6)
        result = {
            "st_mode": base_mode | (0o040000 if is_dir else 0o100000),
            "st_nlink": 2 if is_dir else 1,
            "st_size": ent["size"],
            "st_ctime": mtime,
            "st_mtime": mtime,
            "st_atime": mtime,
            "st_uid": self._uid,
            "st_gid": self._gid,
            "st_blksize": 512,  # TODO: compute from ACTION_DISK_INFO?
            "st_blocks": ent["num_blocks"],
        }
        # Set UF_HIDDEN on .info files when icons mode is enabled: the Amiga
        # icon files are hidden since we display icons via xattrs instead.
        if self._icons_enabled and name.lower().endswith(".info"):
            result["st_flags"] = 0x8000  # UF_HIDDEN
        return result

    def _pin_stat(self, path: str, size: int):
        """Pin path's stat entry as a file of size bytes.

        For a file held on an exclusive lock, from create(), an O_TRUNC
        open() or truncate()'s reopen on BFFS (_reopen_for_output): the
        handler cannot examine it until release(), so this
        entry is all getattr() reports. It is built as a closed file's
        entry is, from protection 0, which a new file gets, so a new
        file's mode does not change at release(). An existing file opened
        with O_TRUNC can keep other protection bits. write() and
        truncate() rebuild it at each new size.
        """
        now = time.time()
        fib = {"dir_type": -3, "size": size, "protection": 0,  # ST_FILE
               "num_blocks": (size + 511) // 512}
        with self._cache_lock:
            self._stat_cache[path] = (now, self._stat_from_fib(fib, path, int(now)))
            self._pinned_stats.add(path)

    def _pinned_end(self, entry, path: str) -> Optional[int]:
        """Return the pinned size of the file the entry's handle holds, or None.

        A file held on an exclusive lock holds only what its handle wrote,
        which its pinned size counts (_pin_stat). BFFS keeps an existing
        file's old bytes until such a handle ends, and seeks into them, so
        truncate() and write() take the pinned size as the end of file,
        not the handler's. On the other handlers the two agree.

        A failed write, or a failed grow in truncate(), can leave data past
        the pin, which does not count it: BFFS refuses the shrink back, and
        a packet that got no reply may land later. It marks the handle
        (past_pin), and truncate() then has the handler's end decide
        between a shrink and a grow (truncate_handle's past_end). A grow,
        and a write past the pin, still fill with zeros from the pin, as
        the bytes past it are from a write that failed.

        A stale pin is measured first (_measure_stale_pin), so the caller
        must not hold the handle's lock.
        """
        path = _handle_path(entry, path)
        if path not in self._pinned_stats:
            return None
        cached = self._get_cached_stat(path)
        return None if cached is None else cached["st_size"]

    def _filter_pending_deletes(self, path: str, entries: List[str]) -> List[str]:
        """Drop directory entries whose full path has a pending (deferred) delete.

        The entries are spelled as the handler stores them, so they are
        compared as it compares names.
        """
        with self._fh_lock:
            if not self._pending_deletes:
                return entries
            pending = {self._folded(p) for p in self._pending_deletes}
            return [
                e for e in entries
                if e in (".", "..") or self._folded(
                    path.rstrip("/") + "/" + e if path != "/" else "/" + e
                ) not in pending
            ]

    def opendir(self, path):
        path = self._state_path(path)
        with self._fh_lock:
            handle = self._next_fh
            self._next_fh += 1
            self._fh_cache[handle] = {
                "dir_handle": True,
                "path": path,
                "closed": False,
            }
        return handle

    def releasedir(self, path, fh):
        with self._fh_lock:
            entry = self._fh_cache.get(fh)
            if not entry:
                return 0
            del self._fh_cache[fh]
        # A deferred rmdir() is looked for under the handle's path, as
        # release() looks for a deferred unlink().
        resolved_path = _handle_path(entry, path)
        if not resolved_path:
            return 0
        with self._fh_lock:
            if resolved_path not in self._pending_deletes:
                return 0
            still_open = any(
                v.get("dir_handle") and v.get("path") == resolved_path
                for v in self._fh_cache.values()
            )
            if still_open:
                return 0
            self._pending_deletes.discard(resolved_path)
        # Guard: skip deferred delete if handler is dead or read-only
        if self.bridge.state.crashed or not self.bridge._write_enabled:
            return 0
        delete_succeeded = False
        try:
            self.bridge.flush_volume()
            # The cycle clears pfs3aio's and SmartFilesystem's in-use state
            # after a directory change, but with a file open it breaks
            # SmartFilesystem's handles and can stop pfs3aio (#129).
            if not self._any_file_open():
                self.bridge.inhibit_cycle()
            res1, res2 = self.bridge.delete_dir_atomic(resolved_path)
            if res1 == 0:
                self._log_op("releasedir", resolved_path, f"deferred delete failed res2={res2}")
            else:
                delete_succeeded = True
                with self._cache_lock:
                    self._stat_cache.pop(resolved_path, None)
                    parent_path = resolved_path.rsplit("/", 1)[0] or "/"
                    self._dir_cache.pop(parent_path, None)
        except Exception as e:
            self._log_op("releasedir", resolved_path, f"deferred delete exception: {type(e).__name__}: {e}")
        if not delete_succeeded:
            with self._fh_lock:
                self._drop_failed_dir_delete(resolved_path)
            return 0
        self._resolve_pending_dir_deletes(resolved_path)
        return 0

    def readdir(self, path, fh):
        self._check_handler_alive()
        path = self._state_path(path)
        # Check directory cache first
        with self._cache_lock:
            if path in self._dir_cache:
                ts, cached_entries = self._dir_cache[path]
                if time.time() - ts < self._dir_cache_ttl:
                    self._track_op("readdir", path, cached=True)
                    return self._filter_pending_deletes(path, cached_entries)
                del self._dir_cache[path]

        self._track_op("readdir", path, cached=False)
        entries = [".", ".."]
        dir_entries = self.bridge.list_dir_path(path)
        now = time.time()
        for ent in dir_entries:
            name = ent["name"]
            entries.append(name)
            # Pre-populate stat cache from directory listing
            child_path = path.rstrip("/") + "/" + name if path != "/" else "/" + name
            stat_result = self._stat_from_fib(ent, name, int(now))
            with self._cache_lock:
                _prune_cache(self._stat_cache, self._pinned_stats)
                # A pinned entry stands until release (_pin_stat), and
                # read() stops at its size: BFFS lists a file it holds for
                # ACTION_FINDOUTPUT at its old size.
                if child_path not in self._pinned_stats:
                    self._stat_cache[child_path] = (now, stat_result)

        # Add virtual icon files if this directory has a custom icon
        if self._icon_handler and self._has_valid_icon(path):
            from . import platform
            icon_file, _ = platform.get_icon_file_names()
            if icon_file:
                entries.append(icon_file)

        # Add virtual volume icon file at root if Disk.info exists
        if self._icon_handler and path == "/" and self._has_valid_icon("/"):
            from . import platform
            _, volume_icon_file = platform.get_icon_file_names()
            if volume_icon_file:
                entries.append(volume_icon_file)

        # Filter out entries with pending (deferred) deletes
        entries = self._filter_pending_deletes(path, entries)

        # Cache the directory listing
        with self._cache_lock:
            _prune_cache(self._dir_cache)
            self._dir_cache[path] = (now, entries)
        return entries

    def open(self, path, flags):
        self._log_op("open", path, f"flags=0x{flags:x}")
        self._check_handler_alive()
        if not self.bridge._write_enabled and (flags & (os.O_WRONLY | os.O_RDWR)):
            raise FuseOSError(errno.EROFS)
        path = self._state_path(path)
        # Handle virtual Icon\r file - return a special handle
        if self._icons_enabled and self._is_icon_file(path):
            parent_dir = self._get_parent_dir(path)
            if self._has_valid_icon(parent_dir):
                with self._fh_lock:
                    handle = self._next_fh
                    self._next_fh += 1
                    self._fh_cache[handle] = {
                        "virtual_icon": True,
                        "parent_dir": parent_dir,
                        "pos": 0,
                        "lock": threading.Lock(),
                        "closed": False,
                    }
                return handle
            raise FuseOSError(errno.ENOENT)
        # Handle virtual .VolumeIcon.icns file - return a special handle
        if self._icons_enabled and self._is_volume_icon_file(path):
            if self._has_valid_icon("/"):
                icns_data = self._get_icon_for_path("/")
                if icns_data:
                    with self._fh_lock:
                        handle = self._next_fh
                        self._next_fh += 1
                        self._fh_cache[handle] = {
                            "virtual_volume_icon": True,
                            "icns_data": icns_data,
                            "pos": 0,
                            "lock": threading.Lock(),
                            "closed": False,
                        }
                    return handle
            raise FuseOSError(errno.ENOENT)
        opened, res2 = self.bridge.open_file_reply(path, flags)
        if opened is None:
            # The file exists, but the handler will not open it while
            # another handle holds it, such as one from create().
            if res2 == ERROR_OBJECT_IN_USE:
                raise FuseOSError(errno.EBUSY)
            info = self.bridge.stat_path(path)
            if info and info.get("dir_type", 0) >= 0:
                raise FuseOSError(errno.EISDIR)
            raise FuseOSError(errno.ENOENT)
        fh_addr, parent_lock = opened
        with self._fh_lock:
            handle = self._next_fh
            self._next_fh += 1
            self._fh_cache[handle] = {
                "fh_addr": fh_addr,
                "parent_lock": parent_lock,
                "pos": None,
                "lock": threading.Lock(),
                "closed": False,
                "dirty": False,  # Track if file was written to
                "path": path,
            }
        # open_file() takes an exclusive ACTION_FINDOUTPUT lock for O_TRUNC
        # without O_RDONLY, as create() does, and the file is empty now.
        if flags & os.O_TRUNC and (flags & getattr(os, "O_ACCMODE", 3)) != os.O_RDONLY:
            self._pin_stat(path, 0)
        return handle

    def read(self, path, size, offset, fh):
        self._check_handler_alive()
        with self._fh_lock:
            entry = self._fh_cache.get(fh)
        if entry is None:
            data = self.bridge.read_file(path, size, offset)
            if data is None:
                raise FuseOSError(errno.EIO)
            return data
        # Virtual Icon\r file returns empty content (icon is in ResourceFork)
        if entry.get("virtual_icon"):
            return b""
        # Virtual .VolumeIcon.icns file returns actual ICNS data
        if entry.get("virtual_volume_icon"):
            icns_data = entry.get("icns_data", b"")
            end = min(offset + size, len(icns_data))
            if offset >= len(icns_data):
                return b""
            return icns_data[offset:end]
        fh_addr = entry["fh_addr"]
        with entry["lock"]:
            if entry.get("closed"):
                raise FuseOSError(errno.EIO)
            # A file held on an exclusive lock holds only what its handle
            # wrote, which its pinned size counts (_pin_stat). BFFS still
            # serves the old data past that until release, and reading it
            # would move the point BFFS cuts the file at.
            pinned = _handle_path(entry, path)
            if pinned in self._pinned_stats:
                with self._cache_lock:
                    cached = self._stat_cache.get(pinned)
                if cached is not None:
                    size = max(0, min(size, cached[1]["st_size"] - offset))
                    if not size:
                        return b""
            if entry["pos"] is None or offset != entry["pos"]:
                data = self.bridge.read_handle_at(fh_addr, offset, size)
            else:
                data = self.bridge.read_handle(fh_addr, size)
            if data is None:
                # The handler's position is unknown after a failed read or
                # seek, so the next read must seek explicitly.
                entry["pos"] = None
                raise FuseOSError(errno.EIO)
            # A refused seek past end of file also reads as empty but leaves
            # the handler's position where it was, so cache only real data.
            entry["pos"] = offset + len(data) if data else None
            return data

    @staticmethod
    def _fit_file_size_limit(offset: int, size: int) -> int:
        """Return how many of size bytes at offset fit within _MAX_FILE_SIZE.

        Raises EFBIG when offset is past the limit, or at it with bytes to
        write. With size 0 this checks a file length, such as a truncate
        target.
        """
        room = _MAX_FILE_SIZE - offset
        if room < 0 or (room == 0 and size > 0):
            raise FuseOSError(errno.EFBIG)
        return min(size, room)

    def write(self, path, data, offset, fh):
        self._log_op("write", path, f"offset={offset} size={len(data)} fh={fh}")
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        with self._fh_lock:
            entry = self._fh_cache.get(fh)
        if entry is None:
            raise FuseOSError(errno.EIO)
        fh_addr = entry["fh_addr"]
        end = self._pinned_end(entry, path)
        with entry["lock"]:
            if entry.get("closed"):
                raise FuseOSError(errno.EIO)
            data = data[:self._fit_file_size_limit(offset, len(data))]
            if not data:
                # POSIX: a zero-length write changes nothing, even past end
                # of file.
                return 0
            if entry["pos"] is None or offset != entry["pos"]:
                written, res2 = self.bridge.write_handle_at(
                    fh_addr, offset, data, end=end)
            else:
                written, res2 = self.bridge.write_handle_reply(fh_addr, data)
            if written < 0:
                # The handler's position is unknown after a failed write or
                # seek, so the next write must seek explicitly.
                entry["pos"] = None
                # It may have left data past the pinned size (_pinned_end).
                if end is None or offset + len(data) > end:
                    entry["past_pin"] = True
                raise FuseOSError(_WRITE_ERRNOS.get(res2, errno.EIO))
            entry["pos"] = offset + written
            entry["dirty"] = True  # Mark as needing flush on close
        # Keep pinned stat cache in sync so a mid-write getattr (e.g. cp -v
        # checking progress) sees the growing size rather than the size=0
        # baseline written by create(). The pin is under the handle's path.
        path = _handle_path(entry, path)
        if path in self._pinned_stats:
            with self._cache_lock:
                cached = self._stat_cache.get(path)
                if cached is not None and offset + written > cached[1]["st_size"]:
                    self._pin_stat(path, offset + written)
        return written

    def _settle_handles(self, entry, far, seek: bool):
        """Set the cached positions after a truncate that reached its length.

        With seek, the truncating handle moves to the start of the file and
        caches that position. After a shrink, SmartFilesystem can fail a
        write at the new end of file until the handle moves, and a seek to
        where the handle already is does not count, so the write's own seek
        to the new end would not help. The seek also follows a grow, or a
        truncate that changed nothing, as truncate_handle does not say
        which it made; that costs one packet. The caller asks for no seek
        when the file is now empty, where the start is the new end, or when
        the handle is a temporary one that is released next.

        far holds the other open handles whose cached position is past
        0x7FFFFFFF. A truncate neither moves them nor clears that position,
        since no seek could return them there. But a pfs3aio shrink moves
        every handle on the file past the new end to it, and a read at the
        cached position would then skip its seek and return whatever is
        written there next. So each one's position is checked
        (handle_position): one from 0 to 0x7FFFFFFF means it moved, and it
        forgets its cached position. A read at its old offset then fails,
        as no seek can reach it, rather than return the wrong bytes. Any
        other answer means it is still past 0x7FFFFFFF, and it keeps it.

        A packet that gets no reply may still be with the handler, and more
        packets would reuse its memory sooner, so none follows it, as in
        truncate_handle. A seek to the start is not expected to be refused,
        so one that fails other than with ERROR_SEEK_ERROR, which is a
        reply, counts as such. The far handles not yet checked then forget
        their positions.
        """
        answered = True
        if seek:
            result = self.bridge.seek_handle(entry["fh_addr"], 0)[0]
            if result is SeekResult.OK:
                entry["pos"] = 0
            answered = result is not SeekResult.FAILED
        for other in far:
            if answered:
                answered, position = self.bridge.handle_position(
                    other["fh_addr"])
                if answered and position is None:
                    continue
            other["pos"] = None

    def truncate(self, path, length, fh=None):
        self._check_handler_alive()
        if self._debug:
            print(f"[FUSE][truncate] path={path} length={length} fh={fh}")
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        # ACTION_SET_FILE_SIZE carries the size in dp_Arg2, a signed LONG,
        # so the handler sees only the low 32 bits of the length: 4 GiB + 4
        # and -4 GiB + 4 both arrive as 4. Fail a negative length with
        # EINVAL, as truncate(2) does, and one above 0x7FFFFFFF with EFBIG,
        # as write() does.
        if length < 0:
            raise FuseOSError(errno.EINVAL)
        self._fit_file_size_limit(length, 0)
        if fh is None:
            fh = self.open(path, os.O_WRONLY)
            temp_handle = True
        else:
            temp_handle = False
        try:
            with self._fh_lock:
                entry = self._fh_cache.get(fh)
                # Every other open file handle on the volume, on any file:
                # handlers match names case-insensitively, so an entry's
                # path does not tell which handles share its file.
                # Directory and icon handles have no fh_addr. A handle past
                # 0x7FFFFFFF is not moved: no seek can return it there, so
                # its next read would fail.
                handles = [e for h, e in self._fh_cache.items()
                           if h != fh and "fh_addr" in e and not e.get("closed")]
                others = [e for e in handles if (e["pos"] or 0) <= _LONG_MAX]
                far = [e for e in handles if (e["pos"] or 0) > _LONG_MAX]
            if entry is None:
                raise FuseOSError(errno.EIO)
            fh_addr = entry["fh_addr"]
            end = self._pinned_end(entry, path)
            with entry["lock"]:
                if entry.get("closed"):
                    raise FuseOSError(errno.EIO)
                # Only a shrink of an unpinned file can move the far
                # handles: a pinned file is held on an exclusive lock, so
                # none is on it, and a grow moves no handle. The end is
                # measured now, since no packet may follow a lost reply; an
                # end that cannot be measured counts as a shrink.
                far_may_move = False
                if far and end is None:
                    measured = self.bridge._handle_end(fh_addr)
                    far_may_move = measured is None or length < measured
                # A shrink that stops short or is refused as in use moves
                # the other handles and is sent again. Every read and write
                # carries its offset, so the handler's position for another
                # handle is only a cache.
                size, res2 = self.bridge.truncate_handle(
                    fh_addr, length, [e["fh_addr"] for e in others], end=end,
                    past_end=bool(entry.get("past_pin")))
                # truncate_handle moves the handle, and handlers differ in
                # where a size change leaves it, so the next read or write
                # seeks. The retry moves the others, and a pfs3aio shrink
                # moves any past the new end to it, so they seek too. A
                # writable mount serves one request at a time, so no other
                # callback is using them.
                entry["pos"] = None
                for other in others:
                    other["pos"] = None
                # BFFS refuses every ACTION_SET_FILE_SIZE with
                # ERROR_OBJECT_NOT_FOUND, but it can still empty the file.
                if size < 0 and not length and res2 == ERROR_OBJECT_NOT_FOUND:
                    size, res2 = self._reopen_for_output(
                        entry, _handle_path(entry, path))
                    if res2 == ERROR_OBJECT_IN_USE:
                        raise FuseOSError(errno.EBUSY)
                if size < 0 and not res2:
                    # A size change with no reply in either wait gives
                    # (-1, 0) (set_handle_size_reply), and the handler may
                    # still carry it out, so no packet follows it here. A
                    # pfs3aio shrink would move the far handles, so they
                    # forget their positions, as a moved one does in
                    # _settle_handles. A shrink below the pin has the pin
                    # measured again (_measure_stale_pin). A change to a
                    # length at or past the pin is not marked: from a pin
                    # that agrees with the handler, a lost grow cannot
                    # leave the end below it.
                    if far_may_move:
                        for other in far:
                            other["pos"] = None
                    if end is not None and length < end:
                        entry["stale_pin"] = True
                if size < 0 and end is not None and length > end:
                    # A failed grow's zeros may stay past the pin
                    # (_pinned_end).
                    entry["past_pin"] = True
                if size < 0:
                    raise FuseOSError(_WRITE_ERRNOS.get(res2, errno.EIO))
                entry["dirty"] = True  # Truncate modifies the file
                if size == length:
                    # The handler's end is now length, as the pin is.
                    entry.pop("past_pin", None)
                    self._settle_handles(entry, far,
                                         seek=length > 0 and not temp_handle)
            # A pinned size is all getattr reports until release, and
            # write() only ever raises it. Left stale, it would place the
            # next O_APPEND write past the new end of file. The pin is under
            # the handle's path. A shrink that stopped short left the file at
            # size. A smaller size with no error code, which no handler is
            # known to reply, is not a size the file is known to have, so
            # the pin stays.
            path = _handle_path(entry, path)
            if path in self._pinned_stats and size >= length:
                with self._cache_lock:
                    if path in self._stat_cache:
                        self._pin_stat(path, size)
            # Still short of length after any retry, or not changed at all.
            # EIO is also what SmartFilesystem's refusal of such a shrink
            # gives.
            if size != length:
                raise FuseOSError(errno.EIO)
        finally:
            if temp_handle:
                self.release(path, fh)
        return 0

    def _reopen_for_output(self, entry, path) -> Tuple[int, int]:
        """Empty the file for a truncate to 0 that BFFS refused.

        The entry's handle is replaced with one opened with
        ACTION_FINDOUTPUT. BFFS cuts a file opened that way to the furthest
        byte the handle reached when it ends, once it has written, and a
        zero-length write (write_empty_reply()) does that at once, so that
        BFFS's refusal fails the truncate. BFFS grants the open only while
        no other handle holds the file, so the entry's own handle ends
        first, and the new handle holds the file alone until release().
        Until then BFFS reports the old size, so the size is pinned at 0, as
        open() does for O_TRUNC.

        Returns (0, 0), or (-1, dp_Res2) when the open or the write fails,
        and the file keeps its data. After a refusal of either, the file is
        opened again with ACTION_FINDUPDATE, so the entry keeps a handle; a
        refused write's handle ends first, which cuts nothing. After a write
        that got no reply, the entry keeps the new handle. After an open
        that got no reply, which the handler may still hold, or a refused
        open again, the entry is left closed with no handle: its reads,
        writes and truncates fail with EIO, and release() ends nothing. So
        is it after an open or a write that raises, as one does when the
        emulated RAM runs out, and the error goes on to the caller: the old
        FileHandle is back in the pool, and the next open can take it. A
        write that raises ends the new handle first, unwritten.
        """
        bridge = self.bridge
        bridge.close_file(entry["fh_addr"])
        opened, res2, emptied = None, 0, False
        try:
            opened, res2 = bridge.open_file_reply(path, os.O_RDWR | os.O_TRUNC)
            if opened is not None:
                try:
                    written, res2 = bridge.write_empty_reply(opened[0])
                except Exception:
                    # Kept, the new handle would cut the file at release
                    # after any later write. Ended unwritten, it cuts
                    # nothing.
                    (new_fh, lock), opened = opened, None
                    bridge.close_file(new_fh)
                    if lock:
                        bridge.free_lock(lock)
                    raise
                emptied = written == 0
                if not emptied and res2:
                    bridge.close_file(opened[0])
                    # The FileHandle is back in the pool, whatever the
                    # free_lock below does.
                    lock, opened = opened[1], None
                    if lock:
                        bridge.free_lock(lock)
            if opened is None and res2:
                opened = bridge.open_file(path, os.O_RDWR)
        finally:
            # The entry is set first: the old FileHandle is already back
            # in the pool, even when the free_lock below raises.
            old_lock = entry.get("parent_lock")
            if opened is None:
                entry.update(fh_addr=0, parent_lock=0, closed=True)
            else:
                entry["fh_addr"], entry["parent_lock"] = opened
            if old_lock:
                bridge.free_lock(old_lock)
        if not emptied:
            return -1, res2
        self._pin_stat(path, 0)
        return 0, 0

    def create(self, path, mode, fi=None):
        self._log_op("create", path, f"mode=0o{mode:o}")
        try:
            self._check_handler_alive()
            if not self.bridge._write_enabled:
                raise FuseOSError(errno.EROFS)
            if self._is_platform_special(path):
                raise FuseOSError(errno.ENOENT)
            path = self._state_path(path)
            opened = self.bridge.open_file(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            if opened is None:
                self._log_op("create", path, "FAILED: open_file returned None")
                raise FuseOSError(errno.EIO)
            fh_addr, parent_lock = opened
            self._forget_pending_delete(path)
            with self._fh_lock:
                handle = self._next_fh
                self._next_fh += 1
                self._fh_cache[handle] = {
                    "fh_addr": fh_addr,
                    "parent_lock": parent_lock,
                    "pos": None,
                    "lock": threading.Lock(),
                    "closed": False,
                    "dirty": True,  # New files are always dirty
                    "path": path,
                }
            # Prime stat/dir cache so immediate getattr after create doesn't fail.
            # Pin the entry: in write mode _cache_ttl is 0, so without pinning
            # libfuse2's post-create getattr probe would fall through to PFS3
            # and get ERROR_OBJECT_IN_USE on the still-open handle.
            self._pin_stat(path, 0)
            with self._cache_lock:
                parent_path, _ = self._split_path(path)
                self._dir_cache.pop(parent_path, None)
            self._log_op("create", path, f"SUCCESS handle={handle} fh_addr=0x{fh_addr:x}")
            return handle
        except FuseOSError:
            raise
        except Exception as e:
            self._log_op("create", path, f"EXCEPTION: {type(e).__name__}: {e}")
            raise FuseOSError(errno.EIO) from e

    def unlink(self, path):
        self._log_op("unlink", path, "")
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        path = self._state_path(path)
        if self._winfsp:
            # WinFsp passes the name the deleting file handle was opened
            # under, also after a rename through that handle (#290), so the
            # file handles opened under path tell which file to delete. A
            # handle a failed truncate() reopen left closed counts, as in
            # release(). Files opened under path that are now at two paths,
            # as when a DeleteFile() of a new file there comes while a file
            # renamed from there is open, leave path as given, as before.
            with self._fh_lock:
                targets = {
                    v.get("path") for v in self._fh_cache.values()
                    if "fh_addr" in v and self._folded(
                        v.get("opened_as", v.get("path")) or "") == self._folded(path)
                }
            if len(targets) == 1 and path not in targets:
                target = targets.pop()
                self._log_op("unlink", path, f"renamed open file: {target}")
                path = target
        dir_path, name = self._split_path(path)
        if not name:
            raise FuseOSError(errno.EINVAL)
        lock_bptr, _, locks = self.bridge.locate_path(dir_path)
        try:
            # For root directory, get a proper lock
            if dir_path == "/" and lock_bptr == 0:
                lock_bptr, _ = self.bridge.locate(0, "")
                if lock_bptr:
                    locks.append(lock_bptr)
            if lock_bptr == 0:
                self._log_op("unlink", path, f"parent dir not found: {dir_path}")
                raise FuseOSError(errno.ENOENT)
            res1, res2 = self.bridge.delete_object(lock_bptr, name)
            self._log_op("unlink", path, f"delete_object res1={res1} res2={res2}")
            if res1 == 0:
                if res2 == 202:
                    # ERROR_OBJECT_IN_USE: file has an active handle (WinFSP calls
                    # unlink before release). Defer deletion to release().
                    with self._fh_lock:
                        has_handle = any(v.get("path") == path for v in self._fh_cache.values())
                        if has_handle:
                            self._pending_deletes.add(path)
                    if has_handle:
                        with self._cache_lock:
                            # A file held on an exclusive lock keeps its pinned
                            # entry: getattr() reports it through the handle,
                            # and read() stops at its size, until release()
                            # (#263).
                            if path not in self._pinned_stats:
                                self._stat_cache.pop(path, None)
                            self._dir_cache.pop(dir_path, None)
                        return 0
                raise FuseOSError(_DELETE_ERRNOS.get(res2, errno.EIO))
            with self._cache_lock:
                self._stat_cache.pop(path, None)
                self._dir_cache.pop(dir_path, None)
            self._pinned_stats.discard(path)
            return 0
        finally:
            self._free_locks("unlink", path, locks)

    def rmdir(self, path):
        """Remove a directory, deferring if children have pending deletes."""
        self._log_op("rmdir", path, "")
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        path = self._state_path(path)
        dir_path, name = self._split_path(path)
        if not name:
            raise FuseOSError(errno.EINVAL)
        res1, res2 = self.bridge.delete_dir_atomic(path)
        files_open = res1 == 0 and res2 == 202 and self._any_file_open()
        if res1 == 0 and res2 == 202 and not files_open:
            # OBJECT_IN_USE with no file open: pfs3aio and SmartFilesystem
            # keep in-use state after a directory change, which an inhibit
            # cycle clears. With a file open the cycle breaks
            # SmartFilesystem's handles and can stop pfs3aio (#129).
            self.bridge.inhibit_cycle()
            res1, res2 = self.bridge.delete_dir_atomic(path)
        self._log_op("rmdir", path, f"delete_dir_atomic res1={res1} res2={res2}")
        if res1 == 0:
            if res2 not in (202, 216):
                raise FuseOSError(_DELETE_ERRNOS.get(res2, errno.EIO))
            # OBJECT_IN_USE: pfs3aio refuses while a file inside is open,
            # even an unlinked one, and answers 202 before 216.
            # DIRECTORY_NOT_EMPTY: the directory holds entries, an unlinked
            # open file among them. Defer only where a retry will come:
            # release() of the last pending child, or, for 202, releasedir()
            # of a FUSE directory handle open on this path, which can clear
            # the refusal only with the cycle that a file open now rules
            # out. And defer only while the directory holds nothing but
            # pending deletes; anything else stays, so it is not empty (#168).
            with self._fh_lock:
                pending_children = self._pending_children(path)
                dir_open = res2 == 202 and not files_open and any(
                    v.get("dir_handle") and v.get("path") == path
                    for v in self._fh_cache.values()
                )
            listed = []
            if pending_children or res2 == 202:
                listed = self.bridge.list_dir_path(path)
            # Each pending child is still listed until its release, so any
            # further entry is live. Names are not compared: the handler
            # stores a host name in its own case, encoding and length.
            live = len(listed) > len(pending_children)
            defer = not live and bool(pending_children or dir_open)
            if defer:
                with self._fh_lock:
                    if pending_children:
                        self._pending_dir_deletes[path] = pending_children
                    self._pending_deletes.add(path)
            else:
                err = errno.ENOTEMPTY if listed or res2 == 216 else errno.EBUSY
                self._log_op("rmdir", path, f"nothing would retry: {errno.errorcode[err]}")
                raise FuseOSError(err)
            with self._cache_lock:
                self._stat_cache.pop(path, None)
                self._dir_cache.pop(dir_path, None)
                self._dir_cache.pop(path, None)
            self._log_op("rmdir", path, f"deferred (res2={res2})")
            return 0
        # Immediate success
        self._forget_pending_delete(path)
        with self._cache_lock:
            self._stat_cache.pop(path, None)
            self._dir_cache.pop(dir_path, None)
            self._dir_cache.pop(path, None)
        return 0

    def _pending_children(self, path: str) -> set:
        """Direct children of path with a pending delete that a retry will
        send. Caller holds _fh_lock.

        A file child counts only while a FUSE handle with an AmigaDOS
        FileHandle is open on it, whose release() sends the delete (#222).
        A directory child counts while it waits on children of its own or
        on a directory handle, as _drop_failed_dir_delete keeps it.

        Direct children only: _resolve_pending_dir_deletes strikes a deleted
        path off its parent's set and no other.
        """
        held = {v.get("path") for v in self._fh_cache.values()
                if "fh_addr" in v or v.get("dir_handle")}
        children = {p for p in self._pending_deletes
                    if p.rsplit("/", 1)[0] == path and p in held}
        children.update(d for d in self._pending_dir_deletes if d.rsplit("/", 1)[0] == path)
        return children

    def _forget_pending_delete(self, path: str):
        """Stop hiding path once its delete is done or a new object holds the name."""
        with self._fh_lock:
            self._pending_deletes.discard(path)
            self._pending_dir_deletes.pop(path, None)

    def _any_file_open(self) -> bool:
        """True while a FUSE handle holds an AmigaDOS FileHandle and a lock."""
        with self._fh_lock:
            return any("fh_addr" in v for v in self._fh_cache.values())

    def _drop_failed_dir_delete(self, path: str):
        """Keep a directory whose deferred delete failed hidden only while a
        retry is coming. Caller holds _fh_lock.

        A retry comes from the release() of a pending child (path is in
        _pending_dir_deletes) or the releasedir() of a FUSE directory handle
        open on path. Otherwise the directory shows again, and so does each
        deferred parent waiting on it, which can now never be emptied.
        """
        while True:
            if path in self._pending_dir_deletes or any(
                v.get("dir_handle") and v.get("path") == path
                for v in self._fh_cache.values()
            ):
                self._pending_deletes.add(path)
                return
            self._pending_deletes.discard(path)
            parent = path.rsplit("/", 1)[0] or "/"
            if path not in self._pending_dir_deletes.get(parent, ()):
                return
            del self._pending_dir_deletes[parent]
            path = parent

    def _drop_refused_delete(self, path: str):
        """Show a file whose deferred delete the handler refused at its last
        release(), with no handle left to send it again (#222). Caller holds
        _fh_lock.

        A deferred rmdir() of its directory now waits only on its other
        pending children. With none left, nothing can empty the directory,
        which shows again as _drop_failed_dir_delete shows it.
        """
        self._pending_deletes.discard(path)
        parent = path.rsplit("/", 1)[0] or "/"
        waiting = self._pending_dir_deletes.get(parent, set())
        if path not in waiting:
            return
        waiting.discard(path)
        if not waiting:
            del self._pending_dir_deletes[parent]
            self._drop_failed_dir_delete(parent)

    def mkdir(self, path, mode):
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        path = self._state_path(path)
        dir_path, name = self._split_path(path)
        if not name:
            raise FuseOSError(errno.EINVAL)
        parent_lock, _, locks = self.bridge.locate_path(dir_path)
        try:
            if parent_lock == 0 and dir_path != "/":
                raise FuseOSError(errno.ENOENT)
            new_lock, _ = self.bridge.create_dir(parent_lock, name)
            if new_lock == 0:
                raise FuseOSError(errno.EIO)
            self.bridge.free_lock(new_lock)
        finally:
            for l in reversed(locks):
                self.bridge.free_lock(l)
        self._forget_pending_delete(path)
        with self._cache_lock:
            self._dir_cache.pop(dir_path, None)
        return 0

    def rename(self, old, new):
        self._check_handler_alive()
        if self._debug:
            print(f"[FUSE][rename] old={old} new={new}")
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        _, src_name = self._split_path(old)
        dst_dir, dst_name = self._split_path(new)
        if not src_name or not dst_name:
            raise FuseOSError(errno.EINVAL)
        # Two spellings of one name reach here equal: to rename(2) that is
        # one file, which stays as it is, and a handler may call it taken.
        if old == new:
            self.getattr(old)
            return 0
        old = self._state_path(old)
        # The new name keeps its own spelling: a rename can change the case.
        new = self._state_path(dst_dir).rstrip("/") + "/" + dst_name
        res1, res2 = self._rename_object(old, new)
        # AmigaDOS renames onto no existing name, while rename(2) replaces
        # one, and editors save by renaming a new file over the old one.
        # Handlers match names case-insensitively, so two paths that differ
        # only in case can name one object, which a replace would delete.
        # lower() folds at least the letters any handler folds.
        if res1 == 0 and res2 == ERROR_OBJECT_EXISTS and old.lower() != new.lower():
            self._replace_existing(old, new)
            return 0
        if res1 == 0:
            raise FuseOSError(_RENAME_ERRNOS.get(res2, errno.EIO))
        return 0

    def _replace_existing(self, old: str, new: str):
        """Rename old onto new, which exists, as rename(2) does.

        A file replaces a file, and a directory an empty directory. new is
        renamed to a temporary name in its directory, old is renamed to
        new, and the temporary name is deleted, so that one of the two
        objects is under new at every step. A step that fails puts back
        what the steps before it moved, and rename() fails.

        The replaced object, at the temporary name, is deleted as unlink()
        deletes a file: one still open is deleted at its release(), and
        hidden until then.
        """
        # The mount hides these names, so the host can't mean to replace one.
        if self._is_platform_special(new):
            raise FuseOSError(errno.EEXIST)
        # new as the state spells the object there, which step 1 moves.
        replaced = self._state_path(new)
        src = self._examine(old)
        dst = self._examine(replaced)
        if src is None or dst is None:
            # Neither examined nor pinned: gone, or held on an exclusive lock.
            raise FuseOSError(errno.ENOENT if src is None else errno.EBUSY)
        dst_dir, _ = self._split_path(new)
        src_is_dir = src["dir_type"] >= 0
        dst_is_dir = dst["dir_type"] >= 0
        if dst_is_dir and not src_is_dir:
            raise FuseOSError(errno.EISDIR)
        if src_is_dir and not dst_is_dir:
            raise FuseOSError(errno.ENOTDIR)
        if dst_is_dir and self.bridge.list_dir_path(replaced):
            raise FuseOSError(errno.ENOTEMPTY)
        # Put back under the name it had, which may differ from new in case,
        # or be new cut to the volume's name limit.
        on_disk = dst["name"]
        orig = replaced if on_disk is None else dst_dir.rstrip("/") + "/" + on_disk
        for _ in range(8):
            temp = dst_dir.rstrip("/") + f"/.amifuse-{os.urandom(4).hex()}"
            res1, res2 = self._rename_object(replaced, temp)
            if res1 or res2 != ERROR_OBJECT_EXISTS:
                break
        if res1 == 0:
            raise FuseOSError(_RENAME_ERRNOS.get(res2, errno.EIO))
        try:
            res1, res2 = self._rename_object(old, new)
            if res1 == 0:
                raise FuseOSError(_RENAME_ERRNOS.get(res2, errno.EIO))
        except Exception:
            self._rename_back(temp, orig)
            raise
        try:
            self._delete_replaced(temp)
        except Exception:
            self._rename_back(new, old)
            self._rename_back(temp, orig)
            raise

    def _examine(self, path: str) -> Optional[Dict]:
        """Examine path for _replace_existing(); None when it can't be.

        A file held on an exclusive lock can't be examined, but has a
        pinned entry (_pin_stat): a file whose name on disk is not known.
        """
        info = self.bridge.stat_path(path)
        if info:
            return info
        with self._cache_lock:
            if path in self._pinned_stats:
                return {"dir_type": -3, "name": None}  # ST_FILE
        return None

    def _delete_replaced(self, temp: str):
        """Delete the replaced object, now at temp, for _replace_existing().

        A file still open is left to its release(), and hidden until then,
        as unlink() does. A delete that gets no reply may still happen, so
        the replace stands. Raises FuseOSError when the handler refuses.
        """
        # delete_dir_atomic() sends ACTION_DELETE_OBJECT, for a file too.
        res1, res2 = self.bridge.delete_dir_atomic(temp)
        deferred = False
        if res1 == 0 and res2 == ERROR_OBJECT_IN_USE:
            with self._fh_lock:
                deferred = any(v.get("path") == temp and "fh_addr" in v
                               for v in self._fh_cache.values())
                if deferred:
                    self._pending_deletes.add(temp)
        if res1 == 0 and not deferred:
            if res2 != -1:  # -1: no reply
                raise FuseOSError(_RENAME_ERRNOS.get(res2, errno.EIO))
            print(f"[amifuse] rename: no reply to the delete of the replaced "
                  f"object, which may stay at {temp}", file=sys.stderr, flush=True)
        if res1:
            # A deferred delete of the target followed it to temp in step 1.
            self._forget_pending_delete(temp)
        with self._cache_lock:
            # A file still open keeps its pinned entry, which is all
            # getattr() has through its handle while it is held on an
            # exclusive lock.
            if not deferred:
                self._pinned_stats.discard(temp)
                self._stat_cache.pop(temp, None)
            self._dir_cache.pop(self._split_path(temp)[0], None)

    def _rename_back(self, old: str, new: str):
        """Undo one step of _replace_existing()."""
        res1, res2 = self._rename_object(old, new)
        if res1 == 0:
            # The object stays under old, in sight, and nothing else says so.
            print(f"[amifuse] rename: could not rename {old} back to {new} "
                  f"(DOS error {res2})", file=sys.stderr, flush=True)

    def _rename_object(self, old: str, new: str) -> Tuple[int, int]:
        """Send one ACTION_RENAME_OBJECT; on success, move what follows old.

        Returns the reply, or (0, ERROR_DIR_NOT_FOUND) when a parent
        directory is not found. Open handles, pinned entries and deferred
        deletes move from old to new, also below a renamed directory.
        """
        src_dir, src_name = self._split_path(old)
        dst_dir, dst_name = self._split_path(new)
        src_lock, _, src_locks = self.bridge.locate_path(src_dir)
        dst_lock, _, dst_locks = self.bridge.locate_path(dst_dir)
        try:
            if (src_lock == 0 and src_dir != "/") or (dst_lock == 0 and dst_dir != "/"):
                return 0, ERROR_DIR_NOT_FOUND
            res1, res2 = self.bridge.rename_object(src_lock, src_name, dst_lock, dst_name)
            if res1 == 0:
                return res1, res2
        finally:
            # Free the dir locks on every exit; a leaked lock lingers in
            # handler memory and can block later deletes with OBJECT_IN_USE.
            for l in reversed(src_locks):
                self.bridge.free_lock(l)
            for l in reversed(dst_locks):
                self.bridge.free_lock(l)
        # Another spelling of what the state already has: nothing moves.
        if old == new:
            return res1, res2
        # Handles of the renamed file itself, now in another directory.
        moved_out = []
        with self._cache_lock:
            self._stat_cache.pop(new, None)
            self._pinned_stats.discard(new)
            # Handles follow the file to its new name, also below a renamed
            # directory, and so does the pinned entry of a file held on an
            # exclusive lock: getattr() has nothing else for it.
            for pinned in list(self._pinned_stats):
                moved = _renamed_path(pinned, old, new)
                if moved is not None:
                    self._pinned_stats.discard(pinned)
                    cached = self._stat_cache.pop(pinned, None)
                    if cached is not None:
                        self._stat_cache[moved] = cached
                        self._pinned_stats.add(moved)
            self._stat_cache.pop(old, None)
            self._dir_cache.pop(src_dir, None)
            self._dir_cache.pop(dst_dir, None)
            with self._fh_lock:
                for entry in self._fh_cache.values():
                    moved = _renamed_path(entry.get("path", ""), old, new)
                    if moved is not None:
                        # Directory and icon handles have no fh_addr.
                        if moved == new and src_dir != dst_dir and "fh_addr" in entry:
                            moved_out.append(entry)
                        if self._winfsp:  # see utimens()
                            entry.setdefault("opened_as", entry["path"])
                        entry["path"] = moved
                # Deferred deletes follow too. release() and releasedir()
                # look for them under the handle's path, and one left under
                # the old path would hide a file created there later, and
                # delete it at its release().
                self._pending_deletes = {
                    _renamed_path(p, old, new) or p for p in self._pending_deletes
                }
                self._pending_dir_deletes = {
                    _renamed_path(d, old, new) or d: {
                        _renamed_path(c, old, new) or c for c in children
                    }
                    for d, children in self._pending_dir_deletes.items()
                }
                self._held_dates = {
                    _renamed_path(p, old, new) or p: when
                    for p, when in self._held_dates.items()
                }
        for entry in moved_out:
            self._move_parent_lock(entry, dst_dir)
        return res1, res2

    def _move_parent_lock(self, entry: Dict, dir_path: str):
        """Move an open file's lock on its directory to dir_path.

        open() keeps a lock on the directory a file was opened in until
        release(). Left on the directory the file was renamed out of, it
        keeps the handler from deleting that directory once it is empty.
        The entry keeps the old lock if dir_path can't be locked.
        """
        with entry["lock"]:
            # release() marks the entry closed before it frees the lock.
            if entry.get("closed") or not entry.get("parent_lock"):
                return
            lock, _, _ = self.bridge.locate_path(dir_path)
            if dir_path == "/" and lock == 0:
                lock, _ = self.bridge.locate(0, "")
            if lock == 0:
                self._log_op("rename", dir_path, "could not lock the new directory")
                return
            self.bridge.free_lock(entry["parent_lock"])
            entry["parent_lock"] = lock

    def flush(self, path, fh):
        self._log_op("flush", path, f"fh={fh}")
        return 0

    def fsync(self, path, fdatasync, fh):
        self._log_op("fsync", path, f"fh={fh} fdatasync={fdatasync}")
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            return 0
        # Handlers keep data and metadata in their buffers. ACTION_FLUSH has
        # no per-file form, so write out the whole volume.
        try:
            self.bridge.flush_volume()
        except OSError as exc:
            raise FuseOSError(errno.EIO) from exc
        return 0

    def fsyncdir(self, path, datasync, fh):
        self._log_op("fsyncdir", path, f"fh={fh} datasync={datasync}")
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            return 0
        # A create, rename, delete or mkdir in the directory can still be in
        # the handler's buffers, and ACTION_FLUSH writes out the whole volume.
        try:
            self.bridge.flush_volume()
        except OSError as exc:
            raise FuseOSError(errno.EIO) from exc
        return 0

    def chmod(self, path, mode):
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        # Amiga filesystems doesn't support Unix-style chmod; accept and ignore.
        return 0

    def chown(self, path, uid, gid):
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        # Ignore ownership changes; keep host uid/gid.
        return 0

    def utimens(self, path, times=None):
        """Set the file's date to the modification time.

        A handler stamps a file it closes after a write, and Windows dates
        a copy on its open handle, so the date of an open file is held, the
        last one asked for, and set at its last release(). A closed file is
        dated at once. A date before 1978 is set to 1978-01-01.

        A request for the date a closed file has sends nothing. AmigaDOS
        keeps no access time, and WinFsp (_winfsp) passes an access-time-
        only change with the date getattr() reports, so there a request for
        that date holds nothing for an open file either: the handler's stamp
        at close stands. libfuse passes both times as asked for, so the last
        date asked for an open file is held even when it is that date.

        WinFsp also goes on passing the name a file was opened under after
        rename() moved it. A request for the name an open file was renamed
        from is held for that file, and one that two objects could answer
        is dropped.
        """
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        self._check_handler_alive()
        if path == "/" or (self._icon_handler and (
                self._is_icon_file(path) or self._is_volume_icon_file(path))):
            return 0
        path = self._state_path(path)
        mtime = time.time() if times is None else times[1]
        with self._fh_lock:
            target = self._renamed_open_file(path)
        if target is None:
            return 0
        try:
            current = self.getattr(target)["st_mtime"]
        except FuseOSError as e:
            if e.args[0] != errno.ENOENT:
                raise
            current = None
        unchanged = current is not None and abs(mtime - current) < 0.01  # half a tick
        with self._fh_lock:
            # Write mounts run single-threaded: no handle came or went since.
            if self._has_open_file(target):
                if not (unchanged and self._winfsp):
                    self._held_dates[target] = mtime
                return 0
        if current is None or unchanged:
            return 0
        self._set_date(target, mtime)
        return 0

    def _has_open_file(self, path: str) -> bool:
        """Whether a file handle is open on path. Hold _fh_lock."""
        return any(
            "fh_addr" in v and v.get("path") == path and not v.get("closed")
            for v in self._fh_cache.values()
        )

    def _renamed_open_file(self, path: str) -> Optional[str]:
        """The path an object opened as path was renamed to, else path.

        None when that name could be two objects: another open handle, of
        a file or a directory, has it, or two renamed ones were opened
        under it. Hold _fh_lock.
        """
        moved = {
            v.get("path") for v in self._fh_cache.values()
            if not v.get("closed") and v.get("path") != path
            and self._folded(v.get("opened_as") or "") == self._folded(path)
        }
        if not moved:
            return path
        if len(moved) > 1 or any(
                v.get("path") == path and not v.get("closed")
                for v in self._fh_cache.values()):
            return None
        return moved.pop()

    def _set_date(self, path: str, when: float):
        dir_path, name = self._split_path(path)
        lock, _, locks = self.bridge.locate_path(dir_path)
        try:
            if lock == 0 and dir_path != "/":
                raise FuseOSError(errno.ENOENT)
            res1, res2 = self.bridge.set_date(lock, name, when)
        finally:
            for l in reversed(locks):
                self.bridge.free_lock(l)
        with self._cache_lock:
            self._stat_cache.pop(path, None)
        # A handler without the packet keeps its own dates, as before.
        if res1 == 0 and res2 != ERROR_ACTION_NOT_KNOWN:
            raise FuseOSError(_SET_DATE_ERRNOS.get(res2, errno.EIO))

    def _set_held_date(self, path: str):
        """Set the date utimens() held for path, once no handle is open on it.

        A refusal has nowhere to go but stderr: utimens() returned 0 long
        ago, the date can only be set after the handle ends, since a handler
        stamps a file it closes, and close() does not wait for release().
        The program that asked never hears of it. On macOS and Linux a
        daemon mount's stderr is /dev/null (_report_ready), so there the
        line takes --interactive to see. On Windows it reaches the console,
        or mount-<letter>.log for a mount the launcher starts.
        """
        with self._fh_lock:
            if path not in self._held_dates or self._has_open_file(path):
                return
            if path in self._pending_deletes:
                # release() sends the delete next, then sets or drops it.
                return
            when = self._held_dates.pop(path)
        try:
            self._set_date(path, when)
        except FuseOSError as e:
            print(f"[amifuse] could not set the date of {path} (errno {e.args[0]})",
                  file=sys.stderr, flush=True)
            self._log_op("release", path, f"held date not set: errno {e.args[0]}")

    def access(self, path, mode):
        self._log_op("access", path, f"mode={mode}")
        if self._is_platform_special(path):
            raise FuseOSError(errno.ENOENT)
        if not self.bridge._write_enabled and (mode & os.W_OK):
            raise FuseOSError(errno.EROFS)
        return 0

    def listxattr(self, path):
        if self._debug:
            print(f"[FUSE][listxattr] path={path} icons_enabled={self._icons_enabled}", flush=True)
        if not self._icons_enabled or not self._icon_handler:
            return []

        # For Icon\r files, check if parent has a valid icon
        # For other paths, check if the path itself has a valid icon
        if self._is_icon_file(path):
            parent_dir = self._get_parent_dir(path)
            has_icon = self._has_valid_icon(parent_dir)
        else:
            has_icon = self._has_valid_icon(path)

        result = self._icon_handler.get_listxattr_for_path(path, has_icon)
        if self._debug and result:
            print(f"[FUSE][listxattr] {path} -> {result}", flush=True)
        return result

    def getxattr(self, path, name, position=0):
        if self._debug:
            print(f"[FUSE][getxattr] path={path} name={name} position={position}", flush=True)
        enoattr = getattr(errno, "ENOATTR", errno.ENODATA)

        if not self._icons_enabled or not self._icon_handler:
            raise FuseOSError(enoattr)

        # For Icon\r files, get icon data from parent directory
        # For other paths, get icon data from the path itself
        if self._is_icon_file(path):
            icon_source_path = self._get_parent_dir(path)
        else:
            icon_source_path = path

        # Get icon data and has_icon status
        icns_data = self._get_icon_for_path(icon_source_path)
        has_icon = self._has_valid_icon(icon_source_path)

        # Use DarwinIconHandler to get the xattr value
        result = self._icon_handler.get_xattr_value(path, name, icns_data, has_icon, position)

        if result is not None:
            if self._debug:
                print(f"[FUSE][getxattr] {name} for {path}: {len(result)} bytes", flush=True)
            return result

        raise FuseOSError(enoattr)

    def _find_info_file(self, path: str) -> Optional[str]:
        """Find the .info file for a given path using case-insensitive matching.

        For root ("/"), looks for Disk.info.
        For other paths, looks for path.info.

        Returns:
            The actual path to the .info file, or None if not found.
        """
        if path == "/":
            # Root directory uses Disk.info for its icon
            target_name = "disk.info"
            dir_path = "/"
        else:
            # Normal files/directories: append .info suffix
            # Get parent directory and target name
            if "/" in path[1:]:  # Has subdirectories
                dir_path = path.rsplit("/", 1)[0] or "/"
                base_name = path.rsplit("/", 1)[1]
            else:
                dir_path = "/"
                base_name = path[1:]  # Remove leading /
            target_name = (base_name + ".info").lower()

        # List directory and find matching .info file case-insensitively
        try:
            entries = self.bridge.list_dir_path(dir_path)
            for entry in entries:
                name = entry.get("name", "")
                if name.lower() == target_name:
                    if dir_path == "/":
                        return "/" + name
                    else:
                        return dir_path + "/" + name
        except Exception:
            pass

        return None

    def _has_valid_icon(self, path: str) -> bool:
        """Check if the file at path has a valid .info icon file that can be converted.

        This actually tries to generate the ICNS to ensure consistency with _get_icon_for_path.
        """
        if not self._icons_enabled:
            return False

        # Check existence cache first
        cached = self._icon_existence_cache.get(path)
        if cached is not None:
            return cached

        # Actually try to get the icon - this ensures we can parse it
        icns_data = self._get_icon_for_path(path)
        if icns_data:
            self._icon_existence_cache.put(path, True)
            return True

        self._icon_existence_cache.put(path, False)
        return False

    def _get_icon_for_path(self, path: str) -> Optional[bytes]:
        """Get ICNS data for a file by parsing its .info file."""
        if not self._icons_enabled:
            return None

        # Check ICNS cache first (keyed by base path)
        cached = self._icon_cache.get(path)
        if cached is not None:
            return cached

        # Find the .info file using case-insensitive matching
        info_path = self._find_info_file(path)
        if not info_path:
            return None

        info_stat = self._get_cached_stat(info_path)
        if info_stat is None:
            info_stat = self.bridge.stat_path(info_path)
        if not info_stat:
            return None

        # Read the entire .info file
        # info_stat could be from bridge.stat_path (has 'size') or from _stat_cache (has 'st_size')
        if isinstance(info_stat, dict):
            file_size = info_stat.get("size") or info_stat.get("st_size", 0)
        else:
            file_size = 0
        if file_size <= 0 or file_size > 1024 * 1024:  # Max 1MB
            return None

        data = self.bridge.read_file(info_path, file_size, 0)
        if not data:
            if self._debug:
                print(f"[FUSE] Failed to read icon data from {info_path}", flush=True)
            return None

        if self._debug:
            print(f"[FUSE] Parsing icon: {info_path} ({len(data)} bytes)", flush=True)

        # Parse the icon
        icon_info = self._icon_parser.parse(data)
        if not icon_info:
            if self._debug:
                print(f"[FUSE] Failed to parse icon from {info_path}", flush=True)
            return None

        # Convert to ICNS
        from .icon_parser import create_icns
        aspect_ratio = icon_info.get("aspect_ratio", 1.0)
        icns_data = create_icns(icon_info["rgba"], icon_info["width"], icon_info["height"],
                                debug=self._debug, aspect_ratio=aspect_ratio)

        # Cache the result
        self._icon_cache.put(path, icns_data)

        if self._debug:
            print(f"[FUSE] Generated {len(icns_data)} byte ICNS for {path} "
                  f"({icon_info['width']}x{icon_info['height']} {icon_info['format']})", flush=True)
            # Save ICNS to temp file for verification
            safe_name = path.replace("/", "_").lstrip("_")
            icns_path = f"/tmp/amifuse_icon_{safe_name}.icns"
            with open(icns_path, "wb") as f:
                f.write(icns_data)
            print(f"[FUSE] DEBUG: Saved ICNS to {icns_path}", flush=True)
            # Also save resource fork for verification
            from .resource_fork import build_resource_fork
            rfork_data = build_resource_fork(icns_data, 0)
            rfork_path = f"/tmp/amifuse_icon_{safe_name}.rsrc"
            with open(rfork_path, "wb") as f:
                f.write(rfork_data)
            print(f"[FUSE] DEBUG: Saved resource fork ({len(rfork_data)} bytes) to {rfork_path}", flush=True)

        return icns_data

    def setxattr(self, path, name, value, options, position=0):
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        return 0

    def removexattr(self, path, name):
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        return 0

    def _free_locks(self, op: str, path: str, locks) -> None:
        """Free each of locks, apart from the others.

        A lock left held keeps its directory in use until unmount, so one
        free that fails is logged and doesn't stop the rest, or change what
        the delete it follows has settled.
        """
        for l in reversed(locks):
            try:
                self.bridge.free_lock(l)
            except Exception as e:
                self._log_op(op, path, f"free_lock exception: {type(e).__name__}: {e}")

    def _resolve_pending_dir_deletes(self, deleted_child_path: str):
        """After a child is deleted, check if its parent directory can now be removed.

        Walks up the directory tree resolving deferred rmdir operations when all
        blocking children have been deleted.
        """
        parent_dir = deleted_child_path.rsplit("/", 1)[0] or "/"
        while True:
            with self._fh_lock:
                if parent_dir not in self._pending_dir_deletes:
                    break
                self._pending_dir_deletes[parent_dir].discard(deleted_child_path)
                if self._pending_dir_deletes[parent_dir]:
                    # Still has other pending children - stop here
                    break
                # All children resolved - remove from dir_deletes to prevent re-entry
                # but keep in _pending_deletes until delete confirmed
                del self._pending_dir_deletes[parent_dir]
            # Attempt the actual directory deletion outside the lock
            locks = []
            try:
                # pfs3aio stops answering after an inhibit cycle with a file
                # open deeper in the tree, and SmartFilesystem drops every
                # open handle (#129). Cycle only with no file open, and
                # before taking the parent's lock: SmartFilesystem answers a
                # lock from before the cycle with ERROR_DEVICE_NOT_MOUNTED.
                if not self._any_file_open():
                    self.bridge.inhibit_cycle()
                dir_path, name = self._split_path(parent_dir)
                lock_bptr, _, locks = self.bridge.locate_path(dir_path)
                if dir_path == "/" and lock_bptr == 0:
                    lock_bptr, _ = self.bridge.locate(0, "")
                    if lock_bptr:
                        locks.append(lock_bptr)
                if lock_bptr != 0:
                    res1, res2 = self.bridge.delete_object(lock_bptr, name)
                    self._log_op("release", parent_dir, f"deferred rmdir res1={res1} res2={res2}")
                    if res1 == 0:
                        with self._fh_lock:
                            self._drop_failed_dir_delete(parent_dir)
                        break
                else:
                    with self._fh_lock:
                        self._drop_failed_dir_delete(parent_dir)
                    break
                # Delete succeeded - now remove from _pending_deletes
                with self._fh_lock:
                    self._pending_deletes.discard(parent_dir)
                with self._cache_lock:
                    self._stat_cache.pop(parent_dir, None)
                    self._dir_cache.pop(dir_path, None)
            except Exception:
                self._log_op("release", parent_dir, "deferred rmdir exception")
                with self._fh_lock:
                    self._drop_failed_dir_delete(parent_dir)
                break
            finally:
                self._free_locks("release", parent_dir, locks)
            # Successfully deleted this directory - walk up to check its parent
            deleted_child_path = parent_dir
            parent_dir = parent_dir.rsplit("/", 1)[0] or "/"

    def release(self, path, fh):
        self._log_op("release", path, f"fh={fh}")
        with self._fh_lock:
            entry = self._fh_cache.get(fh)
            if not entry:
                return 0
            entry["closed"] = True
            del self._fh_cache[fh]
        # Virtual Icon\r files have no real handle to close
        if entry.get("virtual_icon"):
            return 0
        # Virtual .VolumeIcon.icns files have no real handle to close
        if entry.get("virtual_volume_icon"):
            return 0
        with entry["lock"]:
            # truncate() may have ended the handle and failed to open
            # another (_reopen_for_output).
            if entry["fh_addr"]:
                self._log_op("release", path, f"closing fh_addr=0x{entry['fh_addr']:x}")
                self.bridge.close_file(entry["fh_addr"])
            parent_lock = entry.get("parent_lock", 0)
            if parent_lock:
                self.bridge.free_lock(parent_lock)
        # The pin is under the handle's path, and so is a delete unlink()
        # deferred, which it records only under a path a handle has.
        resolved_path = _handle_path(entry, path)
        # Unpin stat cache so future getattr re-queries PFS3 for current size.
        self._pinned_stats.discard(resolved_path)
        if self._cache_ttl <= 0:
            with self._cache_lock:
                self._stat_cache.pop(resolved_path, None)
        if resolved_path:
            self._set_held_date(resolved_path)
        # Check for deferred deletes (WinFSP unlink-before-release pattern)
        if resolved_path:
            with self._fh_lock:
                if resolved_path not in self._pending_deletes:
                    return 0
                # Check if another handle for this path is still open
                still_open = any(
                    v.get("path") == resolved_path and not v.get("closed")
                    for v in self._fh_cache.values()
                )
                if still_open:
                    return 0
                self._pending_deletes.discard(resolved_path)
            delete_succeeded = False
            dropped = False
            locks = []
            try:
                dir_path, name = self._split_path(resolved_path)
                lock_bptr, _, locks = self.bridge.locate_path(dir_path)
                if dir_path == "/" and lock_bptr == 0:
                    lock_bptr, _ = self.bridge.locate(0, "")
                    if lock_bptr:
                        locks.append(lock_bptr)
                no_reply = False
                if lock_bptr != 0:
                    res1, res2 = self.bridge.delete_object(lock_bptr, name)
                    self._log_op("release", resolved_path, f"deferred delete res1={res1} res2={res2}")
                    delete_succeeded = res1 != 0
                    # No reply: the handler may still delete it.
                    no_reply = not delete_succeeded and res2 == -1
                if not delete_succeeded:
                    with self._fh_lock:
                        # A handle left on the path, closed by a failed
                        # truncate() reopen, sends the delete again.
                        dropped = not no_reply and not any(
                            v.get("path") == resolved_path and "fh_addr" in v
                            for v in self._fh_cache.values()
                        )
                        if dropped:
                            self._drop_refused_delete(resolved_path)
                        else:
                            self._pending_deletes.add(resolved_path)
                if delete_succeeded:
                    with self._cache_lock:
                        self._stat_cache.pop(resolved_path, None)
                        self._dir_cache.pop(dir_path, None)
            except Exception as e:
                self._log_op("release", resolved_path, f"deferred delete exception: {type(e).__name__}: {e}")
                # Hidden again only when the delete got no answer: one the
                # handler answered is settled above.
                if not delete_succeeded and not dropped:
                    with self._fh_lock:
                        self._pending_deletes.add(resolved_path)
            finally:
                self._free_locks("release", resolved_path, locks)
            if dropped:
                # The file stays, and takes the date utimens() held for it.
                self._set_held_date(resolved_path)
            else:
                with self._fh_lock:
                    self._held_dates.pop(resolved_path, None)
            # Resolve any deferred parent directory deletes only on success
            if delete_succeeded:
                self._resolve_pending_dir_deletes(resolved_path)
        return 0

    def init(self, path):
        """Called when filesystem is mounted. Notify shell on Windows."""
        if self._unmount_control is not None:
            self._unmount_control.start()
        self._start_timer_thread()
        if self._mount_record is not None:
            from .platform import write_mount_record
            write_mount_record(self._mount_record)
        self._report_ready()
        if sys.platform.startswith("win") and self._mountpoint:
            from .platform import notify_shell_drive_change
            notify_shell_drive_change(str(self._mountpoint), added=True)

    def _report_ready(self):
        """Tell the daemon-mode parent the mount is up, and let the terminal go.

        As libfuse's daemonize did after its fork: stdin, stdout and stderr
        go to /dev/null, and the working directory to /, so the mount holds
        neither the terminal nor a directory.
        """
        fd = self._ready_fd
        if fd is None:
            return
        self._ready_fd = None
        try:
            os.write(fd, b"ready\n")
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            sys.stdout.flush()
            sys.stderr.flush()
            null = os.open(os.devnull, os.O_RDWR)
            for std in (0, 1, 2):
                os.dup2(null, std)
            if null > 2:
                os.close(null)
            os.chdir("/")
        except OSError:
            pass

    def _start_timer_thread(self):
        """Have the bridge reply to timer.device requests between FUSE operations.

        Without it an idle handler never sees time pass, so work it defers
        to a timer, such as a write-back, would wait for the next operation.
        The bridge's emulation thread does it while no operation is in
        progress (HandlerBridge.start_idle_timers); a handler that crashes
        there shuts the mount down, as a FUSE operation does when it finds
        the handler crashed.
        """
        bridge = self.bridge
        if self._timers_started or getattr(bridge, "_emu", None) is None:
            return
        if getattr(getattr(bridge, "vh", None), "timer_device", None) is None:
            return
        self._timers_started = True
        bridge.start_idle_timers(on_crash=self._schedule_crash_shutdown)

    def _stop_timer_thread(self):
        if not getattr(self, "_timers_started", False):
            return
        self._timers_started = False
        self.bridge.stop_idle_timers()

    def destroy(self, path):
        """Called when filesystem is unmounted. Flush and release all resources."""
        control = self._unmount_control
        if control is not None:
            control.stop()
        # Before the final flush, so that no timer burst runs during it.
        self._stop_timer_thread()
        success = True
        print("[amifuse] Unmounting - flushing volume...", flush=True)
        try:
            if self.bridge._write_enabled:
                if _handler_has_crashed(self.bridge):
                    raise RuntimeError("handler crashed before volume flush")
                unanswered = self.bridge.wait_for_pending()
                flush_error = None
                try:
                    self.bridge.flush_volume(wait_pending=False)
                except Exception as e:
                    # A handler that takes packets in order, still at work
                    # on one, does not answer the flush: the packets still
                    # out are named all the same.
                    if not unanswered:
                        raise
                    flush_error = e
                if unanswered:
                    # A handler may answer only once a packet wakes it, so
                    # the flush may have brought the replies; the work they
                    # end then needs a flush of its own.
                    unanswered = self.bridge.wait_for_pending(timeout=1.0)
                    if not unanswered:
                        self.bridge.flush_volume(wait_pending=False)
                        flush_error = None
                if unanswered:
                    success = False
                    print(f"[amifuse] WARNING: the handler has not answered "
                          f"{', '.join(unanswered)}; the volume may be left "
                          f"half changed", flush=True)
                if flush_error is not None:
                    raise flush_error
        except Exception as e:
            success = False
            print(f"[amifuse] WARNING: flush failed: {e}", flush=True)
        try:
            shutdown = getattr(self.bridge, "shutdown_runtime", None)
            if shutdown is not None:
                shutdown()
        except Exception as e:
            success = False
            print(f"[amifuse] WARNING: runtime shutdown failed: {e}", flush=True)
        backend = getattr(self.bridge, "backend", None)
        if backend is not None:
            try:
                backend.sync()
            except Exception as e:
                success = False
                print(f"[amifuse] WARNING: backend sync failed: {e}", flush=True)
            try:
                backend.close()
            except Exception as e:
                success = False
                print(f"[amifuse] WARNING: backend close failed: {e}", flush=True)
        # Mark bridge as closed so close() becomes a no-op if called after destroy()
        self.bridge._closed = True
        # Notify Explorer that drive was removed
        if sys.platform.startswith("win") and self._mountpoint:
            from .platform import notify_shell_drive_change
            notify_shell_drive_change(str(self._mountpoint), added=False)
        if control is not None:
            control.complete(success)
        print("[amifuse] Unmount complete." if success else
              "[amifuse] Unmount completed with errors.", flush=True)

    def statfs(self, path):
        """Return filesystem statistics (disk space info).

        Maps Amiga InfoData to POSIX statvfs fields for Explorer/df.
        """
        self._check_handler_alive()
        # Check disk info cache
        now = time.time()
        cached = self._disk_info_cache
        if cached is not None:
            cache_ts, cache_result = cached
            ttl = self._disk_info_ttl
            if ttl < 0 or (now - cache_ts < ttl):
                return cache_result

        info = self.bridge.get_disk_info()
        if info is None:
            # Fallback: return zeros (better than raising an error)
            result = {
                'f_bsize': 512,
                'f_frsize': 512,
                'f_blocks': 0,
                'f_bfree': 0,
                'f_bavail': 0,
                'f_files': 0,
                'f_ffree': 0,
                'f_favail': 0,
                'f_namemax': 107,  # Amiga FFS/PFS3 max filename length
            }
        else:
            bsize = info['bytes_per_block']
            total = info['num_blocks']
            used = info['num_blocks_used']
            free = max(0, total - used)

            result = {
                'f_bsize': bsize,
                'f_frsize': bsize,
                'f_blocks': total,
                'f_bfree': free,
                'f_bavail': free,  # No reserved blocks concept in AmigaOS
                'f_files': 0,      # Amiga FS doesn't have inode limits
                'f_ffree': 0,
                'f_favail': 0,
                'f_namemax': 107,  # BSTR max: 255 minus length byte, but FFS uses 30, PFS3 uses 107
            }

        # Cache the result
        self._disk_info_cache = (now, result)
        return result


def get_partition_info(image: Path, block_size: Optional[int], partition: Optional[str]) -> dict:
    """Get partition name, dostype and block size in bytes from RDB.

    The name is the one the partition mounts as: a repeated drive name reads
    DH0.1 (rdb_inspect.find_partition). A damaged drive name, which amitools
    reads as None, is named by the partition's number: #0. The block size
    is the partition's own (de_SizeBlock), which the handler uses.
    """
    from .rdb_inspect import open_rdisk, find_partition, partition_label
    ref = find_partition(image, block_size, partition) if partition else None
    mbr_idx = ref.mbr_partition_index if ref else None
    blkdev, rdisk, _mbr_ctx = open_rdisk(image, block_size=block_size, mbr_partition_index=mbr_idx)
    try:
        if partition is None:
            part = rdisk.get_partition(0)
        else:
            part = rdisk.get_partition(ref.number) if ref else None
        if part is None:
            return {"name": "AmigaFS", "dostype": None, "block_size": None}
        return {
            "name": ref.name if ref and ref.name else partition_label(part),
            "dostype": part.part_blk.dos_env.dos_type,
            "block_size": part.part_blk.dos_env.block_size * 4,
        }
    finally:
        rdisk.close()
        blkdev.close()


def get_partition_name(image: Path, block_size: Optional[int], partition: Optional[str]) -> str:
    """Get the partition name from RDB without starting the handler."""
    return get_partition_info(image, block_size, partition)["name"]


def _entry_partitions(error: Exception, image: Path,
                      block_size: Optional[int]) -> List[Tuple[str, str]]:
    """Return (label, selector) choices after the disk's own RDB fails.

    Names and numeric fallbacks must resolve to the exact partition being
    offered. Display labels such as #0 remain separate from selector 0,
    including when a real drive name happens to start with #.
    """
    from .rdb_inspect import DiskRDBError, find_partition, list_partitions

    if not isinstance(error, DiskRDBError):
        return []
    try:
        parts = list_partitions(image, block_size).partitions
    except OSError:
        return []
    choices = []
    for part in parts:
        selector = part.name if part.name is not None else str(part.index)
        try:
            ref = find_partition(image, block_size, selector)
        except OSError:
            continue
        if ref is not None and (ref.mbr_partition_index, ref.number) == (
                part.mbr_partition_index, part.index):
            label = part.name if part.name is not None else f"#{part.index}"
            choices.append((label, selector))
    return choices


def _entry_partitions_hint(choices: List[Tuple[str, str]]) -> str:
    """Return the recovery hint with a usable selector, not its label."""
    if not choices:
        return ""
    shown = [escape_text(label) for label, _ in choices]
    selector = shlex.quote(escape_text(choices[0][1]))
    return (f"\nThe partitions in the MBR's 0x76 entries still open, read-only: "
            f"{', '.join(shown)}. Open one with --partition {selector}.")


# The PID that extracted the driver, then mkstemp's random part, whose
# alphabet has no "-", and the DOS type. Earlier versions wrote no PID.
_EXTRACTED_DRIVER_NAME = re.compile(r"amifuse_(\d{1,9})-\w+_[0-9a-f]{8}\.handler")


def _remove_stale_drivers() -> None:
    """Remove extracted drivers that killed runs left in the temp directory.

    A file is used only from its extraction until the bridge has loaded it,
    so one that _left_by_ended_run calls stale is removed. A file of another
    name is left alone, as nothing says whether a run still needs it. A file
    that can't be removed is left for a later run.
    """
    import tempfile
    from .platform import _left_by_ended_run

    temp_dir = tempfile.gettempdir()
    try:
        names = os.listdir(temp_dir)
    except OSError:
        return
    for name in names:
        match = _EXTRACTED_DRIVER_NAME.fullmatch(name)
        if match is None:
            continue
        path = os.path.join(temp_dir, name)
        try:
            if _left_by_ended_run(int(match[1]), os.lstat(path).st_mtime):
                os.unlink(path)
        except OSError:
            pass


def extract_embedded_driver(
    image: Path, block_size: Optional[int], partition: Optional[str]
) -> Optional[Tuple[Path, str, int]]:
    """Extract filesystem driver from RDB if available for the partition's dostype.

    Returns (temp file path, printable dostype tag, dostype) for the extracted
    driver, or None if the RDB has no filesystem entry matching the
    partition's dostype. The caller is responsible for deleting the temp
    file when done.

    Raises DriverNotFound when no readable entry matches but the RDB has a
    damaged one that may be the partition's: one of its DOS type, or a
    corrupt header that hid the entries after it. Another driver is not
    picked in its place.
    """
    import tempfile
    from .rdb_inspect import open_rdisk, find_partition, partition_label

    ref = find_partition(image, block_size, partition) if partition else None
    mbr_idx = ref.mbr_partition_index if ref else None
    blkdev, rdisk, _mbr_ctx = open_rdisk(image, block_size=block_size, mbr_partition_index=mbr_idx)
    try:
        # Get the partition and its dostype
        if partition is None:
            part = rdisk.get_partition(0)
        else:
            part = rdisk.get_partition(ref.number) if ref else None
        if part is None:
            return None

        target_dostype = part.part_blk.dos_env.dos_type
        dt_str = dos_type_tag(target_dostype)

        # Search filesystem blocks for matching dostype
        for fs in rdisk.fs:
            if fs.fshd.dos_type == target_dostype:
                # Found matching filesystem - extract to temp file
                data = fs.get_data()
                _remove_stale_drivers()
                # Create temp file that persists until program exits. It is
                # named by number: the tag's characters come from the image.
                # The PID is for _remove_stale_drivers.
                fd, temp_path = tempfile.mkstemp(
                    suffix=f"_{target_dostype:08x}.handler",
                    prefix=f"amifuse_{os.getpid()}-")
                try:
                    # The file object closes fd even if a write fails, and
                    # retries short writes until all of data is written.
                    with os.fdopen(fd, "wb") as handler_file:
                        handler_file.write(data)
                except OSError as exc:
                    os.unlink(temp_path)
                    # Name the temp file, so that a full temp directory is
                    # not mistaken for a problem with the image.
                    raise OSError(
                        f"cannot write the filesystem driver to the temporary "
                        f"file {temp_path}: {exc}"
                    ) from exc
                except BaseException:
                    os.unlink(temp_path)
                    raise
                return Path(temp_path), dt_str, target_dostype

        # The lenient RDB parser drops damaged entries; None is a corrupt
        # header, whose DOS type is unknown.
        damaged = [(dos_type, warning)
                   for dos_type, warning, _num in getattr(rdisk, "damaged_fs", ())
                   if dos_type in (target_dostype, None)]
        if damaged:
            known = any(dos_type == target_dostype for dos_type, _ in damaged)
            raise DriverNotFound(
                f"Partition {escape_text(partition_label(part))} ({dt_str}) "
                f"{'has' if known else 'may have'} a damaged filesystem driver "
                f"in its RDB: {'; '.join(warning for _, warning in damaged)}.\n"
                "AmiFUSE does not use another driver in its place; "
                "use --driver to choose one."
            )
        return None
    finally:
        rdisk.close()
        blkdev.close()


def resolve_driver(
    image: Path,
    driver: Optional[Path],
    block_size: Optional[int],
    partition: Optional[str],
    adf_info=None,
    part_info: Optional[dict] = None,
    iso_info=None,
) -> ResolvedDriver:
    """Pick the filesystem driver for an ADF, an ISO or an RDB partition.

    Order: --driver; the RDB's driver for the partition's exact DOS type;
    then, for DOS0-DOS7 volumes, driver_resolver.find_folder_driver() (your
    drivers folder, the bundled FastFileSystem). An ISO needs --driver.
    part_info is get_partition_info()'s result when the caller has it.

    Raises DriverNotFound. Errors reading the RDB propagate. Only an RDB
    extraction is temporary, so only that is the caller's to delete.
    """
    if driver is not None:
        return ResolvedDriver(Path(driver), "explicit", str(driver))
    if adf_info is not None:
        floppy_type = "HD" if adf_info.is_hd else "DD"
        dt_str = driver_resolver.dos_tag(adf_info.dos_type)
        return driver_resolver.find_folder_driver(
            adf_info.dos_type, f"ADF floppy image detected ({floppy_type}, {dt_str}).")
    if iso_info is not None:
        raise DriverNotFound(
            f"ISO 9660 image detected (volume: {escape_text(iso_info.volume_id)}).\n"
            "ISO images don't contain embedded filesystem drivers.\n"
            "You need to specify a filesystem handler with --driver.\n"
            "For ISO 9660, use CDFileSystem from an AmigaOS installation."
        )

    result = extract_embedded_driver(image, block_size, partition)
    if result is not None:
        temp_driver, dt_str, dostype = result
        return ResolvedDriver(
            temp_driver, "rdb", f"{dt_str}/0x{dostype:08x} (from RDB)", temporary=True,
            display_name=f"the {dt_str} (0x{dostype:08x}) driver stored in the RDB")

    if part_info is None:
        part_info = get_partition_info(image, block_size, partition)
    dostype = part_info.get("dostype")
    if not driver_resolver.is_dos_type(dostype):
        raise DriverNotFound(
            "No embedded filesystem driver found for this partition.\n"
            "You need to specify a filesystem handler with --driver"
        )
    name = part_info.get("name") or partition
    return driver_resolver.find_folder_driver(
        dostype,
        f"Partition {escape_text(name)} has no filesystem driver for "
        f"{driver_resolver.dos_tag(dostype)} in its RDB.",
    )


def _close_bridge(bridge):
    """Close the bridge as mount or format ends, reporting a failed close.

    An error from the close goes to stderr instead of being raised, so
    that it neither replaces the error the command exits with nor turns a
    finished format or unmount into a traceback. destroy() reports its own
    shutdown errors the same way.
    """
    try:
        bridge.close()
    except Exception as exc:
        print(f"[amifuse] could not close the bridge: {exc}", file=sys.stderr)


def _fork_daemon_parent() -> int:
    """Fork for daemon mode; the parent exits once the child is mounted or gone.

    libfuse daemonizes after the mount is set up, with a fork. The bridge
    cannot be forked: its emulation runs on a thread of its own (see
    emu_thread.py), and a fork keeps only the forking thread. So the mount
    forks here, before the bridge is built, and the child builds it, mounts
    in the foreground and reports over a pipe. Returns, in the child, the
    fd to write "ready" to once mounted; the child keeps the terminal until
    then, so its errors reach the user. The parent reads the pipe: on
    "ready" it exits 0, and once the child ends first, with the child's
    exit status.
    """
    read_fd, write_fd = os.pipe()
    sys.stdout.flush()
    sys.stderr.flush()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.setsid()
        except OSError:
            pass
        return write_fd
    os.close(write_fd)
    data = b""
    try:
        while b"\n" not in data:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            data += chunk
    finally:
        os.close(read_fd)
    if data.startswith(b"ready"):
        os._exit(0)
    _, status = os.waitpid(pid, 0)
    code = os.waitstatus_to_exitcode(status)
    os._exit(code if code >= 0 else 1)


def mount_fuse(
    image: Path,
    driver: Optional[Path],
    mountpoint: Optional[Path],
    block_size: Optional[int],
    volname_opt: Optional[str] = None,
    debug: bool = False,
    trace: bool = False,
    write: bool = False,
    partition: Optional[str] = None,
    icons: bool = False,
    foreground: Optional[bool] = None,
):
    fuse_class = _require_fuse()
    from .rdb_inspect import detect_adf, detect_iso
    from . import platform as plat

    # Fail fast if FUSE driver is missing -- before any image analysis
    plat.check_fuse_available()

    # First, check if this is an ADF (floppy disk image)
    adf_info = detect_adf(image)
    iso_info = None
    part_info = None

    if adf_info is not None:
        # ADF floppy detected - use DF0 as the partition name
        part_name = "DF0"  # Will use actual volume name from handler later
    else:
        # Check for ISO 9660 image
        iso_info = detect_iso(image)

        if iso_info is not None:
            part_name = "CD0"
        else:
            # HDF/RDB image - get partition info
            try:
                part_info = get_partition_info(image, block_size, partition)
            except IOError as e:
                names = _entry_partitions(e, image, block_size)
                raise SystemExit(f"Error: {e}{_entry_partitions_hint(names)}")
            part_name = part_info["name"]

    try:
        resolved = resolve_driver(
            image, driver, block_size, partition,
            adf_info=adf_info, part_info=part_info, iso_info=iso_info,
        )
    except IOError as e:
        raise SystemExit(f"Error: {e}")
    except DriverNotFound as e:
        raise SystemExit(str(e)) from None
    driver = resolved.path
    driver_desc = resolved.description
    temp_driver = resolved.path if resolved.temporary else None

    try:
        # Auto-create mountpoint on macOS/Windows if not specified
        if mountpoint is None:
            mountpoint = plat.get_default_mountpoint(volname_opt or part_name)
            if mountpoint is None:
                if sys.platform.startswith("win"):
                    raise SystemExit(
                        "No free drive letter found. Specify a mountpoint with --mountpoint D:"
                    )
                raise SystemExit("--mountpoint is required on this platform")

        validation_error = plat.validate_mountpoint(mountpoint)
        if validation_error:
            raise SystemExit(validation_error)

        try:
            fuse_mountpoint = plat.get_fuse_mountpoint(mountpoint)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if sys.platform.startswith("win"):
            drive = plat.windows_drive_mountpoint(mountpoint)
            if drive:
                # Keep OS probes, shell notifications and user-facing paths in
                # the normal namespace; only WinFsp needs the device spelling.
                mountpoint = Path(drive)

        # Create mountpoint directory if it doesn't exist
        if not plat.should_auto_create_mountpoint(mountpoint):
            if not mountpoint.exists():
                try:
                    mountpoint.mkdir(parents=True, exist_ok=True)
                except FileExistsError:
                    raise SystemExit(plat.format_stale_mountpoint_error(mountpoint))
                except OSError as exc:
                    if exc.errno in (errno.EIO, errno.ENOTCONN):
                        raise SystemExit(plat.format_stale_mountpoint_error(mountpoint))
                    raise SystemExit(
                        f"Could not create mountpoint {mountpoint}: {exc.strerror or exc}"
                    )

        if foreground is None:
            # Daemon mode is the default on every platform; --interactive /
            # --foreground opts in to staying attached to the terminal.
            foreground = False

        # Print startup banner
        print(__banner__)
        if adf_info is not None:
            floppy_type = "HD" if adf_info.is_hd else "DD"
            print(f"Mounting ADF floppy ({floppy_type}) from {image}")
        elif iso_info is not None:
            print(f"Mounting ISO 9660 image ({escape_text(iso_info.volume_id)}) from {image}")
        else:
            print(f"Mounting partition '{escape_text(part_name)}' from {image}")
        print(f"Filesystem driver: {driver_desc}")
        print(f"Mount point: {mountpoint}")
        if sys.platform.startswith("win") and fuse_mountpoint.startswith("\\\\.\\"):
            print("[amifuse] global drive; unmount from an Administrator shell")
        if foreground:
            print("[amifuse] interactive mode; press Ctrl+C to unmount")
        elif sys.platform.startswith("darwin"):
            # On FUSE-T, `amifuse unmount` needs this mount's record to tie
            # it to this process, and writing one can fail; `umount` works
            # on both macOS backends.
            print("[amifuse] daemon mode; unmount with: umount "
                  + shlex.quote(str(mountpoint)))
        else:
            print(f"[amifuse] daemon mode; unmount with: amifuse unmount {mountpoint}")

        if write:
            # Guard against accidental writes without explicit intent.
            print("[amifuse] write mode enabled; ensure the image is backed up")

        if icons:
            print("[amifuse] icon mode enabled; Amiga icons will appear as macOS custom icons")

        # Daemon mode forks here, before the handler starts; see
        # _fork_daemon_parent. The child mounts in the foreground.
        ready_fd = None
        if not foreground and not sys.platform.startswith("win"):
            ready_fd = _fork_daemon_parent()
            foreground = True

        try:
            bridge = HandlerBridge(
                image,
                driver,
                driver_name=resolved.display_name,
                block_size=block_size,
                read_only=not write,
                debug=debug,
                trace=trace,
                partition=partition,
                adf_info=adf_info,
                iso_info=iso_info,
                remove_driver=resolved.temporary,
            )
        except (OSError, ValueError) as exc:
            # Lock conflicts and invalid partitions are expected refusals.
            if debug:
                raise
            raise SystemExit(f"Cannot mount {image}: {exc}") from exc
        if _handler_has_crashed(bridge):
            _close_bridge(bridge)
            raise SystemExit("Filesystem handler crashed during startup; mount aborted.")

        # Let default signal handling work - FUSE will call destroy() on unmount
        volname = plat.fuse_option_value(volname_opt or bridge.volume_name())
        if _handler_has_crashed(bridge):
            _close_bridge(bridge)
            raise SystemExit("Filesystem handler crashed during startup; mount aborted.")

        # Pre-generate volume icon if platform requires it at mount time
        temp_volicon = None
        if icons:
            temp_volicon = plat.pre_generate_volume_icon(bridge, debug=debug)
            if temp_volicon:
                print(f"[amifuse] Volume icon generated: {temp_volicon}")

        # Multi-threaded mode with caching to minimize macOS polling.
        use_threads = not write
        fuse_kwargs = {
            "foreground": foreground,
            "ro": not write,
            "allow_other": False,
            "nothreads": not use_threads,
            "fsname": f"amifuse:{volname}",
            "default_permissions": True,  # Let kernel handle permission checks
        }
        # WinFSP maps FUSE uid=0/gid=0 to Windows SID S-1-5-0 (Nobody), blocking
        # write access for the current user.  uid/gid=-1 tells WinFSP to use the
        # mounting user's SID instead.
        if sys.platform.startswith("win"):
            fuse_kwargs["uid"] = -1
            fuse_kwargs["gid"] = -1
        # subtype is a Linux-only FUSE option; WinFSP and macFUSE don't support it
        if sys.platform.startswith("linux"):
            fuse_kwargs["subtype"] = "amifuse"
        # Add platform-specific mount options
        platform_opts = plat.get_mount_options(
            volname=volname,
            volicon_path=str(temp_volicon) if temp_volicon else None,
            icons_enabled=icons
        )
        fuse_kwargs.update(platform_opts)

        if debug:
            print(f"[amifuse] FUSE options: {fuse_kwargs}", flush=True)

        control = None
        operations = None
        try:
            operations = AmigaFuseFS(bridge, debug=debug, icons=icons, mountpoint=mountpoint,
                                     ready_fd=ready_fd)
            # Absolute now: the working directory changes once mounted.
            operations._mount_record = {
                "mountpoint": os.path.abspath(mountpoint),
                "image": os.path.abspath(image),
                "partition": partition,
            }
            if sys.platform.startswith("win"):
                from .windows_unmount import UnmountControl
                control = UnmountControl()
                operations._unmount_control = control
            try:
                fuse_class(
                    operations,
                    fuse_mountpoint,
                    **fuse_kwargs,
                )
            except RuntimeError as exc:
                if not sys.platform.startswith("win") or debug:
                    raise
                raise SystemExit(
                    f"WinFsp could not mount '{mountpoint}': {exc}"
                ) from exc
        finally:
            if control is not None:
                control.close()
            # destroy() stops it, but a mount can end without destroy().
            if operations is not None:
                operations._stop_timer_thread()
            plat.remove_mount_record()
            _close_bridge(bridge)
            # Clean up temp volume icon file
            if temp_volicon is not None and temp_volicon.exists():
                temp_volicon.unlink()
    finally:
        # Every exit removes the driver extracted from the RDB, also one
        # that stops before mounting. A failed removal is reported, but does
        # not replace the message the mount exits with.
        if temp_driver is not None:
            try:
                temp_driver.unlink(missing_ok=True)
            except OSError as exc:
                print(f"[amifuse] could not remove {temp_driver}: {exc}",
                      file=sys.stderr)


def _ver_number(ver: Optional[str]) -> Optional[Tuple[int, str]]:
    """Return the major version and the version in a $VER text.

    E.g. (19, "19.2") for "name 19.2 (date)": the first word that is two
    numbers joined by a dot. None if there is no such word.
    """
    for token in (ver or "").split():
        major, dot, minor = token.partition(".")
        if dot and major.isdigit() and minor.isdigit():
            return int(major), token
    return None


def _is_pfs3_driver(dostype: int, ver: Optional[str]) -> bool:
    """Return whether format takes the driver for pfs3aio.

    PFS3 names itself in its $VER, whatever the partition's DOS type. A
    driver without $VER is taken for PFS3 on a PFS partition, since it may
    be one; a $VER naming another filesystem is not PFS3.
    """
    if ver is None:
        # "PFS\x", "PDS\x", "muPF", "muAF" and "AFS\1".
        return (dostype & 0xFFFFFF00 in (0x50465300, 0x50445300)
                or dostype in (0x6D755046, 0x6D754146, 0x41465301))
    return any(name in ver.upper()
               for name in ("PROFESSIONAL-FILE-SYSTEM", "PFS-III", "PFS3"))


def _pfs3_format_refusal(dostype: int, block_bytes: Optional[int], driver: Path,
                         partition: str) -> Optional[str]:
    """Return why format refuses pfs3aio on a partition with large blocks, or None.

    pfs3aio before 20.0 cannot format blocks over 1024 bytes: 19.2 fixes
    its reserved blocks at 1024 bytes, counts 0 sectors per reserved block
    and divides by zero, whatever the partition's DOS type. PFS3 names
    itself in its $VER. A driver without $VER is refused on a PFS
    partition, since it may be one of those.
    Raises OSError when the driver cannot be read.
    """
    if block_bytes is None or block_bytes <= 1024:
        return None
    ver = driver_resolver.find_version(Path(driver).read_bytes())
    if not _is_pfs3_driver(dostype, ver):
        return None
    number = _ver_number(ver)
    if number is not None and number[0] >= 20:
        return None
    if number is None:
        found = "the driver's version could not be determined"
    else:
        found = f"the driver is version {number[1]}"
    return (
        f"Error: partition '{partition}' has {block_bytes}-byte blocks, which "
        f"pfs3aio before 20.0 cannot format, and {found}. Use 512- or "
        "1024-byte blocks, or pfs3aio 20.0 or later."
    )


def _pfs3_volume_name_refusal(dostype: int, driver: Path,
                              volname: str) -> Optional[str]:
    """Return why format refuses a volume name too long for pfs3aio, or None.

    PFS3's root block holds at most 31 characters of volume name. pfs3aio
    answers success for a longer one, but stores the length asked for with
    only the first 31 characters: the name then reads wrong, and with 40,
    107 or 255 characters (19.2 tested) the handler crashes at every later
    open. The root block's layout does not change with the version, so
    every version is refused. The length is counted as cmd_format counts it.
    Raises OSError when the driver cannot be read.
    """
    length = len(volname.encode("latin-1", errors="replace"))
    if length <= 31:
        return None
    ver = driver_resolver.find_version(Path(driver).read_bytes())
    if not _is_pfs3_driver(dostype, ver):
        return None
    return (f"Error: volume name is {length} characters long; pfs3aio "
            "stores at most 31. Use a shorter name.")


def _refuse_long_volume_name(volname: str) -> None:
    """Refuse a volume name that cannot be sent to the handler.

    The handler gets the name as a BSTR, whose length is a single byte, so
    a longer name cannot be sent. The length is counted as the BSTR encodes
    it, one byte per character. Shorter names are mostly the handler's to
    judge, and handlers differ: FastFileSystem refuses more than 30
    characters and SmartFilesystem stores all 255. pfs3aio holds 31 but
    answers success for more and leaves a damaged volume, so format_volume
    refuses a longer name once it knows the driver.
    """
    length = len(volname.encode("latin-1", errors="replace"))
    if length > 255:
        raise SystemExit(
            f"Error: volume name is {length} characters long; at most 255 are allowed.")


def format_volume(
    image: Path,
    driver: Optional[Path],
    block_size: Optional[int],
    partition: Optional[str],
    volname: Optional[str] = None,
    debug: bool = False,
    dos_type: Optional[int] = None,
):
    """Format a partition of an RDB disk, or a floppy image.

    A floppy has no partitions, so partition is its volume name if volname
    is None, as in `amifuse format floppy.adf Name`. A name in both places
    is refused for a floppy. dos_type, which only a floppy takes, replaces
    the DOS type in its block 0. rdb_inspect.floppy_to_format says what
    counts as a floppy here.
    """
    from .rdb_inspect import floppy_to_format

    bridge = None

    # Name a missing image or a directory plainly, as the file commands do.
    if not is_windows_disk(image):
        if not image.exists():
            raise SystemExit(f"Error: image file not found: {image}")
        if image.is_dir():
            raise SystemExit(f"Error: image is a directory: {image}")

    # Get the floppy's or the partition's dostype
    try:
        adf_info = floppy_to_format(image, dos_type, block_size)
        if adf_info is not None:
            part_info = {"name": "DF0", "dostype": adf_info.dos_type,
                         "block_size": adf_info.block_size}
        elif dos_type is not None:
            raise SystemExit(
                "Error: --dos-type is only for a floppy image (901120 or "
                "1802240 bytes); a partition has its DOS type in the RDB.")
        else:
            part_info = get_partition_info(image, block_size, partition)
    except (OSError, ValueError) as exc:
        # A missing or unreadable image, or one without a usable RDB.
        if debug:
            raise
        raise SystemExit(f"Error: {exc}") from exc
    if adf_info is not None:
        if partition is not None and volname is not None:
            raise SystemExit(
                f"Error: {image} is a floppy image, which has no partitions. "
                "Use: amifuse format <image> <name>")
        volname = partition if volname is None else volname
        partition = None
    volname = "Empty" if volname is None else volname
    _refuse_long_volume_name(volname)
    dostype = part_info["dostype"]
    if dostype is None:
        raise SystemExit(f"Could not determine dostype for partition '{partition}'.")
    dt_str = dos_type_tag(dostype)

    # Resolve driver
    try:
        resolved = resolve_driver(
            image, driver, block_size, partition, adf_info=adf_info, part_info=part_info)
    except DriverNotFound as exc:
        raise SystemExit(str(exc)) from None
    except (OSError, ValueError) as exc:
        if debug:
            raise
        raise SystemExit(f"Error: {exc}") from exc
    driver = resolved.path
    driver_desc = resolved.description
    temp_driver = resolved.path if resolved.temporary else None

    try:
        # Inside the try, so that a failing print, such as a volume name
        # the console cannot encode, still removes an extracted driver.
        print(__banner__)
        if adf_info is not None:
            floppy_type = "HD" if adf_info.is_hd else "DD"
            print(f"Formatting {floppy_type} floppy image {image}")
        else:
            print(f"Formatting partition '{partition}' in {image}")
        print(f"Filesystem driver: {driver_desc}")
        print(f"Volume name: {volname}")
        print(f"DOS type: {dt_str} (0x{dostype:08x})")

        # Refuse before the handler starts, so the image is not touched.
        try:
            refusal = (_pfs3_format_refusal(
                dostype, part_info.get("block_size"), driver, partition)
                or _pfs3_volume_name_refusal(dostype, driver, volname))
        except OSError as exc:
            if debug:
                raise
            # The refusals read the driver before the loader does. One from
            # the RDB was read from the image without error: the file that
            # failed is its temporary copy.
            if resolved.display_name:
                name = f"the temporary copy of {resolved.display_name}"
            else:
                name = driver
            raise SystemExit(
                f"Error: cannot read {name}: {exc.strerror or exc}") from exc
        if refusal is not None:
            raise SystemExit(refusal)

        try:
            bridge = HandlerBridge(
                image,
                driver,
                driver_name=resolved.display_name,
                block_size=block_size,
                read_only=False,
                debug=debug,
                partition=partition,
                adf_info=adf_info,
                remove_driver=resolved.temporary,
            )
        except (OSError, ValueError) as exc:
            # Any OSError or ValueError from the whole constructor: opening
            # and locking the image (read-only, busy, a refused partition
            # lock), loading the handler, the bootstrap's RDB read and the
            # startup run. As broad as mount's catch on purpose.
            if debug:
                raise
            raise SystemExit(f"Error: {exc}") from exc
        _raise_if_handler_crashed(bridge, "format startup")

        # Inhibit the volume so the handler releases it for formatting.
        # Each packet waits for its own reply, so that a late one cannot
        # pass for the next packet's.
        pkt_addr, _ = bridge.launcher.send_inhibit(bridge.state, True)
        reply = bridge._run_until_reply(pkt_addr, "format inhibit")
        _raise_if_handler_crashed(bridge, "format inhibit")
        if debug and reply:
            print(f"[amifuse] INHIBIT reply: res1={reply[0]} res2={reply[1]}")

        # Allocate volume name BSTR and send ACTION_FORMAT
        _, volname_bptr = bridge.launcher.alloc_bstr(volname, label="FormatVolName")
        pkt_addr, _ = bridge.launcher.send_format(bridge.state, volname_bptr, dostype)
        # Format can take many cycles for large partitions — PFS3 must
        # initialize bitmap and root blocks proportional to partition size.
        # Use generous limits; the loop returns immediately once a reply arrives.
        reply = bridge._run_until_reply(
            pkt_addr, "format", max_iters=2000, cycles=2_000_000
        )
        _raise_if_handler_crashed(bridge, "format")

        if reply is None:
            raise SystemExit("Format failed: no reply from handler.")

        res1, res2 = reply
        if res1 == 0:
            error_msgs = {
                ERROR_OBJECT_IN_USE: "Object in use",
                ERROR_DISK_WRITE_PROTECTED: "Disk is write-protected",
                ERROR_DEVICE_NOT_MOUNTED: "Device not mounted",
                ERROR_NOT_A_DOS_DISK: "Not a DOS disk",
                ERROR_NO_DISK: "No disk in drive",
            }
            error_desc = error_msgs.get(res2, f"error code {res2}")
            raise SystemExit(f"Format failed: {error_desc}")

        if debug:
            print(f"[amifuse] FORMAT reply: res1={res1} res2={res2}")

        # FastFileSystem finishes the new bitmap when inhibition ends.
        # Wait for this packet before flushing, without restarting the bridge.
        pkt_addr, _ = bridge.launcher.send_inhibit(bridge.state, False)
        reply = bridge._run_until_reply(
            pkt_addr, "format uninhibit", max_iters=500, cycles=2_000_000
        )
        _raise_if_handler_crashed(bridge, "format uninhibit")
        if reply is None:
            raise SystemExit("Format failed: no reply to ending ACTION_INHIBIT.")
        if reply[0] == 0:
            raise SystemExit(
                f"Format failed: handler refused to end ACTION_INHIBIT "
                f"(error code {reply[1]})")

        # Flush metadata written while the handler mounts the new volume.
        pkt_addr, _ = bridge.launcher.send_flush(bridge.state)
        reply = bridge._run_until_reply(
            pkt_addr, "format flush", max_iters=500, cycles=2_000_000
        )
        _raise_if_handler_crashed(bridge, "format flush")
        # Uninhibit has mounted the new volume, so a refusal saying there
        # is no volume fails the format. Only ERROR_ACTION_NOT_KNOWN, from
        # a handler without ACTION_FLUSH, passes.
        if reply is None:
            raise SystemExit("Format failed: no reply to ACTION_FLUSH from handler.")
        if reply[0] == 0 and reply[1] != ERROR_ACTION_NOT_KNOWN:
            raise SystemExit(
                f"Format failed: handler refused ACTION_FLUSH (error code {reply[1]})")
        # "Format complete" promises the new volume is on stable storage.
        try:
            bridge.backend.sync()
            bridge.backend.fsync()
        except OSError as exc:
            raise SystemExit(f"Format failed: {exc}") from exc

        if adf_info is not None:
            print(f"Format complete. Volume '{volname}' created on {image}.")
        else:
            print(f"Format complete. Volume '{volname}' created on partition '{partition}'.")
    finally:
        try:
            if bridge is not None:
                # A failed close is reported, not raised. On success the
                # fsync above has already made the volume durable.
                _close_bridge(bridge)
        finally:
            # Every exit removes the driver extracted from the RDB, also one
            # whose bridge fails to close. A failed removal is reported, but
            # does not replace the message the format exits with.
            if temp_driver is not None:
                try:
                    temp_driver.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"[amifuse] could not remove {temp_driver}: {exc}",
                          file=sys.stderr)


def _json_error(command: str, code: str, message: str, details: dict = None) -> dict:
    """Build a JSON error envelope."""
    result = {
        "status": "error",
        "command": command,
        "version": __version__,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if details:
        result["error"]["details"] = details
    return result


def _json_result(command: str, **kwargs) -> dict:
    """Build a JSON success envelope."""
    result = {
        "status": "ok",
        "command": command,
        "version": __version__,
    }
    result.update(kwargs)
    return result


def _mount_error_details(state: "MountState") -> Dict:
    """Machine-readable details for a NOT_MOUNTED error envelope.

    The two ways a volume can be rejected leave different evidence, and each
    reports what it actually has. An InfoData reply carries the disk type the
    handler named; a refusal carries only the res2 code that produced the
    verdict. Reporting the res2 keeps the refusal branch machine-readable
    instead of leaving prose in `message` as a consumer's only handle -- both
    branches can yield the same reason string, so the distinction would
    otherwise be invisible.

    Every key is read defensively: this feeds an error path, and a KeyError
    here would be swallowed by the caller's blanket `except Exception` and
    reported as HANDLER_ERROR, hiding the diagnosis it was trying to give.
    """
    details = {}
    if state.info is not None:
        for key in ("disk_type", "volume_node"):
            if key in state.info:
                details[key] = state.info[key]
    if state.dos_error is not None:
        details["dos_error"] = state.dos_error
    return details


def _mount_state_details(state: "MountState") -> Dict:
    """Mount context for an error envelope raised on a mounted-enough volume.

    A path that is missing on a volume whose mount could not be confirmed is
    a different situation from one missing on a healthy volume, and the two
    otherwise emit byte-identical envelopes.
    """
    details = {"filesystem_responsive": state.mounted}
    if state.mounted is None:
        details["mount_state_reason"] = state.reason
    return details


def _refuse_crashed_handler(bridge, command: str, use_json: bool) -> None:
    """Exit with status 1 when the filesystem handler has crashed.

    A crashed handler answers no packet, so its disk info reads as "mount
    state unknown", a directory as empty and every path as missing. Without
    this check a file command reports a crash as an empty volume or a
    missing file.
    """
    import json

    if not _handler_has_crashed(bridge):
        return
    if use_json:
        print(json.dumps(_json_error(
            command, "HANDLER_ERROR", "Filesystem handler crashed")))
        raise SystemExit(1)
    raise SystemExit("Error: filesystem handler crashed")


def _check_file_command_mount(bridge, command: str, use_json: bool) -> MountState:
    """Reject unusable volumes before resolving or creating a file."""
    import json

    state = bridge.is_mounted()
    # Checked after is_mounted(): the handler can crash on the disk info
    # packet itself.
    _refuse_crashed_handler(bridge, command, use_json)
    if state.mounted is False:
        if use_json:
            print(json.dumps(_json_error(
                command, "NOT_MOUNTED",
                f"No usable volume mounted: {state.reason}",
                details=_mount_error_details(state),
            )))
            raise SystemExit(1)
        raise SystemExit(f"Error: no usable volume mounted: {state.reason}")
    if state.mounted is None and not use_json:
        print(f"Warning: mount state unknown -- {state.reason}", file=sys.stderr)
    return state


def _cleanup_bridge(bridge, temp_driver=None):
    """Shut down a HandlerBridge and release all resources.

    Mirrors the cleanup pattern from destroy() -- each step is independent
    so failures don't cascade.

    Args:
        bridge: HandlerBridge instance (or None, in which case only
            temp_driver cleanup is attempted).
        temp_driver: Optional Path to a temp driver file created by
            extract_embedded_driver(). Deleted after bridge shutdown.
    """
    if bridge is not None:
        try:
            shutdown = getattr(getattr(bridge, "vh", None), "shutdown", None)
            if shutdown is not None:
                shutdown()
        except Exception:
            pass
        try:
            backend = getattr(bridge, "backend", None)
            if backend is not None:
                backend.sync()
        except Exception:
            pass
        try:
            backend = getattr(bridge, "backend", None)
            if backend is not None:
                backend.close()
        except Exception:
            pass
    if temp_driver is not None:
        try:
            temp_driver.unlink(missing_ok=True)
        except Exception:
            pass


def _refuse_unsendable_path_args(args, command: str):
    """Exit with INVALID_ARGUMENT when the volume path a file command takes
    (`--file`, or `ls`'s `--path`) is one the handler can't take as typed.

    Sent as typed, a colon or a character outside Latin-1 names another file
    (#257, #258): `write --file T/draft:notes.txt` would empty T/notes.txt.
    """
    import json as _json

    for name in ("file", "path"):
        path = getattr(args, name, None)
        if not isinstance(path, str):
            continue
        reason = _unsendable_path_reason(path)
        if reason is None:
            continue
        message = f"Invalid Amiga path: {path}: {reason}"
        if getattr(args, "json", False):
            print(_json.dumps(_json_error(command, "INVALID_ARGUMENT", message)))
            sys.exit(1)
        raise SystemExit(f"Error: {message[0].lower()}{message[1:]}")


def _create_bridge_from_args(args, command: str, read_only: bool = True):
    """Create a HandlerBridge from CLI arguments.

    Refuses a volume path the handler can't take as typed first, then
    handles driver resolution (RDB extraction or explicit --driver),
    ADF/ISO detection, and JSON error reporting.

    Args:
        args: Parsed argparse namespace. Must have: image, partition, driver,
              block_size, debug. May have: json.
        command: Command name for error envelopes.
        read_only: Whether the bridge should be read-only (default True).
            Write commands (e.g. 'write') pass False to enable modifications.

    Returns:
        Tuple of (HandlerBridge, temp_driver_path_or_None). The caller must
        call _cleanup_bridge(bridge, temp_driver) when done.

    Raises:
        SystemExit on error (with JSON error if args.json is True).
    """
    import json as _json
    from .rdb_inspect import detect_adf, detect_iso

    _refuse_unsendable_path_args(args, command)

    use_json = getattr(args, "json", False)
    image = args.image

    # Validate image exists
    if not is_windows_disk(image) and not image.exists():
        if use_json:
            print(_json.dumps(_json_error(command, "IMAGE_NOT_FOUND",
                f"Image file not found: {image}")))
            sys.exit(1)
        raise SystemExit(f"Error: image file not found: {image}")

    # Detect image type
    adf_info = detect_adf(image)
    iso_info = None
    driver = getattr(args, "driver", None)
    partition = getattr(args, "partition", None)
    block_size = getattr(args, "block_size", None)
    debug = getattr(args, "debug", False)
    temp_driver = None

    if adf_info is None:
        iso_info = detect_iso(image)

    try:
        resolved = resolve_driver(
            image, driver, block_size, partition, adf_info=adf_info, iso_info=iso_info)
    except IOError as e:
        names = _entry_partitions(e, image, block_size)
        if use_json:
            print(_json.dumps(_json_error(
                command, "IMAGE_INVALID", str(e),
                details={"partitions": [label for label, _ in names]} if names else None)))
            sys.exit(1)
        raise SystemExit(f"Error: {e}{_entry_partitions_hint(names)}")
    except DriverNotFound as e:
        msg = " ".join(str(e).splitlines())
        if use_json:
            print(_json.dumps(_json_error(command, "DRIVER_NOT_FOUND", msg)))
            sys.exit(1)
        raise SystemExit(msg)
    driver = resolved.path
    if resolved.temporary:
        temp_driver = resolved.path

    # Create the bridge
    try:
        bridge = HandlerBridge(
            image,
            driver,
            driver_name=resolved.display_name,
            block_size=block_size,
            read_only=read_only,
            debug=debug,
            partition=partition,
            adf_info=adf_info,
            iso_info=iso_info,
            remove_driver=resolved.temporary,
        )
    except SystemExit as e:
        if temp_driver is not None:
            try:
                temp_driver.unlink(missing_ok=True)
            except Exception:
                pass
        if use_json:
            if isinstance(e, StartupDiskRejected):
                print(_json.dumps(_json_error(
                    command, "NOT_MOUNTED", f"No usable volume mounted: {e.reason}",
                    details={"dos_error": e.res2},
                )))
            else:
                print(_json.dumps(_json_error(command, "HANDLER_ERROR", str(e))))
            sys.exit(1)
        raise
    except Exception as e:
        if temp_driver is not None:
            try:
                temp_driver.unlink(missing_ok=True)
            except Exception:
                pass
        # A busy image is not a handler fault; the handler never started.
        in_use = isinstance(e, ImageInUseError)
        # With --driver, the bridge is the first to open the image.
        names = _entry_partitions(e, image, block_size)
        if use_json:
            if in_use:
                error = _json_error(command, "IMAGE_IN_USE", str(e))
            else:
                error = _json_error(command, "HANDLER_ERROR",
                    f"Failed to initialize filesystem handler: {e}",
                    details={"partitions": [label for label, _ in names]} if names else None)
            print(_json.dumps(error))
            sys.exit(1)
        if in_use:
            raise SystemExit(f"Error: {e}")
        raise SystemExit(
            f"Error initializing handler: {e}{_entry_partitions_hint(names)}")

    return bridge, temp_driver


def _format_protection(prot_bits: int) -> str:
    """Format Amiga protection bits as a human-readable string."""
    prot = DosProtection(prot_bits)
    return str(prot)


def _not_walked_reason(ent):
    """Why _ls_recursive lists directory entry `ent` but stays out of it, or None.

    A link may lead back to a directory the walk is inside, so it would never
    end. The handler follows a hard link, and refuses a soft link in a path.
    The names come from the disk, where a colon or a slash can be: a slash
    would split the name, and `q:` is the root of the volume, which holds
    `q:` again.
    """
    if ent.get("dir_type") == 3:  # ST_SOFTLINK
        return "it is a soft link"
    if ent.get("dir_type") == 4:  # ST_LINKDIR
        return "it is a hard link to a directory"
    if "/" in ent["name"]:
        return "a slash separates AmigaDOS path components"
    return _unsendable_path_reason(ent["name"])


def _listing_of(raw):
    """What tells directory listing `raw` from those above it, or None.

    A damaged directory block can lead back to a directory that holds it,
    and nothing in the entry says so. fib_DiskKey alone can't tell, as
    pfs3aio and ODFileSystem give one key to many directories. With the rest
    of the listing it can detect repeated visits. A directory inside another
    can also list the same entries, so the walk allows one repeat before
    stopping. This is a heuristic: three identical listings along a sound
    chain are indistinguishable from a loop. Siblings are checked separately.
    No key says nothing, and an empty directory leads nowhere.
    """
    if not raw or not all(ent.get("disk_key") for ent in raw):
        return None
    return tuple(
        (ent["disk_key"], ent["name"], ent.get("dir_type"), ent.get("size"),
         ent.get("protection"), ent.get("mtime"))
        for ent in raw
    )


def _ls_recursive(bridge, root_path: str) -> list:
    """Recursively list all entries under root_path.

    A directory it can't go into is listed, without its contents, and named
    on stderr. So is one whose listing repeats twice above it.
    """
    entries = []
    stack = [(root_path, ())]
    while stack:
        current, above = stack.pop()
        raw = bridge.list_dir_path(current)
        listing = _listing_of(raw)
        if above.count(listing) >= 2:
            print(f"Warning: contents of {escape_text(current)} not listed (it leads back "
                  "to a directory above it, so the volume may be damaged)",
                  file=sys.stderr)
            continue
        if listing is not None:
            above = above + (listing,)
        dir_children = []
        for ent in raw:
            is_dir = ent.get("dir_type", 0) > 0
            child = "/" + ent["name"] if current == "/" else current + "/" + ent["name"]
            prot_bits = ent.get("protection", 0)
            prot_str = _format_protection(prot_bits)
            entries.append({
                "name": ent["name"],
                "path": child,
                "type": "dir" if is_dir else "file",
                "size": ent.get("size", 0),
                "protection": prot_str,
                "protection_bits": prot_bits,
            })
            if is_dir:
                reason = _not_walked_reason(ent)
                if reason is None:
                    dir_children.append(child)
                else:
                    print(f"Warning: contents of {escape_text(child)} not listed ({reason})",
                          file=sys.stderr)
        # Traverse in sorted order for deterministic output
        stack.extend((child, above) for child in reversed(sorted(dir_children)))
    return entries


def cmd_ls(args):
    """Handle the 'ls' subcommand."""
    import json

    use_json = getattr(args, "json", False)
    path = getattr(args, "path", "/")
    recursive = getattr(args, "recursive", False)

    # Normalize path (strip trailing slash, ensure leading slash)
    path = "/" + path.strip("/")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    bridge, temp_driver = _create_bridge_from_args(args, "ls")
    try:
        # A handler that rejected the disk still answers directory reads with
        # an empty result. The stat fallback below would catch that, but it is
        # skipped at the root (path != "/"), and -R bypasses it entirely --
        # so without this guard the default `amifuse ls image.hdf` reports an
        # unmountable image as an empty volume and exits 0.
        state = bridge.is_mounted()
        _refuse_crashed_handler(bridge, "ls", use_json)
        mounted, mount_reason = state.mounted, state.reason
        if mounted is False:
            if use_json:
                print(json.dumps(_json_error(
                    "ls", "NOT_MOUNTED",
                    f"No usable volume mounted: {mount_reason}",
                    details=_mount_error_details(state),
                )))
                sys.exit(1)
            raise SystemExit(
                f"Error: no usable volume mounted: {mount_reason}")

        if recursive:
            entries = _ls_recursive(bridge, path)
        else:
            raw = bridge.list_dir_path(path)
            if not raw and path != "/":
                # Path might not exist -- try stat to distinguish
                stat = bridge.stat_path(path)
                _refuse_crashed_handler(bridge, "ls", use_json)
                if stat is None:
                    # On an unmountable volume no path resolves, so this
                    # branch is reachable for exactly the condition the check
                    # above exists for. Carry the mount state, or a missing
                    # directory on a healthy volume looks identical.
                    details = _mount_state_details(state)
                    if use_json:
                        print(json.dumps(_json_error("ls", "FILE_NOT_FOUND",
                            f"Path not found: {path}", details=details)))
                        sys.exit(1)
                    if mounted is None:
                        print(f"Warning: mount state unknown -- {mount_reason}",
                              file=sys.stderr)
                    raise SystemExit(f"Error: path not found: {path}")
            entries = []
            for ent in raw:
                is_dir = ent.get("dir_type", 0) > 0
                child = "/" + ent["name"] if path == "/" else path + "/" + ent["name"]
                prot_bits = ent.get("protection", 0)
                prot_str = _format_protection(prot_bits)
                entries.append({
                    "name": ent["name"],
                    "path": child,
                    "type": "dir" if is_dir else "file",
                    "size": ent.get("size", 0),
                    "protection": prot_str,
                    "protection_bits": prot_bits,
                })

        # A crash during the listing cuts it short.
        _refuse_crashed_handler(bridge, "ls", use_json)
        if use_json:
            result = _json_result("ls",
                target=str(args.image),
                path=path,
                entries=entries,
                filesystem_responsive=mounted,
            )
            print(json.dumps(result, indent=2))
        else:
            for ent in entries:
                name = escape_text(ent["name"])
                if ent["type"] == "dir":
                    print(f"  {name:30s}  <dir>     {ent['protection']}")
                else:
                    print(f"  {name:30s}  {ent['size']:>10d}  {ent['protection']}")
            # An empty listing that we could not confirm is a real listing
            # looks identical to an empty volume. Say so, on stderr, so the
            # listing itself stays clean for anything parsing stdout.
            if not entries and mounted is None:
                print(f"Warning: mount state unknown -- {mount_reason}",
                      file=sys.stderr)
    except SystemExit:
        raise
    except Exception as e:
        if use_json:
            print(json.dumps(_json_error("ls", "HANDLER_ERROR",
                f"Directory listing failed: {e}")))
            sys.exit(1)
        raise SystemExit(f"Error during directory listing: {e}")
    finally:
        _cleanup_bridge(bridge, temp_driver)


def cmd_verify(args):
    """Handle the 'verify' subcommand."""
    import json

    use_json = getattr(args, "json", False)
    file_path = getattr(args, "file", None)
    expect_size = getattr(args, "expect_size", None)

    # Warn if --expect-size used without --file (it has no meaning alone)
    if expect_size is not None and file_path is None:
        if use_json:
            print(json.dumps(_json_error("verify", "INVALID_ARGUMENT",
                "--expect-size requires --file")))
            sys.exit(1)
        raise SystemExit("Error: --expect-size requires --file")

    try:
        bridge, temp_driver = _create_bridge_from_args(args, "verify")
    except StartupDiskRejected as e:
        # Match the report for a disk refused after startup. JSON mode has
        # already been answered by _create_bridge_from_args.
        print(f"File: {file_path}" if file_path else "Volume: (none)")
        print(f"  Filesystem responsive: NO -- {e.reason}")
        raise
    try:
        # Reject a disk that the handler conclusively identified as unusable
        # before either verification path can manufacture a successful result.
        state = bridge.is_mounted()
        _refuse_crashed_handler(bridge, "verify", use_json)
        mounted, mount_reason = state.mounted, state.reason
        if mounted is False:
            if use_json:
                print(json.dumps(_json_error(
                    "verify", "NOT_MOUNTED",
                    f"No usable volume mounted: {mount_reason}",
                    details=_mount_error_details(state),
                )))
                sys.exit(1)
            # Keep the diagnosis attached to what the user actually asked
            # about, so --file does not get a bare volume-shaped report.
            if file_path:
                print(f"File: {file_path}")
            else:
                print("Volume: (none)")
            print(f"  Filesystem responsive: NO -- {mount_reason}")
            if state.info is not None and "disk_type" in state.info:
                print(f"  id_DiskType: 0x{state.info['disk_type']:08x}")
            # The report belongs on stdout, but every other error path in
            # this file puts its reason on stderr. Echo it so that
            # `verify image.hdf 2>&1 >/dev/null` is not silent for exactly
            # the condition this check exists to surface.
            print(f"Error: no usable volume mounted: {mount_reason}",
                  file=sys.stderr)
            sys.exit(1)

        responsive_text = "yes"
        if mounted is None:
            responsive_text = f"unknown -- {mount_reason}"

        if file_path:
            # Verify a specific file
            normalized = "/" + file_path.lstrip("/")
            stat = bridge.stat_path(normalized)
            _refuse_crashed_handler(bridge, "verify", use_json)
            if stat is None:
                # A missing file on a volume whose mount could not be
                # confirmed is a different situation from a missing file on
                # a healthy volume. Carry the mount state so the error path
                # stays as informative as the success path below.
                details = _mount_state_details(state)
                if use_json:
                    print(json.dumps(_json_error("verify", "FILE_NOT_FOUND",
                        f"File not found: {file_path}", details=details)))
                    sys.exit(1)
                print(f"File: {file_path}")
                print(f"  Exists: no")
                print(f"  Filesystem responsive: {responsive_text}")
                raise SystemExit(f"Error: file not found: {file_path}")

            result_data = {
                "target": str(args.image),
                "file": file_path,
                "exists": True,
                "size": stat.get("size", 0),
                "type": "dir" if stat.get("dir_type", 0) > 0 else "file",
                "filesystem_responsive": mounted,
            }
            if expect_size is not None:
                result_data["expected_size"] = expect_size
                result_data["size_matches"] = stat.get("size", 0) == expect_size

            if use_json:
                print(json.dumps(_json_result("verify", **result_data), indent=2))
            else:
                print(f"File: {file_path}")
                print(f"  Exists: yes")
                print(f"  Type: {result_data['type']}")
                print(f"  Size: {stat.get('size', 0)}")
                print(f"  Filesystem responsive: {responsive_text}")
                if expect_size is not None:
                    match = "yes" if result_data["size_matches"] else "NO"
                    print(f"  Expected size: {expect_size} ({match})")
        else:
            # Verify volume -- count files and dirs
            total_files = 0
            total_dirs = 0
            total_size = 0
            entries = _ls_recursive(bridge, "/")
            for ent in entries:
                if ent["type"] == "dir":
                    total_dirs += 1
                else:
                    total_files += 1
                    total_size += ent.get("size", 0)

            volname = bridge.volume_name()
            # A crash during the walk cuts the counts short.
            _refuse_crashed_handler(bridge, "verify", use_json)
            result_data = {
                "target": str(args.image),
                "volume": volname,
                "total_dirs": total_dirs,
                "total_files": total_files,
                "total_size_bytes": total_size,
                "filesystem_responsive": mounted,
            }

            if use_json:
                print(json.dumps(_json_result("verify", **result_data), indent=2))
            else:
                print(f"Volume: {escape_text(volname)}")
                print(f"  Directories: {total_dirs}")
                print(f"  Files: {total_files}")
                print(f"  Total size: {total_size:,} bytes")
                print(f"  Filesystem responsive: {responsive_text}")
    except SystemExit:
        raise
    except Exception as e:
        if use_json:
            print(json.dumps(_json_error("verify", "HANDLER_ERROR",
                f"Verification failed: {e}")))
            sys.exit(1)
        raise SystemExit(f"Error during verification: {e}")
    finally:
        _cleanup_bridge(bridge, temp_driver)


def cmd_hash(args):
    """Handle the 'hash' subcommand."""
    import hashlib
    import json

    use_json = getattr(args, "json", False)
    file_path = args.file
    algorithm = getattr(args, "algorithm", "sha256")

    # Validate algorithm
    supported = ("md5", "sha1", "sha256")
    if algorithm not in supported:
        if use_json:
            print(json.dumps(_json_error("hash", "INVALID_ARGUMENT",
                f"Unsupported algorithm: {algorithm}. Use one of: {', '.join(supported)}")))
            sys.exit(1)
        raise SystemExit(
            f"Unsupported hash algorithm: {algorithm}. "
            f"Supported: {', '.join(supported)}")

    bridge, temp_driver = _create_bridge_from_args(args, "hash")
    mount_details = None
    try:
        state = _check_file_command_mount(bridge, "hash", use_json)
        mount_details = _mount_state_details(state)
        normalized = "/" + file_path.lstrip("/")
        stat = bridge.stat_path(normalized)
        _refuse_crashed_handler(bridge, "hash", use_json)
        if stat is None:
            if use_json:
                print(json.dumps(_json_error("hash", "FILE_NOT_FOUND",
                    f"File not found: {file_path}", details=_mount_state_details(state))))
                sys.exit(1)
            raise SystemExit(f"Error: file not found: {file_path}")

        if stat.get("dir_type", 0) > 0:
            if use_json:
                print(json.dumps(_json_error("hash", "INVALID_ARGUMENT",
                    f"Cannot hash a directory: {file_path}", details=_mount_state_details(state))))
                sys.exit(1)
            raise SystemExit(f"Error: cannot hash a directory: {file_path}")

        file_size = stat.get("size", 0)

        # Read file in chunks and compute hash
        h = hashlib.new(algorithm)
        fh_result = bridge.open_file(normalized)
        if fh_result is None:
            if use_json:
                print(json.dumps(_json_error("hash", "HANDLER_ERROR",
                    f"Failed to open file: {file_path}", details=_mount_state_details(state))))
                sys.exit(1)
            raise SystemExit(f"Error: failed to open file: {file_path}")

        fh_addr, _dir_lock = fh_result
        try:
            chunk_size = IO_CHUNK
            bytes_read = 0
            # Seek to start once, then use sequential read_handle() calls.
            # read_handle() reads from the current file position without
            # seeking -- much more efficient than read_handle_at() which
            # does an absolute seek before every read.
            bridge.seek_handle(fh_addr, 0)
            while bytes_read < file_size:
                to_read = min(chunk_size, file_size - bytes_read)
                data = bridge.read_handle(fh_addr, to_read)
                if data is None:
                    if use_json:
                        print(json.dumps(_json_error("hash", "HANDLER_ERROR",
                            f"Handler failed to read {file_path} at byte {bytes_read}",
                            details=mount_details)))
                        sys.exit(1)
                    raise SystemExit(
                        f"Error: handler failed to read {file_path} at byte {bytes_read}")
                if not data:
                    break
                h.update(data)
                bytes_read += len(data)
            if bytes_read != file_size:
                raise OSError(
                    f"Incomplete read of {file_path}: expected {file_size} "
                    f"bytes, got {bytes_read}"
                )
        finally:
            bridge.close_file(fh_addr)

        hash_hex = h.hexdigest()

        if use_json:
            result = _json_result("hash",
                target=str(args.image),
                file=file_path,
                size=file_size,
                bytes_read=bytes_read,
                algorithm=algorithm,
                hash=hash_hex,
                filesystem_responsive=state.mounted,
            )
            print(json.dumps(result, indent=2))
        else:
            print(f"File: {file_path}")
            print(f"  Size: {file_size}")
            print(f"  {algorithm}: {hash_hex}")
    except SystemExit:
        raise
    except Exception as e:
        if use_json:
            print(json.dumps(_json_error("hash", "HANDLER_ERROR",
                f"Hash computation failed: {e}", details=mount_details)))
            sys.exit(1)
        raise SystemExit(f"Error computing hash: {e}")
    finally:
        _cleanup_bridge(bridge, temp_driver)


def _ensure_parent_dirs(bridge, path: str, use_json: bool = False,
                        debug: bool = False, error_details=None):
    """Create parent directories for path if they don't exist.

    Walks each path component and creates missing directories via
    create_dir(). Similar to 'mkdir -p' behavior.

    Args:
        bridge: HandlerBridge instance with write enabled.
        path: Absolute Amiga path (e.g. "/S/User-Startup").
        use_json: If True, emit JSON errors instead of raising SystemExit.
        debug: If True, print debug messages for directory creation.
        error_details: Mount context to include in JSON errors.

    Raises:
        SystemExit on error if use_json is False.
    """
    import json as _json

    parts = [p for p in path.split("/") if p]
    if len(parts) <= 1:
        return  # No parent dirs needed (file is in root)

    # Check/create each directory component (all except the last, which is the file)
    for i in range(1, len(parts)):
        dir_path = "/" + "/".join(parts[:i])
        stat = bridge.stat_path(dir_path)
        if stat is not None:
            # Already exists -- verify it's a directory
            if stat.get("dir_type", 0) <= 0:
                msg = f"Path component is a file, not a directory: {dir_path}"
                if use_json:
                    print(_json.dumps(_json_error("write", "HANDLER_ERROR", msg, error_details)))
                    sys.exit(1)
                raise SystemExit(f"Error: {msg}")
            continue

        # Need to create this directory
        parent = "/" + "/".join(parts[:i-1]) if i > 1 else "/"
        parent_lock, _, locks = bridge.locate_path(parent)
        if parent == "/" and parent_lock == 0:
            parent_lock, _ = bridge.locate(0, "")
            if parent_lock:
                locks.append(parent_lock)
        if parent_lock == 0 and parent != "/":
            msg = f"Cannot locate parent directory: {parent}"
            if use_json:
                print(_json.dumps(_json_error("write", "HANDLER_ERROR", msg, error_details)))
                sys.exit(1)
            raise SystemExit(f"Error: {msg}")

        try:
            # create_dir(0, name) is valid when parent is root ("/").
            # locate_path("/") returns lock=0 (empty parts list), and the
            # handler interprets lock=0 as the volume root. This is the same
            # pattern used by FUSE mkdir() in AmigaFuseOps.
            new_lock, res2 = bridge.create_dir(parent_lock, parts[i-1])
            if new_lock == 0:
                msg = f"Failed to create directory: {dir_path} (error {res2})"
                if use_json:
                    print(_json.dumps(_json_error("write", "HANDLER_ERROR", msg, error_details)))
                    sys.exit(1)
                raise SystemExit(f"Error: {msg}")
            # Debug logging matching codebase pattern
            if debug:
                print(f"[amifuse] Created directory: {dir_path}", flush=True)
            bridge.free_lock(new_lock)
        finally:
            for l in reversed(locks):
                bridge.free_lock(l)


def cmd_read(args):
    """Handle the 'read' subcommand."""
    import json

    use_json = getattr(args, "json", False)
    file_path = args.file
    out_path = getattr(args, "out", None)
    temp_output = None

    # Default output name = basename of Amiga path
    if out_path is None:
        out_path = os.path.basename(file_path)
    stdout_mode = (out_path == "-")

    # Reject --out - with --json: binary data on stdout followed by
    # JSON text is unparseable. Use --out <path> to separate file data
    # from JSON output.
    if stdout_mode and use_json:
        print(json.dumps(_json_error("read", "STDOUT_JSON_CONFLICT",
            "Cannot use --out - with --json; use --out <path> to separate "
            "file data from JSON output.")))
        sys.exit(1)

    bridge, temp_driver = _create_bridge_from_args(args, "read")
    mount_details = None
    try:
        state = _check_file_command_mount(bridge, "read", use_json)
        mount_details = _mount_state_details(state)
        normalized = "/" + file_path.lstrip("/")
        stat = bridge.stat_path(normalized)
        _refuse_crashed_handler(bridge, "read", use_json)
        if stat is None:
            if use_json:
                print(json.dumps(_json_error("read", "FILE_NOT_FOUND",
                    f"File not found: {file_path}", details=_mount_state_details(state))))
                sys.exit(1)
            raise SystemExit(f"Error: file not found: {file_path}")

        if stat.get("dir_type", 0) > 0:
            if use_json:
                print(json.dumps(_json_error("read", "IS_DIRECTORY",
                    f"Cannot read a directory: {file_path}", details=_mount_state_details(state))))
                sys.exit(1)
            raise SystemExit(f"Error: cannot read a directory: {file_path}")

        file_size = stat.get("size", 0)

        # Open file for reading
        fh_result = bridge.open_file(normalized)
        if fh_result is None:
            if use_json:
                print(json.dumps(_json_error("read", "HANDLER_ERROR",
                    f"Failed to open file: {file_path}", details=_mount_state_details(state))))
                sys.exit(1)
            raise SystemExit(f"Error: failed to open file: {file_path}")

        # Note: _dir_lock from open_file() is never freed. This is a
        # pre-existing pattern also present in cmd_hash().
        # The lock is released when the bridge is destroyed.
        fh_addr, _dir_lock = fh_result
        try:
            chunk_size = IO_CHUNK
            bytes_read = 0
            # Seek to start once, then use sequential read_handle() calls.
            bridge.seek_handle(fh_addr, 0)

            if stdout_mode:
                out_fd = sys.stdout.buffer
            else:
                try:
                    # Follow symlinks as open(..., "wb") did. Stage regular
                    # files beside the destination for an atomic replacement;
                    # special outputs such as /dev/null remain streams.
                    destination = Path(out_path).resolve()
                    if destination.exists() and not destination.is_file():
                        out_fd = open(destination, "wb")
                    else:
                        if destination.exists() and not os.access(destination, os.W_OK):
                            raise PermissionError(f"Output is not writable: {destination}")
                        # A plain file beside the destination is created like
                        # the output itself: the umask applies on POSIX and the
                        # directory's inherited ACL on Windows. os.replace keeps
                        # those, so a private staging directory would not do.
                        staged = destination.with_name(
                            f".amifuse-{os.urandom(8).hex()}.tmp")
                        try:
                            out_fd = open(staged, "xb")
                        except OSError as e:
                            # Name the directory, not a path the user never
                            # asked for. Even an existing writable output
                            # needs a new entry in its directory.
                            where = f"cannot stage output in {destination.parent}"
                            cause = e.strerror or str(e)
                            if isinstance(e, PermissionError):
                                message = f"{where} (directory not writable): {cause}"
                            else:
                                message = f"{where}: {cause}"
                            raise type(e)(message) from e
                        temp_output = staged
                except OSError as e:
                    if use_json:
                        print(json.dumps(_json_error("read", "HANDLER_ERROR",
                            f"Cannot create output file: {e}")))
                        sys.exit(1)
                    raise SystemExit(f"Error: cannot create output file: {e}")

            try:
                while bytes_read < file_size:
                    to_read = min(chunk_size, file_size - bytes_read)
                    data = bridge.read_handle(fh_addr, to_read)
                    if data is None:
                        if use_json:
                            print(json.dumps(_json_error("read", "HANDLER_ERROR",
                                f"Handler failed to read {file_path} at byte {bytes_read}",
                                details=mount_details)))
                            sys.exit(1)
                        raise SystemExit(
                            f"Error: handler failed to read {file_path} at byte {bytes_read}")
                    if not data:
                        break
                    out_fd.write(data)
                    bytes_read += len(data)
                if bytes_read != file_size:
                    raise OSError(
                        f"Incomplete read of {file_path}: expected {file_size} "
                        f"bytes, got {bytes_read}"
                    )
            finally:
                if not stdout_mode:
                    out_fd.close()
        finally:
            bridge.close_file(fh_addr)

        if temp_output is not None:
            if destination.exists():
                os.chmod(temp_output, destination.stat().st_mode & 0o777)
            os.replace(temp_output, destination)
            temp_output = None

        if use_json:
            result = _json_result("read",
                target=str(args.image),
                file=file_path,
                size=file_size,
                bytes_read=bytes_read,
                output="-" if stdout_mode else out_path,
                filesystem_responsive=state.mounted,
            )
            print(json.dumps(result, indent=2))
        else:
            if stdout_mode:
                # Don't print metadata when outputting to stdout --
                # the file data is the only output.
                pass
            else:
                print(f"Extracted: {file_path}")
                print(f"  Size: {file_size}")
                print(f"  Bytes read: {bytes_read}")
                print(f"  Output: {out_path}")
    except SystemExit:
        raise
    except Exception as e:
        if use_json:
            print(json.dumps(_json_error("read", "HANDLER_ERROR",
                f"File extraction failed: {e}", details=mount_details)))
            sys.exit(1)
        raise SystemExit(f"Error extracting file: {e}")
    finally:
        try:
            if temp_output is not None:
                try:
                    # Windows cannot delete a staged file that became
                    # read-only when the destination's mode was copied.
                    try:
                        os.chmod(temp_output, 0o600)
                    except FileNotFoundError:
                        pass
                    temp_output.unlink(missing_ok=True)
                except OSError as e:
                    print(f"Warning: cannot remove temporary output: {e}", file=sys.stderr)
        finally:
            _cleanup_bridge(bridge, temp_driver)


def _open_write_source(host_path: Path, use_json: bool):
    """Open the source of `amifuse write`, or exit with an argument error.

    Returns the open file and its size. Both come from the one handle the
    write loop reads, so a path that is replaced afterwards is not copied
    in its place. Only a regular file is accepted. O_NONBLOCK keeps a FIFO
    from blocking the open, and is cleared again for the reads. O_NOCTTY
    keeps a terminal opened as the source from becoming this process's
    controlling terminal before it is refused.
    """
    import json
    import stat

    def refuse(code, message):
        if use_json:
            print(json.dumps(_json_error("write", code, message)))
            sys.exit(1)
        raise SystemExit(f"Error: {message[0].lower()}{message[1:]}")

    def unreadable(exc):
        refuse("INVALID_ARGUMENT",
               f"Cannot read source file: {host_path}: {exc.strerror or exc}")

    nonblock = getattr(os, "O_NONBLOCK", 0)
    flags = os.O_RDONLY | nonblock
    for name in ("O_BINARY", "O_NOCTTY"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(host_path, flags)
    except FileNotFoundError:
        refuse("SOURCE_NOT_FOUND", f"Source file not found: {host_path}")
    except OSError as exc:
        unreadable(exc)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            refuse("INVALID_ARGUMENT", f"Source is not a regular file: {host_path}")
        if nonblock:
            os.set_blocking(fd, True)
        # Read a byte too: a source another program has locked at its start,
        # or one whose data cannot be read, opens but fails on its first read.
        try:
            os.read(fd, 1)
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError as exc:
            unreadable(exc)
        return os.fdopen(fd, "rb"), st.st_size
    except BaseException:
        os.close(fd)
        raise


# `amifuse write` keeps a copy of its source up to this size in memory; a
# larger source is copied to a temporary file on the host's disk.
_WRITE_SPOOL_MAX = 16 * 1024 * 1024


def _spool_write_source(source, host_path: Path, host_size: int, use_json: bool):
    """Copy the whole source of `amifuse write` before the image is touched.

    Opening the destination empties an existing file. A source that fails
    part way through, one another program has locked from some offset say,
    would then leave the destination holding part of the new contents.

    Closes source, and returns the copy positioned at its start, or exits
    with an error.
    """
    import contextlib
    import json
    import tempfile

    def refuse(code, message, details=None):
        if use_json:
            print(json.dumps(_json_error("write", code, message, details)))
            sys.exit(1)
        raise SystemExit(f"Error: {message[0].lower()}{message[1:]}")

    def unspoolable(exc):
        refuse("HANDLER_ERROR",
               f"Write operation failed: cannot copy the source to a "
               f"temporary file: {exc.strerror or exc}")

    spool = tempfile.SpooledTemporaryFile(max_size=_WRITE_SPOOL_MAX)
    try:
        with source:
            size = 0
            while True:
                try:
                    data = source.read(IO_CHUNK)
                except OSError as exc:
                    refuse("INVALID_ARGUMENT",
                           f"Cannot read source file: {host_path}: {exc.strerror or exc}")
                if not data:
                    break
                size += len(data)
                if size > _MAX_FILE_SIZE:
                    # The source holds more than its fstat() size: it grew
                    # while it was read
                    refuse("FILE_TOO_LARGE",
                           f"Source holds more than {_MAX_FILE_SIZE} bytes, the "
                           f"largest AmigaDOS file: {host_path}",
                           {"size": host_size, "max_size": _MAX_FILE_SIZE})
                try:
                    spool.write(data)
                except OSError as exc:
                    unspoolable(exc)
        try:
            # A spool on disk writes out what it still buffers here.
            spool.seek(0)
        except OSError as exc:
            unspoolable(exc)
        return spool
    except BaseException:
        # A full disk leaves the failed bytes buffered, and close() fails
        # flushing them again; that must not replace the error above.
        with contextlib.suppress(OSError):
            spool.close()
        raise


# How `amifuse write` names the AmigaDOS error code of a failed write: the
# codes _WRITE_ERRNOS maps for the FUSE layer.
_WRITE_ERROR_REASONS = {
    ERROR_DISK_FULL: "disk full",
    ERROR_DISK_WRITE_PROTECTED: "disk is write-protected",
    ERROR_WRITE_PROTECTED: "file is write-protected",
}


def cmd_write(args):
    """Handle the 'write' subcommand."""
    import json

    use_json = getattr(args, "json", False)
    debug = getattr(args, "debug", False)
    amiga_path = args.file
    host_path = Path(args.input)  # --in maps to args.input (dest="input")

    # Before the source is read and the warning printed; the bridge checks again.
    _refuse_unsendable_path_args(args, "write")

    # Validate host source file. Both checks open the path to stat it, and
    # can fail: a Windows named pipe with no free instance fails is_dir().
    try:
        exists = host_path.exists()
        is_dir = exists and host_path.is_dir()
    except OSError as exc:
        message = f"Cannot read source file: {host_path}: {exc.strerror or exc}"
        if use_json:
            print(json.dumps(_json_error("write", "INVALID_ARGUMENT", message)))
            sys.exit(1)
        raise SystemExit(f"Error: {message[0].lower()}{message[1:]}")

    if not exists:
        if use_json:
            print(json.dumps(_json_error("write", "SOURCE_NOT_FOUND",
                f"Source file not found: {host_path}")))
            sys.exit(1)
        raise SystemExit(f"Error: source file not found: {host_path}")

    if is_dir:
        if use_json:
            print(json.dumps(_json_error("write", "INVALID_ARGUMENT",
                f"Source is a directory, not a file: {host_path}")))
            sys.exit(1)
        raise SystemExit(f"Error: source is a directory: {host_path}")

    # Refuse before the bridge exists: opening the destination with O_TRUNC
    # (ACTION_FINDOUTPUT) would already empty an existing file. So the
    # source is opened here, and a source that cannot be read fails first.
    source, host_size = _open_write_source(host_path, use_json)
    try:
        if host_size > _MAX_FILE_SIZE:
            if use_json:
                print(json.dumps(_json_error("write", "FILE_TOO_LARGE",
                    f"Source file is larger than {_MAX_FILE_SIZE} bytes, the "
                    f"largest AmigaDOS file: {host_path} ({host_size} bytes)",
                    {"size": host_size, "max_size": _MAX_FILE_SIZE})))
                sys.exit(1)
            raise SystemExit(
                f"Error: source file is larger than {_MAX_FILE_SIZE} bytes, the "
                f"largest AmigaDOS file: {host_path} ({host_size} bytes)")

        # From here on, source is a copy of the whole source file, so a read
        # error fails the command before the image is touched.
        source = _spool_write_source(source, host_path, host_size, use_json)

        # Safety warning (stderr so it doesn't interfere with JSON output)
        print(f"WARNING: This will modify the disk image: {args.image}", file=sys.stderr)
        print(f"  Source: {host_path} ({host_size:,} bytes)", file=sys.stderr)
        print(f"  Destination: {amiga_path}", file=sys.stderr)
        print(f"  Ensure you have a backup of the image before proceeding.", file=sys.stderr)

        bridge, temp_driver = _create_bridge_from_args(args, "write", read_only=False)
    except BaseException:
        source.close()
        raise
    flushed = False  # Guard flag to avoid double flush
    write_started = False
    mount_details = None
    # Added to a failure report while an existing destination, emptied when
    # it was opened, holds only part of the new contents.
    partial = ""
    try:
        state = _check_file_command_mount(bridge, "write", use_json)
        mount_details = _mount_state_details(state)
        normalized = "/" + amiga_path.lstrip("/")

        # Create parent directories if needed
        write_started = True
        _ensure_parent_dirs(bridge, normalized, use_json=use_json, debug=debug,
                            error_details=mount_details)

        # A write that fails removes a file it created. An existing file
        # cannot get its old contents back, and is kept. A lookup that gets
        # no clear answer counts as neither.
        lock, res2, locks = bridge.locate_path(normalized)
        for l in reversed(locks):
            bridge.free_lock(l)
        existed = lock != 0
        new_name = lock == 0 and res2 in _NOT_FOUND_ERRORS

        # Open for write (create/truncate)
        fh_result = bridge.open_file(normalized, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        if fh_result is None:
            if use_json:
                print(json.dumps(_json_error("write", "HANDLER_ERROR",
                    f"Failed to open file for writing: {amiga_path}", details=mount_details)))
                sys.exit(1)
            raise SystemExit(f"Error: failed to open file for writing: {amiga_path}")

        # Note: dir_lock from open_file() is never freed. This is a
        # pre-existing pattern also present in cmd_hash().
        # Not a blocker -- the lock is released when the bridge is destroyed.
        fh_addr, dir_lock = fh_result
        if existed:
            partial = (f"; the existing {amiga_path} is left partial, and its "
                       f"old contents are gone")
        completed = False
        try:
            # Handler crash safety: If the handler crashes during the
            # write loop, the error paths handle it gracefully:
            # - flush_volume() checks state.crashed and returns early (safe)
            # - close_file() sends a packet that gets no reply (harmless)
            # - write_handle_reply() returning (-1, 0) triggers the error path below
            chunk_size = IO_CHUNK
            bytes_written = 0
            with source as f:
                while True:
                    data = f.read(chunk_size)
                    if not data:
                        break
                    n, res2 = bridge.write_handle_reply(fh_addr, data)
                    # Distinguish write failure modes:
                    # n < 0, res2 != 0: the handler failed the write with
                    #   that AmigaDOS error code
                    # n < 0, res2 == 0: a failure with no error code, which
                    #   is almost always no reply from the handler (crashed
                    #   or unresponsive)
                    # 0 <= n < len(data): Partial write; res2 names why if
                    #   the handler gave a code (a refusal with DOSFALSE
                    #   comes as 0 bytes and a code)
                    # n == len(data): Success
                    if n < 0:
                        details = {**mount_details, "bytes_written": bytes_written,
                                   "expected": host_size}
                        if res2:
                            reason = _WRITE_ERROR_REASONS.get(res2, f"error code {res2}")
                            details["dos_error"] = res2
                        else:
                            reason = "no reply from handler"
                        if use_json:
                            print(json.dumps(_json_error("write", "WRITE_ERROR",
                                f"Write failed at offset {bytes_written}: "
                                f"{reason}{partial}",
                                details)))
                            sys.exit(1)
                        raise SystemExit(
                            f"Error: write failed at offset {bytes_written}: "
                            f"{reason}{partial}")
                    if n < len(data):
                        # Partial write -- handler couldn't write all data
                        # (likely disk full). Record what was written then error.
                        bytes_written += n
                        details = {**mount_details, "bytes_written": bytes_written,
                                   "expected": host_size}
                        if res2:
                            reason = _WRITE_ERROR_REASONS.get(res2, f"error code {res2}")
                            details["dos_error"] = res2
                        else:
                            reason = "disk full?"
                        if use_json:
                            print(json.dumps(_json_error("write", "WRITE_ERROR",
                                f"Partial write: wrote {bytes_written} of "
                                f"{host_size} bytes ({reason}){partial}",
                                details)))
                            sys.exit(1)
                        raise SystemExit(
                            f"Error: partial write ({bytes_written}/{host_size} "
                            f"bytes, {reason}){partial}")
                    bytes_written += n
            completed = True
            partial = ""
        finally:
            bridge.close_file(fh_addr)
            if new_name and not completed:
                # The name was not there before: leave nothing under it.
                name = [p for p in normalized.split("/") if p][-1]
                try:
                    removed = bridge.delete_object(dir_lock, name)[0] != 0
                except Exception:
                    removed = False
                if not removed:
                    print(f"Warning: cannot remove the partial file {amiga_path}",
                          file=sys.stderr)

        # Flush is CRITICAL after writes -- without this, writes cached in
        # handler memory never reach the image file. One that fails gets no
        # second try below: it would wait for the same packets again.
        flushed = True  # Mark as flushed to avoid double flush
        bridge.flush_volume()

        if use_json:
            result = _json_result("write",
                target=str(args.image),
                file=amiga_path,
                source=str(host_path),
                size=host_size,
                bytes_written=bytes_written,
                filesystem_responsive=state.mounted,
            )
            print(json.dumps(result, indent=2))
        else:
            print(f"Written: {amiga_path}")
            print(f"  Source: {host_path}")
            print(f"  Size: {host_size}")
            print(f"  Bytes written: {bytes_written}")
    except SystemExit:
        raise
    except Exception as e:
        if use_json:
            print(json.dumps(_json_error("write", "HANDLER_ERROR",
                f"Write operation failed: {e}{partial}", details=mount_details)))
            sys.exit(1)
        raise SystemExit(f"Error during write: {e}{partial}")
    finally:
        # On error paths, we still want to flush what was written to
        # avoid losing partially-written data. But if flush_volume() already
        # succeeded on the success path, skip the redundant flush.
        if write_started and not flushed:
            try:
                bridge.flush_volume()
            except Exception:
                pass
        _cleanup_bridge(bridge, temp_driver)
        source.close()


def _partition_driver(image: Path, block_size: Optional[int],
                      name: Optional[str]):
    """Name the driver `mount --partition name` would use without --driver.

    Returns the driver's inspect --json object, or None, and a line of text:
    its description, or why there is none. An RDB driver is extracted to
    hash it, then deleted; its object has no path. A name of None is a
    damaged drive name (rdb_inspect.list_partitions).
    """
    import hashlib

    if name is None:
        # Without --partition a mount opens partition 0 of the disk's first
        # RDB, which need not be this one.
        return None, "damaged drive name: no --partition name selects it"
    try:
        resolved = resolve_driver(image, None, block_size, name)
    except (DriverNotFound, OSError) as exc:
        return None, " ".join(str(exc).splitlines())
    info = resolved.to_json()
    if resolved.temporary:
        try:
            data = resolved.path.read_bytes()
        except OSError as exc:
            return None, f"cannot read the driver extracted from the RDB: {exc}"
        finally:
            try:
                resolved.path.unlink()
            except OSError:
                pass
        info.update(path=None, version=driver_resolver.find_version(data),
                    sha256=hashlib.sha256(data).hexdigest())
    return info, resolved.description


def cmd_inspect(args):
    """Handle the 'inspect' subcommand."""
    import json as _json
    import amitools.fs.DosType as DosType
    from .rdb_inspect import (
        open_each_rdb, detect_adf, detect_iso, list_partitions,
        amiga_rdb_partitions, build_rdisk_desc, print_inspect_report,
        mount_names, DiskRDBError, rdb_label, find_partition,
    )

    use_json = getattr(args, "json", False)

    # Check that image exists (before any detection)
    if not is_windows_disk(args.image) and not args.image.exists():
        if use_json:
            envelope = _json_error("inspect", "IMAGE_NOT_FOUND",
                                   f"Image not found: {args.image}")
            envelope["image"] = str(args.image)
            print(_json.dumps(envelope, indent=2))
            sys.exit(1)
        raise SystemExit(f"Error: Image not found: {args.image}")

    # First check for ADF
    adf_info = detect_adf(args.image)
    if adf_info is not None:
        # Name the handler a mount without --driver would use.
        try:
            resolved = resolve_driver(args.image, None, None, None, adf_info=adf_info)
            driver_error = None
        except DriverNotFound as exc:
            resolved = None
            driver_error = str(exc)
        if use_json:
            dt_str = DosType.num_to_tag_str(adf_info.dos_type)
            floppy_type = "HD" if adf_info.is_hd else "DD"
            note = "ADF files don't contain embedded filesystem drivers."
            if driver_error:
                note += " " + " ".join(driver_error.splitlines()[1:])
            envelope = _json_result("inspect",
                image=str(args.image),
                image_type="adf",
                floppy_type=floppy_type,
                sectors_per_track=adf_info.sectors_per_track,
                cylinders=adf_info.cylinders,
                heads=adf_info.heads,
                block_size=adf_info.block_size,
                total_blocks=adf_info.total_blocks,
                dos_type=dt_str,
                dos_type_raw=f"0x{adf_info.dos_type:08x}",
                driver=resolved.to_json() if resolved else None,
                note=note,
            )
            print(_json.dumps(envelope, indent=2))
            return
        dt_str = DosType.num_to_tag_str(adf_info.dos_type)
        floppy_type = "HD" if adf_info.is_hd else "DD"
        print(f"ADF Floppy Image: {args.image}")
        print(f"  Type: {floppy_type} ({adf_info.sectors_per_track} sectors/track)")
        print(f"  Geometry: {adf_info.cylinders} cylinders, {adf_info.heads} heads")
        print(f"  Block size: {adf_info.block_size} bytes")
        print(f"  Total blocks: {adf_info.total_blocks}")
        print(f"  DOS type: {dt_str} (0x{adf_info.dos_type:08x})")
        print("\nNote: ADF files don't contain embedded filesystem drivers.")
        if resolved is not None:
            print(f"Filesystem driver: {resolved.description}")
        else:
            print("\n".join(driver_error.splitlines()[1:]))
        return

    # Check for ISO 9660
    iso_info = detect_iso(args.image)
    if iso_info is not None:
        if use_json:
            envelope = _json_result("inspect",
                image=str(args.image),
                image_type="iso",
                volume_id=iso_info.volume_id,
                block_size=iso_info.block_size,
                total_blocks=iso_info.total_blocks,
            )
            print(_json.dumps(envelope, indent=2))
            return
        print(f"ISO 9660 Image: {args.image}")
        print(f"  Volume ID: {escape_text(iso_info.volume_id)}")
        print(f"  Block size: {iso_info.block_size} bytes")
        print(f"  Total blocks: {iso_info.total_blocks}")
        return

    if use_json:
        # JSON mode: collect all RDBs into a single envelope. Unlike the
        # rdb-inspect tool (which skips unreadable RDBs), an open failure
        # is fatal here so scripts get a definitive error envelope. On a
        # disk with its own RDB beside the MBR, the failures of that RDB
        # and of entries holding one are notes, unless nothing opens.
        amiga_parts = amiga_rdb_partitions(args.image)
        all_rdbs = []
        rdb_parts = []
        notes = []
        first_error = None
        beside_mbr = False  # The disk has its own RDB beside the MBR.
        for rdb_idx, opened in open_each_rdb(
                args.image, args.block_size, len(amiga_parts)):
            if isinstance(opened, IOError):
                beside_mbr = beside_mbr or bool(
                    amiga_parts and isinstance(opened, DiskRDBError))
                first_error = first_error or opened
                if beside_mbr:
                    notes.append(f"{rdb_label(amiga_parts, rdb_idx)}: {opened}")
                    continue
                envelope = _json_error("inspect", "OPEN_FAILED", str(opened))
                envelope["image"] = str(args.image)
                print(_json.dumps(envelope, indent=2))
                sys.exit(1)
            blkdev, rdisk, mbr_ctx = opened
            beside_mbr = beside_mbr or (
                mbr_ctx is not None and mbr_ctx.mbr_partition is None)
            try:
                all_rdbs.append(build_rdisk_desc(rdisk, mbr_ctx))
                rdb_parts.append(list(rdisk.parts))
            finally:
                rdisk.close()
                blkdev.close()
        if not all_rdbs:
            envelope = _json_error("inspect", "OPEN_FAILED", str(first_error))
            envelope["image"] = str(args.image)
            print(_json.dumps(envelope, indent=2))
            sys.exit(1)

        # Resolved with the image closed: a Windows disk admits one reader.
        for desc, names in zip(all_rdbs, mount_names(rdb_parts)):
            for part, name in zip(desc["partitions"], names):
                if name not in (None, part["name"]):
                    # A repeated drive name, which mount --partition takes
                    # as AmigaOS names it. A damaged one has no such name.
                    part["mount_name"] = name
                part["driver"], text = _partition_driver(
                    args.image, args.block_size, name)
                if part["driver"] is None:
                    part["driver_note"] = text

        envelope = _json_result("inspect",
            image=str(args.image),
            image_type="rdb",
        )
        if len(all_rdbs) == 1:
            envelope.update(all_rdbs[0])
        else:
            envelope["rdbs"] = all_rdbs
        if notes:
            envelope["notes"] = notes
        print(_json.dumps(envelope, indent=2))
        return

    print_inspect_report(args.image, args.block_size, args.full)
    print("\nFilesystem driver per partition (without --driver):")
    # Names are unique: list_partitions renames a repeated one (DH0.1). A
    # damaged drive name has none, so the partition is labelled by its
    # number, as get_partition_info names it, but only if --partition takes
    # that number to it: a number selects the first RDB with a partition of
    # that number, which on a disk with more than one RDB may be another,
    # and a partition named as the number comes first on any disk. Otherwise
    # the partition is labelled by its RDB too, and the line says what the
    # number selects instead.
    amiga_parts = amiga_rdb_partitions(args.image)

    def rdb_name(rdb_idx):
        # The RDB yielded with index None comes first on every disk.
        return ("the first RDB" if rdb_idx is None else
                rdb_label(amiga_parts, rdb_idx))

    for part in list_partitions(args.image, args.block_size).partitions:
        text = _partition_driver(args.image, args.block_size, part.name)[1]
        if part.name is not None:
            print(f"  {escape_text(part.name)}: {text}")
            continue
        selector = f"--partition {part.index}"
        try:
            ref = find_partition(args.image, args.block_size, str(part.index))
        except OSError as exc:
            note = (f"cannot tell what {selector} selects: "
                    + " ".join(str(exc).splitlines()))
        else:
            if ref is not None and (ref.mbr_partition_index, ref.number) == (
                    part.mbr_partition_index, part.index):
                print(f"  #{part.index}: {text}")
                continue
            if ref is None:
                note = f"{selector} selects no partition"
            elif ref.name is not None:
                note = f"{selector} selects '{escape_text(ref.name)}' instead"
            else:
                note = (f"{selector} selects #{ref.number} in "
                        f"{rdb_name(ref.mbr_partition_index)} instead")
        print(f"  #{part.index} in {rdb_name(part.mbr_partition_index)}: "
              f"damaged drive name: {note}")


def cmd_mount(args):
    """Handle the 'mount' subcommand."""
    _validate_driver_path(args.driver)

    foreground = args.foreground
    if args.profile and foreground is None:
        foreground = True
    if args.profile and not foreground:
        raise SystemExit("--profile requires --interactive/--foreground.")

    if args.profile:
        profiler = cProfile.Profile()
        profiler.enable()

    mount_fuse(
        args.image, args.driver, args.mountpoint,
        args.block_size, args.volname, args.debug, args.trace, args.write,
        partition=args.partition,
        icons=args.icons,
        foreground=foreground,
    )

    if args.profile:
        profiler.disable()
        with open("profile.txt", "w") as f:
            stats = pstats.Stats(profiler, stream=f)
            stats.sort_stats(pstats.SortKey.CUMULATIVE)
            stats.print_stats()


def cmd_format(args):
    """Handle the 'format' subcommand."""
    _validate_driver_path(args.driver)
    # Refuse a name that cannot be sent before the image is opened. A
    # floppy's name is in args.partition; format_volume refuses that one.
    if args.volname is not None:
        _refuse_long_volume_name(args.volname)

    format_volume(
        args.image, args.driver, args.block_size,
        partition=args.partition,
        volname=args.volname,
        debug=args.debug,
        dos_type=getattr(args, "dos_type", None),
    )


def cmd_unmount(args):
    """Handle the 'unmount' subcommand."""
    from . import platform as plat

    mountpoint = args.mountpoint
    if sys.platform.startswith("win"):
        drive = plat.windows_drive_mountpoint(mountpoint)
        if drive:
            mountpoint = Path(drive + "\\")
    is_mounted = False
    try:
        is_mounted = os.path.ismount(mountpoint)
    except OSError:
        is_mounted = False
    if not is_mounted and not plat.is_stale_mountpoint(mountpoint):
        raise SystemExit(f"Mountpoint {mountpoint} is not currently mounted.")
    # On macOS, `umount -f` unmounts any volume, and the fallback below kills
    # an AmiFUSE process: refuse a mount that isn't AmiFUSE's before either.
    if sys.platform.startswith("darwin") and not plat.is_amifuse_mount_macos(mountpoint):
        raise SystemExit(
            f"Mountpoint {mountpoint} is not a mount AmiFUSE can identify as "
            f"its own, so amifuse unmount leaves it alone. If it is an "
            f"AmiFUSE mount, stop its amifuse process or run "
            f"umount {mountpoint}."
        )

    cmd = plat.get_unmount_command(mountpoint)
    _no_window = {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}
    if cmd:
        result = subprocess.run(cmd, check=False, **_no_window)
        if result.returncode != 0:
            killed_pids = kill_mount_owner_processes(mountpoint)
            if killed_pids:
                result = subprocess.run(cmd, check=False, **_no_window)
        if result.returncode != 0:
            raise SystemExit(
                f"Unmount failed with exit code {result.returncode}: "
                f"{' '.join(cmd)}"
            )
    else:
        if sys.platform.startswith("win"):
            try:
                killed_pids = plat.stop_mount_processes(
                    plat._find_mount_owner_pids(mountpoint))
            except OSError as exc:
                raise SystemExit(str(exc)) from exc
        else:
            killed_pids = kill_mount_owner_processes(mountpoint)
        if not killed_pids:
            message = f"No amifuse process found for mountpoint {mountpoint}."
            if sys.platform.startswith("win") and not plat.is_windows_admin():
                message += (
                    " If it was mounted from an Administrator shell, "
                    "unmount it from an Administrator shell."
                )
            raise SystemExit(message)
        print(f"Unmounted {mountpoint} (stopped "
              f"{'processes' if len(killed_pids) > 1 else 'process'}"
              f" {', '.join(str(p) for p in killed_pids)}).")


def _truncate_left(s, max_width):
    """Truncate a string from the left, preserving the rightmost characters.

    The filename at the end of a path is more useful than the directory prefix,
    so we trim from the left and prepend an ellipsis when truncation occurs.

    >>> _truncate_left("C:/very/long/path/to/file.hdf", 20)
    '..path/to/file.hdf'
    """
    if len(s) <= max_width:
        return s
    return ".." + s[-(max_width - 2):]


def cmd_status(args):
    """Handle the 'status' subcommand."""
    import json
    from . import platform as plat

    try:
        mounts = plat.find_amifuse_mounts()
    except OSError as exc:
        if getattr(args, "json", False):
            print(json.dumps({
                "status": "error",
                "command": "status",
                "error": f"Process discovery failed: {exc}",
                "mounts": [],
            }, indent=2))
        else:
            print(f"Error: Process discovery failed: {exc}")
        sys.exit(1)

    if getattr(args, "json", False):
        print(json.dumps({
            "status": "ok",
            "command": "status",
            "mounts": mounts,
        }, indent=2))
    else:
        if not mounts:
            print("No active AmiFUSE mounts.")
        else:
            # Tabular output
            print(f"{'PID':<8} {'Mountpoint':<20} {'Image':<40} {'Uptime'}")
            print("-" * 80)
            for m in mounts:
                uptime = m.get("uptime_seconds")
                if uptime is not None:
                    mins, secs = divmod(uptime, 60)
                    hrs, mins = divmod(mins, 60)
                    uptime_str = f"{hrs}h {mins}m {secs}s"
                else:
                    uptime_str = "N/A"
                image = _truncate_left(m.get("image") or "N/A", 40)
                mountpoint = m.get("mountpoint") or "N/A"
                print(f"{m['pid']:<8} {mountpoint:<20} {image:<40} {uptime_str}")


def cmd_doctor(args):
    """Handle the 'doctor' subcommand."""
    from .doctor import cmd_doctor as _cmd_doctor
    _cmd_doctor(args)


def cmd_register(args):
    """Handle the 'register' subcommand."""
    from .windows_shell import register
    register()


def cmd_unregister(args):
    """Handle the 'unregister' subcommand."""
    from .windows_shell import unregister
    unregister()


def _validate_driver_path(driver: Optional[Path]) -> None:
    if driver is None:
        return
    if not driver.exists():
        raise SystemExit(f"Filesystem driver not found: {driver}")
    if not driver.is_file():
        raise SystemExit(f"Filesystem driver is not a regular file: {driver}")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="amifuse",
        description=f"{__banner__}\n\n"
        "Mount Amiga filesystem images via FUSE.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
commands:
  inspect <image>           Inspect RDB partitions and filesystems.
    --block-size N            Override block size (defaults to auto/512).
    --full                    Show full partition details.
    --json                    Output machine-readable JSON instead of text.

  mount <image>             Mount an Amiga filesystem image via FUSE.
    --driver PATH             Filesystem binary (default: RDB, your drivers
                              folder, then the bundled FastFileSystem
                              for DOS0-7).
    --mountpoint PATH         Mount location (default: /Volumes/<partition>).
    --partition NAME          Partition name (e.g. DH0) or index (defaults to first).
    --block-size N            Override block size (defaults to auto/512).
    --volname NAME            Override volume name (defaults to partition name).
    --daemon                  Detach after mounting (default on macOS/Linux).
    --interactive             Stay attached; Ctrl+C unmounts the filesystem.
                              Windows uses this mode by default.
    --write                   Enable read-write mode (experimental).
    --icons                   Convert Amiga .info icons to native macOS icons (experimental).
    --debug                   Enable debug logging of FUSE operations.
    --trace                   Enable vamos instruction tracing (very noisy).
    --profile                 Enable profiling and write stats to profile.txt.

  unmount <mountpoint>      Unmount an existing AmiFUSE mount.

  status                    Show active AmiFUSE mounts on this system.
    --json                    Output results as JSON.

  doctor                    Check prerequisites and environment readiness.
    --json                    Output results as JSON.
    --fix                     Auto-fix what can be fixed.

  register                  Add Windows Explorer context menu entries.
  unregister                Remove Windows Explorer context menu entries.

  format <image> <partition> [volname]
                              Format an Amiga partition.
    --driver PATH             Filesystem binary (default: RDB, your drivers
                              folder, then the bundled FastFileSystem
                              for DOS0-7).
    --block-size N            Override block size (defaults to auto/512).
    --debug                   Enable debug logging.

  ls <image> [path]         List files in an Amiga filesystem image.
    --path PATH               Directory path to list (default: /).
    --partition NAME          Partition name (e.g. DH0) or index.
    --driver PATH             Filesystem binary (default: RDB, your drivers
                              folder, then the bundled FastFileSystem
                              for DOS0-7).
    --block-size N            Override block size (defaults to auto/512).
    --recursive               List all entries recursively.
    --json                    Output results as JSON.
    --debug                   Enable debug logging.

  verify <image>            Verify an Amiga filesystem image.
    --file PATH               Verify a specific file.
    --expect-size N           Expected file size in bytes (with --file).
    --partition NAME          Partition name (e.g. DH0) or index.
    --driver PATH             Filesystem binary (default: RDB, your drivers
                              folder, then the bundled FastFileSystem
                              for DOS0-7).
    --block-size N            Override block size (defaults to auto/512).
    --json                    Output results as JSON.
    --debug                   Enable debug logging.

  hash <image>              Compute file hash from an Amiga filesystem image.
    --file PATH               Path to file within the image (required).
    --algorithm ALG           Hash algorithm: md5, sha1, sha256 (default: sha256).
    --partition NAME          Partition name (e.g. DH0) or index.
    --driver PATH             Filesystem binary (default: RDB, your drivers
                              folder, then the bundled FastFileSystem
                              for DOS0-7).
    --block-size N            Override block size (defaults to auto/512).
    --json                    Output results as JSON.
    --debug                   Enable debug logging.

  read <image>              Extract a file from an Amiga filesystem image.
    --file PATH               Path to file within the image (required).
    --out PATH                Output path on host (default: filename, '-' for stdout).
                              Note: --out - cannot be combined with --json.
    --partition NAME          Partition name (e.g. DH0) or index.
    --driver PATH             Filesystem binary (default: RDB, your drivers
                              folder, then the bundled FastFileSystem
                              for DOS0-7).
    --block-size N            Override block size (defaults to auto/512).
    --json                    Output results as JSON.
    --debug                   Enable debug logging.

  write <image>             Write a file into an Amiga filesystem image.
    --file PATH               Destination path within the image (required).
    --in PATH                 Source file on the host (required).
    --partition NAME          Partition name (e.g. DH0) or index.
    --driver PATH             Filesystem binary (default: RDB, your drivers
                              folder, then the bundled FastFileSystem
                              for DOS0-7).
    --block-size N            Override block size (defaults to auto/512).
    --json                    Output results as JSON.
    --debug                   Enable debug logging.
""",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=__banner__,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    from .rdb_inspect import block_size_arg, dos_type_arg

    # inspect subcommand
    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect RDB partitions and filesystems."
    )
    inspect_parser.add_argument("image", type=Path, help="Disk image file or physical disk device")
    inspect_parser.add_argument(
        "--block-size", type=block_size_arg, help="Override block size (defaults to auto/512)."
    )
    inspect_parser.add_argument(
        "--full", action="store_true", help="Show full partition details."
    )
    inspect_parser.add_argument(
        "--json", action="store_true", help="Output machine-readable JSON instead of text."
    )
    inspect_parser.set_defaults(func=cmd_inspect)

    # mount subcommand
    mount_parser = subparsers.add_parser(
        "mount", help="Mount an Amiga filesystem image via FUSE."
    )
    mount_parser.add_argument("image", type=Path, help="Disk image file or physical disk device")
    mount_parser.add_argument("--driver", type=Path, help="Filesystem binary (default: RDB, your drivers folder, then the bundled FastFileSystem for DOS0-7)")
    mount_parser.add_argument("--mountpoint", type=Path, help="Mount location (default: /Volumes/<partition> on macOS, first free drive letter on Windows)")
    mount_parser.add_argument(
        "--partition", type=str, help="Partition name (e.g. DH0) or index (defaults to first)."
    )
    mount_parser.add_argument(
        "--block-size", type=block_size_arg, help="Override block size (defaults to auto/512)."
    )
    mount_parser.add_argument(
        "--volname", type=str, help="Override volume name displayed by the OS (defaults to partition name)."
    )
    run_mode = mount_parser.add_mutually_exclusive_group()
    run_mode.add_argument(
        "--daemon",
        dest="foreground",
        action="store_const",
        const=False,
        help="Detach after mounting (default on macOS/Linux).",
    )
    run_mode.add_argument(
        "--interactive",
        "--foreground",
        dest="foreground",
        action="store_const",
        const=True,
        help="Stay attached to the terminal; press Ctrl+C to unmount (default on Windows).",
    )
    mount_parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging of FUSE operations."
    )
    mount_parser.add_argument(
        "--trace",
        action="store_true",
        help="Enable vamos instruction tracing (very noisy).",
    )
    mount_parser.add_argument(
        "--profile", action="store_true", help="Enable profiling and write stats to profile.txt."
    )
    mount_parser.add_argument(
        "--write", action="store_true", help="Enable read-write mode (experimental)."
    )
    mount_parser.add_argument(
        "--icons", action="store_true",
        help="Convert Amiga .info icons to native macOS icons (experimental)."
    )
    mount_parser.set_defaults(func=cmd_mount, foreground=None)

    # unmount subcommand
    unmount_parser = subparsers.add_parser(
        "unmount", help="Unmount an existing AmiFUSE mount."
    )
    unmount_parser.add_argument("mountpoint", type=Path, help="Mounted filesystem path")
    unmount_parser.set_defaults(func=cmd_unmount)

    # status subcommand
    status_parser = subparsers.add_parser(
        "status", help="Show active AmiFUSE mounts on this system."
    )
    status_parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON.",
    )
    status_parser.set_defaults(func=cmd_status)

    # doctor subcommand
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check prerequisites and environment readiness."
    )
    doctor_parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON.",
    )
    doctor_parser.add_argument(
        "--fix", action="store_true",
        help="Auto-fix what can be fixed.",
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    # register / unregister subcommands (Windows shell integration)
    register_parser = subparsers.add_parser(
        "register", help="Register AmiFUSE file associations (Windows)."
    )
    register_parser.set_defaults(func=cmd_register)

    unregister_parser = subparsers.add_parser(
        "unregister", help="Remove AmiFUSE file associations (Windows)."
    )
    unregister_parser.set_defaults(func=cmd_unregister)

    # format subcommand
    format_parser = subparsers.add_parser(
        "format", help="Format an Amiga partition or floppy image."
    )
    format_parser.add_argument("image", type=Path, help="Disk image file")
    format_parser.add_argument(
        "partition", type=str,
        help="Partition name (e.g. DH0) or index; for a floppy image, the volume name.")
    format_parser.add_argument("volname", nargs="?", help="Volume name (default: Empty)")
    format_parser.add_argument("--driver", type=Path, help="Filesystem binary (default: RDB, your drivers folder, then the bundled FastFileSystem for DOS0-7)")
    format_parser.add_argument(
        "--block-size", type=block_size_arg, help="Override block size (defaults to auto/512)."
    )
    format_parser.add_argument(
        "--dos-type", type=dos_type_arg,
        help="DOS type of a floppy image, DOS0 to DOS7 (default: the one in "
             "block 0, or DOS0 for a blank image). Needed to overwrite an "
             "image of a floppy's size that is neither.",
    )
    format_parser.add_argument(
        "--debug", action="store_true", help="Enable debug logging."
    )
    format_parser.set_defaults(func=cmd_format)

    # ls subcommand
    ls_parser = subparsers.add_parser(
        "ls", help="List files in an Amiga filesystem image (no FUSE needed)."
    )
    ls_parser.add_argument("image", type=Path, help="Disk image file")
    ls_parser.add_argument(
        "--path", type=str, default="/",
        help="Directory path to list (default: root).",
    )
    ls_parser.add_argument(
        "--partition", type=str,
        help="Partition name (e.g. DH0) or index (defaults to first).",
    )
    ls_parser.add_argument(
        "--driver", type=Path,
        help="Filesystem binary (default: RDB, your drivers folder, "
             "then the bundled FastFileSystem for DOS0-7).",
    )
    ls_parser.add_argument(
        "--block-size", type=block_size_arg,
        help="Override block size (defaults to auto/512).",
    )
    ls_parser.add_argument(
        "--recursive", action="store_true",
        help="List all entries recursively.",
    )
    ls_parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON.",
    )
    ls_parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging.",
    )
    ls_parser.set_defaults(func=cmd_ls)

    # verify subcommand
    verify_parser = subparsers.add_parser(
        "verify", help="Verify an Amiga filesystem image (no FUSE needed)."
    )
    verify_parser.add_argument("image", type=Path, help="Disk image file")
    verify_parser.add_argument(
        "--file", type=str, dest="file",
        help="Verify a specific file exists and get its metadata.",
    )
    verify_parser.add_argument(
        "--expect-size", type=int, dest="expect_size",
        help="Expected file size in bytes (requires --file).",
    )
    verify_parser.add_argument(
        "--partition", type=str,
        help="Partition name (e.g. DH0) or index (defaults to first).",
    )
    verify_parser.add_argument(
        "--driver", type=Path,
        help="Filesystem binary (default: RDB, your drivers folder, "
             "then the bundled FastFileSystem for DOS0-7).",
    )
    verify_parser.add_argument(
        "--block-size", type=block_size_arg,
        help="Override block size (defaults to auto/512).",
    )
    verify_parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON.",
    )
    verify_parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging.",
    )
    verify_parser.set_defaults(func=cmd_verify)

    # hash subcommand
    hash_parser = subparsers.add_parser(
        "hash", help="Compute hash of a file in an Amiga filesystem image (no FUSE needed)."
    )
    hash_parser.add_argument("image", type=Path, help="Disk image file")
    hash_parser.add_argument(
        "--file", type=str, required=True, dest="file",
        help="Path to file within the image.",
    )
    hash_parser.add_argument(
        "--algorithm", type=str, default="sha256",
        choices=["md5", "sha1", "sha256"],
        help="Hash algorithm (default: sha256).",
    )
    hash_parser.add_argument(
        "--partition", type=str,
        help="Partition name (e.g. DH0) or index (defaults to first).",
    )
    hash_parser.add_argument(
        "--driver", type=Path,
        help="Filesystem binary (default: RDB, your drivers folder, "
             "then the bundled FastFileSystem for DOS0-7).",
    )
    hash_parser.add_argument(
        "--block-size", type=block_size_arg,
        help="Override block size (defaults to auto/512).",
    )
    hash_parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON.",
    )
    hash_parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging.",
    )
    hash_parser.set_defaults(func=cmd_hash)

    # read subcommand
    read_parser = subparsers.add_parser(
        "read", help="Extract a file from an Amiga filesystem image (no FUSE needed)."
    )
    read_parser.add_argument("image", type=Path, help="Disk image file")
    read_parser.add_argument(
        "--file", type=str, required=True, dest="file",
        help="Path to file within the image.",
    )
    read_parser.add_argument(
        "--out", type=str, default=None,
        help="Output path on host (default: filename in current dir, '-' for stdout). "
             "Note: --out - cannot be combined with --json.",
    )
    read_parser.add_argument(
        "--partition", type=str,
        help="Partition name (e.g. DH0) or index (defaults to first).",
    )
    read_parser.add_argument(
        "--driver", type=Path,
        help="Filesystem binary (default: RDB, your drivers folder, "
             "then the bundled FastFileSystem for DOS0-7).",
    )
    read_parser.add_argument(
        "--block-size", type=block_size_arg,
        help="Override block size (defaults to auto/512).",
    )
    read_parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON.",
    )
    read_parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging.",
    )
    read_parser.set_defaults(func=cmd_read)

    # write subcommand
    write_parser = subparsers.add_parser(
        "write", help="Write a file into an Amiga filesystem image."
    )
    write_parser.add_argument("image", type=Path, help="Disk image file")
    write_parser.add_argument(
        "--file", type=str, required=True, dest="file",
        help="Destination path within the Amiga image.",
    )
    write_parser.add_argument(
        "--in", type=str, required=True, dest="input",
        help="Source file on the host filesystem.",
    )
    write_parser.add_argument(
        "--partition", type=str,
        help="Partition name (e.g. DH0) or index (defaults to first).",
    )
    write_parser.add_argument(
        "--driver", type=Path,
        help="Filesystem binary (default: RDB, your drivers folder, "
             "then the bundled FastFileSystem for DOS0-7).",
    )
    write_parser.add_argument(
        "--block-size", type=block_size_arg,
        help="Override block size (defaults to auto/512).",
    )
    write_parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON.",
    )
    write_parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging.",
    )
    write_parser.set_defaults(func=cmd_write)

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as exc:
        raise SystemExit(f"Error: {exc}") from None

if __name__ == "__main__":
    main()
