"""The thread the emulation runs on.

vamos runs its tasks as greenlets, and a greenlet can only be switched to
from the thread that created it. So the machine, the scheduler and every
call that may run or wake a task live on one thread, and a caller on any
other thread (a FUSE operation, the CLI's main thread) hands its call over
and waits for the result. Exceptions, SystemExit among them, come back to
the caller.

While no call is pending the thread runs an idle hook: the bridge replies
to timer.device requests there and runs the handler on, which the mount's
own timer thread did before. The hook returns how long to wait for the
next call before it runs again, or None to wait for a call.
"""

import functools
import queue
import threading
from typing import Any, Callable, Optional


class _Done(Exception):
    """Posted to end the thread."""


class EmuThread:
    # The longest the thread waits for a call at once. A handler that arms
    # a timer does so from a call on this thread, so the idle hook is asked
    # again after every call; this only bounds a sleep with nothing pending.
    MAX_IDLE_WAIT = 3600.0

    def __init__(self, name: str = "amifuse-emu"):
        self._queue: "queue.Queue" = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name=name, daemon=True)
        self._idle: Optional[Callable[[], Optional[float]]] = None
        self._idle_lock = threading.Lock()
        self._stopped = False

    def start(self):
        self._thread.start()

    @property
    def owns_current_thread(self) -> bool:
        return threading.current_thread() is self._thread

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def call(self, fn: Callable, *args, **kwargs) -> Any:
        """Run fn(*args, **kwargs) on the thread and return its result.

        On the thread itself the call runs directly. An exception fn raises
        is raised here instead, SystemExit included.
        """
        if self.owns_current_thread:
            return fn(*args, **kwargs)
        if self._stopped or not self._thread.is_alive():
            raise RuntimeError("the emulation thread has stopped")
        done = threading.Event()
        box: list = []
        self._queue.put((fn, args, kwargs, done, box))
        done.wait()
        ok, value = box[0]
        if ok:
            return value
        raise value

    def set_idle(self, hook: Optional[Callable[[], Optional[float]]]):
        """Set the idle hook, or None for none; wakes the thread to use it."""
        with self._idle_lock:
            self._idle = hook
        self._queue.put(None)

    def stop(self, timeout: Optional[float] = 5.0):
        """End the thread once the calls queued so far are done.

        From the thread itself, it ends after the current call returns.
        """
        self._stopped = True
        self._queue.put(_Done)
        if not self.owns_current_thread and self._thread.is_alive():
            self._thread.join(timeout)

    def _loop(self):
        timeout = None
        while True:
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is _Done:
                return
            if item is not None:
                fn, args, kwargs, done, box = item
                try:
                    box.append((True, fn(*args, **kwargs)))
                except BaseException as exc:  # handed to the caller
                    box.append((False, exc))
                done.set()
            with self._idle_lock:
                hook = self._idle
            if hook is None:
                timeout = None
                continue
            try:
                timeout = hook()
            except Exception:
                timeout = None
            if timeout is not None:
                timeout = min(max(timeout, 0.0), self.MAX_IDLE_WAIT)


def on_emu_thread(method):
    """Run a method on its object's emulation thread (self._emu), if it has one."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        emu = getattr(self, "_emu", None)
        if emu is None or emu.owns_current_thread or not emu.alive:
            return method(self, *args, **kwargs)
        return emu.call(method, self, *args, **kwargs)

    return wrapper


def route_methods_through_emu(cls, exclude=()):
    """Wrap every method of cls defined on cls itself with on_emu_thread.

    Dunder methods, properties and the names in exclude stay as they are.
    """
    for name, value in list(vars(cls).items()):
        if name.startswith("__") or name in exclude:
            continue
        if isinstance(value, (staticmethod, classmethod, property)):
            continue
        if callable(value):
            setattr(cls, name, on_emu_thread(value))
