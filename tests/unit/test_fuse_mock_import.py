"""Import the mount callbacks under their fixture without collection side effects."""

import subprocess
import sys
from pathlib import Path


def test_fuse_mock_imports_callbacks_in_a_fresh_interpreter():
    script = """
import sys
import pytest
from tests.conftest import fuse_mock

assert 'amifuse.fuse_fs' not in sys.modules
with pytest.MonkeyPatch.context() as patch:
    fixture = fuse_mock.__wrapped__(patch)
    next(fixture)
    try:
        from amifuse.fuse_fs import AmigaFuseFS, RunResult
        assert len({RunResult.BUSY, RunResult.IDLE, RunResult.DONE}) == 3
    finally:
        next(fixture, None)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
