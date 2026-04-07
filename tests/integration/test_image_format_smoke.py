"""Pytest wrapper for tools/image_format_smoke.py.

Exercises image-format detection and mount paths as a subprocess,
validating that all format cases succeed. Requires external fixtures at
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
SCRIPT = REPO_ROOT / "tools" / "image_format_smoke.py"

_skip_no_fixtures = pytest.mark.skipif(
    not _has_external_fixtures(),
    reason="External fixtures not found at ~/AmigaOS/AmiFuse/",
)


@_skip_no_fixtures
class TestImageFormatSmoke:
    """Run image format detection smoke tests."""

    def test_image_format_smoke_all(self):
        """Run all image format cases and verify they pass."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--json"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
        )
        assert proc.returncode == 0, f"Image format smoke failed:\n{proc.stderr}"
        results = json.loads(proc.stdout)
        assert isinstance(results, list), "Expected JSON list of results"
        for result in results:
            assert result.get("status") == "ok", (
                f"Image format case failed: {result}"
            )
