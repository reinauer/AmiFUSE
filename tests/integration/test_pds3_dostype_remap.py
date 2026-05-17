"""End-to-end test for the PDS\\3 -> PFS\\3 dostype remap.

Skip-gated on the presence of ``testdisk.hdf`` in the repository root. That
image uses ``PDS\\3`` dostype for its DH0 partition; without the remap,
``amifuse ls`` returns an empty entry list (see PDS3_MOUNT_BUG.md).

When the test fixture is missing the tests skip cleanly, so CI without
the test image stays green.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTDISK_HDF = REPO_ROOT / "testdisk.hdf"


def _require_testdisk():
    if not TESTDISK_HDF.exists():
        pytest.skip(f"testdisk.hdf not present at {TESTDISK_HDF}; "
                    "drop a PDS\\3 image there to enable this test")


def _run_amifuse(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "amifuse", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _parse_json_stdout(proc: subprocess.CompletedProcess) -> dict:
    text = proc.stdout
    idx = text.find("{")
    assert idx != -1, (
        f"No JSON object found in stdout.\nstdout: {text!r}\nstderr: {proc.stderr!r}"
    )
    return json.loads(text[idx:])


class TestPds3DostypeRemap:
    """Validates the PDS\\3 -> PFS\\3 remap path on a real image."""

    def test_inspect_reports_remap(self):
        _require_testdisk()
        proc = _run_amifuse("inspect", "--json", str(TESTDISK_HDF))
        assert proc.returncode == 0, (
            f"inspect failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        remap = data.get("dostype_remap")
        assert remap is not None, (
            "Expected dostype_remap envelope on inspect of testdisk.hdf "
            "(its DH0 partition uses PDS\\3)."
        )
        assert remap["strict"] is False
        parts = remap["partitions"]
        assert any(p["original"].startswith("PDS\\") for p in parts)
        assert all(p["remapped"].startswith("PFS\\") for p in parts)

    def test_inspect_strict_flag_changes_envelope(self):
        _require_testdisk()
        proc = _run_amifuse(
            "inspect", "--json", "--strict-dostype", str(TESTDISK_HDF)
        )
        assert proc.returncode == 0
        data = _parse_json_stdout(proc)
        remap = data.get("dostype_remap")
        assert remap is not None
        assert remap["strict"] is True

    def test_ls_dh0_lists_entries(self, pfs3_driver):
        _require_testdisk()
        proc = _run_amifuse(
            "ls", "--json",
            "--driver", str(pfs3_driver),
            "--partition", "DH0",
            str(TESTDISK_HDF),
        )
        assert proc.returncode == 0, (
            f"ls failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
        data = _parse_json_stdout(proc)
        assert data["status"] == "ok"
        entries = data.get("entries", [])
        assert len(entries) > 0, (
            "Empty entry list on testdisk.hdf DH0 — the PDS\\3 -> PFS\\3 "
            "remap did not take effect (see PDS3_MOUNT_BUG.md)."
        )

    def test_ls_dh0_strict_dostype_fails_or_empty(self, pfs3_driver):
        """--strict-dostype reverts to the broken ACCESS_DS path.

        Until Option A lands (proper HD_SCSICMD fix), mounting PDS\\3 with
        --strict-dostype either hangs the handler or yields an empty listing.
        We accept either outcome here — this test exists to lock in the
        regression direction (strict-on => degraded, strict-off => works).
        """
        _require_testdisk()
        proc = _run_amifuse(
            "ls", "--json", "--strict-dostype",
            "--driver", str(pfs3_driver),
            "--partition", "DH0",
            str(TESTDISK_HDF),
            timeout=45.0,
        )
        if proc.returncode != 0:
            return  # acceptable: handler refused / crashed under ACCESS_DS
        data = _parse_json_stdout(proc)
        entries = data.get("entries", [])
        assert len(entries) == 0, (
            "Strict-dostype mount of PDS\\3 unexpectedly listed entries. "
            "If ACCESS_DS now works, update this test and revisit the "
            "Option A follow-up in PDS3_MOUNT_BUG.md."
        )
