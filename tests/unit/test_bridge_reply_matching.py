"""What each HandlerBridge call makes of its packets' replies.

A scripted handler answers each packet the bridge sends: each call must
return its result, send the packets it should and free the locks it took.
A call whose packet gets no reply fails and sends nothing further. Which
reply answers which packet is DosPktHandler's to match.
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
# The args of each packet as the tests compare them.
_SHAPE = {
    ACTION_LOCATE_OBJECT: lambda a: (a[0], a[1]),
    ACTION_FREE_LOCK: lambda a: (a[0],),
    ACTION_EXAMINE_OBJECT: lambda a: (a[0],),
    ACTION_EXAMINE_NEXT: lambda a: (a[0],),
    ACTION_FINDINPUT: lambda a: (a[2], a[1]),
    ACTION_FINDUPDATE: lambda a: (a[2], a[1]),
    ACTION_FINDOUTPUT: lambda a: (a[2], a[1]),
    ACTION_DELETE_OBJECT: lambda a: (a[0], a[1]),
    ACTION_CREATE_DIR: lambda a: (a[0], a[1]),
    ACTION_END: lambda a: (a[0],),
    ACTION_INHIBIT: lambda a: (bool(a[0]),),
}


class ScriptedPackets:
    """Stands in for HandlerBridge._packet and answers as scripted.

    table maps an action to answer(args, names) -> (res1, res2) or
    (res1, res2, fib), where fib is what the reply fills into the
    FileInfoBlock. args are shaped as in _SHAPE: (name BPTR, lock) for an
    open. With silent, no packet gets a reply.
    """

    def __init__(self, table, names, silent=False):
        self._table = table
        self._names = names
        self._silent = silent
        self.sent = []  # (action, args)
        self.fib = {}

    def __call__(self, action, args, label, *more, **limits):
        # A call that read failures as entries could list on and on.
        assert len(self.sent) < 64, "the call kept sending packets"
        args = _SHAPE[action](list(args))
        self.sent.append((action, args))
        if self._silent:
            return None
        res1, res2, *fib = self._table[action](args, self._names)
        if fib:
            self.fib = fib[0]
        return (res1, res2)

    def sent_of(self, action):
        return [args for a, args in self.sent if a == action]


class Names:
    """_alloc_bstr stand-in: the BPTR indexes the names sent."""

    def __init__(self):
        self.names = []

    def alloc(self, text):
        self.names.append(text)
        return 0x8000, len(self.names) - 1

    def __getitem__(self, bptr):
        return self.names[bptr]


def _run(fuse_fs_mod, monkeypatch, table, silent=False):
    """A bridge on ScriptedPackets(table); returns the bridge and them."""
    names = Names()
    packets = ScriptedPackets(table, names, silent)
    bridge = object.__new__(fuse_fs_mod.HandlerBridge)
    bridge._lock = threading.RLock()
    bridge._debug = False
    bridge.state = SimpleNamespace(crashed=False)
    bridge._packet = packets
    bridge._fh_arg = lambda fh_addr: fh_addr
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
                           **packets.fib})
    return bridge, packets


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
            for bptr, lock in bridge._packet.sent_of(action)]


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


def test_locate(fuse_fs_mod, monkeypatch):
    bridge, packets = _run(fuse_fs_mod, monkeypatch,
                            {ACTION_LOCATE_OBJECT: _locate})

    assert bridge.locate(0, "") == (ROOT_LOCK, 0)


@pytest.mark.parametrize("flags, action", OPENS, ids=OPEN_IDS)
def test_open_file_refused(fuse_fs_mod, monkeypatch, flags, action):
    """A refused open fails; the late DOSTRUE does not open the file."""
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_LOCATE_OBJECT: _locate,
        action: lambda args, names: (0, ERROR_OBJECT_NOT_FOUND),
        ACTION_FREE_LOCK: _ok,
    })

    assert bridge.open_file("/f", flags) is None
    assert _opened(bridge, action) == [("f", ROOT_LOCK)]
    assert packets.sent_of(ACTION_FREE_LOCK) == [(ROOT_LOCK,)]
    bridge._free_fh.assert_called_once_with(FH_ADDR)


@pytest.mark.parametrize("flags, action", OPENS, ids=OPEN_IDS)
def test_open_file_opened(fuse_fs_mod, monkeypatch, flags, action):
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_LOCATE_OBJECT: _locate,
        action: _ok,
    })

    assert bridge.open_file("/f", flags) == (FH_ADDR, ROOT_LOCK)
    assert _opened(bridge, action) == [("f", ROOT_LOCK)]


def _examine_next(entries):
    """EXAMINE_NEXT answers that list entries, then ERROR_NO_MORE_ENTRIES."""
    queue = list(entries)

    def answer(args, names):
        if queue:
            return (-1, 0, {"name": queue.pop(0)})
        return (0, ERROR_NO_MORE_ENTRIES)
    return answer


def test_list_dir(fuse_fs_mod, monkeypatch):
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_EXAMINE_OBJECT: lambda args, names: (-1, 0, {"name": "Work"}),
        ACTION_EXAMINE_NEXT: _examine_next(["a", "b"]),
        ACTION_FREE_LOCK: _ok,
    })

    assert [e["name"] for e in bridge.list_dir(0)] == ["a", "b"]
    assert packets.sent_of(ACTION_EXAMINE_OBJECT) == [(ROOT_LOCK,)]
    assert packets.sent_of(ACTION_EXAMINE_NEXT) == [(ROOT_LOCK,)] * 3
    assert packets.sent_of(ACTION_FREE_LOCK) == [(ROOT_LOCK,)]


@pytest.mark.parametrize("lock, examined, freed", [
    (DIR_LOCK, DIR_LOCK, []),
    (0, ROOT_LOCK, [(ROOT_LOCK,)]),
], ids=["dir", "root"])
def test_list_dir_examine_refused(fuse_fs_mod, monkeypatch, lock, examined, freed):
    """A refused EXAMINE lists nothing and frees the root lock list_dir took."""
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_EXAMINE_OBJECT: lambda args, names: (0, 211),
        ACTION_EXAMINE_NEXT: _examine_next(["a"]),
        ACTION_FREE_LOCK: _ok,
    })

    assert bridge.list_dir(lock) == []
    assert packets.sent_of(ACTION_EXAMINE_OBJECT) == [(examined,)]
    assert packets.sent_of(ACTION_EXAMINE_NEXT) == []
    assert packets.sent_of(ACTION_FREE_LOCK) == freed


def test_volume_name(fuse_fs_mod, monkeypatch):
    """The root is examined with the real root lock, which is then freed."""
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_EXAMINE_OBJECT: lambda args, names: (-1, 0, {"name": "Work"}),
        ACTION_EXAMINE_NEXT: _examine_next([]),
        ACTION_FREE_LOCK: _ok,
    })

    assert bridge.volume_name() == "Work"
    assert packets.sent_of(ACTION_EXAMINE_OBJECT) == [(ROOT_LOCK,)]
    assert packets.sent_of(ACTION_FREE_LOCK) == [(ROOT_LOCK,)]


def test_delete_object(fuse_fs_mod, monkeypatch):
    """A delete refused as in use stays refused (unlink defers it on 202)."""
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_DELETE_OBJECT: lambda args, names: (0, ERROR_OBJECT_IN_USE),
    })

    assert bridge.delete_object(ROOT_LOCK, "f") == (0, ERROR_OBJECT_IN_USE)


def test_delete_dir_atomic(fuse_fs_mod, monkeypatch):
    """The delete goes to the parent's real lock, and that lock is freed."""
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_DELETE_OBJECT: lambda args, names: (0, ERROR_DIRECTORY_NOT_EMPTY),
        ACTION_FREE_LOCK: _ok,
    })

    assert bridge.delete_dir_atomic("/d/e") == (0, ERROR_DIRECTORY_NOT_EMPTY)
    deletes = [(lock, bridge.names[bptr])
               for lock, bptr in packets.sent_of(ACTION_DELETE_OBJECT)]
    assert deletes == [(DIR_LOCK, "e")]
    assert packets.sent_of(ACTION_FREE_LOCK) == [(DIR_LOCK,)]


