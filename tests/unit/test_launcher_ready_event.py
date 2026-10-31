"""A launch counts a drive as its own only once its mount signals it (#237).

Another program (a CLI mount, a USB stick, ``subst``) can take a launch's
letter before the launch's mount is up. The launch passes each mount a token
in ``AMIFUSE_READY_TOKEN``; the mount holds the named event for it from FUSE
init on, and the poll counts the drive only when the drive and the event are
both present on two consecutive ticks.

Windows-only, like ``test_launcher.py``. The launcher tests mock Popen,
ctypes.windll, the drive probe and the event check, and send the logs to
``tmp_path``; the last test uses the real Windows events.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

TOKEN_ENV = "AMIFUSE_READY_TOKEN"
ALLOCATED = (
    "Drive D: is already allocated; choose a different drive letter or "
    "free it first.\n"
)


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A scripted launch. ``w.on_tick(tick)`` runs at each poll tick (1, 2,
    ...) and changes ``w.drives`` (drive paths present), ``w.ready`` (tokens
    whose event exists) and ``w.exits`` (letter -> ``(code, stderr)`` for a
    mount that has exited). ``w.run(verb, units)`` runs ``main``; ``w.tokens``
    maps each letter to the token its mount got, ``w.envs`` holds each spawn's
    environment, ``w.opened`` the ``(tick, path)`` of each Explorer window and
    ``w.dialogs`` each dialog's text."""
    import ctypes
    from amifuse import launcher as mod

    class World:
        pass

    w = World()
    w.mod = mod
    w.tick = 0
    w.drives, w.ready, w.exits = set(), set(), {}
    w.tokens, w.envs, w.opened, w.dialogs = {}, [], [], []
    w.on_tick = lambda tick: None

    windll = MagicMock()
    windll.kernel32.OpenMutexW.return_value = 1  # tray already running
    windll.user32.MessageBoxW.side_effect = lambda _h, text, _t, _f: (
        w.dialogs.append(text.value if hasattr(text, "value") else text)
    )
    monkeypatch.setattr(ctypes, "windll", windll)
    monkeypatch.setattr(mod, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(mod.os, "_exit", MagicMock())
    monkeypatch.setattr(
        mod.platform, "_windows_allocated_drive_letters", lambda: {"A", "B", "C"}
    )
    monkeypatch.setattr(mod.os.path, "exists", lambda p: str(p) in w.drives)
    monkeypatch.setattr(mod, "_mount_ready", lambda token: token in w.ready,
                        raising=False)
    monkeypatch.setattr(
        mod.os, "startfile", lambda p: w.opened.append((w.tick, str(p)))
    )

    def sleep(_seconds):
        w.tick += 1
        w.on_tick(w.tick)

    monkeypatch.setattr("time.sleep", sleep)

    def fake_popen(cmd, **kwargs):
        letter = cmd[cmd.index("--mountpoint") + 1]
        env = kwargs.get("env") or {}
        w.envs.append(env)
        w.tokens[letter] = env.get(TOKEN_ENV)
        stderr = kwargs["stderr"]
        proc = MagicMock()

        def poll():
            if letter not in w.exits:
                return None
            code, text = w.exits[letter]
            if text and stderr is not subprocess.DEVNULL and not stderr.closed:
                stderr.write(text)
                stderr.flush()
                w.exits[letter] = (code, "")
            proc.returncode = code
            return code

        proc.poll.side_effect = poll
        return proc

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)

    def run(verb, units=(None,)):
        monkeypatch.setattr(
            mod,
            "_enumerate_mount_units",
            lambda image: (
                [mod._MountUnit(name=n, label=n or "floppy disk") for n in units],
                None,
            ),
        )
        mod.main([verb, "test.adf"])
        return (tmp_path / "launcher.log").read_text()

    w.run = run
    return w


@pytest.mark.parametrize("verb", ["open", "mount"])
def test_another_programs_drive_is_not_the_launchs(world, verb):
    """The issue's repro: another program holds D: before the launch's
    mount is up. The launch opens no Explorer window and reports its own
    mount's error once that mount exits."""
    w = world
    w.drives.add("D:\\")

    def on_tick(tick):
        if tick == 3:
            w.exits["D:"] = (1, ALLOCATED)

    w.on_tick = on_tick
    log = w.run(verb)

    assert "Mount ready at D:" not in log
    assert w.opened == []
    assert len(w.dialogs) == 1
    assert "Could not mount floppy disk (D:):" in w.dialogs[0]
    assert "Drive D: is already allocated" in w.dialogs[0]


def test_another_programs_drive_until_the_deadline(world):
    """Another program's drive and a mount that neither exits nor signals:
    the launch times out softly instead of opening the other drive."""
    w = world
    w.drives.add("D:\\")
    log = w.run("open")

    assert "Mount ready at D:" not in log
    assert w.opened == []
    assert len(w.dialogs) == 1
    assert "floppy disk (D:) did not appear yet" in w.dialogs[0]


