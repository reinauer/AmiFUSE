"""Examine through ACTION_EXAMINE_OBJECT64 and _NEXT64 where the handler has them.

The 64 bit packets fill the FileInfoBlock as the classic ones do, and also
store the size and block count as 64 bit values in fib_Reserved, at 228 and
236. A handler without them answers DOSFALSE, ERROR_ACTION_NOT_KNOWN.
"""

import struct
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from amitools.vamos.machine.mock.mem import MockMemory

from amifuse import fuse_fs as fs
from amifuse.startup_runner import HandlerLauncher

EXAMINE, EXAMINE_NEXT, EXAMINE64, EXAMINE_NEXT64 = 23, 24, 26408, 26409
ERROR_ACTION_NOT_KNOWN = 209
FIB = 0x1000
BIG = (5 << 30) + 123


def _fill(mem, name, size, dir_type=-3, wide=True):
    """A FileInfoBlock as the 64 bit packets leave it: fib_Size 0 above 2 GiB."""
    fib = bytearray(mem.r_block(FIB, 260))
    struct.pack_into(">i", fib, 4, dir_type)
    fib[8:9 + len(name)] = bytes([len(name)]) + name.encode("latin-1")
    struct.pack_into(">I", fib, 124, size if size <= 0x7FFFFFFF else 0)
    struct.pack_into(">i", fib, 128, (size + 511) // 512)
    if wide:
        struct.pack_into(">QQ", fib, 228, size, (size + 511) // 512)
    mem.w_block(FIB, bytes(fib))


class Handler:
    """Answers Examine of a directory holding one file of BIG bytes.

    knows64: "yes" answers the 64 bit packets, "no" refuses them, "says-yes"
    answers them with success and fills in only the classic fields.
    replies: a reply to give instead, by packet type, None for no reply.
    """

    def __init__(self, knows64="yes", replies=None):
        self.mem = MockMemory(size_kib=64)
        self.knows64 = knows64
        self.replies = dict(replies or {})
        self.sent = []
        self.next = {}

    def answer(self, ptype, lock):
        self.sent.append(ptype)
        wide = ptype in (EXAMINE64, EXAMINE_NEXT64)
        if ptype in self.replies:
            return self.replies[ptype]
        if wide and self.knows64 == "no":
            return (0, ERROR_ACTION_NOT_KNOWN)
        wide = wide and self.knows64 == "yes"
        if ptype in (EXAMINE, EXAMINE64):
            self.next[lock] = 0
            _fill(self.mem, "Dir", 0, dir_type=2, wide=wide)
            return (-1, 0)
        if self.next[lock] == 0:
            self.next[lock] = 1
            _fill(self.mem, "Big", BIG, wide=wide)
            if not wide:
                self.mem.w32(FIB + 124, 0xFFFFFFFF)
            return (-1, 0)
        return (0, fs.ERROR_NO_MORE_ENTRIES)


def _bridge(handler):
    b = object.__new__(fs.HandlerBridge)
    b._lock = threading.RLock()
    b._debug = False
    b.state = SimpleNamespace(crashed=False)
    b.mem = handler.mem
    b._examine64 = None
    b._alloc_fib = lambda: SimpleNamespace(addr=FIB)
    replies = []

    def send_packet(state, ptype, args):
        assert args == [0x10, FIB >> 2]
        replies.append(handler.answer(ptype, args[0]))
        return 0x2000, 0

    b.launcher = object.__new__(HandlerLauncher)
    b.launcher.send_packet = send_packet
    b._run_until_reply = lambda pkt, label: replies.pop(0)
    return b


def test_wide_fib_gives_the_64_bit_size_and_blocks():
    mem = MockMemory(size_kib=64)
    _fill(mem, "Big", BIG)
    info = fs._parse_fib(mem, FIB, True)
    assert info["name"] == "Big"
    assert info["size"] == BIG
    assert info["num_blocks"] == (BIG + 511) // 512
    assert fs._parse_fib(mem, FIB)["size"] == 0


def test_the_launcher_sends_the_64_bit_types():
    launcher = object.__new__(HandlerLauncher)
    launcher.send_packet = MagicMock(return_value=(0x2000, 0))
    launcher.send_examine64("state", 0x10, FIB)
    launcher.send_examine64("state", 0x10, FIB, next_entry=True)
    assert [c.args for c in launcher.send_packet.call_args_list] == [
        ("state", EXAMINE64, [0x10, FIB >> 2]),
        ("state", EXAMINE_NEXT64, [0x10, FIB >> 2]),
    ]


def test_a_handler_with_the_packets_lists_the_64_bit_size():
    handler = Handler("yes")
    b = _bridge(handler)
    entries = b.list_dir(0x10)
    assert [(e["name"], e["size"]) for e in entries] == [("Big", BIG)]
    assert b._examine64 is True
    assert handler.sent == [EXAMINE64, EXAMINE_NEXT64, EXAMINE_NEXT64]


def test_a_handler_without_them_gets_classic_packets_after_one_try():
    handler = Handler("no")
    b = _bridge(handler)
    entries = b.list_dir(0x10)
    assert [(e["name"], e["size"]) for e in entries] == [("Big", 0xFFFFFFFF)]
    assert b._examine64 is False
    assert b._examine_lock(0x10)["name"] == "Dir"
    assert handler.sent == [EXAMINE64, EXAMINE, EXAMINE_NEXT, EXAMINE_NEXT, EXAMINE]


def test_a_success_without_the_64_bit_size_counts_as_unknown():
    handler = Handler("says-yes")
    b = _bridge(handler)
    entries = b.list_dir(0x10)
    assert [(e["name"], e["size"]) for e in entries] == [("Big", 0xFFFFFFFF)]
    assert b._examine64 is False
    assert handler.sent == [EXAMINE64, EXAMINE, EXAMINE_NEXT, EXAMINE_NEXT]


def test_a_failed_examine_decides_nothing():
    handler = Handler("yes", {EXAMINE64: (0, fs.ERROR_OBJECT_IN_USE),
                              EXAMINE: (0, fs.ERROR_OBJECT_IN_USE)})
    b = _bridge(handler)
    assert b._examine_lock(0x10) is None
    assert b._examine64 is None
    handler.replies.clear()
    assert b._examine_lock(0x10)["name"] == "Dir"
    assert b._examine64 is True
    assert handler.sent == [EXAMINE64, EXAMINE, EXAMINE64]


def test_no_reply_to_the_probe_sends_nothing_else():
    handler = Handler("yes", {EXAMINE64: None})
    b = _bridge(handler)
    assert b._examine_lock(0x10) is None
    assert b._examine64 is None
    assert handler.sent == [EXAMINE64]


def test_no_reply_to_the_classic_retry_decides_nothing():
    handler = Handler("no", {EXAMINE: None})
    b = _bridge(handler)
    assert b._examine_lock(0x10) is None
    assert b._examine64 is None
    assert handler.sent == [EXAMINE64, EXAMINE]
