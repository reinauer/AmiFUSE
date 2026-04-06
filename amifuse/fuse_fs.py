"""
FUSE bridge that forwards basic filesystem ops into the Amiga handler
via our vamos bootstrap. Read/write is experimental and enabled with --write.
"""

import cProfile
import pstats

import argparse
import errno
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

# signal.SIGKILL is not defined on Windows; fall back to SIGTERM so that
# _kill_mount_owner_processes() can still compile and will use the strongest
# signal available.
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from fuse import FUSE, FuseOSError, LoggingMixIn, Operations  # type: ignore
except ImportError:
    FUSE = None

    class FuseOSError(RuntimeError):
        pass

    class LoggingMixIn:
        pass

    class Operations:
        pass

from .driver_runtime import BlockDeviceBackend
from .vamos_runner import VamosHandlerRuntime
from .bootstrap import BootstrapAllocator
from .process_mgr import ProcessManager
from .startup_runner import (
    HandlerLauncher,
    OFFSET_BEGINNING,
    _get_block_state,
    _clear_all_block_state,
    _snapshot_block_state,
    _restore_block_state,
)
from amitools.vamos.astructs.access import AccessStruct  # type: ignore
from amitools.vamos.libstructs.dos import FileInfoBlockStruct, FileHandleStruct, DosPacketStruct  # type: ignore
from amitools.vamos.lib.dos.DosProtection import DosProtection  # type: ignore


def _require_fuse():
    if FUSE is None:
        raise SystemExit(
            "Python FUSE bindings not found. Install fusepy (pip install fusepy) "
            "or build the local python-fuse bindings and add them to PYTHONPATH."
        )


def _handler_has_crashed(obj) -> bool:
    state = getattr(obj, "state", None)
    return getattr(state, "crashed", False) is True


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


