"""Position and size through the AmigaOS 4 packets 8001-8004.

They go out with DP64_INIT in dp_Res0, which a handler with them leaves, and
it answers ACTION_CHANGE_FILE_POSITION64 and ACTION_CHANGE_FILE_SIZE64 with
DOSTRUE, not the old position or the new size. One without them answers
DOSFALSE and ERROR_ACTION_NOT_KNOWN in a DosPacket's dp_Res1 and dp_Res2, at
offset 12, over DP64_INIT.
"""

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from amitools.vamos.machine.mock.mem import MockMemory

from amifuse import fuse_fs as fs
from amifuse.startup_runner import (
    DP64_INIT, DP64_RES1, OFFSET_BEGINNING, OFFSET_CURRENT, OFFSET_END)

CHANGE_POS, GET_POS, CHANGE_SIZE, GET_SIZE = 8001, 8002, 8003, 8004
FH, FH_ARG = 0x100, 0x2000
BIG = (5 << 30) + 123


class File64:
    """A handler with the 64 bit packets and one file of size bytes."""

    def __init__(self, size=BIG, knows64=True):
        self.size, self.pos, self.knows64 = size, 0, knows64
        self.sent = []
        self.full = False

    def answer(self, ptype, arg1, arg2, arg3):
        self.sent.append(ptype)
        if self.knows64 is None:
            return None
        if not self.knows64:
            return (0, 0, 209)
        if arg1 != FH_ARG:
            return (DP64_INIT, -1 if ptype in (GET_POS, GET_SIZE) else 0, 114)
        if ptype == GET_POS:
            return (DP64_INIT, self.pos, 0)
        if ptype == GET_SIZE:
            return (DP64_INIT, self.size, 0)
        base = {OFFSET_BEGINNING: 0, OFFSET_CURRENT: self.pos,
                OFFSET_END: self.size}[arg3]
        if ptype == CHANGE_POS:
            if not 0 <= base + arg2 <= self.size:
                return (DP64_INIT, 0, fs.ERROR_SEEK_ERROR)
            self.pos = base + arg2
            return (DP64_INIT, -1, 0)
        if self.full and base + arg2 > self.size:
            return (DP64_INIT, 0, 221)
        self.size = base + arg2
        self.pos = min(self.pos, self.size)
        return (DP64_INIT, -1, 0)


