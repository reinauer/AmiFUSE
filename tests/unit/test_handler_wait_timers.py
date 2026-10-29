"""Where the bridge waits on a handler: startup, packet waits and idle time.

- A handler that blocks in its init code before it replies to the startup
  packet, such as on its own timer, must not have that block recorded as
  its main loop (#159).
- A handler whose packet wait ran out is run on by the mount's timer
  thread while the mount idles, until it parks (#145).
- A packet wait that finds the handler parked at its main loop without a
  reply sleeps to a timer that falls due soon, so a reply that waits on
  the handler's own timer, or a child's, comes within the wait (#144).
"""

import math
import threading
import time
from types import SimpleNamespace

import pytest

from amifuse import fuse_fs as fs

TASK = 0x3000          # the handler's process
CHILD = 0x3800         # a child process of the handler
DOS_PORT = 0x6000      # the handler's pr_MsgPort
REPLY_PORT = 0x6100    # the bridge's caller reply port
TIMER_PORT = 0x6200    # the handler's timer reply port
MAIN_PC, MAIN_SP = 0x4000, 0x7000
REPLY = (0x1900, 0x1a00, 1, 0)  # (msg, pkt, res1, res2), as poll_replies returns it

# How HandlerLauncher.run_burst reports a burst that ended:
BUSY = SimpleNamespace(done=False, error=False, cycles=200_000)  # at its cycle limit
PARKED = SimpleNamespace(done=False, error=True, cycles=0)       # in Wait()

# The handler parked in its main-loop Wait().
MAIN_LOOP_WAIT = {"wait_blocked_sp": MAIN_SP - 4, "wait_blocked_mask": 0x101,
                  "wait_blocked_ret": MAIN_PC}


@pytest.fixture(autouse=True)
def _isolate_exec_state(monkeypatch):
    """Leave amitools' class-level Exec block state and signals alone."""
    from amitools.vamos.lib.lexec.signalfunc import SignalFunc

    monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)
    monkeypatch.setattr(SignalFunc, "_fallback_signals", 0)


class Clock:
    """The timer device's clock, which the bridge's sleeps move on.

    tick > 0 makes it read real time rounded down to a tick, as
    time.monotonic does on Windows before Python 3.13 (15.6 ms).
    """

    def __init__(self, tick=0.0):
        self.real = 100.0
        self.tick = tick
        self.sleeps = []

    @property
    def now(self):
        if not self.tick:
            return self.real
        return math.floor(self.real / self.tick) * self.tick

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.real += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(fs.time, "sleep", clock.sleep)
    return clock


class Timer:
    """The bridge's view of AmiFuseTimerDevice, with real deadlines on clock.

    requests are (delay, task, bits); rearm is a delay at which each
    delivered request is armed again, as a handler retrying on a timer does.
    """

    MIN_DELAY = 0.02

    def __init__(self, clock, requests=(), rearm=None):
        self.clock = clock
        self.pending = [(clock() + delay, task, bits) for delay, task, bits in requests]
        self.rearm = rearm
        self.event = threading.Event()
        self.deliveries = 0
        self.fast_forwards = 0

    def next_deadline(self):
        return min((deadline for deadline, _, _ in self.pending), default=math.inf)

    def deliver(self, fast_forward=False):
        self.fast_forwards += fast_forward
        now = self.clock()
        due = [req for req in self.pending if fast_forward or req[0] <= now]
        signals = {}
        for req in due:
            self.pending.remove(req)
            self.deliveries += 1
            signals[req[1]] = signals.get(req[1], 0) | req[2]
            if self.rearm is not None:
                self.pending.append((now + self.rearm, req[1], req[2]))
        return signals

    def delays(self, msg_addr):
        return False


class Mem:
    """Just the 32-bit words the bridge reads and writes, such as tc_SigRecvd."""

    def __init__(self):
        self.words = {}

    def r32(self, addr):
        return self.words.get(addr, 0)

    def w32(self, addr, val):
        self.words[addr] = val


