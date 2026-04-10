"""CLI JSON contract tests -- validate amifuse subprocess JSON output.

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

    def test_inspect_rdb_image(self, pfs3_image):
        """inspect --json on an RDB image returns partitions."""
        proc = _run_amifuse("inspect", "--json", str(pfs3_image))
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "inspect"
        assert data["image_type"] == "rdb"
        assert "partitions" in data
        assert isinstance(data["partitions"], list)
        assert len(data["partitions"]) >= 1

    def test_inspect_adf_image(self, ofs_adf_image):
        """inspect --json on an ADF image returns floppy metadata."""
        proc = _run_amifuse("inspect", "--json", str(ofs_adf_image))
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
# B. ls --json
# ---------------------------------------------------------------------------


class TestLsJson:
    """Test the ls subcommand with --json output."""

    def test_ls_root(self, pfs3_image, pfs3_driver):
        """ls --json on root of PFS3 image returns known files."""
        proc = _run_amifuse(
            "ls", "--json",
            "--driver", str(pfs3_driver),
            str(pfs3_image),
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "ls"
        assert data["path"] == "/"
        assert "entries" in data
        assert isinstance(data["entries"], list)
        names = [e["name"] for e in data["entries"]]
        assert "foo.md" in names
        # Validate entry structure
        foo = next(e for e in data["entries"] if e["name"] == "foo.md")
        assert foo["type"] == "file"
        assert foo["size"] == 1971
        assert "protection" in foo

    def test_ls_nonexistent_path(self, pfs3_image, pfs3_driver):
        """ls --json on a nonexistent directory returns FILE_NOT_FOUND."""
        proc = _run_amifuse(
            "ls", "--json",
            "--driver", str(pfs3_driver),
            "--path", "does_not_exist",
            str(pfs3_image),
        )
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["error"]["code"] == "FILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# C. verify --json
# ---------------------------------------------------------------------------


class TestVerifyJson:
    """Test the verify subcommand with --json output."""

    def test_verify_volume(self, pfs3_image, pfs3_driver):
        """verify --json (no --file) returns volume summary."""
        proc = _run_amifuse(
            "verify", "--json",
            "--driver", str(pfs3_driver),
            str(pfs3_image),
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "verify"
        assert data["volume"] == "PFS3AIO Volume"
        assert data["total_files"] >= 1
        assert data["filesystem_responsive"] is True

    def test_verify_specific_file(self, pfs3_image, pfs3_driver):
        """verify --json --file foo.md returns file metadata."""
        proc = _run_amifuse(
            "verify", "--json",
            "--driver", str(pfs3_driver),
            "--file", "foo.md",
            str(pfs3_image),
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "verify"
        assert data["file"] == "foo.md"
        assert data["exists"] is True
        assert data["size"] == 1971
        assert data["type"] == "file"

    def test_verify_nonexistent_file(self, pfs3_image, pfs3_driver):
        """verify --json --file missing.txt returns FILE_NOT_FOUND."""
        proc = _run_amifuse(
            "verify", "--json",
            "--driver", str(pfs3_driver),
            "--file", "missing.txt",
            str(pfs3_image),
        )
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["error"]["code"] == "FILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# D. hash --json
# ---------------------------------------------------------------------------


class TestHashJson:
    """Test the hash subcommand with --json output."""

    def test_hash_known_file(self, pfs3_image, pfs3_driver):
        """hash --json --file foo.md returns stable sha256 digest."""
        proc = _run_amifuse(
            "hash", "--json",
            "--driver", str(pfs3_driver),
            "--file", "foo.md",
            str(pfs3_image),
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        assert data["command"] == "hash"
        assert data["file"] == "foo.md"
        assert data["algorithm"] == "sha256"
        assert data["size"] == 1971
        assert data["bytes_read"] == 1971
        # Verify hash is a valid 64-char hex string
        assert len(data["hash"]) == 64
        assert all(c in "0123456789abcdef" for c in data["hash"])
        # Known digest for foo.md in pfs.hdf
        assert data["hash"] == (
            "973f7effb62de137e85d8a30fa22b9cef422f7653d9aa7ccc2753834565a7006"
        )

    def test_hash_missing_file_flag(self, pfs3_image, pfs3_driver):
        """hash without --file exits with argparse error (exit 2)."""
        proc = _run_amifuse(
            "hash", "--json",
            "--driver", str(pfs3_driver),
            str(pfs3_image),
        )
        assert proc.returncode == 2  # argparse error
        assert "the following arguments are required: --file" in proc.stderr

    def test_hash_nonexistent_file(self, pfs3_image, pfs3_driver):
        """hash --json --file no_such_file returns FILE_NOT_FOUND."""
        proc = _run_amifuse(
            "hash", "--json",
            "--driver", str(pfs3_driver),
            "--file", "no_such_file.txt",
            str(pfs3_image),
        )
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["error"]["code"] == "FILE_NOT_FOUND"


# ---------------------------------------------------------------------------
# E. read command
# ---------------------------------------------------------------------------


class TestReadCommand:
    """Test the read subcommand."""

    def test_read_missing_file_flag(self, pfs3_image, pfs3_driver):
        """read without --file exits with argparse error (exit 2)."""
        proc = _run_amifuse(
            "read",
            "--driver", str(pfs3_driver),
            str(pfs3_image),
        )
        assert proc.returncode == 2
        assert "the following arguments are required: --file" in proc.stderr

    def test_read_json_with_output(self, pfs3_image, pfs3_driver):
        """read --json --file foo.md --out <tmp> extracts file and returns JSON."""
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False) as tmp:
            out_path = tmp.name

        try:
            proc = _run_amifuse(
                "read", "--json",
                "--driver", str(pfs3_driver),
                "--file", "foo.md",
                "--out", out_path,
                str(pfs3_image),
            )
            assert proc.returncode == 0
            data = _parse_json_stdout(proc)
            assert data["status"] == "ok"
            assert data["command"] == "read"
            assert data["file"] == "foo.md"
            assert data["size"] == 1971
            assert data["bytes_read"] == 1971
            # Verify the extracted file exists and has content
            extracted = Path(out_path).read_bytes()
            assert len(extracted) == 1971
        finally:
            Path(out_path).unlink(missing_ok=True)

    def test_read_stdout_json_conflict(self, pfs3_image, pfs3_driver):
        """read --json --out - is rejected as STDOUT_JSON_CONFLICT."""
        proc = _run_amifuse(
            "read", "--json",
            "--driver", str(pfs3_driver),
            "--file", "foo.md",
            "--out", "-",
            str(pfs3_image),
        )
        assert proc.returncode != 0
        data = _parse_json_stdout(proc)
        assert data["status"] == "error"
        assert data["error"]["code"] == "STDOUT_JSON_CONFLICT"

    def test_read_nonexistent_file(self, pfs3_image, pfs3_driver):
        """read --json --file no_such_file returns FILE_NOT_FOUND."""
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            out_path = tmp.name

        try:
            proc = _run_amifuse(
                "read", "--json",
                "--driver", str(pfs3_driver),
                "--file", "no_such_file.txt",
                "--out", out_path,
                str(pfs3_image),
            )
            assert proc.returncode != 0
            data = _parse_json_stdout(proc)
            assert data["status"] == "error"
            assert data["error"]["code"] == "FILE_NOT_FOUND"
        finally:
            Path(out_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# F. write --json
# ---------------------------------------------------------------------------


class TestWriteJson:
    """Test the write subcommand error paths via JSON output."""

    def test_write_missing_required_flags(self, pfs3_image, pfs3_driver):
        """write without --file and --in should exit 2 (argparse error)."""
        proc = _run_amifuse(
            "write", str(pfs3_image),
            "--driver", str(pfs3_driver),
            "--json",
        )
        assert proc.returncode == 2

    def test_write_source_not_found(self, pfs3_image, pfs3_driver):
        """write with nonexistent --in path should report JSON error."""
        proc = _run_amifuse(
            "write", str(pfs3_image),
            "--file", "/test.txt",
            "--in", "/nonexistent/source.bin",
            "--driver", str(pfs3_driver),
            "--json",
        )
        assert proc.returncode != 0
        if proc.stdout.strip():
            data = _parse_json_stdout(proc)
            assert data["status"] == "error"

    def test_write_source_is_directory(self, pfs3_image, pfs3_driver):
        """write with a directory as --in path should report error."""
        proc = _run_amifuse(
            "write", str(pfs3_image),
            "--file", "/test.txt",
            "--in", str(pfs3_image.parent),  # Use parent dir as source
            "--driver", str(pfs3_driver),
            "--json",
        )
        assert proc.returncode != 0
