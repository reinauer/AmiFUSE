"""Unmount must acknowledge cleanup, never substitute process termination."""

import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import windows_unmount as unmount


class Events:
    def __init__(self):
        self.events = {}
        self.closed = []

    def open(self, pid, kind, create=False):
        if create:
            self.events[kind] = threading.Event()
        if kind not in self.events:
            raise OSError("not found")
        return kind

    def set(self, handle):
        self.events[handle].set()

    def wait(self, handle, timeout):
        return self.events[handle].wait(timeout)

    def close(self, handle):
        self.closed.append(handle)


@pytest.mark.parametrize("failure", [None, "flush", "sync", "close", "crashed"])
def test_unmount_waits_for_destroy_and_reports_failures(monkeypatch, failure):
    from amifuse.fuse_fs import AmigaFuseFS

    events = Events()
    monkeypatch.setattr(unmount, "_Events", lambda: events)
    exit_requested = threading.Event()
    monkeypatch.setattr(unmount, "_fuse_exit_callback", lambda: exit_requested.set)
    order = []

    def step(name):
        def run():
            assert not events.events["done"].is_set()
            order.append(name)
            if name == failure:
                raise OSError(name)
        return run

    bridge = SimpleNamespace(
        _write_enabled=True, state=SimpleNamespace(crashed=failure == "crashed"),
        flush_volume=step("flush"), vh=SimpleNamespace(shutdown=step("shutdown")),
        backend=SimpleNamespace(sync=step("sync"), close=step("close")),
    )
    fs = AmigaFuseFS(bridge)
    control = unmount.UnmountControl()
    fs._unmount_control = control
    fs.init("/")
    outcome = []

    def client():
        try:
            unmount.request_unmount(os.getpid(), timeout=3)
            outcome.append("ok")
        except OSError:
            outcome.append("error")

    thread = threading.Thread(target=client)
    thread.start()
    try:
        assert exit_requested.wait(2)
        assert not outcome  # fuse_exit alone is not a successful unmount
        fs.destroy("/")
        thread.join(2)
        assert not thread.is_alive()
        assert outcome == (["ok"] if failure is None else ["error"])
        assert order == (["flush"] if failure != "crashed" else []) + [
            "shutdown", "sync", "close"]
    finally:
        control.close()
        thread.join(4)


def test_timeout_leaves_process_running(monkeypatch):
    events = Events()
    monkeypatch.setattr(unmount, "_Events", lambda: events)
    control = unmount.UnmountControl()
    try:
        with pytest.raises(OSError, match="has not been terminated"):
            unmount.request_unmount(42, timeout=0)
        assert not control.completed
        assert events.events["stop"].is_set()
    finally:
        control.close()


def test_missing_control_is_an_error(monkeypatch):
    monkeypatch.setattr(unmount, "_Events", Events)
    with pytest.raises(OSError, match="has not been terminated"):
        unmount.request_unmount(42)


def test_batch_signals_all_mounts_and_collects_failures(monkeypatch):
    opened, closed, signaled, waits = [], [], [], []
    now = [100.0]

    class BatchEvents:
        def open(self, pid, kind):
            if pid == 1 and kind == "done":
                raise OSError("old mount has no events")
            opened.append((pid, kind))
            return pid, kind

        def set(self, handle):
            signaled.append(handle[0])

        def wait(self, handle, timeout):
            assert signaled == [2, 3, 4]
            waits.append((handle, timeout))
            pid, kind = handle
            if pid == 2:
                now[0] += timeout
                return False  # exhaust the shared deadline
            return kind == "done" or pid == 3  # 3 fails flush, 4 succeeds

        def close(self, handle):
            closed.append(handle)

    monkeypatch.setattr(unmount, "_Events", BatchEvents)
    monkeypatch.setattr(unmount.time, "monotonic", lambda: now[0])
    with pytest.raises(unmount.UnmountError) as caught:
        unmount.request_unmount_many([1, 2, 3, 4, 4], timeout=30)
    assert caught.value.completed == [4]
    assert set(caught.value.failures) == {1, 2, 3}
    assert "older AmiFUSE" in str(caught.value.failures[1])
    assert "Timed out" in str(caught.value.failures[2])
    assert "flush failure" in str(caught.value.failures[3])
    assert waits[0] == ((2, "done"), 30)
    assert all(timeout == 0 for _, timeout in waits[1:])
    assert closed == opened  # includes handles from the partially opened PID


