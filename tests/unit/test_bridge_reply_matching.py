"""Each HandlerBridge call takes only the reply to its own packet.

A packet the bridge stopped waiting for is still answered later, while
another call waits. A scripted launcher queues such a late reply ahead of
(or behind) every packet's own reply, and each call must still return
its own result and leave none of its own replies queued. When only the
late reply comes, the call fails as if it had no reply.
"""

import os
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amifuse import fuse_fs

ACTION_LOCATE_OBJECT = 8
ACTION_FREE_LOCK = 15
ACTION_DELETE_OBJECT = 16
ACTION_EXAMINE_OBJECT = 23
ACTION_EXAMINE_NEXT = 24
ACTION_INHIBIT = 31
ACTION_CREATE_DIR = 22
ACTION_END = 1007
ACTION_FINDUPDATE = 1004
ACTION_FINDINPUT = 1005
ACTION_FINDOUTPUT = 1006

ERROR_OBJECT_IN_USE = 202
ERROR_OBJECT_NOT_FOUND = 205
ERROR_DIRECTORY_NOT_EMPTY = 216
ERROR_NO_MORE_ENTRIES = 232

ROOT_LOCK = 0x123
DIR_LOCK = 0x300
NEW_DIR_LOCK = 0x400
FH_ADDR = 0x5000
# The late reply is DOSTRUE, as for a timed-out ACTION_DISK_INFO: it passes
# for a lock, for an open or delete that succeeded, or for a listed entry.
LATE_RES = (-1, 0)


OWN_PKT_BASE = 0x10000   # the packets the bridge sends
LATE_PKT_BASE = 0x70000  # the packets that timed out earlier


class ScriptedLauncher:
    """Answers each packet it is sent, with a late reply to another packet.

    table maps an action to answer(args, names) -> (res1, res2) or
    (res1, res2, fib), where fib is what the reply fills into the
    FileInfoBlock. mode "alone": the late reply comes in a burst of its
    own, ahead of the packet's own. mode "behind": both come in one burst,
    the late one last. mode "silent": only the late reply comes.
    """

    def __init__(self, table, mode, names):
        self._table = table
        self._mode = mode
        self._names = names
        self._next = 0
        self.bursts = []            # queued reply bursts, oldest first
        self.sent = []              # (action, args, pkt)
        self.answered = set()       # packets whose own reply was delivered
        self.answered_at_send = []  # answered, as it was at each send
        self.fib = {}

    def _send(self, action, *args):
        # A call that reads late replies as entries could list on and on.
        assert self._next < 64, "the call kept sending packets"
        pkt = OWN_PKT_BASE + self._next * 0x100
        late = LATE_PKT_BASE + self._next * 0x100
        self._next += 1
        self.answered_at_send.append(set(self.answered))
        self.sent.append((action, args, pkt))
        res1, res2, *fib = self._table[action](args, self._names)
        own = (0, pkt, res1, res2, fib[0] if fib else None)
        stale = (0, late, *LATE_RES, None)
        if self._mode == "alone":
            self.bursts += [[stale], [own]]
        elif self._mode == "behind":
            self.bursts.append([own, stale])
        else:
            self.bursts.append([stale])
        return pkt, 0

    def run_until_replies(self, **_wait):
        """One wait: the oldest queued burst, or [] once nothing is queued."""
        if not self.bursts:
            return []
        burst = self.bursts.pop(0)
        for _, pkt, _, _, fib in burst:
            if pkt < LATE_PKT_BASE:
                self.answered.add(pkt)
                if fib is not None:
                    self.fib = fib
        return [(msg, pkt, res1, res2) for msg, pkt, res1, res2, _ in burst]

    def own_replies_queued(self):
        return [r[1] for burst in self.bursts for r in burst if r[1] < LATE_PKT_BASE]

    def sent_of(self, action):
        return [args for a, args, _ in self.sent if a == action]

    # The send_* calls the nine bridge methods use.
    def send_locate(self, state, lock, name_bptr, mode=-2):
        return self._send(ACTION_LOCATE_OBJECT, lock, name_bptr)

    def send_free_lock(self, state, lock):
        return self._send(ACTION_FREE_LOCK, lock)

    def send_examine(self, state, lock, fib_addr):
        return self._send(ACTION_EXAMINE_OBJECT, lock)

    def send_examine_next(self, state, lock, fib_addr):
        return self._send(ACTION_EXAMINE_NEXT, lock)

    def send_findinput(self, state, name_bptr, lock, fh_addr):
        pkt, msg = self._send(ACTION_FINDINPUT, name_bptr, lock)
        return fh_addr, pkt, msg

    def send_findupdate(self, state, name_bptr, lock, fh_addr):
        pkt, msg = self._send(ACTION_FINDUPDATE, name_bptr, lock)
        return fh_addr, pkt, msg

    def send_findoutput(self, state, name_bptr, lock, fh_addr):
        pkt, msg = self._send(ACTION_FINDOUTPUT, name_bptr, lock)
        return fh_addr, pkt, msg

    def send_delete_object(self, state, lock, name_bptr):
        return self._send(ACTION_DELETE_OBJECT, lock, name_bptr)

    def send_create_dir(self, state, lock, name_bptr):
        return self._send(ACTION_CREATE_DIR, lock, name_bptr)

    def send_end_handle(self, state, fh_addr):
        return self._send(ACTION_END, fh_addr)

    def send_inhibit(self, state, inhibit):
        return self._send(ACTION_INHIBIT, inhibit)


