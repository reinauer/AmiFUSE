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


class TestStartupPacket:
    """The startup packet is the one StandardPacket the launcher builds.

    Every later packet goes through DosPktHandler.
    """

    def _make_launcher(self):
        from amifuse.startup_runner import HandlerLauncher

        launcher = HandlerLauncher.__new__(HandlerLauncher)
        launcher._emu = None
        launcher.alloc = MagicMock()
        launcher.mem = MagicMock()
        launcher.port_mgr = MagicMock()
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

    def test_packet_is_allocated_and_queued(self):
        launcher = self._make_launcher()
        new_mem = SimpleNamespace(addr=0x5000, size=68)
        launcher.alloc.alloc_memory.return_value = new_mem

        pkt_addr, msg_addr = launcher._build_std_packet(0x1000, 0x2000, 1, [7, 8])

        assert launcher.alloc.alloc_memory.call_args.args[0] == 68
        launcher.alloc.free_memory.assert_not_called()
        assert (pkt_addr, msg_addr) == (0x5000 + 20, 0x5000)
        launcher.port_mgr.put_msg.assert_called_once_with(0x1000, 0x5000)
        # dp_Type, then the args from dp_Arg1
        launcher.mem.w32.assert_any_call(0x5014 + 8, 1)
        launcher.mem.w32.assert_any_call(0x5014 + 12, 7)
        launcher.mem.w32.assert_any_call(0x5014 + 16, 8)

    def test_failed_allocation_queues_nothing(self):
        launcher = self._make_launcher()
        launcher.alloc.alloc_memory.side_effect = RuntimeError("oom")

        with pytest.raises(RuntimeError, match="oom"):
            launcher._build_std_packet(0x1000, 0x2000, 1, [])

        launcher.port_mgr.put_msg.assert_not_called()


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


class TestSignalPort:
    """A message's arrival reaches the port's owner the way the port asks."""

    def _make_launcher(self, flags, sigbit=5, task=0x3000):
        from amifuse.startup_runner import HandlerLauncher

        launcher = HandlerLauncher.__new__(HandlerLauncher)
        launcher._emu = None
        launcher._debug = False
        launcher._mp_flags_offset = 14
        launcher._mp_sigbit_offset = 15
        launcher._mp_sigtask_offset = 16
        launcher.mem = MagicMock()
        fields = {0x1000 + 14: flags, 0x1000 + 15: sigbit}
        launcher.mem.r8.side_effect = fields.__getitem__
        launcher.mem.r32.return_value = task
        launcher.exec_impl = MagicMock()
        launcher.signal_task = MagicMock()
        return launcher

    def test_signal_port_signals_its_task(self):
        from amitools.vamos.libstructs.exec_ import MsgPortFlags

        launcher = self._make_launcher(MsgPortFlags.PA_SIGNAL)
        launcher._signal_port(0x1000)
        launcher.signal_task.assert_called_once_with(0x3000, 1 << 5)
        launcher.exec_impl.msg_func.notify_port.assert_not_called()

    def test_softint_port_goes_to_amitools_putmsg_logic(self):
        from amitools.vamos.libstructs.exec_ import MsgPortFlags

        launcher = self._make_launcher(MsgPortFlags.PA_SOFTINT)
        launcher._signal_port(0x1000)
        launcher.exec_impl.msg_func.notify_port.assert_called_once_with(0x1000)
        launcher.signal_task.assert_not_called()

    def test_ignore_port_tells_nobody(self):
        from amitools.vamos.libstructs.exec_ import MsgPortFlags

        launcher = self._make_launcher(MsgPortFlags.PA_IGNORE)
        launcher._signal_port(0x1000)
        launcher.signal_task.assert_not_called()
        launcher.exec_impl.msg_func.notify_port.assert_not_called()
