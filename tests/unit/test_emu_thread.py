"""The emulation thread: calls handed over, exceptions back, an idle hook."""

import threading
import time

import pytest

from amifuse.emu_thread import EmuThread, on_emu_thread, route_methods_through_emu


@pytest.fixture
def emu():
    emu = EmuThread(name="test-emu")
    emu.start()
    yield emu
    emu.stop()


def test_call_runs_on_the_thread_and_returns_the_result(emu):
    seen = []

    def fn(a, b=0):
        seen.append(threading.current_thread().name)
        return a + b

    assert emu.call(fn, 1, b=2) == 3
    assert seen == ["test-emu"]
    assert not emu.owns_current_thread


@pytest.mark.parametrize("error", [ValueError("bad"), SystemExit("exit 2")])
def test_an_exception_comes_back_to_the_caller(emu, error):
    def fn():
        raise error

    with pytest.raises(type(error)) as caught:
        emu.call(fn)
    assert caught.value is error
    # the thread goes on
    assert emu.call(lambda: 7) == 7


def test_a_call_from_the_thread_runs_directly(emu):
    def outer():
        assert emu.owns_current_thread
        return emu.call(lambda: threading.current_thread().name)

    assert emu.call(outer) == "test-emu"


def test_idle_hook_runs_while_no_call_is_pending(emu):
    ticks = []

    def idle():
        ticks.append(time.monotonic())
        return 0.02

    emu.set_idle(idle)
    time.sleep(0.3)
    emu.set_idle(None)
    count = len(ticks)
    assert 3 <= count <= 20
    time.sleep(0.1)
    # none after the hook is taken away
    assert len(ticks) == count


def test_idle_hook_returning_none_waits_for_a_call(emu):
    ticks = []

    def idle():
        ticks.append(1)
        return None

    emu.set_idle(idle)
    time.sleep(0.2)
    # set_idle woke the thread once; with None it waits for a call
    assert len(ticks) == 1
    emu.call(lambda: None)
    time.sleep(0.05)
    assert len(ticks) == 2


def test_stop_ends_the_thread_and_later_calls_fail():
    emu = EmuThread()
    emu.start()
    emu.stop()
    assert not emu.alive
    with pytest.raises(RuntimeError):
        emu.call(lambda: 1)


def test_a_call_queued_behind_stop_fails_instead_of_hanging():
    """A call that passed the stopped check as stop() ran is failed by the thread."""
    emu = EmuThread()
    emu.start()
    gate = threading.Event()
    busy = threading.Thread(target=lambda: emu.call(gate.wait, 5))
    busy.start()
    # stop() posts its end marker while the thread is busy, and a call
    # that passed the stopped check just before lands behind it
    emu._queue.put(emu_thread_done())
    errors = []

    def late():
        try:
            emu.call(lambda: 1)
        except RuntimeError as exc:
            errors.append(exc)

    late_thread = threading.Thread(target=late)
    late_thread.start()
    deadline = time.monotonic() + 2
    while emu._queue.qsize() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    gate.set()
    late_thread.join(3)
    busy.join(3)
    assert not late_thread.is_alive()
    assert len(errors) == 1
    assert "stopped" in str(errors[0])
    assert not emu.alive


def emu_thread_done():
    from amifuse.emu_thread import _Done

    return _Done


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_a_call_waiting_on_a_dead_thread_fails():
    """A thread that dies with a call queued fails the call."""
    emu = EmuThread()
    emu.start()
    gate = threading.Event()
    busy = threading.Thread(target=lambda: emu.call(gate.wait, 5))
    busy.start()

    class Die(BaseException):
        pass

    def die():
        raise Die()

    emu.set_idle(die)  # a BaseException the loop does not catch ends it
    errors = []

    def late():
        try:
            emu.call(lambda: 1)
        except RuntimeError as exc:
            errors.append(exc)

    late_thread = threading.Thread(target=late)
    late_thread.start()
    deadline = time.monotonic() + 2
    while emu._queue.qsize() < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    gate.set()
    late_thread.join(3)
    busy.join(3)
    assert not late_thread.is_alive()
    assert len(errors) == 1
    emu._thread.join(3)
    assert not emu.alive


def test_ctrl_c_reaches_a_waiting_caller_soon(emu):
    """The caller waits in slices, so a SIGINT interrupts it within one."""
    import signal

    # Python's own handler, whatever another test or plugin installed
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    gate = threading.Event()
    start = time.monotonic()
    threading.Timer(0.05, signal.raise_signal, [signal.SIGINT]).start()
    try:
        with pytest.raises(KeyboardInterrupt):
            emu.call(gate.wait, 5)
        assert time.monotonic() - start < 1.0
    finally:
        gate.set()
        signal.signal(signal.SIGINT, previous)


def test_idle_hook_waits_while_calls_are_queued(emu):
    """With calls queued the hook runs only once its own delay has passed."""
    ticks = []

    def idle():
        ticks.append(1)
        return 3600.0

    emu.set_idle(idle)
    deadline = time.monotonic() + 2
    while not ticks and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ticks == [1]
    gate = threading.Event()
    started = threading.Event()
    threads = [threading.Thread(
        target=lambda: emu.call(lambda: (started.set(), gate.wait(5))))]
    threads += [threading.Thread(target=lambda: emu.call(lambda: None)) for _ in range(5)]
    threads[0].start()
    assert started.wait(2)
    for thread in threads[1:]:
        thread.start()
    deadline = time.monotonic() + 2
    while emu._queue.qsize() < 5 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert emu._queue.qsize() == 5
    gate.set()
    for thread in threads:
        thread.join(3)
    time.sleep(0.05)
    # once after the last call, not after each of the six
    assert ticks == [1, 1]


def test_stop_from_the_thread_ends_it_after_the_call():
    emu = EmuThread()
    emu.start()
    assert emu.call(lambda: (emu.stop(), 5)[1]) == 5
    emu._thread.join(2)
    assert not emu.alive


class Thing:
    def __init__(self, emu):
        self._emu = emu

    def where(self):
        return threading.current_thread().name

    @property
    def prop(self):
        return "prop"


route_methods_through_emu(Thing, exclude={"skipped"})


def test_routed_methods_run_on_the_thread(emu):
    thing = Thing(emu)
    assert thing.where() == "test-emu"
    assert thing.prop == "prop"


def test_routed_method_runs_directly_without_a_thread():
    thing = Thing(None)
    assert thing.where() == threading.current_thread().name
    stopped = EmuThread()
    stopped.start()
    stopped.stop()
    assert Thing(stopped).where() == threading.current_thread().name
