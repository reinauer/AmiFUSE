"""Integration tests for `amifuse unmount` CLI error paths.

These tests exercise argument validation and error messaging without
requiring FUSE or real mounts -- they run on all platforms.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


def _run_amifuse(*args, timeout=30.0):
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class TestUnmountCli:
    """Validate amifuse unmount error paths."""

    def test_unmount_no_arguments(self):
        """unmount with no args should fail with usage/argument error."""
        proc = _run_amifuse("unmount")
        assert proc.returncode != 0
        # argparse prints usage to stderr
        assert "mountpoint" in proc.stderr.lower() or "usage" in proc.stderr.lower(), (
            f"Expected usage/argument error, got:\n"
            f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
        )

    def test_unmount_nonexistent_path(self):
        """unmount on a path that doesn't exist should fail cleanly."""
        proc = _run_amifuse("unmount", "/nonexistent/path/nowhere")
        assert proc.returncode != 0
        combined = proc.stdout + proc.stderr
        # Should not be a raw traceback -- look for a clean error message
        assert "Traceback" not in combined, (
            f"Got a raw traceback instead of a clean error:\n{combined}"
        )
        assert len(combined.strip()) > 0, "Expected an error message, got nothing"

    def test_unmount_non_mounted_path(self):
        """unmount on an existing but non-mounted directory should fail cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = _run_amifuse("unmount", tmpdir)
            assert proc.returncode != 0
            combined = proc.stdout + proc.stderr
            assert "Traceback" not in combined, (
                f"Got a raw traceback instead of a clean error:\n{combined}"
            )
            assert len(combined.strip()) > 0, "Expected an error message, got nothing"
