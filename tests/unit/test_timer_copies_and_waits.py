"""Copied timer requests, and DoIO and WaitIO on a request the device holds.

A handler may open one timerequest and send copies of it (CopyMem, or a
struct assignment), so the device knows its requests by the token
OpenDevice writes into io_Unit, not by address. And Exec in vamos cannot
wait in DoIO or WaitIO, so AmiFuseExecLibrary has the device complete a
held request at once there, leaving no reply to come later.

The fixture and helpers are test_timer_device's: amitools' ExecLibrary
over a fake memory and clock.
"""

import logging
from types import SimpleNamespace

import pytest

from tests.unit.test_timer_device import (  # noqa: F401 (timer is a fixture)
    BASE, IO, IO2, NT_REPLYMSG, PORT, SIGBIT, TASK, TR_ADDREQUEST,
    TR_GETSYSTIME, UNIT_ECLOCK, UNIT_VBLANK, UNIT_WAITUNTIL, _m68k_list, timer,
)

COPY = 0x1200
_IO_UNIT = 24


def _copy(timer, src=IO, dst=COPY):
    """Copy a struct timerequest, as CopyMem or a struct assignment does."""
    timer.mem.data[dst:dst + 40] = timer.mem.data[src:src + 40]


@pytest.fixture
def exec_timer(timer):
    """The timer fixture with AmiFUSE's ExecLibrary in place of amitools'.

    The fixture builds its ExecLibrary without a runtime, by __new__, so
    the subclass is swapped in the same way.
    """
    from amifuse.timer_device import AmiFuseExecLibrary

    timer.exec.__class__ = AmiFuseExecLibrary
    return timer


# ---------------------------------------------------------------------------
# A. A copy of an opened request (#143)
# ---------------------------------------------------------------------------


class TestCopiedRequest:

    def test_copy_is_held_until_its_delay(self, timer):
        """A copy of an opened request waits for its delay, as the original does."""
        _copy(timer)
        timer.send(COPY, micro=200_000)
        assert timer.exec.port_mgr.has_msg(PORT) is False
        assert timer.device.next_deadline() == pytest.approx(100.2)
        timer.clock.now = 100.2
        assert timer.device.deliver() == {TASK: 1 << SIGBIT}
        assert timer.exec.port_mgr.ports[PORT].queue == [COPY]
        assert _m68k_list(timer.mem) == [COPY]

    def test_copy_and_original_are_held_side_by_side(self, timer):
        """The original and its copy are two requests, each replied at its own deadline."""
        _copy(timer)
        timer.send(IO, micro=100_000)
        timer.send(COPY, micro=300_000)
        timer.clock.now = 100.1
        timer.device.deliver()
        assert timer.exec.port_mgr.ports[PORT].queue == [IO]
        timer.clock.now = 100.3
        timer.device.deliver()
        assert timer.exec.port_mgr.ports[PORT].queue == [IO, COPY]

    def test_copy_keeps_the_unit_of_the_original(self, timer):
        """The token tells the copy's unit: E-clock ticks, or an absolute unit."""
        timer.device.open_dev(timer.ctx, IO, UNIT_ECLOCK, 0)
        timer.device.open_dev(timer.ctx, IO2, UNIT_WAITUNTIL, 0)
        _copy(timer, IO, COPY)
        _copy(timer, IO2, COPY + 0x40)
        timer.send(COPY, secs=0, micro=5_000_000)  # 0.5 s of 10 MHz E-clock
        assert timer.device.next_deadline() == pytest.approx(100.5)
        timer.send(COPY + 0x40, secs=5)
        assert timer.exec.port_mgr.ports[PORT].queue == [COPY + 0x40]

    def test_close_device_ends_the_copies(self, timer):
        """After CloseDevice on the original, its copies complete at once."""
        _copy(timer)
        timer.device.close_dev(timer.ctx, IO)
        timer.send(COPY, secs=5)
        assert timer.exec.port_mgr.ports[PORT].queue == [COPY]
        assert timer.device.next_deadline() == float("inf")
        assert not timer.device.delays(COPY)

    def test_each_open_has_its_own_token(self, timer):
        """Closing one opened request leaves another, and its copies, held."""
        assert timer.mem.r32(IO + _IO_UNIT) != timer.mem.r32(IO2 + _IO_UNIT)
        _copy(timer)
        timer.device.close_dev(timer.ctx, IO2)
        timer.send(COPY, secs=1)
        assert timer.exec.port_mgr.has_msg(PORT) is False
        assert timer.device.delays(COPY)

    @pytest.mark.parametrize("unit", [0, 0x4000, 0x80000000])
    def test_request_never_opened_completes_at_once(self, timer, unit):
        """A request with this device's io_Device but no token is not held."""
        timer.mem.w32(COPY + 14, PORT)
        timer.mem.w32(COPY + 20, BASE)
        timer.mem.w32(COPY + _IO_UNIT, unit)
        timer.send(COPY, secs=5)
        assert timer.exec.port_mgr.ports[PORT].queue == [COPY]
        assert timer.device.next_deadline() == float("inf")

    def test_tokens_wrap_without_reusing_a_live_one(self, timer):
        """Past the last token, numbering starts again and skips those in use."""
        from amifuse.timer_device import _TOKEN_FIRST, _TOKEN_LAST

        live = timer.mem.r32(IO + _IO_UNIT)
        assert live == _TOKEN_FIRST
        timer.device._last_token = _TOKEN_LAST
        timer.device.open_dev(timer.ctx, COPY, UNIT_VBLANK, 0)
        token = timer.mem.r32(COPY + _IO_UNIT)
        assert token not in (live, timer.mem.r32(IO2 + _IO_UNIT))
        assert token & 1 and token >= _TOKEN_FIRST


