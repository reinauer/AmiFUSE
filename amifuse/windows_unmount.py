"""Cooperative WinFsp shutdown using per-process Windows events.

The controller only requests fuse_exit. WinFsp drains its dispatcher and
calls destroy on the mount thread, where handler and device cleanup belong.
"""

import ctypes
import os
import threading


class _Events:
    def __init__(self):
        self.dll = ctypes.WinDLL("kernel32", use_last_error=True)
        ptr, word = ctypes.c_void_p, ctypes.c_uint32
        signatures = {
            "CreateEventW": (ptr, [ptr, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]),
            "OpenEventW": (ptr, [word, ctypes.c_int, ctypes.c_wchar_p]),
            "SetEvent": (ctypes.c_int, [ptr]),
            "WaitForSingleObject": (word, [ptr, word]),
            "CloseHandle": (ctypes.c_int, [ptr]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.dll, name)
            fn.restype, fn.argtypes = result, args

    def open(self, pid, kind, create=False):
        name = "Global\\AmiFUSE-%d-%s" % (pid, kind)
        if create:
            handle = self.dll.CreateEventW(None, True, False, name)
        else:
            # SYNCHRONIZE | EVENT_MODIFY_STATE
            handle = self.dll.OpenEventW(0x100002, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if create and ctypes.get_last_error() == 183:
            self.close(handle)
            raise OSError("unmount control event already exists")
        return handle

    def set(self, handle):
        if not self.dll.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, handle, timeout):
        result = self.dll.WaitForSingleObject(handle, int(timeout * 1000))
        if result == 0:
            return True
        if result == 258:  # WAIT_TIMEOUT
            return False
        raise ctypes.WinError(ctypes.get_last_error())

    def close(self, handle):
        self.dll.CloseHandle(handle)


def _fuse_exit_callback():
    # Must capture the pointer in init: fuse_get_context is thread-local.
    # fusepy's public fuse_exit() cannot be called from our watcher thread.
    import fuse
    pointer = ctypes.c_void_p(fuse._libfuse.fuse_get_context().contents.fuse)
    if not pointer.value:
        raise RuntimeError("WinFsp did not provide a FUSE session")
    return lambda: fuse._libfuse.fuse_exit(pointer)


class UnmountControl:
    def __init__(self):
        self.api = _Events()
        self.handles = {}
        self.thread = None
        self.stopped = threading.Event()
        self.completed = False
        try:
            for kind in ("stop", "done", "failed"):
                self.handles[kind] = self.api.open(os.getpid(), kind, create=True)
        except BaseException:
            self.close()
            raise

    def start(self):
        callback = _fuse_exit_callback()

        def watch():
            try:
                while not self.stopped.is_set():
                    if self.api.wait(self.handles["stop"], 0.1):
                        if not self.stopped.is_set():
                            callback()
                        return
            except Exception:
                self.complete(False)

        thread = threading.Thread(target=watch, daemon=True)
        thread.start()
        self.thread = thread

    def stop(self):
        self.stopped.set()
        if self.thread is not None:
            self.thread.join()
            self.thread = None

    def complete(self, success):
        if not success and "failed" in self.handles:
            self.api.set(self.handles["failed"])
        if "done" in self.handles:
            self.api.set(self.handles["done"])
        self.completed = True

    def close(self):
        self.stop()
        if not self.completed:
            self.complete(False)
        for handle in self.handles.values():
            self.api.close(handle)
        self.handles.clear()


def request_unmount(pid, timeout=30.0):
    api = _Events()
    handles = {}
    try:
        try:
            for kind in ("stop", "done", "failed"):
                handles[kind] = api.open(pid, kind)
        except OSError as exc:
            raise OSError(
                "Cannot request clean unmount of process %d; it may be an "
                "older AmiFUSE or require Administrator privileges. "
                "The process has not been terminated." % pid
            ) from exc
        api.set(handles["stop"])
        if not api.wait(handles["done"], timeout):
            raise OSError("Timed out waiting for process %d to flush and unmount; "
                          "the process has not been terminated." % pid)
        if api.wait(handles["failed"], 0):
            raise OSError("Process %d reported an unmount/flush failure; "
                          "check the mount log before removing the disk." % pid)
    finally:
        for handle in handles.values():
            api.close(handle)
