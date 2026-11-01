"""Volume paths the handler can't take as typed are refused (#257, #258).

The handler takes names in Latin-1, and in AmigaDOS a colon ends a device
or volume name. A character outside Latin-1 would reach it as "?" and a
colon would end the name early, so either names another file. fusepy's
dispatch refuses such a path (a lookup gets ENOENT, a new name EINVAL), and
the file commands (ls, verify, hash, read, write) refuse it with
INVALID_ARGUMENT before the handler starts.
"""

import errno
import json
from types import SimpleNamespace

import pytest

from amifuse import fuse_fs
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

    def unlink(self, path):
        return self._record("unlink", path)

    def rmdir(self, path):
        return self._record("rmdir", path)

    def truncate(self, path, length, fh=None):
        return self._record("truncate", path, length, fh)

    def utimens(self, path, times=None):
        return self._record("utimens", path, times)

    def chmod(self, path, mode):
        return self._record("chmod", path, mode)

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


# Each would reach the handler with "?" in place of a character: the euro
# sign, a curly quote, an en dash, a CJK character, a lone surrogate (an
# undecodable byte in a POSIX file name), and a decomposed accent ("e" and
# U+0301, as macOS can pass "café"), which is refused, not normalized.
NON_LATIN1 = ["/a€.txt", "/a’.txt", "/a–.txt", "/a中.txt",
              "/a\udce9.txt", "/café.txt"]


@pytest.mark.parametrize("path", NON_LATIN1)
def test_dispatch_refuses_a_name_outside_latin1(path):
    ops = _Recorder()

    assert _errno_of(ops, "getattr", path, None) == errno.ENOENT
    assert _errno_of(ops, "open", path, 0) == errno.ENOENT
    assert _errno_of(ops, "unlink", path) == errno.ENOENT
    assert _errno_of(ops, "rmdir", path) == errno.ENOENT
    assert _errno_of(ops, "truncate", path, 0, None) == errno.ENOENT
    assert _errno_of(ops, "utimens", path, None) == errno.ENOENT
    assert _errno_of(ops, "chmod", path, 0o644) == errno.ENOENT
    assert _errno_of(ops, "create", path, 0o644) == errno.EINVAL
    assert _errno_of(ops, "mkdir", path, 0o755) == errno.EINVAL
    assert _errno_of(ops, "rename", "/a?.txt", path) == errno.EINVAL
    assert _errno_of(ops, "rename", path, "/b.txt") == errno.ENOENT
    assert ops.calls == []


def test_dispatch_still_passes_latin1_names():
    ops = _Recorder()

    assert ops("getattr", "/café ÿ?.txt", None) == 0
    assert ops("create", "/café.txt", 0o644) == 0
    assert ops("rename", "/café.txt", "/naïve.txt") == 0
    assert [call[0] for call in ops.calls] == ["getattr", "create", "rename"]


# --- The file commands -------------------------------------------------------


@pytest.fixture
def no_handler(monkeypatch):
    """Fail the test if a command gets as far as starting the handler."""

    def started(*args, **kwargs):
        raise AssertionError("the handler was started")

    monkeypatch.setattr(fuse_fs, "resolve_driver", started)
    monkeypatch.setattr(fuse_fs, "HandlerBridge", started)


def _args(tmp_path, use_json, **extra):
    image = tmp_path / "disk.hdf"
    image.write_bytes(b"\0" * 1024)
    source = tmp_path / "source.txt"
    source.write_bytes(b"draft\n")
    values = dict(image=image, json=use_json, debug=False, driver=None,
                  partition=None, block_size=None)
    values.update(extra)
    return SimpleNamespace(**values)


def _commands(tmp_path, path):
    return {
        "ls": (fuse_fs.cmd_ls, dict(path=path, recursive=False)),
        "verify": (fuse_fs.cmd_verify, dict(file=path, expect_size=None)),
        "hash": (fuse_fs.cmd_hash, dict(file=path, algorithm="sha256")),
        "read": (fuse_fs.cmd_read, dict(file=path, out=str(tmp_path / "out.bin"))),
        "write": (fuse_fs.cmd_write, dict(file=path, input=str(tmp_path / "source.txt"))),
    }


COMMANDS = ["ls", "verify", "hash", "read", "write"]
BAD_PATHS = ["T/draft:notes.txt", "/T/draft:/notes.txt", "T/a€.txt",
             "T–dir/notes.txt"]


@pytest.mark.parametrize("path", BAD_PATHS)
@pytest.mark.parametrize("command", COMMANDS)
def test_file_command_refuses_the_path_as_json(no_handler, tmp_path, capsys,
                                               command, path):
    func, extra = _commands(tmp_path, path)[command]

    with pytest.raises(SystemExit) as exc_info:
        func(_args(tmp_path, True, **extra))

    assert exc_info.value.code == 1
    error = json.loads(capsys.readouterr().out)
    assert error["command"] == command
    assert error["error"]["code"] == "INVALID_ARGUMENT"
    assert path in error["error"]["message"]
    assert not (tmp_path / "out.bin").exists()


@pytest.mark.parametrize("path, reason", [
    ("T/draft:notes.txt", "colon"),
    ("T/a€.txt", "U+20AC"),
])
@pytest.mark.parametrize("command", COMMANDS)
def test_file_command_refuses_the_path_as_text(no_handler, tmp_path, command,
                                               path, reason):
    func, extra = _commands(tmp_path, path)[command]

    with pytest.raises(SystemExit) as exc_info:
        func(_args(tmp_path, False, **extra))

    message = str(exc_info.value.code)
    assert message.startswith("Error: ")
    assert path in message
    assert reason in message


def test_write_refuses_the_path_before_reading_the_source(no_handler, tmp_path,
                                                          capsys):
    args = _args(tmp_path, True, file="T/draft:notes.txt",
                 input=str(tmp_path / "missing.txt"))

    with pytest.raises(SystemExit):
        fuse_fs.cmd_write(args)

    out, err = capsys.readouterr()
    assert json.loads(out)["error"]["code"] == "INVALID_ARGUMENT"
    assert "WARNING" not in err
