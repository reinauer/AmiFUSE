"""Unit tests for the retries of a refused or deferred rmdir().

- An inhibit cycle with a file open breaks SmartFilesystem's handles and
  can stop pfs3aio (#129). rmdir()'s retry after ERROR_OBJECT_IN_USE
  (202), releasedir() and the release() of a pending child send one only
  while no file is open, and take their locks after it. A 202 that only
  the cycle could clear fails with EBUSY while a file is open.
- rmdir() defers only while the directory lists nothing but pending
  deletes; any other entry stays, so the directory is not empty (#168).
- A deferred delete refused at release() or releasedir() keeps the
  directory hidden only while another retry is coming (#168, #197).
- A deferred file delete refused at release(), with no handle left to
  send it again, shows the file again with the date utimens() held for
  it, and a deferred rmdir() of its directory waits only on its other
  pending children. rmdir() defers only behind a pending file that a
  handle still holds (#222).

The tests drive the real callbacks over small in-memory volumes.
"""

import errno

import pytest

from tests.unit.test_rmdir_object_in_use import (
    ERROR_DIRECTORY_NOT_EMPTY,
    ERROR_OBJECT_IN_USE,
    _errno_of,
    _hold,
    _InUseVolume,
    _parent,
    _visible,
)

ERROR_DEVICE_NOT_MOUNTED = 218


class _FfsVolume(_InUseVolume):
    """Refuses an open file itself (202), but a directory that holds one is
    only not empty (216), as on FastFileSystem."""

    def _delete(self, path):
        if path in self.dirs and path not in self.busy:
            self.deletes.append(path)
            if any(_parent(p) == path for p in self.dirs | self.files):
                return 0, ERROR_DIRECTORY_NOT_EMPTY
            self.dirs.discard(path)
            self.deleted.append(path)
            return -1, 0
        return super()._delete(path)


class _SfsVolume(_InUseVolume):
    """Answers a lock taken before an inhibit cycle with 218, as
    SmartFilesystem does."""

    def __init__(self, dirs, files):
        super().__init__(dirs, files)
        self._epoch = 0
        self._lock_epochs = {}

    def locate_path(self, path):
        lock, info, locks = super().locate_path(path)
        if lock:
            self._lock_epochs[lock] = self._epoch
        return lock, info, locks

    def inhibit_cycle(self):
        super().inhibit_cycle()
        self._epoch += 1

    def delete_object(self, lock, name):
        if self._lock_epochs[lock] != self._epoch:
            return 0, ERROR_DEVICE_NOT_MOUNTED
        return super().delete_object(lock, name)


class _StaleStateVolume(_InUseVolume):
    """Refuses a directory in ``busy`` until an inhibit cycle, as pfs3aio
    and SmartFilesystem do after a directory change with nothing open."""

    def inhibit_cycle(self):
        super().inhibit_cycle()
        self.busy.clear()


@pytest.fixture
def make_fs(fuse_mock):
    from amifuse.fuse_fs import AmigaFuseFS

    def make(volume_cls, dirs, files):
        volume = volume_cls(dirs, files)
        fs = AmigaFuseFS(volume, debug=False, icons=False)
        return fs, volume

    return make


# An inhibit cycle only while no file is open (#129)


