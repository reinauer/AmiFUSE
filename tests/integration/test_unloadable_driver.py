"""`format`, `mount` and `ls` with a driver that cannot be loaded.

Each command names the --driver file, or the driver stored in the RDB, in a
one-line error and exits 1, without a traceback. The image is built here
with amitools' rdbtool, so these tests need no AmiFUSE-testing checkout.
"""

import errno
import os
import random
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
AMITOOLS_ROOT = REPO_ROOT / "amitools"

HUNK_HEADER = 0x3F3

DRIVERS = {
    # Bytes in no format the loader knows.
    "random": random.Random(268).randbytes(4096),
    # A HUNK_HEADER tag followed by a header the loader cannot parse.
    "damaged_hunk": struct.pack(">I", HUNK_HEADER) + bytes(8),
    # An ELF magic with a header too short to parse.
    "short_elf": b"\x7fELF" + bytes(4),
}

RDB_DRIVER = "the PFS3 (0x50465303) driver stored in the RDB"


@pytest.fixture(scope="module", autouse=True)
def _require_machine68k():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "disk.hdf"
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.rdbtool", str(path),
         "create", "size=8Mi", "+", "init", "rdb_cyls=2",
         "+", "add", "size=6MiB", "name=DH0", "fs=DOS3"],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return path


@pytest.fixture(params=sorted(DRIVERS))
def driver(request, tmp_path):
    path = tmp_path / "junk.handler"
    path.write_bytes(DRIVERS[request.param])
    return path


def _amifuse(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *args],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=45, check=False,
    )


def _assert_clean_error(proc: subprocess.CompletedProcess, driver: Path):
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "Traceback" not in output, output
    errors = [line for line in proc.stderr.splitlines() if str(driver) in line]
    assert len(errors) == 1, output
    assert "Failed to load handler" in errors[0], output


def test_format(image, driver):
    before = image.read_bytes()
    proc = _amifuse("format", str(image), "DH0", "Vol", "--driver", str(driver))
    _assert_clean_error(proc, driver)
    assert image.read_bytes() == before


@pytest.mark.fuse
def test_mount(image, driver, fuse_available, tmp_path):
    args = ["mount", str(image), "--driver", str(driver), "--foreground"]
    if not sys.platform.startswith("win"):
        # Windows picks a free drive letter; the handler fails before WinFsp.
        args += ["--mountpoint", str(tmp_path / "mnt")]
    _assert_clean_error(_amifuse(*args), driver)


def test_ls(image, driver):
    _assert_clean_error(_amifuse("ls", str(image), "--driver", str(driver)), driver)


@pytest.fixture
def runtime():
    from amifuse.vamos_runner import VamosHandlerRuntime

    runtime = VamosHandlerRuntime()
    runtime.setup(cpu="68020")
    yield runtime
    runtime.shutdown()


@pytest.mark.parametrize("kind,error,reason", [
    ("missing", FileNotFoundError, "no such file"),
    ("directory", ValueError, "not a regular file"),
])
def test_handler_path_not_a_file(runtime, tmp_path, kind, error, reason):
    # mount and format refuse such a --driver before loading it; the file
    # commands reach load_handler with it.
    path = tmp_path / "drv.handler"
    if kind == "directory":
        path.mkdir()
    with pytest.raises(error) as caught:
        runtime.load_handler(path)
    assert str(caught.value) == f"Failed to load handler {path}: {reason}"


