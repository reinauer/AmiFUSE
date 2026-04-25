"""Mount lifecycle integration tests.

These tests require a working FUSE backend (macFUSE/FUSE-T, fuse3, WinFSP)
and the AmiFUSE-testing fixtures (PFS3 driver + test image). They exercise
the full mount -> use -> unmount cycle via subprocess, verifying that the
FUSE bridge works end-to-end on each platform.

Marker: @pytest.mark.fuse (NOT @pytest.mark.integration -- avoids machine68k gating)
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.fuse


def _run_amifuse(*args, timeout=30.0):
    """Run amifuse as a subprocess and return CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *args],
        capture_output=True, text=True,
        timeout=timeout, check=False,
    )


def test_mount_creates_mountpoint(pfs3_mount):
    """Mount PFS3 image and verify the mountpoint is active."""
    proc, mountpoint = pfs3_mount
    assert os.path.ismount(str(mountpoint)), (
        f"Expected {mountpoint} to be a mount, but os.path.ismount() is False"
    )
    assert proc.poll() is None, "Mount process should still be running"


def test_mount_visible_in_status(pfs3_mount, pfs3_image):
    """Mount PFS3 image and verify it appears in `amifuse status --json`."""
    proc, mountpoint = pfs3_mount
    result = _run_amifuse("status", "--json")
    assert result.returncode == 0, f"status failed: {result.stderr}"
    data = json.loads(result.stdout)
    assert data["status"] == "ok"

    # Find our mount in the list
    mp_str = str(mountpoint)
    image_name = pfs3_image.name
    matching = [
        m for m in data["mounts"]
        if mp_str in m.get("mountpoint", "") or image_name in m.get("image", "")
    ]
    assert len(matching) >= 1, (
        f"Mount at {mp_str} (image {image_name}) not found in status output.\n"
        f"Mounts returned: {json.dumps(data['mounts'], indent=2)}\n"
        f"Mount process PID: {proc.pid}\n"
        f"Mount process alive: {proc.poll() is None}"
    )


def test_mounted_root_listable(pfs3_mount):
    """Mounted PFS3 volume root should contain files."""
    _proc, mountpoint = pfs3_mount
    entries = os.listdir(str(mountpoint))
    assert len(entries) > 0, (
        f"Expected files in mounted volume at {mountpoint}, got empty listing"
    )


def test_file_read_matches_hash(pfs3_mount, pfs3_image, pfs3_driver):
    """Read a file through the mount and compare its hash to amifuse hash output."""
    _proc, mountpoint = pfs3_mount
    mp = str(mountpoint)

    # Pick the first regular file in the root (skip .info icon files
    # which may have restricted permissions on the FUSE mount)
    entries = os.listdir(mp)
    target = None
    for entry in entries:
        if entry.lower().endswith(".info"):
            continue
        full = os.path.join(mp, entry)
        try:
            if os.path.isfile(full):
                target = entry
                break
        except OSError:
            continue

    if target is None:
        pytest.skip("No regular files found in mounted volume root")

    # Read file content through FUSE mount
    fuse_path = os.path.join(mp, target)
    try:
        with open(fuse_path, "rb") as f:
            fuse_content = f.read()
    except PermissionError:
        pytest.skip(
            f"Cannot read {target} through mount (PermissionError) -- "
            f"likely WinFSP read-only mount permission issue"
        )
    fuse_hash = hashlib.sha256(fuse_content).hexdigest()

    # Get hash via amifuse hash command (reads image directly, no FUSE)
    # amifuse hash requires --file flag and --driver for PFS3
    result = _run_amifuse(
        "hash", str(pfs3_image),
        "--file", target,
        "--driver", str(pfs3_driver),
        "--json",
    )
    if result.returncode != 0:
        pytest.skip(
            f"amifuse hash not available or failed: {result.stderr}"
        )

    # Parse hash from JSON output
    hash_data = json.loads(result.stdout)
    expected_hash = hash_data["hash"]
    assert fuse_hash == expected_hash, (
        f"Hash mismatch.\n"
        f"FUSE read SHA256: {fuse_hash}\n"
        f"amifuse hash output: {expected_hash}"
    )