def test_in_use_rmdir_sends_no_inhibit_cycle_while_a_file_is_open(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d", "/a"}, files={"/a/y"})
    volume.busy.add("/d")
    _hold(fs, volume, "/a/y")

    assert _errno_of(fs.rmdir, "/d") == errno.EBUSY

    assert volume.inhibit_cycles == []
    assert volume.deletes == ["/d"]
    assert _visible(fs, "/d")


def test_releasedir_sends_no_inhibit_cycle_while_a_file_is_open(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d", "/a"}, files={"/a/y"})
    volume.busy.add("/d")
    dh = fs.opendir("/d")
    assert fs.rmdir("/d") == 0  # cycles with nothing open, deferred
    cycles = len(volume.inhibit_cycles)
    _hold(fs, volume, "/a/y")
    volume.busy.clear()

    fs.releasedir("/d", dh)

    assert volume.inhibit_cycles[cycles:] == []
    assert volume.deleted == ["/d"]
    assert fs._pending_deletes == set()


def test_in_use_rmdir_with_a_file_open_elsewhere_is_busy_though_the_directory_is_open(make_fs):
    # Only the cycle clears this refusal, and the file open rules it out at
    # releasedir() too: deferring would report a delete that never happens.
    fs, volume = make_fs(_StaleStateVolume, dirs={"/", "/d", "/a"}, files={"/a/y"})
    volume.busy.add("/d")
    _hold(fs, volume, "/a/y")
    dh = fs.opendir("/d")

    assert _errno_of(fs.rmdir, "/d") == errno.EBUSY
    fs.releasedir("/d", dh)

    assert volume.inhibit_cycles == []
    assert volume.deletes == ["/d"]
    assert fs._pending_deletes == set()
    assert _visible(fs, "/d")


def test_in_use_rmdir_still_cycles_and_retries_with_no_file_open(make_fs):
    # The cycle clears the in-use state that refuses an Explorer folder
    # delete on pfs3aio and SmartFilesystem.
    fs, volume = make_fs(_StaleStateVolume, dirs={"/", "/d"}, files=set())
    volume.busy.add("/d")

    assert fs.rmdir("/d") == 0

    assert volume.inhibit_cycles == [[]]
    assert volume.deleted == ["/d"]


def test_releasedir_still_cycles_with_no_file_open(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files=set())
    volume.busy.add("/d")
    dh = fs.opendir("/d")
    assert fs.rmdir("/d") == 0
    volume.busy.clear()

    fs.releasedir("/d", dh)

    assert volume.inhibit_cycles == [[], []]
    assert volume.deleted == ["/d"]
    assert fs._pending_deletes == set()


def test_deferred_rmdir_at_release_takes_the_parent_lock_after_the_cycle(make_fs):
    fs, volume = make_fs(_SfsVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    assert fs.rmdir("/d") == 0

    fs.release("/d/x", hx)

    assert volume.inhibit_cycles == [[]]
    assert volume.deleted == ["/d/x", "/d"]
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}


# Defer only while the directory lists nothing but pending deletes (#168)


@pytest.mark.parametrize("volume_cls", [_FfsVolume, _InUseVolume], ids=["216", "202"])
@pytest.mark.parametrize("live", [("/g/w", "file"), ("/g/s", "dir")], ids=["file", "dir"])
def test_rmdir_of_a_directory_with_a_live_entry_is_not_empty(make_fs, volume_cls, live):
    live_path, kind = live
    dirs = {"/", "/g"} | ({live_path} if kind == "dir" else set())
    files = {"/g/z"} | ({live_path} if kind == "file" else set())
    fs, volume = make_fs(volume_cls, dirs=dirs, files=files)
    hz = _hold(fs, volume, "/g/z")
    assert fs.unlink("/g/z") == 0

    assert _errno_of(fs.rmdir, "/g") == errno.ENOTEMPTY
    assert fs._pending_deletes == {"/g/z"}
    assert fs._pending_dir_deletes == {}

    fs.release("/g/z", hz)
    assert volume.deleted == ["/g/z"]
    assert _visible(fs, "/g")
    assert _visible(fs, live_path)


class _ShortNameVolume(_FfsVolume):
    """Lists names cut to 30 characters, as FastFileSystem stores them."""

    def list_dir_path(self, path):
        return [{"name": e["name"][:30]} for e in super().list_dir_path(path)]


def test_pending_entry_the_handler_lists_under_another_name_still_defers(make_fs):
    long_name = "/g/a_rather_long_file_name_past_thirty_chars.txt"
    fs, volume = make_fs(_ShortNameVolume, dirs={"/", "/g"}, files={long_name})
    h = _hold(fs, volume, long_name)
    assert fs.unlink(long_name) == 0

    assert fs.rmdir("/g") == 0
    fs.release(long_name, h)

    assert volume.deleted == [long_name, "/g"]
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}


# A refused deferred delete hides the directory only while a retry is
# coming (#168, #197)


def test_deferred_rmdir_refused_at_release_shows_the_directory(make_fs):
    fs, volume = make_fs(_FfsVolume, dirs={"/", "/g"}, files={"/g/z"})
    hz = _hold(fs, volume, "/g/z")
    assert fs.unlink("/g/z") == 0
    assert fs.rmdir("/g") == 0
    volume.files.add("/g/n")  # made after the rmdir()

    fs.release("/g/z", hz)

    assert volume.deleted == ["/g/z"]
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}
    assert _visible(fs, "/g")


