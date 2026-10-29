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
import signal
import subprocess
import sys
import threading
import time

from enum import Enum
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from .platform import kill_mount_owner_processes

FUSE = None


class FuseOSError(OSError):
    """FUSE-compatible error that does not require importing fusepy."""

    def __init__(self, error):
        super().__init__(error, os.strerror(error))


class _FuseOperations:
    """The fusepy Operations defaults used by AmigaFuseFS."""

    bmap = None
    lock = None

    def __call__(self, op, *args):
        if not hasattr(self, op):
            raise FuseOSError(errno.EFAULT)
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
from .driver_resolver import DriverNotFound, ResolvedDriver
from .driver_runtime import BlockDeviceBackend, ImageInUseError
from .vamos_runner import VamosHandlerRuntime
from .bootstrap import BootstrapAllocator
from .process_mgr import ProcessManager
from .startup_runner import (
    HandlerLauncher,
    OFFSET_BEGINNING,
    OFFSET_CURRENT,
    OFFSET_END,
    _get_block_state,
    _clear_all_block_state,
    _get_active_block_state,
    _packet_type_name,
    _snapshot_block_state,
    _restore_block_state,
)
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
    """
    dir_type = mem.r32s(fib_addr + 4)   # fib_DirEntryType (signed LONG)
    protection = mem.r32(fib_addr + 116)  # fib_Protection (LONG)
    size = mem.r32(fib_addr + 124)      # fib_Size (ULONG)
    num_blocks = mem.r32s(fib_addr + 128)      # fib_NumBlocks (LONG)
    name_bytes = mem.r_block(fib_addr + 8, 108)  # fib_FileName starts at offset 8
    name_len = name_bytes[0]
    name = name_bytes[1 : 1 + name_len].decode("latin-1", errors="ignore")
    return {
        "dir_type": dir_type,
        "size": size,
        "name": name,
        "protection": protection,
        "num_blocks": num_blocks,
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
        adf_info=None,
        iso_info=None,
    ):
        self._lock = threading.RLock()  # Reentrant lock for thread safety
        self._debug = debug
        self._adf_info = adf_info
        self._iso_info = iso_info
        self.backend = None
        self.vh = None
        self._closed = False
        # Set when a wait runs out of busy cycles; see _run_until_replies.
        self._busy_ran_out = False
        try:
            self._initialize(image, driver, block_size, read_only, debug,
                             trace, partition, adf_info, iso_info)
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
        seg_baddr = self.vh.load_handler(driver)
        # Build DeviceNode/FSSM using seglist bptr. Pass the backend's block
        # size request, so both open the disk the same way.
        ba = BootstrapAllocator(
            self.vh, image, block_size=block_size, partition=partition, adf_info=adf_info,
            iso_info=iso_info, mbr_partition_index=mbr_idx,
        )
        if adf_info:
            handler_name = "DF0:"
        elif iso_info:
            handler_name = "CD0:"
        else:
            handler_name = "DH0:"
        boot = ba.alloc_all(handler_seglist_baddr=seg_baddr, handler_seglist_bptr=seg_baddr, handler_name=handler_name)
        self._partition_index = boot["part"].num if boot.get("part") else 0

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
                print(f"[amifuse]   Volume: {iso_info.volume_id}")
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
        self.state = self.launcher.launch_with_startup(debug=debug)

        # Initialize ProcessManager early for multi-process support (e.g., SFS)
        # This must happen before _run_until_replies since SFS creates children during startup
        self.proc_mgr = ProcessManager(
            vh=self.vh,
            machine=self.vh.machine,
            exec_impl=self.vh.slm.exec_impl,
            parent_proc_addr=self.state.process_addr
        )

        # run startup to completion
        # Use moderate cycle count for startup to allow child processes to run.
        # SFS creates child processes that need to run and register ports.
        # Smaller bursts give us more chances to interleave child execution.
        replies = self._run_startup_until_replies(cycles=10_000, max_iters=2000)
        # Check if startup succeeded - res1=0 means the handler rejected the disk
        pkt = DosPacketStruct(self.mem, self.state.stdpkt_addr)
        startup_res1 = pkt.res1.val
        startup_res2 = pkt.res2.val
        if not replies and startup_res1 == 0 and not self.state.crashed:
            if self._debug:
                print("[amifuse] No startup reply yet; attempting deferred signal flush before failing")
            if not self.state.main_loop_pc:
                from amitools.vamos.machine.regs import REG_D0
                cpu = self.vh.machine.cpu
                self._capture_main_loop_from_block_state()
                _clear_all_block_state()
                cpu.w_reg(REG_D0, 0)
                self._set_saved_main_reg(REG_D0, 0)
                self._capture_main_loop_state()
            self._flush_pending_signals()
            replies = self._run_startup_until_replies(cycles=10_000, max_iters=2000)
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
        if self._debug:
            print(
                f"[amifuse] Post-startup state: pc=0x{self.state.pc:x} "
                f"sp=0x{self.state.sp:x} main_loop_pc=0x{self.state.main_loop_pc:x}"
            )
        # Capture main loop state from the current blocking state BEFORE
        # clearing it. run_burst() during _run_until_replies may have
        # already set main_loop_pc; if not, grab it from the raw
        # blocking variables. This is critical for WaitPort-based handlers
        # where clearing the blocking state and setting D0=0 would cause a
        # NULL-message crash when the handler resumes from the WaitPort
        # return address.
        from amitools.vamos.machine.regs import REG_D0
        cpu = self.vh.machine.cpu
        if not self.state.main_loop_pc:
            self._capture_main_loop_from_block_state()
        if not self.state.main_loop_pc:
            self.state.main_loop_pc = 0
            self.state.main_loop_sp = 0
            self.state.wait_mask = 0
            self.state.block_state = None
            _clear_all_block_state()
            cpu.w_reg(REG_D0, 0)
            self._set_saved_main_reg(REG_D0, 0)
            self._capture_main_loop_state(max_iters=500, cycles=50_000)
        if not self.state.initialized:
            self.state.initialized = True
        if self._debug:
            wait_mask = getattr(self.state, "wait_mask", 0)
            if wait_mask:
                print(
                    f"[amifuse] Saved main_loop_pc=0x{self.state.pc:x}, "
                    f"main_loop_sp=0x{self.state.sp:x} wait_mask=0x{wait_mask:x}"
                )
            else:
                print(
                    f"[amifuse] Saved main_loop_pc=0x{self.state.pc:x}, "
                    f"main_loop_sp=0x{self.state.sp:x}"
                )
        # Flush pending timer events so the handler can complete delayed disk
        # validation.  PFS3 uses TR_ADDREQUEST to schedule deferred volume
        # validation; the timer signal must fire before the volume becomes
        # accessible.  We deliver all pending signals from the handler's
        # wait mask so it can process any queued timer or I/O events.
        self._flush_pending_signals()
        # cache a best-effort volume name
        self._volname = None
        self._fib_mem = None
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

    def _set_saved_main_reg(self, reg_num: int, value: int):
        """Mirror manual register changes into the saved main handler state."""
        if self.state.regs is not None:
            self.state.regs[reg_num] = value

    def _reserved_port_addrs(self) -> set[int]:
        reserved = {self.state.port_addr, self.state.reply_port_addr}
        if hasattr(self, "proc_mgr"):
            for proc in self.proc_mgr.processes.values():
                if proc.is_child and not proc.exited and proc.port_addr:
                    reserved.add(proc.port_addr)
        return reserved

    def _drain_non_essential_ports(self, pmgr):
        reserved_ports = self._reserved_port_addrs()
        timer = self._timer_device()
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
            for msg_addr in kept:
                pmgr.put_msg(port_addr, msg_addr)

    def _timer_device(self):
        """The runtime's AmiFuseTimerDevice, or None."""
        return getattr(getattr(self, "vh", None), "timer_device", None)

    def _deliver_timers(self, fast_forward: bool = False) -> int:
        """Reply to due timer requests; return the signals sent to the handler.

        Replies signal their port's task, which sets the bits in
        tc_SigRecvd and the fallback signals; the caller decides how the
        parked handler wakes. With fast_forward every pending request is
        due, so that a handler's initialisation needs no real time.
        """
        timer = self._timer_device()
        if timer is None:
            return 0
        signals = timer.deliver(fast_forward=fast_forward)
        if signals and self._debug:
            print(
                f"[amifuse] timer: replied, signals "
                f"{ {hex(task): hex(bits) for task, bits in signals.items()} }"
                f"{' (fast-forward)' if fast_forward else ''}"
            )
        return signals.get(self.state.process_addr, 0)

    def _deliver_timers_in_packet_wait(self):
        """Reply to timer requests while a packet wait finds the handler parked.

        Due requests are replied. A handler parked anywhere but its main
        loop waits within the packet, possibly for its own timer, so every
        pending request is then due, as when timer.device completed at
        once. At its main loop the deadlines hold, as for SFS's 0.5 s
        write-back. The main loop is its Wait() return PC on its stack
        frame: a Wait() in a subroutine shared with deeper code returns to
        the same PC on a deeper frame.
        """
        timer = self._timer_device()
        if timer is None or timer.next_deadline() == math.inf:
            return
        state = self.state
        at_main_loop = (
            bool(state.main_loop_pc)
            and state.pc == state.main_loop_pc
            and state.sp == state.main_loop_sp
        )
        signals = self._deliver_timers(fast_forward=not at_main_loop)
        self._leave_timer_signals_to_wait_frame(signals)

    def _leave_timer_signals_to_wait_frame(self, signals: int):
        """Keep due timer bits for a handler parked in Wait() in tc_SigRecvd only.

        The resume from its saved Wait() frame in run_burst returns them
        from tc_SigRecvd and clears them there. Left in the fallback signals
        too, the handler's next Wait() would return them again: a spurious
        timer wake.
        """
        if not signals:
            return
        block = _get_active_block_state(self.state.block_state)
        if block is not None and block.get("wait_blocked_sp") is not None:
            from amitools.vamos.lib.lexec.signalfunc import SignalFunc
            SignalFunc._fallback_signals &= ~signals

    def _run_ready_children(self, cycles_per_child: int):
        """Run child processes once, keeping the parent's blocking state."""
        if not hasattr(self, "proc_mgr"):
            return
        parent_block_state = _snapshot_block_state()
        self.proc_mgr.run_all_ready_children(cycles_per_child=cycles_per_child)
        if self.state.initialized:
            _restore_block_state(parent_block_state)
        else:
            _clear_all_block_state()

    def _parked_at_main_loop(self) -> bool:
        """Whether the handler waits in its main-loop Wait() for signals.

        Both its PC and its SP must match the main loop's, as in
        _deliver_timers_in_packet_wait.
        """
        state = self.state
        if (
            state.crashed
            or not state.main_loop_pc
            or state.pc != state.main_loop_pc
            or state.sp != state.main_loop_sp
        ):
            return False
        block = _get_active_block_state(state.block_state)
        # A WaitPort() wakes only for a message, never for a signal.
        return block is None or block.get("wait_blocked_sp") is not None

    def _resume_with(self, signals: int, cycles: int = 200_000, max_iters: int = 50):
        """Wake the handler parked at its main loop with signals, and run it.

        Wait() returns the signals the handler waits for, which are cleared
        from the fallback signals and tc_SigRecvd as Wait() would. The
        parent and then any ready children run until the parent parks with
        nothing pending, under the busy limits of _run_until_replies and at
        most max_iters parks. Signals the handler does not wait for stay
        pending. Nothing polls the reply port here: a late reply belongs to
        the next packet's wait.
        """
        from amitools.vamos.lib.lexec.signalfunc import SignalFunc
        from amitools.vamos.libstructs.exec_ import TaskStruct
        from amitools.vamos.machine.regs import REG_D0

        state = self.state
        block = _get_active_block_state(state.block_state)
        wait_mask = (block or {}).get("wait_blocked_mask") or state.wait_mask
        signals &= wait_mask
        if not signals or not self._parked_at_main_loop():
            # A child may have been signalled instead.
            self._run_ready_children(cycles // 4)
            return
        SignalFunc._fallback_signals &= ~signals
        sigrecvd_off = TaskStruct.sdef.find_field_def_by_name("tc_SigRecvd").offset
        sigrecvd_addr = state.process_addr + sigrecvd_off
        self.mem.w32(sigrecvd_addr, self.mem.r32(sigrecvd_addr) & ~signals)
        # state.pc and state.sp already hold the parked Wait() frame.
        self.vh.machine.cpu.w_reg(REG_D0, signals)
        self._set_saved_main_reg(REG_D0, signals)
        _clear_all_block_state()
        state.block_state = None
        if self._debug:
            print(f"[amifuse] timer: resuming handler with D0=0x{signals:x}")

        busy_cycles = 0
        idle_iters = 0
        while idle_iters < max_iters:
            busy_limit = (
                self._SHORT_BUSY_CYCLES if self._busy_ran_out
                else self._MAX_BUSY_CYCLES
            )
            if busy_cycles >= busy_limit:
                # As in _run_until_replies: the next wait gets the short limit.
                self._busy_ran_out = True
                if self._debug:
                    print(f"[amifuse] timer: handler still busy after "
                          f"{busy_cycles:,} cycles, pc=0x{state.pc:x}")
                return
            self.launcher.run_burst(state, max_cycles=cycles, debug=self._debug)
            rs = state.run_state
            if state.crashed:
                return
            busy = not getattr(rs, "error", None) and not getattr(rs, "done", False)
            if busy:
                busy_cycles += cycles
            else:
                self._busy_ran_out = False
            self._run_ready_children(cycles // 4)
            if busy:
                continue
            if getattr(rs, "done", False):
                return
            # It parked. A burst that parks reports no cycles, so the parked
            # state decides: with a wake already pending (a self-signal, a
            # quick reply, a child's reply) run_burst left no block state,
            # or the resume below finds the wake, and it runs again.
            if (
                _get_active_block_state(state.block_state) is not None
                and not self.launcher.setup_resume_if_blocked(state, debug=self._debug)
            ):
                return
            idle_iters += 1

    # How long the mount's timer thread waits before it looks again at a
    # handler it could not wake (busy, or waiting somewhere else).
    _TIMER_RETRY = 1.0

    def run_due_timers(self) -> Optional[float]:
        """Reply to due timer requests and let the parked handler act on them.

        Called by the mount's timer thread between FUSE operations. Skips
        a handler that is not parked at its main loop, such as one whose
        packet wait ran out. Returns the seconds until the next call is
        due, or None when no timer request is pending.
        """
        with self._lock:
            timer = self._timer_device()
            if timer is None or self.state.crashed:
                return None
            if self._busy_ran_out or not self._parked_at_main_loop():
                return None if timer.next_deadline() == math.inf else self._TIMER_RETRY
            deliveries = timer.deliveries
            signals = self._deliver_timers()
            if timer.deliveries != deliveries:
                self._resume_with(signals)
            deadline = timer.next_deadline()
            if deadline == math.inf:
                return None
            return max(0.0, deadline - timer.clock())

    def _run_startup_until_replies(
        self,
        max_iters: int = 2000,
        cycles: int = 10_000,
        sleep_base: float = 0.0005,
        sleep_max: float = 0.01,
    ):
        """Run startup bursts until we have the startup reply.

        Some handlers reply to startup before their private mount path has
        reached the final steady-state Wait() loop. Keep driving startup after
        the reply arrives and hand the saved reply back to the caller once the
        startup budget is exhausted or the handler stops making forward
        progress.
        """
        replies = []
        sleep_time = sleep_base

        if self.state.crashed:
            if self._debug:
                print("[amifuse] startup: handler crashed, returning empty")
            return []

        for i in range(max_iters):
            old_pc = self.state.pc
            self.launcher.run_burst(self.state, max_cycles=cycles, debug=self._debug)
            rs = self.state.run_state
            new_pc = self.state.pc
            parent_block_state = None

            if self._debug and i < 3:
                cycles_run = getattr(rs, "cycles", 0) if rs else 0
                print(
                    f"[amifuse] startup iter {i}: PC 0x{old_pc:x}->0x{new_pc:x} "
                    f"cycles={cycles_run} done={getattr(rs, 'done', False)} "
                    f"error={getattr(rs, 'error', None)} "
                    f"kind={getattr(rs, 'error_kind', None)} "
                    f"detail={getattr(rs, 'error_detail', None)!r}"
                )

            if self.state.crashed:
                if self._debug:
                    print(f"[amifuse] startup: handler crashed at iter {i}")
                return replies

            polled = self.launcher.poll_replies(
                self.state.reply_port_addr, debug=self._debug
            )
            if polled:
                replies.extend(polled)
                if self._debug and i > 5:
                    print(
                        f"[amifuse] startup: got {len(replies)} replies "
                        f"after {i} iters; continuing"
                    )

            if hasattr(self, "proc_mgr"):
                parent_block_state = _snapshot_block_state()
                num_children = self.proc_mgr.run_all_ready_children(
                    cycles_per_child=cycles // 4
                )
                if self.state.initialized:
                    _restore_block_state(parent_block_state)
                else:
                    _clear_all_block_state()
                if num_children > 0 and self._debug:
                    print(f"[amifuse] startup ran {num_children} child process(es)")

            parent_wait_blocked = (
                parent_block_state is not None
                and (
                    parent_block_state.get("waitport_blocked_sp") is not None
                    or parent_block_state.get("wait_blocked_sp") is not None
                )
            )
            children_settled = not (
                hasattr(self, "proc_mgr") and self.proc_mgr.has_unsettled_children()
            )
            parent_blocked = parent_wait_blocked or bool(getattr(rs, "error", None))
            if replies and parent_blocked and children_settled:
                if parent_block_state is not None:
                    _restore_block_state(parent_block_state)
                if self._debug:
                    print(
                        f"[amifuse] startup: returning after {i} iters with "
                        f"{len(replies)} reply/replies and settled children"
                    )
                return replies

            if getattr(rs, "error", None):
                if not replies:
                    # Parked before replying: a handler that waits on its own
                    # timer during startup gets it at once, as it always did.
                    self._deliver_timers(fast_forward=True)
                polled = self.launcher.poll_replies(
                    self.state.reply_port_addr, debug=self._debug
                )
                if polled:
                    replies.extend(polled)
                if hasattr(self, "proc_mgr"):
                    parent_block_state = _snapshot_block_state()
                    self.proc_mgr.run_all_ready_children(
                        cycles_per_child=cycles // 2
                    )
                    if self.state.initialized:
                        _restore_block_state(parent_block_state)
                    else:
                        _clear_all_block_state()
                continue

            if getattr(rs, "done", False) and not replies:
                break

            # A burst that ends at its cycle limit, with neither done nor
            # error set, stopped the handler in the middle of its work, so
            # the next burst follows at once.
            if sleep_base > 0 and getattr(rs, "done", False):
                time.sleep(sleep_time)
                sleep_time = min(sleep_time * 2, sleep_max)

        if self._debug and not replies:
            print(
                f"[amifuse] startup: exhausted {max_iters} iters with no "
                f"replies, pc=0x{self.state.pc:x}"
            )
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

    def _run_until_replies(self, max_iters: int = 50, cycles: int = 200_000, sleep_base: float = 0.0005, sleep_max: float = 0.01, drain_non_essential: bool = True):
        """Run handler bursts until at least one reply is queued.

        A burst that ends at its cycle limit, with neither done nor error
        set, stopped the handler in the middle of its work. Such bursts run
        again at once and count against a busy limit of _MAX_BUSY_CYCLES.
        Every other burst, in which the handler waited or exited, counts
        against max_iters. Returns [] when either limit runs out first, and
        says so on stderr when it is _MAX_BUSY_CYCLES.

        A handler whose wait ran out is still running, and the next packet
        would resume it with the full busy limit again. Until it replies to
        any packet, waits or exits, later waits get _SHORT_BUSY_CYCLES
        instead. The limit is checked before every burst, so a handler that
        waits or exits during such a wait gets the full limit for the rest
        of it. A reply may belong to an earlier packet, so every packet
        wait must go through _run_until_reply, which drops such a reply.

        A handler waiting on timer.device parks like any other. Timer
        requests that are due when the wait starts are replied first, so
        their signals wake the handler with the packet's, and so are those
        due whenever the wait finds it parked. Parked anywhere but its main
        loop, it may wait for its own timer before it replies, so every
        pending request is then due. Between packets, a mount's timer
        thread replies to them (run_due_timers). The limits
        count cycles and bursts, not time: Delay() sleeps on the host and
        spends no cycles, so the time a handler spends in it is bounded by
        neither, as before.

        Known limits: a parent that waits while a child process does the
        packet's work still counts against max_iters, because
        ProcessManager.run_all_ready_children drops whether the child is
        still busy. And a packet whose wait ran out stays queued, or held by
        the handler, until the handler answers it, but HandlerLauncher
        builds packets in a ring of 8 buffers, so the 8th packet sent after
        it reuses its buffer. If the first is still queued, the port then
        holds the new packet twice and the first no more; either way, a late
        reply to the first carries the new packet's address and passes for
        its answer. A wait that ends at max_iters leaves its packet behind
        the same way, as every wait of more than 50 bursts did before the
        busy limit. Last, while the parent is parked at its main loop its
        timers keep their deadlines, so a reply that waits on a timer cannot
        come within the packet wait: a handler that replies only once its
        own timer fires, or a child that waits on a timer while the parent
        sits at its main loop. The wait ends without that reply, and the
        late reply is dropped (_run_until_reply).
        """
        from amitools.vamos.lib.ExecLibrary import ExecLibrary
        replies = []
        sleep_time = sleep_base
        idle_iters = 0
        busy_cycles = 0

        # Check if handler has crashed - if so, don't try to run it
        if self.state.crashed:
            if self._debug:
                print("[amifuse] _run_until_replies: handler crashed, returning empty")
            return []

        # Guard against re-entering at the exit trap addresses (0x400/0x402).
        # The emulator uses low memory addresses as trap vectors; if PC lands there,
        # it means the handler tried to exit. Reset to main loop to keep it alive.
        if self.state.pc <= 0x1000 and getattr(self.state, "main_loop_pc", 0):
            self.state.pc = self.state.main_loop_pc
            self.state.sp = self.state.main_loop_sp

        # Reply to timer requests that fell due since the handler last ran.
        # Their signals wake it with the DOS port's: below, or through the
        # saved Wait() frame in run_burst.
        timer_signals = self._deliver_timers()
        self._leave_timer_signals_to_wait_frame(timer_signals)

        # If handler is at main_loop_pc and there's a pending message, set D0 to wake it
        # This handles the case where handler is blocked waiting but blocked state was cleared
        if (
            self.state.main_loop_pc
            and self.state.pc == self.state.main_loop_pc
            and not self.state.block_state
        ):
            pmgr = self.vh.slm.exec_impl.port_mgr
            has_msg = pmgr.has_msg(self.state.port_addr)
            if has_msg:
                # Deliver the DOS port signal and those of due timer
                # requests. Other signals were handled during
                # _flush_pending_signals at init time, and stray replies
                # from other devices are drained after every packet.
                # Use the handler's actual Wait mask to determine which
                # signal to deliver.  Handlers like CDFileSystem use a
                # custom Wait loop whose mask may not include the port's
                # mp_SigBit; signalling the port bit alone won't wake them.
                # Fall back to bit 8 for handlers with no captured mask.
                from amitools.vamos.libstructs.exec_ import MsgPortStruct
                mp_sigbit_off = MsgPortStruct.sdef.find_field_def_by_name("mp_SigBit").offset
                sigbit = self.mem.read(0, self.state.port_addr + mp_sigbit_off)
                port_signal = 1 << sigbit if 0 <= sigbit < 32 else 0x100
                wait_mask = getattr(self.state, 'wait_mask', 0)
                if wait_mask and not (wait_mask & port_signal):
                    # Port signal not in wait mask — deliver the full mask
                    dos_signal = wait_mask
                else:
                    dos_signal = port_signal | (timer_signals & wait_mask)
                # Wait() clears the timer bits it returns, from the fallback
                # signals and tc_SigRecvd; the others stay pending.
                from amitools.vamos.lib.lexec.signalfunc import SignalFunc
                returned_timer = timer_signals & dos_signal
                SignalFunc._fallback_signals &= ~returned_timer
                self.vh.machine.cpu.w_reg(0, dos_signal)  # REG_D0
                self._set_saved_main_reg(0, dos_signal)
                # Also set tc_SigRecvd in case handler checks the task structure
                from amitools.vamos.libstructs.exec_ import TaskStruct
                sigrecvd_off = TaskStruct.sdef.find_field_def_by_name("tc_SigRecvd").offset
                proc_addr = self.state.process_addr
                self.mem.w32(
                    proc_addr + sigrecvd_off,
                    (dos_signal & ~returned_timer) | (timer_signals & ~dos_signal),
                )
                if self._debug:
                    print(
                        f"[amifuse] Resuming: port_signal=0x{port_signal:x} "
                        f"wait_mask=0x{wait_mask:x} D0=0x{dos_signal:x} "
                        f"tc_SigRecvd@0x{proc_addr + sigrecvd_off:x}=0x{dos_signal:x}"
                    )

        for i in itertools.count():
            busy_limit = (
                self._SHORT_BUSY_CYCLES if self._busy_ran_out
                else self._MAX_BUSY_CYCLES
            )
            if idle_iters >= max_iters or busy_cycles >= busy_limit:
                break
            old_pc = self.state.pc
            self.launcher.run_burst(self.state, max_cycles=cycles, debug=self._debug)
            rs = self.state.run_state
            new_pc = self.state.pc
            # A burst that ends at its cycle limit, with neither done nor
            # error set, stopped the handler in the middle of its work.
            busy = not getattr(rs, "error", None) and not getattr(rs, "done", False)
            if busy:
                busy_cycles += cycles
            else:
                # It waited or exited, so it is not caught in a loop.
                self._busy_ran_out = False
            # A wait can take 50,000 busy bursts, so --debug reports only
            # every 1000th of them, with the busy cycles so far, and the
            # poll of a busy burst only then or when a reply is queued.
            show = self._debug and (not busy or busy_cycles % (1000 * cycles) == 0)
            if show and busy:
                print(
                    f"[amifuse] _run_until_replies: busy for {busy_cycles:,} "
                    f"cycles, pc=0x{new_pc:x}"
                )
            # Log first few iterations to debug handler movement
            if self._debug and i < 3:
                cycles_run = getattr(rs, 'cycles', 0) if rs else 0
                print(f"[amifuse] iter {i}: PC 0x{old_pc:x}->0x{new_pc:x} cycles={cycles_run} done={getattr(rs, 'done', False)} error={getattr(rs, 'error', None)}")
            # Check if handler crashed during this burst
            if self.state.crashed:
                if self._debug:
                    print(f"[amifuse] _run_until_replies: handler crashed at iter {i}")
                return []
            # Check for replies first - if we have them, we're done
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

            # Run any child processes that were created (e.g., SFS DosList handler)
            # This allows children to initialize and register their ports
            if hasattr(self, 'proc_mgr'):
                if self._debug and i < 5:
                    print(f"[amifuse] iter {i}: calling run_all_ready_children")
                parent_block_state = _snapshot_block_state()
                num_children = self.proc_mgr.run_all_ready_children(
                    cycles_per_child=cycles // 4
                )
                if self.state.initialized:
                    _restore_block_state(parent_block_state)
                else:
                    _clear_all_block_state()
                if num_children > 0 and show:
                    print(f"[amifuse] Ran {num_children} child process(es)")
            else:
                if self._debug and i < 5:
                    print(f"[amifuse] iter {i}: no proc_mgr")

            # Check for error FIRST - errors take precedence over blocked state
            if getattr(rs, "error", None):
                idle_iters += 1
                # Error during run - might be WaitPort block or FindPort yield
                # Check if we have replies first
                polled = self.launcher.poll_replies(
                    self.state.reply_port_addr, debug=self._debug
                )
                if polled:
                    replies = polled
                    break
                # No replies - run children and continue (don't break)
                if hasattr(self, 'proc_mgr'):
                    parent_block_state = _snapshot_block_state()
                    self.proc_mgr.run_all_ready_children(
                        cycles_per_child=cycles // 2
                    )
                    if self.state.initialized:
                        _restore_block_state(parent_block_state)
                    else:
                        _clear_all_block_state()
                else:
                    _clear_all_block_state()
                # A handler that waits for its own timer before it replies
                # gets it within this wait; the next burst resumes it.
                self._deliver_timers_in_packet_wait()
                continue
            if busy:
                # Run it again at once.
                continue
            # If handler exited (done=True) without blocking, stop looping - it's not coming back
            if not self.state.initialized:
                # Handler exited during startup without entering main loop
                # The startup packet results are in the packet struct - let caller check them
                break
            idle_iters += 1
            # Yield with exponential backoff to avoid tight polling loops.
            if sleep_base > 0:
                time.sleep(sleep_time)
                sleep_time = min(sleep_time * 2, sleep_max)
        if self._debug and not replies:
            print(
                f"[amifuse] _run_until_replies: no replies after {idle_iters} "
                f"idle iters and {busy_cycles:,} of {busy_limit:,} busy cycles, "
                f"pc=0x{self.state.pc:x}"
            )
        if replies:
            # It answered a packet, so it is not caught in a loop, even if it
            # went on to the next packet without waiting.
            self._busy_ran_out = False
        elif busy_cycles >= busy_limit:
            if busy_limit == self._MAX_BUSY_CYCLES:
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

        # Drain stray IO replies that accumulated during packet processing:
        # SendIO queues them at once. timer.device replies stay, since they
        # come only when due and the handler takes them.
        # During startup (drain_non_essential=False), keep all messages
        # so _flush_pending_signals can deliver them for deferred init.
        if drain_non_essential:
            pmgr = self.vh.slm.exec_impl.port_mgr
            self._drain_non_essential_ports(pmgr)

        return replies

    def _capture_main_loop_from_block_state(self) -> bool:
        """Adopt the handler's Wait/WaitPort return frame as the main loop.

        When a run burst left the handler blocked, the Exec-side blocking
        variables hold the return PC/SP of the Wait()/WaitPort() call. If
        they look valid, save them as the main-loop restart point and
        snapshot the block state. Returns True if a main loop was captured.
        """
        from amitools.vamos.lib.ExecLibrary import ExecLibrary

        waitport_sp = _get_block_state(ExecLibrary, "_waitport_blocked_sp")
        wait_sp = _get_block_state(ExecLibrary, "_wait_blocked_sp")
        waitport_ret = _get_block_state(ExecLibrary, "_waitport_blocked_ret")
        wait_ret = _get_block_state(ExecLibrary, "_wait_blocked_ret")
        wait_mask = _get_block_state(ExecLibrary, "_wait_blocked_mask")
        blocked_sp = waitport_sp if waitport_sp is not None else wait_sp
        blocked_ret = waitport_ret if waitport_ret is not None else wait_ret
        if blocked_sp is None:
            return False
        ret_addr = blocked_ret if blocked_ret is not None else self.mem.r32(blocked_sp)
        if ret_addr < 0x800:
            return False
        self.state.main_loop_pc = ret_addr
        self.state.main_loop_sp = blocked_sp + 4
        if wait_mask:
            self.state.wait_mask = wait_mask
        self.state.initialized = True
        self.state.block_state = _snapshot_block_state()
        if self._debug:
            mask_str = (
                f" wait_mask=0x{self.state.wait_mask:x}"
                if self.state.wait_mask
                else ""
            )
            print(
                f"[amifuse] Captured main_loop from blocking state: "
                f"pc=0x{ret_addr:x}, sp=0x{blocked_sp + 4:x}{mask_str}"
            )
        return True

    def _capture_main_loop_state(self, max_iters: int = 50, cycles: int = 10_000):
        """Run until the handler blocks in Wait/WaitPort and capture restart PC/SP."""
        if self.state.main_loop_pc:
            return  # Already captured (e.g. during _run_until_replies)
        from amitools.vamos.lib.ExecLibrary import ExecLibrary
        for i in range(max_iters):
            if self.state.pc < 0x800 or self.state.pc > 0xFFFFFF:
                print(f"[_capture] ERROR: invalid state.pc=0x{self.state.pc:x} before run_burst", file=sys.stderr)
                return
            rs = self.launcher.run_burst(self.state, max_cycles=cycles, debug=self._debug)

            # Run child processes - SFS needs DosList handler to run during init
            if hasattr(self, 'proc_mgr'):
                parent_block_state = _snapshot_block_state()
                self.proc_mgr.run_all_ready_children(cycles_per_child=cycles // 2)
                if self.state.initialized:
                    _restore_block_state(parent_block_state)
                else:
                    _clear_all_block_state()

            if self.state.main_loop_pc:
                return
            waitport_sp = _get_block_state(ExecLibrary, '_waitport_blocked_sp')
            wait_sp = _get_block_state(ExecLibrary, '_wait_blocked_sp')
            waitport_ret = _get_block_state(ExecLibrary, '_waitport_blocked_ret')
            wait_ret = _get_block_state(ExecLibrary, '_wait_blocked_ret')
            blocked_sp = waitport_sp if waitport_sp is not None else wait_sp
            blocked_ret = waitport_ret if waitport_ret is not None else wait_ret
            if blocked_sp is not None:
                ret_addr = blocked_ret if blocked_ret is not None else self.mem.r32(blocked_sp)
                if ret_addr >= 0x800:
                    self.state.main_loop_pc = ret_addr
                    self.state.main_loop_sp = blocked_sp + 4
                    self.state.block_state = _snapshot_block_state()
                    # Capture Wait mask for _flush_pending_signals
                    wait_mask = _get_block_state(ExecLibrary, '_wait_blocked_mask')
                    if wait_mask is not None and wait_mask != 0:
                        self.state.wait_mask = wait_mask
                    if self._debug:
                        block_kind = "WaitPort" if waitport_sp is not None else "Wait"
                        mask_str = f" wait_mask=0x{self.state.wait_mask:x}" if self.state.wait_mask else ""
                        print(
                            f"[amifuse] _capture_main_loop_state: {block_kind} "
                            f"captured at iter {i}, current_pc=0x{self.state.pc:x} "
                            f"ret=0x{ret_addr:x}, sp=0x{blocked_sp+4:x}{mask_str}"
                        )
                return
            # Check if handler crashed (invalid PC)
            if rs and hasattr(rs, 'pc') and rs.pc < 0x800:
                print(f"[amifuse] _capture_main_loop_state: handler crashed at iter {i}, pc=0x{rs.pc:x}", file=sys.stderr)
                return

    def _flush_pending_signals(self, max_rounds: int = 20):
        """Deliver pending signals so the handler can complete deferred init.

        PFS3 uses Signal(FindTask(NULL), ...) to schedule deferred volume
        validation steps.  Each self-signal causes Wait() to return
        immediately, letting the handler continue validation within one
        burst.  When validation finishes (or the handler needs to wait for
        real time / IO), it calls Wait() without pending self-signals and
        blocks.

        We deliver an initial timer signal (bit 19) to kick off the first
        validation cycle.  Subsequent rounds only fire if the handler
        blocked again with pending signals (port messages or self-signals).
        Every round first replies to all pending timer.device requests, as
        if their time had passed, so work a handler defers to a timer is
        done here; a handler that re-arms its timer costs a short round.
        """
        if not self.state.main_loop_pc:
            return
        from amitools.vamos.libstructs.exec_ import ExecLibraryStruct, TaskStruct, MsgPortStruct
        from amitools.vamos.lib.ExecLibrary import ExecLibrary
        from amitools.vamos.lib.lexec.signalfunc import SignalFunc
        from amitools.vamos.machine.regs import REG_D0

        # If we don't have a wait mask yet, run the handler once to capture it.
        if not self.state.wait_mask:
            _clear_all_block_state()
            self.state.pc = self.state.main_loop_pc
            self.state.sp = self.state.main_loop_sp
            self.vh.machine.cpu.w_reg(REG_D0, 0)
            self._set_saved_main_reg(REG_D0, 0)
            self.launcher.run_burst(self.state, max_cycles=100_000)
            wait_mask = _get_block_state(ExecLibrary, '_wait_blocked_mask')
            if wait_mask and wait_mask != 0:
                self.state.wait_mask = wait_mask
            _clear_all_block_state()

        wait_mask = self.state.wait_mask
        if not wait_mask:
            return

        if self._debug:
            print(f"[amifuse] _flush_pending_signals: wait_mask=0x{wait_mask:x}")

        pmgr = self.vh.slm.exec_impl.port_mgr
        exec_base = self.mem.r32(4)
        this_task_off = ExecLibraryStruct.sdef.find_field_def_by_name("ThisTask").offset
        this_task = self.mem.r32(exec_base + this_task_off)
        sigrecvd_off = TaskStruct.sdef.find_field_def_by_name("tc_SigRecvd").offset
        mp_sigbit_off = MsgPortStruct.sdef.find_field_def_by_name("mp_SigBit").offset

        for rnd in range(max_rounds):
            # --- Gather all pending signals ---
            # 0. Every pending timer request is due: initialisation takes
            #    no real time. Its reply sets tc_SigRecvd and a port.
            timer_signals = self._deliver_timers(fast_forward=True)

            # 1. Self-signals set by handler via Signal(FindTask(NULL), ...)
            self_signals = SignalFunc._fallback_signals

            # 2. tc_SigRecvd from m68k memory (IO completion, etc.)
            tc_sigrecvd = self.mem.r32(this_task + sigrecvd_off)

            # 3. Signals from ports with actual messages
            port_signals = 0
            for port_addr, port in pmgr.ports.items():
                has_msg = (port.queue is not None and len(port.queue) > 0)
                if not has_msg:
                    try:
                        mp_msglist_off = MsgPortStruct.sdef.find_field_def_by_name("mp_MsgList").offset
                        list_addr = port_addr + mp_msglist_off
                        lh_head = self.mem.r32(list_addr)
                        has_msg = (lh_head != 0 and lh_head != list_addr + 4)
                    except Exception:
                        pass
                if has_msg:
                    try:
                        sigbit = self.mem.read(0, port_addr + mp_sigbit_off)
                        if 0 <= sigbit < 32:
                            port_signals |= 1 << sigbit
                    except Exception:
                        pass

            # 4. On the first round, inject the timer/validation signal to
            #    kick off the deferred validation cycle.
            timer_signal = (1 << 19) if rnd == 0 else 0

            # Combine and mask to what the handler is waiting for.
            combined = (
                self_signals | tc_sigrecvd | port_signals | timer_signal | timer_signals
            )
            actual_signals = combined & wait_mask

            # If no signals to deliver (and past the first round), we're done.
            if actual_signals == 0 and rnd > 0:
                if self._debug:
                    print(f"[amifuse] _flush round {rnd}: no pending signals, done")
                break

            # Ensure at least the timer signal on first round.
            if actual_signals == 0:
                actual_signals = (1 << 19) & wait_mask
                if actual_signals == 0:
                    actual_signals = wait_mask  # Last resort

            if self._debug:
                print(f"[amifuse] _flush round {rnd}: signals=0x{actual_signals:x} "
                      f"(self=0x{self_signals:x} tc=0x{tc_sigrecvd:x} "
                      f"ports=0x{port_signals:x} timer=0x{timer_signal:x} "
                      f"timer.device=0x{timer_signals:x})")

            # Clear delivered signals from _fallback_signals and tc_SigRecvd,
            # mimicking how Wait() atomically clears returned signals.
            SignalFunc._fallback_signals &= ~actual_signals
            self.mem.w32(this_task + sigrecvd_off, tc_sigrecvd & ~actual_signals)

            # Set up Wait() return: D0 = returned signals
            self.state.pc = self.state.main_loop_pc
            self.state.sp = self.state.main_loop_sp
            self.vh.machine.cpu.w_reg(REG_D0, actual_signals)
            self._set_saved_main_reg(REG_D0, actual_signals)
            _clear_all_block_state()

            old_pc = self.state.pc
            self.launcher.run_burst(self.state, max_cycles=2_000_000)
            rs = self.state.run_state
            if self._debug:
                cycles_run = getattr(rs, 'cycles', 0) if rs else 0
                print(f"[amifuse] _flush round {rnd}: PC 0x{old_pc:x}->0x{self.state.pc:x} "
                      f"cycles={cycles_run} error={getattr(rs, 'error', None)} crashed={self.state.crashed}")

            # Drain any spurious replies (timer completion etc.)
            self.launcher.poll_replies(self.state.reply_port_addr)
            _clear_all_block_state()
            if self.state.crashed:
                break

            # If the handler made no progress (0 cycles), it blocked
            # immediately and these signals aren't useful.  Stop flushing.
            cycles_ran = getattr(rs, 'cycles', 0) if rs else 0
            if cycles_ran == 0 and rnd > 0:
                if self._debug:
                    print(f"[amifuse] _flush round {rnd}: no progress, stopping")
                break

            # Capture updated wait_mask if handler changed it
            new_mask = _get_block_state(ExecLibrary, '_wait_blocked_mask')
            if new_mask and new_mask != 0:
                wait_mask = new_mask
                self.state.wait_mask = new_mask

        # Drain stray replies from non-essential ports (disk change etc.).
        # timer.device replies stay for the handler: they come only when due.
        self._drain_non_essential_ports(pmgr)

        # Clean up fallback signals to avoid stale signals during normal
        # DOS packet processing.
        SignalFunc._fallback_signals = 0

        # After the flush rounds, the handler is blocked in Wait() and
        # run_burst has set state.pc to the Wait() return address (= the
        # main loop entry).  Update main_loop_pc/sp to match so subsequent
        # _run_until_replies calls work correctly.
        if self.state.pc >= 0x800:
            self.state.main_loop_pc = self.state.pc
            self.state.main_loop_sp = self.state.sp

        # Ensure clean blocking state for normal operation.
        _clear_all_block_state()

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
        short limit, so the rounds after it get the full one.
        """
        for _ in range(self._MAX_REPLY_ROUNDS):
            replies = self._run_until_replies(**wait)
            self._log_replies(label, replies)
            if not replies:
                return None
            answer = None
            for _, reply_pkt, res1, res2 in replies:
                if reply_pkt == pkt_addr:
                    answer = (res1, res2)
                elif self._debug:
                    print(f"[amifuse][{label}] dropped late reply for "
                          f"pkt=0x{reply_pkt:x}")
            if answer is not None:
                return answer
        return None

    def _alloc_fib(self):
        if self._fib_mem is None:
            self._fib_mem = self.vh.alloc.alloc_struct(
                FileInfoBlockStruct, label="FUSE_FIB"
            )
            if self._debug:
                fib_end = self._fib_mem.addr + FileInfoBlockStruct.get_size()
                print(f"[amifuse] FIB allocated at 0x{self._fib_mem.addr:x}-0x{fib_end:x} (size={FileInfoBlockStruct.get_size()})")
        self.mem.w_block(self._fib_mem.addr, b"\x00" * FileInfoBlockStruct.get_size())
        return self._fib_mem

    def _alloc_read_buf(self, size: int):
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

    def _alloc_bstr(self, text: str):
        encoded = text.encode("latin-1", errors="replace")
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
        if self._fh_pool:
            addr = self._fh_pool.pop()
            mem_obj = self._fh_mem.get(addr)
            if mem_obj:
                self.mem.w_block(addr, b"\x00" * FileHandleStruct.get_size())
            return addr
        mem_obj = self.vh.alloc.alloc_struct(FileHandleStruct, label="FUSE_FH")
        self.mem.w_block(mem_obj.addr, b"\x00" * FileHandleStruct.get_size())
        self._fh_mem[mem_obj.addr] = mem_obj
        return mem_obj.addr

    def _free_fh(self, fh_addr: int):
        if fh_addr in self._fh_mem:
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

    def list_dir(self, lock_bptr: int) -> List[Dict]:
        with self._lock:
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
            for iter_num in range(65536):
                pkt_addr, _ = self.launcher.send_examine_next(
                    self.state, lock_bptr, fib_mem.addr)
                reply = self._run_until_reply(pkt_addr, "examine_next")
                if reply is None or reply[0] == 0:
                    if self._debug:
                        res2 = reply[1] if reply else -1
                        print(f"[amifuse] list_dir: ExamineNext #{iter_num} ended res2={res2}")
                    break
                entry = _parse_fib(self.mem, fib_mem.addr)
                if self._debug:
                    print(f"[amifuse] list_dir: ExamineNext #{iter_num} name='{entry['name']}' type={entry['dir_type']}")
                if not entry["name"]:
                    break
                entries.append(entry)
            if root_lock:
                self.free_lock(root_lock)
            return entries

    def list_dir_path(self, path: str) -> List[Dict]:
        """List directory contents by path."""
        with self._lock:
            if path == "/":
                return self.list_dir(0)
            lock, _, locks = self.locate_path(path)
            if lock == 0:
                return []
            entries = self.list_dir(lock)
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
        """Best-effort name: RDB drive name, else first dir entry, else fallback."""
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
            # try RDB partition name
            try:
                if self.backend.rdb and self.backend.rdb.parts:
                    name = self.backend.rdb.parts[0].part_blk.drv_name
                    if name:
                        self._volname = name
                        return name
            except Exception:
                pass
            # try root listing
            entries = self.list_dir(0)
            for ent in entries:
                if ent.get("name"):
                    self._volname = ent["name"]
                    return ent["name"]
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
                buf_mem = self._alloc_read_buf(n)
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

        A failed write of more than one packet that would have extended
        the file shrinks it back to its old end and keeps the error, as
        _write_past_end does after a failed fill. Only such a write
        measures the old end first, with two seeks. A write of one packet
        costs nothing more but is not undone: pfs3aio and SmartFilesystem
        leave nothing of a packet they fail, but FastFileSystem keeps the
        part it wrote. A write into a file whose end cannot be measured
        goes out without the undo. On pfs3aio and SmartFilesystem that is
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
            if len(data) <= IO_CHUNK:
                return self._write_packets(fh_addr, data)
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
                buf_mem = self._alloc_read_buf(len(chunk))
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

    def write_handle_at(self, fh_addr: int, offset: int, data: bytes) -> Tuple[int, int]:
        """Write data at offset and return (dp_Res1, dp_Res2) like write_handle_reply().

        A write past end of file first fills the gap with zeros, as POSIX
        requires. Any other failed seek, including one to an offset that
        does not fit in a LONG, fails the write instead of writing at the
        handle's old position.
        """
        with self._lock:
            result, _ = self.seek_handle(fh_addr, offset)
            if result is SeekResult.PAST_END:
                return self._write_past_end(fh_addr, offset, data)
            if result is not SeekResult.OK:
                return -1, 0
            return self.write_handle_reply(fh_addr, data)

    def _write_past_end(self, fh_addr: int, offset: int, data: bytes) -> Tuple[int, int]:
        """Fill the file with zeros from its end to offset, then write data.

        AmigaDOS cannot seek past end of file, so this writes the gap
        instead. Returns (dp_Res1, dp_Res2) like write_handle_reply(). With
        no data, the file only grows to offset, as truncate() needs. A
        write that fails once the fill has begun shrinks the file back to
        its old end, so the zeros do not stay behind and fill the volume.

        The bridge lock is held until the fill ends, and a writable mount
        serves one request at a time, so a large gap stalls the whole mount.

        ACTION_SET_FILE_SIZE would extend the file in one packet, but FFS,
        OFS, pfs3aio and SFS leave the new range holding whatever its blocks
        held before, such as a deleted file's data.
        """
        with self._lock:
            end = self._handle_end(fh_addr)
            # ERROR_SEEK_ERROR inside the file (AROS afs cannot follow a
            # damaged file's extension blocks) is a failure, not a gap.
            if end is None or end > offset:
                return -1, 0
            # Fail before writing anything when the volume cannot hold the
            # gap and the data. Block overhead can still run the fill out of
            # space near the limit, and the handler then reports disk full.
            info = self.get_disk_info()
            if info and info["bytes_per_block"]:
                bpb = info["bytes_per_block"]
                free_blocks = max(info["num_blocks"] - info["num_blocks_used"], 0)
                # The file's last block may still have room before a new one
                # is needed.
                if offset - end + len(data) > free_blocks * bpb + (-end % bpb):
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
                        others: Sequence[int] = ()) -> Tuple[int, int]:
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

        SetFileSize() leaves no other handle on the file past its new end.
        FFS stops a shrink at such a handle and replies with the size it
        reached, and SFS refuses it with ERROR_OBJECT_IN_USE. After either
        reply, each handle in others moves to the start of its file and
        the shrink is sent once more. A shrink that gets no reply is not
        sent again: the handler may still hold its packet, and more
        packets would reuse its memory sooner. For the same reason the
        moves end at the first seek that fails, and the shrink is then not
        sent again. The size returned can still differ from length.
        """
        # A fill past 0x7FFFFFFF would leave a file no seek can reach the
        # end of, and ACTION_SET_FILE_SIZE carries the size in a LONG.
        if not 0 <= length <= _LONG_MAX:
            return -1, 0
        with self._lock:
            # A truncate to 0 cannot grow the file, so it skips the seeks.
            grow = False
            if length:
                end = self._handle_end(fh_addr)
                if end == length:
                    # Nothing to change, and BFFS would refuse the packet.
                    return length, 0
                if end is not None:
                    grow = length > end
                else:
                    result, _ = self.seek_handle(fh_addr, length)
                    if result is SeekResult.FAILED:
                        return -1, 0
                    grow = result is SeekResult.PAST_END
            if grow:
                # _write_past_end measures the end again. Two more seeks
                # cost little next to the zeros it then writes.
                written, res2 = self._write_past_end(fh_addr, length, b"")
                return (length, 0) if written >= 0 else (-1, res2)
            size, res2 = self.set_handle_size_reply(fh_addr, length)
            # Send it again only after a reply that shows the stop: a size
            # past length, as from FFS, or ERROR_OBJECT_IN_USE (202) from
            # SFS. Any other refusal, such as BFFS's DOSFALSE, is not about
            # other handles, and with no reply the handler may still hold
            # the packet.
            if others and (size > length
                           or (size < 0 and res2 == ERROR_OBJECT_IN_USE)):
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
            buf_mem = self._alloc_read_buf(1)
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

    def flush_volume(self):
        """Flush the handler's buffers to disk. Call on unmount."""
        with self._lock:
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
        """Inhibit then uninhibit the handler to clear all internal state."""
        with self._lock:
            if self.state.crashed:
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


class AmigaFuseFS(_FuseOperations):
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
        mountpoint: Optional[Path] = None,
    ):
        self.bridge = bridge
        self._debug = debug
        self._mountpoint = mountpoint
        self._unmount_control = None
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
        # open(), cleared by release()/unlink().
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
        self._last_op_time = time.time()
        self._op_count = 0
        # Replies to timer.device requests while the mount is idle; see
        # _run_timer_thread.
        self._timer_thread: Optional[threading.Thread] = None
        self._timer_stop = False
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

    def _get_cached_stat(self, path: str) -> Optional[Dict]:
        """Return cached stat result if still valid, else None."""
        with self._cache_lock:
            if path in self._stat_cache:
                ts, result = self._stat_cache[path]
                if (time.time() - ts < self._cache_ttl) or path in self._pinned_stats:
                    return result
                del self._stat_cache[path]
        return None

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
        if fh is not None:
            with self._fh_lock:
                path = _handle_path(self._fh_cache.get(fh), path)
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
        # Check if path has a pending (deferred) delete - hide from callers
        with self._fh_lock:
            pending = path in self._pending_deletes
        if pending:
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
        full path (getattr) and a bare entry name (readdir) work.
        """
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
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
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

    def _filter_pending_deletes(self, path: str, entries: List[str]) -> List[str]:
        """Drop directory entries whose full path has a pending (deferred) delete."""
        with self._fh_lock:
            if not self._pending_deletes:
                return entries
            return [
                e for e in entries
                if e in (".", "..") or (
                    (path.rstrip("/") + "/" + e if path != "/" else "/" + e)
                    not in self._pending_deletes
                )
            ]

    def opendir(self, path):
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
        try:
            self.bridge.flush_volume()
            self.bridge.inhibit_cycle()
            res1, res2 = self.bridge.delete_dir_atomic(resolved_path)
            if res1 == 0:
                with self._fh_lock:
                    self._pending_deletes.add(resolved_path)
                self._log_op("releasedir", resolved_path, "deferred delete failed permanently, path hidden until unmount")
            else:
                with self._cache_lock:
                    self._stat_cache.pop(resolved_path, None)
                    parent_path = resolved_path.rsplit("/", 1)[0] or "/"
                    self._dir_cache.pop(parent_path, None)
        except Exception as e:
            self._log_op("releasedir", resolved_path, f"deferred delete exception: {type(e).__name__}: {e}")
            with self._fh_lock:
                self._pending_deletes.add(resolved_path)
        # Only resolve parent dir deletes if deletion succeeded
        if resolved_path not in self._pending_deletes:
            self._resolve_pending_dir_deletes(resolved_path)
        return 0

    def readdir(self, path, fh):
        self._check_handler_alive()
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
        with entry["lock"]:
            if entry.get("closed"):
                raise FuseOSError(errno.EIO)
            data = data[:self._fit_file_size_limit(offset, len(data))]
            if not data:
                # POSIX: a zero-length write changes nothing, even past end
                # of file.
                return 0
            if entry["pos"] is None or offset != entry["pos"]:
                written, res2 = self.bridge.write_handle_at(fh_addr, offset, data)
            else:
                written, res2 = self.bridge.write_handle_reply(fh_addr, data)
            if written < 0:
                # The handler's position is unknown after a failed write or
                # seek, so the next write must seek explicitly.
                entry["pos"] = None
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
                # 0x7FFFFFFF is left alone: no seek can return it there, so
                # its next read would fail.
                others = [e for h, e in self._fh_cache.items()
                          if h != fh and "fh_addr" in e and not e.get("closed")
                          and (e["pos"] or 0) <= _LONG_MAX]
            if entry is None:
                raise FuseOSError(errno.EIO)
            fh_addr = entry["fh_addr"]
            with entry["lock"]:
                if entry.get("closed"):
                    raise FuseOSError(errno.EIO)
                # A shrink that stops short or is refused as in use moves
                # the other handles and is sent again. Every read and write
                # carries its offset, so the handler's position for another
                # handle is only a cache.
                size, res2 = self.bridge.truncate_handle(
                    fh_addr, length, [e["fh_addr"] for e in others])
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
                if size < 0:
                    raise FuseOSError(_WRITE_ERRNOS.get(res2, errno.EIO))
                entry["dirty"] = True  # Truncate modifies the file
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
        writes and truncates fail with EIO, and release() ends nothing.
        """
        bridge = self.bridge
        bridge.close_file(entry["fh_addr"])
        opened, res2 = bridge.open_file_reply(path, os.O_RDWR | os.O_TRUNC)
        emptied = False
        if opened is not None:
            written, res2 = bridge.write_empty_reply(opened[0])
            emptied = written == 0
            if not emptied and res2:
                bridge.close_file(opened[0])
                if opened[1]:
                    bridge.free_lock(opened[1])
                opened = None
        if opened is None and res2:
            opened = bridge.open_file(path, os.O_RDWR)
        if entry.get("parent_lock"):
            bridge.free_lock(entry["parent_lock"])
        if opened is None:
            entry.update(fh_addr=0, parent_lock=0, closed=True)
        else:
            entry["fh_addr"], entry["parent_lock"] = opened
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
        dir_path, name = self._split_path(path)
        if not name:
            raise FuseOSError(errno.EINVAL)
        lock_bptr, _, locks = self.bridge.locate_path(dir_path)
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
                        self._stat_cache.pop(path, None)
                        self._dir_cache.pop(dir_path, None)
                    for l in reversed(locks):
                        self.bridge.free_lock(l)
                    return 0
            # Free locks before raising (fixes a lock leak on error)
            for l in reversed(locks):
                self.bridge.free_lock(l)
            raise FuseOSError(errno.EIO)
        with self._cache_lock:
            self._stat_cache.pop(path, None)
            self._dir_cache.pop(dir_path, None)
        self._pinned_stats.discard(path)
        for l in reversed(locks):
            self.bridge.free_lock(l)
        return 0

    def rmdir(self, path):
        """Remove a directory, deferring if children have pending deletes."""
        self._log_op("rmdir", path, "")
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        dir_path, name = self._split_path(path)
        if not name:
            raise FuseOSError(errno.EINVAL)
        res1, res2 = self.bridge.delete_dir_atomic(path)
        if res1 == 0 and res2 == 202:
            # OBJECT_IN_USE — inhibit cycle clears handler internal state, retry
            self.bridge.inhibit_cycle()
            res1, res2 = self.bridge.delete_dir_atomic(path)
        self._log_op("rmdir", path, f"delete_dir_atomic res1={res1} res2={res2}")
        if res1 == 0:
            if res2 == 202:
                # OBJECT_IN_USE: pfs3aio refuses while a file inside is open,
                # even an unlinked one. Defer only where a retry will come:
                # release() of the last pending child, or releasedir() of a
                # FUSE directory handle open on this path.
                with self._fh_lock:
                    pending_children = self._pending_children(path)
                    dir_open = any(
                        v.get("dir_handle") and v.get("path") == path
                        for v in self._fh_cache.values()
                    )
                    if pending_children:
                        self._pending_dir_deletes[path] = pending_children
                    if pending_children or dir_open:
                        self._pending_deletes.add(path)
                if not (pending_children or dir_open):
                    # Nothing would retry. pfs3aio answers 202 before 216, so
                    # a directory that still lists entries is not empty.
                    err = errno.ENOTEMPTY if self.bridge.list_dir_path(path) else errno.EBUSY
                    self._log_op("rmdir", path, f"in use, nothing would retry: {errno.errorcode[err]}")
                    raise FuseOSError(err)
                with self._cache_lock:
                    self._stat_cache.pop(path, None)
                    self._dir_cache.pop(dir_path, None)
                    self._dir_cache.pop(path, None)
                self._log_op("rmdir", path, "deferred (object in use)")
                return 0
            if res2 == 216:
                # DIRECTORY_NOT_EMPTY — defer only if pending children exist.
                with self._fh_lock:
                    pending_children = self._pending_children(path)
                    if pending_children:
                        self._pending_dir_deletes[path] = pending_children.copy()
                        self._pending_deletes.add(path)
                if pending_children:
                    with self._cache_lock:
                        self._stat_cache.pop(path, None)
                        self._dir_cache.pop(dir_path, None)
                        self._dir_cache.pop(path, None)
                    self._log_op("rmdir", path, "deferred (pending child deletes)")
                    return 0
                # No pending children — genuinely not empty
                raise FuseOSError(errno.ENOTEMPTY)
            # Unrecognized error
            raise FuseOSError(errno.EIO)
        # Immediate success
        self._forget_pending_delete(path)
        with self._cache_lock:
            self._stat_cache.pop(path, None)
            self._dir_cache.pop(dir_path, None)
            self._dir_cache.pop(path, None)
        return 0

    def _pending_children(self, path: str) -> set:
        """Direct children of path with a pending delete. Caller holds _fh_lock.

        Direct children only: _resolve_pending_dir_deletes strikes a deleted
        path off its parent's set and no other.
        """
        children = {p for p in self._pending_deletes if p.rsplit("/", 1)[0] == path}
        children.update(d for d in self._pending_dir_deletes if d.rsplit("/", 1)[0] == path)
        return children

    def _forget_pending_delete(self, path: str):
        """Stop hiding path once its delete is done or a new object holds the name."""
        with self._fh_lock:
            self._pending_deletes.discard(path)
            self._pending_dir_deletes.pop(path, None)

    def mkdir(self, path, mode):
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
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
        src_dir, src_name = self._split_path(old)
        dst_dir, dst_name = self._split_path(new)
        if not src_name or not dst_name:
            raise FuseOSError(errno.EINVAL)
        src_lock, _, src_locks = self.bridge.locate_path(src_dir)
        dst_lock, _, dst_locks = self.bridge.locate_path(dst_dir)
        try:
            if (src_lock == 0 and src_dir != "/") or (dst_lock == 0 and dst_dir != "/"):
                raise FuseOSError(errno.ENOENT)
            res1, _ = self.bridge.rename_object(src_lock, src_name, dst_lock, dst_name)
            if res1 == 0:
                raise FuseOSError(errno.EIO)
        finally:
            # Free the dir locks on every exit; a leaked lock lingers in
            # handler memory and can block later deletes with OBJECT_IN_USE.
            for l in reversed(src_locks):
                self.bridge.free_lock(l)
            for l in reversed(dst_locks):
                self.bridge.free_lock(l)
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
        return 0

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
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        # Ignore timestamp updates for now.
        return 0

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
            try:
                dir_path, name = self._split_path(parent_dir)
                lock_bptr, _, locks = self.bridge.locate_path(dir_path)
                if dir_path == "/" and lock_bptr == 0:
                    lock_bptr, _ = self.bridge.locate(0, "")
                    if lock_bptr:
                        locks.append(lock_bptr)
                if lock_bptr != 0:
                    # pfs3aio stops answering after an inhibit cycle with a
                    # file open deeper in the tree, and SmartFilesystem drops
                    # every open handle (#129). Cycle only with no file open.
                    with self._fh_lock:
                        files_open = any("fh_addr" in v for v in self._fh_cache.values())
                    if not files_open:
                        self.bridge.inhibit_cycle()
                    res1, res2 = self.bridge.delete_object(lock_bptr, name)
                    self._log_op("release", parent_dir, f"deferred rmdir res1={res1} res2={res2}")
                    if res1 == 0:
                        # Directory still not empty (new files created?) - silently discard
                        self._log_op("release", parent_dir, "deferred rmdir failed (dir not empty), discarding")
                        for l in reversed(locks):
                            self.bridge.free_lock(l)
                        # Delete failed - _pending_deletes still has it, just stop
                        break
                else:
                    # Could not locate parent - _pending_deletes still has it, just stop
                    break
                for l in reversed(locks):
                    self.bridge.free_lock(l)
                # Delete succeeded - now remove from _pending_deletes
                with self._fh_lock:
                    self._pending_deletes.discard(parent_dir)
                with self._cache_lock:
                    self._stat_cache.pop(parent_dir, None)
                    self._dir_cache.pop(dir_path, None)
            except Exception:
                self._log_op("release", parent_dir, "deferred rmdir exception, discarding")
                # Delete failed - _pending_deletes still has it, just stop
                break
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
            try:
                dir_path, name = self._split_path(resolved_path)
                lock_bptr, _, locks = self.bridge.locate_path(dir_path)
                if dir_path == "/" and lock_bptr == 0:
                    lock_bptr, _ = self.bridge.locate(0, "")
                    if lock_bptr:
                        locks.append(lock_bptr)
                if lock_bptr != 0:
                    res1, res2 = self.bridge.delete_object(lock_bptr, name)
                    self._log_op("release", resolved_path, f"deferred delete res1={res1} res2={res2}")
                    if res1 != 0:
                        delete_succeeded = True
                    else:
                        with self._fh_lock:
                            self._pending_deletes.add(resolved_path)
                else:
                    with self._fh_lock:
                        self._pending_deletes.add(resolved_path)
                for l in reversed(locks):
                    self.bridge.free_lock(l)
                if delete_succeeded:
                    with self._cache_lock:
                        self._stat_cache.pop(resolved_path, None)
                        self._dir_cache.pop(dir_path, None)
            except Exception as e:
                self._log_op("release", resolved_path, f"deferred delete exception: {type(e).__name__}: {e}")
                with self._fh_lock:
                    self._pending_deletes.add(resolved_path)
            # Resolve any deferred parent directory deletes only on success
            if delete_succeeded:
                self._resolve_pending_dir_deletes(resolved_path)
        return 0

    def init(self, path):
        """Called when filesystem is mounted. Notify shell on Windows."""
        if self._unmount_control is not None:
            self._unmount_control.start()
        # init runs in the mount process, after libfuse has daemonised.
        self._start_timer_thread()
        if sys.platform.startswith("win") and self._mountpoint:
            from .platform import notify_shell_drive_change
            notify_shell_drive_change(str(self._mountpoint), added=True)

    # The longest the timer thread sleeps at once. A request armed with an
    # earlier deadline wakes it anyway.
    _TIMER_MAX_SLEEP = 3600.0

    def _start_timer_thread(self):
        timer = getattr(getattr(self.bridge, "vh", None), "timer_device", None)
        if timer is None or self._timer_thread is not None:
            return
        self._timer_stop = False
        self._timer_thread = threading.Thread(
            target=self._run_timer_thread, args=(timer,),
            name="amifuse-timer", daemon=True,
        )
        self._timer_thread.start()

    def _run_timer_thread(self, timer):
        """Reply to timer.device requests when due, between FUSE operations.

        Without it an idle handler never sees time pass, so work it defers
        to a timer, such as a write-back, would wait for the next operation.
        It sleeps until the earliest deadline, or until a request arms an
        earlier one, and costs nothing while no request is pending.
        """
        bridge = self.bridge
        while True:
            with bridge._lock:
                if self._timer_stop:
                    return
                timeout = None
                try:
                    if not _handler_has_crashed(bridge):
                        timeout = bridge.run_due_timers()
                except Exception:
                    # It goes on, as a mount does when a FUSE operation
                    # raises in the same bridge code: fusepy answers that
                    # with an error, and the next operation runs the handler
                    # from where the burst left it. A handler that crashed
                    # stops the thread below.
                    if self._debug:
                        import traceback
                        print("[amifuse] timer: error delivering timers:", flush=True)
                        traceback.print_exc()
                    timeout = HandlerBridge._TIMER_RETRY
                if _handler_has_crashed(bridge):
                    # Shut the mount down, as a FUSE operation does when it
                    # finds the handler crashed.
                    self._schedule_crash_shutdown()
                    return
                # Cleared after run_due_timers, which reads the deadline
                # last under the lock, so a request the handler armed while
                # it ran is in the timeout. One armed after this point sets
                # the event again.
                timer.event.clear()
                # The clear may have erased the wake of a stop that came
                # while run_due_timers ran.
                if self._timer_stop:
                    return
            if timeout is not None:
                # At least one VBLANK tick, even when a request is due again
                # at once, so that FUSE operations waiting for the lock get
                # it: a resume that outlasts the handler's re-arm delay would
                # otherwise retake it at once, again and again. Event.wait
                # raises OverflowError above threading.TIMEOUT_MAX.
                timeout = min(max(timeout, timer.MIN_DELAY), self._TIMER_MAX_SLEEP)
            timer.event.wait(timeout)

    def _stop_timer_thread(self):
        thread = getattr(self, "_timer_thread", None)
        if thread is None:
            return
        self._timer_thread = None
        self._timer_stop = True
        timer = getattr(getattr(self.bridge, "vh", None), "timer_device", None)
        if timer is not None:
            timer.event.set()
        # Not under the bridge lock: the thread needs it to see the stop.
        thread.join()

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
                self.bridge.flush_volume()
        except Exception as e:
            success = False
            print(f"[amifuse] WARNING: flush failed: {e}", flush=True)
        try:
            shutdown = getattr(getattr(self.bridge, "vh", None), "shutdown", None)
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
    """Get partition name and dostype from RDB."""
    from .rdb_inspect import open_rdisk, find_partition_mbr_index
    mbr_idx = find_partition_mbr_index(image, block_size, partition) if partition else None
    blkdev, rdisk, _mbr_ctx = open_rdisk(image, block_size=block_size, mbr_partition_index=mbr_idx)
    try:
        if partition is None:
            part = rdisk.get_partition(0)
        else:
            part = rdisk.find_partition_by_string(str(partition))
        if part is None:
            return {"name": "AmigaFS", "dostype": None}
        return {
            "name": str(part.get_drive_name()),
            "dostype": part.part_blk.dos_env.dos_type,
        }
    finally:
        rdisk.close()
        blkdev.close()


def get_partition_name(image: Path, block_size: Optional[int], partition: Optional[str]) -> str:
    """Get the partition name from RDB without starting the handler."""
    return get_partition_info(image, block_size, partition)["name"]


def _dos_type_tag(dos_type: int) -> str:
    """Return a DOS type's tag, e.g. "PFS3", for printing.

    An image chooses the bytes, so a character outside 0x20-0x7E is shown
    escaped, e.g. "\\x1b", and never reaches the terminal. A backslash is
    escaped too, as "\\x5c", so that an escape cannot be faked.
    """
    import amitools.fs.DosType as DosType

    return "".join(
        ch if " " <= ch <= "~" and ch != "\\" else f"\\x{ord(ch):02x}"
        for ch in DosType.num_to_tag_str(dos_type)
    )


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
    from .rdb_inspect import open_rdisk, find_partition_mbr_index

    mbr_idx = find_partition_mbr_index(image, block_size, partition) if partition else None
    blkdev, rdisk, _mbr_ctx = open_rdisk(image, block_size=block_size, mbr_partition_index=mbr_idx)
    try:
        # Get the partition and its dostype
        if partition is None:
            part = rdisk.get_partition(0)
        else:
            part = rdisk.find_partition_by_string(str(partition))
        if part is None:
            return None

        target_dostype = part.part_blk.dos_env.dos_type
        dt_str = _dos_type_tag(target_dostype)

        # Search filesystem blocks for matching dostype
        for fs in rdisk.fs:
            if fs.fshd.dos_type == target_dostype:
                # Found matching filesystem - extract to temp file
                data = fs.get_data()
                # Create temp file that persists until program exits. It is
                # named by number: the tag's characters come from the image.
                fd, temp_path = tempfile.mkstemp(
                    suffix=f"_{target_dostype:08x}.handler", prefix="amifuse_")
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
                   for dos_type, warning in getattr(rdisk, "damaged_fs", ())
                   if dos_type in (target_dostype, None)]
        if damaged:
            known = any(dos_type == target_dostype for dos_type, _ in damaged)
            raise DriverNotFound(
                f"Partition {part.get_drive_name()} ({dt_str}) "
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
    drivers folder, the bundled FastFileSystem, the dev fallback). An ISO
    needs --driver. part_info is get_partition_info()'s result when the
    caller has it.

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
            f"ISO 9660 image detected (volume: {iso_info.volume_id}).\n"
            "ISO images don't contain embedded filesystem drivers.\n"
            "You need to specify a filesystem handler with --driver.\n"
            "For ISO 9660, use CDFileSystem from an AmigaOS installation."
        )

    result = extract_embedded_driver(image, block_size, partition)
    if result is not None:
        temp_driver, dt_str, dostype = result
        return ResolvedDriver(
            temp_driver, "rdb", f"{dt_str}/0x{dostype:08x} (from RDB)", temporary=True)

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
        f"Partition {name} has no filesystem driver for "
        f"{driver_resolver.dos_tag(dostype)} in its RDB.",
    )


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
                raise SystemExit(f"Error: {e}")
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
            print(f"Mounting ISO 9660 image ({iso_info.volume_id}) from {image}")
        else:
            print(f"Mounting partition '{part_name}' from {image}")
        print(f"Filesystem driver: {driver_desc}")
        print(f"Mount point: {mountpoint}")
        if sys.platform.startswith("win") and fuse_mountpoint.startswith("\\\\.\\"):
            print("[amifuse] global drive; unmount from an Administrator shell")
        if foreground:
            print("[amifuse] interactive mode; press Ctrl+C to unmount")
        else:
            print(f"[amifuse] daemon mode; unmount with: amifuse unmount {mountpoint}")

        if write:
            # Guard against accidental writes without explicit intent.
            print("[amifuse] write mode enabled; ensure the image is backed up")

        if icons:
            print("[amifuse] icon mode enabled; Amiga icons will appear as macOS custom icons")

        try:
            bridge = HandlerBridge(
                image,
                driver,
                block_size=block_size,
                read_only=not write,
                debug=debug,
                trace=trace,
                partition=partition,
                adf_info=adf_info,
                iso_info=iso_info,
            )
        except (OSError, ValueError) as exc:
            # Lock conflicts and invalid partitions are expected refusals.
            if debug:
                raise
            raise SystemExit(f"Cannot mount {image}: {exc}") from exc
        if _handler_has_crashed(bridge):
            bridge.close()
            raise SystemExit("Filesystem handler crashed during startup; mount aborted.")

        # Let default signal handling work - FUSE will call destroy() on unmount
        volname = volname_opt or bridge.volume_name()
        if _handler_has_crashed(bridge):
            bridge.close()
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
            operations = AmigaFuseFS(bridge, debug=debug, icons=icons, mountpoint=mountpoint)
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
            bridge.close()
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