import threading

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
        # For MBR images with multiple 0x76 partitions, find the right one
        mbr_idx = None
        if partition and adf_info is None and iso_info is None:
            from .rdb_inspect import find_partition_mbr_index
            mbr_idx = find_partition_mbr_index(image, block_size, partition)
        self.backend = BlockDeviceBackend(
            image, block_size=block_size, read_only=read_only, adf_info=adf_info,
            iso_info=iso_info, mbr_partition_index=mbr_idx,
        )
        self.backend.open()
        self.vh = VamosHandlerRuntime()
        # Use 68020 CPU for compatibility with SFS and other modern handlers
        self.vh.setup(cpu="68020")
        if trace:
            self.vh.enable_trace()
        self.mem = self.vh.alloc.get_mem()
        seg_baddr = self.vh.load_handler(driver)
        # Build DeviceNode/FSSM using seglist bptr
        ba = BootstrapAllocator(
            self.vh, image, partition=partition, adf_info=adf_info,
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
                        f"but image is only {image_size:,} bytes. Data may be incomplete."
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
        pkt = AccessStruct(self.mem, DosPacketStruct, self.state.stdpkt_addr)
        startup_res1 = pkt.r_s("dp_Res1")
        startup_res2 = pkt.r_s("dp_Res2")
        if not replies and startup_res1 == 0 and not self.state.crashed:
            if self._debug:
                print("[amifuse] No startup reply yet; attempting deferred signal flush before failing")
            if not self.state.main_loop_pc:
                from amitools.vamos.machine.regs import REG_D0
                cpu = self.vh.machine.cpu
                from amitools.vamos.lib.ExecLibrary import ExecLibrary as _EL
                _wps = _get_block_state(_EL, '_waitport_blocked_sp')
                _ws = _get_block_state(_EL, '_wait_blocked_sp')
                _wpr = _get_block_state(_EL, '_waitport_blocked_ret')
                _wr = _get_block_state(_EL, '_wait_blocked_ret')
                _wm = _get_block_state(_EL, '_wait_blocked_mask')
                _bsp = _wps if _wps is not None else _ws
                _bret = _wpr if _wpr is not None else _wr
                if _bsp is not None:
                    _ret = _bret if _bret is not None else self.mem.r32(_bsp)
                    if _ret >= 0x800:
                        self.state.main_loop_pc = _ret
                        self.state.main_loop_sp = _bsp + 4
                        if _wm is not None and _wm != 0:
                            self.state.wait_mask = _wm
                        self.state.initialized = True
                        self.state.block_state = _snapshot_block_state()
                _clear_all_block_state()
                cpu.w_reg(REG_D0, 0)
                self._set_saved_main_reg(REG_D0, 0)
                self._capture_main_loop_state()
            self._flush_pending_signals()
            replies = self._run_startup_until_replies(cycles=10_000, max_iters=2000)
            startup_res1 = pkt.r_s("dp_Res1")
            startup_res2 = pkt.r_s("dp_Res2")
        if self._debug:
            print(f"[amifuse] Startup packet result: res1={startup_res1} res2={startup_res2}")
        if startup_res1 == 0:
            # Handler rejected the disk - likely invalid filesystem signature
            error_msgs = {
                218: "Not a valid DOS disk (NDOS)",
                225: "Not a DOS disk",
                226: "Wrong disk type",
                303: "Object not found",
            }
            error_desc = error_msgs.get(startup_res2, f"error code {startup_res2}")
            raise SystemExit(f"Filesystem handler rejected the disk: {error_desc}")
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
            from amitools.vamos.lib.ExecLibrary import ExecLibrary as _EL

            _wps = _get_block_state(_EL, "_waitport_blocked_sp")
            _ws = _get_block_state(_EL, "_wait_blocked_sp")
            _wpr = _get_block_state(_EL, "_waitport_blocked_ret")
            _wr = _get_block_state(_EL, "_wait_blocked_ret")
            _wm = _get_block_state(_EL, "_wait_blocked_mask")
            _bsp = _wps if _wps is not None else _ws
            _bret = _wpr if _wpr is not None else _wr
            if _bsp is not None:
                _ret = _bret if _bret is not None else self.mem.r32(_bsp)
                if _ret >= 0x800:
                    self.state.main_loop_pc = _ret
                    self.state.main_loop_sp = _bsp + 4
                    if _wm is not None and _wm != 0:
                        self.state.wait_mask = _wm
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
                            f"pc=0x{_ret:x}, sp=0x{_bsp + 4:x}{mask_str}"
                        )
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
        self._closed = False

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        shutdown = getattr(getattr(self, "vh", None), "shutdown", None)
        if shutdown is not None:
            shutdown()
        backend = getattr(self, "backend", None)
        if backend is not None:
            backend.close()

    def _set_saved_main_reg(self, reg_num: int, value: int):
        """Mirror manual register changes into the saved main handler state."""
        if self.state.regs is not None:
            self.state.regs[reg_num] = value

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

            if self._debug and i < 3:
                cycles_run = getattr(rs, "cycles", 0) if rs else 0
                print(
                    f"[amifuse] startup iter {i}: PC 0x{old_pc:x}->0x{new_pc:x} "
                    f"cycles={cycles_run} done={getattr(rs, 'done', False)} "
                    f"error={getattr(rs, 'error', None)}"
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

            if getattr(rs, "error", None):
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

            if sleep_base > 0:
                time.sleep(sleep_time)
                sleep_time = min(sleep_time * 2, sleep_max)

        if self._debug and not replies:
            print(
                f"[amifuse] startup: exhausted {max_iters} iters with no "
                f"replies, pc=0x{self.state.pc:x}"
            )
        return replies

    def _run_until_replies(self, max_iters: int = 50, cycles: int = 200_000, sleep_base: float = 0.0005, sleep_max: float = 0.01, drain_non_essential: bool = True):
        """Run handler bursts until at least one reply is queued or iterations exhausted."""
        from amitools.vamos.lib.ExecLibrary import ExecLibrary
        replies = []
        sleep_time = sleep_base

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
                # Only deliver the DOS port signal during normal packet
                # processing.  Timer and other signals were already
                # handled during _flush_pending_signals at init time.
                # Delivering ALL signals here causes a timer storm: the
                # handler processes the timer, re-arms it, and SendIO
                # queues another reply, triggering the timer on every
                # single packet until the handler state gets corrupted.
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
                    dos_signal = port_signal
                self.vh.machine.cpu.w_reg(0, dos_signal)  # REG_D0
                self._set_saved_main_reg(0, dos_signal)
                # Also set tc_SigRecvd in case handler checks the task structure
                from amitools.vamos.libstructs.exec_ import TaskStruct
                sigrecvd_off = TaskStruct.sdef.find_field_def_by_name("tc_SigRecvd").offset
                proc_addr = self.state.process_addr
                self.mem.w32(proc_addr + sigrecvd_off, dos_signal)
                if self._debug:
                    print(
                        f"[amifuse] Resuming: port_signal=0x{port_signal:x} "
                        f"wait_mask=0x{wait_mask:x} D0=0x{dos_signal:x} "
                        f"tc_SigRecvd@0x{proc_addr + sigrecvd_off:x}=0x{dos_signal:x}"
                    )

        for i in range(max_iters):
            old_pc = self.state.pc
            self.launcher.run_burst(self.state, max_cycles=cycles, debug=self._debug)
            rs = self.state.run_state
            new_pc = self.state.pc
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
            polled = self.launcher.poll_replies(
                self.state.reply_port_addr, debug=self._debug
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
                if num_children > 0 and self._debug:
                    print(f"[amifuse] Ran {num_children} child process(es)")
            else:
                if self._debug and i < 5:
                    print(f"[amifuse] iter {i}: no proc_mgr")

            # Check for error FIRST - errors take precedence over blocked state
            if getattr(rs, "error", None):
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
                continue
            # If handler exited (done=True) without blocking, stop looping - it's not coming back
            if getattr(rs, "done", False) and not self.state.initialized:
                # Handler exited during startup without entering main loop
                # The startup packet results are in the packet struct - let caller check them
                break
            # Yield with exponential backoff to avoid tight polling loops.
            if sleep_base > 0:
                time.sleep(sleep_time)
                sleep_time = min(sleep_time * 2, sleep_max)
        if self._debug and not replies:
            print(f"[amifuse] _run_until_replies: exhausted {max_iters} iters with no replies, pc=0x{self.state.pc:x}")

        # Drain stale timer/IO messages that accumulated during packet
        # processing.  SendIO queues replies synchronously; without this
        # drain, the next _run_until_replies would deliver timer signals
        # alongside the DOS signal, causing a timer storm.
        # During startup (drain_non_essential=False), keep timer messages
        # so _flush_pending_signals can deliver them for deferred init.
        if drain_non_essential:
            pmgr = self.vh.slm.exec_impl.port_mgr
            dos_port = self.state.port_addr
            reply_port = self.state.reply_port_addr
            for port_addr in list(pmgr.ports.keys()):
                if port_addr != dos_port and port_addr != reply_port:
                    while pmgr.has_msg(port_addr):
                        pmgr.get_msg(port_addr)

        return replies

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
            combined = self_signals | tc_sigrecvd | port_signals | timer_signal
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
                      f"ports=0x{port_signals:x} timer=0x{timer_signal:x})")

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

        # Drain stale messages from ALL non-essential ports (timer, disk
        # change).  After initialization, timer signals must NOT interfere
        # with normal DOS packet processing.
        dos_port = self.state.port_addr
        reply_port = self.state.reply_port_addr
        for port_addr in list(pmgr.ports.keys()):
            if port_addr != dos_port and port_addr != reply_port:
                while pmgr.has_msg(port_addr):
                    pmgr.get_msg(port_addr)

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
        pkt = AccessStruct(self.mem, DosPacketStruct, self.state.stdpkt_addr)
        res1 = pkt.r_s("dp_Res1")
        res2 = pkt.r_s("dp_Res2")
        if res1:
            # Check dp_Port - many handlers set this to their message port in the reply
            dp_port = pkt.r_s("dp_Port")
            alt_port = pkt.r_s("dp_Arg4")
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
                dn_task = self.mem.r32(dn_addr + DeviceNodeStruct.sdef.find_field_def_by_name("dn_Task").offset)
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
        if self._read_buf_mem is None or size > self._read_buf_size:
            self._read_buf_mem = self.vh.alloc.alloc_memory(size, label="FUSE_READBUF")
            self._read_buf_size = size
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
            mem_obj = self.vh.alloc.alloc_memory(len(data), label=f"FUSE_BSTR_{idx}")
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
        self._neg_cache[path] = time.time()

    def locate(self, lock_bptr: int, name: str):
        with self._lock:
            _, name_bptr = self._alloc_bstr(name)
            self.launcher.send_locate(self.state, lock_bptr, name_bptr)
            replies = self._run_until_replies()
            if self._debug:
                if replies:
                    print(f"[amifuse] locate(lock=0x{lock_bptr:x}, name='{name}'): res1=0x{replies[-1][2]:x} res2={replies[-1][3]}")
                else:
                    print(f"[amifuse] locate(lock=0x{lock_bptr:x}, name='{name}'): NO REPLIES")
            return replies[-1][2] if replies else 0, replies[-1][3] if replies else -1

    def free_lock(self, lock_bptr: int):
        with self._lock:
            if lock_bptr:
                self.launcher.send_free_lock(self.state, lock_bptr)
                self._run_until_replies()

    def locate_path(self, path: str) -> Tuple[int, int, List[int]]:
        """Return (lock BPTR, res2, locks_to_free) for the given absolute path."""
        with self._lock:
            if path and path != "/" and self._is_neg_cached(path):
                return 0, -1, []
            parts = [p for p in path.split("/") if p]
            lock = 0
            res2 = 0
            locks: List[int] = []
            if not parts:
                return lock, res2, locks
            for comp in parts:
                _, name_bptr = self._alloc_bstr(comp)
                self.launcher.send_locate(self.state, lock, name_bptr)
                replies = self._run_until_replies()
                lock = replies[-1][2] if replies else 0
                res2 = replies[-1][3] if replies else -1
                if lock == 0:
                    break
                locks.append(lock)
            if lock == 0 and path and path != "/":
                self._set_neg_cached(path)
            return lock, res2, locks

    def open_file(self, path: str, flags: int = os.O_RDONLY) -> Optional[Tuple[int, int]]:
        """Open a file via FINDINPUT/FINDUPDATE/FINDOUTPUT and return the FileHandle address."""
        with self._lock:
            if path and path != "/" and self._is_neg_cached(path):
                return None
            parts = [p for p in path.split("/") if p]
            if not parts:
                return None
            name = parts[-1]
            dir_path = "/" + "/".join(parts[:-1])
            dir_lock, _, locks = self.locate_path(dir_path)
            if dir_path == "/" and dir_lock == 0:
                dir_lock, _ = self.locate(0, "")
                if dir_lock:
                    locks.append(dir_lock)
            if dir_lock == 0 and dir_path != "/":
                if path and path != "/":
                    self._set_neg_cached(path)
                return None
            _, name_bptr = self._alloc_bstr(name)
            mode = flags & getattr(os, 'O_ACCMODE', 3)
            if mode == os.O_RDONLY:
                fh_addr = self._alloc_fh()
                self.launcher.send_findinput(self.state, name_bptr, dir_lock, fh_addr)
            else:
                if not self._write_enabled:
                    return None
                if flags & os.O_TRUNC:
                    fh_addr = self._alloc_fh()
                    if self._debug:
                        print(f"[amifuse][open_file] FINDOUTPUT name={name!r} dir_lock=0x{dir_lock:x} fh_addr=0x{fh_addr:x}", flush=True)
                    self.launcher.send_findoutput(self.state, name_bptr, dir_lock, fh_addr)
                else:
                    fh_addr = self._alloc_fh()
                    if self._debug:
                        print(f"[amifuse][open_file] FINDUPDATE name={name!r} dir_lock=0x{dir_lock:x} fh_addr=0x{fh_addr:x}", flush=True)
                    self.launcher.send_findupdate(self.state, name_bptr, dir_lock, fh_addr)
            replies = self._run_until_replies()
            self._log_replies("find", replies)
            if not replies or replies[-1][2] == 0:
                if self._debug:
                    res2 = replies[-1][3] if replies else -1
                    print(f"[amifuse][open_file] FAILED: replies={bool(replies)} res1={replies[-1][2] if replies else 'none'} res2={res2}", flush=True)
                if path and path != "/":
                    self._set_neg_cached(path)
                self._free_fh(fh_addr)
                for l in reversed(locks):
                    self.free_lock(l)
                return None
            if path:
                self._neg_cache.pop(path, None)
            if self._write_enabled:
                # Writing may create new entries; clear stale negative cache.
                self._neg_cache.clear()
            return fh_addr, dir_lock

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
            self.launcher.send_examine(self.state, lock_bptr, fib_mem.addr)
            replies = self._run_until_replies()
            entries: List[Dict] = []
            if not replies or replies[-1][2] == 0:
                if self._debug:
                    res2 = replies[-1][3] if replies else -1
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
                self.launcher.send_examine_next(self.state, lock_bptr, fib_mem.addr)
                replies = self._run_until_replies()
                if not replies or replies[-1][2] == 0:
                    if self._debug:
                        res2 = replies[-1][3] if replies else -1
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
            self.launcher.send_examine(self.state, lock, fib_mem.addr)
            replies = self._run_until_replies()
            if not replies or replies[-1][2] == 0:
                for l in reversed(locks):
                    self.free_lock(l)
                return None
            info = _parse_fib(self.mem, fib_mem.addr)
            for l in reversed(locks):
                self.free_lock(l)
            return info

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
                self.launcher.send_examine(self.state, root_lock, fib_mem.addr)
                replies = self._run_until_replies()
                if replies and replies[-1][2]:
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

    def read_file(self, path: str, size: int, offset: int) -> bytes:
        with self._lock:
            if path and path != "/" and self._is_neg_cached(path):
                return b""
            # split into parent lock + name
            parts = [p for p in path.split("/") if p]
            if not parts:
                return b""
            name = parts[-1]
            dir_path = "/" + "/".join(parts[:-1])
            dir_lock, _, locks = self.locate_path(dir_path)
            if dir_lock == 0 and dir_path != "/":
                if path and path != "/":
                    self._set_neg_cached(path)
                return b""
            _, name_bptr = self._alloc_bstr(name)
            fh_addr = self._alloc_fh()
            self.launcher.send_findinput(self.state, name_bptr, dir_lock, fh_addr)
            replies = self._run_until_replies()
            self._log_replies("findinput", replies)
            if not replies or replies[-1][2] == 0:
                if path and path != "/":
                    self._set_neg_cached(path)
                self._free_fh(fh_addr)
                for l in reversed(locks):
                    self.free_lock(l)
                return b""
            # optional seek
            if offset:
                self.launcher.send_seek_handle(self.state, fh_addr, offset, OFFSET_BEGINNING)
                self._run_until_replies()
            buf_mem = self._alloc_read_buf(size)
            self.launcher.send_read_handle(self.state, fh_addr, buf_mem.addr, size)
            replies = self._run_until_replies()
            self._log_replies("read", replies)
            if not replies or replies[-1][2] <= 0:
                self.launcher.send_end_handle(self.state, fh_addr)
                self._run_until_replies()
                self._free_fh(fh_addr)
                for l in reversed(locks):
                    self.free_lock(l)
                return b""
            nread = min(replies[-1][2], size)
            data = bytes(self.mem.r_block(buf_mem.addr, nread))
            self.launcher.send_end_handle(self.state, fh_addr)
            self._run_until_replies()
            self._free_fh(fh_addr)
            for l in reversed(locks):
                self.free_lock(l)
            return data

    def seek_handle(self, fh_addr: int, offset: int, mode: int = OFFSET_BEGINNING):
        with self._lock:
            self.launcher.send_seek_handle(self.state, fh_addr, offset, mode)
            self._run_until_replies()

    def read_handle(self, fh_addr: int, size: int) -> bytes:
        with self._lock:
            buf_mem = self._alloc_read_buf(size)
            self.launcher.send_read_handle(self.state, fh_addr, buf_mem.addr, size)
            replies = self._run_until_replies()
            if not replies or replies[-1][2] <= 0:
                return b""
            nread = min(replies[-1][2], size)
            return bytes(self.mem.r_block(buf_mem.addr, nread))


    def read_handle_at(self, fh_addr: int, offset: int, size: int) -> bytes:
        with self._lock:
            self.launcher.send_seek_handle(self.state, fh_addr, offset, OFFSET_BEGINNING)
            self._run_until_replies()
            buf_mem = self._alloc_read_buf(size)
            self.launcher.send_read_handle(self.state, fh_addr, buf_mem.addr, size)
            replies = self._run_until_replies()
            if not replies or replies[-1][2] <= 0:
                return b""
            nread = min(replies[-1][2], size)
            return bytes(self.mem.r_block(buf_mem.addr, nread))


    def write_handle(self, fh_addr: int, data: bytes) -> int:
        with self._lock:
            buf_mem = self._alloc_read_buf(len(data))
            self.mem.w_block(buf_mem.addr, data)
            self.launcher.send_write_handle(self.state, fh_addr, buf_mem.addr, len(data))
            replies = self._run_until_replies()
            self._log_replies("write", replies)
            if not replies:
                return -1
            return replies[-1][2]

    def write_handle_at(self, fh_addr: int, offset: int, data: bytes) -> int:
        with self._lock:
            self.launcher.send_seek_handle(self.state, fh_addr, offset, OFFSET_BEGINNING)
            replies = self._run_until_replies()
            self._log_replies("seek", replies)
            buf_mem = self._alloc_read_buf(len(data))
            self.mem.w_block(buf_mem.addr, data)
            self.launcher.send_write_handle(self.state, fh_addr, buf_mem.addr, len(data))
            replies = self._run_until_replies()
            self._log_replies("write", replies)
            if not replies:
                return -1
            return replies[-1][2]

    def set_handle_size(self, fh_addr: int, size: int, mode: int = OFFSET_BEGINNING) -> int:
        with self._lock:
            self.launcher.send_set_file_size(self.state, fh_addr, size, mode)
            replies = self._run_until_replies()
            self._log_replies("setsize", replies)
            if not replies:
                return -1
            return replies[-1][2]

    def delete_object(self, parent_lock_bptr: int, name: str) -> Tuple[int, int]:
        with self._lock:
            _, name_bptr = self._alloc_bstr(name)
            self.launcher.send_delete_object(self.state, parent_lock_bptr, name_bptr)
            replies = self._run_until_replies()
            self._log_replies("delete", replies)
            if not replies:
                return 0, -1
            return replies[-1][2], replies[-1][3]

    def rename_object(
        self, src_lock_bptr: int, src_name: str, dst_lock_bptr: int, dst_name: str
    ) -> Tuple[int, int]:
        with self._lock:
            _, src_bptr = self._alloc_bstr(src_name)
            _, dst_bptr = self._alloc_bstr(dst_name)
            self.launcher.send_rename_object(
                self.state, src_lock_bptr, src_bptr, dst_lock_bptr, dst_bptr
            )
            replies = self._run_until_replies()
            self._log_replies("rename", replies)
            if not replies:
                return 0, -1
            return replies[-1][2], replies[-1][3]

    def create_dir(self, parent_lock_bptr: int, name: str) -> Tuple[int, int]:
        with self._lock:
            _, name_bptr = self._alloc_bstr(name)
            self.launcher.send_create_dir(self.state, parent_lock_bptr, name_bptr)
            replies = self._run_until_replies()
            self._log_replies("mkdir", replies)
            if not replies:
                return 0, -1
            return replies[-1][2], replies[-1][3]

    def close_file(self, fh_addr: int):
        with self._lock:
            self.launcher.send_end_handle(self.state, fh_addr)
            replies = self._run_until_replies()
            self._log_replies("end", replies)
            # Note: ACTION_FLUSH is for flushing volume buffers, not file-specific.
            # We call flush_volume() on unmount instead of after every file close.
            self._free_fh(fh_addr)

    def flush_volume(self):
        """Flush the handler's buffers to disk. Call on unmount."""
        with self._lock:
            if self.state.crashed:
                return
            if self._debug:
                print("[amifuse] Flushing volume buffers to disk...", flush=True)
            self.launcher.send_flush(self.state)
            replies = self._run_until_replies()
            self._log_replies("flush_volume", replies)
            # Sync the underlying file to disk
            self.backend.sync()
            if self._debug:
                if replies and replies[-1][2] != 0:
                    print("[amifuse] Volume flush complete", flush=True)
                else:
                    print("[amifuse] Volume flush may have failed", flush=True)


class AmigaFuseFS(Operations):
    # macOS special files we should reject immediately without calling handler.
    # Note: "Icon\r" and ".VolumeIcon.icns" are NOT in this list - we handle them for custom icons.
    _MACOS_SPECIAL = frozenset([
        "._.", ".hidden", ".Trashes", ".Spotlight-V100", ".fseventsd",
        ".metadata_never_index", ".com.apple.timemachine.donotpresent",
        ".DS_Store", ".ql_disablethumbnails",
        ".localized", ".TemporaryItems", ".DocumentRevisions-V100",
        ".vol", ".file", ".hotfiles.btree", ".quota.user", ".quota.group",
        ".apdisk", ".com.apple.NetBootX", "mach_kernel", ".PKInstallSandboxManager",
        ".PKInstallSandboxManager-SystemSoftware", ".Trashes.501", "Backups.backupdb",
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
        self._uid = getattr(os, 'getuid', lambda: 0)()
        self._gid = getattr(os, 'getgid', lambda: 0)()
        self._stat_cache: Dict[str, Tuple[float, Dict]] = {}  # path -> (timestamp, stat_result)
        self._cache_ttl = 3600.0  # Cache for 1 hour - read-only FS never changes
        self._neg_cache: Dict[str, float] = {}  # path -> timestamp for ENOENT results
        self._neg_cache_ttl = 3600.0  # Cache negative results for 1 hour
        self._dir_cache: Dict[str, Tuple[float, List[str]]] = {}  # path -> (timestamp, entries)
        self._dir_cache_ttl = 3600.0  # Cache directory listings for 1 hour
        if self.bridge._write_enabled:
            self._cache_ttl = 0.0
            self._neg_cache_ttl = 0.0
            self._dir_cache_ttl = 0.0
        self._fh_lock = threading.Lock()
        self._fh_cache: Dict[int, Dict[str, object]] = {}
        self._next_fh = 1
        self._last_op_time = time.time()
        self._op_count = 0
        # Icon support - use platform-specific handler
        self._icons_enabled = icons
        self._icon_parser = None
        self._icon_cache = None
        self._icon_existence_cache = None
        self._icon_handler = None
        self._crash_shutdown_started = False
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
        if path in self._stat_cache:
            ts, result = self._stat_cache[path]
            if time.time() - ts < self._cache_ttl:
                return result
            del self._stat_cache[path]
        return None

    def _set_cached_stat(self, path: str, result: Dict):
        """Cache a stat result."""
        self._stat_cache[path] = (time.time(), result)

    def _is_neg_cached(self, path: str) -> bool:
        """Return True if path is in negative cache (known non-existent)."""
        if self._neg_cache_ttl <= 0:
            return False
        if path in self._neg_cache:
            if time.time() - self._neg_cache[path] < self._neg_cache_ttl:
                return True
            del self._neg_cache[path]
        return False

    def _set_neg_cached(self, path: str):
        """Cache a negative (ENOENT) result."""
        if self._neg_cache_ttl <= 0:
            return
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
        return {
            "st_mode": (0o755 | 0o040000),  # drwxr-xr-x
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
                import sys
                print(f"[FUSE] Handler crashed - all operations returning EIO", file=sys.stderr, flush=True)
                self._crash_reported = True
            self._schedule_crash_shutdown()
            raise FuseOSError(errno.EIO)

    def _schedule_crash_shutdown(self):
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
        time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGTERM)

    def _log_op(self, op: str, path: str, extra: str = ""):
        """Log operation if debug is enabled."""
        if self._debug:
            import sys
            msg = f"[FUSE][{op}] {path}"
            if extra:
                msg += f" {extra}"
            print(msg, file=sys.stderr, flush=True)

    # --- FUSE operations ---
    def getattr(self, path, fh=None):
        self._check_handler_alive()
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
        # Check positive cache
        cached = self._get_cached_stat(path)
        if cached is not None:
            self._track_op("getattr", path, cached=True)
            return cached
        self._track_op("getattr", path, cached=False)
        info = self.bridge.stat_path(path)
        if not info:
            # Cache negative result
            self._set_neg_cached(path)
            raise FuseOSError(errno.ENOENT)
        is_dir = info["dir_type"] >= 0
        # Convert Amiga protection bits to Unix mode
        prot = DosProtection(info.get("protection", 0))
        base_mode = prot.to_host_mode()
        # For read-only mounts, strip write bits
        if not self.bridge._write_enabled:
            base_mode &= ~(0o222)
        mode = base_mode | (0o040000 if is_dir else 0o100000)
        now = int(time.time())
        result = {
            "st_mode": mode,
            "st_nlink": 2 if is_dir else 1,
            "st_size": info["size"],
            "st_ctime": now,
            "st_mtime": now,
            "st_atime": now,
            "st_uid": self._uid,
            "st_gid": self._gid,
            "st_blksize": 512, # TODO: compute from ACTION_DISK_INFO?
            "st_blocks": info["num_blocks"]
        }
        # Set UF_HIDDEN on all .info files when icons mode is enabled
        # This hides the Amiga icon files since we display icons via xattrs
        if self._icons_enabled and (path.endswith(".info") or path.lower().endswith(".info")):
            result["st_flags"] = result.get("st_flags", 0) | 0x8000  # UF_HIDDEN
        self._set_cached_stat(path, result)
        return result

    def readdir(self, path, fh):
        self._check_handler_alive()
        # Check directory cache first
        if path in self._dir_cache:
            ts, cached_entries = self._dir_cache[path]
            if time.time() - ts < self._dir_cache_ttl:
                self._track_op("readdir", path, cached=True)
                return cached_entries
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
            is_dir = ent["dir_type"] >= 0
            # Convert Amiga protection bits to Unix mode
            prot = DosProtection(ent.get("protection", 0))
            base_mode = prot.to_host_mode()
            # For read-only mounts, strip write bits
            if not self.bridge._write_enabled:
                base_mode &= ~(0o222)
            mode = base_mode | (0o040000 if is_dir else 0o100000)
            stat_result = {
                "st_mode": mode,
                "st_nlink": 2 if is_dir else 1,
                "st_size": ent["size"],
                "st_ctime": int(now),
                "st_mtime": int(now),
                "st_atime": int(now),
                "st_uid": self._uid,
                "st_gid": self._gid,
                "st_blksize": 512, # TODO: compute from ACTION_DISK_INFO?
                "st_blocks": ent["num_blocks"]
            }
            # Set UF_HIDDEN on .info files when icons mode is enabled
            if self._icons_enabled and (name.endswith(".info") or name.lower().endswith(".info")):
                stat_result["st_flags"] = 0x8000  # UF_HIDDEN
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

        # Cache the directory listing
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
        opened = self.bridge.open_file(path, flags)
        if opened is None:
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
            }
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
            if entry["pos"] is None or offset != entry["pos"]:
                data = self.bridge.read_handle_at(fh_addr, offset, size)
            else:
                data = self.bridge.read_handle(fh_addr, size)
            if data is None:
                raise FuseOSError(errno.EIO)
            entry["pos"] = offset + len(data)
            return data

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
            if entry["pos"] is None or offset != entry["pos"]:
                written = self.bridge.write_handle_at(fh_addr, offset, data)
            else:
                written = self.bridge.write_handle(fh_addr, data)
            if written < 0:
                raise FuseOSError(errno.EIO)
            entry["pos"] = offset + written
            entry["dirty"] = True  # Mark as needing flush on close
        return written

    def truncate(self, path, length, fh=None):
        self._check_handler_alive()
        if self._debug:
            print(f"[FUSE][truncate] path={path} length={length} fh={fh}")
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        if fh is None:
            fh = self.open(path, os.O_WRONLY)
            temp_handle = True
        else:
            temp_handle = False
        try:
            with self._fh_lock:
                entry = self._fh_cache.get(fh)
            if entry is None:
                raise FuseOSError(errno.EIO)
            fh_addr = entry["fh_addr"]
            with entry["lock"]:
                if entry.get("closed"):
                    raise FuseOSError(errno.EIO)
                size = self.bridge.set_handle_size(fh_addr, length, OFFSET_BEGINNING)
                if size < 0:
                    raise FuseOSError(errno.EIO)
                if entry["pos"] is not None:
                    entry["pos"] = min(entry["pos"], length)
                entry["dirty"] = True  # Truncate modifies the file
        finally:
            if temp_handle:
                self.release(path, fh)
        return 0

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
                }
            # Prime stat/dir cache so immediate getattr after create doesn't fail.
            now = time.time()
            self._stat_cache[path] = (
                now,
                {
                    "st_mode": 0o100644,
                    "st_nlink": 1,
                    "st_size": 0,
                    "st_ctime": int(now),
                    "st_mtime": int(now),
                    "st_atime": int(now),
                    "st_uid": self._uid,
                    "st_gid": self._gid,
                },
            )
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
            raise FuseOSError(errno.EIO)
        self._stat_cache.pop(path, None)
        self._dir_cache.pop(dir_path, None)
        for l in reversed(locks):
            self.bridge.free_lock(l)
        return 0

    def rmdir(self, path):
        return self.unlink(path)

    def mkdir(self, path, mode):
        self._check_handler_alive()
        if not self.bridge._write_enabled:
            raise FuseOSError(errno.EROFS)
        dir_path, name = self._split_path(path)
        if not name:
            raise FuseOSError(errno.EINVAL)
        parent_lock, _, locks = self.bridge.locate_path(dir_path)
        if parent_lock == 0 and dir_path != "/":
            raise FuseOSError(errno.ENOENT)
        new_lock, _ = self.bridge.create_dir(parent_lock, name)
        if new_lock == 0:
            raise FuseOSError(errno.EIO)
        self.bridge.free_lock(new_lock)
        self._dir_cache.pop(dir_path, None)
        for l in reversed(locks):
            self.bridge.free_lock(l)
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
        if (src_lock == 0 and src_dir != "/") or (dst_lock == 0 and dst_dir != "/"):
            raise FuseOSError(errno.ENOENT)
        res1, _ = self.bridge.rename_object(src_lock, src_name, dst_lock, dst_name)
        if res1 == 0:
            raise FuseOSError(errno.EIO)
        self._stat_cache.pop(old, None)
        self._stat_cache.pop(new, None)
        self._dir_cache.pop(src_dir, None)
        self._dir_cache.pop(dst_dir, None)
        for l in reversed(src_locks):
            self.bridge.free_lock(l)
        for l in reversed(dst_locks):
            self.bridge.free_lock(l)
        return 0

    def flush(self, path, fh):
        self._log_op("flush", path, f"fh={fh}")
        return 0

    def fsync(self, path, fdatasync, fh):
        self._log_op("fsync", path, f"fh={fh} fdatasync={fdatasync}")
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
            self._log_op("release", path, f"closing fh_addr=0x{entry['fh_addr']:x}")
            self.bridge.close_file(entry["fh_addr"])
            parent_lock = entry.get("parent_lock", 0)
            if parent_lock:
                self.bridge.free_lock(parent_lock)
        return 0

    def destroy(self, path):
        """Called when filesystem is unmounted. Flush and release all resources."""
        print("[amifuse] Unmounting - flushing volume...", flush=True)
        try:
            if self.bridge._write_enabled:
                self.bridge.flush_volume()
        except Exception as e:
            print(f"[amifuse] WARNING: flush failed: {e}", flush=True)
        # Shut down the m68k runtime (frees machine, memory maps, temp files)
        try:
            shutdown = getattr(getattr(self.bridge, "vh", None), "shutdown", None)
            if shutdown is not None:
                shutdown()
        except Exception as e:
            print(f"[amifuse] WARNING: runtime shutdown failed: {e}", flush=True)
        # Close the block device backend (releases file handle / image lock)
        try:
            backend = getattr(self.bridge, "backend", None)
            if backend is not None:
                backend.sync()
                backend.close()
        except Exception as e:
            print(f"[amifuse] WARNING: backend close failed: {e}", flush=True)
        print("[amifuse] Unmount complete.", flush=True)


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


def extract_embedded_driver(image: Path, block_size: Optional[int], partition: Optional[str]) -> Optional[Path]:
    """Extract filesystem driver from RDB if available for the partition's dostype.

    Returns path to temp file containing the driver, or None if not found.
    """
    import tempfile
    import amitools.fs.DosType as DosType
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
        dt_str = DosType.num_to_tag_str(target_dostype)

        # Search filesystem blocks for matching dostype
        for fs in rdisk.fs:
            if fs.fshd.dos_type == target_dostype:
                # Found matching filesystem - extract to temp file
                data = fs.get_data()
                # Create temp file that persists until program exits
                fd, temp_path = tempfile.mkstemp(suffix=f"_{dt_str}.handler", prefix="amifuse_")
                os.write(fd, data)
                os.close(fd)
                return Path(temp_path), dt_str, target_dostype

        return None
    finally:
        rdisk.close()
        blkdev.close()


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
    _require_fuse()
    import amitools.fs.DosType as DosType
    from .rdb_inspect import detect_adf, detect_iso
    from . import platform as plat

    # Fail fast if FUSE driver is missing -- before any image analysis
    plat.check_fuse_available()

    # First, check if this is an ADF (floppy disk image)
    adf_info = detect_adf(image)
    iso_info = None

    if adf_info is not None:
        # ADF floppy detected - use DF0 as the partition name
        dt_str = DosType.num_to_tag_str(adf_info.dos_type)
        floppy_type = "HD" if adf_info.is_hd else "DD"
        part_name = "DF0"  # Will use actual volume name from handler later

        # ADF files don't have embedded drivers - user must specify one
        if driver is None:
            raise SystemExit(
                f"ADF floppy image detected ({floppy_type}, {dt_str}).\n"
                "Floppy images don't contain embedded filesystem drivers.\n"
                "You need to specify a filesystem handler with --driver\n"
                "For FFS/OFS floppies, use the L:FastFileSystem from a Workbench disk."
            )
        driver_desc = str(driver)
        temp_driver = None
    else:
        # Check for ISO 9660 image
        iso_info = detect_iso(image)

        if iso_info is not None:
            part_name = "CD0"

            if driver is None:
                raise SystemExit(
                    f"ISO 9660 image detected (volume: {iso_info.volume_id}).\n"
                    "ISO images don't contain embedded filesystem drivers.\n"
                    "You need to specify a filesystem handler with --driver\n"
                    "For ISO 9660, use CDFileSystem from an AmigaOS installation."
                )
            driver_desc = str(driver)
            temp_driver = None
        else:
            # HDF/RDB image - get partition info
            try:
                part_name = get_partition_name(image, block_size, partition)
            except IOError as e:
                raise SystemExit(f"Error: {e}")

            # If no driver specified, try to extract from RDB
            temp_driver = None
            driver_desc = None
            if driver is None:
                try:
                    result = extract_embedded_driver(image, block_size, partition)
                except IOError as e:
                    raise SystemExit(f"Error: {e}")
                if result is None:
                    raise SystemExit(
                        "No embedded filesystem driver found for this partition.\n"
                        "You need to specify a filesystem handler with --driver"
                    )
                temp_driver, dt_str, dostype = result
                driver = temp_driver
                driver_desc = f"{dt_str}/0x{dostype:08x} (from RDB)"
            else:
                driver_desc = str(driver)

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

    # Create mountpoint directory if it doesn't exist
    if not mountpoint.exists():
        if not plat.should_auto_create_mountpoint(mountpoint):
            try:
                mountpoint.mkdir(parents=True, exist_ok=True)
            except FileExistsError:
                raise SystemExit(plat._format_stale_mountpoint_error(mountpoint))
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.ENOTCONN):
                    raise SystemExit(plat._format_stale_mountpoint_error(mountpoint))
                raise SystemExit(
                    f"Could not create mountpoint {mountpoint}: {exc.strerror or exc}"
                )

    if foreground is None:
        foreground = plat.mount_runs_in_foreground_by_default()
    if not foreground and not plat.get_unmount_command(mountpoint):
        raise SystemExit(
            "Daemon mode is not supported on this platform yet because there is "
            "no standalone unmount command. Use --interactive instead."
        )

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
    if foreground:
        print("[amifuse] interactive mode; press Ctrl+C to unmount")
    else:
        print(f"[amifuse] daemon mode; unmount with: amifuse unmount {mountpoint}")

    if write:
        # Guard against accidental writes without explicit intent.
        print("[amifuse] write mode enabled; ensure the image is backed up")

    if icons:
        print("[amifuse] icon mode enabled; Amiga icons will appear as macOS custom icons")

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

    try:
        FUSE(
            AmigaFuseFS(bridge, debug=debug, icons=icons, mountpoint=mountpoint),
            str(mountpoint),
            **fuse_kwargs,
        )
    finally:
        bridge.close()
        # Clean up temp driver file if we extracted one
        if temp_driver is not None and temp_driver.exists():
            temp_driver.unlink()
        # Clean up temp volume icon file
        if temp_volicon is not None and temp_volicon.exists():
            temp_volicon.unlink()


