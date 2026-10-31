"""`amifuse write` onto a full floppy with the bundled FastFileSystem.

The handler fails the write with ERROR_DISK_FULL, and the report says so
instead of "no reply from handler". The image is built here with amitools'
xdftool, so this test needs no AmiFUSE-testing checkout, and runs wherever
machine68k works.
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


def _full_floppy(tmp_path: Path) -> Path:
    """A new FFS floppy with about 85 KiB free, as in the issue."""
    fill = tmp_path / "fill.bin"
    fill.write_bytes(b"U" * 800000)
    image = tmp_path / "full.adf"
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(image),
         "create", "+", "format", "Full", "ffs", "+", "write", str(fill), "fill.bin"],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, f"xdftool failed:\n{proc.stdout}\n{proc.stderr}"
    return image


@pytest.mark.parametrize("use_json", [True, False], ids=["json", "text"])
def test_write_to_full_floppy_reports_disk_full(tmp_path, no_user_driver,
                                                capsys, use_json):
    image = _full_floppy(tmp_path)
    source = tmp_path / "big.bin"
    source.write_bytes(b"B" * 200000)
    args = SimpleNamespace(image=image, file="big.bin", input=str(source),
                           json=use_json, debug=False, driver=None,
                           partition=None, block_size=None)
    with pytest.raises(SystemExit) as caught:
        fs.cmd_write(args)
    if use_json:
        assert caught.value.code == 1
        error = json.loads(capsys.readouterr().out)["error"]
        assert error["code"] == "WRITE_ERROR"
        assert error["message"] == f"Write failed at offset {fs.IO_CHUNK}: disk full"
        assert error["details"]["dos_error"] == 221
        assert error["details"]["bytes_written"] == fs.IO_CHUNK
    else:
        assert str(caught.value) == f"Error: write failed at offset {fs.IO_CHUNK}: disk full"