class Names:
    """_alloc_bstr stand-in: the BPTR indexes the names sent."""

    def __init__(self):
        self.names = []

    def alloc(self, text):
        self.names.append(text)
        return 0x8000, len(self.names) - 1

    def __getitem__(self, bptr):
        return self.names[bptr]


def _run(fuse_fs_mod, monkeypatch, mode, table):
    """A bridge on a ScriptedLauncher(table, mode); returns both."""
    names = Names()
    launcher = ScriptedLauncher(table, mode, names)
    bridge = object.__new__(fuse_fs_mod.HandlerBridge)
    bridge._lock = threading.RLock()
    bridge._debug = False
    bridge.state = SimpleNamespace(crashed=False)
    bridge.launcher = launcher
    bridge._run_until_replies = launcher.run_until_replies
    bridge.names = names
    bridge._alloc_bstr = names.alloc
    bridge._alloc_fib = MagicMock(return_value=SimpleNamespace(addr=0x6000))
    bridge._alloc_fh = MagicMock(return_value=FH_ADDR)
    bridge._free_fh = MagicMock()
    bridge._neg_cache = {}
    bridge._neg_cache_ttl = 0
    bridge._unwritten_outputs = set()
    bridge._write_enabled = True
    bridge._volname = None
    bridge.backend = MagicMock()
    bridge.backend.rdb = None
    bridge.mem = MagicMock()
    # The FileInfoBlock holds whatever the last delivered reply filled in.
    monkeypatch.setattr(
        fuse_fs_mod, "_parse_fib",
        lambda mem, addr: {"name": "", "dir_type": 0, "size": 0,
                           **launcher.fib})
    return bridge, launcher


def _ok(args, names):
    return (-1, 0)


def _locate(args, names):
    lock, name_bptr = args
    if lock == 0 and names[name_bptr] == "":
        return (ROOT_LOCK, 0)
    if lock == 0 and names[name_bptr] == "d":
        return (DIR_LOCK, 0)
    return (0, ERROR_OBJECT_NOT_FOUND)


def _opened(bridge, action):
    """(name, directory lock) of each open packet of this action sent."""
    return [(bridge.names[bptr], lock)
            for bptr, lock in bridge.launcher.sent_of(action)]


MODES = ["alone", "behind"]

# open() flags and the packet open_file sends for them.
OPENS = [
    (os.O_RDONLY, ACTION_FINDINPUT),
    (os.O_RDWR, ACTION_FINDUPDATE),
    (os.O_WRONLY | os.O_TRUNC, ACTION_FINDOUTPUT),
]
OPEN_IDS = ["findinput", "findupdate", "findoutput"]


@pytest.fixture
def fuse_fs_mod():
    return fuse_fs


@pytest.mark.parametrize("mode", MODES)
def test_locate(fuse_fs_mod, monkeypatch, mode):
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode,
                            {ACTION_LOCATE_OBJECT: _locate})

    assert bridge.locate(0, "") == (ROOT_LOCK, 0)
    assert launcher.own_replies_queued() == []


