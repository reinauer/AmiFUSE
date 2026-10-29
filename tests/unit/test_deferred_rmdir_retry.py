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

The tests drive the real callbacks over small in-memory volumes.
"""

import errno

import pytest

from tests.unit.test_rmdir_object_in_use import (
    ERROR_DIRECTORY_NOT_EMPTY,
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
