"""Unit tests for amifuse.vamos_runner module."""

import inspect


def test_reset_runtime_state_clears_global_handler_state():
    from amitools.vamos.lib.DosLibrary import DosLibrary
    from amitools.vamos.lib.ExecLibrary import ExecLibrary
    from amitools.vamos.lib.lexec.signalfunc import SignalFunc
    from amifuse import pending_ports
    from amifuse.vamos_runner import _reset_runtime_state

    ExecLibrary._waitport_blocked_sp = 0x1111
    ExecLibrary._waitport_blocked_port = 0x2222
    ExecLibrary._waitport_blocked_ret = 0x3333
    ExecLibrary._wait_blocked_mask = 0x4444
    ExecLibrary._wait_blocked_sp = 0x5555
    ExecLibrary._wait_blocked_ret = 0x6666
    DosLibrary._waitpkt_blocked = True
    DosLibrary._child_processes[0x7777] = {"name": "stale"}
    SignalFunc._fallback_signals = 0xCCCC
    SignalFunc._fallback_sig_alloc = 0x12345678
    pending_ports.queue_msg(0x8888, 0x9999)
    pending_ports.queue_default(0xAAAA)
    pending_ports.set_last_wait_port(0xBBBB)

    _reset_runtime_state()

    assert ExecLibrary._waitport_blocked_sp is None
    assert ExecLibrary._waitport_blocked_port is None
    assert ExecLibrary._waitport_blocked_ret is None
    assert ExecLibrary._wait_blocked_mask is None
    assert ExecLibrary._wait_blocked_sp is None
    assert ExecLibrary._wait_blocked_ret is None
    assert DosLibrary._waitpkt_blocked is False
    assert DosLibrary._child_processes == {}
    assert SignalFunc._fallback_signals == 0
    assert SignalFunc._fallback_sig_alloc == 0x0000FFFF
    assert pending_ports.pending_msgs == {}
    assert pending_ports.default_msgs == []
    assert pending_ports.last_wait_port is None


def test_vamos_runtime_resets_state_on_setup_and_shutdown():
    from amifuse.vamos_runner import VamosHandlerRuntime

    setup_source = inspect.getsource(VamosHandlerRuntime.setup)
    shutdown_source = inspect.getsource(VamosHandlerRuntime.shutdown)

    assert "_reset_runtime_state()" in setup_source
    assert shutdown_source.count("_reset_runtime_state()") == 2
