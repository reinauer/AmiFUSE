"""fsyncdir() must write the handler's buffers to the image before it returns."""

import errno
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import fuse_fs as fs


def _make_fs(write_enabled=True, crashed=False):
    bridge = Mock()
    bridge._write_enabled = write_enabled
    bridge.state = SimpleNamespace(crashed=crashed)
    return fs.AmigaFuseFS(bridge, debug=False, icons=False), bridge


@pytest.mark.parametrize("datasync", [0, 1])
def test_fsyncdir_flushes_the_volume(datasync):
    ops, bridge = _make_fs()
    assert ops.fsyncdir("/dir", datasync, 1) == 0
    bridge.flush_volume.assert_called_once_with()


def test_fsyncdir_on_read_only_mount_sends_nothing():
    ops, bridge = _make_fs(write_enabled=False)
    assert ops.fsyncdir("/dir", 0, 1) == 0
    bridge.flush_volume.assert_not_called()


def test_fsyncdir_reports_a_failed_flush_as_eio():
    ops, bridge = _make_fs()
    bridge.flush_volume.side_effect = OSError(
        "filesystem handler did not acknowledge ACTION_FLUSH")
    with pytest.raises(fs.FuseOSError) as caught:
        ops.fsyncdir("/dir", 0, 1)
    assert caught.value.errno == errno.EIO


def test_fsyncdir_after_a_crash_is_eio(monkeypatch):
    ops, bridge = _make_fs(crashed=True)
    # A crash schedules the mount's shutdown; keep it from starting here.
    monkeypatch.setattr(ops, "_schedule_crash_shutdown", lambda: None)
    with pytest.raises(fs.FuseOSError) as caught:
        ops.fsyncdir("/dir", 0, 1)
    assert caught.value.errno == errno.EIO
    bridge.flush_volume.assert_not_called()
