"""A process requesting a drive letter does not own the mounted drive."""

import subprocess
import sys

import pytest

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


def test_unmount_leaves_a_starting_process_alone(pfs3_mount, pfs3_image):
    owner, mountpoint = pfs3_mount
    starting = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)",
         "amifuse", "mount", str(pfs3_image), "--mountpoint",
         str(mountpoint)],
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "amifuse", "unmount", str(mountpoint)],
            capture_output=True, timeout=35,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        owner.wait(timeout=10)
        assert starting.poll() is None
    finally:
        starting.kill()
        starting.wait(timeout=10)