def _bridge(timer, launcher, **state):
    """A HandlerBridge parked at its main loop, without an emulator."""
    from amitools.vamos.lib.lexec.PortManager import PortManager

    bridge = fs.HandlerBridge.__new__(fs.HandlerBridge)
    bridge._debug = False
    bridge._busy_ran_out = False
    bridge._idle_unfinished = False
    bridge._idle_busy_cycles = 0
    bridge._lock = threading.RLock()
    bridge._write_enabled = False
    bridge._closed = False
    bridge.launcher = launcher
    bridge.mem = Mem()
    bridge.state = SimpleNamespace(
        crashed=False, process_addr=TASK, port_addr=DOS_PORT,
        reply_port_addr=REPLY_PORT, pc=MAIN_PC, main_loop_pc=MAIN_PC,
        main_loop_sp=MAIN_SP, sp=MAIN_SP, block_state=dict(MAIN_LOOP_WAIT),
        wait_mask=0x101, regs=[0] * 16, initialized=True, run_state=None)
    for key, val in state.items():
        setattr(bridge.state, key, val)
    bridge.vh = SimpleNamespace(
        timer_device=timer, shutdown=lambda: None,
        machine=SimpleNamespace(cpu=SimpleNamespace(w_reg=lambda reg, val: None)),
        slm=SimpleNamespace(exec_impl=SimpleNamespace(port_mgr=PortManager(None))))
    bridge.backend = SimpleNamespace(sync=lambda: None, close=lambda: None)
    return bridge


class Launcher:
    """A handler whose bursts end as scripted; PARKED once the script ends.

    A parked burst leaves the handler in its main-loop Wait() with nothing
    pending, unless wake says it had a wake pending, such as a self-signal,
    which setup_resume_if_blocked then finds. reply_when, called after each
    burst, says whether the handler replied in it; polls counts the looks
    at the reply port.
    """

    last_pkt_type = None

    def __init__(self, outcomes=(), reply_when=lambda: False, wake=lambda burst: False):
        self.outcomes = list(outcomes)
        self.reply_when = reply_when
        self.wake = wake
        self.resumes = 0
        self.bursts = 0
        self.polls = 0
        self.queued = []

    def run_burst(self, state, max_cycles=0, debug=False):
        self.bursts += 1
        rs = self.outcomes.pop(0) if self.outcomes else PARKED
        state.run_state = rs
        if rs is PARKED:
            state.pc, state.sp = MAIN_PC, MAIN_SP
            state.block_state = dict(MAIN_LOOP_WAIT)
        else:
            state.pc, state.sp = 0x4800, MAIN_SP - 0x40
            state.block_state = None
        if self.reply_when():
            self.queued.append(REPLY)
        return rs

    def setup_resume_if_blocked(self, state, debug=False):
        if state.block_state is None or not self.wake(self.bursts):
            return False
        self.resumes += 1
        state.block_state = None
        return True

    def poll_replies(self, port_addr, debug=False):
        self.polls += 1
        replies, self.queued = self.queued, []
        return replies


# ---------------------------------------------------------------------------
# #159: the main loop is where the handler blocks after its startup reply
# ---------------------------------------------------------------------------


