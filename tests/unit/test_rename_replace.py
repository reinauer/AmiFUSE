"""Unit tests for rename() onto an existing name, and of an open file.

The real rename/unlink/release callbacks run over a small in-memory volume
that answers ACTION_RENAME_OBJECT as Amiga handlers do: ERROR_OBJECT_EXISTS
(203) for a name that is taken, also by the source itself in another case.
"""

import errno
import threading

import pytest

ERROR_OBJECT_IN_USE = 202
ERROR_OBJECT_EXISTS = 203
ERROR_OBJECT_NOT_FOUND = 205
ERROR_DIRECTORY_NOT_EMPTY = 216
ERROR_DISK_FULL = 221
DIR = None  # a directory's content in _FakeVolume.objects


def _parent(path):
    return path.rsplit("/", 1)[0] or "/"


def _join(dir_path, name):
    return dir_path.rstrip("/") + "/" + name


class _FakeVolume:
    """Bridge over a dict of paths, matching names case-insensitively.

    Locks name directory paths. refuse maps (src name, dst name) to the
    res2 of each rename between them in turn, None for one that goes ahead.
    """

    def __init__(self, objects):
        self._write_enabled = True
        self.state = None
        self.objects = dict(objects)
        self.objects.setdefault("/", DIR)
        self.open_files = {}  # fh_addr -> path
        self.renames = []
        self.refuse = {}
        self.locks = {}
        self.freed = []
        self._next_lock = 0x100

    def _find(self, path):
        return next((p for p in self.objects if p.lower() == path.lower()), None)

    def _lock(self, path):
        self._next_lock += 4
        self.locks[self._next_lock] = path
        return self._next_lock

    def locate_path(self, path):
        if path == "/":
            return 0, 0, []
        found = self._find(path)
        if found is None or self.objects[found] is not DIR:
            return 0, ERROR_OBJECT_NOT_FOUND, []
        lock = self._lock(found)
        return lock, 0, [lock]

    def locate(self, lock, name):
        assert lock == 0 and name == ""
        return self._lock("/"), 0

    def free_lock(self, lock):
        self.freed.append(lock)

    def _dir(self, lock):
        return self.locks[lock] if lock else "/"

    def stat_path(self, path):
        found = self._find(path)
        if found is None:
            return None
        is_dir = self.objects[found] is DIR
        return {"dir_type": 2 if is_dir else -3, "size": 0, "name": found, "protection": 0,
                "num_blocks": 0}

    def list_dir_path(self, path):
        return [{"name": p.rsplit("/", 1)[1]} for p in self.objects
                if p != "/" and _parent(p).lower() == path.lower()]

    def rename_object(self, src_lock, src_name, dst_lock, dst_name):
        src = self._find(_join(self._dir(src_lock), src_name))
        dst_path = _join(self._dir(dst_lock), dst_name)
        self.renames.append((src_name, dst_name))
        script = self.refuse.get((src_name, dst_name))
        res2 = script.pop(0) if script else None
        if res2 is not None:
            return 0, res2
        if src is None:
            return 0, ERROR_OBJECT_NOT_FOUND
        if self._find(dst_path) is not None:
            return 0, ERROR_OBJECT_EXISTS
        for p in [p for p in self.objects if p == src or p.startswith(src + "/")]:
            self.objects[dst_path + p[len(src):]] = self.objects.pop(p)
        for fh_addr, p in self.open_files.items():
            if p == src:
                self.open_files[fh_addr] = dst_path
        return -1, 0

    def close_file(self, fh_addr):
        self.open_files.pop(fh_addr)

    def _delete(self, path):
        found = self._find(path)
        if found is None:
            return 0, ERROR_OBJECT_NOT_FOUND
        if found in self.open_files.values():
            return 0, ERROR_OBJECT_IN_USE
        if self.list_dir_path(found):
            return 0, ERROR_DIRECTORY_NOT_EMPTY
        del self.objects[found]
        return -1, 0

    def delete_object(self, lock, name):
        return self._delete(_join(self._dir(lock), name))

    def delete_dir_atomic(self, path):
        return self._delete(path)


@pytest.fixture
def make_fs(fuse_mock):
    from amifuse.fuse_fs import AmigaFuseFS

    def make(objects):
        volume = _FakeVolume(objects)
        return AmigaFuseFS(volume, debug=False, icons=False), volume

    return make


def _hold(fs, volume, path, parent_lock=0):
    """Register an open read handle on path, as open() would."""
    handle = fs._next_fh
    fs._next_fh += 1
    volume.open_files[0x1000 + handle] = path
    fs._fh_cache[handle] = {
        "fh_addr": 0x1000 + handle,
        "parent_lock": parent_lock,
        "pos": None,
        "lock": threading.Lock(),
        "closed": False,
        "dirty": False,
        "path": path,
    }
    return handle


def _rename_errno(fs, old, new):
    import amifuse.fuse_fs as fuse_fs_mod

    with pytest.raises(fuse_fs_mod.FuseOSError) as exc_info:
        fs.rename(old, new)
    return exc_info.value.args[0]


def test_file_replaces_a_file(make_fs):
    fs, volume = make_fs({"/a": b"new", "/b": b"old"})

    assert fs.rename("/a", "/b") == 0

    assert volume.objects == {"/": DIR, "/b": b"new"}


