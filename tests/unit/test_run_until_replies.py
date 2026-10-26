"""How long the bridge waits for a handler's reply.

A burst that ends at its cycle limit leaves the handler in the middle of its
work, and the reply can be thousands of bursts away. Such bursts must not use
up the max_iters budget, which bounds the wait on a handler that is parked or
has exited, yet a handler caught in a loop must still fail the packet, and
must not make every later packet wait as long again.
"""

import threading
from itertools import chain, repeat
from types import SimpleNamespace

import pytest

from amifuse import fuse_fs as fs

# How HandlerLauncher.run_burst reports a burst that ended:
BUSY = SimpleNamespace(done=False, error=False)   # at its cycle limit
PARKED = SimpleNamespace(done=False, error=True)  # in Wait(), nothing pending
EXITED = SimpleNamespace(done=True, error=False)  # through the exit trap

REPLY = (0x1000, 0x2000, 65536, 0)  # (msg, pkt, res1, res2), as poll_replies returns it
FLUSH_PKT = 0x2100


class ScriptedLauncher:
    """A handler whose bursts end as scripted, replying in some of them."""

    def __init__(self, outcomes, reply_in=None, replies=None):
        self.outcomes = iter(outcomes)
        # The reply posted in each burst: REPLY in burst reply_in.
        self.replies = dict(replies or {})
        if reply_in is not None:
            self.replies[reply_in] = REPLY
        self.bursts = 0
        self.debug_polls = 0
        self.queued = []
        self.last_pkt_type = None

    def run_burst(self, state, max_cycles=200000, debug=False):
        self.bursts += 1
        state.run_state = next(self.outcomes)
        if self.bursts in self.replies:
            self.queued.append(self.replies[self.bursts])
        return state.run_state

    def poll_replies(self, port_addr, debug=False):
        self.debug_polls += debug
        replies, self.queued = self.queued, []
        return replies

    def send_flush(self, state):
        return FLUSH_PKT, FLUSH_PKT - 0x1000


@pytest.fixture(autouse=True)
def _keep_exec_block_state(monkeypatch):
    # Without a ProcessManager, a parked burst clears the Exec blocking
    # state, which lives on the ExecLibrary class. Leave the real one alone.
    monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)


@pytest.fixture
def sleeps(monkeypatch):
    """The pauses _run_until_replies takes, recorded instead of slept."""
    calls = []
    monkeypatch.setattr(fs.time, "sleep", calls.append)
    return calls


def _bridge(launcher):
    bridge = fs.HandlerBridge.__new__(fs.HandlerBridge)
    bridge._debug = False
    bridge._busy_ran_out = False
    bridge._lock = threading.RLock()
    bridge.launcher = launcher
    bridge.backend = SimpleNamespace(sync=lambda: None, fsync=lambda: None)
    bridge.state = SimpleNamespace(crashed=False, pc=0x4000, main_loop_pc=0,
                                   initialized=True, reply_port_addr=0x3000,
                                   run_state=None)
    # The reply port holds what the handler has posted and not been polled.
    port_mgr = SimpleNamespace(has_msg=lambda port_addr: bool(launcher.queued))
    bridge.vh = SimpleNamespace(
        slm=SimpleNamespace(exec_impl=SimpleNamespace(port_mgr=port_mgr)))
    bridge._drain_non_essential_ports = lambda port_mgr: None
    return bridge


def test_busy_handler_gets_its_reply_after_more_than_max_iters_bursts(sleeps):
    # pfs3aio's first write after a 2 GiB file: 58 bursts at the cycle limit,
    # then the burst in which it replies and waits for the next packet.
    launcher = ScriptedLauncher(chain(repeat(BUSY, 58), repeat(PARKED)), reply_in=59)
    assert _bridge(launcher)._run_until_replies() == [REPLY]
    assert launcher.bursts == 59
    # A working handler runs again at once.
    assert sleeps == []


