"""How long the bridge waits for a handler's reply.

Each run gives the handler and its children _RUN_CHUNK_CYCLES cycles, or
until every task waits. A run that ends with a task still runnable (BUSY)
must not end the wait, and the reply can be thousands of runs away, yet a
handler caught in a loop must still fail the packet, and must not make
every later packet wait as long again. A run after which every task waits
(IDLE) ends the wait unless a timer request falls due soon: only time can
bring the reply then.
"""

import math
import threading
from itertools import chain, repeat
from types import SimpleNamespace

import pytest

from amitools.vamos.schedule import RunResult

from amifuse import fuse_fs as fs

BUSY = RunResult.BUSY    # the cycle budget ran out with a task still runnable
IDLE = RunResult.IDLE    # every task waits
DONE = RunResult.DONE    # no task is left: the handler exited

REPLY = (0x1000, 0x2000, 65536, 0)  # (msg, pkt, res1, res2), as poll_replies returns it
FLUSH_PKT = 0x2100

CHUNK = fs.HandlerBridge._RUN_CHUNK_CYCLES
MAX_RUNS = fs.HandlerBridge._MAX_BUSY_CYCLES // CHUNK
SHORT_RUNS = fs.HandlerBridge._SHORT_BUSY_CYCLES // CHUNK


class ScriptedLauncher:
    """A handler whose runs end as scripted, replying in some of them."""

    def __init__(self, outcomes, reply_in=None, replies=None):
        self.outcomes = iter(outcomes)
        # The reply posted in each run: REPLY in run reply_in.
        self.replies = dict(replies or {})
        if reply_in is not None:
            self.replies[reply_in] = REPLY
        self.runs = 0
        self.cycles = []
        self.debug_polls = 0
        self.queued = []
        self.last_pkt_type = None

    def run(self, max_cycles=None):
        self.runs += 1
        self.cycles.append(max_cycles)
        if self.runs in self.replies:
            self.queued.append(self.replies[self.runs])
        return next(self.outcomes)

    def poll_replies(self, port_addr, debug=False):
        self.debug_polls += debug
        replies, self.queued = self.queued, []
        return replies

    def send_flush(self, state):
        return FLUSH_PKT, FLUSH_PKT - 0x1000


class FakeTimer:
    """The runtime's timer.device, as the bridge sees it."""

    MIN_DELAY = 0.02

    def __init__(self, deadline=math.inf, delay=math.inf):
        self.now = 100.0
        # the deadline of a timer request, and the end of a task's Delay()
        self.deadline = deadline
        self.delay = delay
        self.deliveries = 0
        self.delays_ended = 0
        self.event = threading.Event()

    def clock(self):
        return self.now

    def next_deadline(self):
        return min(self.deadline, self.delay)

    def next_delay(self):
        return self.delay

    def deliver(self, fast_forward=False):
        signals = {}
        if fast_forward or self.delay <= self.now:
            if self.delay != math.inf:
                self.delays_ended += 1
                signals[0x3000] = 0x10
            self.delay = math.inf
        if fast_forward or self.deadline <= self.now:
            if self.deadline != math.inf:
                self.deliveries += 1
                signals[0x3000] = signals.get(0x3000, 0) | 0x10000
            self.deadline = math.inf
        return signals


class Sleeps(list):
    """The pauses recorded instead of slept; .timers lists the clocks they move."""

    def __init__(self):
        super().__init__()
        self.timers = []


@pytest.fixture
def sleeps(monkeypatch):
    """The pauses _run_until_replies takes, recorded instead of slept.

    A sleep moves the fake timer's clock on, when the bridge has one.
    """
    calls = Sleeps()

    def sleep(seconds):
        calls.append(seconds)
        for timer in calls.timers:
            timer.now += seconds

    monkeypatch.setattr(fs.time, "sleep", sleep)
    return calls


