"""
ProcessManager for multi-process filesystem handler support.

This module manages multiple Amiga processes (parent handler + children)
for filesystems like SFS that spawn child processes via CreateNewProc().
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from amitools.vamos.libstructs import ProcessStruct, MsgPortFlags
from amitools.vamos.machine.regs import REG_D0, REG_A6

from .startup_runner import (
    _build_resume_frame,
    _clear_all_block_state,
    _has_blocked_state,
    _snapshot_block_state,
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
            new_entry_pc = info["entry_pc"]
            new_stack = info.get("stack")
            new_sp = new_stack.get_initial_sp()
            new_name = info.get("name", "child")
            new_port_addr = info.get("port_addr", 0)

            existing = self.processes.get(proc_addr)
            if existing is None:
                # Create ProcessState for this child
                state = ProcessState(
                    proc_addr=proc_addr,
                    entry_pc=new_entry_pc,
                    sp=new_sp,
                    name=new_name,
                    port_addr=new_port_addr,
                    is_child=True,
                    started=False,
                    stack=new_stack,
                )
                self.processes[proc_addr] = state
                self.ready_queue.append(state)
                new_children.append(state)
                continue

            # Some handlers, notably SFS during format/reopen, can recycle the
            # same Process address for a fresh child. Refresh the tracked state
            # so we do not resume the stale blocked child context.
            needs_refresh = (
                existing.exited
                or existing.stack is not new_stack
            )
            if needs_refresh:
                if existing.port_addr and self.exec_impl.port_mgr.has_port(
                    existing.port_addr
                ):
                    self.exec_impl.port_mgr.unregister_port(existing.port_addr)
                if new_port_addr and not self.exec_impl.port_mgr.has_port(
                    new_port_addr
                ):
                    self.exec_impl.port_mgr.register_port(new_port_addr)
                existing.entry_pc = new_entry_pc
                existing.sp = new_sp
                existing.name = new_name
                existing.port_addr = new_port_addr
                existing.blocked = False
                existing.wait_mask = 0
                existing.pending_signals = 0
                existing.is_child = True
                existing.started = False
                existing.exited = False
                existing.stack = new_stack
                existing.regs = None
                existing.block_state = None
                if existing not in self.ready_queue:
                    self.ready_queue.append(existing)
                new_children.append(existing)

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

        if not _has_blocked_state(child.block_state):
            child.blocked = False
            child.block_state = None
            return True

        resume = _build_resume_frame(
            child.block_state,
            default_port_addr=child.port_addr,
            mem=self.mem,
            port_mgr=self.exec_impl.port_mgr,
            compute_pending_signals=self._compute_pending_signals,
            clear_signals_from_task=self._clear_signals_from_task,
        )
        if resume is None:
            return False

        child.entry_pc = resume["pc"]
        child.sp = resume["sp"]
        if child.regs is None:
            child.regs = [0] * 16
        child.regs[REG_D0] = resume["d0"]
        _clear_all_block_state()
        child.blocked = False
        child.block_state = None
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
        # Parent Wait()/WaitPort() snapshots are managed by the caller around
        # child scheduling. Clear the global Exec/DOS block state before
        # running a child so it cannot inherit the parent's blocked frame.
        _clear_all_block_state()

        resumed = False
        if child.blocked:
            resumed = self._resume_child_if_ready(child)
            if not resumed:
                self._set_this_task(self.parent_addr)
                self.cpu.w_pc(saved_pc)
                self.cpu.w_sp(saved_sp)
                for i in range(16):
                    self.cpu.w_reg(i, saved_regs[i])
                return False

        first_run = not child.started

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
        seed_execbase = first_run or child.regs is None
        if resumed and child.regs is not None and child.regs[REG_A6] == 0:
            seed_execbase = True
        set_regs = {REG_A6: self.mem.r32(4)} if seed_execbase else None
        if seed_execbase:
            self.cpu.w_reg(REG_A6, self.mem.r32(4))
            if child.regs is None:
                child.regs = [0] * 16
            child.regs[REG_A6] = self.mem.r32(4)
        if not first_run:
            self.cpu.w_sp(sp)

        # Run child
        try:
            run_state = self.machine.run(
                pc=pc,
                sp=sp,
                set_regs=set_regs,
                max_cycles=max_cycles,
                cycles_per_run=max_cycles,
                name=f"child_{child.name}"
            )
            new_pc = self.cpu.r_pc()
            new_sp = self.cpu.r_sp()

            # Update child state
            child.regs = [self.cpu.r_reg(i) for i in range(16)]

            if run_state.done:
                child.exited = True
                from amitools.vamos.lib.DosLibrary import DosLibrary

                if child.port_addr and self.exec_impl.port_mgr.has_port(child.port_addr):
                    self.exec_impl.port_mgr.unregister_port(child.port_addr)
                info = DosLibrary._child_processes.get(child.proc_addr)
                if info is not None and info.get("stack") is child.stack:
                    DosLibrary._child_processes.pop(child.proc_addr, None)
                return False
            elif run_state.error:
                # Only treat errors as resumable blocks when Exec/DOS recorded
                # an actual Wait()/WaitPort() state. Other errors mean this
                # child is no longer safe to resume.
                blocked_state = _snapshot_block_state()
                blocked_sp = blocked_state.get("waitport_blocked_sp")
                if blocked_sp is None:
                    blocked_sp = blocked_state.get("wait_blocked_sp")
                if blocked_sp is None:
                    child.exited = True
                    from amitools.vamos.lib.DosLibrary import DosLibrary

                    if child.port_addr and self.exec_impl.port_mgr.has_port(
                        child.port_addr
                    ):
                        self.exec_impl.port_mgr.unregister_port(child.port_addr)
                    info = DosLibrary._child_processes.get(child.proc_addr)
                    if info is not None and info.get("stack") is child.stack:
                        DosLibrary._child_processes.pop(child.proc_addr, None)
                    _clear_all_block_state()
                    return False
                child.block_state = blocked_state
                _clear_all_block_state()
                child.blocked = True
                return False
            else:
                # Still running, just hit cycle limit
                child.entry_pc = new_pc
                child.sp = new_sp
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
        this_task_off = ExecLibraryStruct.sdef.find_field_def_by_name("ThisTask").offset
        self.mem.w32(exec_base + this_task_off, proc_addr)
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
