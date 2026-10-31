"""Unit tests for getattr() through a handle whose file has a pending delete.

A rename onto a file still open moves that file to a hidden temporary name
and defers its delete to release(). The handle goes on describing its file
until release, while a lookup by name does not find it (#244). A shared
handle on a file unlink() deferred does the same.
"""

import errno

import pytest

from tests.unit.test_rename_replace import DIR, _FakeVolume, _hold


class _SizedVolume(_FakeVolume):
    """_FakeVolume reporting each file's size, so old and new tell apart.

    Paths in exclusive are held on an exclusive lock, which the handler
    cannot examine.
    """

    def __init__(self, objects):
        super().__init__(objects)
        self.exclusive = set()

    def _fib(self, path):
        content = self.objects[path]
        is_dir = content is DIR
        return {"dir_type": 2 if is_dir else -3, "name": path.rsplit("/", 1)[1],
                "size": 0 if is_dir else len(content), "protection": 0,
                "num_blocks": 0}

    def stat_path(self, path):
        found = self._find(path)
        if found is None or found in self.exclusive:
            return None
        return self._fib(found)

    def list_dir_path(self, path):
        return [self._fib(p) for p in self.objects
                if p != "/" and (p.rsplit("/", 1)[0] or "/").lower() == path.lower()]


@pytest.fixture
def make_fs(fuse_mock):
    from amifuse.fuse_fs import AmigaFuseFS

    def make(objects):
        volume = _SizedVolume(objects)
        return AmigaFuseFS(volume, debug=False, icons=False), volume

    return make


def _enoent(call, *args):
    import amifuse.fuse_fs as fuse_fs_mod

    with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
        call(*args)
    return exc_info.value.args[0] == errno.ENOENT


def test_old_handle_describes_the_replaced_file(make_fs):
    fs, volume = make_fs({"/a": b"newer", "/b": b"old"})
    oldfh = _hold(fs, volume, "/b")

    assert fs.rename("/a", "/b") == 0

    assert fs.getattr("/b", oldfh)["st_size"] == len(b"old")
    assert fs.getattr("/b")["st_size"] == len(b"newer")


def test_staged_name_stays_hidden(make_fs):
    fs, volume = make_fs({"/a": b"newer", "/b": b"old"})
    oldfh = _hold(fs, volume, "/b")
    assert fs.rename("/a", "/b") == 0
    (temp,) = fs._pending_deletes

    fs.getattr("/b", oldfh)

    assert _enoent(fs.getattr, temp)
    assert fs.readdir("/", None) == [".", "..", "b"]


def test_release_completes_the_deferred_delete(make_fs):
    fs, volume = make_fs({"/a": b"newer", "/b": b"old"})
    oldfh = _hold(fs, volume, "/b")
    assert fs.rename("/a", "/b") == 0
    (temp,) = fs._pending_deletes
    fs.getattr("/b", oldfh)

    fs.release("/b", oldfh)

    assert volume.objects == {"/": DIR, "/b": b"newer"}
    assert fs._pending_deletes == set()
    assert _enoent(fs.getattr, temp)
    assert fs.getattr("/b")["st_size"] == len(b"newer")


def test_old_handle_on_an_exclusive_lock_keeps_its_pinned_entry(make_fs):
    """A file held on an exclusive lock, from create() or an O_TRUNC open(),
    has only its pinned entry: the handler cannot examine it."""
    fs, volume = make_fs({"/a": b"newer", "/b": b"old"})
    oldfh = _hold(fs, volume, "/b")
    volume.exclusive.add("/b")
    fs._pin_stat("/b", 1234)

    assert fs.rename("/a", "/b") == 0
    (temp,) = fs._pending_deletes
    volume.exclusive = {temp}

    assert fs.getattr("/b", oldfh)["st_size"] == 1234
    assert fs.getattr("/b")["st_size"] == len(b"newer")
    assert _enoent(fs.getattr, temp)

    volume.exclusive = set()
    fs.release("/b", oldfh)
    assert temp not in fs._pinned_stats and temp not in fs._stat_cache
    assert volume.objects == {"/": DIR, "/b": b"newer"}


def test_handle_on_an_unlinked_file_describes_it(make_fs):
    fs, volume = make_fs({"/f": b"data"})
    fh = _hold(fs, volume, "/f")

    assert fs.unlink("/f") == 0

    assert fs.getattr("/f", fh)["st_size"] == len(b"data")
    assert _enoent(fs.getattr, "/f")
    assert fs.readdir("/", None) == [".", ".."]
