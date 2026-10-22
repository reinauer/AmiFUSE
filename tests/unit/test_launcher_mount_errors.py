"""The launcher reports a mount that exits with an error, with its message.

Windows-only, like ``test_launcher.py``: Popen, ctypes.windll and the drive
probes are mocked, and the launcher's logs go to ``tmp_path``.
"""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only")

NO_DRIVER = (
    "ADF floppy image detected (DD, DOS1).\n"
    "Floppy images don't contain embedded filesystem drivers.\n"
    "No driver found for DOS type DOS1.\n"
)


@pytest.fixture
def launcher(monkeypatch, tmp_path):
    """Return ``(run, sleeps, popen_kwargs)``.

    ``run(units, children, present=())`` mounts ``units`` (partition names, or
    ``None`` for a single no-``--partition`` unit) on D:, E:, ... with
    ``main(["mount", ...])``. ``children[i]`` is ``(returncode, stderr_text)``
    for the i-th spawn: the fake child writes the text to the stderr file it is
    given, and ``poll()`` returns the code (``None`` = still running).
    ``present`` lists the drive paths that appear. It returns the dialog text,
    or ``None`` when no dialog was shown.
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
    sleeps = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    popen_kwargs = []

    def run(units, children, present=()):
        monkeypatch.setattr(
            mod,
            "_enumerate_mount_units",
            lambda image: (
                [mod._MountUnit(name=n, label=n or "floppy disk") for n in units],
                None,
            ),
        )
        monkeypatch.setattr(mod.os.path, "exists", lambda p: str(p) in present)
        queue = list(children)

        def fake_popen(cmd, **kwargs):
            popen_kwargs.append(kwargs)
            returncode, text = queue.pop(0)
            if text:
                kwargs["stderr"].write(text)
                kwargs["stderr"].flush()  # a real child's writes reach the file
            proc = MagicMock()
            proc.poll.return_value = returncode
            proc.returncode = returncode
            return proc

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        mod.main(["mount", "test.adf"])
        if not windll.user32.MessageBoxW.called:
            return None
        arg = windll.user32.MessageBoxW.call_args[0][1]
        return arg.value if hasattr(arg, "value") else arg

    return run, sleeps, popen_kwargs


def test_exited_mount_reports_its_error_at_once(launcher, tmp_path):
    """A mount that exits 1 is reported with its stderr, without waiting out
    the 15 s poll and without the soft "did not appear yet" wording."""
    run, sleeps, popen_kwargs = launcher
    msg = run([None], [(1, NO_DRIVER)])

    assert "Could not mount floppy disk (D:):" in msg
    assert "No driver found for DOS type DOS1." in msg
    assert "did not appear" not in msg
    assert len(sleeps) == 1  # reported on the first poll, not at the deadline
    # The child's stderr went to a per-drive file next to launcher.log.
    assert popen_kwargs[0]["stderr"].name == str(tmp_path / "mount-D.log")
    assert (tmp_path / "mount-D.log").read_text() == NO_DRIVER


def test_exited_mount_without_output_reports_its_exit_code(launcher):
    run, _sleeps, _kwargs = launcher
    msg = run([None], [(3, "")])

    assert "Could not mount floppy disk (D:):" in msg
    assert "exited with code 3" in msg


def test_exited_mount_shows_only_the_last_lines(launcher):
    run, _sleeps, _kwargs = launcher
    text = "".join(f"line {i}\n" for i in range(1, 31))
    msg = run([None], [(1, text)])

    assert "line 30" in msg and "line 21" in msg
    assert "line 20" not in msg


def test_fan_out_reports_exited_and_mounted_drives_in_one_dialog(launcher):
    """One partition fails, one mounts, one is still starting: one dialog
    names each with its own wording."""
    run, _sleeps, _kwargs = launcher
    msg = run(
        ["DH0", "DH1", "DH2"],
        [(None, ""), (1, "Cannot mount test.adf: bad partition\n"), (None, "")],
        present={"D:\\"},
    )

    assert "Mounted DH0 (D:)." in msg
    assert "Could not mount DH1 (E:):\nCannot mount test.adf: bad partition" in msg
    assert "DH2 (F:) did not appear yet" in msg


def test_running_and_mounted_children_are_not_reported(launcher):
    """A child that keeps running and whose drive appears shows no dialog."""
    run, _sleeps, _kwargs = launcher
    assert run([None], [(None, "")], present={"D:\\"}) is None


def test_previous_log_for_the_letter_is_replaced(launcher, tmp_path):
    run, _sleeps, _kwargs = launcher
    (tmp_path / "mount-D.log").write_text("old error\n")
    msg = run([None], [(1, NO_DRIVER)])

    assert "old error" not in msg
    assert (tmp_path / "mount-D.log").read_text() == NO_DRIVER


def test_log_held_by_another_launch_is_left_alone(launcher, tmp_path):
    """Two launches that picked the same letter never share its log: while
    another mount holds mount-D.log, this one's stderr goes to DEVNULL and
    its exit code is reported."""
    run, _sleeps, popen_kwargs = launcher
    held = open(str(tmp_path / "mount-D.log"), "w")
    try:
        held.write("other mount\n")
        held.flush()
        msg = run([None], [(1, "")])
        assert popen_kwargs[0]["stderr"] == subprocess.DEVNULL
        assert "exited with code 1" in msg
        assert "other mount" not in msg
        assert (tmp_path / "mount-D.log").read_text() == "other mount\n"
    finally:
        held.close()


def test_log_is_held_until_its_error_is_read(launcher, monkeypatch):
    """Another launch that picks D: after this mount exited, but before the
    poll read its log, cannot replace the log: this launcher still holds it."""
    from amifuse import launcher as mod

    run, _sleeps, _kwargs = launcher
    other = []

    def other_launch(_seconds):
        if not other:
            other.append(mod._open_mount_log("D:"))

    monkeypatch.setattr("time.sleep", other_launch)
    try:
        msg = run([None], [(1, NO_DRIVER)])
        assert other[0] == (subprocess.DEVNULL, None)
        assert "No driver found for DOS type DOS1." in msg
    finally:
        if other and other[0][1] is not None:
            other[0][0].close()
