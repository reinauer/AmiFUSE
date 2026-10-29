"""timer.device whose TR_ADDREQUEST completes after the requested delay.

amitools' TimerDevice leaves IOF_QUICK set on every request, so
ExecLibrary.SendIO replies at once and a handler that re-arms a timer on
every expiry never waits: FastFileSystem's 200 ms tick then runs thousands
of times during startup, and the write-back it drives never sees time pass.

AmiFuseTimerDevice holds each relative TR_ADDREQUEST until its deadline. The
bridge calls deliver() between emulator bursts, and delivery replies the
request the way SendIO's quick path does. Absolute units and the clock
arithmetic are amitools' behaviour.

It also corrects the system time. amitools counts it from the Unix epoch,
while AmigaOS, and dos.library's DateStamp in vamos, count from 1978-01-01,
so a handler stamping files with GetSysTime dated them eight years ahead.

OpenDevice writes a token into io_Unit, as timer.device writes its unit
pointer there, and the device knows its requests by that token. A copy of
an opened request (CopyMem, or a struct assignment) carries it, and is held
like the original.

amitools' DoIO and WaitIO do not wait for a request the device holds.
AmiFuseExecLibrary has them complete it at once instead, the behaviour
before requests were held, so that no reply comes later to a request the
handler takes for finished.

The table is touched only while the bridge lock is held: the device runs
inside emulator bursts, and the bridge and its timer thread take the lock.
Only the event crosses threads without it.

What it relies on in amitools:
- ExecLibrary.SendIO queues no reply for a request whose IOF_QUICK is
  clear after BeginIO.
- WaitIO takes a reply back out only on a port the port manager knows,
  and WaitPort parks only on such a port; on any other empty port it spins.
- CheckIO reads only IOF_QUICK.
- OpenDevice clears io_Unit before the open_dev hook and keeps what the
  hook writes there.
- LibImpl's setup_lib, open_dev and close_dev hooks run on OpenDevice and
  CloseDevice.
- lib_mgr.add_impl_cls accepts a factory, so one instance serves a runtime.
- A library's stub binds its methods when the library opens, so
  AmiFuseExecLibrary is registered before exec.library opens.
"""

import math
import threading
import time
from collections import Counter
from typing import Callable, Dict, Optional, Tuple

from amitools.vamos.lib.ExecLibrary import ExecLibrary  # type: ignore
from amitools.vamos.lib.TimerDevice import (  # type: ignore
    TR_ADDREQUEST,
    TR_GETSYSTIME,
    TimerDevice,
)
from amitools.vamos.libstructs.exec_ import (  # type: ignore
    IORequestStruct,
    MessageStruct,
    MsgPortStruct,
    NodeStruct,
    NodeType,
)
from amitools.vamos.log import log_timer  # type: ignore

UNIT_MICROHZ = 0
UNIT_VBLANK = 1
UNIT_ECLOCK = 2
# UNIT_WAITUNTIL (3) and UNIT_WAITECLOCK (4) take absolute times.
_RELATIVE_UNITS = (UNIT_MICROHZ, UNIT_VBLANK, UNIT_ECLOCK)

IOF_QUICK = 1
IOERR_ABORTED = -2

