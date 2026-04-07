"""
ProcessManager for multi-process filesystem handler support.

This module manages multiple Amiga processes (parent handler + children)
for filesystems like SFS that spawn child processes via CreateNewProc().
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from amitools.vamos.astructs import AccessStruct
from amitools.vamos.libstructs import ProcessStruct, MsgPortFlags
from amitools.vamos.machine.regs import REG_D0, REG_A6
from amitools.vamos.libstructs.dos import MessageStruct

from .startup_runner import (
    _clear_all_block_state,
    _restore_block_state,
    _snapshot_block_state,
    _unlink_msg_from_m68k_list,
)


@dataclass
class ProcessState:
    """Track execution state for one Amiga process."""
    proc_addr: int              # Amiga Process structure address
    entry_pc: int               # Entry point / current PC
    sp: int                     # Current stack pointer
    name: str = ""              # Process name
    port_addr: int = 0          # Process's pr_MsgPort address
    blocked: bool = False       # True if waiting on signals
    wait_mask: int = 0          # Signal mask being waited on
    pending_signals: int = 0    # Signals received while blocked
    is_child: bool = False      # True if spawned by CreateNewProc
    started: bool = False       # True if process has started executing
    exited: bool = False        # True if process has exited
    stack: Any = None           # Stack object (for cleanup)
    regs: Optional[List[int]] = None  # Saved D0-D7/A0-A7 register state
    block_state: Optional[Dict[str, Any]] = None


class ProcessManager:
    """Manage multiple Amiga processes for amifuse.

    This class tracks parent and child processes created via CreateNewProc(),
    allowing amifuse to execute multiple processes in a round-robin fashion.
    """

    def __init__(self, vh, machine, exec_impl, parent_proc_addr: int):
        """Initialize ProcessManager.

        Args:
            vh: VamosHandlerRuntime instance
            machine: m68k Machine instance
            exec_impl: ExecLibrary implementation
            parent_proc_addr: Address of parent handler's Process structure
        """
        self.vh = vh
        self.machine = machine
        self.exec_impl = exec_impl
        self.mem = machine.get_mem()
        self.cpu = machine.get_cpu()

        # Process tracking
        self.processes: Dict[int, ProcessState] = {}
        self.parent_addr = parent_proc_addr
        self.ready_queue: List[ProcessState] = []

        # Register parent process
        parent_state = ProcessState(
            proc_addr=parent_proc_addr,
            entry_pc=0,  # Set by caller
            sp=0,        # Set by caller
            name="parent_handler",
            is_child=False,
            started=True,
        )
        self.processes[parent_proc_addr] = parent_state

    def check_for_new_children(self) -> List[ProcessState]:
        """Check DosLibrary._child_processes for newly created children.

        Returns list of new ProcessState objects that were added.
        """
        from amitools.vamos.lib.DosLibrary import DosLibrary

        new_children = []
        for proc_addr, info in list(DosLibrary._child_processes.items()):
            if proc_addr not in self.processes:
                # Create ProcessState for this child
                state = ProcessState(
                    proc_addr=proc_addr,
                    entry_pc=info["entry_pc"],
                    sp=info["stack"].get_initial_sp(),
                    name=info.get("name", "child"),
                    port_addr=info.get("port_addr", 0),
                    is_child=True,
                    started=False,
                    stack=info.get("stack"),
                )
                self.processes[proc_addr] = state
                self.ready_queue.append(state)
                new_children.append(state)

        return new_children

    def get_ready_children(self) -> List[ProcessState]:
        """Get list of child processes ready to execute."""
        return [p for p in self.ready_queue if not p.exited]

    def has_unsettled_children(self) -> bool:
        """Return True while any child still needs startup work.

        A child is considered settled once it has either exited or blocked in
        its steady-state Wait()/WaitPort() loop.  Startup may stop once the
        parent has replied and all children have reached one of those states.
        """
        self.check_for_new_children()
        for proc in self.processes.values():
            if not proc.is_child or proc.exited:
                continue
            if not proc.started or not proc.blocked:
                return True
        return False

    def _compute_pending_signals(self, mask: int = 0xFFFFFFFF) -> int:
        """Compute pending signals for the current ThisTask."""
        from amitools.vamos.libstructs.exec_ import (
            ExecLibraryStruct,
            MsgPortStruct,
            TaskStruct,
        )

        pending = 0
        this_task = 0
        try:
            exec_base = self.mem.r32(4)
            if exec_base != 0:
                this_task_off = ExecLibraryStruct.sdef.find_field_def_by_name(
                    "ThisTask"
                ).offset
                this_task = self.mem.r32(exec_base + this_task_off)
                if this_task != 0:
                    sigrecvd_off = TaskStruct.sdef.find_field_def_by_name(
                        "tc_SigRecvd"
                    ).offset
                    pending = self.mem.r32(this_task + sigrecvd_off)
        except Exception:
            pass

        for port_addr, port in self.exec_impl.port_mgr.ports.items():
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
                        + MsgPortStruct.sdef.find_field_def_by_name(
                            "mp_SigTask"
                        ).offset
                    )
                    if this_task != 0 and sig_task != this_task:
                        continue
                    sigbit = self.mem.r8(
                        port_addr
                        + MsgPortStruct.sdef.find_field_def_by_name("mp_SigBit").offset
                    )
                    if 0 <= sigbit < 32:
                        pending |= 1 << sigbit
            except Exception:
                continue
        return pending & mask

    def _clear_signals_from_task(self, signals: int):
        """Clear signals from the current ThisTask after a Wait() resume."""
        from amitools.vamos.libstructs.exec_ import ExecLibraryStruct, TaskStruct

        try:
            exec_base = self.mem.r32(4)
            if exec_base != 0:
                this_task_off = ExecLibraryStruct.sdef.find_field_def_by_name(
                    "ThisTask"
                ).offset
                this_task = self.mem.r32(exec_base + this_task_off)
                if this_task != 0:
                    sigrecvd_off = TaskStruct.sdef.find_field_def_by_name(
                        "tc_SigRecvd"
                    ).offset
                    sigrecvd = self.mem.r32(this_task + sigrecvd_off)
                    self.mem.w32(this_task + sigrecvd_off, sigrecvd & ~signals)
        except Exception:
            pass

    def _resume_child_if_ready(self, child: ProcessState) -> bool:
        """Resume a blocked child when its wake condition is satisfied."""
        if not child.blocked or not child.block_state:
            return not child.blocked

        _restore_block_state(child.block_state)
        waitport_sp = child.block_state.get("waitport_blocked_sp")
        wait_sp = child.block_state.get("wait_blocked_sp")
        waitport_ret = child.block_state.get("waitport_blocked_ret")
        wait_ret = child.block_state.get("wait_blocked_ret")
        wait_mask = child.block_state.get("wait_blocked_mask")
        waitpkt_blocked = child.block_state.get("waitpkt_blocked", False)

        blocked_sp = waitport_sp if waitport_sp is not None else wait_sp
        if blocked_sp is None:
            child.blocked = False
            return True

        if wait_sp is not None and wait_mask is not None:
            pending = self._compute_pending_signals(wait_mask)
            if pending == 0:
                return False
            all_pending = self._compute_pending_signals(0xFFFFFFFF)
            blocked_ret = wait_ret
            d0_val = all_pending
            self._clear_signals_from_task(all_pending)
        else:
            waitport_port = child.block_state.get("waitport_blocked_port") or child.port_addr
            if not waitport_port or not self.exec_impl.port_mgr.has_msg(waitport_port):
                return False
            blocked_ret = waitport_ret
            msg_addr = self.exec_impl.port_mgr.get_msg(waitport_port)
            if msg_addr:
                _unlink_msg_from_m68k_list(self.mem, msg_addr)
            if waitpkt_blocked and msg_addr:
                msg = AccessStruct(self.mem, MessageStruct, msg_addr)
                pkt_addr = msg.r_s("mn_Node.ln_Name")
                d0_val = pkt_addr if pkt_addr else 0
            else:
                d0_val = msg_addr if msg_addr else 0

        try:
            ret_addr = blocked_ret if blocked_ret is not None else self.mem.r32(blocked_sp)
        except Exception:
            ret_addr = 0
        if ret_addr == 0:
            return False

        child.entry_pc = ret_addr
        child.sp = blocked_sp + 4
        if child.regs is None:
            child.regs = [0] * 16
        child.regs[REG_D0] = d0_val
        _clear_all_block_state()
        child.blocked = False
        return True

    def run_child_burst(self, child: ProcessState, max_cycles: int = 50000) -> bool:
        """Run a child process for a burst of cycles.

        Args:
            child: ProcessState for the child to run
            max_cycles: Maximum cycles to execute

        Returns:
            True if child is still running, False if exited/blocked
        """
        if child.exited:
            return False

        # Save full current CPU state (all 16 registers)
        saved_pc = self.cpu.r_pc()
        saved_sp = self.cpu.r_sp()
        saved_regs = [self.cpu.r_reg(i) for i in range(16)]  # D0-D7, A0-A7

        # Switch ThisTask to child process before checking resume conditions.
        self._set_this_task(child.proc_addr)

        # Capture whether child is resuming from a block (equivalent of parent's `resumed`)
        was_blocked = child.blocked

        if child.blocked and not self._resume_child_if_ready(child):
            self._set_this_task(self.parent_addr)
            self.cpu.w_pc(saved_pc)
            self.cpu.w_sp(saved_sp)
            for i in range(16):
                self.cpu.w_reg(i, saved_regs[i])
            return False

        # Set up child's context
        if not child.started:
            # First run - set entry point and initial SP
            pc = child.entry_pc
            sp = child.sp
            child.started = True
        else:
            # Resume from where we left off
            pc = child.entry_pc  # Updated after each burst
            sp = child.sp

        if child.regs is not None:
            for i in range(16):
                self.cpu.w_reg(i, child.regs[i])
        seed_execbase = child.regs is None
        if was_blocked and child.regs is not None and child.regs[REG_A6] == 0:
            seed_execbase = True
        if seed_execbase:
            execbase = self.mem.r32(4)
            self.cpu.w_reg(REG_A6, execbase)
            # Keep saved regs consistent so next burst restores the seeded value
            if child.regs is not None:
                child.regs[REG_A6] = execbase

        # Run child
        try:
            run_state = self.machine.run(
                pc=pc,
                sp=sp,
                max_cycles=max_cycles,
                name=f"child_{child.name}"
            )

            # Update child state
            child.entry_pc = run_state.pc
            child.sp = run_state.sp
            child.regs = [self.cpu.r_reg(i) for i in range(16)]

            if run_state.done:
                child.exited = True
                return False
            elif run_state.error:
                # Child blocked (WaitPort/Wait) - preserve its block state.
                child.block_state = _snapshot_block_state()
                _clear_all_block_state()
                child.blocked = True
                return False
            else:
                # Still running, just hit cycle limit
                child.blocked = False
                return True

        finally:
            # Restore full parent context
            self._set_this_task(self.parent_addr)
            self.cpu.w_pc(saved_pc)
            self.cpu.w_sp(saved_sp)
            for i in range(16):
                self.cpu.w_reg(i, saved_regs[i])

    def _set_this_task(self, proc_addr: int):
        """Set ExecBase.ThisTask to the given process."""
        from amitools.vamos.libstructs import ExecLibraryStruct
        exec_base = self.exec_impl.exec_lib.get_addr()
        exec_struct = AccessStruct(self.mem, ExecLibraryStruct, exec_base)
        exec_struct.w_s("ThisTask", proc_addr)
        # Also update exec_impl's internal pointer
        self.exec_impl.exec_lib.this_task.aptr = proc_addr

    def run_all_ready_children(self, cycles_per_child: int = 50000) -> int:
        """Run all ready child processes for one burst each.

        Returns number of children that ran.
        """
        # Check for newly created children first
        self.check_for_new_children()

        ran = 0
        for child in self.get_ready_children():
            self.run_child_burst(child, cycles_per_child)
            ran += 1

        return ran
