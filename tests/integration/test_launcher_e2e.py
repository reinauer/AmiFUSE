"""End-to-end launcher tests (require WinFSP and test fixtures)."""

import os
import subprocess
import sys
import time

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Windows-only"),
    pytest.mark.fuse,
    pytest.mark.slow,
    pytest.mark.integration,
]

DETACHED_FLAGS = 0x00000008 | 0x00000200 | 0x08000000


@pytest.fixture(autouse=True)
def require_winfsp():
    """Skip if WinFSP is not installed."""
    try:
        from amifuse.platform import _get_winfsp_install_dir
        if not _get_winfsp_install_dir():
            pytest.skip("WinFSP not installed")
    except Exception:
        pytest.skip("WinFSP not available")


def _find_free_drive() -> str:
    """Find first available drive letter, return as 'X:'."""
    for letter in "ZYXWVUTSRQPONMLKJIHGFED":
        drive = f"{letter}:"
        if not os.path.exists(drive + "\\"):
            return drive
    pytest.skip("No free drive letter available")


def _wait_for_mount(drive: str, timeout: float = 15.0) -> bool:
    """Wait until os.listdir(drive) succeeds (filesystem ready)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.listdir(drive + "\\")
            return True
        except OSError:
            time.sleep(0.5)
    return False


def _wait_for_unmount(drive: str, timeout: float = 10.0) -> bool:
    """Wait until drive letter disappears."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(drive + "\\"):
            return True
        time.sleep(0.5)
    return False


class TestLauncherE2E:
    """Full mount/unmount cycle tests."""

    def test_launcher_mount_creates_drive_letter(self, ofs_adf_image):
        """Mounting a test image should make a drive letter appear."""
        from amifuse.platform import kill_pids

        drive = _find_free_drive()
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "amifuse", "mount",
                "--mountpoint", drive,
                str(ofs_adf_image),
            ],
            creationflags=DETACHED_FLAGS,
        )
        try:
            assert _wait_for_mount(drive), f"Drive {drive} did not become ready"
        finally:
            kill_pids([proc.pid], timeout=5.0)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            _wait_for_unmount(drive)

    def test_launcher_mount_visible_in_status(self, ofs_adf_image):
        """After mounting, find_amifuse_mounts() should include the mount."""
        from amifuse.platform import find_amifuse_mounts, kill_pids

        drive = _find_free_drive()
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "amifuse", "mount",
                "--mountpoint", drive,
                str(ofs_adf_image),
            ],
            creationflags=DETACHED_FLAGS,
        )
        try:
            assert _wait_for_mount(drive), f"Drive {drive} did not become ready"

            mounts = find_amifuse_mounts()
            mountpoints = [m["mountpoint"] for m in mounts]
            assert drive in mountpoints, (
                f"Expected {drive} in mounts, got: {mountpoints}"
            )
        finally:
            kill_pids([proc.pid], timeout=5.0)
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            _wait_for_unmount(drive)

    def test_launcher_unmount_cleans_up(self, ofs_adf_image):
        """After kill_pids, drive letter should disappear."""
        from amifuse.platform import kill_pids

        drive = _find_free_drive()
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "amifuse", "mount",
                "--mountpoint", drive,
                str(ofs_adf_image),
            ],
            creationflags=DETACHED_FLAGS,
        )
        try:
            assert _wait_for_mount(drive), f"Drive {drive} did not become ready"
        except AssertionError:
            # Clean up even if mount failed
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
            raise

        killed = kill_pids([proc.pid], timeout=5.0)
        assert proc.pid in killed

        assert _wait_for_unmount(drive), f"Drive {drive} did not disappear after unmount"

        # Final safety net
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