_IO_MSG_OFF = IORequestStruct.sdef.find_field_def_by_name("io_Message").offset
_LN_SUCC = _IO_MSG_OFF + NodeStruct.sdef.find_field_def_by_name("ln_Succ").offset
_LN_PRED = _IO_MSG_OFF + NodeStruct.sdef.find_field_def_by_name("ln_Pred").offset
_LN_TYPE = _IO_MSG_OFF + NodeStruct.sdef.find_field_def_by_name("ln_Type").offset
_MN_REPLYPORT = _IO_MSG_OFF + MessageStruct.sdef.find_field_def_by_name("mn_ReplyPort").offset
_IO_DEVICE = IORequestStruct.sdef.find_field_def_by_name("io_Device").offset
_IO_UNIT = IORequestStruct.sdef.find_field_def_by_name("io_Unit").offset
_IO_COMMAND = IORequestStruct.sdef.find_field_def_by_name("io_Command").offset
_IO_FLAGS = IORequestStruct.sdef.find_field_def_by_name("io_Flags").offset
_IO_ERROR = IORequestStruct.sdef.find_field_def_by_name("io_Error").offset
# struct timerequest is the 32-byte struct IORequest, which ends with
# io_Error, then tv_secs and tv_micro (for UNIT_ECLOCK, ev_hi and ev_lo).
# amitools' IORequestStruct is the 48-byte IOStdReq, so its
# TimeRequestStruct places tr_time 16 bytes late.
_TR_SECS = _IO_ERROR + 1
assert _TR_SECS == 32
_TR_MICRO = _TR_SECS + 4
_MP_SIGBIT = MsgPortStruct.sdef.find_field_def_by_name("mp_SigBit").offset
_MP_SIGTASK = MsgPortStruct.sdef.find_field_def_by_name("mp_SigTask").offset
_MP_MSGLIST = MsgPortStruct.sdef.find_field_def_by_name("mp_MsgList").offset
# Unit tokens are odd, so that no aligned pointer left in io_Unit is taken
# for one, and have the top bit set, so that no small number is.
_TOKEN_FIRST = 0x80000001
_TOKEN_LAST = 0xFFFFFFFF


