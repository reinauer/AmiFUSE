"""CLI help and doctor unit tests -- no machine68k or fixtures required.

These tests validate CLI surface area (--help, --version, doctor --json)
without needing external fixtures or a working m68k emulator.
They run on all platforms including Windows.
"""

import json
import subprocess
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_amifuse(*args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
    """Run amifuse as a subprocess and return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ---------------------------------------------------------------------------
# A. --help for all subcommands
# ---------------------------------------------------------------------------


ALL_SUBCOMMANDS = [
    "inspect", "mount", "unmount", "doctor", "format",
    "ls", "verify", "hash", "read", "write",
]


class TestHelpOutput:
    """Every subcommand should respond to --help with exit 0."""

    @pytest.mark.parametrize("subcommand", ALL_SUBCOMMANDS)
    def test_help_exits_zero(self, subcommand):
        """--help for '{subcommand}' should exit 0 and print usage."""
        proc = _run_amifuse(subcommand, "--help")
        assert proc.returncode == 0, (
            f"'{subcommand} --help' returned {proc.returncode}\n"
            f"stderr: {proc.stderr}"
        )
        assert "usage:" in proc.stdout.lower(), (
            f"'{subcommand} --help' missing usage text.\n"
            f"stdout: {proc.stdout[:200]}"
        )

    def test_main_help_exits_zero(self):
        """amifuse --help should exit 0."""
        proc = _run_amifuse("--help")
        assert proc.returncode == 0
        assert "usage:" in proc.stdout.lower()

    def test_version_flag(self):
        """amifuse --version should exit 0 and print version."""
        proc = _run_amifuse("--version")
        assert proc.returncode == 0
        # Version output contains "amifuse" and a version string
        combined = proc.stdout + proc.stderr  # argparse may use either
        assert "amifuse" in combined.lower()


# ---------------------------------------------------------------------------
# B. doctor --json
# ---------------------------------------------------------------------------


class TestDoctorJson:
    """Test the doctor subcommand with --json output."""

    def test_doctor_json_structure(self):
        """doctor --json returns checks dict and overall status."""
        proc = _run_amifuse("doctor", "--json")
        # doctor may exit non-zero if some checks fail (e.g. FUSE missing);
        # but the JSON envelope should still be valid.
        text = proc.stdout
        idx = text.find("{")
        assert idx != -1, (
            f"No JSON object found in stdout.\n"
            f"stdout: {text!r}\nstderr: {proc.stderr!r}"
        )
        data = json.loads(text[idx:])
        assert data["command"] == "doctor"
        assert "checks" in data
        assert "overall" in data
        assert data["overall"] in ("ready", "degraded", "not_ready")
        # Core checks are always present
        for key in ("python", "amitools", "machine68k"):
            assert key in data["checks"], f"Missing core check: {key}"
            assert "ok" in data["checks"][key]
