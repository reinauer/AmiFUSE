"""`format`, `mount` and `ls` with a --driver file that cannot be loaded.

Each command names the file in a one-line error and exits 1, without a
traceback. The image is built here with amitools' rdbtool, so these tests
need no AmiFUSE-testing checkout.
"""

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
    finally:
        folder.chmod(0o755)
    assert str(caught.value.filename) == str(path)
