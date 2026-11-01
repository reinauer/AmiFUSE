"""A disk whose RDB filesystem list links past the end of the image.

The partitions are intact, so inspect and ls still reach the volume, with
the RDB's driver or with --driver.
"""

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

AMITOOLS_ROOT = Path(__file__).resolve().parents[2] / "amitools"
PFS3 = "0x50465303"
PAST_END = 0x100000  # The image has 65536 blocks.


def _run(*args, timeout=60.0):
    return subprocess.run(
        [sys.executable, *args], capture_output=True, text=True,
        timeout=timeout, check=False,
    )


@pytest.fixture
def pastend_image(tmp_path, pfs3_driver):
    """A PFS3 partition DH0 formatted with pfs3aio, the RDB's only
    filesystem, whose header links on to a block past the end."""
    image = tmp_path / "pastend.hdf"
    proc = subprocess.run(
        [sys.executable, "-m", "amitools.tools.rdbtool", str(image),
         "create", "size=32Mi", "+", "init", "rdb_cyls=40",
         "+", "fsadd", str(pfs3_driver), f"dostype={PFS3}",
         "+", "add", "size=50%", f"dostype={PFS3}", "name=DH0"],
        cwd=AMITOOLS_ROOT if AMITOOLS_ROOT.is_dir() else None,
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    proc = _run("-m", "amifuse", "format", "--driver", str(pfs3_driver),
                str(image), "DH0", "Work")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    data = bytearray(image.read_bytes())
    fshd = next(blk for blk in range(16)
                if data[blk * 512:blk * 512 + 4] == b"FSHD")
    longs = list(struct.unpack_from(">128I", data, fshd * 512))
    assert longs[4] == 0xFFFFFFFF  # The list's only entry.
    longs[4] = PAST_END
    longs[2] = 0
    longs[2] = -sum(longs) & 0xFFFFFFFF
    struct.pack_into(">128I", data, fshd * 512, *longs)
    image.write_bytes(bytes(data))
    return image


def _ls(image, *args):
    proc = _run("-m", "amifuse", "ls", "--json", *args, str(image))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout[proc.stdout.find("{"):])
    assert payload["status"] == "ok"
    return payload


def test_inspect_lists_the_partition(pastend_image):
    proc = _run("-m", "amifuse", "inspect", str(pastend_image))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DH0" in proc.stdout
    assert (f"Filesystem list links to block {PAST_END}, past the end of "
            f"the device (remaining filesystem entries skipped)") in proc.stdout


def test_ls_with_driver(pastend_image, pfs3_driver):
    assert _ls(pastend_image, "--driver", str(pfs3_driver))["entries"] == []


def test_ls_with_the_rdb_driver(pastend_image):
    """The entry before the link is kept, so its driver serves DH0."""
    assert _ls(pastend_image)["entries"] == []