def format_volume(
    image: Path,
    driver: Optional[Path],
    block_size: Optional[int],
    partition: str,
    volname: str = "Empty",
    debug: bool = False,
):
    import amitools.fs.DosType as DosType
    bridge = None

    # Get partition dostype from RDB
    part_info = get_partition_info(image, block_size, partition)
    dostype = part_info["dostype"]
    if dostype is None:
        raise SystemExit(f"Could not determine dostype for partition '{partition}'.")
    dt_str = DosType.num_to_tag_str(dostype)

    # Resolve driver
    temp_driver = None
    if driver is None:
        try:
            result = extract_embedded_driver(image, block_size, partition)
        except IOError as e:
            raise SystemExit(f"Error: {e}")
        if result is None:
            raise SystemExit(
                "No embedded filesystem driver found for this partition.\n"
                "You need to specify a filesystem handler with --driver"
            )
        temp_driver, _, _ = result
        driver = temp_driver
        driver_desc = f"{dt_str}/0x{dostype:08x} (from RDB)"
    else:
        driver_desc = str(driver)

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

        # Inhibit the volume so the handler releases it for formatting
        bridge.launcher.send_inhibit(bridge.state, True)
        replies = bridge._run_until_replies()
        if debug and replies:
            _, _, r1, r2 = replies[-1]
            print(f"[amifuse] INHIBIT reply: res1={r1} res2={r2}")

        # Allocate volume name BSTR and send ACTION_FORMAT
        _, volname_bptr = bridge.launcher.alloc_bstr(volname, label="FormatVolName")
        bridge.launcher.send_format(bridge.state, volname_bptr, dostype)
        # Format can take many cycles for large partitions — PFS3 must
        # initialize bitmap and root blocks proportional to partition size.
        # Use generous limits; the loop returns immediately once a reply arrives.
        replies = bridge._run_until_replies(max_iters=2000, cycles=2_000_000)

        if not replies:
            raise SystemExit("Format failed: no reply from handler.")

        _, pkt_addr, res1, res2 = replies[-1]
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

        # Flush first, then uninhibit the new volume. SFS can fault if we
        # flush the formatter bridge after reopening the volume.
        bridge.launcher.send_flush(bridge.state)
        bridge._run_until_replies(max_iters=500, cycles=2_000_000)

        bridge.launcher.send_inhibit(bridge.state, False)
        bridge._run_until_replies(max_iters=500, cycles=2_000_000)

        print(f"Format complete. Volume '{volname}' created on partition '{partition}'.")
    finally:
        if bridge is not None:
            shutdown = getattr(getattr(bridge, "vh", None), "shutdown", None)
            if shutdown is not None:
                shutdown()
            backend = getattr(bridge, "backend", None)
            if backend is not None:
                backend.close()
        if temp_driver is not None and temp_driver.exists():
            temp_driver.unlink()