def test_create_dir(fuse_fs_mod, monkeypatch):
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_CREATE_DIR: lambda args, names: (NEW_DIR_LOCK, 0),
    })

    assert bridge.create_dir(ROOT_LOCK, "n") == (NEW_DIR_LOCK, 0)


def test_close_file_ends_the_handle(fuse_fs_mod, monkeypatch):
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_END: _ok,
    })

    bridge.close_file(FH_ADDR)

    assert packets.sent_of(ACTION_END) == [(FH_ADDR,)]
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
def test_no_reply_fails_the_call(fuse_fs_mod, monkeypatch, call, expected, sent):
    """With no reply of its own, a call fails and sends nothing further."""
    actions = (ACTION_LOCATE_OBJECT, ACTION_FINDINPUT, ACTION_EXAMINE_OBJECT,
               ACTION_EXAMINE_NEXT, ACTION_DELETE_OBJECT, ACTION_CREATE_DIR,
               ACTION_FREE_LOCK)
    bridge, packets = _run(fuse_fs_mod, monkeypatch,
                           {action: _ok for action in actions}, silent=True)

    assert call(bridge) == expected
    assert [action for action, _ in packets.sent] == sent


def test_inhibit_cycle_uninhibits_after_the_inhibit_reply(fuse_fs_mod, monkeypatch):
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_INHIBIT: _ok,
    })

    bridge.inhibit_cycle()

    assert packets.sent == [(ACTION_INHIBIT, (True,)), (ACTION_INHIBIT, (False,))]
    bridge.backend.sync.assert_called_once_with()


@pytest.mark.parametrize("error", [213, ERROR_NO_MORE_ENTRIES])
def test_listing_warning_names_and_escapes_the_directory(fuse_fs_mod, monkeypatch, error, capsys):
    answers = iter([(-1, 0, {"name": ""}), (0, error)])
    bridge, packets = _run(fuse_fs_mod, monkeypatch, {
        ACTION_LOCATE_OBJECT: _locate,
        ACTION_EXAMINE_OBJECT: lambda args, names: (-1, 0, {"name": "dir"}),
        ACTION_EXAMINE_NEXT: lambda args, names: next(answers),
        ACTION_FREE_LOCK: _ok,
    })
    monkeypatch.setattr(bridge, "locate_path", lambda path: (DIR_LOCK, 0, []))
    assert bridge.list_dir_path("/bad\nname") == []
    warning = capsys.readouterr().err
    assert "listing /bad\\x0aname" in warning
    assert "skipped 1 directory entry with an empty name" in warning
    assert ("incomplete" in warning) == (error != ERROR_NO_MORE_ENTRIES)
