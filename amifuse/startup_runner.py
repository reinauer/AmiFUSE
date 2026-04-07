"""
Launch a filesystem handler with a minimal DOS-style bootstrap:

- build Process + MsgPort in vamos memory
- queue ACTION_STARTUP packet (with DeviceNode/FileSysStartupMsg)
- run the handler for a short burst and surface replies
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from amitools.vamos.libstructs.exec_ import (  # type: ignore
    ExecLibraryStruct,
    ListStruct,
    MsgPortFlags,
    MsgPortStruct,
    NodeType,
    TaskState,
)
from amitools.vamos.libstructs.dos import (
    DosPacketStruct,
    MessageStruct,
    ProcessStruct,
    FileHandleStruct,
)  # type: ignore
from amitools.vamos.machine.regs import REG_A6, REG_A7, REG_D0  # type: ignore
from amitools.vamos.task import Stack, ExecTask  # type: ignore
from .amiga_structs import DeviceNodeStruct  # type: ignore


class HandlerTask(ExecTask):
    """Minimal ExecTask wrapper for filesystem handlers.

    This provides just enough to satisfy library calls like StackSwap that
    need ctx.task.get_stack(). We don't use the scheduler infrastructure.
    """

    def __init__(self, stack: Stack):
        # Store stack without calling super().__init__ to avoid scheduler setup
        self._handler_stack = stack
        # ExecTask/MappedTask attributes that might be accessed
        self.task = None
        self.own_task = False
        self.seg_list = None
        self.sched_task = None  # No scheduler task for handlers
        self.ami_task = None
        self.ami_proc = None

    def get_stack(self) -> Stack:
        return self._handler_stack

    def get_seg_list(self):
        return None

    def get_ami_task(self):
        return self.ami_task

    def get_ami_process(self):
        return self.ami_proc

    def get_sched_task(self):
        return self.sched_task

    def free(self):
        pass  # Stack is managed by HandlerLauncher
from amitools.vamos.libstructs.exec_ import NodeStruct  # type: ignore
from amitools.vamos.lib.DosLibrary import DosLibrary  # type: ignore
from .handler_stub import build_entry_stub  # type: ignore


def _get_block_state(cls, name, default=None):
    """Safely get blocking state from ExecLibrary/DosLibrary class variables."""
    return getattr(cls, name, default)


def _set_block_state(cls, name, value):
    """Safely set blocking state on ExecLibrary/DosLibrary class variables."""
    try:
        setattr(cls, name, value)
    except (AttributeError, TypeError):
        pass  # Class doesn't support this attribute


def _clear_all_block_state():
    """Clear all blocking state variables."""
    from amitools.vamos.lib.ExecLibrary import ExecLibrary
    _set_block_state(ExecLibrary, '_waitport_blocked_sp', None)
    _set_block_state(ExecLibrary, '_waitport_blocked_port', None)
    _set_block_state(ExecLibrary, '_waitport_blocked_ret', None)
    _set_block_state(ExecLibrary, '_wait_blocked_mask', None)
    _set_block_state(ExecLibrary, '_wait_blocked_sp', None)
    _set_block_state(ExecLibrary, '_wait_blocked_ret', None)
    _set_block_state(DosLibrary, '_waitpkt_blocked', False)


def _snapshot_block_state():
    """Capture the current parent blocking state so child execution can't clobber it."""
    from amitools.vamos.lib.ExecLibrary import ExecLibrary

    return {
        "waitport_blocked_sp": _get_block_state(ExecLibrary, "_waitport_blocked_sp"),
        "waitport_blocked_port": _get_block_state(
            ExecLibrary, "_waitport_blocked_port"
        ),
        "waitport_blocked_ret": _get_block_state(
            ExecLibrary, "_waitport_blocked_ret"
        ),
        "wait_blocked_mask": _get_block_state(ExecLibrary, "_wait_blocked_mask"),
        "wait_blocked_sp": _get_block_state(ExecLibrary, "_wait_blocked_sp"),
        "wait_blocked_ret": _get_block_state(ExecLibrary, "_wait_blocked_ret"),
        "waitpkt_blocked": _get_block_state(DosLibrary, "_waitpkt_blocked", False),
    }


def _restore_block_state(state):
    """Restore a previously captured blocking state."""
    from amitools.vamos.lib.ExecLibrary import ExecLibrary

    _set_block_state(ExecLibrary, "_waitport_blocked_sp", state["waitport_blocked_sp"])
    _set_block_state(
        ExecLibrary, "_waitport_blocked_port", state["waitport_blocked_port"]
    )
    _set_block_state(
        ExecLibrary, "_waitport_blocked_ret", state["waitport_blocked_ret"]
    )
    _set_block_state(ExecLibrary, "_wait_blocked_mask", state["wait_blocked_mask"])
    _set_block_state(ExecLibrary, "_wait_blocked_sp", state["wait_blocked_sp"])
    _set_block_state(ExecLibrary, "_wait_blocked_ret", state["wait_blocked_ret"])
    _set_block_state(DosLibrary, "_waitpkt_blocked", state["waitpkt_blocked"])


def _has_blocked_state(state):
    """Return True when a saved Exec/DOS block snapshot contains a wait frame."""
    if not state:
        return False
    return (
        state.get("waitport_blocked_sp") is not None
        or state.get("wait_blocked_sp") is not None
    )


def _get_active_block_state(saved_state=None):
    """Prefer live Exec/DOS block state, then fall back to a saved snapshot."""
    live_state = _snapshot_block_state()
    if _has_blocked_state(live_state):
        return live_state
    if _has_blocked_state(saved_state):
        return saved_state
    return None


def _build_resume_frame(
    block_state,
    *,
    default_port_addr,
    mem,
    port_mgr,
    compute_pending_signals,
    clear_signals_from_task,
    debug=False,
):
    """Build the PC/SP/D0 frame needed to resume a blocked Wait()/WaitPort()."""
    if not _has_blocked_state(block_state):
        return None

    waitport_sp = block_state.get("waitport_blocked_sp")
    wait_sp = block_state.get("wait_blocked_sp")
    waitport_ret = block_state.get("waitport_blocked_ret")
    wait_ret = block_state.get("wait_blocked_ret")
    wait_mask = block_state.get("wait_blocked_mask")
    waitpkt_blocked = block_state.get("waitpkt_blocked", False)

    blocked_sp = waitport_sp if waitport_sp is not None else wait_sp
    if blocked_sp is None:
        return None

    if wait_sp is not None and wait_mask is not None:
        pending = compute_pending_signals(wait_mask)
        if pending == 0:
            return None
        d0_val = compute_pending_signals(0xFFFFFFFF)
        clear_signals_from_task(d0_val)
        blocked_ret = wait_ret
    else:
        waitport_port = block_state.get("waitport_blocked_port") or default_port_addr
        if not waitport_port or not port_mgr.has_msg(waitport_port):
            return None
        msg_addr = port_mgr.get_msg(waitport_port)
        if msg_addr:
            _unlink_msg_from_m68k_list(mem, msg_addr, debug=debug)
        if waitpkt_blocked and msg_addr:
            msg = MessageStruct(mem, msg_addr)
            pkt_addr = msg.node.name.aptr
            d0_val = pkt_addr if pkt_addr else 0
        else:
            d0_val = msg_addr if msg_addr else 0
        blocked_ret = waitport_ret

    try:
        ret_addr = blocked_ret if blocked_ret is not None else mem.r32(blocked_sp)
    except Exception:
        ret_addr = 0
    if ret_addr == 0:
        return None

    return {
        "pc": ret_addr,
        "sp": blocked_sp + 4,
        "d0": d0_val,
    }