def test_longest_wait_seen_succeeds_when_no_wait_ran_out_before(sleeps):
    # pfs3aio reads the next packet after freeing the blocks of a deleted
    # 2 GiB file: 13,990 bursts, far beyond the short busy limit.
    launcher = ScriptedLauncher(chain(repeat(BUSY, 13_989), repeat(PARKED)),
                                reply_in=13_990)
    assert _bridge(launcher)._run_until_replies() == [REPLY]
    assert launcher.bursts == 13_990


@pytest.mark.parametrize("cycles", [200_000, 2_000_000])
def test_handler_that_never_finishes_fails_after_the_busy_cycle_limit(sleeps, cycles):
    launcher = ScriptedLauncher(repeat(BUSY))
    assert _bridge(launcher)._run_until_replies(cycles=cycles) == []
    assert launcher.bursts == fs.HandlerBridge._MAX_BUSY_CYCLES // cycles
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
    assert launcher.bursts == 50_000
    assert "WARNING" in capsys.readouterr().err
    for _ in range(2):
        before = launcher.bursts
        assert bridge._run_until_replies() == []
        # _SHORT_BUSY_CYCLES: 50 bursts of 200,000 cycles.
        assert launcher.bursts - before == 50
    assert sleeps == []
    # Only the full limit warns, once for each time it runs out.
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("stop", [PARKED, EXITED], ids=["waits", "exits"])
def test_a_burst_in_which_the_handler_waits_or_exits_restores_the_full_limit(sleeps, stop):
    # The first wait runs out. In the next, the handler stops once, then
    # works for 100 bursts, twice the short limit, and replies.
    launcher = ScriptedLauncher(
        chain(repeat(BUSY, 50_000), [stop], repeat(BUSY, 100), repeat(PARKED)),
        reply_in=50_000 + 1 + 100 + 1)
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    assert bridge._run_until_replies() == [REPLY]
    assert launcher.bursts == 50_102


def test_a_late_reply_restores_the_full_limit(sleeps):
    # A handler that is only slow: the first wait runs out while it works
    # on packet A. It answers A 10 bursts into the wait for packet B and
    # goes straight on to B, without waiting, for 100 bursts, twice the
    # short limit.
    a_reply = (0x1000, 0x2000, 1, 0)
    b_reply = (0x1050, 0x2050, 1, 0)
    launcher = ScriptedLauncher(chain(repeat(BUSY, 50_109), repeat(PARKED)),
                                replies={50_010: a_reply, 50_110: b_reply})
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    assert bridge._run_until_reply(0x2050, "B") == (1, 0)
    assert launcher.bursts == 50_110


def test_run_until_reply_passes_its_wait_arguments_on(sleeps):
    # format_volume waits for its packets with a max_iters and cycles of its
    # own, such as 2000 bursts of 2,000,000 cycles for ACTION_FORMAT. A reply
    # to another packet in burst 1 starts a second round, which must get the
    # same arguments.
    launcher = ScriptedLauncher(repeat(PARKED), replies={1: (0x1050, 0x2050, 1, 0)})
    assert _bridge(launcher)._run_until_reply(0x2000, "format", max_iters=7) is None
    assert launcher.bursts == 1 + 7
    launcher = ScriptedLauncher(repeat(BUSY))
    assert _bridge(launcher)._run_until_reply(0x2000, "format", cycles=2_000_000) is None
    # The busy limit counts the cycles of each burst: 5,000 of 2,000,000.
    assert launcher.bursts == fs.HandlerBridge._MAX_BUSY_CYCLES // 2_000_000


def test_flush_gets_the_full_limit_when_no_wait_ran_out(sleeps):
    # FastFileSystem answers ACTION_FLUSH on a volume it is validating only
    # once it is done: after 2,714 bursts at the cycle limit on a 3.2 GB
    # partition.
    launcher = ScriptedLauncher(chain(repeat(BUSY, 2_714), repeat(PARKED)),
                                replies={2_715: (0x1100, FLUSH_PKT, -1, 0)})
    _bridge(launcher).flush_volume()
    assert launcher.bursts == 2_715


