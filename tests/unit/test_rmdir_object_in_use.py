"""Unit tests for rmdir() of a directory the handler reports in use.

pfs3aio answers a directory delete with ERROR_OBJECT_IN_USE (202) while a
file inside it is open, even one already unlinked. rmdir() defers such a
delete only where a retry will come: the release() of the last pending
child, or the releasedir() of a FUSE directory handle on the path. A path
counts as pending only while its delete is: a successful rmdir(), mkdir()
or create() on it makes it visible again.

The tests drive the real callbacks over a small in-memory volume that
answers like pfs3aio.
"""

import errno
import threading
import types

import pytest

ERROR_OBJECT_IN_USE = 202
ERROR_OBJECT_EXISTS = 203
ERROR_DIRECTORY_NOT_EMPTY = 216


def _parent(path):
    return path.rsplit("/", 1)[0] or "/"


class _InUseVolume:
    """Bridge over a tree of paths that refuses to delete open objects.

    A directory with an open file anywhere below it, or a path in
    ``busy``, is in use (202), as on pfs3aio.
    """

    def __init__(self, dirs, files):
        self._write_enabled = True
        self.state = types.SimpleNamespace(crashed=False)
        self.dirs = set(dirs)
        self.files = set(files)
        self.busy = set()
        self.open_files = {}  # fh_addr -> path
        self.deletes = []  # every path a delete was sent for
        self.deleted = []  # paths a delete removed
        self.inhibit_cycles = []  # paths open at each inhibit cycle
        self._lock_paths = {}
        self._next_fh_addr = 0x1000

    def open(self, path):
        fh_addr = self._next_fh_addr
        self._next_fh_addr += 0x10
        self.open_files[fh_addr] = path
        return fh_addr

    def open_file(self, path, flags):
        if _parent(path) not in self.dirs:
            return None
        self.files.add(path)
        return self.open(path), 0

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
        self.inhibit_cycles.append(sorted(self.open_files.values()))

    def flush_volume(self):
        pass

    def create_dir(self, parent_lock, name):
        path = self._lock_paths[parent_lock].rstrip("/") + "/" + name
        if path in self.dirs or path in self.files:
            return 0, ERROR_OBJECT_EXISTS
        self.dirs.add(path)
        return self.locate_path(path)[0], 0

    def list_dir_path(self, path):
        return [
            {"name": p.rsplit("/", 1)[1]}
            for p in sorted(self.dirs | self.files)
            if p != "/" and _parent(p) == path
        ]

    def stat_path(self, path):
        if path in self.dirs:
            return {"dir_type": 2, "size": 0, "protection": 0, "num_blocks": 0}
        if path in self.files:
            return {"dir_type": -3, "size": 0, "protection": 0, "num_blocks": 0}
        return None

    def _delete(self, path):
        self.deletes.append(path)
        prefix = path.rstrip("/") + "/"
        if path in self.busy or any(
            p == path or p.startswith(prefix) for p in self.open_files.values()
        ):
            return 0, ERROR_OBJECT_IN_USE
        if path in self.dirs:
            if any(_parent(p) == path for p in self.dirs | self.files):
                return 0, ERROR_DIRECTORY_NOT_EMPTY
            self.dirs.discard(path)
        elif path in self.files:
            self.files.discard(path)
        else:
            return 0, 205  # ERROR_OBJECT_NOT_FOUND
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
        volume = _InUseVolume(dirs, files)
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


def _errno_of(call, *args):
    import amifuse.fuse_fs as fuse_fs_mod

    with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
        call(*args)
    return exc_info.value.args[0]


def _visible(fs, path):
    parent = _parent(path)
    name = path.rsplit("/", 1)[1]
    try:
        fs.getattr(path)
    except Exception:
        return False
    return fs._filter_pending_deletes(parent, [name]) == [name]


def _defer_d_behind_open_unlinked_x(fs, volume):
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    assert fs.rmdir("/d") == 0
    assert volume.deleted == []
    assert not _visible(fs, "/d")
    return hx


def test_in_use_rmdir_is_resent_at_release_of_the_open_child(make_fs):
    fs, volume = make_fs(dirs={"/", "/d"}, files={"/d/x"})
    hx = _defer_d_behind_open_unlinked_x(fs, volume)
    assert fs._pending_dir_deletes == {"/d": {"/d/x"}}

    fs.release("/d/x", hx)

    assert volume.deleted == ["/d/x", "/d"]
    assert fs._pending_dir_deletes == {}
    assert fs._pending_deletes == set()
    assert "/d" not in volume.dirs


