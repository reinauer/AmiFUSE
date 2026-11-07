"""Successful CLI handler runs keep normal stderr empty."""
import subprocess
import sys

import pytest
from amifuse.driver_resolver import BUNDLED_DRIVER
from tests.integration.test_bundled_ffs import _seed_adf


@pytest.mark.parametrize("command", ["ls", "verify"])
@pytest.mark.parametrize("debug", [False, True])
def test_cli_teardown_logging(tmp_path, command, debug):
    image = _seed_adf(tmp_path, "DOS3")
    args = [sys.executable, "-m", "amifuse", command, str(image),
            "--driver", str(BUNDLED_DRIVER)]
    if debug:
        args.append("--debug")
    result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    if debug:
        assert "orphan:" in result.stderr or "can't expunge" in result.stderr
    else:
        assert result.stderr == ""
