"""Unit tests for rmdir() deferred behind pending child deletes.

In write mode, unlink() of an open file and rmdir() of a directory that
holds such files are deferred until the files are released. These tests
drive the real unlink/rmdir/release callbacks over a small in-memory
volume that answers like an Amiga handler: 202 for an open file, 216 for
a directory that still has entries.
"""

import errno
import threading

import pytest

ERROR_OBJECT_IN_USE = 202
ERROR_DIRECTORY_NOT_EMPTY = 216


def _parent(path):
    return path.rsplit("/", 1)[0] or "/"


class _FakeVolume:
    """Bridge over a tree of paths, with locks naming directory paths."""

    def __init__(self, dirs, files):
        self._write_enabled = True
        self.state = None
        self.dirs = set(dirs)
        self.files = set(files)
        self.open_files = {}  # fh_addr -> path
        self.deleted = []
        self._lock_paths = {}

    def open(self, path):
        fh_addr = 0x1000 + 0x10 * len(self.open_files)
        self.open_files[fh_addr] = path
        return fh_addr

    def close_file(self, fh_addr):
        self.open_files.pop(fh_addr)

    def locate_path(self, path):
        if path not in self.dirs:
            return 0, None, []
        lock = 0x100 + len(self._lock_paths)
        self._lock_paths[lock] = path
        return lock, None, [lock]

    def free_lock(self, lock):
        pass

    def inhibit_cycle(self):
        pass

    def list_dir_path(self, path):
        return [
            {"name": p.rsplit("/", 1)[1]}
            for p in sorted(self.dirs | self.files)
            if p != "/" and _parent(p) == path
        ]

    def _delete(self, path):
        if path in self.open_files.values():
            return 0, ERROR_OBJECT_IN_USE
        if path in self.dirs:
            if any(_parent(p) == path for p in self.dirs | self.files):
                return 0, ERROR_DIRECTORY_NOT_EMPTY
            self.dirs.discard(path)
        else:
            self.files.discard(path)
        self.deleted.append(path)
        return -1, 0

    def delete_object(self, lock, name):
        parent = self._lock_paths[lock]
        return self._delete(parent.rstrip("/") + "/" + name)

    def delete_dir_atomic(self, path):
        return self._delete(path)


@pytest.fixture
def make_fs(fuse_mock):
    from amifuse.fuse_fs import AmigaFuseFS

    def make(dirs, files):
        volume = _FakeVolume(dirs, files)
        fs = AmigaFuseFS(volume, debug=False, icons=False)
        return fs, volume

    return make


def _hold(fs, volume, path):
    """Register an open read handle on path, as open() would."""
    handle = fs._next_fh
    fs._next_fh += 1
    fs._fh_cache[handle] = {
        "fh_addr": volume.open(path),
        "parent_lock": 0,
        "pos": None,
        "lock": threading.Lock(),
        "closed": False,
        "dirty": False,
        "path": path,
    }
    return handle


def test_nested_deferred_rmdir_runs_after_last_release(make_fs):
    fs, volume = make_fs(
        dirs={"/", "/g", "/g/p", "/g/p/d"},
        files={"/g/p/d/x", "/g/p/z"},
    )
    hx = _hold(fs, volume, "/g/p/d/x")
    hz = _hold(fs, volume, "/g/p/z")

    assert fs.unlink("/g/p/d/x") == 0
    assert fs.unlink("/g/p/z") == 0
    assert fs.rmdir("/g/p/d") == 0
    assert fs.rmdir("/g/p") == 0
    assert volume.deleted == []

    fs.release("/g/p/d/x", hx)
    fs.release("/g/p/z", hz)

    assert volume.deleted == ["/g/p/d/x", "/g/p/d", "/g/p/z", "/g/p"]
    assert fs._pending_dir_deletes == {}
    assert fs._pending_deletes == set()


def test_rmdir_with_only_a_pending_grandchild_is_not_empty(make_fs):
    # /g/p/d is a live directory: deferring /g/p behind its pending
    # grandchild would hide /g/p while it can never be deleted.
    fs, volume = make_fs(dirs={"/", "/g", "/g/p", "/g/p/d"}, files={"/g/p/d/x"})
    _hold(fs, volume, "/g/p/d/x")
    assert fs.unlink("/g/p/d/x") == 0

    import amifuse.fuse_fs as fuse_fs_mod

    with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
        fs.rmdir("/g/p")
    assert exc_info.value.args[0] == errno.ENOTEMPTY
    assert fs._pending_dir_deletes == {}
    assert fs._pending_deletes == {"/g/p/d/x"}
