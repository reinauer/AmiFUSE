"""Host names holding a colon are refused at fusepy's dispatch (#242).

In AmigaDOS a colon ends a device or volume name, so `draft:notes.txt`
would reach the handler as `notes.txt` on a device called `draft`. fusepy
calls every callback as `operations(op, *args)`; there a lookup of such a
name gets ENOENT, and creating one (create, mkdir, a rename target) gets
EINVAL, without calling the callback.
"""

import errno

import pytest

from amifuse.fuse_fs import FuseOSError, _FuseOperations


class _Recorder(_FuseOperations):
    """Records the callbacks the dispatch reaches."""

    def __init__(self):
        self.calls = []

    def _record(self, op, *args):
        self.calls.append((op, args))
        return 0

    def getattr(self, path, fh=None):
        return self._record("getattr", path, fh)

    def open(self, path, flags):
        return self._record("open", path, flags)

    def read(self, path, size, offset, fh):
        return self._record("read", path, size, offset, fh)

    def readdir(self, path, fh):
        return self._record("readdir", path, fh)

    def unlink(self, path):
        return self._record("unlink", path)

    def rmdir(self, path):
        return self._record("rmdir", path)

    def truncate(self, path, length, fh=None):
        return self._record("truncate", path, length, fh)

    def getxattr(self, path, name, position=0):
        return self._record("getxattr", path, name, position)

    def create(self, path, mode, fi=None):
        return self._record("create", path, mode)

    def mkdir(self, path, mode):
        return self._record("mkdir", path, mode)

    def rename(self, old, new):
        return self._record("rename", old, new)


def _errno_of(ops, op, *args):
    with pytest.raises(FuseOSError) as exc_info:
        ops(op, *args)
    return exc_info.value.errno


@pytest.mark.parametrize("op, args", [
    ("getattr", ("/t/draft:notes.txt", None)),
    ("open", ("/t/draft:notes.txt", 0)),
    ("read", ("/t/draft:notes.txt", 10, 0, 1)),
    ("readdir", ("/draft:t", None)),
    ("unlink", ("/t/draft:notes.txt",)),
    ("rmdir", ("/draft:t",)),
    ("truncate", ("/a:/notes.txt", 0, None)),
    ("getxattr", ("/t/draft:notes.txt", "user.x")),
])
def test_lookup_of_a_colon_name_is_enoent_without_the_callback(op, args):
    ops = _Recorder()

    assert _errno_of(ops, op, *args) == errno.ENOENT
    assert ops.calls == []


@pytest.mark.parametrize("op, args", [
    ("create", ("/t/draft:notes.txt", 0o644)),
    ("mkdir", ("/t/draft:", 0o755)),
])
def test_creating_a_colon_name_is_einval_without_the_callback(op, args):
    ops = _Recorder()

    assert _errno_of(ops, op, *args) == errno.EINVAL
    assert ops.calls == []


def test_rename_checks_both_paths():
    ops = _Recorder()

    assert _errno_of(ops, "rename", "/t/draft:notes.txt", "/t/notes.txt") == errno.ENOENT
    assert _errno_of(ops, "rename", "/t/notes.txt", "/t/draft:notes.txt") == errno.EINVAL
    assert _errno_of(ops, "rename", "/a:b", "/c:d") == errno.ENOENT
    assert ops.calls == []

    assert ops("rename", "/t/notes.txt", "/t/draft notes.txt") == 0
    assert ops.calls == [("rename", ("/t/notes.txt", "/t/draft notes.txt"))]


def test_only_volume_paths_are_checked():
    ops = _Recorder()

    assert _errno_of(ops, "getattr", "/x:y", None) == errno.ENOENT
    # A colon outside the path argument, a NULL path and the root still pass.
    assert ops("getxattr", "/notes.txt", "user:x") == 0
    assert ops("read", None, 10, 0, 1) == 0
    assert ops("getattr", "/", None) == 0
    assert [call[0] for call in ops.calls] == ["getxattr", "read", "getattr"]