@pytest.mark.parametrize("flush_res1, acknowledged", [(-1, True), (0, False)],
                         ids=["DOSTRUE", "DOSFALSE"])
def test_flush_waits_for_its_own_reply_after_a_late_one(sleeps, flush_res1, acknowledged):
    # A handler that is only slow: a write's wait ran out while it worked.
    # It answers the write 10 bursts into the flush's wait, then works on
    # the flush for 1000 bursts, beyond the short limit. Only the flush's
    # own reply says whether the handler wrote its buffers.
    write_reply = (0x1000, 0x2000, 65536, 0)
    launcher = ScriptedLauncher(
        chain(repeat(BUSY, 51_009), repeat(PARKED)),
        replies={50_010: write_reply, 51_010: (0x1100, FLUSH_PKT, flush_res1, 0)})
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    if acknowledged:
        bridge.flush_volume()
    else:
        with pytest.raises(OSError, match="ACTION_FLUSH"):
            bridge.flush_volume()
    assert launcher.bursts == 51_010


def test_flush_to_a_handler_still_looping_fails_after_the_short_limit(sleeps, capsys):
    # 50 bursts, as every wait had before the busy limit.
    launcher = ScriptedLauncher(repeat(BUSY))
    bridge = _bridge(launcher)
    assert bridge._run_until_replies() == []
    capsys.readouterr()
    with pytest.raises(OSError, match="ACTION_FLUSH"):
        bridge.flush_volume()
    assert launcher.bursts == 50_000 + 50
    assert capsys.readouterr().err == ""


def test_debug_reports_every_1000th_busy_burst(sleeps, capsys):
    # A long wait would otherwise print a poll line for each busy burst.
    launcher = ScriptedLauncher(chain(repeat(BUSY, 2500), repeat(PARKED)), reply_in=2501)
    bridge = _bridge(launcher)
    bridge._debug = True
    assert bridge._run_until_replies() == [REPLY]
    # Busy bursts 1000 and 2000, and the burst that replies.
    assert launcher.debug_polls == 3
    busy_lines = [line for line in capsys.readouterr().out.splitlines()
                  if "busy for" in line]
    assert busy_lines == [
        "[amifuse] _run_until_replies: busy for 200,000,000 cycles, pc=0x4000",
        "[amifuse] _run_until_replies: busy for 400,000,000 cycles, pc=0x4000",
    ]


def test_debug_shows_the_poll_of_a_reply_in_a_busy_burst(sleeps):
    # pfs3aio replies in the middle of a burst and goes on working, so the
    # burst ends busy: here burst 1500, which is not a 1000th.
    launcher = ScriptedLauncher(repeat(BUSY), reply_in=1500)
    bridge = _bridge(launcher)
    bridge._debug = True
    assert bridge._run_until_replies() == [REPLY]
    # The poll of busy burst 1000, and of burst 1500 with the reply.
    assert launcher.debug_polls == 2


def test_parked_handler_still_fails_after_max_iters_bursts(sleeps, capsys):
    launcher = ScriptedLauncher(repeat(PARKED))
    assert _bridge(launcher)._run_until_replies(max_iters=50) == []
    assert launcher.bursts == 50
    # Only the busy cycle limit warns; a parked handler fails as quietly as before.
    assert capsys.readouterr().err == ""


def test_exited_handler_counts_against_max_iters_and_backs_off(sleeps):
    launcher = ScriptedLauncher(repeat(EXITED))
    assert _bridge(launcher)._run_until_replies(max_iters=5) == []
    assert launcher.bursts == 5
    assert sleeps == [0.0005, 0.001, 0.002, 0.004, 0.008]


def test_work_between_waits_does_not_use_up_max_iters(sleeps):
    # 49 parked bursts, a long stretch of work, then parked again: the 50th
    # parked burst ends the wait, however many working bursts came between.
    launcher = ScriptedLauncher(chain(repeat(PARKED, 49), repeat(BUSY, 1000), repeat(PARKED)))
    assert _bridge(launcher)._run_until_replies() == []
    assert launcher.bursts == 49 + 1000 + 1
