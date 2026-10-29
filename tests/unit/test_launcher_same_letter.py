"""A launch whose mount exits with an error reports it, even when a drive is
present at its letter.

Something other than this launch can take a letter after the launch
selected it (another program, or a launch whose claim on the letter has
ended; test_launcher_letter_claims.py covers the claims). The losing mount
exits 1, but the other drive is present, so a poll that probes the drive
before the process would report the loser as mounted and open Explorer on
the other image. Windows-only, like ``test_launcher.py``: Popen,
ctypes.windll, os.startfile and the drive probe are mocked, and the
launcher's logs go to ``tmp_path``.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

ALLOCATED = (
    "Drive D: is already allocated; choose a different drive letter or free "
    "it first.\n"
)


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    """Return ``(run, opened)``.

    ``run(verb, returncode, stderr_text)`` runs ``main([verb, "test.adf"])``
    for a single unit on D:, with D:\\ present throughout. The fake child
    writes ``stderr_text`` to the stderr file it is given, and ``poll()``
    returns ``returncode`` (``None`` = still running). It returns the dialog
    text, or ``None`` when no dialog was shown. ``opened`` records each
    ``os.startfile`` path.
    """
    import ctypes
    from amifuse import launcher as mod

    windll = MagicMock()
    windll.kernel32.OpenMutexW.return_value = 1  # tray already running
    monkeypatch.setattr(ctypes, "windll", windll)
    monkeypatch.setattr(mod, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(mod.os, "_exit", MagicMock())
    monkeypatch.setattr(
        mod.platform, "_windows_allocated_drive_letters", lambda: {"A", "B", "C"}
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr(
        mod,
        "_enumerate_mount_units",
        lambda image: ([mod._MountUnit(name=None, label="floppy disk")], None),
    )
    # Another launch's mount already holds D:.
    monkeypatch.setattr(mod.os.path, "exists", lambda p: str(p) == "D:\\")
    opened = []
    monkeypatch.setattr(mod.os, "startfile", lambda p: opened.append(str(p)))

    def run(verb, returncode, stderr_text):
        def fake_popen(cmd, **kwargs):
            if stderr_text and kwargs["stderr"] != subprocess.DEVNULL:
                kwargs["stderr"].write(stderr_text)
                kwargs["stderr"].flush()  # a real child's writes reach the file
            proc = MagicMock()
            proc.poll.return_value = returncode
            proc.returncode = returncode
            return proc

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        mod.main([verb, "test.adf"])
        if not windll.user32.MessageBoxW.called:
            return None
        arg = windll.user32.MessageBoxW.call_args[0][1]
        return arg.value if hasattr(arg, "value") else arg

    return run, opened


@pytest.mark.parametrize("verb", ["open", "mount"])
def test_exited_mount_is_reported_although_its_letter_is_present(
    launcher, tmp_path, verb
):
    run, opened = launcher
    msg = run(verb, 1, ALLOCATED)

    assert msg is not None
    assert "Could not mount floppy disk (D:):" in msg
    assert "Drive D: is already allocated" in msg
    assert opened == []  # never opens Explorer on the other image's drive
    log = (tmp_path / "launcher.log").read_text()
    assert "Mount for D: exited with code 1" in log
    assert "Mount ready at D:" not in log


@pytest.mark.parametrize("verb", ["open", "mount"])
def test_exited_mount_is_reported_when_the_other_launch_holds_its_log(
    launcher, tmp_path, verb
):
    """The other ordering of the race: the winning launch opened mount-D.log
    first and still holds it, so this mount's stderr goes to DEVNULL. The
    launch still reports the failure, with its exit code."""
    run, opened = launcher
    held = open(str(tmp_path / "mount-D.log"), "w")
    try:
        msg = run(verb, 1, ALLOCATED)
    finally:
        held.close()

    assert msg is not None
    assert "Could not mount floppy disk (D:):" in msg
    assert "exited with code 1" in msg
    assert opened == []
    assert "Mount ready at D:" not in (tmp_path / "launcher.log").read_text()


def test_running_mount_whose_drive_appears_opens_explorer(launcher, tmp_path):
    """The normal success: the mount is still running and its drive is
    present, so the launch opens Explorer on it and shows no dialog."""
    run, opened = launcher
    assert run("open", None, "") is None
    assert opened == ["D:\\"]
    assert "Mount ready at D:" in (tmp_path / "launcher.log").read_text()