def test_refused_deferred_rmdir_shows_the_parent_waiting_on_it(make_fs):
    fs, volume = make_fs(_FfsVolume, dirs={"/", "/g", "/g/d"}, files={"/g/d/x"})
    hx = _hold(fs, volume, "/g/d/x")
    assert fs.unlink("/g/d/x") == 0
    assert fs.rmdir("/g/d") == 0
    assert fs.rmdir("/g") == 0
    volume.files.add("/g/d/n")

    fs.release("/g/d/x", hx)

    assert volume.deleted == ["/g/d/x"]
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}
    assert _visible(fs, "/g")
    assert _visible(fs, "/g/d")


def test_refused_deferred_rmdir_waits_for_an_open_directory_handle(make_fs):
    fs, volume = make_fs(_FfsVolume, dirs={"/", "/g"}, files={"/g/z"})
    hz = _hold(fs, volume, "/g/z")
    assert fs.unlink("/g/z") == 0
    dh = fs.opendir("/g")
    assert fs.rmdir("/g") == 0
    volume.files.add("/g/n")

    fs.release("/g/z", hz)
    assert not _visible(fs, "/g")  # releasedir() will send the delete again

    fs.releasedir("/g", dh)
    assert volume.deletes.count("/g") == 3
    assert fs._pending_deletes == set()
    assert _visible(fs, "/g")


def test_releasedir_refusal_with_no_retry_coming_shows_the_directory(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files=set())
    volume.busy.add("/d")
    dh = fs.opendir("/d")
    assert fs.rmdir("/d") == 0
    assert not _visible(fs, "/d")

    fs.releasedir("/d", dh)

    assert volume.deleted == []
    assert fs._pending_deletes == set()
    assert _visible(fs, "/d")


def test_releasedir_exception_with_no_retry_coming_shows_the_directory(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files=set())
    volume.busy.add("/d")
    dh = fs.opendir("/d")
    assert fs.rmdir("/d") == 0

    def fail(path):
        raise OSError("handler did not answer")

    volume.delete_dir_atomic = fail
    fs.releasedir("/d", dh)

    assert fs._pending_deletes == set()
    assert _visible(fs, "/d")


def test_releasedir_refusal_waits_for_the_release_of_a_pending_child(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    dh = fs.opendir("/d")
    assert fs.rmdir("/d") == 0

    fs.releasedir("/d", dh)
    assert volume.deleted == []
    assert not _visible(fs, "/d")  # the release of /d/x sends it again

    fs.release("/d/x", hx)
    assert volume.deleted == ["/d/x", "/d"]
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}


# A refused deferred file delete shows the file again; rmdir() defers only
# behind a pending file that a handle still holds (#222)


def _hold_alias(fs, volume, path, alias):
    """A second handle on path's file, opened under another letter case."""
    handle = _hold(fs, volume, path)
    fs._fh_cache[handle]["path"] = alias
    return handle


def test_deferred_delete_refused_at_release_shows_the_file(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    h1 = _hold(fs, volume, "/d/x")
    h2 = _hold_alias(fs, volume, "/d/x", "/d/X")
    assert fs.unlink("/d/x") == 0

    fs.release("/d/x", h1)  # the handle under /d/X keeps it in use

    assert volume.deletes == ["/d/x", "/d/x"]
    assert volume.deleted == []
    assert fs._pending_deletes == set()
    assert _visible(fs, "/d/x")

    fs.release("/d/X", h2)
    assert _errno_of(fs.rmdir, "/d") == errno.ENOTEMPTY
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}
    assert _visible(fs, "/d")
    assert "/d/x" in volume.files


