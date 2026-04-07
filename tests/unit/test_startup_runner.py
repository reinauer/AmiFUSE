"""Unit tests for amifuse.startup_runner module."""

import inspect
import pytest


class TestProcessStackBase:
    """Verify pr_StackBase initialization in _create_process().

    Source inspection tests: fragile by design, to be replaced with
    functional tests when integration test infrastructure is available (Phase 5).
    """

    def test_pr_stackbase_is_stack_upper(self):
        """Verify pr_StackBase is set to the stack upper bound (top of stack)."""
        from amifuse.startup_runner import HandlerLauncher
        source = inspect.getsource(HandlerLauncher._create_process)
        assert "proc.stack_base.bptr = stack.get_upper() >> 2" in source
        assert "proc.stack_base.bptr = stack.get_lower() >> 2" not in source


def test_build_resume_frame_wait_uses_all_pending_signals():
    from amifuse.startup_runner import _build_resume_frame

    cleared = []

    block_state = {
        "waitport_blocked_sp": None,
        "waitport_blocked_port": None,
        "waitport_blocked_ret": None,
        "wait_blocked_mask": 0x12,
        "wait_blocked_sp": 0x2000,
        "wait_blocked_ret": 0x3456,
        "waitpkt_blocked": False,
    }

    class FakeMem:
        def r32(self, _addr):
            raise AssertionError("explicit wait_blocked_ret should be used")

    class FakePortMgr:
        def has_msg(self, _addr):
            return False

        def get_msg(self, _addr):
            raise AssertionError("Wait() resume must not dequeue a message")

    def fake_pending(mask):
        if mask == 0x12:
            return 0x10
        if mask == 0xFFFFFFFF:
            return 0x34
        raise AssertionError(f"unexpected mask {mask:#x}")

    resume = _build_resume_frame(
        block_state,
        default_port_addr=0x9999,
        mem=FakeMem(),
        port_mgr=FakePortMgr(),
        compute_pending_signals=fake_pending,
        clear_signals_from_task=cleared.append,
    )

    assert resume == {"pc": 0x3456, "sp": 0x2004, "d0": 0x34}
    assert cleared == [0x34]


def test_build_resume_frame_waitport_dequeues_message():
    from amifuse.startup_runner import _build_resume_frame

    block_state = {
        "waitport_blocked_sp": 0x1800,
        "waitport_blocked_port": 0x2222,
        "waitport_blocked_ret": 0x4567,
        "wait_blocked_mask": None,
        "wait_blocked_sp": None,
        "wait_blocked_ret": None,
        "waitpkt_blocked": False,
    }

    class FakeMem:
        def __init__(self):
            self.values = {
                0xABC0 + 0: 0x3000,
                0xABC0 + 4: 0x4000,
            }
            self.writes = []

        def r32(self, addr):
            return self.values[addr]

        def w32(self, addr, value):
            self.writes.append((addr, value))

    class FakePortMgr:
        def __init__(self):
            self.got = []

        def has_msg(self, addr):
            return addr == 0x2222

        def get_msg(self, addr):
            self.got.append(addr)
            return 0xABC0

    mem = FakeMem()
    pmgr = FakePortMgr()
    resume = _build_resume_frame(
        block_state,
        default_port_addr=0x9999,
        mem=mem,
        port_mgr=pmgr,
        compute_pending_signals=lambda _mask: 0,
        clear_signals_from_task=lambda _signals: None,
    )

    assert resume == {"pc": 0x4567, "sp": 0x1804, "d0": 0xABC0}
    assert pmgr.got == [0x2222]
    assert mem.writes == [(0x4000, 0x3000), (0x3000 + 4, 0x4000)]


def test_run_burst_reseeds_execbase_when_restarting_at_main_loop_with_null_a6():
    from amifuse.startup_runner import HandlerLauncher

    source = inspect.getsource(HandlerLauncher.run_burst)

    assert "state.pc == state.main_loop_pc" in source
    assert "state.regs[REG_A6] == 0" in source