class AmiFuseTimerDevice(TimerDevice):
    """timer.device that replies to TR_ADDREQUEST when its delay has passed.

    One instance serves a runtime: VamosHandlerRuntime registers a factory
    that returns it, so a request survives the device being expunged and
    opened again.
    """

    # One VBLANK tick. It bounds how often a handler that re-arms with no
    # delay can wake.
    MIN_DELAY = 0.02
    # Seconds from the Unix epoch to the Amiga epoch, 1978-01-01: the 2922
    # days that vamos' DateStamp subtracts (amitools.vamos.lib.dos.AmiTime).
    AMIGA_EPOCH_OFFSET = 2922 * 24 * 60 * 60

    def __init__(self, exec_impl, clock: Callable[[], float] = time.monotonic):
        self._exec = exec_impl
        self.clock = clock
        # io address -> (deadline, io_Device when armed)
        self._pending: Dict[int, Tuple[float, int]] = {}
        # io_Unit token -> unit, from OpenDevice until CloseDevice
        self._tokens: Dict[int, int] = {}
        self._last_token = _TOKEN_LAST
        self._bases = set()
        self._logged = set()
        # Set when a request arms earlier than every pending one, so that a
        # thread waiting for the earliest deadline looks again.
        self.event = threading.Event()
        # Requests replied at their deadline, for tests and --debug.
        self.deliveries = 0
        # Requests that DoIO or WaitIO completed before their deadline, per
        # call, for tests.
        self.completed_early: Counter = Counter()

    @classmethod
    def get_sys_time(cls):
        """The system time as (secs, micros) since the Amiga epoch, in UTC.

        UTC, as vamos' DateStamp uses, so that both clocks agree. GetSysTime
        and TR_GETSYSTIME both read it. Computed here rather than from
        amitools' Unix-epoch value, so a fix there cannot apply twice.
        """
        now = time.time()
        secs = int(now)
        return secs - cls.AMIGA_EPOCH_OFFSET, int((now - secs) * cls.MICRO_HZ)

    # ---- library hooks ----

    def setup_lib(self, ctx, base_addr):
        self._bases.add(base_addr)

    def open_dev(self, ctx, io_request, unit, flags):
        token = self._new_token()
        self._tokens[token] = unit
        ctx.mem.w32(io_request + _IO_UNIT, token)
        return 0

    def close_dev(self, ctx, io_request):
        # Copies of the request carry the same token, and no longer count
        # as opened here either.
        self._tokens.pop(ctx.mem.r32(io_request + _IO_UNIT), None)
        self._pending.pop(io_request, None)

    def BeginIO(self, ctx, io_request):
        mem = ctx.mem
        cmd = mem.r16(io_request + _IO_COMMAND)
        if cmd == TR_GETSYSTIME:
            # In tr_time, where amitools' TimeRequestStruct does not put it:
            # 16 bytes later it misses the answer and writes past the request.
            secs, micros = self.get_sys_time()
            mem.w8(io_request + _IO_ERROR, 0)
            mem.w32(io_request + _TR_SECS, secs)
            mem.w32(io_request + _TR_MICRO, micros)
            return 0
        if cmd != TR_ADDREQUEST:
            return super().BeginIO(ctx, io_request)
        unit = self._unit(mem, io_request)
        if unit not in _RELATIVE_UNITS:
            # Absolute units, and requests not opened here, complete at once
            # as in amitools.
            self._log_once(("unit", unit), "TR_ADDREQUEST on unit %s completes at once", unit)
            return super().BeginIO(ctx, io_request)
        secs = mem.r32(io_request + _TR_SECS)
        micro = mem.r32(io_request + _TR_MICRO)
        if unit == UNIT_ECLOCK:
            delay = ((secs << 32) | micro) / self.ECLOCK_HZ
        else:
            delay = secs + micro / self.MICRO_HZ
        mem.w8(io_request + _IO_ERROR, 0)
        mem.w8(io_request + _IO_FLAGS, mem.r8(io_request + _IO_FLAGS) & ~IOF_QUICK)
        mem.w8(io_request + _LN_TYPE, NodeType.NT_MESSAGE)
        # Registered now, as SendIO's quick path did: WaitPort parks only on
        # a port the port manager knows, and on any other empty port spins.
        self._register_port(mem.r32(io_request + _MN_REPLYPORT))
        deadline = self.clock() + max(delay, self.MIN_DELAY)
        earliest = self.next_deadline()
        # Sending a request that is still pending replaces it.
        self._pending[io_request] = (deadline, mem.r32(io_request + _IO_DEVICE))
        if deadline < earliest:
            self.event.set()
        log_timer.info("BeginIO: TR_ADDREQUEST unit %d delay %.6f s", unit, delay)
        return 0

    def AbortIO(self, ctx, io_request):
        if self._pending.pop(io_request, None) is None:
            return 0
        mem = ctx.mem
        mem.w8s(io_request + _IO_ERROR, IOERR_ABORTED)
        self._reply(mem, io_request)
        return 0

    def complete_early(self, ctx, io_request, call) -> bool:
        """Complete a pending request now, for DoIO or WaitIO; see AmiFuseExecLibrary.

        The reply goes to its port without a signal, as Exec's wait would
        have taken the signal, and WaitIO then takes it back out. Warns once
        per call. Returns whether the request was pending.
        """
        if self._pending.pop(io_request, None) is None:
            return False
        if call not in self.completed_early:
            log_timer.warning(
                "%s on a pending TR_ADDREQUEST: vamos cannot wait for it, so "
                "it completes at once, before its delay", call)
        self.completed_early[call] += 1
        mem = ctx.mem
        mem.w8(io_request + _IO_ERROR, 0)
        self._reply(mem, io_request, signal=False)
        return True

    # ---- bridge interface ----

    def next_deadline(self) -> float:
        """The earliest deadline on the device clock, or inf if none."""
        if not self._pending:
            return math.inf
        return min(deadline for deadline, _ in self._pending.values())

    def deliver(self, fast_forward: bool = False) -> Dict[int, int]:
        """Reply to every request whose deadline has passed.

        With fast_forward, every pending request is due. Returns the signal
        bits sent, per task address.
        """
        if not self._pending:
            return {}
        mem = self._exec.mem
        now = self.clock()
        due = [
            (deadline, io, device)
            for io, (deadline, device) in self._pending.items()
            if fast_forward or deadline <= now
        ]
        signals: Dict[int, int] = {}
        for _, io, device in sorted(due):
            del self._pending[io]
            # Each request on its own, so that one the handler broke cannot
            # cost the others their replies.
            try:
                # A request freed or reused since it was sent is not replied.
                if (
                    mem.r32(io + _IO_DEVICE) != device
                    or mem.r8(io + _LN_TYPE) != NodeType.NT_MESSAGE
                ):
                    continue
                mem.w8(io + _IO_ERROR, 0)
                task, bits = self._reply(mem, io)
            except Exception as exc:
                log_timer.warning("deliver: request 0x%06x not replied: %s", io, exc)
                continue
            self.deliveries += 1
            if bits:
                signals[task] = signals.get(task, 0) | bits
        return signals

    def delays(self, msg_addr: int) -> bool:
        """Whether msg_addr is a request this device holds until its deadline.

        That is a TR_ADDREQUEST on a relative unit, opened here or copied
        from a request opened here, whose reply comes only when due.
        Requests that complete at once (absolute units, TR_GETSYSTIME, a
        request never opened here) are not.
        """
        mem = self._exec.mem
        try:
            return (
                mem.r32(msg_addr + _IO_DEVICE) in self._bases
                and mem.r16(msg_addr + _IO_COMMAND) == TR_ADDREQUEST
                and self._unit(mem, msg_addr) in _RELATIVE_UNITS
            )
        except Exception:
            return False

    # ---- helpers ----

    def _unit(self, mem, io) -> Optional[int]:
        """The unit io was opened on, or None if not opened here."""
        return self._tokens.get(mem.r32(io + _IO_UNIT))

    def _new_token(self) -> int:
        token = self._last_token
        while True:
            token = token + 2 if token < _TOKEN_LAST else _TOKEN_FIRST
            if token not in self._tokens:
                self._last_token = token
                return token

    def _reply(self, mem, io, signal: bool = True) -> Tuple[int, int]:
        """Reply io as ExecLibrary.SendIO's quick path does.

        Registers the reply port with the port manager, so that WaitIO,
        which acts only on registered ports, removes the reply again.
        Like SendIO, it skips the linking and the signal where the port
        cannot be read or written. Returns (task, signal bits) for the task
        it signalled; with signal False it signals none.
        """
        mem.w8(io + _IO_FLAGS, mem.r8(io + _IO_FLAGS) | IOF_QUICK)
        mem.w8(io + _LN_TYPE, NodeType.NT_REPLYMSG)
        reply_port = mem.r32(io + _MN_REPLYPORT)
        if reply_port == 0:
            return 0, 0
        self._register_port(reply_port)
        self._exec.port_mgr.put_msg(reply_port, io)
        try:
            list_addr = reply_port + _MP_MSGLIST
            old_tailpred = mem.r32(list_addr + 8)
            mem.w32(io + _LN_SUCC, list_addr + 4)
            mem.w32(io + _LN_PRED, old_tailpred)
            mem.w32(old_tailpred, io)
            mem.w32(list_addr + 8, io)
        except Exception:
            pass
        if not signal:
            return 0, 0
        try:
            sigbit = mem.r8(reply_port + _MP_SIGBIT)
            task = mem.r32(reply_port + _MP_SIGTASK)
        except Exception:
            return 0, 0
        if not (0 <= sigbit < 32) or task == 0:
            return 0, 0
        self._exec.signal_func.signal(task, 1 << sigbit)
        return task, 1 << sigbit

    def _register_port(self, port):
        port_mgr = self._exec.port_mgr
        if port and not port_mgr.has_port(port):
            port_mgr.register_port(port)

    def _log_once(self, key, fmt, *args):
        if key not in self._logged:
            self._logged.add(key)
            log_timer.info(fmt, *args)


