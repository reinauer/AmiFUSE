"""write names a full volume as "disk full" before any data is written (#462).

The handler refuses a new file, or a new directory on the way to it, with
ERROR_DISK_FULL, which write named only for a failed data write: a new file
got "failed to open file for writing" and a new directory "error 221". The
image is a DOS3 floppy that one file fills to the last block, with the
bundled FastFileSystem.
"""

import json
import subprocess
import sys

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from tests.integration.test_ls_recursive_ffs import _xdftool

pytestmark = pytest.mark.timeout(120)

ERROR_DISK_FULL = 221
# 1731 data blocks, a header and 24 extension blocks: all 1756 free blocks.
FILL_BYTES = 886272


@pytest.fixture
def full_image(tmp_path):
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")
    image, fill = tmp_path / "full.adf", tmp_path / "fill.bin"
    fill.write_bytes(b"\0" * FILL_BYTES)
    _xdftool(image, "create", "+", "format", "Full", "ffs", "+", "write", str(fill), "fill")
    return image


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "small.txt"
    path.write_bytes(b"small\n")
    return path


def _write(image, source, dest, *extra):
    return subprocess.run(
        [sys.executable, "-m", "amifuse", "write", str(image), "--file", dest,
         "--in", str(source), "--driver", str(BUNDLED_DRIVER), *extra],
        capture_output=True, text=True, check=False)


@pytest.mark.parametrize("dest, message", [
    ("new.txt", "failed to open file for writing: new.txt (disk full)"),
    ("d/new.txt", "Failed to create directory: /d (disk full)"),
], ids=["new-file", "new-directory"])
def test_write_on_a_full_volume_says_disk_full(full_image, source, dest, message):
    result = _write(full_image, source, dest)
    assert result.returncode == 1
    assert f"Error: {message}" in result.stderr


@pytest.mark.parametrize("dest", ["new.txt", "d/new.txt"], ids=["new-file", "new-directory"])
def test_write_on_a_full_volume_json(full_image, source, dest):
    result = _write(full_image, source, dest, "--json")
    assert result.returncode == 1
    error = json.loads(result.stdout)["error"]
    assert "(disk full)" in error["message"]
    assert error["details"]["dos_error"] == ERROR_DISK_FULL
