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


@pytest.mark.parametrize("operation", ["release", "releasedir"])
def test_close_flush_failure_is_visible(monkeypatch, capsys, operation):
    bridge = SimpleNamespace(_write_enabled=True,
        flush_volume=Mock(side_effect=OSError("disk unavailable")))
    fs = AmigaFuseFS(bridge)
    fs._winfsp = True
    fs._fh_cache[42] = {"path": "/file", "dirty": True}
    monkeypatch.setattr(fs, operation, lambda *args: 0)
    with pytest.raises(FuseOSError) as caught:
        fs(operation, "/file", 42)
    assert caught.value.errno == errno.EIO
    message = capsys.readouterr().err
    assert operation in message and "/file" in message
    assert "disk unavailable" in message


@pytest.mark.parametrize("reason", ["dirty", "date", "delete", "clean"])
def test_real_release_commits_pending_changes(monkeypatch, reason):
    import threading

    order = []
    bridge = SimpleNamespace(_write_enabled=True,
        close_file=lambda address: order.append("close"),
        flush_volume=lambda: order.append("flush"),
        locate_path=lambda path: (1, 0, []),
        delete_object=lambda lock, name: (order.append("delete") or -1, 0))
    fs = AmigaFuseFS(bridge)
    fs._winfsp = True
    fs._fh_cache[42] = {"path": "/file", "fh_addr": 7,
        "dirty": reason == "dirty", "lock": threading.Lock()}
    if reason == "date":
        fs._held_dates["/file"] = 123
    if reason == "delete":
        fs._pending_deletes.add("/file")
    def date(path):
        if fs._held_dates.pop(path, None) is not None:
            order.append("date")
    monkeypatch.setattr(fs, "_set_held_date", date)
    assert fs("release", "/file", 42) == 0
    assert order == {"dirty": ["close", "flush"],
                     "date": ["close", "date", "flush"],
                     "delete": ["close", "delete", "flush"],
                     "clean": ["close"]}[reason]
    assert not fs._fh_cache and not fs._pending_deletes


def test_real_releasedir_commits_deferred_delete():
    order = []
    bridge = SimpleNamespace(_write_enabled=True,
        state=SimpleNamespace(crashed=False),
        flush_volume=lambda: order.append("flush"),
        inhibit_cycle=lambda: order.append("inhibit"),
        delete_dir_atomic=lambda path: (order.append("delete") or -1, 0))
    fs = AmigaFuseFS(bridge)
    fs._winfsp = True
    fs._fh_cache[42] = {"path": "/dir", "dir_handle": True}
    fs._pending_deletes.add("/dir")
    assert fs("releasedir", "/dir", 42) == 0
    assert order == ["flush", "inhibit", "delete", "flush"]
    assert not fs._fh_cache and not fs._pending_deletes


@pytest.mark.parametrize("operation", ["unlink", "rmdir", "utimens"])
def test_deferred_metadata_does_not_flush_early(monkeypatch, operation):
    bridge = SimpleNamespace(_write_enabled=True, flush_volume=Mock())
    fs = AmigaFuseFS(bridge)
    fs._winfsp = True
    def defer(path, *args):
        if operation == "utimens":
            fs._held_dates[path] = 123
        else:
            fs._pending_deletes.add(path)
        return 0
    monkeypatch.setattr(fs, operation, defer)
    args = ("/file", (0, 123)) if operation == "utimens" else ("/file",)
    assert fs(operation, *args) == 0
    bridge.flush_volume.assert_not_called()
