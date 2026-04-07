"""Pytest wrappers for tools/amifuse_matrix.py smoke tests.

These invoke the matrix runner as a subprocess and validate its JSON output.
They require the external fixture directory at ~/AmigaOS/AmiFuse/.
"""

import json
import subprocess
import sys

import pytest
from pathlib import Path

from tests.integration.conftest import _has_external_fixtures

pytestmark = [pytest.mark.smoke, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX_SCRIPT = REPO_ROOT / "tools" / "amifuse_matrix.py"

_skip_no_fixtures = pytest.mark.skipif(
    not _has_external_fixtures(),
    reason="External fixtures not found at ~/AmigaOS/AmiFuse/",
)


@_skip_no_fixtures
class TestMatrixSmoke:
    """Run matrix smoke tests via subprocess."""

    def test_readonly_matrix_pfs3(self):
        """Run read-only PFS3 matrix check."""
        proc = subprocess.run(
            [
                sys.executable,
                str(MATRIX_SCRIPT),
                "--fixtures",
                "pfs3",
                "--runs",
                "1",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 0, f"Matrix failed:\n{proc.stderr}"
        results = json.loads(proc.stdout)
        # Matrix outputs a list of result objects
        for result in results if isinstance(results, list) else [results]:
            assert result.get("status") != "error", f"Matrix error: {result}"

    def test_readonly_matrix_default(self):
        """Run default read-only matrix (all default fixtures)."""
        proc = subprocess.run(
            [
                sys.executable,
                str(MATRIX_SCRIPT),
                "--runs",
                "1",
                "--json",
            ],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 0, f"Matrix failed:\n{proc.stderr}"
