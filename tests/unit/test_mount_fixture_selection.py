"""The mount/hash comparison must work with regenerated fixture contents."""

import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.integration import test_mount_lifecycle as lifecycle


@pytest.mark.parametrize("has_file", [True, False])
def test_hash_comparison_discovers_fixture_file(tmp_path, monkeypatch, has_file):
    content = b"regenerated fixture\n"
    filename = "renamed.txt"
    (tmp_path / filename).write_bytes(content)
    calls = []

    def run(*args, **kwargs):
        calls.append(args[0])
        if args[0] == "ls":
            entries = [{"name": "directory", "type": "dir"},
                       {"name": "disk.info", "type": "file"},
                       {"name": "unreadable", "type": "file", "protection_bits": 8}]
            if has_file:
                entries.append({"name": filename, "type": "file"})
            result = {"entries": entries}
        else:
            assert args[0] == "hash"
            assert args[args.index("--file") + 1] == filename
            result = {"hash": hashlib.sha256(content).hexdigest()}
        return SimpleNamespace(returncode=0, stdout=json.dumps(result), stderr="")

    def mount(*args, **kwargs):
        calls.append("mount")
        return None, tmp_path

    monkeypatch.setattr(lifecycle, "_run_amifuse", run)
    if has_file:
        lifecycle.test_file_read_matches_hash(mount, tmp_path / "fixture.hdf",
                                              tmp_path / "handler")
        assert calls == ["ls", "hash", "mount"]
    else:
        with pytest.raises(pytest.skip.Exception, match="readable regular file"):
            lifecycle.test_file_read_matches_hash(mount, tmp_path / "fixture.hdf",
                                                  tmp_path / "handler")
        assert calls == ["ls"]