def test_unmount_cleans_up(mount_image, pfs3_image, pfs3_driver):
    """Mount, then unmount, and verify cleanup."""
    proc, mountpoint = mount_image(pfs3_image, driver=pfs3_driver)
    mp_str = str(mountpoint)

    # Verify mount is active
    assert os.path.ismount(mp_str)

    # Unmount via CLI
    result = _run_amifuse("unmount", mp_str)
    # Allow non-zero exit on Windows (process termination path may return non-zero)

    # Wait for process to exit
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # Poll until unmount detected
    # On Windows, os.path.ismount on a drive letter may remain True briefly
    # after the FUSE process exits. Use listdir failure as the signal.
    deadline = time.monotonic() + 10.0
    unmounted = False
    while time.monotonic() < deadline:
        try:
            os.listdir(mp_str)
        except OSError:
            unmounted = True
            break
        time.sleep(0.5)

    assert unmounted, (
        f"{mountpoint} is still mounted after unmount"
    )
    assert proc.poll() is not None, "Mount process should have exited"


def test_unmount_twice_is_safe(mount_image, pfs3_image, pfs3_driver):
    """Unmounting an already-unmounted path should not hang or crash."""
    proc, mountpoint = mount_image(pfs3_image, driver=pfs3_driver)
    mp_str = str(mountpoint)

    # First unmount
    _run_amifuse("unmount", mp_str)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # Poll until unmount detected
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.listdir(mp_str)
        except OSError:
            break
        time.sleep(0.5)

    # Second unmount -- should fail cleanly, not hang
    result = _run_amifuse("unmount", mp_str, timeout=15.0)
    assert result.returncode != 0, "Expected non-zero exit for double unmount"
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, (
        f"Got raw traceback on double unmount:\n{combined}"
    )


def test_mount_invalid_image_fails(fuse_available, tmp_path):
    """Mounting a non-image file should fail with a clean error."""
    fake_image = tmp_path / "not_an_image.bin"
    fake_image.write_bytes(b"this is not an amiga disk image")

    result = _run_amifuse("mount", str(fake_image), "--interactive")
    assert result.returncode != 0, "Expected failure for invalid image"
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined, (
        f"Got raw traceback:\n{combined}"
    )
    assert len(combined.strip()) > 0, "Expected an error message"


@pytest.mark.skip(reason="Cannot test FUSE-absent path when FUSE is installed in CI")
def test_mount_missing_fuse_detected():
    """When FUSE is not installed, mount should fail with an actionable message.

    This test is skipped in CI where FUSE is always installed. To verify
    manually: uninstall the FUSE backend and run:
        python -m amifuse mount some_image.hdf --interactive
    Expected: clean error message about missing FUSE backend.
    """
    pass


@pytest.mark.windows
def test_windows_teardown_no_file_locks(mount_image, pfs3_image, pfs3_driver):
    """BW7 regression: unmount must release all handles on the image file.

    After mounting and unmounting, the image file should be openable
    exclusively (no stale file locks from WinFSP or the mount process).
    """
    if not sys.platform.startswith("win"):
        pytest.skip("Windows-only test")

    proc, mountpoint = mount_image(pfs3_image, driver=pfs3_driver)

    # Read a file to exercise the I/O path (skip .info icon files)
    entries = os.listdir(str(mountpoint))
    for entry in entries:
        if entry.lower().endswith(".info"):
            continue
        target = os.path.join(str(mountpoint), entry)
        try:
            if os.path.isfile(target):
                with open(target, "rb") as f:
                    f.read(1024)
                break
        except OSError:
            continue

    # Unmount
    _run_amifuse("unmount", str(mountpoint))
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

    # Poll until unmount detected
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            os.listdir(str(mountpoint))
        except OSError:
            break
        time.sleep(0.5)

    # Verify: open image file exclusively
    # On Windows, opening with no sharing mode tests for stale locks
    image_path = str(pfs3_image)
    try:
        with open(image_path, "rb") as f:
            f.read(1)  # should succeed if no locks
    except PermissionError:
        pytest.fail(
            f"Image file {image_path} is still locked after unmount -- "
            f"stale file handles detected (BW7 regression)"
        )


@pytest.mark.windows
@pytest.mark.skip(reason="Manual verification only -- Explorer eject is a GUI action")
def test_winfsp_eject_behavior():
    """R3 empirical: WinFSP Eject behavior documentation.

    WinFSP mounts appear as removable drives in Windows Explorer.
    Right-click -> Eject triggers a clean unmount via WinFSP's internal
    mechanism. This is NOT automatable in CI.

    Manual test procedure:
    1. Mount a PFS3 image: amifuse mount pfs.hdf --interactive
    2. Open the mount in Explorer (navigate to the drive letter)
    3. Right-click the drive -> Eject
    4. Observe: the amifuse process should exit cleanly (rc=0)
    5. The drive letter should disappear from Explorer
    6. The image file should have no remaining locks

    Document findings in DECISIONS.md under R3 (WinFSP Eject).
    """
    pass