def format_volume(
    image: Path,
    driver: Optional[Path],
    block_size: Optional[int],
    partition: str,
    volname: str = "Empty",
    debug: bool = False,
):
    bridge = None

    # Name a missing image or a directory plainly, as the file commands do.
    if not is_windows_disk(image):
        if not image.exists():
            raise SystemExit(f"Error: image file not found: {image}")
        if image.is_dir():
            raise SystemExit(f"Error: image is a directory: {image}")

    # Get partition dostype from RDB
    try:
        part_info = get_partition_info(image, block_size, partition)
    except (OSError, ValueError) as exc:
        # A missing or unreadable image, or one without a usable RDB.
        if debug:
            raise
        raise SystemExit(f"Error: {exc}") from exc
    dostype = part_info["dostype"]
    if dostype is None:
        raise SystemExit(f"Could not determine dostype for partition '{partition}'.")
    dt_str = _dos_type_tag(dostype)

    # Resolve driver
    try:
        resolved = resolve_driver(image, driver, block_size, partition, part_info=part_info)
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
        print(f"Formatting partition '{partition}' in {image}")
        print(f"Filesystem driver: {driver_desc}")
        print(f"Volume name: {volname}")
        print(f"DOS type: {dt_str} (0x{dostype:08x})")

        try:
            bridge = HandlerBridge(
                image,
                driver,
                block_size=block_size,
                read_only=False,
                debug=debug,
                partition=partition,
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
                205: "Object in use",
                218: "Not a valid DOS disk",
                225: "Not a DOS disk",
                226: "Wrong disk type",
                232: "Disk is write-protected",
            }
            error_desc = error_msgs.get(res2, f"error code {res2}")
            raise SystemExit(f"Format failed: {error_desc}")

        if debug:
            print(f"[amifuse] FORMAT reply: res1={res1} res2={res2}")

        # Flush the newly formatted volume, then close this formatter runtime.
        # Reopening on the same bridge is redundant and has proven unstable for
        # SFS. Callers that want to verify the new volume should do so in a
        # fresh HandlerBridge.
        pkt_addr, _ = bridge.launcher.send_flush(bridge.state)
        bridge._run_until_reply(
            pkt_addr, "format flush", max_iters=500, cycles=2_000_000
        )
        _raise_if_handler_crashed(bridge, "format flush")
        bridge.backend.sync()

        print(f"Format complete. Volume '{volname}' created on partition '{partition}'.")
    finally:
        if bridge is not None:
            bridge.close()
        if temp_driver is not None and temp_driver.exists():
            temp_driver.unlink()


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


