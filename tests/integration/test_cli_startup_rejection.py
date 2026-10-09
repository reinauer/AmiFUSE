"""A real handler startup refusal must preserve the disk diagnosis."""

import json
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("command", ["ls", "verify", "hash", "read", "write"])
@pytest.mark.parametrize("use_json", [True, False])
def test_sfs_rejected_by_ffs(fixture_root, tmp_path, command, use_json):
    source = fixture_root / "fixtures" / "readonly" / "sfs.hdf"
    driver = fixture_root / "drivers" / "FastFileSystem"
    if not source.exists() or not driver.exists():
        pytest.skip("SFS image and FastFileSystem handler required")
    image = tmp_path / "sfs.hdf"
    shutil.copy2(source, image)
    before = image.read_bytes()
    output, input_file = tmp_path / "out", tmp_path / "in"
    output.write_bytes(b"keep me")
    input_file.write_bytes(b"do not write")
    args = [sys.executable, "-m", "amifuse", command, str(image), "--driver", str(driver)]
    if command in ("hash", "read", "write"):
        args += ["--file", "file"]
    if command == "read":
        args += ["--out", str(output)]
    if command == "write":
        args += ["--in", str(input_file)]
    if use_json:
        args.append("--json")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    if use_json:
        error = json.loads(proc.stdout)["error"]
        assert error["code"] == "NOT_MOUNTED"
        assert error["details"] == {"dos_error": 0}
        message = error["message"]
    else:
        message = proc.stderr
        assert "Error: no usable volume mounted:" in message
        if command == "verify":
            # Same report as a disk refused after startup.
            assert proc.stdout.splitlines() == [
                "Volume: (none)",
                "  Filesystem responsive: NO -- handler rejected the disk: "
                "no reason code supplied (wrong --driver for this filesystem?)",
            ]
    assert "wrong --driver" in message
    assert image.read_bytes() == before
    assert output.read_bytes() == b"keep me"
