"""How mount and format end.

A failing bridge close is reported on stderr and replaces neither the error
the command exits with nor a finished format or unmount; format names each
ACTION_FORMAT error code by its meaning; and mount removes a handler
extracted from the RDB once the bridge has loaded it.
"""

import errno
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from amifuse import fuse_fs, platform, rdb_inspect

DOS3 = 0x444F5303
CLOSE_ERROR = "[amifuse] could not close the bridge: [Errno 5] Input/output error"
STARTUP_CRASH = "Filesystem handler crashed during startup; mount aborted."
INHIBIT, FORMAT, FLUSH = 0x100, 0x200, 0x300


def _failing_close():
    return OSError(errno.EIO, "Input/output error")


# --- format -----------------------------------------------------------------


@pytest.fixture
def format_bridge(monkeypatch, tmp_path):
    """format_volume on DH0 with a mocked bridge; returns (bridge, image).

    The bridge's handler answers INHIBIT and FLUSH, and ACTION_FORMAT with
    bridge.format_reply.
    """
    bridge = MagicMock()
    bridge.state.crashed = False
    bridge.launcher.alloc_bstr.return_value = (None, 0x1234)
    bridge.launcher.send_inhibit.return_value = (INHIBIT, 0)
    bridge.launcher.send_format.return_value = (FORMAT, 0)
    bridge.launcher.send_flush.return_value = (FLUSH, 0)
    bridge.format_reply = (-1, 0)

    def run_until_reply(pkt, label, **kwargs):
        return bridge.format_reply if pkt == FORMAT else (-1, 0)

    bridge._run_until_reply.side_effect = run_until_reply
    monkeypatch.setattr(fuse_fs, "HandlerBridge", MagicMock(return_value=bridge))
    monkeypatch.setattr(fuse_fs, "get_partition_info",
                        lambda *a, **kw: {"name": "DH0", "dostype": DOS3})
    image = tmp_path / "disk.hdf"
    image.write_bytes(b"")
    return bridge, image


def _format(image):
    fuse_fs.format_volume(image, image.parent / "x.handler", None, "DH0")


def test_failing_close_keeps_the_format_error(capsys, format_bridge):
    """The refusal stays the error format exits with (the issue's script)."""
    bridge, image = format_bridge
    bridge.format_reply = (0, fuse_fs.ERROR_NOT_A_DOS_DISK)
    bridge.close.side_effect = _failing_close()

    with pytest.raises(SystemExit) as caught:
        _format(image)

    assert caught.value.code == "Format failed: Not a DOS disk"
    bridge.close.assert_called_once_with()
    assert capsys.readouterr().err.splitlines() == [CLOSE_ERROR]


def test_failing_close_after_format_complete_is_one_line(capsys, format_bridge):
    """A finished format returns normally: exit status 0, no traceback."""
    bridge, image = format_bridge
    bridge.close.side_effect = _failing_close()

    _format(image)

    out = capsys.readouterr()
    assert "Format complete. Volume 'Empty' created on partition 'DH0'." in out.out
    assert out.err.splitlines() == [CLOSE_ERROR]
    bridge.backend.fsync.assert_called_once_with()


FORMAT_ERRORS = {
    202: "Object in use",
    214: "Disk is write-protected",
    218: "Device not mounted",
    225: "Not a DOS disk",
    226: "No disk in drive",
    # ERROR_OBJECT_NOT_FOUND and ERROR_NO_MORE_ENTRIES, once named wrongly.
    205: "error code 205",
    232: "error code 232",
}


@pytest.mark.parametrize("res2, shown", FORMAT_ERRORS.items(),
                         ids=[str(code) for code in FORMAT_ERRORS])
def test_format_names_each_error_code_by_its_meaning(format_bridge, res2, shown):
    bridge, image = format_bridge
    bridge.format_reply = (0, res2)

    with pytest.raises(SystemExit) as caught:
        _format(image)

    assert caught.value.code == f"Format failed: {shown}"


# --- mount ------------------------------------------------------------------


