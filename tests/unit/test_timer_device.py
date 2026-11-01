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
from unittest.mock import Mock

import pytest

BASE = 0x8000          # timer.device's library base
IO = 0x1000            # a struct timerequest
IO2 = 0x1100
PORT = 0x2000          # the handler's timer reply port
TASK = 0x3000          # the task the port signals
UNITS = 0xA000         # the Units OpenDevice allocates
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


class FakeAlloc:
    """ctx.alloc's alloc_astruct: hands out 64-byte blocks from UNITS on."""

    def __init__(self):
        self.next = UNITS
        self.live = set()

    def alloc_astruct(self, struct, label=None):
        addr = self.next
        self.next += 0x40
        self.live.add(addr)
        return SimpleNamespace(addr=addr, free=lambda: self.live.remove(addr))


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
    ns.ctx = SimpleNamespace(mem=mem, alloc=FakeAlloc())
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

        AmigaOS counts from 1978-01-01, 2922 days after the Unix epoch, in
        local time; AmiFUSE's DateStamp does, and so must the timer.
        """
        from amifuse.amiga_time import AMIGA_EPOCH, to_datestamp
        from amifuse.timer_device import AmiFuseTimerDevice

        now = time.time()
        days, minute, tick = to_datestamp(now)
        secs, micros = AmiFuseTimerDevice.get_sys_time()
        assert 0 <= micros < 1_000_000
        assert abs(secs - (days * 86400 + minute * 60 + tick // 50)) <= 2
        assert AMIGA_EPOCH == 252_460_800

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


class TestIdleTimers:
    """AmigaFuseFS has the bridge reply to timers while idle, from init() to destroy().

    The bridge's emulation thread runs run_due_timers() whenever it has
    waited as long as that asked for (HandlerBridge.start_idle_timers); the
    mount only switches it on and off, and is told of a crash found there.
    """

    def _fs(self, timer, emu=True):
        from amifuse.fuse_fs import AmigaFuseFS

        bridge = SimpleNamespace(
            _write_enabled=False, _lock=threading.RLock(),
            state=SimpleNamespace(crashed=False),
            vh=SimpleNamespace(timer_device=timer), shutdown_runtime=lambda: None,
            backend=SimpleNamespace(sync=lambda: None, close=lambda: None),
            start_idle_timers=Mock(), stop_idle_timers=Mock())
        if emu:
            bridge._emu = SimpleNamespace(alive=True)
        return AmigaFuseFS(bridge)

    def test_init_starts_the_timers_and_destroy_stops_them(self):
        fs = self._fs(_Timer())
        fs.init("/")
        fs.bridge.start_idle_timers.assert_called_once_with(
            on_crash=fs._schedule_crash_shutdown)
        assert fs._timers_started
        fs.destroy("/")
        fs.bridge.stop_idle_timers.assert_called_once_with()
        assert not fs._timers_started

    def test_no_timers_without_a_timer_device(self):
        fs = self._fs(None)
        fs.init("/")
        fs.bridge.start_idle_timers.assert_not_called()
        fs.destroy("/")
        fs.bridge.stop_idle_timers.assert_not_called()

    def test_no_timers_without_an_emulation_thread(self):
        fs = self._fs(_Timer(), emu=False)
        fs.init("/")
        fs.bridge.start_idle_timers.assert_not_called()
        fs.destroy("/")


class TestBridgeIdleTimers:
    """What the bridge does when its emulation thread is idle."""

    def _bridge(self, timer, results):
        """A bridge whose run_due_timers returns results, then None.

        A result that is an exception is raised instead.
        """
        from amifuse.fuse_fs import HandlerBridge

        results = list(results)
        calls = []

        def run_due_timers():
            calls.append(1)
            result = results.pop(0) if results else None
            if isinstance(result, Exception):
                raise result
            return result

        bridge = HandlerBridge.__new__(HandlerBridge)
        bridge._debug = False
        bridge._on_crash = None
        bridge.state = SimpleNamespace(crashed=False)
        bridge.vh = SimpleNamespace(timer_device=timer)
        bridge.run_due_timers = run_due_timers
        bridge.calls = calls
        return bridge

    def test_start_sets_the_hook_and_stop_takes_it_away(self):
        bridge = self._bridge(_Timer(), [])
        bridge._emu = Mock()
        on_crash = Mock()
        bridge.start_idle_timers(on_crash=on_crash)
        bridge._emu.set_idle.assert_called_once_with(bridge._idle_timers)
        assert bridge._on_crash is on_crash
        bridge.stop_idle_timers()
        bridge._emu.set_idle.assert_called_with(None)

    def test_hook_returns_the_next_deadline_at_least_a_tick_away(self):
        bridge = self._bridge(_Timer(), [0.0, 5.0])
        # a request due again at once still yields a tick to the callers
        assert bridge._idle_timers() == _Timer.MIN_DELAY
        assert bridge._idle_timers() == 5.0
        # nothing pending: wait for a call
        assert bridge._idle_timers() is None
        assert len(bridge.calls) == 3

    def test_an_error_is_retried_and_shown_under_debug(self, capsys):
        from amifuse.fuse_fs import HandlerBridge

        bridge = self._bridge(_Timer(), [RuntimeError("boom")])
        bridge._debug = True
        assert bridge._idle_timers() == HandlerBridge._TIMER_RETRY
        out = capsys.readouterr()
        assert "error delivering timers" in out.out
        assert "RuntimeError: boom" in out.err

    def test_a_crashed_handler_is_reported_once(self):
        bridge = self._bridge(_Timer(), [])
        on_crash = Mock()
        bridge._on_crash = on_crash
        bridge.state.crashed = True
        assert bridge._idle_timers() is None
        assert bridge._idle_timers() is None
        on_crash.assert_called_once_with()
        assert bridge.calls == []

    def test_a_crash_while_delivering_is_reported(self):
        bridge = self._bridge(_Timer(), [])
        on_crash = Mock()
        bridge._on_crash = on_crash

        def crash():
            bridge.calls.append(1)
            bridge.state.crashed = True
            return None

        bridge.run_due_timers = crash
        assert bridge._idle_timers() is None
        on_crash.assert_called_once_with()
