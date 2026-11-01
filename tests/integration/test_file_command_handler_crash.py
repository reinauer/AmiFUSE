"""File commands must report a real handler crash as an error.

pfs3aio crashes at startup on a root block whose volume name is longer than
the 31 characters its name field holds. The volume is formatted with a valid
name, and the name's length byte is then raised to 40.
"""

import json
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration

NAME = b"ABCDEFGHIJKLMNOPQRSTUVWXYZABCDE"  # 31 characters, the limit


def _run(*args, check=True):
    proc = subprocess.run([sys.executable, *map(str, args)],
                          capture_output=True, text=True, timeout=60)
    if check:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


@pytest.fixture(scope="module")
def crashing_volume(fixture_root, tmp_path_factory):
    driver = fixture_root / "drivers" / "pfs3aio"
    if not driver.exists():
        pytest.skip("pfs3aio handler required")
    image = tmp_path_factory.mktemp("crash") / "pfs3.hdf"
    _run("-m", "amitools.tools.rdbtool", image, "create", "size=8Mi",
         "+", "init", "rdb_cyls=2",
         "+", "add", "size=6MiB", "name=DH0", "dostype=0x50465303")
    _run("-m", "amifuse", "format", "--driver", driver, image, "DH0",
         NAME.decode())
    data = bytearray(image.read_bytes())
    # The root block's name is a BSTR: a length byte, then the characters.
    at = data.find(NAME)
    assert at > 0 and data.find(NAME, at + 1) < 0
    assert data[at - 1] == len(NAME)
    data[at - 1] = 40
    image.write_bytes(bytes(data))
    return image, driver


@pytest.mark.parametrize("command", ["ls", "verify", "hash", "read", "write"])
@pytest.mark.parametrize("use_json", [True, False])
def test_crash_is_an_error(crashing_volume, tmp_path, command, use_json):
    source, driver = crashing_volume
    image = tmp_path / "pfs3.hdf"
    shutil.copy2(source, image)
    before = image.read_bytes()
    output, input_file = tmp_path / "out", tmp_path / "in"
    output.write_bytes(b"keep me")
    input_file.write_bytes(b"do not write")
    args = ["-m", "amifuse", command, image, "--driver", driver]
    if command in ("hash", "read", "write"):
        args += ["--file", "S/Startup-Sequence"]
    if command == "read":
        args += ["--out", output]
    if command == "write":
        args += ["--in", input_file]
    if use_json:
        args.append("--json")
    proc = _run(*args, check=False)
    # The premise: this handler crashes on this volume.
    assert "FATAL: Handler crashed" in proc.stderr
    assert proc.returncode == 1, proc.stdout + proc.stderr
    if use_json:
        error = json.loads(proc.stdout)["error"]
        assert error["code"] == "HANDLER_ERROR"
        assert "handler crashed" in error["message"]
    else:
        assert "Error: filesystem handler crashed" in proc.stderr
        assert proc.stdout == ""
    assert image.read_bytes() == before
    assert output.read_bytes() == b"keep me"
