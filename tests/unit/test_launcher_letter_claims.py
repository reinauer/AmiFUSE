"""Launches that run at once mount on different drive letters.

Each launch claims its letters (a named mutex per letter) until its poll
ends, so a launch that selects while another's mount is still starting
picks another letter, instead of reading the other image's drive as its
own (#186) or failing on the taken letter (#187).

Windows-only, like ``test_launcher.py``: Popen, ctypes.windll (the dialog
and the tray), os.startfile, the allocation bitmask and the drive probe are
mocked, and the launcher's logs go to ``tmp_path``. The claims are the real
named mutexes, so two launches in this process contend as two launcher
processes do.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")


@pytest.fixture
def world(monkeypatch, tmp_path):
    """A fake machine shared by every launch in the test.

    ``world.drives`` holds the letters that are allocated and present (A:-C:
    to start with). ``world.spawned`` records each mount's ``--mountpoint``
    in spawn order and ``world.procs`` its fake process, whose ``poll()``
    returns ``returncode`` (``None`` = still running) and ``stderr`` the
    file it was given. ``world.on_sleep`` runs at each poll's sleep.
    ``world.opened`` records Explorer windows, ``world.dialogs`` each
    dialog's text.
    """
    import ctypes
    from amifuse import launcher as mod

    class World:
        pass

    w = World()
    w.drives = {"A", "B", "C"}
    w.spawned = []
    w.procs = []
    w.opened = []
    w.dialogs = []
    w.on_sleep = lambda: None
    w.mod = mod

    windll = MagicMock()
    windll.kernel32.OpenMutexW.return_value = 1  # tray already running
    windll.user32.MessageBoxW.side_effect = lambda _h, text, _t, _f: (
        w.dialogs.append(text.value if hasattr(text, "value") else text)
    )
    monkeypatch.setattr(ctypes, "windll", windll)
    monkeypatch.setattr(mod, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(mod.os, "_exit", MagicMock())
    monkeypatch.setattr(
        mod.platform, "_windows_allocated_drive_letters", lambda: set(w.drives)
    )
    monkeypatch.setattr(
        mod.os.path, "exists",
        lambda p: len(str(p)) == 3 and str(p)[0] in w.drives,
    )
    monkeypatch.setattr(mod.os, "startfile", lambda p: w.opened.append(str(p)))
    monkeypatch.setattr("time.sleep", lambda _seconds: w.on_sleep())
    monkeypatch.setattr(
        mod,
        "_enumerate_mount_units",
        lambda image: ([mod._MountUnit(name=None, label="floppy disk")], None),
    )

    def fake_popen(cmd, **kwargs):
        proc = MagicMock()
        proc.poll.side_effect = lambda: proc.returncode
        proc.returncode = None
        proc.stderr = kwargs["stderr"]
        w.spawned.append(cmd[cmd.index("--mountpoint") + 1])
        w.procs.append(proc)
        return proc

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    return w


def _two_launches(w, b_mount_validates):
    """Launch B starts during launch A's first poll, while A's mount on its
    letter is still starting. A's drive then arrives during B's first poll
    and B's drive during B's second. With ``b_mount_validates``, B's mount
    exits as a real one does when its letter is allocated by the time it
    checks it: 1, with the "already allocated" message."""
    sleeps = []

    def on_sleep():
        sleeps.append(None)
        if len(sleeps) == 1:
            w.mod.main(["open", "b.adf"])
        elif len(sleeps) == 2:
            w.drives.add(w.spawned[0][0])
            letter = w.spawned[1]
            if b_mount_validates and letter[0] in w.drives:
                proc = w.procs[1]
                if proc.stderr is not w.mod.subprocess.DEVNULL:
                    proc.stderr.write(
                        f"Drive {letter} is already allocated; choose a "
                        "different drive letter or free it first.\n"
                    )
                    proc.stderr.flush()
                proc.returncode = 1
        elif len(sleeps) == 3:
            w.drives.add(w.spawned[1][0])

    w.on_sleep = on_sleep
    w.mod.main(["open", "a.adf"])


def test_second_launch_does_not_read_the_first_launchs_drive(world, tmp_path):
    """#186: B's mount is still starting when A's drive arrives. B mounts
    on E:, not on A's D:, and each launch opens Explorer on its own drive."""
    w = world
    _two_launches(w, b_mount_validates=False)

    assert w.spawned == ["D:", "E:"]
    assert sorted(w.opened) == ["D:\\", "E:\\"]
    assert w.dialogs == []
    log = (tmp_path / "launcher.log").read_text()
    assert "Mount ready at D:" in log and "Mount ready at E:" in log
    # Both launches have released their claims.
    for letter in ("D:", "E:"):
        handle = w.mod._claim_drive_letter(letter)
        assert handle is not None
        w.mod._kernel32.CloseHandle(handle)