def _bridge(launcher, timer=None):
    bridge = fs.HandlerBridge.__new__(fs.HandlerBridge)
    bridge._debug = False
    bridge._busy_ran_out = False
    bridge._idle_busy_cycles = 0
    bridge._lock = threading.RLock()
    bridge.launcher = launcher
    bridge.backend = SimpleNamespace(sync=lambda: None, fsync=lambda: None)
    bridge.state = SimpleNamespace(crashed=False, exited=False, reply_port_addr=0x3000)
    # The reply port holds what the handler has posted and not been polled.
    port_mgr = SimpleNamespace(has_msg=lambda port_addr: bool(launcher.queued))
    bridge.vh = SimpleNamespace(
        slm=SimpleNamespace(exec_impl=SimpleNamespace(port_mgr=port_mgr)),
        timer_device=timer)
    bridge._drain_non_essential_ports = lambda port_mgr: None
    return bridge


def test_busy_handler_gets_its_reply_after_many_runs(sleeps):
    # pfs3aio's first write after a 2 GiB file: 58 runs with the handler
    # still working, then the run in which it replies and waits.
    launcher = ScriptedLauncher(chain(repeat(BUSY, 58), repeat(IDLE)), reply_in=59)
    assert _bridge(launcher)._run_until_replies() == [REPLY]
    assert launcher.runs == 59
    # A working handler runs again at once.
    assert sleeps == []


def test_longest_wait_seen_succeeds_when_no_wait_ran_out_before(sleeps):
    # pfs3aio reads the next packet after freeing the blocks of a deleted
    # 2 GiB file: 13,990 runs of 200,000 cycles, far beyond the short limit.
    launcher = ScriptedLauncher(chain(repeat(BUSY, 13_989), repeat(IDLE)),
                                reply_in=13_990)
    assert _bridge(launcher)._run_until_replies(cycles=200_000) == [REPLY]
    assert launcher.runs == 13_990
    assert launcher.cycles[0] == 200_000


@pytest.mark.parametrize("cycles", [200_000, 2_000_000])
def test_handler_that_never_finishes_fails_after_the_busy_cycle_limit(sleeps, cycles):
    launcher = ScriptedLauncher(repeat(BUSY))
    assert _bridge(launcher)._run_until_replies(cycles=cycles) == []
    assert launcher.runs == fs.HandlerBridge._MAX_BUSY_CYCLES // cycles
    assert sleeps == []


@pytest.mark.parametrize("pkt_type, to", [
    (8, " to ACTION_LOCATE_OBJECT (8)"),
    (1234, " to packet type 1234"),
    (None, ""),
], ids=["named type", "unnamed type", "no packet sent"])
def test_running_out_of_the_busy_cycle_limit_warns_on_stderr(sleeps, capsys, pkt_type, to):
    launcher = ScriptedLauncher(repeat(BUSY))
    launcher.last_pkt_type = pkt_type
    assert _bridge(launcher)._run_until_replies() == []
    assert capsys.readouterr().err == (
        f"[amifuse] WARNING: handler did not reply{to} within the busy limit "
        f"of 10,000,000,000 emulated cycles\n")


def test_after_a_wait_ran_out_later_waits_get_the_short_limit(sleeps, capsys):
    # A handler caught in a loop: each later packet resumes the same loop.
    launcher = ScriptedLauncher(repeat(BUSY))
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    assert launcher.runs == MAX_RUNS
    assert "WARNING" in capsys.readouterr().err
    for _ in range(2):
        before = launcher.runs
        assert bridge._run_until_replies() == []
        assert launcher.runs - before == SHORT_RUNS
    assert sleeps == []
    # Only the full limit warns, once for each time it runs out.
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("stop", [IDLE, DONE], ids=["waits", "exits"])
def test_a_run_in_which_the_handler_waits_or_exits_restores_the_full_limit(sleeps, stop):
    # The first wait runs out. The next finds the handler stopped, and
    # ends. In the one after, it works for 100 runs, ten times the short
    # limit, and replies.
    launcher = ScriptedLauncher(
        chain(repeat(BUSY, MAX_RUNS), [stop], repeat(BUSY, 100), repeat(IDLE)),
        reply_in=MAX_RUNS + 1 + 100 + 1)
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    assert bridge._busy_ran_out
    assert bridge._run_until_replies() == []
    assert not bridge._busy_ran_out
    assert bridge._run_until_replies() == [REPLY]
    assert launcher.runs == MAX_RUNS + 102