def test_unreadable_handler_keeps_its_os_error(runtime, tmp_path, monkeypatch):
    # A read error is not a bad file: it reaches the commands as it was.
    path = tmp_path / "drv.handler"
    path.write_bytes(DRIVERS["random"])
    failure = PermissionError(13, "Permission denied", str(path))

    def load_sys_seglist(*args, **kwargs):
        raise failure

    monkeypatch.setattr(runtime.slm.seg_loader, "load_sys_seglist", load_sys_seglist)
    with pytest.raises(PermissionError) as caught:
        runtime.load_handler(path)
    assert caught.value is failure


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="needs POSIX permissions that bind the user")
def test_handler_in_unreadable_folder_is_not_missing(runtime, tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    path = folder / "drv.handler"
    path.write_bytes(DRIVERS["random"])
    folder.chmod(0)
    try:
        with pytest.raises(PermissionError) as caught:
            runtime.load_handler(path)
        with pytest.raises(PermissionError) as named:
            runtime.load_handler(path, RDB_DRIVER)
    finally:
        folder.chmod(0o755)
    assert str(caught.value.filename) == str(path)
    assert named.value.strerror == (f"Failed to load {RDB_DRIVER}: cannot "
                                    "read its temporary copy: Permission denied")
    assert str(path) not in str(named.value)


def _failing_loader(runtime, monkeypatch, failure):
    def load_sys_seglist(*args, **kwargs):
        raise failure

    monkeypatch.setattr(runtime.slm.seg_loader, "load_sys_seglist", load_sys_seglist)


def test_read_error_names_the_driver(runtime, tmp_path, monkeypatch):
    # A read fails after the open: the error from the loader has no filename.
    path = tmp_path / "drv.handler"
    path.write_bytes(DRIVERS["random"])
    failure = OSError(errno.EIO, "Input/output error")
    _failing_loader(runtime, monkeypatch, failure)
    with pytest.raises(OSError) as caught:
        runtime.load_handler(path)
    assert caught.value.errno == errno.EIO
    assert caught.value.__cause__ is failure
    assert str(path) in str(caught.value)
    assert "Input/output error" in str(caught.value)


def test_read_error_without_an_errno_names_the_driver(runtime, tmp_path,
                                                      monkeypatch):
    path = tmp_path / "drv.handler"
    path.write_bytes(DRIVERS["random"])
    _failing_loader(runtime, monkeypatch, OSError("read failed"))
    with pytest.raises(OSError) as caught:
        runtime.load_handler(path)
    assert str(caught.value) == f"Failed to load handler {path}: read failed"


@pytest.mark.parametrize("filename", [None, "the-temporary-path"])
def test_read_error_uses_the_drivers_name_over_its_path(
        runtime, tmp_path, monkeypatch, filename):
    path = tmp_path / "drv.handler"
    path.write_bytes(DRIVERS["random"])
    _failing_loader(runtime, monkeypatch,
                    OSError(errno.EIO, "Input/output error", filename))
    with pytest.raises(OSError) as caught:
        runtime.load_handler(path, RDB_DRIVER)
    assert caught.value.errno == errno.EIO
    assert caught.value.strerror == (f"Failed to load {RDB_DRIVER}: cannot "
                                     "read its temporary copy: Input/output error")
    assert str(path) not in str(caught.value)
    assert "the-temporary-path" not in str(caught.value)


@pytest.mark.parametrize("kind,error,reason", [
    # The file is the copy the RDB was read into: one that's gone is the
    # copy's fault, the bytes in it the image's.
    ("missing", FileNotFoundError, "cannot read its temporary copy: no such file"),
    ("directory", ValueError, "not a regular file"),
    ("damaged", ValueError, "not a loadable Amiga executable (HUNK or ELF)"),
])
def test_load_errors_use_the_drivers_name_over_its_path(runtime, tmp_path, kind,
                                                        error, reason):
    path = tmp_path / "drv.handler"
    if kind == "directory":
        path.mkdir()
    elif kind == "damaged":
        path.write_bytes(DRIVERS["random"])
    with pytest.raises(error) as caught:
        runtime.load_handler(path, RDB_DRIVER)
    assert str(caught.value) == f"Failed to load {RDB_DRIVER}: {reason}"


def test_parser_error_uses_the_drivers_name_over_its_path(runtime, tmp_path):
    path = tmp_path / "drv.handler"
    path.write_bytes(DRIVERS["damaged_hunk"])
    with pytest.raises(ValueError) as caught:
        runtime.load_handler(path, RDB_DRIVER)
    assert str(caught.value).startswith(f"Failed to load {RDB_DRIVER}: ")
    assert str(path) not in str(caught.value)


@pytest.fixture
def rdb_image(tmp_path):
    """A disk whose RDB stores a damaged driver for its partition's DOS type."""
    path = tmp_path / "rdbdriver.hdf"
    junk = tmp_path / "stored.handler"
    junk.write_bytes(DRIVERS["random"])
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.rdbtool", str(path),
         "create", "size=8Mi", "+", "init", "rdb_cyls=2",
         "+", "add", "size=6MiB", "name=DH0", "fs=PFS3",
         "+", "fsadd", str(junk), "fs=PFS3"],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return path


def _assert_names_the_rdb_driver(proc: subprocess.CompletedProcess):
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "Traceback" not in output, output
    assert f"Failed to load {RDB_DRIVER}: not a loadable" in proc.stderr, output
    assert "amifuse_" not in output, output


def test_rdb_driver_is_named_by_ls(rdb_image):
    _assert_names_the_rdb_driver(_amifuse("ls", str(rdb_image)))


def test_rdb_driver_is_named_by_format(rdb_image):
    _assert_names_the_rdb_driver(_amifuse("format", str(rdb_image), "DH0", "Vol"))


def test_rdb_driver_is_named_by_mount(rdb_image, tmp_path, monkeypatch):
    # The load fails before FUSE, which is stubbed out.
    from amifuse import fuse_fs, platform

    monkeypatch.setattr(fuse_fs, "_require_fuse", lambda: object)
    monkeypatch.setattr(platform, "check_fuse_available", lambda: None)
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(["mount", str(rdb_image), "--foreground",
                      "--mountpoint", str(tmp_path / "mnt")])
    message = str(caught.value.code)
    assert message.startswith(f"Cannot mount {rdb_image}: Failed to load {RDB_DRIVER}: ")
    assert "amifuse_" not in message


@pytest.mark.parametrize("from_rdb", [True, False])
def test_format_names_the_driver_it_cannot_read(rdb_image, tmp_path, monkeypatch,
                                                from_rdb):
    # format reads the driver for its PFS3 refusals, before the loader.
    from amifuse import fuse_fs

    def unreadable(*args):
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(fuse_fs, "_pfs3_format_refusal", unreadable)
    args = ["format", str(rdb_image), "DH0", "Vol"]
    name = f"the temporary copy of {RDB_DRIVER}"
    if not from_rdb:
        name = tmp_path / "stored.handler"
        args += ["--driver", str(name)]
    with pytest.raises(SystemExit) as caught:
        fuse_fs.main(args)
    assert caught.value.code == f"Error: cannot read {name}: Input/output error"
