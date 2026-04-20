"""Integration test for `amifuse status --json` CLI output.

Runs amifuse as a subprocess and validates the JSON envelope.
"""

import json
import subprocess
import sys

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


class TestStatusCli:
    """Validate amifuse status --json subprocess output."""

    def test_status_json_exit_code(self):
        proc = _run_amifuse("status", "--json")
        assert proc.returncode == 0, (
            f"Exit code {proc.returncode}\nstderr: {proc.stderr}")

    def test_status_json_valid_output(self):
        proc = _run_amifuse("status", "--json")
        data = json.loads(proc.stdout)
        assert data["status"] == "ok"
        assert data["command"] == "status"
        assert isinstance(data["mounts"], list)

    def test_status_json_mount_schema(self):
        """If any mounts are present, validate field presence."""
        proc = _run_amifuse("status", "--json")
        data = json.loads(proc.stdout)
        required_keys = {"mountpoint", "image", "pid",
                         "uptime_seconds", "filesystem_type"}
        for mount in data["mounts"]:
            assert required_keys.issubset(mount.keys()), (
                f"Missing keys in mount entry: "
                f"{required_keys - mount.keys()}")

    def test_status_text_mode(self):
        """Text mode (no --json) should also exit 0."""
        proc = _run_amifuse("status")
        assert proc.returncode == 0
