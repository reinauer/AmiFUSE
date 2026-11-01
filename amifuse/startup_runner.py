"""
Launch a filesystem handler as a process under the vamos scheduler:

- build Process + MsgPort in vamos memory
- queue the ACTION_STARTUP packet (with DeviceNode/FileSysStartupMsg)
- add the handler's task to the scheduler and run it until it waits

The handler blocks in Wait(), WaitPort() or WaitPkt() like any vamos task:
the scheduler suspends its greenlet and run() returns. A packet queued to
its port, or a signal, makes it ready again, and the next run() resumes it
inside the wait, with the signals Wait() returns. Child processes it
creates with CreateNewProc() are scheduler tasks too and run in the same
run().

What it relies on in amitools (branch amifuse-0.6.0):
- Scheduler.run_until_idle(max_cycles) runs the ready tasks and returns
  RunResult.IDLE once all wait, BUSY when the budget is spent, DONE when
  none is left; a task woken with set_signal() from outside runs on at the
  next call.
- TaskBase.wait() takes the signals it returns.
- CreateNewProc() adds the child to the scheduler and frees it when it
  ends (DosLibrary.child_procs).
- The port manager carries the packets: a message put to a registered
  port queues there, and GetMsg() unlinks the message it takes from the
  port's list in memory. WaitPort()/WaitPkt() on an empty registered port
  wait in AmiFuseExecLibrary/AmiFuseDosLibrary.
"""

import sys
from dataclasses import dataclass

import greenlet
from typing import Any, Dict, Optional, Tuple

from amitools.vamos.libstructs.exec_ import (  # type: ignore
    ListStruct,
    MsgPortFlags,
    MsgPortStruct,
    NodeStruct,
    NodeType,
    TaskState,
    TaskStruct,
)
from amitools.vamos.libstructs.dos import (  # type: ignore
    DosPacketStruct,
    MessageStruct,
    FileHandleStruct,
)
from amitools.vamos.machine.regs import REG_A6  # type: ignore
from amitools.vamos.schedule import RunResult  # type: ignore
from amitools.vamos.schedule import TaskState as SchedTaskState  # type: ignore
from .amiga_structs import DeviceNodeStruct  # type: ignore
from .emu_thread import route_methods_through_emu
from .handler_process import HandlerProcess
from .handler_stub import build_entry_stub  # type: ignore


# Dos packet opcodes we care about
ACTION_STARTUP = 0
ACTION_LOCATE_OBJECT = 8
ACTION_EXAMINE_OBJECT = 23
ACTION_EXAMINE_NEXT = 24
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
ACTION_SET_DATE = 34
ACTION_FLUSH = 27
ACTION_FORMAT = 1020
ACTION_INHIBIT = 31
# The names of the opcodes above, for messages.
_PACKET_TYPE_NAMES = {
    ACTION_STARTUP: "ACTION_STARTUP",
    ACTION_LOCATE_OBJECT: "ACTION_LOCATE_OBJECT",
    ACTION_EXAMINE_OBJECT: "ACTION_EXAMINE_OBJECT",
    ACTION_EXAMINE_NEXT: "ACTION_EXAMINE_NEXT",
    ACTION_DISK_INFO: "ACTION_DISK_INFO",
    ACTION_READ: "ACTION_READ",
    ACTION_WRITE: "ACTION_WRITE",
    ACTION_SEEK: "ACTION_SEEK",
    ACTION_END: "ACTION_END",
    ACTION_SET_FILE_SIZE: "ACTION_SET_FILE_SIZE",
    ACTION_FREE_LOCK: "ACTION_FREE_LOCK",
    ACTION_FINDUPDATE: "ACTION_FINDUPDATE",
    ACTION_FINDINPUT: "ACTION_FINDINPUT",
    ACTION_FINDOUTPUT: "ACTION_FINDOUTPUT",
    ACTION_DELETE_OBJECT: "ACTION_DELETE_OBJECT",
    ACTION_RENAME_OBJECT: "ACTION_RENAME_OBJECT",
    ACTION_CREATE_DIR: "ACTION_CREATE_DIR",
    ACTION_SET_DATE: "ACTION_SET_DATE",
    ACTION_FLUSH: "ACTION_FLUSH",
    ACTION_FORMAT: "ACTION_FORMAT",
    ACTION_INHIBIT: "ACTION_INHIBIT",
}
OFFSET_BEGINNING = -1
OFFSET_CURRENT = 0
OFFSET_END = 1
SHARED_LOCK = -2
EXCLUSIVE_LOCK = -1

