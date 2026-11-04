"""AMIFUSE_REQUIRE_M68K and AMIFUSE_REQUIRE_FUSE end a run that would skip.

Each test runs pytest on a small test file with tests/integration/conftest.py
as a plugin, and with stand-ins for machine68k and fusepy on PYTHONPATH.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

WORKING_M68K = "class CPU:\n    def __init__(self, cpu_type):\n        pass\n"
BROKEN_M68K = "import os\n\nclass CPU:\n    def __init__(self, cpu_type):\n        os._exit(139)\n"
WORKING_FUSE = "FUSE = object\n"
NO_LIBFUSE = 'raise OSError("Unable to find libfuse")\n'

MARKED_TESTS = """
import pytest

pytestmark = pytest.mark.integration


def test_uses_the_cpu():
    pass


def test_uses_fuse(fuse_available):
    pass
"""

# As test_bundled_ffs.py: not integration-marked, with a probe of its own.
PROBING_TEST = """
import pytest


def test_uses_the_cpu():
    from tests.integration.conftest import _machine68k_works

    if not _machine68k_works():
        pytest.skip("machine68k CPU not functional")
"""


def _run(tmp_path, m68k, fuse, tests=MARKED_TESTS, **required):
    stand_ins = tmp_path / "stand_ins"
    stand_ins.mkdir(exist_ok=True)
    (stand_ins / "machine68k.py").write_text(m68k)
    (stand_ins / "fuse.py").write_text(fuse)
    # fuse_available also looks for this on Linux.
    fusermount = stand_ins / "fusermount"
    fusermount.write_text("#!/bin/sh\n")
    fusermount.chmod(fusermount.stat().st_mode | stat.S_IXUSR)

    test_file = tmp_path / "test_backends.py"
    test_file.write_text(tests)

    env = {k: v for k, v in os.environ.items() if not k.startswith("AMIFUSE_")}
    # A folder of its own, so the outcome is the same with or without the
    # fixtures, which only decide whether a marked test skips.
    env["AMIFUSE_FIXTURE_ROOT"] = str(tmp_path)
    env["PATH"] = os.pathsep.join([str(stand_ins), env.get("PATH", "")])
    env["PYTHONPATH"] = os.pathsep.join([str(stand_ins), str(REPO_ROOT)])
    env.update(required)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.integration.conftest",
         "-p", "no:cacheprovider", "--rootdir", str(tmp_path), "-q", "-rs",
         str(test_file)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


class TestWithoutTheVariables:
    """Elsewhere a missing backend still skips, as it always has."""

    def test_a_broken_cpu_skips(self, tmp_path):
        result = _run(tmp_path, BROKEN_M68K, WORKING_FUSE)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 skipped" in result.stdout
        assert "machine68k CPU not functional" in result.stdout

    def test_a_missing_fuse_library_skips(self, tmp_path):
        result = _run(tmp_path, WORKING_M68K, NO_LIBFUSE)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "1 passed, 1 skipped" in result.stdout
        assert "FUSE backend not available: Unable to find libfuse" in result.stdout


class TestWithTheVariables:
    def test_a_broken_cpu_fails_the_run(self, tmp_path):
        result = _run(tmp_path, BROKEN_M68K, WORKING_FUSE, AMIFUSE_REQUIRE_M68K="1")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "AMIFUSE_REQUIRE_M68K is set, but the machine68k CPU" in result.stdout
        assert "skipped" not in result.stdout
        assert "passed" not in result.stdout

    def test_a_broken_cpu_fails_a_run_of_unmarked_tests(self, tmp_path):
        """The modules with their own probe skip as the marked ones do."""
        result = _run(tmp_path, BROKEN_M68K, WORKING_FUSE, tests=PROBING_TEST,
                      AMIFUSE_REQUIRE_M68K="1")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "skipped" not in result.stdout

    @pytest.mark.parametrize("fuse, reason", [
        (NO_LIBFUSE, "FUSE backend not available: Unable to find libfuse"),
        ('raise ImportError("no fusepy")\n', "fusepy not installed"),
    ], ids=["no-library", "no-fusepy"])
    def test_a_missing_fuse_backend_fails_the_run(self, tmp_path, fuse, reason):
        result = _run(tmp_path, WORKING_M68K, fuse, AMIFUSE_REQUIRE_FUSE="1")
        assert result.returncode == 1, result.stdout + result.stderr
        assert f"AMIFUSE_REQUIRE_FUSE is set: {reason}" in result.stdout
        assert "skipped" not in result.stdout

    @pytest.mark.skipif(not sys.platform.startswith("linux"),
                        reason="fusermount is looked for only on Linux")
    def test_no_fusermount_fails_the_run_on_linux(self, tmp_path):
        no_which = "import shutil\nshutil.which = lambda name: None\n"
        result = _run(tmp_path, WORKING_M68K, no_which + WORKING_FUSE,
                      AMIFUSE_REQUIRE_FUSE="1")
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Neither fusermount nor fusermount3" in result.stdout

    def test_working_backends_pass(self, tmp_path):
        result = _run(tmp_path, WORKING_M68K, WORKING_FUSE,
                      AMIFUSE_REQUIRE_M68K="1", AMIFUSE_REQUIRE_FUSE="1")
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2 passed" in result.stdout


def test_ci_requires_what_each_job_needs():
    """No step in CI may fail without failing its job, and the two jobs that
    run the tests set the variables for the backends they install."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "continue-on-error" not in workflow
    integration = workflow.split("  integration-tests:")[1].split("  tools-smoke:")[0]
    assert 'AMIFUSE_REQUIRE_M68K: "1"' in integration
    # That job doesn't install fusepy; its fuse tests are for the mount job.
    assert "AMIFUSE_REQUIRE_FUSE" not in integration
    mount = workflow.split("  mount-tests:")[1]
    assert 'AMIFUSE_REQUIRE_M68K: "1"' in mount
    assert 'AMIFUSE_REQUIRE_FUSE: "1"' in mount
