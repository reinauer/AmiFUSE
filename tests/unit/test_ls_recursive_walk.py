"""`ls --recursive` and `verify` end on any directory tree (#320).

The walk sends names from the disk as path components. Those are not
always safe: the handler reads `q:` as the root of the volume, which holds
`q:` again, and follows a hard link to an ancestor. `_FakeHandler` answers
as a handler does, and fails a walk that would never end.
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from amifuse import fuse_fs as fs

ST_USERDIR = 2
ST_SOFTLINK = 3
ST_LINKDIR = 4
ST_FILE = -3


def _entry(name, dir_type, size=0):
    return {"name": name, "dir_type": dir_type, "size": size, "protection": 0}


class _FakeHandler:
    """list_dir_path over a tree, with `aliases` naming another path."""

    def __init__(self, tree, aliases=()):
        self.tree = tree
        self.aliases = aliases
        self.calls = []

    def list_dir_path(self, path):
        self.calls.append(path)
        assert len(self.calls) < 50, f"the walk does not end: {self.calls[-3:]}"
        for prefix, target in self.aliases:
            if path == prefix or path.startswith(prefix + "/"):
                path = target + path[len(prefix):]
                path = path.replace("//", "/")
        return self.tree.get(path, [])


def _names(entries):
    return [ent["path"] for ent in entries]


@pytest.mark.parametrize("name", ["q:", "dh0:work", ":"])
def test_a_colon_directory_is_listed_once_and_left_alone(capsys, name):
    # Every name with a colon names the root.
    handler = _FakeHandler(
        {"/": [_entry(name, ST_USERDIR), _entry("f", ST_FILE, 5)]},
        aliases=[("/" + name, "/")],
    )

    entries = fs._ls_recursive(handler, "/")

    assert _names(entries) == ["/" + name, "/f"]
    assert handler.calls == ["/"]
    err = capsys.readouterr().err
    assert f"contents of /{name} not listed (a colon ends" in err


@pytest.mark.parametrize("name", ["/", "a/"])
def test_a_directory_name_with_a_slash_is_left_alone(capsys, name):
    # `locate_path` drops the empty components, so this names the parent.
    handler = _FakeHandler({"/": [_entry(name, ST_USERDIR)]},
                           aliases=[("/" + name.rstrip("/"), "/")])

    assert _names(fs._ls_recursive(handler, "/")) == ["/" + name]
    assert handler.calls == ["/"]
    assert "a slash separates" in capsys.readouterr().err


def test_a_hard_link_to_an_ancestor_is_not_followed(capsys):
    tree = {
        "/": [_entry("a", ST_USERDIR)],
        "/a": [_entry("b", ST_USERDIR)],
        "/a/b": [_entry("loop", ST_LINKDIR), _entry("f", ST_FILE, 5)],
    }
    handler = _FakeHandler(tree, aliases=[("/a/b/loop", "/a")])

    entries = fs._ls_recursive(handler, "/")

    assert _names(entries) == ["/a", "/a/b", "/a/b/loop", "/a/b/f"]
    assert handler.calls == ["/", "/a", "/a/b"]
    assert "/a/b/loop not listed (it is a hard link" in capsys.readouterr().err


def test_a_soft_link_is_not_followed(capsys):
    handler = _FakeHandler({"/": [_entry("up", ST_SOFTLINK)]},
                           aliases=[("/up", "/")])

    assert _names(fs._ls_recursive(handler, "/")) == ["/up"]
    assert handler.calls == ["/"]
    assert "/up not listed (it is a soft link" in capsys.readouterr().err


def test_the_walk_goes_on_past_a_directory_it_leaves_alone(capsys):
    tree = {
        "/": [_entry("q:", ST_USERDIR), _entry("S", ST_USERDIR)],
        "/S": [_entry("Startup-Sequence", ST_FILE, 9)],
    }
    handler = _FakeHandler(tree, aliases=[("/q:", "/")])

    entries = fs._ls_recursive(handler, "/")

    assert _names(entries) == ["/q:", "/S", "/S/Startup-Sequence"]
    assert handler.calls == ["/", "/S"]
    assert "contents of /S" not in capsys.readouterr().err


def test_verify_counts_the_volume_with_a_colon_directory(monkeypatch, capsys):
    handler = _FakeHandler(
        {"/": [_entry("q:", ST_USERDIR), _entry("f", ST_FILE, 5)]},
        aliases=[("/q:", "/")],
    )
    bridge = Mock(list_dir_path=handler.list_dir_path)
    bridge.is_mounted.return_value = fs.MountState(True, None, None)
    bridge.volume_name.return_value = "Test"
    monkeypatch.setattr(fs, "_create_bridge_from_args",
                        lambda *a, **kw: (bridge, None))
    monkeypatch.setattr(fs, "_cleanup_bridge", lambda *a: None)
    monkeypatch.setattr(fs, "_handler_has_crashed", lambda bridge: False)

    fs.cmd_verify(SimpleNamespace(image="disk.adf", json=False, file=None,
                                  expect_size=None))

    captured = capsys.readouterr()
    assert "Directories: 1" in captured.out
    assert "Files: 1" in captured.out
    assert "contents of /q: not listed" in captured.err