def test_windows_cli_uses_control_without_taskkill(monkeypatch):
    from amifuse import fuse_fs, platform

    monkeypatch.setattr(fuse_fs, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    monkeypatch.setattr(platform, "_find_mount_owner_pids", lambda p: [42])
    request = Mock(side_effect=OSError("flush failed"))
    monkeypatch.setattr(unmount, "request_unmount_many", request)
    kill = Mock(side_effect=AssertionError("must not kill"))
    monkeypatch.setattr(platform, "kill_pids", kill)
    monkeypatch.setattr(fuse_fs, "kill_mount_owner_processes", kill)
    with pytest.raises(SystemExit, match="flush failed"):
        fuse_fs.cmd_unmount(SimpleNamespace(mountpoint=Path("R:")))
    request.assert_called_once_with([42], timeout=30.0)
    request.side_effect = lambda pids, **kwargs: pids
    fuse_fs.cmd_unmount(SimpleNamespace(mountpoint=Path("R:")))
    kill.assert_not_called()
    monkeypatch.setattr(platform, "_find_mount_owner_pids", lambda p: [])
    with pytest.raises(SystemExit, match="No amifuse process found"):
        fuse_fs.cmd_unmount(SimpleNamespace(mountpoint=Path("R:")))


def test_exit_callback_captures_mount_thread_context(monkeypatch):
    import ctypes

    get_context = Mock(return_value=SimpleNamespace(contents=SimpleNamespace(fuse=0x123456789)))
    exit_fuse = Mock()
    monkeypatch.setitem(sys.modules, "fuse", SimpleNamespace(
        _libfuse=SimpleNamespace(fuse_get_context=get_context, fuse_exit=exit_fuse)))
    callback = unmount._fuse_exit_callback()
    get_context.side_effect = AssertionError("context accessed from watcher")
    callback()
    assert isinstance(exit_fuse.call_args.args[0], ctypes.c_void_p)
    assert exit_fuse.call_args.args[0].value == 0x123456789


@pytest.mark.parametrize("requested", ["E:", "E:\\", "e:/", "e:\\."])
@pytest.mark.parametrize("mounted", ["E:", "E:\\", "e:/"])
def test_drive_mount_owner_matches_root_aliases(monkeypatch, requested, mounted):
    from amifuse import platform

    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "find_amifuse_mounts", lambda: [
        {"mountpoint": mounted, "pid": 42},
        {"mountpoint": "F:", "pid": 43},
        {"mountpoint": "E:\\other", "pid": 44},
    ])
    assert platform._find_mount_owner_pids(Path(requested)) == [42]


def test_mount_owner_is_process_behind_venv_launcher(monkeypatch):
    # A venv's python.exe re-runs the same command line under the base
    # interpreter; only that child creates the PID-named unmount events.
    from amifuse import platform

    cmd = {"mountpoint": "J:", "image": "pfs.hdf", "uptime_seconds": 1,
           "filesystem_type": None}
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "_find_amifuse_mounts_windows", lambda: [
        dict(cmd, pid=100, parent_pid=50),   # venv launcher
        dict(cmd, pid=200, parent_pid=100),  # base interpreter
    ])
    assert platform._find_mount_owner_pids(Path("J:")) == [200]


@pytest.mark.parametrize("replies", [[], [(0, 0, 0, 209)], [(0, 0, -1, 0)]])
def test_flush_requires_handler_acknowledgement(replies):
    from amifuse.fuse_fs import HandlerBridge

    bridge = HandlerBridge.__new__(HandlerBridge)
    bridge._closed = True
    bridge._lock = threading.RLock()
    bridge.state = SimpleNamespace(crashed=False)
    bridge._debug = False
    bridge.launcher = Mock()
    bridge.backend = Mock()
    bridge._run_until_replies = lambda: replies
    bridge._log_replies = Mock()
    if replies and replies[-1][2]:
        bridge.flush_volume()
    else:
        with pytest.raises(OSError, match="ACTION_FLUSH"):
            bridge.flush_volume()
    bridge.backend.sync.assert_called_once()


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows events")
def test_native_unmount_across_processes(tmp_path):
    marker = tmp_path / "flushed"
    code = '''
import os, sys, threading
from pathlib import Path
from amifuse import windows_unmount as w
requested = threading.Event()
w._fuse_exit_callback = lambda: requested.set
c = w.UnmountControl()
try:
    c.start()
    print("ready", os.getpid(), flush=True)
    if not requested.wait(10):
        raise RuntimeError("no request")
    Path(sys.argv[1]).write_text("flushed")
    c.stop()
    c.complete(True)
finally:
    c.close()
'''
    proc = subprocess.Popen([sys.executable, "-c", code, str(marker)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        # Not proc.pid: in a venv that is the launcher, not the interpreter.
        ready, pid = proc.stdout.readline().split()
        assert ready == "ready"
        unmount.request_unmount(int(pid), timeout=10)
        assert marker.read_text() == "flushed"
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