def test_deferred_delete_refused_at_release_shows_the_parents_waiting_on_it(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/g", "/g/d"}, files={"/g/d/x"})
    h1 = _hold(fs, volume, "/g/d/x")
    h2 = _hold_alias(fs, volume, "/g/d/x", "/g/d/X")
    assert fs.unlink("/g/d/x") == 0
    assert fs.rmdir("/g/d") == 0
    assert fs.rmdir("/g") == 0

    fs.release("/g/d/x", h1)
    fs.release("/g/d/X", h2)

    assert volume.deleted == []
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}
    assert _visible(fs, "/g/d/x")
    assert _visible(fs, "/g/d")
    assert _visible(fs, "/g")


def test_deferred_delete_with_no_reply_at_release_stays_hidden(make_fs):
    # The handler may still hold the packet, and the delete still happen.
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    volume.delete_object = lambda lock, name, **kwargs: (0, -1)

    fs.release("/d/x", hx)

    assert fs._pending_deletes == {"/d/x"}
    assert not _visible(fs, "/d/x")


def test_rmdir_behind_a_pending_file_no_handle_holds_is_not_empty(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    no_reply = lambda lock, name, **kwargs: (0, -1)
    volume.delete_object = no_reply
    fs.release("/d/x", hx)  # no reply: /d/x stays pending, with no handle
    assert fs._pending_deletes == {"/d/x"}

    assert _errno_of(fs.rmdir, "/d") == errno.ENOTEMPTY

    assert fs._pending_deletes == {"/d/x"}
    assert fs._pending_dir_deletes == {}
    assert _visible(fs, "/d")


def test_refused_deferred_delete_waits_for_a_handle_a_failed_truncate_closed(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    h1 = _hold(fs, volume, "/d/x")
    h2 = _hold(fs, volume, "/d/x")
    volume.close_file(fs._fh_cache[h2]["fh_addr"])
    fs._fh_cache[h2].update(fh_addr=0, closed=True)  # truncate() failed to reopen
    h3 = _hold_alias(fs, volume, "/d/x", "/d/X")
    assert fs.unlink("/d/x") == 0

    fs.release("/d/x", h1)  # refused: the handle under /d/X keeps it in use
    assert fs._pending_deletes == {"/d/x"}
    assert not _visible(fs, "/d/x")

    fs.release("/d/X", h3)
    fs.release("/d/x", h2)  # sends the delete again

    assert volume.deleted == ["/d/x"]
    assert fs._pending_deletes == set()


def test_deferred_delete_whose_directory_lookup_gets_no_reply_shows_the_file(make_fs):
    # The delete was never sent, so nothing can still delete the file.
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    volume.locate_path = lambda path, **kwargs: (0, -1, [])

    fs.release("/d/x", hx)

    del volume.locate_path
    assert volume.deletes == ["/d/x"]
    assert fs._pending_deletes == set()
    assert _visible(fs, "/d/x")


class _CaseFoldVolume(_InUseVolume):
    """Matches names in any letter case, as the handlers do."""

    def _delete(self, path):
        return super()._delete(path.lower())


def test_deferred_rmdir_behind_both_spellings_runs_after_the_second_release(make_fs):
    fs, volume = make_fs(_CaseFoldVolume, dirs={"/", "/d"}, files={"/d/x"})
    h1 = _hold(fs, volume, "/d/x")
    h2 = _hold_alias(fs, volume, "/d/x", "/d/X")
    assert fs.unlink("/d/x") == 0
    assert fs.unlink("/d/X") == 0
    assert fs.rmdir("/d") == 0

    fs.release("/d/x", h1)  # refused: the handle under /d/X keeps it in use
    assert volume.deleted == []
    assert not _visible(fs, "/d")  # the release of /d/X deletes the file

    fs.release("/d/X", h2)

    assert volume.deleted == ["/d/x", "/d"]
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}


def _record_dates(fs):
    dates = []
    fs._set_date = lambda path, when, **kwargs: dates.append((path, when))
    return dates


def test_refused_deferred_delete_sets_the_date_utimens_held(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    dates = _record_dates(fs)
    h1 = _hold(fs, volume, "/d/x")
    h2 = _hold_alias(fs, volume, "/d/x", "/d/X")
    fs._held_dates["/d/x"] = 1000.0  # utimens() while the file was open
    assert fs.unlink("/d/x") == 0

    fs.release("/d/x", h1)

    assert dates == [("/d/x", 1000.0)]
    assert fs._held_dates == {}


@pytest.mark.parametrize("answer", [(-1, 0), (0, -1)], ids=["deleted", "no-reply"])
def test_deferred_delete_drops_the_date_utimens_held(make_fs, answer):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    dates = _record_dates(fs)
    hx = _hold(fs, volume, "/d/x")
    fs._held_dates["/d/x"] = 1000.0
    assert fs.unlink("/d/x") == 0
    volume.delete_object = lambda lock, name, **kwargs: answer

    fs.release("/d/x", hx)

    assert dates == []
    assert fs._held_dates == {}


# An exception in the deferred delete at release() (#328). What the delete
# settles does not depend on the locks that are freed after it.


class _Freed:
    """Records each free_lock(); the first `raising` of them raise."""

    def __init__(self, volume, raising=0, raise_on=()):
        self.locks = []
        self.raising = raising
        self.raise_on = set(raise_on)
        volume.free_lock = self

    def __call__(self, lock):
        self.locks.append(lock)
        if lock in self.raise_on:
            raise OSError("free_lock failed")
        if self.raising:
            self.raising -= 1
            raise OSError("free_lock failed")


def test_free_lock_raising_after_the_delete_leaves_the_path_gone(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    fs._stat_cache["/d/x"] = (0.0, {"st_size": 1})
    fs._dir_cache["/d"] = (0.0, ["x"])
    _Freed(volume, raising=1)

    assert fs.release("/d/x", hx) == 0

    assert volume.deleted == ["/d/x"]
    assert fs._pending_deletes == set()
    assert "/d/x" not in fs._stat_cache
    assert "/d" not in fs._dir_cache


def test_free_lock_raising_after_the_delete_still_settles_the_parent(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    assert fs.rmdir("/d") == 0
    _Freed(volume, raising=1)

    fs.release("/d/x", hx)

    assert volume.deleted == ["/d/x", "/d"]
    assert fs._pending_deletes == set()
    assert fs._pending_dir_deletes == {}


def test_free_lock_raising_after_a_refusal_leaves_the_file_shown(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    dates = _record_dates(fs)
    h1 = _hold(fs, volume, "/d/x")
    h2 = _hold_alias(fs, volume, "/d/x", "/d/X")
    fs._held_dates["/d/x"] = 1000.0
    assert fs.unlink("/d/x") == 0
    _Freed(volume, raising=1)

    fs.release("/d/x", h1)  # refused: the handle under /d/X keeps it in use

    assert h2 in fs._fh_cache
    assert fs._pending_deletes == set()
    assert _visible(fs, "/d/x")
    assert dates == [("/d/x", 1000.0)]


def test_delete_object_raising_frees_every_lock_and_keeps_the_path_hidden(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    volume.locate_path = lambda path: (0x201, None, [0x200, 0x201])
    freed = _Freed(volume)

    def raises(lock, name):
        raise OSError("delete failed")

    volume.delete_object = raises

    assert fs.release("/d/x", hx) == 0

    assert freed.locks == [0x201, 0x200]
    assert fs._pending_deletes == {"/d/x"}  # no answer: it may yet be deleted


def test_each_lock_is_freed_when_one_free_raises(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    volume.locate_path = lambda path: (0x201, None, [0x200, 0x201])
    volume.delete_object = lambda lock, name: (-1, 0)
    freed = _Freed(volume, raising=1)

    assert fs.release("/d/x", hx) == 0

    assert freed.locks == [0x201, 0x200]


# An exception in the deletes of unlink() and of a deferred rmdir() (#363).


def _delete_raises(lock, name):
    raise OSError("delete_object failed")


@pytest.mark.parametrize("raising", [0, 1], ids=["delete", "delete+free"])
def test_unlink_with_delete_object_raising_frees_every_lock(make_fs, raising):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    volume.locate_path = lambda path: (0x201, None, [0x200, 0x201])
    volume.delete_object = _delete_raises
    freed = _Freed(volume, raising=raising)

    with pytest.raises(OSError, match="delete_object failed"):
        fs.unlink("/d/x")

    assert freed.locks == [0x201, 0x200]


def _unlink_with(volume, answer):
    volume.locate_path = lambda path: (0x201, None, [0x200, 0x201])
    volume.delete_object = lambda lock, name: answer


def test_unlink_deleted_frees_each_lock_when_one_free_raises(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    _unlink_with(volume, (-1, 0))
    freed = _Freed(volume, raising=1)

    assert fs.unlink("/d/x") == 0

    assert freed.locks == [0x201, 0x200]


def test_unlink_refused_frees_each_lock_when_one_free_raises(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    _unlink_with(volume, (0, 218))
    freed = _Freed(volume, raising=1)

    assert _errno_of(fs.unlink, "/d/x") == errno.EIO

    assert freed.locks == [0x201, 0x200]


def test_unlink_deferred_by_an_open_handle_frees_its_locks(make_fs):
    fs, volume = make_fs(_InUseVolume, dirs={"/", "/d"}, files={"/d/x"})
    _hold(fs, volume, "/d/x")
    _unlink_with(volume, (0, ERROR_OBJECT_IN_USE))
    freed = _Freed(volume)

    assert fs.unlink("/d/x") == 0

    assert freed.locks == [0x201, 0x200]
    assert "/d/x" in fs._pending_deletes


def _deferred_rmdir(make_fs):
    """/d is a deferred rmdir() behind /d/x; release() of /d/x is next."""
    fs, volume = make_fs(_FfsVolume, dirs={"/", "/d"}, files={"/d/x"})
    hx = _hold(fs, volume, "/d/x")
    assert fs.unlink("/d/x") == 0
    assert fs.rmdir("/d") == 0
    assert "/d" in fs._pending_dir_deletes
    return fs, volume, hx


def _answer_rmdir(volume, answer):
    """Let the delete of /d/x through, with two locks for the one of /d."""
    delete, locate = volume.delete_object, volume.locate_path

    def locate_two(path):
        if path == "/" and "/d/x" not in volume.files:
            return 0x201, None, [0x200, 0x201]
        return locate(path)

    volume.locate_path = locate_two
    volume.delete_object = (
        lambda lock, name: answer(lock, name) if name == "d"
        else delete(lock, name))


@pytest.mark.parametrize("answer", [
    _delete_raises,
    lambda lock, name: (-1, 0),
    lambda lock, name: (0, ERROR_DIRECTORY_NOT_EMPTY),
], ids=["raises", "deleted", "refused"])
def test_deferred_rmdir_frees_each_lock_when_one_free_raises(make_fs, answer):
    fs, volume, hx = _deferred_rmdir(make_fs)
    _answer_rmdir(volume, answer)
    freed = _Freed(volume, raise_on={0x201})

    assert fs.release("/d/x", hx) == 0

    assert freed.locks[-2:] == [0x201, 0x200]
    assert "/d" not in fs._pending_dir_deletes


def test_deferred_rmdir_deleted_stays_settled_when_a_free_raises(make_fs):
    fs, volume, hx = _deferred_rmdir(make_fs)
    _answer_rmdir(volume, lambda lock, name: (-1, 0))
    _Freed(volume, raise_on={0x201})
    fs._dir_cache["/"] = (0.0, ["d"])

    assert fs.release("/d/x", hx) == 0

    assert "/d" not in fs._pending_deletes
    assert "/" not in fs._dir_cache
