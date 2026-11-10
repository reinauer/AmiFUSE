"""Do not guess a rename source when open handles disagree."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from amifuse.fuse_fs import AmigaFuseFS


@pytest.mark.parametrize("entries", [
    {1: {"fh_addr": 1, "path": "/one", "opened_as": "/old"},
     2: {"dir_handle": True, "path": "/two", "opened_as": "/old"}},
    {1: {"fh_addr": 1, "path": "/one", "opened_as": "/old"},
     2: {"dir_handle": True, "path": "/old"}},
    {1: {"fh_addr": 1, "path": "/OLD"}},
])
def test_ambiguous_or_unrenamed_handle_keeps_source(monkeypatch, entries):
    fs = AmigaFuseFS(SimpleNamespace(_write_enabled=True,
                                    state=SimpleNamespace(crashed=False)))
    fs._winfsp = True
    fs._fh_cache = entries
    monkeypatch.setattr(fs, "_state_path", lambda path: path)
    rename = Mock(return_value=(-1, 0))
    monkeypatch.setattr(fs, "_rename_object", rename)
    assert fs.rename("/old", "/new") == 0
    rename.assert_called_once_with("/old", "/new")
