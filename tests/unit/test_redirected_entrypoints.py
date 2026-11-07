"""Standalone tools must report success through a legacy code page."""

import os
import shutil
import subprocess
import sys

import pytest

from amifuse.driver_resolver import BUNDLED_DRIVER
from tests.unit.test_driver_resolver import _rdb


@pytest.mark.parametrize("encoding", ["cp1252", "cp1251", "ascii"])
def test_extract_driver_with_redirected_output(tmp_path, encoding):
    image = _rdb(tmp_path, "DOS1", "DOS1")
    output = tmp_path / "fsΩ.bin"
    result = subprocess.run(
        [sys.executable, "-m", "amifuse.rdb_inspect", str(image),
         "--extract-fs", "0", "--out", str(output)],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": encoding + ":strict"},
    )
    assert result.returncode == 0, result.stderr.decode(encoding)
    assert output.read_bytes() == BUNDLED_DRIVER.read_bytes()
    assert b"Wrote filesystem #0" in result.stdout
    assert b"fs\\u03a9.bin" in result.stdout


@pytest.mark.parametrize("encoding", ["cp1252", "cp1251", "ascii"])
def test_driver_info_with_redirected_output(tmp_path, encoding):
    driver = tmp_path / "fsΩ.bin"
    shutil.copyfile(BUNDLED_DRIVER, driver)
    result = subprocess.run(
        [sys.executable, "-m", "amifuse.driver_info", str(driver)],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": encoding + ":strict"},
    )
    assert result.returncode == 0, result.stderr.decode(encoding)
    assert b"Binary:" in result.stdout
    assert b"fs\\u03a9.bin" in result.stdout
