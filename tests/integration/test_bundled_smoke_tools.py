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


def test_generated_emu68_loads_the_embedded_handler(tmp_path, monkeypatch):
    from tools.generated_ffs import make_emu68, BUNDLED_FFS
    from amifuse.fuse_fs import resolve_driver
    from amifuse import driver_resolver
    monkeypatch.setattr(driver_resolver, 'user_driver_dir', lambda: tmp_path / 'drivers')
    image = make_emu68(tmp_path / 'emu68.hdf')
    resolved = resolve_driver(image, None, None, 'QDH0')
    try:
        assert resolved.to_json()['source'] == 'rdb'
        assert resolved.path.read_bytes() == BUNDLED_FFS.read_bytes()
    finally:
        if resolved.temporary:
            resolved.path.unlink()