def _unlink_msg_from_m68k_list(mem, msg_addr, debug=False):
    """Remove a message node from its Amiga Exec doubly-linked list in m68k memory.

    This mirrors the REMOVE() macro: pred->ln_Succ = succ; succ->ln_Pred = pred.
    Without this, handlers that read mp_MsgList directly (bypassing our Python
    PortManager) see stale entries and try to process already-handled packets.
    """
    try:
        ln_succ = mem.r32(msg_addr + 0)  # Node.ln_Succ
        ln_pred = mem.r32(msg_addr + 4)  # Node.ln_Pred
        if ln_succ != 0 and ln_pred != 0:
            mem.w32(ln_pred + 0, ln_succ)  # pred->ln_Succ = succ
            mem.w32(ln_succ + 4, ln_pred)  # succ->ln_Pred = pred
    except Exception as e:
        if debug:
            print(f"[amifuse] WARNING: failed to unlink msg 0x{msg_addr:x}: {e}")


# Dos packet opcodes we care about
ACTION_STARTUP = 0
ACTION_DISK_INFO = 25
ACTION_READ = ord("R")
ACTION_WRITE = ord("W")
ACTION_SEEK = 1008
ACTION_END = 1007
ACTION_SET_FILE_SIZE = 1022
ACTION_FREE_LOCK = 15
ACTION_FINDUPDATE = 1004
ACTION_FINDINPUT = 1005
ACTION_FINDOUTPUT = 1006
ACTION_DELETE_OBJECT = 16
ACTION_RENAME_OBJECT = 17
ACTION_CREATE_DIR = 22
ACTION_FLUSH = 27
ACTION_FORMAT = 1020
ACTION_INHIBIT = 31
OFFSET_BEGINNING = -1
OFFSET_CURRENT = 0
OFFSET_END = 1
SHARED_LOCK = -2
EXCLUSIVE_LOCK = -1


@dataclass
class HandlerLaunchState:
    process_addr: int
    port_addr: int
    reply_port_addr: int
    stack: Stack
    stdpkt_addr: int
    msg_addr: int
    run_state: object
    pc: int
    sp: int
    started: bool
    entry_stub_pc: int = 0  # Original entry stub address for reset
    main_loop_pc: int = 0   # PC to restart from when waiting for messages
    main_loop_sp: int = 0   # SP to use when restarting
    initialized: bool = False  # True after startup is complete
    exit_count: int = 0  # Count consecutive exits without WaitPort block
    crashed: bool = False  # True if handler hit an unrecoverable error
    last_error_pc: int = 0  # PC at time of crash for diagnostics
    consecutive_errors: int = 0  # Count of consecutive errors without successful reply
    wait_mask: int = 0  # Handler's Wait() signal mask (for deferred init)
    regs: Optional[List[int]] = None  # Saved D0-D7/A0-A7 register state
    block_state: Optional[Dict[str, Any]] = None