def test_a_late_reply_restores_the_full_limit(sleeps):
    # A handler that is only slow: the first wait runs out while it works
    # on packet A. It answers A 10 runs into the wait for packet B and
    # goes straight on to B, without waiting, for 100 runs, ten times the
    # short limit.
    a_reply = (0x1000, 0x2000, 1, 0)
    b_reply = (0x1050, 0x2050, 1, 0)
    launcher = ScriptedLauncher(chain(repeat(BUSY, MAX_RUNS + 109), repeat(IDLE)),
                                replies={MAX_RUNS + 10: a_reply, MAX_RUNS + 110: b_reply})
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    assert bridge._run_until_reply(0x2050, "B") == (1, 0)
    assert launcher.runs == MAX_RUNS + 110


def test_run_until_reply_passes_its_wait_arguments_on(sleeps):
    # format_volume waits for its packets with a cycles of its own, such as
    # 2,000,000 for ACTION_FORMAT. A reply to another packet in run 1
    # starts a second round, which must get the same arguments.
    launcher = ScriptedLauncher(repeat(IDLE), replies={1: (0x1050, 0x2050, 1, 0)})
    assert _bridge(launcher)._run_until_reply(0x2000, "format", cycles=300_000) is None
    # The second round finds the handler idle with no reply, and ends.
    assert launcher.runs == 2
    assert launcher.cycles == [300_000, 300_000]
    launcher = ScriptedLauncher(repeat(BUSY))
    assert _bridge(launcher)._run_until_reply(0x2000, "format", cycles=2_000_000) is None
    # The busy limit counts the cycles of each run: 5,000 of 2,000,000.
    assert launcher.runs == fs.HandlerBridge._MAX_BUSY_CYCLES // 2_000_000


def test_flush_gets_the_full_limit_when_no_wait_ran_out(sleeps):
    # FastFileSystem answers ACTION_FLUSH on a volume it is validating only
    # once it is done: after 2,714 runs on a 3.2 GB partition.
    launcher = ScriptedLauncher(chain(repeat(BUSY, 2_714), repeat(IDLE)),
                                replies={2_715: (0x1100, FLUSH_PKT, -1, 0)})
    _bridge(launcher).flush_volume()
    assert launcher.runs == 2_715


@pytest.mark.parametrize("flush_res1, acknowledged", [(-1, True), (0, False)],
                         ids=["DOSTRUE", "DOSFALSE"])
def test_flush_waits_for_its_own_reply_after_a_late_one(sleeps, flush_res1, acknowledged):
    # A handler that is only slow: a write's wait ran out while it worked.
    # It answers the write 10 runs into the flush's wait, then works on
    # the flush for 1000 runs, beyond the short limit. Only the flush's
    # own reply says whether the handler wrote its buffers.
    write_reply = (0x1000, 0x2000, 65536, 0)
    launcher = ScriptedLauncher(
        chain(repeat(BUSY, MAX_RUNS + 1_009), repeat(IDLE)),
        replies={MAX_RUNS + 10: write_reply,
                 MAX_RUNS + 1_010: (0x1100, FLUSH_PKT, flush_res1, 0)})
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    if acknowledged:
        bridge.flush_volume()
    else:
        with pytest.raises(OSError, match="ACTION_FLUSH"):
            bridge.flush_volume()
    assert launcher.runs == MAX_RUNS + 1_010


def test_flush_to_a_handler_still_looping_fails_after_the_short_limit(sleeps, capsys):
    launcher = ScriptedLauncher(repeat(BUSY))
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    capsys.readouterr()
    with pytest.raises(OSError, match="ACTION_FLUSH"):
        bridge.flush_volume()
    assert launcher.runs == MAX_RUNS + SHORT_RUNS
    assert capsys.readouterr().err == ""


