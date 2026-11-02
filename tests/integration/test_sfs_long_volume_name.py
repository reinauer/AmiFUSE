"""A 255-character SmartFilesystem volume name, on a volume format creates.

SmartFilesystem copies the whole name into the FileInfoBlock when the root is
examined, so the block has to hold it (#287). No host FUSE driver is needed.
"""

import os
import subprocess
import sys

import pytest

from amifuse import fuse_fs as fs

pytestmark = pytest.mark.integration


def test_the_examine_stays_inside_the_fib(fixture_root, tmp_path):
    driver = fixture_root / "drivers" / "SmartFilesystem"
    if not driver.exists():
        pytest.skip("Missing SmartFilesystem")
    image = tmp_path / "sfs.hdf"
    subprocess.run(
        [sys.executable, "-m", "amitools.tools.rdbtool", str(image), "create",
         "size=8Mi", "+", "init", "rdb_cyls=2", "+", "add", "size=6MiB",
         "name=DH0", "dostype=0x53465300"],
        check=True, capture_output=True)
    name = "".join(chr(ord("A") + i % 26) for i in range(255))
    fs.format_volume(image, driver, None, "DH0", volname=name)

    bridge = fs.HandlerBridge(image, driver, read_only=False, partition="DH0")
    try:
        fib = bridge._alloc_fib()
        guard = bridge.vh.alloc.alloc_memory(16, label="guard")
        assert guard.addr == fib.addr + fib.size
        bridge.mem.w_block(guard.addr, b"\xa5" * 16)

        volname = bridge.volume_name()

        assert bytes(bridge.mem.r_block(guard.addr, 16)) == b"\xa5" * 16
        # The rest of the name isn't read: fib_Comment's length byte is in it.
        assert volname == name[:107]

        fh, _ = bridge.open_file("/file", os.O_CREAT | os.O_RDWR)
        bridge.write_handle(fh, b"data")
        bridge.close_file(fh)
        assert [(e["name"], e["size"]) for e in bridge.list_dir(0)] == [("file", 4)]
    finally:
        bridge.close()
