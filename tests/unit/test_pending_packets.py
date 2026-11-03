"""A packet whose wait ran out keeps its memory until the handler answers.

The handler still holds such a packet and answers it later. Until then
the bridge must not build another packet in its StandardPacket, nor reuse
its name BSTRs, its data buffer or its FileHandle (#117). A late reply
that the bridge drops gives back the lock or file it grants (#194). And
unmount runs the handler until every packet has its reply before it
flushes the volume (#118).

A scripted handler on the real HandlerLauncher, in MockMemory, takes each
packet the bridge queues and answers it at once, later, or never. It
stands in for the scheduler: each run takes the queued packets and ends
BUSY while an answer is still to come, IDLE once the handler waits.
"""

import errno
import math
import os
import threading
from types import SimpleNamespace

import pytest

from amitools.vamos.error import VamosInternalError
from amitools.vamos.libstructs.dos import DosPacketStruct, FileHandleStruct
from amitools.vamos.libstructs.exec_ import MsgPortStruct
from amitools.vamos.machine.mock.mem import MockMemory
from amitools.vamos.mem.alloc import MemoryAlloc
from amitools.vamos.schedule import RunResult

from amifuse import fuse_fs as fs
from amifuse.startup_runner import (
    ACTION_DELETE_OBJECT,
    ACTION_END,
    ACTION_EXAMINE_OBJECT,
    ACTION_FINDINPUT,
    ACTION_FINDOUTPUT,
    ACTION_FINDUPDATE,
    ACTION_FLUSH,
    ACTION_FREE_LOCK,
    ACTION_LOCATE_OBJECT,
    ACTION_READ,
    ACTION_RENAME_OBJECT,
    ACTION_SET_DATE,
    ACTION_SET_FILE_SIZE,
    ACTION_WRITE,
    HandlerLauncher,
)

# How HandlerLauncher.run reports a run that ended:
BUSY = RunResult.BUSY   # the handler still works
PARKED = RunResult.IDLE  # in Wait(), nothing pending

NEVER = math.inf
DP_RES1 = DosPacketStruct.sdef.find_field_def_by_name("dp_Res1").offset
DP_RES2 = DosPacketStruct.sdef.find_field_def_by_name("dp_Res2").offset
FH_ARGS = FileHandleStruct.sdef.find_field_def_by_name("fh_Args").offset
# Names of the packets whose args carry a name BSTR, and where.
NAME_ARG = {ACTION_LOCATE_OBJECT: 1, ACTION_FINDINPUT: 2, ACTION_FINDUPDATE: 2}


class Ports:
    """The port manager's message queues, as exec's PutMsg/GetMsg see them."""

    def __init__(self):
        self.ports = {}

    def has_port(self, port):
        return port in self.ports

    def has_msg(self, port):
        return bool(self.ports.get(port))

    def put_msg(self, port, msg):
        self.ports.setdefault(port, []).append(msg)

    def get_msg(self, port):
        return self.ports[port].pop(0)


class Handler:
    """Takes every queued packet in a run and answers it as scripted.

    delay(pkt_type, name) gives the runs until the answer: 0 answers
    in the run that takes it, NEVER not at all. A run is busy while
    an answer is still to come, as for a handler growing a file, which
    still answers other packets at once. answer(pkt_type, args, name)
    gives (res1, res2), and may write into the memory the args name.
    With in_order, it takes no packet while one it took is unanswered.
    """

    def __init__(self, launcher, mem, ports, state):
        self.launcher = launcher
        self.mem = mem
        self.ports = ports
        self.state = state
        self.delay = lambda pkt_type, name: 0
        self.answer = lambda pkt_type, args, name: (-1, 0)
        self.in_order = False
        self.waiting = []   # [runs to go, msg, pkt, pkt_type, args, name]
        self.events = []    # ("take" | "answer", pkt_type, pkt, args, name)

    def name(self, pkt_type, args):
        if pkt_type not in NAME_ARG:
            return None
        addr = args[NAME_ARG[pkt_type]] << 2
        return bytes(self.mem.r_block(addr + 1, self.mem.r8(addr))).decode()

    def run(self, max_cycles=None):
        port = self.state.port_addr
        while self.ports.has_msg(port) and not (self.in_order and self.waiting):
            msg = self.ports.get_msg(port)
            pkt = msg + self.launcher._msg_size
            pkt_type = self.mem.r32(pkt + self.launcher._dp_type_offset)
            args = [self.mem.r32(pkt + self.launcher._dp_arg1_offset + 4 * i)
                    for i in range(4)]
            name = self.name(pkt_type, args)
            self.events.append(("take", pkt_type, pkt, args, name))
            self.waiting.append(
                [self.delay(pkt_type, name), msg, pkt, pkt_type, args, name])
        for entry in self.waiting:
            entry[0] -= 1
        for entry in [e for e in self.waiting if e[0] < 0]:
            self.waiting.remove(entry)
            _, msg, pkt, pkt_type, args, name = entry
            res1, res2 = self.answer(pkt_type, args, name)
            self.mem.w32(pkt + DP_RES1, res1 & 0xFFFFFFFF)
            self.mem.w32(pkt + DP_RES2, res2 & 0xFFFFFFFF)
            self.ports.put_msg(self.state.reply_port_addr, msg)
            self.events.append(("answer", pkt_type, pkt, args, name))
        busy = any(e[0] != NEVER and e[0] >= 0 for e in self.waiting)
        return BUSY if busy else PARKED

    def answer_now(self, pkt_type):
        """Answer the waiting packets of pkt_type in the next run."""
        for entry in self.waiting:
            if entry[3] == pkt_type:
                entry[0] = 0

    def taken(self, pkt_type):
        return [e for e in self.events if e[0] == "take" and e[1] == pkt_type]


