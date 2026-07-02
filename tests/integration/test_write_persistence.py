"""Integration tests for write persistence.

Verifies that files written to a writable mount persist across unmount/remount
cycles and that written content is readable immediately.
"""
import os
import shutil
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.fuse, pytest.mark.slow]


@pytest.fixture
def writable_pfs3_mount(pfs3_image, pfs3_driver, fuse_available, mount_image, tmp_path):
    """Mount a COPY of the PFS3 image in write mode.

    Uses a copy of the test image to avoid corrupting the shared fixture.
    Yields (process, mountpoint, work_image_path).
    """
    # Copy image so writes don't corrupt the shared fixture
    work_image = tmp_path / "write_test.hdf"
    shutil.copy2(pfs3_image, work_image)

    proc, mountpoint = mount_image(
        work_image,
        driver=pfs3_driver,
        extra_args=["--write"],
    )

    yield proc, mountpoint, work_image


def test_write_file_readable_immediately(writable_pfs3_mount):
    """Write a file through FUSE mount and read it back immediately."""
    _proc, mountpoint, _work_image = writable_pfs3_mount
    mp = str(mountpoint)

    test_file = os.path.join(mp, "testfile.txt")
    test_content = b"Hello from AmiFUSE write test!"

    with open(test_file, "wb") as f:
        f.write(test_content)

    # Verify file exists
    assert os.path.exists(test_file), "File should exist after write"

    # Read back and verify content
    with open(test_file, "rb") as f:
        readback = f.read()
    assert readback == test_content, (
        f"Content mismatch: {readback!r} != {test_content!r}"
    )


def test_write_file_appears_in_listing(writable_pfs3_mount):
    """Written file should appear in os.listdir of the mount root."""
    _proc, mountpoint, _work_image = writable_pfs3_mount
    mp = str(mountpoint)

    test_file = os.path.join(mp, "listed_file.txt")
    with open(test_file, "wb") as f:
        f.write(b"listing test")

    entries = os.listdir(mp)
    assert "listed_file.txt" in entries, (
        f"Expected 'listed_file.txt' in listing, got: {entries}"
    )


def test_write_persists_after_remount(
    writable_pfs3_mount, mount_image, pfs3_driver,
):
    """Write a file, unmount, remount read-only, verify file present.

    This is the full persistence test: data must survive the unmount/remount
    cycle, proving it was flushed to the image file.
    """
    proc, mountpoint, work_image = writable_pfs3_mount
    mp = str(mountpoint)

    # Write a test file
    test_file = os.path.join(mp, "persist_test.txt")
    test_content = b"Data that must survive remount"
    with open(test_file, "wb") as f:
        f.write(test_content)

    # Verify written
    assert os.path.exists(test_file)

    # Unmount the writable mount
    subprocess.run(
        [sys.executable, "-m", "amifuse", "unmount", mp],
        capture_output=True, text=True, timeout=10, check=False,
    )

    # Wait for process exit
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # Wait for unmount to complete
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if sys.platform.startswith("win"):
            try:
                os.listdir(mp)
            except OSError:
                break
        else:
            if not os.path.ismount(mp):
                break
        time.sleep(0.5)

    # Remount read-only
    proc2, mountpoint2 = mount_image(work_image, driver=pfs3_driver)
    mp2 = str(mountpoint2)

    # Verify the file persisted
    remounted_file = os.path.join(mp2, "persist_test.txt")
    assert os.path.exists(remounted_file), (
        f"File 'persist_test.txt' did not persist after remount.\n"
        f"Listing: {os.listdir(mp2)}"
    )

    with open(remounted_file, "rb") as f:
        readback = f.read()
    assert readback == test_content, (
        f"Content did not persist: {readback!r} != {test_content!r}"
    )