def test_refused_rename_puts_the_target_back(make_fs):
    fs, volume = make_fs({"/a": b"new", "/b": b"old"})
    volume.refuse[("a", "b")] = [None, ERROR_DISK_FULL]

    assert _rename_errno(fs, "/a", "/b") == errno.ENOSPC

    assert volume.objects == {"/": DIR, "/a": b"new", "/b": b"old"}
    temp = volume.renames[1][1]
    assert volume.renames == [("a", "b"), ("b", temp), ("a", "b"), (temp, "b")]


def test_refused_delete_puts_both_back(make_fs):
    fs, volume = make_fs({"/a": b"new", "/b": b"old"})
    deletes = []

    def refuse_delete(lock, name):
        deletes.append(name)
        return 0, 222  # ERROR_DELETE_PROTECTED

    volume.delete_object = refuse_delete

    assert _rename_errno(fs, "/a", "/b") == errno.EIO

    assert volume.objects == {"/": DIR, "/a": b"new", "/b": b"old"}
    temp = volume.renames[1][1]
    assert deletes == [temp]
    assert volume.renames == [("a", "b"), ("b", temp), ("a", "b"), ("b", "a"), (temp, "b")]


def test_open_target_is_deleted_at_its_release(make_fs):
    fs, volume = make_fs({"/a": b"new", "/b": b"old"})
    fh = _hold(fs, volume, "/b")

    assert fs.rename("/a", "/b") == 0

    (temp,) = fs._pending_deletes
    assert fs._fh_cache[fh]["path"] == temp
    assert volume.objects == {"/": DIR, "/b": b"new", temp: b"old"}
    fs.release("/b", fh)
    assert volume.objects == {"/": DIR, "/b": b"new"}
    assert fs._pending_deletes == set()


def test_case_only_rename_refused_is_not_a_replace(make_fs):
    """A rename to the source's own name in another case must not delete
    the source as the replaced file."""
    fs, volume = make_fs({"/d": DIR, "/d/a": b"data"})

    assert _rename_errno(fs, "/d/a", "/D/A") == errno.EEXIST

    assert volume.renames == [("a", "A")]
    assert volume.objects["/d/a"] == b"data"


@pytest.mark.parametrize("res2, expected", [
    (ERROR_OBJECT_IN_USE, errno.EBUSY),
    (ERROR_OBJECT_NOT_FOUND, errno.ENOENT),
    (ERROR_DIRECTORY_NOT_EMPTY, errno.ENOTEMPTY),
    (ERROR_DISK_FULL, errno.ENOSPC),
])
def test_refusal_is_mapped_to_errno(make_fs, res2, expected):
    fs, volume = make_fs({"/a": b"data"})
    volume.refuse[("a", "c")] = [res2]

    assert _rename_errno(fs, "/a", "/c") == expected


@pytest.mark.parametrize("objects, expected", [
    ({"/s": b"", "/t": DIR}, errno.EISDIR),
    ({"/s": DIR, "/t": b""}, errno.ENOTDIR),
    ({"/s": DIR, "/t": DIR, "/t/f": b""}, errno.ENOTEMPTY),
], ids=["file-onto-dir", "dir-onto-file", "dir-onto-full-dir"])
def test_replace_needs_matching_kinds(make_fs, objects, expected):
    fs, volume = make_fs(objects)

    assert _rename_errno(fs, "/s", "/t") == expected

    assert volume.renames == [("s", "t")]


def test_directory_replaces_an_empty_directory(make_fs):
    fs, volume = make_fs({"/s": DIR, "/s/f": b"f", "/t": DIR})

    assert fs.rename("/s", "/t") == 0

    assert volume.objects == {"/": DIR, "/t": DIR, "/t/f": b"f"}


def test_open_file_moved_out_takes_its_directory_lock(make_fs):
    """The lock follows the file to each new directory, and stays when the
    file is renamed within one. A directory handle has none to move."""
    fs, volume = make_fs({"/d": DIR, "/d/x": b"x", "/e": DIR, "/m": DIR})
    d_lock = volume._lock("/d")
    fh = _hold(fs, volume, "/d/x", parent_lock=d_lock)
    entry = fs._fh_cache[fh]
    dh = fs.opendir("/m")

    assert fs.rename("/m", "/e/m") == 0
    assert fs._fh_cache[dh]["path"] == "/e/m"

    assert fs.rename("/d/x", "/d/y") == 0
    assert entry["parent_lock"] == d_lock and d_lock not in volume.freed

    assert fs.rename("/d/y", "/e/x") == 0
    e_lock = entry["parent_lock"]
    assert volume.locks[e_lock] == "/e"
    assert d_lock in volume.freed and e_lock not in volume.freed

    assert fs.rename("/e/x", "/x") == 0
    assert volume.locks[entry["parent_lock"]] == "/"
    assert e_lock in volume.freed


def test_closed_entry_keeps_its_lock_for_release(make_fs):
    """release() marks an entry closed and then frees its lock: a rename in
    between leaves the lock to it, and moves the open entry's."""
    fs, volume = make_fs({"/d": DIR, "/d/x": b"x"})
    closing_lock, open_lock = volume._lock("/d"), volume._lock("/d")
    closing = fs._fh_cache[_hold(fs, volume, "/d/x", parent_lock=closing_lock)]
    still_open = fs._fh_cache[_hold(fs, volume, "/d/x", parent_lock=open_lock)]
    closing["closed"] = True

    assert fs.rename("/d/x", "/x") == 0

    assert closing["parent_lock"] == closing_lock
    assert closing_lock not in volume.freed
    assert volume.locks[still_open["parent_lock"]] == "/"
    assert open_lock in volume.freed
