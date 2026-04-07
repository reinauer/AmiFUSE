"""CLI command integration tests -- validate JSON output via subprocess.

Tests run `amifuse` as a subprocess and validate JSON envelopes for
commands that support --json.  Error paths (nonexistent images, missing
required flags) are also exercised.
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from tests.fixtures.paths import PFS3AIO, PFS3_8MB_HDF, BLANK_ADF

pytestmark = pytest.mark.integration

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


def _parse_json_stdout(proc: subprocess.CompletedProcess) -> dict:
    """Extract the JSON object from stdout, ignoring vamos debug noise.

    The vamos m68k emulator may print 'can't expunge ...' and 'orphan: ...'
    lines to stdout before the actual JSON.  This helper finds the first
    '{' and parses from there.
    """
    text = proc.stdout
    idx = text.find("{")
    assert idx != -1, (
        f"No JSON object found in stdout.\n"
        f"stdout: {text!r}\nstderr: {proc.stderr!r}"
    )
    return json.loads(text[idx:])


# ---------------------------------------------------------------------------
# A. inspect --json
# ---------------------------------------------------------------------------


class TestInspectJson:
    """Test the inspect subcommand with --json output."""

    def test_inspect_rdb_image(self):
        """inspect --json on an RDB image returns partitions."""
        proc = _run_amifuse("inspect", "--json", str(PFS3_8MB_HDF))
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "inspect"
        assert data["image_type"] == "rdb"
        assert "partitions" in data
        assert isinstance(data["partitions"], list)
        assert len(data["partitions"]) >= 1

    def test_inspect_adf_image(self):
        """inspect --json on an ADF image returns floppy metadata."""
        proc = _run_amifuse("inspect", "--json", str(BLANK_ADF))
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "inspect"
        assert data["image_type"] == "adf"
        assert data["floppy_type"] in ("DD", "HD")
        assert "block_size" in data
        assert "total_blocks" in data
        assert "dos_type" in data

    def test_inspect_nonexistent_image(self):
        """inspect --json on a missing file returns JSON error with exit 1."""
        proc = _run_amifuse("inspect", "--json", "no_such_image.hdf")
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["command"] == "inspect"
        assert data["error"]["code"] == "IMAGE_NOT_FOUND"


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
        data = _parse_json_stdout(proc)
        assert data["command"] == "doctor"
        assert "checks" in data
        assert "overall" in data
        assert data["overall"] in ("ready", "degraded", "not_ready")
        # Core checks are always present
        for key in ("python", "amitools", "machine68k"):
            assert key in data["checks"], f"Missing core check: {key}"
            assert "ok" in data["checks"][key]


# ---------------------------------------------------------------------------
# C. ls --json
# ---------------------------------------------------------------------------


class TestLsJson:
    """Test the ls subcommand with --json output."""

    def test_ls_root(self):
        """ls --json on root of 8MB PFS3 image returns hello.txt."""
        proc = _run_amifuse(
            "ls", "--json",
            "--driver", str(PFS3AIO),
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "ls"
        assert data["path"] == "/"
        assert "entries" in data
        assert isinstance(data["entries"], list)
        names = [e["name"] for e in data["entries"]]
        assert "hello.txt" in names
        # Validate entry structure
        hello = next(e for e in data["entries"] if e["name"] == "hello.txt")
        assert hello["type"] == "file"
        assert hello["size"] == 19
        assert "protection" in hello

    def test_ls_nonexistent_path(self):
        """ls --json on a nonexistent directory returns FILE_NOT_FOUND."""
        proc = _run_amifuse(
            "ls", "--json",
            "--driver", str(PFS3AIO),
            "--path", "does_not_exist",
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["error"]["code"] == "FILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# D. verify --json
# ---------------------------------------------------------------------------


class TestVerifyJson:
    """Test the verify subcommand with --json output."""

    def test_verify_volume(self):
        """verify --json (no --file) returns volume summary."""
        proc = _run_amifuse(
            "verify", "--json",
            "--driver", str(PFS3AIO),
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "verify"
        assert data["volume"] == "TestVol"
        assert data["total_files"] >= 1
        assert data["filesystem_responsive"] is True

    def test_verify_specific_file(self):
        """verify --json --file hello.txt returns file metadata."""
        proc = _run_amifuse(
            "verify", "--json",
            "--driver", str(PFS3AIO),
            "--file", "hello.txt",
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "verify"
        assert data["file"] == "hello.txt"
        assert data["exists"] is True
        assert data["size"] == 19
        assert data["type"] == "file"

    def test_verify_nonexistent_file(self):
        """verify --json --file missing.txt returns FILE_NOT_FOUND."""
        proc = _run_amifuse(
            "verify", "--json",
            "--driver", str(PFS3AIO),
            "--file", "missing.txt",
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["error"]["code"] == "FILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# E. hash --json
# ---------------------------------------------------------------------------


class TestHashJson:
    """Test the hash subcommand with --json output."""

    def test_hash_known_file(self):
        """hash --json --file hello.txt returns stable sha256 digest."""
        proc = _run_amifuse(
            "hash", "--json",
            "--driver", str(PFS3AIO),
            "--file", "hello.txt",
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "hash"
        assert data["file"] == "hello.txt"
        assert data["algorithm"] == "sha256"
        assert data["size"] == 19
        assert data["bytes_read"] == 19
        # The hash is deterministic for a fixed fixture file.
        assert data["hash"] == (
            "c027f025c0899cf90aeaaaa6c1d25fffef776cf7940c48c649c362e3b9fdb4f7"
        )

    def test_hash_missing_file_flag(self):
        """hash without --file exits with argparse error (exit 2)."""
        proc = _run_amifuse(
            "hash", "--json",
            "--driver", str(PFS3AIO),
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode == 2  # argparse error
        assert "the following arguments are required: --file" in proc.stderr

    def test_hash_nonexistent_file(self):
        """hash --json --file no_such_file returns FILE_NOT_FOUND."""
        proc = _run_amifuse(
            "hash", "--json",
            "--driver", str(PFS3AIO),
            "--file", "no_such_file.txt",
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["error"]["code"] == "FILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# F. read command
# ---------------------------------------------------------------------------


class TestReadCommand:
    """Test the read subcommand."""

    def test_read_missing_file_flag(self):
        """read without --file exits with argparse error (exit 2)."""
        proc = _run_amifuse(
            "read",
            "--driver", str(PFS3AIO),
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode == 2
        assert "the following arguments are required: --file" in proc.stderr

    def test_read_json_with_output(self):
        """read --json --file hello.txt --out <tmp> extracts file and returns JSON."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            out_path = tmp.name

        try:
            proc = _run_amifuse(
                "read", "--json",
                "--driver", str(PFS3AIO),
                "--file", "hello.txt",
                "--out", out_path,
                str(PFS3_8MB_HDF),
            )
            assert proc.returncode == 0
            data = _parse_json_stdout(proc)
            assert data["status"] == "ok"
            assert data["command"] == "read"
            assert data["file"] == "hello.txt"
            assert data["size"] == 19
            assert data["bytes_read"] == 19
            # Verify the extracted file exists and has content
            extracted = Path(out_path).read_bytes()
            assert len(extracted) == 19
        finally:
            Path(out_path).unlink(missing_ok=True)

    def test_read_stdout_json_conflict(self):
        """read --json --out - is rejected as STDOUT_JSON_CONFLICT."""
        proc = _run_amifuse(
            "read", "--json",
            "--driver", str(PFS3AIO),
            "--file", "hello.txt",
            "--out", "-",
            str(PFS3_8MB_HDF),
        )
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["error"]["code"] == "STDOUT_JSON_CONFLICT"

    def test_read_nonexistent_file(self):
        """read --json --file no_such_file returns FILE_NOT_FOUND."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            out_path = tmp.name

        try:
            proc = _run_amifuse(
                "read", "--json",
                "--driver", str(PFS3AIO),
                "--file", "no_such_file.txt",
                "--out", out_path,
                str(PFS3_8MB_HDF),
            )
            assert proc.returncode != 0
            data = _parse_json_stdout(proc)
            assert data["status"] == "error"
            assert data["error"]["code"] == "FILE_NOT_FOUND"
        finally:
            Path(out_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# G. write --json
# ---------------------------------------------------------------------------


class TestWriteJson:
    """Test the write subcommand error paths via JSON output."""

    def test_write_missing_required_flags(self):
        """write without --file and --in should exit 2 (argparse error)."""
        proc = _run_amifuse(
            "write", str(PFS3_8MB_HDF),
            "--driver", str(PFS3AIO),
            "--json",
        )
        assert proc.returncode == 2

    def test_write_source_not_found(self):
        """write with nonexistent --in path should report JSON error."""
        proc = _run_amifuse(
            "write", str(PFS3_8MB_HDF),
            "--file", "/test.txt",
            "--in", "/nonexistent/source.bin",
            "--driver", str(PFS3AIO),
            "--json",
        )
        assert proc.returncode != 0
        if proc.stdout.strip():
            data = _parse_json_stdout(proc)
            assert data["status"] == "error"

    def test_write_source_is_directory(self):
        """write with a directory as --in path should report error."""
        proc = _run_amifuse(
            "write", str(PFS3_8MB_HDF),
            "--file", "/test.txt",
            "--in", str(PFS3_8MB_HDF.parent),  # Use images/ dir as source
            "--driver", str(PFS3AIO),
            "--json",
        )
        assert proc.returncode != 0


# ---------------------------------------------------------------------------
# H. --help for all subcommands
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