def _bridge(handler, pos64=None):
    b = object.__new__(fs.HandlerBridge)
    b._lock = threading.RLock()
    b._debug = False
    b.state = SimpleNamespace(crashed=False)
    b.mem = MockMemory(size_kib=64)
    b.mem.w32(FH + 36, FH_ARG)  # fh_Args
    b._pos64 = pos64
    answers = []

    def send_packet64(state, ptype, arg1, arg2=0, arg3=0):
        answers.append(handler.answer(ptype, arg1, arg2, arg3))
        pkt = 0x1000 + 0x40 * len(answers)
        if answers[-1] is not None:
            b.mem.w32(pkt + DP64_RES1, (answers[-1][1] >> 32) & 0xFFFFFFFF)
            b.mem.w32(pkt + DP64_RES1 + 4, answers[-1][1] & 0xFFFFFFFF)
        return pkt, 0

    b.launcher = SimpleNamespace(send_packet64=send_packet64)

    def run_until_reply(pkt, label):
        answer = answers[(pkt - 0x1000) // 0x40 - 1]
        return None if answer is None else (answer[0], answer[2])

    b._run_until_reply = run_until_reply
    b.launcher.send_seek_handle = MagicMock(side_effect=AssertionError("classic seek"))
    b.launcher.send_set_file_size = MagicMock(side_effect=AssertionError("classic size"))
    return b


def test_the_first_need_asks_once():
    handler = File64()
    b = _bridge(handler)
    assert b.max_file_size() == fs._MAX_FILE_SIZE64
    assert b.max_file_size() == fs._MAX_FILE_SIZE64
    assert handler.sent == [GET_POS]


def test_a_handler_without_them_keeps_the_long_limit():
    for knows64 in (False, None):  # refused, or no reply
        b = _bridge(File64(knows64=knows64))
        assert b.max_file_size() == fs._MAX_FILE_SIZE
        assert b._pos64 is False


def test_seek_past_4_gib_answers_the_old_position():
    b = _bridge(File64(), pos64=True)
    assert b.seek_handle(FH, BIG - 10) == (fs.SeekResult.OK, 0)
    assert b.seek_handle(FH, 0, OFFSET_CURRENT) == (fs.SeekResult.OK, BIG - 10)
    assert b.seek_handle(FH, BIG + 1) == (fs.SeekResult.PAST_END, None)
    assert b.handle_position(FH) == (True, BIG - 10)


def test_end_and_extent_above_4_gib():
    handler = File64()
    b = _bridge(handler, pos64=True)
    b.seek_handle(FH, 7)
    assert b._handle_extent(FH) == (7, BIG)
    assert handler.pos == 7
    assert b._handle_end(FH) == BIG
    assert handler.pos == BIG


def test_set_size_answers_the_size_the_handler_reports():
    handler = File64()
    b = _bridge(handler, pos64=True)
    assert b.set_handle_size_reply(FH, BIG + 4096) == (BIG + 4096, 0)
    assert handler.sent[-2:] == [CHANGE_SIZE, GET_SIZE]
    assert b.set_handle_size_reply(FH, -1) == (-1, 0)
    handler.full = True
    assert b.set_handle_size_reply(FH, BIG + 8192) == (-1, 221)


class RefusesPosition(File64):
    """A handler with the packets that fails GET_FILE_POSITION64 on the file."""

    def answer(self, ptype, arg1, arg2, arg3):
        if ptype == GET_POS and arg1 == FH_ARG:
            self.sent.append(ptype)
            return (DP64_INIT, -1, 211)
        return super().answer(ptype, arg1, arg2, arg3)


def test_a_refused_position_is_no_position():
    """dp_Res1 -1 of a failed GET_FILE_POSITION64 reads as -1, not 2**64 - 1."""
    b = _bridge(RefusesPosition(), pos64=True)
    assert b.seek_handle(FH, 10) == (fs.SeekResult.FAILED, None)
    assert b.handle_position(FH) == (True, None)


def test_the_launcher_builds_a_dospacket64():
    from amitools.vamos.libstructs.dos import DosPacketStruct
    from amitools.vamos.libstructs.exec_ import MessageStruct, NodeStruct
    from amifuse.startup_runner import DP64_SIZE, HandlerLauncher

    launcher = HandlerLauncher.__new__(HandlerLauncher)
    launcher.mem = mem = MockMemory(size_kib=64)
    launcher.alloc = MagicMock()
    launcher.alloc.alloc_memory.return_value = SimpleNamespace(addr=0x1000)
    launcher.port_mgr = MagicMock()
    launcher.exec_impl = SimpleNamespace(port_mgr=launcher.port_mgr)
    launcher._link_msg_to_port = launcher._signal_port = lambda *a: None
    launcher._stdpkt_ring, launcher._stdpkt_sizes, launcher._stdpkt_index = [], [], 0
    launcher._stdpkt_ring_size, launcher._stdpkt_retired = 8, []
    launcher.pending = {}
    launcher._msg_size = MessageStruct.get_size()
    launcher._pkt_size = DosPacketStruct.get_size()
    for name in ("Succ", "Pred", "Type", "Name"):
        setattr(launcher, f"_msg_ln_{name.lower()}_offset",
                NodeStruct.sdef.find_field_def_by_name(f"ln_{name}").offset)
    launcher._mn_replyport_offset = MessageStruct.sdef.find_field_def_by_name("mn_ReplyPort").offset
    launcher._mn_length_offset = MessageStruct.sdef.find_field_def_by_name("mn_Length").offset
    for name in ("link", "port", "type", "arg1"):
        setattr(launcher, f"_dp_{name}_offset",
                DosPacketStruct.sdef.find_field_def_by_name(f"dp_{name.capitalize()}").offset)
    state = SimpleNamespace(port_addr=0x40, reply_port_addr=0x80)

    pkt, msg = launcher.send_packet64(state, CHANGE_POS, FH_ARG, BIG, OFFSET_END)

    assert pkt == msg + launcher._msg_size
    assert DP64_SIZE == 64
    assert mem.r16(msg + launcher._mn_length_offset) == launcher._msg_size + DP64_SIZE
    assert mem.r32s(pkt + 12) == DP64_INIT
    assert mem.r32(pkt + 8) == CHANGE_POS
    assert mem.r32(pkt + 32) == FH_ARG
    assert (mem.r32(pkt + 40) << 32 | mem.r32(pkt + 44)) == BIG
    assert mem.r32s(pkt + 48) == OFFSET_END
    assert launcher.pending[pkt] == (CHANGE_POS, ())