__version__ = "v0.4.9"
__banner__ = f"amifuse {__version__} - Copyright (C) 2025-2026 by Stefan Reinauer"


def _inspect_rdisk(rdisk, full=False):
    """Print RDB info, filesystem drivers, and warnings for a single RDisk."""
    from .rdb_inspect import format_fs_summary
    for line in rdisk.get_info(full=full):
        print(line)
    fs_lines = format_fs_summary(rdisk)
    if fs_lines:
        print("\nFilesystem drivers:")
        for line in fs_lines:
            print(" ", line)
    warnings = getattr(rdisk, 'rdb_warnings', [])
    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  {w}")


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


def _create_bridge_from_args(args, command: str):
    """Create a HandlerBridge from CLI arguments.

    Handles driver resolution (RDB extraction or explicit --driver),
    ADF/ISO detection, and JSON error reporting.

    Args:
        args: Parsed argparse namespace. Must have: image, partition, driver,
              block_size, debug. May have: json.
        command: Command name for error envelopes.

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
    if not image.exists():
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

    if adf_info is not None:
        if driver is None:
            msg = ("ADF floppy image detected. Floppy images don't contain "
                   "embedded filesystem drivers. Use --driver to specify one.")
            if use_json:
                print(_json.dumps(_json_error(command, "DRIVER_NOT_FOUND", msg)))
                sys.exit(1)
            raise SystemExit(msg)
    else:
        iso_info = detect_iso(image)
        if iso_info is not None:
            if driver is None:
                msg = ("ISO 9660 image detected. ISO images don't contain "
                       "embedded filesystem drivers. Use --driver to specify one.")
                if use_json:
                    print(_json.dumps(_json_error(command, "DRIVER_NOT_FOUND", msg)))
                    sys.exit(1)
                raise SystemExit(msg)
        else:
            # RDB image -- extract embedded driver if not specified
            if driver is None:
                try:
                    result = extract_embedded_driver(image, block_size, partition)
                except IOError as e:
                    if use_json:
                        print(_json.dumps(_json_error(command, "IMAGE_INVALID", str(e))))
                        sys.exit(1)
                    raise SystemExit(f"Error: {e}")
                if result is None:
                    msg = ("No embedded filesystem driver found. "
                           "Use --driver to specify one.")
                    if use_json:
                        print(_json.dumps(_json_error(command, "DRIVER_NOT_FOUND", msg)))
                        sys.exit(1)
                    raise SystemExit(msg)
                temp_driver, _, _ = result
                driver = temp_driver

    # Create the bridge
    try:
        bridge = HandlerBridge(
            image,
            driver,
            block_size=block_size,
            read_only=True,
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
            print(_json.dumps(_json_error(command, "HANDLER_ERROR", str(e))))
            sys.exit(1)
        raise
    except Exception as e:
        if temp_driver is not None:
            try:
                temp_driver.unlink(missing_ok=True)
            except Exception:
                pass
        if use_json:
            print(_json.dumps(_json_error(command, "HANDLER_ERROR",
                f"Failed to initialize filesystem handler: {e}")))
            sys.exit(1)
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
        if recursive:
            entries = _ls_recursive(bridge, path)
        else:
            raw = bridge.list_dir_path(path)
            if not raw and path != "/":
                # Path might not exist -- try stat to distinguish
                stat = bridge.stat_path(path)
                if stat is None:
                    if use_json:
                        print(json.dumps(_json_error("ls", "FILE_NOT_FOUND",
                            f"Path not found: {path}")))
                        sys.exit(1)
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
            )
            print(json.dumps(result, indent=2))
        else:
            for ent in entries:
                if ent["type"] == "dir":
                    print(f"  {ent['name']:30s}  <dir>     {ent['protection']}")
                else:
                    print(f"  {ent['name']:30s}  {ent['size']:>10d}  {ent['protection']}")
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

    bridge, temp_driver = _create_bridge_from_args(args, "verify")
    try:
        if file_path:
            # Verify a specific file
            normalized = "/" + file_path.lstrip("/")
            stat = bridge.stat_path(normalized)
            if stat is None:
                if use_json:
                    print(json.dumps(_json_error("verify", "FILE_NOT_FOUND",
                        f"File not found: {file_path}")))
                    sys.exit(1)
                raise SystemExit(f"Error: file not found: {file_path}")

            result_data = {
                "target": str(args.image),
                "file": file_path,
                "exists": True,
                "size": stat.get("size", 0),
                "type": "dir" if stat.get("dir_type", 0) > 0 else "file",
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
                "filesystem_responsive": True,
            }

            if use_json:
                print(json.dumps(_json_result("verify", **result_data), indent=2))
            else:
                print(f"Volume: {volname}")
                print(f"  Directories: {total_dirs}")
                print(f"  Files: {total_files}")
                print(f"  Total size: {total_size:,} bytes")
                print(f"  Filesystem responsive: yes")
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
    try:
        normalized = "/" + file_path.lstrip("/")
        stat = bridge.stat_path(normalized)
        if stat is None:
            if use_json:
                print(json.dumps(_json_error("hash", "FILE_NOT_FOUND",
                    f"File not found: {file_path}")))
                sys.exit(1)
            raise SystemExit(f"Error: file not found: {file_path}")

        if stat.get("dir_type", 0) > 0:
            if use_json:
                print(json.dumps(_json_error("hash", "INVALID_ARGUMENT",
                    f"Cannot hash a directory: {file_path}")))
                sys.exit(1)
            raise SystemExit(f"Error: cannot hash a directory: {file_path}")

        file_size = stat.get("size", 0)

        # Read file in chunks and compute hash
        h = hashlib.new(algorithm)
        fh_result = bridge.open_file(normalized)
        if fh_result is None:
            if use_json:
                print(json.dumps(_json_error("hash", "HANDLER_ERROR",
                    f"Failed to open file: {file_path}")))
                sys.exit(1)
            raise SystemExit(f"Error: failed to open file: {file_path}")

        fh_addr, _dir_lock = fh_result
        try:
            chunk_size = 65536
            bytes_read = 0
            # Seek to start once, then use sequential read_handle() calls.
            # read_handle() reads from the current file position without
            # seeking -- much more efficient than read_handle_at() which
            # does an absolute seek before every read.
            bridge.seek_handle(fh_addr, 0)
            while bytes_read < file_size:
                to_read = min(chunk_size, file_size - bytes_read)
                data = bridge.read_handle(fh_addr, to_read)
                if not data:
                    break
                h.update(data)
                bytes_read += len(data)
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
                f"Hash computation failed: {e}")))
            sys.exit(1)
        raise SystemExit(f"Error computing hash: {e}")
    finally:
        _cleanup_bridge(bridge, temp_driver)


def cmd_inspect(args):
    """Handle the 'inspect' subcommand."""
    import json as _json
    import amitools.fs.DosType as DosType
    from .rdb_inspect import (
        open_rdisk, format_mbr_info, detect_adf, detect_iso, detect_mbr,
        MBR_TYPE_AMIGA_RDB,
    )

    use_json = getattr(args, "json", False)

    # Check that image exists (before any detection)
    if not args.image.exists():
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
        if use_json:
            dt_str = DosType.num_to_tag_str(adf_info.dos_type)
            floppy_type = "HD" if adf_info.is_hd else "DD"
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
                note="ADF files don't contain embedded filesystem drivers. Use --driver to specify a handler.",
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
        print("Use --driver to specify a filesystem handler when mounting.")
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

    # Detect if MBR with multiple 0x76 partitions
    mbr_info = detect_mbr(args.image)
    multi_rdb = False
    amiga_parts = []
    if mbr_info and mbr_info.has_amiga_partitions:
        amiga_parts = [p for p in mbr_info.partitions if p.partition_type == MBR_TYPE_AMIGA_RDB]
        if len(amiga_parts) > 1:
            multi_rdb = True

    if use_json:
        # JSON mode: collect all RDBs into a single envelope
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
                desc = rdisk.get_desc()
                if mbr_ctx is not None:
                    mbr_desc = {
                        "scheme": mbr_ctx.scheme,
                        "offset_blocks": mbr_ctx.offset_blocks,
                        "all_partitions": [
                            {
                                "index": p.index,
                                "type": p.partition_type,
                                "bootable": p.bootable,
                                "start_lba": p.start_lba,
                                "num_sectors": p.num_sectors,
                            }
                            for p in mbr_ctx.mbr_info.partitions
                        ],
                    }
                    if mbr_ctx.mbr_partition is not None:
                        mbr_desc["partition_index"] = mbr_ctx.mbr_partition.index
                        mbr_desc["partition_size"] = mbr_ctx.mbr_partition.num_sectors
                    desc["mbr"] = mbr_desc
                warnings = getattr(rdisk, 'rdb_warnings', [])
                if warnings:
                    desc["warnings"] = warnings
                all_rdbs.append(desc)
            finally:
                rdisk.close()
                blkdev.close()

        if len(all_rdbs) == 1:
            rdb_data = all_rdbs[0]
        else:
            rdb_data = all_rdbs
        envelope = _json_result("inspect",
            image=str(args.image),
            image_type="rdb",
        )
        if isinstance(rdb_data, list):
            envelope["rdbs"] = rdb_data
        else:
            envelope.update(rdb_data)
        print(_json.dumps(envelope, indent=2))
        return

    if multi_rdb:
        # Show MBR table once using first partition's context
        try:
            blkdev, rdisk, mbr_ctx = open_rdisk(args.image, block_size=args.block_size, mbr_partition_index=0)
        except IOError as e:
            raise SystemExit(f"Error: {e}")
        for line in format_mbr_info(mbr_ctx):
            print(line)
        rdisk.close()
        blkdev.close()

        # Show each RDB
        for rdb_idx in range(len(amiga_parts)):
            try:
                blkdev, rdisk, mbr_ctx = open_rdisk(
                    args.image, block_size=args.block_size, mbr_partition_index=rdb_idx
                )
            except IOError as e:
                print(f"\nMBR partition [{amiga_parts[rdb_idx].index}]: Error: {e}")
                continue
            try:
                print(f"\n=== Amiga RDB in MBR partition [{mbr_ctx.mbr_partition.index}]"
                      f" (offset: {mbr_ctx.offset_blocks} sectors) ===\n")
                _inspect_rdisk(rdisk, full=args.full)
            finally:
                rdisk.close()
                blkdev.close()
    else:
        # Single RDB (or non-MBR): existing behavior
        blkdev = None
        rdisk = None
        mbr_ctx = None
        try:
            try:
                blkdev, rdisk, mbr_ctx = open_rdisk(args.image, block_size=args.block_size)
            except IOError as e:
                raise SystemExit(f"Error: {e}")
            if mbr_ctx is not None:
                for line in format_mbr_info(mbr_ctx):
                    print(line)
                print()
            _inspect_rdisk(rdisk, full=args.full)
        finally:
            if rdisk is not None:
                rdisk.close()
            if blkdev is not None:
                blkdev.close()


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
    is_mounted = False
    try:
        is_mounted = os.path.ismount(mountpoint)
    except OSError:
        is_mounted = False
    if not is_mounted and not plat._is_stale_mountpoint(mountpoint):
        raise SystemExit(f"Mountpoint {mountpoint} is not currently mounted.")

    cmd = plat.get_unmount_command(mountpoint)
    if not cmd:
        raise SystemExit(
            "This platform does not provide a standalone unmount command yet. "
            "Stop the amifuse process that owns the mount instead."
        )

    result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        killed_pids = _kill_mount_owner_processes(mountpoint)
        if killed_pids:
            result = subprocess.run(cmd, check=False)
    if result.returncode != 0:
        raise SystemExit(
            f"Unmount failed with exit code {result.returncode}: {' '.join(cmd)}"
        )


def cmd_doctor(args):
    """Handle the 'doctor' subcommand."""
    import json

    checks = {}
    suggestions = []

    # Check 1: Python version
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    py_ok = sys.version_info >= (3, 9)
    checks["python"] = {
        "version": py_ver,
        "minimum": "3.9",
        "ok": py_ok,
    }
    if not py_ok:
        suggestions.append("Upgrade Python to 3.9 or later.")

    # Check 2: amitools
    try:
        import amitools  # type: ignore
        checks["amitools"] = {"available": True, "ok": True}
    except ImportError:
        checks["amitools"] = {"available": False, "ok": False}
        suggestions.append("Install amitools: pip install amitools-amifuse[vamos]")

    # Check 3: machine68k (m68k CPU emulator)
    try:
        import machine68k  # type: ignore
        checks["machine68k"] = {"available": True, "ok": True}
    except ImportError:
        checks["machine68k"] = {"available": False, "ok": False}
        suggestions.append("Install machine68k (required for m68k emulation).")

    # Check 4: fusepy
    try:
        import fuse  # type: ignore
        fusepy_ver = getattr(fuse, "__version__", "unknown")
        checks["fusepy"] = {"installed": True, "version": fusepy_ver, "ok": True}
    except ImportError:
        checks["fusepy"] = {"installed": False, "ok": False}
        suggestions.append("Install fusepy: pip install fusepy")

    # Check 5: FUSE backend (platform-specific)
    # NOTE: check_fuse_available() only performs an active check on Windows
    # (WinFSP registry/path detection). On macOS/Linux it returns immediately
    # because fusepy raises its own clear error at mount time if libfuse is
    # missing (see platform.py line 101-104). This means on non-Windows the
    # fuse_backend check will always report "installed: true" even if the
    # native FUSE driver is not actually present -- the real check happens
    # at mount time, not here.
    from . import platform as plat
    try:
        # check_fuse_available() raises SystemExit (not a regular exception)
        # when the FUSE backend is missing, so we must catch SystemExit here.
        plat.check_fuse_available()
        backend_name = "macFUSE" if sys.platform.startswith("darwin") else (
            "WinFSP" if sys.platform.startswith("win") else "libfuse"
        )
        checks["fuse_backend"] = {"name": backend_name, "installed": True, "ok": True}
    except SystemExit:
        backend_name = "macFUSE" if sys.platform.startswith("darwin") else (
            "WinFSP" if sys.platform.startswith("win") else "libfuse"
        )
        checks["fuse_backend"] = {"name": backend_name, "installed": False, "ok": False}
        suggestions.append(f"Install {backend_name} for FUSE mount support.")

    # Determine overall status
    missing = [k for k, v in checks.items() if not v["ok"]]
    # Core requirements: python, amitools, machine68k
    core_missing = [k for k in missing if k in ("python", "amitools", "machine68k")]
    if core_missing:
        overall = "not_ready"
    elif missing:
        overall = "degraded"  # fusepy/fuse_backend missing = no mount, but ls/verify/hash work
    else:
        overall = "ready"

    result = {
        "status": "ok" if overall == "ready" else ("warning" if overall == "degraded" else "error"),
        "command": "doctor",
        "version": __version__,
        "checks": checks,
        "overall": overall,
        "missing": missing,
        "suggestions": suggestions,
    }

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        print(f"amifuse {__version__} environment check\n")
        for name, check in checks.items():
            status_str = "OK" if check["ok"] else "MISSING"
            detail = ""
            if "version" in check:
                detail = f" ({check['version']})"
            elif "name" in check:
                detail = f" ({check['name']})"
            print(f"  {name:20s} {status_str}{detail}")
        print(f"\nOverall: {overall}")
        if suggestions:
            print("\nSuggestions:")
            for s in suggestions:
                print(f"  - {s}")

    if overall == "not_ready":
        sys.exit(1)
    elif overall == "degraded":
        sys.exit(2)
    # else sys.exit(0) -- implicit


def _kill_mount_owner_processes(mountpoint: Path) -> List[int]:
    pids = _find_mount_owner_pids(mountpoint)
    if not pids:
        return []

    remaining = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            remaining.append(pid)
        except (ProcessLookupError, OSError):
            continue

    deadline = time.time() + 1.0
    while remaining and time.time() < deadline:
        still_alive = []
        for pid in remaining:
            if _pid_exists(pid):
                still_alive.append(pid)
        if not still_alive:
            return pids
        remaining = still_alive
        time.sleep(0.05)

    for pid in remaining:
        try:
            os.kill(pid, _SIGKILL)
        except (ProcessLookupError, OSError):
            continue

    return pids


def _find_mount_owner_pids(mountpoint: Path) -> List[int]:
    """Find PIDs of amifuse processes that own the given mountpoint.

    Dispatches to platform-specific discovery: ``ps`` on Unix,
    ``wmic`` on Windows.
    """
    if sys.platform.startswith("win"):
        return _find_mount_owner_pids_windows(mountpoint)
    return _find_mount_owner_pids_unix(mountpoint)


def _find_mount_owner_pids_windows(mountpoint: Path) -> List[int]:
    """Find amifuse PIDs on Windows using wmic."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where",
             "name like '%python%'",
             "get", "ProcessId,CommandLine",
             "/FORMAT:LIST"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []

    current_pid = os.getpid()
    raw_mountpoint = str(mountpoint)
    abs_mountpoint = str(mountpoint.resolve(strict=False))
    pids = []

    # wmic /FORMAT:LIST outputs key=value pairs separated by blank lines
    current_cmdline = None
    current_pid_val = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            # End of a record -- evaluate what we have
            if current_cmdline is not None and current_pid_val is not None:
                pid = current_pid_val
                command = current_cmdline
                if pid != current_pid and "amifuse" in command:
                    try:
                        tokens = shlex.split(command, posix=False)
                    except ValueError:
                        tokens = command.split()
                    if _command_matches_mountpoint(tokens, raw_mountpoint, abs_mountpoint):
                        pids.append(pid)
            current_cmdline = None
            current_pid_val = None
            continue
        if line.startswith("CommandLine="):
            current_cmdline = line[len("CommandLine="):]
        elif line.startswith("ProcessId="):
            try:
                current_pid_val = int(line[len("ProcessId="):])
            except ValueError:
                current_pid_val = None

    # Handle last record if no trailing blank line
    if current_cmdline is not None and current_pid_val is not None:
        pid = current_pid_val
        command = current_cmdline
        if pid != current_pid and "amifuse" in command:
            try:
                tokens = shlex.split(command, posix=False)
            except ValueError:
                tokens = command.split()
            if _command_matches_mountpoint(tokens, raw_mountpoint, abs_mountpoint):
                pids.append(pid)

    return pids


