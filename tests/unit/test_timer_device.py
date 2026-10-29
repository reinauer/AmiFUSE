"""Unit tests for amifuse.timer_device and the bridge code that delivers it.

AmiFuseTimerDevice holds a relative TR_ADDREQUEST until its deadline, and
the bridge replies to it between emulator bursts. These tests drive the
device through amitools' own ExecLibrary.SendIO, AbortIO and WaitIO, over a
fake memory and a fake clock, so the reply lands where a handler looks for
it: in the port manager's queue and in the port's m68k message list.
"""

import threading
import time
from types import SimpleNamespace

import pytest

BASE = 0x8000          # timer.device's library base
IO = 0x1000            # a struct timerequest
IO2 = 0x1100
PORT = 0x2000          # the handler's timer reply port
TASK = 0x3000          # the task the port signals
SIGBIT = 4

TR_ADDREQUEST = 9
TR_GETSYSTIME = 11
UNIT_MICROHZ, UNIT_VBLANK, UNIT_ECLOCK, UNIT_WAITUNTIL = 0, 1, 2, 3
NT_MESSAGE, NT_REPLYMSG = 5, 7
_TC_SIGRECVD = 26      # struct Task's tc_SigRecvd


class FakeMem:
    """Big-endian memory that rejects out-of-range values and addresses, as machine68k does."""

    _BITS = {0: 8, 1: 16, 2: 32}

    def __init__(self, size=0x10000):
        self.data = bytearray(size)

    def _check(self, addr, n):
        if not 0 <= addr <= len(self.data) - n:
            raise MemoryError(addr)

    def read(self, width, addr):
        n = self._BITS[width] // 8
        self._check(addr, n)
        return int.from_bytes(self.data[addr:addr + n], "big")

    def reads(self, width, addr):
        bits = self._BITS[width]
        val = self.read(width, addr)
        return val - (1 << bits) if val >> (bits - 1) else val

    def write(self, width, addr, val):
        bits = self._BITS[width]
        if not 0 <= val < (1 << bits):
            raise OverflowError(f"can't convert {val} to uint{bits}")
        self._check(addr, bits // 8)
        self.data[addr:addr + bits // 8] = val.to_bytes(bits // 8, "big")

    def writes(self, width, addr, val):
        bits = self._BITS[width]
        if not -(1 << (bits - 1)) <= val < (1 << (bits - 1)):
            raise OverflowError(f"can't convert {val} to int{bits}")
        self.write(width, addr, val & ((1 << bits) - 1))

    def r8(self, addr): return self.read(0, addr)
    def r8s(self, addr): return self.reads(0, addr)
    def r16(self, addr): return self.read(1, addr)
    def r32(self, addr): return self.read(2, addr)
    def r32s(self, addr): return self.reads(2, addr)
    def w8(self, addr, val): self.write(0, addr, val)
    def w8s(self, addr, val): self.writes(0, addr, val)
    def w16(self, addr, val): self.write(1, addr, val)
    def w32(self, addr, val): self.write(2, addr, val)


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Signals:
    """Exec's signal function in AmiFUSE's fallback mode: records the bits."""

    def __init__(self):
        self.sent = []

    def signal(self, task, bits):
        self.sent.append((task, bits))


def _exec_lib(mem, device):
    """An ExecLibrary with just what SendIO, AbortIO and WaitIO use."""
    from amitools.vamos.lib.ExecLibrary import ExecLibrary
    from amitools.vamos.lib.lexec.PortManager import PortManager
    from amitools.vamos.libstructs.exec_ import (
        IORequestStruct, MessageStruct, MsgPortStruct, NodeStruct,
    )

    exec_lib = ExecLibrary.__new__(ExecLibrary)
    exec_lib.mem = mem
    exec_lib.port_mgr = PortManager(None)
    exec_lib.signal_func = Signals()
    vlib = SimpleNamespace(get_impl=lambda: device)
    exec_lib.lib_mgr = SimpleNamespace(
        get_vlib_by_addr=lambda addr: vlib if addr == BASE else None)
    io_field = IORequestStruct.sdef.find_field_def_by_name
    exec_lib._io_device_off = io_field("io_Device").offset
    exec_lib._io_flags_off = io_field("io_Flags").offset
    exec_lib._io_error_off = io_field("io_Error").offset
    exec_lib._io_reply_port_off = MessageStruct.sdef.find_field_def_by_name(
        "mn_ReplyPort").offset
    exec_lib._io_node_type_off = NodeStruct.sdef.find_field_def_by_name("ln_Type").offset
    port_field = MsgPortStruct.sdef.find_field_def_by_name
    exec_lib._msg_port_list_off = port_field("mp_MsgList").offset
    exec_lib._msg_port_sigbit_off = port_field("mp_SigBit").offset
    exec_lib._msg_port_sigtask_off = port_field("mp_SigTask").offset
    return exec_lib


def _new_port(mem, addr=PORT, sigbit=SIGBIT, task=TASK):
    """A MsgPort as CreateMsgPort builds it, with an empty message list."""
    mem.w8(addr + 15, sigbit)
    mem.w32(addr + 16, task)
    mem.w32(addr + 20, addr + 24)  # lh_Head -> &lh_Tail
    mem.w32(addr + 24, 0)          # lh_Tail
    mem.w32(addr + 28, addr + 20)  # lh_TailPred -> &lh_Head


def _m68k_list(mem, port=PORT):
    """The nodes linked into the port's m68k message list, head to tail."""
    nodes = []
    node = mem.r32(port + 20)
    while node != port + 24:
        nodes.append(node)
        node = mem.r32(node)
        assert len(nodes) < 10, "message list does not end"
    return nodes


@pytest.fixture
def timer():
    """A device, its clock, memory, Exec and ctx, and one opened request."""
    from amifuse.timer_device import AmiFuseTimerDevice

    mem = FakeMem()
    clock = FakeClock()
    ns = SimpleNamespace(mem=mem, clock=clock)
    ns.device = None
    ns.exec = _exec_lib(mem, None)
    ns.device = AmiFuseTimerDevice(ns.exec, clock=clock)
    ns.exec.lib_mgr = SimpleNamespace(
        get_vlib_by_addr=lambda addr: SimpleNamespace(get_impl=lambda: ns.device)
        if addr == BASE else None)
    ns.ctx = SimpleNamespace(mem=mem)
    ns.device.setup_lib(ns.ctx, BASE)
    _new_port(mem)
    for io in (IO, IO2):
        mem.w32(io + 14, PORT)  # mn_ReplyPort
        mem.w32(io + 20, BASE)  # io_Device
        ns.device.open_dev(ns.ctx, io, UNIT_VBLANK, 0)

    def send(io=IO, secs=0, micro=0, cmd=TR_ADDREQUEST):
        mem.w16(io + 28, cmd)
        mem.w32(io + 32, secs)
        mem.w32(io + 36, micro)
        return ns.exec.SendIO(ns.ctx, io)

    ns.send = send
    return ns


# ---------------------------------------------------------------------------
# A. The device -- arming, delivery, AbortIO, WaitIO
# ---------------------------------------------------------------------------


class TestTimerDevice:
    """TR_ADDREQUEST on a relative unit waits; everything else is as before."""

    def test_request_is_held_until_its_delay(self, timer):
        """SendIO queues no reply; the request is due after its delay."""
        timer.send(micro=500_000)
        assert timer.exec.port_mgr.has_msg(PORT) is False
        assert timer.mem.r8(IO + 30) & 1 == 0          # IOF_QUICK cleared
        assert timer.mem.r8(IO + 8) == NT_MESSAGE
        assert timer.device.next_deadline() == pytest.approx(100.5)
        timer.clock.now = 100.49
        assert timer.device.deliver() == {}
        assert timer.exec.port_mgr.has_msg(PORT) is False

    def test_delay_has_a_floor_of_one_vblank_tick(self, timer):
        """A zero delay still waits 20 ms, bounding how often it can wake."""
        timer.send()
        assert timer.device.next_deadline() == pytest.approx(100.02)

    def test_eclock_unit_counts_eclock_ticks(self, timer):
        """On UNIT_ECLOCK the time is an EClockVal: (hi << 32 | lo) ticks."""
        timer.device.open_dev(timer.ctx, IO, UNIT_ECLOCK, 0)
        timer.send(secs=0, micro=timer.device.ECLOCK_HZ // 4)
        assert timer.device.next_deadline() == pytest.approx(100.25)

    def test_getsystime_stays_quick(self, timer):
        """TR_GETSYSTIME is replied at once, as amitools always did."""
        timer.send(cmd=TR_GETSYSTIME)
        assert timer.exec.port_mgr.has_msg(PORT) is True
        assert timer.device.next_deadline() == float("inf")

    def test_getsystime_answers_in_tr_time(self, timer):
        """The time lands in tr_time, after the 32-byte IORequest, and nowhere else.

        A struct timerequest is 40 bytes. amitools wrote the answer at +48,
        past the end of the request.
        """
        timer.mem.w32(IO + 48, 0xDEADBEEF)
        timer.mem.w32(IO + 52, 0xDEADBEEF)
        timer.send(cmd=TR_GETSYSTIME)
        secs, micros = timer.device.get_sys_time()
        assert abs(timer.mem.r32(IO + 32) - secs) <= 1
        assert timer.mem.r32(IO + 36) < 1_000_000
        assert timer.mem.r32(IO + 48) == timer.mem.r32(IO + 52) == 0xDEADBEEF

    def test_system_time_counts_from_the_amiga_epoch_like_datestamp(self):
        """GetSysTime and dos.library's DateStamp tell the same time.

        AmigaOS counts from 1978-01-01, 2922 days after the Unix epoch;
        vamos' DateStamp subtracts them from UTC, and so must the timer.
        """
        from amitools.vamos.lib.dos.AmiTime import sys_to_ami_time
        from amifuse.timer_device import AmiFuseTimerDevice

        now = time.time()
        stamp = sys_to_ami_time(now)
        secs, micros = AmiFuseTimerDevice.get_sys_time()
        assert 0 <= micros < 1_000_000
        assert abs(secs - (stamp.tday * 86400 + stamp.tmin * 60 + stamp.tick // 50)) <= 2
        assert AmiFuseTimerDevice.AMIGA_EPOCH_OFFSET == 252_460_800

    @pytest.mark.parametrize("unit", [UNIT_WAITUNTIL, None])
    def test_absolute_or_unknown_unit_stays_quick(self, timer, unit):
        """UNIT_WAITUNTIL, or a request never opened here, completes at once."""
        if unit is None:
            timer.device.close_dev(timer.ctx, IO)
        else:
            timer.device.open_dev(timer.ctx, IO, unit, 0)
        timer.send(secs=5)
        assert timer.exec.port_mgr.has_msg(PORT) is True
        assert timer.device.next_deadline() == float("inf")

    def test_delivery_replies_once(self, timer):
        """A due request is replied to its port, flagged done, exactly once."""
        timer.send(micro=200_000)
        timer.clock.now = 100.2
        assert timer.device.deliver() == {TASK: 1 << SIGBIT}
        assert timer.exec.port_mgr.ports[PORT].queue == [IO]
        assert _m68k_list(timer.mem) == [IO]
        assert timer.mem.r8(IO + 30) & 1 == 1          # IOF_QUICK: CheckIO says done
        assert timer.mem.r8(IO + 8) == NT_REPLYMSG
        assert timer.mem.r8s(IO + 31) == 0
        assert timer.exec.signal_func.sent == [(TASK, 1 << SIGBIT)]
        assert timer.exec.CheckIO(timer.ctx, IO) == IO
        assert timer.device.deliver(fast_forward=True) == {}
        assert timer.device.deliveries == 1

    def test_resend_replaces_the_pending_request(self, timer):
        """Sending a request that is still pending re-arms it, once."""
        timer.send(secs=1)
        timer.send(micro=100_000)
        assert timer.device.next_deadline() == pytest.approx(100.1)
        timer.clock.now = 101.5
        timer.device.deliver()
        assert timer.device.deliveries == 1
        assert _m68k_list(timer.mem) == [IO]

    def test_abortio_replies_aborted(self, timer):
        """AbortIO on a pending request replies it with IOERR_ABORTED."""
        timer.send(secs=1)
        assert timer.exec.AbortIO(timer.ctx, IO) == 0
        assert timer.mem.r8s(IO + 31) == -2
        assert timer.mem.r8(IO + 30) & 1 == 1
        assert timer.mem.r8(IO + 8) == NT_REPLYMSG
        assert timer.exec.port_mgr.ports[PORT].queue == [IO]
        assert timer.device.next_deadline() == float("inf")

    def test_abortio_of_a_request_not_pending_does_nothing(self, timer):
        """A request already replied is not replied again."""
        timer.send(micro=50_000)
        timer.clock.now = 101.0
        timer.device.deliver()
        timer.exec.WaitIO(timer.ctx, IO)
        timer.exec.signal_func.sent.clear()
        assert timer.exec.AbortIO(timer.ctx, IO) == 0
        assert timer.exec.port_mgr.has_msg(PORT) is False
        assert timer.exec.signal_func.sent == []

    def test_close_device_drops_the_request(self, timer):
        """CloseDevice forgets a pending request, so it is never replied."""
        timer.send(secs=1)
        timer.device.close_dev(timer.ctx, IO)
        assert timer.device.deliver(fast_forward=True) == {}
        assert timer.exec.port_mgr.has_msg(PORT) is False

    def test_abort_waitio_rearm_leaves_no_stale_link(self, timer):
        """AbortIO + WaitIO takes the reply back out; re-arming links it once.

        The port is created unregistered, as CreateMsgPort does in AmiFUSE.
        Arming registers it, so WaitIO removes the reply from both the
        port manager's queue and the m68k list.
        """
        assert not timer.exec.port_mgr.has_port(PORT)
        timer.send(secs=1)
        timer.exec.AbortIO(timer.ctx, IO)
        assert timer.exec.WaitIO(timer.ctx, IO) == -2
        assert timer.exec.port_mgr.ports[PORT].queue == []
        assert _m68k_list(timer.mem) == []
        timer.send(micro=200_000)
        timer.clock.now = 101.0
        timer.device.deliver()
        assert timer.exec.port_mgr.ports[PORT].queue == [IO]
        assert _m68k_list(timer.mem) == [IO]

    def _waitport_frame(self, timer, waitpkt=False):
        """The resume frame of a process parked in WaitPort() (or WaitPkt()) on PORT."""
        from amifuse.startup_runner import _build_resume_frame

        sp = 0x4000
        timer.mem.w32(sp, 0x5000)  # WaitPort's return address
        block = {"waitport_blocked_sp": sp, "waitport_blocked_port": PORT,
                 "waitport_blocked_ret": 0x5000, "waitpkt_blocked": waitpkt}
        return _build_resume_frame(
            block, default_port_addr=PORT, mem=timer.mem,
            port_mgr=timer.exec.port_mgr,
            compute_pending_signals=lambda mask: 0,
            clear_signals_from_task=lambda signals: None,
            leave_queued=timer.device.delays)

    def test_child_parked_in_waitport_wakes_on_delivery(self, timer):
        """A process parked in WaitPort() on the timer port resumes with the reply.

        As Exec's WaitPort() does, the resume leaves the reply queued, so
        the GetMsg() that follows, as in while (!GetMsg(p)) WaitPort(p),
        takes it.
        """
        timer.send(micro=100_000)
        assert self._waitport_frame(timer) is None
        timer.clock.now = 100.1
        timer.device.deliver()
        assert self._waitport_frame(timer) == {"pc": 0x5000, "sp": 0x4004, "d0": IO}
        assert timer.exec.port_mgr.ports[PORT].queue == [IO]
        assert _m68k_list(timer.mem) == [IO]

    @pytest.mark.parametrize("case", ["waitpkt", "quick reply"])
    def test_resume_still_takes_other_messages_off_the_port(self, timer, case):
        """WaitPkt() and messages timer.device did not delay are taken off, as before."""
        if case == "waitpkt":
            timer.send(micro=100_000)
            timer.clock.now = 100.1
            timer.device.deliver()
        else:
            timer.send(cmd=TR_GETSYSTIME)  # replied at once by SendIO
        assert timer.exec.port_mgr.ports[PORT].queue == [IO]
        frame = self._waitport_frame(timer, waitpkt=case == "waitpkt")
        assert frame["pc"] == 0x5000
        assert timer.exec.port_mgr.ports[PORT].queue == []
        assert _m68k_list(timer.mem) == []

    # Each place that resumes a WaitPort() passes timer.device's delays, so
    # a delayed reply stays queued for the GetMsg() that follows.

    _DOS_PORT = 0x2800     # the handler's own pr_MsgPort
    _DOS_MSG = 0x2900

    @pytest.fixture
    def parked(self, timer, monkeypatch):
        """No live Exec/DOS block state; the resumes below clear it."""
        from amitools.vamos.lib.DosLibrary import DosLibrary
        from amitools.vamos.lib.ExecLibrary import ExecLibrary

        for name in ("waitport_blocked_sp", "waitport_blocked_port",
                     "waitport_blocked_ret", "wait_blocked_mask",
                     "wait_blocked_sp", "wait_blocked_ret"):
            monkeypatch.setattr(ExecLibrary, f"_{name}", None)
        monkeypatch.setattr(DosLibrary, "_waitpkt_blocked", False)
        timer.mem.w32(0x4000, 0x5000)  # WaitPort's return address
        return {"waitport_blocked_sp": 0x4000, "waitport_blocked_port": PORT,
                "waitport_blocked_ret": 0x5000, "waitpkt_blocked": False}

    def _launcher(self, timer):
        from amifuse.startup_runner import HandlerLauncher

        launcher = HandlerLauncher.__new__(HandlerLauncher)
        launcher.mem = timer.mem
        launcher.exec_impl = timer.exec
        launcher._debug = False
        launcher._warned_tc_sigrecvd = False
        cpu = SimpleNamespace(regs={})
        cpu.w_reg = cpu.regs.__setitem__
        launcher.vh = SimpleNamespace(
            timer_device=timer.device, machine=SimpleNamespace(cpu=cpu),
            alloc=SimpleNamespace(get_mem=lambda: timer.mem))
        return launcher

    def _state(self, block, port_addr=PORT):
        return SimpleNamespace(
            port_addr=port_addr, block_state=dict(block), pc=0x7000, sp=0x7800,
            regs=[0] * 16, initialized=True, main_loop_pc=0x5000,
            main_loop_sp=0x4004, wait_mask=0)

    def _deliver(self, timer):
        timer.send(micro=100_000)
        timer.clock.now = 100.1
        timer.device.deliver()
        assert timer.exec.port_mgr.ports[PORT].queue == [IO]

    def _assert_reply_left_queued(self, timer, pc, d0):
        assert (pc, d0) == (0x5000, IO)
        assert timer.exec.port_mgr.ports[PORT].queue == [IO]
        assert _m68k_list(timer.mem) == [IO]

    def test_setup_resume_leaves_a_delayed_reply_queued(self, timer, parked):
        """setup_resume_if_blocked, for a reply that came after the burst."""
        launcher = self._launcher(timer)
        state = self._state(parked)
        self._deliver(timer)
        assert launcher.setup_resume_if_blocked(state)
        self._assert_reply_left_queued(timer, state.pc, state.regs[0])

    def test_burst_that_parks_leaves_a_delayed_reply_queued(self, timer, parked):
        """_finalize_burst_blocked, for a reply already there when the burst parks."""
        launcher = self._launcher(timer)
        state = self._state(parked)
        self._deliver(timer)
        launcher._finalize_burst_blocked(
            state, state.block_state, 0x4000, 0x5000, None, 0x4000, 0x10000)
        self._assert_reply_left_queued(timer, state.pc, state.regs[0])

    def test_child_resume_leaves_a_delayed_reply_queued(self, timer, parked):
        """ProcessManager._resume_child_if_ready, for a child parked on its timer port."""
        from amifuse.process_mgr import ProcessManager, ProcessState

        pm = ProcessManager.__new__(ProcessManager)
        pm.mem = timer.mem
        pm.exec_impl = timer.exec
        pm.vh = SimpleNamespace(timer_device=timer.device)
        child = ProcessState(proc_addr=TASK, entry_pc=0x7000, sp=0x7800,
                             port_addr=PORT, blocked=True, block_state=dict(parked))
        self._deliver(timer)
        assert pm._resume_child_if_ready(child)
        self._assert_reply_left_queued(timer, child.entry_pc, child.regs[0])

    @pytest.mark.parametrize("on", ["dos port", "timer port"])
    def test_burst_parked_in_waitport_wakes_on_the_port_it_waits_on(
            self, timer, parked, on):
        """A WaitPort() park wakes for its own port, not for the DOS port.

        A handler in WaitPort(timer_port) with a packet on its DOS port
        stays parked: resumed, it would get D0 = 0, which no WaitPort()
        returns. A reply on the timer port wakes it with an empty DOS port.
        """
        launcher = self._launcher(timer)
        state = self._state(parked, port_addr=self._DOS_PORT)
        timer.exec.port_mgr.register_port(self._DOS_PORT)
        if on == "dos port":
            timer.send(micro=100_000)  # pending: the timer port is empty
            timer.exec.port_mgr.put_msg(self._DOS_PORT, self._DOS_MSG)
        else:
            self._deliver(timer)
        launcher._finalize_burst_blocked(
            state, state.block_state, 0x4000, 0x5000, None, 0x4000, 0x10000)
        if on == "dos port":
            # Still parked: the block state stays and D0 is not set.
            assert state.block_state == parked
            assert launcher.vh.machine.cpu.regs == {}
            assert timer.exec.port_mgr.ports[self._DOS_PORT].queue == [self._DOS_MSG]
        else:
            self._assert_reply_left_queued(timer, state.pc, state.regs[0])

    def test_bad_reply_port_is_replied_as_sendio_does(self, timer):
        """A reply port outside memory gets no link and no signal, and raises nothing.

        The other requests due with it are replied as usual.
        """
        bad = len(timer.mem.data) + 0x1000
        timer.mem.w32(IO + 14, bad)
        timer.send(IO, micro=100_000)
        timer.send(IO2, micro=200_000)
        timer.clock.now = 101.0
        assert timer.device.deliver() == {TASK: 1 << SIGBIT}
        assert timer.device.deliveries == 2
        assert timer.exec.port_mgr.ports[bad].queue == [IO]
        assert _m68k_list(timer.mem) == [IO2]

    def test_unreadable_request_does_not_stop_the_others(self, timer):
        """A request whose memory is gone is dropped; the rest are replied."""
        timer.send(IO, micro=100_000)
        timer.device._pending[len(timer.mem.data) + 0x1000] = (100.0, BASE)
        timer.clock.now = 101.0
        assert timer.device.deliver() == {TASK: 1 << SIGBIT}
        assert timer.device.deliveries == 1
        assert timer.device.next_deadline() == float("inf")
        assert _m68k_list(timer.mem) == [IO]

    def test_fast_forward_delivers_every_pending_request(self, timer):
        """With fast_forward, requests whose time has not come are due too."""
        timer.send(IO, secs=5)
        timer.send(IO2, secs=60)
        assert timer.device.deliver(fast_forward=True) == {TASK: 1 << SIGBIT}
        assert timer.exec.port_mgr.ports[PORT].queue == [IO, IO2]
        assert _m68k_list(timer.mem) == [IO, IO2]

    @pytest.mark.parametrize("change", ["device", "type"])
    def test_freed_or_reused_request_is_not_replied(self, timer, change):
        """A request whose io_Device or ln_Type changed since it was sent is dropped."""
        timer.send(micro=100_000)
        if change == "device":
            timer.mem.w32(IO + 20, 0)
        else:
            timer.mem.w8(IO + 8, NT_REPLYMSG)
        assert timer.device.deliver(fast_forward=True) == {}
        assert timer.device.deliveries == 0
        assert timer.device.next_deadline() == float("inf")

    def test_event_is_set_only_for_an_earlier_deadline(self, timer):
        """The waiting thread is woken only when it must look again sooner."""
        timer.send(IO, secs=1)
        assert timer.device.event.is_set()            # the first arm
        timer.device.event.clear()
        timer.send(IO2, secs=2)
        assert not timer.device.event.is_set()        # later than the earliest
        timer.send(IO2, micro=500_000)
        assert timer.device.event.is_set()            # earlier than the earliest

    def test_delays_only_requests_it_holds(self, timer):
        """Only a relative TR_ADDREQUEST opened here counts as delayed.

        The drain keeps those replies; the rest completed at once.
        """
        timer.mem.w16(IO + 28, TR_ADDREQUEST)
        assert timer.device.delays(IO)
        timer.mem.w16(IO + 28, TR_GETSYSTIME)
        assert not timer.device.delays(IO)
        timer.mem.w16(IO2 + 28, TR_ADDREQUEST)
        timer.device.open_dev(timer.ctx, IO2, UNIT_WAITUNTIL, 0)
        assert not timer.device.delays(IO2)
        copy = 0x1200                       # copied from an opened request
        timer.mem.data[copy:copy + 40] = timer.mem.data[IO:IO + 40]
        timer.mem.w16(copy + 28, TR_ADDREQUEST)
        assert timer.device.delays(copy)
        timer.mem.w32(IO + 20, BASE + 0x100)  # another device
        timer.mem.w16(IO + 28, TR_ADDREQUEST)
        assert not timer.device.delays(IO)

    def test_waitport_on_a_pending_request_parks(self, timer, monkeypatch):
        """WaitPort on the reply port of a pending request parks, then gets the reply.

        amitools' WaitPort parks only on a port the port manager knows and
        spins on any other empty one, so arming registers the port.
        """
        from amitools.vamos.error import UnsupportedFeatureError
        from amitools.vamos.lib.ExecLibrary import ExecLibrary
        from amitools.vamos.lib.lexec.msgfunc import MessageFunc

        for name in ("sp", "port", "ret"):
            monkeypatch.setattr(ExecLibrary, f"_waitport_blocked_{name}", None)

        class Spin:
            def wait(self, mask):
                raise AssertionError("WaitPort spins on an unregistered port")

        sp = 0x4000
        timer.mem.w32(sp, 0x5000)
        ctx = SimpleNamespace(mem=timer.mem, cpu=SimpleNamespace(r_reg=lambda reg: sp))
        msg_func = MessageFunc(ctx, None, Spin(), None, timer.exec.port_mgr)
        port = SimpleNamespace(addr=PORT)
        timer.send(micro=100_000)
        with pytest.raises(UnsupportedFeatureError):
            msg_func.wait_port(port)
        assert ExecLibrary._waitport_blocked_port == PORT
        timer.clock.now = 100.1
        timer.device.deliver()
        assert msg_func.wait_port(port).addr == IO


# ---------------------------------------------------------------------------
# B. The bridge -- drain, packet resume, and the mount's timer thread
# ---------------------------------------------------------------------------


class _Timer:
    """The bridge's view of AmiFuseTimerDevice."""

    MIN_DELAY = 0.02

    def __init__(self, owned=(), signals=None, deadline=float("inf"), held=False):
        self.owned = set(owned)
        self.signals = signals or {}
        self.deadline = deadline
        # Held: the signals are not due, so only a fast-forward sends them.
        self.held = held
        self.event = threading.Event()
        self.deliveries = 0
        self.delivered = []

    def delays(self, msg_addr):
        return msg_addr in self.owned

    def deliver(self, fast_forward=False):
        self.delivered.append(fast_forward)
        if self.held and not fast_forward:
            return {}
        if self.signals:
            self.deliveries += 1
        signals, self.signals = self.signals, {}
        return signals

    def next_deadline(self):
        return self.deadline

    def clock(self):
        return time.monotonic()


class TestBridgeDelivery:
    """Where HandlerBridge replies to due timer requests."""

    def _bridge(self, timer, **state):
        from amifuse import fuse_fs as fs
        from amitools.vamos.lib.lexec.PortManager import PortManager

        bridge = fs.HandlerBridge.__new__(fs.HandlerBridge)
        bridge._debug = False
        bridge._busy_ran_out = False
        bridge._lock = threading.RLock()
        bridge.state = SimpleNamespace(
            crashed=False, process_addr=TASK, port_addr=0x6000,
            reply_port_addr=0x6100, pc=0x4000, main_loop_pc=0x4000,
            main_loop_sp=0x7000, sp=0x7000, block_state=None,
            wait_mask=0x101, regs=[0] * 16, initialized=True)
        for key, val in state.items():
            setattr(bridge.state, key, val)
        bridge.mem = FakeMem()
        bridge.vh = SimpleNamespace(
            timer_device=timer, machine=SimpleNamespace(cpu=SimpleNamespace(
                regs={}, w_reg=lambda reg, val: bridge.vh.machine.cpu.regs.__setitem__(reg, val))),
            slm=SimpleNamespace(exec_impl=SimpleNamespace(port_mgr=PortManager(None))))
        return bridge

    def test_drain_keeps_timer_replies(self):
        """Stray device replies are dropped; a delivered timer reply stays."""
        timer = _Timer(owned={IO})
        bridge = self._bridge(timer)
        pmgr = bridge.vh.slm.exec_impl.port_mgr
        pmgr.register_port(PORT)
        pmgr.put_msg(PORT, 0x1800)  # a stray reply from another device
        pmgr.put_msg(PORT, IO)
        pmgr.put_msg(bridge.state.port_addr, 0x1900)  # a DOS packet: reserved
        bridge._drain_non_essential_ports(pmgr)
        assert pmgr.ports[PORT].queue == [IO]
        assert pmgr.ports[bridge.state.port_addr].queue == [0x1900]

    def test_drain_drops_timer_replies_that_came_at_once(self, timer):
        """A timer request that completed at once is drained; a delayed one stays."""
        bridge = self._bridge(timer.device)
        bridge.vh.slm.exec_impl.port_mgr = pmgr = timer.exec.port_mgr
        timer.send(IO2, micro=100_000)
        timer.clock.now = 100.1
        timer.device.deliver()
        timer.send(IO, cmd=TR_GETSYSTIME)
        assert pmgr.ports[PORT].queue == [IO2, IO]
        bridge._drain_non_essential_ports(pmgr)
        assert pmgr.ports[PORT].queue == [IO2]

    def test_deliver_timers_returns_the_handlers_signals(self):
        """Only the bits for the handler's own task wake it; children's wait."""
        timer = _Timer(signals={TASK: 0x1, 0x9000: 0x10})
        assert self._bridge(timer)._deliver_timers() == 0x1
        assert timer.delivered == [False]

    def test_packet_resume_adds_due_timer_bits(self, monkeypatch):
        """The resume for a packet returns the DOS bit and the timer's bit."""
        from amifuse import fuse_fs as fs
        from amitools.vamos.lib.lexec.signalfunc import SignalFunc

        timer = _Timer(signals={TASK: 0x1})
        bridge = self._bridge(timer)
        pmgr = bridge.vh.slm.exec_impl.port_mgr
        pmgr.register_port(bridge.state.port_addr)
        pmgr.put_msg(bridge.state.port_addr, 0x1900)
        bridge.mem.w8(bridge.state.port_addr + 15, 8)  # mp_SigBit of the DOS port
        monkeypatch.setattr(SignalFunc, "_fallback_signals", 0x1)
        seen = {}

        class Launcher:
            last_pkt_type = None

            def run_burst(self, state, max_cycles=0, debug=False):
                seen["d0"] = state.regs[0]
                state.run_state = SimpleNamespace(done=False, error=True, cycles=0)

            def poll_replies(self, port_addr, debug=False):
                return [(0x1900, 0x1a00, 1, 0)]

        bridge.launcher = Launcher()
        monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)
        assert bridge._run_until_replies() == [(0x1900, 0x1a00, 1, 0)]
        assert seen["d0"] == 0x101
        assert bridge.vh.machine.cpu.regs[0] == 0x101
        # Wait() clears what it returns, so the next Wait() gets no timer bit.
        assert SignalFunc._fallback_signals == 0
        assert bridge.mem.r32(TASK + _TC_SIGRECVD) & 0x1 == 0

    def test_timer_bit_for_a_wait_frame_is_returned_once(self, monkeypatch):
        """A due timer bit for a handler parked in Wait() leaves the fallback signals.

        The resume from the saved Wait() frame returns it from tc_SigRecvd
        and clears it there; the next Wait() must not return it again.
        """
        from amifuse import fuse_fs as fs
        from amitools.vamos.lib.lexec.signalfunc import SignalFunc

        timer = _Timer(signals={TASK: 0x1})
        bridge = self._bridge(timer, pc=0x4100, block_state={
            "wait_blocked_sp": 0x6ff0, "wait_blocked_mask": 0x1,
            "wait_blocked_ret": 0x4100})
        monkeypatch.setattr(SignalFunc, "_fallback_signals", 0x1 | 0x20)
        bridge.mem.w32(TASK + _TC_SIGRECVD, 0x1)  # as Signal() sets it

        class Launcher:
            last_pkt_type = None

            def run_burst(self, state, max_cycles=0, debug=False):
                state.run_state = SimpleNamespace(done=False, error=True, cycles=0)

            def poll_replies(self, port_addr, debug=False):
                return [(0x1900, 0x1a00, 1, 0)]

        bridge.launcher = Launcher()
        monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)
        bridge._run_until_replies()
        assert SignalFunc._fallback_signals == 0x20
        assert bridge.mem.r32(TASK + _TC_SIGRECVD) == 0x1

    def _packet_wait(self, monkeypatch, bridge, timer, reply_after_delivery):
        """Run a packet wait whose handler replies once a timer was delivered."""
        from amifuse import fuse_fs as fs

        bursts = []

        class Launcher:
            last_pkt_type = None

            def run_burst(self, state, max_cycles=0, debug=False):
                bursts.append(timer.deliveries)
                state.run_state = SimpleNamespace(done=False, error=True, cycles=0)

            def poll_replies(self, port_addr, debug=False):
                if reply_after_delivery and timer.deliveries:
                    return [(0x1900, 0x1a00, 1, 0)]
                return []

        bridge.launcher = Launcher()
        monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)
        return bridge._run_until_replies(max_iters=5), bursts

    def test_timer_a_handler_waits_for_mid_packet_is_replied_in_the_wait(self, monkeypatch):
        """Parked away from its main loop, the handler gets every pending timer at once.

        It waits in Wait() for its own timer before replying to the packet,
        so the timer fast-forwards, as when timer.device completed at once,
        and the same wait gets the reply.
        """
        timer = _Timer(signals={TASK: 0x1}, deadline=time.monotonic() + 60, held=True)
        bridge = self._bridge(timer, pc=0x4100, block_state={
            "wait_blocked_sp": 0x6ff0, "wait_blocked_mask": 0x1,
            "wait_blocked_ret": 0x4100})
        replies, bursts = self._packet_wait(monkeypatch, bridge, timer, True)
        assert replies == [(0x1900, 0x1a00, 1, 0)]
        assert timer.delivered == [False, True]
        assert bursts == [0, 1]

    def test_no_fast_forward_at_the_main_loop(self, monkeypatch):
        """Parked at its main loop, the handler's timers keep their deadlines."""
        timer = _Timer(signals={TASK: 0x1}, deadline=time.monotonic() + 60, held=True)
        bridge = self._bridge(timer, block_state={
            "wait_blocked_sp": 0x6ff0, "wait_blocked_mask": 0x101,
            "wait_blocked_ret": 0x4000})
        replies, _ = self._packet_wait(monkeypatch, bridge, timer, False)
        assert replies == []
        assert True not in timer.delivered
        assert len(timer.delivered) == 6  # at the start, then once per idle burst

    def test_run_due_timers_skips_a_busy_handler(self):
        """A handler whose packet wait ran out is left alone, and retried later."""
        timer = _Timer(signals={TASK: 0x1}, deadline=0.0)
        bridge = self._bridge(timer)
        bridge._busy_ran_out = True
        assert bridge.run_due_timers() == bridge._TIMER_RETRY
        assert timer.delivered == []

    def test_run_due_timers_skips_a_handler_not_at_its_main_loop(self):
        timer = _Timer(signals={TASK: 0x1}, deadline=0.0)
        bridge = self._bridge(timer, pc=0x4100)
        assert bridge.run_due_timers() == bridge._TIMER_RETRY
        assert timer.delivered == []

    # A Wait() in a subroutine called from the main loop and from deeper
    # code returns to the main loop's PC on a deeper stack frame.
    _DEEPER_FRAME = {"sp": 0x6f00, "block_state": {
        "wait_blocked_sp": 0x6efc, "wait_blocked_mask": 0x1,
        "wait_blocked_ret": 0x4000}}

    def test_run_due_timers_skips_the_main_loop_pc_on_a_deeper_frame(self):
        timer = _Timer(signals={TASK: 0x1}, deadline=0.0)
        bridge = self._bridge(timer, **self._DEEPER_FRAME)
        assert bridge.run_due_timers() == bridge._TIMER_RETRY
        assert timer.delivered == []
        assert bridge.state.sp == 0x6f00

    def test_main_loop_pc_on_a_deeper_frame_gets_its_timer_in_the_wait(self, monkeypatch):
        """Parked at the main loop's PC on a deeper frame, it waits within the packet."""
        timer = _Timer(signals={TASK: 0x1}, deadline=time.monotonic() + 60, held=True)
        bridge = self._bridge(timer, **self._DEEPER_FRAME)
        replies, bursts = self._packet_wait(monkeypatch, bridge, timer, True)
        assert replies == [(0x1900, 0x1a00, 1, 0)]
        assert timer.delivered == [False, True]
        assert bursts == [0, 1]

    # The handler parked again in its main-loop Wait().
    _PARKED = {"wait_blocked_sp": 0x6ffc, "wait_blocked_mask": 0x101,
               "wait_blocked_ret": 0x4000}

    def _parking_launcher(self, bursts, resumes, wakes=None):
        """A launcher whose bursts all park, reporting 0 cycles as machine_run does.

        wakes maps a burst number to where its wake comes from: "burst",
        already pending when it parks, so run_burst leaves no block state;
        or "child", a child's reply after it, which setup_resume_if_blocked
        finds. resumes records what each setup_resume_if_blocked call found.
        """
        parked = self._PARKED
        wakes = wakes or {}

        class Launcher:
            def run_burst(self, state, max_cycles=0, debug=False):
                bursts.append((state.pc, state.regs[0]))
                state.run_state = SimpleNamespace(done=False, error=True, cycles=0)
                pending = wakes.get(len(bursts)) == "burst"
                state.block_state = None if pending else dict(parked)

            def setup_resume_if_blocked(self, state, debug=False):
                found = wakes.get(len(bursts)) == "child"
                resumes.append(found)
                if found:
                    state.block_state = None
                return found

        return Launcher()

    def test_run_due_timers_wakes_the_parked_handler(self, monkeypatch):
        """Due bits are returned by its main-loop Wait(), and it runs until it parks."""
        from amifuse import fuse_fs as fs

        timer = _Timer(signals={TASK: 0x1})
        bridge = self._bridge(timer)
        bursts, resumes = [], []
        bridge.launcher = self._parking_launcher(bursts, resumes)
        monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)
        assert bridge.run_due_timers() is None
        # It parks with nothing pending: one burst.
        assert bursts == [(0x4000, 0x1)]
        assert resumes == [False]
        assert bridge._busy_ran_out is False

    @pytest.mark.parametrize("wake", ["burst", "child"])
    def test_resume_runs_a_handler_that_parks_with_a_wake_pending(self, monkeypatch, wake):
        """A park with a wake pending runs again in the same resume.

        Every burst that parks reports 0 cycles. Parked with a self-signal
        or a quick reply already pending (burst), or with a child's reply
        that came after it (child), the handler must not wait for the next
        timer or FUSE operation.
        """
        from amifuse import fuse_fs as fs

        timer = _Timer(signals={TASK: 0x1})
        bridge = self._bridge(timer)
        bursts, resumes = [], []
        bridge.launcher = self._parking_launcher(bursts, resumes, wakes={1: wake})
        monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)
        assert bridge.run_due_timers() is None
        assert len(bursts) == 2
        assert resumes == ([False] if wake == "burst" else [True, False])

    def test_timer_resume_that_runs_out_sets_busy_ran_out(self, monkeypatch):
        """A resume that never parks again stops at the busy limit, as a packet does."""
        from amifuse import fuse_fs as fs

        timer = _Timer(signals={TASK: 0x1})
        bridge = self._bridge(timer)
        monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES", 1_000_000)

        class Launcher:
            bursts = 0

            def run_burst(self, state, max_cycles=0, debug=False):
                Launcher.bursts += 1
                state.run_state = SimpleNamespace(done=False, error=False, cycles=max_cycles)

        bridge.launcher = Launcher()
        monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)
        bridge.run_due_timers()
        assert Launcher.bursts == 5
        assert bridge._busy_ran_out is True


