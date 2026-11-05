"""dos.library and intuition.library whose clock reads tell local time.

amitools' DateStamp tells UTC, and its CurrentTime reads amitools' own
TimerDevice, not the one AmiFUSE swaps in, which counts from 1970 in UTC.
Both read the clock of amiga_time instead, as AmiFuseTimerDevice does.

The dos.library also waits in WaitPkt(): amitools' fails on an empty
process port, where a handler's main loop waits for its next packet. And
its Delay() lets the other tasks run: amitools' sleeps on the host inside
the call, which holds up every task, and a handler that polls with Delay()
for a child it started, as SmartFilesystem does, would wait for the slice
to end before the child could run. Here the task waits on SIGF_SINGLE, as
on AmigaOS, and AmiFuseTimerDevice signals it when the delay has passed
(delay_timer, set by VamosHandlerRuntime).

What it relies on in amitools:
- A library's stub binds its methods when the library opens, so
  VamosHandlerRuntime registers these classes before then.
- WaitPkt() takes the packet from the pr_MsgPort of ExecBase->ThisTask,
  a port the port manager knows, and fails when it is empty.
- CreateNewProc() makes the child's process through
  create_child_process(ctx, name, **kw_args) (amitools 0.9.0 or later).
- exec_lib.signal_func.wait() suspends the current task until one of the
  signals is set, and takes the signals it returns.
"""

import time

from amitools.vamos.lib.DosLibrary import DosLibrary  # type: ignore
from amitools.vamos.lib.IntuitionLibrary import IntuitionLibrary  # type: ignore
from amitools.vamos.libstructs.dos import DateStampStruct, ProcessStruct  # type: ignore
from amitools.vamos.libstructs.exec_ import MsgPortFlags, MsgPortStruct  # type: ignore
from amitools.vamos.machine.regs import REG_D1  # type: ignore

from amifuse.amiga_time import sys_time, to_datestamp
from amifuse.handler_process import HandlerProcess
from amifuse.timer_device import SIGF_SINGLE


class AmiFuseDosLibrary(DosLibrary):
    """dos.library whose DateStamp tells local time, whose WaitPkt and Delay
    wait as tasks, and whose CreateNewProc() children are HandlerProcesses."""

    # The AmiFuseTimerDevice that ends a Delay(); without one Delay() sleeps
    # as amitools' does.
    delay_timer = None

    def Delay(self, ctx):
        ticks = ctx.cpu.r_reg(REG_D1)
        timer = self.delay_timer
        task = ctx.exec_lib.exec_lib.this_task.aptr
        if timer is None or ticks <= 0 or task == 0:
            return super().Delay(ctx)
        # one tick is 1/50 s
        timer.add_delay(task, timer.clock() + ticks / 50.0)
        ctx.exec_lib.signal_func.wait(SIGF_SINGLE)
        return 0

    def create_child_process(self, ctx, name, **kw_args):
        """A child of the handler serves DOS calls like the handler itself."""
        return HandlerProcess(ctx, ctx.machine, ctx.alloc, name, **kw_args)

    def DateStamp(self, ctx):
        ds_ptr = ctx.cpu.r_reg(REG_D1)
        ds = DateStampStruct(ctx.mem, ds_ptr)
        ds.ds_Days.val, ds.ds_Minute.val, ds.ds_Tick.val = to_datestamp(time.time())
        return ds_ptr

    def WaitPkt(self, ctx):
        """Wait for a packet on the process port, then take it, as DOS does."""
        proc_addr = ctx.exec_lib.exec_lib.this_task.aptr
        if proc_addr != 0:
            port_addr = ProcessStruct(ctx.mem, proc_addr).pr_MsgPort.addr
            port_mgr = ctx.exec_lib.port_mgr
            if port_mgr.has_port(port_addr):
                port = MsgPortStruct(ctx.mem, port_addr)
                while not port_mgr.has_msg(port_addr):
                    sig_bit = port.mp_SigBit.val
                    if port.mp_Flags.val != MsgPortFlags.PA_SIGNAL or not 0 <= sig_bit < 32:
                        break
                    ctx.exec_lib.signal_func.wait(1 << sig_bit)
        return super().WaitPkt(ctx)


class AmiFuseIntuitionLibrary(IntuitionLibrary):
    """intuition.library whose CurrentTime tells the system time."""

    def CurrentTime(self, ctx, secs_ptr, micros_ptr):
        secs, micros = sys_time()
        ctx.mem.w32(secs_ptr, secs)
        ctx.mem.w32(micros_ptr, micros)
