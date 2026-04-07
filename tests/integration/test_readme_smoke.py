"""Pytest wrapper for tools/readme_smoke.py.

Exercises documented README/CLI examples as a subprocess, validating
that all example commands succeed. Requires external fixtures at
~/AmigaOS/AmiFuse/.
"""

import json
import subprocess
import sys

import pytest
from pathlib import Path

from tests.integration.conftest import _has_external_fixtures

pytestmark = [pytest.mark.smoke, pytest.mark.slow]

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "tools" / "readme_smoke.py"

_skip_no_fixtures = pytest.mark.skipif(
    not _has_external_fixtures(),
    reason="External fixtures not found at ~/AmigaOS/AmiFuse/",
)


@_skip_no_fixtures
class TestReadmeSmoke:
    """Run README/CLI example smoke tests."""

    def test_readme_smoke_all(self):
        """Run all README examples and verify they pass."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 0, f"Readme smoke failed:\n{proc.stderr}"
        results = json.loads(proc.stdout)
        assert isinstance(results, list), "Expected JSON list of results"
        for result in results:
            assert result.get("status") == "ok", f"Readme example failed: {result}"