def test_in_use_rmdir_under_a_deferred_parent_resolves_upwards(make_fs):
    fs, volume = make_fs(dirs={"/", "/g", "/g/d"}, files={"/g/d/x"})
    hx = _hold(fs, volume, "/g/d/x")
    assert fs.unlink("/g/d/x") == 0
    assert fs.rmdir("/g/d") == 0
    assert fs.rmdir("/g") == 0
    assert fs._pending_dir_deletes == {"/g/d": {"/g/d/x"}, "/g": {"/g/d"}}

    fs.release("/g/d/x", hx)

    assert volume.deleted == ["/g/d/x", "/g/d", "/g"]
    assert fs._pending_dir_deletes == {}
    assert fs._pending_deletes == set()


def test_deferred_rmdir_at_release_skips_the_inhibit_cycle_while_a_file_is_open(make_fs):
    # pfs3aio stops answering after an inhibit cycle with a file open
    # deeper in the tree, so the retry at release sends none then.
    fs, volume = make_fs(dirs={"/", "/d", "/a", "/a/b"}, files={"/d/x", "/a/b/y"})
    hx = _defer_d_behind_open_unlinked_x(fs, volume)
    _hold(fs, volume, "/a/b/y")
    cycles = len(volume.inhibit_cycles)

    fs.release("/d/x", hx)

    assert volume.inhibit_cycles[cycles:] == []
    assert volume.deleted == ["/d/x", "/d"]
    assert fs._pending_deletes == set()


def test_deferred_rmdir_at_release_cycles_with_no_file_open(make_fs):
    fs, volume = make_fs(dirs={"/", "/d"}, files={"/d/x"})
    hx = _defer_d_behind_open_unlinked_x(fs, volume)
    cycles = len(volume.inhibit_cycles)

    fs.release("/d/x", hx)

    assert volume.inhibit_cycles[cycles:] == [[]]
    assert volume.deleted == ["/d/x", "/d"]


def test_in_use_rmdir_of_a_directory_with_entries_and_no_retry_is_not_empty(make_fs):
    # /d/x is open but not unlinked: nothing would ever resend the delete,
    # so deferring it would only hide a directory that stays on disk.
    fs, volume = make_fs(dirs={"/", "/d"}, files={"/d/x"})
    _hold(fs, volume, "/d/x")

    assert _errno_of(fs.rmdir, "/d") == errno.ENOTEMPTY
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}
    assert _visible(fs, "/d")


def test_in_use_rmdir_of_an_empty_directory_with_no_retry_is_busy(make_fs):
    fs, volume = make_fs(dirs={"/", "/d"}, files=set())
    volume.busy.add("/d")

    assert _errno_of(fs.rmdir, "/d") == errno.EBUSY
    assert fs._pending_deletes == set()
    assert _visible(fs, "/d")


def test_in_use_rmdir_with_an_open_dir_handle_is_resent_at_releasedir(make_fs):
    fs, volume = make_fs(dirs={"/", "/d"}, files=set())
    volume.busy.add("/d")
    dh = fs.opendir("/d")

    assert fs.rmdir("/d") == 0
    assert fs._pending_deletes == {"/d"}
    assert fs._pending_dir_deletes == {}

    volume.busy.clear()
    fs.releasedir("/d", dh)

    assert volume.deleted == ["/d"]
    assert fs._pending_deletes == set()


def test_rmdir_that_succeeds_ends_a_failed_deferred_delete(make_fs):
    fs, volume = make_fs(dirs={"/", "/d"}, files={"/d/x"})
    hx = _defer_d_behind_open_unlinked_x(fs, volume)
    volume.busy.add("/d")  # the deferred delete at release fails
    fs.release("/d/x", hx)
    assert volume.deleted == ["/d/x"]
    assert "/d" in volume.dirs
    volume.busy.clear()

    assert fs.rmdir("/d") == 0

    assert volume.deleted == ["/d/x", "/d"]
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}


def test_mkdir_of_a_pending_path_makes_it_visible(make_fs):
    fs, volume = make_fs(dirs={"/"}, files=set())
    fs._pending_deletes.add("/d")
    fs._pending_dir_deletes["/d"] = {"/d/x"}

    assert fs.mkdir("/d", 0o755) == 0

    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}
    assert _visible(fs, "/d")


def test_create_of_a_pending_path_makes_it_visible(make_fs):
    fs, volume = make_fs(dirs={"/"}, files=set())
    fs._pending_deletes.add("/f")

    fh = fs.create("/f", 0o644)

    assert fs._pending_deletes == set()
    assert _visible(fs, "/f")
    fs.release("/f", fh)
    assert volume.deletes == []
    assert "/f" in volume.files


def test_create_over_an_unlinked_open_file_survives_the_old_release(make_fs):
    fs, volume = make_fs(dirs={"/"}, files={"/f"})
    old = _hold(fs, volume, "/f")
    assert fs.unlink("/f") == 0
    assert fs._pending_deletes == {"/f"}

    new = fs.create("/f", 0o644)
    fs.release("/f", old)
    fs.release("/f", new)

    assert "/f" in volume.files
    assert volume.deleted == []
    assert fs._pending_deletes == set()
