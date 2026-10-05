"""Concurrent real handler sessions without requiring a host FUSE driver."""

from contextlib import ExitStack
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from amitools.fs.FSString import FSString
from amitools.fs.blkdev.DiskGeometry import DiskGeometry
from amitools.fs.blkdev.RawBlockDevice import RawBlockDevice
from amitools.fs.rdb.RDisk import RDisk

pytestmark = pytest.mark.integration

WORKER = """
import os, sys, time
from pathlib import Path
from amifuse.fuse_fs import HandlerBridge
image, driver, partition, ready, stop = map(Path, sys.argv[1:])
bridge = HandlerBridge(image, driver, partition=str(partition), read_only=False)
def write(name):
    handle = bridge.open_file('/' + name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
    assert handle is not None
    fh, _ = handle
    try:
        data = (str(partition) + ':' + name).encode() * 200
        assert bridge.write_handle(fh, data) == len(data)
    finally:
        bridge.close_file(fh)
    bridge.flush_volume()
try:
    write('before')
    ready.touch()
    deadline = time.monotonic() + 40
    while not stop.exists():
        if time.monotonic() > deadline:
            raise TimeoutError('parent did not release worker')
        time.sleep(0.02)
    write('after')
finally:
    bridge.close()
"""


def run_cli(*args):
    result = subprocess.run([sys.executable, "-m", "amifuse", *map(str, args)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def wait_unmounted(proc):
    # FUSE-T in macOS CI can exit with SIGPIPE after successful unmount.
    # Require process exit without forcing it; the remount below checks that
    # writes persisted and that the partition lock was released.
    expected = (0, -signal.SIGPIPE) if sys.platform == "darwin" else (0,)
    assert proc.wait(timeout=10) in expected


@pytest.fixture(params=[("pfs3aio", 0x50465303),
                       ("FastFileSystem", 0x444f5301),
                       ("SmartFilesystem", 0x53465300)])
def partition_image(request, fixture_root, tmp_path):
    name, dos_type = request.param
    driver = fixture_root / "drivers" / name
    if not driver.exists():
        pytest.skip(f"Missing handler {name}")
    image = tmp_path / "two.hdf"
    raw = RawBlockDevice(str(image), read_only=False)
    with ExitStack() as cleanup:
        raw.create(64 * 16 * 32)
        cleanup.callback(raw.close)
        disk = RDisk(raw)
        cleanup.callback(disk.close)
        disk.create(DiskGeometry(64, 16, 32), rdb_cyls=1)
        disk.add_partition(FSString("DH0"), (1, 31), dos_type=dos_type)
        disk.add_partition(FSString("DH1"), (32, 63), dos_type=dos_type)
        raw.flush()
    for partition in ("DH0", "DH1"):
        run_cli("format", image, partition, partition, "--driver", driver)
    return image, driver


def test_concurrent_handler_writes_persist(partition_image, tmp_path):
    image, driver = partition_image
    metadata = image.read_bytes()[:512 * 512]
    workers = []
    try:
        for partition in ("DH0", "DH1"):
            ready = tmp_path / (partition + ".ready")
            stop = tmp_path / (partition + ".stop")
            proc = subprocess.Popen(
                [sys.executable, "-c", WORKER, str(image), str(driver),
                 partition, str(ready), str(stop)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            workers.append((proc, stop))
            deadline = time.monotonic() + 20
            while not ready.exists():
                if proc.poll() is not None or time.monotonic() > deadline:
                    proc.kill()
                    out, err = proc.communicate(timeout=5)
                    pytest.fail(out + err)
                time.sleep(0.02)
        # Both processes now own live writable handler sessions. A reader
        # of the same partition must fail rather than observe cached writes.
        conflict = subprocess.run(
            [sys.executable, "-m", "amifuse", "ls", str(image), "--driver",
             str(driver), "--partition", "DH0"], capture_output=True,
            text=True, timeout=15)
        assert conflict.returncode != 0
        assert "cannot lock partition" in conflict.stdout + conflict.stderr
        for index, (proc, stop) in enumerate(workers):
            stop.touch()
            out, err = proc.communicate(timeout=20)
            assert proc.returncode == 0, out + err
            # Remount the released partition while the other remains live.
            partition = f"DH{index}"
            for name in ("before", "after"):
                output = tmp_path / f"{partition}-{name}"
                run_cli("read", image, "--driver", driver, "--partition",
                        partition, "--file", name, "--out", output)
                assert output.read_bytes() == f"{partition}:{name}".encode() * 200
        assert image.read_bytes()[:len(metadata)] == metadata
    finally:
        for proc, stop in workers:
            if proc.poll() is None:
                proc.kill()
            proc.communicate(timeout=5)


@pytest.mark.parametrize("partition_image", [("FastFileSystem", 0x444f5301)],
                         indirect=True)
def test_handler_recovers_after_range_error(partition_image):
    image, driver = partition_image
    script = """
import os, sys
from pathlib import Path
from amifuse.fuse_fs import HandlerBridge
bridge = HandlerBridge(Path(sys.argv[1]), Path(sys.argv[2]),
                       partition='DH0', read_only=False)
check = bridge.backend._check_range
faults = []
def reject_once(block, count):
    if not faults:
        faults.append(block)
        check(0, 1)  # Real partition guard: RDB metadata is outside DH0.
    check(block, count)
try:
    bridge.backend._check_range = reject_once
    failed = bridge.open_file('/rejected', os.O_WRONLY | os.O_CREAT)
    assert faults and failed is None
    bridge.backend._check_range = check
    handle = bridge.open_file('/recovered', os.O_WRONLY | os.O_CREAT)
    assert handle is not None
    fh, _ = handle
    assert bridge.write_handle(fh, b'recovered') == 9
    bridge.close_file(fh)
    bridge.flush_volume()
    assert bridge.read_file('/recovered', 9, 0) == b'recovered'
finally:
    bridge.backend._check_range = check
    bridge.close()
"""
    result = subprocess.run([sys.executable, "-c", script, str(image), str(driver)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.fuse
def test_concurrent_partition_mounts(partition_image, mount_image):
    image, driver = partition_image
    metadata = image.read_bytes()[:512 * 512]
    first, first_mount = mount_image(
        image, driver=driver, extra_args=["--partition", "DH0", "--write"])
    second, second_mount = mount_image(
        image, driver=driver, extra_args=["--partition", "DH1", "--write"])
    first_mount, second_mount = Path(first_mount), Path(second_mount)
    (first_mount / "first").write_bytes(b"first partition")
    (second_mount / "second").write_bytes(b"second partition")
    run_cli("unmount", first_mount)
    wait_unmounted(first)
    (second_mount / "after").write_bytes(b"first mount already closed")
    run_cli("unmount", second_mount)
    wait_unmounted(second)
    for partition, files in [("DH0", {"first": b"first partition"}),
                             ("DH1", {"second": b"second partition",
                                      "after": b"first mount already closed"})]:
        proc, mount = mount_image(image, driver=driver,
                                  extra_args=["--partition", partition])
        for name, content in files.items():
            assert (Path(mount) / name).read_bytes() == content
        run_cli("unmount", mount)
        wait_unmounted(proc)
    assert image.read_bytes()[:len(metadata)] == metadata
