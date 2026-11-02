"""Ending the tasks a run leaves behind.

rem_task() only works on a task whose greenlet is active: for one that never
started or has ended, greenlet raises its TaskStop straight back in the caller.
That stopped _stop_tasks at that task, leaving the ones after it suspended, and
made close() fail with a bare TaskStop.
"""

from types import SimpleNamespace

import pytest

from amitools.vamos.machine import Machine
from amitools.vamos.schedule import PythonTask, RunResult, Scheduler, TaskState

from amifuse.startup_runner import HandlerLauncher
from amifuse.vamos_runner import VamosHandlerRuntime


@pytest.fixture
def machine():
    machine = Machine()
    yield machine
    machine.cleanup()


def _waiter(machine, name="waiter"):
    return PythonTask(name, machine, lambda task: task.wait(1))


def _removed(scheduler, *tasks):
    return (all(task.get_state() == TaskState.TS_REMOVED for task in tasks)
            and scheduler.get_num_tasks() == 0)


def _crashing_child(scheduler, machine, name="child"):
    """Added and run after the other tasks: its exception ends the run."""

    def crash(task):
        raise RuntimeError("injected")

    child = PythonTask(name, machine, crash)
    scheduler.add_task(child)
    return child


def test_a_task_that_never_started_does_not_stop_the_others(machine):
    scheduler = Scheduler(machine)
    waiter = _waiter(machine)
    scheduler.add_task(waiter)
    assert scheduler.run_until_idle(1000) == RunResult.IDLE
    # Added once the run ended, as by a run that ends BUSY: it never started,
    # and it comes first in _stop_tasks' list.
    added = PythonTask("added", machine, lambda task: 0)
    scheduler.add_task(added)
    assert added.get_state() == TaskState.TS_ADDED
    VamosHandlerRuntime._stop_tasks(scheduler)
    assert _removed(scheduler, added, waiter)
    assert waiter.glet.dead


def test_a_task_whose_greenlet_died_does_not_stop_the_others(machine):
    scheduler = Scheduler(machine)
    waiter = _waiter(machine)
    scheduler.add_task(waiter)
    child = _crashing_child(scheduler, machine)
    with pytest.raises(RuntimeError, match="injected"):
        scheduler.run_until_idle(1000)
    assert child.glet.dead and child.get_state() != TaskState.TS_REMOVED
    VamosHandlerRuntime._stop_tasks(scheduler)
    assert _removed(scheduler, child, waiter)
    assert waiter.glet.dead


def _launcher(scheduler, task):
    launcher = HandlerLauncher.__new__(HandlerLauncher)
    launcher._emu = None
    launcher._debug = False
    launcher.scheduler = scheduler
    launcher.state = SimpleNamespace(
        crashed=False, exited=False, started=False, task=task,
        last_traceback=None)
    return launcher


def test_a_childs_crash_unwinds_the_waiting_handler(machine, capsys):
    scheduler = Scheduler(machine)
    handler = _waiter(machine, "handler")
    scheduler.add_task(handler)
    child = _crashing_child(scheduler, machine)
    launcher = _launcher(scheduler, handler)
    assert launcher.run(1000) == RunResult.DONE
    assert launcher.state.crashed
    assert handler.get_state() == TaskState.TS_REMOVED
    # Taken off the lists without unwinding, it would stay suspended.
    assert handler.glet.dead
    VamosHandlerRuntime._stop_tasks(scheduler)
    assert _removed(scheduler, child, handler)


def test_a_handlers_own_crash_leaves_no_task(machine, capsys):
    scheduler = Scheduler(machine)
    handler = _crashing_child(scheduler, machine, "handler")
    launcher = _launcher(scheduler, handler)
    assert launcher.run(1000) == RunResult.DONE
    assert launcher.state.crashed
    assert _removed(scheduler, handler)
