"""A packet whose wait ran out keeps its memory until the handler answers.

The handler still holds such a packet and answers it later. Until then
the bridge must not build another packet in its StandardPacket, nor reuse
its name BSTRs, its data buffer or its FileHandle (#117). A late reply
that the bridge drops gives back the lock or file it grants (#194). And
unmount runs the handler until every packet has its reply before it
flushes the volume (#118).

A scripted handler on the real HandlerLauncher, in MockMemory, takes each
packet the bridge queues and answers it at once, later, or never.
"""

import math
import os
import threading
from types import SimpleNamespace

import pytest

from amitools.vamos.lib.lexec.signalfunc import SignalFunc
from amitools.vamos.libstructs.dos import DosPacketStruct, FileHandleStruct
from amitools.vamos.libstructs.exec_ import MsgPortStruct
from amitools.vamos.machine.mock.mem import MockMemory
from amitools.vamos.mem.alloc import MemoryAlloc

from amifuse import fuse_fs as fs
from amifuse.startup_runner import (
    ACTION_END,
    ACTION_FINDINPUT,
    ACTION_FINDUPDATE,
    ACTION_FLUSH,
    ACTION_FREE_LOCK,
    ACTION_LOCATE_OBJECT,
    ACTION_READ,
    ACTION_SET_FILE_SIZE,
    ACTION_WRITE,
    HandlerLauncher,
)

# How HandlerLauncher.run_burst reports a burst that ended:
BUSY = SimpleNamespace(done=False, error=False)   # at its cycle limit
PARKED = SimpleNamespace(done=False, error=True)  # in Wait(), nothing pending

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
    """Takes every queued packet in a burst and answers it as scripted.

    delay(pkt_type, name) gives the bursts until the answer: 0 answers
    in the burst that takes it, NEVER not at all. A burst is busy while
    an answer is still to come, as for a handler growing a file, which
    still answers other packets at once. answer(pkt_type, args, name)
    gives (res1, res2), and may write into the memory the args name.
    """

    def __init__(self, launcher, mem, ports, state):
        self.launcher = launcher
        self.mem = mem
        self.ports = ports
        self.state = state
        self.delay = lambda pkt_type, name: 0
        self.answer = lambda pkt_type, args, name: (-1, 0)
        self.waiting = []   # [bursts to go, msg, pkt, pkt_type, args, name]
        self.events = []    # ("take" | "answer", pkt_type, pkt, args, name)

    def name(self, pkt_type, args):
        if pkt_type not in NAME_ARG:
            return None
        addr = args[NAME_ARG[pkt_type]] << 2
        return bytes(self.mem.r_block(addr + 1, self.mem.r8(addr))).decode()

    def run_burst(self, state, max_cycles=200000, debug=False):
        port = self.state.port_addr
        while self.ports.has_msg(port):
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
        state.run_state = BUSY if busy else PARKED
        return state.run_state

    def answer_now(self, pkt_type):
        """Answer the waiting packets of pkt_type in the next burst."""
        for entry in self.waiting:
            if entry[3] == pkt_type:
                entry[0] = 0

    def taken(self, pkt_type):
        return [e for e in self.events if e[0] == "take" and e[1] == pkt_type]


@pytest.fixture(autouse=True)
def _keep_exec_state(monkeypatch):
    # Without a ProcessManager, a parked burst clears the Exec blocking
    # state, which lives on the ExecLibrary class. Leave the real one
    # alone, and the signals the launcher sets on each packet too.
    monkeypatch.setattr(fs, "_clear_all_block_state", lambda: None)
    monkeypatch.setattr(SignalFunc, "_fallback_signals", SignalFunc._fallback_signals)


@pytest.fixture
def bridge():
    """A writable HandlerBridge on a scripted handler; returns (bridge, handler)."""
    mem = MockMemory(size_kib=1024)
    alloc = MemoryAlloc(mem, addr=0x1000)
    ports = Ports()
    vh = SimpleNamespace(
        alloc=alloc, slm=SimpleNamespace(exec_impl=SimpleNamespace(port_mgr=ports)))
    launcher = HandlerLauncher(vh, {}, 0)
    state = SimpleNamespace(
        port_addr=alloc.alloc_struct(MsgPortStruct, label="port").addr,
        reply_port_addr=alloc.alloc_struct(MsgPortStruct, label="reply").addr,
        crashed=False, pc=0x4000, main_loop_pc=0, initialized=True,
        run_state=None, block_state=None)
    ports.ports[state.port_addr] = []
    ports.ports[state.reply_port_addr] = []
    handler = Handler(launcher, mem, ports, state)
    launcher.run_burst = handler.run_burst

    b = fs.HandlerBridge.__new__(fs.HandlerBridge)
    b._lock = threading.RLock()
    b._debug = False
    b._closed = False
    b._busy_ran_out = False
    b._idle_unfinished = False
    b._idle_busy_cycles = 0
    b.vh, b.mem, b.launcher, b.state = vh, mem, launcher, state
    b.backend = SimpleNamespace(sync=lambda: None, fsync=lambda: None,
                                close=lambda: None)
    b._volname = None
    b._fib_mem = None
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
    # A wait gives up after 60 busy bursts, or 10 once one has run out.
    monkeypatch.setattr(fs.HandlerBridge, "_MAX_BUSY_CYCLES", 60 * 200_000)
    monkeypatch.setattr(fs.HandlerBridge, "_SHORT_BUSY_CYCLES", 10 * 200_000)


def test_unmount_waits_for_a_packet_still_out_before_the_flush(bridge, short_limits, capsys):
    b, handler = bridge
    fh = _open(b, handler, "/grow", 0x5550)
    # A grow that outlasts both waits of set_handle_size: 100 bursts.
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