@pytest.fixture
def bridge():
    """A writable HandlerBridge on a scripted handler; returns (bridge, handler)."""
    mem = MockMemory(size_kib=1024)
    alloc = MemoryAlloc(mem, addr=0x1000)
    ports = Ports()
    vh = SimpleNamespace(
        alloc=alloc, scheduler=None, timer_device=None, shutdown=lambda: None,
        slm=SimpleNamespace(exec_impl=SimpleNamespace(port_mgr=ports)))
    launcher = HandlerLauncher(vh, {}, 0)
    state = SimpleNamespace(
        port_addr=alloc.alloc_struct(MsgPortStruct, label="port").addr,
        reply_port_addr=alloc.alloc_struct(MsgPortStruct, label="reply").addr,
        crashed=False, exited=False, initialized=True)
    ports.ports[state.port_addr] = []
    ports.ports[state.reply_port_addr] = []
    handler = Handler(launcher, mem, ports, state)
    # the scripted handler stands in for the scheduler's run
    launcher.run = handler.run

    b = fs.HandlerBridge.__new__(fs.HandlerBridge)
    b._lock = threading.RLock()
    b._debug = False
    b._closed = False
    b._busy_ran_out = False
    b.vh, b.mem, b.launcher, b.state = vh, mem, launcher, state
    b.backend = SimpleNamespace(sync=lambda: None, fsync=lambda: None,
                                close=lambda: None)
    b._volname = None
    b._fib_mem = None
    b._datestamp_mem = None
    b._read_buf_mem = None
    b._read_buf_size = 0
    b._bstr_ring = []
    b._bstr_sizes = []
    b._bstr_index = 0
    b._bstr_ring_size = 8
    b._fh_pool = []
    b._fh_mem = {}
    b._unwritten_outputs = set()
    b._neg_cache = {}
    b._neg_cache_ttl = 0.0
    b._write_enabled = True
    return b, handler


def _bstr(mem, bptr):
    addr = bptr << 2
    return bytes(mem.r_block(addr + 1, mem.r8(addr))).decode()


def _open(b, handler, path, fh_args):
    """Open path for update; the handler's answer fills in fh_Args."""
    def answer(pkt_type, args, name):
        if pkt_type == ACTION_FINDUPDATE:
            handler.mem.w32((args[0] << 2) + FH_ARGS, fh_args)
        return -1, 0
    handler.answer = answer
    fh, _ = b.open_file(path, os.O_RDWR)
    return fh


def test_no_packet_is_built_where_a_packet_still_out_lies(bridge):
    b, handler = bridge
    handler.delay = lambda pkt_type, name: NEVER if name == "held" else 0
    handler.answer = lambda pkt_type, args, name: (0x400, 0)

    assert b.locate(0, "held") == (0, -1)
    for i in range(9):
        assert b.locate(0, f"name{i}") == (0x400, 0)

    (_, _, held_pkt, held_args, _), *later = handler.taken(ACTION_LOCATE_OBJECT)
    assert len(later) == 9
    assert held_pkt not in [pkt for _, _, pkt, _, _ in later]
    # The handler still finds its own packet and name where it left them.
    assert b.mem.r32(held_pkt + b.launcher._dp_type_offset) == ACTION_LOCATE_OBJECT
    assert _bstr(b.mem, held_args[1]) == "held"


def test_a_late_reply_names_its_own_packet_only(bridge):
    b, handler = bridge
    # The late reply comes a burst before the answer to "after".
    handler.delay = lambda pkt_type, name: {"held": NEVER, "after": 1}.get(name, 0)
    handler.answer = lambda pkt_type, args, name: (
        (0x777, 0) if name == "held" else (0x400, 0))

    assert b.locate(0, "held") == (0, -1)
    # After seven more, "after" is the ninth packet: in a ring of eight it
    # would go where "held" still lies.
    for i in range(7):
        assert b.locate(0, f"name{i}") == (0x400, 0)
    handler.answer_now(ACTION_LOCATE_OBJECT)
    # The late reply for "held" comes in this wait and is not its answer.
    assert b.locate(0, "after") == (0x400, 0)


