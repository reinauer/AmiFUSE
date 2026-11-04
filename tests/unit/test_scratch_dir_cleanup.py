"""The scratch directory of a killed run doesn't stay for good (#362).

A run keeps its directory for as long as it runs, and a kill skips the
cleanup, so a later run removes it. It must never remove one a running
AmiFUSE is using.
"""

import os
import subprocess
import sys
import tempfile
import time
from unittest.mock import MagicMock

import pytest

from amifuse import vamos_runner


@pytest.fixture
def temp_dir(monkeypatch, tmp_path):
    path = tmp_path / "temp"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


def _left(temp_dir, pid, age=7200):
    """A scratch directory of the given PID, with a file in it."""
    path = temp_dir / f"amifuse_{pid}-k3x9_1b2"
    (path / "system").mkdir(parents=True)
    (path / "system" / "f").write_bytes(b"x")
    when = time.time() - age
    os.utime(path, (when, when))
    return path


def _ended_pid():
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def test_removes_what_a_killed_run_left(temp_dir):
    path = _left(temp_dir, _ended_pid())

    vamos_runner._remove_stale_scratch_dirs()

    assert not path.exists()


def test_keeps_the_directory_of_a_running_process(temp_dir):
    proc = subprocess.Popen([sys.executable, "-c", "input()"],
                            stdin=subprocess.PIPE)
    try:
        path = _left(temp_dir, proc.pid)
        vamos_runner._remove_stale_scratch_dirs()
        assert (path / "system" / "f").exists()
    finally:
        proc.communicate(b"\n")
    del proc

    vamos_runner._remove_stale_scratch_dirs()

    assert not path.exists()


def test_keeps_a_recent_directory_of_an_ended_process(temp_dir):
    path = _left(temp_dir, _ended_pid(), age=60)

    vamos_runner._remove_stale_scratch_dirs()

    assert path.exists()


@pytest.mark.parametrize("name", [
    "amifuse_k3x9_1b2",  # an earlier version's, with no PID
    "amifuse_abc-k3x9",
    "amifuse_1234567890-k3x9",
    "other_123-k3x9",
])
def test_leaves_other_names_alone(temp_dir, monkeypatch, name):
    monkeypatch.setattr("amifuse.platform._pid_exists", lambda pid: False)
    path = temp_dir / name
    path.mkdir()
    when = time.time() - 7200
    os.utime(path, (when, when))

    vamos_runner._remove_stale_scratch_dirs()

    assert path.exists()


def test_leaves_a_file_of_a_directory_name_alone(temp_dir, monkeypatch):
    monkeypatch.setattr("amifuse.platform._pid_exists", lambda pid: False)
    path = temp_dir / "amifuse_123-k3x9"
    path.write_bytes(b"x")
    when = time.time() - 7200
    os.utime(path, (when, when))

    vamos_runner._remove_stale_scratch_dirs()

    assert path.exists()


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="a directory symlink needs a privilege on Windows")
def test_leaves_what_a_link_of_a_directory_name_points_at(temp_dir, tmp_path,
                                                          monkeypatch):
    monkeypatch.setattr("amifuse.platform._pid_exists", lambda pid: False)
    target = tmp_path / "kept"
    target.mkdir()
    (target / "f").write_bytes(b"x")
    link = temp_dir / "amifuse_123-k3x9"
    link.symlink_to(target)
    when = time.time() - 7200
    os.utime(link, (when, when), follow_symlinks=False)

    vamos_runner._remove_stale_scratch_dirs()

    assert (target / "f").exists()


def test_goes_on_without_a_listing_of_the_temp_directory(temp_dir, monkeypatch):
    def listdir(path):
        raise OSError("no listing")

    monkeypatch.setattr(os, "listdir", listdir)

    vamos_runner._remove_stale_scratch_dirs()


def test_removal_errors_stop_no_other_directory(temp_dir, monkeypatch):
    path = _left(temp_dir, _ended_pid())
    later = _left(temp_dir, _ended_pid())
    real = vamos_runner.shutil.rmtree
    seen = []

    def rmtree(target, ignore_errors=False):
        seen.append(target)
        assert ignore_errors
        real(target, ignore_errors=True)

    monkeypatch.setattr(vamos_runner.shutil, "rmtree", rmtree)

    vamos_runner._remove_stale_scratch_dirs()

    assert sorted(seen) == sorted([str(path), str(later)])


def test_name_of_a_scratch_directory_is_one_the_sweep_knows(temp_dir):
    with tempfile.TemporaryDirectory(prefix=f"amifuse_{os.getpid()}-") as name:
        match = vamos_runner._SCRATCH_DIR_NAME.fullmatch(os.path.basename(name))

    assert match is not None and int(match[1]) == os.getpid()


def test_setup_sweeps_and_names_its_directory(temp_dir, monkeypatch):
    for name in ("Machine", "MemoryMap", "TraceManager", "VamosPathManager",
                 "Scheduler", "Runtime", "SetupLibManager"):
        monkeypatch.setattr(vamos_runner, name, MagicMock())
    left = _left(temp_dir, _ended_pid())
    runtime = vamos_runner.VamosHandlerRuntime()

    runtime.setup()

    try:
        assert not left.exists()
        match = vamos_runner._SCRATCH_DIR_NAME.fullmatch(
            os.path.basename(runtime._temp_dir.name))
        assert match is not None and int(match[1]) == os.getpid()
    finally:
        runtime._temp_dir.cleanup()


@pytest.mark.parametrize("handle,error,gone", [
    (123, 0, False), (0, 87, True), (0, 5, False), (0, 6, False),
])
def test_windows_cleanup_requires_a_definitely_gone_owner(
        monkeypatch, handle, error, gone):
    import ctypes
    from amifuse import platform as plat

    kernel = MagicMock()
    kernel.OpenProcess.return_value = handle
    dll = MagicMock(return_value=kernel)
    monkeypatch.setattr(ctypes, "WinDLL", dll, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error, raising=False)
    monkeypatch.setattr(plat.sys, "platform", "win32")

    assert plat._temp_owner_gone(12345) is gone
    dll.assert_called_once_with("kernel32", use_last_error=True)
    if handle:
        kernel.CloseHandle.assert_called_once_with(handle)
    else:
        kernel.CloseHandle.assert_not_called()