@pytest.mark.parametrize("flags, action", OPENS, ids=OPEN_IDS)
@pytest.mark.parametrize("mode", MODES)
def test_open_file_refused(fuse_fs_mod, monkeypatch, mode, flags, action):
    """A refused open fails; the late DOSTRUE does not open the file."""
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode, {
        ACTION_LOCATE_OBJECT: _locate,
        action: lambda args, names: (0, ERROR_OBJECT_NOT_FOUND),
        ACTION_FREE_LOCK: _ok,
    })

    assert bridge.open_file("/f", flags) is None
    assert _opened(bridge, action) == [("f", ROOT_LOCK)]
    assert launcher.sent_of(ACTION_FREE_LOCK) == [(ROOT_LOCK,)]
    bridge._free_fh.assert_called_once_with(FH_ADDR)
    assert launcher.own_replies_queued() == []


@pytest.mark.parametrize("flags, action", OPENS, ids=OPEN_IDS)
@pytest.mark.parametrize("mode", MODES)
def test_open_file_opened(fuse_fs_mod, monkeypatch, mode, flags, action):
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode, {
        ACTION_LOCATE_OBJECT: _locate,
        action: _ok,
    })

    assert bridge.open_file("/f", flags) == (FH_ADDR, ROOT_LOCK)
    assert _opened(bridge, action) == [("f", ROOT_LOCK)]
    assert launcher.own_replies_queued() == []


def _examine_next(entries):
    """EXAMINE_NEXT answers that list entries, then ERROR_NO_MORE_ENTRIES."""
    queue = list(entries)

    def answer(args, names):
        if queue:
            return (-1, 0, {"name": queue.pop(0)})
        return (0, ERROR_NO_MORE_ENTRIES)
    return answer


@pytest.mark.parametrize("mode", MODES)
def test_list_dir(fuse_fs_mod, monkeypatch, mode):
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_EXAMINE_OBJECT: lambda args, names: (-1, 0, {"name": "Work"}),
        ACTION_EXAMINE_NEXT: _examine_next(["a", "b"]),
        ACTION_FREE_LOCK: _ok,
    })

    assert [e["name"] for e in bridge.list_dir(0)] == ["a", "b"]
    assert launcher.sent_of(ACTION_EXAMINE_OBJECT) == [(ROOT_LOCK,)]
    assert launcher.sent_of(ACTION_EXAMINE_NEXT) == [(ROOT_LOCK,)] * 3
    assert launcher.sent_of(ACTION_FREE_LOCK) == [(ROOT_LOCK,)]
    assert launcher.own_replies_queued() == []


@pytest.mark.parametrize("lock, examined, freed", [
    (DIR_LOCK, DIR_LOCK, []),
    (0, ROOT_LOCK, [(ROOT_LOCK,)]),
], ids=["dir", "root"])
@pytest.mark.parametrize("mode", MODES)
def test_list_dir_examine_refused(fuse_fs_mod, monkeypatch, mode, lock, examined, freed):
    """A refused EXAMINE lists nothing and frees the root lock list_dir took."""
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_EXAMINE_OBJECT: lambda args, names: (0, 211),
        ACTION_EXAMINE_NEXT: _examine_next(["a"]),
        ACTION_FREE_LOCK: _ok,
    })

    assert bridge.list_dir(lock) == []
    assert launcher.sent_of(ACTION_EXAMINE_OBJECT) == [(examined,)]
    assert launcher.sent_of(ACTION_EXAMINE_NEXT) == []
    assert launcher.sent_of(ACTION_FREE_LOCK) == freed
    assert launcher.own_replies_queued() == []


@pytest.mark.parametrize("mode", MODES)
def test_volume_name(fuse_fs_mod, monkeypatch, mode):
    """The root is examined with the real root lock, which is then freed."""
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_EXAMINE_OBJECT: lambda args, names: (-1, 0, {"name": "Work"}),
        ACTION_EXAMINE_NEXT: _examine_next([]),
        ACTION_FREE_LOCK: _ok,
    })

    assert bridge.volume_name() == "Work"
    assert launcher.sent_of(ACTION_EXAMINE_OBJECT) == [(ROOT_LOCK,)]
    assert launcher.sent_of(ACTION_FREE_LOCK) == [(ROOT_LOCK,)]
    assert launcher.own_replies_queued() == []


