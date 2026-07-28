"""CLI help and doctor unit tests -- no machine68k or fixtures required.

These tests validate CLI surface area (--help, --version, doctor --json)
without needing external fixtures or a working m68k emulator.
They run on all platforms including Windows.
"""

import json
import os
import subprocess
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_amifuse(
    *args: str,
    timeout: float = 30.0,
    env=None,
) -> subprocess.CompletedProcess:
    """Run amifuse as a subprocess and return the CompletedProcess."""
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )


def _fuse_import_probe(tmp_path):
    """Return an environment where importing fuse leaves a marker and fails."""
    marker = tmp_path / "fuse-imported"
    (tmp_path / "fuse.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['AMIFUSE_FUSE_IMPORT_MARKER']).touch()\n"
        "raise RuntimeError('fuse import probe')\n"
    )
    env = os.environ.copy()
    env["AMIFUSE_FUSE_IMPORT_MARKER"] = str(marker)
    pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(tmp_path)
        if not pythonpath
        else os.pathsep.join((str(tmp_path), pythonpath))
    )
    return env, marker


# ---------------------------------------------------------------------------
# A. --help for all subcommands
# ---------------------------------------------------------------------------


ALL_SUBCOMMANDS = [
    "inspect", "mount", "unmount", "doctor", "format",
    "ls", "verify", "hash", "read", "write",
    "register", "unregister",
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


class TestLazyFuseImport:
    """Only an actual mount may import fusepy and its native library."""

    @pytest.mark.parametrize(
        "args, expected_returncode",
        [
            (("--version",), 0),
            (("mount", "--help"), 0),
            (("status", "--json"), 0),
            (("ls", "--json", "missing.hdf"), 1),
        ],
    )
    def test_non_mount_cli_does_not_import_fusepy(
        self, tmp_path, args, expected_returncode
    ):
        env, marker = _fuse_import_probe(tmp_path)

        proc = _run_amifuse(*args, env=env)

        assert proc.returncode == expected_returncode, proc.stderr
        assert not marker.exists()

    def test_doctor_does_not_import_fusepy(self, tmp_path):
        env, marker = _fuse_import_probe(tmp_path)

        proc = _run_amifuse("doctor", "--json", env=env)

        assert proc.returncode in (0, 1, 2)
        assert not marker.exists()

    def test_mount_imports_fusepy(self, tmp_path):
        env, marker = _fuse_import_probe(tmp_path)

        proc = _run_amifuse("mount", "missing.hdf", env=env)

        assert proc.returncode != 0
        assert marker.exists()
        assert "fuse import probe" in proc.stderr


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
        assert "checks" in data
        assert "overall_status" in data
        assert data["overall_status"] in ("ready", "degraded", "not_ready")
        # checks is a list of dicts
        assert isinstance(data["checks"], list)
        check_names = {c["name"] for c in data["checks"]}
        for name in ("python", "amitools", "machine68k"):
            assert name in check_names, f"Missing core check: {name}"
        for check in data["checks"]:
            assert "status" in check
            assert "name" in check
