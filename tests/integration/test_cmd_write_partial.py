"""What a failed `amifuse write` leaves on an FFS floppy.

With the bundled FastFileSystem on images built by amitools' xdftool, so
these tests need no AmiFUSE-testing checkout.

- A source another program has locked part way through fails the command
  before the image is touched, whether the destination name exists or not.
- A write the handler fails removes the file it created under a new name,
  and names an existing destination it could not restore.
"""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from amifuse import driver_resolver
from amifuse import fuse_fs as fs

REPO_ROOT = Path(__file__).resolve().parents[2]
AMITOOLS_ROOT = REPO_ROOT / "amitools"
OLD = b"The destination's old contents\n"


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


def _xdftool(image: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(image), *args],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )


def _floppy(tmp_path: Path, fill: int = 0) -> Path:
    """A new FFS floppy holding Old.txt, and a fill file of that many bytes."""
    old = tmp_path / "old.txt"
    old.write_bytes(OLD)
    image = tmp_path / "disk.adf"
    cmd = ["create", "+", "format", "Seed", "ffs", "+", "write", str(old), "Old.txt"]
    if fill:
        data = tmp_path / "fill.bin"
        data.write_bytes(b"U" * fill)
        cmd += ["+", "write", str(data), "fill.bin"]
    proc = _xdftool(image, *cmd)
    assert proc.returncode == 0, f"xdftool failed:\n{proc.stdout}\n{proc.stderr}"
    return image


def _read_back(image: Path, name: str, out: Path):
    """The file's contents, or None when the image has no such file."""
    proc = _xdftool(image, "read", name, str(out))
    return out.read_bytes() if proc.returncode == 0 else None


def _free_line(image: Path) -> str:
    proc = _xdftool(image, "info")
    assert proc.returncode == 0, proc.stderr
    return next(line for line in proc.stdout.splitlines() if line.startswith("free:"))


def _args(image: Path, name: str, source: Path) -> SimpleNamespace:
    return SimpleNamespace(image=image, file=name, input=str(source), json=True,
                           debug=False, driver=None, partition=None, block_size=None)


@pytest.mark.skipif(sys.platform != "win32",
                    reason="only Windows byte-range locks make reads fail")
@pytest.mark.parametrize("name", ["Old.txt", "New.txt"], ids=["existing", "new"])
def test_source_locked_part_way_leaves_image_unchanged(tmp_path, no_user_driver,
                                                       capsys, name):
    """The issue's repro: another handle locks the source's second chunk."""
    import msvcrt

    image = _floppy(tmp_path)
    before = image.read_bytes()
    source = tmp_path / "new.bin"
    source.write_bytes(bytes(range(256)) * 800)
    with open(source, "rb") as other:
        other.seek(fs.IO_CHUNK)
        msvcrt.locking(other.fileno(), msvcrt.LK_NBLCK, fs.IO_CHUNK)
        try:
            with pytest.raises(SystemExit) as caught:
                fs.cmd_write(_args(image, name, source))
        finally:
            other.seek(fs.IO_CHUNK)
            msvcrt.locking(other.fileno(), msvcrt.LK_UNLCK, fs.IO_CHUNK)
    assert caught.value.code == 1
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "INVALID_ARGUMENT"
    assert error["message"].startswith(f"Cannot read source file: {source}: ")
    assert image.read_bytes() == before
    assert _read_back(image, "Old.txt", tmp_path / "old-back.bin") == OLD
    assert _read_back(image, "New.txt", tmp_path / "new-back.bin") is None


def test_disk_full_removes_a_new_file(tmp_path, no_user_driver, capsys):
    image = _floppy(tmp_path, fill=800000)
    free = _free_line(image)
    source = tmp_path / "big.bin"
    source.write_bytes(b"B" * 200000)
    with pytest.raises(SystemExit):
        fs.cmd_write(_args(image, "big.bin", source))
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == f"Write failed at offset {fs.IO_CHUNK}: disk full"
    assert _read_back(image, "big.bin", tmp_path / "big-back.bin") is None
    assert _free_line(image) == free


def test_disk_full_names_an_existing_destination(tmp_path, no_user_driver, capsys):
    image = _floppy(tmp_path, fill=800000)
    source = tmp_path / "big.bin"
    source.write_bytes(b"B" * 200000)
    with pytest.raises(SystemExit):
        fs.cmd_write(_args(image, "Old.txt", source))
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "WRITE_ERROR"
    assert error["message"].startswith("Write failed at offset ")
    assert error["message"].endswith(
        ": disk full; the existing Old.txt is left partial, and its old "
        "contents are gone")
    kept = _read_back(image, "Old.txt", tmp_path / "old-back.bin")
    assert kept is not None and kept != OLD