class TestStartupMainLoop:
    INIT_RET, INIT_SP = 0x5000, 0x7400     # a Wait in the handler's init code
    LOOP_RET, LOOP_SP = 0x5800, 0x7800     # its packet loop

    def _launcher(self):
        from amifuse.startup_runner import HandlerLauncher
        from amitools.vamos.lib.lexec.PortManager import PortManager

        launcher = HandlerLauncher.__new__(HandlerLauncher)
        launcher._debug = False
        pmgr = PortManager(None)
        for port in (DOS_PORT, REPLY_PORT, TIMER_PORT):
            pmgr.register_port(port)
        launcher.exec_impl = SimpleNamespace(port_mgr=pmgr)
        launcher.mem = None
        launcher.vh = SimpleNamespace(
            timer_device=None,
            machine=SimpleNamespace(cpu=SimpleNamespace(w_reg=lambda reg, val: None)),
            alloc=SimpleNamespace(get_mem=lambda: None))
        launcher._compute_pending_signals = lambda mask: 0
        return launcher

    def _state(self):
        return SimpleNamespace(
            port_addr=DOS_PORT, reply_port_addr=REPLY_PORT, block_state=None,
            pc=0x4400, sp=0x7c00, regs=[0] * 16, initialized=False, startup_replied=False,
            main_loop_pc=0, main_loop_sp=0, wait_mask=0)

    def _block(self, launcher, state, ret, sp, port=None, wait_mask=None):
        """A burst that blocks in WaitPort(port), or in Wait(wait_mask)."""
        if port is not None:
            block = {"waitport_blocked_sp": sp - 4, "waitport_blocked_port": port,
                     "waitport_blocked_ret": ret, "waitpkt_blocked": False}
        else:
            block = {"wait_blocked_sp": sp - 4, "wait_blocked_mask": wait_mask,
                     "wait_blocked_ret": ret}
        state.block_state = block
        launcher._finalize_burst_blocked(
            state, block, sp - 4, ret, wait_mask,
            sp - 4 if port is not None else None, 0x100000)

    @pytest.mark.parametrize("init_wait", ["WaitPort", "Wait"])
    def test_a_wait_before_the_startup_reply_is_not_the_main_loop(self, init_wait):
        """An init-time wait on the handler's timer leaves the main loop unset.

        Its packet loop, where it blocks once its startup reply is on the
        reply port, becomes the main loop, with its own Wait() mask.
        """
        launcher = self._launcher()
        state = self._state()
        if init_wait == "WaitPort":
            self._block(launcher, state, self.INIT_RET, self.INIT_SP, port=TIMER_PORT)
        else:
            self._block(launcher, state, self.INIT_RET, self.INIT_SP, wait_mask=0x10)
        assert (state.main_loop_pc, state.main_loop_sp) == (0, 0)
        assert state.wait_mask == 0
        # Still initialized, as before: the bridge keeps its block state
        # across child runs, and captures its loop if it never replies.
        assert state.initialized
        # It resumes from the init wait, not from a main loop.
        assert (state.pc, state.sp) == (self.INIT_RET, self.INIT_SP)

        launcher.exec_impl.port_mgr.put_msg(REPLY_PORT, 0x1900)  # the startup reply
        self._block(launcher, state, self.LOOP_RET, self.LOOP_SP, wait_mask=0x100)
        assert (state.main_loop_pc, state.main_loop_sp) == (self.LOOP_RET, self.LOOP_SP)
        assert state.wait_mask == 0x100

    def test_block_after_the_polled_startup_reply_is_the_main_loop(self):
        """The bridge took the reply off already, and a wake is pending at the block."""
        launcher = self._launcher()
        state = self._state()
        state.startup_replied = True
        launcher._compute_pending_signals = lambda mask: mask  # it runs on at once
        launcher._clear_signals_from_task = lambda signals: None
        self._block(launcher, state, self.LOOP_RET, self.LOOP_SP, wait_mask=0x100)
        assert (state.main_loop_pc, state.main_loop_sp) == (self.LOOP_RET, self.LOOP_SP)

    def test_startup_wait_notes_the_startup_reply(self):
        """_run_startup_until_replies sets the flag when it polls the reply."""
        launcher = Launcher(reply_when=lambda: True)
        bridge = _bridge(None, launcher, initialized=False, startup_replied=False)
        assert bridge._run_startup_until_replies(cycles=10_000, max_iters=5) == [REPLY]
        assert bridge.state.startup_replied is True

    def test_main_loop_is_kept_after_startup(self):
        """Once recorded, the main loop stays, as before."""
        launcher = self._launcher()
        state = self._state()
        launcher.exec_impl.port_mgr.put_msg(REPLY_PORT, 0x1900)
        self._block(launcher, state, self.LOOP_RET, self.LOOP_SP, port=DOS_PORT)
        self._block(launcher, state, self.INIT_RET, self.INIT_SP, port=TIMER_PORT)
        assert (state.main_loop_pc, state.main_loop_sp) == (self.LOOP_RET, self.LOOP_SP)


# ---------------------------------------------------------------------------
# #145: the timer thread runs on a handler whose packet wait ran out
# ---------------------------------------------------------------------------


