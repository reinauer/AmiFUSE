"""Names the handler can't take as typed, on real handlers (#257, #258).

A character outside Latin-1 reached the handler as "?", so creating
`a€.txt` emptied `a?.txt`; and `amifuse write --file T/draft:notes.txt`
emptied `T/notes.txt`, the file commands sending a colon path unchecked.
Through a mount such a name is now refused before any packet, and the file
commands refuse it before the handler starts.

The mount tests call the callbacks through `AmigaFuseFS.__call__`, as fusepy
does, on a DOS3 floppy made with xdftool for the bundled FastFileSystem. The
file command tests run on a copy of pfs.hdf with pfs3aio. No host FUSE driver
is needed.
"""

import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from amifuse import driver_resolver
from amifuse import fuse_fs
from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf

AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"

OLD = b"The contents of a?.txt\n"
NOTES = b"the original notes\n"


def _xdftool(image, *args):
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(image), *args],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout


def _errno_of(ops, op, *args):
    with pytest.raises(FuseOSError) as exc_info:
        ops(op, *args)
    return exc_info.value.errno


def test_a_name_outside_latin1_leaves_the_question_mark_file(tmp_path):
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")
    image = tmp_path / "disk.adf"
    old = tmp_path / "old.txt"
    old.write_bytes(OLD)
    _xdftool(image, "create", "+", "format", "Seed", "DOS3",
             "+", "write", str(old), "a?.txt", "+", "write", str(old), "cafe?.txt")

    bridge = HandlerBridge(image, driver_resolver.BUNDLED_DRIVER,
                           read_only=False, adf_info=detect_adf(image))
    ops = AmigaFuseFS(bridge)
    try:
        # The decomposed accent ("e" and U+0301, as macOS can pass "café")
        # would reach the handler as "cafe?.txt".
        for name in ("/a€.txt", "/a™.txt", "/café.txt"):
            assert _errno_of(ops, "getattr", name, None) == errno.ENOENT
            assert _errno_of(ops, "open", name, os.O_RDONLY) == errno.ENOENT
            assert _errno_of(ops, "create", name, 0o644) == errno.EINVAL
            assert _errno_of(ops, "unlink", name) == errno.ENOENT
            assert _errno_of(ops, "rename", "/a?.txt", name) == errno.EINVAL
            assert _errno_of(ops, "rename", name, "/moved.txt") == errno.ENOENT

        # A Latin-1 name outside ASCII still reaches the handler as typed.
        fh = ops("create", "/café.txt", 0o644)
        ops("write", "/café.txt", NOTES, 0, fh)
        ops("release", "/café.txt", fh)

        assert ops("getattr", "/a?.txt", None)["st_size"] == len(OLD)
        names = sorted(n for n in ops("readdir", "/", None) if n not in (".", ".."))
        assert names == ["a?.txt", "cafe?.txt", "café.txt"]
    finally:
        ops("destroy", "/")

    for name in ("a?.txt", "cafe?.txt"):
        _xdftool(image, "read", name, str(tmp_path / "back.bin"))
        assert (tmp_path / "back.bin").read_bytes() == OLD


@pytest.fixture
def pfs_args(fixture_root, tmp_path):
    """Build `amifuse <command>` args for a copy of pfs.hdf with pfs3aio."""
    source = fixture_root / "fixtures" / "readonly" / "pfs.hdf"
    driver = fixture_root / "drivers" / "pfs3aio"
    if not source.exists() or not driver.exists():
        pytest.skip("Missing pfs.hdf or pfs3aio")
    image = tmp_path / "pfs.hdf"
    shutil.copyfile(source, image)

    def pfs_args(**extra):
        return SimpleNamespace(image=image, json=False, debug=False,
                               driver=driver, partition=None, block_size=None,
                               **extra)

    return pfs_args


def _refused(func, args, path):
    with pytest.raises(SystemExit) as exc_info:
        func(args)
    message = str(exc_info.value.code)
    assert message.startswith("Error: ") and path in message


@pytest.mark.integration
def test_file_commands_refuse_names_that_alias_another_file(pfs_args, tmp_path):
    original = tmp_path / "original.txt"
    original.write_bytes(NOTES)
    draft = tmp_path / "draft.txt"
    draft.write_bytes(b"draft\n")
    fuse_fs.cmd_write(pfs_args(file="T/notes.txt", input=str(original)))
    fuse_fs.cmd_write(pfs_args(file="T/a?.txt", input=str(original)))

    for path in ("T/draft:notes.txt", "T/a€.txt"):
        _refused(fuse_fs.cmd_write, pfs_args(file=path, input=str(draft)), path)
        _refused(fuse_fs.cmd_read, pfs_args(file=path, out=str(tmp_path / "got.bin")), path)
        _refused(fuse_fs.cmd_hash, pfs_args(file=path, algorithm="sha256"), path)
        _refused(fuse_fs.cmd_verify, pfs_args(file=path, expect_size=None), path)
        _refused(fuse_fs.cmd_ls, pfs_args(path=path, recursive=False), path)
    assert not (tmp_path / "got.bin").exists()

    for path in ("T/notes.txt", "T/a?.txt"):
        back = tmp_path / "back.bin"
        fuse_fs.cmd_read(pfs_args(file=path, out=str(back)))
        assert back.read_bytes() == NOTES