@pytest.mark.parametrize("mode", MODES)
def test_delete_object(fuse_fs_mod, monkeypatch, mode):
    """A delete refused as in use stays refused (unlink defers it on 202)."""
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode, {
        ACTION_DELETE_OBJECT: lambda args, names: (0, ERROR_OBJECT_IN_USE),
    })

    assert bridge.delete_object(ROOT_LOCK, "f") == (0, ERROR_OBJECT_IN_USE)
    assert launcher.own_replies_queued() == []


@pytest.mark.parametrize("mode", MODES)
def test_delete_dir_atomic(fuse_fs_mod, monkeypatch, mode):
    """The delete goes to the parent's real lock, and that lock is freed."""
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_DELETE_OBJECT: lambda args, names: (0, ERROR_DIRECTORY_NOT_EMPTY),
        ACTION_FREE_LOCK: _ok,
    })

    assert bridge.delete_dir_atomic("/d/e") == (0, ERROR_DIRECTORY_NOT_EMPTY)
    deletes = [(lock, bridge.names[bptr])
               for lock, bptr in launcher.sent_of(ACTION_DELETE_OBJECT)]
    assert deletes == [(DIR_LOCK, "e")]
    assert launcher.sent_of(ACTION_FREE_LOCK) == [(DIR_LOCK,)]
    assert launcher.own_replies_queued() == []


@pytest.mark.parametrize("mode", MODES)
def test_create_dir(fuse_fs_mod, monkeypatch, mode):
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, mode, {
        ACTION_CREATE_DIR: lambda args, names: (NEW_DIR_LOCK, 0),
    })

    assert bridge.create_dir(ROOT_LOCK, "n") == (NEW_DIR_LOCK, 0)
    assert launcher.own_replies_queued() == []


def test_close_file_waits_for_its_end_reply(fuse_fs_mod, monkeypatch):
    """Left queued, the ACTION_END reply would pass for the next call's."""
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, "alone", {
        ACTION_END: _ok,
    })

    bridge.close_file(FH_ADDR)

    assert launcher.own_replies_queued() == []
    bridge._free_fh.assert_called_once_with(FH_ADDR)


@pytest.mark.parametrize("call, expected, sent", [
    (lambda b: b.locate(0, ""), (0, -1), [ACTION_LOCATE_OBJECT]),
    (lambda b: b.open_file("/f", os.O_RDONLY), None,
     [ACTION_LOCATE_OBJECT, ACTION_FINDINPUT]),
    (lambda b: b.list_dir(DIR_LOCK), [], [ACTION_EXAMINE_OBJECT]),
    (lambda b: b.delete_object(ROOT_LOCK, "f"), (0, -1), [ACTION_DELETE_OBJECT]),
    (lambda b: b.delete_dir_atomic("/e"), (0, -1),
     [ACTION_LOCATE_OBJECT, ACTION_DELETE_OBJECT]),
    (lambda b: b.create_dir(ROOT_LOCK, "n"), (0, -1), [ACTION_CREATE_DIR]),
], ids=["locate", "open_file", "list_dir", "delete_object",
        "delete_dir_atomic", "create_dir"])
def test_only_a_late_reply_is_no_reply(fuse_fs_mod, monkeypatch, call, expected, sent):
    """With no reply of its own, a call fails and sends nothing further."""
    actions = (ACTION_LOCATE_OBJECT, ACTION_FINDINPUT, ACTION_EXAMINE_OBJECT,
               ACTION_EXAMINE_NEXT, ACTION_DELETE_OBJECT, ACTION_CREATE_DIR,
               ACTION_FREE_LOCK)
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, "silent",
                            {action: _ok for action in actions})

    assert call(bridge) == expected
    assert [action for action, _, _ in launcher.sent] == sent


def test_inhibit_cycle_uninhibits_after_the_inhibit_reply(fuse_fs_mod, monkeypatch):
    bridge, launcher = _run(fuse_fs_mod, monkeypatch, "alone", {
        ACTION_INHIBIT: _ok,
    })

    bridge.inhibit_cycle()

    (inhibit, _, inhibit_pkt), (uninhibit, args, _) = launcher.sent
    assert (inhibit, uninhibit, args) == (ACTION_INHIBIT, ACTION_INHIBIT, (False,))
    assert inhibit_pkt in launcher.answered_at_send[1]
    assert launcher.own_replies_queued() == []
    bridge.backend.sync.assert_called_once_with()
