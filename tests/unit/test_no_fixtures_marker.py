"""The no_fixtures marker: a test that needs machine68k but no fixture root.

A checkout with AmiFUSE-testing beside it always finds a fixture root, so each
test copies the real conftest.py and paths.py into a tree under tmp_path that
has none, with a stand-in for machine68k, and runs pytest there.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

WORKING_M68K = "class CPU:\n    def __init__(self, cpu_type):\n        pass\n"
BROKEN_M68K = "import os\n\nclass CPU:\n    def __init__(self, cpu_type):\n        os._exit(139)\n"

TESTS = """
import pytest

pytestmark = pytest.mark.integration


def test_needs_fixtures():
    pass


@pytest.mark.no_fixtures
def test_needs_only_the_cpu():
    pass
"""


def _run(tmp_path, m68k):
    root = tmp_path / "AmiFUSE"
    for package in ("tests/fixtures", "tests/integration"):
        (root / package).mkdir(parents=True)
    for name in ("tests/__init__.py", "tests/fixtures/__init__.py",
                 "tests/fixtures/paths.py", "tests/integration/__init__.py",
                 "tests/integration/conftest.py"):
        shutil.copy(REPO_ROOT / name, root / name)
    (root / "machine68k.py").write_text(m68k)
    (root / "tests/integration/test_markers.py").write_text(TESTS)

    home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AMIFUSE_")}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONPATH=str(root))
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--rootdir",
         str(root), "-q", "-rs", "tests/integration/test_markers.py"],
        cwd=root, env=env, capture_output=True, text=True, timeout=60,
    )


def test_it_runs_without_a_fixture_root(tmp_path):
    result = _run(tmp_path, WORKING_M68K)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout
    assert "No fixture root found" in result.stdout


def test_it_skips_without_a_working_cpu(tmp_path):
    """It says why: the fixtures aren't what this test is missing."""
    result = _run(tmp_path, BROKEN_M68K)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 skipped" in result.stdout
    assert "machine68k CPU not functional" in result.stdout