@pytest.fixture
def mount(monkeypatch, tmp_path):
    """mount_fuse on DH0 with a handler extracted from the RDB, up to FUSE.

    Returns a namespace: bridge, the extracted handler, whether it existed
    when the bridge was built (handler_at_load), the volume icon, FUSE (it
    returns as after an unmount, without destroy()) and run(**kwargs).
    """
    handler = tmp_path / "amifuse_extracted.handler"
    handler.write_bytes(b"\x00\x00\x03\xf3handler")
    volicon = tmp_path / "volicon.icns"
    volicon.write_bytes(b"icon")
    bridge = MagicMock()
    bridge.state.crashed = False
    bridge.volume_name.return_value = "Work"
    fuse = MagicMock()
    handler_at_load = []

    def load_handler(*args, **kwargs):
        handler_at_load.append(handler.exists())
        return bridge

    import amifuse.windows_unmount as windows_unmount

    monkeypatch.setattr(rdb_inspect, "detect_adf", lambda image: None)
    monkeypatch.setattr(rdb_inspect, "detect_iso", lambda image: None)
    monkeypatch.setattr(platform, "check_fuse_available", lambda: None)
    monkeypatch.setattr(platform, "validate_mountpoint", lambda mp: None)
    monkeypatch.setattr(platform, "get_fuse_mountpoint", lambda mp: str(mp))
    monkeypatch.setattr(platform, "should_auto_create_mountpoint", lambda mp: True)
    monkeypatch.setattr(platform, "pre_generate_volume_icon",
                        lambda *a, **kw: volicon)
    monkeypatch.setattr(fuse_fs, "get_partition_info",
                        lambda *a, **kw: {"name": "DH0", "dostype": DOS3})
    monkeypatch.setattr(fuse_fs, "resolve_driver", lambda *a, **kw: SimpleNamespace(
        path=handler, description="DOS3/0x444f5303 (from RDB)", temporary=True))
    monkeypatch.setattr(fuse_fs, "HandlerBridge", MagicMock(side_effect=load_handler))
    monkeypatch.setattr(windows_unmount, "UnmountControl", MagicMock())
    monkeypatch.setattr(fuse_fs, "AmigaFuseFS", MagicMock())
    monkeypatch.setattr(fuse_fs, "FUSE", fuse)

    def run(**kwargs):
        return fuse_fs.mount_fuse(Path("work.hdf"), None, tmp_path / "mnt", None,
                                  partition="DH0", foreground=True, **kwargs)

    return SimpleNamespace(bridge=bridge, handler=handler,
                           handler_at_load=handler_at_load, volicon=volicon,
                           fuse=fuse, run=run)


def test_extracted_handler_is_gone_once_the_bridge_is_up(mount):
    """A mount killed while FUSE runs leaves no extracted handler behind."""
    seen = []
    mount.fuse.side_effect = lambda *a, **kw: seen.append(mount.handler.exists())

    mount.run()

    assert mount.handler_at_load == [True]
    assert seen == [False]


def test_failed_early_removal_is_retried_at_the_end(monkeypatch, capsys, mount):
    """A handler the early removal cannot remove is removed at the end."""
    real_unlink = Path.unlink
    calls = []

    def unlink(self, missing_ok=False):
        if self == mount.handler:
            calls.append(self)
            if len(calls) == 1:
                raise PermissionError(errno.EACCES, "Access is denied", str(self))
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    mount.run()

    assert len(calls) == 2
    assert not mount.handler.exists()
    assert "could not remove" not in capsys.readouterr().err


def _crash_at_startup(bridge):
    bridge.state.crashed = True


def _crash_reading_volume_name(bridge):
    def volume_name():
        bridge.state.crashed = True
        return "Work"

    bridge.volume_name.side_effect = volume_name


@pytest.mark.parametrize("crash", [_crash_at_startup, _crash_reading_volume_name],
                         ids=["at-startup", "reading-volume-name"])
def test_failing_close_keeps_the_startup_crash_error(capsys, mount, crash):
    crash(mount.bridge)
    mount.bridge.close.side_effect = _failing_close()

    with pytest.raises(SystemExit) as caught:
        mount.run()

    assert caught.value.code == STARTUP_CRASH
    assert capsys.readouterr().err.splitlines() == [CLOSE_ERROR]
    mount.fuse.assert_not_called()
    assert not mount.handler.exists()


def test_failing_runtime_teardown_keeps_the_startup_crash_error(capsys, mount):
    """Not only an OSError: the crashed runtime's teardown may raise anything."""
    _crash_at_startup(mount.bridge)
    mount.bridge.close.side_effect = RuntimeError("stale library state")

    with pytest.raises(SystemExit) as caught:
        mount.run()

    assert caught.value.code == STARTUP_CRASH
    assert capsys.readouterr().err.splitlines() == [
        "[amifuse] could not close the bridge: stale library state"]


def test_failing_close_keeps_the_fuse_error(capsys, mount):
    """An error already propagating from FUSE stands."""
    failure = RuntimeError("WinFsp refused the mount")
    mount.fuse.side_effect = failure
    mount.bridge.close.side_effect = _failing_close()

    # debug re-raises the FUSE error on every platform.
    with pytest.raises(RuntimeError) as caught:
        mount.run(debug=True)

    assert caught.value is failure
    assert CLOSE_ERROR in capsys.readouterr().err.splitlines()


def test_failing_close_after_unmount_is_one_line(capsys, mount):
    """A mount whose FUSE loop returns without destroy() (which closes the
    bridge itself) returns, reports the close and removes the icon."""
    mount.bridge.close.side_effect = _failing_close()

    mount.run(icons=True)

    mount.fuse.assert_called_once()
    assert capsys.readouterr().err.splitlines() == [CLOSE_ERROR]
    assert not mount.volicon.exists()
    assert not mount.handler.exists()
