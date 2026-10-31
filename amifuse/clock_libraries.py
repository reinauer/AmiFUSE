"""dos.library and intuition.library whose clock reads tell local time.

amitools' DateStamp tells UTC, and its CurrentTime reads amitools' own
TimerDevice, not the one AmiFUSE swaps in, which counts from 1970 in UTC.
Both read the clock of amiga_time instead, as AmiFuseTimerDevice does.

What it relies on in amitools:
- A library's stub binds its methods when the library opens, so
  VamosHandlerRuntime registers these classes before then.
- DosLibrary keeps its process table and wait state on the class
  (DosLibrary._child_processes, DosLibrary._waitpkt_blocked), which the
  subclass shares and never rebinds.
"""

import time

from amitools.vamos.lib.DosLibrary import DosLibrary  # type: ignore
from amitools.vamos.lib.IntuitionLibrary import IntuitionLibrary  # type: ignore
from amitools.vamos.libstructs.dos import DateStampStruct  # type: ignore
from amitools.vamos.machine.regs import REG_D1  # type: ignore

from amifuse.amiga_time import sys_time, to_datestamp


class AmiFuseDosLibrary(DosLibrary):
    """dos.library whose DateStamp tells local time."""

    def DateStamp(self, ctx):
        ds_ptr = ctx.cpu.r_reg(REG_D1)
        ds = DateStampStruct(ctx.mem, ds_ptr)
        ds.ds_Days.val, ds.ds_Minute.val, ds.ds_Tick.val = to_datestamp(time.time())
        return ds_ptr


class AmiFuseIntuitionLibrary(IntuitionLibrary):
    """intuition.library whose CurrentTime tells the system time."""

    def CurrentTime(self, ctx, secs_ptr, micros_ptr):
        secs, micros = sys_time()
        ctx.mem.w32(secs_ptr, secs)
        ctx.mem.w32(micros_ptr, micros)
