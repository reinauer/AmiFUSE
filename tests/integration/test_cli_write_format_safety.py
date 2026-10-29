"""`amifuse write` and `amifuse format` with the bundled FastFileSystem.

The images are built here with amitools' xdftool and rdbtool, so these
tests need no AmiFUSE-testing checkout, and run wherever machine68k works.

- write with a source it cannot read leaves the destination's old contents.
- format fsyncs the image before it reports "Format complete".
"""

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from amifuse import driver_resolver
from amifuse import fuse_fs as fs

REPO_ROOT = Path(__file__).resolve().parents[2]
AMITOOLS_ROOT = REPO_ROOT / "amitools"

OLD_DATA = b"The destination's old contents\n"
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


def _amitool(module: str, *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"{module} {args} failed:\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _xdf_read(image: Path, amiga_path: str, tmp_path: Path) -> bytes:
    out = tmp_path / "xdf-read.bin"
    _amitool("amitools.tools.xdftool", str(image), "read", amiga_path, str(out))
    return out.read_bytes()


def _hold_unreadable(path: Path):
    """Make path unreadable to this process; returns an undo function.

    Windows: another handle holds it open without read sharing, as another
    program can. Elsewhere: its mode is 000.
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateFileW.restype = wintypes.HANDLE
        k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        generic_read, no_sharing, open_existing = 0x80000000, 0, 3
        handle = k32.CreateFileW(str(path), generic_read, no_sharing, None,
                                 open_existing, 0, None)
        assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
        return lambda: k32.CloseHandle(handle)
    if os.geteuid() == 0:
        pytest.skip("root reads a file of mode 000")
    path.chmod(0)
    return lambda: path.chmod(0o644)


def _write_args(image: Path, source: Path) -> SimpleNamespace:
    return SimpleNamespace(image=image, file="Old.txt", input=str(source),
                           json=True, debug=False, driver=None, partition=None,
                           block_size=None)


def test_unreadable_source_leaves_the_destination(tmp_path, no_user_driver, capsys):
    seed = tmp_path / "old.txt"
    seed.write_bytes(OLD_DATA)
    image = tmp_path / "disk.adf"
    _amitool("amitools.tools.xdftool", str(image), "format", "Seed", "DOS3",
             "+", "write", str(seed), "Old.txt")

    source = tmp_path / "new.bin"
    source.write_bytes(NEW_DATA)
    undo = _hold_unreadable(source)
    try:
        with pytest.raises(SystemExit):
            fs.cmd_write(_write_args(image, source))
    finally:
        undo()
    assert _xdf_read(image, "Old.txt", tmp_path) == OLD_DATA
    assert '"INVALID_ARGUMENT"' in capsys.readouterr().out

    # Once readable, the same source replaces it.
    fs.cmd_write(_write_args(image, source))
    assert '"status": "ok"' in capsys.readouterr().out
    assert _xdf_read(image, "Old.txt", tmp_path) == NEW_DATA


def test_format_fsyncs_the_image_before_complete(tmp_path, no_user_driver,
                                                 monkeypatch, capsys):
    image = tmp_path / "disk.hdf"
    _amitool(
        "amitools.tools.rdbtool", str(image),
        "create", "size=8Mi", "+", "init", "rdb_cyls=2",
        "+", "add", "size=6MiB", "name=DH0", "fs=DOS3",
    )
    image_stat = os.stat(image)
    fsynced = []
    real_fsync = os.fsync

    def fsync(fd):
        if os.path.samestat(os.fstat(fd), image_stat):
            # What format has printed by the time the image is fsynced.
            fsynced.append(capsys.readouterr().out)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    fs.format_volume(image, None, None, "DH0", volname="Fresh")
    assert fsynced, "format did not fsync the image"
    assert "Format complete." not in "".join(fsynced)
    assert "Format complete." in capsys.readouterr().out
    listing = _amitool("amitools.tools.xdftool", str(image), "open", "part=DH0",
                       "+", "list")
    assert "Fresh" in listing