class HandlerLauncher:
    def __init__(
        self,
        vh,
        boot_info: Dict,
        segment_addr: int,
    ):
        self.vh = vh
        self.alloc = vh.alloc
        self.mem = vh.alloc.get_mem()
        self.exec_impl = vh.slm.exec_impl
        # ExecBase is stored at address 4 on Amiga systems
        self.exec_base_addr = self.mem.r32(4)
        self.boot = boot_info
        self.segment_addr = segment_addr
        # Scratch packet buffers to avoid unbounded allocs (we serialize requests).
        self._stdpkt_ring = []
        self._stdpkt_sizes = []
        self._stdpkt_index = 0
        self._stdpkt_ring_size = 8
        # Cache hot struct sizes/offsets for packet marshalling.
        self._msg_size = MessageStruct.get_size()
        self._pkt_size = DosPacketStruct.get_size()
        self._msg_ln_succ_offset = NodeStruct.sdef.find_field_def_by_name("ln_Succ").offset
        self._msg_ln_pred_offset = NodeStruct.sdef.find_field_def_by_name("ln_Pred").offset
        self._msg_ln_type_offset = NodeStruct.sdef.find_field_def_by_name("ln_Type").offset
        self._msg_ln_name_offset = NodeStruct.sdef.find_field_def_by_name("ln_Name").offset
        self._mn_replyport_offset = MessageStruct.sdef.find_field_def_by_name("mn_ReplyPort").offset
        self._mn_length_offset = MessageStruct.sdef.find_field_def_by_name("mn_Length").offset
        self._dp_link_offset = DosPacketStruct.sdef.find_field_def_by_name("dp_Link").offset
        self._dp_port_offset = DosPacketStruct.sdef.find_field_def_by_name("dp_Port").offset
        self._dp_type_offset = DosPacketStruct.sdef.find_field_def_by_name("dp_Type").offset
        self._dp_arg1_offset = DosPacketStruct.sdef.find_field_def_by_name("dp_Arg1").offset
        self._lh_head_offset = ListStruct.sdef.find_field_def_by_name("lh_Head").offset
        self._lh_tail_offset = ListStruct.sdef.find_field_def_by_name("lh_Tail").offset
        self._lh_tailpred_offset = ListStruct.sdef.find_field_def_by_name("lh_TailPred").offset
        self._lh_type_offset = ListStruct.sdef.find_field_def_by_name("lh_Type").offset
        # Cache mp_SigBit offset for signal computation
        self._mp_sigbit_offset = MsgPortStruct.sdef.find_field_def_by_name("mp_SigBit").offset
        self._mp_msglist_offset = MsgPortStruct.sdef.find_field_def_by_name("mp_MsgList").offset
        # Local signal allocation tracking (bits 0-31, lower 16 reserved by system)
        self._signals_allocated = 0x0000FFFF  # Reserve first 16 signals
        # Debug flag (set by launch_with_startup); consistent with codebase self._debug pattern
        self._debug = False
        # Rate-limit warning for tc_SigRecvd read failures (once per instance)
        self._warned_tc_sigrecvd = False

    def _compute_pending_signals(self, mask: int = 0xFFFFFFFF) -> int:
        """Compute pending signals from all message ports AND tc_SigRecvd, ANDed with mask.

        Signals can come from two sources:
        1. Message ports with pending messages (DOS packets)
        2. tc_SigRecvd - signals set directly by device drivers (IO completion)
        """
        from amitools.vamos.libstructs.exec_ import ExecLibraryStruct, TaskStruct

        pending = 0
        this_task = 0

        # First, include any signals already set in tc_SigRecvd (e.g., IO completion)
        try:
            exec_base = self.mem.r32(4)
            if exec_base != 0:
                this_task_off = ExecLibraryStruct.sdef.find_field_def_by_name("ThisTask").offset
                this_task = self.mem.r32(exec_base + this_task_off)
                if this_task != 0:
                    sigrecvd_off = TaskStruct.sdef.find_field_def_by_name("tc_SigRecvd").offset
                    pending = self.mem.r32(this_task + sigrecvd_off)
        except Exception as e:
            if not self._warned_tc_sigrecvd:
                self._warned_tc_sigrecvd = True
                if self._debug:
                    print(f"[amifuse] WARNING: tc_SigRecvd read failed: {e}")

        # Then add signals from message ports with pending messages
        port_mgr = self.exec_impl.port_mgr
        for port_addr, port in port_mgr.ports.items():
            try:
                if port.queue is not None and len(port.queue) > 0:
                    flags = self.mem.r8(
                        port_addr
                        + MsgPortStruct.sdef.find_field_def_by_name("mp_Flags").offset
                    )
                    if flags != MsgPortFlags.PA_SIGNAL:
                        continue
                    sig_task = self.mem.r32(
                        port_addr
                        + MsgPortStruct.sdef.find_field_def_by_name("mp_SigTask").offset
                    )
                    if this_task != 0 and sig_task != this_task:
                        continue
                    sigbit = self.mem.read(0, port_addr + self._mp_sigbit_offset)
                    if 0 <= sigbit < 32:
                        pending |= 1 << sigbit
            except Exception:
                continue
        return pending & mask

    def _clear_signals_from_task(self, signals: int):
        """Clear the specified signals from tc_SigRecvd.

        This mimics what Wait() does when it returns - it atomically clears
        the returned signals from tc_SigRecvd. When we resume from a Wait()
        block without actually running Wait(), we must clear signals ourselves.
        """
        from amitools.vamos.libstructs.exec_ import ExecLibraryStruct, TaskStruct
        try:
            exec_base = self.mem.r32(4)
            if exec_base != 0:
                this_task_off = ExecLibraryStruct.sdef.find_field_def_by_name("ThisTask").offset
                this_task = self.mem.r32(exec_base + this_task_off)
                if this_task != 0:
                    sigrecvd_off = TaskStruct.sdef.find_field_def_by_name("tc_SigRecvd").offset
                    current = self.mem.r32(this_task + sigrecvd_off)
                    self.mem.w32(this_task + sigrecvd_off, current & ~signals)
        except Exception:
            pass

    def _capture_cpu_regs(self, state: HandlerLaunchState):
        """Save the current D0-D7/A0-A7 state for later handler resumes."""
        cpu = self.vh.machine.cpu
        state.regs = [cpu.r_reg(i) for i in range(16)]

    def _set_saved_reg(self, state: HandlerLaunchState, reg_num: int, value: int):
        """Keep state.regs in sync with manual register adjustments."""
        if state.regs is None:
            self._capture_cpu_regs(state)
        state.regs[reg_num] = value

    # ---- low-level helpers ----

    def _write_bstr(self, text: str, label: str) -> int:
        # Use latin-1 encoding for Amiga compatibility (handles chars 0-255)
        encoded = text.encode("latin-1", errors="replace")
        # Some handlers temporarily treat BSTR payloads as C strings and
        # read/write one byte past the counted payload to append a NUL.
        data = bytes([len(encoded)]) + encoded + b"\x00"
        mem_obj = self.alloc.alloc_memory(len(data), label=label)
        self.mem.w_block(mem_obj.addr, data)
        return mem_obj.addr

    def _init_msgport(
        self,
        port_addr: int,
        task_addr: int,
        sigbit: int = None,
        flags: int = MsgPortFlags.PA_SIGNAL,
    ):
        mp = MsgPortStruct(self.mem, port_addr)
        # zero first to clear garbage
        self.mem.w_block(port_addr, b"\x00" * MsgPortStruct.get_size())
        mp.node.type.val = NodeType.NT_MSGPORT
        mp.flags.val = flags
        if sigbit is None:
            sigbit = self._alloc_signal_bit()
        else:
            # Mark the signal bit as allocated in our local tracking
            if 0 <= sigbit < 32:
                self._signals_allocated |= (1 << sigbit)
        mp.sig_bit.val = sigbit
        mp.sig_task.aptr = task_addr
        # Initialize mp_MsgList as a proper empty Amiga list
        # An empty list has: lh_Head -> &lh_Tail, lh_Tail = 0, lh_TailPred -> &lh_Head
        list_addr = port_addr + self._mp_msglist_offset
        lst = ListStruct(self.mem, list_addr)
        # Get addresses of list header fields
        lh_head_addr = list_addr + self._lh_head_offset
        lh_tail_addr = list_addr + self._lh_tail_offset
        # Empty list: Head points to Tail address, TailPred points to Head address
        lst.head.aptr = lh_tail_addr
        lst.tail.aptr = 0
        lst.tail_pred.aptr = lh_head_addr
        lst.type.val = NodeType.NT_MESSAGE
        return sigbit

    def _create_port(
        self,
        name: str,
        task_addr: int,
        sigbit: int = None,
        flags: int = MsgPortFlags.PA_SIGNAL,
    ) -> int:
        port_mem = self.alloc.alloc_memory(MsgPortStruct.get_size(), label=name)
        self._init_msgport(port_mem.addr, task_addr, sigbit=sigbit, flags=flags)
        if not self.exec_impl.port_mgr.has_port(port_mem.addr):
            self.exec_impl.port_mgr.register_port(port_mem.addr)
        return port_mem.addr

    def _set_exec_this_task(self, proc_addr: int, stack: Stack):
        exec_struct = ExecLibraryStruct(self.mem, self.exec_base_addr)
        exec_struct.sfields.get_field_by_name("ThisTask").aptr = proc_addr
        exec_struct.sfields.get_field_by_name("SysStkLower").aptr = stack.get_lower()
        exec_struct.sfields.get_field_by_name("SysStkUpper").aptr = stack.get_upper()
        # keep exec impl in sync for StackSwap
        self.exec_impl.stk_lower = stack.get_lower()
        self.exec_impl.stk_upper = stack.get_upper()

    def _create_process(self, name="handler") -> Tuple[int, int, Stack]:
        stack = Stack.alloc(self.alloc, 32 * 1024, name=name + "_stack")
        # clear stack to avoid garbage parameters being consumed by the handler
        self.mem.w_block(stack.get_lower(), b"\x00" * stack.get_size())
        proc_mem = self.alloc.alloc_memory(ProcessStruct.get_size(), label="HandlerProcess")
        # clear struct to avoid garbage pointers
        self.mem.w_block(proc_mem.addr, b"\x00" * ProcessStruct.get_size())
        proc = ProcessStruct(self.mem, proc_mem.addr)
        name_cstr = self.alloc.alloc_memory(len(name) + 1, label="HandlerName")
        self.mem.w_cstr(name_cstr.addr, name)
        proc.task.node.type.val = NodeType.NT_PROCESS
        proc.task.node.name.aptr = name_cstr.addr
        proc.task.node.pri.val = 0
        proc.task.state.val = TaskState.TS_READY
        proc.task.flags.val = 0
        proc.task.id_nest_cnt.val = 0
        proc.task.td_nest_cnt.val = 0
        proc.task.sp_reg.aptr = stack.get_initial_sp()
        proc.task.sp_lower.aptr = stack.get_lower()
        proc.task.sp_upper.aptr = stack.get_upper()
        proc.stack_size.val = stack.get_size()
        # pr_StackBase should be the upper bound (top) of the stack allocation.
        # The 68k stack grows downward, so the "base" for StackSwap/Exit is the
        # highest address.  Note: amitools' DosLibrary.py uses get_lower() but
        # that is inconsistent with how handlers (SFS, PFS3) interpret the field.
        proc.stack_base.bptr = stack.get_upper() >> 2
        proc.glob_vec.aptr = 0xFFFFFFFF
        # NOTE: Do NOT set pr_CLI - SFS checks this to distinguish handler vs CLI
        # invocation, and rejects startup if pr_CLI is set.
        # dummy file handles for stdio
        fh_in_mem = self.alloc.alloc_struct(FileHandleStruct, label="NullFHIn")
        fh_out_mem = self.alloc.alloc_struct(FileHandleStruct, label="NullFHOut")
        fh_in = FileHandleStruct(self.mem, fh_in_mem.addr)
        fh_out = FileHandleStruct(self.mem, fh_out_mem.addr)
        fh_in.type.aptr = 0
        fh_out.type.aptr = 0
        proc.cis.bptr = fh_in_mem.addr >> 2
        proc.cos.bptr = fh_out_mem.addr >> 2
        proc.console_task.aptr = 0
        proc.window_ptr.aptr = 0xFFFFFFFF
        port_addr = proc.msg_port.addr
        # FFS TaskWait uses a fixed SIGMASK (0x100), so set mp_SigBit=8.
        sigbit = self._init_msgport(port_addr, proc_mem.addr, sigbit=8)
        # advertise signal allocation in task
        proc.task.sig_alloc.val = 1 << sigbit
        proc.task.sig_wait.val = 0
        proc.task.sig_recvd.val = 0
        self._set_exec_this_task(proc_mem.addr, stack)
        # register MsgPort with exec port manager so WaitPort/GetMsg see it
        if not self.exec_impl.port_mgr.has_port(port_addr):
            self.exec_impl.port_mgr.register_port(port_addr)
        return proc_mem.addr, port_addr, stack

    def alloc_bstr(self, text: str, label: str = "bstr") -> Tuple[int, int]:
        """Return (addr, bptr) of a freshly allocated BSTR."""
        addr = self._write_bstr(text, label)
        return addr, addr >> 2

    def _build_std_packet(
        self, dest_port_addr: int, reply_port_addr: int, pkt_type: int, args
    ) -> Tuple[int, int]:
        """Allocate contiguous StandardPacket = Message + DosPacket."""
        total = self._msg_size + self._pkt_size
        if not self._stdpkt_ring:
            self._stdpkt_ring = [None] * self._stdpkt_ring_size
            self._stdpkt_sizes = [0] * self._stdpkt_ring_size
        idx = self._stdpkt_index
        self._stdpkt_index = (idx + 1) % self._stdpkt_ring_size
        sp_mem = self._stdpkt_ring[idx]
        if sp_mem is None or total > self._stdpkt_sizes[idx]:
            new_mem = self.alloc.alloc_memory(total, label=f"stdpkt_scratch_{idx}")
            if sp_mem is not None:
                self.alloc.free_memory(sp_mem)
            sp_mem = new_mem
            self._stdpkt_ring[idx] = sp_mem
            self._stdpkt_sizes[idx] = total
        sp_addr = sp_mem.addr
        self.mem.w_block(sp_addr, b"\x00" * total)
        pkt_addr = sp_addr + self._msg_size
        # message
        self.mem.w8(sp_addr + self._msg_ln_type_offset, NodeType.NT_MESSAGE)
        self.mem.w32(sp_addr + self._msg_ln_succ_offset, 0)
        self.mem.w32(sp_addr + self._msg_ln_pred_offset, 0)
        self.mem.w32(sp_addr + self._mn_replyport_offset, reply_port_addr & 0xFFFFFFFF)
        self.mem.w16(sp_addr + self._mn_length_offset, total)
        self.mem.w32(sp_addr + self._msg_ln_name_offset, pkt_addr & 0xFFFFFFFF)
        # packet
        self.mem.w32(pkt_addr + self._dp_link_offset, sp_addr & 0xFFFFFFFF)
        self.mem.w32(pkt_addr + self._dp_port_offset, reply_port_addr & 0xFFFFFFFF)
        self.mem.w32(pkt_addr + self._dp_type_offset, pkt_type & 0xFFFFFFFF)
        arg_addr = pkt_addr + self._dp_arg1_offset
        for val in args[:7]:
            self.mem.w32(arg_addr, val & 0xFFFFFFFF)
            arg_addr += 4
        # queue message to destination port - both Python queue and Amiga memory
        self.exec_impl.port_mgr.put_msg(dest_port_addr, sp_addr)
        # Also link the message into the port's mp_MsgList in memory
        # This is necessary because some handlers (like FFS) read mp_MsgList directly
        self._link_msg_to_port(dest_port_addr, sp_addr)
        # Signal the target task like real AmigaOS PutMsg() does.
        # Without this, Wait() never sees the DOS port signal because
        # _fallback_signals is not set - only the Python port queue has the
        # message.  Handlers that call Wait(mask) would block indefinitely.
        try:
            sigbit = self.mem.r8(dest_port_addr + self._mp_sigbit_offset)
            if 0 <= sigbit < 32:
                from amitools.vamos.lib.lexec.signalfunc import SignalFunc
                SignalFunc._fallback_signals |= 1 << sigbit
        except Exception as e:
            if self._debug:
                print(f"[amifuse] WARNING: signal delivery failed for port 0x{dest_port_addr:x}: {e}")
        return pkt_addr, sp_addr

    def _link_msg_to_port(self, port_addr: int, msg_addr: int):
        """Link a message into a port's mp_MsgList in Amiga memory.

        This ensures handlers that read mp_MsgList directly (without calling GetMsg)
        can find the message.

        NOTE: We always reinitialize the list as a single-element list. The vamos
        GetMsg trap removes from the Python queue but doesn't unlink from the
        memory list. By always reinitializing, we avoid list corruption from
        stale entries.
        """
        list_addr = port_addr + self._mp_msglist_offset
        lh_head_addr = list_addr + self._lh_head_offset
        lh_tail_addr = list_addr + self._lh_tail_offset

        # Always reinitialize as single-element list to avoid stale entry issues
        # lh_Head points to first node, lh_Tail is 0 (end marker), lh_TailPred points to last node
        self.mem.w32(list_addr + self._lh_head_offset, msg_addr & 0xFFFFFFFF)
        self.mem.w32(list_addr + self._lh_tail_offset, 0)
        self.mem.w32(list_addr + self._lh_tailpred_offset, msg_addr & 0xFFFFFFFF)
        self.mem.w8(list_addr + self._lh_type_offset, NodeType.NT_MESSAGE)
        # Message node: ln_Succ points to lh_Tail (end of list), ln_Pred points to lh_Head
        self.mem.w32(msg_addr + self._msg_ln_succ_offset, lh_tail_addr & 0xFFFFFFFF)
        self.mem.w32(msg_addr + self._msg_ln_pred_offset, lh_head_addr & 0xFFFFFFFF)

    # ---- public orchestration ----

    def launch_with_startup(self, extra_packets=None, debug=False) -> HandlerLaunchState:
        self._debug = debug
        proc_addr, port_addr, stack = self._create_process(name="amifuse_handler")
        # The caller reply port is consumed by host-side polling, not by the
        # Amiga handler task. Keep it out of the handler's signal namespace.
        reply_port = self._create_port(
            "caller_port",
            0,
            sigbit=0xFF,
            flags=MsgPortFlags.PA_IGNORE,
        )
        # fill DeviceNode dn_Task now that we have a port
        dn = DeviceNodeStruct(self.mem, self.boot["dn_addr"])
        dn.sfields.get_field_by_name("dn_Task").val = port_addr
        # startup packet args per pfs3: Arg1=mount name, Arg2=FSSM BPTR, Arg3=DeviceNode BPTR
        startup_pkt, startup_msg = self._build_std_packet(
            port_addr,
            reply_port,
            ACTION_STARTUP,
            [
                self.boot["dn_name_addr"] >> 2,
                self.boot["fssm_addr"] >> 2,
                self.boot["dn_addr"] >> 2,
            ],
        )
        # queue any additional packets before starting the handler
        if extra_packets:
            for pkt_type, args in extra_packets:
                self._build_std_packet(port_addr, reply_port, pkt_type, args)
        # Build entry stub that jumps to segment start.
        # Handler's own startup code will set up registers and call WaitPort/GetMsg
        # to retrieve the startup packet from pr_MsgPort.
        stub_pc = build_entry_stub(self.mem, self.alloc, self.segment_addr)

        # Register a minimal task with exec context so StackSwap and similar calls work.
        # This provides ctx.task.get_stack() for library calls that need stack info.
        handler_task = HandlerTask(stack)
        if hasattr(self.vh, 'slm') and hasattr(self.vh.slm, 'exec_ctx'):
            self.vh.slm.exec_ctx.set_cur_task_process(handler_task, None)

        return HandlerLaunchState(
            process_addr=proc_addr,
            port_addr=port_addr,
            stack=stack,
            stdpkt_addr=startup_pkt,
            msg_addr=startup_msg,
            run_state=None,
            pc=stub_pc,
            sp=stack.get_initial_sp(),
            started=False,
            reply_port_addr=reply_port,
            entry_stub_pc=stub_pc,
        )

    def setup_resume_if_blocked(self, state: HandlerLaunchState, debug: bool = False) -> bool:
        """Check if handler is blocked waiting and set up resume state if signal/message pending.

        This should be called before run_burst to handle the case where a message
        was queued or signal was set since the last burst. Returns True if resume was set up.
        """
        block_state = _get_active_block_state(state.block_state)
        if block_state is None:
            return False

        mem = self.vh.alloc.get_mem()
        cpu = self.vh.machine.cpu
        resume = _build_resume_frame(
            block_state,
            default_port_addr=state.port_addr,
            mem=mem,
            port_mgr=self.exec_impl.port_mgr,
            compute_pending_signals=self._compute_pending_signals,
            clear_signals_from_task=self._clear_signals_from_task,
            debug=debug,
        )
        if resume is None:
            return False

        state.pc = resume["pc"]
        state.sp = resume["sp"]
        cpu.w_reg(REG_D0, resume["d0"])
        self._set_saved_reg(state, REG_D0, resume["d0"])

        # Clear blocked state
        _clear_all_block_state()
        state.block_state = None

        return True

    def run_burst(self, state: HandlerLaunchState, max_cycles=200000, debug: bool = False):
        """Run the handler from its current PC/SP for a limited number of cycles."""
        import sys
        from types import SimpleNamespace
        # If handler has already crashed, don't try to run it
        if state.crashed:
            return state.run_state

        # Check if we need to resume from a blocked Wait/WaitPort
        resumed = self.setup_resume_if_blocked(state, debug=debug)
        active_block_state = _get_active_block_state(state.block_state)
        if not resumed and active_block_state is not None:
            # The handler is genuinely parked in Wait()/WaitPort() with no
            # wakeup condition yet. Re-entering at the saved return PC would
            # bypass the blocking API and immediately fall into GetMsg(None).
            state.run_state = SimpleNamespace(
                done=False,
                error=True,
                cycles=0,
                pc=state.pc,
                sp=state.sp,
            )
            return state.run_state

        cpu = self.vh.machine.cpu
        mem = self.vh.alloc.get_mem()
        ram_end = self.vh.machine.get_ram_total()

        def _pc_valid(pc: int) -> bool:
            # 0x800 is the minimum valid code address; below this is Amiga system
            # vectors, trap handlers, and reserved memory that cannot contain handler code.
            return 0x800 <= pc < ram_end

        # Validate PC before attempting to run - if it's garbage, handler is dead
        if not _pc_valid(state.pc):
            state.crashed = True
            state.last_error_pc = state.pc
            return state.run_state

        # Restore the saved CPU frame first.
        #
        # Do not blindly reset A6 on every burst: the handler may be resumed
        # in the middle of compiled C code, and the compiler is free to keep
        # live state in A6. Clobbering it on ordinary cycle-limit resumes
        # corrupts locals such as handler-global pointers. Seed ExecBase only
        # when entering from scratch, or when a synthetic Wait/WaitPort resume
        # left A6 as 0.
        if state.regs is not None:
            for i in range(16):
                cpu.w_reg(i, state.regs[i])
        first_run = not state.started
        seed_execbase = first_run or state.regs is None
        if resumed and state.regs is not None and state.regs[REG_A6] == 0:
            seed_execbase = True
        if (
            state.main_loop_pc
            and state.pc == state.main_loop_pc
            and state.regs is not None
            and state.regs[REG_A6] == 0
        ):
            # Some reopen paths restart directly at the saved main loop PC
            # after the explicit Wait/WaitPort block state has already been
            # cleared. Treat a NULL A6 there the same as a synthetic wakeup.
            seed_execbase = True
        set_regs = {REG_A6: self.exec_base_addr} if seed_execbase else None
        if seed_execbase:
            cpu.w_reg(REG_A6, self.exec_base_addr)
            self._set_saved_reg(state, REG_A6, self.exec_base_addr)
        if not state.started:
            state.started = True
        # After the first execution, pre-set CPU SP to match state.sp so
        # machine.run()'s prepare() heuristic (|sp - current_sp| > 16)
        # does NOT fire.  prepare() pushes an exit trap on the stack which
        # is needed for the initial startup (clean exit if handler RTS's)
        # but would corrupt the handler's stack during resumption from a
        # Wait/WaitPort block.
        run_sp = state.sp
        if not first_run:
            cpu.w_sp(run_sp)
        run_state = self.vh.machine.run(
            state.pc,
            sp=run_sp,
            set_regs=set_regs,
            max_cycles=max_cycles,
            cycles_per_run=max_cycles,
            name="handler_burst",
        )
        state.run_state = run_state
        new_pc = cpu.r_pc()
        new_sp = cpu.r_reg(REG_A7)
        self._capture_cpu_regs(state)

        # Detect crash: if error occurred and new_pc is garbage, handler is dead
        if run_state.error and not _pc_valid(new_pc):
            state.consecutive_errors += 1
            # After first error, mark as crashed - don't try to recover
            # The handler's internal state is corrupted and restarting won't help
            if state.consecutive_errors == 1:
                # Dump CPU state for debugging
                d_regs = [cpu.r_reg(i) for i in range(8)]
                a_regs = [cpu.r_reg(8 + i) for i in range(8)]
                print(f"\n[amifuse] FATAL: Handler crashed", file=sys.stderr)
                print(f"[amifuse]   Initial PC=0x{state.pc:x} crashed at PC=0x{new_pc:x}", file=sys.stderr)
                print(f"[amifuse]   main_loop_pc=0x{state.main_loop_pc:x} entry_stub_pc=0x{state.entry_stub_pc:x}", file=sys.stderr)
                print(f"[amifuse]   D0-D7: {' '.join(f'{r:08x}' for r in d_regs)}", file=sys.stderr)
                print(f"[amifuse]   A0-A7: {' '.join(f'{r:08x}' for r in a_regs)}", file=sys.stderr)
                if a_regs[6] == 0:
                    print(f"[amifuse]   NOTE: A6 (ExecBase) is NULL - handler lost ExecBase reference", file=sys.stderr)
                print(f"[amifuse]   Restart amifuse to recover.", file=sys.stderr)
            state.crashed = True
            state.last_error_pc = new_pc
            return run_state
        # Check if WaitPort or Wait blocked in the current burst.
        block_state = _snapshot_block_state()
        waitport_sp = block_state.get("waitport_blocked_sp")
        wait_sp = block_state.get("wait_blocked_sp")
        waitport_ret = block_state.get("waitport_blocked_ret")
        wait_ret = block_state.get("wait_blocked_ret")
        wait_mask = block_state.get("wait_blocked_mask")
        # Use whichever blocking call was triggered
        blocked_sp = waitport_sp if waitport_sp is not None else wait_sp
        blocked_ret = waitport_ret if waitport_ret is not None else wait_ret
        # Capture updated PC/SP for the next burst, but only if the run was
        # interrupted mid-execution (cycle limit). If run_state.done is True,
        # the handler returned via the exit trap (PC would be 0x402 = hw_exc_addr,
        # which is invalid for re-entry). If there's an error, the PC might also
        # be at an invalid location.
        # Check error first since _terminate_run sets both error and done
        if run_state.error:
            # WaitPort/Wait or other blocking call failed - handler is waiting for a message.
            if blocked_sp is not None:
                state.block_state = block_state
                ret_addr = blocked_ret if blocked_ret is not None else mem.r32(blocked_sp)
                # Save as main loop PC if not yet set - this is where handler waits for messages
                if (not state.initialized or state.main_loop_pc == 0) and _pc_valid(ret_addr):
                    state.main_loop_pc = ret_addr
                    state.main_loop_sp = blocked_sp + 4
                    state.initialized = True
                    # Capture Wait mask for _flush_pending_signals before
                    # _clear_all_block_state wipes it.
                    if wait_mask is not None and not state.wait_mask:
                        state.wait_mask = wait_mask
                # Check if there's something pending that would wake the handler
                if wait_mask is not None:
                    # Wait() blocked - check for ANY pending signals (messages OR IO completion)
                    pending = self._compute_pending_signals(wait_mask)
                    has_pending = pending != 0
                    if debug:
                        all_pending = self._compute_pending_signals(0xFFFFFFFF)
                        print(f"[run_burst] Wait block: ret=0x{ret_addr:x} mask=0x{wait_mask:x} pending_masked=0x{pending:x} pending_all=0x{all_pending:x} has_pending={has_pending}")
                else:
                    # WaitPort blocked - check for message on the port
                    has_pending = self.exec_impl.port_mgr.has_msg(state.port_addr)
                if has_pending:
                    # Resume from blocking call return address (saved on stack)
                    if _pc_valid(ret_addr):
                        state.pc = ret_addr
                        state.sp = blocked_sp + 4
                    elif state.main_loop_pc and _pc_valid(state.main_loop_pc):
                        state.pc = state.main_loop_pc
                        state.sp = state.main_loop_sp
                    # CRITICAL: Set D0 before resuming from Wait() or WaitPort()
                    if wait_mask is not None:
                        # Wait() resume - set D0 to pending signals
                        if wait_mask == 0:
                            pending = self._compute_pending_signals(0xFFFFFFFF)
                        else:
                            pending = self._compute_pending_signals(wait_mask)
                        cpu.w_reg(REG_D0, pending)
                        self._set_saved_reg(state, REG_D0, pending)
                        if debug:
                            print(f"[run_burst] Wait resume: D0=0x{pending:x}")
                        # Clear returned signals from tc_SigRecvd (like Wait() would)
                        self._clear_signals_from_task(pending)
                    elif waitport_sp is not None:
                        # WaitPort()/WaitPkt() resume - set D0 appropriately
                        waitport_port = block_state.get("waitport_blocked_port")
                        if waitport_port is not None:
                            # CRITICAL: use get_msg (not peek_msg) to dequeue.
                            msg_addr = self.exec_impl.port_mgr.get_msg(waitport_port)
                            if msg_addr:
                                _unlink_msg_from_m68k_list(self.mem, msg_addr, debug=debug)
                            if block_state.get("waitpkt_blocked", False) and msg_addr:
                                # WaitPkt() resume - extract packet from message
                                msg = MessageStruct(self.mem, msg_addr)
                                pkt_addr = msg.node.name.aptr
                                d0_val = pkt_addr if pkt_addr else 0
                            else:
                                # WaitPort() resume - return message address
                                d0_val = msg_addr if msg_addr else 0
                            cpu.w_reg(REG_D0, d0_val)
                            self._set_saved_reg(state, REG_D0, d0_val)
                    # Clear both blocking states
                    _clear_all_block_state()
                    state.block_state = None
                else:
                    # No message pending - save restart point for later
                    if _pc_valid(ret_addr):
                        state.pc = ret_addr
                        state.sp = blocked_sp + 4
                    elif state.main_loop_pc and _pc_valid(state.main_loop_pc):
                        state.pc = state.main_loop_pc
                        state.sp = state.main_loop_sp
            elif state.initialized and state.main_loop_pc != 0:
                # Fall back to saved main loop PC
                state.pc = state.main_loop_pc
                state.sp = state.main_loop_sp
            # else: keep current PC (error happened during initialization)
        elif run_state.done:
            # Handler exited via exit trap.
            # Check if WaitPort/Wait blocked - if so, we can get the return address from the saved SP
            if blocked_sp is not None:
                state.block_state = block_state
                ret_addr = blocked_ret if blocked_ret is not None else mem.r32(blocked_sp)
                # Save as main loop PC if not yet initialized
                if (not state.initialized or state.main_loop_pc == 0) and _pc_valid(ret_addr):
                    state.main_loop_pc = ret_addr
                    state.main_loop_sp = blocked_sp + 4  # Pop return address
                    state.initialized = True
                    if wait_mask is not None and not state.wait_mask:
                        state.wait_mask = wait_mask
                # Only restart immediately if there's something pending (message or signal).
                # Otherwise, leave state pointing to Wait/WaitPort return - caller
                # will queue a message before calling run_burst again.
                if wait_mask is not None:
                    # Wait() blocked - check for ANY pending signals (messages OR IO completion)
                    pending = self._compute_pending_signals(wait_mask)
                    has_pending = pending != 0
                else:
                    # WaitPort blocked - check for message on the port
                    has_pending = self.exec_impl.port_mgr.has_msg(state.port_addr)
                if has_pending:
                    # Restart from return address (where Wait/WaitPort was called)
                    if _pc_valid(ret_addr):
                        state.pc = ret_addr
                        state.sp = blocked_sp + 4  # Pop the return address
                    elif state.main_loop_pc and _pc_valid(state.main_loop_pc):
                        state.pc = state.main_loop_pc
                        state.sp = state.main_loop_sp
                    # CRITICAL: Set D0 before resuming from Wait() or WaitPort()
                    if wait_mask is not None:
                        # Wait() resume - set D0 to pending signals
                        if wait_mask == 0:
                            pending = self._compute_pending_signals(0xFFFFFFFF)
                        else:
                            pending = self._compute_pending_signals(wait_mask)
                        cpu.w_reg(REG_D0, pending)
                        self._set_saved_reg(state, REG_D0, pending)
                        # Clear returned signals from tc_SigRecvd (like Wait() would)
                        self._clear_signals_from_task(pending)
                    elif waitport_sp is not None:
                        # WaitPort()/WaitPkt() resume - set D0 appropriately
                        waitport_port = block_state.get("waitport_blocked_port")
                        if waitport_port is not None:
                            # CRITICAL: use get_msg (not peek_msg) to dequeue.
                            msg_addr = self.exec_impl.port_mgr.get_msg(waitport_port)
                            if msg_addr:
                                _unlink_msg_from_m68k_list(self.mem, msg_addr, debug=debug)
                            if block_state.get("waitpkt_blocked", False) and msg_addr:
                                # WaitPkt() resume - extract packet from message
                                msg = MessageStruct(self.mem, msg_addr)
                                pkt_addr = msg.node.name.aptr
                                d0_val = pkt_addr if pkt_addr else 0
                            else:
                                # WaitPort() resume - return message address
                                d0_val = msg_addr if msg_addr else 0
                            cpu.w_reg(REG_D0, d0_val)
                            self._set_saved_reg(state, REG_D0, d0_val)
                    # Clear the blocked states
                    _clear_all_block_state()
                    state.block_state = None
                    state.exit_count = 0  # Reset exit counter
                else:
                    # No message pending - save restart point but don't spin.
                    # Next run_burst will restart from here when a message is queued.
                    if _pc_valid(ret_addr):
                        state.pc = ret_addr
                        state.sp = blocked_sp + 4
                    elif state.main_loop_pc and _pc_valid(state.main_loop_pc):
                        state.pc = state.main_loop_pc
                        state.sp = state.main_loop_sp
                    # Keep blocked state so we know handler is waiting
            else:
                # Normal exit trap (RTS to run_exit_addr) without WaitPort block.
                # For FFS, this means startup processing is complete. Check if there's
                # a reply we can read from the packet.
                has_pending = self.exec_impl.port_mgr.has_msg(state.port_addr)
                if has_pending and state.initialized and state.main_loop_pc != 0:
                    # Messages waiting and we have a valid restart point
                    state.pc = state.main_loop_pc
                    state.sp = state.main_loop_sp
                # else: Startup complete or no restart point - let caller check packet results
        else:
            state.block_state = None
            if _pc_valid(new_pc):
                state.pc = new_pc
                state.sp = new_sp
            elif state.main_loop_pc and _pc_valid(state.main_loop_pc):
                state.pc = state.main_loop_pc
                state.sp = state.main_loop_sp
        return run_state

    def send_disk_info(self, state: HandlerLaunchState, info_buf_addr: int):
        # Arg1 = InfoData* (APTR)
        return self.send_packet(state, ACTION_DISK_INFO, [info_buf_addr])

    def send_read(self, state: HandlerLaunchState, buf_addr: int, offset_bytes: int, length_bytes: int):
        # Arg1 = window (not used), Arg2 = offset (block), Arg3 = buf, Arg4 = length
        # pfs3 uses TD style; offset in bytes, length in bytes; Arg1 ignored
        return self.send_packet(
            state,
            ACTION_READ,
            [
                0,
                offset_bytes,
                buf_addr,
                length_bytes,
            ],
        )

    def poll_replies(self, port_addr: int, debug: bool = False):
        """Return a list of (msg_addr, pkt_addr, res1, res2) for all queued replies."""
        results = []
        pmgr = self.exec_impl.port_mgr
        has_port = pmgr.has_port(port_addr)
        has_msg = pmgr.has_msg(port_addr) if has_port else False
        if debug:
            print(f"[poll_replies] port=0x{port_addr:x} has_port={has_port} has_msg={has_msg}")
        while has_port and has_msg:
            msg_addr = pmgr.get_msg(port_addr)
            msg = MessageStruct(self.mem, msg_addr)
            pkt_addr = msg.node.name.aptr
            pkt = DosPacketStruct(self.mem, pkt_addr)
            res1 = pkt.res1.val
            res2 = pkt.res2.val
            if debug:
                print(f"[poll_replies] got msg=0x{msg_addr:x} pkt=0x{pkt_addr:x} res1=0x{res1:x} res2={res2}")
            results.append((msg_addr, pkt_addr, res1, res2))
            has_msg = pmgr.has_msg(port_addr)
        return results

    def send_packet(self, state: HandlerLaunchState, pkt_type: int, args):
        """Queue a packet to the handler and return the msg/pkt addresses."""
        pkt_addr, msg_addr = self._build_std_packet(
            state.port_addr, state.reply_port_addr, pkt_type, args
        )
        return pkt_addr, msg_addr

    def send_locate(
        self,
        state: HandlerLaunchState,
        lock_bptr: int,
        name_bptr: int,
        mode: int = SHARED_LOCK,
    ):
        """ACTION_LOCATE_OBJECT: returns a new lock BPTR in dp_Res1."""
        return self.send_packet(state, 8, [lock_bptr, name_bptr, mode])

    def send_examine(self, state: HandlerLaunchState, lock_bptr: int, fib_addr: int):
        """ACTION_EXAMINE_OBJECT"""
        return self.send_packet(state, 23, [lock_bptr, fib_addr >> 2])

    def send_examine_next(self, state: HandlerLaunchState, lock_bptr: int, fib_addr: int):
        """ACTION_EXAMINE_NEXT"""
        return self.send_packet(state, 24, [lock_bptr, fib_addr >> 2])

    def send_free_lock(self, state: HandlerLaunchState, lock_bptr: int):
        """ACTION_FREE_LOCK: release a lock returned by LOCATE."""
        return self.send_packet(state, ACTION_FREE_LOCK, [lock_bptr])

    def send_findinput(
        self, state: HandlerLaunchState, name_bptr: int, dir_lock_bptr: int = 0, fh_addr: int = None
    ):
        """ACTION_FINDINPUT: allocate a FileHandle if needed, return (fh_addr, pkt_addr, msg_addr)."""
        if fh_addr is None:
            fh_mem = self.alloc.alloc_struct(FileHandleStruct, label="FindInputFH")
            self.mem.w_block(fh_mem.addr, b"\x00" * FileHandleStruct.get_size())
            fh_addr = fh_mem.addr
        pkt_addr, msg_addr = self.send_packet(
            state,
            ACTION_FINDINPUT,
            [
                fh_addr >> 2,  # BPTR to FileHandle
                dir_lock_bptr,
                name_bptr,
            ],
        )
        return fh_addr, pkt_addr, msg_addr

    def send_findupdate(
        self, state: HandlerLaunchState, name_bptr: int, dir_lock_bptr: int = 0, fh_addr: int = None
    ):
        """ACTION_FINDUPDATE: open or create a file for read/write."""
        if fh_addr is None:
            fh_mem = self.alloc.alloc_struct(FileHandleStruct, label="FindUpdateFH")
            self.mem.w_block(fh_mem.addr, b"\x00" * FileHandleStruct.get_size())
            fh_addr = fh_mem.addr
        pkt_addr, msg_addr = self.send_packet(
            state,
            ACTION_FINDUPDATE,
            [
                fh_addr >> 2,  # BPTR to FileHandle
                dir_lock_bptr,
                name_bptr,
            ],
        )
        return fh_addr, pkt_addr, msg_addr

    def send_findoutput(
        self, state: HandlerLaunchState, name_bptr: int, dir_lock_bptr: int = 0, fh_addr: int = None, debug: bool = False
    ):
        """ACTION_FINDOUTPUT: open or create a file for output (truncate)."""
        if fh_addr is None:
            fh_mem = self.alloc.alloc_struct(FileHandleStruct, label="FindOutputFH")
            self.mem.w_block(fh_mem.addr, b"\x00" * FileHandleStruct.get_size())
            fh_addr = fh_mem.addr
        pkt_addr, msg_addr = self.send_packet(
            state,
            ACTION_FINDOUTPUT,
            [
                fh_addr >> 2,  # BPTR to FileHandle
                dir_lock_bptr,
                name_bptr,
            ],
        )
        return fh_addr, pkt_addr, msg_addr

    def send_delete_object(self, state: HandlerLaunchState, lock_bptr: int, name_bptr: int):
        """ACTION_DELETE_OBJECT: delete a file or directory."""
        return self.send_packet(state, ACTION_DELETE_OBJECT, [lock_bptr, name_bptr])

    def send_rename_object(
        self,
        state: HandlerLaunchState,
        src_lock_bptr: int,
        src_name_bptr: int,
        dst_lock_bptr: int,
        dst_name_bptr: int,
    ):
        """ACTION_RENAME_OBJECT: rename or move an object."""
        return self.send_packet(
            state,
            ACTION_RENAME_OBJECT,
            [
                src_lock_bptr,
                src_name_bptr,
                dst_lock_bptr,
                dst_name_bptr,
            ],
        )

    def send_create_dir(self, state: HandlerLaunchState, lock_bptr: int, name_bptr: int):
        """ACTION_CREATE_DIR: create a directory, returns new lock in res1."""
        return self.send_packet(state, ACTION_CREATE_DIR, [lock_bptr, name_bptr])

    def send_read_handle(
        self, state: HandlerLaunchState, fh_addr: int, buf_addr: int, length_bytes: int
    ):
        """ACTION_READ using a FileHandle filled by FINDINPUT; uses fh_Args as the fileentry pointer."""
        fh = FileHandleStruct(self.mem, fh_addr)
        fileentry_ptr = fh.args.val
        return self.send_packet(
            state,
            ACTION_READ,
            [
                fileentry_ptr,
                buf_addr,
                length_bytes,
            ],
        )

    def send_write_handle(
        self, state: HandlerLaunchState, fh_addr: int, buf_addr: int, length_bytes: int
    ):
        """ACTION_WRITE using a FileHandle filled by FINDOUTPUT/FINDUPDATE."""
        fh = FileHandleStruct(self.mem, fh_addr)
        fileentry_ptr = fh.args.val
        return self.send_packet(
            state,
            ACTION_WRITE,
            [
                fileentry_ptr,
                buf_addr,
                length_bytes,
            ],
        )

    def send_set_file_size(
        self, state: HandlerLaunchState, fh_addr: int, size: int, mode: int = OFFSET_BEGINNING
    ):
        """ACTION_SET_FILE_SIZE on a FileHandle; mode defaults to OFFSET_BEGINNING."""
        fh = FileHandleStruct(self.mem, fh_addr)
        fileentry_ptr = fh.args.val
        return self.send_packet(
            state,
            ACTION_SET_FILE_SIZE,
            [
                fileentry_ptr,
                size,
                mode,
            ],
        )

    def send_seek_handle(self, state: HandlerLaunchState, fh_addr: int, offset: int, mode: int = OFFSET_BEGINNING):
        """ACTION_SEEK on a FileHandle; mode defaults to OFFSET_BEGINNING."""
        fh = FileHandleStruct(self.mem, fh_addr)
        fileentry_ptr = fh.args.val
        return self.send_packet(
            state,
            ACTION_SEEK,
            [
                fileentry_ptr,
                offset,
                mode,
            ],
        )

    def send_end_handle(self, state: HandlerLaunchState, fh_addr: int):
        """ACTION_END on a FileHandle; closes the file handle in the handler."""
        fh = FileHandleStruct(self.mem, fh_addr)
        fileentry_ptr = fh.args.val
        return self.send_packet(
            state,
            ACTION_END,
            [
                fileentry_ptr,
            ],
        )

    def send_flush(self, state: HandlerLaunchState):
        """ACTION_FLUSH: flush filesystem buffers."""
        return self.send_packet(state, ACTION_FLUSH, [])

    def send_inhibit(self, state: HandlerLaunchState, inhibit: bool):
        """ACTION_INHIBIT: dp_Arg1=DOSTRUE (-1) to inhibit, DOSFALSE (0) to uninhibit."""
        return self.send_packet(state, ACTION_INHIBIT, [-1 if inhibit else 0])

    def send_format(self, state: HandlerLaunchState, volname_bptr: int, dostype: int):
        """ACTION_FORMAT: dp_Arg1=volume name BSTR (BPTR), dp_Arg2=DosType."""
        return self.send_packet(state, ACTION_FORMAT, [volname_bptr, dostype])

    def _alloc_signal_bit(self) -> int:
        """Reserve a signal bit from our local bitmap."""
        for bit in range(32):
            mask = 1 << bit
            if (self._signals_allocated & mask) == 0:
                self._signals_allocated |= mask
                return bit
        return 16  # fallback to first user signal