class TestBusyHandlerWhileIdle:
    def test_busy_handler_runs_on_until_it_parks(self, clock):
        """It runs from where the wait left it, and its late reply stays queued."""
        launcher = Launcher([BUSY] * 3)
        bridge = _bridge(Timer(clock), launcher, pc=0x4800, block_state=None)
        bridge._busy_ran_out = True
        assert bridge.run_due_timers() is None  # parked, nothing pending
        assert launcher.bursts == 4
        assert bridge._busy_ran_out is False
        assert (bridge.state.pc, bridge.state.sp) == (MAIN_PC, MAIN_SP)
        assert launcher.polls == 0

    def test_busy_handler_runs_in_slices(self, clock, monkeypatch):
        """Each look runs it for _SHORT_BUSY_CYCLES, then asks for another at once."""
        monkeypatch.setattr(fs.HandlerBridge, "_SHORT_BUSY_CYCLES", 3 * 200_000)
        launcher = Launcher([BUSY] * 5)
        bridge = _bridge(Timer(clock), launcher, pc=0x4800, block_state=None)
        bridge._busy_ran_out = True
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert launcher.bursts == 3
        assert bridge._busy_ran_out is True
        assert bridge.run_due_timers() is None
        assert launcher.bursts == 6
        assert bridge._busy_ran_out is False

    def test_parked_handler_then_gets_its_due_timers(self, clock):
        """Parked at its main loop, it is woken for a timer due meanwhile."""
        timer = Timer(clock, [(0.0, TASK, 0x1)])
        launcher = Launcher([BUSY])
        bridge = _bridge(timer, launcher, pc=0x4800, block_state=None)
        bridge._busy_ran_out = True
        assert bridge.run_due_timers() is None
        assert timer.deliveries == 1
        assert launcher.bursts == 3  # busy, parks; the timer's resume, parks

    def test_handler_caught_in_a_loop_is_left_after_the_busy_limit(self, clock, monkeypatch):
        """After _MAX_BUSY_CYCLES of slices it waits for the next FUSE operation."""
        monkeypatch.setattr(fs.HandlerBridge, "_SHORT_BUSY_CYCLES", 2 * 200_000)
        monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES", 4 * 200_000)
        launcher = Launcher([BUSY] * 100)
        bridge = _bridge(Timer(clock), launcher, pc=0x4800, block_state=None)
        bridge._busy_ran_out = True
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert launcher.bursts == 4
        assert bridge.run_due_timers() is None
        assert launcher.bursts == 4
        # A pending timer makes the thread look again later.
        bridge.vh.timer_device.pending.append((clock() + 5, TASK, 0x1))
        assert bridge.run_due_timers() == bridge._TIMER_RETRY
        assert launcher.bursts == 4

    def test_slice_keeps_its_limit_when_the_handler_parks_with_a_wake(self, clock, monkeypatch):
        """A park with a wake pending, mid-work, does not lift the slice's limit."""
        monkeypatch.setattr(fs.HandlerBridge, "_SHORT_BUSY_CYCLES", 3 * 200_000)
        launcher = Launcher([BUSY, PARKED] + [BUSY] * 10, wake=lambda burst: burst == 2)
        bridge = _bridge(Timer(clock), launcher, pc=0x4800, block_state=None)
        bridge._busy_ran_out = True
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert launcher.resumes == 1
        assert launcher.bursts == 4  # busy, parks with a wake, busy twice
        # It waited, so the next packet gets the full limit; the thread runs it on.
        assert bridge._busy_ran_out is False
        assert bridge._idle_unfinished is True

    def test_handler_that_keeps_waking_itself_is_run_on(self, clock):
        """A slice that ends at max_iters parks, each with a wake, leaves it running."""
        launcher = Launcher([BUSY] + [PARKED] * 60, wake=lambda burst: burst <= 55)
        bridge = _bridge(Timer(clock), launcher, pc=0x4800, block_state=None)
        bridge._busy_ran_out = True
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert launcher.bursts == 51
        assert bridge._busy_ran_out is False  # not caught in a busy loop
        assert bridge._idle_unfinished is True
        assert bridge.run_due_timers() is None
        assert launcher.bursts == 56  # it parked with nothing pending
        assert bridge._idle_unfinished is False

    def test_handler_kept_awake_by_a_stray_reply_is_drained(self, clock):
        """A reply no one takes is drained after a run that leaves it awake."""
        launcher = Launcher([BUSY] + [PARKED] * 60, wake=lambda burst: True)
        bridge = _bridge(Timer(clock), launcher, pc=0x4800, block_state=None)
        pmgr = bridge.vh.slm.exec_impl.port_mgr
        pmgr.register_port(0x6300)
        pmgr.put_msg(0x6300, 0x1800)  # another device's reply
        bridge._busy_ran_out = True
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert not pmgr.has_msg(0x6300)

    def test_waking_itself_uses_up_the_idle_budget(self, clock, monkeypatch):
        """Each look that leaves it awake costs a slice, so the thread stops."""
        monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES",
                            2 * fs.HandlerBridge._SHORT_BUSY_CYCLES)
        launcher = Launcher([PARKED] * 1000, wake=lambda burst: True)
        bridge = _bridge(Timer(clock), launcher, pc=0x4800, block_state=None)
        bridge._idle_unfinished = True
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert bridge.run_due_timers() is None
        assert launcher.bursts == 100

    def test_packet_wait_takes_the_handler_over(self, clock):
        bridge = _bridge(Timer(clock), Launcher(reply_when=lambda: True))
        bridge._idle_unfinished = True
        assert bridge._run_until_replies() == [REPLY]
        assert bridge._idle_unfinished is False

    def test_timer_resume_that_runs_out_uses_the_idle_budget(self, clock, monkeypatch):
        """Its cycles count, so a handler caught in a loop is left at once."""
        monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES", 2 * 200_000)
        timer = Timer(clock, [(0.0, TASK, 0x1)])
        launcher = Launcher([BUSY] * 100)
        bridge = _bridge(timer, launcher)
        assert bridge.run_due_timers() == Timer.MIN_DELAY
        assert launcher.bursts == 2
        assert bridge._busy_ran_out is True
        assert bridge.run_due_timers() is None
        assert launcher.bursts == 2

    def test_wait_that_runs_out_wakes_the_timer_thread(self, clock, monkeypatch):
        """It gets the thread's full budget, whatever an earlier episode used."""
        monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES", 2 * 200_000)
        timer = Timer(clock)
        bridge = _bridge(timer, Launcher([BUSY] * 100), pc=0x4800, block_state=None)
        bridge._idle_busy_cycles = 2 * 200_000
        assert bridge._run_until_replies() == []
        assert bridge._busy_ran_out is True
        assert timer.event.is_set()
        assert bridge._idle_busy_cycles == 0

    def test_idle_mount_runs_a_busy_handler_until_it_parks(self):
        """The mount's timer thread does it with no FUSE operation and no timer."""
        launcher = Launcher([BUSY] * 3)
        bridge = _bridge(Timer(time.monotonic), launcher, pc=0x4800, block_state=None)
        bridge._busy_ran_out = True
        mount = fs.AmigaFuseFS(bridge)
        mount.init("/")
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with bridge._lock:
                    if not bridge._busy_ran_out:
                        break
                time.sleep(0.01)
            assert bridge._busy_ran_out is False
            assert launcher.bursts == 4
        finally:
            mount.destroy("/")


