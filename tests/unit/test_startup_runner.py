"""Unit tests for amifuse.startup_runner module."""

import inspect
from types import SimpleNamespace
from unittest.mock import MagicMock

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


class TestStdpktRingBuffer:
    """Verify stdpkt ring buffer frees old allocation on growth."""

    def _make_launcher(self):
        from amifuse.startup_runner import HandlerLauncher

        launcher = HandlerLauncher.__new__(HandlerLauncher)
        launcher._emu = None
        launcher.alloc = MagicMock()
        launcher.mem = MagicMock()
        launcher.port_mgr = MagicMock()
        launcher._stdpkt_ring = []
        launcher._stdpkt_sizes = []
        launcher._stdpkt_ring_size = 8
        launcher._stdpkt_index = 0
        launcher.pending = {}
        launcher._stdpkt_retired = []
        # Minimal field offsets (actual values don't matter for allocation tests)
        launcher._msg_size = 20
        launcher._pkt_size = 48
        launcher._msg_ln_type_offset = 0
        launcher._msg_ln_succ_offset = 4
        launcher._msg_ln_pred_offset = 8
        launcher._mn_replyport_offset = 12
        launcher._mn_length_offset = 16
        launcher._msg_ln_name_offset = 18
        launcher._dp_link_offset = 0
        launcher._dp_port_offset = 4
        launcher._dp_type_offset = 8
        launcher._dp_arg1_offset = 12
        launcher._debug = False
        # Stub out the port list and the signal: only allocation is tested
        launcher._link_msg_to_port = lambda *args, **kwargs: None
        launcher._signal_port = lambda *args, **kwargs: None
        return launcher

    def test_stdpkt_first_alloc(self):
        launcher = self._make_launcher()
        new_mem = SimpleNamespace(addr=0x5000, size=68)
        launcher.alloc.alloc_memory.return_value = new_mem

        launcher._build_std_packet(0x1000, 0x2000, 1, [])

        launcher.alloc.alloc_memory.assert_called_once()
        launcher.alloc.free_memory.assert_not_called()
        assert launcher._stdpkt_ring[0] is new_mem
        launcher.port_mgr.put_msg.assert_called_once_with(0x1000, 0x5000)

    def test_stdpkt_reuses_existing_slot(self):
        launcher = self._make_launcher()
        existing = SimpleNamespace(addr=0x5000, size=68)
        launcher._stdpkt_ring = [existing] + [None] * 7
        launcher._stdpkt_sizes = [68] + [0] * 7

        launcher._build_std_packet(0x1000, 0x2000, 1, [])

        launcher.alloc.alloc_memory.assert_not_called()
        launcher.alloc.free_memory.assert_not_called()

    def test_stdpkt_grows_and_frees_old_slot(self):
        launcher = self._make_launcher()
        old_mem = SimpleNamespace(addr=0x5000, size=32)
        new_mem = SimpleNamespace(addr=0x6000, size=68)
        launcher._stdpkt_ring = [old_mem] + [None] * 7
        launcher._stdpkt_sizes = [32] + [0] * 7
        launcher.alloc.alloc_memory.return_value = new_mem

        launcher._build_std_packet(0x1000, 0x2000, 1, [])

        launcher.alloc.alloc_memory.assert_called_once()
        launcher.alloc.free_memory.assert_called_once_with(old_mem)
        assert launcher._stdpkt_ring[0] is new_mem

    def test_stdpkt_failed_growth_keeps_old_slot(self):
        launcher = self._make_launcher()
        old_mem = SimpleNamespace(addr=0x5000, size=32)
        launcher._stdpkt_ring = [old_mem] + [None] * 7
        launcher._stdpkt_sizes = [32] + [0] * 7
        launcher.alloc.alloc_memory.side_effect = RuntimeError("oom")

        with pytest.raises(RuntimeError, match="oom"):
            launcher._build_std_packet(0x1000, 0x2000, 1, [])

        assert launcher._stdpkt_ring[0] is old_mem
        assert launcher._stdpkt_sizes[0] == 32
        launcher.alloc.free_memory.assert_not_called()


class TestSignalTask:
    """A packet's signal reaches the task's scheduler task, or its tc_SigRecvd."""

    def _make_launcher(self, found):
        from amifuse.startup_runner import HandlerLauncher

        launcher = HandlerLauncher.__new__(HandlerLauncher)
        launcher._emu = None
        launcher.mem = MagicMock()
        launcher.mem.r32.return_value = 0x0
        launcher.scheduler = SimpleNamespace(find_task_pred_func=lambda pred: found)
        return launcher

    def test_scheduler_task_gets_the_signal(self):
        task = MagicMock()
        launcher = self._make_launcher(task)
        assert launcher.signal_task(0x3000, 0x100)
        task.set_signal.assert_called_once_with(0x100, 0x100)
        launcher.mem.w32.assert_not_called()

    def test_task_the_scheduler_does_not_know_gets_tc_sigrecvd(self):
        from amifuse.startup_runner import _TC_SIGRECVD

        launcher = self._make_launcher(None)
        launcher.mem.r32.return_value = 0x20
        assert not launcher.signal_task(0x3000, 0x100)
        launcher.mem.w32.assert_called_once_with(0x3000 + _TC_SIGRECVD, 0x120)