def test_debug_reports_every_1000th_busy_run(sleeps, capsys):
    # A long wait would otherwise print a poll line for each busy run.
    launcher = ScriptedLauncher(chain(repeat(BUSY, 2500), repeat(IDLE)), reply_in=2501)
    bridge = _bridge(launcher)
    bridge._debug = True
    assert bridge._run_until_replies() == [REPLY]
    # Busy runs 1000 and 2000, and the run that replies.
    assert launcher.debug_polls == 3
    busy_lines = [line for line in capsys.readouterr().out.splitlines()
                  if "busy for" in line]
    assert busy_lines == [
        "[amifuse] _run_until_replies: busy for 1,000,000,000 cycles",
        "[amifuse] _run_until_replies: busy for 2,000,000,000 cycles",
    ]


def test_debug_shows_the_poll_of_a_reply_in_a_busy_run(sleeps):
    # pfs3aio replies in the middle of a run and goes on working, so the
    # run ends busy: here run 1500, which is not a 1000th.
    launcher = ScriptedLauncher(repeat(BUSY), reply_in=1500)
    bridge = _bridge(launcher)
    bridge._debug = True
    assert bridge._run_until_replies() == [REPLY]
    # The poll of busy run 1000, and of run 1500 with the reply.
    assert launcher.debug_polls == 2


def test_idle_handler_without_a_timer_fails_at_once(sleeps, capsys):
    # Every task waits and nothing will wake one: no reply is coming.
    launcher = ScriptedLauncher(repeat(IDLE))
    assert _bridge(launcher)._run_until_replies() == []
    assert launcher.runs == 1
    assert sleeps == []
    # Only the busy cycle limit warns; an idle handler fails quietly.
    assert capsys.readouterr().err == ""


def test_exited_handler_fails_at_once(sleeps):
    launcher = ScriptedLauncher(repeat(DONE))
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    assert launcher.runs == 1
    assert sleeps == []
    assert not bridge._busy_ran_out


def test_reply_that_waits_on_a_timer_comes_within_the_wait(sleeps):
    # The handler waits on its own timer before it replies: the wait sleeps
    # to the deadline, replies to the request, and runs the handler on.
    timer = FakeTimer(deadline=100.3)
    sleeps.timers.append(timer)
    launcher = ScriptedLauncher(repeat(IDLE), reply_in=2)
    assert _bridge(launcher, timer)._run_until_replies() == [REPLY]
    assert launcher.runs == 2
    assert sleeps == [pytest.approx(0.3)]
    assert timer.deliveries == 1


def test_timer_beyond_the_wait_is_not_slept_to(sleeps):
    timer = FakeTimer(deadline=100.0 + fs.HandlerBridge._PACKET_TIMER_WAIT + 1)
    sleeps.timers.append(timer)
    launcher = ScriptedLauncher(repeat(IDLE))
    assert _bridge(launcher, timer)._run_until_replies() == []
    assert launcher.runs == 1
    assert sleeps == []
    assert timer.deliveries == 0


def test_due_timer_is_replied_before_the_run(sleeps):
    # A request due when the wait starts is replied first, so that its
    # signal wakes the handler with the packet's.
    timer = FakeTimer(deadline=99.0)
    launcher = ScriptedLauncher(repeat(IDLE), reply_in=1)
    assert _bridge(launcher, timer)._run_until_replies() == [REPLY]
    assert timer.deliveries == 1
    assert launcher.runs == 1


@pytest.mark.parametrize("ticks", [10, 100, 250])
def test_a_delay_beyond_the_wait_is_slept_to(sleeps, ticks):
    # The handler calls Delay() while it serves the packet: a Delay()
    # always ends, so the wait sleeps to it however long it is, and the
    # reply comes afterwards.
    timer = FakeTimer(delay=100.0 + ticks / 50)
    sleeps.timers.append(timer)
    launcher = ScriptedLauncher(repeat(IDLE), reply_in=2)
    assert _bridge(launcher, timer)._run_until_replies() == [REPLY]
    assert launcher.runs == 2
    assert sleeps == [pytest.approx(ticks / 50)]
    assert timer.delays_ended == 1
    assert timer.deliveries == 0


