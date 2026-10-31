"""Host names holding a colon on real handlers (#242).

pfs3aio, SmartFilesystem and FastFileSystem 47.4 read `draft:notes.txt` as
`notes.txt` in the same directory, and the bundled FastFileSystem as
`notes.txt` in the root, so creating it emptied another file. Such a name is
now refused before any packet: ENOENT for a lookup, EINVAL for a new name.

The tests call the callbacks through `AmigaFuseFS.__call__`, as fusepy does
(calling a callback directly skips the check), on copies of the fixture
images or on a DOS3 floppy made with xdftool for the bundled
FastFileSystem. No host FUSE driver is needed.
"""

import contextlib
import errno
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from amifuse import driver_resolver
from amifuse.fuse_fs import AmigaFuseFS, FuseOSError, HandlerBridge
from amifuse.rdb_inspect import detect_adf

AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"

PFS = ("pfs.hdf", "pfs3aio")
SFS = ("sfs.hdf", "SmartFilesystem")
OFS = ("ofs.adf", "FastFileSystem")

NOTES = b"the original notes\n"


def _fixture(handler):
    return pytest.param(handler, marks=pytest.mark.integration, id=handler[1])


BUNDLED_FFS = pytest.param(None, id="bundled-FastFileSystem")


@pytest.fixture
def open_ops(request, tmp_path):
    """AmigaFuseFS over a copy of a fixture image, or over a new DOS3 floppy
    with the bundled FastFileSystem for None."""
    stack = contextlib.ExitStack()

    def open_ops(handler):
        if handler is None:
            from tests.integration.conftest import _machine68k_works

            if not _machine68k_works():
                pytest.skip("machine68k CPU not functional")
            image = tmp_path / "gen.adf"
            proc = subprocess.run(
                [sys.executable, "-m", "amitools.tools.xdftool", str(image),
                 "create", "+", "format", "Gen", "DOS3"],
                cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
                capture_output=True, text=True, check=False,
            )
            assert proc.returncode == 0, proc.stdout + proc.stderr
            driver = driver_resolver.BUNDLED_DRIVER
        else:
            fixture_root = request.getfixturevalue("fixture_root")
            image_name, driver_name = handler
            source = fixture_root / "fixtures" / "readonly" / image_name
            driver = fixture_root / "drivers" / driver_name
            if not source.exists() or not driver.exists():
                pytest.skip(f"Missing {image_name} or {driver_name}")
            image = tmp_path / image_name
            shutil.copy2(source, image)
        bridge = HandlerBridge(image, driver, read_only=False, adf_info=detect_adf(image))
        stack.callback(bridge.close)
        return AmigaFuseFS(bridge)

    with stack:
        yield open_ops


def _errno_of(ops, op, *args):
    with pytest.raises(FuseOSError) as exc_info:
        ops(op, *args)
    return exc_info.value.errno


def _read_all(ops, path):
    fh = ops("open", path, os.O_RDONLY)
    try:
        return ops("read", path, 100, 0, fh)
    finally:
        ops("release", path, fh)


@pytest.mark.parametrize("handler", [_fixture(PFS), _fixture(SFS), _fixture(OFS), BUNDLED_FFS])
def test_colon_names_are_refused_and_leave_the_other_file(open_ops, handler):
    ops = open_ops(handler)
    ops("mkdir", "/t", 0o755)
    fh = ops("create", "/t/notes.txt", 0o644)
    ops("write", "/t/notes.txt", NOTES, 0, fh)
    ops("release", "/t/notes.txt", fh)

    assert _errno_of(ops, "getattr", "/t/draft:notes.txt", None) == errno.ENOENT
    assert _errno_of(ops, "open", "/t/draft:notes.txt", os.O_RDONLY) == errno.ENOENT
    assert _errno_of(ops, "create", "/t/draft:notes.txt", 0o644) == errno.EINVAL
    assert _errno_of(ops, "mkdir", "/t/draft:", 0o755) == errno.EINVAL
    assert _errno_of(ops, "rename", "/t/notes.txt", "/t/draft:notes.txt") == errno.EINVAL
    assert _errno_of(ops, "rename", "/t/draft:notes.txt", "/t/moved.txt") == errno.ENOENT
    assert _errno_of(ops, "unlink", "/t/draft:notes.txt") == errno.ENOENT

    assert ops("getattr", "/t/notes.txt", None)["st_size"] == len(NOTES)
    assert _read_all(ops, "/t/notes.txt") == NOTES
    assert [n for n in ops("readdir", "/t", None) if n not in (".", "..")] == ["notes.txt"]
    assert "notes.txt" not in [e["name"] for e in ops.bridge.list_dir_path("/")]