# ---------------------------------------------------------------------------
# B. DoIO and WaitIO on a held request (#147)
# ---------------------------------------------------------------------------


def _arm(timer, io=IO, secs=1, cmd=TR_ADDREQUEST):
    timer.mem.w16(io + 28, cmd)
    timer.mem.w32(io + 32, secs)
    timer.mem.w32(io + 36, 0)


def _assert_done_and_off_the_port(timer, io=IO):
    assert timer.exec.port_mgr.ports[PORT].queue == []
    assert _m68k_list(timer.mem) == []
    assert timer.mem.r8(io + 8) == NT_REPLYMSG
    assert timer.mem.r8s(io + 31) == 0
    assert timer.exec.CheckIO(timer.ctx, io) == io
    # Nothing comes later: no stray reply on the handler's port.
    assert timer.device.next_deadline() == float("inf")
    assert timer.device.deliver(fast_forward=True) == {}
    assert timer.exec.port_mgr.has_msg(PORT) is False


class TestDoIOAndWaitIO:

    def test_doio_completes_a_held_request_at_once(self, exec_timer):
        """DoIO returns with the request done and no reply left to come."""
        t = exec_timer
        _arm(t)
        assert t.exec.DoIO(t.ctx, IO) == 0
        _assert_done_and_off_the_port(t)
        assert t.exec.signal_func.sent == []
        assert t.device.completed_early == {"DoIO": 1}

    def test_waitio_completes_a_pending_request(self, exec_timer):
        """WaitIO on a pending request returns with it done and off the port."""
        t = exec_timer
        t.send(secs=1)
        assert t.exec.WaitIO(t.ctx, IO) == 0
        _assert_done_and_off_the_port(t)
        assert t.exec.signal_func.sent == []
        assert t.device.completed_early == {"WaitIO": 1}

    def test_a_copy_is_completed_too(self, exec_timer):
        """A held copy of an opened request is completed like the original."""
        t = exec_timer
        _copy(t)
        _arm(t, COPY)
        assert t.exec.DoIO(t.ctx, COPY) == 0
        _assert_done_and_off_the_port(t, COPY)

    def test_waitio_after_the_reply_takes_it_as_before(self, exec_timer):
        """A replied request is not completed again: WaitIO only takes the reply."""
        t = exec_timer
        t.send(micro=100_000)
        t.clock.now = 100.1
        t.device.deliver()
        assert t.exec.WaitIO(t.ctx, IO) == 0
        assert t.exec.port_mgr.ports[PORT].queue == []
        assert t.device.completed_early == {}

    def test_abortio_then_waitio_keeps_the_abort(self, exec_timer):
        """AbortIO and WaitIO, the usual way to cancel, still give IOERR_ABORTED."""
        t = exec_timer
        t.send(secs=1)
        t.exec.AbortIO(t.ctx, IO)
        assert t.exec.WaitIO(t.ctx, IO) == -2
        assert t.exec.port_mgr.ports[PORT].queue == []
        assert _m68k_list(t.mem) == []
        assert t.device.completed_early == {}

    @pytest.mark.parametrize("cmd, unit", [
        (TR_GETSYSTIME, UNIT_VBLANK),
        (TR_ADDREQUEST, UNIT_WAITUNTIL),
    ])
    def test_doio_on_a_quick_request_is_unchanged(self, exec_timer, cmd, unit):
        """A request the device completes in BeginIO goes through DoIO as before."""
        t = exec_timer
        t.device.open_dev(t.ctx, IO, unit, 0)
        _arm(t, cmd=cmd)
        assert t.exec.DoIO(t.ctx, IO) == 0
        assert t.mem.r8(IO + 8) == NT_REPLYMSG
        assert t.exec.port_mgr.has_msg(PORT) is False
        assert t.device.completed_early == {}

    def test_other_devices_are_left_alone(self, exec_timer):
        """A device without complete_early gets amitools' DoIO and WaitIO."""
        t = exec_timer
        begun = []
        other = SimpleNamespace(BeginIO=lambda ctx, io: begun.append(io))
        t.exec.lib_mgr = SimpleNamespace(
            get_vlib_by_addr=lambda addr: SimpleNamespace(get_impl=lambda: other))
        assert t.exec.DoIO(t.ctx, IO) == 0
        assert t.exec.WaitIO(t.ctx, IO) == 0
        assert begun == [IO]

    def test_warns_once_per_call(self, exec_timer, caplog):
        """One warning for DoIO and one for WaitIO, however often they come."""
        t = exec_timer
        with caplog.at_level(logging.WARNING, logger="timer"):
            for _ in range(2):
                _arm(t)
                t.exec.DoIO(t.ctx, IO)
                t.send(secs=1)
                t.exec.WaitIO(t.ctx, IO)
        warnings = [r.getMessage() for r in caplog.records if r.name == "timer"]
        assert len(warnings) == 2
        assert warnings[0].startswith("DoIO on a pending TR_ADDREQUEST")
        assert warnings[1].startswith("WaitIO on a pending TR_ADDREQUEST")
        assert t.device.completed_early == {"DoIO": 2, "WaitIO": 2}
