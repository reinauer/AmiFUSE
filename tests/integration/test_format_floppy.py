"""`amifuse format` on a floppy image, with the bundled FastFileSystem.

The images are blank files or built here with xdftool, so these tests need
no AmiFUSE-testing checkout. Like test_bundled_ffs.py they are not
integration-marked, and skip only when machine68k does not work.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from amifuse import driver_resolver
from amifuse.fuse_fs import HandlerBridge
from amifuse.rdb_inspect import detect_adf

REPO_ROOT = Path(__file__).resolve().parents[2]
AMITOOLS_ROOT = REPO_ROOT / "amitools"

DD, HD = 901120, 1802240
SEED_DATA = b"An old file\n"
NEW_DATA = bytes((i * 13 + 5) % 251 for i in range(3000))


@pytest.fixture(scope="module", autouse=True)
def _require_machine68k():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")


@pytest.fixture
def no_user_driver(monkeypatch, tmp_path):
    """An empty user drivers folder and no dev fallback: the bundled handler."""
    home = tmp_path / "home"
    home.mkdir()
    for name in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(name, str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setattr(
        driver_resolver, "DEV_FALLBACK_DRIVER", tmp_path / "no-dev" / "FastFileSystem")


def _xdftool(image: Path, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(image), *args],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"xdftool {args} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _amifuse(*args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *map(str, args)],
        capture_output=True, text=True, check=False,
    )


def _blank(tmp_path: Path, size: int = DD) -> Path:
    image = tmp_path / "floppy.adf"
    image.write_bytes(bytes(size))
    return image


def _seed(tmp_path: Path, dos: str) -> Path:
    """A floppy with a file on it, made by xdftool."""
    source = tmp_path / "Old.txt"
    source.write_bytes(SEED_DATA)
    image = tmp_path / "seed.adf"
    _xdftool(image, "format", "Seed", dos, "+", "write", str(source), "Old.txt")
    return image


def _digest(image: Path) -> str:
    return hashlib.sha256(image.read_bytes()).hexdigest()


def _clean(image: Path, volume: str, dos: str) -> str:
    """Check that xdftool finds an empty, valid volume; returns its listing."""
    assert "Filesystem is clean" in _xdftool(image, "fsck")
    listing = _xdftool(image, "list")
    first = listing.splitlines()[0].split()
    assert first[0] == volume
    assert f"{dos}:" in listing.splitlines()[0]
    return listing


@pytest.mark.parametrize("size,name", [(DD, "DD"), (HD, "HD")])
def test_blank_image(tmp_path, no_user_driver, size, name):
    """A blank file becomes a floppy that the other commands and xdftool read
    and that takes a file."""
    image = _blank(tmp_path, size)
    result = _amifuse("format", image, "Fresh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Formatting {name} floppy image {image}" in result.stdout
    assert "Filesystem driver: FastFileSystem 0.2 (21.10.2026) (bundled" in result.stdout
    assert f"Volume 'Fresh' created on {image}" in result.stdout
    assert image.stat().st_size == size

    assert detect_adf(image).dos_type == 0x444F5300
    _clean(image, "Fresh", "DOS0")
    listing = _amifuse("verify", image)
    assert listing.returncode == 0, listing.stdout + listing.stderr
    assert "Volume: Fresh" in listing.stdout

    source = tmp_path / "source.bin"
    source.write_bytes(NEW_DATA)
    written = _amifuse("write", image, "--file", "New.bin", "--in", source)
    assert written.returncode == 0, written.stdout + written.stderr
    out = tmp_path / "read-back.bin"
    _xdftool(image, "read", "New.bin", str(out))
    assert out.read_bytes() == NEW_DATA


@pytest.mark.parametrize("size", [DD, HD], ids=["dd", "hd"])
@pytest.mark.parametrize("n", range(8))
def test_blank_image_takes_the_option(tmp_path, no_user_driver, n, size):
    image = _blank(tmp_path, size)
    result = _amifuse("format", image, "Fresh", "--dos-type", f"DOS{n}")
    assert result.returncode == 0, result.stdout + result.stderr
    assert image.read_bytes()[:4] == b"DOS" + bytes([n])
    _clean(image, "Fresh", f"DOS{n}")


def test_dos_type_stays_and_old_contents_go(tmp_path, no_user_driver):
    image = _seed(tmp_path, "DOS3")
    assert "Old.txt" in _xdftool(image, "list")
    result = _amifuse("format", image, "Fresh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DOS type: DOS3 (0x444f5303)" in result.stdout
    assert "Old.txt" not in _clean(image, "Fresh", "DOS3")


def test_option_replaces_the_dos_type(tmp_path, no_user_driver):
    image = _seed(tmp_path, "DOS1")
    result = _amifuse("format", image, "Fresh", "--dos-type", "DOS5")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Old.txt" not in _clean(image, "Fresh", "DOS5")


def test_other_contents_go_only_on_request(tmp_path, no_user_driver):
    image = tmp_path / "other.adf"
    image.write_bytes(b"\xaa" * DD)

    result = _amifuse("format", image, "Fresh")
    assert result.returncode == 1
    assert "--dos-type" in result.stderr
    assert image.read_bytes() == b"\xaa" * DD

    result = _amifuse("format", image, "Fresh", "--dos-type", "DOS1")
    assert result.returncode == 0, result.stdout + result.stderr
    _clean(image, "Fresh", "DOS1")


@pytest.mark.parametrize("name", ["A" * 31, "", "a:b", "a/b"],
                         ids=["31-chars", "empty", "colon", "slash"])
@pytest.mark.parametrize("make", [_blank, lambda path: _seed(path, "DOS1")],
                         ids=["blank", "formatted"])
def test_a_name_the_handler_refuses_writes_nothing(tmp_path, no_user_driver, make, name):
    image = make(tmp_path)
    before = _digest(image)
    result = _amifuse("format", image, name)
    assert result.returncode == 1
    assert "Format failed: error code 210" in result.stderr
    assert "Format complete" not in result.stdout
    assert _digest(image) == before


def test_longest_name_ffs_takes(tmp_path, no_user_driver):
    image = _blank(tmp_path)
    result = _amifuse("format", image, "A" * 30)
    assert result.returncode == 0, result.stdout + result.stderr
    _clean(image, "A" * 30, "DOS0")


def test_a_floppy_argument_too_long_for_a_bstr_writes_nothing(tmp_path, no_user_driver):
    image = _blank(tmp_path)
    result = _amifuse("format", image, "A" * 256)
    assert result.returncode == 1
    assert "at most 255 are allowed" in result.stderr
    assert image.read_bytes() == bytes(DD)


def test_a_partition_argument_on_a_floppy_writes_nothing(tmp_path, no_user_driver):
    image = _seed(tmp_path, "DOS1")
    before = _digest(image)
    result = _amifuse("format", image, "DF0", "Fresh")
    assert result.returncode == 1
    assert "no partitions" in result.stderr
    assert _digest(image) == before


def test_an_open_floppy_writes_nothing(tmp_path, no_user_driver):
    """A session on the image holds it, as a mount does."""
    image = _seed(tmp_path, "DOS1")
    before = _digest(image)
    bridge = HandlerBridge(image, driver_resolver.BUNDLED_DRIVER,
                           adf_info=detect_adf(image))
    try:
        result = _amifuse("format", image, "Fresh")
    finally:
        bridge.close()
    assert result.returncode == 1
    assert "in use" in result.stderr
    assert _digest(image) == before


@pytest.mark.parametrize("command", [
    ("ls",), ("verify",), ("inspect",), ("hash", "--file", "x"), ("read", "--file", "x"),
], ids=lambda command: command[0])
def test_a_blank_image_is_still_no_adf_elsewhere(tmp_path, no_user_driver, command):
    """format alone takes a blank file for a floppy."""
    image = _blank(tmp_path)
    args = [command[0], image, *command[1:]]
    result = _amifuse(*args)
    assert result.returncode != 0
    assert "No valid RDB found" in result.stdout + result.stderr
    assert image.read_bytes() == bytes(DD)