# The handler's stack. SFS and PFS3 are the hungriest of the handlers seen.
HANDLER_STACK_SIZE = 32 * 1024
# The handler's DOS port signals with SIGB_DOS, as FFS's fixed Wait mask
# (0x100) expects.
SIGB_DOS = 8

_TC_SIGRECVD = TaskStruct.sdef.find_field_def_by_name("tc_SigRecvd").offset


def _packet_type_name(pkt_type) -> str:
    """Name a dp_Type for messages, as "ACTION_FLUSH (27)" or "packet type 99"."""
    name = _PACKET_TYPE_NAMES.get(pkt_type)
    return f"packet type {pkt_type}" if name is None else f"{name} ({pkt_type})"


@dataclass
class HandlerLaunchState:
    process_addr: int
    port_addr: int  # the port packets go to; may change after startup
    reply_port_addr: int
    stack: Any
    stdpkt_addr: int  # the startup packet
    msg_addr: int
    proc: Any = None  # the HandlerProcess
    task: Any = None  # its scheduler task
    started: bool = False
    initialized: bool = False  # True after startup is complete
    startup_replied: bool = False  # The bridge polled the startup reply
    exited: bool = False  # The handler's task ended
    crashed: bool = False  # True if handler hit an unrecoverable error
    last_error: Any = None  # the error that ended it
    last_error_pc: int = 0  # PC at time of crash for diagnostics
    last_traceback: Any = None  # the Python traceback of an exception that ended it


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
        self.scheduler = vh.scheduler
        self.exec_impl = vh.slm.exec_impl
        self.port_mgr = self.exec_impl.port_mgr
        # ExecBase is stored at address 4 on Amiga systems
        self.exec_base_addr = self.mem.r32(4)
        self.boot = boot_info
        self.segment_addr = segment_addr
        self.state: Optional[HandlerLaunchState] = None
        # the thread the emulation runs on (emu_thread.py); the bridge sets
        # it, and every method below then runs there
        self._emu = None
        # Scratch packet buffers to avoid unbounded allocs (we serialize requests).
        self._stdpkt_ring = []
        self._stdpkt_sizes = []
        self._stdpkt_index = 0
        self._stdpkt_ring_size = 8
        # Packets send_packet() queued that have no reply yet: packet
        # address -> (dp_Type, args). poll_replies() removes each packet it
        # sees answered. Until then the handler may still read the packet
        # and the memory its args point to, so none of it is reused.
        self.pending: Dict[int, Tuple[int, Tuple[int, ...]]] = {}
        # StandardPackets taken out of the ring while their packet was
        # still out; each is freed once its packet is answered.
        self._stdpkt_retired = []
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
        self._mp_flags_offset = MsgPortStruct.sdef.find_field_def_by_name("mp_Flags").offset
        self._mp_sigbit_offset = MsgPortStruct.sdef.find_field_def_by_name("mp_SigBit").offset
        self._mp_sigtask_offset = MsgPortStruct.sdef.find_field_def_by_name("mp_SigTask").offset
        self._mp_msglist_offset = MsgPortStruct.sdef.find_field_def_by_name("mp_MsgList").offset
        # Debug flag (set by launch_with_startup); consistent with codebase self._debug pattern
        self._debug = False
        # dp_Type of the last packet queued, for warnings about its reply
        self.last_pkt_type = None

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
        sigbit: int,
        flags: int = MsgPortFlags.PA_SIGNAL,
    ):
        mp = MsgPortStruct(self.mem, port_addr)
        # zero first to clear garbage
        self.mem.w_block(port_addr, b"\x00" * MsgPortStruct.get_size())
        mp.node.type.val = NodeType.NT_MSGPORT
        mp.flags.val = flags
        mp.sig_bit.val = sigbit
        mp.sig_task.aptr = task_addr
        # Initialize mp_MsgList as a proper empty Amiga list
        # An empty list has: lh_Head -> &lh_Tail, lh_Tail = 0, lh_TailPred -> &lh_Head
        list_addr = port_addr + self._mp_msglist_offset
        lst = ListStruct(self.mem, list_addr)
        lh_head_addr = list_addr + self._lh_head_offset
        lh_tail_addr = list_addr + self._lh_tail_offset
        lst.head.aptr = lh_tail_addr
        lst.tail.aptr = 0
        lst.tail_pred.aptr = lh_head_addr
        lst.type.val = NodeType.NT_MESSAGE

    def _create_port(
        self,
        name: str,
        task_addr: int,
        sigbit: int,
        flags: int = MsgPortFlags.PA_SIGNAL,
    ) -> int:
        port_mem = self.alloc.alloc_memory(MsgPortStruct.get_size(), label=name)
        self._init_msgport(port_mem.addr, task_addr, sigbit=sigbit, flags=flags)
        if not self.port_mgr.has_port(port_mem.addr):
            self.port_mgr.register_port(port_mem.addr)
        return port_mem.addr

    def _create_process(self, name: str, start_pc: int) -> Tuple[HandlerProcess, int]:
        """The handler's process, with its task not yet added to the scheduler.

        Returns the process and the address of its pr_MsgPort.
        """
        handler = HandlerProcess(
            self.vh.slm.dos_ctx,
            self.vh.machine,
            self.alloc,
            name,
            stack_size=HANDLER_STACK_SIZE,
            start_pc=start_pc,
            start_regs={REG_A6: self.exec_base_addr},
        )
        stack = handler.get_stack()
        # clear stack to avoid garbage parameters being consumed by the handler
        self.mem.w_block(stack.get_lower(), b"\x00" * stack.get_size())
        proc = handler.proc
        task = proc.task
        task.node.pri.val = 0
        task.state.val = TaskState.TS_READY
        task.sp_reg.aptr = stack.get_initial_sp()
        task.sp_lower.aptr = stack.get_lower()
        task.sp_upper.aptr = stack.get_upper()
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
        # The DOS port, signalled with SIGB_DOS. The task's system signals
        # (0-15) count as allocated, as exec's new_task() leaves them.
        port_addr = proc.msg_port.addr
        self._init_msgport(port_addr, proc.addr, sigbit=SIGB_DOS)
        # register MsgPort with exec port manager so WaitPort/GetMsg see it
        if not self.port_mgr.has_port(port_addr):
            self.port_mgr.register_port(port_addr)
        return handler, port_addr

    def alloc_bstr(self, text: str, label: str = "bstr") -> Tuple[int, int]:
        """Return (addr, bptr) of a freshly allocated BSTR."""
        addr = self._write_bstr(text, label)
        return addr, addr >> 2

    def _build_std_packet(
        self, dest_port_addr: int, reply_port_addr: int, pkt_type: int, args
    ) -> Tuple[int, int]:
        """Allocate contiguous StandardPacket = Message + DosPacket, and queue it.

        A ring slot whose packet is still out (pending) gets a new
        StandardPacket, and the old one is freed once its reply has come,
        so that no packet is built where the handler may still read
        another, and a late reply's address names one packet only.
        """
        total = self._msg_size + self._pkt_size
        if not self._stdpkt_ring:
            self._stdpkt_ring = [None] * self._stdpkt_ring_size
            self._stdpkt_sizes = [0] * self._stdpkt_ring_size
        retired = []
        for old in self._stdpkt_retired:
            if old.addr + self._msg_size in self.pending:
                retired.append(old)
            else:
                self.alloc.free_memory(old)
        self._stdpkt_retired = retired
        idx = self._stdpkt_index
        self._stdpkt_index = (idx + 1) % self._stdpkt_ring_size
        sp_mem = self._stdpkt_ring[idx]
        if sp_mem is not None and sp_mem.addr + self._msg_size in self.pending:
            self._stdpkt_retired.append(sp_mem)
            sp_mem = self._stdpkt_ring[idx] = None
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
        self.last_pkt_type = pkt_type
        arg_addr = pkt_addr + self._dp_arg1_offset
        for val in args[:7]:
            self.mem.w32(arg_addr, val & 0xFFFFFFFF)
            arg_addr += 4
        # queue message to destination port - both Python queue and Amiga memory
        self.port_mgr.put_msg(dest_port_addr, sp_addr)
        # Also link the message into the port's mp_MsgList in memory
        # This is necessary because some handlers (like FFS) read mp_MsgList directly
        self._link_msg_to_port(dest_port_addr, sp_addr)
        # Signal the port's task like PutMsg() does, so that a Wait() on the
        # port's signal returns. A task not yet running finds the message
        # on the port when it first waits for it.
        self._signal_port(dest_port_addr)
        return pkt_addr, sp_addr

    def _link_msg_to_port(self, port_addr: int, msg_addr: int):
        """Link a message into a port's mp_MsgList in Amiga memory.

        This ensures handlers that read mp_MsgList directly (without calling GetMsg)
        can find the message.

        NOTE: We always reinitialize the list as a single-element list. GetMsg
        on a port the port manager knows unlinks the message it takes, so
        the list is empty again afterwards, and a stale entry cannot stay.
        """
        list_addr = port_addr + self._mp_msglist_offset
        lh_head_addr = list_addr + self._lh_head_offset
        lh_tail_addr = list_addr + self._lh_tail_offset
        self.mem.w32(list_addr + self._lh_head_offset, msg_addr & 0xFFFFFFFF)
        self.mem.w32(list_addr + self._lh_tail_offset, 0)
        self.mem.w32(list_addr + self._lh_tailpred_offset, msg_addr & 0xFFFFFFFF)
        self.mem.w8(list_addr + self._lh_type_offset, NodeType.NT_MESSAGE)
        self.mem.w32(msg_addr + self._msg_ln_succ_offset, lh_tail_addr & 0xFFFFFFFF)
        self.mem.w32(msg_addr + self._msg_ln_pred_offset, lh_head_addr & 0xFFFFFFFF)

    # ---- signals ----

    def _signal_port(self, port_addr: int):
        """Signal the task of a PA_SIGNAL port, as PutMsg() does."""
        try:
            flags = self.mem.r8(port_addr + self._mp_flags_offset)
            sigbit = self.mem.r8(port_addr + self._mp_sigbit_offset)
            task_addr = self.mem.r32(port_addr + self._mp_sigtask_offset)
        except Exception as e:
            if self._debug:
                print(f"[amifuse] WARNING: port 0x{port_addr:x} unreadable: {e}")
            return
        if flags != MsgPortFlags.PA_SIGNAL or not 0 <= sigbit < 32 or task_addr == 0:
            return
        self.signal_task(task_addr, 1 << sigbit)

    def signal_task(self, task_addr: int, signals: int) -> bool:
        """Set signals on the Amiga task at task_addr, as Signal() does.

        The scheduler task of that Amiga task gets them, which makes it
        ready if it waits for one of them; the next run() resumes it. A
        task the scheduler does not know (one that ended) gets them in its
        tc_SigRecvd only. Returns whether a scheduler task got them.
        """

        def is_task(sched_task):
            map_task = getattr(sched_task, "map_task", None)
            ami_task = map_task.get_ami_task() if map_task is not None else None
            return ami_task is not None and ami_task.addr == task_addr

        sched_task = self.scheduler.find_task_pred_func(is_task)
        if sched_task is None:
            try:
                recvd = self.mem.r32(task_addr + _TC_SIGRECVD)
                self.mem.w32(task_addr + _TC_SIGRECVD, recvd | signals)
            except Exception:
                pass
            return False
        sched_task.set_signal(signals, signals)
        return True

    # ---- public orchestration ----

    def launch_with_startup(self, extra_packets=None, debug=False) -> HandlerLaunchState:
        """Set the handler up and queue its startup packet.

        The handler's task is added to the scheduler and runs at the first
        run().
        """
        self._debug = debug
        # Build entry stub that jumps to segment start.
        # Handler's own startup code will set up registers and call WaitPort/GetMsg
        # to retrieve the startup packet from pr_MsgPort.
        stub_pc = build_entry_stub(self.mem, self.alloc, self.segment_addr)
        handler, port_addr = self._create_process("amifuse_handler", stub_pc)
        proc_addr = handler.proc.addr
        # The caller reply port is consumed by host-side polling, not by the
        # Amiga handler task. Keep it out of the handler's signal namespace.
        reply_port = self._create_port(
            "caller_port",
            0,
            sigbit=0xFF,
            flags=MsgPortFlags.PA_IGNORE,
        )
        # Fill DeviceNode dol_Task now that we have a port.
        dn = DeviceNodeStruct(self.mem, self.boot["dn_addr"])
        dn.dol_Task.aptr = port_addr
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

        task = handler.get_sched_task()
        self.scheduler.add_task(task)
        self.state = HandlerLaunchState(
            process_addr=proc_addr,
            port_addr=port_addr,
            reply_port_addr=reply_port,
            stack=handler.get_stack(),
            stdpkt_addr=startup_pkt,
            msg_addr=startup_msg,
            proc=handler,
            task=task,
        )
        return self.state

    def run(self, max_cycles: Optional[int] = None) -> RunResult:
        """Run the handler and its children until all wait, the budget is spent, or they end.

        Returns the scheduler's RunResult. A handler whose task ended is
        noted in the state: crashed if it ended with a machine error or an
        exception in a library call, exited otherwise. After that run()
        does nothing and returns DONE.
        """
        state = self.state
        if state.crashed or state.exited:
            return RunResult.DONE
        state.started = True
        task = state.task
        try:
            result = self.scheduler.run_until_idle(max_cycles)
        except greenlet.error:
            # Called from a thread other than the one that runs the tasks:
            # a caller's mistake, not the handler's end.
            raise
        except Exception as exc:
            # An exception in a library call unwound the task's greenlet:
            # the handler cannot run on.
            import traceback
            state.last_traceback = traceback.format_exc()
            self._note_crash(exc)
            if task.get_state() != SchedTaskState.TS_REMOVED:
                self.scheduler.terminate_task(task)
            return RunResult.DONE
        if task.get_state() == SchedTaskState.TS_REMOVED:
            state.exited = True
            error = task.get_error()
            if error is not None:
                self._note_crash(error)
            elif self._debug:
                print(f"[amifuse] handler exited with code {task.get_exit_code()}")
        return result

    def _note_crash(self, error):
        state = self.state
        pc = getattr(error, "pc", 0) or 0
        state.crashed = True
        state.exited = True
        state.last_error = error
        state.last_error_pc = pc
        print(
            f"\n[amifuse] FATAL: Handler crashed: {type(error).__name__}: {error}",
            file=sys.stderr,
        )
        if pc:
            print(f"[amifuse]   at PC=0x{pc:x}", file=sys.stderr)
        if self._debug and getattr(state, "last_traceback", None):
            print(state.last_traceback, file=sys.stderr)
        print("[amifuse]   Restart amifuse to recover.", file=sys.stderr)

    def handler_waiting(self) -> bool:
        """Whether the handler's task waits for a signal."""
        return self.state.task.get_state() == SchedTaskState.TS_WAIT

    def send_disk_info(self, state: HandlerLaunchState, info_buf_addr: int):
        # Arg1 = InfoData* (BPTR)
        return self.send_packet(state, ACTION_DISK_INFO, [info_buf_addr >> 2])

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
            self.pending.pop(pkt_addr, None)
            has_msg = pmgr.has_msg(port_addr)
        return results

    def send_packet(self, state: HandlerLaunchState, pkt_type: int, args):
        """Queue a packet to the handler and return the msg/pkt addresses.

        The packet stays in pending until poll_replies() sees its reply.
        """
        pkt_addr, msg_addr = self._build_std_packet(
            state.port_addr, state.reply_port_addr, pkt_type, args
        )
        self.pending[pkt_addr] = (
            pkt_type, tuple(val & 0xFFFFFFFF for val in args[:7])
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
        return self.send_packet(state, ACTION_LOCATE_OBJECT, [lock_bptr, name_bptr, mode])

    def send_examine(self, state: HandlerLaunchState, lock_bptr: int, fib_addr: int):
        """ACTION_EXAMINE_OBJECT"""
        return self.send_packet(state, ACTION_EXAMINE_OBJECT, [lock_bptr, fib_addr >> 2])

    def send_examine_next(self, state: HandlerLaunchState, lock_bptr: int, fib_addr: int):
        """ACTION_EXAMINE_NEXT"""
        return self.send_packet(state, ACTION_EXAMINE_NEXT, [lock_bptr, fib_addr >> 2])

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

    def send_set_date(
        self, state: HandlerLaunchState, lock_bptr: int, name_bptr: int, datestamp_addr: int
    ):
        """ACTION_SET_DATE: date the object name in directory lock_bptr.

        dp_Arg1 is unused; dp_Arg4 is the address (not a BPTR) of a
        struct DateStamp.
        """
        return self.send_packet(
            state, ACTION_SET_DATE, [0, lock_bptr, name_bptr, datestamp_addr])

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


# Every call into the launcher may run or wake a task, so all of them run
# on the emulation thread (see emu_thread.py).
route_methods_through_emu(HandlerLauncher)
