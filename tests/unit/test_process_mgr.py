"""Unit tests for amifuse.process_mgr module."""

import inspect


def test_child_runner_does_not_clobber_a6_every_burst():
    from amifuse.process_mgr import ProcessManager

    source = inspect.getsource(ProcessManager.run_child_burst)

    assert "seed_execbase = first_run or child.regs is None" in source
    assert "if resumed and child.regs is not None and child.regs[REG_A6] == 0:" in source
    assert "self.cpu.w_reg(REG_A6, self.mem.r32(4))" in source


def test_child_runner_clears_parent_block_state_before_running_child():
    from amifuse.process_mgr import ProcessManager

    source = inspect.getsource(ProcessManager.run_child_burst)

    assert "_clear_all_block_state()" in source


def test_check_for_new_children_refreshes_reused_process_slots():
    from amitools.vamos.lib.DosLibrary import DosLibrary
    from amifuse.process_mgr import ProcessManager, ProcessState

    class FakePortMgr:
        def __init__(self):
            self.ports = {0x5000}

        def has_port(self, addr):
            return addr in self.ports

        def unregister_port(self, addr):
            self.ports.remove(addr)

        def register_port(self, addr):
            self.ports.add(addr)

    class FakeStack:
        def __init__(self, sp):
            self._sp = sp

        def get_initial_sp(self):
            return self._sp

    pm = ProcessManager.__new__(ProcessManager)
    pm.exec_impl = type("ExecImpl", (), {"port_mgr": FakePortMgr()})()
    stale_stack = FakeStack(0x1000)
    fresh_stack = FakeStack(0x2000)
    stale = ProcessState(
        proc_addr=0x4000,
        entry_pc=0x8000,
        sp=stale_stack.get_initial_sp(),
        name="old-child",
        port_addr=0x5000,
        blocked=True,
        is_child=True,
        started=True,
        stack=stale_stack,
        regs=[0] * 16,
        block_state={"waitport_blocked_sp": 0x6000},
    )
    pm.processes = {stale.proc_addr: stale}
    pm.ready_queue = []

    old_children = DosLibrary._child_processes
    DosLibrary._child_processes = {
        stale.proc_addr: {
            "proc_addr": stale.proc_addr,
            "entry_pc": 0x8100,
            "stack": fresh_stack,
            "port_addr": 0x5100,
            "name": "new-child",
            "seglist_bptr": 0x1234,
        }
    }
    try:
        new_children = ProcessManager.check_for_new_children(pm)
    finally:
        DosLibrary._child_processes = old_children

    assert new_children == [stale]
    assert stale.entry_pc == 0x8100
    assert stale.sp == fresh_stack.get_initial_sp()
    assert stale.name == "new-child"
    assert stale.port_addr == 0x5100
    assert stale.blocked is False
    assert stale.started is False
    assert stale.exited is False
    assert stale.stack is fresh_stack
    assert stale.regs is None
    assert stale.block_state is None
    assert pm.ready_queue == [stale]
    assert 0x5100 in pm.exec_impl.port_mgr.ports


def test_child_exit_unregisters_child_port():
    from amifuse.process_mgr import ProcessManager, ProcessState

    class FakePortMgr:
        def __init__(self):
            self.ports = {0x5000}

        def has_port(self, addr):
            return addr in self.ports

        def unregister_port(self, addr):
            self.ports.remove(addr)

    class FakeCpu:
        def r_pc(self):
            return 0

        def r_sp(self):
            return 0

        def r_reg(self, _):
            return 0

        def w_pc(self, _):
            pass

        def w_sp(self, _):
            pass

        def w_reg(self, *_):
            pass

    class FakeRunState:
        done = True
        error = False

    class FakeMachine:
        def run(self, **_kwargs):
            return FakeRunState()

    pm = ProcessManager.__new__(ProcessManager)
    pm.exec_impl = type("ExecImpl", (), {"port_mgr": FakePortMgr()})()
    pm.machine = FakeMachine()
    pm.cpu = FakeCpu()
    pm.mem = type("Mem", (), {"r32": lambda self, _addr: 0})()
    pm.parent_addr = 0x1000
    pm._set_this_task = lambda _addr: None
    child = ProcessState(
        proc_addr=0x4000,
        entry_pc=0x8000,
        sp=0x9000,
        port_addr=0x5000,
        is_child=True,
        started=False,
        stack=object(),
    )

    ProcessManager.run_child_burst(pm, child)

    assert child.exited is True
    assert 0x5000 not in pm.exec_impl.port_mgr.ports


def test_child_error_without_block_state_is_treated_as_exit():
    from amifuse.process_mgr import ProcessManager, ProcessState

    class FakePortMgr:
        def __init__(self):
            self.ports = {0x5000}

        def has_port(self, addr):
            return addr in self.ports

        def unregister_port(self, addr):
            self.ports.remove(addr)

    class FakeCpu:
        def r_pc(self):
            return 0

        def r_sp(self):
            return 0

        def r_reg(self, _):
            return 0

        def w_pc(self, _):
            pass

        def w_sp(self, _):
            pass

        def w_reg(self, *_):
            pass

    class FakeRunState:
        done = False
        error = True

    class FakeMachine:
        def run(self, **_kwargs):
            return FakeRunState()

    pm = ProcessManager.__new__(ProcessManager)
    pm.exec_impl = type("ExecImpl", (), {"port_mgr": FakePortMgr()})()
    pm.machine = FakeMachine()
    pm.cpu = FakeCpu()
    pm.mem = type("Mem", (), {"r32": lambda self, _addr: 0})()
    pm.parent_addr = 0x1000
    pm._set_this_task = lambda _addr: None
    child = ProcessState(
        proc_addr=0x4000,
        entry_pc=0x8000,
        sp=0x9000,
        port_addr=0x5000,
        is_child=True,
        started=False,
        stack=object(),
    )

    ProcessManager.run_child_burst(pm, child)

    assert child.exited is True
    assert child.blocked is False
    assert 0x5000 not in pm.exec_impl.port_mgr.ports
