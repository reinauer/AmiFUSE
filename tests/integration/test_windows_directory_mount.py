"""WinFsp creates a directory mountpoint; its parent must already exist."""

import os
import subprocess
import sys
import time

import pytest

from tests.integration.test_bundled_ffs import _seed_adf

pytestmark = [pytest.mark.fuse,
              pytest.mark.skipif(sys.platform != "win32", reason="WinFsp")]


def test_directory_mount_and_unmount_from_another_directory(tmp_path, fuse_available):
    image = _seed_adf(tmp_path, "DOS3")
    mountpoint = tmp_path / "mount"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "amifuse", "mount", str(image),
         "--interactive", "--mountpoint", str(mountpoint)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                pytest.fail(f"Mount exited: {out!r} {err!r}")
            try:
                if "Root.txt" in os.listdir(mountpoint):
                    break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("Directory mount never became readable")
        result = subprocess.run(
            [sys.executable, "-m", "amifuse", "unmount", str(mountpoint)],
            cwd=elsewhere, capture_output=True, timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        proc.wait(timeout=10)
        assert not mountpoint.exists()
    finally:
        if proc.poll() is None:
            subprocess.run(
                [sys.executable, "-m", "amifuse", "unmount", str(mountpoint)],
                capture_output=True, timeout=20,
            )
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