def test_a_poll_loop_of_short_delays_is_not_counted_as_idle(sleeps):
    # SmartFilesystem polls for its helper with short Delay()s: each one
    # is slept to, and none of them counts against the idle iterations.
    timer = FakeTimer(delay=100.02)
    sleeps.timers.append(timer)
    runs = 80

    class Polling(ScriptedLauncher):
        def run(self, max_cycles=None):
            result = super().run(max_cycles)
            if self.runs < runs:
                timer.delay = timer.now + 0.02  # the next Delay(1)
            return result

    launcher = Polling(repeat(IDLE), reply_in=runs)
    assert _bridge(launcher, timer)._run_until_replies(max_iters=50) == [REPLY]
    assert launcher.runs == runs
    assert len(sleeps) == runs - 1
    assert sum(sleeps) == pytest.approx(0.02 * (runs - 1))


def test_a_timer_due_before_the_delay_is_replied_first(sleeps):
    # A child waits on a timer request while the handler delays: the wait
    # sleeps to the request, replies to it and runs on, then to the delay.
    timer = FakeTimer(deadline=100.3, delay=102.0)
    sleeps.timers.append(timer)
    launcher = ScriptedLauncher(repeat(IDLE), reply_in=3)
    assert _bridge(launcher, timer)._run_until_replies() == [REPLY]
    assert launcher.runs == 3
    assert sleeps == [pytest.approx(0.3), pytest.approx(1.7)]
    assert timer.deliveries == 1
    assert timer.delays_ended == 1


