"""How the bridge runs a handler's startup.

A burst that ends at its cycle limit leaves the handler in the middle of its
work, so the next burst must follow at once. FastFileSystem validating a
1.2 GB volume works through all 2000 startup bursts; with a pause after each
one, startup took 21 s instead of 0.55 s.
"""

from itertools import chain, repeat
from types import SimpleNamespace

import pytest

from amifuse import fuse_fs as fs

# How HandlerLauncher.run_burst reports a burst that ended:
BUSY = SimpleNamespace(done=False, error=False)   # at its cycle limit
PARKED = SimpleNamespace(done=False, error=True)  # in Wait(), nothing pending

REPLY = (0x1000, 0x2000, -1, 0)  # (msg, pkt, res1, res2), as poll_replies returns it


class ScriptedLauncher:
    """A handler whose bursts end as scripted, replying in one of them."""

    def __init__(self, outcomes, reply_in=None):
        self.outcomes = iter(outcomes)
        self.reply_in = reply_in
        self.bursts = 0
        self.queued = []

    def run_burst(self, state, max_cycles=200000, debug=False):
        self.bursts += 1
        state.run_state = next(self.outcomes)
        if self.bursts == self.reply_in:
            self.queued.append(REPLY)
        return state.run_state

    def poll_replies(self, port_addr, debug=False):
        replies, self.queued = self.queued, []
        return replies


@pytest.fixture
def sleeps(monkeypatch):
    """The pauses the startup loop takes, recorded instead of slept."""
    calls = []
    monkeypatch.setattr(fs.time, "sleep", calls.append)
    return calls


def _bridge(launcher):
    bridge = fs.HandlerBridge.__new__(fs.HandlerBridge)
    bridge._debug = False
    bridge.launcher = launcher
    bridge.state = SimpleNamespace(crashed=False, pc=0x4000, initialized=False,
                                   reply_port_addr=0x3000, run_state=None)
    return bridge


def test_busy_startup_bursts_run_again_at_once(sleeps):
    # 100 bursts at the cycle limit, then the burst in which the handler
    # replies to the startup packet and parks in Wait().
    launcher = ScriptedLauncher(chain(repeat(BUSY, 100), repeat(PARKED)), reply_in=101)
    replies = _bridge(launcher)._run_startup_until_replies(cycles=10_000, max_iters=2000)
    assert replies == [REPLY]
    assert launcher.bursts == 101
    assert sleeps == []


def test_startup_that_never_settles_still_ends_after_max_iters(sleeps):
    launcher = ScriptedLauncher(repeat(BUSY))
    assert _bridge(launcher)._run_startup_until_replies(cycles=10_000, max_iters=2000) == []
    assert launcher.bursts == 2000
    assert sleeps == []