def test_a_write_still_out_keeps_its_data_buffer(bridge):
    b, handler = bridge
    fh = _open(b, handler, "/file", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_WRITE else 0

    def answer(pkt_type, args, name):
        if pkt_type == ACTION_READ:
            handler.mem.w_block(args[1], b"B" * args[2])
            return args[2], 0
        return -1, 0
    handler.answer = answer

    assert b._write_packets(fh, b"A" * 100) == (-1, 0)
    assert b.read_handle(fh, 10) == b"B" * 10

    (_, _, _, write_args, _), = handler.taken(ACTION_WRITE)
    (_, _, _, read_args, _), = handler.taken(ACTION_READ)
    assert read_args[1] != write_args[1]
    # The handler can still copy out what the write asked for.
    assert bytes(b.mem.r_block(write_args[1], 100)) == b"A" * 100


def test_a_late_reply_frees_the_buffer_it_held(bridge):
    b, handler = bridge
    fh = _open(b, handler, "/file", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_WRITE else 0
    handler.answer = lambda pkt_type, args, name: (
        (args[2], 0) if pkt_type == ACTION_READ else (0x400, 0))

    assert b._write_packets(fh, b"A" * 100) == (-1, 0)
    assert b.read_handle(fh, 10) is not None  # the write's buffer is set aside
    (_, _, _, write_args, _), = handler.taken(ACTION_WRITE)
    assert write_args[1] in b.vh.alloc.mem_objs

    handler.answer_now(ACTION_WRITE)
    # The late reply comes in this wait, and nothing is allocated after it.
    assert b.locate(0, "next") == (0x400, 0)

    assert write_args[1] not in b.vh.alloc.mem_objs
    assert b._retired_data == []


def test_a_set_date_still_out_keeps_its_date_stamp(bridge):
    b, handler = bridge
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_SET_DATE else 0
    handler.answer = lambda pkt_type, args, name: (-1, 0)
    first, second = 1_000_000_000.0, 1_700_000_000.0

    assert b.set_date(0x100, "first", first) == (0, -1)
    handler.delay = lambda pkt_type, name: 0
    assert b.set_date(0x100, "second", second) == (-1, 0)

    (_, _, _, first_args, _), (_, _, _, second_args, _) = handler.taken(ACTION_SET_DATE)
    assert second_args[3] != first_args[3]
    # The handler still finds the first file's date where it left it.
    stamp = tuple(b.mem.r32(first_args[3] + offset) for offset in (0, 4, 8))
    assert stamp == fs.to_datestamp(first)
    assert first_args[3] in b.vh.alloc.mem_objs

    handler.answer_now(ACTION_SET_DATE)
    # The late reply comes in this wait, and frees the first DateStamp.
    assert b.locate(0, "next") == (-1, 0)
    assert first_args[3] not in b.vh.alloc.mem_objs
    assert b._retired == []


def test_io_fails_once_too_much_memory_is_held(bridge, monkeypatch):
    b, handler = bridge
    monkeypatch.setattr(fs.HandlerBridge, "_RETIRED_MAX", 3 * 1000)
    fh = _open(b, handler, "/file", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_READ else 0
    handler.answer = lambda pkt_type, args, name: (
        (args[2], 0) if pkt_type == ACTION_READ else (0x400, 0))

    # Each read the handler leaves unanswered sets the last one's buffer
    # aside: three of them fit.
    for _ in range(4):
        assert b.read_handle(fh, 1000) is None
    assert len(handler.taken(ACTION_READ)) == 4
    assert sum(m.size for m in b._retired_data) == 3 * 1000

    # The fourth buffer would pass the cap: the I/O fails, and sends nothing.
    assert b.read_handle(fh, 1000) is None
    assert b._write_packets(fh, b"A" * 1000) == (-1, 0)
    assert len(handler.taken(ACTION_READ)) == 4
    assert not handler.taken(ACTION_WRITE)
    with pytest.raises(OSError) as exc:
        b._alloc_read_buf(1000)
    assert exc.value.errno == errno.EIO

    # Once the handler answers them, I/O works again.
    handler.answer_now(ACTION_READ)
    assert b.locate(0, "next") == (0x400, 0)
    assert b._retired_data == []
    handler.delay = lambda pkt_type, name: 0
    assert b.read_handle(fh, 1000) == bytes(1000)


def test_io_at_the_cap_takes_the_replies_already_queued(bridge, monkeypatch):
    b, handler = bridge
    monkeypatch.setattr(fs.HandlerBridge, "_RETIRED_MAX", 3 * 1000)
    fh = _open(b, handler, "/file", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_READ else 0
    handler.answer = lambda pkt_type, args, name: (
        (args[2], 0) if pkt_type == ACTION_READ else (0x400, 0))
    for _ in range(5):
        assert b.read_handle(fh, 1000) is None
    assert len(handler.taken(ACTION_READ)) == 4  # the fifth was past the cap

    # The handler answers them while the mount idles: the idle timers
    # run it on, and take no replies.
    handler.answer_now(ACTION_READ)
    handler.run()
    events = len(handler.events)

    # The next read recovers by itself: no other packet goes out first.
    handler.delay = lambda pkt_type, name: 0
    assert b.read_handle(fh, 1000) == bytes(1000)
    assert [(kind, pkt_type) for kind, pkt_type, *_ in handler.events[events:]] == [
        ("take", ACTION_READ), ("answer", ACTION_READ)]
    assert b._retired_data == []


def _reads_run_out_at_the_cap(b, handler, monkeypatch):
    """Fill the cap with reads whose waits run out on a handler still at work."""
    monkeypatch.setattr(fs.HandlerBridge, "_RETIRED_MAX", 3 * 1000)
    fh = _open(b, handler, "/file", 0x5550)
    handler.delay = lambda pkt_type, name: 10_000
    handler.answer = lambda pkt_type, args, name: (
        (args[2], 0) if pkt_type == ACTION_READ else (0x400, 0))
    for _ in range(4):
        assert b.read_handle(fh, 1000) is None
    assert b._busy_ran_out
    return fh


def test_a_late_reply_taken_at_the_cap_restores_the_full_limit(
        bridge, short_limits, monkeypatch):
    # #330: the handler answers the reads while the mount idles and goes
    # straight on. The next read takes it 31 runs, past the short limit.
    b, handler = bridge
    fh = _reads_run_out_at_the_cap(b, handler, monkeypatch)
    handler.answer_now(ACTION_READ)
    handler.run()

    handler.delay = lambda pkt_type, name: 30
    assert b.read_handle(fh, 1000) == bytes(1000)
    assert not b._busy_ran_out


def test_a_read_that_fails_at_the_cap_leaves_the_handler_to_the_idle_timers(
        bridge, short_limits, monkeypatch):
    # The idle timers had given the handler up as caught in a loop, but it
    # answers a lookup. The reply frees no read buffer, so the read fails
    # and sends nothing: with no packet to wait for, the idle timers must
    # run the handler on again, or the buffers never come free.
    b, handler = bridge
    fh = _reads_run_out_at_the_cap(b, handler, monkeypatch)
    assert b.locate(0, "late") == (0, -1)
    handler.answer_now(ACTION_LOCATE_OBJECT)
    handler.run()
    b._idle_busy_cycles = fs.HandlerBridge._MAX_BUSY_CYCLES

    assert b.read_handle(fh, 1000) is None
    assert handler.taken(ACTION_FREE_LOCK) == []  # sent, and nothing ran since

    b.vh.timer_device = SimpleNamespace(
        MIN_DELAY=0.02, deliveries=0, deliver=lambda fast_forward=False: {},
        next_deadline=lambda: math.inf)
    assert b.run_due_timers() == 0.02
    assert handler.taken(ACTION_FREE_LOCK)  # the handler ran on


def _at_the_cap(b, handler, monkeypatch):
    """Fill the cap with reads never answered, and hold every small buffer.

    Lookups never answered hold every name BSTR, and an examine never
    answered the FIB, so that the requests after them set those aside.
    The handler answers every other packet at once: a lookup with lock
    0x400, an examine as a file. Returns the FileHandle of /file.
    """
    monkeypatch.setattr(fs.HandlerBridge, "_RETIRED_MAX", 3 * 1000)
    held = {f"h{i}" for i in range(8)}
    examine_held = [True]

    def delay(pkt_type, name):
        if pkt_type == ACTION_EXAMINE_OBJECT and examine_held:
            examine_held.clear()
            return NEVER
        return NEVER if pkt_type == ACTION_READ or name in held else 0
    handler.delay = delay

    def answer(pkt_type, args, name):
        if pkt_type in (ACTION_FINDINPUT, ACTION_FINDUPDATE, ACTION_FINDOUTPUT):
            handler.mem.w32((args[0] << 2) + FH_ARGS, 0x5550)
            return -1, 0
        if pkt_type == ACTION_LOCATE_OBJECT:
            return 0x400, 0
        if pkt_type == ACTION_EXAMINE_OBJECT:
            handler.mem.w32((args[1] << 2) + 4, -3 & 0xFFFFFFFF)  # ST_FILE
            return -1, 0
        if pkt_type in (ACTION_READ, ACTION_WRITE):
            return args[2], 0
        return -1, 0
    handler.answer = answer

    fh, _ = b.open_file("/file", os.O_RDWR)
    for _ in range(4):
        assert b.read_handle(fh, 1000) is None
    for name in sorted(held):
        assert b.locate(0, name) == (0, -1)
    assert b.list_dir(0x400) == []
    # The reads fill the cap; nothing small has been set aside yet.
    assert sum(m.size for m in b._retired_data) == 3 * 1000
    assert not b._retired
    return fh


def _locks_out(handler):
    """Locks the handler granted that have not been freed."""
    granted = [e for e in handler.events
               if e[0] == "answer" and e[1] == ACTION_LOCATE_OBJECT]
    return len(granted) - len(handler.taken(ACTION_FREE_LOCK))


def _data_still_capped(b, handler, fh):
    """A read or write past the cap fails, and sends nothing."""
    reads = len(handler.taken(ACTION_READ))
    writes = len(handler.taken(ACTION_WRITE))
    assert b.read_handle(fh, 1000) is None
    assert b._write_packets(fh, b"A" * 1000) == (-1, 0)
    assert len(handler.taken(ACTION_READ)) == reads
    assert len(handler.taken(ACTION_WRITE)) == writes
    assert sum(m.size for m in b._retired_data) == 3 * 1000


def test_at_the_cap_a_lookup_and_an_open_still_work(bridge, monkeypatch):
    b, handler = bridge
    fh = _at_the_cap(b, handler, monkeypatch)
    ops = fs.AmigaFuseFS(b)
    out = _locks_out(handler)

    lock, res2, locks = b.locate_path("/a/b/c")
    assert (lock, res2, locks) == (0x400, 0, [0x400])
    b.free_lock(lock)
    assert b.list_dir_path("/a") == []
    handle = ops.open("/a/file", os.O_RDWR)
    assert ops._fh_cache[handle]["fh_addr"]
    ops.release("/a/file", handle)

    assert _locks_out(handler) == out
    # The name BSTRs and the FIB those requests needed were set aside.
    assert b._retired
    _data_still_capped(b, handler, fh)


def test_at_the_cap_an_unlink_still_works(bridge, monkeypatch):
    b, handler = bridge
    fh = _at_the_cap(b, handler, monkeypatch)
    ops = fs.AmigaFuseFS(b)
    out = _locks_out(handler)

    assert ops.unlink("/a/gone") == 0

    (_, _, _, args, _), = handler.taken(ACTION_DELETE_OBJECT)
    assert _bstr(b.mem, args[1]) == "gone"
    assert _locks_out(handler) == out
    _data_still_capped(b, handler, fh)


def test_at_the_cap_a_rename_replace_still_works(bridge, monkeypatch):
    b, handler = bridge
    fh = _at_the_cap(b, handler, monkeypatch)
    ops = fs.AmigaFuseFS(b)
    out = _locks_out(handler)
    answer = handler.answer
    renames, deletes = [], []

    def exists_once(pkt_type, args, name):
        # Names are read as the packet is answered: later packets reuse
        # the BSTRs.
        if pkt_type == ACTION_RENAME_OBJECT:
            renames.append((_bstr(b.mem, args[1]), _bstr(b.mem, args[3])))
            # The first rename finds the target there: rename() replaces it.
            if len(renames) == 1:
                return 0, fs.ERROR_OBJECT_EXISTS
        if pkt_type == ACTION_DELETE_OBJECT:
            deletes.append(_bstr(b.mem, args[1]))
        return answer(pkt_type, args, name)
    handler.answer = exists_once

    assert ops.rename("/a/new", "/a/old") == 0

    temp = renames[1][1]
    assert renames == [("new", "old"), ("old", temp), ("new", "old")]
    assert temp.startswith(".amifuse-")
    assert deletes == [temp]
    assert _locks_out(handler) == out
    _data_still_capped(b, handler, fh)


def test_at_the_cap_an_unwritten_truncating_open_still_empties_the_file(
        bridge, monkeypatch):
    # BFFS empties a file opened with ACTION_FINDOUTPUT only once its
    # handle has written; close_file() sends a write of no bytes for that.
    b, handler = bridge
    fh = _at_the_cap(b, handler, monkeypatch)
    writes = len(handler.taken(ACTION_WRITE))

    (out_fh, lock), res2 = b.open_file_reply("/a/t", os.O_RDWR | os.O_TRUNC)
    assert res2 == 0
    b.close_file(out_fh)
    b.free_lock(lock)

    (_, _, _, args, _), = handler.taken(ACTION_WRITE)[writes:]
    assert args[2] == 0
    kinds = [(kind, pkt_type) for kind, pkt_type, *_ in handler.events]
    assert kinds.index(("answer", ACTION_WRITE)) < kinds.index(("take", ACTION_END))
    _data_still_capped(b, handler, fh)


def test_a_truncate_whose_reopen_raises_leaves_its_handle_closed(bridge, monkeypatch):
    # BFFS's truncate to 0 ends the handle, then opens the file again
    # (_reopen_for_output), and that open raises, as one does when the
    # emulated RAM runs out.
    b, handler = bridge
    file_ids = {"file": 0x5550, "other": 0x6660}

    def answer(pkt_type, args, name):
        if pkt_type in (ACTION_FINDUPDATE, ACTION_FINDOUTPUT):
            handler.mem.w32((args[0] << 2) + FH_ARGS, file_ids.get(name, 0x8880))
            return -1, 0
        return (0x400, 0) if pkt_type == ACTION_LOCATE_OBJECT else (-1, 0)
    handler.answer = answer

    ops = fs.AmigaFuseFS(b)
    handle = ops.open("/file", os.O_RDWR)
    entry = ops._fh_cache[handle]
    fh = entry["fh_addr"]
    monkeypatch.setattr(b, "truncate_handle", lambda fh, length, others=(), **_: (
        -1, fs.ERROR_OBJECT_NOT_FOUND))
    open_file_reply = b.open_file_reply
    no_memory = [True]

    def open_file_reply_once_out_of_memory(path, flags=os.O_RDONLY):
        if no_memory:
            no_memory.clear()
            raise VamosInternalError("[alloc: NO MEMORY for 000100 bytes]")
        return open_file_reply(path, flags)
    monkeypatch.setattr(b, "open_file_reply", open_file_reply_once_out_of_memory)

    with pytest.raises(VamosInternalError):
        ops.truncate("/file", 0, handle)
    assert entry["closed"] and entry["fh_addr"] == 0
    assert b._fh_pool == [fh]

    # Another file gets the FileHandle, and the closed entry's release
    # does not end that file's handle.
    other = ops.open("/other", os.O_RDWR)
    assert ops._fh_cache[other]["fh_addr"] == fh
    ops.release("/file", handle)
    ended = [args[0] for _, _, _, args, _ in handler.taken(ACTION_END)]
    assert ended == [file_ids["file"]]
    assert b._fh_pool == []


def test_a_truncate_whose_write_of_no_bytes_raises_leaves_its_handle_closed(
        bridge, monkeypatch):
    # BFFS's truncate to 0 ends the handle and opens the file again with
    # ACTION_FINDOUTPUT (_reopen_for_output), and the write of no bytes
    # raises, as it does when the emulated RAM runs out. Kept, the new
    # handle would cut the file at release after any later write.
    b, handler = bridge
    file_ids = iter(range(0x5550, 0x5600, 0x10))

    def answer(pkt_type, args, name):
        if pkt_type in (ACTION_FINDUPDATE, ACTION_FINDOUTPUT):
            handler.mem.w32((args[0] << 2) + FH_ARGS, next(file_ids))
            return -1, 0
        return (0x400, 0) if pkt_type == ACTION_LOCATE_OBJECT else (-1, 0)
    handler.answer = answer

    ops = fs.AmigaFuseFS(b)
    handle = ops.open("/file", os.O_RDWR)
    entry = ops._fh_cache[handle]
    monkeypatch.setattr(b, "truncate_handle", lambda fh, length, others=(), **_: (
        -1, fs.ERROR_OBJECT_NOT_FOUND))

    def no_memory():
        raise VamosInternalError("[alloc: NO MEMORY for 000004 bytes]")
    monkeypatch.setattr(b, "_alloc_empty_buf", no_memory)

    with pytest.raises(VamosInternalError):
        ops.truncate("/file", 0, handle)
    assert entry["closed"] and entry["fh_addr"] == 0
    assert entry["parent_lock"] == 0
    # Both handles ended, the new one with no write.
    ended = [args[0] for _, _, _, args, _ in handler.taken(ACTION_END)]
    assert ended == [0x5550, 0x5560]
    assert handler.taken(ACTION_WRITE) == []

    ops.release("/file", handle)
    assert len(handler.taken(ACTION_END)) == 2
    assert handler.taken(ACTION_WRITE) == []


@pytest.mark.parametrize("failing", [1, 2], ids=["new-lock", "old-lock"])
def test_a_truncate_whose_lock_free_raises_keeps_no_pooled_handle(
        bridge, monkeypatch, failing):
    # BFFS's truncate to 0 ends the handle and opens the file again
    # (_reopen_for_output). The write that empties it is refused, so the
    # new handle ends too, and the file is opened for update. Freeing the
    # new handle's lock, or the old one's, raises, as it does when the
    # emulated RAM runs out.
    b, handler = bridge
    locks = iter(range(0x400, 0x500))

    def answer(pkt_type, args, name):
        if pkt_type in (ACTION_FINDUPDATE, ACTION_FINDOUTPUT):
            handler.mem.w32((args[0] << 2) + FH_ARGS, 0x5550)
            return -1, 0
        if pkt_type == ACTION_WRITE:
            return -1, fs.ERROR_DISK_FULL
        return (next(locks), 0) if pkt_type == ACTION_LOCATE_OBJECT else (-1, 0)
    handler.answer = answer

    ops = fs.AmigaFuseFS(b)
    handle = ops.open("/file", os.O_RDWR)
    entry = ops._fh_cache[handle]
    monkeypatch.setattr(b, "truncate_handle", lambda fh, length, others=(), **_: (
        -1, fs.ERROR_OBJECT_NOT_FOUND))
    free_lock = b.free_lock
    freed = []

    def free_lock_failing_once(lock):
        freed.append(lock)
        if len(freed) == failing:
            raise VamosInternalError("[alloc: NO MEMORY for 000100 bytes]")
        free_lock(lock)
    monkeypatch.setattr(b, "free_lock", free_lock_failing_once)

    with pytest.raises(VamosInternalError):
        ops.truncate("/file", 0, handle)
    assert len(freed) == 2 and len(set(freed)) == 2
    # The entry names neither a FileHandle the next open can take nor a
    # lock already freed: closed after the new lock's failure, before the
    # file was opened for update, and holding that update handle after
    # the old lock's.
    assert entry["fh_addr"] not in b._fh_pool
    assert entry["parent_lock"] not in freed
    assert entry["closed"] == (failing == 1)


def test_a_file_handle_goes_back_to_the_pool_once(bridge):
    b, handler = bridge
    fh = _open(b, handler, "/file", 0x5550)

    b._free_fh(fh)
    b._free_fh(fh)

    assert b._fh_pool == [fh]


def test_an_open_still_out_keeps_its_file_handle(bridge):
    b, handler = bridge
    handler.delay = lambda pkt_type, name: NEVER if name == "held" else 0

    assert b.open_file("/held") is None
    fh = _open(b, handler, "/other", 0x5550)

    (_, _, _, held_args, _), = handler.taken(ACTION_FINDINPUT)
    assert fh is not None and fh != held_args[0] << 2


def test_a_dropped_late_lock_is_freed(bridge):
    b, handler = bridge
    handler.delay = lambda pkt_type, name: NEVER if name == "late" else 0
    handler.answer = lambda pkt_type, args, name: (
        (0x777, 0) if name == "late" else (0x400, 0))

    assert b.locate(0, "late") == (0, -1)
    handler.answer_now(ACTION_LOCATE_OBJECT)
    assert b.locate(0, "next") == (0x400, 0)
    b.locate(0, "after")  # the handler takes what was queued meanwhile

    assert [args[0] for _, _, _, args, _ in handler.taken(ACTION_FREE_LOCK)] == [0x777]


def test_a_dropped_late_open_is_ended(bridge):
    b, handler = bridge
    handler.delay = lambda pkt_type, name: NEVER if name == "late" else 0

    def answer(pkt_type, args, name):
        if pkt_type == ACTION_FINDINPUT:
            handler.mem.w32((args[0] << 2) + FH_ARGS, 0x6660)
        return (-1, 0) if pkt_type != ACTION_LOCATE_OBJECT else (0x400, 0)
    handler.answer = answer

    assert b.open_file("/late") is None
    handler.answer_now(ACTION_FINDINPUT)
    assert b.locate(0, "next") == (0x400, 0)
    b.locate(0, "after")

    assert [args[0] for _, _, _, args, _ in handler.taken(ACTION_END)] == [0x6660]


def _mount(b):
    ops = fs.AmigaFuseFS.__new__(fs.AmigaFuseFS)
    ops.bridge = b
    ops._unmount_control = None
    ops._mountpoint = None
    return ops


@pytest.fixture
def short_limits(monkeypatch):
    # A wait gives up after 60 busy runs, or 10 once one has run out.
    chunk = fs.HandlerBridge._RUN_CHUNK_CYCLES
    monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES", 60 * chunk)
    monkeypatch.setattr(fs.HandlerBridge, "_SHORT_BUSY_CYCLES", 10 * chunk)


def test_unmount_waits_for_a_packet_still_out_before_the_flush(bridge, short_limits, capsys):
    b, handler = bridge
    fh = _open(b, handler, "/grow", 0x5550)
    # A grow that outlasts both waits of set_handle_size: 100 runs.
    handler.delay = lambda pkt_type, name: 100 if pkt_type == ACTION_SET_FILE_SIZE else 0
    assert b.set_handle_size(fh, 0x7FFFFFFF) == -1

    _mount(b).destroy(None)

    kinds = [(kind, pkt_type) for kind, pkt_type, *_ in handler.events]
    before_flush = kinds[:kinds.index(("take", ACTION_FLUSH))]
    assert ("answer", ACTION_SET_FILE_SIZE) in before_flush
    assert "Unmount complete." in capsys.readouterr().out


def test_unmount_reports_a_packet_never_answered(bridge, short_limits, capsys):
    b, handler = bridge
    fh = _open(b, handler, "/grow", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_SET_FILE_SIZE else 0
    assert b.set_handle_size(fh, 0x7FFFFFFF) == -1

    _mount(b).destroy(None)

    out = capsys.readouterr().out
    assert "ACTION_SET_FILE_SIZE" in out
    assert "Unmount completed with errors." in out
    # The volume is still flushed.
    assert handler.taken(ACTION_FLUSH)


def _busy_for_good(b, handler, monkeypatch, parks_once=False, never=(ACTION_SET_FILE_SIZE,)):
    """Leave a packet out with a handler still at work on it, 1 s a run.

    With parks_once the first run finds it waiting instead. Returns the
    fake clock, a list holding the time.monotonic() value.
    """
    fh = _open(b, handler, "/grow", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type in never else 0
    assert b.set_handle_size(fh, 0x7FFFFFFF) == -1

    now = [1000.0]
    monkeypatch.setattr(fs.time, "monotonic", lambda: now[0])
    runs = []

    def run(max_cycles=None):
        handler.run(max_cycles)
        now[0] += 1.0
        runs.append(1)
        return PARKED if parks_once and len(runs) == 1 else BUSY
    b.launcher.run = run
    return now


def test_unmount_wait_ends_at_its_deadline(bridge, short_limits, monkeypatch):
    b, handler = bridge
    now = _busy_for_good(b, handler, monkeypatch)

    unanswered = b.wait_for_pending(timeout=15.0)
    assert now[0] - 1000.0 == 15.0
    assert unanswered == [fs._packet_type_name(ACTION_SET_FILE_SIZE)]


def test_unmount_wait_ends_when_the_handler_waits(bridge, short_limits, monkeypatch):
    # Every task waits and no timer is pending: nothing more comes, so
    # the wait does not run to its deadline.
    b, handler = bridge
    now = _busy_for_good(b, handler, monkeypatch, parks_once=True)

    unanswered = b.wait_for_pending(timeout=15.0)
    assert now[0] - 1000.0 == 1.0
    assert unanswered == [fs._packet_type_name(ACTION_SET_FILE_SIZE)]


def test_unmount_flush_after_the_deadline_gets_the_short_limit(
        bridge, short_limits, monkeypatch, capsys):
    # Still at work on the grow, the handler takes no ACTION_FLUSH either.
    b, handler = bridge
    now = _busy_for_good(b, handler, monkeypatch,
                         never=(ACTION_SET_FILE_SIZE, ACTION_FLUSH))

    _mount(b).destroy(None)

    # 15 s of waiting for the grow, 10 runs for the flush, then 1 s more
    # for the packets still out, which are named.
    assert now[0] - 1000.0 == 26.0
    out = capsys.readouterr().out
    assert "has not answered" in out
    assert "Unmount completed with errors." in out


def _io_reply_port(b, handler):
    """A port of the handler's own for IO replies, and a message for it."""
    io_port, io_msg = (b.vh.alloc.alloc_memory(64, label=label).addr
                       for label in ("io_port", "io_msg"))
    handler.ports.ports[io_port] = []
    return io_port, io_msg


def test_unmount_wait_leaves_a_handler_at_work_its_io_reply(
        bridge, short_limits, monkeypatch):
    # #240: at work on a packet, the handler sends an IO request whose
    # reply comes at once, and takes it with GetMsg() only after the wait
    # that queued it has ended at the short limit, 10 runs.
    b, handler = bridge
    fh = _open(b, handler, "/grow", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_SET_FILE_SIZE else 0
    assert b.set_handle_size(fh, 0x7FFFFFFF) == -1
    io_port, io_msg = _io_reply_port(b, handler)
    ports = handler.ports
    now = [1000.0]
    monkeypatch.setattr(fs.time, "monotonic", lambda: now[0])
    runs, got = [0], []

    def run(max_cycles=None):
        now[0] += 1.0
        runs[0] += 1
        if runs[0] == 1:
            ports.put_msg(io_port, io_msg)  # SendIO()
        elif runs[0] == 12 and ports.has_msg(io_port):
            got.append(ports.get_msg(io_port))  # GetMsg(), then the answer
            handler.answer_now(ACTION_SET_FILE_SIZE)
        result = handler.run(max_cycles)
        return BUSY if not got else result
    b.launcher.run = run

    assert b.wait_for_pending(timeout=15.0) == []
    assert got == [io_msg]


def test_unmount_wait_drains_io_replies_once_the_handler_waits(
        bridge, short_limits):
    b, handler = bridge
    fh = _open(b, handler, "/grow", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_SET_FILE_SIZE else 0
    assert b.set_handle_size(fh, 0x7FFFFFFF) == -1
    io_port, io_msg = _io_reply_port(b, handler)

    def run(max_cycles=None):
        # A reply it will not take, then the answer, and it waits again.
        handler.ports.put_msg(io_port, io_msg)
        handler.answer_now(ACTION_SET_FILE_SIZE)
        return handler.run(max_cycles)
    b.launcher.run = run

    assert b.wait_for_pending() == []
    assert not handler.ports.has_msg(io_port)


def test_unmount_wait_leaves_a_handler_at_work_its_io_reply_on_a_late_reply(
        bridge, short_limits):
    # #240: the wait gets one packet's reply, and that ends the short
    # limit, while the handler goes on with the other packet still out.
    b, handler = bridge
    fh = _open(b, handler, "/grow", 0x5550)
    handler.delay = lambda pkt_type, name: (
        NEVER if pkt_type in (ACTION_SET_FILE_SIZE, ACTION_LOCATE_OBJECT) else 0)
    assert b.set_handle_size(fh, 0x7FFFFFFF) == -1
    assert b.locate(0, "late") == (0, -1)
    io_port, io_msg = _io_reply_port(b, handler)
    ports = handler.ports
    runs, got = [0], []

    def run(max_cycles=None):
        runs[0] += 1
        if runs[0] == 1:
            ports.put_msg(io_port, io_msg)  # SendIO()
            handler.answer_now(ACTION_LOCATE_OBJECT)
            handler.run(max_cycles)
            return BUSY
        if ports.has_msg(io_port):
            got.append(ports.get_msg(io_port))  # GetMsg(), then the answer
            handler.answer_now(ACTION_SET_FILE_SIZE)
        return handler.run(max_cycles)
    b.launcher.run = run

    assert b.wait_for_pending() == []
    assert got == [io_msg]


def test_a_wait_that_ends_on_a_reply_leaves_a_handler_at_work_its_io_reply(bridge):
    # #240: two packets are queued. The handler answers the first and, in
    # the same burst, starts on the second with a SendIO(). The burst ends
    # before it takes the reply, and the first answer ends the wait.
    b, handler = bridge
    handler.answer = lambda pkt_type, args, name: (0x400, 0)
    handler.delay = lambda pkt_type, name: NEVER if name == "second" else 0
    io_port, io_msg = _io_reply_port(b, handler)
    ports = handler.ports
    runs, got = [0], []

    def run(max_cycles=None):
        runs[0] += 1
        if runs[0] == 1:
            ports.put_msg(io_port, io_msg)
            handler.run(max_cycles)
            return BUSY
        handler.run(max_cycles)
        if ports.has_msg(io_port):
            got.append(ports.get_msg(io_port))
            handler.answer_now(ACTION_LOCATE_OBJECT)
        return handler.run(max_cycles)
    b.launcher.run = run

    assert b.locate(0, "first") == (0x400, 0)
    assert b.locate(0, "second") == (0x400, 0)
    assert got == [io_msg]


def test_a_wait_that_runs_out_leaves_a_handler_at_work_its_io_reply(
        bridge, short_limits):
    # The IO reply stays through the waits that end with the handler at
    # work, and goes with the first that leaves it waiting.
    b, handler = bridge
    handler.delay = lambda pkt_type, name: NEVER
    io_port, io_msg = _io_reply_port(b, handler)
    ports = handler.ports
    ports.put_msg(io_port, io_msg)
    result = [BUSY]

    def run(max_cycles=None):
        handler.run(max_cycles)
        return result[0]
    b.launcher.run = run

    assert b.locate(0, "ran-out") == (0, -1)
    assert b._busy_ran_out
    assert ports.has_msg(io_port)

    result[0] = PARKED
    assert b.locate(0, "parked") == (0, -1)
    assert not b._busy_ran_out
    assert not ports.has_msg(io_port)


def test_startup_rounds_that_end_busy_leave_the_handler_its_io_reply(bridge):
    # #240: PFS3's deferred validation can outlast every round, and the
    # next packet's wait runs it on.
    b, handler = bridge
    io_port, io_msg = _io_reply_port(b, handler)
    handler.ports.put_msg(io_port, io_msg)
    b.launcher.run = lambda max_cycles=None: BUSY

    b._flush_pending_signals()

    assert handler.ports.has_msg(io_port)


def test_unmount_names_the_packets_still_out_when_the_flush_fails(
        bridge, short_limits, capsys):
    # Still at work on the grow, a handler that takes packets in order
    # does not take the flush.
    b, handler = bridge
    fh = _open(b, handler, "/grow", 0x5550)
    handler.delay = lambda pkt_type, name: NEVER if pkt_type == ACTION_SET_FILE_SIZE else 0
    assert b.set_handle_size(fh, 0x7FFFFFFF) == -1
    handler.in_order = True

    _mount(b).destroy(None)

    out = capsys.readouterr().out
    assert not handler.taken(ACTION_FLUSH)
    warning, = [line for line in out.splitlines() if "has not answered" in line]
    assert "ACTION_SET_FILE_SIZE" in warning and "ACTION_FLUSH" in warning
    assert "flush failed" in out
    assert "Unmount completed with errors." in out


def test_a_wait_past_its_deadline_leaves_a_handler_still_at_work_its_io_reply(
        bridge):
    # A wait_for_pending wait whose deadline has passed before it runs the
    # handler, after an earlier wait ran out: the handler is still at work,
    # so a reply on its own port stays for it to take.
    b, handler = bridge
    io_port, io_msg = _io_reply_port(b, handler)
    handler.ports.put_msg(io_port, io_msg)
    runs = []
    b.launcher.run = lambda max_cycles=None: runs.append(max_cycles)
    b._busy_ran_out = True

    b._run_until_replies(deadline=fs.time.monotonic() - 1)

    assert handler.ports.has_msg(io_port)
    assert runs == []