def test_a_deadline_cuts_the_sleep_to_a_delay_short(sleeps, monkeypatch):
    # The wait's deadline, on the monotonic clock, still ends it.
    timer = FakeTimer(delay=105.0)
    sleeps.timers.append(timer)
    now = [100.0]
    monkeypatch.setattr(fs.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        timer.now += seconds

    monkeypatch.setattr(fs.time, "sleep", sleep)
    launcher = ScriptedLauncher(repeat(IDLE), reply_in=2)
    bridge = _bridge(launcher, timer)
    assert bridge._run_until_replies(deadline=101.0) == []
    assert launcher.runs == 1
    assert sleeps == [pytest.approx(1.0)]
    assert timer.delays_ended == 0
    # the handler waits, so it is not taken for busy
    assert not bridge._busy_ran_out


def _fake_clocks(sleeps, timer, monkeypatch):
    """Move the monotonic clock and the timer's together, with each pause."""
    sleeps.timers.append(timer)
    now = [100.0]
    monkeypatch.setattr(fs.time, "monotonic", lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds
        timer.now += seconds

    monkeypatch.setattr(fs.time, "sleep", sleep)


class DelayingForEver(ScriptedLauncher):
    """A handler that never replies, beside a task that always delays again."""

    def __init__(self, timer):
        super().__init__(repeat(IDLE))
        self.timer = timer

    def run(self, max_cycles=None):
        result = super().run(max_cycles)
        self.timer.delay = self.timer.now + 1.0
        return result


def test_a_wait_gives_up_on_delays_that_never_end(sleeps, monkeypatch):
    # Nothing else ends this wait, and it holds the emulation thread: every
    # other FUSE call and the unmount's close() queue behind it.
    limit = fs.HandlerBridge._DELAY_WAIT
    timer = FakeTimer()
    _fake_clocks(sleeps, timer, monkeypatch)
    launcher = DelayingForEver(timer)
    bridge = _bridge(launcher, timer)
    assert bridge._run_until_replies() == []
    assert sum(sleeps) == pytest.approx(limit)
    assert launcher.runs == limit + 1
    # the handler waits, so it is not taken for busy
    assert not bridge._busy_ran_out


def test_the_delay_limit_cuts_a_long_delay_short(sleeps, monkeypatch):
    limit = fs.HandlerBridge._DELAY_WAIT
    timer = FakeTimer(delay=100.0 + limit + 5)
    _fake_clocks(sleeps, timer, monkeypatch)
    launcher = ScriptedLauncher(repeat(IDLE))
    assert _bridge(launcher, timer)._run_until_replies() == []
    assert launcher.runs == 2
    assert sleeps == [pytest.approx(limit)]
    assert timer.delays_ended == 0


def test_a_wait_with_a_later_deadline_keeps_it_past_the_delay_limit(sleeps, monkeypatch):
    # The caller's deadline decides, not the limit for a wait without one.
    wait = fs.HandlerBridge._DELAY_WAIT * 2
    timer = FakeTimer()
    _fake_clocks(sleeps, timer, monkeypatch)
    launcher = DelayingForEver(timer)
    assert _bridge(launcher, timer)._run_until_replies(deadline=100.0 + wait) == []
    assert sum(sleeps) == pytest.approx(wait)


# ---------------------------------------------------------------------------
# The idle hook runs a handler whose wait ran out (run_due_timers)
# ---------------------------------------------------------------------------


def test_idle_hook_runs_a_busy_handler_on_until_it_waits(sleeps):
    timer = FakeTimer()
    launcher = ScriptedLauncher([BUSY, BUSY, IDLE])
    bridge = _bridge(launcher, timer)
    bridge._busy_ran_out = True
    assert bridge.run_due_timers() == FakeTimer.MIN_DELAY
    assert bridge.run_due_timers() == FakeTimer.MIN_DELAY
    assert bridge.run_due_timers() is None
    assert launcher.runs == 3
    assert launcher.cycles == [fs.HandlerBridge._SHORT_BUSY_CYCLES] * 3
    # it waited: the next packet gets the full limit again
    assert not bridge._busy_ran_out
    assert bridge._idle_busy_cycles == 0


def test_handler_caught_in_a_loop_is_left_after_the_busy_limit(sleeps, monkeypatch):
    """After _MAX_BUSY_CYCLES of idle runs it waits for the next packet."""
    monkeypatch.setattr(fs.HandlerBridge, "_SHORT_BUSY_CYCLES", 2 * CHUNK)
    monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES", 4 * CHUNK)
    timer = FakeTimer()
    launcher = ScriptedLauncher(repeat(BUSY))
    bridge = _bridge(launcher, timer)
    bridge._busy_ran_out = True
    assert bridge.run_due_timers() == FakeTimer.MIN_DELAY
    assert bridge.run_due_timers() == FakeTimer.MIN_DELAY
    assert launcher.runs == 2
    # the limit is reached: no more runs while the mount idles
    assert bridge.run_due_timers() is None
    assert bridge.run_due_timers() is None
    assert launcher.runs == 2
    assert bridge._busy_ran_out
    # a pending timer request makes the hook look again later
    timer.deadline = timer.now + 5
    assert bridge.run_due_timers() == fs.HandlerBridge._TIMER_RETRY
    assert launcher.runs == 2
    # a packet wait that gets a reply lifts the limit
    launcher.outcomes = iter([IDLE])
    launcher.replies[3] = REPLY
    assert bridge._run_until_replies() == [REPLY]
    assert bridge._idle_busy_cycles == 0
    assert not bridge._busy_ran_out


def test_a_packet_wait_that_runs_out_counts_toward_the_idle_limit(sleeps, monkeypatch):
    """The idle runs after a timer reply count as well."""
    monkeypatch.setattr(fs.HandlerBridge, "_SHORT_BUSY_CYCLES", CHUNK)
    monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES", 2 * CHUNK)
    timer = FakeTimer(deadline=99.0)
    launcher = ScriptedLauncher(repeat(BUSY))
    bridge = _bridge(launcher, timer)
    # a due timer reply runs the handler, which stays busy
    assert bridge.run_due_timers() == FakeTimer.MIN_DELAY
    assert bridge._busy_ran_out
    assert bridge._idle_busy_cycles == CHUNK
    assert bridge.run_due_timers() == FakeTimer.MIN_DELAY
    assert bridge.run_due_timers() is None
    assert launcher.runs == 2