def test_ready_event_after_the_drive_counts(world):
    """The drive shows before the event (ticks 1-3); the event from tick 4
    on. The launch counts the drive at tick 5, the second tick with both."""
    w = world
    w.drives.add("D:\\")

    def on_tick(tick):
        if tick == 4:
            w.ready.add(w.tokens["D:"])

    w.on_tick = on_tick
    log = w.run("open")

    assert w.opened == [(5, "D:\\")]
    assert w.dialogs == []
    assert "Mount ready at D:" in log


def test_event_for_a_single_tick_is_not_a_mount(world):
    """WinFsp calls init before it sets the mount point. A mount whose mount
    point is then refused holds its event only briefly, and its process can
    still look alive at that tick: one tick with the drive and the event is
    not enough."""
    w = world
    w.drives.add("D:\\")  # another program's drive

    def on_tick(tick):
        if tick == 2:
            w.ready.add(w.tokens["D:"])
        elif tick == 3:
            w.ready.clear()
        elif tick == 4:
            w.exits["D:"] = (1, ALLOCATED)

    w.on_tick = on_tick
    log = w.run("open")

    assert "Mount ready at D:" not in log
    assert w.opened == []
    assert len(w.dialogs) == 1
    assert "Drive D: is already allocated" in w.dialogs[0]


def test_each_mount_gets_its_own_token(world, monkeypatch):
    """Each spawn's environment is the launcher's plus its own token, and
    the poll checks each letter against its own mount's token."""
    w = world
    monkeypatch.setenv("AMIFUSE_TEST_MARKER", "kept")
    w.drives.update({"D:\\", "E:\\"})

    def on_tick(tick):
        if tick == 1:
            w.ready.add(w.tokens["E:"])  # only E:'s mount is up

    w.on_tick = on_tick
    w.run("open", units=("DH0", "DH1"))

    assert len(w.envs) == 2
    assert all(env.get("AMIFUSE_TEST_MARKER") == "kept" for env in w.envs)
    d, e = w.tokens["D:"], w.tokens["E:"]
    assert d and e and d != e and len(d) >= 32
    assert [path for _tick, path in w.opened] == ["E:\\"]
    assert len(w.dialogs) == 1
    assert "Mounted DH1 (E:)." in w.dialogs[0]
    assert "DH0 (D:) did not appear yet" in w.dialogs[0]


def test_ready_event_lives_as_long_as_the_mount(monkeypatch):
    """Real Windows events: the mount's UnmountControl creates the event in
    start() and drops it in close(). The launcher's checks see it while it
    lives and never keep it alive themselves."""
    import os

    from amifuse import launcher, windows_unmount

    token = os.urandom(16).hex()
    monkeypatch.setenv(TOKEN_ENV, token)
    monkeypatch.setattr(windows_unmount, "_fuse_exit_callback",
                        lambda: (lambda: None))
    control = windows_unmount.UnmountControl()
    try:
        assert not launcher._mount_ready(token)
        control.start()
        assert all(launcher._mount_ready(token) for _ in range(3))
        assert not launcher._mount_ready(os.urandom(16).hex())
    finally:
        control.close()
    assert not launcher._mount_ready(token)


def test_ready_event_goes_when_teardown_starts(monkeypatch):
    """destroy() calls stop() before its flush, which can take seconds. The
    event goes there, not later in close(): a mount whose mount point WinFsp
    refused after init must not read as up through its teardown."""
    import os

    from amifuse import launcher, windows_unmount

    token = os.urandom(16).hex()
    monkeypatch.setenv(TOKEN_ENV, token)
    monkeypatch.setattr(windows_unmount, "_fuse_exit_callback",
                        lambda: (lambda: None))
    control = windows_unmount.UnmountControl()
    try:
        control.start()
        assert launcher._mount_ready(token)
        control.stop()
        assert not launcher._mount_ready(token)
    finally:
        control.close()


def test_mount_does_not_join_an_existing_ready_event(monkeypatch, capsys):
    """An event of that name that another process already holds would
    outlive this mount, so the mount does not take it as its own."""
    import ctypes
    import os

    from amifuse import launcher, windows_unmount

    token = os.urandom(16).hex()
    monkeypatch.setenv(TOKEN_ENV, token)
    monkeypatch.setattr(windows_unmount, "_fuse_exit_callback",
                        lambda: (lambda: None))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    other = kernel32.CreateEventW(None, True, False,
                                  "Local\\amifuse-ready-" + token)
    assert other
    control = None
    try:
        control = windows_unmount.UnmountControl()
        control.start()
        assert "ready" not in control.handles
        assert "could not signal the launcher" in capsys.readouterr().err
        control.close()
        # The mount closed only its own handle: the other holder's event
        # is still there.
        assert launcher._mount_ready(token)
    finally:
        if control is not None:
            control.close()
        kernel32.CloseHandle(other)