class TestTimerThread:
    """AmigaFuseFS runs one amifuse-timer thread from init() to destroy()."""

    def _fs(self, timer, crashed=False, results=()):
        """An AmigaFuseFS whose run_due_timers returns results, then None.

        A result that is an exception is raised instead.
        """
        from amifuse.fuse_fs import AmigaFuseFS

        calls = []
        results = list(results)

        def run_due_timers():
            calls.append(time.monotonic())
            result = results.pop(0) if results else None
            if isinstance(result, Exception):
                raise result
            return result  # None: nothing pending, wait for the event

        bridge = SimpleNamespace(
            _write_enabled=False, _lock=threading.RLock(),
            state=SimpleNamespace(crashed=crashed), run_due_timers=run_due_timers,
            vh=SimpleNamespace(timer_device=timer, shutdown=lambda: None),
            backend=SimpleNamespace(sync=lambda: None, close=lambda: None))
        return AmigaFuseFS(bridge), calls

    def test_thread_starts_with_init_and_stops_with_destroy(self):
        timer = _Timer()
        fs, calls = self._fs(timer)
        fs.init("/")
        thread = fs._timer_thread
        assert thread.name == "amifuse-timer" and thread.daemon
        time.sleep(0.2)
        # Nothing pending: one look, then it waits on the event.
        assert len(calls) == 1
        timer.event.set()
        time.sleep(0.2)
        assert len(calls) == 2
        fs.destroy("/")
        assert not thread.is_alive()
        assert fs._timer_thread is None

    @pytest.fixture
    def shutdowns(self, monkeypatch):
        """Set by a crash shutdown, which would otherwise send SIGTERM to pytest."""
        from amifuse.fuse_fs import AmigaFuseFS

        done = threading.Event()
        monkeypatch.setattr(
            AmigaFuseFS, "_terminate_mount_after_crash", lambda self: done.set())
        return done

    def test_thread_stops_when_the_handler_crashed(self, shutdowns):
        fs, calls = self._fs(_Timer(), crashed=True)
        fs.init("/")
        fs._timer_thread.join(2)
        assert not fs._timer_thread.is_alive()
        assert calls == []
        assert shutdowns.wait(2)
        fs.destroy("/")

    def test_crash_in_a_timer_burst_shuts_the_mount_down(self, shutdowns):
        """A handler that crashes while idle shuts the mount down, as a FUSE operation does."""
        timer = _Timer()
        fs, calls = self._fs(timer)

        def run_due_timers():
            calls.append(time.monotonic())
            fs.bridge.state.crashed = True
            return None  # nothing pending: unchecked, the thread would wait forever

        fs.bridge.run_due_timers = run_due_timers
        fs.init("/")
        fs._timer_thread.join(2)
        assert not fs._timer_thread.is_alive()
        assert len(calls) == 1
        assert shutdowns.wait(2)
        fs.destroy("/")

    def test_thread_yields_the_lock_when_a_request_is_due_again_at_once(self):
        """After each look it waits a tick, so FUSE operations get the lock.

        Each call stands for a resume that outlasts the handler's re-arm
        delay: it holds the lock, re-arms (which sets the event) and leaves
        the request due again at once. It counts a fixed number of looks,
        not a time window, so a slow runner only makes it take longer.
        """
        timer = _Timer()
        fs, _ = self._fs(timer)
        looks = 6
        spans = []
        done = threading.Event()

        def run_due_timers():
            start = time.monotonic()
            while time.monotonic() - start < 0.05:  # emulation holds the GIL
                pass
            spans.append((start, time.monotonic()))
            if len(spans) == looks:
                done.set()
                return None  # nothing pending: it waits on the event
            timer.event.set()
            return 0.0

        fs.bridge.run_due_timers = run_due_timers
        fs.init("/")
        waits = []
        deadline = time.monotonic() + 30
        while not done.is_set() and time.monotonic() < deadline:
            start = time.monotonic()
            if fs.bridge._lock.acquire(timeout=5.0):
                fs.bridge._lock.release()
            waits.append(time.monotonic() - start)
            time.sleep(0.005)
        fs.destroy("/")
        assert len(spans) == looks
        gaps = [nxt[0] - prev[1] for prev, nxt in zip(spans, spans[1:])]
        assert min(gaps) >= timer.MIN_DELAY / 2
        assert max(waits) < 2.0

    def test_destroy_during_a_timer_burst_does_not_hang(self):
        """A stop that comes while run_due_timers runs ends the thread.

        The thread clears the event after run_due_timers, which would erase
        the stop's wake; with nothing pending it would then wait for good,
        and destroy() with it, before the final flush.
        """
        timer = _Timer()
        fs, calls = self._fs(timer)
        inside, release = threading.Event(), threading.Event()

        def run_due_timers():
            calls.append(time.monotonic())
            inside.set()
            release.wait(5)  # a resume in progress, holding the bridge lock
            return None  # nothing pending afterwards

        fs.bridge.run_due_timers = run_due_timers
        fs.init("/")
        thread = fs._timer_thread
        assert inside.wait(5)
        destroy = threading.Thread(target=fs.destroy, args=("/",), daemon=True)
        destroy.start()
        time.sleep(0.2)  # destroy has set the stop and the event, and joins
        release.set()
        destroy.join(3)
        hung = destroy.is_alive()
        timer.event.set()  # frees a hung thread, so it does not outlive the test
        destroy.join(3)
        assert not hung
        assert not thread.is_alive()
        assert len(calls) == 1

    def test_a_long_timeout_does_not_end_the_thread(self):
        """A deadline beyond threading.TIMEOUT_MAX is slept in bounded steps."""
        timer = _Timer()
        fs, calls = self._fs(timer, results=[threading.TIMEOUT_MAX * 10])
        fs.init("/")
        time.sleep(0.2)
        assert fs._timer_thread.is_alive()
        assert len(calls) == 1
        thread = fs._timer_thread
        fs.destroy("/")
        assert not thread.is_alive()

    def test_an_error_does_not_end_the_thread(self, monkeypatch, capsys):
        """An exception from run_due_timers is reported under --debug, then retried."""
        from amifuse.fuse_fs import HandlerBridge

        monkeypatch.setattr(HandlerBridge, "_TIMER_RETRY", 0.05)
        timer = _Timer()
        fs, calls = self._fs(timer, results=[RuntimeError("boom")])
        fs._debug = True
        fs.init("/")
        time.sleep(0.3)
        assert fs._timer_thread.is_alive()
        assert len(calls) == 2
        fs.destroy("/")
        err = capsys.readouterr()
        assert "error delivering timers" in err.out
        assert "RuntimeError: boom" in err.err

    def test_no_thread_without_a_timer_device(self):
        fs, _ = self._fs(None)
        fs.init("/")
        assert fs._timer_thread is None
        fs.destroy("/")
