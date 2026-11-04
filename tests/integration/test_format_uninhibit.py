"""Formatting finishes the handler's inhibit before flushing its bitmap."""

import shutil
import subprocess
import sys

import pytest

from amifuse.fuse_fs import AmigaFuseFS, HandlerBridge, format_volume
from amifuse.rdb_inspect import detect_adf
from tests.fixtures.images import make_ffs_partition

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("partitioned", [False, True], ids=["floppy", "partition"])
def test_ffs47_format_writes_a_valid_bitmap(tmp_path, fixture_root, partitioned):
    driver = fixture_root / "drivers" / "FastFileSystem"
    if not driver.exists():
        pytest.skip("FastFileSystem 47.4 fixture required")
    image = tmp_path / ("disk.hdf" if partitioned else "disk.adf")
    if partitioned:
        make_ffs_partition(image)
        partition = "DH0"
    else:
        image.write_bytes(bytes(901120))
        partition = None
    format_volume(image, driver, None, partition, "Fresh")
    if partitioned:
        from amifuse.rdb_inspect import open_rdisk
        raw, disk, _ = open_rdisk(image)
        try:
            part = disk.parts[0].create_blkdev()
            part.open()
            start = part.blk_off * raw.block_bytes
            size = part.num_blocks * part.block_bytes
            raw_image = tmp_path / "partition.hdf"
            raw_image.write_bytes(image.read_bytes()[start:start + size])
        finally:
            disk.close()
            raw.close()
    else:
        raw_image = image
    result = subprocess.run(
        [sys.executable, "-m", "amitools.tools.xdftool", str(raw_image), "fsck"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Filesystem is clean" in result.stdout


@pytest.mark.parametrize("driver_name,image_name", [
    ("pfs3aio", "pfs.hdf"), ("SmartFilesystem", "sfs.hdf"),
])
def test_format_remounts_and_persists_other_handlers(
        tmp_path, fixture_root, driver_name, image_name):
    source = fixture_root / "fixtures" / "readonly" / image_name
    driver = fixture_root / "drivers" / driver_name
    if not source.exists() or not driver.exists():
        pytest.skip("Missing handler fixture")
    image = tmp_path / image_name
    shutil.copy2(source, image)
    format_volume(image, driver, None, None, "Fresh")
    bridge = HandlerBridge(image, driver, read_only=False)
    try:
        ops = AmigaFuseFS(bridge, icons=False)
        handle = ops.create("/check", 0o644)
        assert ops.write("/check", b"persisted", 0, handle) == 9
        ops.release("/check", handle)
        ops.fsync("/", False, None)
    finally:
        bridge.close()
    bridge = HandlerBridge(image, driver)
    try:
        assert bridge.read_file("/check", 9, 0) == b"persisted"
    finally:
        bridge.close()
