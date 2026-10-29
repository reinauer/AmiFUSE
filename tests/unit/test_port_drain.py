"""Unit tests for HandlerBridge._drain_non_essential_ports.

After a packet wait the bridge drains stray IO replies from the ports the
handler does not take packets on. It keeps the handler process's own
pr_MsgPort, where SmartFilesystem's front process forwards packets (#188),
and takes what it drains off the port's m68k mp_MsgList as well as the port
manager's queue, as GetMsg() does (#216).
"""

from types import SimpleNamespace

PROCESS = 0x3000       # the handler process amifuse created
DOS_PORT = 0x5000      # the port the startup reply switched state.port_addr to
REPLY_PORT = 0x5100    # the bridge's caller port
IO_PORT = 0x6000       # a port the handler's IO replies come back on
IO = 0x7000            # an IO request replied at once
IO2 = 0x7100
PKT_MSG = 0x7200       # a packet's message


class FakeMem:
    """Big-endian 32-bit memory, enough for Exec list links."""

    def __init__(self, size=0x10000):
        self.data = bytearray(size)

    def r32(self, addr):
        return int.from_bytes(self.data[addr:addr + 4], "big")

    def w32(self, addr, val):
        self.data[addr:addr + 4] = val.to_bytes(4, "big")


class FakeTimer:
    """A timer.device that delays exactly the requests in `delayed`."""

    def __init__(self, delayed=()):
        self.delayed = set(delayed)

    def delays(self, msg_addr):
        return msg_addr in self.delayed


def _msg_list(port):
    from amitools.vamos.libstructs.exec_ import MsgPortStruct  # type: ignore

    return port + MsgPortStruct.sdef.find_field_def_by_name("mp_MsgList").offset


def _new_list(mem, port):
    """An empty mp_MsgList: lh_Head -> &lh_Tail, lh_Tail = 0, lh_TailPred -> &lh_Head."""
    lst = _msg_list(port)
    mem.w32(lst, lst + 4)
    mem.w32(lst + 4, 0)
    mem.w32(lst + 8, lst)


def _add_tail(mem, port, msg):
    """Link msg at the tail of the port's list, as ExecLibrary.SendIO does."""
    lst = _msg_list(port)
    old_tailpred = mem.r32(lst + 8)
    mem.w32(msg, lst + 4)
    mem.w32(msg + 4, old_tailpred)
    mem.w32(old_tailpred, msg)
    mem.w32(lst + 8, msg)


def _list_nodes(mem, port):
    """The nodes linked in the port's list, head to tail."""
    lst = _msg_list(port)
    nodes, node = [], mem.r32(lst)
    while node != lst + 4:
        nodes.append(node)
        node = mem.r32(node)
        assert len(nodes) < 10, "list does not end at lh_Tail"
    return nodes


def _bridge(timer=None):
    from amifuse import fuse_fs as fs
    from amitools.vamos.lib.lexec.PortManager import PortManager  # type: ignore

    bridge = fs.HandlerBridge.__new__(fs.HandlerBridge)
    bridge._debug = False
    bridge.mem = FakeMem()
    bridge.state = SimpleNamespace(
        process_addr=PROCESS, port_addr=DOS_PORT, reply_port_addr=REPLY_PORT)
    bridge.vh = SimpleNamespace(timer_device=timer)
    return bridge, PortManager(None)


def _process_port():
    from amitools.vamos.libstructs.dos import ProcessStruct  # type: ignore

    return PROCESS + ProcessStruct.sdef.find_field_def_by_name("pr_MsgPort").offset


class TestDrainKeepsTheProcessPort:
    def test_packet_forwarded_to_the_process_port_stays(self):
        """A packet on the handler process's pr_MsgPort is not drained.

        state.port_addr is another port, as after SmartFilesystem's startup
        reply; an IO reply on an IO port still goes.
        """
        bridge, pmgr = _bridge()
        proc_port = _process_port()
        pmgr.register_port(proc_port)
        pmgr.register_port(IO_PORT)
        pmgr.put_msg(proc_port, PKT_MSG)
        pmgr.put_msg(IO_PORT, IO)

        bridge._drain_non_essential_ports(pmgr)

        assert pmgr.ports[proc_port].queue == [PKT_MSG]
        assert pmgr.ports[IO_PORT].queue == []


class TestDrainUnlinksWhatItDrops:
    def test_drained_reply_leaves_queue_and_list(self):
        """A reply SendIO queued and linked is gone from both views of the port."""
        bridge, pmgr = _bridge()
        mem = bridge.mem
        pmgr.register_port(IO_PORT)
        _new_list(mem, IO_PORT)
        pmgr.put_msg(IO_PORT, IO)
        _add_tail(mem, IO_PORT, IO)

        bridge._drain_non_essential_ports(pmgr)

        assert pmgr.ports[IO_PORT].queue == []
        lst = _msg_list(IO_PORT)
        assert (mem.r32(lst), mem.r32(lst + 8)) == (lst + 4, lst)

    def test_kept_timer_reply_stays_linked_alone(self):
        """Around a delayed timer reply the drain keeps, the list is rejoined."""
        bridge, pmgr = _bridge(FakeTimer(delayed={IO2}))
        mem = bridge.mem
        pmgr.register_port(IO_PORT)
        _new_list(mem, IO_PORT)
        for msg in (IO, IO2):
            pmgr.put_msg(IO_PORT, msg)
            _add_tail(mem, IO_PORT, msg)

        bridge._drain_non_essential_ports(pmgr)

        assert pmgr.ports[IO_PORT].queue == [IO2]
        assert _list_nodes(mem, IO_PORT) == [IO2]
        lst = _msg_list(IO_PORT)
        assert mem.r32(IO2 + 4) == lst
        assert mem.r32(lst + 8) == IO2
