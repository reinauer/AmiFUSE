"""Real handlers on RDBs whose blocks are larger than 512 bytes."""

from contextlib import ExitStack
import json
import subprocess
import sys

import pytest

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

from tests.unit.test_mbr_rdb_block_size import _make_mbr

pytestmark = pytest.mark.integration


def run_cli(*args):
    result = subprocess.run([sys.executable, "-m", "amifuse", *map(str, args)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def make_rdb(image, block_bytes, dos_type):
    cyls = (8 << 20) // (block_bytes * 4 * 32)
    raw = RawBlockDevice(str(image), read_only=False, block_bytes=block_bytes)
    with ExitStack() as cleanup:
        raw.create(cyls * 4 * 32)
        cleanup.callback(raw.close)
        disk = RDisk(raw)
        cleanup.callback(disk.close)
        disk.create(DiskGeometry(cyls, 4, 32), rdb_cyls=1)
        disk.add_partition(FSString("DH0"), (1, cyls - 1), dos_type=dos_type)
        raw.flush()


@pytest.mark.parametrize("name,dos_type,block_bytes,start_lba", [
    ("pfs3aio", 0x50465303, 1024, None),
    ("pfs3aio", 0x50465303, 1024, 2048),
    ("FastFileSystem", 0x444F5303, 4096, 2048),
], ids=["pfs3-1024", "pfs3-1024-mbr", "ffs-4096-mbr"])
def test_format_write_list_and_read(fixture_root, tmp_path, name, dos_type,
                                    block_bytes, start_lba):
    driver = fixture_root / "drivers" / name
    if not driver.exists():
        pytest.skip(f"Missing handler {name}")
    image = tmp_path / "rdb.hdf"
    make_rdb(image, block_bytes, dos_type)
    if start_lba is not None:
        # An Emu68-style disk: the RDB inside a 0x76 MBR partition.
        image = _make_mbr(tmp_path, [(start_lba, image.read_bytes())])
    data = bytes(range(256)) * 40
    source = tmp_path / "probe.bin"
    source.write_bytes(data)

    run_cli("format", image, "DH0", "Large", "--driver", driver)
    run_cli("write", image, "--driver", driver, "--file", "probe",
            "--in", source)
    listing = json.loads(run_cli("ls", image, "--driver", driver,
                                 "--json").stdout)
    assert "probe" in [entry["name"] for entry in listing["entries"]]
    output = tmp_path / "probe.out"
    run_cli("read", image, "--driver", driver, "--file", "probe",
            "--out", output)
    assert output.read_bytes() == data