# ---------------------------------------------------------------------------
# #144: a packet wait sleeps to a timer the handler's reply waits on
# ---------------------------------------------------------------------------


class TestPacketWaitSleepsToTimer:
    @pytest.mark.parametrize("owner", [TASK, CHILD], ids=["handler", "child"])
    def test_reply_that_waits_on_a_timer_comes_within_the_wait(self, clock, owner):
        """The handler, or its child, replies only once its 0.3 s timer fires."""
        timer = Timer(clock, [(0.3, owner, 0x1)])
        launcher = Launcher(reply_when=lambda: timer.deliveries > 0)
        bridge = _bridge(timer, launcher)
        assert bridge._run_until_replies() == [REPLY]
        assert clock.sleeps == [pytest.approx(0.3)]
        assert timer.fast_forwards == 0

    def test_coarse_clock_still_delivers_within_the_wait(self, monkeypatch):
        """A sleep that ends before the clock's next tick does not lose the timer."""
        clock = Clock(tick=1 / 64)
        monkeypatch.setattr(fs.time, "sleep", clock.sleep)
        timer = Timer(clock, [(0.3, TASK, 0x1)])
        bridge = _bridge(timer, Launcher(reply_when=lambda: timer.deliveries > 0))
        assert bridge._run_until_replies() == [REPLY]
        assert len(clock.sleeps) > 1

    # The handler parked in its main-loop WaitPort() on its DOS port.
    MAIN_LOOP_WAITPORT = {"waitport_blocked_sp": MAIN_SP - 4,
                          "waitport_blocked_port": DOS_PORT,
                          "waitport_blocked_ret": MAIN_PC, "waitpkt_blocked": False}

    @pytest.mark.parametrize("children", [False, True],
                             ids=["its timer replies to the DOS port", "a child's timer"])
    def test_waitport_main_loop_gets_a_reply_that_waits_on_a_timer(self, clock, children):
        """A handler in WaitPort() on its DOS port is slept for too.

        Its own timer request may reply to that port, which wakes it; or a
        child replies once its own timer fired.
        """
        timer = Timer(clock, [(0.3, CHILD if children else TASK, 0x1)])
        launcher = Launcher(reply_when=lambda: not children and timer.deliveries > 0)
        run_burst = launcher.run_burst

        def in_waitport(state, max_cycles=0, debug=False):
            rs = run_burst(state, max_cycles, debug)
            state.block_state = dict(self.MAIN_LOOP_WAITPORT)
            return rs

        launcher.run_burst = in_waitport
        bridge = _bridge(timer, launcher, block_state=dict(self.MAIN_LOOP_WAITPORT))
        if children:
            def run_children(cycles_per_child):
                # The child replies once its timer was delivered.
                if timer.deliveries and not launcher.polls_since_reply:
                    launcher.queued.append(REPLY)
                    launcher.polls_since_reply = 1
                return 1

            launcher.polls_since_reply = 0
            bridge.proc_mgr = SimpleNamespace(
                processes={CHILD: SimpleNamespace(is_child=True, exited=False, port_addr=0x6400)},
                run_all_ready_children=run_children)
        assert bridge._run_until_replies() == [REPLY]
        assert clock.sleeps == [pytest.approx(0.3)]

    def test_timer_beyond_the_wait_is_not_slept_to(self, clock):
        timer = Timer(clock, [(fs.HandlerBridge._PACKET_TIMER_WAIT + 0.5, TASK, 0x1)])
        bridge = _bridge(timer, Launcher())
        assert bridge._run_until_replies() == []
        assert clock.sleeps == []
        assert timer.deliveries == 0

    def test_rearmed_timer_ends_the_wait_at_its_time_limit(self, clock):
        """A handler that re-arms its timer and never replies costs the time limit."""
        timer = Timer(clock, [(0.25, TASK, 0x1)], rearm=0.25)
        bridge = _bridge(timer, Launcher())
        start = clock()
        assert bridge._run_until_replies() == []
        # Due at 0.25, 0.5, 0.75 and 1.0 s; the next is past the limit.
        assert clock.sleeps == [0.25] * 4
        assert clock() - start == fs.HandlerBridge._PACKET_TIMER_WAIT == 1.0

    def test_handler_parked_elsewhere_is_not_slept_for(self, clock):
        """Away from its main loop its timers fast-forward, as before."""
        timer = Timer(clock, [(0.3, TASK, 0x1)], rearm=0.3)
        launcher = Launcher()
        bridge = _bridge(timer, launcher)
        # Every burst parks in a Wait() deeper than the main loop.
        run_burst = launcher.run_burst

        def deeper(state, max_cycles=0, debug=False):
            rs = run_burst(state, max_cycles, debug)
            state.pc = 0x4900
            return rs

        launcher.run_burst = deeper
        assert bridge._run_until_replies() == []
        assert clock.sleeps == []
        assert timer.fast_forwards > 0
