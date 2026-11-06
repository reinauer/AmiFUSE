"""Windows metadata callbacks commit before reporting success."""

import errno
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse.fuse_fs import AmigaFuseFS, FuseOSError


@pytest.mark.parametrize("operation,args", [
    ("mkdir", ("/dir", 0o755)), ("create", ("/file", 0o644)),
    ("unlink", ("/file",)), ("rmdir", ("/dir",)),
    ("rename", ("/old", "/new")), ("utimens", ("/file", (0, 0))),
])
@pytest.mark.parametrize("windows", [False, True])
def test_metadata_acknowledgement_order(monkeypatch, operation, args, windows):
    order = []
    bridge = SimpleNamespace(_write_enabled=True,
        flush_volume=lambda: order.append("flush"))
    fs = AmigaFuseFS(bridge)
    fs._winfsp = windows
    monkeypatch.setattr(fs, operation, lambda *args: order.append(operation))
    fs(operation, *args)
    assert order == [operation] + (["flush"] if windows else [])


def test_flush_failure_reaches_metadata_caller(monkeypatch):
    bridge = SimpleNamespace(_write_enabled=True,
        flush_volume=Mock(side_effect=OSError("flush failed")))
    fs = AmigaFuseFS(bridge)
    fs._winfsp = True
    monkeypatch.setattr(fs, "mkdir", lambda *args: 0)
    with pytest.raises(FuseOSError) as caught:
        fs("mkdir", "/dir", 0o755)
    assert caught.value.errno == errno.EIO
