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


@pytest.mark.parametrize("release_fails", [False, True])
def test_failed_create_flush_releases_unpublished_handle(monkeypatch, release_fails):
    order = []

    def failed_flush():
        order.append("flush")
        raise OSError("flush failed")

    bridge = SimpleNamespace(_write_enabled=True, flush_volume=failed_flush)
    fs = AmigaFuseFS(bridge)
    fs._winfsp = True

    def create(path, mode):
        order.append("create")
        fs._fh_cache[42] = {"path": path}
        return 42

    def release(path, handle):
        assert (path, handle) == ("/new", 42)
        order.append("release")
        fs._fh_cache.pop(handle)
        if release_fails:
            raise RuntimeError("cleanup failed")

    monkeypatch.setattr(fs, "create", create)
    monkeypatch.setattr(fs, "release", release)
    with pytest.raises(FuseOSError) as caught:
        fs("create", "/new", 0o644)
    assert caught.value.errno == errno.EIO
    assert isinstance(caught.value.__cause__, OSError)
    assert order == ["create", "flush", "release"]
    assert not fs._fh_cache
