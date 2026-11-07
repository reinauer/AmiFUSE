"""A successful write must survive redirected legacy-code-page output."""

import hashlib
import json
import shutil
import os
import subprocess
import sys

import pytest

from tests.integration.test_bundled_ffs import _seed_adf


@pytest.mark.parametrize("encoding,name", [
    ("cp1252", "c\x81d.txt"),
    ("cp1251", "caf\xe9.txt"),
    ("ascii", "caf\xe9.txt"),
])
@pytest.mark.integration
def test_write_with_redirected_output(tmp_path, fixture_root, encoding, name):
    image = _seed_adf(tmp_path, "DOS3")
    driver_args = []
    if encoding == "cp1252":
        image = tmp_path / "pfs.hdf"
        shutil.copy2(fixture_root / "fixtures/readonly/pfs.hdf", image)
        driver_args = ["--driver", str(fixture_root / "drivers/pfs3aio")]
    source = tmp_path / "input"
    source.write_bytes(b"written successfully\n")
    result = subprocess.run(
        [sys.executable, "-m", "amifuse", "write", str(image),
         "--file", name, "--in", str(source), *driver_args], capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": encoding + ":strict"},
    )
    assert result.returncode == 0, result.stderr.decode(encoding)
    assert b"Written:" in result.stdout
    readback = subprocess.run(
        [sys.executable, "-m", "amifuse", "hash", str(image),
         "--file", name, "--json", *driver_args], capture_output=True,
    )
    assert readback.returncode == 0, readback.stderr
    assert json.loads(readback.stdout)["hash"] == hashlib.sha256(
        source.read_bytes()).hexdigest()