def _check_file_command_mount(bridge, command: str, use_json: bool) -> MountState:
    """Reject unusable volumes before resolving or creating a file."""
    import json

    state = bridge.is_mounted()
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


def _create_bridge_from_args(args, command: str, read_only: bool = True):
    """Create a HandlerBridge from CLI arguments.

    Handles driver resolution (RDB extraction or explicit --driver),
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
        if use_json:
            print(_json.dumps(_json_error(command, "IMAGE_INVALID", str(e))))
            sys.exit(1)
        raise SystemExit(f"Error: {e}")
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
            block_size=block_size,
            read_only=read_only,
            debug=debug,
            partition=partition,
            adf_info=adf_info,
            iso_info=iso_info,
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
        if use_json:
            if in_use:
                error = _json_error(command, "IMAGE_IN_USE", str(e))
            else:
                error = _json_error(command, "HANDLER_ERROR",
                    f"Failed to initialize filesystem handler: {e}")
            print(_json.dumps(error))
            sys.exit(1)
        if in_use:
            raise SystemExit(f"Error: {e}")
        raise SystemExit(f"Error initializing handler: {e}")

    return bridge, temp_driver


def _format_protection(prot_bits: int) -> str:
    """Format Amiga protection bits as a human-readable string."""
    prot = DosProtection(prot_bits)
    return str(prot)


def _ls_recursive(bridge, root_path: str) -> list:
    """Recursively list all entries under root_path."""
    entries = []
    stack = [root_path]
    while stack:
        current = stack.pop()
        raw = bridge.list_dir_path(current)
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
                dir_children.append(child)
        # Traverse in sorted order for deterministic output
        stack.extend(reversed(sorted(dir_children)))
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
                if ent["type"] == "dir":
                    print(f"  {ent['name']:30s}  <dir>     {ent['protection']}")
                else:
                    print(f"  {ent['name']:30s}  {ent['size']:>10d}  {ent['protection']}")
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
                print(f"Volume: {volname}")
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


def cmd_write(args):
    """Handle the 'write' subcommand."""
    import json

    use_json = getattr(args, "json", False)
    debug = getattr(args, "debug", False)
    amiga_path = args.file
    host_path = Path(args.input)  # --in maps to args.input (dest="input")

    # Validate host source file
    if not host_path.exists():
        if use_json:
            print(json.dumps(_json_error("write", "SOURCE_NOT_FOUND",
                f"Source file not found: {host_path}")))
            sys.exit(1)
        raise SystemExit(f"Error: source file not found: {host_path}")

    if host_path.is_dir():
        if use_json:
            print(json.dumps(_json_error("write", "INVALID_ARGUMENT",
                f"Source is a directory, not a file: {host_path}")))
            sys.exit(1)
        raise SystemExit(f"Error: source is a directory: {host_path}")

    host_size = host_path.stat().st_size

    # Refuse before the bridge exists: opening the destination with O_TRUNC
    # (ACTION_FINDOUTPUT) would already empty an existing file.
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

    # Safety warning (stderr so it doesn't interfere with JSON output)
    print(f"WARNING: This will modify the disk image: {args.image}", file=sys.stderr)
    print(f"  Source: {host_path} ({host_size:,} bytes)", file=sys.stderr)
    print(f"  Destination: {amiga_path}", file=sys.stderr)
    print(f"  Ensure you have a backup of the image before proceeding.", file=sys.stderr)

    bridge, temp_driver = _create_bridge_from_args(args, "write", read_only=False)
    flushed = False  # Guard flag to avoid double flush
    write_started = False
    mount_details = None
    try:
        state = _check_file_command_mount(bridge, "write", use_json)
        mount_details = _mount_state_details(state)
        normalized = "/" + amiga_path.lstrip("/")

        # Create parent directories if needed
        write_started = True
        _ensure_parent_dirs(bridge, normalized, use_json=use_json, debug=debug,
                            error_details=mount_details)

        # Open for write (create/truncate)
        fh_result = bridge.open_file(normalized, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        if fh_result is None:
            if use_json:
                print(json.dumps(_json_error("write", "HANDLER_ERROR",
                    f"Failed to open file for writing: {amiga_path}", details=mount_details)))
                sys.exit(1)
            raise SystemExit(f"Error: failed to open file for writing: {amiga_path}")

        # Note: _dir_lock from open_file() is never freed. This is a
        # pre-existing pattern also present in cmd_hash().
        # Not a blocker -- the lock is released when the bridge is destroyed.
        fh_addr, _dir_lock = fh_result
        try:
            # Handler crash safety: If the handler crashes during the
            # write loop, the error paths handle it gracefully:
            # - flush_volume() checks state.crashed and returns early (safe)
            # - close_file() sends a packet that gets no reply (harmless)
            # - write_handle() returning -1 triggers the error path below
            chunk_size = IO_CHUNK
            bytes_written = 0
            with open(host_path, "rb") as f:
                while True:
                    data = f.read(chunk_size)
                    if not data:
                        break
                    if bytes_written + len(data) > _MAX_FILE_SIZE:
                        # The source holds more than its stat() size: it grew
                        # while it was read, or it is a stream that reports 0
                        if use_json:
                            print(json.dumps(_json_error("write", "FILE_TOO_LARGE",
                                f"Source holds more than {_MAX_FILE_SIZE} bytes, "
                                f"the largest AmigaDOS file; the destination "
                                f"keeps the first {bytes_written} bytes",
                                {**mount_details, "size": host_size,
                                 "bytes_written": bytes_written,
                                 "max_size": _MAX_FILE_SIZE})))
                            sys.exit(1)
                        raise SystemExit(
                            f"Error: source holds more than {_MAX_FILE_SIZE} "
                            f"bytes, the largest AmigaDOS file; the destination "
                            f"keeps the first {bytes_written} bytes")
                    n = bridge.write_handle(fh_addr, data)
                    # Distinguish write failure modes:
                    # n < 0:  No reply from handler (crashed or unresponsive)
                    # n == 0: Handler returned DOSFALSE (write operation failed)
                    # 0 < n < len(data): Partial write (likely disk full)
                    # n == len(data): Success
                    if n < 0:
                        # No reply from handler
                        if use_json:
                            print(json.dumps(_json_error("write", "WRITE_ERROR",
                                f"Write failed at offset {bytes_written}: "
                                f"no reply from handler",
                                {**mount_details, "bytes_written": bytes_written,
                                 "expected": host_size})))
                            sys.exit(1)
                        raise SystemExit(
                            f"Error: write failed at offset {bytes_written}: "
                            f"no reply from handler")
                    if n == 0:
                        # Handler returned DOSFALSE -- write operation failed
                        if use_json:
                            print(json.dumps(_json_error("write", "WRITE_ERROR",
                                f"Write failed at offset {bytes_written}: "
                                f"handler returned failure (DOSFALSE)",
                                {**mount_details, "bytes_written": bytes_written,
                                 "expected": host_size})))
                            sys.exit(1)
                        raise SystemExit(
                            f"Error: write failed at offset {bytes_written}: "
                            f"handler returned failure")
                    if n < len(data):
                        # Partial write -- handler couldn't write all data
                        # (likely disk full). Record what was written then error.
                        bytes_written += n
                        if use_json:
                            print(json.dumps(_json_error("write", "WRITE_ERROR",
                                f"Partial write: wrote {bytes_written} of "
                                f"{host_size} bytes (disk full?)",
                                {**mount_details, "bytes_written": bytes_written,
                                 "expected": host_size})))
                            sys.exit(1)
                        raise SystemExit(
                            f"Error: partial write ({bytes_written}/{host_size} "
                            f"bytes, disk full?)")
                    bytes_written += n
        finally:
            bridge.close_file(fh_addr)

        # Flush is CRITICAL after writes -- without this, writes cached in
        # handler memory never reach the image file.
        bridge.flush_volume()
        flushed = True  # Mark as flushed to avoid double flush

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
                f"Write operation failed: {e}", details=mount_details)))
            sys.exit(1)
        raise SystemExit(f"Error during write: {e}")
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


def _partition_driver(image: Path, block_size: Optional[int], name: str):
    """Name the driver `mount --partition name` would use without --driver.

    Returns the driver's inspect --json object, or None, and a line of text:
    its description, or why there is none. An RDB driver is extracted to
    hash it, then deleted; its object has no path.
    """
    import hashlib

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
        open_rdisk, detect_adf, detect_iso, list_partitions,
        amiga_rdb_partitions, build_rdisk_desc, print_inspect_report,
        find_partition_mbr_index,
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
        print(f"  Volume ID: {iso_info.volume_id}")
        print(f"  Block size: {iso_info.block_size} bytes")
        print(f"  Total blocks: {iso_info.total_blocks}")
        return

    if use_json:
        # JSON mode: collect all RDBs into a single envelope. Unlike the
        # rdb-inspect tool (which skips unreadable RDBs), any open failure
        # is fatal here so scripts get a definitive error envelope.
        amiga_parts = amiga_rdb_partitions(args.image)
        multi_rdb = len(amiga_parts) > 1
        all_rdbs = []
        for rdb_idx in range(len(amiga_parts) if multi_rdb else 1):
            mbr_partition_index = rdb_idx if multi_rdb else None
            try:
                blkdev, rdisk, mbr_ctx = open_rdisk(
                    args.image, block_size=args.block_size,
                    mbr_partition_index=mbr_partition_index,
                )
            except IOError as e:
                envelope = _json_error("inspect", "OPEN_FAILED", str(e))
                envelope["image"] = str(args.image)
                print(_json.dumps(envelope, indent=2))
                sys.exit(1)
            try:
                all_rdbs.append(build_rdisk_desc(rdisk, mbr_ctx))
            finally:
                rdisk.close()
                blkdev.close()

        # Resolved with the image closed: a Windows disk admits one reader.
        for rdb_idx, desc in enumerate(all_rdbs):
            # A Parceiro disk's own RDB opens through every 0x76 entry, so
            # its copies here are one RDB, not a clash.
            parceiro = desc.get("mbr", {}).get("scheme") == "parceiro"
            seen = set()
            for part in desc["partitions"]:
                name = part["name"]
                earlier = name.lower() in seen
                seen.add(name.lower())
                # mount --partition picks the first RDB holding the name, so
                # a same-named partition in a later RDB cannot be mounted.
                selected = None
                if multi_rdb and not parceiro:
                    try:
                        selected = find_partition_mbr_index(
                            args.image, args.block_size, name)
                    except IOError:
                        # A failed lookup: the driver lookup below meets
                        # the same error and gives it as the note.
                        pass
                if selected is not None and selected != rdb_idx:
                    part["driver"] = None
                    part["driver_note"] = (
                        f"another RDB on this disk also has a partition named "
                        f"{name}, and mount --partition {name} uses that one")
                    continue
                # Within an RDB, mount --partition picks the first match too.
                if earlier:
                    part["driver"] = None
                    part["driver_note"] = _earlier_name_note(name)
                    continue
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
        print(_json.dumps(envelope, indent=2))
        return

    print_inspect_report(args.image, args.block_size, args.full)
    print("\nFilesystem driver per partition (without --driver):")
    listed = list_partitions(args.image, args.block_size)
    # Without a fallback, names repeat only within one RDB.
    seen = set()
    for part in listed.partitions:
        if part.name.lower() in seen:
            text = _earlier_name_note(part.name)
        else:
            text = _partition_driver(args.image, args.block_size, part.name)[1]
        seen.add(part.name.lower())
        print(f"  {part.name}: {text}")
    if listed.fallback_reason and not _is_parceiro(args.image, args.block_size):
        print(f"  Note: {listed.fallback_reason}")


def _earlier_name_note(name: str) -> str:
    """Why a partition named like an earlier one in its RDB has no driver."""
    return (f"an earlier partition in this RDB is also named {name}, "
            f"and mount --partition {name} uses that one")


def _is_parceiro(image: Path, block_size: Optional[int]) -> bool:
    """Whether the image's RDB is a Parceiro disk's own, beside its MBR.

    list_partitions finds that one RDB through every 0x76 entry, so its
    names repeat without clashing.
    """
    from .rdb_inspect import open_rdisk

    try:
        blkdev, rdisk, mbr_ctx = open_rdisk(image, block_size=block_size)
    except IOError:
        return False
    rdisk.close()
    blkdev.close()
    return mbr_ctx is not None and mbr_ctx.scheme == "parceiro"


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

    format_volume(
        args.image, args.driver, args.block_size,
        partition=args.partition,
        volname=args.volname,
        debug=args.debug,
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

    # inspect subcommand
    inspect_parser = subparsers.add_parser(
        "inspect", help="Inspect RDB partitions and filesystems."
    )
    inspect_parser.add_argument("image", type=Path, help="Disk image file or physical disk device")
    inspect_parser.add_argument(
        "--block-size", type=int, help="Override block size (defaults to auto/512)."
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
        "--block-size", type=int, help="Override block size (defaults to auto/512)."
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
        "format", help="Format an Amiga partition."
    )
    format_parser.add_argument("image", type=Path, help="Disk image file")
    format_parser.add_argument("partition", type=str, help="Partition name (e.g. DH0) or index.")
    format_parser.add_argument("volname", nargs="?", default="Empty", help="Volume name (default: Empty)")
    format_parser.add_argument("--driver", type=Path, help="Filesystem binary (default: RDB, your drivers folder, then the bundled FastFileSystem for DOS0-7)")
    format_parser.add_argument(
        "--block-size", type=int, help="Override block size (defaults to auto/512)."
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
        "--block-size", type=int,
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
        "--block-size", type=int,
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
        "--block-size", type=int,
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
        "--block-size", type=int,
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
        "--block-size", type=int,
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
