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


def test_windows_cli_uses_control_without_taskkill(monkeypatch):
    from amifuse import fuse_fs, platform

    monkeypatch.setattr(fuse_fs, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(os.path, "ismount", lambda p: True)
    monkeypatch.setattr(platform, "_find_mount_owner_pids", lambda p: [42])
    request = Mock(side_effect=OSError("flush failed"))
    monkeypatch.setattr(unmount, "request_unmount", request)
    kill = Mock(side_effect=AssertionError("must not kill"))
    monkeypatch.setattr(platform, "kill_pids", kill)
    monkeypatch.setattr(fuse_fs, "kill_mount_owner_processes", kill)
    with pytest.raises(SystemExit, match="flush failed"):
        fuse_fs.cmd_unmount(SimpleNamespace(mountpoint=Path("R:")))
    request.assert_called_once_with(42, timeout=30.0)
    request.side_effect = None
    fuse_fs.cmd_unmount(SimpleNamespace(mountpoint=Path("R:")))
    kill.assert_not_called()


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
import sys, threading
from pathlib import Path
from amifuse import windows_unmount as w
requested = threading.Event()
w._fuse_exit_callback = lambda: requested.set
c = w.UnmountControl()
try:
    c.start()
    print("ready", flush=True)
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
        assert proc.stdout.readline().strip() == "ready"
        unmount.request_unmount(proc.pid, timeout=10)
        assert marker.read_text() == "flushed"
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.communicate(timeout=10)