def _find_mount_owner_pids_unix(mountpoint: Path) -> List[int]:
    """Find amifuse PIDs on Unix using ps."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []

    current_pid = os.getpid()
    raw_mountpoint = str(mountpoint)
    abs_mountpoint = str(mountpoint.resolve(strict=False))
    pids = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            pid_str, command = line.split(None, 1)
            pid = int(pid_str)
        except ValueError:
            continue
        if pid == current_pid or "amifuse" not in command:
            continue
        try:
            tokens = shlex.split(command)
        except ValueError:
            continue
        if not _command_matches_mountpoint(tokens, raw_mountpoint, abs_mountpoint):
            continue
        pids.append(pid)

    return pids


def _command_matches_mountpoint(tokens: List[str], raw_mountpoint: str, abs_mountpoint: str) -> bool:
    for idx, token in enumerate(tokens):
        if token != "--mountpoint":
            continue
        if idx + 1 >= len(tokens):
            return False
        mount_arg = tokens[idx + 1]
        if mount_arg == raw_mountpoint or mount_arg == abs_mountpoint:
            return True
        try:
            resolved = str(Path(mount_arg).expanduser().resolve(strict=False))
        except OSError:
            continue
        if resolved == abs_mountpoint:
            return True
    return False


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Windows raises a generic OSError for invalid/dead PIDs
        return False
    return True


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
    --driver PATH             Filesystem binary (default: extract from RDB).
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

  doctor                    Check prerequisites and environment readiness.
    --json                    Output results as JSON.

  format <image> <partition> [volname]
                              Format an Amiga partition.
    --driver PATH             Filesystem binary (default: extract from RDB).
    --block-size N            Override block size (defaults to auto/512).
    --debug                   Enable debug logging.

  ls <image> [path]         List files in an Amiga filesystem image.
    --path PATH               Directory path to list (default: /).
    --partition NAME          Partition name (e.g. DH0) or index.
    --driver PATH             Filesystem binary (default: extract from RDB).
    --block-size N            Override block size (defaults to auto/512).
    --recursive               List all entries recursively.
    --json                    Output results as JSON.
    --debug                   Enable debug logging.

  verify <image>            Verify an Amiga filesystem image.
    --file PATH               Verify a specific file.
    --expect-size N           Expected file size in bytes (with --file).
    --partition NAME          Partition name (e.g. DH0) or index.
    --driver PATH             Filesystem binary (default: extract from RDB).
    --block-size N            Override block size (defaults to auto/512).
    --json                    Output results as JSON.
    --debug                   Enable debug logging.

  hash <image>              Compute file hash from an Amiga filesystem image.
    --file PATH               Path to file within the image (required).
    --algorithm ALG           Hash algorithm: md5, sha1, sha256 (default: sha256).
    --partition NAME          Partition name (e.g. DH0) or index.
    --driver PATH             Filesystem binary (default: extract from RDB).
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
    inspect_parser.add_argument("image", type=Path, help="Disk image file")
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
    mount_parser.add_argument("image", type=Path, help="Disk image file")
    mount_parser.add_argument("--driver", type=Path, help="Filesystem binary (default: extract from RDB if available)")
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

    # doctor subcommand
    doctor_parser = subparsers.add_parser(
        "doctor", help="Check prerequisites and environment readiness."
    )
    doctor_parser.add_argument(
        "--json", action="store_true",
        help="Output results as JSON.",
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    # format subcommand
    format_parser = subparsers.add_parser(
        "format", help="Format an Amiga partition."
    )
    format_parser.add_argument("image", type=Path, help="Disk image file")
    format_parser.add_argument("partition", type=str, help="Partition name (e.g. DH0) or index.")
    format_parser.add_argument("volname", nargs="?", default="Empty", help="Volume name (default: Empty)")
    format_parser.add_argument("--driver", type=Path, help="Filesystem binary (default: extract from RDB if available)")
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
        help="Filesystem binary (default: extract from RDB if available).",
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
        help="Filesystem binary (default: extract from RDB if available).",
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
        help="Filesystem binary (default: extract from RDB if available).",
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

    args = parser.parse_args(argv)
    try:
        args.func(args)
    except FileNotFoundError as exc:
        raise SystemExit(f"Error: {exc}") from None

if __name__ == "__main__":
    main()