class AmiFuseExecLibrary(ExecLibrary):
    """ExecLibrary whose DoIO and WaitIO return with no request pending.

    Exec's DoIO and WaitIO wait until the device replies; amitools' return
    at once. While every request completed in BeginIO that did not matter.
    A request AmiFuseTimerDevice holds is replied later, to a handler that
    takes it for finished: after DoIO the reply is a stray message on its
    port, and after WaitIO the handler may reuse or free the request while
    it is still queued. vamos cannot block in a library call, so a device
    that holds requests completes one at once here (complete_early), and
    WaitIO takes the reply back off the port, as after a wait.
    """

    def DoIO(self, ctx, io_request):
        result = super().DoIO(ctx, io_request)
        if self._complete_early(ctx, io_request, "DoIO"):
            result = super().WaitIO(ctx, io_request)
        return result

    def WaitIO(self, ctx, io_request):
        self._complete_early(ctx, io_request, "WaitIO")
        return super().WaitIO(ctx, io_request)

    def _complete_early(self, ctx, io_request, call) -> bool:
        vlib = self.lib_mgr.get_vlib_by_addr(ctx.mem.r32(io_request + self._io_device_off))
        impl = vlib.get_impl() if vlib is not None else None
        complete = getattr(impl, "complete_early", None)
        return complete is not None and complete(ctx, io_request, call)