def test_second_launch_mounts_on_the_next_letter_instead_of_failing(world):
    """#187: B's mount checks its letter once A's drive has arrived. B
    mounts on E:, so it never fails on a taken letter or shows a dialog."""
    w = world
    _two_launches(w, b_mount_validates=True)

    assert w.spawned == ["D:", "E:"]
    assert sorted(w.opened) == ["D:\\", "E:\\"]
    assert w.dialogs == []


def test_letter_claimed_by_another_launch_is_skipped(world):
    """A letter that another launch has claimed is not selected, although
    GetLogicalDrives still reports it free."""
    w = world
    other = w.mod._claim_drive_letter("D:")
    assert other is not None
    try:
        w.on_sleep = lambda: w.drives.add(w.spawned[0][0])
        w.mod.main(["mount", "a.adf"])
    finally:
        w.mod._kernel32.CloseHandle(other)

    assert w.spawned == ["E:"]
    assert w.dialogs == []


def test_letter_allocated_just_before_its_claim_is_skipped(world, monkeypatch):
    """Between this launch reading the bitmask and claiming D:, another
    launch's drive arrives on D: and that launch releases its claim. The
    claim then succeeds, but D: is taken, so this launch mounts on E:."""
    w = world
    other = w.mod._claim_drive_letter("D:")
    assert other is not None
    reads = []

    def bitmask():
        snapshot = set(w.drives)
        reads.append(None)
        if len(reads) == 1:
            w.drives.add("D")
            w.mod._kernel32.CloseHandle(other)
        return snapshot

    monkeypatch.setattr(w.mod.platform, "_windows_allocated_drive_letters",
                        bitmask)
    w.on_sleep = lambda: w.drives.add(w.spawned[0][0])
    w.mod.main(["open", "b.adf"])

    assert w.spawned == ["E:"]
    assert w.opened == ["E:\\"]
    assert w.dialogs == []


def test_claims_that_cannot_be_created_use_the_letters_unclaimed(world,
                                                                 tmp_path):
    """No claim can be created: an object of another type holds every
    claim's name, so each fails with an error other than ACCESS_DENIED. The
    launch logs it and mounts on D: unclaimed, as before claims, instead of
    finding no letter."""
    import ctypes

    w = world
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateEventW.restype = ctypes.c_void_p
    kernel32.CreateEventW.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p,
    ]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    events = [
        kernel32.CreateEventW(None, True, False, w.mod._CLAIM_NAME.format(c))
        for c in w.mod._DRIVE_LETTER_ALPHABET
    ]
    try:
        assert all(events)
        w.on_sleep = lambda: w.drives.add(w.spawned[0][0])
        w.mod.main(["mount", "a.adf"])
    finally:
        for event in events:
            if event:
                kernel32.CloseHandle(event)

    assert w.spawned == ["D:"]
    assert w.dialogs == []
    assert "Could not claim D: (error 6)" in (
        tmp_path / "launcher.log").read_text()
