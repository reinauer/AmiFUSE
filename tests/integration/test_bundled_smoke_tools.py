"""FFS/OFS smoke tools run from an empty external-fixture directory."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.no_fixtures]
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('tool,args', [
    ('image_format_smoke.py', ['--case', 'adf-ofs']),
    ('image_format_smoke.py', ['--case', 'emu68-ffs']),
    ('amifuse_matrix.py', ['--worker', 'ffs']),
    ('amifuse_matrix.py', ['--worker', 'ofs']),
    ('amifuse_matrix.py', ['--worker', 'ffs-rw']),
    ('amifuse_matrix.py', ['--worker', 'ofs-rw']),
])
def test_generated_ffs_tool(tmp_path, tool, args):
    env = dict(os.environ, AMIFUSE_FIXTURE_ROOT=str(tmp_path))
    proc = subprocess.run(
        [sys.executable, str(ROOT / 'tools' / tool), *args, '--json'],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    result = json.loads(proc.stdout)
    assert result['status'] == 'ok', result
    assert not (tmp_path / 'drivers').exists()
    assert not (tmp_path / 'fixtures').exists()
